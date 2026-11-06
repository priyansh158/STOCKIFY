#include "stocksim/pack_reader.hpp"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <cstring>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

namespace {

template <typename T>
T load(const std::byte* p) {
    T v;
    std::memcpy(&v, p, sizeof(T));
    return v;
}

}  // namespace

PackHeader validate_pack(const std::byte* data, std::size_t length) {
    if (length < kHeaderSize) {
        throw FormatError("not an .evt v1 file (" + std::to_string(length) + " bytes, shorter than the header)");
    }
    PackHeader h;
    h.version = load<std::uint16_t>(data + 8);
    h.flags = load<std::uint16_t>(data + 10);
    const auto header_size = load<std::uint32_t>(data + 12);
    h.count = load<std::uint64_t>(data + 16);
    h.t_min = load<std::int64_t>(data + 24);
    h.t_max = load<std::int64_t>(data + 32);
    h.price_basis = load<std::uint8_t>(data + 40);
    if (std::memcmp(data, kPackMagic, sizeof kPackMagic) != 0 || h.version != kPackVersion ||
        header_size != kHeaderSize) {
        throw FormatError("not an .evt v1 file (version " + std::to_string(h.version) + ", header " +
                          std::to_string(header_size) + ")");
    }
    if (h.price_basis > 1) {
        throw FormatError("unknown price basis " + std::to_string(h.price_basis) + " at byte 40");
    }
    std::size_t offset = kHeaderSize;
    std::uint64_t n = 0;
    bool have_last = false;
    EventKey last;
    while (offset < length) {
        const Event e = read_record(data, length, offset, 0);
        const EventKey key{e.t, rank_of(e.kind), 0, e.seq};
        if (have_last && key < last) {
            throw FormatError("records out of order at byte " + std::to_string(offset) + " (seq " +
                              std::to_string(e.seq) + ")");
        }
        last = key;
        have_last = true;
        offset += e.size;
        ++n;
    }
    if (n != h.count) {
        throw FormatError("pack has " + std::to_string(n) + " records, header says " + std::to_string(h.count));
    }
    return h;
}

PackReader::PackReader(const std::filesystem::path& path) : path_(path) {
    file_ = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file_ == INVALID_HANDLE_VALUE) {
        file_ = nullptr;
        throw FormatError("cannot open pack " + path.string() + " (error " + std::to_string(GetLastError()) + ")");
    }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file_, &size)) {
        CloseHandle(file_);
        throw FormatError("cannot size pack " + path.string());
    }
    length_ = static_cast<std::size_t>(size.QuadPart);
    if (length_ < kHeaderSize) {
        CloseHandle(file_);
        throw FormatError("not an .evt v1 file: " + path.string() + " has " + std::to_string(length_) + " bytes");
    }
    mapping_ = CreateFileMappingW(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (mapping_ == nullptr) {
        CloseHandle(file_);
        throw FormatError("cannot map pack " + path.string() + " (error " + std::to_string(GetLastError()) + ")");
    }
    data_ = static_cast<const std::byte*>(MapViewOfFile(mapping_, FILE_MAP_READ, 0, 0, 0));
    if (data_ == nullptr) {
        CloseHandle(mapping_);
        CloseHandle(file_);
        throw FormatError("cannot view pack " + path.string() + " (error " + std::to_string(GetLastError()) + ")");
    }
    try {
        header_ = validate_pack(data_, length_);
    } catch (const FormatError& e) {
        UnmapViewOfFile(data_);
        CloseHandle(mapping_);
        CloseHandle(file_);
        throw FormatError(path.string() + ": " + e.what());
    }
}

PackReader::~PackReader() {
    if (data_ != nullptr) {
        UnmapViewOfFile(data_);
    }
    if (mapping_ != nullptr) {
        CloseHandle(mapping_);
    }
    if (file_ != nullptr) {
        CloseHandle(file_);
    }
}

}  // namespace stocksim
