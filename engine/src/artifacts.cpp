#include "stocksim/artifacts.hpp"

#include <fstream>
#include <iterator>

#include "stocksim/format.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/metrics.hpp"
#include "stocksim/sha256.hpp"

namespace stocksim {

namespace {

// Column headers of the other files, as stockify.oracle.artifacts writes them (A1 §6.3).
constexpr const char* kPositions =
    // <block name="positions-eod-columns" same-as="stockify/oracle/artifacts.py:positions-eod-columns" same-as-mode="sequence" same-as-pattern='(?P<value>[a-z][a-z_]*)'>
    "trade_date,instrument_id,product,qty,avg_price_paise,mark_paise,mark_source,unrealised_paise,realised_paise,"
    "underlying_mark_paise";
// </block>
constexpr const char* kEquity = "trade_date,nav_paise,cash_paise,margin_blocked_paise,gross_pnl_paise,charges_paise";
constexpr const char* kMargin =
    // <block name="margin-daily-columns" same-as="stockify/oracle/artifacts.py:margin-daily-columns" same-as-mode="sequence" same-as-pattern='(?P<value>[a-z][a-z_]*)'>
    "trade_date,underlying_id,model,step,scan,somc,elm,addons,total";
// </block>

// The ✔ rows of A1 §6.3, in order (checked by blockwatch).
// <block name="deterministic-files" same-as="docs/plan/A1_schemas.md:deterministic-files" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+\.[a-z]+)"'>
constexpr const char* kDeterministic[] = {"orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
                                          "margin_daily.csv", "metrics.json", "events_log.txt"};
// </block>

std::string read_all(const std::filesystem::path& p) {
    std::ifstream in(p, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
}

}  // namespace

std::map<std::string, std::string> write_artifacts(const Engine& e, const std::filesystem::path& folder) {
    e.ensure_idle("write_artifacts");  // never the half-done state of a failed run (M3 review)
    std::filesystem::create_directories(folder);
    const Book& b = e.book();

    const auto into = [](CsvWriter& w) { return [&w](std::initializer_list<CsvCell> cells) { w.add_row(cells); }; };

    CsvWriter orders{std::string(kOrdersHeader)};
    for (const auto& [id, o] : b.orders) {  // std::map: order_id order; this day's orders (runner.day_orders)
        if (e.prior_final().count(id) == 0) {
            order_row(o, into(orders));
        }
    }
    orders.write(folder / "orders.csv");

    CsvWriter events{std::string(kOrderEventsHeader)};
    for (const OrderEventRow& r : b.order_events) {
        order_event_row(r, into(events));
    }
    events.write(folder / "order_events.csv");

    CsvWriter fills{std::string(kFillsHeader)};
    for (const Fill& f : b.fills) {
        fill_row(f, into(fills));
    }
    fills.write(folder / "fills.csv");

    CsvWriter charges{std::string(kChargesHeader)};
    std::int64_t n = e.charge_row_base();  // rows continue across a paper session's days (VOL 5.2)
    for (const ChargeRow& c : b.charges) {
        charge_row(++n, c, into(charges));
    }
    charges.write(folder / "charges.csv");

    CsvWriter positions(kPositions);
    for (const PositionEodRow& r : e.positions_eod()) {
        const std::string d = iso_date(r.trade_date);
        positions.add_row({std::string_view(d), std::int64_t{r.instrument}, to_string(r.product), r.qty, r.avg_price,
                           r.mark, std::string_view(r.mark_source), r.unrealised, r.realised,
                           r.underlying_mark ? CsvCell(*r.underlying_mark) : CsvCell(CsvNull{})});
    }
    positions.write(folder / "positions_eod.csv");

    CsvWriter equity(kEquity);
    for (const EquityRow& r : e.equity()) {
        const std::string d = iso_date(r.trade_date);
        equity.add_row({std::string_view(d), r.nav, r.cash, r.blocked, r.gross_pnl, r.charges});
    }
    equity.write(folder / "equity.csv");

    CsvWriter margin(kMargin);
    for (const MarginDailyRow& r : e.margin_daily()) {
        const std::string d = iso_date(r.trade_date);
        margin.add_row({std::string_view(d), std::int64_t{r.underlying}, std::string_view(r.model),
                        std::string_view(r.step), r.scan, r.somc, r.elm, r.addons, r.total});
    }
    margin.write(folder / "margin_daily.csv");

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
