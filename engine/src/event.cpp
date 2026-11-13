#include "stocksim/event.hpp"

#include <cstring>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

namespace {

template <typename T>
T load(const std::byte* p) {
    T v;
    std::memcpy(&v, p, sizeof(T));
    return v;
}

template <typename T>
void store(std::string& out, T v) {
    char buf[sizeof(T)];
    std::memcpy(buf, &v, sizeof(T));
    out.append(buf, sizeof(T));
}

void require_kind(const Event& e, EventKind k, const char* what) {
    if (e.kind != k) {
        throw FormatError(std::string("decode_") + what + " on a " + std::string(to_string(e.kind)) + " record");
    }
}

QuoteFields load_quote(const std::byte* p) {
    QuoteFields q;
    q.ltp = load<std::int64_t>(p + 0);
    q.last_qty = load<std::int64_t>(p + 8);
    q.atp = load<std::int64_t>(p + 16);
    q.volume = load<std::int64_t>(p + 24);
    q.buy_qty = load<std::int64_t>(p + 32);
    q.sell_qty = load<std::int64_t>(p + 40);
    q.open = load<std::int64_t>(p + 48);
    q.high = load<std::int64_t>(p + 56);
    q.low = load<std::int64_t>(p + 64);
    q.close = load<std::int64_t>(p + 72);
    return q;
}

void store_quote(std::string& out, const QuoteFields& q) {
    for (std::int64_t v : {q.ltp, q.last_qty, q.atp, q.volume, q.buy_qty, q.sell_qty, q.open, q.high, q.low, q.close}) {
        store(out, v);
    }
}

void store_prefix(std::string& out, const Event& e, std::uint16_t size) {
    store(out, static_cast<std::uint16_t>(e.kind));
    store(out, size);
    store(out, e.instrument);
    store(out, e.t);
    store(out, e.seq);
}

bool known_kind(std::uint16_t k) { return k >= 1 && k <= 8; }

template <std::size_t N>
std::string_view name_of(std::uint16_t code, const std::pair<std::uint16_t, std::string_view> (&table)[N]) {
    for (const auto& [c, name] : table) {
        if (c == code) {
            return name;
        }
    }
    return {};
}

}  // namespace

std::string_view gap_reason_name(std::uint16_t code) { return name_of(code, kGapReasons); }

std::string_view refdata_subtype_name(std::uint16_t code) { return name_of(code, kRefDataSubtypes); }

TickFields decode_tick(const Event& e) {
    if (e.kind != EventKind::TickQuote && e.kind != EventKind::TickFull) {
        throw FormatError("decode_tick on a " + std::string(to_string(e.kind)) + " record");
    }
    TickFields f;
    const std::byte* p = e.payload();
    f.quote = load_quote(p);
    if (e.kind == EventKind::TickFull) {
        f.full = true;
        f.last_trade_ts_s = load<std::int64_t>(p + 80);
        f.oi = load<std::int64_t>(p + 88);
        f.oi_high = load<std::int64_t>(p + 96);
        f.oi_low = load<std::int64_t>(p + 104);
        f.exch_ts_s = load<std::int64_t>(p + 112);
        for (int i = 0; i < 10; ++i) {
            const std::byte* lv = p + 120 + 24 * i;
            DepthLevel d{load<std::int64_t>(lv), load<std::int64_t>(lv + 8), load<std::int32_t>(lv + 16)};
            (i < 5 ? f.bids[static_cast<std::size_t>(i)] : f.asks[static_cast<std::size_t>(i - 5)]) = d;
        }
    }
    return f;
}

IndexFields decode_index(const Event& e) {
    require_kind(e, EventKind::Index, "index");
    const std::byte* p = e.payload();
    return IndexFields{load<std::int64_t>(p),      load<std::int64_t>(p + 8),  load<std::int64_t>(p + 16),
                       load<std::int64_t>(p + 24), load<std::int64_t>(p + 32), load<std::int64_t>(p + 40),
                       load<std::int64_t>(p + 48)};
}

BarFields decode_bar(const Event& e) {
    require_kind(e, EventKind::Bar, "bar");
    const std::byte* p = e.payload();
    BarFields b;
    b.interval_s = load<std::int32_t>(p);
    b.start_ns = load<std::int64_t>(p + 8);
    b.open = load<std::int64_t>(p + 16);
    b.high = load<std::int64_t>(p + 24);
    b.low = load<std::int64_t>(p + 32);
    b.close = load<std::int64_t>(p + 40);
    b.volume = load<std::int64_t>(p + 48);
    b.oi = load<std::int64_t>(p + 56);
    return b;
}

GapFields decode_gap(const Event& e) {
    require_kind(e, EventKind::Gap, "gap");
    const std::byte* p = e.payload();
    return GapFields{load<std::uint8_t>(p), load<std::uint16_t>(p + 2), load<std::uint32_t>(p + 4)};
}

RefDataFields decode_refdata(const Event& e) {
    require_kind(e, EventKind::RefData, "refdata");
    const std::byte* p = e.payload();
    const auto len = load<std::uint32_t>(p + 4);
    return RefDataFields{load<std::uint16_t>(p), std::string_view(reinterpret_cast<const char*>(p + 8), len)};
}

SettleFields decode_settle(const Event& e) {
    require_kind(e, EventKind::Settle, "settle");
    const std::byte* p = e.payload();
    return SettleFields{load<std::uint8_t>(p), load<std::int32_t>(p + 4), load<std::int64_t>(p + 8)};
}

std::string encode_record(const Event& e) {
    std::string out;
    out.reserve(e.size);
    switch (e.kind) {
        case EventKind::TickQuote:
        case EventKind::TickFull: {
            const TickFields f = decode_tick(e);
            store_prefix(out, e, fixed_size(e.kind));
            store_quote(out, f.quote);
            if (f.full) {
                for (std::int64_t v : {f.last_trade_ts_s, f.oi, f.oi_high, f.oi_low, f.exch_ts_s}) {
                    store(out, v);
                }
                for (const auto* side : {&f.bids, &f.asks}) {
                    for (const DepthLevel& d : *side) {
                        store(out, d.price);
                        store(out, d.qty);
                        store(out, d.orders);
                        store(out, std::int32_t{0});
                    }
                }
            }
            break;
        }
        case EventKind::Index: {
            const IndexFields f = decode_index(e);
            store_prefix(out, e, fixed_size(e.kind));
            for (std::int64_t v : {f.value, f.high, f.low, f.open, f.close, f.change, f.exch_ts_s}) {
                store(out, v);
            }
            break;
        }
        case EventKind::Bar: {
            const BarFields b = decode_bar(e);
            store_prefix(out, e, fixed_size(e.kind));
            store(out, b.interval_s);
            store(out, std::int32_t{0});
            for (std::int64_t v : {b.start_ns, b.open, b.high, b.low, b.close, b.volume, b.oi}) {
                store(out, v);
            }
            break;
        }
        case EventKind::Clock:
            store_prefix(out, e, fixed_size(e.kind));
            break;
        case EventKind::Gap: {
            const GapFields g = decode_gap(e);
            store_prefix(out, e, fixed_size(e.kind));
            store(out, g.phase);
            store(out, std::uint8_t{0});
            store(out, g.reason);
            store(out, g.scope);
            break;
        }
        case EventKind::RefData: {
            const RefDataFields r = decode_refdata(e);
            store_prefix(out, e, static_cast<std::uint16_t>(32 + r.json.size()));
            store(out, r.subtype);
            store(out, std::uint16_t{0});
            store(out, static_cast<std::uint32_t>(r.json.size()));
            out.append(r.json);
            break;
        }
        case EventKind::Settle: {
            const SettleFields s = decode_settle(e);
            store_prefix(out, e, fixed_size(e.kind));
            store(out, s.subtype);
            out.append(3, '\0');
            store(out, s.date);
            store(out, s.price);
            break;
        }
    }
    return out;
}

Event read_record(const std::byte* data, std::size_t length, std::size_t offset, std::uint32_t src,
                  std::size_t base_offset) {
    const std::size_t at = base_offset + offset;
    if (length - offset < kPrefixSize) {
        throw FormatError("truncated record prefix at byte " + std::to_string(at));
    }
    const std::byte* p = data + offset;
    const auto kind = load<std::uint16_t>(p);
    const auto size = load<std::uint16_t>(p + 2);
    if (!known_kind(kind)) {
        throw FormatError("unknown record kind " + std::to_string(kind) + " at byte " + std::to_string(at));
    }
    const auto k = static_cast<EventKind>(kind);
    if (k == EventKind::RefData) {
        if (size < 32 || length - offset < 32) {
            throw FormatError("REFDATA of size " + std::to_string(size) + " at byte " + std::to_string(at));
        }
        const auto len = load<std::uint32_t>(p + kPrefixSize + 4);
        if (size != 32 + len) {
            throw FormatError("REFDATA size " + std::to_string(size) + " != 32 + " + std::to_string(len) +
                              " at byte " + std::to_string(at));
        }
    } else if (size != fixed_size(k)) {
        throw FormatError(std::string(to_string(k)) + " of size " + std::to_string(size) + " at byte " +
                          std::to_string(at));
    }
    if (length - offset < size) {
        throw FormatError("truncated " + std::string(to_string(k)) + " record at byte " + std::to_string(at));
    }
    if (k == EventKind::Gap && gap_reason_name(load<std::uint16_t>(p + kPrefixSize + 2)).empty()) {
        throw FormatError("unknown GAP reason " + std::to_string(load<std::uint16_t>(p + kPrefixSize + 2)) +
                          " at byte " + std::to_string(at));
    }
    if (k == EventKind::RefData && refdata_subtype_name(load<std::uint16_t>(p + kPrefixSize)).empty()) {
        throw FormatError("unknown REFDATA subtype " + std::to_string(load<std::uint16_t>(p + kPrefixSize)) +
                          " at byte " + std::to_string(at));
    }
    Event e;
    e.kind = k;
    e.size = size;
    e.instrument = load<std::uint32_t>(p + 4);
    e.t = load<std::int64_t>(p + 8);
    e.seq = load<std::uint64_t>(p + 16);
    e.src = src;
    e.record = p;
    return e;
}

}  // namespace stocksim
