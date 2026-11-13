// Event records of event packs and journals (A1 §5) and the ordering key of A4 §1.3.
//
// An Event is a view into encoded bytes (a memory-mapped pack or a pushed batch): the prefix fields decoded,
// the payload left in place. decode_*() read a payload with memcpy (no alignment assumptions);
// encode_record() writes one back from the decoded fields, so a reader → re-encoder round trip proves the
// layout byte for byte.
#pragma once

#include <array>
#include <compare>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>

#include "stocksim/types.hpp"

namespace stocksim {

// (t, rank, src, seq): one total order for every event, input or internal (A4 §1.3).
struct EventKey {
    TimeNs t = 0;
    std::uint16_t rank = 0;
    std::uint32_t src = 0;   // the pack's index in the bundle's pack list; 0 for journals and internal events
    std::uint64_t seq = 0;   // record sequence (input) or creation counter (internal)
    auto operator<=>(const EventKey&) const = default;
};

// Ranks of A4 §1.3. Input kinds use 1–5; internal events 0 and 6–10, so an input never ties an internal event.
// <block name="event-ranks" same-as="docs/plan/A4_engine_semantics.md:event-ranks" same-as-mode="sequence" same-as-format="flat" same-as-pattern='kRank(?P<value>[A-Za-z]+) =' keep-sorted keep-sorted-pattern='= (?P<value>\d+);' keep-sorted-format="numeric">
inline constexpr std::uint16_t kRankPhase = 0;
inline constexpr std::uint16_t kRankGap = 1;
inline constexpr std::uint16_t kRankRefData = 2;
inline constexpr std::uint16_t kRankSettle = 3;
inline constexpr std::uint16_t kRankMarket = 4;
inline constexpr std::uint16_t kRankClock = 5;
inline constexpr std::uint16_t kRankBarBuilt = 6;
inline constexpr std::uint16_t kRankMisSqoff = 7;
inline constexpr std::uint16_t kRankTimer = 8;
inline constexpr std::uint16_t kRankSegClose = 9;
inline constexpr std::uint16_t kRankDayEnd = 10;
// </block>

constexpr std::uint16_t rank_of(EventKind k) {
    switch (k) {
        case EventKind::Gap: return kRankGap;
        case EventKind::RefData: return kRankRefData;
        case EventKind::Settle: return kRankSettle;
        case EventKind::TickQuote:
        case EventKind::TickFull:
        case EventKind::Index:
        case EventKind::Bar: return kRankMarket;
        case EventKind::Clock: return kRankClock;
    }
    return kRankClock;
}

inline constexpr std::size_t kHeaderSize = 64;
inline constexpr std::size_t kPrefixSize = 24;
inline constexpr std::size_t kMaxRefDataJson = 65'535 - 32;  // size is a u16 (A1 §5)
inline constexpr char kPackMagic[8] = {'S', 'T', 'K', 'E', 'V', 'T', '0', '1'};
inline constexpr std::uint16_t kPackVersion = 1;
inline constexpr std::uint16_t kFlagJournal = 1;
inline constexpr std::uint16_t kFlagClock = 2;

// GAP reasons and REFDATA subtypes (A1 §5); a value outside these is a malformed record, as in the oracle.
// <block name="gap-reasons" same-as="docs/plan/A1_schemas.md:gap-reasons" same-as-pattern='\{(?P<key>\d+), "(?P<value>[a-z_]+)"\}'>
inline constexpr std::pair<std::uint16_t, std::string_view> kGapReasons[] = {{1, "disconnect"}, {2, "process_stop"}, {3, "token_expired"}, {4, "stall"}, {5, "not_subscribed"}};
// </block>
// <block name="refdata-subtypes" same-as="docs/plan/A1_schemas.md:refdata-subtypes" same-as-pattern='\{(?P<key>\d+), "(?P<value>[a-z_]+)"\}'>
inline constexpr std::pair<std::uint16_t, std::string_view> kRefDataSubtypes[] = {{1, "instrument_add"}, {2, "price_band"}, {3, "margin_requirement"}, {4, "spec_change"}};
// </block>

// The name of a code, or an empty view for an unknown one.
std::string_view gap_reason_name(std::uint16_t code);
std::string_view refdata_subtype_name(std::uint16_t code);

// Fixed record sizes; REFDATA is 32 + len.
constexpr std::uint16_t fixed_size(EventKind k) {
    switch (k) {
        case EventKind::TickQuote: return 104;
        case EventKind::TickFull: return 384;
        case EventKind::Index: return 80;
        case EventKind::Bar: return 88;
        case EventKind::Clock: return 24;
        case EventKind::Gap: return 32;
        case EventKind::RefData: return 0;
        case EventKind::Settle: return 40;
    }
    return 0;
}

struct Event {
    EventKind kind = EventKind::Clock;
    InstrumentId instrument = 0;
    TimeNs t = 0;
    std::uint64_t seq = 0;
    std::uint32_t src = 0;
    const std::byte* record = nullptr;  // the whole record, prefix included
    std::uint16_t size = 0;             // whole record size

    EventKey key() const { return EventKey{t, rank_of(kind), src, seq}; }
    const std::byte* payload() const { return record + kPrefixSize; }
};

// ── Decoded payloads (A1 §5) ─────────────────────────────────────────────────

struct QuoteFields {
    Paise ltp = 0;
    Qty last_qty = 0;
    Paise atp = 0;
    Qty volume = 0, buy_qty = 0, sell_qty = 0;
    Paise open = 0, high = 0, low = 0, close = 0;
};

struct DepthLevel {
    Paise price = 0;
    Qty qty = 0;
    std::int32_t orders = 0;
};

struct TickFields {
    QuoteFields quote;
    bool full = false;  // TICK_FULL: the fields below are set
    std::int64_t last_trade_ts_s = 0, oi = 0, oi_high = 0, oi_low = 0, exch_ts_s = 0;
    std::array<DepthLevel, 5> bids{};
    std::array<DepthLevel, 5> asks{};
};

struct IndexFields {
    std::int64_t value = 0, high = 0, low = 0, open = 0, close = 0, change = 0, exch_ts_s = 0;  // points × 100
};

struct BarFields {
    std::int32_t interval_s = 0;
    TimeNs start_ns = 0;
    Paise open = 0, high = 0, low = 0, close = 0;
    Qty volume = 0;
    std::int64_t oi = 0;
};

struct GapFields {
    std::uint8_t phase = 0;   // 0 start, 1 end
    std::uint16_t reason = 0; // 1 disconnect … 5 not_subscribed (packs.GAP_REASONS)
    std::uint32_t scope = 0;  // 0 all, 1 the instrument in the prefix
};

struct RefDataFields {
    std::uint16_t subtype = 0;  // 1 instrument_add … 4 spec_change (packs.REFDATA_SUBTYPES)
    std::string_view json;      // canonical JSON text, a view into the record
};

struct SettleFields {
    std::uint8_t subtype = 0;  // 0 final settlement price of the underlying, 1 daily settlement price
    Date date = 0;             // YYYYMMDD
    Paise price = 0;
};

TickFields decode_tick(const Event& e);  // TICK_QUOTE or TICK_FULL
IndexFields decode_index(const Event& e);
BarFields decode_bar(const Event& e);
GapFields decode_gap(const Event& e);
RefDataFields decode_refdata(const Event& e);
SettleFields decode_settle(const Event& e);

// The record re-encoded from its decoded fields (paddings written as zero).
std::string encode_record(const Event& e);

// Reads one record prefix at `offset` of `data` and checks kind and size against the bytes available.
// FormatError naming the byte offset on any violation. `base_offset` is added to offsets in messages.
Event read_record(const std::byte* data, std::size_t length, std::size_t offset, std::uint32_t src,
                  std::size_t base_offset = 0);

}  // namespace stocksim
