// Dated rule rows and the T0 flat margin model (A4 §6.5, D-14; labelled approximate), the twins of
// stockify.oracle.margin and the oracle's `_bp` / `_cnc_credit_ppb` row lookups.
//
// A dated row is in force on a date when its effective_from is on or before it; among those, the one with the
// latest effective_from wins, the first in bundle order on a tie. Dates are ISO strings, compared as text.
#pragma once

#include <algorithm>
#include <functional>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "stocksim/instruments.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

// The row in force on `date` among `rows` (a JSON list), or nullptr. With `applies`, a row must carry each of
// those keys with that value in its own `applies` object (extra keys in the row are ignored).
const Json* row_in_force(const Json& rows, const std::string& date,
                         const std::vector<std::pair<std::string, std::string>>& applies = {});

struct MarginLeg {
    InstrumentId instrument = 0;
    Product product = Product::CNC;
    Qty units = 0;  // signed
};

// One underlying's margin breakdown in paise, each part rounded up to the rupee (a margin_daily.csv row, M4.8).
struct UnderlyingMargin {
    InstrumentId underlying = 0;
    Paise scan = 0, somc = 0, elm = 0, addons = 0;
    std::string step;  // the staircase step (MEZ 4.9): "B3_spanlite_calibrated" | "B4_spanlite_placeholder" | …
    Paise total() const { return std::max(scan, somc) + elm + addons; }
};

// A margin model (A2 §2): flat (T0, here) | spanlite (T1, M4.8) | span (T2) | live_with_fallback.
class MarginModel {
 public:
    virtual ~MarginModel() = default;
    // The requirement of NRML legs (signed units); price(iid) gives a reference price or nullopt.
    virtual Paise requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                              const std::function<std::optional<Paise>(InstrumentId)>& price,
                              const std::string& date) const = 0;
    // The requirement of an MIS position or order.
    virtual Paise mis(std::string_view kind, Qty units, Paise price, const std::string& date) const = 0;
    virtual std::string label() const = 0;  // drives the run's labels (I-14)
    // Per-underlying rows for margin_daily.csv at DAY_END; models without a breakdown (flat) write none.
    virtual std::vector<UnderlyingMargin> breakdown(const std::vector<MarginLeg>& /*legs*/,
                                                    const InstrumentTable& /*instruments*/,
                                                    const std::function<std::optional<Paise>(InstrumentId)>& /*price*/,
                                                    const std::string& /*date*/) const {
        return {};
    }
    virtual std::string_view daily_model() const { return {}; }  // the `model` column; empty: no rows
    // Labels the model used while running (D-60: delivery_margin_approx), merged into the run's; none by default.
    virtual const std::set<std::string>& used_labels() const {
        static const std::set<std::string> none;
        return none;
    }
};

class FlatMargin final : public MarginModel {
 public:
    explicit FlatMargin(const Json& bundle_root);

    Ppb mis_rate(std::string_view segment, const std::string& date) const;
    // Σ over NRML futures and short NRML options of |units| × reference × flat rate, rounded up to the rupee:
    // ceil_to(ceil_div(raw, 10⁹), 100). Reference: futures → their price; short options → the underlying's,
    // else the strike. A short index option on its expiry day adds the expiry-day ELM (X-9).
    Paise requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                      const std::function<std::optional<Paise>(InstrumentId)>& price,
                      const std::string& date) const override;
    // MIS: round_half_up_div(|units| · price · mis_ppb, 10⁹).
    Paise mis(std::string_view kind, Qty units, Paise price, const std::string& date) const override;
    std::string label() const override { return "margin:flat(approx)"; }
    // Labels the model used (D-60: delivery_margin_approx), merged into the run's.
    const std::set<std::string>& used_labels() const override { return used_labels_; }

 private:
    Ppb rate(const Json& rows, std::string_view key, const std::string& date,
             const std::vector<std::pair<std::string, std::string>>& applies) const;
    Ppb addon(const std::string& date) const;  // the expiry-day ELM rate in force, else 0 (X-9)
    // The stock F&O delivery-margin schedule (Z-4, D-60); amounts in paise × 10¹⁸.
    std::optional<int> offset(const Instrument& row, const std::string& date) const;
    const Json* schedule_row(const std::string& date, int offset, std::string_view applies_to) const;
    Int128 short_delivery(const Instrument& row, Qty units, const std::optional<Paise>& spot, const std::string& date,
                          const Int128& normal) const;
    Int128 long_option_delivery(const Instrument& row, Qty units, const std::optional<Paise>& spot,
                                const std::string& date) const;
    Json flat_;
    Json mis_rows_;
    Json addon_;
    Json schedule_;
    std::vector<std::string> days_;
    mutable std::set<std::string> used_labels_;
};

}  // namespace stocksim
