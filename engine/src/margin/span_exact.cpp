// Exact SPAN (MEZ 4.10): see span_exact.hpp. Mirrors stockify/fno/span_exact.py step by step, in its order, so a
// file the oracle refuses (SpanCalcError) is refused here at the same point.
#include "stocksim/margin/span_exact.hpp"

#include <algorithm>
#include <array>
#include <numeric>
#include <set>
#include <stdexcept>

namespace stocksim::span {

namespace {

constexpr std::size_t kScenarios = 16;      // span_file.SCENARIOS
constexpr const char* kRateClass = "1";     // the only rate class the reader keeps
constexpr int kOraclePrecision = 50;        // the oracle's Decimal context (prec=50, Inexact trapped)
constexpr std::int32_t kMaxExponent = 1'000'000;
constexpr std::int64_t kPow10[] = {1,
                                   10,
                                   100,
                                   1'000,
                                   10'000,
                                   100'000,
                                   1'000'000,
                                   10'000'000,
                                   100'000'000,
                                   1'000'000'000,
                                   10'000'000'000,
                                   100'000'000'000,
                                   1'000'000'000'000,
                                   10'000'000'000'000,
                                   100'000'000'000'000,
                                   1'000'000'000'000'000,
                                   10'000'000'000'000'000,
                                   100'000'000'000'000'000,
                                   1'000'000'000'000'000'000};
constexpr int kMaxPow = 18;

[[noreturn]] void overflow(const std::string& what) { throw std::overflow_error("exact SPAN: " + what + " overflows"); }

std::int32_t checked_exponent(std::int64_t e) {
    if (e > kMaxExponent || e < -kMaxExponent) {
        overflow("an exponent");
    }
    return static_cast<std::int32_t>(e);
}

// x × 10^k (k >= 0), exact or std::overflow_error.
Int128 times_pow10(Int128 x, std::int64_t k) {
    if (x.is_zero()) {
        return x;
    }
    while (k > 0) {
        const int step = static_cast<int>(std::min<std::int64_t>(k, kMaxPow));
        x = x * kPow10[step];
        k -= step;
    }
    return x;
}

// The non-negative remainder of x / d for d > 0.
std::int64_t mod(const Int128& x, std::int64_t d) { return (x - x.floor_div(d) * d).to_int64(); }

Int128 product(const Int128& a, const Int128& b) {
    if (b.fits_int64()) {
        return a * b.to_int64();
    }
    if (a.fits_int64()) {
        return b * a.to_int64();
    }
    overflow("a product");
}

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'; }
bool is_digit(char c) { return c >= '0' && c <= '9'; }

// A run of digits with single '_' between them (Python's grouping); appends each digit to `out`.
// Returns the index after the run; `count` is the number of digits read.
std::size_t digit_run(std::string_view s, std::size_t i, std::string& out, std::size_t& count, bool& ok) {
    count = 0;
    bool prev_digit = false;
    while (i < s.size()) {
        if (is_digit(s[i])) {
            out.push_back(s[i]);
            ++count;
            prev_digit = true;
        } else if (s[i] == '_' && prev_digit && i + 1 < s.size() && is_digit(s[i + 1])) {
            prev_digit = false;
        } else {
            break;
        }
        ++i;
    }
    if (count == 0 && i < s.size() && s[i] == '_') {
        ok = false;
    }
    return i;
}

}  // namespace

// ── SpanDecimal ──────────────────────────────────────────────────────────────

void SpanDecimal::normalise() {
    if (coef_.is_zero()) {
        exp_ = 0;
        return;
    }
    while (mod(coef_, 10) == 0) {
        coef_ = coef_.floor_div(10);
        exp_ = checked_exponent(std::int64_t{exp_} + 1);
    }
}

std::optional<SpanDecimal> SpanDecimal::parse(std::string_view text) {
    while (!text.empty() && is_space(text.front())) {
        text.remove_prefix(1);
    }
    while (!text.empty() && is_space(text.back())) {
        text.remove_suffix(1);
    }
    std::size_t i = 0;
    bool negative = false;
    if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
        negative = text[i] == '-';
        ++i;
    }
    bool ok = true;
    std::string int_digits, frac_digits;
    std::size_t n_int = 0, n_frac = 0;
    i = digit_run(text, i, int_digits, n_int, ok);
    if (i < text.size() && text[i] == '.') {
        i = digit_run(text, i + 1, frac_digits, n_frac, ok);
    }
    if (!ok || n_int + n_frac == 0) {
        return std::nullopt;  // "", "+", ".", "nan", "inf", "Infinity", garbage
    }
    std::int64_t exponent = -static_cast<std::int64_t>(n_frac);
    if (i < text.size() && (text[i] == 'e' || text[i] == 'E')) {
        ++i;
        bool exp_negative = false;
        if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
            exp_negative = text[i] == '-';
            ++i;
        }
        std::string exp_digits;
        std::size_t n_exp = 0;
        i = digit_run(text, i, exp_digits, n_exp, ok);
        if (!ok || n_exp == 0) {
            return std::nullopt;
        }
        std::int64_t e = 0;
        for (const char c : exp_digits) {
            e = e * 10 + (c - '0');
            if (e > kMaxExponent) {
                overflow("a decimal's exponent");
            }
        }
        exponent += exp_negative ? -e : e;
    }
    if (i != text.size()) {
        return std::nullopt;
    }
    // Digits accumulate exactly; zeros are held back until a non-zero digit follows, so trailing zeros never
    // cost coefficient room (normalisation would strip them anyway).
    SpanDecimal out;
    Int128 coef = 0;
    std::int64_t pending_zeros = 0;
    bool any = false;
    const std::string digits = int_digits + frac_digits;
    for (const char c : digits) {
        if (c == '0') {
            ++pending_zeros;
            continue;
        }
        coef = any ? times_pow10(coef, pending_zeros + 1) + Int128(c - '0') : Int128(c - '0');
        any = true;
        pending_zeros = 0;
    }
    if (!any) {
        return out;  // any zero, whatever its sign or exponent
    }
    out.coef_ = negative ? -coef : coef;
    out.exp_ = checked_exponent(exponent + pending_zeros);
    out.normalise();
    return out;
}

SpanDecimal SpanDecimal::operator-() const {
    SpanDecimal out = *this;
    out.coef_ = -coef_;
    return out;
}

SpanDecimal operator+(const SpanDecimal& a, const SpanDecimal& b) {
    if (a.is_zero()) {
        return b;
    }
    if (b.is_zero()) {
        return a;
    }
    const std::int32_t e = std::min(a.exp_, b.exp_);
    SpanDecimal out;
    out.coef_ = times_pow10(a.coef_, std::int64_t{a.exp_} - e) + times_pow10(b.coef_, std::int64_t{b.exp_} - e);
    out.exp_ = e;
    out.normalise();
    return out;
}

SpanDecimal operator*(const SpanDecimal& a, const SpanDecimal& b) {
    SpanDecimal out;
    if (a.is_zero() || b.is_zero()) {
        return out;
    }
    out.coef_ = product(a.coef_, b.coef_);
    out.exp_ = checked_exponent(std::int64_t{a.exp_} + b.exp_);
    out.normalise();
    return out;
}

std::strong_ordering operator<=>(const SpanDecimal& a, const SpanDecimal& b) {
    if (a.sign() != b.sign()) {
        return a.sign() <=> b.sign();
    }
    return (a - b).sign() <=> 0;
}

std::optional<SpanDecimal> SpanDecimal::divide_exact(const SpanDecimal& a, const SpanDecimal& b) {
    if (b.is_zero()) {
        throw NumericError("exact SPAN: division by zero");
    }
    if (a.is_zero()) {
        return SpanDecimal{};
    }
    if (!b.coef_.fits_int64() || b.coef_.to_int64() == kInt64Min) {
        overflow("a divisor");
    }
    const bool neg = a.negative() != b.negative();
    const Int128 num = a.negative() ? -a.coef_ : a.coef_;
    const std::int64_t den = b.negative() ? -b.coef_.to_int64() : b.coef_.to_int64();
    const std::int64_t g = std::gcd(den, mod(num, den));  // gcd(den, num): num mod den keeps it in int64
    Int128 q = num.floor_div(g);                          // exact: g divides num
    std::int64_t rest = den / g;
    int twos = 0, fives = 0;
    while (rest % 2 == 0) {
        rest /= 2;
        ++twos;
    }
    while (rest % 5 == 0) {
        rest /= 5;
        ++fives;
    }
    if (rest != 1) {
        return std::nullopt;  // a factor other than 2 and 5: the quotient never terminates (Inexact)
    }
    // q / (2^twos · 5^fives) = q · 2^(k - twos) · 5^(k - fives) / 10^k with k = max(twos, fives).
    const int k = std::max(twos, fives);
    for (int n = twos; n < k; ++n) {
        q = q * 2;
    }
    for (int n = fives; n < k; ++n) {
        q = q * 5;
    }
    SpanDecimal out;
    out.coef_ = neg ? -q : q;
    out.exp_ = checked_exponent(std::int64_t{a.exp_} - b.exp_ - k);
    out.normalise();
    std::string digits = out.coef_.to_string();
    if (!digits.empty() && digits.front() == '-') {
        digits.erase(0, 1);
    }
    if (static_cast<int>(digits.size()) > kOraclePrecision) {
        return std::nullopt;  // the oracle would round it: Inexact (unreachable inside Int128)
    }
    return out;
}

Int128 SpanDecimal::ceiling() const {
    if (exp_ >= 0) {
        return times_pow10(coef_, exp_);
    }
    Int128 x = coef_;
    std::int64_t k = -std::int64_t{exp_};
    while (k > 0) {  // ceil(ceil(x / a) / b) = ceil(x / ab) for positive a, b
        const int step = static_cast<int>(std::min<std::int64_t>(k, kMaxPow));
        x = x.ceil_div(kPow10[step]);
        k -= step;
    }
    return x;
}

std::optional<Int128> SpanDecimal::scaled_integer(int power) const {
    if (is_zero()) {
        return Int128(0);
    }
    const std::int64_t e = std::int64_t{exp_} + power;
    if (e < 0) {
        return std::nullopt;  // normalised: the coefficient has no trailing zero to absorb it
    }
    return times_pow10(coef_, e);
}

std::string SpanDecimal::to_string() const {
    std::string digits = coef_.to_string();
    std::string sign;
    if (!digits.empty() && digits.front() == '-') {
        sign = "-";
        digits.erase(0, 1);
    }
    if (exp_ >= 0) {
        return sign + digits + std::string(static_cast<std::size_t>(exp_), '0');
    }
    const auto frac = static_cast<std::size_t>(-std::int64_t{exp_});
    if (digits.size() <= frac) {
        digits.insert(0, frac - digits.size() + 1, '0');
    }
    digits.insert(digits.size() - frac, ".");
    return sign + digits;
}

Paise rupees_up_paise(const SpanDecimal& rupees) {
    if (rupees.negative()) {
        throw SpanCalcError("negative SPAN " + rupees.to_string());
    }
    return checked_mul(rupees.ceiling().to_int64(), kPaisePerRupee);  // whole rupees → paise
}

std::string key_name(const ContractKey& key) {
    return key.kind + " " + key.expiry + (key.strike ? " " + std::to_string(*key.strike) : std::string());
}

// ── From the bundle ──────────────────────────────────────────────────────────

namespace {

std::optional<std::string> opt_string(const Json& v, const std::string& what) {
    if (v.is_null()) {
        return std::nullopt;
    }
    if (!v.is_string()) {
        throw FormatError(what + ": expected a string or null");
    }
    return v.get<std::string>();
}

std::vector<Tier> tiers(const Json& rows, const std::string& what) {
    std::vector<Tier> out;
    for (const Json& t : rows) {
        out.push_back(Tier{t.at("number").get<std::int64_t>(), opt_string(t.at("start"), what + " start"),
                           opt_string(t.at("end"), what + " end"), t.at("rates")});
    }
    return out;
}

}  // namespace

Commodity commodity_from_bundle(const std::string& code, const Json& d) {
    Commodity cc;
    cc.code = code;
    for (const Json& c : d.at("contracts")) {
        ContractKey key{c.at("kind").get<std::string>(), c.at("expiry").get<std::string>(), std::nullopt};
        if (key.kind != "FUT" && !c.at("strike_paise").is_null()) {
            key.strike = c.at("strike_paise").get<Paise>();
        }
        Contract contract{c.at("price"), c.at("composite_delta"), {}};
        for (const Json& v : c.at("risk_array")) {
            contract.risk_array.push_back(v);
        }
        cc.contracts.insert_or_assign(std::move(key), std::move(contract));  // a repeated key: the last wins
    }
    cc.cvf = d.at("cvf");
    cc.som_method = opt_string(d.at("som_method"), code + " som_method");
    cc.scan_tiers = tiers(d.at("scan_tiers"), code + " scan tier");
    (void)d.at("intra_tiers");
    (void)d.at("inter_tiers");
    cc.som_tiers = tiers(d.at("som_tiers"), code + " som tier");
    for (const Json& sp : d.at("calendar_spreads")) {
        CalendarSpread spread{sp.at("number").get<std::int64_t>(),
                              opt_string(sp.at("charge_method"), code + " charge_method"), sp.at("rates"), {}};
        for (const Json& x : sp.at("legs")) {
            spread.legs.push_back(
                SpreadLeg{x.at("expiry").get<std::string>(), x.at("side").get<std::string>(), x.at("weight")});
        }
        cc.calendar_spreads.push_back(std::move(spread));
    }
    return cc;
}

// ── The calculation ──────────────────────────────────────────────────────────

namespace {

// The oracle's _dec: a finite decimal string, else SpanCalcError naming it.
SpanDecimal dec(const Json& v, const std::string& what) {
    if (!v.is_string()) {
        throw SpanCalcError(what + ": not a decimal: " + v.dump());
    }
    const std::string text = v.get<std::string>();
    const std::optional<SpanDecimal> value = SpanDecimal::parse(text);
    if (!value) {
        throw SpanCalcError(what + ": not a finite decimal: '" + text + "'");
    }
    return *value;
}

const Json* rate_of(const Json& rates) {
    if (!rates.is_object()) {
        return nullptr;
    }
    const auto it = rates.find(kRateClass);
    return it == rates.end() ? nullptr : &*it;
}

std::string range_text(const std::optional<std::string>& s) { return s ? *s : "None"; }

// _tier_for: the tier whose expiry range holds `expiry`; else the tier without a range; a half-open range is refused.
const Tier& tier_for(const std::vector<Tier>& tiers, const std::string& expiry, const std::string& what) {
    for (const Tier& t : tiers) {
        if (t.start.has_value() != t.end.has_value()) {
            throw SpanCalcError(what + ": tier " + std::to_string(t.number) + " has a half-open range " +
                                range_text(t.start) + ".." + range_text(t.end));
        }
    }
    for (const Tier& t : tiers) {
        if (t.start && t.end && *t.start <= expiry && expiry <= *t.end) {
            return t;
        }
    }
    for (const Tier& t : tiers) {
        if (!t.start && !t.end) {
            return t;
        }
    }
    throw SpanCalcError(what + ": no tier holds expiry " + expiry);
}

using Book = std::vector<std::pair<ContractKey, Qty>>;  // netted, non-zero, in first-seen order (a dict)

SpanDecimal scan_risk(const Commodity& cc, const Book& book) {
    std::map<std::optional<std::int64_t>, std::array<SpanDecimal, kScenarios>> groups;  // scan tier → losses
    for (const auto& [key, units] : book) {
        std::optional<std::int64_t> tier;
        if (!cc.scan_tiers.empty()) {
            tier = tier_for(cc.scan_tiers, key.expiry, cc.code + " scan tier").number;
        }
        auto& losses = groups[tier];
        const Contract& c = cc.contracts.at(key);
        if (c.risk_array.size() < kScenarios) {
            throw RunError(cc.code + " " + key_name(key) + ": the risk array has " +
                           std::to_string(c.risk_array.size()) + " values, not 16");
        }
        const SpanDecimal u(units);
        for (std::size_t s = 0; s < kScenarios; ++s) {
            losses[s] += u * dec(c.risk_array[s], key_name(key) + " ra[" + std::to_string(s + 1) + "]");  // rupees
        }
    }
    SpanDecimal total;
    for (const auto& [tier, losses] : groups) {
        total += std::max(*std::max_element(losses.begin(), losses.end()), SpanDecimal{});  // each tier floored at 0
    }
    return total;
}

SpanDecimal intra_spread(const Commodity& cc, const Book& book) {
    std::map<std::string, SpanDecimal> delta;  // expiry → net delta, units of the underlying
    for (const auto& [key, units] : book) {
        delta[key.expiry] += SpanDecimal(units) * dec(cc.contracts.at(key).composite_delta, key_name(key) + " delta");
    }
    auto delta_at = [&](const std::string& expiry) {
        const auto it = delta.find(expiry);
        return it == delta.end() ? SpanDecimal{} : it->second;
    };
    std::vector<const CalendarSpread*> spreads;
    for (const CalendarSpread& sp : cc.calendar_spreads) {
        spreads.push_back(&sp);
    }
    std::stable_sort(spreads.begin(), spreads.end(),
                     [](const CalendarSpread* x, const CalendarSpread* y) { return x->number < y->number; });
    SpanDecimal charge;  // rupees
    for (const CalendarSpread* spread : spreads) {
        if (spread->legs.empty()) {
            continue;
        }
        const std::string what = cc.code + " spread " + std::to_string(spread->number);
        std::vector<SpanDecimal> weights;
        for (const SpreadLeg& leg : spread->legs) {
            weights.push_back(dec(leg.weight, what + " weight"));
        }
        const SpanDecimal first = delta_at(spread->legs.front().expiry);
        if (first.is_zero()) {
            continue;
        }
        const int sign_a = first.sign() * (spread->legs.front().side == "A" ? 1 : -1);  // the sign side A needs
        std::vector<SpanDecimal> counts;
        bool formed = true;
        for (std::size_t n = 0; n < spread->legs.size(); ++n) {
            const SpreadLeg& leg = spread->legs[n];
            const SpanDecimal d = delta_at(leg.expiry);
            const int want = leg.side == "A" ? sign_a : -sign_a;
            if (d.is_zero() || (d.sign() > 0) != (want > 0)) {
                formed = false;
                break;
            }
            if (weights[n].sign() <= 0) {
                throw SpanCalcError(what + ": weight " + leg.weight.dump());
            }
            const std::optional<SpanDecimal> spreads_n = SpanDecimal::divide_exact(d.abs(), weights[n]);
            if (!spreads_n) {
                throw SpanCalcError(what + ": spreads not exact");
            }
            counts.push_back(*spreads_n);  // delta units / (delta units per spread) = spreads
        }
        if (!formed) {
            continue;
        }
        const SpanDecimal n = *std::min_element(counts.begin(), counts.end());
        if (n.is_zero()) {
            continue;
        }
        if (spread->charge_method != std::optional<std::string>("F")) {
            throw SpanCalcError(what + ": charge method " + spread->charge_method.value_or("None"));
        }
        const Json* rate = rate_of(spread->rates);
        if (rate == nullptr) {
            throw SpanCalcError(what + ": no rate class " + kRateClass);
        }
        charge += n * dec(*rate, what + " rate");  // spreads × rupees per spread
        for (std::size_t k = 0; k < spread->legs.size(); ++k) {
            const SpreadLeg& leg = spread->legs[k];
            const int want = leg.side == "A" ? sign_a : -sign_a;
            delta[leg.expiry] -= SpanDecimal(want) * n * weights[k];  // toward zero
        }
    }
    return charge;
}

SpanDecimal short_option_minimum(const Commodity& cc, const Book& book) {
    std::vector<std::pair<const ContractKey*, Qty>> shorts;
    for (const auto& [key, units] : book) {
        if (key.kind != "FUT" && units < 0) {
            shorts.emplace_back(&key, units);
        }
    }
    if (shorts.empty()) {
        return {};
    }
    if (cc.som_method != std::optional<std::string>("GROSS")) {
        throw SpanCalcError(cc.code + ": short option minimum method " + cc.som_method.value_or("None"));
    }
    SpanDecimal som;  // rupees
    for (const auto& [key, units] : shorts) {
        const Tier& tier = tier_for(cc.som_tiers, key->expiry, cc.code + " som tier");
        const Json* rate = rate_of(tier.rates);
        if (rate == nullptr) {
            throw SpanCalcError(cc.code + " som tier " + std::to_string(tier.number) + ": no rate class " +
                                kRateClass);
        }
        som += SpanDecimal(checked_sub(0, units)) * dec(*rate, cc.code + " som rate");  // short units × rupees/unit
    }
    return som;
}

}  // namespace

SpanResult span_requirement(const Commodity& cc, const std::vector<std::pair<ContractKey, Qty>>& legs) {
    Book book;  // _net: repeated keys netted in first-seen order, zeros dropped
    for (const auto& [key, units] : legs) {
        auto it = std::find_if(book.begin(), book.end(), [&](const auto& x) { return x.first == key; });
        if (it == book.end()) {
            book.emplace_back(key, units);
        } else {
            it->second = checked_add(it->second, units);
        }
    }
    std::erase_if(book, [](const auto& x) { return x.second == 0; });
    SpanResult out;
    for (const auto& [key, units] : book) {
        if (!cc.contracts.contains(key)) {
            out.missing.push_back(key);
        }
    }
    if (!out.missing.empty()) {
        return out;
    }
    std::set<std::string> pf_types;  // sorted, as the oracle's sorted({...})
    for (const auto& [key, units] : book) {
        pf_types.insert(key.kind == "FUT" ? "FUT" : "OOP");
    }
    for (const std::string& pf : pf_types) {
        const auto it = cc.cvf.is_object() ? cc.cvf.find(pf) : cc.cvf.end();
        const bool known = cc.cvf.is_object() && it != cc.cvf.end() && !it->is_null();
        if (!known || dec(*it, cc.code + " " + pf + " cvf") != SpanDecimal(1)) {
            const std::string shown = !known ? "None" : it->is_string() ? "'" + it->get<std::string>() + "'" : it->dump();
            throw SpanCalcError(cc.code + " " + pf + ": contract value factor " + shown +
                                "; only 1 (one unit) is handled");
        }
    }
    out.scan = scan_risk(cc, book);
    out.intra_spread = intra_spread(cc, book);
    out.inter_credit = SpanDecimal{};
    out.som = short_option_minimum(cc, book);
    for (const auto& [key, units] : book) {
        if (key.kind == "FUT") {
            continue;
        }
        const SpanDecimal value = SpanDecimal(units) * dec(cc.contracts.at(key).price, key_name(key) + " price");
        if (units > 0) {
            out.long_option_value += value;
        } else {
            out.short_option_value -= value;
        }
    }
    out.span = std::max({out.scan + out.intra_spread - out.inter_credit - out.long_option_value +
                             out.short_option_value,
                         out.som, SpanDecimal{}});
    return out;
}

Paise span_paise(const SpanResult& result) {
    if (!result.missing.empty()) {
        std::string names;
        for (const ContractKey& k : result.missing) {
            names += (names.empty() ? "" : ", ") + key_name(k);
        }
        throw SpanCalcError("no SPAN margin: missing " + names);
    }
    return rupees_up_paise(result.span);
}

}  // namespace stocksim::span
