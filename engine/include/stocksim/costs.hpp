// Charges (A4 §5 as amended by D-46), the twin of stockify.oracle.costs. Integer ppb arithmetic only.
//
// Each component has exactly one matching row in force on the trade date: a row matches when every key of its
// `applies` object matches the order (list values by membership); among rows with the same `applies`, the one in
// force is the latest whose effective_from is on or before the date. Zero or several matches is an error.
// Components are exact in E = 10⁻¹⁸ paisa (Int128); stamp duty alone is rounded half up to the whole rupee;
// the row total is the exact sum rounded half up once to the paisa.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <string_view>

#include "stocksim/json_bundle.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

struct ChargeParts {
    Paise turnover = 0, brokerage = 0, stt = 0, exchange = 0, sebi = 0, stamp = 0, gst = 0, dp = 0, other = 0;
    std::optional<Paise> total;  // the once-rounded exact sum (orders, exercise); flat fees sum their parts

    Paise row_total() const;
};

class Costs {
 public:
    Costs(const Json& charges, const Json& costs_cfg);

    ChargeParts order(std::string_view kind, Product product, Side side, Paise turnover, const std::string& trade_date) const;
    std::optional<ChargeParts> dp(const std::string& trade_date) const;
    std::optional<ChargeParts> sqoff_fee(const std::string& trade_date) const;
    ChargeParts exercise(std::string_view kind, Paise intrinsic_value, const std::string& trade_date) const;

 private:
    using Attrs = std::map<std::string, std::string>;
    const Json* row_for(std::string_view component, const Attrs& attrs, const std::string& trade_date,
                        bool required) const;
    const Json& rows(std::string_view component) const;
    Paise gst_on(const std::string& trade_date, Paise amount) const;

    Json components_;
    bool dp_charges_ = true;
    bool auto_square_off_fee_ = true;
};

}  // namespace stocksim
