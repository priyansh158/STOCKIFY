// stockify._engine.testing: pack reader and event sources, shaped like stockify.data.packs.read (M3.2 tests).
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#include "stocksim/errors.hpp"
#include "stocksim/event.hpp"
#include "stocksim/pack_reader.hpp"
#include "stocksim/source.hpp"

namespace py = pybind11;

namespace {

py::tuple key_tuple(const stocksim::EventKey& k) { return py::make_tuple(k.t, k.rank, k.src, k.seq); }

py::dict record_dict(const stocksim::Event& e, const py::object& json_loads) {
    using stocksim::EventKind;
    py::dict r;
    r["kind"] = std::string(stocksim::to_string(e.kind));
    r["instrument_id"] = e.instrument;
    r["t_event"] = e.t;
    r["seq"] = e.seq;
    switch (e.kind) {
        case EventKind::TickQuote:
        case EventKind::TickFull: {
            const stocksim::TickFields f = stocksim::decode_tick(e);
            const auto& q = f.quote;
            r["ltp"] = q.ltp;
            r["last_qty"] = q.last_qty;
            r["atp"] = q.atp;
            r["volume"] = q.volume;
            r["buy_qty"] = q.buy_qty;
            r["sell_qty"] = q.sell_qty;
            r["open"] = q.open;
            r["high"] = q.high;
            r["low"] = q.low;
            r["close"] = q.close;
            if (f.full) {
                r["last_trade_ts_s"] = f.last_trade_ts_s;
                r["oi"] = f.oi;
                r["oi_high"] = f.oi_high;
                r["oi_low"] = f.oi_low;
                r["exch_ts_s"] = f.exch_ts_s;
                py::list depth;
                for (const auto* side : {&f.bids, &f.asks}) {
                    for (const auto& d : *side) {
                        depth.append(py::make_tuple(d.price, d.qty, d.orders));
                    }
                }
                r["depth"] = depth;
            }
            break;
        }
        case EventKind::Index: {
            const auto f = stocksim::decode_index(e);
            r["value"] = f.value;
            r["high"] = f.high;
            r["low"] = f.low;
            r["open"] = f.open;
            r["close"] = f.close;
            r["change"] = f.change;
            r["exch_ts_s"] = f.exch_ts_s;
            break;
        }
        case EventKind::Bar: {
            const auto b = stocksim::decode_bar(e);
            r["interval_s"] = b.interval_s;
            r["start_ns"] = b.start_ns;
            r["open"] = b.open;
            r["high"] = b.high;
            r["low"] = b.low;
            r["close"] = b.close;
            r["volume"] = b.volume;
            r["oi"] = b.oi;
            break;
        }
        case EventKind::Clock:
            break;
        case EventKind::Gap: {
            const auto g = stocksim::decode_gap(e);
            r["phase"] = g.phase;
            r["reason"] = std::string(stocksim::gap_reason_name(g.reason));
            r["scope"] = g.scope;
            break;
        }
        case EventKind::RefData: {
            const auto x = stocksim::decode_refdata(e);
            r["subtype"] = std::string(stocksim::refdata_subtype_name(x.subtype));
            r["payload"] = json_loads(py::bytes(std::string(x.json)).attr("decode")("utf-8"));
            break;
        }
        case EventKind::Settle: {
            const auto s = stocksim::decode_settle(e);
            char date[16];
            std::snprintf(date, sizeof date, "%04d-%02d-%02d", s.date / 10000, s.date / 100 % 100, s.date % 100);
            r["subtype"] = s.subtype;
            r["date"] = std::string(date);
            r["price_paise"] = s.price;
            break;
        }
    }
    return r;
}

}  // namespace

void bind_testing_packs(py::module_& t) {
    // (header, records) exactly as stockify.data.packs.read returns them.
    t.def("read_pack", [](const std::string& path) {
        const py::object json_loads = py::module_::import("json").attr("loads");
        const auto reader = std::make_shared<stocksim::PackReader>(path);
        const stocksim::PackHeader& h = reader->header();
        py::dict header;
        header["version"] = h.version;
        header["flags"] = h.flags;
        header["count"] = h.count;
        header["t_min"] = h.t_min;
        header["t_max"] = h.t_max;
        header["price_basis"] = h.price_basis_name();
        py::list records;
        stocksim::PackSource src;
        src.add(reader);
        while (const stocksim::Event* e = src.next()) {
            records.append(record_dict(*e, json_loads));
        }
        return py::make_tuple(header, records);
    });
    // The pack rebuilt from decoded fields: header bytes as read, then every record re-encoded.
    t.def("reencode_pack", [](const std::string& path) {
        const auto reader = std::make_shared<stocksim::PackReader>(path);
        std::string out(reinterpret_cast<const char*>(reader->data()), stocksim::kHeaderSize);
        stocksim::PackSource src;
        src.add(reader);
        while (const stocksim::Event* e = src.next()) {
            out += stocksim::encode_record(*e);
        }
        return py::bytes(out);
    });
    // Keys (t, rank, src, seq) of several packs merged by PackSource.
    t.def("merge_keys", [](const std::vector<std::string>& paths) {
        stocksim::PackSource src;
        for (const auto& p : paths) {
            src.add(std::make_shared<stocksim::PackReader>(p));
        }
        py::list keys;
        while (true) {
            const auto peeked = src.peek_key();
            const stocksim::Event* e = src.next();
            if (e == nullptr) {
                break;
            }
            if (!peeked || *peeked != e->key()) {
                throw stocksim::FormatError("peek_key disagrees with next()");
            }
            keys.append(key_tuple(e->key()));
        }
        return keys;
    });
    // Keys of records pushed batch by batch into a LiveBatchSource, draining after each push.
    t.def("live_keys", [](const std::vector<py::bytes>& batches) {
        stocksim::LiveBatchSource live;
        py::list keys;
        for (const py::bytes& b : batches) {
            const std::string data = b;
            live.push(std::as_bytes(std::span<const char>(data.data(), data.size())));
            while (const stocksim::Event* e = live.next()) {
                keys.append(key_tuple(e->key()));
            }
        }
        return keys;
    });
    // A script of pushes and reads on one LiveBatchSource: ("push", bytes) -> "ok" or "error: …";
    // ("read", n) -> the keys of up to n records read (n < 0: all that are there).
    t.def("live_steps", [](const std::vector<std::pair<std::string, py::object>>& steps) {
        stocksim::LiveBatchSource live;
        py::list out;
        for (const auto& [op, arg] : steps) {
            if (op == "push") {
                const std::string data = arg.cast<py::bytes>();
                try {
                    live.push(std::as_bytes(std::span<const char>(data.data(), data.size())));
                    out.append("ok");
                } catch (const std::exception& ex) {
                    out.append(std::string("error: ") + ex.what());
                }
            } else {
                long long n = arg.cast<long long>();
                py::list keys;
                while (n != 0) {
                    const stocksim::Event* e = live.next();
                    if (e == nullptr) {
                        break;
                    }
                    keys.append(key_tuple(e->key()));
                    --n;
                }
                out.append(keys);
            }
        }
        return out;
    });
}
