"""Paper trading (M5.12, A2 §6): paper sessions, how they are doing, and the owner's controls.

The API only reads the lab and writes commands. A pause, resume, stop or stop-all goes into `session_commands`
and the daemon's dispatcher journals it before it applies it through `lifecycle` (D-68, D-72); the API never
changes a session's state itself. The one thing it creates is a CREATED session, through `store.create_session`.
There is no route that places, modifies or cancels an order, paper or real: paper orders exist only inside the
engine, through strategies. Money is integer paise; times are ns plus an ISO IST string.

Fixed paths (`/api/trade/login`, `/api/trade/stop-all`, `/api/trade/sessions`) are declared before
`/api/trade/sessions/{session_id}`. Nothing here returns a secret, a token, or a path on this machine: free text that
came from the daemon (timeline details, errors) has any absolute path replaced.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from fractions import Fraction
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from server.schemas import (CommandStatusDTO, SessionCommandDTO, SessionCreatedDTO, SessionDetailDTO, SessionDTO, SessionFillDTO,
                            SessionChartDTO, SessionMarginDTO, SessionOrderDTO, SessionPositionDTO, SessionStatsDTO, SessionTimelineDTO, TradeLoginDTO)

router = APIRouter(tags=["trade"])

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INSTRUMENT = re.compile(r"[0-9]{1,19}")
MAX_BODY_BYTES = 64 * 1024
MAX_UNIVERSE = 500
MAX_LIVE_SESSIONS = 50
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|]*")
_PAGE = 5000


class CreateSessionBody(BaseModel):
    """`config` is a `stockify.session/1` object (A1 §6.5); `session_id` is optional (one is made when it is left out)."""
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None
    config: dict[str, Any]


def _ist(ns: int | None) -> str | None:
    if ns is None:
        return None
    from stockify import timeutil
    return timeutil.ns_to_ist(ns).isoformat()


def _scrub(value: Any) -> Any:
    """`value` with every absolute Windows path in its strings (at any depth) replaced by `<path>`."""
    if isinstance(value, str):
        return _PATH.sub("<path>", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _int(v: Any) -> int | None:
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None


def _session_or_404(session_id: str) -> dict:
    from stockify import store
    row = store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no paper session {session_id!r}")
    return row


def _check_date(trade_date: str | None) -> None:
    if trade_date is not None and not _DATE.fullmatch(trade_date):
        raise HTTPException(status_code=422, detail=["trade_date must look like 2026-11-17"])


def _threshold() -> str:
    from stockify.config import loader
    return str(loader.load_json("paper.json")["coverage_threshold"])


# ── creating a session ───────────────────────────────────────────────────────

async def _body_cap(request: Request) -> None:
    """Refuse a request body over 64 KiB (by its declared length, then by what was actually read)."""
    declared = request.headers.get("content-length")
    too_big = declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES
    if too_big or len(await request.body()) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="the request body is over 64 KiB")


@router.post("/api/trade/sessions", response_model=SessionCreatedDTO, status_code=201)
def create_session(body: CreateSessionBody, _size: None = Depends(_body_cap)) -> SessionCreatedDTO:
    from stockify import sessionconfig, store, timeutil
    from stockify.research import registry
    from stockify.strategies import _catalogue
    universe = body.config.get("universe")
    if isinstance(universe, list) and len(universe) > MAX_UNIVERSE:
        raise HTTPException(status_code=422, detail=[f"universe: at most {MAX_UNIVERSE} instruments"])
    if sum(s["status"] != "STOPPED" for s in store.list_sessions()) >= MAX_LIVE_SESSIONS:
        raise HTTPException(status_code=422, detail=[f"at most {MAX_LIVE_SESSIONS} sessions that are not STOPPED; "
                                                     "stop one first"])
    try:
        cfg = sessionconfig.validate(body.config)
    except sessionconfig.SessionConfigError as e:
        raise HTTPException(status_code=422, detail=e.problems)
    s = cfg["strategy"]
    if s["kind"] not in ("python", "cpp"):
        raise HTTPException(status_code=422, detail=[f"strategy.kind: {s['kind']!r} cannot be started from the API"])
    schema = _catalogue.schema_for(s["kind"], s["ref"])
    if schema is None:                  # the API never imports code of the caller's choosing
        raise HTTPException(status_code=422, detail=[f"strategy.ref: {s['ref']!r} is not in the catalogue"])
    problems = _catalogue.validate_params(schema, s.get("params", {}))
    if problems:
        raise HTTPException(status_code=422, detail=[f"strategy.params.{p}" for p in problems])
    name = cfg.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail=["name: required"])
    now = timeutil.now_ns()
    if body.session_id is not None:
        candidates = [body.session_id]
    else:
        day = timeutil.ns_to_ist(now).strftime("%Y%m%d")
        candidates = [f"paper-{day}-{secrets.token_hex(3)}" for _ in range(5)]
    for sid in candidates:
        try:
            store.create_session(sid, name, cfg, now)
            registry.ensure_family(cfg["family_id"])              # M6.4: the session's family has its row
            return SessionCreatedDTO(session_id=sid)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=[f"session_id: {e}"])
        except sqlite3.IntegrityError:
            continue
    raise HTTPException(status_code=409, detail=f"a session {candidates[-1]!r} already exists (ids ignore case)")


# ── reading sessions ─────────────────────────────────────────────────────────

def _tokens_symbols(day: str, ids: set[int]) -> tuple[dict[int, int], dict[int, str]]:
    """instrument id -> the Kite token valid on `day`, and -> tradingsymbol, from refdata (empty if it has none)."""
    if not ids:
        return {}, {}
    from stockify.data import refdata
    marks = ",".join("?" * len(ids))
    try:
        conn = refdata._connect()
        tokens = {r[0]: r[1] for r in conn.execute(
            f"SELECT instrument_id, instrument_token FROM instrument_tokens WHERE instrument_id IN ({marks}) "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ORDER BY valid_from",
            (*sorted(ids), day, day))}
        from stockify.data.instruments import display_name
        symbols = {r["instrument_id"]: n for r in conn.execute(
            "SELECT instrument_id, tradingsymbol, underlying, kind, expiry, strike_paise FROM instruments "
            f"WHERE instrument_id IN ({marks})", tuple(sorted(ids))) if (n := display_name(dict(r)))}
    except sqlite3.Error:        # an unreadable refdata is not "no names": say so (D-143), as the other pages do
        raise HTTPException(status_code=409, detail="the instrument names cannot be read right now") from None
    return tokens, symbols


def _all_rows(session_id: str, table: str, day: str) -> list[dict]:
    from stockify import store
    out: list[dict] = []
    while True:
        page = store.session_rows(session_id, table, trade_date=day, offset=len(out), limit=_PAGE)
        out += page
        if len(page) < _PAGE:
            return out


def _derived(session_id: str, day: str) -> list[dict]:
    """Open positions of a day that has not ended: the previous day's end-of-day book with this day's fills applied
    in fill order (average cost; a flip takes the fill's price). Marks are not known here."""
    from stockify import store
    book: dict[tuple[int, str], list[int]] = {}
    for d in reversed([r["trade_date"] for r in store.session_days(session_id) if r["trade_date"] < day]):
        end = store.session_day_end(session_id, d)
        if end["equity"] is not None:
            for p in end["positions"]:
                qty, iid = _int(p.get("qty")), _int(p.get("instrument_id"))
                if qty and iid is not None:
                    book[(iid, str(p.get("product")))] = [qty, _int(p.get("avg_price_paise")) or 0]
            break
    products = {int(o["order_id"]): o.get("product") for o in _all_rows(session_id, "orders", day)}
    for f in _all_rows(session_id, "fills", day):
        key = (int(f["instrument_id"]), str(products.get(int(f["order_id"]))))
        qty, price = int(f["qty"]) * (1 if f["side"] == "BUY" else -1), int(f["price_paise"])
        held, avg = book.get(key, [0, 0])
        if held == 0 or (held > 0) == (qty > 0):
            total = held + qty
            avg = (abs(held) * avg + abs(qty) * price) // abs(total)
        elif abs(qty) > abs(held):
            avg = price
        book[key] = [held + qty, avg]
    return [{"instrument_id": iid, "product": prod, "qty": q, "avg_price_paise": a}
            for (iid, prod), (q, a) in sorted(book.items()) if q != 0]


def _positions(session_id: str, day: str) -> list[SessionPositionDTO]:
    from stockify import store
    end = store.session_day_end(session_id, day)
    if end["equity"] is not None:
        rows, source = end["positions"], "eod"
    else:
        try:
            rows, source = _derived(session_id, day), "fills"
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            raise HTTPException(status_code=409, detail=f"the stored orders or fills of {day} are malformed: "
                                                        "replay the day")
    rows = [r for r in rows if isinstance(r, dict) and _int(r.get("qty")) and _int(r.get("instrument_id")) is not None]
    tokens, symbols = _tokens_symbols(day, {int(r["instrument_id"]) for r in rows})
    out = [SessionPositionDTO(trade_date=day, instrument_id=int(r["instrument_id"]),
                              instrument_token=tokens.get(int(r["instrument_id"])),
                              tradingsymbol=symbols.get(int(r["instrument_id"])), product=r.get("product"),
                              qty=int(r["qty"]), avg_price_paise=_int(r.get("avg_price_paise")),
                              mark_paise=_int(r.get("mark_paise")), unrealised_paise=_int(r.get("unrealised_paise")),
                              source=source) for r in rows]
    return sorted(out, key=lambda p: (p.instrument_id, p.product or ""))


def _daily_pnl(session: dict) -> list[tuple[str, int]]:
    """(trade_date, NAV change) per day with an equity row; a first day is measured from the capital."""
    from stockify import store
    prev = (session.get("config") or {}).get("capital_paise")
    out = []
    for r in store.session_equities(session["session_id"]):
        out.append((r["trade_date"], r["nav_paise"] - (prev if prev is not None else r["nav_paise"])))
        prev = r["nav_paise"]
    return out


def _stats(pnls: list[int]) -> SessionStatsDTO:
    cum = peak = drawdown = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
    return SessionStatsDTO(days=len(pnls), pnl_paise=sum(pnls), win_days=sum(p > 0 for p in pnls),
                           loss_days=sum(p < 0 for p in pnls), best_day_paise=max(pnls, default=None),
                           worst_day_paise=min(pnls, default=None), max_drawdown_paise=drawdown)


def _is_full(day: dict | None, threshold: Fraction) -> bool:
    """A day counts as full coverage only when its coverage is known and reached the threshold."""
    import json
    if day is None:
        return False
    try:
        raw = json.loads(day.get("coverage_json") or "null")
    except ValueError:
        raw = None
    try:
        if isinstance(raw, dict) and "fraction" in raw:
            return Fraction(raw["fraction"]) >= threshold
        if day.get("coverage") is not None:
            return Fraction(str(day["coverage"])) >= threshold
    except (ValueError, ZeroDivisionError):
        pass
    return False


def _open_positions(session_id: str, day: str) -> int | None:
    try:
        return len(_positions(session_id, day))
    except HTTPException:                   # malformed stored rows: no count, never a failed list
        return None


def _session_dto(session: dict) -> SessionDTO:
    import json
    from stockify import store
    sid = session["session_id"]
    days = store.session_days(sid)
    latest = days[-1] if days else None
    pnl = None
    if latest is not None:
        pnl = dict(_daily_pnl(session)).get(latest["trade_date"])
    parity = next((d["parity"] for d in reversed(days) if d.get("parity")), None)
    try:
        labels = json.loads(latest["labels_json"] or "[]") if latest else []
    except ValueError:
        labels = []
    return SessionDTO(session_id=sid, name=session["name"] or "", status=session["status"],
                      pause_reasons=session["pause_reasons"], today_pnl_paise=pnl,
                      open_positions=_open_positions(sid, latest["trade_date"]) if latest else None,
                      coverage_today=latest.get("coverage") if latest else None, parity_last=parity,
                      labels=[str(x) for x in labels], created_ist=_ist(session["created_ns"]) or "")


def _day_or_latest(session_id: str, trade_date: str | None) -> str | None:
    from stockify import store
    _check_date(trade_date)
    if trade_date is not None:
        return trade_date
    days = store.session_days(session_id)
    return days[-1]["trade_date"] if days else None


@router.get("/api/trade/sessions", response_model=list[SessionDTO])
def list_sessions() -> list[SessionDTO]:
    from stockify import store
    return [_session_dto(s) for s in store.list_sessions()]


@router.get("/api/trade/login", response_model=TradeLoginDTO)
def login() -> TradeLoginDTO:
    """The Kite login window of the next session (today's, until its open), on the owner's clock (`owner_tz`)."""
    from server.routers import kite as kite_router
    from stockify import timeutil
    from stockify.data import calendar
    now = timeutil.now_ns()
    row = next((r for r in calendar.upcoming(timeutil.ns_to_ist(now).date(), 14)
                if r["open"] and r["continuous_start_ns"] > now), None)
    if row is None:
        raise HTTPException(status_code=404, detail="no trading day in the next 14 days")
    local = calendar.owner_tz()

    def at(ns: int) -> str:
        return f"{timeutil.ns_to_ist(ns).astimezone(local):%H:%M %Z}"

    return TradeLoginDTO(waiting_for_login=kite_router._daemon_waiting_for_login(), day=row["date"].isoformat(),
                         window_local=[at(row["login_from_ns"]), at(row["login_until_ns"])],
                         last_usable_local=at(row["login_until_ns"]), market_open_local=at(row["continuous_start_ns"]))


@router.post("/api/trade/stop-all", response_model=SessionCommandDTO, status_code=202)
def stop_all() -> SessionCommandDTO:
    """The kill switch: queue `stop_all`; the daemon pauses every session (reason `stop_all`) journal first."""
    return _queue(None, "stop_all")


@router.get("/api/trade/commands/{cmd_id}", response_model=CommandStatusDTO)
def command_status(cmd_id: int) -> CommandStatusDTO:
    """pending (not yet applied), applied, or failed (with the daemon's error text)."""
    from stockify import store
    row = store.get_command(cmd_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no command {cmd_id}")
    result = row.get("result") or ""
    status = "pending" if row["applied_ns"] is None else ("failed" if result.startswith("error") else "applied")
    return CommandStatusDTO(cmd_id=row["cmd_id"], session_id=row["session_id"], command=row["command"],
                            status=status, created_ns=row["created_ns"], created_ist=_ist(row["created_ns"]) or "",
                            applied_ns=row["applied_ns"], applied_ist=_ist(row["applied_ns"]),
                            error=_scrub(result) if status == "failed" else None)


@router.get("/api/trade/sessions/{session_id}", response_model=SessionDetailDTO)
def get_session(session_id: str) -> SessionDetailDTO:
    from stockify import store
    session = _session_or_404(session_id)
    days = {d["trade_date"]: d for d in store.session_days(session_id)}
    threshold = Fraction(_threshold())
    daily = _daily_pnl(session)
    full = [p for d, p in daily if _is_full(days.get(d), threshold)]
    return SessionDetailDTO(session=_session_dto(session), config=_scrub(session.get("config") or {}),
                            trade_date=max(days) if days else None, stats_all=_stats([p for _, p in daily]),
                            stats_full_coverage=_stats(full), coverage_threshold=_threshold(),
                            has_coverage_holes=len(full) != len(daily))


@router.get("/api/trade/sessions/{session_id}/positions", response_model=list[SessionPositionDTO])
def session_positions(session_id: str, trade_date: str | None = Query(None)) -> list[SessionPositionDTO]:
    _session_or_404(session_id)
    day = _day_or_latest(session_id, trade_date)
    return _positions(session_id, day) if day else []


def _rows(session_id: str, table: str, trade_date: str | None, offset: int, limit: int) -> list[dict]:
    from stockify import store
    _session_or_404(session_id)
    day = _day_or_latest(session_id, trade_date)
    return _scrub(store.session_rows(session_id, table, trade_date=day, offset=offset, limit=limit)) if day else []


def _typed(model: type[BaseModel], rows: list[dict]) -> list[Any]:
    """The stored rows as `model`: a blank cell is null, a key the model does not name is left out, and a row the model
    cannot read (a key field missing or not a number) is a clean 409, never a 500."""
    try:
        return [model.model_validate({k: (None if v == "" else v) for k, v in r.items()}) for r in rows]
    except ValueError as e:
        raise HTTPException(status_code=409, detail=f"a stored {model.__name__} row is malformed: {type(e).__name__}") from e


@router.get("/api/trade/sessions/{session_id}/orders", response_model=list[SessionOrderDTO])
def session_orders(session_id: str, trade_date: str | None = Query(None), offset: int = Query(0, ge=0),
                   limit: int = Query(500, ge=1, le=5000)) -> list[SessionOrderDTO]:
    return _typed(SessionOrderDTO, _rows(session_id, "orders", trade_date, offset, limit))


@router.get("/api/trade/sessions/{session_id}/fills", response_model=list[SessionFillDTO])
def session_fills(session_id: str, trade_date: str | None = Query(None), offset: int = Query(0, ge=0),
                  limit: int = Query(500, ge=1, le=5000)) -> list[SessionFillDTO]:
    return _typed(SessionFillDTO, _rows(session_id, "fills", trade_date, offset, limit))


_PAPER_SAYS = "Paper trading: no real orders."
_SESSION_CHART_PLAIN = {
    "no_pack": "This day has no recorded market data, so there are no bars; its trades are listed without a chart. ",
    "pack unreadable": "This day's market data could not be read, so there are no bars; its trades are listed without a chart. ",
}


@router.get("/api/trade/sessions/{session_id}/chart", response_model=SessionChartDTO)
def session_chart(session_id: str, instrument: str | None = Query(None),
                  trade_date: str | None = Query(None), interval_s: str | None = Query(None)) -> SessionChartDTO:
    """One instrument of one paper-session day as bars (read incrementally from the day's journal), a marker per paper
    fill with the strategy's note, and each closing fill's trade profit (CH 6.13). Same wire format as a run's chart
    (`/api/tests/{run_id}/chart`: times in UTC seconds, prices as exact rupee strings) plus `trade_date`, `days` (for the
    day picker), `paper` (always true) and `live` (the day is open). A day without a journal, or one that cannot be
    read, is a 200 with `data_state`. Read-only; no id, path or exception text is echoed."""
    from server.routers.tests import _chart_dto
    from stockify.reporting import chart_session
    if interval_s is not None:                       # the day is drawn at the bars its journal holds: refuse, do not ignore
        raise HTTPException(status_code=422, detail="this chart does not take interval_s")
    if instrument is not None and not (_INSTRUMENT.fullmatch(instrument) and int(instrument) <= 2**63 - 1):
        raise HTTPException(status_code=422, detail="the chart request is not valid")      # (a typed Query would echo the input)
    try:
        model = chart_session.session_chart(session_id, trade_date, None if instrument is None else int(instrument))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such session, day or instrument") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="the chart request is not valid") from None
    dto = _chart_dto(model)
    plain = _SESSION_CHART_PLAIN.get(model["data_state"], "") + _PAPER_SAYS
    return SessionChartDTO(**{**dto.model_dump(), "plain": plain, "trade_date": model["trade_date"],
                              "days": model["days"], "paper": True, "live": model["live"]})


@router.get("/api/trade/sessions/{session_id}/timeline", response_model=list[SessionTimelineDTO])
def session_timeline(session_id: str, offset: int = Query(0, ge=0),
                     limit: int = Query(200, ge=1, le=2000)) -> list[SessionTimelineDTO]:
    from stockify import store
    _session_or_404(session_id)
    return [SessionTimelineDTO(engine_ns=r["engine_ns"], wall_ns=r["wall_ns"], wall_ist=_ist(r["wall_ns"]) or "",
                               kind=r["kind"], detail=_scrub(r["detail"]))
            for r in store.timeline(session_id, offset=offset, limit=limit)]


@router.get("/api/trade/sessions/{session_id}/margin", response_model=list[SessionMarginDTO])
def session_margin(session_id: str, trade_date: str | None = Query(None), offset: int = Query(0, ge=0),
                   limit: int = Query(200, ge=1, le=2000)) -> list[SessionMarginDTO]:
    """Kite's margin answer beside the model's, per poll. Only the totals are returned: not the legs, the market
    snapshot, or the SPAN file."""
    from stockify import store
    _session_or_404(session_id)
    _check_date(trade_date)
    rows = [r for r in store.margin_live_rows(session_id=session_id) if trade_date in (None, r.get("trade_date"))]
    return [SessionMarginDTO(wall_ns=r["wall_ns"], wall_ist=_ist(r["wall_ns"]) or "", trade_date=r.get("trade_date"),
                             step=r.get("step"), kite_total_paise=r.get("kite_total_paise"),
                             ours_total_paise=r.get("ours_total_paise"), gap_paise=r.get("gap_paise"),
                             gap_ppm=r.get("gap_ppm"), error=_scrub(r.get("error")))
            for r in rows[offset:offset + limit]]


# ── controls: commands for the daemon, never a state change here ─────────────

def _control(session_id: str, verb: str) -> SessionCommandDTO:
    from stockify import store, timeutil
    session = _session_or_404(session_id)
    if session["status"] == "STOPPED":
        raise HTTPException(status_code=409, detail=f"session {session_id!r} is STOPPED")
    return _queue(session_id, verb)


def _queue(session_id: str | None, verb: str) -> SessionCommandDTO:
    """Queue a command, or return the cmd_id of an identical one (same session, kind and args) that is still
    pending. A pending command has no expiry: it is applied whenever the daemon next runs."""
    from stockify import store, timeutil
    for row in store.pending_commands():
        if (row["session_id"], row["command"], row["args"]) == (session_id, verb, None):
            return SessionCommandDTO(cmd_id=row["cmd_id"])
    try:
        return SessionCommandDTO(cmd_id=store.add_command(session_id, verb, None, timeutil.now_ns()))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no paper session {session_id!r}")


@router.post("/api/trade/sessions/{session_id}/pause", response_model=SessionCommandDTO, status_code=202)
def pause_session(session_id: str) -> SessionCommandDTO:
    return _control(session_id, "pause")


@router.post("/api/trade/sessions/{session_id}/resume", response_model=SessionCommandDTO, status_code=202)
def resume_session(session_id: str) -> SessionCommandDTO:
    return _control(session_id, "resume")


@router.post("/api/trade/sessions/{session_id}/stop", response_model=SessionCommandDTO, status_code=202)
def stop_session(session_id: str) -> SessionCommandDTO:
    return _control(session_id, "stop")
