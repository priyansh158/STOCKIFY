// Canonical JSON writer (A4 §9.2), the twin of stockify.numerics.dumps_canonical: sorted keys, no spaces,
// floats via format_float, strings escaped exactly as the oracle does (", \, \n, \r, \t by name; other
// control characters as \u00XX; everything else, non-ASCII included, written as UTF-8). nlohmann/json is used
// only for reading bundles (A2 §1).
#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace stocksim {

class JValue {
 public:
    using Array = std::vector<JValue>;
    // Insertion order is kept; the writer sorts keys by byte, which for UTF-8 is code point order (Python's).
    using Object = std::vector<std::pair<std::string, JValue>>;

    JValue() : v_(std::monostate{}) {}                      // null
    JValue(bool b) : v_(b) {}                               // NOLINT: implicit by design
    JValue(int v) : v_(static_cast<std::int64_t>(v)) {}     // NOLINT
    JValue(std::int64_t v) : v_(v) {}                       // NOLINT
    JValue(std::uint64_t v) : v_(v) {}                      // NOLINT: PCG32 stream positions (A1 §6.4)
    JValue(double v) : v_(v) {}                             // NOLINT
    JValue(const char* s) : v_(std::string(s)) {}           // NOLINT
    JValue(std::string s) : v_(std::move(s)) {}             // NOLINT
    JValue(std::string_view s) : v_(std::string(s)) {}      // NOLINT
    JValue(Array a) : v_(std::move(a)) {}                   // NOLINT
    JValue(Object o) : v_(std::move(o)) {}                  // NOLINT

    static JValue array() { return JValue(Array{}); }
    static JValue object() { return JValue(Object{}); }

    // Object access (creates the key); array append. Both throw std::logic_error on the wrong kind.
    JValue& operator[](const std::string& key);
    void push_back(JValue v);

    const auto& variant() const { return v_; }

 private:
    std::variant<std::monostate, bool, std::int64_t, std::uint64_t, double, std::string, Array, Object> v_;
};

std::string json_quote(std::string_view text);
std::string dumps_canonical(const JValue& value);

}  // namespace stocksim
