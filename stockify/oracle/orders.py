"""Orders, fills and their enums (A4 §3)."""
from __future__ import annotations

from dataclasses import dataclass, field

SIDES = ("BUY", "SELL")
PRODUCTS = ("CNC", "MIS", "NRML")
TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VALIDITIES = ("DAY", "IOC")
ACTIVE = ("OPEN", "TRIGGER_PENDING")
FINAL = ("COMPLETE", "CANCELLED", "REJECTED", "SLICED")
LIVE = ("PENDING",) + ACTIVE                    # every status an order has before it is final
REASONS = ("", "R_INSTRUMENT", "R_PRODUCT", "R_SESSION", "R_CAS", "R_QTY_LOT", "R_TICK", "R_BAND", "R_FREEZE",
           "R_HOLDINGS", "R_NO_REFERENCE", "R_MARGIN", "EOD", "IOC", "USER", "RMS")
SYSTEM_TAGS = ("AUTO_SQOFF", "RMS_SQOFF")


@dataclass
class Order:
    order_id: int
    parent_id: int
    instrument_id: int
    side: str
    qty: int
    product: str
    type: str                      # as placed: MARKET | LIMIT | SL | SL-M
    price: int = 0
    trigger: int = 0
    validity: str = "DAY"
    protection: object = "AUTO"    # "AUTO" or int bp
    tag: str = ""
    t_submit: int = 0
    t_activate: int = 0
    status: str = "PENDING"
    filled_qty: int = 0
    fill_value: int = 0            # Σ price × qty of fills
    reason: str = ""
    # working state
    limit: int | None = None       # effective limit after protection / trigger
    from_market: bool = False
    triggered: bool = False
    protection_bp: int | None = None
    reserved: int = 0              # funds reserved at submit (A4 §6.5)
    first_eligible_done: bool = False   # snapshot_quote: arrived vs resting; IOC: matched once
    trade_date: str | None = None
    charged: bool = False
    eod_after: str | None = None   # D-44: DAY order on daily bars, cancel after this date's bar

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty

    @property
    def avg_price(self) -> int:
        return self.fill_value // self.filled_qty if self.filled_qty else 0

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    def copy(self) -> "Order":
        return Order(**self.__dict__)


@dataclass
class Fill:
    fill_id: int
    order_id: int
    t_ns: int
    instrument_id: int
    side: str
    qty: int
    price: int
    evidence: str


@dataclass
class OrderView:
    """What strategies see (`ctx.order`, `on_order_update`)."""
    order_id: int
    instrument: int
    side: str
    qty: int
    product: str
    type: str
    price: int
    trigger: int
    status: str
    reason: str
    filled_qty: int
    avg_price: int
    tag: str = ""

    @classmethod
    def of(cls, o: Order) -> "OrderView":
        return cls(o.order_id, o.instrument_id, o.side, o.qty, o.product, o.type, o.price, o.trigger, o.status,
                   o.reason, o.filled_qty, o.avg_price, o.tag)


@dataclass
class FillView:
    order_id: int
    instrument: int
    side: str
    qty: int
    price: int
    t_ns: int
    evidence: str


@dataclass
class Pending:
    """Callbacks generated during one handler, delivered afterwards in order (A4 §3.9)."""
    items: list = field(default_factory=list)
