// The day-to-day state (A1 §6.4), field for field stockify.oracle.sod_state.capture / restore: the same bytes out,
// the same refusals in. Each helper names the oracle function it mirrors.
#include "stocksim/sod_state.hpp"

#include <algorithm>
#include <array>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <utility>
#include <vector>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/session.hpp"

namespace stocksim {

namespace {

constexpr std::string_view kSchema = "stockify.sod_state/1";

// The top-level keys, as sod_state.KEYS (the A1 §6.4 table; the oracle's blockwatch block guards that list).
constexpr std::string_view kKeys[] = {"schema", "trade_date", "cash_paise", "ledger", "pending_orders",
                                      "final_orders", "dp_done", "timers", "built_bars", "counters", "rng",
                                      "subscriptions", "strategy_enabled", "pause_reasons", "queued_notifications",
                                      "fsp_seen", "market", "bar_history", "strategy"};
constexpr std::string_view kLedgerFields[] = {"capital_paise", "charges_total_paise", "reserved_paise", "positions"};
constexpr std::string_view kPositionFields[] = {"instrument_id", "product", "style", "q", "d", "c", "realised", "vm",
                                                "last_mark", "mark_source"};
constexpr std::string_view kOrderFields[] = {
    "order_id", "parent_id", "instrument_id", "side", "qty", "product", "type", "price", "trigger", "validity",
    "protection", "tag", "t_submit", "t_activate", "status", "filled_qty", "fill_value", "reason", "limit",
    "from_market", "triggered", "protection_bp", "reserved", "first_eligible_done", "trade_date", "charged",
    "eod_after"};
constexpr std::string_view kCounters[] = {"next_order", "next_fill", "next_timer", "charge_row"};
constexpr std::string_view kRngFields[] = {"slippage", "strategy"};
constexpr std::string_view kSubscriptionFields[] = {"ticks", "bars"};
constexpr std::string_view kMarketFields[] = {"ltp", "t", "s0", "s1", "bid", "ask", "book", "band", "oi",
                                              "min_interval", "day"};
constexpr std::string_view kSnapshotFields[] = {"t", "ltp", "volume", "atp", "high", "low"};

std::string py_repr(std::string_view s) { return "'" + std::string(s) + "'"; }  // repr() of a plain string

// A Python list repr of strings: ['a', 'b'].
std::string py_list(const std::vector<std::string>& items) {
    std::string out = "[";
    for (std::size_t i = 0; i < items.size(); ++i) {
        out += (i != 0 ? ", " : "") + py_repr(items[i]);
    }
    return out + "]";
}

// ── Typed reads (the oracle's KeyError / TypeError, here with the field named) ────────────────────────────────

[[nodiscard]] RunError bad(std::string_view where, std::string_view what) {
    return RunError("sod_state: " + std::string(where) + " " + std::string(what));
}

std::int64_t as_int(const Json& v, std::string_view where) {
    if (!v.is_number_integer()) {
        throw bad(where, "must be an integer");
    }
    if (v.is_number_unsigned() && v.get<std::uint64_t>() > static_cast<std::uint64_t>(INT64_MAX)) {
        throw bad(where, "does not fit in int64");
    }
    return v.get<std::int64_t>();
}

std::uint64_t as_u64(const Json& v, std::string_view where) {
    if (!v.is_number_integer() || (!v.is_number_unsigned() && v.get<std::int64_t>() < 0)) {
        throw bad(where, "must be a non-negative 64-bit integer");
    }
    return v.get<std::uint64_t>();
}

std::optional<std::int64_t> as_opt_int(const Json& v, std::string_view where) {
    return v.is_null() ? std::nullopt : std::optional<std::int64_t>(as_int(v, where));
}

std::int32_t as_i32(const Json& v, std::string_view where) {
    const std::int64_t x = as_int(v, where);
    if (x < INT32_MIN || x > INT32_MAX) {
        throw bad(where, "does not fit in int32");
    }
    return static_cast<std::int32_t>(x);
}

InstrumentId as_iid(const Json& v, std::string_view where) {
    const std::int64_t x = as_int(v, where);
    if (x < 0 || x > 4294967295LL) {
        throw bad(where, "must be between 0 and 4294967295");
    }
    return static_cast<InstrumentId>(x);
}

const std::string& as_str(const Json& v, std::string_view where) {
    if (!v.is_string()) {
        throw bad(where, "must be a string");
    }
    return v.get_ref<const std::string&>();
}

bool as_bool(const Json& v, std::string_view where) {
    if (!v.is_boolean()) {
        throw bad(where, "must be a boolean");
    }
    return v.get<bool>();
}

Date as_date(const Json& v, std::string_view where) {
    try {
        return parse_iso_date(as_str(v, where));
    } catch (const FormatError& e) {
        throw bad(where, e.what());
    }
}

std::optional<Date> as_opt_date(const Json& v, std::string_view where) {
    return v.is_null() ? std::nullopt : std::optional<Date>(as_date(v, where));
}

const Json& as_list(const Json& v, std::string_view where, std::optional<std::size_t> size = std::nullopt) {
    if (!v.is_array() || (size && v.size() != *size)) {
        throw bad(where, size ? "must be a list of " + std::to_string(*size) : std::string("must be a list"));
    }
    return v;
}

// Exactly these keys: a missing one, or one the schema does not have, is refused (sod_state._need).
void need(const Json& d, std::span<const std::string_view> keys, std::string_view where) {
    if (!d.is_object()) {
        throw RunError("sod_state: " + std::string(where) + " must be an object");
    }
    for (const std::string_view k : keys) {
        if (!d.contains(std::string(k))) {
            throw RunError("sod_state: " + std::string(where) + " is missing " + py_repr(k));
        }
    }
    std::vector<std::string> extra;
    for (const auto& [k, v] : d.items()) {
        if (std::find(keys.begin(), keys.end(), k) == keys.end()) {
            extra.push_back(k);
        }
    }
    if (!extra.empty()) {
        std::sort(extra.begin(), extra.end());
        throw RunError("sod_state: " + std::string(where) + " has unknown keys " + py_list(extra));
    }
}

// ── Parsed JSON → the canonical writer's value ───────────────────────────────

JValue to_jvalue(const Json& j) {
    switch (j.type()) {
        case Json::value_t::null: return JValue();
        case Json::value_t::boolean: return JValue(j.get<bool>());
        case Json::value_t::number_unsigned: {
            const std::uint64_t u = j.get<std::uint64_t>();
            return u > static_cast<std::uint64_t>(INT64_MAX) ? JValue(u) : JValue(static_cast<std::int64_t>(u));
        }
        case Json::value_t::number_integer: return JValue(j.get<std::int64_t>());
        case Json::value_t::number_float: return JValue(j.get<double>());
        case Json::value_t::string: return JValue(j.get<std::string>());
        case Json::value_t::array: {
            JValue out = JValue::array();
            for (const Json& x : j) {
                out.push_back(to_jvalue(x));
            }
            return out;
        }
        case Json::value_t::object: {
            JValue out = JValue::object();
            for (const auto& [k, v] : j.items()) {
                out[k] = to_jvalue(v);
            }
            return out;
        }
        default: throw RunError("sod_state: a value is not JSON-typed");
    }
}

JValue opt(const std::optional<std::int64_t>& v) { return v ? JValue(*v) : JValue(); }
JValue opt_date(const std::optional<Date>& d) { return d ? JValue(iso_date(*d)) : JValue(); }
JValue iid_of(InstrumentId iid) { return JValue(static_cast<std::int64_t>(iid)); }

// ── Capture (sod_state.capture) ──────────────────────────────────────────────

JValue order_record(const Order& o) {  // sod_state._order
    JValue r = JValue::object();
    r["order_id"] = JValue(o.order_id);
    r["parent_id"] = JValue(o.parent_id);
    r["instrument_id"] = iid_of(o.instrument);
    r["side"] = o.side_text;
    r["qty"] = o.qty;
    r["product"] = o.product_text;
    r["type"] = o.type_text;
    r["price"] = o.price;
    r["trigger"] = o.trigger;
    r["validity"] = o.validity_text;
    r["protection"] = o.protection ? JValue(static_cast<std::int64_t>(*o.protection)) : JValue("AUTO");
    r["tag"] = o.tag;
    r["t_submit"] = o.t_submit;
    r["t_activate"] = o.t_activate;
    r["status"] = std::string(to_string(o.status));
    r["filled_qty"] = o.filled_qty;
    r["fill_value"] = o.fill_value;
    r["reason"] = std::string(to_string(o.reason));
    r["limit"] = opt(o.limit);
    r["from_market"] = o.from_market;
    r["triggered"] = o.triggered;
    r["protection_bp"] = o.protection_bp ? JValue(static_cast<std::int64_t>(*o.protection_bp)) : JValue();
    r["reserved"] = o.reserved;
    r["first_eligible_done"] = o.first_eligible_done;
    r["trade_date"] = opt_date(o.trade_date);
    r["charged"] = o.charged;
    r["eod_after"] = opt_date(o.eod_after);
    return r;
}

JValue snapshot(const std::optional<Snapshot>& s) {
    if (!s) {
        return JValue();
    }
    JValue out = JValue::object();
    out["t"] = s->t;
    out["ltp"] = s->ltp;
    out["volume"] = s->volume;
    out["atp"] = s->atp;
    out["high"] = s->high;
    out["low"] = s->low;
    return out;
}

JValue market_record(const MarketState& m) {  // sod_state._market
    JValue out = JValue::object();
    out["ltp"] = opt(m.ltp);
    out["t"] = opt(m.t);
    out["s0"] = snapshot(m.s0);
    out["s1"] = snapshot(m.s1);
    out["bid"] = opt(m.bid);
    out["ask"] = opt(m.ask);
    if (m.book) {
        JValue levels = JValue::array();
        for (const DepthLevel& lv : *m.book) {
            levels.push_back(JValue::Array{JValue(lv.price), JValue(lv.qty), JValue(static_cast<std::int64_t>(lv.orders))});
        }
        out["book"] = std::move(levels);
    } else {
        out["book"] = JValue();
    }
    out["band"] = m.band ? JValue(JValue::Array{JValue(m.band->first), JValue(m.band->second)}) : JValue();
    out["oi"] = opt(m.oi);
    out["min_interval"] = m.min_interval ? JValue(static_cast<std::int64_t>(*m.min_interval)) : JValue();
    out["day"] = m.day != 0 ? JValue(iso_date(m.day)) : JValue();
    return out;
}

}  // namespace

// The helpers that read or write the engine's internals (a friend of Engine).
struct SodState {
    static std::vector<InternalEvent> pending_internal(const Engine& e, InternalKind kind);
    static JValue timers_of(const Engine& e);
    static JValue built_of(const Engine& e);
    static void restore(Engine& e, const Json& state);
};

// The internal events of `kind` still to come (left in the queue, or passed over after the last DAY_END), in the
// order they would fire: by key (sod_state._pending_internal).
std::vector<InternalEvent> SodState::pending_internal(const Engine& e, InternalKind kind) {
    std::vector<InternalEvent> out;
    auto queue = e.internal_;  // a copy: the queue itself is never disturbed
    while (!queue.empty()) {
        if (queue.top().kind == kind) {
            out.push_back(queue.top());
        }
        queue.pop();
    }
    for (const InternalEvent& ev : e.carried_) {
        if (ev.kind == kind) {
            out.push_back(ev);
        }
    }
    std::sort(out.begin(), out.end(), [](const InternalEvent& a, const InternalEvent& b) { return a.key < b.key; });
    return out;
}

JValue SodState::timers_of(const Engine& e) {  // sod_state._timers
    JValue out = JValue::array();
    std::set<std::uint64_t> seen;
    for (const InternalEvent& ev : pending_internal(e, InternalKind::Timer)) {
        const auto it = e.timers_.find(ev.timer);
        if (it == e.timers_.end() || it->second.first != ev.key.t || seen.count(ev.timer) != 0) {
            continue;  // cancelled (its queue entry is stale), or re-armed since
        }
        seen.insert(ev.timer);
        out.push_back(JValue::Array{JValue(ev.timer), JValue(it->second.first), JValue(it->second.second)});
    }
    std::vector<std::string> missing;
    for (const auto& [tid, spec] : e.timers_) {
        if (seen.count(tid) == 0) {
            missing.push_back(std::to_string(tid));
        }
    }
    if (!missing.empty()) {
        std::string list;
        for (std::size_t i = 0; i < missing.size(); ++i) {
            list += (i != 0 ? ", " : "") + missing[i];
        }
        throw RunError("timers [" + list + "] have no queued event");
    }
    return out;
}

JValue SodState::built_of(const Engine& e) {  // sod_state._built
    JValue out = JValue::array();
    for (const InternalEvent& ev : pending_internal(e, InternalKind::BarBuilt)) {
        const auto acc = e.built_.find({ev.instrument, ev.interval_s});
        if (acc == e.built_.end()) {
            continue;
        }
        const auto it = acc->second.find(ev.start);
        if (it == acc->second.end()) {
            continue;
        }
        JValue row = JValue::Array{iid_of(ev.instrument), JValue(static_cast<std::int64_t>(ev.interval_s)), JValue(ev.start)};
        for (const std::int64_t v : it->second) {
            row.push_back(JValue(v));
        }
        out.push_back(std::move(row));
    }
    return out;
}

std::string Engine::checked_strategy_state() const {
    if (!strategy_) {
        throw RunError("no strategy set");
    }
    const std::string raw = strategy_->save_state();  // the Python trampoline hands back checked canonical JSON
    Json parsed;
    try {
        parsed = Json::parse(raw);
    } catch (const Json::parse_error& e) {
        throw RunError(std::string("strategy save_state() returned invalid JSON: ") + e.what());
    }
    if (!parsed.is_object()) {
        throw RunError("strategy save_state() must return a JSON object");
    }
    return dumps_canonical(to_jvalue(parsed));
}

std::string eod_state_json(const Engine& e) {  // sod_state.capture
    const Ledger& led = e.book_.ledger;
    // Invariants for what the state leaves out (A1 §6.4): DAY_END releases every CNC credit, and an instrument's
    // last price is its market LTP, so both are rebuilt on load rather than carried.
    if (!led.unavailable.empty()) {
        std::vector<std::string> dates;
        for (const auto& [d, v] : led.unavailable) {
            dates.push_back(iso_date(d));
        }
        throw RunError("sod_state: CNC releases left after DAY_END: " + py_list(dates));
    }
    std::map<InstrumentId, Paise> ltps;
    for (const auto& [iid, m] : e.market_) {
        if (m.ltp) {
            ltps[iid] = *m.ltp;
        }
    }
    if (ltps != e.underlying_last_) {
        throw RunError("sod_state: last prices differ from the market LTPs");
    }

    JValue st = JValue::object();
    st["schema"] = std::string(kSchema);
    st["trade_date"] = opt_date(e.state_date_);
    st["cash_paise"] = led.cash;
    JValue ledger = JValue::object();
    ledger["capital_paise"] = led.capital;
    ledger["charges_total_paise"] = led.charges_total;
    ledger["reserved_paise"] = led.reserved;
    JValue positions = JValue::array();
    for (const Position& p : led.positions()) {  // in the order they were first opened
        JValue r = JValue::object();
        r["instrument_id"] = iid_of(p.instrument);
        r["product"] = std::string(to_string(p.product));
        r["style"] = p.style == CashStyle::Premium ? "premium" : "variation";
        r["q"] = p.q;
        r["d"] = p.d;
        r["c"] = p.c;
        r["realised"] = p.realised;
        r["vm"] = p.vm;
        r["last_mark"] = opt(p.last_mark);
        r["mark_source"] = p.mark_source;
        positions.push_back(std::move(r));
    }
    ledger["positions"] = std::move(positions);
    st["ledger"] = std::move(ledger);

    JValue pending = JValue::array();
    JValue final_orders = JValue::array();
    for (const auto& [id, o] : e.book_.orders) {  // std::map: by order_id
        (o.final() ? final_orders : pending).push_back(order_record(o));
    }
    st["pending_orders"] = std::move(pending);
    st["final_orders"] = std::move(final_orders);

    JValue dp = JValue::array();
    for (const auto& [iid, d] : e.book_.dp_done) {  // std::set: sorted
        dp.push_back(JValue::Array{iid_of(iid), JValue(iso_date(d))});
    }
    st["dp_done"] = std::move(dp);
    st["timers"] = SodState::timers_of(e);
    st["built_bars"] = SodState::built_of(e);

    JValue counters = JValue::object();
    counters["next_order"] = JValue(e.next_order_);
    counters["next_fill"] = JValue(e.book_.next_fill);
    counters["next_timer"] = JValue(e.next_timer_);
    counters["charge_row"] = e.charge_row_base_ + static_cast<std::int64_t>(e.book_.charges.size());
    st["counters"] = std::move(counters);

    JValue rng = JValue::object();
    rng["slippage"] = JValue::Array{JValue(e.book_.rng.state()), JValue(e.book_.rng.inc())};
    rng["strategy"] = JValue::Array{JValue(e.strategy_rng_.state()), JValue(e.strategy_rng_.inc())};
    st["rng"] = std::move(rng);

    JValue subs = JValue::object();
    JValue ticks = JValue::array();
    for (const InstrumentId iid : e.sub_ticks_) {
        ticks.push_back(iid_of(iid));
    }
    subs["ticks"] = std::move(ticks);
    JValue bars = JValue::object();
    for (const auto& [iid, intervals] : e.sub_bars_) {
        JValue list = JValue::array();
        for (const std::int32_t isec : intervals) {
            list.push_back(JValue(static_cast<std::int64_t>(isec)));
        }
        bars[std::to_string(iid)] = std::move(list);
    }
    subs["bars"] = std::move(bars);
    st["subscriptions"] = std::move(subs);

    st["strategy_enabled"] = e.strategy_enabled_;
    JValue reasons = JValue::array();
    for (const std::string& r : e.pause_reasons_) {
        reasons.push_back(JValue(r));
    }
    st["pause_reasons"] = std::move(reasons);
    st["queued_notifications"] = to_jvalue(e.queued_notifications_);

    JValue fsp = JValue::array();
    for (const auto& [key, px] : e.fsp_seen_) {  // std::map: sorted by (underlying, date)
        fsp.push_back(JValue::Array{iid_of(key.first), JValue(iso_date(key.second)), JValue(px)});
    }
    st["fsp_seen"] = std::move(fsp);

    JValue market = JValue::object();
    JValue history = JValue::object();
    for (const auto& [iid, m] : e.market_) {
        market[std::to_string(iid)] = market_record(m);
        for (const auto& [isec, rows] : m.bars) {
            JValue list = JValue::array();
            for (const BarRow& r : rows) {
                list.push_back(JValue::Array{JValue(r.start), JValue(r.open), JValue(r.high), JValue(r.low),
                                             JValue(r.close), JValue(r.volume), JValue(r.oi)});
            }
            history[std::to_string(iid) + ":" + std::to_string(isec)] = std::move(list);
        }
    }
    st["market"] = std::move(market);
    st["bar_history"] = std::move(history);

    const std::string strategy = e.day_end_strategy_state_ ? *e.day_end_strategy_state_ : e.checked_strategy_state();
    st["strategy"] = to_jvalue(Json::parse(strategy));
    return dumps_canonical(st);
}

// ── Restore (sod_state.restore) ──────────────────────────────────────────────

namespace {

Order order_from(const Json& o, std::string_view group) {
    need(o, kOrderFields, group);
    const std::string where(group);
    Order out;
    out.order_id = as_u64(o.at("order_id"), where + ".order_id");
    out.parent_id = as_u64(o.at("parent_id"), where + ".parent_id");
    out.instrument = as_iid(o.at("instrument_id"), where + ".instrument_id");
    out.side_text = as_str(o.at("side"), where + ".side");
    out.product_text = as_str(o.at("product"), where + ".product");
    out.type_text = as_str(o.at("type"), where + ".type");
    out.validity_text = as_str(o.at("validity"), where + ".validity");
    // The enums are meaningful only once validation passed (orders.hpp), so an unknown spelling keeps its default.
    out.side = parse_side(out.side_text).value_or(Side::Buy);
    out.product = parse_product(out.product_text).value_or(Product::CNC);
    out.type = parse_order_type(out.type_text).value_or(OrderType::Market);
    out.validity = parse_validity(out.validity_text).value_or(Validity::Day);
    out.qty = as_int(o.at("qty"), where + ".qty");
    out.price = as_int(o.at("price"), where + ".price");
    out.trigger = as_int(o.at("trigger"), where + ".trigger");
    const Json& prot = o.at("protection");
    if (prot.is_string() && prot.get_ref<const std::string&>() == "AUTO") {
        out.protection.reset();
    } else {
        out.protection = as_i32(prot, where + ".protection");
    }
    out.tag = as_str(o.at("tag"), where + ".tag");
    out.t_submit = as_int(o.at("t_submit"), where + ".t_submit");
    out.t_activate = as_int(o.at("t_activate"), where + ".t_activate");
    const std::string& status = as_str(o.at("status"), where + ".status");
    const std::optional<OrderStatus> st = parse_order_status(status);
    if (!st) {
        throw RunError("sod_state: " + where + ": unknown status " + py_repr(status));
    }
    out.status = *st;
    out.filled_qty = as_int(o.at("filled_qty"), where + ".filled_qty");
    out.fill_value = as_int(o.at("fill_value"), where + ".fill_value");
    const std::string& reason = as_str(o.at("reason"), where + ".reason");
    const std::optional<Reason> rs = parse_reason(reason);
    if (!rs) {
        throw RunError("sod_state: " + where + ": unknown reason " + py_repr(reason));
    }
    out.reason = *rs;
    out.limit = as_opt_int(o.at("limit"), where + ".limit");
    out.from_market = as_bool(o.at("from_market"), where + ".from_market");
    out.triggered = as_bool(o.at("triggered"), where + ".triggered");
    if (!o.at("protection_bp").is_null()) {
        out.protection_bp = as_i32(o.at("protection_bp"), where + ".protection_bp");
    }
    out.reserved = as_int(o.at("reserved"), where + ".reserved");
    out.first_eligible_done = as_bool(o.at("first_eligible_done"), where + ".first_eligible_done");
    out.trade_date = as_opt_date(o.at("trade_date"), where + ".trade_date");
    out.charged = as_bool(o.at("charged"), where + ".charged");
    out.eod_after = as_opt_date(o.at("eod_after"), where + ".eod_after");
    return out;
}

std::optional<Snapshot> snapshot_from(const Json& v) {  // sod_state._snapshot
    if (v.is_null()) {
        return std::nullopt;
    }
    need(v, kSnapshotFields, "market snapshot");
    Snapshot s;
    s.t = as_int(v.at("t"), "market snapshot.t");
    s.ltp = as_int(v.at("ltp"), "market snapshot.ltp");
    s.volume = as_int(v.at("volume"), "market snapshot.volume");
    s.atp = as_int(v.at("atp"), "market snapshot.atp");
    s.high = as_int(v.at("high"), "market snapshot.high");
    s.low = as_int(v.at("low"), "market snapshot.low");
    return s;
}

MarketState market_from(const Json& m) {
    need(m, kMarketFields, "market");
    MarketState out;
    out.ltp = as_opt_int(m.at("ltp"), "market.ltp");
    out.t = as_opt_int(m.at("t"), "market.t");
    out.s0 = snapshot_from(m.at("s0"));
    out.s1 = snapshot_from(m.at("s1"));
    out.bid = as_opt_int(m.at("bid"), "market.bid");
    out.ask = as_opt_int(m.at("ask"), "market.ask");
    if (const Json& book = m.at("book"); !book.is_null()) {
        std::array<DepthLevel, 10> levels{};
        const Json& rows = as_list(book, "market.book", 10);
        for (std::size_t i = 0; i < 10; ++i) {
            const Json& lv = as_list(rows[i], "market.book level", 3);
            levels[i] = DepthLevel{as_int(lv[0], "market.book price"), as_int(lv[1], "market.book qty"),
                                   as_i32(lv[2], "market.book orders")};
        }
        out.book = levels;
    }
    if (const Json& band = m.at("band"); !band.is_null()) {
        const Json& pair = as_list(band, "market.band", 2);
        out.band = std::make_pair(as_int(pair[0], "market.band lower"), as_int(pair[1], "market.band upper"));
    }
    out.oi = as_opt_int(m.at("oi"), "market.oi");
    if (!m.at("min_interval").is_null()) {
        out.min_interval = as_i32(m.at("min_interval"), "market.min_interval");
    }
    out.day = m.at("day").is_null() ? 0 : as_date(m.at("day"), "market.day");
    return out;
}

// int(key) for an object keyed by an instrument id in decimal.
InstrumentId iid_key(const std::string& key, std::string_view where) {
    if (key.empty() || key.size() > 10 || !std::all_of(key.begin(), key.end(), [](char c) { return c >= '0' && c <= '9'; })) {
        throw bad(where, "key " + py_repr(key) + " is not an instrument id");
    }
    const std::int64_t v = std::stoll(key);
    if (v > 4294967295LL) {
        throw bad(where, "key " + py_repr(key) + " is not an instrument id");
    }
    return static_cast<InstrumentId>(v);
}

}  // namespace

void SodState::restore(Engine& e, const Json& state) {  // sod_state.restore
    if (!state.is_object() || !state.contains("schema")) {
        throw RunError("sod_state: state must be an object with a 'schema'");
    }
    const Json& schema = state.at("schema");
    if (!schema.is_string() || schema.get_ref<const std::string&>() != kSchema) {
        throw RunError("sod_state: schema must be " + py_repr(kSchema) + ", got " +
                       (schema.is_string() ? py_repr(schema.get_ref<const std::string&>()) : schema.dump()));
    }
    need(state, kKeys, "state");
    const Json& led_doc = state.at("ledger");
    need(led_doc, kLedgerFields, "ledger");
    Ledger led(as_int(state.at("cash_paise"), "cash_paise"), as_int(led_doc.at("capital_paise"), "ledger.capital_paise"));
    led.charges_total = as_int(led_doc.at("charges_total_paise"), "ledger.charges_total_paise");
    led.reserved = as_int(led_doc.at("reserved_paise"), "ledger.reserved_paise");
    for (const Json& p : as_list(led_doc.at("positions"), "ledger.positions")) {
        need(p, kPositionFields, "ledger position");
        const std::string& product = as_str(p.at("product"), "ledger position.product");
        const std::optional<Product> prod = parse_product(product);
        if (!prod) {
            throw RunError("sod_state: ledger position: unknown product " + py_repr(product));
        }
        const std::string& style = as_str(p.at("style"), "ledger position.style");
        if (style != "premium" && style != "variation") {
            throw RunError("sod_state: ledger position: unknown style " + py_repr(style));
        }
        Position& pos = led.get_or_open(as_iid(p.at("instrument_id"), "ledger position.instrument_id"), *prod,
                                        style == "premium" ? CashStyle::Premium : CashStyle::Variation);
        pos.q = as_int(p.at("q"), "ledger position.q");
        pos.d = as_i32(p.at("d"), "ledger position.d");
        pos.c = as_int(p.at("c"), "ledger position.c");
        pos.realised = as_int(p.at("realised"), "ledger position.realised");
        pos.vm = as_int(p.at("vm"), "ledger position.vm");
        pos.last_mark = as_opt_int(p.at("last_mark"), "ledger position.last_mark");
        pos.mark_source = as_str(p.at("mark_source"), "ledger position.mark_source");
    }
    Book& book = e.book_;
    book.ledger = std::move(led);
    book.orders.clear();
    e.prior_final_.clear();
    for (const char* group : {"pending_orders", "final_orders"}) {
        for (const Json& o : as_list(state.at(group), group)) {
            Order ord = order_from(o, group);
            const OrderId id = ord.order_id;
            if (ord.final()) {
                e.prior_final_.insert(id);
            }
            book.orders.insert_or_assign(id, std::move(ord));
        }
    }
    book.dp_done.clear();
    for (const Json& row : as_list(state.at("dp_done"), "dp_done")) {
        const Json& pair = as_list(row, "dp_done row", 2);
        book.dp_done.insert({as_iid(pair[0], "dp_done instrument_id"), as_date(pair[1], "dp_done trade_date")});
    }
    const Json& c = state.at("counters");
    need(c, kCounters, "counters");
    e.next_order_ = as_u64(c.at("next_order"), "counters.next_order");
    book.next_fill = as_u64(c.at("next_fill"), "counters.next_fill");
    e.next_timer_ = as_u64(c.at("next_timer"), "counters.next_timer");
    e.charge_row_base_ = as_int(c.at("charge_row"), "counters.charge_row");
    e.min_live_ = e.next_order_;
    for (const auto& [id, o] : book.orders) {
        if (!o.final()) {
            e.min_live_ = std::min(e.min_live_, id);
        }
    }
    const Json& rng = state.at("rng");
    need(rng, kRngFields, "rng");
    const auto stream = [](const Json& v, const char* name) {
        const Json& pair = as_list(v, std::string("rng.") + name, 2);
        return Pcg32::at(as_u64(pair[0], std::string("rng.") + name + " state"),
                         as_u64(pair[1], std::string("rng.") + name + " inc"));
    };
    book.rng = stream(rng.at("slippage"), "slippage");
    e.strategy_rng_ = stream(rng.at("strategy"), "strategy");
    for (const Json& row : as_list(state.at("timers"), "timers")) {
        const Json& t = as_list(row, "timer", 3);
        const std::uint64_t tid = as_u64(t[0], "timer id");
        const TimeNs at = as_int(t[1], "timer t_ns");
        e.timers_[tid] = {at, as_int(t[2], "timer period_ns")};
        InternalEvent ev{EventKey{at, kRankTimer, 0, 0}, InternalKind::Timer};
        ev.timer = tid;
        e.push_internal(std::move(ev));
    }
    for (const Json& row : as_list(state.at("built_bars"), "built_bars")) {
        const Json& b = as_list(row, "built bar", 9);
        const InstrumentId iid = as_iid(b[0], "built bar instrument_id");
        const std::int32_t isec = as_i32(b[1], "built bar interval_s");
        const TimeNs start = as_int(b[2], "built bar start_ns");
        std::array<std::int64_t, 6> bar{};
        for (std::size_t i = 0; i < 6; ++i) {
            bar[i] = as_int(b[3 + i], "built bar value");
        }
        e.built_[{iid, isec}][start] = bar;
        InternalEvent ev{EventKey{checked_add(start, checked_mul(isec, kNsPerSecond)), kRankBarBuilt, 0, 0},
                         InternalKind::BarBuilt};
        ev.instrument = iid;
        ev.interval_s = isec;
        ev.start = start;
        e.push_internal(std::move(ev));
    }
    const Json& subs = state.at("subscriptions");
    need(subs, kSubscriptionFields, "subscriptions");
    e.sub_ticks_.clear();
    for (const Json& iid : as_list(subs.at("ticks"), "subscriptions.ticks")) {
        e.sub_ticks_.insert(as_iid(iid, "subscriptions.ticks"));
    }
    if (!subs.at("bars").is_object()) {
        throw RunError("sod_state: subscriptions.bars must be an object");
    }
    e.sub_bars_.clear();
    for (const auto& [key, intervals] : subs.at("bars").items()) {
        std::set<std::int32_t>& set = e.sub_bars_[iid_key(key, "subscriptions.bars")];
        for (const Json& isec : as_list(intervals, "subscriptions.bars")) {
            set.insert(as_i32(isec, "subscriptions.bars interval_s"));
        }
    }
    e.strategy_enabled_ = as_bool(state.at("strategy_enabled"), "strategy_enabled");
    e.pause_reasons_.clear();
    for (const Json& r : as_list(state.at("pause_reasons"), "pause_reasons")) {
        e.pause_reasons_.insert(as_str(r, "pause_reasons"));
    }
    e.queued_notifications_ = as_list(state.at("queued_notifications"), "queued_notifications");
    e.fsp_seen_.clear();
    for (const Json& row : as_list(state.at("fsp_seen"), "fsp_seen")) {
        const Json& f = as_list(row, "fsp_seen row", 3);
        e.fsp_seen_[{as_iid(f[0], "fsp_seen underlying_id"), as_date(f[1], "fsp_seen date")}] =
            as_int(f[2], "fsp_seen paise");
    }
    if (!state.at("market").is_object()) {
        throw RunError("sod_state: market must be an object");
    }
    e.market_.clear();
    for (const auto& [key, m] : state.at("market").items()) {
        e.market_[iid_key(key, "market")] = market_from(m);
    }
    e.underlying_last_.clear();
    for (const auto& [iid, m] : e.market_) {
        if (m.ltp) {
            e.underlying_last_[iid] = *m.ltp;
        }
    }
    if (!state.at("bar_history").is_object()) {
        throw RunError("sod_state: bar_history must be an object");
    }
    for (const auto& [key, rows] : state.at("bar_history").items()) {
        const std::size_t colon = key.find(':');
        if (colon == std::string::npos) {
            throw bad("bar_history", "key " + py_repr(key) + " is not <instrument_id>:<interval_s>");
        }
        const InstrumentId iid = iid_key(key.substr(0, colon), "bar_history");
        const std::string isec_text = key.substr(colon + 1);
        if (isec_text.empty() || isec_text.size() > 10 ||
            !std::all_of(isec_text.begin(), isec_text.end(), [](char ch) { return ch >= '0' && ch <= '9'; })) {
            throw bad("bar_history", "key " + py_repr(key) + " is not <instrument_id>:<interval_s>");
        }
        const std::int64_t isec = std::stoll(isec_text);
        if (isec > INT32_MAX) {
            throw bad("bar_history", "key " + py_repr(key) + " is not <instrument_id>:<interval_s>");
        }
        std::vector<BarRow> bars;
        for (const Json& row : as_list(rows, "bar_history")) {
            const Json& r = as_list(row, "bar_history row", 7);
            bars.push_back(BarRow{as_int(r[0], "bar start_ns"), as_int(r[1], "bar open"), as_int(r[2], "bar high"),
                                  as_int(r[3], "bar low"), as_int(r[4], "bar close"), as_int(r[5], "bar volume"),
                                  as_int(r[6], "bar oi")});
        }
        e.market_[iid].bars[static_cast<std::int32_t>(isec)] = std::move(bars);
    }
    e.state_date_ = as_opt_date(state.at("trade_date"), "trade_date");
    if (!state.at("strategy").is_object()) {
        throw RunError("sod_state: strategy must be an object");
    }
    e.orders_drained_ = e.next_order_;  // loaded orders were journaled on their own day (drain_outputs)
    // The strategy's part goes last, as in the oracle; without a strategy yet, set_strategy hands it over.
    const std::string text = state.at("strategy").dump();
    if (e.strategy_) {
        try {
            e.strategy_->load_state(text);
        } catch (const RunError&) {
            throw;
        } catch (const std::exception& ex) {
            throw RunError(std::string("strategy load_state() raised: ") + ex.what());
        }
    } else {
        e.pending_strategy_state_ = text;
    }
}

void load_sod_state_json(Engine& e, const Json& state) {  // runner.Engine._load_state
    if (e.started_) {
        throw RunError("sod_state must be loaded before the first event");
    }
    if (e.resumed_) {
        throw RunError("sod_state is already loaded");
    }
    SodState::restore(e, state);
    e.resumed_ = true;
}

void load_sod_state(Engine& e, std::string_view text) {  // runner.Engine.load_sod_state
    if (e.started_) {
        throw RunError("sod_state must be loaded before the first event");
    }
    Json state;
    try {
        state = Json::parse(text.begin(), text.end());
    } catch (const Json::parse_error& ex) {
        throw RunError(std::string("sod_state: invalid JSON: ") + ex.what());
    }
    load_sod_state_json(e, state);
}

}  // namespace stocksim
