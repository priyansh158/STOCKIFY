// orb_cpp: stockify.strategies.opening_range_breakout.OpeningRangeBreakout in C++ (M3.10).
// Same params, same orders, same artifacts: a run with either one must be byte-identical.
#include <algorithm>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/notes.hpp"
#include "stocksim/session.hpp"
#include "stocksim/strategy.hpp"
#include "stocksim/strategy_state.hpp"

namespace stocksim::strategies {

class OrbCpp final : public Strategy {
 public:
    void on_start(Context& ctx) override {
        const Json& p = ctx.params();
        const std::string symbol = param_string(p, "symbol");
        const auto iid = ctx.resolve(symbol);
        if (!iid) {
            throw RunError(symbol + " is not in this run's bundle");
        }
        iid_ = *iid;
        symbol_ = symbol;
        minutes_ = param_int(p, "range_minutes", 15);
        qty_ = param_int(p, "qty");
        short_ = param_bool(p, "short", false);
        exit_hhmm_ = param_string(p, "exit_hhmm", "15:10");
        day_.reset();
        configured_ = true;
        ctx.subscribe_bars(iid_, 60);
    }

    void on_bar(Context& ctx, const Bar& bar) override {
        if (ctx.today() != day_) {
            new_day(ctx);
        }
        if (bar.start_ns < open_ns_) {
            return;
        }
        if (bar.start_ns < checked_add(open_ns_, checked_mul(checked_mul(minutes_, 60), kNsPerSecond))) {
            hi_ = hi_ ? std::max(*hi_, bar.high) : bar.high;
            lo_ = lo_ ? std::min(*lo_, bar.low) : bar.low;
            return;
        }
        if (!hi_ || closed_) {
            return;
        }
        ctx.plot("range_high", *hi_, "price", iid_);  // paise: the range as the bars after it see it
        ctx.plot("range_low", *lo_, "price", iid_);
        if (traded_) {
            return;
        }
        const Spec* spec = ctx.spec(iid_);
        if (spec == nullptr) {
            throw RunError("orb_cpp: instrument " + std::to_string(iid_) + " has no spec today");
        }
        const Paise tick = spec->tick;
        if (bar.close > *hi_) {
            entry_id_ = place(ctx, "BUY", "MARKET", 0, "orb_long",
                               "Bought " + fmt_qty(qty_) + " " + symbol_ + ": price " + fmt_price(bar.close) +
                                   " broke above the " + std::to_string(minutes_) + "-minute opening range high of " +
                                   fmt_price(*hi_));
            stop_side_ = "SELL";
            stop_trigger_ = checked_mul(floor_div(*lo_, tick), tick);
            traded_ = true;
        } else if (short_ && bar.close < *lo_) {
            entry_id_ = place(ctx, "SELL", "MARKET", 0, "orb_short",
                               "Sold short " + fmt_qty(qty_) + " " + symbol_ + ": price " + fmt_price(bar.close) +
                                   " broke below the " + std::to_string(minutes_) + "-minute opening range low of " +
                                   fmt_price(*lo_));
            stop_side_ = "BUY";
            stop_trigger_ = checked_mul(ceil_div(*hi_, tick), tick);
            traded_ = true;
        }
    }

    // The stop exists only for what the entry has filled: one SL-M of exactly each fill's quantity.
    void on_fill(Context& ctx, const FillView& fill) override {
        if (entry_id_ && fill.order_id == *entry_id_) {
            const std::string qty = fmt_qty(fill.qty);
            const std::string trigger = fmt_price(stop_trigger_);
            std::string note = *stop_side_ == "SELL"
                                   ? "Stop-loss for the " + qty + " bought: sells if price falls to " + trigger
                                   : "Stop-loss for the " + qty + " sold short: buys back if price rises to " + trigger;
            stop_ids_.push_back(place(ctx, stop_side_->c_str(), "SL-M", stop_trigger_, "orb_stop", std::move(note), fill.qty));
        } else if (std::find(stop_ids_.begin(), stop_ids_.end(), fill.order_id) != stop_ids_.end()) {
            const std::optional<PositionView> pos = ctx.position(iid_, "MIS");  // a stop filled: the day is closed once flat
            closed_ = !(pos && pos->qty != 0);
        }
    }

    void on_timer(Context& ctx, std::uint64_t /*timer_id*/) override {
        closed_ = true;  // the time exit ends the day's lines
        for (const OrderId id : stop_ids_) {
            ctx.cancel(id);
        }
        const std::optional<PositionView> pos = ctx.position(iid_, "MIS");
        if (pos && pos->qty != 0) {
            const Qty units = pos->qty > 0 ? pos->qty : checked_sub(0, pos->qty);
            place(ctx, pos->qty > 0 ? "SELL" : "BUY", "MARKET", 0, "orb_exit",
                  "Closed " + fmt_qty(units) + " " + symbol_ + " at the " + exit_hhmm_ + " time exit", units);
        }
    }

    // A paper session's day-to-day state (A2 §4.1): the attributes of the Python original, as it saves them. Before
    // the first bar of a day it has no range keys; "{}" (a state written before this existed, or a twin that was
    // never started) is the fresh-day defaults.
    std::string save_state() const override {
        if (!configured_) {
            return "{}";
        }
        Json st = Json::object();
        st["symbol"] = symbol_;
        st["iid"] = iid_;
        st["minutes"] = minutes_;
        st["qty"] = qty_;
        st["short"] = short_;
        st["exit_hhmm"] = exit_hhmm_;
        st["day"] = day_ ? Json(iso_date(*day_)) : Json(nullptr);
        if (day_) {
            const auto opt = [](const std::optional<std::int64_t>& v) { return v ? Json(*v) : Json(nullptr); };
            st["open_ns"] = open_ns_;
            st["hi"] = opt(hi_);
            st["lo"] = opt(lo_);
            st["traded"] = traded_;
            st["closed"] = closed_;
            st["entry_id"] = entry_id_ ? Json(*entry_id_) : Json(nullptr);
            st["stop_side"] = stop_side_ ? Json(*stop_side_) : Json(nullptr);
            st["stop_trigger"] = stop_trigger_;
            st["stop_ids"] = Json(stop_ids_);
        }
        return st.dump();
    }

    void load_state(std::string_view text) override {
        constexpr std::string_view kOwner = "orb_cpp";
        Json st;
        try {
            st = Json::parse(text);
        } catch (const std::exception& e) {
            throw RunError("orb_cpp state: not JSON: " + std::string(e.what()));
        }
        if (st.is_object() && st.empty()) {
            *this = OrbCpp{};  // the fresh-day defaults
            return;
        }
        constexpr std::size_t kHead = 7;  // symbol, iid, minutes, qty, short, exit_hhmm, day
        constexpr std::size_t kDay = 9;   // open_ns, hi, lo, traded, closed, entry_id, stop_side, stop_trigger, stop_ids
        state::as_object(kOwner, st, {"symbol", "iid", "minutes", "qty", "short", "exit_hhmm", "day", "open_ns", "hi",
                                      "lo", "traded", "closed", "entry_id", "stop_side", "stop_trigger", "stop_ids"});
        OrbCpp n;  // read whole, then taken: a refused state leaves this strategy as it was
        n.symbol_ = state::read_str(kOwner, st, "symbol");
        n.iid_ = static_cast<InstrumentId>(state::read_int(kOwner, st, "iid", 0, UINT32_MAX));
        n.minutes_ = state::read_int(kOwner, st, "minutes");
        n.qty_ = state::read_int(kOwner, st, "qty");
        n.short_ = state::read_bool(kOwner, st, "short");
        n.exit_hhmm_ = state::read_str(kOwner, st, "exit_hhmm");
        n.day_ = state::read_opt_date(kOwner, st, "day");
        const std::size_t want = kHead + (n.day_ ? kDay : 0);
        if (st.size() != want) {
            state::refuse(kOwner, "(range keys)", n.day_ ? "are incomplete" : "are saved only once a day has started");
        }
        if (n.day_) {
            n.open_ns_ = state::read_int(kOwner, st, "open_ns");
            n.hi_ = state::read_opt_int(kOwner, st, "hi");
            n.lo_ = state::read_opt_int(kOwner, st, "lo");
            n.traded_ = state::read_bool(kOwner, st, "traded");
            n.closed_ = state::read_bool(kOwner, st, "closed");
            if (const auto id = state::read_opt_int(kOwner, st, "entry_id", 0)) {
                n.entry_id_ = static_cast<OrderId>(*id);
            }
            n.stop_side_ = state::read_opt_str(kOwner, st, "stop_side");
            n.stop_trigger_ = state::read_int(kOwner, st, "stop_trigger");
            const Json& ids = state::need(kOwner, st, "stop_ids");
            if (!ids.is_array()) {
                state::refuse(kOwner, "stop_ids", "is not a list");
            }
            for (const Json& id : ids) {
                if (id.is_boolean() || !(id.is_number_integer() || id.is_number_unsigned()) || id.is_number_float() ||
                    (id.is_number_unsigned() ? id.get<std::uint64_t>() > static_cast<std::uint64_t>(INT64_MAX)
                                             : id.get<std::int64_t>() < 0)) {
                    state::refuse(kOwner, "stop_ids", "holds something that is not an order id");
                }
                n.stop_ids_.push_back(id.get<OrderId>());
            }
            // The Python original sets these together, so they are all set or all clear.
            if (n.hi_.has_value() != n.lo_.has_value()) {
                state::refuse(kOwner, "hi/lo", "must both be set or both be null");
            }
            if (n.stop_side_ && *n.stop_side_ != "SELL" && *n.stop_side_ != "BUY") {
                state::refuse(kOwner, "stop_side", "is not SELL or BUY");
            }
            if (n.traded_ != n.entry_id_.has_value() || n.traded_ != n.stop_side_.has_value() ||
                (!n.traded_ && !n.stop_ids_.empty())) {
                state::refuse(kOwner, "traded/entry_id/stop_side/stop_ids", "disagree about whether an entry was placed");
            }
        }
        n.configured_ = true;
        *this = std::move(n);
    }

 private:
    void new_day(Context& ctx) {
        day_ = ctx.today();
        open_ns_ = ist_ns(*day_, "09:15");
        hi_.reset();
        lo_.reset();
        traded_ = false;
        closed_ = false;
        entry_id_.reset();
        stop_side_.reset();
        stop_trigger_ = 0;
        stop_ids_.clear();
        const TimeNs exit_ns = ist_ns(*day_, exit_hhmm_);
        if (exit_ns > ctx.now()) {
            ctx.schedule_at(exit_ns);
        }
    }

    OrderId place(Context& ctx, const char* side, const char* type, Paise trigger, const char* tag,
                  std::string note, std::optional<Qty> qty = std::nullopt) const {
        PlaceRequest r;
        r.instrument = iid_;
        r.side = side;
        r.qty = qty.value_or(qty_);
        r.product = "MIS";
        r.type = type;
        r.trigger = trigger;
        r.tag = tag;
        r.note = std::move(note);
        return ctx.place(r);
    }

    InstrumentId iid_ = 0;
    std::string symbol_;  // the symbol param text exactly as given
    std::int64_t minutes_ = 15;
    Qty qty_ = 0;
    bool short_ = false;
    std::string exit_hhmm_ = "15:10";
    std::optional<Date> day_;
    TimeNs open_ns_ = 0;
    std::optional<Paise> hi_, lo_;
    bool traded_ = false;
    bool closed_ = false;  // the day's position is closed: no more range lines
    std::optional<OrderId> entry_id_;
    std::optional<std::string> stop_side_;  // None until an entry is placed
    Paise stop_trigger_ = 0;
    std::vector<OrderId> stop_ids_;
    bool configured_ = false;  // on_start ran, or a state with the parameters was loaded
};

STOCKSIM_STRATEGY(OrbCpp, "orb_cpp")

}  // namespace stocksim::strategies
