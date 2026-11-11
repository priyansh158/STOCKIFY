// orb_cpp: stockify.strategies.opening_range_breakout.OpeningRangeBreakout in C++ (M3.10).
// Same params, same orders, same artifacts: a run with either one must be byte-identical.
#include <algorithm>
#include <optional>
#include <string>
#include <vector>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
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
        if (traded_ || !hi_) {
            return;
        }
        const Spec* spec = ctx.spec(iid_);
        if (spec == nullptr) {
            throw RunError("orb_cpp: instrument " + std::to_string(iid_) + " has no spec today");
        }
        const Paise tick = spec->tick;
        if (bar.close > *hi_) {
            entry_id_ = place(ctx, "BUY", "MARKET", 0, "orb_long");
            stop_side_ = "SELL";
            stop_trigger_ = checked_mul(floor_div(*lo_, tick), tick);
            traded_ = true;
        } else if (short_ && bar.close < *lo_) {
            entry_id_ = place(ctx, "SELL", "MARKET", 0, "orb_short");
            stop_side_ = "BUY";
            stop_trigger_ = checked_mul(ceil_div(*hi_, tick), tick);
            traded_ = true;
        }
    }

    // The stop exists only for what the entry has filled: one SL-M of exactly each fill's quantity.
    void on_fill(Context& ctx, const FillView& fill) override {
        if (entry_id_ && fill.order_id == *entry_id_) {
            stop_ids_.push_back(place(ctx, stop_side_, "SL-M", stop_trigger_, "orb_stop", fill.qty));
        }
    }

    void on_timer(Context& ctx, std::uint64_t /*timer_id*/) override {
        for (const OrderId id : stop_ids_) {
            ctx.cancel(id);
        }
        const std::optional<PositionView> pos = ctx.position(iid_, "MIS");
        if (pos && pos->qty != 0) {
            const Qty units = pos->qty > 0 ? pos->qty : checked_sub(0, pos->qty);
            place(ctx, pos->qty > 0 ? "SELL" : "BUY", "MARKET", 0, "orb_exit", units);
        }
    }

 private:
    void new_day(Context& ctx) {
        day_ = ctx.today();
        open_ns_ = ist_ns(*day_, "09:15");
        hi_.reset();
        lo_.reset();
        traded_ = false;
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
                  std::optional<Qty> qty = std::nullopt) const {
        PlaceRequest r;
        r.instrument = iid_;
        r.side = side;
        r.qty = qty.value_or(qty_);
        r.product = "MIS";
        r.type = type;
        r.trigger = trigger;
        r.tag = tag;
        return ctx.place(r);
    }

    InstrumentId iid_ = 0;
    std::int64_t minutes_ = 15;
    Qty qty_ = 0;
    bool short_ = false;
    std::string exit_hhmm_ = "15:10";
    std::optional<Date> day_;
    TimeNs open_ns_ = 0;
    std::optional<Paise> hi_, lo_;
    bool traded_ = false;
    std::optional<OrderId> entry_id_;
    const char* stop_side_ = "SELL";
    Paise stop_trigger_ = 0;
    std::vector<OrderId> stop_ids_;
};

STOCKSIM_STRATEGY(OrbCpp, "orb_cpp")

}  // namespace stocksim::strategies
