// Memory-mapped reader for .evt event packs (A1 §5; MapViewOfFile, Windows only, D-26).
//
// Opening a pack validates it completely, with the same checks as stockify.data.packs.read: magic, version,
// header size, known kinds, record sizes (REFDATA 32 + len), non-decreasing (t_event, rank, seq), no trailing
// bytes, and the header's record count. Any violation is a FormatError naming the byte offset.
#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>

#include "stocksim/event.hpp"

namespace stocksim {

struct PackHeader {
    std::uint16_t version = 0;
    std::uint16_t flags = 0;
    std::uint64_t count = 0;
    TimeNs t_min = 0;
    TimeNs t_max = 0;
    std::uint8_t price_basis = 0;  // 0 raw, 1 adjusted

    std::string price_basis_name() const { return price_basis == 0 ? "raw" : "adjusted"; }
};

// Validates a whole pack held in memory (header + records). Used by PackReader and by tests.
PackHeader validate_pack(const std::byte* data, std::size_t length);

class PackReader {
 public:
    explicit PackReader(const std::filesystem::path& path);
    ~PackReader();
    PackReader(const PackReader&) = delete;
    PackReader& operator=(const PackReader&) = delete;

    const PackHeader& header() const { return header_; }
    const std::byte* data() const { return data_; }
    std::size_t length() const { return length_; }
    const std::filesystem::path& path() const { return path_; }

 private:
    std::filesystem::path path_;
    void* file_ = nullptr;     // HANDLE
    void* mapping_ = nullptr;  // HANDLE
    const std::byte* data_ = nullptr;
    std::size_t length_ = 0;
    PackHeader header_;
};

}  // namespace stocksim
