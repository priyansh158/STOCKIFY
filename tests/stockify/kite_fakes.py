"""Fake Kite clients for the paper-only tests (Task 3, LCB 5.1). No network, no credentials.

`FakeKiteConnect` mimics kiteconnect 5.2.2's `KiteConnect`: the same `_routes` table (copied from the installed
SDK; `test_kite_readonly` checks it against the real one whenever kiteconnect is importable), the same
`_request` / `_get` / `_post` / `_put` / `_delete` chain and the same method names. `_request` never sends
anything: it records `(route, method, url)` in `sent`.
"""
from __future__ import annotations

SDK_VERSION = "5.2.2"

ROUTES_5_2_2 = {
    "api.token": "/session/token",
    "api.token.invalidate": "/session/token",
    "api.token.renew": "/session/refresh_token",
    "user.profile": "/user/profile",
    "user.margins": "/user/margins",
    "user.margins.segment": "/user/margins/{segment}",
    "orders": "/orders",
    "trades": "/trades",
    "order.info": "/orders/{order_id}",
    "order.place": "/orders/{variety}",
    "order.modify": "/orders/{variety}/{order_id}",
    "order.cancel": "/orders/{variety}/{order_id}",
    "order.trades": "/orders/{order_id}/trades",
    "portfolio.positions": "/portfolio/positions",
    "portfolio.holdings": "/portfolio/holdings",
    "portfolio.holdings.auction": "/portfolio/holdings/auctions",
    "portfolio.positions.convert": "/portfolio/positions",
    "mf.orders": "/mf/orders",
    "mf.order.info": "/mf/orders/{order_id}",
    "mf.order.place": "/mf/orders",
    "mf.order.cancel": "/mf/orders/{order_id}",
    "mf.sips": "/mf/sips",
    "mf.sip.info": "/mf/sips/{sip_id}",
    "mf.sip.place": "/mf/sips",
    "mf.sip.modify": "/mf/sips/{sip_id}",
    "mf.sip.cancel": "/mf/sips/{sip_id}",
    "mf.holdings": "/mf/holdings",
    "mf.instruments": "/mf/instruments",
    "market.instruments.all": "/instruments",
    "market.instruments": "/instruments/{exchange}",
    "market.margins": "/margins/{segment}",
    "market.historical": "/instruments/historical/{instrument_token}/{interval}",
    "market.trigger_range": "/instruments/trigger_range/{transaction_type}",
    "market.quote": "/quote",
    "market.quote.ohlc": "/quote/ohlc",
    "market.quote.ltp": "/quote/ltp",
    "gtt": "/gtt/triggers",
    "gtt.place": "/gtt/triggers",
    "gtt.info": "/gtt/triggers/{trigger_id}",
    "gtt.modify": "/gtt/triggers/{trigger_id}",
    "gtt.delete": "/gtt/triggers/{trigger_id}",
    "order.margins": "/margins/orders",
    "order.margins.basket": "/margins/basket",
    "order.contract_note": "/charges/orders",
}

# What kite_client must deny on 5.2.2: every route under /orders, /gtt, /mf/orders, /mf/sips, plus the
# position conversion (PUT /portfolio/positions). The margin and charges calculators stay.
EXPECTED_DENY_5_2_2 = frozenset({
    "orders", "order.info", "order.place", "order.modify", "order.cancel", "order.trades",
    "gtt", "gtt.place", "gtt.info", "gtt.modify", "gtt.delete",
    "mf.orders", "mf.order.info", "mf.order.place", "mf.order.cancel",
    "mf.sips", "mf.sip.info", "mf.sip.place", "mf.sip.modify", "mf.sip.cancel",
    "portfolio.positions.convert",
})

# Every order-writing method of kiteconnect 5.2.2's KiteConnect.
ORDER_METHODS_5_2_2 = (
    "place_order", "modify_order", "cancel_order", "exit_order", "place_autoslice_order",
    "place_gtt", "modify_gtt", "delete_gtt",
    "place_mf_order", "cancel_mf_order", "place_mf_sip", "modify_mf_sip", "cancel_mf_sip",
    "convert_position",
)


class FakeSession:
    """Stands in for requests.Session: records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def request(self, method, url, *args, **kwargs):  # noqa: ANN001, ANN201
        self.sent.append((method, url))
        return None

    def post(self, url, *args, **kwargs):  # noqa: ANN001, ANN201
        return self.request("POST", url, *args, **kwargs)

    def put(self, url, *args, **kwargs):  # noqa: ANN001, ANN201
        return self.request("PUT", url, *args, **kwargs)


class FakeKiteConnect:
    """KiteConnect's shape without the network."""

    _routes = dict(ROUTES_5_2_2)
    root = "https://api.kite.trade"
    instances: list["FakeKiteConnect"] = []

    def __init__(self, api_key, access_token=None, **_kwargs):  # noqa: ANN001
        self.api_key = api_key
        self.access_token = access_token
        self.sent: list[tuple] = []
        self.reqsession = FakeSession()
        FakeKiteConnect.instances.append(self)

    def set_access_token(self, access_token):  # noqa: ANN001
        self.access_token = access_token

    # ── the SDK's request chain ──────────────────────────────────────────────
    def _request(self, route, method, url_args=None, params=None, is_json=False, query_params=None):  # noqa: ANN001
        uri = self._routes[route].format(**(url_args or {}))
        self.sent.append((route, method, uri, params, query_params))
        self.reqsession.request(method, self.root + uri)
        return {"route": route, "method": method}

    def _get(self, route, url_args=None, params=None, is_json=False):  # noqa: ANN001
        return self._request(route, "GET", url_args=url_args, params=params, is_json=is_json)

    def _post(self, route, url_args=None, params=None, is_json=False, query_params=None):  # noqa: ANN001
        return self._request(route, "POST", url_args=url_args, params=params, is_json=is_json,
                             query_params=query_params)

    def _put(self, route, url_args=None, params=None, is_json=False, query_params=None):  # noqa: ANN001
        return self._request(route, "PUT", url_args=url_args, params=params, is_json=is_json,
                             query_params=query_params)

    def _delete(self, route, url_args=None, params=None, is_json=False):  # noqa: ANN001
        return self._request(route, "DELETE", url_args=url_args, params=params, is_json=is_json)

    # ── reads ────────────────────────────────────────────────────────────────
    def generate_session(self, request_token, api_secret):  # noqa: ANN001
        self._post("api.token", params={"request_token": request_token})
        self.set_access_token("session-token")
        return {"access_token": "session-token", "user_id": "AB1234"}

    def profile(self):
        return self._get("user.profile")

    def margins(self, segment=None):  # noqa: ANN001
        return self._get("user.margins")

    def quote(self, *instruments):  # noqa: ANN002
        self._get("market.quote", params={"i": instruments})
        return {"NSE:RELIANCE": {"last_price": 1.0}}

    def ltp(self, *instruments):  # noqa: ANN002
        return self._get("market.quote.ltp", params={"i": instruments})

    def ohlc(self, *instruments):  # noqa: ANN002
        return self._get("market.quote.ohlc", params={"i": instruments})

    def instruments(self, exchange=None):  # noqa: ANN001
        self._get("market.instruments.all")
        return []

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        self._get("market.historical", url_args={"instrument_token": instrument_token, "interval": interval})
        return []

    def positions(self):
        return self._get("portfolio.positions")

    def holdings(self):
        return self._get("portfolio.holdings")

    def orders(self):
        return self._get("orders")

    def order_margins(self, params):  # noqa: ANN001
        return self._post("order.margins", params=params, is_json=True)

    def basket_order_margins(self, params, consider_positions=True, mode=None):  # noqa: ANN001
        return self._post("order.margins.basket", params=params, is_json=True,
                          query_params={"consider_positions": consider_positions, "mode": mode})

    def get_virtual_contract_note(self, params):  # noqa: ANN001
        return self._post("order.contract_note", params=params, is_json=True)

    # ── order writers (what kite_client must make unreachable) ──────────────
    def place_order(self, variety, **params):  # noqa: ANN001
        return self._post("order.place", url_args={"variety": variety}, params=params)

    def place_autoslice_order(self, variety, **params):  # noqa: ANN001
        return self._post("order.place", url_args={"variety": variety}, params=params)

    def modify_order(self, variety, order_id=None, **params):  # noqa: ANN001
        return self._put("order.modify", url_args={"variety": variety, "order_id": order_id}, params=params)

    def cancel_order(self, variety, order_id=None, **_):  # noqa: ANN001
        return self._delete("order.cancel", url_args={"variety": variety, "order_id": order_id})

    def exit_order(self, variety, order_id=None, **_):  # noqa: ANN001
        return self.cancel_order(variety, order_id=order_id)

    def place_gtt(self, **params):
        return self._post("gtt.place", params=params)

    def modify_gtt(self, trigger_id=None, **params):  # noqa: ANN001
        return self._put("gtt.modify", url_args={"trigger_id": trigger_id}, params=params)

    def delete_gtt(self, trigger_id=None, **_):  # noqa: ANN001
        return self._delete("gtt.delete", url_args={"trigger_id": trigger_id})

    def place_mf_order(self, **params):
        return self._post("mf.order.place", params=params)

    def cancel_mf_order(self, order_id=None, **_):  # noqa: ANN001
        return self._delete("mf.order.cancel", url_args={"order_id": order_id})

    def place_mf_sip(self, **params):
        return self._post("mf.sip.place", params=params)

    def modify_mf_sip(self, sip_id=None, **params):  # noqa: ANN001
        return self._put("mf.sip.modify", url_args={"sip_id": sip_id}, params=params)

    def cancel_mf_sip(self, sip_id=None, **_):  # noqa: ANN001
        return self._delete("mf.sip.cancel", url_args={"sip_id": sip_id})

    def convert_position(self, **params):
        return self._put("portfolio.positions.convert", params=params)


class FakeKite:
    """A minimal inner client for ReadOnlyKite: records every call in `calls`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, name: str, **kwargs) -> dict:  # noqa: ANN003
        self.calls.append({"name": name, **kwargs})
        return {"name": name}

    def quote(self, *instruments):  # noqa: ANN002
        return self._record("quote", instruments=instruments)

    def ltp(self, *instruments):  # noqa: ANN002
        return self._record("ltp", instruments=instruments)

    def basket_order_margins(self, params, consider_positions=True, mode=None):  # noqa: ANN001
        return self._record("basket_order_margins", params=params, consider_positions=consider_positions, mode=mode)

    def _post(self, route, url_args=None, params=None, is_json=False, query_params=None):  # noqa: ANN001
        return self._record("_post", route=route, params=params, is_json=is_json, query_params=query_params)

    def place_order(self, **params):
        raise AssertionError("ReadOnlyKite must never reach an order method")
