#include "stocksim/format.hpp"

#include <charconv>
#include <cmath>
#include <fstream>
#include <utility>

#include "stocksim/errors.hpp"

namespace stocksim {

std::string format_float(double x) {
    if (!std::isfinite(x)) {
        throw NumericError("non-finite float has no canonical form");
    }
    const bool negative = std::signbit(x);
    if (x == 0.0) {
        return negative ? "-0.0" : "0.0";
    }
    // Shortest round-trip digits in scientific form, e.g. "-1.2345e-07".
    char buf[64];
    const auto res = std::to_chars(buf, buf + sizeof buf, x, std::chars_format::scientific);
    if (res.ec != std::errc{}) {
        throw NumericError("float formatting failed");
    }
    const std::string_view sci(buf, static_cast<std::size_t>(res.ptr - buf));
    std::size_t i = negative ? 1 : 0;
    std::string digits;
    while (i < sci.size() && sci[i] != 'e') {
        if (sci[i] != '.') {
            digits.push_back(sci[i]);
        }
        ++i;
    }
    int exp10 = 0;
    std::from_chars(sci.data() + i + 1 + (sci[i + 1] == '+' ? 1 : 0), sci.data() + sci.size(), exp10);
    const int ndigits = static_cast<int>(digits.size());
    const int decpt = exp10 + 1;  // position of the decimal point relative to the first digit

    std::string out = negative ? "-" : "";
    if (decpt <= -4 || decpt > 16) {
        out.push_back(digits[0]);
        if (ndigits > 1) {
            out.push_back('.');
            out.append(digits, 1);
        }
        const int e = decpt - 1;
        out.push_back('e');
        out.push_back(e < 0 ? '-' : '+');
        const std::string mag = std::to_string(e < 0 ? -e : e);
        if (mag.size() < 2) {
            out.push_back('0');
        }
        out += mag;
    } else if (decpt <= 0) {
        out += "0.";
        out.append(static_cast<std::size_t>(-decpt), '0');
        out += digits;
    } else if (decpt < ndigits) {
        out.append(digits, 0, static_cast<std::size_t>(decpt));
        out.push_back('.');
        out.append(digits, static_cast<std::size_t>(decpt));
    } else {
        out += digits;
        out.append(static_cast<std::size_t>(decpt - ndigits), '0');
        out += ".0";
    }
    return out;
}

std::string csv_cell(const CsvCell& cell) {
    if (std::holds_alternative<CsvNull>(cell)) {
        return "";
    }
    if (const bool* b = std::get_if<bool>(&cell)) {
        return *b ? "true" : "false";
    }
    if (const std::int64_t* v = std::get_if<std::int64_t>(&cell)) {
        return std::to_string(*v);
    }
    const std::string_view s = std::get<std::string_view>(cell);
    if (s.find_first_of(",\"\n\r") == std::string_view::npos) {
        return std::string(s);
    }
    std::string out = "\"";
    for (char c : s) {
        if (c == '"') {
            out += "\"\"";
        } else {
            out.push_back(c);
        }
    }
    out.push_back('"');
    return out;
}

CsvWriter::CsvWriter(std::string header) : text_(std::move(header)) { text_.push_back('\n'); }

void CsvWriter::add_row(std::initializer_list<CsvCell> cells) {
    bool first = true;
    for (const CsvCell& c : cells) {
        if (!first) {
            text_.push_back(',');
        }
        first = false;
        text_ += csv_cell(c);
    }
    text_.push_back('\n');
    ++rows_;
}

void CsvWriter::write(const std::filesystem::path& path) const { write_file_bytes(path, text_); }

void write_file_bytes(const std::filesystem::path& path, std::string_view bytes) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out) {
        throw FormatError("cannot write " + path.string());
    }
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    if (!out) {
        throw FormatError("error while writing " + path.string());
    }
}

}  // namespace stocksim
