#include "stocksim/json_bundle.hpp"

#include <fstream>
#include <iterator>
#include <sstream>

#include "stocksim/errors.hpp"
#include "stocksim/sha256.hpp"

namespace stocksim {

const Json& require(const Json& obj, std::string_view key, std::string_view where) {
    if (!obj.is_object()) {
        throw FormatError("bundle: " + std::string(where) + " is not an object");
    }
    const auto it = obj.find(std::string(key));
    if (it == obj.end()) {
        throw FormatError("bundle: missing " + std::string(where) + "." + std::string(key));
    }
    return *it;
}

std::int64_t require_int(const Json& obj, std::string_view key, std::string_view where) {
    const Json& v = require(obj, key, where);
    if (!v.is_number_integer()) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " is not an integer");
    }
    if (v.is_number_unsigned() && v.get<std::uint64_t>() > static_cast<std::uint64_t>(INT64_MAX)) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " does not fit in int64");
    }
    return v.get<std::int64_t>();
}

std::string require_string(const Json& obj, std::string_view key, std::string_view where) {
    const Json& v = require(obj, key, where);
    if (!v.is_string()) {
        throw FormatError("bundle: " + std::string(where) + "." + std::string(key) + " is not a string");
    }
    return v.get<std::string>();
}

Bundle parse_bundle(std::string_view text) {
    Bundle b;
    b.sha256 = sha256_hex(text);
    try {
        b.root = Json::parse(text.begin(), text.end());
    } catch (const Json::parse_error& e) {
        throw FormatError(std::string("bundle: invalid JSON: ") + e.what());
    }
    const Json& r = b.root;
    if (require_string(r, "schema", "bundle") != kBundleSchema) {
        throw FormatError("bundle: unsupported schema '" + r["schema"].get<std::string>() + "', expected " +
                          std::string(kBundleSchema));
    }
    const Json& seed = require(r, "seed", "bundle");
    if (!seed.is_number_integer() || (seed.is_number_integer() && !seed.is_number_unsigned() &&
                                      seed.get<std::int64_t>() < 0)) {
        throw FormatError("bundle: seed must be a non-negative integer");
    }
    b.seed = seed.get<std::uint64_t>();

    // days[].seed (seed_mode per_day, VOL 5.1) is the only source of a day's seed; the days themselves are
    // read by SessionCalendar.
    if (const auto days = r.find("days"); days != r.end() && days->is_array()) {
        for (std::size_t i = 0; i < days->size(); ++i) {
            const Json& d = (*days)[i];
            const auto it = d.is_object() ? d.find("seed") : d.end();
            if (!d.is_object() || it == d.end()) {
                b.day_seeds.emplace_back(std::nullopt);
                continue;
            }
            if (!it->is_number_unsigned()) {
                throw FormatError("bundle: days[" + std::to_string(i) + "].seed must be a non-negative 64-bit integer");
            }
            b.day_seeds.emplace_back(it->get<std::uint64_t>());
        }
    }

    const Json& packs = require(r, "packs", "bundle");
    if (!packs.is_array()) {
        throw FormatError("bundle: packs is not a list");
    }
    for (std::size_t i = 0; i < packs.size(); ++i) {
        const std::string where = "packs[" + std::to_string(i) + "]";
        b.packs.push_back(PackRef{require_string(packs[i], "sha256", where), require_string(packs[i], "path", where),
                                  require_string(packs[i], "price_basis", where)});
    }

    const Json& s = require(r, "strategy", "bundle");
    b.strategy.kind = require_string(s, "kind", "strategy");
    // A scripted strategy (goldens) has actions instead of a ref; Python supplies the object either way.
    if (const auto it = s.find("ref"); it != s.end() && it->is_string()) {
        b.strategy.ref = it->get<std::string>();
    }
    if (const auto it = s.find("source_sha256"); it != s.end() && it->is_string()) {
        b.strategy.source_sha256 = it->get<std::string>();
    }
    b.strategy.params = s.contains("params") ? s["params"] : Json::object();

    const Json& labels = require(r, "labels", "bundle");
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
