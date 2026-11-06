// stockify._engine.Engine and the Python strategy trampoline (A2 §3, M3.10).
//
// A Python strategy written for the oracle runs unchanged: every hook receives objects of the same classes
// (stockify.strategy.Bar, Tick, PhaseChange, …) and the context's methods return what
// stockify.oracle.context.Context returns.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <memory>
#include <string>
#include <vector>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/pack_reader.hpp"

namespace py = pybind11;

namespace {

using stocksim::Engine;

// Python classes the oracle hands to strategies, imported once per strategy.
struct OracleTypes {
    py::object bar, tick, phase_change, date, json_loads;
    static OracleTypes load() {
        py::module_ s = py::module_::import("stockify.strategy");
        return OracleTypes{s.attr("Bar"), s.attr("Tick"), s.attr("PhaseChange"), py::module_::import("datetime").attr("date"),
                       py::module_::import("json").attr("loads")};
    }
};

py::object opt(const std::optional<std::int64_t>& v) { return v ? py::object(py::int_(*v)) : py::object(py::none()); }

py::object to_python(const stocksim::Json& j, const OracleTypes& t) { return t.json_loads(j.dump()); }

// The Python-visible ctx. It refers to the engine weakly: the engine owns the strategy, and a strong reference
// back would be a cycle through C++ that Python's collector cannot break (the engine, and its mapped packs,
// would never be freed).
struct CtxProxy {
    std::weak_ptr<Engine> engine;
    OracleTypes types;
    py::object params;

    stocksim::Context& c() const {
        const std::shared_ptr<Engine> e = engine.lock();
        if (!e) {
            throw py::value_error("this ctx belongs to an engine that no longer exists");
        }
        return e->context();
    }

    py::object bar(const stocksim::Bar& b) const {
        return types.bar(b.instrument, b.interval_s, b.start_ns, b.t_ns, b.open, b.high, b.low, b.close, b.volume, b.oi,
                         b.built);
    }
    py::object tick(const stocksim::Tick& t) const {
        return types.tick(t.instrument, t.t_ns, t.ltp, t.volume, t.atp, opt(t.bid), opt(t.ask), t.stale);
    }
};

struct RngProxy {
    std::shared_ptr<CtxProxy> ctx;
};

class StrategyTrampoline : public stocksim::Strategy {
 public:
    StrategyTrampoline(py::object obj, std::shared_ptr<CtxProxy> ctx) : obj_(std::move(obj)), ctx_(std::move(ctx)) {
        ctx_obj_ = py::cast(ctx_);
    }
    void on_start(stocksim::Context&) override { obj_.attr("on_start")(ctx_obj_); }
    void on_bar(stocksim::Context&, const stocksim::Bar& b) override { obj_.attr("on_bar")(ctx_obj_, ctx_->bar(b)); }
    void on_tick(stocksim::Context&, const stocksim::Tick& t) override {
        obj_.attr("on_tick")(ctx_obj_, ctx_->tick(t));
    }
    void on_timer(stocksim::Context&, std::uint64_t id) override { obj_.attr("on_timer")(ctx_obj_, id); }
    void on_session_phase(stocksim::Context&, const stocksim::PhaseChange& p) override {
        obj_.attr("on_session_phase")(ctx_obj_, ctx_->types.phase_change(p.group, p.phase, p.t_ns));
    }
    void on_session_end(stocksim::Context&) override { obj_.attr("on_session_end")(ctx_obj_); }

 private:
    py::object obj_;
    std::shared_ptr<CtxProxy> ctx_;
    py::object ctx_obj_;
};

}  // namespace

void bind_engine(py::module_& m) {
    py::class_<RngProxy>(m, "StrategyRng")
        .def("random", [](RngProxy& r) { return r.ctx->c().rng().random(); })
        .def("below", [](RngProxy& r, std::uint32_t n) { return r.ctx->c().rng().below(n); });

    py::class_<CtxProxy, std::shared_ptr<CtxProxy>>(m, "Context")
        .def("now", [](const CtxProxy& x) { return x.c().now(); })
        .def("today", [](const CtxProxy& x) {
            const stocksim::Date d = x.c().today();
            return x.types.date(d / 10000, d / 100 % 100, d % 100);
        })
        .def("phase", [](const CtxProxy& x, stocksim::InstrumentId iid) { return x.c().phase(iid); })
        .def("subscribe_ticks", [](CtxProxy& x, stocksim::InstrumentId iid) { x.c().subscribe_ticks(iid); })
        .def("subscribe_bars", [](CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec) {
            x.c().subscribe_bars(iid, isec);
        })
        .def("last_tick", [](const CtxProxy& x, stocksim::InstrumentId iid) -> py::object {
            const auto t = x.c().last_tick(iid);
            return t ? x.tick(*t) : py::object(py::none());
        })
        .def("depth", [](const CtxProxy& x, stocksim::InstrumentId iid) -> py::object {
            const auto* book = x.c().depth(iid);
            if (book == nullptr) {
                return py::none();
            }
            py::list out;
            for (const auto& lv : *book) {
                out.append(py::make_tuple(lv.price, lv.qty, lv.orders));
            }
            return out;
        })
        .def("stale", [](const CtxProxy& x, stocksim::InstrumentId iid) { return x.c().stale(iid); })
        .def("bars", [](const CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec, std::int64_t n) {
            py::list out;
            for (const auto& b : x.c().bars(iid, isec, n)) {
                out.append(x.bar(b));
            }
            return out;
        })
        .def("schedule_at", [](CtxProxy& x, stocksim::TimeNs t) { return x.c().schedule_at(t); })
        .def("schedule_every", [](CtxProxy& x, stocksim::TimeNs period, stocksim::TimeNs first) {
            return x.c().schedule_every(period, first);
        })
        .def("cancel_timer", [](CtxProxy& x, std::uint64_t id) { x.c().cancel_timer(id); })
        .def("log", [](CtxProxy& x, const std::string& level, const py::object& msg) {
            x.c().log(level, std::string(py::str(msg)));
        })
        .def("rng", [](const std::shared_ptr<CtxProxy>& x) { return RngProxy{x}; })
        .def_property_readonly("params", [](const CtxProxy& x) { return x.params; })
        .def("resolve", [](const CtxProxy& x, const std::string& symbol) {
            const auto id = x.c().resolve(symbol);
            if (!id) {
                throw py::key_error(symbol + " is not in this run's bundle");
            }
            return *id;
        })
        .def("spec", [](const CtxProxy& x, stocksim::InstrumentId iid) -> py::object {
            const stocksim::Spec* s = x.c().spec(iid);
            return s ? to_python(s->row, x.types) : py::object(py::none());
        })
        .def_property_readonly_static("rupees", [](const py::object&) {
            return py::module_::import("stockify.strategy").attr("rupees");
        })
        .def_property_readonly_static("paise", [](const py::object&) {
            return py::module_::import("stockify.numerics").attr("to_paise");
        });

    py::class_<Engine, std::shared_ptr<Engine>>(m, "Engine")
        .def(py::init([](const std::string& bundle_path) {
                 return std::make_shared<Engine>(stocksim::load_bundle(bundle_path));
             }),
             py::arg("bundle_path"))
        .def("add_pack", [](Engine& e, const std::string& path) {
            e.add_pack(std::make_shared<stocksim::PackReader>(path));
        })
        .def("set_strategy_py", [](const std::shared_ptr<Engine>& e, py::object strategy) {
            auto ctx = std::make_shared<CtxProxy>();
            ctx->engine = e;
            ctx->types = OracleTypes::load();
            ctx->params = to_python(e->bundle().strategy.params, ctx->types);
            e->set_strategy(std::make_shared<StrategyTrampoline>(std::move(strategy), ctx));
        })
        .def("run", [](Engine& e) { e.run(); })
        .def("push", [](Engine& e, const py::bytes& batch) {
            const std::string data = batch;
            e.push(std::as_bytes(std::span<const char>(data.data(), data.size())));
        })
        .def("finish", &Engine::finish)
        .def("drain_outputs", [](Engine& e) {
            py::dict out;
            out["orders"] = py::list();
            out["order_events"] = py::list();
            out["fills"] = py::list();
            out["charges"] = py::list();
            out["log"] = e.drain_log();
            return out;
        })
        .def("events_log", [](const Engine& e) { return e.log_lines(); })
        .def("labels", &Engine::labels);

    py::register_exception<stocksim::RunError>(m, "RunError", PyExc_RuntimeError);
}
