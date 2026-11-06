// Run bundle loader (A1 §6.2). Python resolves every input into bundle.json (I-11); money and rates in it are
// already integers (paise, ppb, ppm, bp), so nothing here parses decimals or floats for money.
//
// M3.1 reads the envelope (schema, seed, packs, strategy, labels) and keeps the parsed document; the typed
// views of days, instruments, charges and policies are added by the tasks that use them (M3.3–M3.8).
#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

#include "nlohmann/json.hpp"

namespace stocksim {

// ordered_json keeps the file's key order, so anything the oracle iterates in dict order (a day's groups,
// mis_cutoff_ns, strategy params) is iterated in the same order here.
using Json = nlohmann::ordered_json;

inline constexpr std::string_view kBundleSchema = "stockify.bundle/1";

struct PackRef {
    std::string sha256;
    std::string path;         // as written in the bundle (relative to the lab folder); Python passes absolute paths
    std::string price_basis;  // "raw" | "adjusted" (I-5)
};

struct StrategyRef {
    std::string kind;  // "python" | "cpp"
    std::string ref;
    std::string source_sha256;
    Json params;
};

struct Bundle {
    std::string sha256;  // of the bundle file's bytes
    std::uint64_t seed = 0;
    std::vector<PackRef> packs;
    StrategyRef strategy;
    std::vector<std::string> labels;
    Json root;  // the whole document
};

// FormatError on unreadable input, invalid JSON, a wrong schema or a missing / mistyped envelope field.
Bundle parse_bundle(std::string_view text);
Bundle load_bundle(const std::filesystem::path& path);

// Typed access with a FormatError that names the JSON path.
const Json& require(const Json& obj, std::string_view key, std::string_view where);
std::int64_t require_int(const Json& obj, std::string_view key, std::string_view where);
std::string require_string(const Json& obj, std::string_view key, std::string_view where);

}  // namespace stocksim
