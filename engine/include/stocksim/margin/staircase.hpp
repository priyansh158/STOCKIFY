// The margin staircase and the strict exact-SPAN model (MEZ 4.10), the engine's twin of stockify/fno/staircase.py.
//
// Per underlying per check, the most accurate step that can price every one of its NRML legs (never mixing
// sources within one underlying):
//
//     B1_span_nse_elm          exact SPAN from the NSE file in force at `now` + the ELM % of the day's NSE file
//                              (no X-9 add-on). Needs the % and the exact price of every charged leg (futures:
//                              their own price; sold options: the index); bought options need neither.
//     B2_span_rule_elm         the same SPAN + ELM and the X-9 add-on from the dated rules (SpanLite::rule_elm).
//     B3_spanlite_calibrated   SPAN-lite with an adopted row; B4_spanlite_placeholder with the placeholder row.
//     B5_flat                  the flat model on this underlying's legs.
//
// The file in force: the last entry of bundle span[D] with created_ns <= now. A missing contract in any leg steps
// the whole underlying down. `pair` prices the pre-trade check's before and after at ONE step per underlying, the
// lower of the two. Labels: margin:<step>, margin_steps:<step>=<days>, margin_span:no_section.
//
// span_exact (strict) is B1/B2 only: where SPAN cannot price an underlying, or the day's ELM file lacks a charged
// leg's %, the run fails naming it (RunError "span_exact: …"; a file it cannot apply is a span::SpanCalcError).
#pragma once

#include <map>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "stocksim/margin.hpp"
#include "stocksim/margin/span_exact.hpp"
#include "stocksim/margin/spanlite.hpp"

namespace stocksim {

class StaircaseMargin final : public MarginModel {
 public:
    static constexpr const char* kNoSectionLabel = "margin_span:no_section";

    // `model` is run.margin.model ("staircase" | "span_exact", the margin_daily `model` column); strict for span_exact.
    StaircaseMargin(const Json& bundle_root, SpanLiteHost host, std::string model, bool strict);

    Paise requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                      const std::function<std::optional<Paise>(InstrumentId)>& price,
                      const std::string& date) const override;
    Paise mis(std::string_view kind, Qty units, Paise price, const std::string& date) const override {
        return flat_.mis(kind, units, price, date);  // MIS shares: the same rule under every model (A4 §6.5)
    }
    std::string label() const override { return "margin:" + model_; }
    std::vector<UnderlyingMargin> breakdown(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                            const std::function<std::optional<Paise>(InstrumentId)>& price,
                                            const std::string& date) const override;
    std::pair<Paise, Paise> pair(const std::vector<MarginLeg>& before, const std::vector<MarginLeg>& after,
                                 const InstrumentTable& instruments,
                                 const std::function<std::optional<Paise>(InstrumentId)>& price_before,
                                 const std::function<std::optional<Paise>(InstrumentId)>& price_after,
                                 const std::string& date) const override;
    std::string_view daily_model() const override { return model_; }
    const std::set<std::string>& used_labels() const override;

 private:
    using Legs = SpanLiteMargin::Legs;
    using Price = std::function<std::optional<Paise>(InstrumentId)>;

    std::map<InstrumentId, Legs> group(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments) const;
    UnderlyingMargin one(InstrumentId uid, const Legs& legs, const InstrumentTable& instruments, const Price& price,
                         const std::string& date, std::size_t floor = 0) const;
    UnderlyingMargin exact(InstrumentId uid, const Legs& legs, const Price& price, const std::string& date,
                           bool allow_b1) const;
    std::pair<const span::Commodity*, std::string> commodity(const std::optional<std::string>& und,
                                                             const std::string& date) const;
    std::optional<Paise> b1_elm(const std::optional<std::string>& und, InstrumentId uid, const Legs& legs,
                                const Price& price, const std::string& date) const;
    void used(const std::string& step, const std::string& date) const { step_days_[step].insert(date); }

    std::string model_;
    bool strict_;
    bool no_section_;
    Json span_;  // bundle span (an object; {} when absent or null)
    Json elm_;   // bundle elm
    SpanLiteMargin spanlite_;
    FlatMargin flat_;
    SpanLiteHost host_;
    mutable std::map<std::tuple<std::string, std::size_t, std::string>, std::optional<span::Commodity>> cc_;
    mutable std::map<std::string, std::set<std::string>> step_days_;  // step → run days on which a check used it
    mutable std::set<std::string> labels_;
};

}  // namespace stocksim
