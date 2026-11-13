"""One-shot check that the Kite Connect plan gives market data (M1.0 gate, K-11).

`python -m stockify kite check` runs, in order:

1. config      API key and secret are set (values are never printed)
2. token       today's access token exists and is fresh (06:00 IST rule, K-7)
3. profile     an authenticated REST call works
4. quote       market data over REST (NSE:NIFTY 50)
5. historical  day and minute candles (the paid data plan)
6. websocket   KiteTicker connects, subscribes NIFTY 50 in full mode and
               receives a raw binary frame (the recorder's path)

It talks to Kite directly instead of through `kite_client`'s wrappers,
because those swallow errors and this check exists to show them. Secrets
and tokens never appear in the output.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta

from stockify import timeutil
from stockify.log import redact

INDEX_SYMBOL = "NSE:NIFTY 50"
WS_TIMEOUT_S = 15.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _websocket(api_key: str, access_token: str, token: int) -> Check:
    """One short-lived live connection, role `dev` in the connection budget (M5.14)."""
    from stockify.daemon import conn_budget
    try:
        ticket = conn_budget.register("dev", limit=conn_budget.limit())
    except conn_budget.BudgetExceeded as e:
        return Check("websocket", False, str(e))
    try:
        return _websocket_check(api_key, access_token, token)
    finally:
        ticket.release()


def _websocket_check(api_key: str, access_token: str, token: int) -> Check:
    from kiteconnect import KiteTicker

    frames: list[int] = []
    errors: list[str] = []
    got_tick = threading.Event()
    kws = KiteTicker(api_key, access_token, reconnect=False)

    def on_connect(ws, response):  # noqa: ANN001
        ws.subscribe([token])
        ws.set_mode(ws.MODE_FULL, [token])

    def on_message(ws, payload, is_binary):  # noqa: ANN001
        if is_binary:
            frames.append(len(payload))
            if len(payload) > 1:      # 1-byte frames are heartbeats
                got_tick.set()

    def on_error(ws, code, reason):  # noqa: ANN001
        errors.append(f"{code} {redact(reason)}")
        got_tick.set()

    def on_close(ws, code, reason):  # noqa: ANN001
        if code not in (None, 1000):
            errors.append(f"closed {code} {redact(reason)}")
        got_tick.set()

    kws.on_connect = on_connect
    kws.on_message = on_message
    kws.on_error = on_error
    kws.on_close = on_close
    kws.connect(threaded=True)
    got_tick.wait(WS_TIMEOUT_S)
    try:
        kws.close()
    except Exception:
        pass

    data_frames = [n for n in frames if n > 1]
    if data_frames:
        return Check("websocket", True,
                     f"raw binary frame received ({data_frames[0]} bytes; "
                     f"{len(frames)} frames in total)")
    if errors:
        return Check("websocket", False, "; ".join(errors))
    if frames:
        return Check("websocket", True,
                     f"connected; only heartbeats in {WS_TIMEOUT_S:.0f} s (normal outside market hours)")
    return Check("websocket", False, f"no frames within {WS_TIMEOUT_S:.0f} s")


def run() -> list[Check]:
    import kite_client

    checks: list[Check] = []
    if not kite_client.is_enabled():
        return [Check("config", False, "KITE_API_KEY / KITE_API_SECRET not set, or kiteconnect missing")]
    checks.append(Check("config", True, f"API key set ({kite_client._api_key_fingerprint()})"))

    payload = kite_client.load_cached_token()
    if not payload or not payload.get("access_token"):
        checks.append(Check("token", False, "no saved token: log in via /portfolio, then Login to Kite"))
        return checks
    obtained = payload.get("obtained_at")
    when = (timeutil.s_to_ist(int(obtained)).strftime("%Y-%m-%d %H:%M IST")
            if obtained else "unknown time")
    if not kite_client.token_is_fresh(payload):
        checks.append(Check("token", False,
                            f"token from {when} expired at 06:00 IST: log in again via /portfolio"))
        return checks
    checks.append(Check("token", True, f"fresh (obtained {when})"))

    kite = kite_client.get_client()
    try:
        profile = kite.profile()
        checks.append(Check("profile", True, f"user type {profile.get('user_type', '?')}"))
    except Exception as e:
        checks.append(Check("profile", False, _err(e)))
        return checks

    token = None
    try:
        q = kite.quote([INDEX_SYMBOL])[INDEX_SYMBOL]
        token = int(q["instrument_token"])
        checks.append(Check("quote", True, f"{INDEX_SYMBOL} last {q.get('last_price')} (token {token})"))
    except Exception as e:
        checks.append(Check("quote", False, _err(e)))

    if token is not None:
        now = timeutil.now_ist().replace(tzinfo=None)
        try:
            days = kite.historical_data(token, now - timedelta(days=10), now, "day")
            minutes = kite.historical_data(token, now - timedelta(days=4), now, "minute")
            last = minutes[-1]["date"] if minutes else None
            checks.append(Check("historical", bool(days) and bool(minutes),
                                f"{len(days)} day candles, {len(minutes)} minute candles"
                                + (f", last minute bar {last}" if last else "")))
        except Exception as e:
            checks.append(Check("historical", False, _err(e)))
        checks.append(_websocket(kite_client._api_key(), payload["access_token"], token))
    return checks


def passed(checks: list[Check]) -> bool:
    return all(c.ok for c in checks) and {c.name for c in checks} >= {
        "config", "token", "profile", "quote", "historical", "websocket"}
