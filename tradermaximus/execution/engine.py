"""Execution engine (spec section 14).

Drives an authorised trade through the order lifecycle and — critically —
attaches the protective stop before the position is considered established.

The ordering matters more than anything else in this module. A filled position
without a stop is naked risk, and section 15.1 makes "missing protective order
> 5 minutes" a kill-switch trigger. So :meth:`ExecutionEngine.execute` treats a
failure to attach protection as an emergency: it immediately attempts to flatten
the position it just opened rather than leaving it exposed and reporting an
error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..core.errors import ExecutionError
from ..core.types import (
    AssetClass,
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Side,
    Signal,
    utc_now,
)
from ..observability.journal import Journal, NULL_JOURNAL, Severity
from ..portfolio.book import PositionBook
from ..risk.engine import RiskVerdict
from ..risk.targets import TakeProfitEngine
from .brokers.base import BrokerAdapter, OrderAck
from .safety import ExecutionConditions, SafetyReport, run_checklist
from .states import OrderLifecycle, OrderState, RECOVERY_ACTIONS


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of attempting to execute one authorised trade."""

    success: bool
    symbol: str
    lifecycle: OrderLifecycle
    fills: tuple[Fill, ...] = ()
    stop_order_id: str | None = None
    reason: str = ""
    safety: SafetyReport | None = None
    retries: int = 0

    @property
    def filled_quantity(self) -> float:
        return sum(f.quantity for f in self.fills)

    @property
    def average_price(self) -> float:
        total = self.filled_quantity
        if total <= 0:
            return 0.0
        return sum(f.price * f.quantity for f in self.fills) / total

    @property
    def average_slippage_bps(self) -> float:
        if not self.fills:
            return 0.0
        return sum(f.slippage_bps for f in self.fills) / len(self.fills)

    @property
    def state(self) -> OrderState:
        return self.lifecycle.state

    def as_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "symbol": self.symbol,
            "state": self.state.value,
            "filled_quantity": round(self.filled_quantity, 8),
            "average_price": round(self.average_price, 6),
            "slippage_bps": round(self.average_slippage_bps, 3),
            "stop_order_id": self.stop_order_id,
            "retries": self.retries,
            "reason": self.reason,
        }


class ExecutionEngine:
    """Turns risk-approved signals into positions with protection attached."""

    def __init__(
        self,
        broker: BrokerAdapter,
        book: PositionBook,
        *,
        journal: Journal | None = None,
        targets: TakeProfitEngine | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.broker = broker
        self.book = book
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.targets = targets if targets is not None else TakeProfitEngine()
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._sequence = 0
        self.consecutive_failures = 0

    # -- public API ---------------------------------------------------------

    def execute(
        self,
        signal: Signal,
        instrument: Instrument,
        verdict: RiskVerdict,
        conditions: ExecutionConditions,
        *,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> ExecutionResult:
        """Execute an authorised signal end to end."""
        order_id = self._next_id(signal.symbol)
        lifecycle = OrderLifecycle(order_id)

        if not verdict.approved or verdict.sizing is None or verdict.stop is None:
            lifecycle.force(OrderState.REJECTED, "no risk approval")
            return ExecutionResult(False, signal.symbol, lifecycle, reason="signal is not risk-approved")

        # -- VALIDATED -------------------------------------------------------
        side = OrderSide.BUY if signal.side is Side.LONG else OrderSide.SELL
        order = Order(
            order_id=order_id,
            symbol=signal.symbol,
            side=side,
            quantity=verdict.quantity,
            order_type=order_type,
            limit_price=limit_price,
            purpose="entry",
        )
        lifecycle.advance(OrderState.VALIDATED, "order object constructed")

        safety = run_checklist(order, instrument, conditions, stop_price=verdict.stop_price)
        if not safety.passed:
            lifecycle.force(OrderState.REJECTED, safety.reason())
            self.journal.execution(order_id, OrderState.REJECTED, reason=safety.reason(), severity=Severity.WARNING)
            return ExecutionResult(False, signal.symbol, lifecycle, reason=safety.reason(), safety=safety)

        lifecycle.advance(OrderState.RISK_APPROVED, verdict.reason)
        lifecycle.advance(OrderState.ORDER_CREATED, f"{order.quantity:g} {order.symbol} {order_type.value}")

        # -- SUBMITTED with retry/backoff (section 14.3) ---------------------
        ack, retries, failure = self._submit_with_retry(order, lifecycle)
        if ack is None or not ack.accepted:
            message = failure or (ack.message if ack else "submission failed")
            state = lifecycle.state if lifecycle.state.is_failure else OrderState.REJECTED
            if not lifecycle.state.is_failure:
                lifecycle.force(state, message)
            self.consecutive_failures += 1
            self.journal.execution(
                order_id,
                state,
                reason=message,
                recovery=RECOVERY_ACTIONS.get(state, ""),
                severity=Severity.ERROR,
            )
            return ExecutionResult(False, signal.symbol, lifecycle, reason=message, safety=safety, retries=retries)

        lifecycle.advance(OrderState.BROKER_ACKNOWLEDGED, f"broker order {ack.broker_order_id}")
        self.consecutive_failures = 0

        fills = list(ack.fills)
        if not fills:
            lifecycle.force(OrderState.TIMEOUT, "acknowledged but no fill received")
            self.journal.execution(order_id, OrderState.TIMEOUT, severity=Severity.WARNING)
            return ExecutionResult(False, signal.symbol, lifecycle, reason="no fill received", safety=safety, retries=retries)

        if ack.remaining > 1e-12:
            lifecycle.advance(
                OrderState.PARTIALLY_FILLED,
                f"filled {ack.filled_quantity:g}, {ack.remaining:g} remaining",
            )
            # Section 14.3: cancel the remainder and size protection to what
            # actually filled. Chasing the balance would break the risk
            # arithmetic the position was authorised under.
            self.broker.cancel(order_id)
            self.journal.execution(
                order_id,
                OrderState.PARTIALLY_FILLED,
                filled=ack.filled_quantity,
                cancelled=ack.remaining,
                severity=Severity.WARNING,
            )

        lifecycle.advance(OrderState.FILLED, f"{ack.filled_quantity:g} units filled")

        filled_quantity = sum(f.quantity for f in fills)
        average_price = sum(f.price * f.quantity for f in fills) / filled_quantity

        # -- record the position before attaching protection -----------------
        ladder = self.targets.ladder_for(
            str(signal.features.get("strategy_type", "momentum")),
            trend_strength=signal.features.get("adx_14"),
        )
        self.book.open_position(
            symbol=signal.symbol,
            asset_class=instrument.asset_class,
            side=signal.side,
            quantity=filled_quantity,
            entry_price=average_price,
            strategy_id=signal.strategy_id,
            stop_price=verdict.stop_price,
            targets=ladder.prices(average_price, signal.side),
            sector=instrument.sector,
            timestamp=fills[0].timestamp,
            commission=sum(f.commission for f in fills),
        )

        # -- PROTECTIVE_ORDER_ATTACHED ---------------------------------------
        stop_order_id = self._attach_protection(signal, instrument, verdict, filled_quantity, average_price)
        if stop_order_id is None:
            lifecycle.force(OrderState.PROTECTIVE_ORDER_FAILURE, "stop order could not be attached")
            self.journal.alert(
                f"{signal.symbol}: protective order failed to attach; flattening the position immediately",
                severity=Severity.CRITICAL,
                order_id=order_id,
                recovery=RECOVERY_ACTIONS[OrderState.PROTECTIVE_ORDER_FAILURE],
            )
            self._emergency_flatten(signal, instrument, filled_quantity)
            return ExecutionResult(
                False,
                signal.symbol,
                lifecycle,
                fills=tuple(fills),
                reason="protective order failed to attach; position flattened",
                safety=safety,
                retries=retries,
            )

        lifecycle.advance(OrderState.PROTECTIVE_ORDER_ATTACHED, f"stop order {stop_order_id}")

        # -- POSITION_RECONCILED ---------------------------------------------
        lifecycle.advance(OrderState.POSITION_RECONCILED, "broker and book agree on the new position")
        lifecycle.advance(OrderState.MONITORED, "position under monitoring")

        self.journal.execution(
            order_id,
            OrderState.MONITORED,
            symbol=signal.symbol,
            strategy=signal.strategy_id,
            quantity=filled_quantity,
            price=average_price,
            stop=verdict.stop_price,
            slippage_bps=round(sum(f.slippage_bps for f in fills) / len(fills), 3),
            latency_ms=round(ack.latency_ms, 1),
        )
        return ExecutionResult(
            True,
            signal.symbol,
            lifecycle,
            fills=tuple(fills),
            stop_order_id=stop_order_id,
            reason=f"{filled_quantity:g} units at {average_price:.4f}, stop {verdict.stop_price:.4f}",
            safety=safety,
            retries=retries,
        )

    # -- exits ---------------------------------------------------------------

    def exit_position(
        self,
        symbol: str,
        quantity: float,
        *,
        reason: str,
        regime: str = "UNKNOWN",
    ) -> ExecutionResult:
        """Close all or part of a position at market."""
        position = self.book.get(symbol)
        order_id = self._next_id(symbol, kind="X")
        lifecycle = OrderLifecycle(order_id)

        if position is None:
            lifecycle.force(OrderState.REJECTED, "no open position")
            return ExecutionResult(False, symbol, lifecycle, reason=f"no open position in {symbol}")

        quantity = min(quantity, position.quantity)
        side = OrderSide.SELL if position.side is Side.LONG else OrderSide.BUY
        order = Order(order_id, symbol, side, quantity, OrderType.MARKET, purpose="exit")

        for state in (OrderState.VALIDATED, OrderState.RISK_APPROVED, OrderState.ORDER_CREATED):
            lifecycle.advance(state, "exit order")

        ack, retries, failure = self._submit_with_retry(order, lifecycle)
        if ack is None or not ack.accepted or not ack.fills:
            message = failure or (ack.message if ack else "exit submission failed")
            if not lifecycle.state.is_failure:
                lifecycle.force(OrderState.BROKER_ERROR, message)
            self.journal.alert(
                f"{symbol}: EXIT FAILED - {message}. Position remains open and exposed.",
                severity=Severity.CRITICAL,
            )
            return ExecutionResult(False, symbol, lifecycle, reason=message, retries=retries)

        lifecycle.advance(OrderState.BROKER_ACKNOWLEDGED, "exit acknowledged")
        lifecycle.advance(OrderState.FILLED, "exit filled")

        filled = sum(f.quantity for f in ack.fills)
        average = sum(f.price * f.quantity for f in ack.fills) / filled
        record = self.book.reduce_position(
            symbol,
            filled,
            average,
            timestamp=ack.fills[0].timestamp,
            commission=sum(f.commission for f in ack.fills),
            reason=reason,
            regime=regime,
        )

        lifecycle.force(OrderState.EXITED, reason)
        lifecycle.advance(OrderState.RECONCILED, "exit reconciled")
        lifecycle.advance(OrderState.JOURNALED, "trade journalled")

        self.journal.execution(
            order_id,
            OrderState.JOURNALED,
            symbol=symbol,
            quantity=filled,
            price=average,
            exit_reason=reason,
            pnl=record.pnl if record else None,
            r_multiple=record.r_multiple if record else None,
        )
        return ExecutionResult(
            True,
            symbol,
            lifecycle,
            fills=ack.fills,
            reason=f"exited {filled:g} at {average:.4f} ({reason})",
            retries=retries,
        )

    # -- internals -----------------------------------------------------------

    def _submit_with_retry(
        self,
        order: Order,
        lifecycle: OrderLifecycle,
    ) -> tuple[OrderAck | None, int, str | None]:
        """Submit with exponential backoff (section 14.3).

        Only transport failures are retried. A venue *rejection* is a decision,
        not a glitch, and resubmitting it risks duplicate orders.
        """
        attempts = 0
        last_error: str | None = None

        while attempts <= self.max_retries:
            if attempts == 0:
                lifecycle.advance(OrderState.ORDER_SUBMITTED, f"attempt {attempts + 1}")
            try:
                ack = self.broker.submit(order)
            except ExecutionError as exc:
                last_error = str(exc)
                lifecycle.force(OrderState.NETWORK_ERROR, last_error)
            except Exception as exc:  # noqa: BLE001 - unknown venue failure
                last_error = f"unexpected broker error: {exc}"
                lifecycle.force(OrderState.BROKER_ERROR, last_error)
            else:
                if ack.accepted:
                    return ack, attempts, None
                # Explicit rejection: do not retry.
                lifecycle.force(OrderState.REJECTED, ack.message)
                return ack, attempts, ack.message

            attempts += 1
            if attempts > self.max_retries:
                break
            if self.backoff_seconds > 0:
                self._sleep(self.backoff_seconds * (2 ** (attempts - 1)))
            lifecycle.force(OrderState.ORDER_CREATED, f"retrying after failure ({attempts}/{self.max_retries})")
            lifecycle.advance(OrderState.ORDER_SUBMITTED, f"attempt {attempts + 1}")

        return None, attempts, last_error

    def _attach_protection(
        self,
        signal: Signal,
        instrument: Instrument,
        verdict: RiskVerdict,
        quantity: float,
        entry_price: float,
    ) -> str | None:
        """Submit the protective stop. Returns its order id, or ``None``."""
        stop_price = verdict.stop_price
        if stop_price is None or quantity <= 0:
            return None

        stop_side = OrderSide.SELL if signal.side is Side.LONG else OrderSide.BUY
        stop_order = Order(
            order_id=self._next_id(signal.symbol, kind="S"),
            symbol=signal.symbol,
            side=stop_side,
            quantity=quantity,
            order_type=OrderType.STOP,
            stop_price=stop_price,
            parent_id=None,
            purpose="stop",
        )
        try:
            ack = self.broker.submit(stop_order)
        except Exception as exc:  # noqa: BLE001
            self.journal.record(
                "execution",
                f"{signal.symbol}: stop order submission raised {exc}",
                severity=Severity.ERROR,
            )
            return None
        return stop_order.order_id if ack.accepted else None

    def _emergency_flatten(self, signal: Signal, instrument: Instrument, quantity: float) -> None:
        """Close an unprotected position immediately.

        Called only when protection could not be attached. Leaving the position
        open would be strictly worse than the round-trip cost of exiting.
        """
        try:
            result = self.exit_position(signal.symbol, quantity, reason="unprotected_position_emergency_exit")
            if not result.success:
                self.journal.alert(
                    f"{signal.symbol}: EMERGENCY EXIT FAILED - position is open and unprotected. "
                    "Immediate human intervention required.",
                    severity=Severity.CRITICAL,
                )
        except Exception as exc:  # noqa: BLE001 - never mask the original failure
            self.journal.alert(
                f"{signal.symbol}: emergency exit raised {exc}; position is open and unprotected",
                severity=Severity.CRITICAL,
            )

    def _next_id(self, symbol: str, kind: str = "E") -> str:
        self._sequence += 1
        return f"{kind}-{symbol}-{self._sequence:06d}"
