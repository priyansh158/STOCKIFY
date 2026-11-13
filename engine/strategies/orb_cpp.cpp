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
            std::string note = stop_side_ == std::string_view("SELL")
                                   ? "Stop-loss for the " + qty + " bought: sells if price falls to " + trigger
                                   : "Stop-loss for the " + qty + " sold short: buys back if price rises to " + trigger;
            stop_ids_.push_back(place(ctx, stop_side_, "SL-M", stop_trigger_, "orb_stop", std::move(note), fill.qty));
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

 private:
    void new_day(Context& ctx) {
        day_ = ctx.today();
        open_ns_ = ist_ns(*day_, "09:15");
        hi_.reset();
        lo_.reset();
        traded_ = false;
        closed_ = false;
        entry_id_.reset();
        stop_side_ = "SELL";
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
    const char* stop_side_ = "SELL";
    Paise stop_trigger_ = 0;
    std::vector<OrderId> stop_ids_;
};

STOCKSIM_STRATEGY(OrbCpp, "orb_cpp")

}  // namespace stocksim::strategies
