// The strategy interface (A2 §2, §4): hooks the engine calls and the plain values it passes.
// Field for field these mirror stockify.strategy (Bar, Tick, PhaseChange, PositionView, Funds) and
// stockify.oracle.orders (OrderView, FillView), so a Python strategy sees the same objects on both engines.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

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

}  // namespace stocksim
