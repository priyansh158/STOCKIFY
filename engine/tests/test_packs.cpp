// A1 §5 / A4 §1.3: event records, pack validation, and the three event sources. The round trip against packs
// written by the Python builder is in tests/stockify/test_engine_packs.py.
#include <ostream>  // doctest prints string_view through operator<<

#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/event.hpp"
#include "stocksim/pack_reader.hpp"
#include "stocksim/source.hpp"

using namespace stocksim;

namespace {

template <typename T>
void put(std::string& out, T v) {
    char buf[sizeof(T)];
    std::memcpy(buf, &v, sizeof(T));
    out.append(buf, sizeof(T));
}

std::string prefix(EventKind k, std::uint16_t size, std::uint32_t iid, std::int64_t t, std::uint64_t seq) {
    std::string r;
    put(r, static_cast<std::uint16_t>(k));
    put(r, size);
    put(r, iid);
    put(r, t);
    put(r, seq);
    return r;
}

std::string bar(std::uint32_t iid, std::int64_t t, std::uint64_t seq, std::int64_t close) {
    std::string r = prefix(EventKind::Bar, 88, iid, t, seq);
    put(r, std::int32_t{60});
    put(r, std::int32_t{0});
    for (std::int64_t v : {t - 60'000'000'000LL, close - 5, close + 10, close - 10, close, std::int64_t{1000},
                           std::int64_t{7}}) {
        put(r, v);
    }
    return r;
}

std::string clock(std::int64_t t, std::uint64_t seq) { return prefix(EventKind::Clock, 24, 0, t, seq); }

std::string gap(std::uint32_t iid, std::int64_t t, std::uint64_t seq) {
    std::string r = prefix(EventKind::Gap, 32, iid, t, seq);
    put(r, std::uint8_t{0});
    put(r, std::uint8_t{0});
    put(r, std::uint16_t{4});
    put(r, std::uint32_t{1});
    return r;
}

std::string refdata(std::uint32_t iid, std::int64_t t, std::uint64_t seq, const std::string& json) {
    std::string r = prefix(EventKind::RefData, static_cast<std::uint16_t>(32 + json.size()), iid, t, seq);
    put(r, std::uint16_t{2});
    put(r, std::uint16_t{0});
    put(r, static_cast<std::uint32_t>(json.size()));
    return r + json;
}

std::string settle(std::uint32_t iid, std::int64_t t, std::uint64_t seq) {
    std::string r = prefix(EventKind::Settle, 40, iid, t, seq);
    put(r, std::uint8_t{0});
    r.append(3, '\0');
    put(r, std::int32_t{20261103});
    put(r, std::int64_t{2'512'345});
    return r;
}

std::string header(std::uint64_t count, std::int64_t tmin, std::int64_t tmax, std::uint8_t basis = 1) {
    std::string h(kPackMagic, sizeof kPackMagic);
    put(h, kPackVersion);
    put(h, std::uint16_t{0});
    put(h, std::uint32_t{64});
    put(h, count);
    put(h, tmin);
    put(h, tmax);
    put(h, basis);
    h.append(23, '\0');
    return h;
}

std::filesystem::path write_temp(const std::string& name, const std::string& bytes) {
    const auto path = std::filesystem::temp_directory_path() / ("stocksim_test_" + name + ".evt");
    std::ofstream(path, std::ios::binary).write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    return path;
}

const std::byte* as_bytes(const std::string& s) { return reinterpret_cast<const std::byte*>(s.data()); }

}  // namespace

TEST_CASE("records decode and re-encode byte for byte") {
    const std::vector<std::string> records = {bar(11, 2000, 1, 250'000), clock(2000, 2), gap(11, 1000, 3),
                                              refdata(11, 1500, 4, R"({"lower_paise":1,"upper_paise":9})"),
                                              settle(5, 1800, 5)};
    for (const std::string& r : records) {
        const Event e = read_record(as_bytes(r), r.size(), 0, 0);
        CHECK(e.size == r.size());
        CHECK(encode_record(e) == r);
    }
    const Event b = read_record(as_bytes(records[0]), records[0].size(), 0, 3);
    const BarFields f = decode_bar(b);
    CHECK(b.instrument == 11);
    CHECK(b.key() == EventKey{2000, kRankMarket, 3, 1});
    CHECK(f.interval_s == 60);
    CHECK(f.close == 250'000);
    CHECK(f.high == 250'010);
    CHECK(f.oi == 7);
    const Event r = read_record(as_bytes(records[3]), records[3].size(), 0, 0);
    CHECK(decode_refdata(r).subtype == 2);
    CHECK(decode_refdata(r).json == R"({"lower_paise":1,"upper_paise":9})");
    const Event s = read_record(as_bytes(records[4]), records[4].size(), 0, 0);
    CHECK(decode_settle(s).date == 20261103);
    CHECK(decode_settle(s).price == 2'512'345);
    CHECK_THROWS_AS(decode_settle(b), FormatError);
}

TEST_CASE("record checks name the byte offset") {
    std::string bad_kind = clock(1, 1);
    bad_kind[0] = 9;
    CHECK_THROWS_WITH_AS(read_record(as_bytes(bad_kind), bad_kind.size(), 0, 0, 64),
                         doctest::Contains("unknown record kind 9 at byte 64"), FormatError);
    std::string bad_size = bar(1, 1, 1, 100);
    bad_size[2] = 80;
    CHECK_THROWS_WITH_AS(read_record(as_bytes(bad_size), bad_size.size(), 0, 0),
                         doctest::Contains("BAR of size 80"), FormatError);
    std::string bad_ref = refdata(1, 1, 1, "{}");
    bad_ref[2] = 40;
    CHECK_THROWS_WITH_AS(read_record(as_bytes(bad_ref), bad_ref.size(), 0, 0),
                         doctest::Contains("REFDATA size 40 != 32 + 2"), FormatError);
    const std::string cut = bar(1, 1, 1, 100).substr(0, 50);
    CHECK_THROWS_WITH_AS(read_record(as_bytes(cut), cut.size(), 0, 0), doctest::Contains("truncated"), FormatError);
}

TEST_CASE("a pack is validated completely on open") {
    const std::string body = gap(1, 1000, 1) + bar(1, 2000, 2, 100) + clock(2000, 3);
    const std::string good = header(3, 1000, 2000) + body;
    PackReader reader(write_temp("good", good));
    CHECK(reader.header().count == 3);
    CHECK(reader.header().price_basis_name() == "adjusted");
    CHECK(reader.header().t_max == 2000);

    CHECK_THROWS_WITH_AS(PackReader(write_temp("count", header(4, 1000, 2000) + body)),
                         doctest::Contains("pack has 3 records, header says 4"), FormatError);
    CHECK_THROWS_WITH_AS(PackReader(write_temp("order", header(2, 0, 0) + bar(1, 2000, 1, 100) + gap(1, 1000, 2))),
                         doctest::Contains("out of order at byte 152"), FormatError);
    // Same time: GAP (rank 1) must come before BAR (rank 4).
    CHECK_THROWS_AS(PackReader(write_temp("rank", header(2, 0, 0) + bar(1, 2000, 1, 100) + gap(1, 2000, 2))),
                    FormatError);
    std::string magic = good;
    magic[7] = '2';
    CHECK_THROWS_WITH_AS(PackReader(write_temp("magic", magic)), doctest::Contains("not an .evt v1 file"),
                         FormatError);
    CHECK_THROWS_AS(PackReader(write_temp("trailing", good + "xyz")), FormatError);
    CHECK_THROWS_AS(PackReader(write_temp("short", good.substr(0, 40))), FormatError);
    CHECK_THROWS_AS(PackReader(std::filesystem::temp_directory_path() / "stocksim_no_such_pack.evt"), FormatError);
}

TEST_CASE("PackSource merges packs by (t, rank, pack index, seq)") {
    const auto a = std::make_shared<PackReader>(
        write_temp("merge_a", header(3, 1000, 3000) + bar(1, 1000, 1, 100) + bar(1, 2000, 2, 101) + bar(1, 3000, 3, 102)));
    const auto b = std::make_shared<PackReader>(
        write_temp("merge_b", header(3, 1000, 2000) + gap(2, 1000, 1) + bar(2, 2000, 2, 200) + clock(2000, 3)));
    PackSource src;
    src.add(a);
    src.add(b);
    std::vector<EventKey> keys;
    REQUIRE(src.peek_key() == EventKey{1000, kRankGap, 1, 1});
    while (const Event* e = src.next()) {
        keys.push_back(e->key());
    }
    const std::vector<EventKey> expected = {{1000, kRankGap, 1, 1},    {1000, kRankMarket, 0, 1},
                                            {2000, kRankMarket, 0, 2}, {2000, kRankMarket, 1, 2},
                                            {2000, kRankClock, 1, 3},  {3000, kRankMarket, 0, 3}};
    CHECK(keys == expected);
    CHECK_FALSE(src.peek_key().has_value());
    CHECK(src.next() == nullptr);
}

TEST_CASE("VectorSource serves records in key order") {
    VectorSource v({clock(3000, 3), bar(1, 1000, 1, 5), gap(1, 1000, 2)}, 0);
    std::vector<EventKind> kinds;
    while (const Event* e = v.next()) {
        kinds.push_back(e->kind);
    }
    CHECK(kinds == std::vector<EventKind>{EventKind::Gap, EventKind::Bar, EventKind::Clock});
}

TEST_CASE("LiveBatchSource takes batches in order and keeps events valid until the next push") {
    LiveBatchSource live;
    const std::string first = bar(1, 1000, 1, 100) + bar(1, 2000, 2, 101);
    live.push(std::as_bytes(std::span<const char>(first.data(), first.size())));
    CHECK(live.pending() == 2);
    const Event* e1 = live.next();
    REQUIRE(e1 != nullptr);
    CHECK(decode_bar(*e1).close == 100);
    const Event* e2 = live.next();
    REQUIRE(e2 != nullptr);
    CHECK(live.next() == nullptr);
    CHECK(decode_bar(*e2).close == 101);  // still readable: the batch is kept until the next push
    const std::string second = clock(2000, 3) + bar(1, 3000, 4, 102);
    live.push(std::as_bytes(std::span<const char>(second.data(), second.size())));
    CHECK(live.peek_key() == EventKey{2000, kRankClock, 0, 3});
    CHECK(live.next()->kind == EventKind::Clock);
    CHECK(decode_bar(*live.next()).close == 102);
    const std::string stale = bar(1, 2500, 5, 1);
    CHECK_THROWS_WITH_AS(live.push(std::as_bytes(std::span<const char>(stale.data(), stale.size()))),
                         doctest::Contains("out of order"), FormatError);
}
