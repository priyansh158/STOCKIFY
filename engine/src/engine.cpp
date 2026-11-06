// The engine's event loop and handlers. Each function names the stockify.oracle.runner method it mirrors.
#include "stocksim/engine.hpp"

#include <algorithm>
#include <cctype>
#include <stdexcept>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"

namespace stocksim {

namespace {

std::int64_t int_or(const Json& obj, const char* key, std::int64_t fallback) {
    const auto it = obj.find(key);
    return it == obj.end() || it->is_null() ? fallback : it->get<std::int64_t>();
}

}  // namespace

// ── Construction ─────────────────────────────────────────────────────────────

Engine::Engine(Bundle bundle)
    : bundle_(std::move(bundle)),
      calendar_(require(bundle_.root, "days", "bundle")),
      instruments_(require(bundle_.root, "instruments", "bundle"), calendar_),
      ctx_(*this),
      strategy_rng_(bundle_.seed, kStrategyStream),
      labels_(bundle_.labels.begin(), bundle_.labels.end()) {
    const Json& pricing = require(bundle_.root, "pricing", "bundle");
    stale_after_ns_ = checked_mul(require_int(pricing, "stale_after_ms", "pricing"), 1'000'000);
    const Json& run = require(bundle_.root, "run", "bundle");
    fill_kind_ = require_string(require(run, "fill_model", "run"), "kind", "run.fill_model");
    last_day_end_ = calendar_.last_day_end();
}

void Engine::set_strategy(std::shared_ptr<Strategy> strategy) { strategy_ = std::move(strategy); }

void Engine::add_pack(std::shared_ptr<const PackReader> pack) {
    if (started_) {
        throw RunError("add_pack after the run started");
    }
    packs_.add(std::move(pack));
}

std::vector<std::string> Engine::drain_log() {
    std::vector<std::string> out(log_lines_.begin() + static_cast<std::ptrdiff_t>(log_drained_), log_lines_.end());
    log_drained_ = log_lines_.size();
    return out;
}

std::vector<std::string> Engine::labels() const { return {labels_.begin(), labels_.end()}; }

// ── Time and phases ──────────────────────────────────────────────────────────

std::string_view Engine::group_of(InstrumentId iid, std::optional<Date> d) const {  // runner.group_of
    return instruments_.group_of(iid, d ? *d : date_of(now_));
}

// ── The loop (runner.run; A4 §1.3, §1.4) ─────────────────────────────────────

void Engine::push_internal(InternalEvent ev) {  // runner.push
    ev.key.seq = ++internal_seq_;
    ev.key.src = 0;
    internal_.push(std::move(ev));
}

void Engine::schedule_days() {  // runner._schedule_days
    for (const DaySchedule& day : calendar_.days()) {
        for (const auto& [g, phases] : day.groups) {
            std::optional<TimeNs> close;
            for (const PhaseSpan& p : phases) {
                InternalEvent open{EventKey{p.start_ns, kRankPhase, 0, 0}, InternalKind::Phase, g, p.phase};
                push_internal(std::move(open));
                InternalEvent shut{EventKey{p.end_ns, kRankPhase, 0, 0}, InternalKind::Phase, g, "CLOSED"};
                push_internal(std::move(shut));
                if ((p.phase == "CONTINUOUS" || p.phase == "CAS") && (!close || p.end_ns > *close)) {
                    close = p.end_ns;
                }
            }
            if (close) {
                InternalEvent sc{EventKey{*close, kRankSegClose, 0, 0}, InternalKind::SegClose, g};
                sc.date = day.date;
                push_internal(std::move(sc));
            }
        }
        for (const auto& [seg, t] : day.mis_cutoff) {
            if (t && (seg == "CM" || seg == "FO")) {
                InternalEvent ms{EventKey{*t, kRankMisSqoff, 0, 0}, InternalKind::MisSqoff};
                ms.segment = seg;
                ms.date = day.date;
                push_internal(std::move(ms));
            }
        }
        InternalEvent de{EventKey{day.day_end_ns, kRankDayEnd, 0, 0}, InternalKind::DayEnd};
        de.date = day.date;
        push_internal(std::move(de));
    }
}

void Engine::start(std::optional<EventKey> first_input) {
    if (started_) {
        return;
    }
    if (!strategy_) {
        throw RunError("no strategy set");
    }
    started_ = true;
    schedule_days();
    // on_start sees the earliest time of the run.
    std::optional<TimeNs> first;
    if (!internal_.empty()) {
        first = internal_.top().key.t;
    }
    if (first_input && (!first || first_input->t < *first)) {
        first = first_input->t;
    }
    now_ = first.value_or(0);
    strategy_->on_start(ctx_);
    deliver();
}

void Engine::process_internal_before(const std::optional<EventKey>& next_input) {
    while (!internal_.empty() && (!next_input || internal_.top().key < *next_input)) {
        const InternalEvent ev = internal_.top();
        internal_.pop();
        if (ev.key.t > last_day_end_) {
            continue;  // timers after the last day never fire
        }
        now_ = ev.key.t;
        key_ = ev.key;
        activate_due(now_);
        dispatch_internal(ev);
    }
}

void Engine::run() { run(packs_); }

void Engine::run(EventSource& source) {
    start(source.peek_key());
    for (;;) {
        const std::optional<EventKey> next = source.peek_key();
        process_internal_before(next);
        if (!next) {
            break;
        }
        const Event* e = source.next();
        now_ = e->t;
        key_ = e->key();
        activate_due(now_);
        process_input(*e);
    }
}

void Engine::push(std::span<const std::byte> batch) {
    live_.push(batch);
    start(live_.peek_key());
    while (const std::optional<EventKey> next = live_.peek_key()) {
        process_internal_before(next);
        const Event* e = live_.next();
        now_ = e->t;
        key_ = e->key();
        activate_due(now_);
        process_input(*e);
    }
}

void Engine::finish() {
    start(std::nullopt);
    process_internal_before(std::nullopt);
}

void Engine::activate_due(TimeNs /*t*/) {
    // M3.5: PENDING orders whose t_activate has come activate here, in (t_activate, order_id) order.
}

// ── Logging and callbacks ────────────────────────────────────────────────────

void Engine::log(std::string_view level, std::string_view msg) {  // runner.log
    log_lines_.push_back(std::to_string(now_) + " " + std::string(level) + " " + std::string(msg));
}

void Engine::deliver() {  // runner._deliver
    if (delivering_) {
        return;
    }
    delivering_ = true;
    // M3.5: pop and deliver the queued on_order_update / on_fill callbacks in the order generated.
    delivering_ = false;
}

void Engine::track_open() {
    // M3.8: record (t, any position open) transitions for the exposure metric.
}

// ── Dispatch ─────────────────────────────────────────────────────────────────

void Engine::dispatch_internal(const InternalEvent& ev) {  // runner._dispatch_internal
    switch (ev.kind) {
        case InternalKind::Phase: on_phase(ev); break;
        case InternalKind::SegClose: seg_close(ev); break;
        case InternalKind::MisSqoff: mis_sqoff(ev); break;
        case InternalKind::DayEnd: day_end(ev); break;
        case InternalKind::Timer: on_timer(ev); break;
        case InternalKind::BarBuilt: on_built(ev); break;
    }
    track_open();
}

void Engine::process_input(const Event& e) {  // runner._dispatch_input
    switch (e.kind) {
        case EventKind::Bar:
        case EventKind::TickFull:
        case EventKind::TickQuote:
        case EventKind::Index:
            if (!instruments_.contains(e.instrument)) {
                throw RunError("pack record for instrument " + std::to_string(e.instrument) + " not in the bundle");
            }
            ++event_no_;
            on_market(e);
            break;
        case EventKind::Gap: on_gap(e); break;
        case EventKind::RefData: on_refdata(e); break;
        case EventKind::Settle: on_settle(e); break;
        case EventKind::Clock: break;  // only advances time (A4 §1.6)
    }
    deliver();
    track_open();
}

// ── Market data (A4 §1.5, §2) ────────────────────────────────────────────────

void Engine::on_market(const Event& e) {  // runner._on_market
    const InstrumentId iid = e.instrument;
    MarketState& m = market_[iid];
    const Date d = date_of(e.t);
    if (m.day != d) {
        m.day = d;
        m.s0.reset();
        m.s1.reset();
    }
    if (e.kind == EventKind::Bar) {
        const BarFields b = decode_bar(e);
        if (!m.min_interval || b.interval_s < *m.min_interval) {
            m.min_interval = b.interval_s;
        }
        m.bars[b.interval_s].push_back(BarRow{b.start_ns, b.open, b.high, b.low, b.close, b.volume, b.oi});
        if (b.interval_s == *m.min_interval) {
            // M3.6: match the instrument's eligible orders against the bar (bar or eod_close model).
            m.ltp = b.close;
            m.t = e.t;
            underlying_last_[iid] = b.close;
            // M3.5: DAY orders whose session's daily bar has now been used are cancelled (D-44).
        }
        // M3.5: IOC remainders are cancelled after their first eligible event (A4 §3.7).
        deliver();
        const auto sb = sub_bars_.find(iid);
        if (sb != sub_bars_.end() && sb->second.count(b.interval_s) != 0) {
            strategy_->on_bar(ctx_, Bar{iid, b.interval_s, b.start_ns, e.t, b.open, b.high, b.low, b.close, b.volume,
                                        b.oi, false});
            deliver();
        }
        return;
    }
    if (e.kind == EventKind::Index) {
        const IndexFields f = decode_index(e);
        m.ltp = f.value;
        m.t = e.t;
        underlying_last_[iid] = f.value;
        build_bars(iid, e.t, f.value, std::nullopt, std::nullopt);
        if (sub_ticks_.count(iid) != 0) {
            strategy_->on_tick(ctx_, Tick{iid, e.t, f.value, 0, 0, std::nullopt, std::nullopt, false});
            deliver();
        }
        return;
    }
    const TickFields f = decode_tick(e);
    const QuoteFields& q = f.quote;
    m.s0 = m.s1;
    m.s1 = Snapshot{e.t, q.ltp, q.volume, q.atp, q.high, q.low};
    m.ltp = q.ltp;
    m.t = e.t;
    if (f.full) {
        std::array<DepthLevel, 10> book{};
        for (std::size_t i = 0; i < 5; ++i) {
            book[i] = f.bids[i];
            book[5 + i] = f.asks[i];
        }
        m.book = book;
        m.bid = book[0].price != 0 ? std::optional<Paise>(book[0].price) : std::nullopt;
        m.ask = book[5].price != 0 ? std::optional<Paise>(book[5].price) : std::nullopt;
    }
    if ((fill_kind_ == "snapshot_ltp" || fill_kind_ == "snapshot_quote") &&
        !(fill_kind_ == "snapshot_quote" && e.kind != EventKind::TickFull)) {
        match_snapshot(iid, m);
    }
    underlying_last_[iid] = q.ltp;
    const std::optional<Qty> prev_vol = m.s0 ? std::optional<Qty>(m.s0->volume) : std::nullopt;
    build_bars(iid, e.t, q.ltp, q.volume, prev_vol);
    // M3.5: IOC remainders are cancelled after their first eligible event (A4 §3.7).
    deliver();
    if (sub_ticks_.count(iid) != 0) {
        const MarketState& mm = market_[iid];
        strategy_->on_tick(ctx_, Tick{iid, e.t, q.ltp, q.volume, q.atp, mm.bid, mm.ask, false});
        deliver();
    }
}

std::optional<Evidence> Engine::evidence(const MarketState& m) {  // runner._evidence (A4 §2.1)
    if (!m.s0 || !m.s1) {
        return std::nullopt;
    }
    const Snapshot& s0 = *m.s0;
    const Snapshot& s1 = *m.s1;
    Evidence ev;
    ev.dv = s1.volume - s0.volume;
    if (ev.dv < 0) {
        log("WARN", "day volume decreased; treated as 0");
        ev.dv = 0;
    }
    ev.lo = std::min(s0.ltp, s1.ltp);
    ev.hi = std::max(s0.ltp, s1.ltp);
    if (s1.low < s0.low) {
        ev.lo = std::min(ev.lo, s1.low);
    }
    if (s1.high > s0.high) {
        ev.hi = std::max(ev.hi, s1.high);
    }
    if (ev.dv > 0) {
        const Int128 value = Int128::mul(s1.atp, s1.volume) - Int128::mul(s0.atp, s0.volume);
        if (Int128::mul(ev.lo, ev.dv) <= value && value <= Int128::mul(ev.hi, ev.dv)) {
            ev.value = value;
        }
    }
    ev.t0 = s0.t;
    ev.t1 = s1.t;
    return ev;
}

void Engine::match_snapshot(InstrumentId iid, MarketState& m) {  // runner._match_snapshot
    const Spec* spec = instruments_.spec_on(iid, date_of(now_));
    if (spec == nullptr) {
        throw RunError("instrument " + std::to_string(iid) + " has no spec on " + iso_date(date_of(now_)));
    }
    const std::optional<Evidence> ev = evidence(m);
    // M3.6: trigger and fill the instrument's eligible orders (snapshot_ltp / snapshot_quote, A4 §4.2–§4.3).
    (void)ev;
}

void Engine::build_bars(InstrumentId iid, TimeNs t, Paise price, std::optional<Qty> vol,
                        std::optional<Qty> prev_vol) {  // runner._build_bars (A4 §2.2)
    const auto sb = sub_bars_.find(iid);
    if (sb == sub_bars_.end()) {
        return;
    }
    for (const std::int32_t isec : sb->second) {  // std::set: ascending, as sorted() in the oracle
        if (market_[iid].min_interval) {
            continue;  // the pack has real bars for it
        }
        const Date d = date_of(t);
        const std::optional<TimeNs> cs = calendar_.continuous_start(group_of(iid, d), d);
        if (!cs || t < *cs) {
            continue;
        }
        const TimeNs step = checked_mul(isec, kNsPerSecond);
        const TimeNs start = *cs + floor_div(t - *cs, step) * step;
        auto& acc = built_[{iid, isec}];
        // dv = vol if prev_vol is None else (max(0, vol - prev_vol) if vol is not None else 0); None counts as 0.
        std::int64_t dv = 0;
        if (!prev_vol) {
            dv = vol.value_or(0);
        } else if (vol) {
            dv = std::max<std::int64_t>(0, *vol - *prev_vol);
        }
        const auto it = acc.find(start);
        if (it == acc.end()) {
            acc[start] = {price, price, price, price, dv, 0};
            InternalEvent ev{EventKey{start + step, kRankBarBuilt, 0, 0}, InternalKind::BarBuilt};
            ev.instrument = iid;
            ev.interval_s = isec;
            ev.start = start;
            push_internal(std::move(ev));
        } else {
            auto& b = it->second;
            b[1] = std::max(b[1], price);
            b[2] = std::min(b[2], price);
            b[3] = price;
            b[4] += dv;
        }
    }
}

void Engine::on_built(const InternalEvent& ev) {  // runner._on_built
    const auto acc = built_.find({ev.instrument, ev.interval_s});
    if (acc == built_.end()) {
        return;
    }
    const auto it = acc->second.find(ev.start);
    if (it == acc->second.end()) {
        return;
    }
    const std::array<std::int64_t, 6> b = it->second;
    acc->second.erase(it);
    market_[ev.instrument].bars[ev.interval_s].push_back(BarRow{ev.start, b[0], b[1], b[2], b[3], b[4], b[5]});
    strategy_->on_bar(ctx_, Bar{ev.instrument, ev.interval_s, ev.start, now_, b[0], b[1], b[2], b[3], b[4], b[5], true});
    deliver();
}

void Engine::on_gap(const Event& e) {  // runner._on_gap
    const GapFields g = decode_gap(e);
    if (g.scope == 1) {
        const auto it = market_.find(e.instrument);
        if (it != market_.end()) {
            it->second.s0.reset();
            it->second.s1.reset();
        }
    } else {
        for (auto& [iid, m] : market_) {
            m.s0.reset();
            m.s1.reset();
        }
    }
    log("INFO", "GAP " + std::string(gap_reason_name(g.reason)) + (g.phase == 0 ? " start" : " end") + " scope " +
                    std::to_string(e.instrument));
}

void Engine::on_refdata(const Event& e) {  // runner._on_refdata
    const RefDataFields r = decode_refdata(e);
    if (refdata_subtype_name(r.subtype) == "price_band") {
        const Json payload = Json::parse(r.json.begin(), r.json.end());
        market_[e.instrument].band = std::make_pair(payload.at("lower_paise").get<Paise>(),
                                                    payload.at("upper_paise").get<Paise>());
    }
}

void Engine::on_settle(const Event& e) {  // runner._on_settle
    const SettleFields s = decode_settle(e);
    if (s.subtype == 1) {
        // M3.8: daily settlement price: variation-settle open futures positions in this contract.
        return;
    }
    fsp_seen_[{e.instrument, s.date}] = s.price;
    // M3.8: final settlement of expiring contracts at this FSP (A4 §7.2).
}

// ── Internal events ──────────────────────────────────────────────────────────

void Engine::on_phase(const InternalEvent& ev) {  // runner._on_phase
    // M3.5: at the CAS phase start with cas_policy none, open orders in CM_CAS stocks are cancelled (R_CAS).
    strategy_->on_session_phase(ctx_, PhaseChange{ev.group, ev.phase, now_});
    deliver();
}

void Engine::seg_close(const InternalEvent& /*ev*/) {  // runner._seg_close
    // M3.5: DAY orders of this group expire (EOD). M3.8: provisional marks of open futures.
    deliver();
}

void Engine::mis_sqoff(const InternalEvent& /*ev*/) {  // runner._mis_sqoff
    // M3.8: AUTO_SQOFF market orders for this segment's open MIS positions.
    deliver();
}

void Engine::day_end(const InternalEvent& /*ev*/) {  // runner._day_end
    // M3.8: expiry fallback, positions_eod rows, the day's NAV row.
    strategy_->on_session_end(ctx_);
    deliver();
    // M3.8: CNC sale proceeds held back on earlier days become usable.
}

void Engine::on_timer(const InternalEvent& ev) {  // runner._on_timer
    const auto it = timers_.find(ev.timer);
    if (it == timers_.end()) {
        return;
    }
    const auto [t, period] = it->second;
    if (period != 0) {
        it->second = {t + period, period};
        InternalEvent next{EventKey{t + period, kRankTimer, 0, 0}, InternalKind::Timer};
        next.timer = ev.timer;
        push_internal(std::move(next));
    } else {
        timers_.erase(it);
    }
    strategy_->on_timer(ctx_, ev.timer);
    deliver();
}

// ── Context (oracle/context.py) ──────────────────────────────────────────────

TimeNs Context::now() const { return e_.now_; }

Date Context::today() const { return e_.date_of(e_.now_); }

std::string Context::phase(InstrumentId iid) const {
    return std::string(e_.phase_at(e_.group_of(iid), e_.now_));
}

void Context::subscribe_ticks(InstrumentId iid) { e_.sub_ticks_.insert(iid); }

void Context::subscribe_bars(InstrumentId iid, std::int32_t interval_s) { e_.sub_bars_[iid].insert(interval_s); }

std::optional<Tick> Context::last_tick(InstrumentId iid) const {
    const auto it = e_.market_.find(iid);
    if (it == e_.market_.end() || !it->second.ltp) {
        return std::nullopt;
    }
    const MarketState& m = it->second;
    return Tick{iid,
                m.t.value_or(0),
                *m.ltp,
                m.s1 ? m.s1->volume : 0,
                m.s1 ? m.s1->atp : 0,
                m.bid,
                m.ask,
                stale(iid)};
}

const std::array<DepthLevel, 10>* Context::depth(InstrumentId iid) const {
    const auto it = e_.market_.find(iid);
    return it != e_.market_.end() && it->second.book ? &*it->second.book : nullptr;
}

bool Context::stale(InstrumentId iid) const {
    const auto it = e_.market_.find(iid);
    if (it == e_.market_.end() || !it->second.t) {
        return true;
    }
    return e_.now_ - *it->second.t > e_.stale_after_ns_;
}

std::vector<Bar> Context::bars(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const {
    std::vector<Bar> out;
    const auto it = e_.market_.find(iid);
    if (it == e_.market_.end()) {
        return out;
    }
    const auto rows_it = it->second.bars.find(interval_s);
    if (rows_it == it->second.bars.end()) {
        return out;
    }
    const auto& rows = rows_it->second;
    const auto size = static_cast<std::int64_t>(rows.size());
    // Python rows[-n:]: the last n for n > 0, all for n == 0, rows[|n|:] for n < 0.
    const std::int64_t first = n > 0 ? std::max<std::int64_t>(0, size - n) : (n == 0 ? 0 : std::min(size, -n));
    for (std::int64_t i = first; i < size; ++i) {
        const BarRow& r = rows[static_cast<std::size_t>(i)];
        out.push_back(Bar{iid, interval_s, r.start, r.start + static_cast<TimeNs>(interval_s) * kNsPerSecond, r.open,
                          r.high, r.low, r.close, r.volume, r.oi, false});
    }
    return out;
}

std::uint64_t Context::schedule_at(TimeNs t) {
    if (t <= e_.now_) {
        throw std::invalid_argument("schedule_at needs a time after now()");
    }
    const std::uint64_t tid = e_.next_timer_++;
    e_.timers_[tid] = {t, 0};
    InternalEvent ev{EventKey{t, kRankTimer, 0, 0}, InternalKind::Timer};
    ev.timer = tid;
    e_.push_internal(std::move(ev));
    return tid;
}

std::uint64_t Context::schedule_every(TimeNs period, TimeNs first) {
    if (first <= e_.now_ || period <= 0) {
        throw std::invalid_argument("schedule_every needs first_ns after now() and a positive period");
    }
    const std::uint64_t tid = e_.next_timer_++;
    e_.timers_[tid] = {first, period};
    InternalEvent ev{EventKey{first, kRankTimer, 0, 0}, InternalKind::Timer};
    ev.timer = tid;
    e_.push_internal(std::move(ev));
    return tid;
}

void Context::cancel_timer(std::uint64_t timer_id) { e_.timers_.erase(timer_id); }

const Json& Context::params() const { return e_.bundle_.strategy.params; }

void Context::log(std::string_view level, std::string_view msg) {
    std::string upper(level);
    for (char& c : upper) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    e_.log(upper, msg);
}

Pcg32& Context::rng() { return e_.strategy_rng_; }

std::optional<InstrumentId> Context::resolve(std::string_view symbol) const {
    // symbol.rpartition(":"): the exchange is everything before the last ':', or empty.
    const std::size_t colon = symbol.rfind(':');
    const std::string_view exchange = colon == std::string_view::npos ? std::string_view() : symbol.substr(0, colon);
    const std::string_view name = colon == std::string_view::npos ? symbol : symbol.substr(colon + 1);
    for (const Instrument& r : e_.instruments_.rows()) {
        if (r.tradingsymbol == name && (exchange.empty() || r.exchange == exchange)) {
            return r.id;
        }
    }
    return std::nullopt;
}

const Spec* Context::spec(InstrumentId iid) const { return e_.instruments_.spec_on(iid, e_.date_of(e_.now_)); }

}  // namespace stocksim
