// The engine (A4): one ordered stream of input records and internal events, processed one at a time.
//
// It mirrors stockify.oracle.runner.Engine handler by handler, because the oracle is the reference
// implementation of A4 and the two must produce identical artifacts; each C++ function names the oracle
// method it mirrors. engine.cpp holds the loop, market state and timers (M3.3); engine_trading.cpp the
// orders (M3.5), fills (M3.6), costs (M3.7, costs.cpp) and the ledger and end of day (M3.8).
#pragma once

#include <array>
#include <atomic>
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
struct SodState;  // sod_state.cpp: the capture / restore helpers, which see the engine's internals

// The day-to-day state (sod_state.hpp, VOL 5.3): written and applied by these, which see the engine's internals.
std::string eod_state_json(const Engine& engine);
void load_sod_state(Engine& engine, std::string_view text);
void load_sod_state_json(Engine& engine, const Json& state);

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
    std::optional<std::int64_t> oi;                  // the last TICK_FULL or bar that carried it (M4.4)
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

    // The option-chain view (M4.4, C-13), field for field the oracle's: quotes, IVs (bid / ask / mid) and the
    // Greeks at the mid IV, in rupees; the forward and its source; the ATM strike.
    struct ChainRow {
        InstrumentId instrument = 0;
        std::string kind;
        Paise strike = 0;
        std::optional<Paise> bid, ask;
        std::optional<double> mid;  // paise, (bid + ask) / 2
        std::optional<Paise> ltp;
        bool stale = true;
        std::optional<std::int64_t> oi;
        std::optional<double> iv_bid, iv_ask, iv_mid;
        std::optional<std::string> iv_reason;  // why iv_mid is missing although there is a mid
        std::optional<double> delta, gamma, vega, theta;
    };
    struct ChainView {
        std::vector<ChainRow> rows;
        std::optional<double> forward;  // paise
        std::optional<std::string> forward_source;  // future | synthetic | forward_from_index
        std::optional<Paise> atm_strike;
    };
    // Cached per engine step (the event key); deterministic.
    ChainView chain(std::string_view underlying, std::string_view expiry) const;

 private:
    ChainView compute_chain(std::string_view underlying, std::string_view expiry) const;
    std::optional<double> quote_mid(InstrumentId iid) const;  // (bid + ask) / 2 in paise, both sides quoted

 public:

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
    std::optional<Paise> underlying_mark;  // D-55: a derivative's underlying mark at day end, else none
};

struct MarginDailyRow {  // margin_daily.csv (M4.8)
    Date trade_date = 0;
    InstrumentId underlying = 0;
    std::string model;
    std::string step;  // the staircase step that priced it (MEZ 4.9): B1..B5 for staircase / span_exact, B3/B4 spanlite
    Paise scan = 0, somc = 0, elm = 0, addons = 0, total = 0;
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
    // Applies the bundle's `sod_state` (A1 §6.4) when it has one; the strategy's part waits for set_strategy.
    explicit Engine(Bundle bundle);

    // Hands a resumed strategy its saved state (A4 §1.10); RunError when its load_state fails.
    // `name` is what a C++ strategy's failures are called (a Python strategy's trampoline names itself).
    void set_strategy(std::shared_ptr<Strategy> strategy, std::string_view name = {});
    const Strategy* strategy() const { return strategy_.get(); }
    void add_pack(std::shared_ptr<const PackReader> pack);  // packs in bundle order: the pack's src is its index

    void run();                                    // backtest over the added packs, then finish()
    void run(EventSource& source);                 // backtest over any source, then finish()
    void push(std::span<const std::byte> batch);   // live: encoded records, processed up to the batch end (A4 §1.4)
    void finish();                                 // the internal events left after the last input

    // A paper session's day-to-day state (sod_state.hpp). Both hold the engine's busy guard, so a strategy callback
    // that calls back into them is refused, and a refused load leaves the engine as it was.
    void load_state_text(std::string_view text);   // before the first event, at most once
    std::string eod_state() const;                 // after a day has ended and nothing has been processed since

    // One call at a time, and nothing after a failure (M3 review): run / push / finish refuse while another is in
    // progress (another thread, the GIL released for a C++ strategy, or a strategy calling back in), and once an
    // exception has left one of them the engine refuses them all. Readers check with ensure_idle; the log stays
    // readable after a failure (allow_failed), so the traceback can be written out.
    bool failed() const { return failed_; }
    void ensure_idle(const char* what, bool allow_failed = false) const;  // RunError if busy (or failed)

    const Bundle& bundle() const { return bundle_; }
    const std::vector<std::string>& log_lines() const { return log_lines_; }
    std::vector<std::string> drain_log();          // log lines since the last call
    Outputs drain_outputs();                       // everything since the last call (the log included)
    std::vector<std::string> labels() const;       // sorted
    Context& context() { return ctx_; }
    TimeNs now() const { return now_; }

    // Results, for the artifacts (M3.9).
    const Book& book() const { return book_; }
    // Final orders loaded from a sod_state: this day's orders.csv leaves them out (A1 §6.4 `final_orders`).
    const std::set<OrderId>& prior_final() const { return prior_final_; }
    std::int64_t charge_row_base() const { return charge_row_base_; }  // charges.csv rows written on earlier days
    const InstrumentTable& instruments() const { return instruments_; }
    const std::vector<PositionEodRow>& positions_eod() const { return positions_eod_; }
    const std::vector<EquityRow>& equity() const { return equity_; }
    const std::vector<MarginDailyRow>& margin_daily() const { return margin_daily_; }
    const std::vector<SettleClose>& settle_closes() const { return settle_closes_; }
    const std::map<std::string, std::int64_t>& rejects() const { return rejects_; }
    Ppm participation_used() const { return participation_used_; }
    std::optional<double> exposure() const;

 private:
    friend class Context;
    friend struct SodState;
    friend std::string eod_state_json(const Engine& engine);
    friend void load_sod_state(Engine& engine, std::string_view text);
    friend void load_sod_state_json(Engine& engine, const Json& state);

    // time and phases
    Date date_of(TimeNs t) const { return ist_date(t); }
    std::string_view group_of(InstrumentId iid, std::optional<Date> d = std::nullopt) const;
    std::string_view phase_at(std::string_view group, TimeNs t) const { return calendar_.phase_at(group, t); }
    // A day's CONTINUOUS start, over the bundle's days and its prev_days / next_days (runner.continuous_start): a
    // bar dated an earlier day (a daily bar at the next midnight) is matched against its own session (A1 §6.2).
    std::optional<TimeNs> continuous_start(std::string_view group, Date d) const {
        return cal_all_.continuous_start(group, d);
    }
    static SessionCalendar all_days(const Json& root, const SessionCalendar& days);  // prev_days + days + next_days
    // save_state() as A1 §6.4 embeds it: parsed, a JSON object; RunError (naming the strategy when it can) otherwise.
    std::string checked_strategy_state() const;
    const Spec* spec(InstrumentId iid, Date d) const { return instruments_.spec_on(iid, d); }
    const Spec& spec_or_throw(InstrumentId iid, Date d) const;

    // the loop
    void start(std::optional<EventKey> first_input);
    void process_internal_before(const std::optional<EventKey>& next_input);
    void process_input(const Event& e);
    void push_internal(InternalEvent ev);
    void schedule_days();
    void close_out();
    void reseed_for(TimeNs t);  // seed_mode per_day: re-create both streams at a seeded day's first event  // the final DAY_END after a last input past the last trading day (D-52)

    void log(std::string_view level, std::string_view msg);
    void deliver();  // queued on_order_update / on_fill callbacks (A4 §3.9); held while the strategy is disabled
    bool hook_on(std::string_view hook);  // false (and a log line) while the strategy is disabled (A4 §3.11)
    void on_strategy_control(std::string_view json);  // a journaled pause or resume (D-70)
    void track_open();

    // funds (A4 §6.5)
    std::optional<Paise> mark_price(InstrumentId iid) const;
    std::optional<Paise> ref_price(InstrumentId iid) const;
    // The margin model's portfolio (D-80): NRML positions and MIS futures and options, one net NRML leg each.
    std::vector<MarginLeg> fo_book(const Ledger& ledger) const;
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
    SessionCalendar calendar_;  // the bundle's days: processed (phases, DAY_END), seeded
    SessionCalendar cal_all_;   // with prev_days and next_days (A1 §6.2): calendar only, never processed
    bool day_bundle_ = false;   // a paper session's day bundle (prev_days or next_days present)
    InstrumentTable instruments_;
    TimeNs stale_after_ns_ = 0;
    std::string fill_kind_;  // bar | snapshot_ltp | snapshot_quote | eod_close
    Json fill_cfg_;
    Json policies_;
    std::string mark_kind_;  // ltp | mid
    TimeNs latency_ns_ = 0;
    Costs costs_;
    std::unique_ptr<MarginModel> margin_;  // flat (T0), spanlite (T1, M4.8), staircase / span_exact (MEZ 4.10)
    std::shared_ptr<Strategy> strategy_;
    Context ctx_;
    Pcg32 strategy_rng_;
    Book book_;
    std::vector<std::pair<Date, std::uint64_t>> day_seeds_;  // (IST date, days[].seed), in day order (VOL 5.1)
    std::size_t next_day_seed_ = 0;
    PackSource packs_;
    LiveBatchSource live_;

    std::priority_queue<InternalEvent, std::vector<InternalEvent>, LaterKey> internal_;
    std::uint64_t internal_seq_ = 0;
    bool started_ = false;
    mutable std::atomic<bool> busy_{false};
    bool failed_ = false;
    class Running;  // the guard around run / push / finish
    class Scope;    // the busy guard around the state calls (it does not poison the engine when one refuses)
    bool closed_out_ = false;
    TimeNs now_ = 0;
    EventKey key_{0, 0, 0, 0};
    TimeNs last_day_end_ = 0;

    OrderId next_order_ = 1;
    std::vector<OrderId> live_ids_;  // ids of the orders that are not final, ascending (never a walk over an id range)
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

    // Day to day (VOL 5.3, A4 §1.10): see sod_state.hpp.
    bool resumed_ = false;                                  // a sod_state was applied: no on_start
    bool settled_ = false;                                  // a day has ended and nothing was processed since
    std::vector<InternalEvent> carried_;                    // internal events passed over after the last DAY_END
    std::set<OrderId> prior_final_;                         // final orders loaded from the state
    std::int64_t charge_row_base_ = 0;                      // charges.csv rows written on earlier days
    std::optional<Date> state_date_;                        // the last bundle trading day whose DAY_END ran
    std::optional<std::string> day_end_strategy_state_;     // save_state() at DAY_END, checked (day bundles only)
    std::optional<std::string> pending_strategy_state_;     // loaded before set_strategy: handed over then
    std::string strategy_name_;                             // a C++ strategy's registered name, for its refusals
    bool strategy_enabled_ = true;                          // D-70, A4 §3.11: a journaled switch, carried day to day
    std::set<std::string> pause_reasons_;
    Json queued_notifications_ = Json::array();

    std::vector<PositionEodRow> positions_eod_;
    std::vector<EquityRow> equity_;
    std::vector<MarginDailyRow> margin_daily_;
    std::vector<SettleClose> settle_closes_;
    std::map<std::string, std::int64_t> rejects_;
    Ppm participation_used_ = 0;  // ppm of a bar's volume, the most over the run
    std::vector<std::pair<TimeNs, bool>> open_changes_;  // (t, any position open) transitions

    std::vector<std::string> log_lines_;
    std::size_t log_drained_ = 0;
    std::optional<EventKey> chain_key_;  // the step the chain cache belongs to
    std::map<std::pair<std::string, std::string>, Context::ChainView> chain_cache_;
    OrderId orders_drained_ = 1;  // the next order id not yet drained
    std::size_t events_drained_ = 0, fills_drained_ = 0, charges_drained_ = 0;
    std::set<std::string> labels_;
};

}  // namespace stocksim
