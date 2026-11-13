// Checked reads of a C++ strategy's saved in-day state (A2 §4.1, CH 6.24).
//
// A paper session hands a twin the state its Python original would have saved: a JSON object of exactly the
// original's attributes. Whatever the text holds, a read that does not fit raises RunError naming the strategy and
// the key: a missing key, a key that is not expected, a type that is not the one written, a value out of range.
// "{}" is the state written before the twins kept any: it is accepted by the callers as the fresh-day defaults.
#pragma once

#include <cstdint>
#include <initializer_list>
#include <optional>
#include <string>
#include <string_view>

#include "stocksim/errors.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/session.hpp"

namespace stocksim::state {

[[noreturn]] inline void refuse(std::string_view owner, std::string_view key, const std::string& what) {
    throw RunError(std::string(owner) + " state: " + std::string(key) + " " + what);
}

// The state is a JSON object whose keys are all among `allowed`.
inline const Json& as_object(std::string_view owner, const Json& state, std::initializer_list<std::string_view> allowed) {
    if (!state.is_object()) {
        refuse(owner, "(the state)", "is not an object");
    }
    for (const auto& [key, value] : state.items()) {
        bool known = false;
        for (const std::string_view a : allowed) {
            known = known || key == a;
        }
        if (!known) {
            refuse(owner, key, "is not a key this strategy saves");
        }
    }
    return state;
}

inline const Json& need(std::string_view owner, const Json& state, std::string_view key) {
    const auto it = state.find(std::string(key));
    if (it == state.end()) {
        refuse(owner, key, "is missing");
    }
    return *it;
}

// An integer written as an integer (not a float, not a bool, not text) inside [lo, hi].
inline std::int64_t read_int(std::string_view owner, const Json& state, std::string_view key,
                             std::int64_t lo = INT64_MIN, std::int64_t hi = INT64_MAX) {
    const Json& v = need(owner, state, key);
    if (v.is_boolean() || !(v.is_number_integer() || v.is_number_unsigned())) {
        refuse(owner, key, "is not an integer");
    }
    if (v.is_number_unsigned() && v.get<std::uint64_t>() > static_cast<std::uint64_t>(INT64_MAX)) {
        refuse(owner, key, "is beyond 64 bits");
    }
    const auto n = v.get<std::int64_t>();
    if (n < lo || n > hi) {
        refuse(owner, key, "is out of range: " + std::to_string(n));
    }
    return n;
}

inline std::optional<std::int64_t> read_opt_int(std::string_view owner, const Json& state, std::string_view key,
                                                std::int64_t lo = INT64_MIN, std::int64_t hi = INT64_MAX) {
    if (need(owner, state, key).is_null()) {
        return std::nullopt;
    }
    return read_int(owner, state, key, lo, hi);
}

inline bool read_bool(std::string_view owner, const Json& state, std::string_view key) {
    const Json& v = need(owner, state, key);
    if (!v.is_boolean()) {
        refuse(owner, key, "is not true or false");
    }
    return v.get<bool>();
}

inline std::string read_str(std::string_view owner, const Json& state, std::string_view key) {
    const Json& v = need(owner, state, key);
    if (!v.is_string()) {
        refuse(owner, key, "is not text");
    }
    return v.get<std::string>();
}

inline std::optional<std::string> read_opt_str(std::string_view owner, const Json& state, std::string_view key) {
    if (need(owner, state, key).is_null()) {
        return std::nullopt;
    }
    return read_str(owner, state, key);
}

// A date written as Python's date.isoformat(): YYYY-MM-DD, a real calendar day, year 1 or later.
inline std::optional<Date> read_opt_date(std::string_view owner, const Json& state, std::string_view key) {
    const std::optional<std::string> text = read_opt_str(owner, state, key);
    if (!text) {
        return std::nullopt;
    }
    try {
        const Date d = parse_iso_date(*text);
        if (d < 10101) {
            refuse(owner, key, "is not a date: " + *text);
        }
        return d;
    } catch (const RunError&) {
        throw;
    } catch (const std::exception&) {
        refuse(owner, key, "is not an ISO date: " + *text);
    }
}

}  // namespace stocksim::state
