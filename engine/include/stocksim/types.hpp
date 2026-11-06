// Core value types and enums (A2 §2, A4 §0.1, §3).
//
// Money is int64 paise, quantities int64 units, time UTC epoch nanoseconds, rates integers (ppb, ppm, bp).
// Enum spellings are exactly those the oracle writes into artifacts (A4 §9.2), so `to_string` output is what
// orders.csv and order_events.csv contain.
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>

namespace stocksim {

using Paise = std::int64_t;
using Qty = std::int64_t;
using TimeNs = std::int64_t;
using InstrumentId = std::uint32_t;
using OrderId = std::uint64_t;
using Date = std::int32_t;  // YYYYMMDD
using Ppb = std::int64_t;   // parts per 10^9: charge and tax rates
using Ppm = std::int64_t;   // parts per 10^6: participation, depth-take fraction
using Bp = std::int32_t;    // basis points: market protection

inline constexpr std::int64_t kPpb = 1'000'000'000;
inline constexpr std::int64_t kPpm = 1'000'000;
inline constexpr std::int64_t kBp = 10'000;
inline constexpr std::int64_t kPaisePerRupee = 100;
inline constexpr TimeNs kNsPerSecond = 1'000'000'000;

enum class Side : std::uint8_t { Buy, Sell };
enum class Product : std::uint8_t { CNC, MIS, NRML };
enum class OrderType : std::uint8_t { Market, Limit, SL, SLM };
enum class Validity : std::uint8_t { Day, IOC };
enum class OrderStatus : std::uint8_t { Pending, Open, TriggerPending, Complete, Cancelled, Rejected, Sliced };
enum class Reason : std::uint16_t {
    None, Instrument, Product, Session, Cas, QtyLot, Tick, Band, Freeze, Holdings, NoReference, Margin,  // rejects
    Eod, Ioc, User, Rms                                                                                  // cancels
};
enum class EventKind : std::uint16_t { TickQuote = 1, TickFull = 2, Index = 3, Bar = 4, Clock = 5, Gap = 6,
                                       RefData = 7, Settle = 8 };

// ── Spellings (A4 §9.2) ──────────────────────────────────────────────────────

constexpr std::string_view to_string(Side v) { return v == Side::Buy ? "BUY" : "SELL"; }

constexpr std::string_view to_string(Product v) {
    switch (v) {
        case Product::CNC: return "CNC";
        case Product::MIS: return "MIS";
        case Product::NRML: return "NRML";
    }
    return "?";
}

constexpr std::string_view to_string(OrderType v) {
    switch (v) {
        case OrderType::Market: return "MARKET";
        case OrderType::Limit: return "LIMIT";
        case OrderType::SL: return "SL";
        case OrderType::SLM: return "SL-M";
    }
    return "?";
}

constexpr std::string_view to_string(Validity v) { return v == Validity::Day ? "DAY" : "IOC"; }

constexpr std::string_view to_string(OrderStatus v) {
    switch (v) {
        case OrderStatus::Pending: return "PENDING";
        case OrderStatus::Open: return "OPEN";
        case OrderStatus::TriggerPending: return "TRIGGER_PENDING";
        case OrderStatus::Complete: return "COMPLETE";
        case OrderStatus::Cancelled: return "CANCELLED";
        case OrderStatus::Rejected: return "REJECTED";
        case OrderStatus::Sliced: return "SLICED";
    }
    return "?";
}

// The empty string for Reason::None, as in the oracle's `reason` column.
constexpr std::string_view to_string(Reason v) {
    switch (v) {
        case Reason::None: return "";
        case Reason::Instrument: return "R_INSTRUMENT";
        case Reason::Product: return "R_PRODUCT";
        case Reason::Session: return "R_SESSION";
        case Reason::Cas: return "R_CAS";
        case Reason::QtyLot: return "R_QTY_LOT";
        case Reason::Tick: return "R_TICK";
        case Reason::Band: return "R_BAND";
        case Reason::Freeze: return "R_FREEZE";
        case Reason::Holdings: return "R_HOLDINGS";
        case Reason::NoReference: return "R_NO_REFERENCE";
        case Reason::Margin: return "R_MARGIN";
        case Reason::Eod: return "EOD";
        case Reason::Ioc: return "IOC";
        case Reason::User: return "USER";
        case Reason::Rms: return "RMS";
    }
    return "?";
}

constexpr std::string_view to_string(EventKind v) {
    switch (v) {
        case EventKind::TickQuote: return "TICK_QUOTE";
        case EventKind::TickFull: return "TICK_FULL";
        case EventKind::Index: return "INDEX";
        case EventKind::Bar: return "BAR";
        case EventKind::Clock: return "CLOCK";
        case EventKind::Gap: return "GAP";
        case EventKind::RefData: return "REFDATA";
        case EventKind::Settle: return "SETTLE";
    }
    return "?";
}

// Parsing: the inverse of to_string; std::nullopt for an unknown spelling.
std::optional<Side> parse_side(std::string_view s);
std::optional<Product> parse_product(std::string_view s);
std::optional<OrderType> parse_order_type(std::string_view s);
std::optional<Validity> parse_validity(std::string_view s);
std::optional<OrderStatus> parse_order_status(std::string_view s);
std::optional<Reason> parse_reason(std::string_view s);

}  // namespace stocksim
