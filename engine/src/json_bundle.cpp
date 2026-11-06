#include "stocksim/json_bundle.hpp"

#include <fstream>
#include <iterator>
#include <sstream>

#include "stocksim/errors.hpp"
#include "stocksim/sha256.hpp"

namespace stocksim {

const nlohmann::json& require(const nlohmann::json& obj, std::string_view key, std::string_view where) {
    if (!obj.is_object()) {
        throw FormatError("bundle: " + std::string(where) + " is not an object");
    }
    const auto it = obj.find(std::string(key));
    if (it == obj.end()) {
        throw FormatError("bundle: missing " + std::string(where) + "." + std::string(key));
    }
    return *it;
}

std::int64_t require_int(const nlohmann::json& obj, std::string_view key, std::string_view where) {
    const nlohmann::json& v = require(obj, key, where);
    if (!v.is_number_integer()) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " is not an integer");
    }
    if (v.is_number_unsigned() && v.get<std::uint64_t>() > static_cast<std::uint64_t>(INT64_MAX)) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " does not fit in int64");
    }
    return v.get<std::int64_t>();
}

std::string require_string(const nlohmann::json& obj, std::string_view key, std::string_view where) {
    const nlohmann::json& v = require(obj, key, where);
    if (!v.is_string()) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " is not a string");
    }
    return v.get<std::string>();
}

Bundle parse_bundle(std::string_view text) {
    Bundle b;
    b.sha256 = sha256_hex(text);
    try {
        b.root = nlohmann::json::parse(text.begin(), text.end());
    } catch (const nlohmann::json::parse_error& e) {
        throw FormatError(std::string("bundle: invalid JSON: ") + e.what());
    }
    const nlohmann::json& r = b.root;
    if (require_string(r, "schema", "bundle") != kBundleSchema) {
        throw FormatError("bundle: unsupported schema '" + r["schema"].get<std::string>() + "', expected " +
                          std::string(kBundleSchema));
    }
    const nlohmann::json& seed = require(r, "seed", "bundle");
    if (!seed.is_number_integer() || (seed.is_number_integer() && !seed.is_number_unsigned() &&
                                      seed.get<std::int64_t>() < 0)) {
        throw FormatError("bundle: seed must be a non-negative integer");
    }
    b.seed = seed.get<std::uint64_t>();

    const nlohmann::json& packs = require(r, "packs", "bundle");
    if (!packs.is_array()) {
        throw FormatError("bundle: packs is not a list");
    }
    for (std::size_t i = 0; i < packs.size(); ++i) {
        const std::string where = "packs[" + std::to_string(i) + "]";
        b.packs.push_back(PackRef{require_string(packs[i], "sha256", where), require_string(packs[i], "path", where),
                                  require_string(packs[i], "price_basis", where)});
    }

    const nlohmann::json& s = require(r, "strategy", "bundle");
    b.strategy.kind = require_string(s, "kind", "strategy");
    b.strategy.ref = require_string(s, "ref", "strategy");
    if (const auto it = s.find("source_sha256"); it != s.end() && it->is_string()) {
        b.strategy.source_sha256 = it->get<std::string>();
    }
    b.strategy.params = s.contains("params") ? s["params"] : nlohmann::json::object();

    const nlohmann::json& labels = require(r, "labels", "bundle");
    if (!labels.is_array()) {
        throw FormatError("bundle: labels is not a list");
    }
    for (const auto& l : labels) {
        if (!l.is_string()) {
            throw FormatError("bundle: a label is not a string");
        }
        b.labels.push_back(l.get<std::string>());
    }
    return b;
}

Bundle load_bundle(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw FormatError("cannot read bundle " + path.string());
    }
    const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    return parse_bundle(text);
}

}  // namespace stocksim
