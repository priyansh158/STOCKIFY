// The engine (A4): one ordered stream of input records and internal events, processed one at a time.
//
// It mirrors stockify.oracle.runner.Engine handler by handler, because the oracle is the reference
// implementation of A4 and the two must produce identical artifacts; each C++ function names the oracle
// method it mirrors. engine.cpp holds the loop, market state and timers (M3.3); engine_trading.cpp the
// orders (M3.5), fills (M3.6), costs (M3.7, costs.cpp) and the ledger and end of day (M3.8).
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <queue>
#include <set>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "stocksim/costs.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/event.hpp"
#include "stocksim/instruments.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/ledger.hpp"
#include "stocksim/margin.hpp"
#include "stocksim/orders.hpp"
#include "stocksim/pcg32.hpp"
#include "stocksim/session.hpp"
#include "stocksim/source.hpp"
#include "stocksim/strategy.hpp"

namespace stocksim {

class Engine;

// ── Market state (A4 §2) ─────────────────────────────────────────────────────

struct Snapshot {
    TimeNs t = 0;
    Paise ltp = 0;
    Qty volume = 0;
    Paise atp = 0;
    Paise high = 0;
    Paise low = 0;
};

struct BarRow {
    TimeNs start = 0;
    Paise open = 0, high = 0, low = 0, close = 0;
    Qty volume = 0;
    std::int64_t oi = 0;
};

struct MarketState {
    std::optional<Paise> ltp;
    std::optional<TimeNs> t;
    std::optional<Snapshot> s0, s1;
    std::optional<Paise> bid, ask;
    std::optional<std::array<DepthLevel, 10>> book;  // bids 1–5, then asks 1–5
    std::optional<std::pair<Paise, Paise>> band;     // (lower, upper)
    std::optional<std::int32_t> min_interval;        // smallest interval of real bars seen
    std::map<std::int32_t, std::vector<BarRow>> bars;
    Date day = 0;
};

// Trade evidence between the last two snapshots of a day (A4 §2.1).
struct Evidence {
    Qty dv = 0;
    Paise lo = 0, hi = 0;
    std::optional<Int128> value;  // atp₁·vol₁ − atp₀·vol₀ when valid (lo·ΔV ≤ value ≤ hi·ΔV, ΔV > 0)
    TimeNs t0 = 0, t1 = 0;
};

// ── Internal events (A4 §1.2) ────────────────────────────────────────────────

enum class InternalKind : std::uint8_t { Phase, SegClose, MisSqoff, DayEnd, Timer, BarBuilt };

struct InternalEvent {
    EventKey key;
    InternalKind kind = InternalKind::Phase;
    std::string group;    // Phase, SegClose
    std::string phase;    // Phase
    std::string segment;  // MisSqoff
    Date date = 0;        // SegClose, MisSqoff, DayEnd
    std::uint64_t timer = 0;
    InstrumentId instrument = 0;  // BarBuilt
    std::int32_t interval_s = 0;  // BarBuilt
    TimeNs start = 0;             // BarBuilt
};

struct LaterKey {
    bool operator()(const InternalEvent& a, const InternalEvent& b) const { return b.key < a.key; }
};

// ── Context (A2 §2, §4.2): what a strategy can see and do ───────────────────

// An order as a strategy places it: strings as given (validated by the engine, A4 §3.2).
struct PlaceRequest {
    InstrumentId instrument = 0;
    std::string side, product, type;
    Qty qty = 0;
    Paise price = 0;
    Paise trigger = 0;
    std::string validity = "DAY";
    std::optional<Bp> protection;  // nullopt = AUTO
    std::string tag;
};

struct ModifyRequest {
    std::optional<Qty> qty;
    std::optional<Paise> price;
    std::optional<Paise> trigger;
    std::optional<std::string> type;
    std::string detail;  // ",".join(f"{k}={v}" for k, v in sorted(changes.items())), built by the caller
};

class Context {
 public:
    explicit Context(Engine& e) : e_(e) {}

    TimeNs now() const;
    Date today() const;
    std::string phase(InstrumentId iid) const;

    void subscribe_ticks(InstrumentId iid);
    void subscribe_bars(InstrumentId iid, std::int32_t interval_s);
    std::optional<Tick> last_tick(InstrumentId iid) const;
    const std::array<DepthLevel, 10>* depth(InstrumentId iid) const;
    bool stale(InstrumentId iid) const;
    std::vector<Bar> bars(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const;

    // Indicators over the instrument's bars of one interval (A2 §4.3); NaN during warm-up.
    double sma(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const;
    double ema(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const;
    double rsi(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const;
    double atr(InstrumentId iid, std::int32_t interval_s, std::int64_t n) const;
    double session_vwap(InstrumentId iid) const;  // bars only: Σ((H+L+C)//3 · V) / Σ V since today's open

    OrderId place(const PlaceRequest& r);
    bool modify(OrderId id, const ModifyRequest& m);
    bool cancel(OrderId id);
    std::optional<OrderView> order(OrderId id) const;
    std::optional<PositionView> position(InstrumentId iid, std::string_view product) const;
    std::vector<PositionView> positions() const;
    Funds funds() const;

    std::uint64_t schedule_at(TimeNs t);                        // std::invalid_argument unless t > now()
    std::uint64_t schedule_every(TimeNs period, TimeNs first);  // std::invalid_argument unless first > now(), period > 0
    void cancel_timer(std::uint64_t timer_id);

    const Json& params() const;
    void log(std::string_view level, std::string_view msg);  // the level is upper-cased
    Pcg32& rng();                                            // stream 2 (A4 §4.5)

    std::optional<InstrumentId> resolve(std::string_view exchange_symbol) const;  // "NSE:RELIANCE" or "RELIANCE"
    const Spec* spec(InstrumentId iid) const;
    const Json& instrument_rows() const;  // the bundle's rows (the oracle's ctx._rows())

    // F&O lookups over the bundle's rows (the oracle's ctx.expiries / futures_chain / chain).
    std::vector<Date> expiries(std::string_view underlying, std::string_view kind) const;  // "FUT", "OPT", "CE", "PE"
    std::vector<InstrumentId> futures_chain(std::string_view underlying) const;
    struct ChainRow {
        InstrumentId instrument = 0;
        std::string kind;
        Paise strike = 0;
    };
    std::vector<ChainRow> chain(std::string_view underlying, std::string_view expiry) const;

 private:
    Engine& e_;
};

// Rows of the deterministic CSVs other than orders, fills and charges.
struct PositionEodRow {
    Date trade_date = 0;
    InstrumentId instrument = 0;
    Product product = Product::CNC;
    Qty qty = 0;
    Paise avg_price = 0, mark = 0;
    std::string mark_source;
    Paise unrealised = 0, realised = 0;
};

struct EquityRow {
    Date trade_date = 0;
    Paise nav = 0, cash = 0, blocked = 0, gross_pnl = 0, charges = 0;
};

struct SettleClose {
    TimeNs t = 0;
    InstrumentId instrument = 0;
    Product product = Product::CNC;
    Side side = Side::Buy;
    Qty units = 0;
    Paise price = 0;
};

// What happened since the last drain (A2 §2, for the M5 journals): new orders as they are now, and half-open
// ranges into book().order_events / fills / charges, which only ever grow (a bar path's rows are appended).
struct Outputs {
    std::vector<OrderId> orders;
    std::size_t events_from = 0, events_to = 0;
    std::size_t fills_from = 0, fills_to = 0;
    std::size_t charges_from = 0, charges_to = 0;  // charge row_no = index + 1
    std::vector<std::string> log;
};

// ── The engine ───────────────────────────────────────────────────────────────

class Engine {
 public:
    explicit Engine(Bundle bundle);

    void set_strategy(std::shared_ptr<Strategy> strategy);
    const Strategy* strategy() const { return strategy_.get(); }
    void add_pack(std::shared_ptr<const PackReader> pack);  // packs in bundle order: the pack's src is its index

    void run();                                    // backtest over the added packs, then finish()
    void run(EventSource& source);                 // backtest over any source, then finish()
    void push(std::span<const std::byte> batch);   // live: encoded records, processed up to the batch end (A4 §1.4)
    void finish();                                 // the internal events left after the last input

    const Bundle& bundle() const { return bundle_; }
    const std::vector<std::string>& log_lines() const { return log_lines_; }
    std::vector<std::string> drain_log();          // log lines since the last call
    Outputs drain_outputs();                       // everything since the last call (the log included)
    std::vector<std::string> labels() const;       // sorted
    Context& context() { return ctx_; }
    TimeNs now() const { return now_; }

    // Results, for the artifacts (M3.9).
    const Book& book() const { return book_; }
    const InstrumentTable& instruments() const { return instruments_; }
    const std::vector<PositionEodRow>& positions_eod() const { return positions_eod_; }
    const std::vector<EquityRow>& equity() const { return equity_; }
    const std::vector<SettleClose>& settle_closes() const { return settle_closes_; }
    const std::map<std::string, std::int64_t>& rejects() const { return rejects_; }
    Ppm participation_used() const { return participation_used_; }
    std::optional<double> exposure() const;

 private:
    friend class Context;

    // time and phases
    Date date_of(TimeNs t) const { return ist_date(t); }
    std::string_view group_of(InstrumentId iid, std::optional<Date> d = std::nullopt) const;
    std::string_view phase_at(std::string_view group, TimeNs t) const { return calendar_.phase_at(group, t); }
    const Spec* spec(InstrumentId iid, Date d) const { return instruments_.spec_on(iid, d); }
    const Spec& spec_or_throw(InstrumentId iid, Date d) const;

    // the loop
    void start(std::optional<EventKey> first_input);
    void process_internal_before(const std::optional<EventKey>& next_input);
    void process_input(const Event& e);
    void push_internal(InternalEvent ev);
    void schedule_days();
    void close_out();  // the final DAY_END after a last input past the last trading day (D-52)

    void log(std::string_view level, std::string_view msg);
    void deliver();  // queued on_order_update / on_fill callbacks (A4 §3.9)
    void track_open();

    // funds (A4 §6.5)
    std::optional<Paise> mark_price(InstrumentId iid) const;
    std::optional<Paise> ref_price(InstrumentId iid) const;
    Paise blocked(const Ledger& ledger, Date d) const;
    Funds funds(const Ledger& ledger) const;
    bool reduces(InstrumentId iid, Product product, Side side, Qty qty) const;
    Paise required(const Order& o, Paise px) const;

    // orders (A4 §3)
    Bp bp(const Order& o) const;
    Order& reject(Order& o, Reason code);
    OrderId place(const PlaceRequest& r, OrderId parent = 0, bool system = false);
    std::optional<Reason> validate(Order& o, bool system, Reason start, Reason upto);
    Paise order_px(const Order& o) const;
    void accept(Order& o);
    std::vector<OrderId> live_orders();
    void activate_due(TimeNs t);
    void activate(Order& o);
    bool modify(OrderId id, const ModifyRequest& m);
    bool cancel(OrderId id, Reason reason);
    void cancel_in(Book& book, Order& o, Reason reason);
    void event(Book& book, const Order& o, std::string_view event, std::string detail = "");
    OrderView view_of(const Order& o) const;

    // fills (A4 §4) and completion (§5.4)
    CashStyle style(InstrumentId iid, Product product) const;
    std::optional<Ppb> cnc_credit_ppb() const;
    void fill(Book& book, Order& o, Qty qty, Paise price, const std::string& evidence, TimeNs t);
    void charge_order(Book& book, Order& o);
    void charge_row(Book& book, const std::string& kind, OrderId order_id, InstrumentId iid, Date d,
                    const ChargeParts& parts);
    std::vector<OrderId> eligible(InstrumentId iid, TimeNs t_start);
    bool band_ok(InstrumentId iid, Paise price) const;
    void simulate_bar(Book& book, InstrumentId iid, const BarFields& bar, TimeNs t, bool high_first,
                      const std::vector<OrderId>& eligible_ids, Paise tick, Qty lot);
    void match_bar(InstrumentId iid, const BarFields& bar, TimeNs t);
    void adopt(Book& sim);
    void match_eod(InstrumentId iid, const BarFields& bar, TimeNs t);
    void after_market(InstrumentId iid);

    // handlers
    void dispatch_internal(const InternalEvent& ev);
    void on_market(const Event& e);
    std::optional<Evidence> evidence(const MarketState& m);  // logs a decreasing day volume
    void match_snapshot(InstrumentId iid, MarketState& m);
    void build_bars(InstrumentId iid, TimeNs t, Paise price, std::optional<Qty> vol, std::optional<Qty> prev_vol);
    void on_gap(const Event& e);
    void on_refdata(const Event& e);
    void on_settle(const Event& e);
    std::vector<std::size_t> expiring(InstrumentId underlying_id, Date d);
    void settle_final(InstrumentId underlying_id, Date d, Paise fsp, const std::string& source);
    void settle_physical(InstrumentId stock, Date d, Paise fsp, const std::string& source,
                         std::vector<std::size_t> idx);
    void on_phase(const InternalEvent& ev);
    bool daily_bars(InstrumentId iid) const;
    void seg_close(const InternalEvent& ev);
    void mis_sqoff(const InternalEvent& ev);
    void day_end(const InternalEvent& ev);
    void on_timer(const InternalEvent& ev);
    void on_built(const InternalEvent& ev);

    Bundle bundle_;
    SessionCalendar calendar_;
    InstrumentTable instruments_;
    TimeNs stale_after_ns_ = 0;
    std::string fill_kind_;  // bar | snapshot_ltp | snapshot_quote | eod_close
    Json fill_cfg_;
    Json policies_;
    std::string mark_kind_;  // ltp | mid
    TimeNs latency_ns_ = 0;
    Costs costs_;
    FlatMargin margin_;
    std::shared_ptr<Strategy> strategy_;
    Context ctx_;
    Pcg32 strategy_rng_;
    Book book_;
    PackSource packs_;
    LiveBatchSource live_;

    std::priority_queue<InternalEvent, std::vector<InternalEvent>, LaterKey> internal_;
    std::uint64_t internal_seq_ = 0;
    bool started_ = false;
    bool closed_out_ = false;
    TimeNs now_ = 0;
    EventKey key_{0, 0, 0, 0};
    TimeNs last_day_end_ = 0;

    OrderId next_order_ = 1;
    OrderId min_live_ = 1;  // orders below this id are all final
    std::map<InstrumentId, MarketState> market_;
    std::set<InstrumentId> sub_ticks_;
    std::map<InstrumentId, std::set<std::int32_t>> sub_bars_;
    std::map<std::pair<InstrumentId, std::int32_t>, std::map<TimeNs, std::array<std::int64_t, 6>>> built_;
    std::map<std::uint64_t, std::pair<TimeNs, TimeNs>> timers_;  // id → (next time, period or 0)
    std::uint64_t next_timer_ = 1;
    std::map<std::pair<InstrumentId, Date>, Paise> fsp_seen_;
    std::map<InstrumentId, Paise> underlying_last_;
    std::uint64_t event_no_ = 0;
    bool delivering_ = false;

    std::vector<PositionEodRow> positions_eod_;
    std::vector<EquityRow> equity_;
    std::vector<SettleClose> settle_closes_;
    std::map<std::string, std::int64_t> rejects_;
    Ppm participation_used_ = 0;  // ppm of a bar's volume, the most over the run
    std::vector<std::pair<TimeNs, bool>> open_changes_;  // (t, any position open) transitions

    std::vector<std::string> log_lines_;
    std::size_t log_drained_ = 0;
    OrderId orders_drained_ = 1;  // the next order id not yet drained
    std::size_t events_drained_ = 0, fills_drained_ = 0, charges_drained_ = 0;
    std::set<std::string> labels_;
};

}  // namespace stocksim
