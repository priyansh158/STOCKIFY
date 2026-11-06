// Orders, fills and everything a fill can change (A4 §3–§6), the twins of stockify.oracle.orders and the
// oracle's `Book`. A bar's two simulated paths each run on a fork of the Book (A4 §4.1).
#pragma once

#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "stocksim/costs.hpp"
#include "stocksim/ledger.hpp"
#include "stocksim/pcg32.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

struct Order {
    OrderId order_id = 0;
    OrderId parent_id = 0;
    InstrumentId instrument = 0;
    // As the strategy passed them (written to orders.csv as given, even when invalid and rejected), and parsed;
    // the enums are meaningful only once validation has passed R_INSTRUMENT / R_PRODUCT.
    std::string side_text, product_text, type_text, validity_text;
    Side side = Side::Buy;
    Qty qty = 0;
    Product product = Product::CNC;
    OrderType type = OrderType::Market;  // as placed (a modify can change it)
    Paise price = 0;
    Paise trigger = 0;
    Validity validity = Validity::Day;
    std::optional<Bp> protection;  // nullopt = AUTO
    std::string tag;
    TimeNs t_submit = 0;
    TimeNs t_activate = 0;
    OrderStatus status = OrderStatus::Pending;
    Qty filled_qty = 0;
    Paise fill_value = 0;  // Σ price × qty of fills
    Reason reason = Reason::None;
    // working state
    std::optional<Paise> limit;  // effective limit after protection / trigger
    bool from_market = false;
    bool triggered = false;
    std::optional<Bp> protection_bp;
    Paise reserved = 0;  // funds reserved at submit (A4 §6.5)
    bool first_eligible_done = false;
    std::optional<Date> trade_date;
    bool charged = false;
    std::optional<Date> eod_after;  // D-44: a DAY order on daily bars, cancelled after this date's bar

    Qty remaining() const { return qty - filled_qty; }
    Paise avg_price() const { return filled_qty != 0 ? floor_div(fill_value, filled_qty) : 0; }
    int sign() const { return side == Side::Buy ? 1 : -1; }
    bool active() const { return status == OrderStatus::Open || status == OrderStatus::TriggerPending; }
    bool final() const {
        return status == OrderStatus::Complete || status == OrderStatus::Cancelled ||
               status == OrderStatus::Rejected || status == OrderStatus::Sliced;
    }
    bool live() const { return status == OrderStatus::Pending || active(); }
};

struct Fill {
    std::uint64_t fill_id = 0;
    OrderId order_id = 0;
    TimeNs t_ns = 0;
    InstrumentId instrument = 0;
    Side side = Side::Buy;
    Qty qty = 0;
    Paise price = 0;
    std::string evidence;
};

struct OrderEventRow {
    TimeNs t = 0;
    OrderId order_id = 0;
    std::string event;
    std::string detail;
};

struct ChargeRow {
    std::string kind;  // order | dp | sqoff_fee | settlement
    OrderId order_id = 0;
    InstrumentId instrument = 0;
    Date trade_date = 0;
    ChargeParts parts;
    Paise total = 0;
};

struct Callback {
    bool is_fill = false;
    OrderId order_id = 0;  // an order callback
    Fill fill;             // a fill callback
};

struct Book {
    Ledger ledger;
    std::map<OrderId, Order> orders;  // all orders (a fork: the forked instrument's live orders only)
    Pcg32 rng;
    std::vector<Fill> fills;
    std::vector<ChargeRow> charges;
    std::vector<OrderEventRow> order_events;
    std::deque<Callback> callbacks;
    std::set<std::pair<InstrumentId, Date>> dp_done;
    std::uint64_t next_fill = 1;

    Book(Ledger l, Pcg32 r) : ledger(std::move(l)), rng(r) {}

    // A copy for one bar path: the ledger, the instrument's active orders, the RNG (runner.Book.fork).
    Book fork(InstrumentId iid) const {
        Book b(ledger, rng);
        for (const auto& [id, o] : orders) {
            if (o.instrument == iid && o.active()) {
                b.orders.emplace(id, o);
            }
        }
        b.dp_done = dp_done;
        b.next_fill = next_fill;
        return b;
    }
};

}  // namespace stocksim
