// stockify._engine.Engine and the Python strategy trampoline (A2 §3, M3.10).
//
// A Python strategy written for the oracle runs unchanged: every hook receives objects of the same classes
// (stockify.strategy.Bar, Tick, PhaseChange, …) and the context's methods return what
// stockify.oracle.context.Context returns.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>
#include <type_traits>
#include <variant>
#include <vector>

#include "stocksim/artifacts.hpp"
#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/pack_reader.hpp"
#include "stocksim/sod_state.hpp"

namespace py = pybind11;

namespace {

using stocksim::Engine;

// Python classes the oracle hands to strategies, imported once per strategy.
struct OracleTypes {
    py::object bar, tick, phase_change, position_view, funds, order_view, fill_view, date, json_loads, chain_view;
    static OracleTypes load() {
        py::module_ s = py::module_::import("stockify.strategy");
        py::module_ o = py::module_::import("stockify.oracle.orders");
        return OracleTypes{s.attr("Bar"),          s.attr("Tick"),          s.attr("PhaseChange"),
                           s.attr("PositionView"), s.attr("Funds"),         o.attr("OrderView"),
                           o.attr("FillView"),     py::module_::import("datetime").attr("date"),
                           py::module_::import("json").attr("loads"), s.attr("ChainView")};
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
    py::object order(const stocksim::OrderView& v) const {
        return types.order_view(v.order_id, v.instrument, v.side, v.qty, v.product, v.type, v.price, v.trigger, v.status,
                                v.reason, v.filled_qty, v.avg_price, v.tag);
    }
    py::object fill(const stocksim::FillView& f) const {
        return types.fill_view(f.order_id, f.instrument, f.side, f.qty, f.price, f.t_ns, f.evidence);
    }
    py::object position(const stocksim::PositionView& p) const {
        return types.position_view(p.instrument, p.product, p.qty, p.avg_price, p.realised);
    }
    py::object rows;  // ctx._rows(), parsed once
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
    void on_order_update(stocksim::Context&, const stocksim::OrderView& v) override {
        obj_.attr("on_order_update")(ctx_obj_, ctx_->order(v));
    }
    void on_fill(stocksim::Context&, const stocksim::FillView& f) override {
        obj_.attr("on_fill")(ctx_obj_, ctx_->fill(f));
    }

    // The day-to-day state (A1 §6.4, A2 §4.1): save_state() checked by the oracle's own sod_state.strategy_state
    // (a JSON object of JSON-typed values that canonical JSON gives back exactly) and handed over as canonical
    // JSON; load_state() gets the saved object back. Both fail as the oracle fails: a RunError naming the strategy.
    std::string save_state() const override {
        try {
            const py::module_ sod = py::module_::import("stockify.oracle.sod_state");
            const py::object state = sod.attr("strategy_state")(obj_);
            return py::str(py::module_::import("stockify.numerics").attr("dumps_canonical")(state));
        } catch (py::error_already_set& err) {
            if (err.matches(py::module_::import("stockify.oracle.sod_state").attr("StateError"))) {
                throw stocksim::RunError(std::string(py::str(err.value())));
            }
            throw;
        }
    }
    void load_state(std::string_view text) override {
        const py::object state = py::module_::import("json").attr("loads")(py::str(text.data(), text.size()));
        try {
            obj_.attr("load_state")(state);
        } catch (py::error_already_set& err) {
            const std::string owner = py::str(py::type::handle_of(obj_).attr("__name__"));
            const std::string kind = py::str(err.type().attr("__name__"));
            throw stocksim::RunError("strategy " + owner + ".load_state() raised " + kind + ": " +
                                     std::string(py::str(err.value())));
        }
    }

 private:
    py::object obj_;
    std::shared_ptr<CtxProxy> ctx_;
    py::object ctx_obj_;
};

// A CSV cell as the Python value its text spells: None, bool, int or str.
py::object cell_object(const stocksim::CsvCell& c) {
    return std::visit(
        [](const auto& v) -> py::object {
            using T = std::decay_t<decltype(v)>;
            if constexpr (std::is_same_v<T, stocksim::CsvNull>) {
                return py::none();
            } else if constexpr (std::is_same_v<T, bool>) {
                return py::bool_(v);
            } else if constexpr (std::is_same_v<T, std::int64_t>) {
                return py::int_(v);
            } else {
                return py::str(v.data(), v.size());
            }
        },
        c);
}

// A row sink (artifacts.hpp) that collects dicts keyed by the CSV header's column names.
class DictRows {
 public:
    explicit DictRows(std::string_view header) {
        for (std::size_t a = 0; a <= header.size();) {
            const std::size_t z = std::min(header.find(',', a), header.size());
            names_.emplace_back(std::string(header.substr(a, z - a)));
            a = z + 1;
        }
    }
    void operator()(std::initializer_list<stocksim::CsvCell> cells) {
        py::dict d;
        std::size_t i = 0;
        for (const stocksim::CsvCell& c : cells) {
            d[names_.at(i++)] = cell_object(c);
        }
        rows.append(d);
    }
    py::list rows;

 private:
    std::vector<py::str> names_;
};

// A Python exception from a strategy hook ends the run (A2 §3, M3.10): its traceback goes into the log (so into
// events_log.txt) and the exception reaches the caller unchanged. An engine error is logged the same way.
// Nothing is swallowed.
//
// A C++ strategy never touches Python, so its runs release the GIL (A2 §3) and daemon threads keep moving;
// the GIL is back before any handler below runs (the release is undone as the stack unwinds).
template <class F>
void guarded(Engine& e, F&& step) {
    try {
        if (dynamic_cast<const StrategyTrampoline*>(e.strategy()) == nullptr) {
            py::gil_scoped_release nogil;
            step();
        } else {
            step();
        }
    } catch (py::error_already_set& err) {
        e.context().log("ERROR", "the strategy raised an exception; the run stops");
        const py::list parts = py::module_::import("traceback").attr("format_exception")(err.type(), err.value(),
                                                                                         err.trace());
        for (const py::handle part : parts) {
            for (const py::handle line : py::str(part).attr("splitlines")()) {
                e.context().log("ERROR", std::string(py::str(line)));
            }
        }
        throw;
    } catch (const std::exception& ex) {
        e.context().log("ERROR", std::string("the run stops: ") + ex.what());
        throw;
    }
}

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
        .def("sma", [](const CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec, std::int64_t n) {
            return x.c().sma(iid, isec, n);
        })
        .def("ema", [](const CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec, std::int64_t n) {
            return x.c().ema(iid, isec, n);
        })
        .def("rsi", [](const CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec, std::int64_t n) {
            return x.c().rsi(iid, isec, n);
        })
        .def("atr", [](const CtxProxy& x, stocksim::InstrumentId iid, std::int32_t isec, std::int64_t n) {
            return x.c().atr(iid, isec, n);
        })
        .def("session_vwap", [](const CtxProxy& x, stocksim::InstrumentId iid) { return x.c().session_vwap(iid); })
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
        .def("place",
             [](CtxProxy& x, const py::object& instrument, const py::object& side, const py::object& qty,
                const py::object& product, const py::object& type, const py::object& price, const py::object& trigger,
                const py::object& validity, const py::object& protection, const py::object& tag) {
                 // The oracle's Context.place: int() of instrument, qty, price and trigger; the rest as given.
                 stocksim::PlaceRequest r;
                 r.instrument = py::int_(instrument).cast<stocksim::InstrumentId>();
                 r.side = py::str(side);
                 r.qty = py::int_(qty).cast<stocksim::Qty>();
                 r.product = py::str(product);
                 r.type = py::str(type);
                 r.price = py::int_(price).cast<stocksim::Paise>();
                 r.trigger = py::int_(trigger).cast<stocksim::Paise>();
                 r.validity = py::str(validity);
                 if (py::isinstance<py::int_>(protection)) {  // an int (bool included, as isinstance(…, int))
                     r.protection = protection.cast<stocksim::Bp>();
                 }
                 r.tag = py::str(tag);
                 return x.c().place(r);
             },
             py::arg("instrument"), py::arg("side"), py::arg("qty"), py::arg("product"), py::arg("type"),
             py::arg("price") = 0, py::arg("trigger") = 0, py::arg("validity") = "DAY", py::arg("protection") = "AUTO",
             py::arg("tag") = "")
        .def("modify",
             [](CtxProxy& x, std::uint64_t order_id, const py::kwargs& changes) {
                 stocksim::ModifyRequest m;
                 std::vector<std::pair<std::string, std::string>> parts;
                 for (const auto& [k, h] : changes) {
                     const py::object v = py::reinterpret_borrow<py::object>(h);
                     const std::string key = py::str(k);
                     parts.emplace_back(key, std::string(py::str(v)));
                     if (key == "qty") {
                         m.qty = py::int_(v).cast<stocksim::Qty>();
                     } else if (key == "price") {
                         m.price = py::int_(v).cast<stocksim::Paise>();
                     } else if (key == "trigger") {
                         m.trigger = py::int_(v).cast<stocksim::Paise>();
                     } else if (key == "type") {
                         m.type = std::string(py::str(v));
                     }
                 }
                 std::sort(parts.begin(), parts.end());
                 for (std::size_t i = 0; i < parts.size(); ++i) {
                     m.detail += (i ? "," : "") + parts[i].first + "=" + parts[i].second;
                 }
                 return x.c().modify(order_id, m);
             })
        .def("cancel", [](CtxProxy& x, std::uint64_t order_id) { return x.c().cancel(order_id); })
        .def("order", [](const CtxProxy& x, std::uint64_t order_id) -> py::object {
            const auto v = x.c().order(order_id);
            return v ? x.order(*v) : py::object(py::none());
        })
        .def("position", [](const CtxProxy& x, stocksim::InstrumentId iid, const py::object& product) -> py::object {
            const auto p = x.c().position(iid, std::string(py::str(product)));
            return p ? x.position(*p) : py::object(py::none());
        })
        .def("positions", [](const CtxProxy& x) {
            py::list out;
            for (const auto& p : x.c().positions()) {
                out.append(x.position(p));
            }
            return out;
        })
        .def("funds", [](const CtxProxy& x) {
            const stocksim::Funds f = x.c().funds();
            return x.types.funds(f.available, f.cash, f.blocked, f.reserved);
        })
        .def("expiries", [](const CtxProxy& x, const std::string& underlying, const std::string& kind) {
            py::list out;  // ISO strings, like the oracle's (the bundle's expiry fields)
            for (const stocksim::Date d : x.c().expiries(underlying, kind)) {
                out.append(stocksim::iso_date(d));
            }
            return out;
        })
        .def("futures_chain", [](const CtxProxy& x, const std::string& underlying) {
            return x.c().futures_chain(underlying);
        })
        .def("chain", [](const CtxProxy& x, const std::string& underlying, const std::string& expiry) {
            // stockify.strategy.ChainView with the oracle's row keys, in the oracle's order (M4.4).
            const auto opt_f = [](const std::optional<double>& v) { return v ? py::object(py::float_(*v)) : py::object(py::none()); };
            const auto opt_s = [](const std::optional<std::string>& v) { return v ? py::object(py::str(*v)) : py::object(py::none()); };
            const stocksim::Context::ChainView view = x.c().chain(underlying, expiry);
            py::list rows;
            for (const auto& r : view.rows) {
                py::dict d;
                d["instrument_id"] = r.instrument;
                d["kind"] = r.kind;
                d["strike_paise"] = r.strike;
                d["bid_paise"] = opt(r.bid);
                d["ask_paise"] = opt(r.ask);
                d["mid_paise"] = opt_f(r.mid);
                d["ltp_paise"] = opt(r.ltp);
                d["stale"] = py::bool_(r.stale);
                d["oi"] = opt(r.oi);
                d["iv_bid"] = opt_f(r.iv_bid);
                d["iv_ask"] = opt_f(r.iv_ask);
                d["iv_mid"] = opt_f(r.iv_mid);
                d["iv_reason"] = opt_s(r.iv_reason);
                d["delta"] = opt_f(r.delta);
                d["gamma"] = opt_f(r.gamma);
                d["vega"] = opt_f(r.vega);
                d["theta"] = opt_f(r.theta);
                rows.append(d);
            }
            return x.types.chain_view(rows, opt_f(view.forward), opt_s(view.forward_source), opt(view.atm_strike));
        })
        .def("_rows", [](CtxProxy& x) {
            if (!x.rows) {
                x.rows = to_python(x.c().instrument_rows(), x.types);
            }
            return x.rows;
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
        .def("set_strategy_cpp", [](Engine& e, const std::string& name) { e.set_strategy(stocksim::make_strategy(name)); },
             "A registered C++ strategy (STOCKSIM_STRATEGY); its params come from the bundle.")
        .def("run", [](Engine& e) { guarded(e, [&] { e.run(); }); })
        .def("push", [](Engine& e, const py::bytes& batch) {
            const std::string data = batch;
            guarded(e, [&] { e.push(std::as_bytes(std::span<const char>(data.data(), data.size()))); });
        })
        .def("finish", [](Engine& e) { guarded(e, [&] { e.finish(); }); })
        .def("drain_outputs", [](Engine& e) {
            // Rows exactly as the CSVs spell them (artifacts.hpp), as dicts keyed by column name.
            e.ensure_idle("drain_outputs");
            const stocksim::Outputs o = e.drain_outputs();
            const stocksim::Book& b = e.book();
            DictRows orders(stocksim::kOrdersHeader), events(stocksim::kOrderEventsHeader),
                fills(stocksim::kFillsHeader), charges(stocksim::kChargesHeader);
            for (const stocksim::OrderId id : o.orders) {
                stocksim::order_row(b.orders.at(id), orders);
            }
            for (std::size_t i = o.events_from; i < o.events_to; ++i) {
                stocksim::order_event_row(b.order_events[i], events);
            }
            for (std::size_t i = o.fills_from; i < o.fills_to; ++i) {
                stocksim::fill_row(b.fills[i], fills);
            }
            for (std::size_t i = o.charges_from; i < o.charges_to; ++i) {  // row_no continues across days (VOL 5.2)
                stocksim::charge_row(e.charge_row_base() + static_cast<std::int64_t>(i) + 1, b.charges[i], charges);
            }
            py::dict out;
            out["orders"] = orders.rows;
            out["order_events"] = events.rows;
            out["fills"] = fills.rows;
            out["charges"] = charges.rows;
            out["log"] = o.log;
            return out;
        })
        .def("events_log", [](const Engine& e) {
            e.ensure_idle("events_log", true);  // readable after a failure: it holds the traceback
            return e.log_lines();
        })
        .def("write_artifacts", [](const Engine& e, const std::string& folder) {
            return stocksim::write_artifacts(e, std::filesystem::path(folder));
        })
        .def("labels", &Engine::labels)
        // A paper session's day-to-day state (A1 §6.4, VOL 5.3): the same bytes as the oracle's eod_state().
        .def("load_sod_state", [](Engine& e, const std::string& text) {
            e.ensure_idle("load_sod_state");
            stocksim::load_sod_state(e, text);
        }, "A start-of-day state (eod_state() of the previous day); before the first event only, at most once.")
        .def("eod_state", [](const Engine& e) {
            e.ensure_idle("eod_state");
            return stocksim::eod_state_json(e);
        }, "The state for the next day's sod_state, as canonical JSON.")
        .def("book", [](const Engine& e) {
            // Open positions as (instrument_id, product, signed units), in the order they were first opened; between
            // pushes only (the M5 daemon keeps them pinned, Task 15).
            e.ensure_idle("book");
            py::list out;
            for (const stocksim::Position& p : e.book().ledger.positions()) {
                if (p.q != 0) {
                    out.append(py::make_tuple(p.instrument, std::string(stocksim::to_string(p.product)), p.qty()));
                }
            }
            return out;
        });

    m.def("strategies", &stocksim::strategy_names, "The registered C++ strategies' names, sorted.");

    py::register_exception<stocksim::RunError>(m, "RunError", PyExc_RuntimeError);
}
