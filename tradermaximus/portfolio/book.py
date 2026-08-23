"""Position book: the system's internal record of what it owns.

This is the *internal* side of reconciliation. The broker holds the
authoritative truth; this book holds what the system believes, and section 14's
``POSITION_RECONCILED`` state exists to prove the two agree before trading
continues.

Equity accounting is deliberately simple and explicit: cash moves on fills,
positions carry unrealised P&L, and equity is the sum. No mark-to-market magic
happens off-screen.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Iterator, Mapping, Sequence

from ..core.types import (
    AssetClass,
    Fill,
    OrderSide,
    Position,
    Side,
    TradeRecord,
    utc_now,
)


@dataclass(slots=True)
class PortfolioSnapshot:
    """Immutable-by-convention view of the book at one instant."""

    timestamp: datetime
    equity: float
    cash: float
    positions: tuple[Position, ...]
    prices: Mapping[str, float]

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def notional(self, position: Position) -> float:
        return position.notional(self.prices.get(position.symbol, position.entry_price))

    @property
    def gross_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(self.notional(p) for p in self.positions) / self.equity

    @property
    def net_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(self.notional(p) * p.side.sign for p in self.positions) / self.equity

    @property
    def total_exposure(self) -> float:
        """Absolute exposure as a fraction of equity (section 13.1)."""
        return self.gross_exposure

    @property
    def total_risk(self) -> float:
        """Sum of open risk-to-stop as a fraction of equity."""
        if self.equity <= 0:
            return 0.0
        return sum(p.risk_amount() for p in self.positions) / self.equity

    def exposure_by(self, attribute: str) -> dict[str, float]:
        """Exposure fractions grouped by ``sector`` or ``asset_class``."""
        buckets: dict[str, float] = defaultdict(float)
        if self.equity <= 0:
            return dict(buckets)
        for position in self.positions:
            raw = getattr(position, attribute)
            key = raw.value if isinstance(raw, AssetClass) else str(raw)
            buckets[key] += self.notional(position) / self.equity
        return dict(buckets)

    def position_weights(self) -> dict[str, float]:
        """Each position's share of total exposure (weights sum to 1)."""
        total = sum(self.notional(p) for p in self.positions)
        if total <= 0:
            return {}
        return {p.symbol: self.notional(p) / total for p in self.positions}

    def count_by_asset_class(self) -> dict[AssetClass, int]:
        counts: dict[AssetClass, int] = defaultdict(int)
        for position in self.positions:
            counts[position.asset_class] += 1
        return dict(counts)

    def unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl(self.prices.get(p.symbol, p.entry_price)) for p in self.positions)

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "positions": self.position_count,
            "gross_exposure": round(self.gross_exposure, 4),
            "net_exposure": round(self.net_exposure, 4),
            "total_risk": round(self.total_risk, 4),
            "unrealised_pnl": round(self.unrealised_pnl(), 2),
        }


class PositionBook:
    """Tracks positions, cash, equity history and closed trades."""

    def __init__(self, starting_equity: float, *, cash_reserve: float = 0.30) -> None:
        if starting_equity <= 0:
            raise ValueError("starting equity must be positive")
        self.starting_equity = starting_equity
        self.cash = starting_equity
        self.cash_reserve = cash_reserve
        self._positions: dict[str, Position] = {}
        self._prices: dict[str, float] = {}
        self.closed_trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.daily_equity: dict[date, float] = {}
        self.peak_equity = starting_equity
        self.monthly_peak = starting_equity
        self.weekly_peak = starting_equity
        self._realised_today: dict[date, float] = defaultdict(float)

    # -- access -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._positions)

    def __iter__(self) -> Iterator[Position]:
        return iter(list(self._positions.values()))

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._positions

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def get(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    @property
    def prices(self) -> Mapping[str, float]:
        return dict(self._prices)

    # -- pricing ------------------------------------------------------------

    def update_price(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        self._prices[symbol] = price
        position = self._positions.get(symbol)
        if position is not None:
            position.mark(price)

    def update_prices(self, prices: Mapping[str, float]) -> None:
        for symbol, price in prices.items():
            self.update_price(symbol, price)

    def price_of(self, symbol: str) -> float:
        position = self._positions.get(symbol)
        return self._prices.get(symbol, position.entry_price if position else 0.0)

    # -- equity -------------------------------------------------------------

    @property
    def equity(self) -> float:
        """Cash plus the marked value of every open position."""
        return self.cash + sum(
            position.notional(self.price_of(position.symbol)) * (1 if position.side is Side.LONG else -1)
            + (position.notional(position.entry_price) if position.side is Side.SHORT else 0.0)
            for position in self._positions.values()
        )

    @property
    def available_cash(self) -> float:
        """Cash above the section 25.1 reserve, which is never traded."""
        return max(0.0, self.cash - self.starting_equity * self.cash_reserve)

    def snapshot(self, timestamp: datetime | None = None) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            timestamp=timestamp or utc_now(),
            equity=self.equity,
            cash=self.cash,
            positions=self.positions,
            prices=dict(self._prices),
        )

    def mark_equity(self, timestamp: datetime | None = None) -> float:
        """Record an equity point and refresh the peak trackers."""
        timestamp = timestamp or utc_now()
        equity = self.equity
        self.equity_curve.append((timestamp, equity))
        self.daily_equity[timestamp.date()] = equity
        self.peak_equity = max(self.peak_equity, equity)
        self.monthly_peak = max(self.monthly_peak, equity)
        self.weekly_peak = max(self.weekly_peak, equity)
        return equity

    def reset_monthly_peak(self) -> None:
        self.monthly_peak = self.equity

    def reset_weekly_peak(self) -> None:
        self.weekly_peak = self.equity

    # -- drawdowns ----------------------------------------------------------

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def monthly_drawdown(self) -> float:
        if self.monthly_peak <= 0:
            return 0.0
        return max(0.0, (self.monthly_peak - self.equity) / self.monthly_peak)

    @property
    def weekly_drawdown(self) -> float:
        if self.weekly_peak <= 0:
            return 0.0
        return max(0.0, (self.weekly_peak - self.equity) / self.weekly_peak)

    def daily_pnl_pct(self, today: date | None = None, previous_equity: float | None = None) -> float:
        """Today's P&L as a signed fraction of the prior session's close."""
        if previous_equity is None:
            prior = [eq for day, eq in sorted(self.daily_equity.items()) if today is None or day < today]
            previous_equity = prior[-1] if prior else self.starting_equity
        if previous_equity <= 0:
            return 0.0
        return (self.equity - previous_equity) / previous_equity

    def returns(self) -> list[float]:
        """Period-over-period returns from the recorded equity curve."""
        values = [equity for _, equity in self.equity_curve]
        return [
            (curr - prev) / prev
            for prev, curr in zip(values, values[1:])
            if prev > 0
        ]

    # -- mutation -----------------------------------------------------------

    def open_position(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        side: Side,
        quantity: float,
        entry_price: float,
        strategy_id: str,
        stop_price: float | None = None,
        targets: Sequence[tuple[float, float]] = (),
        sector: str = "UNKNOWN",
        timestamp: datetime | None = None,
        commission: float = 0.0,
    ) -> Position:
        if symbol in self._positions:
            raise ValueError(f"position already open for {symbol}; scale in via add_to_position")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        position = Position(
            symbol=symbol,
            asset_class=asset_class,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            opened_at=timestamp or utc_now(),
            strategy_id=strategy_id,
            stop_price=stop_price,
            targets=list(targets),
            sector=sector,
        )
        self._positions[symbol] = position
        self._prices.setdefault(symbol, entry_price)
        self.cash -= quantity * entry_price * side.sign
        self.cash -= commission
        return position

    def add_to_position(self, symbol: str, quantity: float, price: float, commission: float = 0.0) -> Position:
        """Scale in, re-basing the entry to the weighted average cost."""
        position = self._require(symbol)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        total_quantity = position.quantity + quantity
        position.entry_price = (
            position.entry_price * position.quantity + price * quantity
        ) / total_quantity
        position.quantity = total_quantity
        self.cash -= quantity * price * position.side.sign
        self.cash -= commission
        return position

    def reduce_position(
        self,
        symbol: str,
        quantity: float,
        price: float,
        *,
        timestamp: datetime | None = None,
        commission: float = 0.0,
        reason: str = "unspecified",
        regime: str = "UNKNOWN",
    ) -> TradeRecord | None:
        """Close all or part of a position, returning a record when fully closed."""
        position = self._require(symbol)
        quantity = min(quantity, position.quantity)
        if quantity <= 0:
            return None

        pnl = (price - position.entry_price) * quantity * position.side.sign - commission
        position.realised_pnl += pnl
        position.quantity -= quantity
        self.cash += quantity * price * position.side.sign
        self.cash -= commission
        self._realised_today[(timestamp or utc_now()).date()] += pnl

        if position.quantity > 1e-12:
            return None

        del self._positions[symbol]
        # R is measured against the risk taken at entry, not the current stop.
        initial_risk = position.initial_risk
        record = TradeRecord(
            symbol=symbol,
            strategy_id=position.strategy_id,
            side=position.side,
            quantity=quantity,
            entry_price=position.entry_price,
            exit_price=price,
            opened_at=position.opened_at,
            closed_at=timestamp or utc_now(),
            pnl=position.realised_pnl,
            r_multiple=(position.realised_pnl / initial_risk) if initial_risk > 0 else 0.0,
            costs=commission,
            exit_reason=reason,
            regime=regime,
        )
        self.closed_trades.append(record)
        return record

    def close_position(self, symbol: str, price: float, **kwargs: object) -> TradeRecord | None:
        position = self._require(symbol)
        return self.reduce_position(symbol, position.quantity, price, **kwargs)  # type: ignore[arg-type]

    def close_all(self, prices: Mapping[str, float] | None = None, *, reason: str = "close_all") -> list[TradeRecord]:
        records: list[TradeRecord] = []
        for symbol in list(self._positions):
            price = (prices or {}).get(symbol, self.price_of(symbol))
            record = self.close_position(symbol, price, reason=reason)
            if record is not None:
                records.append(record)
        return records

    def set_stop(self, symbol: str, stop_price: float) -> None:
        self._require(symbol).stop_price = stop_price

    def apply_fill(self, fill: Fill, *, asset_class: AssetClass, strategy_id: str = "MANUAL") -> None:
        """Apply a broker fill to the book (used by the execution engine)."""
        existing = self._positions.get(fill.symbol)
        if existing is None:
            side = Side.LONG if fill.side is OrderSide.BUY else Side.SHORT
            self.open_position(
                symbol=fill.symbol,
                asset_class=asset_class,
                side=side,
                quantity=fill.quantity,
                entry_price=fill.price,
                strategy_id=strategy_id,
                timestamp=fill.timestamp,
                commission=fill.commission,
            )
            return

        increasing = (existing.side is Side.LONG and fill.side is OrderSide.BUY) or (
            existing.side is Side.SHORT and fill.side is OrderSide.SELL
        )
        if increasing:
            self.add_to_position(fill.symbol, fill.quantity, fill.price, fill.commission)
        else:
            self.reduce_position(
                fill.symbol,
                fill.quantity,
                fill.price,
                timestamp=fill.timestamp,
                commission=fill.commission,
                reason="fill",
            )

    def _require(self, symbol: str) -> Position:
        position = self._positions.get(symbol)
        if position is None:
            raise KeyError(f"no open position for {symbol}")
        return position

    # -- reporting ----------------------------------------------------------

    def trades_for(self, strategy_id: str) -> list[TradeRecord]:
        return [t for t in self.closed_trades if t.strategy_id == strategy_id]

    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    def summary(self) -> dict[str, object]:
        return {
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "open_positions": len(self._positions),
            "closed_trades": len(self.closed_trades),
            "realised_pnl": round(self.realised_pnl(), 2),
            "drawdown": round(self.drawdown, 4),
            "peak_equity": round(self.peak_equity, 2),
        }
