# A2 — Interfaces: C++ engine, Python binding, strategy API, REST

Sketches, not final code: names, responsibilities and signatures that the milestones rely on. Behaviour is defined in `A4_engine_semantics.md`; data formats in `A1_schemas.md`. Keep public names stable once M3 ships; Python strategies depend on them.

---

## §1 C++ layout (`engine/`)

```
engine/
  CMakeLists.txt  CMakePresets.json            (configure, build and test presets)
  include/stocksim/
    types.hpp  int128.hpp  decimal.hpp  pcg32.hpp  sha256.hpp  format.hpp  json_out.hpp  json_bundle.hpp
    event.hpp  pack_reader.hpp  source.hpp
    session.hpp  instruments.hpp  market_state.hpp  bar_builder.hpp
    orders.hpp  fills/fill_model.hpp  costs.hpp  ledger.hpp
    margin/margin_model.hpp  pricing/black76.hpp  chain.hpp
    indicators.hpp  strategy.hpp  context.hpp  engine.hpp  artifacts.hpp
  src/ ...            (one .cpp per header where needed; fills/, margin/, pricing/ subfolders)
  strategies/         sma_cross_cpp.cpp  orb_cpp.cpp  (registered C++ strategies)
  bindings/module.cpp (pybind11 → stockify._engine)
  tools/stocksim_run.cpp
  tests/              doctest files, one per module
  third_party/        doctest.h  nlohmann/json.hpp  picosha2.h  VERSIONS.md
```

- `int128.hpp`: portable 64×64→128 multiply, 128÷64 divide, floor/ceil/round-half-up helpers (A4 §0.2); MSVC intrinsics (`_umul128`, `_udiv128`) or a two-limb implementation (Windows only, D-26).
- `decimal.hpp`: exact decimal-string → ppb/ppm/bp/paise conversion (A4 §0.1); never via `double`.
- `format.hpp` / `json_out.hpp`: the canonical float format and JSON writer of A4 §9.2. nlohmann/json is used only for *reading* bundles.

## §2 Core C++ types and interfaces

```cpp
namespace stocksim {

using Paise = std::int64_t;  using Qty = std::int64_t;  using TimeNs = std::int64_t;
using InstrumentId = std::uint32_t;  using OrderId = std::uint64_t;  using Date = std::int32_t; // YYYYMMDD
using Ppb = std::int64_t;  using Ppm = std::int64_t;  using Bp = std::int32_t;

enum class Side : std::uint8_t { Buy, Sell };
enum class Product : std::uint8_t { CNC, MIS, NRML };
enum class OrderType : std::uint8_t { Market, Limit, SL, SLM };
enum class Validity : std::uint8_t { Day, IOC };
enum class OrderStatus : std::uint8_t { Pending, Open, TriggerPending, Complete, Cancelled, Rejected, Sliced };
enum class Reason : std::uint16_t { None, Instrument, Product, Session, Cas, QtyLot, Tick, Band, Freeze,
                                    Holdings, NoReference, Margin, Eod, Ioc, User, Rms };
enum class EventKind : std::uint16_t { TickQuote = 1, TickFull = 2, Index = 3, Bar = 4, Clock = 5,
                                       Gap = 6, RefData = 7, Settle = 8 };

struct Event {                       // a view into a pack/journal record (A1 §5)
  EventKind kind; InstrumentId instrument; TimeNs t; std::uint64_t seq; std::uint32_t src;
  const std::byte* payload; std::uint16_t size;
};

class EventSource {                  // PackSource (one or many packs), VectorSource, LiveBatchSource
 public:
  virtual ~EventSource() = default;
  virtual const Event* next() = 0;                 // nullptr at end (or at batch end for live)
  virtual std::optional<EventKey> peek_key() const = 0;   // (t, rank, src, seq) of the next record
};

struct Protection { bool is_auto = true; Bp bp = 0; };   // A4 §3.3
struct OrderRequest {
  InstrumentId instrument; Side side; Qty qty; Product product; OrderType type;   // type is required
  Paise price = 0; Paise trigger = 0; Validity validity = Validity::Day;
  Protection protection;  std::string tag;
};
struct OrderModify { std::optional<Qty> qty; std::optional<Paise> price; std::optional<Paise> trigger;
                     std::optional<OrderType> type; };

class FillModel {                    // bar | snapshot_ltp | snapshot_quote | eod_close (A4 §4)
 public:
  virtual ~FillModel() = default;
  virtual void on_market_event(const Event& e, const MarketState& m, OrderBook& book, FillSink& out) = 0;
  virtual std::string_view name() const = 0;
  virtual bool approximate() const = 0;            // drives labels (I-14)
};

struct MarginResult { Paise total; Paise scan; Paise somc; Paise elm; Paise addons; std::string label; };
class MarginModel {                  // flat (T0) | spanlite (T1) | span (T2) | live_with_fallback
 public:
  virtual ~MarginModel() = default;
  virtual MarginResult requirement(const PortfolioView& p, const MarketView& m, TimeNs now) const = 0;
};

class CostEngine {                   // A4 §5; rows from the bundle, rates in ppb
 public:
  ChargeBreakdown charge_order(const ExecutedOrder& o, Date trade_date) const;
  std::vector<ChargeRow> charge_settlement(const SettlementEvent& s) const;  // exercise STT/brokerage, physical delivery
};

class Strategy {
 public:
  virtual ~Strategy() = default;
  virtual void on_start(Context&) {}
  virtual void on_bar(Context&, const Bar&) {}
  virtual void on_tick(Context&, const Tick&) {}
  virtual void on_timer(Context&, std::uint64_t timer_id) {}
  virtual void on_fill(Context&, const Fill&) {}
  virtual void on_order_update(Context&, const OrderView&) {}
  virtual void on_session_phase(Context&, const PhaseChange&) {}
  virtual void on_session_end(Context&) {}
  virtual std::string save_state() const { return "{}"; }
  virtual void load_state(std::string_view) {}
};

class Context {
 public:
  // time & session
  TimeNs now() const;  Date today() const;  std::string_view phase(InstrumentId) const;
  // market data
  void subscribe_ticks(InstrumentId);  void subscribe_bars(InstrumentId, int interval_s);
  const Tick* last_tick(InstrumentId) const;  const Depth* depth(InstrumentId) const;
  bool stale(InstrumentId) const;  BarSeries bars(InstrumentId, int interval_s, int n) const;
  double sma(InstrumentId, int interval_s, int n) const;   // also ema, rsi, atr, session_vwap (§4.3)
  // orders & portfolio
  OrderId place(const OrderRequest&);  bool modify(OrderId, const OrderModify&);  bool cancel(OrderId);
  const OrderView* order(OrderId) const;  const Position* position(InstrumentId, Product) const;
  std::vector<Position> positions() const;  Funds funds() const;
  // timers, params, logging, randomness
  std::uint64_t schedule_at(TimeNs);  std::uint64_t schedule_every(TimeNs period, TimeNs first);
  void cancel_timer(std::uint64_t);  const nlohmann::json& params() const;
  void log(LogLevel, std::string_view);  Pcg32& rng();     // stream 2 (A4 §4.5): below(n), random()
  // reference data, F&O (M4)
  InstrumentId resolve(std::string_view exchange_symbol) const;   // "NSE:RELIANCE"
  const Spec& spec(InstrumentId) const;
  std::vector<Date> expiries(std::string_view underlying, Kind) const;
  std::vector<InstrumentId> futures_chain(std::string_view underlying) const;
  ChainView chain(std::string_view underlying, Date expiry);      // forward, ATM, per-strike quotes + Greeks
};

class Engine {
 public:
  explicit Engine(const Bundle&);
  void set_strategy(std::unique_ptr<Strategy>);
  void load_sod_state(std::string_view json);      // M5.4
  void run(EventSource&);                          // backtest
  void push(std::span<const std::byte> batch);     // live: encoded records (A1 §5, no header)
  void finish();                                   // processes remaining internal events (A4 §1.4)
  Outputs drain_outputs();                         // new orders, order events, fills, charges, logs since last call
  std::string eod_state() const;
  void write_artifacts(const std::filesystem::path&) const;
  std::vector<std::string> labels() const;
};

}  // namespace stocksim

#define STOCKSIM_STRATEGY(Class, Name)  /* registers a factory under Name at static-init time */
```

## §3 Python binding (`stockify._engine`)

```python
build_info() -> dict                       # engine_build_id, git_sha, dirty, compiler, flags_hash, python_version

class Engine:
    def __init__(self, bundle_path: str) -> None
    def add_pack(self, path: str) -> None
    def set_strategy_cpp(self, name: str) -> None            # params come from the bundle
    def set_strategy_py(self, strategy: "stockify.strategy.Strategy") -> None
    def load_sod_state(self, state_json: str) -> None
    def run(self) -> None
    def push(self, batch: bytes) -> None
    def finish(self) -> None
    def drain_outputs(self) -> dict          # {"orders": [...], "order_events": [...], "fills": [...], "charges": [...], "log": [...]}
    def eod_state(self) -> str
    def write_artifacts(self, folder: str) -> None
    def labels(self) -> list[str]
```

- With a Python strategy set, `run`/`push` hold the GIL (callbacks need it). With a C++ strategy, release it (`py::gil_scoped_release`) so daemon threads keep moving.
- A Python exception in a hook aborts the run: the traceback goes to `events_log.txt`, status `failed` (M3.10).
- `stockify/engine.py` wraps the module: version check (M0.3), bundle/pack helpers, `run_folder(config) -> path`.

## §4 Python strategy API (`stockify/strategy.py`)

The same API is implemented by the oracle (M2) and the engine (M3). A strategy written once runs on both.

### 4.1 Base class

```python
class Strategy:
    def on_start(self, ctx): ...
    def on_bar(self, ctx, bar): ...                 # bar.instrument, .interval_s, .start_ns, .t_ns, .open/.high/.low/.close (paise), .volume (units, never contracts: D-79), .oi (units)
    def on_tick(self, ctx, tick): ...               # tick.instrument, .t_ns, .ltp, .volume, .atp, .bid/.ask (FULL only), .stale
    def on_timer(self, ctx, timer_id): ...
    def on_fill(self, ctx, fill): ...               # fill.order_id, .instrument, .side, .qty, .price, .t_ns, .evidence
    def on_order_update(self, ctx, order): ...      # order.status, .reason, .filled_qty, .avg_price
    def on_session_phase(self, ctx, change): ...    # change.group, .phase, .t_ns
    def on_session_end(self, ctx): ...              # at DAY_END (A4 §1.10)
    def save_state(self) -> dict: ...              # default: every attribute, encoded (below)
    def load_state(self, state: dict) -> None: ... # default: every saved attribute set back
```

**Day-to-day state (paper sessions, D-66, VOL 5.2).** A paper session runs one engine per trading day; `on_start`
runs only on its first day. Each later day starts from `load_state(state)` with what `save_state()` returned at the
previous day's `DAY_END` (A4 §1.10; the subscriptions, timers and orders come back from the engine's own state). So
**everything a strategy sets in `on_start` or changes later must round-trip through `save_state` / `load_state`**,
or the strategy breaks (or silently differs) on day 2.
- The default `save_state()` snapshots the instance's own attributes (`self.state_attrs()`, sorted by name) with
  `stockify.strategy.encode_state`: int, bool, str and None as they are; `Decimal` exactly, as `{"$decimal": "<text>"}`;
  a tuple as `{"$tuple": [...]}` (restored as a tuple); lists and dicts with string keys (not starting with `$`) of
  these. Any other value (a float, a `date`, an int-keyed dict, an object) raises `StrategyStateError` naming the class
  and the attribute: implement `save_state` / `load_state` for it. The default `load_state` decodes and sets each one
  back (`decode_state`). `state_attrs()` exists because the source guard refuses `vars()`, `setattr()` and
  `getattr()` in strategy code: an override calls it to extend the default and `super().load_state(state)` to restore it.
- An override may return any JSON object of JSON-typed values: null, bool, int, finite floats (canonical JSON gives
  them back exactly), str, lists, objects with string keys. The engine checks it at each `DAY_END` of a paper day and
  fails the day there, naming the strategy and the bad value (NaN, Infinity, a tuple, a non-string key, any other type).
  An exception raised inside `save_state` or `load_state` fails the run as `RunError` naming the strategy, the
  exception type and its message.
- Shipped strategies: the default covers `sma_cross`, `buy_and_hold` (a `Decimal`) and `fuzz.RandomTrader`.
  `opening_range_breakout` and `eod_short_straddle` override it to store a `date` as ISO text; `_option_legs`
  (`short_straddle_stop`, `iron_fly`) and `_script` to store int-keyed dicts as `[key, value]` pairs; `_margin_watch`
  to re-load its wrapped strategy by ref and hand it its own state. Each is tested split against whole
  (`tests/stockify/test_sod_state.py`).
- A C++ strategy's `save_state()` returns JSON text for an object; `load_state` gets the same text back.

### 4.2 Context (snake_case mirror of §2)

`now()`, `today()`, `phase(iid)`, `subscribe_ticks(iid)`, `subscribe_bars(iid, interval_s)`, `last_tick(iid)`, `depth(iid)`, `stale(iid)`, `bars(iid, interval_s, n)`, `sma/ema/rsi/atr(iid, interval_s, n)`, `session_vwap(iid)`, `place(instrument, side, qty, product, type, price=0, trigger=0, validity="DAY", protection="AUTO", tag="")` (**`type` is required**; protection is `"AUTO"` or an integer number of basis points), `modify(order_id, **changes)`, `cancel(order_id)`, `order(order_id)`, `position(iid, product)`, `positions()`, `funds()`, `schedule_at(t_ns)`, `schedule_every(period_ns, first_ns)`, `cancel_timer(id)`, `params` (dict), `log(level, msg)`, `rng()` (`.random()` in [0, 1), `.below(n)` in [0, n)), `resolve("NSE:RELIANCE")`, `spec(iid)`, `expiries(underlying, kind)`, `futures_chain(underlying)`, `chain(underlying, expiry)`. Money is in paise (`int`); helpers `rupees(paise)` and `paise("123.45")` exist for readability.

### 4.3 Indicator definitions (identical in both implementations; A3 U-05)

Inputs are paise values; arithmetic in `double` **in exactly this expression order** (A4 §0.3). `NaN` until enough samples.
- `SMA(n)`: keep the exact `int64` sum of the last n values; `sma = double(sum) / n`.
- `EMA(n)`: `alpha = 2.0 / (n + 1.0)`; seed `ema` with the SMA of the first n values; then `ema = ema + alpha * (x - ema)`.
- `RSI(n)` (Wilder): seed `avg_gain`/`avg_loss` with the simple means of the first n changes; then `avg = (avg * (n - 1) + x) / n` for each; `rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)`, and 100.0 when `avg_loss == 0`.
- `ATR(n)` (Wilder): true range `tr = max(H - L, |H - C_prev|, |L - C_prev|)` in integers; seed with the simple mean of the first n TRs; then `atr = (atr * (n - 1) + tr) / n`.
- `session_vwap`: integer (128-bit) sums of `price × volume` and `volume` since the group's `CONTINUOUS` start, using interval VWAP values for ticks (A4 §2.1, falling back to `ltp` when invalid) or `(H + L + C) / 3` (integer floor division) for bars; `vwap = double(sum_pv) / double(sum_v)`.

### 4.4 Rules for strategy code (checked when a strategy loads)

A strategy module may not import or call: `time`, `datetime.now/today/utcnow`, `random`, `secrets`, `os`, `subprocess`, `socket`, `requests`/`httpx`, file I/O. An AST check enforces this allow-list at load (C-9, I-2). Use `ctx.now()`, `ctx.rng()` and `ctx.log()` instead.

### 4.5 Example

```python
from stockify.strategy import Strategy

class SmaCross(Strategy):
    """Long when fast SMA > slow SMA on daily bars; flat otherwise. Params: symbol, fast, slow."""
    def on_start(self, ctx):
        self.iid = ctx.resolve(ctx.params["symbol"])           # e.g. "NSE:RELIANCE"
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):
        fast = ctx.sma(self.iid, 86400, ctx.params["fast"])
        slow = ctx.sma(self.iid, 86400, ctx.params["slow"])
        if fast != fast or slow != slow:                        # NaN during warm-up
            return
        pos = ctx.position(self.iid, "CNC")
        held = pos.qty if pos else 0
        if fast > slow and held == 0:
            # leave headroom for the protection band (A4 §3.3) and charges
            qty = (ctx.funds().available * 95 // 100) // (bar.close * 103 // 100)
            if qty > 0:
                ctx.place(self.iid, "BUY", qty, "CNC", "MARKET", tag="entry")
        elif fast < slow and held > 0:
            ctx.place(self.iid, "SELL", held, "CNC", "MARKET", tag="exit")
```

## §5 Python data-layer surface (for orientation)

| Module | Key functions |
|---|---|
| `stockify.paths` | `lab_dir()`, `refdata_db()`, `bars_db()`, `landing_file(date)`, `archive_dir(date)`, `packs_dir()`, `runs_dir()`, `journals_dir(session)` |
| `stockify.timeutil` | `ist_date(t_ns)`, `ist_time_to_ns(date, "HH:MM")` |
| `stockify.store` | the only access layer for `lab.sqlite` (A1 §4) |
| `stockify.numerics` | exact decimal → ppb/ppm/bp/paise, integer helpers, canonical float format and JSON writer (A4 §0, §9.2) — the Python twins of `int128.hpp`, `decimal.hpp`, `format.hpp` |
| `stockify.data.instruments` | `sync(on_date)`, `resolve_token`, `get`, `find`, `spec_at`, `expiries`, `option_chain` |
| `stockify.data.calendar` | `is_trading_day`, `trading_days`, `phases`, `phase_at`, `generated_expiries`, `validate_expiries` |
| `stockify.data.nse_files` | `fetch(dataset, date)`, `backfill(datasets, start, end)`, parsers |
| `stockify.data.history` | `fetch_bars(instrument_id, interval, start, end)`, `detect_adjustments()` |
| `stockify.data.packs` | `build(spec) -> sha256`, `read(path) -> numpy records`, `encode(records) -> bytes` |
| `stockify.bundle` | `resolve_bundle(run_config) -> dict`, `day_bundle(session, date) -> dict` |
| `stockify.runs` | `launch(config) -> run_id`, `import_run(folder)`, `rerun(run_id)` |
| `stockify.oracle` | `run(config) -> RunResult` |

## §6 REST API (FastAPI)

All money fields are integer paise; times are UTC ns plus an ISO IST string for display. DTOs live in `server/schemas.py` and are mirrored in `web/src/lib/types.ts`. List endpoints take `offset`/`limit`. Declare fixed paths (e.g. `/api/tests/compare`) before parameterised ones (`/api/tests/{id}`), or FastAPI will route `compare` as an id.

| Method & path | Purpose | Milestone |
|---|---|---|
| `GET /api/strategies` | Registered C++ and discovered Python strategies with parameter schemas | M3.14 |
| `POST /api/tests` | Validate a run config and queue it → `{run_id}` | M3.14 |
| `GET /api/tests` | Runs (filters: status, family) | M3.14 |
| `GET /api/tests/compare?ids=` | Side-by-side comparison | M6.1 |
| `GET /api/tests/{id}` | Config, status, metrics, labels, manifest | M3.14 |
| `GET /api/tests/{id}/{orders,fills,equity,charges,logs,margin}` | Run artifacts | M3.14 / M4.14 |
| `POST /api/tests/{id}/cancel` | Kill a queued or running run | M3.14 |
| `GET /api/tests/{id}/export` | Zip export (CSV + JSON) | M6.2 |
| `GET /api/tests/{id}/research` | A run's deflated Sharpe (with N, V, rank), bootstrap intervals, plain sentences; 404 for an unknown run. May write the derived `research.json` cache | M6.4 / M6.8 |
| `GET /api/research/families` | Trial families with N, best run and its DSR (`offset`/`limit`) | M6.4 |
| `GET /api/research/families/{family_id}` | One family's tries, other runs, sessions, holdout unlocks (the id contains `/`; 404 unknown) | M6.4 |
| `POST /api/trade/sessions` | Create a paper session → `{session_id}` | M5.12 |
| `GET /api/trade/sessions` | Sessions with status, today's P&L, coverage, parity | M5.12 |
| `GET /api/trade/sessions/{id}` (+ `/positions`, `/orders`, `/fills`, `/timeline`, `/margin`) | Session detail | M5.12 |
| `POST /api/trade/sessions/{id}/{pause,resume,stop}` | Controls | M5.12 |
| `POST /api/trade/stop-all` | Kill switch | M5.12 |
| `GET /api/trade/commands/{cmd_id}` | A queued command: pending, applied or failed, its times and error | M5.12 |
| `GET /api/trade/login` | The Kite login window of the next session on the owner's clock, and whether the daemon is waiting for a login | M5.12 |
| `GET /api/data/recorder` | Latest recorder status | M1.5 |
| `GET /api/data/quality?date=` | Quality metrics for a day | M1.9 |
| `GET /api/data/coverage?from=&to=` | Coverage matrix by date and dataset, bar ranges, storage, recorder (read-only; defaults to the last `default_days`, 422 over `max_days`) | M6.3 |
| `GET /api/data/gaps?date=` | The recording gaps of one day (404 when not recorded) | M6.3 |
| `GET /api/research/families` (+ `/{id}`) | Trial families, trial counts, DSR | M6.4 |

`/api/trade` (M5.12, `server/routers/trade.py`) reads the lab and writes **commands**: pause, resume, stop and stop-all are rows in `session_commands` (answer 202 `{cmd_id}`), which the daemon journals and then applies through `lifecycle` (D-68, D-72); the API never changes a session's state, and 409 means the session is STOPPED. The only thing it creates is a CREATED session (`POST` body `{session_id?, config}`, a `stockify.session/1` object whose strategy must be in the catalogue; 201 `{session_id}`, 409 for a duplicate id, 422 with a list of problems). It has no route that places, modifies or cancels an order. `today_pnl_paise` is the latest recorded day's NAV less the day before's (the capital for a first day). A session's detail gives its statistics twice: `stats_all` and `stats_full_coverage` (days whose coverage reached `paper.json` `coverage_threshold`; a day with unknown coverage is not full), with `has_coverage_holes` (I-14). `/positions` of a day that has ended are its stored end-of-day book; before that they are the previous end-of-day book with the day's fills applied (`source` `eod` or `fills`); each carries the `instrument_token` valid on that day. `GET /api/trade/login` returns `{waiting_for_login, day, window_local, last_usable_local, market_open_local}`: the window, its last usable moment (the window's end) and the 09:15 open, as `HH:MM TZ` in `schedule.json` `owner_tz`. Free text from the daemon (timeline details, margin errors) has absolute paths replaced by `<path>`. Bounds (WP 5.2): a create body is at most 64 KiB (413), a universe at most 500 instruments and at most 50 sessions that are not STOPPED (422). A command identical to one still pending (same session, kind and args) returns that command's `cmd_id` instead of queueing again; a pending command has no expiry, it is applied when the daemon next runs, and `GET /api/trade/commands/{cmd_id}` shows whether it has been. Orders and fills are scrubbed like the timeline; stored rows that are malformed give a 409 on `/positions` and no position count on the card, never a 500.
