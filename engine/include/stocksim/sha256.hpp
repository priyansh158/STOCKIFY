// SHA-256 over bytes and files (a thin wrapper over the vendored PicoSHA2): bundle, pack and artifact hashes.
#pragma once

#include <cstddef>
#include <filesystem>
#include <span>
#include <string>
#include <string_view>

namespace stocksim {

// Lowercase hex digest.
std::string sha256_hex(std::span<const std::byte> data);
std::string sha256_hex(std::string_view text);
std::string sha256_file_hex(const std::filesystem::path& path);  // FormatError if the file cannot be read

}  // namespace stocksim
