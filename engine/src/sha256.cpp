#include "stocksim/sha256.hpp"

#include <fstream>
#include <iterator>
#include <vector>

#include "picosha2.h"
#include "stocksim/errors.hpp"

namespace stocksim {

std::string sha256_hex(std::span<const std::byte> data) {
    const auto* first = reinterpret_cast<const unsigned char*>(data.data());
    return picosha2::hash256_hex_string(first, first + data.size());
}

std::string sha256_hex(std::string_view text) {
    return sha256_hex(std::as_bytes(std::span<const char>(text.data(), text.size())));
}

std::string sha256_file_hex(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw FormatError("cannot read " + path.string());
    }
    picosha2::hash256_one_by_one hasher;
    std::vector<unsigned char> buffer(1 << 20);
    while (in) {
        in.read(reinterpret_cast<char*>(buffer.data()), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize got = in.gcount();
        if (got > 0) {
            hasher.process(buffer.begin(), buffer.begin() + got);
        }
    }
    if (in.bad()) {
        throw FormatError("error while reading " + path.string());
    }
    hasher.finish();
    return picosha2::get_hash_hex_string(hasher);
}

}  // namespace stocksim
