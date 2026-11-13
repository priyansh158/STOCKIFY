// The run folder's deterministic artifacts (A1 §6.3, A4 §9), the twin of stockify.oracle.artifacts.write.
// manifest.json (wall clock, host) is written by the Python caller: it is outside the hashed set.
//
// The order, order-event, fill and charge rows are built here once, as cells in header order, for both the
// CSV files and Engine::drain_outputs (M5 journals): the two can never disagree on a column.
#pragma once

#include <cstdint>
#include <filesystem>
#include <initializer_list>
#include <map>
#include <string>
#include <string_view>

#include "stocksim/engine.hpp"
#include "stocksim/format.hpp"

namespace stocksim {

// Column headers, as stockify.oracle.artifacts writes them (A1 §6.3).
inline constexpr std::string_view kOrdersHeader =
    "order_id,parent_id,instrument_id,side,product,type,validity,qty,price_paise,trigger_paise,protection_bp,"
    "tag,t_submit_ns,t_activate_ns,status,filled_qty,avg_price_paise,reason,note";
inline constexpr std::string_view kOrderEventsHeader = "t_ns,order_id,event,detail";
inline constexpr std::string_view kFillsHeader = "fill_id,order_id,t_ns,instrument_id,side,qty,price_paise,evidence";
inline constexpr std::string_view kChargesHeader =
    "row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,dp,"
    "other,total";

namespace detail {
inline std::int64_t i64(std::uint64_t v) { return static_cast<std::int64_t>(v); }
}  // namespace detail

// Each hands `sink` one row as std::initializer_list<CsvCell>; the string_views live for that call only.
template <class Sink>
void order_row(const Order& o, Sink&& sink) {
    const std::string status(to_string(o.status));
    const std::string reason(to_string(o.reason));
    sink(std::initializer_list<CsvCell>{
        detail::i64(o.order_id), detail::i64(o.parent_id), std::int64_t{o.instrument}, std::string_view(o.side_text),
        std::string_view(o.product_text), std::string_view(o.type_text), std::string_view(o.validity_text), o.qty,
        o.price, o.trigger, o.protection_bp ? CsvCell(std::int64_t{*o.protection_bp}) : CsvCell(CsvNull{}),
        std::string_view(o.tag), o.t_submit, o.t_activate, std::string_view(status), o.filled_qty, o.avg_price(),
        std::string_view(reason), std::string_view(o.note)});
}

template <class Sink>
void order_event_row(const OrderEventRow& r, Sink&& sink) {
    sink(std::initializer_list<CsvCell>{r.t, detail::i64(r.order_id), std::string_view(r.event),
                                        std::string_view(r.detail)});
}

template <class Sink>
void fill_row(const Fill& f, Sink&& sink) {
    sink(std::initializer_list<CsvCell>{detail::i64(f.fill_id), detail::i64(f.order_id), f.t_ns,
                                        std::int64_t{f.instrument}, to_string(f.side), f.qty, f.price,
                                        std::string_view(f.evidence)});
}

template <class Sink>
void charge_row(std::int64_t row_no, const ChargeRow& c, Sink&& sink) {  // row_no counts from 1 over the run
    const std::string d = iso_date(c.trade_date);
    const ChargeParts& p = c.parts;
    sink(std::initializer_list<CsvCell>{row_no, std::string_view(c.kind), detail::i64(c.order_id),
                                        std::int64_t{c.instrument}, std::string_view(d), p.turnover, p.brokerage,
                                        p.stt, p.exchange, p.sebi, p.stamp, p.gst, p.dp, p.other, c.total});
}

// Writes orders.csv … events_log.txt and determinism.json into `folder`; returns file name → SHA-256.
std::map<std::string, std::string> write_artifacts(const Engine& engine, const std::filesystem::path& folder);

}  // namespace stocksim
