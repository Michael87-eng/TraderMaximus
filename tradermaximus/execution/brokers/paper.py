"""Paper broker with a realistic fill model.

The spec's deployment pipeline (section 23.1) puts 4-8 weeks of paper trading
before any live capital, and the user has stated they intend to paper trade
until convinced. A paper broker that fills every order instantly at the
requested price would make that period worthless — it would validate a strategy
that does not exist.

So this broker models the frictions that actually decide whether an edge
survives:

* **Spread** — buys cross at the ask, sells at the bid.
* **Market impact** — slippage grows with participation in the bar's volume,
  as a square-root law rather than linearly.
* **Partial fills** — orders that are large relative to available volume fill
  only partly, exactly as they would live.
* **Latency and rejection** — configurable, so recovery paths get exercised.
* **Commission** — charged in basis points on notional.

Every source of randomness is seeded, so a paper run is reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ...core.errors import ExecutionError
from ...core.types import (
    AssetClass,
    Fill,
    Order,
    OrderSide,
    OrderType,
    Quote,
    utc_now,
)
from .base import BrokerAccount, BrokerAdapter, BrokerPosition, OrderAck


@dataclass
class MarketState:
    """What the paper broker knows about an instrument right now."""

    price: float
    bar_volume: float = 1_000_000.0
    spread_bps: float = 5.0
    volatility: float = 0.02

    @property
    def quote(self) -> Quote:
        half = self.price * (self.spread_bps / 10_000.0) / 2.0
        return Quote(timestamp=utc_now(), bid=self.price - half, ask=self.price + half)


@dataclass
class PaperFillModel:
    """Parameters governing simulated execution quality."""

    #: Slippage at 100% participation, in basis points. Real impact follows
    #: roughly a square-root law in participation rate, so this is the
    #: coefficient rather than a flat cost.
    impact_coefficient_bps: float = 120.0
    #: Fraction of a bar's volume an order may consume before it fills partly.
    max_participation: float = 0.10
    commission_bps: float = 1.0
    latency_ms: float = 45.0
    latency_jitter_ms: float = 20.0
    #: Probability the venue rejects an otherwise-valid order.
    reject_probability: float = 0.0
    #: Probability a marketable order is only partly filled.
    partial_fill_probability: float = 0.05
    random_slippage_bps: float = 2.0
    #: Reject orders the simulated account cannot fund. Real venues do this,
    #: and without it a backtest can quietly run on imaginary capital and
    #: report returns that were never achievable.
    enforce_buying_power: bool = True


class PaperBroker(BrokerAdapter):
    """Simulated venue used for paper trading and backtesting."""

    name = "paper"
    is_paper = True
    supported_assets = frozenset({AssetClass.EQUITIES, AssetClass.CRYPTO, AssetClass.FOREX})

    def __init__(
        self,
        *,
        starting_cash: float = 100_000.0,
        model: PaperFillModel | None = None,
        seed: int = 1337,
    ) -> None:
        self.model = model if model is not None else PaperFillModel()
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self._market: dict[str, MarketState] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._working: dict[str, Order] = {}
        self._rng = random.Random(seed)
        self.fills: list[Fill] = []

    # -- market plumbing ----------------------------------------------------

    def set_market(self, symbol: str, state: MarketState) -> None:
        self._market[symbol] = state

    def update_price(self, symbol: str, price: float, *, bar_volume: float | None = None) -> None:
        current = self._market.get(symbol)
        if current is None:
            self._market[symbol] = MarketState(price=price, bar_volume=bar_volume or 1_000_000.0)
        else:
            current.price = price
            if bar_volume is not None:
                current.bar_volume = bar_volume

    def market(self, symbol: str) -> MarketState:
        state = self._market.get(symbol)
        if state is None:
            raise ExecutionError(f"paper broker has no market data for {symbol}")
        return state

    # -- order handling -----------------------------------------------------

    def submit(self, order: Order) -> OrderAck:
        if order.quantity <= 0:
            return OrderAck(order.order_id, False, message="quantity must be positive")

        state = self.market(order.symbol)
        latency = max(0.0, self._rng.gauss(self.model.latency_ms, self.model.latency_jitter_ms))

        if self.model.enforce_buying_power and not self._can_fund(order, state):
            return OrderAck(
                order.order_id,
                False,
                message=(
                    f"insufficient buying power: {order.quantity:g} {order.symbol} "
                    f"needs ~{order.quantity * state.price:,.2f}, cash available {self.cash:,.2f}"
                ),
                latency_ms=latency,
            )

        if self.model.reject_probability and self._rng.random() < self.model.reject_probability:
            return OrderAck(
                order.order_id,
                False,
                message="venue rejected the order (simulated)",
                latency_ms=latency,
            )

        # Resting orders park until a price update triggers them.
        if order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT} or (
            order.order_type is OrderType.LIMIT and not self._is_marketable(order, state)
        ):
            self._working[order.order_id] = order
            return OrderAck(
                order.order_id,
                True,
                broker_order_id=f"PB-{order.order_id}",
                remaining=order.quantity,
                message="order working",
                latency_ms=latency,
            )

        fills, remaining = self._execute(order, state)
        for fill in fills:
            self._apply(fill)
        if remaining > 1e-12:
            self._working[order.order_id] = order.with_quantity(remaining)

        return OrderAck(
            order_id=order.order_id,
            accepted=True,
            broker_order_id=f"PB-{order.order_id}",
            fills=tuple(fills),
            remaining=remaining,
            message="filled" if remaining <= 1e-12 else "partially filled",
            latency_ms=latency,
        )

    def cancel(self, order_id: str) -> bool:
        return self._working.pop(order_id, None) is not None

    def open_orders(self) -> Sequence[Order]:
        return tuple(self._working.values())

    def trigger_working_orders(self, symbol: str, price: float) -> list[Fill]:
        """Fill any resting order whose trigger the new price has crossed.

        The backtester calls this on each bar so stop and limit orders behave
        the way they would at a venue rather than only at submission time.
        """
        self.update_price(symbol, price)
        state = self.market(symbol)
        triggered: list[Fill] = []

        for order_id, order in list(self._working.items()):
            if order.symbol != symbol:
                continue
            if not self._is_triggered(order, price):
                continue
            del self._working[order_id]
            fills, remaining = self._execute(order, state, force_market=True)
            for fill in fills:
                self._apply(fill)
                triggered.append(fill)
            if remaining > 1e-12:
                self._working[order_id] = order.with_quantity(remaining)
        return triggered

    def _can_fund(self, order: Order, state: MarketState) -> bool:
        """Whether the simulated account can pay for this order.

        Only *increasing* exposure consumes buying power; a sell that closes or
        reduces a long is always fundable, and must be — refusing it would trap
        the account in a position it is trying to exit.
        """
        existing = self._positions.get(order.symbol)
        held = existing.quantity if existing else 0.0
        signed = order.quantity if order.side is OrderSide.BUY else -order.quantity

        if held * signed < 0:
            # Opposing direction: reducing or closing. Always permitted up to
            # the held size; beyond that only the excess opens new exposure.
            opening = max(0.0, abs(signed) - abs(held))
        else:
            opening = abs(signed)

        if opening <= 0:
            return True
        required = opening * state.price
        return required <= max(0.0, self.cash) + 1e-9

    # -- fill model ---------------------------------------------------------

    @staticmethod
    def _is_marketable(order: Order, state: MarketState) -> bool:
        if order.limit_price is None:
            return True
        if order.side is OrderSide.BUY:
            return order.limit_price >= state.quote.ask
        return order.limit_price <= state.quote.bid

    @staticmethod
    def _is_triggered(order: Order, price: float) -> bool:
        if order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT} and order.stop_price is not None:
            # A sell stop protects a long: it fires when price falls to it.
            if order.side is OrderSide.SELL:
                return price <= order.stop_price
            return price >= order.stop_price
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            if order.side is OrderSide.BUY:
                return price <= order.limit_price
            return price >= order.limit_price
        return True

    def _execute(
        self,
        order: Order,
        state: MarketState,
        *,
        force_market: bool = False,
    ) -> tuple[list[Fill], float]:
        """Produce fills for an order, returning ``(fills, unfilled quantity)``."""
        quote = state.quote
        reference = quote.ask if order.side is OrderSide.BUY else quote.bid

        # How much of the order can the bar absorb?
        capacity = max(0.0, state.bar_volume * self.model.max_participation)
        quantity = order.quantity
        if capacity > 0 and quantity > capacity:
            filled_quantity = capacity
        elif self.model.partial_fill_probability and self._rng.random() < self.model.partial_fill_probability:
            # Occasional partial fill even on a modest order, so the recovery
            # path is genuinely exercised in paper mode.
            filled_quantity = quantity * self._rng.uniform(0.4, 0.9)
        else:
            filled_quantity = quantity

        if filled_quantity <= 0:
            return [], quantity

        participation = (filled_quantity / state.bar_volume) if state.bar_volume > 0 else 0.0
        # Square-root market impact: doubling size costs ~1.41x, not 2x.
        impact_bps = self.model.impact_coefficient_bps * math.sqrt(max(0.0, participation))
        noise_bps = self._rng.gauss(0.0, self.model.random_slippage_bps)
        total_bps = impact_bps + noise_bps

        direction = 1 if order.side is OrderSide.BUY else -1
        fill_price = reference * (1.0 + direction * total_bps / 10_000.0)
        if fill_price <= 0:
            raise ExecutionError(f"simulated fill price for {order.symbol} is not positive")

        # Slippage is measured against the mid, so the spread shows up in it.
        slippage_bps = abs(fill_price - quote.mid) / quote.mid * 10_000.0 if quote.mid > 0 else 0.0
        commission = filled_quantity * fill_price * self.model.commission_bps / 10_000.0

        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=round(filled_quantity, 8),
            price=round(fill_price, 8),
            timestamp=utc_now(),
            commission=round(commission, 6),
            slippage_bps=round(slippage_bps, 4),
        )
        return [fill], max(0.0, quantity - filled_quantity)

    def _apply(self, fill: Fill) -> None:
        """Update simulated cash and positions from a fill."""
        self.fills.append(fill)
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        self.cash -= signed * fill.price + fill.commission

        existing = self._positions.get(fill.symbol)
        if existing is None:
            self._positions[fill.symbol] = BrokerPosition(fill.symbol, signed, fill.price)
            return

        new_quantity = existing.quantity + signed
        if abs(new_quantity) < 1e-12:
            del self._positions[fill.symbol]
            return

        if existing.quantity * signed > 0:
            # Adding to the position: weighted-average the entry.
            average = (
                existing.average_price * abs(existing.quantity) + fill.price * abs(signed)
            ) / abs(new_quantity)
        else:
            average = existing.average_price  # reducing leaves the basis alone
        self._positions[fill.symbol] = BrokerPosition(fill.symbol, new_quantity, average)

    # -- account ------------------------------------------------------------

    def positions(self) -> Sequence[BrokerPosition]:
        out: list[BrokerPosition] = []
        for position in self._positions.values():
            state = self._market.get(position.symbol)
            price = state.price if state else position.average_price
            out.append(
                BrokerPosition(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    average_price=position.average_price,
                    market_value=position.quantity * price,
                    unrealised_pnl=(price - position.average_price) * position.quantity,
                )
            )
        return tuple(out)

    def account(self) -> BrokerAccount:
        equity = self.cash + sum(p.market_value for p in self.positions())
        return BrokerAccount(equity=equity, cash=self.cash, buying_power=max(0.0, self.cash))

    def execution_quality(self) -> Mapping[str, float]:
        """Realised slippage and commission statistics for the session."""
        if not self.fills:
            return {"fills": 0, "avg_slippage_bps": 0.0, "max_slippage_bps": 0.0, "total_commission": 0.0}
        slippages = [f.slippage_bps for f in self.fills]
        return {
            "fills": len(self.fills),
            "avg_slippage_bps": round(sum(slippages) / len(slippages), 4),
            "max_slippage_bps": round(max(slippages), 4),
            "total_commission": round(sum(f.commission for f in self.fills), 4),
        }
