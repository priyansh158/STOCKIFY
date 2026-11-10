#include "stocksim/json_out.hpp"

#include <algorithm>
#include <stdexcept>

#include "stocksim/format.hpp"

namespace stocksim {

JValue& JValue::operator[](const std::string& key) {
    if (std::holds_alternative<std::monostate>(v_)) {
        v_ = Object{};
    }
    auto* obj = std::get_if<Object>(&v_);
    if (obj == nullptr) {
        throw std::logic_error("JValue: not an object");
    }
    for (auto& [k, v] : *obj) {
        if (k == key) {
            return v;
        }
    }
    obj->emplace_back(key, JValue{});
    return obj->back().second;
}

void JValue::push_back(JValue v) {
    if (std::holds_alternative<std::monostate>(v_)) {
        v_ = Array{};
    }
    auto* arr = std::get_if<Array>(&v_);
    if (arr == nullptr) {
        throw std::logic_error("JValue: not an array");
    }
    arr->push_back(std::move(v));
}

std::string json_quote(std::string_view text) {
    static constexpr char hex[] = "0123456789abcdef";
    std::string out = "\"";
    for (char ch : text) {
        const auto code = static_cast<unsigned char>(ch);
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (code < 0x20) {
                    out += "\\u00";
                    out.push_back(hex[code >> 4]);
                    out.push_back(hex[code & 0xF]);
                } else {
                    out.push_back(ch);
                }
        }
    }
    out.push_back('"');
    return out;
}

namespace {

void write(const JValue& value, std::string& out) {
    const auto& v = value.variant();
    if (std::holds_alternative<std::monostate>(v)) {
        out += "null";
    } else if (const bool* b = std::get_if<bool>(&v)) {
        out += *b ? "true" : "false";
    } else if (const std::int64_t* i = std::get_if<std::int64_t>(&v)) {
        out += std::to_string(*i);
    } else if (const std::uint64_t* u = std::get_if<std::uint64_t>(&v)) {
        out += std::to_string(*u);
    } else if (const double* d = std::get_if<double>(&v)) {
        out += format_float(*d);
    } else if (const std::string* s = std::get_if<std::string>(&v)) {
        out += json_quote(*s);
    } else if (const auto* a = std::get_if<JValue::Array>(&v)) {
        out.push_back('[');
        bool first = true;
        for (const JValue& item : *a) {
            if (!first) {
                out.push_back(',');
            }
            first = false;
            write(item, out);
        }
        out.push_back(']');
    } else {
        const auto& o = std::get<JValue::Object>(v);
        std::vector<const std::pair<std::string, JValue>*> items;
        items.reserve(o.size());
        for (const auto& kv : o) {
            items.push_back(&kv);
        }
        std::sort(items.begin(), items.end(), [](const auto* a, const auto* b) { return a->first < b->first; });
        out.push_back('{');
        bool first = true;
        for (const auto* kv : items) {
            if (!first) {
                out.push_back(',');
            }
            first = false;
            out += json_quote(kv->first);
            out.push_back(':');
            write(kv->second, out);
        }
        out.push_back('}');
    }
}

}  // namespace

std::string dumps_canonical(const JValue& value) {
    std::string out;
    write(value, out);
    return out;
}

}  // namespace stocksim
