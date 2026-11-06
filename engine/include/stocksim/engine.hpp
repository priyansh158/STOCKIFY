// The engine (A4 §1): one ordered stream of input records and internal events, processed one at a time.
//
// It mirrors stockify.oracle.runner.Engine handler by handler, because the oracle is the reference
// implementation of A4 and the two must produce identical artifacts. Section by section:
//   M3.3  event loop, clock, phases, timers, bars built from snapshots   (this file, engine.cpp)
//   M3.4  instruments and specs                                         (instruments.hpp)
//   M3.5  orders · M3.6 fills · M3.7 costs · M3.8 ledger, margin, end of day
// Until a section lands, its step inside a handler is a no-op, which is exactly the oracle's behaviour for a
// strategy that places no orders.
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

#include "stocksim/event.hpp"
#include "stocksim/instruments.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/pcg32.hpp"
#include "stocksim/session.hpp"
#include "stocksim/source.hpp"
#include "stocksim/strategy.hpp"

namespace stocksim {

class Engine;

class RunError : public std::runtime_error {
 public:
    explicit RunError(const std::string& what) : std::runtime_error(what) {}
};

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

// Trade evidence between the last two snapshots of a day (A4 §2.1).
struct Evidence {
    Qty dv = 0;
    Paise lo = 0, hi = 0;
    std::optional<Int128> value;  // atp₁·vol₁ − atp₀·vol₀ when valid (lo·ΔV ≤ value ≤ hi·ΔV, ΔV > 0)
    TimeNs t0 = 0, t1 = 0;
};

struct LaterKey {
    bool operator()(const InternalEvent& a, const InternalEvent& b) const { return b.key < a.key; }
};

// ── Context (A2 §2, §4.2): what a strategy can see and do ───────────────────

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

    std::uint64_t schedule_at(TimeNs t);                        // std::invalid_argument unless t > now()
    std::uint64_t schedule_every(TimeNs period, TimeNs first);  // std::invalid_argument unless first > now(), period > 0
    void cancel_timer(std::uint64_t timer_id);

    const Json& params() const;
    void log(std::string_view level, std::string_view msg);  // the level is upper-cased
    Pcg32& rng();                                            // stream 2 (A4 §4.5)

    std::optional<InstrumentId> resolve(std::string_view exchange_symbol) const;  // "NSE:RELIANCE" or "RELIANCE"
    const Spec* spec(InstrumentId iid) const;

 private:
    Engine& e_;
};

// ── The engine ───────────────────────────────────────────────────────────────

class Engine {
 public:
    explicit Engine(Bundle bundle);

    void set_strategy(std::shared_ptr<Strategy> strategy);
    void add_pack(std::shared_ptr<const PackReader> pack);  // packs in bundle order: the pack's src is its index

    void run();                                    // backtest over the added packs, then finish()
    void run(EventSource& source);                 // backtest over any source, then finish()
    void push(std::span<const std::byte> batch);   // live: encoded records, processed up to the batch end (A4 §1.4)
    void finish();                                 // the internal events left after the last input

    const Bundle& bundle() const { return bundle_; }
    const std::vector<std::string>& log_lines() const { return log_lines_; }
    std::vector<std::string> drain_log();          // log lines since the last call
    std::vector<std::string> labels() const;       // sorted
    Context& context() { return ctx_; }
    TimeNs now() const { return now_; }

 private:
    friend class Context;

    // time and phases
    Date date_of(TimeNs t) const { return ist_date(t); }
    std::string_view group_of(InstrumentId iid, std::optional<Date> d = std::nullopt) const;
    std::string_view phase_at(std::string_view group, TimeNs t) const { return calendar_.phase_at(group, t); }

    // the loop
    void start(std::optional<EventKey> first_input);
    void process_internal_before(const std::optional<EventKey>& next_input);
    void process_input(const Event& e);
    void push_internal(InternalEvent ev);
    void schedule_days();
    void activate_due(TimeNs t);  // M3.5

    void log(std::string_view level, std::string_view msg);
    void deliver();  // queued on_order_update / on_fill callbacks (A4 §3.9)
    void track_open();

    // handlers
    void dispatch_internal(const InternalEvent& ev);
    void on_market(const Event& e);
    std::optional<Evidence> evidence(const MarketState& m);  // logs a decreasing day volume
    void match_snapshot(InstrumentId iid, MarketState& m);
    void build_bars(InstrumentId iid, TimeNs t, Paise price, std::optional<Qty> vol, std::optional<Qty> prev_vol);
    void on_gap(const Event& e);
    void on_refdata(const Event& e);
    void on_settle(const Event& e);
    void on_phase(const InternalEvent& ev);
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
    std::shared_ptr<Strategy> strategy_;
    Context ctx_;
    Pcg32 strategy_rng_;
    PackSource packs_;
    LiveBatchSource live_;

    std::priority_queue<InternalEvent, std::vector<InternalEvent>, LaterKey> internal_;
    std::uint64_t internal_seq_ = 0;
    bool started_ = false;
    TimeNs now_ = 0;
    EventKey key_{0, 0, 0, 0};
    TimeNs last_day_end_ = 0;

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

    std::vector<std::string> log_lines_;
    std::size_t log_drained_ = 0;
    std::set<std::string> labels_;
};

}  // namespace stocksim
