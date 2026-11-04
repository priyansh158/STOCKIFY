// stockify._engine: pybind11 binding of stocksim_core.
#include <pybind11/pybind11.h>

#include "stocksim/build_info.hpp"

namespace py = pybind11;

PYBIND11_MODULE(_engine, m) {
    m.doc() = "STOCKIFY C++ engine (stocksim_core)";
    m.def("build_info", [] {
        const stocksim::BuildInfo& b = stocksim::build_info();
        py::dict d;
        d["engine_build_id"] = b.engine_build_id;
        d["git_sha"] = b.git_sha;
        d["git_dirty"] = b.git_dirty;
        d["compiler_id"] = b.compiler_id;
        d["compiler_version"] = b.compiler_version;
        d["build_type"] = b.build_type;
        d["cxx_standard"] = b.cxx_standard;
        d["flags_hash"] = b.flags_hash;
        d["python_version"] = b.python_version;
        return d;
    }, "Engine build identity (invariant I-8).");
}
