"""Domain types shared by every engine.

These dataclasses are the contract between layers. They are intentionally
immutable where possible: an engine returns a new object rather than mutating
a shared one, which keeps the audit trail truthful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AssetClass(str, Enum):
    EQUITIES = "equities"
    CRYPTO = "crypto"
    FOREX = "forex"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        return {
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }[self.value]


class Decision(str, Enum):
    """Outcome of the decision pipeline for one candidate."""

    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"
    REDUCE = "REDUCE"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV bar. Timestamps are always tz-aware UTC bar-open times."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Candle.timestamp must be timezone-aware (UTC)")

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_shadow(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_shadow(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    def is_valid(self) -> bool:
        """Structural sanity: OHLC ordering holds and nothing is NaN/negative."""
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(math.isnan(v) or math.isinf(v) for v in values):
            return False
        if min(self.open, self.high, self.low, self.close) <= 0:
            return False
        if self.volume < 0:
            return False
        return self.high >= max(self.open, self.close) and self.low <= min(self.open, self.close)


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book snapshot used for spread and execution checks."""

    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid * 10_000.0


@dataclass(frozen=True, slots=True)
class Instrument:
    """Static reference data for a tradeable symbol."""

    symbol: str
    asset_class: AssetClass
    exchange: str = "UNKNOWN"
    sector: str = "UNKNOWN"
    tick_size: float = 0.01
    lot_size: float = 1.0
    min_order_size: float = 1.0
    active: bool = True

    def round_size(self, size: float) -> float:
        """Floor a raw size onto the instrument's lot grid.

        Flooring (never rounding up) guarantees the sized risk can only come in
        at or under the risk budget.
        """
        if self.lot_size <= 0:
            return size
        lots = math.floor(size / self.lot_size + 1e-9)
        return max(0.0, lots * self.lot_size)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Per-component signal score, retained so decisions stay explainable."""

    components: Mapping[str, float]

    @property
    def total(self) -> float:
        return round(sum(self.components.values()), 4)

    def as_dict(self) -> dict[str, float]:
        return dict(self.components)


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's proposal. Carries no authorisation: risk decides that."""

    symbol: str
    asset_class: AssetClass
    strategy_id: str
    side: Side
    entry_price: float
    timestamp: datetime
    timeframe: Timeframe = Timeframe.D1
    score: ScoreBreakdown = field(default_factory=lambda: ScoreBreakdown({}))
    structure_level: float | None = None
    atr: float | None = None
    regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    features: Mapping[str, Any] = field(default_factory=dict)
    invalidation: tuple[str, ...] = ()

    @property
    def total_score(self) -> float:
        return self.score.total


# ---------------------------------------------------------------------------
# Orders, fills, positions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "day"
    parent_id: str | None = None
    purpose: str = "entry"  # entry | stop | target | exit
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def with_quantity(self, quantity: float) -> "Order":
        return replace(self, quantity=quantity)


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    timestamp: datetime
    commission: float = 0.0
    slippage_bps: float = 0.0


@dataclass(slots=True)
class Position:
    """A live position. Mutable because fills and trailing stops update it."""

    symbol: str
    asset_class: AssetClass
    side: Side
    quantity: float
    entry_price: float
    opened_at: datetime
    strategy_id: str = "MANUAL"
    stop_price: float | None = None
    targets: list[tuple[float, float]] = field(default_factory=list)
    sector: str = "UNKNOWN"
    realised_pnl: float = 0.0
    highest_price: float | None = None
    lowest_price: float | None = None
    stop_moved_to_breakeven: bool = False
    trailing_active: bool = False

    def __post_init__(self) -> None:
        if self.highest_price is None:
            self.highest_price = self.entry_price
        if self.lowest_price is None:
            self.lowest_price = self.entry_price

    def notional(self, price: float) -> float:
        return abs(self.quantity) * price

    def unrealised_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.quantity * self.side.sign

    def gain_pct(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price * self.side.sign

    def risk_amount(self) -> float:
        """Capital at risk down to the protective stop. Zero if unprotected."""
        if self.stop_price is None:
            return 0.0
        return abs(self.entry_price - self.stop_price) * abs(self.quantity)

    def mark(self, price: float) -> None:
        self.highest_price = max(self.highest_price or price, price)
        self.lowest_price = min(self.lowest_price or price, price)


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """A closed round-trip, written to the journal and used by drift detection."""

    symbol: str
    strategy_id: str
    side: Side
    quantity: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    pnl: float
    r_multiple: float
    costs: float = 0.0
    exit_reason: str = "unspecified"
    regime: str = "UNKNOWN"

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def holding_days(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 86400.0


def utc_now() -> datetime:
    """Single source of wall-clock truth; always tz-aware UTC."""
    return datetime.now(timezone.utc)


def sorted_candles(candles: Sequence[Candle]) -> list[Candle]:
    return sorted(candles, key=lambda c: c.timestamp)
