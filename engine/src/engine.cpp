// The engine's event loop and handlers. Each function names the stockify.oracle.runner method it mirrors.
#include "stocksim/engine.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>

#include "stocksim/errors.hpp"
#include "stocksim/indicators.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/margin/spanlite.hpp"
#include "stocksim/pricing/black76.hpp"

namespace stocksim {

namespace {

const Json& run_of(const Bundle& b) { return require(b.root, "run", "bundle"); }

}  // namespace

namespace {
double rate_on(const Json& bundle_root, const std::string& iso_day);  // defined with the chain view below
}  // namespace

// ── Construction ─────────────────────────────────────────────────────────────

Engine::Engine(Bundle bundle)
    : bundle_(std::move(bundle)),
      calendar_(require(bundle_.root, "days", "bundle")),
      instruments_(require(bundle_.root, "instruments", "bundle"), calendar_),
      costs_(require(bundle_.root, "charges", "bundle"), require(run_of(bundle_), "costs", "run")),
      ctx_(*this),
      strategy_rng_(bundle_.seed, kStrategyStream),
      book_(Ledger(require_int(run_of(bundle_), "capital_paise", "run"), require_int(run_of(bundle_), "capital_paise", "run")),
            Pcg32(bundle_.seed, kSlippageStream)),
      labels_(bundle_.labels.begin(), bundle_.labels.end()) {
    const Json& pricing = require(bundle_.root, "pricing", "bundle");
    stale_after_ns_ = checked_mul(require_int(pricing, "stale_after_ms", "pricing"), 1'000'000);
    const Json& run = run_of(bundle_);
    fill_cfg_ = require(run, "fill_model", "run");
    fill_kind_ = require_string(fill_cfg_, "kind", "run.fill_model");
    policies_ = require(run, "policies", "run");
    mark_kind_ = require_string(run, "mark", "run");
    latency_ns_ = checked_mul(require_int(run, "latency_ms", "run"), 1'000'000);
    last_day_end_ = calendar_.last_day_end();
    const Json& margin_cfg = require(run, "margin", "run");
    if (require_string(margin_cfg, "model", "run.margin") == "spanlite") {
        margin_ = std::make_unique<SpanLiteMargin>(
            bundle_.root,
            SpanLiteHost{[this] { return now_; },
                         [this](const std::string& und, const std::string& expiry) { return ctx_.chain(und, expiry); },
                         [this](InstrumentId iid, Date d) { return spec(iid, d); },
                         [this](const std::string& label) { labels_.insert(label); },
                         [this](const std::string& d) { return rate_on(bundle_.root, d); }});
    } else {
        margin_ = std::make_unique<FlatMargin>(bundle_.root);
    }
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

Outputs Engine::drain_outputs() {
    Outputs out;
    for (auto it = book_.orders.lower_bound(orders_drained_); it != book_.orders.end(); ++it) {
        out.orders.push_back(it->first);
        orders_drained_ = it->first + 1;
    }
    out.events_from = events_drained_;
    out.events_to = events_drained_ = book_.order_events.size();
    out.fills_from = fills_drained_;
    out.fills_to = fills_drained_ = book_.fills.size();
    out.charges_from = charges_drained_;
    out.charges_to = charges_drained_ = book_.charges.size();
    out.log = drain_log();
    return out;
}

std::vector<std::string> Engine::labels() const {  // the run's labels and the margin model's (D-60), sorted
    std::set<std::string> all = labels_;
    all.insert(margin_->used_labels().begin(), margin_->used_labels().end());
    return {all.begin(), all.end()};
}

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

class Engine::Running {
 public:
    Running(Engine& e, const char* what) : e_(e) {
        e.ensure_idle(what);
        if (e.busy_.exchange(true)) {  // another thread got in between
            throw RunError(std::string(what) + ": the engine is busy (a run, push or finish is in progress)");
        }
    }
    ~Running() {
        if (!done_) {
            e_.failed_ = true;  // an exception is leaving: the engine's state is no longer trustworthy
        }
        e_.busy_ = false;
    }
    Running(const Running&) = delete;
    Running& operator=(const Running&) = delete;
    void done() { done_ = true; }

 private:
    Engine& e_;
    bool done_ = false;
};

void Engine::ensure_idle(const char* what, bool allow_failed) const {
    if (busy_.load()) {
        throw RunError(std::string(what) + ": the engine is busy (a run, push or finish is in progress)");
    }
    if (failed_ && !allow_failed) {
        throw RunError(std::string(what) + ": this engine's run failed earlier; start a new engine");
    }
}

void Engine::run() { run(packs_); }

void Engine::run(EventSource& source) {
    Running guard(*this, "run");
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
    close_out();
    guard.done();
}

void Engine::push(std::span<const std::byte> batch) {
    Running guard(*this, "push");
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
    guard.done();
}

void Engine::finish() {
    Running guard(*this, "finish");
    start(std::nullopt);
    process_internal_before(std::nullopt);
    close_out();
    guard.done();
}

// The final close-out (A4 §1.10, D-52; the end of runner.run): when the last input came after the last trading
// day's DAY_END, one more DAY_END at that moment, dated by its own IST date. Once per run.
void Engine::close_out() {
    if (closed_out_ || now_ <= last_day_end_) {
        return;
    }
    closed_out_ = true;
    InternalEvent ev;
    ev.key = EventKey{now_, kRankDayEnd, 0, ++internal_seq_};
    ev.kind = InternalKind::DayEnd;
    ev.date = date_of(now_);
    key_ = ev.key;
    activate_due(now_);
    dispatch_internal(ev);
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
    try {
        while (!book_.callbacks.empty()) {
            const Callback cb = book_.callbacks.front();
            book_.callbacks.pop_front();
            if (!cb.is_fill) {
                // The order as it is now (OrderView.of at delivery), not as it was when the callback was queued.
                strategy_->on_order_update(ctx_, view_of(book_.orders.at(cb.order_id)));
            } else {
                const Fill& f = cb.fill;
                strategy_->on_fill(ctx_, FillView{f.order_id, f.instrument, std::string(to_string(f.side)), f.qty,
                                                  f.price, f.t_ns, f.evidence});
            }
        }
    } catch (...) {
        delivering_ = false;
        throw;
    }
    delivering_ = false;
}

void Engine::track_open() {  // runner._track_open: (t, any position open) transitions for the exposure metric
    bool is_open = false;
    for (const Position& p : book_.ledger.positions()) {
        if (p.q != 0) {
            is_open = true;
            break;
        }
    }
    const bool was_open = open_changes_.empty() ? false : open_changes_.back().second;
    if (was_open != is_open) {
        open_changes_.emplace_back(now_, is_open);
    }
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
        m.oi = b.oi;
        if (b.interval_s == *m.min_interval) {
            if (fill_kind_ == "bar") {
                match_bar(iid, b, e.t);
            } else if (fill_kind_ == "eod_close") {
                match_eod(iid, b, e.t);
            }
            MarketState& mm = market_[iid];  // matching never adds instruments, but read it fresh
            mm.ltp = b.close;
            mm.t = e.t;
            underlying_last_[iid] = b.close;
            const Date bar_day = date_of(b.start_ns);
            for (const OrderId id : live_orders()) {  // D-44: the session's daily bar has now been used
                Order& o = book_.orders.at(id);
                if (o.instrument == iid && o.eod_after && *o.eod_after <= bar_day) {
                    cancel_in(book_, o, Reason::Eod);
                }
            }
        }
        after_market(iid);
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
        m.oi = f.oi;
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
    after_market(iid);
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
    if (s.subtype == 1) {  // the daily settlement price of a contract: variation-settle its open futures
        for (Position& p : book_.ledger.positions()) {
            if (p.instrument == e.instrument && p.q != 0 && p.style == CashStyle::Variation &&
                instruments_.at(e.instrument).kind == "FUT") {
                book_.ledger.settle_variation(p, s.price, "settle");
            }
        }
        return;
    }
    fsp_seen_[{e.instrument, s.date}] = s.price;
    settle_final(e.instrument, s.date, s.price, "settle");
}

// ── Internal events ──────────────────────────────────────────────────────────

void Engine::on_phase(const InternalEvent& ev) {  // runner._on_phase
    if (ev.group == "CM_CAS" && ev.phase == "CAS" && policies_.at("cas").get<std::string>() == "none") {
        const Date d = date_of(now_);
        for (auto& [id, o] : book_.orders) {  // all orders, in id order
            if (o.live() && group_of(o.instrument, d) == "CM_CAS") {
                cancel_in(book_, o, Reason::Cas);
            }
        }
    }
    strategy_->on_session_phase(ctx_, PhaseChange{ev.group, ev.phase, now_});
    deliver();
}

void Engine::seg_close(const InternalEvent& ev) {  // runner._seg_close
    const Date d = ev.date;
    for (const OrderId id : live_orders()) {
        Order& o = book_.orders.at(id);
        if (o.validity == Validity::Day && group_of(o.instrument, d) == ev.group && o.trade_date.value_or(d) <= d) {
            if (o.status == OrderStatus::Pending && o.t_activate > now_) {
                continue;  // queued for a later session
            }
            if (daily_bars(o.instrument)) {
                o.eod_after = d;  // D-44: cancelled once today's daily bar is matched
                continue;
            }
            cancel_in(book_, o, Reason::Eod);
        }
    }
    if (ev.group == "FO_FUT" || ev.group == "FO_OPT") {  // provisional marks of open futures (A4 §6.4)
        for (Position& p : book_.ledger.positions()) {
            const Instrument& row = instruments_.at(p.instrument);
            if (p.q != 0 && p.style == CashStyle::Variation && row.session_group == ev.group && row.kind == "FUT") {
                if (const std::optional<Paise> m = mark_price(p.instrument)) {
                    book_.ledger.settle_variation(p, *m, "ltp");
                }
            }
        }
    }
    deliver();
}

void Engine::mis_sqoff(const InternalEvent& ev) {  // runner._mis_sqoff
    std::vector<std::pair<std::pair<InstrumentId, std::string>, std::size_t>> keyed;  // sorted(positions.items())
    const auto& ps = book_.ledger.positions();
    for (std::size_t i = 0; i < ps.size(); ++i) {
        keyed.push_back({{ps[i].instrument, std::string(to_string(ps[i].product))}, i});
    }
    std::sort(keyed.begin(), keyed.end());
    for (const auto& [key, i] : keyed) {
        const Position p = book_.ledger.positions()[i];  // a copy: place() may add positions
        if (p.product != Product::MIS || p.q == 0) {
            continue;
        }
        const std::string_view g = group_of(p.instrument, ev.date);
        if ((g.substr(0, 2) == "FO" ? "FO" : "CM") != ev.segment) {
            continue;
        }
        PlaceRequest r;
        r.instrument = p.instrument;
        r.side = p.d > 0 ? "SELL" : "BUY";
        r.qty = p.q;
        r.product = "MIS";
        r.type = "MARKET";
        r.tag = "AUTO_SQOFF";
        place(r, 0, true);
    }
    deliver();
}

void Engine::day_end(const InternalEvent& ev) {  // runner._day_end
    const Date d = ev.date;
    // Expiry fallback (A4 §7.2): no FSP by DAY_END.
    for (std::size_t i = 0; i < book_.ledger.positions().size(); ++i) {
        const Position& p = book_.ledger.positions()[i];
        const Instrument& row = instruments_.at(p.instrument);
        if (p.q != 0 && row.is_derivative() && row.last_day() == d) {
            const std::optional<InstrumentId> uid = row.underlying_id;
            std::optional<Paise> last;
            if (uid && *uid != 0) {
                const auto it = underlying_last_.find(*uid);
                if (it != underlying_last_.end()) {
                    last = it->second;
                }
            }
            if (!last) {
                throw RunError((row.tradingsymbol.empty() ? std::to_string(p.instrument) : row.tradingsymbol) +
                               ": expired on " + iso_date(d) + " with no FSP and no underlying price");
            }
            labels_.insert("settlement_fallback");
            settle_final(*uid, d, *last, "fallback");
        }
    }
    Ledger& led = book_.ledger;
    std::vector<std::pair<std::pair<InstrumentId, std::string>, std::size_t>> keyed;  // sorted(positions.items())
    for (std::size_t i = 0; i < led.positions().size(); ++i) {
        keyed.push_back({{led.positions()[i].instrument, std::string(to_string(led.positions()[i].product))}, i});
    }
    std::sort(keyed.begin(), keyed.end());
    for (const auto& [key, i] : keyed) {
        const Position& p = led.positions()[i];
        if (p.q == 0) {
            continue;
        }
        std::optional<Paise> m = mark_price(p.instrument);
        const std::string src = m ? mark_kind_ : "last";
        if (!m) {
            m = p.last_mark ? *p.last_mark : p.avg_price();
        }
        const Paise unreal = p.d * checked_sub(checked_mul(p.q, *m), p.c);
        positions_eod_.push_back(PositionEodRow{d, p.instrument, p.product, p.qty(), p.avg_price(), *m, src, unreal,
                                                p.realised});
    }
    const Paise nav = led.nav([this](InstrumentId i) { return mark_price(i); });
    const Paise blk = blocked(led, d);
    equity_.push_back(EquityRow{d, nav, led.cash, blk, nav - led.capital + led.charges_total, led.charges_total});
    if (!margin_->daily_model().empty()) {  // SPAN-lite's per-underlying day rows (M4.8)
        std::vector<MarginLeg> legs;
        for (const Position& p : led.positions()) {
            if (p.q != 0 && p.product == Product::NRML) {
                legs.push_back(MarginLeg{p.instrument, p.product, p.qty()});
            }
        }
        for (const UnderlyingMargin& b :
             margin_->breakdown(legs, instruments_, [this](InstrumentId i) { return ref_price(i); }, iso_date(d))) {
            margin_daily_.push_back(MarginDailyRow{d, b.underlying, std::string(margin_->daily_model()), b.scan,
                                                   b.somc, b.elm, b.addons, b.total()});
        }
    }
    strategy_->on_session_end(ctx_);
    deliver();
    book_.ledger.release_day(next_date(d));
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
    // n < 0: start at |n|, but -n overflows for INT64_MIN; any |n| at or past the end is the end (M3 review).
    const std::int64_t first =
        n > 0 ? std::max<std::int64_t>(0, size - n) : (n == 0 ? 0 : (n < -size ? size : -n));
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

const Json& Context::instrument_rows() const { return require(e_.bundle_.root, "instruments", "bundle"); }

namespace {

std::vector<Paise> closes_of(const std::map<InstrumentId, MarketState>& market, InstrumentId iid, std::int32_t isec) {
    std::vector<Paise> out;
    const auto it = market.find(iid);
    if (it != market.end()) {
        const auto rows = it->second.bars.find(isec);
        if (rows != it->second.bars.end()) {
            for (const BarRow& r : rows->second) {
                out.push_back(r.close);
            }
        }
    }
    return out;
}

}  // namespace

double Context::sma(InstrumentId iid, std::int32_t isec, std::int64_t n) const {
    return stocksim::sma(closes_of(e_.market_, iid, isec), n);
}

double Context::ema(InstrumentId iid, std::int32_t isec, std::int64_t n) const {
    return stocksim::ema(closes_of(e_.market_, iid, isec), n);
}

double Context::rsi(InstrumentId iid, std::int32_t isec, std::int64_t n) const {
    return stocksim::rsi(closes_of(e_.market_, iid, isec), n);
}

double Context::atr(InstrumentId iid, std::int32_t isec, std::int64_t n) const {
    std::vector<std::array<Paise, 3>> hlc;
    const auto it = e_.market_.find(iid);
    if (it != e_.market_.end()) {
        const auto rows = it->second.bars.find(isec);
        if (rows != it->second.bars.end()) {
            for (const BarRow& r : rows->second) {
                hlc.push_back({r.high, r.low, r.close});
            }
        }
    }
    return stocksim::atr(hlc, n);
}

double Context::session_vwap(InstrumentId iid) const {  // context.session_vwap: bars only for now
    const auto it = e_.market_.find(iid);
    if (it == e_.market_.end() || it->second.bars.empty()) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const Date d = e_.date_of(e_.now_);
    const std::optional<TimeNs> cs = e_.calendar_.continuous_start(e_.group_of(iid, d), d);
    const auto& rows = it->second.bars.begin()->second;  // the smallest interval (min(m.bars))
    Int128 pv = 0, v = 0;
    for (const BarRow& r : rows) {
        if (cs && r.start >= *cs && e_.date_of(r.start) == d) {
            pv += Int128::mul(floor_div(checked_add(checked_add(r.high, r.low), r.close), 3), r.volume);
            v += r.volume;
        }
    }
    return v.is_zero() ? std::numeric_limits<double>::quiet_NaN() : pv.to_double() / v.to_double();
}

std::vector<Date> Context::expiries(std::string_view underlying, std::string_view kind) const {
    std::string k(kind);
    for (char& c : k) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    const Date today = e_.date_of(e_.now_);
    std::set<Date> out;
    for (const Instrument& r : e_.instruments_.rows()) {
        const bool kind_ok = k == "OPT" ? r.is_option() : r.kind == k;
        if (r.underlying == std::optional<std::string>(std::string(underlying)) && kind_ok && r.expiry &&
            r.last_day() >= today) {
            out.insert(*r.expiry);
        }
    }
    return {out.begin(), out.end()};
}

std::vector<InstrumentId> Context::futures_chain(std::string_view underlying) const {
    std::vector<const Instrument*> rows;
    for (const Instrument& r : e_.instruments_.rows()) {
        if (r.underlying == std::optional<std::string>(std::string(underlying)) && r.kind == "FUT") {
            rows.push_back(&r);
        }
    }
    std::stable_sort(rows.begin(), rows.end(), [](const Instrument* a, const Instrument* b) {
        return a->expiry.value_or(0) < b->expiry.value_or(0);
    });
    std::vector<InstrumentId> out;
    for (const Instrument* r : rows) {
        out.push_back(r->id);
    }
    return out;
}

namespace {

// pricing.risk_free in force on an IST date (rate_ppb → decimal), as the oracle's black76.rate_on; 0 if none.
double rate_on(const Json& bundle_root, const std::string& iso_day) {
    const auto pricing = bundle_root.find("pricing");
    if (pricing == bundle_root.end() || !pricing->contains("risk_free")) {
        return 0.0;
    }
    const Json* best = nullptr;
    for (const Json& row : pricing->at("risk_free")) {
        const std::string& from = row.at("effective_from").get_ref<const std::string&>();
        if (from <= iso_day && (best == nullptr || from > best->at("effective_from").get_ref<const std::string&>())) {
            best = &row;
        }
    }
    return best != nullptr ? static_cast<double>(best->at("rate_ppb").get<std::int64_t>()) / 1'000'000'000 : 0.0;
}

}  // namespace

std::optional<double> Context::quote_mid(InstrumentId iid) const {
    const auto it = e_.market_.find(iid);
    if (it == e_.market_.end() || !it->second.bid || !it->second.ask) {
        return std::nullopt;
    }
    return static_cast<double>(*it->second.bid + *it->second.ask) / 2;
}

Context::ChainView Context::chain(std::string_view underlying, std::string_view expiry) const {  // context.chain
    if (e_.chain_key_ != e_.key_) {
        e_.chain_key_ = e_.key_;
        e_.chain_cache_.clear();
    }
    const auto k = std::make_pair(std::string(underlying), std::string(expiry));
    auto it = e_.chain_cache_.find(k);
    if (it == e_.chain_cache_.end()) {
        it = e_.chain_cache_.emplace(k, compute_chain(underlying, expiry)).first;
    }
    return it->second;
}

Context::ChainView Context::compute_chain(std::string_view underlying, std::string_view expiry) const {
    ChainView view;
    std::optional<Date> want;
    try {
        want = parse_iso_date(expiry);
    } catch (const FormatError&) {
        return view;  // the oracle compares strings: a non-date matches nothing
    }
    const std::optional<std::string> und{std::string(underlying)};
    std::vector<const Instrument*> rows;
    for (const Instrument& r : e_.instruments_.rows()) {
        if (r.underlying == und && r.expiry == want && r.is_option()) {
            rows.push_back(&r);
        }
    }
    std::stable_sort(rows.begin(), rows.end(), [](const Instrument* a, const Instrument* b) {
        const Paise sa = a->strike.value_or(0), sb = b->strike.value_or(0);
        return sa != sb ? sa < sb : a->kind < b->kind;
    });
    if (rows.empty()) {
        return view;
    }
    const double t = pricing::year_fraction(e_.now_, rows.front()->last_trading_ns.value_or(e_.now_));
    const double r = rate_on(e_.bundle_.root, iso_date(e_.date_of(e_.now_)));
    const double disc = std::exp(-r * t);

    // The forward (C-13): the same-expiry future's mid; else the synthetic median; else the index value.
    for (const Instrument& fut : e_.instruments_.rows()) {
        if (fut.underlying == und && fut.expiry == want && fut.kind == "FUT") {
            if (const auto mid = quote_mid(fut.id)) {
                view.forward = *mid;
                view.forward_source = "future";
                break;
            }
        }
    }
    if (!view.forward) {
        std::map<Paise, std::pair<std::optional<double>, std::optional<double>>> sides;  // strike → (CE, PE) mids
        for (const Instrument* row : rows) {
            const auto mid = quote_mid(row->id);
            if (mid && !stale(row->id)) {
                auto& s = sides[row->strike.value_or(0)];
                (row->kind == "CE" ? s.first : s.second) = *mid;
            }
        }
        struct Pair {
            double gap;
            Paise strike;
            double c, p;
        };
        std::vector<Pair> pairs;
        for (const auto& [strike, s] : sides) {
            if (s.first && s.second) {
                pairs.push_back(Pair{std::fabs(*s.first - *s.second), strike, *s.first, *s.second});
            }
        }
        std::sort(pairs.begin(), pairs.end(), [](const Pair& a, const Pair& b) {
            return a.gap != b.gap ? a.gap < b.gap : a.strike < b.strike;
        });
        if (pairs.size() > 5) {
            pairs.resize(5);
        }
        if (pairs.size() >= 2) {
            std::vector<double> values;
            for (const Pair& q : pairs) {
                values.push_back(static_cast<double>(q.strike) + (q.c - q.p) / disc);
            }
            std::sort(values.begin(), values.end());
            const std::size_t n = values.size();
            view.forward = n % 2 == 1 ? values[n / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
            view.forward_source = "synthetic";
        } else if (const auto uid = rows.front()->underlying_id) {
            const auto m = e_.market_.find(*uid);
            if (m != e_.market_.end() && m->second.ltp) {
                view.forward = static_cast<double>(*m->second.ltp);
                view.forward_source = "forward_from_index";
            }
        }
    }
    if (view.forward && !(*view.forward > 0)) {
        view.forward.reset();  // quotes that imply no positive forward give no IVs
        view.forward_source.reset();
    }
    if (view.forward) {
        std::optional<Paise> best;
        for (const Instrument* row : rows) {  // strike order: on a tie the first, lower, strike stays
            const Paise k = row->strike.value_or(0);
            if (!best || std::fabs(static_cast<double>(k) - *view.forward) <
                             std::fabs(static_cast<double>(*best) - *view.forward)) {
                best = k;
            }
        }
        view.atm_strike = best;
    }

    for (const Instrument* row : rows) {
        ChainRow c;
        c.instrument = row->id;
        c.kind = row->kind;
        c.strike = row->strike.value_or(0);
        const bool is_call = row->kind == "CE";
        const auto m = e_.market_.find(row->id);
        if (m != e_.market_.end()) {
            c.bid = m->second.bid;
            c.ask = m->second.ask;
            c.ltp = m->second.ltp;
            c.oi = m->second.oi;
        }
        c.mid = quote_mid(row->id);
        c.stale = stale(row->id);
        if (view.forward) {
            const double f_r = *view.forward / 100.0;
            const double k_r = static_cast<double>(c.strike) / 100.0;
            const auto side_iv = [&](const std::optional<Paise>& quote) -> std::optional<double> {
                if (!quote) {
                    return std::nullopt;
                }
                const pricing::ImpliedVol iv =
                    pricing::implied_vol(is_call, static_cast<double>(*quote) / 100.0, f_r, k_r, t, r);
                return iv.reason ? std::nullopt : std::optional<double>(iv.sigma);
            };
            c.iv_bid = side_iv(c.bid);
            c.iv_ask = side_iv(c.ask);
            if (c.mid) {
                const pricing::ImpliedVol iv = pricing::implied_vol(is_call, *c.mid / 100.0, f_r, k_r, t, r);
                if (iv.reason) {
                    c.iv_reason = std::string(pricing::to_string(*iv.reason));
                } else {
                    const pricing::Greeks g = pricing::greeks(is_call, f_r, k_r, t, r, iv.sigma);
                    c.iv_mid = iv.sigma;
                    c.delta = g.delta;
                    c.gamma = g.gamma;
                    c.vega = g.vega;
                    c.theta = g.theta;
                }
            }
        }
        view.rows.push_back(std::move(c));
    }
    return view;
}

OrderId Context::place(const PlaceRequest& r) { return e_.place(r); }

bool Context::modify(OrderId id, const ModifyRequest& m) { return e_.modify(id, m); }

bool Context::cancel(OrderId id) { return e_.cancel(id, Reason::User); }

std::optional<OrderView> Context::order(OrderId id) const {
    const auto it = e_.book_.orders.find(id);
    return it == e_.book_.orders.end() ? std::nullopt : std::optional<OrderView>(e_.view_of(it->second));
}

std::optional<PositionView> Context::position(InstrumentId iid, std::string_view product) const {
    const std::optional<Product> p = parse_product(product);
    if (!p) {
        return std::nullopt;  // the oracle looks up (iid, product) as given, which finds nothing
    }
    const Position* pos = e_.book_.ledger.position(iid, *p);
    if (pos == nullptr) {
        return std::nullopt;
    }
    return PositionView{pos->instrument, std::string(to_string(pos->product)), pos->qty(), pos->avg_price(),
                        pos->realised};
}

std::vector<PositionView> Context::positions() const {
    std::vector<PositionView> out;
    for (const Position& p : e_.book_.ledger.positions()) {
        if (p.q != 0) {
            out.push_back(PositionView{p.instrument, std::string(to_string(p.product)), p.qty(), p.avg_price(),
                                       p.realised});
        }
    }
    std::sort(out.begin(), out.end(), [](const PositionView& a, const PositionView& b) {
        return a.instrument != b.instrument ? a.instrument < b.instrument : a.product < b.product;
    });
    return out;
}

Funds Context::funds() const { return e_.funds(e_.book_.ledger); }

}  // namespace stocksim
