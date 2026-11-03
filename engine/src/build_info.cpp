#include "stocksim/build_info.hpp"

#include "stocksim/build_info_data.hpp"

namespace stocksim {

const BuildInfo& build_info() {
    static const BuildInfo info{
        build_data::engine_build_id,
        build_data::git_sha,
        build_data::git_dirty,
        build_data::compiler_id,
        build_data::compiler_version,
        build_data::build_type,
        build_data::cxx_standard,
        build_data::flags_hash,
        build_data::python_version,
    };
    return info;
}

std::string build_info_text() {
    const BuildInfo& b = build_info();
    return "stocksim " + b.engine_build_id + " (git " + b.git_sha + (b.git_dirty ? "-dirty" : "") +
           ", " + b.compiler_id + " " + b.compiler_version + ", " + b.build_type + ", C++" +
           b.cxx_standard + ", python " + b.python_version + ")";
}

}  // namespace stocksim
