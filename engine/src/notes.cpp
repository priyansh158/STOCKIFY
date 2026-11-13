#include "stocksim/notes.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <iterator>
#include <ranges>
#include <vector>

#include "stocksim/errors.hpp"

namespace stocksim {
namespace {

constexpr char32_t kReplacement = 0xFFFD;
constexpr char32_t kEllipsis = 0x2026;

// One UTF-8 decode step on a non-empty view. Returns the code point and sets `used` to the bytes consumed; an
// invalid sequence gives U+FFFD and consumes its maximal ill-formed prefix (at least one byte).
char32_t decode_one(std::string_view s, std::size_t& used) {
    const auto b0 = static_cast<unsigned char>(s.front());
    used = 1;
    if (b0 < 0x80) return b0;

    std::size_t need = 0;
    unsigned char lo = 0x80;  // the allowed range of the second byte
    unsigned char hi = 0xBF;
    char32_t cp = 0;
    if (b0 >= 0xC2 && b0 <= 0xDF) {
        need = 1;
        cp = b0 & 0x1Fu;
    } else if (b0 >= 0xE0 && b0 <= 0xEF) {
        need = 2;
        cp = b0 & 0x0Fu;
        if (b0 == 0xE0) lo = 0xA0;  // no overlong forms
        if (b0 == 0xED) hi = 0x9F;  // U+D800..U+DFFF are not scalar values
    } else if (b0 >= 0xF0 && b0 <= 0xF4) {
        need = 3;
        cp = b0 & 0x07u;
        if (b0 == 0xF0) lo = 0x90;
        if (b0 == 0xF4) hi = 0x8F;  // nothing above U+10FFFF
    } else {
        return kReplacement;  // a stray continuation byte, C0, C1 or F5..FF
    }

    for (std::size_t i = 1; i <= need; ++i) {
        if (i >= s.size()) return kReplacement;  // truncated: the valid prefix is one bad sequence
        const auto b = static_cast<unsigned char>(s[i]);
        if (b < (i == 1 ? lo : 0x80) || b > (i == 1 ? hi : 0xBF)) return kReplacement;
        cp = (cp << 6) | (b & 0x3Fu);
        used = i + 1;
    }
    return cp;
}

void append_utf8(std::string& out, char32_t cp) {
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

constexpr bool is_blanked(char32_t cp) {
    return cp < 0x20 || (cp >= 0x7F && cp <= 0x9F) || cp == 0x2028 || cp == 0x2029;
}

// The magnitude of v as an unsigned value: well defined for INT64_MIN too (unsigned negation wraps by definition).
constexpr std::uint64_t magnitude(std::int64_t v) {
    const auto u = static_cast<std::uint64_t>(v);
    return v < 0 ? 0 - u : u;
}

// The digits of n, the last three together and the rest in pairs: 1234567 -> "12,34,567".
std::string group_indian(std::uint64_t n) {
    std::array<char, 20> buf{};
    const auto end = std::to_chars(buf.data(), buf.data() + buf.size(), n).ptr;
    const std::string_view digits(buf.data(), static_cast<std::size_t>(end - buf.data()));
    if (digits.size() <= 3) return std::string(digits);
    const std::string_view head = digits.substr(0, digits.size() - 3);
    std::string out;
    for (std::size_t i = 0; i < head.size(); ++i) {
        if (i != 0 && (head.size() - i) % 2 == 0) out += ',';
        out += head[i];
    }
    out += ',';
    out += digits.substr(digits.size() - 3);
    return out;
}

}  // namespace

std::string clean_note(std::string_view text) {
    std::vector<char32_t> cps;
    cps.reserve(text.size());
    while (!text.empty()) {
        std::size_t used = 1;
        const char32_t cp = decode_one(text, used);
        cps.push_back(is_blanked(cp) ? U' ' : cp);
        text.remove_prefix(used);
    }
    const auto is_space = [](char32_t c) { return c == U' '; };
    const auto first = std::ranges::find_if_not(cps, is_space);
    auto kept = std::ranges::subrange(first, cps.end()) | std::views::reverse | std::views::drop_while(is_space) |
                std::views::reverse;
    std::vector<char32_t> cleaned(kept.begin(), kept.end());
    if (cleaned.size() > kNoteMax) {
        cleaned.resize(kNoteMax - 1);
        cleaned.push_back(kEllipsis);
    }
    std::string out;
    out.reserve(cleaned.size());
    for (const char32_t cp : cleaned) append_utf8(out, cp);
    return out;
}

std::string fmt_price(Paise paise) {
    const std::uint64_t mag = magnitude(paise);
    const std::uint64_t frac = mag % 100;
    std::string out = paise < 0 ? "-" : "";
    out += group_indian(mag / 100);
    out += '.';
    out += static_cast<char>('0' + frac / 10);
    out += static_cast<char>('0' + frac % 10);
    return out;
}

std::string fmt_qty(Qty units) {
    std::string out = units < 0 ? "-" : "";
    out += group_indian(magnitude(units));
    return out;
}

Paise to_level(double x) {
    if (!std::isfinite(x)) throw RunError("a level must be finite");
    const double r = std::nearbyint(x * 10000.0);
    // 2^63 is exactly representable; anything in [-2^63, 2^63) converts without overflow.
    if (!(r >= -9223372036854775808.0 && r < 9223372036854775808.0)) throw RunError("a level does not fit in an int64");
    return static_cast<Paise>(r);
}

}  // namespace stocksim
