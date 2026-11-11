// The live margin model (MEZ 5.1, D-69, M5.6), the engine's twin of stockify/fno/live_margin.py.
//
// Kite's /margins/basket answer reaches the engine only as a journaled REFDATA record, `margin_requirement`
// (integers only; the payload is listed in live_margin.py). It is checked on arrival (RunError
// "margin_requirement: ..." exactly as the oracle words it), and a fresh answer for exactly the current book's
// staircase legs (the hash of the sorted [[iid, units]]) prices the book: span + exposure + additional (D-62: the
// option premium is not margin). Everything else, and every pre-trade increment (`pair`), is the staircase.
// An answer is as old as its record: fresh while 0 <= now - t <= fresh_ns.
#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "stocksim/margin/staircase.hpp"

namespace stocksim {

using LiveLegs = std::vector<std::pair<InstrumentId, Qty>>;  // sorted by instrument

inline constexpr Paise kLiveMaxPaise = 1'000'000'000'000'000'000;  // three of them still fit an int64
inline constexpr const char* kLiveStep = "L1_kite";
inline constexpr std::int64_t kLiveFreshSeconds = 360;  // sessionconfig.LIVE_FRESH_S

// SHA-256 (lowercase hex) of the canonical JSON of the sorted [[iid, units], ...].
std::string live_portfolio_sha256(LiveLegs legs);

struct LiveAnswer {
    std::string sha;
    LiveLegs legs;
    Paise span = 0, exposure = 0, additional = 0, option_premium = 0;
    TimeNs requested_ns = 0;
};

// A margin_requirement payload (JSON text) checked and decoded; RunError("margin_requirement: ...") if refused.
LiveAnswer check_live_payload(std::string_view json);

class LiveAnswers {
 public:
    void put(LiveAnswer answer, TimeNs t);
    // The latest answer for `sha` if it is fresh at `now`, else nullptr.
    const LiveAnswer* fresh(const std::string& sha, TimeNs now, TimeNs fresh_ns) const;

 private:
    std::map<std::string, std::pair<TimeNs, LiveAnswer>> by_sha_;
};

class LiveWithFallbackMargin final : public StaircaseMargin {
 public:
    LiveWithFallbackMargin(const Json& bundle_root, SpanLiteHost host, const LiveAnswers& answers, TimeNs fresh_ns);

    Paise requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                      const std::function<std::optional<Paise>(InstrumentId)>& price,
                      const std::string& date) const override;
    std::vector<UnderlyingMargin> breakdown(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                            const std::function<std::optional<Paise>(InstrumentId)>& price,
                                            const std::string& date) const override;
    const std::set<std::string>& used_labels() const override;

    // The staircase legs of a book, sorted: what the hash covers.
    LiveLegs live_legs(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments) const;
    // The fresh answer for exactly this book now, or nullptr.
    const LiveAnswer* live(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments) const;
    // The staircase's per-underlying rows, without recording their steps as used (a read, for the daemon).
    std::vector<UnderlyingMargin> fallback(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                           const std::function<std::optional<Paise>(InstrumentId)>& price,
                                           const std::string& date) const;

 private:
    const LiveAnswers& answers_;
    TimeNs fresh_ns_;
    std::function<TimeNs()> now_;
    mutable std::int64_t l1_checks_ = 0;
    mutable std::set<std::string> labels_;
};

}  // namespace stocksim
