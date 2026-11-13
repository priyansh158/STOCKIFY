// stocksim_run: run a bundle with a C++-registered strategy, without Python (M3.11; benchmarks and CI).
//
//   stocksim_run --version
//   stocksim_run --bundle <run>/bundle.json --out <dir> [--lab <dir>]
//
// Pack paths in the bundle are relative to the lab folder. The default lab is the bundle folder's
// grandparent, because a run folder is $LAB/runs/<run_id>/. Writes the deterministic artifacts and
// determinism.json exactly as stockify.engine.run_bundle does, plus a small manifest.json (the only place a
// clock is read, and only for duration_ms). No Python, no SQLite, no network.
//
// Each pack must hash to the SHA-256 the bundle names (as Python's resolve_packs checks).
//
// Exit codes: 0 done, 1 the run failed (events_log.txt ends with the reason), 2 usage or a non-C++ strategy.
#include <chrono>
#include <cstdio>
#include <cstring>
#include <exception>
#include <filesystem>
#include <memory>
#include <optional>
#include <span>
#include <string>

#include "nlohmann/json.hpp"
#include "stocksim/artifacts.hpp"
#include "stocksim/build_info.hpp"
#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/format.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/pack_reader.hpp"
#include "stocksim/sha256.hpp"
#include "stocksim/strategy.hpp"

namespace fs = std::filesystem;

namespace {

int usage() {
    std::fprintf(stderr,
                 "usage: stocksim_run --version\n"
                 "       stocksim_run --bundle <run>/bundle.json --out <dir> [--lab <dir>]\n"
                 "(C++-registered strategies only; pack paths are relative to --lab, by default the bundle\n"
                 " folder's grandparent: $LAB/runs/<run_id>/bundle.json)\n");
    return 2;
}

void write_manifest(const stocksim::Engine& e, const stocksim::Bundle& b, const fs::path& out, std::int64_t ms) {
    nlohmann::json m;  // std::map keys: written sorted, as Python's json.dumps(sort_keys=True)
    m["engine"] = "stocksim_run";
    m["engine_build_id"] = stocksim::build_info().engine_build_id;
    m["bundle_sha256"] = b.sha256;
    m["labels"] = e.labels();
    nlohmann::json packs = nlohmann::json::array();
    for (const auto& p : b.packs) {
        packs.push_back(p.sha256);
    }
    m["packs"] = packs;
    m["duration_ms"] = ms;
    m["run_id"] = out.filename().string();
    stocksim::write_file_bytes(out / "manifest.json", m.dump(2) + "\n");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
        std::printf("%s\n", stocksim::build_info_text().c_str());
        return 0;
    }
    std::optional<fs::path> bundle_path, out, lab;
    for (int i = 1; i < argc; i += 2) {
        if (i + 1 >= argc) {
            return usage();
        }
        const std::string flag = argv[i];
        if (flag == "--bundle") {
            bundle_path = fs::path(argv[i + 1]);
        } else if (flag == "--out") {
            out = fs::path(argv[i + 1]);
        } else if (flag == "--lab") {
            lab = fs::path(argv[i + 1]);
        } else {
            return usage();
        }
    }
    if (!bundle_path || !out) {
        return usage();
    }

    stocksim::Bundle bundle;
    try {
        bundle = stocksim::load_bundle(*bundle_path);
    } catch (const std::exception& ex) {
        std::fprintf(stderr, "stocksim_run: %s\n", ex.what());
        return 2;
    }
    if (bundle.strategy.kind != "cpp") {
        std::fprintf(stderr,
                     "stocksim_run: strategy.kind is '%s'; only C++-registered strategies run here "
                     "(use python -m stockify run for Python strategies)\n",
                     bundle.strategy.kind.c_str());
        return 2;
    }
    const fs::path lab_dir = lab ? *lab : fs::absolute(*bundle_path).parent_path().parent_path().parent_path();

    const auto started = std::chrono::steady_clock::now();
    std::unique_ptr<stocksim::Engine> engine;
    // Every failure from here on, the run or writing its results (a locked file, a bad metric), exits 1 with
    // events_log.txt written; nothing reaches std::terminate (M3 review).
    const auto fail = [&](const std::exception& ex) {
        std::string log;
        if (engine) {
            engine->context().log("ERROR", std::string("the run stops: ") + ex.what());
            for (const std::string& line : engine->log_lines()) {
                log += line + "\n";
            }
        } else {
            log = std::string("0 ERROR the run stops: ") + ex.what() + "\n";
        }
        try {
            stocksim::write_file_bytes(*out / "events_log.txt", log);
        } catch (const std::exception&) {  // nowhere left to write it: stderr below still says why
        }
        std::fprintf(stderr, "stocksim_run: the run stops: %s\n", ex.what());
        return 1;
    };
    try {
        fs::create_directories(*out);
        engine = std::make_unique<stocksim::Engine>(bundle);
        for (const auto& p : bundle.packs) {
            auto pack = std::make_shared<stocksim::PackReader>(lab_dir / fs::path(p.path));
            // The pack must be the one the bundle names, as Python's resolve_packs checks (M3 review).
            const std::string actual =
                stocksim::sha256_hex(std::span<const std::byte>(pack->data(), pack->length()));
            if (actual != p.sha256) {
                throw stocksim::RunError("pack " + p.path + ": hash mismatch: the file hashes to " + actual +
                                         ", the bundle names " + p.sha256);
            }
            engine->add_pack(std::move(pack));
        }
        engine->set_strategy(stocksim::make_strategy(bundle.strategy.ref));
        engine->run();
        const auto ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started);
        stocksim::write_artifacts(*engine, *out);
        write_manifest(*engine, bundle, *out, ms.count());
        std::fprintf(stderr, "stocksim_run: %zu orders, %zu fills in %lld ms -> %s\n", engine->book().orders.size(),
                     engine->book().fills.size(), static_cast<long long>(ms.count()), out->string().c_str());
        return 0;
    } catch (const std::exception& ex) {
        return fail(ex);
    }
}
