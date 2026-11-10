// T1 SPAN-lite margin (M4.8, C-12), the engine's twin of stockify/fno/spanlite.py: per underlying, the NRML futures
// and options revalued under 14 scenarios (price {0, ±⅓, ±⅔, ±1}·PSR × IV·(1 ± VSR)) plus two extreme moves
// (±extreme_multiple·PSR, unchanged IV, weighted); scan = worst loss ≥ 0; SOMC per short lot; ELM on futures and
// short options; the expiry-day add-on on short index options (X-9); each part rounded up to the rupee;
// total = max(scan, SOMC) + ELM + add-on. Same expression order as the oracle: parity is exact on one platform.
#pragma once

#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "stocksim/engine.hpp"
#include "stocksim/margin.hpp"

namespace stocksim {

// What SPAN-lite needs from the engine, as callbacks (the engine wires them; tests can fake them).
struct SpanLiteHost {
    std::function<TimeNs()> now;
    std::function<Context::ChainView(const std::string& underlying, const std::string& expiry)> chain;
    std::function<const Spec*(InstrumentId, Date)> spec;
    std::function<void(const std::string&)> add_label;
    std::function<double(const std::string& date)> rate;  // pricing.risk_free in force (decimal)
};

class SpanLiteMargin final : public MarginModel {
 public:
    static constexpr const char* kIvFallbackLabel = "margin_spanlite:iv_fallback";

    SpanLiteMargin(const Json& bundle_root, SpanLiteHost host);

    Paise requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                      const std::function<std::optional<Paise>(InstrumentId)>& price,
                      const std::string& date) const override;
    Paise mis(std::string_view kind, Qty units, Paise price, const std::string& date) const override {
        return flat_.mis(kind, units, price, date);  // MIS shares: the same rule under every model (A4 §6.5)
    }
    std::string label() const override { return "margin:spanlite(approx)"; }
    std::vector<UnderlyingMargin> breakdown(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                            const std::function<std::optional<Paise>(InstrumentId)>& price,
                                            const std::string& date) const override;
    std::string_view daily_model() const override { return "spanlite"; }

    // Used by the staircase (MEZ 4.10) as the oracle's StaircaseMargin uses SpanLiteMargin.
    using Legs = std::vector<std::pair<const Instrument*, Qty>>;
    // The spanlite row for an instrument's underlying (one naming the underlying wins over its class), or nullptr.
    const Json* find_params(const Instrument& row, const std::string& date) const;
    // One underlying's SPAN-lite breakdown (spanlite._one).
    UnderlyingMargin one(InstrumentId underlying, const Legs& legs,
                         const std::function<std::optional<Paise>(InstrumentId)>& price,
                         const std::string& date) const;
    // (ELM, expiry-day add-on) in paise from the dated rules, each rounded up to the rupee (spanlite.rule_elm):
    // the row's elm_pct on every exposure notional, the X-9 add-on on short index options expiring that day.
    std::pair<Paise, Paise> rule_elm(const Legs& legs, const std::function<std::optional<Paise>(InstrumentId)>& price,
                                     const std::string& date, std::optional<Paise> und) const;

 private:
    const Json& params(const Instrument& row, const std::string& date) const;
    double iv(const Instrument& row, const Context::ChainView* chain, const std::string& date) const;
    std::optional<Context::ChainView> chain_of(const Instrument& row) const;

    Json rows_;
    Json addon_rows_;
    FlatMargin flat_;
    SpanLiteHost host_;
    mutable std::map<InstrumentId, std::pair<std::string, double>> last_iv_;  // iid → (date, last valid mid IV)
};

}  // namespace stocksim
