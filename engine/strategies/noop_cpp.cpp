// noop_cpp: subscribes to every instrument's ticks and does nothing (M3.13 benchmark (a)). It measures the
// engine's own cost per event: decoding, ordering, market state, evidence and the strategy dispatch.
#include "stocksim/engine.hpp"
#include "stocksim/strategy.hpp"

namespace stocksim::strategies {

class NoopCpp final : public Strategy {
 public:
    void on_start(Context& ctx) override {
        for (const Json& row : ctx.instrument_rows()) {
            ctx.subscribe_ticks(row.at("id").get<InstrumentId>());
        }
    }
};

STOCKSIM_STRATEGY(NoopCpp, "noop_cpp")

}  // namespace stocksim::strategies
