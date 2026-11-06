#include "stocksim/artifacts.hpp"

#include <fstream>
#include <iterator>

#include "stocksim/format.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/metrics.hpp"
#include "stocksim/sha256.hpp"

namespace stocksim {

namespace {

// Column headers, as stockify.oracle.artifacts writes them (A1 §6.3).
constexpr const char* kOrders =
    "order_id,parent_id,instrument_id,side,product,type,validity,qty,price_paise,trigger_paise,protection_bp,"
    "tag,t_submit_ns,t_activate_ns,status,filled_qty,avg_price_paise,reason";
constexpr const char* kOrderEvents = "t_ns,order_id,event,detail";
constexpr const char* kFills = "fill_id,order_id,t_ns,instrument_id,side,qty,price_paise,evidence";
constexpr const char* kCharges =
    "row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,dp,"
    "other,total";
constexpr const char* kPositions =
    "trade_date,instrument_id,product,qty,avg_price_paise,mark_paise,mark_source,unrealised_paise,realised_paise";
constexpr const char* kEquity = "trade_date,nav_paise,cash_paise,margin_blocked_paise,gross_pnl_paise,charges_paise";
constexpr const char* kMargin = "trade_date,underlying_id,model,scan,somc,elm,addons,total";

// The ✔ rows of A1 §6.3, in order (checked by blockwatch).
// <block name="deterministic-files" same-as="docs/plan/A1_schemas.md:deterministic-files" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+\.[a-z]+)"'>
constexpr const char* kDeterministic[] = {"orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
                                          "margin_daily.csv", "metrics.json", "events_log.txt"};
// </block>

std::int64_t i64(std::uint64_t v) { return static_cast<std::int64_t>(v); }

std::string read_all(const std::filesystem::path& p) {
    std::ifstream in(p, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
}

}  // namespace

std::map<std::string, std::string> write_artifacts(const Engine& e, const std::filesystem::path& folder) {
    std::filesystem::create_directories(folder);
    const Book& b = e.book();

    CsvWriter orders(kOrders);
    for (const auto& [id, o] : b.orders) {  // std::map: order_id order
        const std::string status(to_string(o.status));
        const std::string reason(to_string(o.reason));
        orders.add_row({i64(o.order_id), i64(o.parent_id), std::int64_t{o.instrument}, std::string_view(o.side_text),
                        std::string_view(o.product_text), std::string_view(o.type_text),
                        std::string_view(o.validity_text), o.qty, o.price, o.trigger,
                        o.protection_bp ? CsvCell(std::int64_t{*o.protection_bp}) : CsvCell(CsvNull{}),
                        std::string_view(o.tag), o.t_submit, o.t_activate, std::string_view(status), o.filled_qty,
                        o.avg_price(), std::string_view(reason)});
    }
    orders.write(folder / "orders.csv");

    CsvWriter events(kOrderEvents);
    for (const OrderEventRow& r : b.order_events) {
        events.add_row({r.t, i64(r.order_id), std::string_view(r.event), std::string_view(r.detail)});
    }
    events.write(folder / "order_events.csv");

    CsvWriter fills(kFills);
    for (const Fill& f : b.fills) {
        fills.add_row({i64(f.fill_id), i64(f.order_id), f.t_ns, std::int64_t{f.instrument}, to_string(f.side), f.qty,
                       f.price, std::string_view(f.evidence)});
    }
    fills.write(folder / "fills.csv");

    CsvWriter charges(kCharges);
    std::int64_t n = 0;
    for (const ChargeRow& c : b.charges) {
        const std::string d = iso_date(c.trade_date);
        const ChargeParts& p = c.parts;
        charges.add_row({++n, std::string_view(c.kind), i64(c.order_id), std::int64_t{c.instrument}, std::string_view(d),
                         p.turnover, p.brokerage, p.stt, p.exchange, p.sebi, p.stamp, p.gst, p.dp, p.other, c.total});
    }
    charges.write(folder / "charges.csv");

    CsvWriter positions(kPositions);
    for (const PositionEodRow& r : e.positions_eod()) {
        const std::string d = iso_date(r.trade_date);
        positions.add_row({std::string_view(d), std::int64_t{r.instrument}, to_string(r.product), r.qty, r.avg_price,
                           r.mark, std::string_view(r.mark_source), r.unrealised, r.realised});
    }
    positions.write(folder / "positions_eod.csv");

    CsvWriter equity(kEquity);
    for (const EquityRow& r : e.equity()) {
        const std::string d = iso_date(r.trade_date);
        equity.add_row({std::string_view(d), r.nav, r.cash, r.blocked, r.gross_pnl, r.charges});
    }
    equity.write(folder / "equity.csv");

    CsvWriter(kMargin).write(folder / "margin_daily.csv");  // M4

    write_file_bytes(folder / "metrics.json", dumps_canonical(compute_metrics(e)) + "\n");

    std::string log;
    for (std::size_t i = 0; i < e.log_lines().size(); ++i) {
        log += (i != 0 ? "\n" : "") + e.log_lines()[i];
    }
    if (!e.log_lines().empty()) {
        log += "\n";
    }
    write_file_bytes(folder / "events_log.txt", log);

    std::map<std::string, std::string> hashes;
    for (const char* name : kDeterministic) {
        hashes[name] = sha256_hex(std::string_view(read_all(folder / name)));
    }
    // json.dumps(hashes, indent=2, sort_keys=True) + "\n"
    std::string text = "{\n";
    std::size_t i = 0;
    for (const auto& [name, h] : hashes) {
        text += "  " + json_quote(name) + ": " + json_quote(h) + (++i < hashes.size() ? ",\n" : "\n");
    }
    text += "}\n";
    write_file_bytes(folder / "determinism.json", text);
    return hashes;
}

}  // namespace stocksim
