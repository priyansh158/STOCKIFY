// The strategy interface (A2 §2, §4): hooks the engine calls and the plain values it passes.
// Field for field these mirror stockify.strategy (Bar, Tick, PhaseChange, PositionView, Funds) and
// stockify.oracle.orders (OrderView, FillView), so a Python strategy sees the same objects on both engines.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "stocksim/json_bundle.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

class Context;

struct Bar {
    InstrumentId instrument = 0;
    std::int32_t interval_s = 0;
    TimeNs start_ns = 0;
    TimeNs t_ns = 0;
    Paise open = 0, high = 0, low = 0, close = 0;
    Qty volume = 0;
    std::int64_t oi = 0;
    bool built = false;
};

struct Tick {
    InstrumentId instrument = 0;
    TimeNs t_ns = 0;
    Paise ltp = 0;
    Qty volume = 0;
    Paise atp = 0;
    std::optional<Paise> bid;
    std::optional<Paise> ask;
    bool stale = false;
};

struct PhaseChange {
    std::string group;
    std::string phase;
    TimeNs t_ns = 0;
};

// Spellings as in orders.csv (side "BUY", type "SL-M", status "TRIGGER_PENDING", reason "R_TICK" or "").
struct OrderView {
    OrderId order_id = 0;
    InstrumentId instrument = 0;
    std::string side;
    Qty qty = 0;
    std::string product;
    std::string type;
    Paise price = 0;
    Paise trigger = 0;
    std::string status;
    std::string reason;
    Qty filled_qty = 0;
    Paise avg_price = 0;
    std::string tag;
};

struct FillView {
    OrderId order_id = 0;
    InstrumentId instrument = 0;
    std::string side;
    Qty qty = 0;
    Paise price = 0;
    TimeNs t_ns = 0;
    std::string evidence;
};

struct PositionView {
    InstrumentId instrument = 0;
    std::string product;
    Qty qty = 0;  // signed
    Paise avg_price = 0;
    Paise realised = 0;
};

struct Funds {
    Paise available = 0;
    Paise cash = 0;
    Paise blocked = 0;
    Paise reserved = 0;
};

class Strategy {
 public:
    virtual ~Strategy() = default;
    virtual void on_start(Context&) {}
    virtual void on_bar(Context&, const Bar&) {}
    virtual void on_tick(Context&, const Tick&) {}
    virtual void on_timer(Context&, std::uint64_t /*timer_id*/) {}
    virtual void on_fill(Context&, const FillView&) {}
    virtual void on_order_update(Context&, const OrderView&) {}
    virtual void on_session_phase(Context&, const PhaseChange&) {}
    virtual void on_session_end(Context&) {}
    virtual std::string save_state() const { return "{}"; }
    virtual void load_state(std::string_view) {}
};

// ── The C++ strategy registry (A2 §2) ───────────────────────────────────────
//
// STOCKSIM_STRATEGY(Class, "name"), written in the class's own namespace with its unqualified name, registers a
// factory at static-init time; a bundle's `strategy.kind: cpp`
// names it in `strategy.ref` (A1 §6.1). Strategy sources are built as an OBJECT library and linked straight
// into each binary, so the linker can never drop a registration the way it drops unreferenced members of a
// static library.
using StrategyFactory = std::shared_ptr<Strategy> (*)();

bool register_strategy(std::string_view name, StrategyFactory make);  // std::logic_error for a name taken twice
std::shared_ptr<Strategy> make_strategy(std::string_view name);       // RunError for an unknown name
std::vector<std::string> strategy_names();                            // sorted

// Params as the Python strategies read them (A2 §4.2): int(p.get(k, default)), bool(...) and str values.
// A missing key without a default, or a value int() would refuse, is a RunError naming the key.
std::int64_t param_int(const Json& params, std::string_view key, std::optional<std::int64_t> fallback = std::nullopt);
bool param_bool(const Json& params, std::string_view key, bool fallback);  // Python truthiness
std::string param_string(const Json& params, std::string_view key, std::optional<std::string> fallback = std::nullopt);

}  // namespace stocksim

#define STOCKSIM_STRATEGY(Class, Name)                                                              \
    namespace {                                                                                     \
    [[maybe_unused]] const bool stocksim_registered_##Class = ::stocksim::register_strategy(        \
        Name, []() -> std::shared_ptr<::stocksim::Strategy> { return std::make_shared<Class>(); }); \
    }
