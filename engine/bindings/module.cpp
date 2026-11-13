// stockify._engine: pybind11 binding of stocksim_core.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstring>
#include <string>
#include <vector>

#include "stocksim/build_info.hpp"
#include "stocksim/costs.hpp"
#include "stocksim/decimal.hpp"
#include "stocksim/format.hpp"
#include "stocksim/indicators.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/pcg32.hpp"
#include "stocksim/pricing/black76.hpp"
#include "stocksim/sha256.hpp"

namespace py = pybind11;

void bind_testing_packs(py::module_& t);  // testing_packs.cpp
void bind_engine(py::module_& m);         // engine.cpp

namespace {

// Python value → JValue, with the oracle's rules (bool before int, str keys only, int64 range).
stocksim::JValue to_jvalue(const py::handle& obj) {
    if (obj.is_none()) {
        return stocksim::JValue();
    }
    if (py::isinstance<py::bool_>(obj)) {
        return stocksim::JValue(obj.cast<bool>());
    }
    if (py::isinstance<py::int_>(obj)) {
        return stocksim::JValue(obj.cast<std::int64_t>());  // OverflowError outside int64
    }
    if (py::isinstance<py::float_>(obj)) {
        return stocksim::JValue(obj.cast<double>());
    }
    if (py::isinstance<py::str>(obj)) {
        return stocksim::JValue(obj.cast<std::string>());
    }
    if (py::isinstance<py::dict>(obj)) {
        stocksim::JValue out = stocksim::JValue::object();
        for (const auto& [k, v] : obj.cast<py::dict>()) {
            if (!py::isinstance<py::str>(k)) {
                throw py::type_error("canonical JSON keys must be strings");
            }
            out[k.cast<std::string>()] = to_jvalue(v);
        }
        return out;
    }
    if (py::isinstance<py::list>(obj) || py::isinstance<py::tuple>(obj)) {
        stocksim::JValue out = stocksim::JValue::array();
        for (const auto& v : obj) {
            out.push_back(to_jvalue(v));
        }
        return out;
    }
    throw py::type_error("not JSON-serialisable: " + std::string(py::str(py::type::handle_of(obj).attr("__name__"))));
}


// Python int <-> Int128 for the cross-checks (pybind11 converts only up to 64 bits).
stocksim::Int128 to_i128(const py::int_& x) {
    const py::object mask = py::int_((py::int_(1).attr("__lshift__")(64))).attr("__sub__")(1);
    const std::int64_t hi = x.attr("__rshift__")(64).cast<std::int64_t>();
    const std::uint64_t lo = x.attr("__and__")(mask).cast<std::uint64_t>();
    return stocksim::Int128::from_parts(hi, lo);
}

py::int_ from_i128(const stocksim::Int128& v) { return py::int_(py::str(v.to_string())); }

void bind_testing(py::module_& t) {
    t.doc() = "Engine internals exposed for cross-checks against the Python oracle (tests only).";
    t.def("floor_div", &stocksim::floor_div);
    t.def("ceil_div", &stocksim::ceil_div);
    t.def("round_half_up_div", &stocksim::round_half_up_div);
    t.def("floor_to", &stocksim::floor_to);
    t.def("ceil_to", &stocksim::ceil_to);
    t.def("mul_div_floor", &stocksim::mul_div_floor);
    t.def("mul_div_round_half_up", &stocksim::mul_div_round_half_up);
    t.def("to_ppb", [](const std::string& s) { return stocksim::to_ppb(s); });
    t.def("to_ppm", [](const std::string& s) { return stocksim::to_ppm(s); });
    t.def("to_bp", [](const std::string& s) { return stocksim::to_bp(s); });
    t.def("to_paise", [](const std::string& s) { return stocksim::to_paise(s); });
    t.def("format_float", &stocksim::format_float);
    t.def("sma", &stocksim::sma);
    t.def("ema", &stocksim::ema);
    t.def("rsi", &stocksim::rsi);
    t.def("atr", &stocksim::atr);  // [(high, low, close), …]
    // Black-76 (M4.3), cross-checked against stockify.fno.black76.
    t.def("b76_price", &stocksim::pricing::price, py::arg("is_call"), py::arg("f"), py::arg("k"), py::arg("t"),
          py::arg("r"), py::arg("sigma"));
    t.def("b76_greeks", [](bool is_call, double f, double k, double t, double r, double sigma) {
        const stocksim::pricing::Greeks g = stocksim::pricing::greeks(is_call, f, k, t, r, sigma);
        return py::make_tuple(g.price, g.delta, g.gamma, g.vega, g.theta, g.rho);
    });
    t.def("b76_implied_vol", [](bool is_call, double target, double f, double k, double t, double r, double tick) {
        const stocksim::pricing::ImpliedVol iv = stocksim::pricing::implied_vol(is_call, target, f, k, t, r, tick);
        return py::make_tuple(iv.sigma, iv.reason ? py::object(py::str(std::string(stocksim::pricing::to_string(*iv.reason))))
                                                  : py::object(py::none()));
    }, py::arg("is_call"), py::arg("target"), py::arg("f"), py::arg("k"), py::arg("t"), py::arg("r"),
       py::arg("tick") = stocksim::pricing::kDefaultTick);
    t.def("b76_year_fraction", &stocksim::pricing::year_fraction);
    // Many doubles at once (little-endian float64 bytes) → the formatted strings joined by "\n".
    t.def("format_floats", [](const py::bytes& raw) {
        const std::string buf = raw;
        if (buf.size() % sizeof(double) != 0) {
            throw py::value_error("format_floats needs a whole number of float64 values");
        }
        std::string out;
        out.reserve(buf.size() * 3);
        for (std::size_t i = 0; i < buf.size(); i += sizeof(double)) {
            double x = 0.0;
            std::memcpy(&x, buf.data() + i, sizeof(double));
            if (i != 0) {
                out.push_back('\n');
            }
            out += stocksim::format_float(x);
        }
        return out;
    });
    t.def("dumps_canonical", [](const py::handle& obj) { return stocksim::dumps_canonical(to_jvalue(obj)); });
    t.def("csv_cell", [](const py::handle& v) {
        if (v.is_none()) {
            return stocksim::csv_cell(stocksim::CsvNull{});
        }
        if (py::isinstance<py::bool_>(v)) {
            return stocksim::csv_cell(v.cast<bool>());
        }
        if (py::isinstance<py::int_>(v)) {
            return stocksim::csv_cell(v.cast<std::int64_t>());
        }
        if (py::isinstance<py::str>(v)) {
            const std::string s = v.cast<std::string>();
            return stocksim::csv_cell(std::string_view(s));
        }
        throw py::type_error("CSV cells are None, bool, int or str");
    });
    t.def("sha256_hex", [](const py::bytes& data) { return stocksim::sha256_hex(std::string_view(std::string(data))); });
    t.def("parse_bundle_envelope", [](const std::string& text) {
        const stocksim::Bundle b = stocksim::parse_bundle(text);
        py::dict d;
        d["sha256"] = b.sha256;
        d["seed"] = b.seed;
        py::list packs;
        for (const auto& p : b.packs) {
            packs.append(py::make_tuple(p.sha256, p.path, p.price_basis));
        }
        d["packs"] = packs;
        d["strategy"] = py::make_tuple(b.strategy.kind, b.strategy.ref, b.strategy.source_sha256);
        d["labels"] = b.labels;
        return d;
    });

    t.def("int128_mul", [](std::int64_t a, std::int64_t b) { return from_i128(stocksim::Int128::mul(a, b)); });
    t.def("int128_add", [](const py::int_& a, const py::int_& b) { return from_i128(to_i128(a) + to_i128(b)); });
    t.def("int128_sub", [](const py::int_& a, const py::int_& b) { return from_i128(to_i128(a) - to_i128(b)); });
    t.def("int128_mul64", [](const py::int_& a, std::int64_t b) { return from_i128(to_i128(a) * b); });
    t.def("int128_floor_div", [](const py::int_& a, std::int64_t d) { return from_i128(to_i128(a).floor_div(d)); });
    t.def("int128_ceil_div", [](const py::int_& a, std::int64_t d) { return from_i128(to_i128(a).ceil_div(d)); });
    t.def("int128_to_double", [](const py::int_& a) { return to_i128(a).to_double(); });
    t.def("int128_cmp", [](const py::int_& a, const py::int_& b) {
        const auto c = to_i128(a) <=> to_i128(b);
        return c < 0 ? -1 : (c > 0 ? 1 : 0);
    });

    // The C++ cost engine on a bundle's charges object (rates already in ppb) and a run's costs config.
    t.def("costs_eval", [](const std::string& charges_json, const std::string& cfg_json, const std::string& what,
                           const std::string& kind, const std::string& product, const std::string& side,
                           std::int64_t value, const std::string& date) -> py::object {
        const stocksim::Costs c(stocksim::Json::parse(charges_json), stocksim::Json::parse(cfg_json));
        std::optional<stocksim::ChargeParts> p;
        if (what == "order") {
            p = c.order(kind, stocksim::parse_product(product).value(), stocksim::parse_side(side).value(), value, date);
        } else if (what == "dp") {
            p = c.dp(date);
        } else if (what == "sqoff_fee") {
            p = c.sqoff_fee(date);
        } else if (what == "exercise") {
            p = c.exercise(kind, value, date);
        } else {
            throw py::value_error("what: order | dp | sqoff_fee | exercise");
        }
        if (!p) {
            return py::none();
        }
        py::dict d;
        d["turnover"] = p->turnover;
        d["brokerage"] = p->brokerage;
        d["stt"] = p->stt;
        d["exchange"] = p->exchange;
        d["sebi"] = p->sebi;
        d["stamp"] = p->stamp;
        d["gst"] = p->gst;
        d["dp"] = p->dp;
        d["other"] = p->other;
        d["total"] = p->row_total();
        return d;
    });

    py::class_<stocksim::Pcg32>(t, "Pcg32")
        .def(py::init<std::uint64_t, std::uint64_t>(), py::arg("initstate"), py::arg("initseq"))
        .def("next_u32", &stocksim::Pcg32::next_u32)
        .def("below", &stocksim::Pcg32::below)
        .def("next_u64", &stocksim::Pcg32::next_u64)
        .def("random", &stocksim::Pcg32::random)
        .def_property_readonly("state", &stocksim::Pcg32::state)
        .def_property_readonly("inc", &stocksim::Pcg32::inc);
}

}  // namespace

PYBIND11_MODULE(_engine, m) {
    m.doc() = "STOCKIFY C++ engine (stocksim_core)";
    py::register_exception<stocksim::FormatError>(m, "FormatError", PyExc_ValueError);
    py::register_exception<stocksim::NumericError>(m, "NumericError", PyExc_ValueError);
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

    bind_engine(m);

    py::module_ testing = m.def_submodule("testing");
    bind_testing(testing);
    bind_testing_packs(testing);
}
