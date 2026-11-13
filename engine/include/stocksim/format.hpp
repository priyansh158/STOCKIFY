// Canonical output (A4 §9.2), the twins of stockify.numerics.format_float and stockify.oracle.artifacts._csv.
//
// Floats: the shortest round-trip digits, laid out like Python's repr: fixed notation when the decimal point
// position decpt satisfies -4 < decpt <= 16 (1e-4 <= |x| < 1e16), otherwise d.ddde±XX with at least two
// exponent digits; integral values in fixed notation end in ".0". Non-finite values are a NumericError.
//
// CSV: a header line, then one line per row, each ending in "\n"; cells are empty (null), true/false,
// integers or plain strings. A string holding ',', '"' or '\n' is quoted with '"' doubled. Never floats.
#pragma once

#include <cstdint>
#include <filesystem>
#include <initializer_list>
#include <string>
#include <string_view>
#include <variant>

namespace stocksim {

std::string format_float(double x);

struct CsvNull {};

// One CSV cell; a std::string_view must outlive the add_row call only.
using CsvCell = std::variant<CsvNull, bool, std::int64_t, std::string_view>;

std::string csv_cell(const CsvCell& cell);

class CsvWriter {
 public:
    explicit CsvWriter(std::string header);
    void add_row(std::initializer_list<CsvCell> cells);
    const std::string& text() const { return text_; }
    std::size_t rows() const { return rows_; }
    void write(const std::filesystem::path& path) const;  // bytes exactly as text(); FormatError on failure

 private:
    std::string text_;
    std::size_t rows_ = 0;
};

// Write bytes to a file exactly (binary mode, no newline translation).
void write_file_bytes(const std::filesystem::path& path, std::string_view bytes);

}  // namespace stocksim
