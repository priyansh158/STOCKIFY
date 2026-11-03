// Engine build identity (invariant I-8): embedded in every run manifest.
#pragma once

#include <string>

namespace stocksim {

struct BuildInfo {
    std::string engine_build_id;  // first 12 hex of SHA-256 over the fields below
    std::string git_sha;
    bool git_dirty;
    std::string compiler_id;
    std::string compiler_version;
    std::string build_type;
    std::string cxx_standard;
    std::string flags_hash;
    std::string python_version;  // Python the pybind module was built against
};

const BuildInfo& build_info();

// One-line human-readable summary, used by `stocksim_run --version`.
std::string build_info_text();

}  // namespace stocksim
