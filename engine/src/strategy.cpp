// The C++ strategy registry and the param readers (A2 §2, §4.2).
#include "stocksim/strategy.hpp"

#include <cmath>
#include <functional>
#include <map>
#include <stdexcept>

#include "stocksim/decimal.hpp"
#include "stocksim/errors.hpp"

namespace stocksim {

namespace {

// A function-local static: registrations run during static initialisation, in no particular order.
std::map<std::string, StrategyFactory, std::less<>>& registry() {
    static std::map<std::string, StrategyFactory, std::less<>> r;
    return r;
}

const Json* find(const Json& params, std::string_view key) {
    if (!params.is_object()) {
        return nullptr;
    }
    const auto it = params.find(std::string(key));
    return it == params.end() ? nullptr : &*it;
}

[[noreturn]] void bad(std::string_view key, const std::string& why) {
    throw RunError("strategy param '" + std::string(key) + "' " + why);
}

}  // namespace

bool register_strategy(std::string_view name, StrategyFactory make) {
    if (!registry().emplace(std::string(name), make).second) {
        throw std::logic_error("two C++ strategies are registered as '" + std::string(name) + "'");
    }
    return true;
}

std::shared_ptr<Strategy> make_strategy(std::string_view name) {
    const auto it = registry().find(name);
    if (it == registry().end()) {
        std::string known;
        for (const auto& [n, f] : registry()) {
            known += (known.empty() ? "" : ", ") + n;
        }
        throw RunError("no C++ strategy is registered as '" + std::string(name) + "' (known: " + known + ")");
    }
    return it->second();
}

std::vector<std::string> strategy_names() {
    std::vector<std::string> out;
    for (const auto& [n, f] : registry()) {
        out.push_back(n);
    }
    return out;
}

std::int64_t param_int(const Json& params, std::string_view key, std::optional<std::int64_t> fallback) {
    const Json* v = find(params, key);
    if (v == nullptr) {
        if (!fallback) {
            bad(key, "is missing");
        }
        return *fallback;
    }
    if (v->is_boolean()) {
        return v->get<bool>() ? 1 : 0;
    }
    if (v->is_number_unsigned()) {
        const auto u = v->get<std::uint64_t>();
        if (u > static_cast<std::uint64_t>(INT64_MAX)) {
            bad(key, "is beyond int64");
        }
        return static_cast<std::int64_t>(u);
    }
    if (v->is_number_integer()) {
        return v->get<std::int64_t>();
    }
    if (v->is_number_float()) {  // int(2.7) == 2: toward zero
        const double t = std::trunc(v->get<double>());
        if (!(t >= -9.223372036854775808e18 && t < 9.223372036854775808e18)) {
            bad(key, "is beyond int64");
        }
        return static_cast<std::int64_t>(t);
    }
    if (v->is_string()) {
        if (const auto i = python_int(v->get_ref<const std::string&>())) {
            return *i;
        }
    }
    bad(key, "is not an integer: " + v->dump());
}

bool param_bool(const Json& params, std::string_view key, bool fallback) {
    const Json* v = find(params, key);
    if (v == nullptr) {
        return fallback;
    }
    switch (v->type()) {
        case Json::value_t::null:
            return false;
        case Json::value_t::boolean:
            return v->get<bool>();
        case Json::value_t::number_integer:
            return v->get<std::int64_t>() != 0;
        case Json::value_t::number_unsigned:
            return v->get<std::uint64_t>() != 0;
        case Json::value_t::number_float:
            return v->get<double>() != 0.0;
        case Json::value_t::string:
            return !v->get_ref<const std::string&>().empty();
        default:
            return !v->empty();  // arrays and objects
    }
}

std::string param_string(const Json& params, std::string_view key, std::optional<std::string> fallback) {
    const Json* v = find(params, key);
    if (v == nullptr) {
        if (!fallback) {
            bad(key, "is missing");
        }
        return *fallback;
    }
    if (!v->is_string()) {
        bad(key, "is not a string: " + v->dump());
    }
    return v->get<std::string>();
}

}  // namespace stocksim
