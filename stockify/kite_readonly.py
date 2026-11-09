"""The daemon's read-only view of Kite (LCB 5.1, plan Global Constraints "Paper only").

`ReadOnlyKite` lets through only the calls in `ALLOWED`: quotes, history, instruments, profile, account margins,
and the margin and charges calculators. Every other name, and every `_`-prefixed name, raises
`ForbiddenKiteCall`; the wrapped client is held in a closure, never as an attribute. Margin calls always send
`consider_positions=False` and refuse `True`, so no answer mixes in the owner's real positions (D-69).

`client()` wraps `kite_client.get_client()`, which already strips Kite's order routes for every caller; this
wrapper is the second, narrower fence the daemon uses.
"""
from __future__ import annotations

ALLOWED = frozenset({"basket_order_margins", "order_margins", "get_virtual_contract_note", "quote", "ltp", "ohlc",
                     "profile", "margins", "historical_data", "instruments"})


class ForbiddenKiteCall(RuntimeError):
    """A Kite call outside ReadOnlyKite.ALLOWED, or a margin call asking for the real positions."""


def _no_positions(consider_positions: object) -> None:
    if consider_positions is not False:
        raise ForbiddenKiteCall("Kite margin calls must use consider_positions=False: a paper book never "
                                "mixes in the real account's positions")


def _query(mode: object) -> dict:
    query = {"consider_positions": "false"}
    if mode is not None:
        query["mode"] = str(mode)
    return query


class ReadOnlyKite:
    """`ReadOnlyKite(inner)`: a read-only view of a Kite client. It has no attributes of its own."""

    __slots__ = ()

    def __new__(cls, inner):  # noqa: ANN001, ANN204
        if inner is None:
            raise ValueError("ReadOnlyKite needs a client")

        # Both calculators post directly with the literal query `consider_positions=false`: the SDK would send
        # Python's `False` ("False") for the basket and nothing at all for single orders.
        def basket_order_margins(orders, *, consider_positions=False, mode=None):  # noqa: ANN001, ANN202
            _no_positions(consider_positions)
            return inner._post("order.margins.basket", params=orders, is_json=True, query_params=_query(mode))

        def order_margins(orders, *, consider_positions=False, mode=None):  # noqa: ANN001, ANN202
            _no_positions(consider_positions)
            return inner._post("order.margins", params=orders, is_json=True, query_params=_query(mode))

        wrapped = {"basket_order_margins": basket_order_margins, "order_margins": order_margins}

        def lookup(name: str):  # noqa: ANN202
            if not isinstance(name, str) or name.startswith("_") or name not in ALLOWED:
                raise ForbiddenKiteCall(f"Kite call {name!r} is not allowed (read-only, paper only)")
            if name in wrapped:
                return wrapped[name]
            return getattr(inner, name)

        bound = type(cls.__name__, (cls,), {"__slots__": (), "__getattribute__": _getattribute(lookup)})
        return object.__new__(bound)

    def __setattr__(self, name, value):  # noqa: ANN001
        raise ForbiddenKiteCall("ReadOnlyKite cannot be changed")

    def __delattr__(self, name):  # noqa: ANN001
        raise ForbiddenKiteCall("ReadOnlyKite cannot be changed")

    def __reduce_ex__(self, protocol):  # noqa: ANN001, ANN204
        raise ForbiddenKiteCall("ReadOnlyKite cannot be copied or pickled")

    def __repr__(self) -> str:
        return "<ReadOnlyKite>"


def _getattribute(lookup):  # noqa: ANN001, ANN202
    def __getattribute__(self, name):  # noqa: ANN001, ANN202, N807
        return lookup(name)
    return __getattribute__


def client() -> ReadOnlyKite:
    """The read-only view of `kite_client.get_client()`; its KiteAuthError subclasses pass through unchanged."""
    import kite_client
    return ReadOnlyKite(kite_client.get_client())
