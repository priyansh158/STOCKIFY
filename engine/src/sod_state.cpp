// The day-to-day state (A1 §6.4), field for field stockify.oracle.sod_state.capture / restore: the same bytes out,
// the same refusals in. Each helper names the oracle function it mirrors.
#include "stocksim/sod_state.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <tuple>
#include <unordered_set>
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
// <block name="sod-state-keys-cpp" same-as="stockify/oracle/sod_state.py:sod-state-keys" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kKeys[] = {"schema", "trade_date", "cash_paise", "ledger", "pending_orders",
                                      "final_orders", "dp_done", "timers", "built_bars", "counters", "rng",
                                      "subscriptions", "strategy_enabled", "pause_reasons", "queued_notifications",
                                      "fsp_seen", "market", "bar_history", "strategy"};
// </block>
constexpr std::string_view kLedgerFields[] = {"capital_paise", "charges_total_paise", "reserved_paise", "positions"};
// <block name="sod-state-position-fields-cpp" same-as="stockify/oracle/sod_state.py:sod-state-position-fields" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kPositionFields[] = {"instrument_id", "product", "style", "q", "d", "c", "realised", "vm",
                                                "last_mark", "mark_source"};
// </block>
// <block name="sod-state-order-fields-cpp" same-as="stockify/oracle/sod_state.py:sod-state-order-fields" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kOrderFields[] = {
    "order_id", "parent_id", "instrument_id", "side", "qty", "product", "type", "price", "trigger", "validity",
    "protection", "tag", "t_submit", "t_activate", "status", "filled_qty", "fill_value", "reason", "limit",
    "from_market", "triggered", "protection_bp", "reserved", "first_eligible_done", "trade_date", "charged",
    "eod_after"};
// </block>
// <block name="sod-state-counters-cpp" same-as="stockify/oracle/sod_state.py:sod-state-counters" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kCounters[] = {"next_order", "next_fill", "next_timer", "charge_row"};
// </block>
constexpr std::string_view kRngFields[] = {"slippage", "strategy"};
constexpr std::string_view kSubscriptionFields[] = {"ticks", "bars"};
// <block name="sod-state-market-fields-cpp" same-as="stockify/oracle/sod_state.py:sod-state-market-fields" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kMarketFields[] = {"ltp", "t", "s0", "s1", "bid", "ask", "book", "band", "oi",
                                              "min_interval", "day"};
// </block>
// <block name="sod-state-snapshot-fields-cpp" same-as="stockify/oracle/sod_state.py:sod-state-snapshot-fields" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
constexpr std::string_view kSnapshotFields[] = {"t", "ltp", "volume", "atp", "high", "low"};
// </block>

std::string py_repr(std::string_view s) { return "'" + std::string(s) + "'"; }  // repr() of a plain string

// A Python list repr of strings: ['a', 'b'].
std::string py_list(const std::vector<std::string>& items) {
    std::string out = "[";
    for (std::size_t i = 0; i < items.size(); ++i) {
        out += (i != 0 ? ", " : "") + py_repr(items[i]);
    }
    return out + "]";
}

// ── Typed reads (sod_state._int and friends: the same words) ────────────────────────────────────────────────

[[nodiscard]] RunError bad(std::string_view where, std::string_view what) {
    return RunError("sod_state: " + std::string(where) + " " + std::string(what));
}

// A piece of the file's text as it goes into an error message (sod_state.esc): quoted, one printable ASCII byte at a
// time (' and \ escaped, everything else as \xHH), cut at 100 bytes.
std::string esc(std::string_view s) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out = "'";
    const std::size_t n = std::min<std::size_t>(s.size(), 100);
    for (std::size_t i = 0; i < n; ++i) {
        const auto b = static_cast<unsigned char>(s[i]);
        if (b == 0x27) {
            out += "\\'";
        } else if (b == 0x5C) {
            out += "\\\\";
        } else if (b >= 0x20 && b <= 0x7E) {
            out += static_cast<char>(b);
        } else {
            out += "\\x";
            out += kHex[b >> 4];
            out += kHex[b & 15];
        }
    }
    return out + (s.size() > 100 ? "..." : "") + "'";
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

std::int64_t as_nonneg(const Json& v, std::string_view where) {
    const std::int64_t x = as_int(v, where);
    if (x < 0) {
        throw bad(where, "must not be negative");
    }
    return x;
}

std::int64_t as_pos(const Json& v, std::string_view where) {
    const std::int64_t x = as_int(v, where);
    if (x <= 0) {
        throw bad(where, "must be positive");
    }
    return x;
}

std::int64_t as_atleast1(const Json& v, std::string_view where) {
    const std::int64_t x = as_int(v, where);
    if (x < 1) {
        throw bad(where, "must be at least 1");
    }
    return x;
}

// An id counter or the charge-row count: at least `lo`, at most kCounterMax (sod_state.COUNTER_MAX).
constexpr std::int64_t kCounterMax = std::int64_t{1} << 62;

std::int64_t as_counter(const Json& v, std::string_view where, std::int64_t lo) {
    const std::int64_t x = as_int(v, where);
    if (x < lo) {
        throw bad(where, lo == 1 ? "must be at least 1" : "must not be negative");
    }
    if (x > kCounterMax) {
        throw bad(where, "must be at most " + std::to_string(kCounterMax));
    }
    return x;
}

std::optional<std::int64_t> as_opt_int(const Json& v, std::string_view where) {
    return v.is_null() ? std::nullopt : std::optional<std::int64_t>(as_int(v, where));
}

std::optional<std::int64_t> as_opt_nonneg(const Json& v, std::string_view where) {
    return v.is_null() ? std::nullopt : std::optional<std::int64_t>(as_nonneg(v, where));
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

// A real YYYY-MM-DD date, year 1 to 9999 (the oracle's datetime.date).
Date as_date(const Json& v, std::string_view where) {
    if (!v.is_string()) {
        throw bad(where, "must be a YYYY-MM-DD date");
    }
    try {
        const Date d = parse_iso_date(v.get_ref<const std::string&>());
        if (d < 10000) {
            throw FormatError("year 0");
        }
        return d;
    } catch (const FormatError&) {
        throw bad(where, "must be a YYYY-MM-DD date");
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
        std::string list = "[";
        for (std::size_t i = 0; i < extra.size(); ++i) {
            list += (i != 0 ? ", " : "") + esc(extra[i]);
        }
        throw RunError("sod_state: " + std::string(where) + " has unknown keys " + list + "]");
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

namespace {
struct Staged;  // what a valid state holds (defined with the restore code below)
}

// The helpers that read or write the engine's internals (a friend of Engine).
struct SodState {
    static std::vector<InternalEvent> pending_internal(const Engine& e, InternalKind kind);
    static JValue timers_of(const Engine& e);
    static JValue built_of(const Engine& e);
    static Staged parse(const Engine& e, const Json& state);
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
        parsed = parse_strategy_state(raw);
    } catch (const RunError& e) {
        throw RunError("strategy save_state() returned an unusable state: " + std::string(e.what()));
    }
    if (!parsed.is_object()) {
        throw RunError("strategy save_state() must return a JSON object");
    }
    return dumps_canonical(to_jvalue(parsed));
}

std::string eod_state_json(const Engine& e) {  // sod_state.capture
    if (!e.state_date_) {
        throw RunError("sod_state: no trading day has ended yet");
    }
    if (!e.settled_) {
        throw RunError("sod_state: events were processed after the last day ended");
    }
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
    counters["charge_row"] = checked_add(e.charge_row_base_, static_cast<std::int64_t>(e.book_.charges.size()));
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

// ── Reading the text (sod_state.loads) ───────────────────────────────────────

namespace {

constexpr std::size_t kMaxDepth = 64;             // levels in a free-form part (sod_state.MAX_DEPTH)
constexpr std::size_t kTextDepth = kMaxDepth + 1;  // …and in the whole text (the state object is one more)

// Builds the document as the parser reports it, and refuses what the oracle's reader refuses: a key twice in one
// object, nesting past `max_depth`, an integer outside 64 bits, a number that is out of range. Iterative: nothing
// here recurses on the file's depth.
class Builder final : public Json::json_sax_t {
 public:
    explicit Builder(std::size_t max_depth) : max_depth_(max_depth) {}
    Json root;
    std::string error;  // set when a rule above (not the grammar) stopped the parse

    bool null() override { return place(Json(nullptr)); }
    bool boolean(bool v) override { return place(Json(v)); }
    bool number_integer(number_integer_t v) override { return place(Json(v)); }
    bool number_unsigned(number_unsigned_t v) override { return place(Json(v)); }
    bool number_float(number_float_t v, const string_t& text) override {
        if (!std::isfinite(v)) {
            return fail("a number is out of range");
        }
        if (text.find_first_of(".eE") == std::string::npos) {  // an integer the parser could not keep as one
            return fail("an integer outside 64 bits");
        }
        return place(Json(v));
    }
    bool string(string_t& v) override { return place(Json(v)); }
    bool binary(binary_t&) override { return fail("a value is not JSON-typed"); }
    bool start_object(std::size_t) override {
        if (!open(Json::object())) {
            return false;
        }
        keys_.emplace_back();
        return true;
    }
    bool key(string_t& k) override {
        if (!keys_.back().insert(k).second) {
            return fail("duplicate key " + esc(k));
        }
        slot_ = &(*stack_.back())[k];
        return true;
    }
    bool end_object() override {
        stack_.pop_back();
        keys_.pop_back();
        return true;
    }
    bool start_array(std::size_t) override { return open(Json::array()); }
    bool end_array() override {
        stack_.pop_back();
        return true;
    }
    bool parse_error(std::size_t, const std::string& token, const Json::exception& ex) override {
        if (error.empty()) {
            if (ex.id == 406) {  // the parser's own overflow check, before it calls number_float
                const bool integer = !token.empty() && token.find_first_not_of("-0123456789") == std::string::npos;
                error = integer ? "an integer outside 64 bits" : "a number is out of range";
            } else {
                error = "invalid JSON: " + std::string(ex.what());
            }
        }
        return false;
    }

 private:
    bool fail(std::string what) {
        error = std::move(what);
        return false;
    }
    Json* put(Json&& v) {
        if (stack_.empty()) {
            root = std::move(v);
            return &root;
        }
        Json* parent = stack_.back();
        if (parent->is_array()) {
            parent->push_back(std::move(v));
            return &parent->back();
        }
        *slot_ = std::move(v);
        return slot_;
    }
    bool place(Json&& v) {
        put(std::move(v));
        return true;
    }
    bool open(Json&& container) {
        if (stack_.size() + 1 > max_depth_) {
            return fail("the text is nested more than " + std::to_string(max_depth_) + " levels deep");
        }
        stack_.push_back(put(std::move(container)));
        return true;
    }

    std::size_t max_depth_;
    std::vector<Json*> stack_;
    std::vector<std::unordered_set<std::string>> keys_;
    Json* slot_ = nullptr;
};

// A bundle file's `sod_state` member, read by the same reader as a state text and nothing else of the file: the
// document is parsed again as events, the member is built by a Builder (so its depth, big integers, repeated keys and
// numbers out of range are refused as in a text), and everything outside it is only counted past. A grammar error
// outside the member is left to the bundle's own parse.
class Extract final : public Json::json_sax_t {
 public:
    Extract() : inner_(kTextDepth) {}
    Builder inner_;
    bool found = false;

    bool null() override { return leaf([&] { return inner_.null(); }); }
    bool boolean(bool v) override { return leaf([&] { return inner_.boolean(v); }); }
    bool number_integer(number_integer_t v) override { return leaf([&] { return inner_.number_integer(v); }); }
    bool number_unsigned(number_unsigned_t v) override { return leaf([&] { return inner_.number_unsigned(v); }); }
    bool number_float(number_float_t v, const string_t& t) override {
        return leaf([&] { return inner_.number_float(v, t); });
    }
    bool string(string_t& v) override { return leaf([&] { return inner_.string(v); }); }
    bool binary(binary_t& v) override { return leaf([&] { return inner_.binary(v); }); }
    bool start_object(std::size_t n) override { return open([&] { return inner_.start_object(n); }); }
    bool start_array(std::size_t n) override { return open([&] { return inner_.start_array(n); }); }
    bool key(string_t& k) override {
        if (in_) {
            return inner_.key(k);
        }
        if (level_ == 1) {
            pending_ = k == "sod_state";
        }
        return true;
    }
    bool end_object() override { return close([&] { return inner_.end_object(); }); }
    bool end_array() override { return close([&] { return inner_.end_array(); }); }
    bool parse_error(std::size_t position, const std::string& token, const Json::exception& ex) override {
        return in_ ? inner_.parse_error(position, token, ex) : false;
    }

 private:
    // Is this value the member's (or inside it)? Called once per value.
    bool takes() {
        if (in_) {
            return true;
        }
        if (level_ == 1 && pending_) {
            pending_ = false;
            found = true;
            return true;
        }
        return false;
    }
    template <typename F>
    bool leaf(F f) {
        return takes() ? f() : true;
    }
    template <typename F>
    bool open(F f) {
        const bool feed = takes();
        ++level_;
        if (!feed) {
            return true;
        }
        in_ = true;
        return f();
    }
    template <typename F>
    bool close(F f) {
        --level_;
        if (!in_) {
            return true;
        }
        const bool ok = f();
        if (level_ == 1) {
            in_ = false;  // the member's own container is closed
        }
        return ok;
    }

    std::size_t level_ = 0;
    bool in_ = false;
    bool pending_ = false;
};

// The same refusals for a state text and for the state a C++ strategy hands back.
Json parse_text(std::string_view text, std::size_t max_depth) {
    if (text.size() > kSodStateMaxBytes) {
        throw RunError("sod_state: the text is larger than " + std::to_string(kSodStateMaxBytes) + " bytes");
    }
    if (text.find('\0') != std::string_view::npos) {
        throw RunError("sod_state: the text has a NUL character");
    }
    if (text.starts_with("\xEF\xBB\xBF")) {
        throw RunError("sod_state: invalid JSON: a byte order mark comes first");
    }
    Builder builder(max_depth);
    const bool ok = Json::sax_parse(text.begin(), text.end(), &builder, Json::input_format_t::json, true, false);
    if (!ok || !builder.error.empty()) {
        throw RunError("sod_state: " + (builder.error.empty() ? std::string("invalid JSON") : builder.error));
    }
    return std::move(builder.root);
}

// A free-form part (the strategy's state, the queued notifications): bounded depth, checked without recursion
// (sod_state._free_form; integers and floats were bounded when the text was read).
void check_free_form(const Json& root, std::string_view owner) {
    std::vector<std::pair<const Json*, std::size_t>> stack{{&root, 1}};
    while (!stack.empty()) {
        const auto [v, level] = stack.back();
        stack.pop_back();
        if (!v->is_array() && !v->is_object()) {
            continue;
        }
        if (level > kMaxDepth) {
            throw RunError("sod_state: " + std::string(owner) + " is nested more than " + std::to_string(kMaxDepth) +
                           " levels deep");
        }
        for (const Json& child : *v) {
            stack.emplace_back(&child, level + 1);
        }
    }
}

// ── Restore (sod_state.parse / restore) ──────────────────────────────────────

constexpr std::int64_t kDayNs = 86'400LL * 1'000'000'000LL;

// A decimal that is the one spelling of its number: ASCII digits, no leading zero, at most 10 of them.
bool is_decimal(std::string_view s) {
    return !s.empty() && s.size() <= 10 && (s[0] != '0' || s == "0") &&
           std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; });
}

// int(key) for an object keyed by an instrument id in decimal.
InstrumentId iid_key(const std::string& key, std::string_view where) {
    if (!is_decimal(key) || std::stoll(key) > 4294967295LL) {
        throw bad(where, "key " + esc(key) + " is not an instrument id");
    }
    return static_cast<InstrumentId>(std::stoll(key));
}

// The first instant after an IST calendar day: its end, as UTC ns.
TimeNs day_after(Date d) {
    return (days_from_civil(d / 10000, d / 100 % 100, d % 100) + 1) * kDayNs - kIstOffsetNs;
}

std::optional<Snapshot> snapshot_from(const Json& v) {  // sod_state._snapshot
    if (v.is_null()) {
        return std::nullopt;
    }
    need(v, kSnapshotFields, "market snapshot");
    Snapshot s;
    s.t = as_int(v.at("t"), "market snapshot.t");
    s.ltp = as_nonneg(v.at("ltp"), "market snapshot.ltp");
    s.volume = as_nonneg(v.at("volume"), "market snapshot.volume");
    s.atp = as_nonneg(v.at("atp"), "market snapshot.atp");
    s.high = as_nonneg(v.at("high"), "market snapshot.high");
    s.low = as_nonneg(v.at("low"), "market snapshot.low");
    return s;
}

// sod_state._read_order: the same checks in the same order.
Order order_from(const Json& o, std::string_view group, const InstrumentTable& ids, std::int64_t next_order,
                 std::set<OrderId>& seen) {
    need(o, kOrderFields, group);
    const std::string where = std::string(group) + ".";
    Order out;
    const std::int64_t id = as_atleast1(o.at("order_id"), where + "order_id");
    out.order_id = static_cast<OrderId>(id);
    if (!seen.insert(out.order_id).second) {
        throw RunError("sod_state: " + std::string(group) + ": order " + std::to_string(id) + " is listed twice");
    }
    if (id >= next_order) {
        throw RunError("sod_state: " + std::string(group) + ": order " + std::to_string(id) +
                       " is not below counters.next_order (" + std::to_string(next_order) + ")");
    }
    const std::string& status = as_str(o.at("status"), where + "status");
    const std::optional<OrderStatus> st = parse_order_status(status);
    if (!st) {
        throw RunError("sod_state: " + std::string(group) + ": unknown status " + esc(status));
    }
    const std::string& reason = as_str(o.at("reason"), where + "reason");
    const std::optional<Reason> rs = parse_reason(reason);
    if (!rs) {
        throw RunError("sod_state: " + std::string(group) + ": unknown reason " + esc(reason));
    }
    out.status = *st;
    out.reason = *rs;
    if (group == "pending_orders" && out.final()) {
        throw RunError("sod_state: " + std::string(group) + ": order " + std::to_string(id) + " has the final status " +
                       esc(status));
    }
    if (group == "final_orders" && !out.final()) {
        throw RunError("sod_state: " + std::string(group) + ": order " + std::to_string(id) + " has the live status " +
                       esc(status));
    }
    // A rejected order keeps what the strategy placed it with, valid or not (orders.csv writes it as given); every
    // other order was accepted, so its words and numbers are the engine's own.
    const bool placed = out.status != OrderStatus::Rejected;
    out.parent_id = static_cast<OrderId>(as_nonneg(o.at("parent_id"), where + "parent_id"));
    out.instrument = as_iid(o.at("instrument_id"), where + "instrument_id");
    if (group == "pending_orders" && !ids.contains(out.instrument)) {
        throw bad(where + "instrument_id", std::to_string(out.instrument) + " is not in the bundle");
    }
    const auto known = [&](const std::string& text, bool ok, std::string_view what) {
        if (placed && !ok) {
            throw RunError("sod_state: " + std::string(group) + ": unknown " + std::string(what) + " " + esc(text));
        }
    };
    out.side_text = as_str(o.at("side"), where + "side");
    known(out.side_text, parse_side(out.side_text).has_value(), "side");
    out.qty = placed ? as_pos(o.at("qty"), where + "qty") : as_int(o.at("qty"), where + "qty");
    out.product_text = as_str(o.at("product"), where + "product");
    out.type_text = as_str(o.at("type"), where + "type");
    out.validity_text = as_str(o.at("validity"), where + "validity");
    known(out.product_text, parse_product(out.product_text).has_value(), "product");
    known(out.type_text, parse_order_type(out.type_text).has_value(), "type");
    known(out.validity_text, parse_validity(out.validity_text).has_value(), "validity");
    // The enums are meaningful only for an order that passed the checks above (orders.hpp).
    out.side = parse_side(out.side_text).value_or(Side::Buy);
    out.product = parse_product(out.product_text).value_or(Product::CNC);
    out.type = parse_order_type(out.type_text).value_or(OrderType::Market);
    out.validity = parse_validity(out.validity_text).value_or(Validity::Day);
    out.price = as_int(o.at("price"), where + "price");
    out.trigger = as_int(o.at("trigger"), where + "trigger");
    const Json& prot = o.at("protection");
    if (prot.is_string() && prot.get_ref<const std::string&>() == "AUTO") {
        out.protection.reset();
    } else {
        out.protection = as_i32(prot, where + "protection");
    }
    out.tag = as_str(o.at("tag"), where + "tag");
    out.t_submit = as_int(o.at("t_submit"), where + "t_submit");
    out.t_activate = as_int(o.at("t_activate"), where + "t_activate");
    out.filled_qty = as_nonneg(o.at("filled_qty"), where + "filled_qty");
    if (placed && out.filled_qty > out.qty) {
        throw bad(where + "filled_qty", "must not be more than qty");
    }
    out.fill_value = as_int(o.at("fill_value"), where + "fill_value");
    out.limit = as_opt_int(o.at("limit"), where + "limit");
    out.from_market = as_bool(o.at("from_market"), where + "from_market");
    out.triggered = as_bool(o.at("triggered"), where + "triggered");
    if (!o.at("protection_bp").is_null()) {
        out.protection_bp = as_i32(o.at("protection_bp"), where + "protection_bp");
    }
    out.reserved = as_nonneg(o.at("reserved"), where + "reserved");
    out.first_eligible_done = as_bool(o.at("first_eligible_done"), where + "first_eligible_done");
    out.trade_date = as_opt_date(o.at("trade_date"), where + "trade_date");
    out.charged = as_bool(o.at("charged"), where + "charged");
    out.eod_after = as_opt_date(o.at("eod_after"), where + "eod_after");
    return out;
}

Pcg32 stream_from(const Json& v, const std::string& name) {  // sod_state._stream
    const std::string where = "rng." + name;
    const Json& pair = as_list(v, where, 2);
    std::uint64_t parts[2]{};
    const char* what[] = {"state", "inc"};
    for (std::size_t i = 0; i < 2; ++i) {
        const Json& x = pair[i];
        if (!x.is_number_integer() || (!x.is_number_unsigned() && x.get<std::int64_t>() < 0)) {
            throw bad(where, std::string(what[i]) + " must be a non-negative 64-bit integer");
        }
        parts[i] = x.get<std::uint64_t>();
    }
    if (parts[1] % 2 == 0) {
        throw bad(where, "inc must be odd");
    }
    return Pcg32::at(parts[0], parts[1]);
}

MarketState market_from(const Json& m) {
    need(m, kMarketFields, "market");
    MarketState out;
    out.ltp = as_opt_nonneg(m.at("ltp"), "market.ltp");
    out.t = as_opt_int(m.at("t"), "market.t");
    out.s0 = snapshot_from(m.at("s0"));
    out.s1 = snapshot_from(m.at("s1"));
    out.bid = as_opt_nonneg(m.at("bid"), "market.bid");
    out.ask = as_opt_nonneg(m.at("ask"), "market.ask");
    if (const Json& book = m.at("book"); !book.is_null()) {
        std::array<DepthLevel, 10> levels{};
        const Json& rows = as_list(book, "market.book", 10);
        for (std::size_t i = 0; i < 10; ++i) {
            const Json& lv = as_list(rows[i], "market.book level", 3);
            levels[i] = DepthLevel{as_nonneg(lv[0], "market.book price"), as_nonneg(lv[1], "market.book qty"),
                                   as_i32(lv[2], "market.book orders")};
        }
        out.book = levels;
    }
    if (const Json& band = m.at("band"); !band.is_null()) {
        const Json& pair = as_list(band, "market.band", 2);
        out.band = std::make_pair(as_nonneg(pair[0], "market.band lower"), as_nonneg(pair[1], "market.band upper"));
    }
    out.oi = as_opt_nonneg(m.at("oi"), "market.oi");
    if (!m.at("min_interval").is_null()) {
        const std::int32_t v = as_i32(m.at("min_interval"), "market.min_interval");
        if (v <= 0) {
            throw bad("market.min_interval", "must be positive");
        }
        out.min_interval = v;
    }
    out.day = m.at("day").is_null() ? 0 : as_date(m.at("day"), "market.day");
    return out;
}

// Everything a valid state holds, built without touching the engine; the commit below cannot fail.
struct Staged {
    Date day = 0;
    std::optional<Ledger> ledger;
    std::map<OrderId, Order> orders;
    std::vector<OrderId> live_ids;
    std::set<OrderId> prior_final;
    std::set<std::pair<InstrumentId, Date>> dp_done;
    std::uint64_t next_order = 1, next_fill = 1, next_timer = 1;
    std::int64_t charge_row = 0;
    std::optional<Pcg32> slippage, strategy_rng;
    std::vector<std::tuple<std::uint64_t, TimeNs, TimeNs>> timers;
    struct Built {
        InstrumentId iid;
        std::int32_t isec;
        TimeNs start;
        std::array<std::int64_t, 6> bar;
    };
    std::vector<Built> built;
    std::set<InstrumentId> sub_ticks;
    std::map<InstrumentId, std::set<std::int32_t>> sub_bars;
    bool strategy_enabled = true;
    std::set<std::string> pause_reasons;
    Json queued = Json::array();
    std::map<std::pair<InstrumentId, Date>, Paise> fsp_seen;
    std::map<InstrumentId, MarketState> market;
    std::string strategy_text;   // as handed to load_state
    std::string strategy_canon;  // canonical, for eod_state before a run
};

}  // namespace

Staged SodState::parse(const Engine& e, const Json& state) {  // sod_state.parse
    if (!state.is_object() || !state.contains("schema")) {
        throw RunError("sod_state: state must be an object with a 'schema'");
    }
    const Json& schema = state.at("schema");
    if (!schema.is_string() || schema.get_ref<const std::string&>() != kSchema) {
        throw RunError("sod_state: schema must be " + py_repr(kSchema) + ", got " +
                       (schema.is_string() ? esc(schema.get_ref<const std::string&>()) : std::string("a non-string")));
    }
    need(state, kKeys, "state");
    const InstrumentTable& ids = e.instruments_;
    Staged s;
    const Date first_day = e.calendar_.days().front().date;
    s.day = as_date(state.at("trade_date"), "trade_date");
    if (s.day >= first_day) {
        throw RunError("sod_state: trade_date " + iso_date(s.day) + " must be before the bundle's first day " +
                       iso_date(first_day));
    }
    const TimeNs after = day_after(s.day);
    const std::string day_text = iso_date(s.day);

    const Json& c = state.at("counters");
    need(c, kCounters, "counters");
    s.next_order = static_cast<std::uint64_t>(as_counter(c.at("next_order"), "counters.next_order", 1));
    s.next_fill = static_cast<std::uint64_t>(as_counter(c.at("next_fill"), "counters.next_fill", 1));
    s.next_timer = static_cast<std::uint64_t>(as_counter(c.at("next_timer"), "counters.next_timer", 1));
    s.charge_row = as_counter(c.at("charge_row"), "counters.charge_row", 0);

    const std::int64_t cash = as_int(state.at("cash_paise"), "cash_paise");
    const Json& led_doc = state.at("ledger");
    need(led_doc, kLedgerFields, "ledger");
    const std::int64_t capital = as_int(led_doc.at("capital_paise"), "ledger.capital_paise");
    s.ledger.emplace(cash, capital);
    Ledger& led = *s.ledger;
    led.charges_total = as_int(led_doc.at("charges_total_paise"), "ledger.charges_total_paise");
    led.reserved = as_int(led_doc.at("reserved_paise"), "ledger.reserved_paise");
    std::set<std::pair<InstrumentId, Product>> opened;
    for (const Json& p : as_list(led_doc.at("positions"), "ledger.positions")) {
        need(p, kPositionFields, "ledger position");
        const InstrumentId iid = as_iid(p.at("instrument_id"), "ledger position.instrument_id");
        if (!ids.contains(iid)) {
            throw bad("ledger position.instrument_id", std::to_string(iid) + " is not in the bundle");
        }
        const std::string& product = as_str(p.at("product"), "ledger position.product");
        const std::optional<Product> prod = parse_product(product);
        if (!prod) {
            throw RunError("sod_state: ledger position: unknown product " + esc(product));
        }
        const std::string& style = as_str(p.at("style"), "ledger position.style");
        if (style != "premium" && style != "variation") {
            throw RunError("sod_state: ledger position: unknown style " + esc(style));
        }
        const std::int64_t q = as_nonneg(p.at("q"), "ledger position.q");
        const std::int64_t d = as_int(p.at("d"), "ledger position.d");
        if (d != 1 && d != -1) {
            throw bad("ledger position.d", "must be 1 or -1");
        }
        const std::int64_t cost = as_nonneg(p.at("c"), "ledger position.c");
        if (q == 0 && cost != 0) {
            throw bad("ledger position.c", "must be 0 when q is 0");
        }
        const std::int64_t realised = as_int(p.at("realised"), "ledger position.realised");
        const std::int64_t vm = as_int(p.at("vm"), "ledger position.vm");
        const std::optional<std::int64_t> last_mark = as_opt_int(p.at("last_mark"), "ledger position.last_mark");
        const std::string& mark_source = as_str(p.at("mark_source"), "ledger position.mark_source");
        if (!opened.insert({iid, *prod}).second) {
            throw RunError("sod_state: ledger position " + std::to_string(iid) + " " + product + " is listed twice");
        }
        Position& pos = led.get_or_open(iid, *prod, style == "premium" ? CashStyle::Premium : CashStyle::Variation);
        pos.q = q;
        pos.d = static_cast<std::int32_t>(d);
        pos.c = cost;
        pos.realised = realised;
        pos.vm = vm;
        pos.last_mark = last_mark;
        pos.mark_source = mark_source;
    }

    std::set<OrderId> seen;
    for (const char* group : {"pending_orders", "final_orders"}) {
        for (const Json& o : as_list(state.at(group), group)) {
            Order ord = order_from(o, group, ids, static_cast<std::int64_t>(s.next_order), seen);
            if (ord.final()) {
                s.prior_final.insert(ord.order_id);
            } else {
                s.live_ids.push_back(ord.order_id);
            }
            s.orders.emplace(ord.order_id, std::move(ord));
        }
    }
    std::sort(s.live_ids.begin(), s.live_ids.end());

    for (const Json& row : as_list(state.at("dp_done"), "dp_done")) {
        const Json& pair = as_list(row, "dp_done row", 2);
        const InstrumentId iid = as_iid(pair[0], "dp_done instrument_id");
        if (!ids.contains(iid)) {
            throw bad("dp_done instrument_id", std::to_string(iid) + " is not in the bundle");
        }
        s.dp_done.insert({iid, as_date(pair[1], "dp_done trade_date")});
    }

    std::set<std::uint64_t> timer_ids;
    for (const Json& row : as_list(state.at("timers"), "timers")) {
        const Json& t = as_list(row, "timer", 3);
        const auto tid = static_cast<std::uint64_t>(as_atleast1(t[0], "timer id"));
        const TimeNs at = as_int(t[1], "timer t_ns");
        const TimeNs period = as_nonneg(t[2], "timer period_ns");
        if (at > 0 && period > INT64_MAX - at) {
            throw RunError("sod_state: timer " + std::to_string(tid) + ": t_ns + period_ns does not fit in int64");
        }
        if (at < after) {
            throw RunError("sod_state: timer " + std::to_string(tid) + " is not after the saved day " + day_text);
        }
        if (!timer_ids.insert(tid).second) {
            throw RunError("sod_state: timer " + std::to_string(tid) + " is listed twice");
        }
        if (tid >= s.next_timer) {
            throw RunError("sod_state: timer " + std::to_string(tid) + " is not below counters.next_timer (" +
                           std::to_string(s.next_timer) + ")");
        }
        s.timers.emplace_back(tid, at, period);
    }

    std::set<std::tuple<InstrumentId, std::int32_t, TimeNs>> built_keys;
    for (const Json& row : as_list(state.at("built_bars"), "built_bars")) {
        const Json& b = as_list(row, "built bar", 9);
        const InstrumentId iid = as_iid(b[0], "built bar instrument_id");
        if (!ids.contains(iid)) {
            throw bad("built bar instrument_id", std::to_string(iid) + " is not in the bundle");
        }
        const std::int32_t isec = as_i32(b[1], "built bar interval_s");
        if (isec <= 0) {
            throw bad("built bar interval_s", "must be positive");
        }
        const TimeNs start = as_int(b[2], "built bar start_ns");
        const TimeNs span = static_cast<TimeNs>(isec) * kNsPerSecond;
        if (start > 0 && span > INT64_MAX - start) {
            throw RunError("sod_state: built bar: start_ns + interval_s does not fit in int64");
        }
        if (start + span < after) {
            throw RunError("sod_state: built bar " + std::to_string(iid) + " " + std::to_string(isec) +
                           " is not after the saved day " + day_text);
        }
        Staged::Built built{iid, isec, start, {}};
        for (std::size_t i = 0; i < 6; ++i) {
            built.bar[i] = as_nonneg(b[3 + i], "built bar value");
        }
        if (!built_keys.insert({iid, isec, start}).second) {
            throw RunError("sod_state: built bar " + std::to_string(iid) + " " + std::to_string(isec) + " " +
                           std::to_string(start) + " is listed twice");
        }
        s.built.push_back(built);
    }

    const Json& rng = state.at("rng");
    need(rng, kRngFields, "rng");
    s.slippage = stream_from(rng.at("slippage"), "slippage");
    s.strategy_rng = stream_from(rng.at("strategy"), "strategy");

    const Json& subs = state.at("subscriptions");
    need(subs, kSubscriptionFields, "subscriptions");
    for (const Json& v : as_list(subs.at("ticks"), "subscriptions.ticks")) {
        const InstrumentId iid = as_iid(v, "subscriptions.ticks");
        if (!ids.contains(iid)) {
            throw bad("subscriptions.ticks", std::to_string(iid) + " is not in the bundle");
        }
        s.sub_ticks.insert(iid);
    }
    if (!subs.at("bars").is_object()) {
        throw RunError("sod_state: subscriptions.bars must be an object");
    }
    for (const auto& [key, intervals] : subs.at("bars").items()) {
        const InstrumentId iid = iid_key(key, "subscriptions.bars");
        if (!ids.contains(iid)) {
            throw bad("subscriptions.bars", "key " + esc(key) + " is not in the bundle");
        }
        std::set<std::int32_t>& mine = s.sub_bars[iid];
        for (const Json& v : as_list(intervals, "subscriptions.bars")) {
            const std::int32_t isec = as_i32(v, "subscriptions.bars interval_s");
            if (isec <= 0) {
                throw bad("subscriptions.bars interval_s", "must be positive");
            }
            mine.insert(isec);
        }
    }

    s.strategy_enabled = as_bool(state.at("strategy_enabled"), "strategy_enabled");
    for (const Json& r : as_list(state.at("pause_reasons"), "pause_reasons")) {
        s.pause_reasons.insert(as_str(r, "pause_reasons"));
    }
    // Checked in place, before the copy: a copy of a deeply nested value recurses (a bundle file's DOM keeps any depth).
    check_free_form(as_list(state.at("queued_notifications"), "queued_notifications"), "queued_notifications");
    s.queued = state.at("queued_notifications");

    for (const Json& row : as_list(state.at("fsp_seen"), "fsp_seen")) {
        const Json& f = as_list(row, "fsp_seen row", 3);
        const InstrumentId uid = as_iid(f[0], "fsp_seen underlying_id");
        if (!ids.contains(uid)) {
            throw bad("fsp_seen underlying_id", std::to_string(uid) + " is not in the bundle");
        }
        const Date d = as_date(f[1], "fsp_seen date");
        const Paise px = as_int(f[2], "fsp_seen paise");
        if (!s.fsp_seen.emplace(std::make_pair(uid, d), px).second) {
            throw RunError("sod_state: fsp_seen " + std::to_string(uid) + " " + iso_date(d) + " is listed twice");
        }
    }

    if (!state.at("market").is_object()) {
        throw RunError("sod_state: market must be an object");
    }
    for (const auto& [key, m] : state.at("market").items()) {
        const InstrumentId iid = iid_key(key, "market");
        if (!ids.contains(iid)) {
            throw bad("market", "key " + esc(key) + " is not in the bundle");
        }
        s.market[iid] = market_from(m);
    }

    if (!state.at("bar_history").is_object()) {
        throw RunError("sod_state: bar_history must be an object");
    }
    for (const auto& [key, rows] : state.at("bar_history").items()) {
        const std::size_t colon = key.find(':');
        const std::string iid_text = key.substr(0, colon);
        const std::string isec_text = colon == std::string::npos ? "" : key.substr(colon + 1);
        if (colon == std::string::npos || !is_decimal(iid_text) || !is_decimal(isec_text) ||
            std::stoll(iid_text) > 4294967295LL || std::stoll(isec_text) <= 0 || std::stoll(isec_text) > INT32_MAX) {
            throw bad("bar_history", "key " + esc(key) + " is not <instrument_id>:<interval_s>");
        }
        const auto iid = static_cast<InstrumentId>(std::stoll(iid_text));
        const auto isec = static_cast<std::int32_t>(std::stoll(isec_text));
        if (!ids.contains(iid)) {
            throw bad("bar_history", "key " + esc(key) + " is not in the bundle");
        }
        const auto market = s.market.find(iid);
        if (market == s.market.end()) {
            throw bad("bar_history", "key " + esc(key) + " has no market entry");
        }
        std::vector<BarRow> bars;
        for (const Json& row : as_list(rows, "bar_history")) {
            const Json& r = as_list(row, "bar_history row", 7);
            BarRow b{};
            b.start = as_int(r[0], "bar start_ns");
            b.open = as_nonneg(r[1], "bar open");
            b.high = as_nonneg(r[2], "bar high");
            b.low = as_nonneg(r[3], "bar low");
            b.close = as_nonneg(r[4], "bar close");
            b.volume = as_nonneg(r[5], "bar volume");
            b.oi = as_nonneg(r[6], "bar oi");
            if (!bars.empty() && b.start <= bars.back().start) {
                throw RunError("sod_state: bar_history " + key + ": start_ns must rise from row to row");
            }
            bars.push_back(b);
        }
        market->second.bars[isec] = std::move(bars);
    }

    if (!state.at("strategy").is_object()) {
        throw RunError("sod_state: strategy must be an object");
    }
    check_free_form(state.at("strategy"), "strategy");
    s.strategy_text = state.at("strategy").dump();
    s.strategy_canon = dumps_canonical(to_jvalue(state.at("strategy")));
    return s;
}

void SodState::restore(Engine& e, const Json& state) {  // sod_state.restore
    Staged s = parse(e, state);
    // The strategy's part comes first among the changes: it is the one step that runs code we did not write, so
    // its failure must leave the engine untouched. Without a strategy yet, set_strategy hands it over (and a
    // failure there is final for that engine). Everything below cannot fail.
    if (e.strategy_) {
        try {
            e.strategy_->load_state(s.strategy_text);
        } catch (const RunError&) {
            throw;
        } catch (const std::exception& ex) {
            throw RunError("strategy " + (e.strategy_name_.empty() ? std::string() : e.strategy_name_ + ".") +
                           "load_state() raised: " + ex.what());
        }
    } else {
        e.pending_strategy_state_ = s.strategy_text;
    }
    Book& book = e.book_;
    book.ledger = std::move(*s.ledger);
    book.orders = std::move(s.orders);
    e.prior_final_ = std::move(s.prior_final);
    e.live_ids_ = std::move(s.live_ids);
    book.dp_done = std::move(s.dp_done);
    e.next_order_ = s.next_order;
    book.next_fill = s.next_fill;
    e.next_timer_ = s.next_timer;
    e.charge_row_base_ = s.charge_row;
    book.rng = *s.slippage;
    e.strategy_rng_ = *s.strategy_rng;
    e.timers_.clear();
    for (const auto& [tid, at, period] : s.timers) {
        e.timers_[tid] = {at, period};
        InternalEvent ev{EventKey{at, kRankTimer, 0, 0}, InternalKind::Timer};
        ev.timer = tid;
        e.push_internal(std::move(ev));
    }
    for (const Staged::Built& b : s.built) {
        e.built_[{b.iid, b.isec}][b.start] = b.bar;
        InternalEvent ev{EventKey{b.start + static_cast<TimeNs>(b.isec) * kNsPerSecond, kRankBarBuilt, 0, 0},
                         InternalKind::BarBuilt};
        ev.instrument = b.iid;
        ev.interval_s = b.isec;
        ev.start = b.start;
        e.push_internal(std::move(ev));
    }
    e.sub_ticks_ = std::move(s.sub_ticks);
    e.sub_bars_ = std::move(s.sub_bars);
    e.strategy_enabled_ = s.strategy_enabled;
    e.pause_reasons_ = std::move(s.pause_reasons);
    e.queued_notifications_ = std::move(s.queued);
    e.fsp_seen_ = std::move(s.fsp_seen);
    e.market_ = std::move(s.market);
    e.underlying_last_.clear();
    for (const auto& [iid, m] : e.market_) {
        if (m.ltp) {
            e.underlying_last_[iid] = *m.ltp;
        }
    }
    e.state_date_ = s.day;
    e.orders_drained_ = e.next_order_;  // loaded orders were journaled on their own day (drain_outputs)
    // Until the engine runs, the state it would write for the strategy is the one it was handed (start() clears it).
    e.day_end_strategy_state_ = std::move(s.strategy_canon);
    e.settled_ = true;
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
    if (e.resumed_) {
        throw RunError("sod_state is already loaded");
    }
    load_sod_state_json(e, parse_text(text, kTextDepth));
}

Json parse_strategy_state(std::string_view text) { return parse_text(text, kMaxDepth); }

std::optional<Json> bundle_sod_state(std::string_view bundle_text) {
    // Always run: a key may be spelled with a JSON escape for one of its letters, which the bundle's own parse
    // decodes, so looking for the literal text would let that spelling skip the checked reader.
    Extract extract;
    const bool ok = Json::sax_parse(bundle_text.begin(), bundle_text.end(), &extract, Json::input_format_t::json, true,
                                    false);
    if (!ok) {
        if (!extract.inner_.error.empty()) {
            throw RunError("sod_state: " + extract.inner_.error);
        }
        return std::nullopt;  // a grammar error elsewhere: the bundle's own parse reports it
    }
    if (!extract.found) {
        return std::nullopt;
    }
    return std::move(extract.inner_.root);
}

}  // namespace stocksim
