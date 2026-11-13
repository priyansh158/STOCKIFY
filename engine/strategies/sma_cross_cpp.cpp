// sma_cross_cpp: stockify.strategies.sma_cross.SmaCross in C++ (M3.10), the proof of the native path.
// Same params, same orders, same artifacts: a run with either one must be byte-identical.
#include <cmath>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/notes.hpp"
#include "stocksim/strategy.hpp"

namespace stocksim::strategies {

class SmaCrossCpp final : public Strategy {
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
        isec_ = static_cast<std::int32_t>(param_int(p, "interval_s", 86400));
        fast_ = param_int(p, "fast");
        slow_ = param_int(p, "slow");
        long_short_ = param_bool(p, "long_short", false);
        product_ = param_string(p, "product", "CNC");
        if (long_short_ && product_ == "CNC") {
            throw std::invalid_argument("long_short needs a derivative traded NRML (no CNC short selling)");
        }
        ctx.subscribe_bars(iid_, isec_);
    }

    void on_bar(Context& ctx, const Bar& bar) override {
        const double fast = ctx.sma(iid_, isec_, fast_);
        const double slow = ctx.sma(iid_, isec_, slow_);
        if (std::isnan(fast) || std::isnan(slow)) {  // NaN during warm-up
            return;
        }
        const auto fast_paise = static_cast<std::int64_t>(fast);  // positive paise: the cast truncates, as int() does
        const auto slow_paise = static_cast<std::int64_t>(slow);
        ctx.plot("fast", fast_paise, "price", iid_);
        ctx.plot("slow", slow_paise, "price", iid_);
        const std::optional<PositionView> pos = ctx.position(iid_, product_);
        const Qty held = pos ? pos->qty : 0;
        Qty want = 0;
        if (fast > slow) {
            want = held <= 0 ? units(ctx, bar.close) : held;
        } else if (long_short_) {
            want = held >= 0 ? -units(ctx, bar.close) : held;
        }
        const Qty delta = checked_sub(want, held);
        if (delta == 0) {
            return;
        }
        // The sentence is D-95's: the same words as the Python strategy, so the notes match byte for byte.
        const std::string near = fast_paise == slow_paise ? " just" : "";  // printed paise equal, averages not
        const std::string move = fast > slow ? "rose" + near + " above" : fast < slow ? "fell" + near + " below" : "is level with";
        const std::string why = "the " + std::to_string(fast_) + "-bar average " + fmt_price(fast_paise) + " " + move +
                                " the " + std::to_string(slow_) + "-bar average " + fmt_price(slow_paise);
        const std::string qty = fmt_qty(delta > 0 ? delta : -delta);
        if (delta > 0) {
            const std::string lead = held >= 0 ? "Bought " + qty : "Flipped to long " + fmt_qty(want);
            place(ctx, "BUY", delta, held >= 0 ? "entry" : "flip", lead + " " + symbol_ + ": " + why);
        } else {
            const std::string lead = want >= 0  ? "Sold " + qty
                                     : held > 0 ? "Flipped to short " + fmt_qty(-want)
                                                : "Sold short " + qty;
            place(ctx, "SELL", -delta, want >= 0 ? "exit" : "flip", lead + " " + symbol_ + ": " + why);
        }
    }

 private:
    Qty units(Context& ctx, Paise close) const {
        if (product_ == "CNC") {
            return floor_div(floor_div(checked_mul(ctx.funds().available, 95), 100),
                             floor_div(checked_mul(close, 103), 100));
        }
        const Spec* spec = ctx.spec(iid_);
        if (spec == nullptr) {
            throw RunError("sma_cross_cpp: instrument " + std::to_string(iid_) + " has no spec today");
        }
        return checked_mul(param_int(ctx.params(), "lots", 1), spec->lot);
    }

    void place(Context& ctx, const char* side, Qty qty, const char* tag, std::string note) const {
        PlaceRequest r;
        r.instrument = iid_;
        r.side = side;
        r.qty = qty;
        r.product = product_;
        r.type = "MARKET";
        r.tag = tag;
        r.note = std::move(note);
        ctx.place(r);
    }

    InstrumentId iid_ = 0;
    std::string symbol_;  // the symbol param text exactly as given
    std::int32_t isec_ = 86400;
    std::int64_t fast_ = 0, slow_ = 0;
    bool long_short_ = false;
    std::string product_ = "CNC";
};

STOCKSIM_STRATEGY(SmaCrossCpp, "sma_cross_cpp")

}  // namespace stocksim::strategies
