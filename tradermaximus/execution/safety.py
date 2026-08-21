"""Pre-execution safety checklist (spec section 17.1).

Seventeen checks that must all pass before an order reaches a venue. The
checklist runs *after* risk authorisation and immediately before submission,
because the market can move between the two: a spread that was 8bps when the
signal was scored can be 80bps by the time the order is ready.

The checklist returns every failure rather than short-circuiting on the first,
so a single journal entry explains everything that was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from ..core.types import Instrument, Order, OrderType, Quote, utc_now


@dataclass(frozen=True, slots=True)
class SafetyCheck:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"[{'OK' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class SafetyReport:
    checks: tuple[SafetyCheck, ...]
    checked_at: datetime = field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[SafetyCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def reason(self) -> str:
        if self.passed:
            return f"all {len(self.checks)} pre-execution checks passed"
        return "; ".join(f"{c.name} ({c.detail})" for c in self.failures)


@dataclass(frozen=True, slots=True)
class ExecutionConditions:
    """Live conditions at the moment of submission."""

    quote: Quote | None = None
    market_open: bool = True
    data_age_seconds: float | None = None
    average_daily_volume: float | None = None
    buying_power: float | None = None
    kill_switch_active: bool = False
    circuit_breaker_active: bool = False
    risk_approved: bool = False
    protective_order_supported: bool = True
    broker_supports_order_type: bool = True
    max_spread_bps: float = 50.0
    max_data_age_seconds: float = 5.0
    max_participation: float = 0.10


def run_checklist(
    order: Order,
    instrument: Instrument,
    conditions: ExecutionConditions,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> SafetyReport:
    """Run every section 17.1 check and return the full report."""
    checks: list[SafetyCheck] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append(SafetyCheck(name, passed, detail))

    # 1-2. Instrument validity
    check(
        "symbol_valid_and_active",
        bool(instrument.symbol.strip()) and instrument.active,
        f"{instrument.symbol} active={instrument.active}",
    )
    check("market_open", conditions.market_open, "market session is open" if conditions.market_open else "market closed")

    # 3. Data freshness
    if conditions.data_age_seconds is None:
        check("data_fresh", False, "data age unknown")
    else:
        fresh = conditions.data_age_seconds <= conditions.max_data_age_seconds
        check(
            "data_fresh",
            fresh,
            f"{conditions.data_age_seconds:.1f}s old (max {conditions.max_data_age_seconds:.1f}s)",
        )

    # 4. Spread
    if conditions.quote is None:
        check("spread_acceptable", False, "no quote available")
    else:
        spread = conditions.quote.spread_bps
        check(
            "spread_acceptable",
            spread <= conditions.max_spread_bps,
            f"{spread:.1f}bps (max {conditions.max_spread_bps:.1f}bps)",
        )
        check(
            "quote_not_crossed",
            conditions.quote.ask >= conditions.quote.bid > 0,
            f"bid={conditions.quote.bid} ask={conditions.quote.ask}",
        )

    # 5. Liquidity and participation
    if conditions.average_daily_volume is None:
        check("liquidity_sufficient", False, "average daily volume unknown")
    else:
        participation = order.quantity / conditions.average_daily_volume if conditions.average_daily_volume > 0 else 1.0
        check(
            "liquidity_sufficient",
            participation <= conditions.max_participation,
            f"order is {participation:.2%} of ADV (max {conditions.max_participation:.0%})",
        )

    # 6. Risk approval
    check("risk_engine_approved", conditions.risk_approved, "risk verdict attached")

    # 7-8. Order shape
    check("quantity_valid", order.quantity > 0, f"quantity={order.quantity:g}")
    check(
        "quantity_meets_minimum",
        order.quantity >= instrument.min_order_size,
        f"{order.quantity:g} vs minimum {instrument.min_order_size:g}",
    )

    # 9-10. Prices
    if order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
        check("limit_price_present", order.limit_price is not None and order.limit_price > 0, f"limit={order.limit_price}")
    if order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
        check("stop_price_present", order.stop_price is not None and order.stop_price > 0, f"stop={order.stop_price}")

    if stop_price is not None:
        check("protective_stop_defined", stop_price > 0, f"stop at {stop_price:g}")
    if target_price is not None:
        check("target_price_valid", target_price > 0, f"target at {target_price:g}")

    # 11. Funding
    if conditions.buying_power is not None and conditions.quote is not None:
        required = order.quantity * conditions.quote.ask
        check(
            "sufficient_buying_power",
            required <= conditions.buying_power,
            f"needs {required:,.2f}, have {conditions.buying_power:,.2f}",
        )

    # 12-13. System halts
    check("kill_switch_inactive", not conditions.kill_switch_active, "kill switch state")
    check("circuit_breaker_inactive", not conditions.circuit_breaker_active, "circuit breaker state")

    # 14-15. Venue capability
    check(
        "order_type_supported",
        conditions.broker_supports_order_type,
        f"{order.order_type.value} supported by venue",
    )
    check(
        "protective_order_attachable",
        conditions.protective_order_supported,
        "venue accepts protective orders",
    )

    return SafetyReport(tuple(checks))
