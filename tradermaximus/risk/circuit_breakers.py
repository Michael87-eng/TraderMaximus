"""Daily, weekly and monthly circuit breakers (spec section 16).

Breakers are *stateful* and *latching*: once a daily loss limit trips it stays
tripped until the next session, even if the market recovers within the same
day. A breaker that un-trips on a bounce is not a breaker — it is a suggestion,
and it would let a bad day become a catastrophic one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable

from ..core.types import utc_now


class BreakerScope(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class BreakerAction(str, Enum):
    """Escalating responses, per sections 16.1-16.3."""

    HALT_NEW_TRADES = "halt_new_trades"
    CANCEL_NON_ESSENTIAL_ORDERS = "cancel_non_essential_orders"
    CANCEL_ALL_ORDERS = "cancel_all_orders"
    REDUCE_EXPOSURE_50 = "reduce_exposure_to_50_percent"
    DEFENSIVE_MODE = "enter_defensive_mode"
    HALT_ALL_TRADING = "halt_all_trading"
    REQUIRE_HUMAN_REVIEW = "require_human_review"


@dataclass(frozen=True, slots=True)
class BreakerTrip:
    scope: BreakerScope
    rule: str
    value: float
    limit: float
    actions: tuple[BreakerAction, ...]
    tripped_at: datetime = field(default_factory=utc_now)
    message: str = ""

    @property
    def requires_human(self) -> bool:
        return BreakerAction.REQUIRE_HUMAN_REVIEW in self.actions

    @property
    def halts_new_trades(self) -> bool:
        return bool(
            {BreakerAction.HALT_NEW_TRADES, BreakerAction.HALT_ALL_TRADING} & set(self.actions)
        )


@dataclass(slots=True)
class BreakerState:
    """Rolling counters, reset on their own cadence."""

    session_date: date
    week_start: date
    month_start: date
    trades_today: int = 0
    orders_today: int = 0
    session_start_equity: float = 0.0
    week_start_equity: float = 0.0
    month_peak_equity: float = 0.0
    week_peak_equity: float = 0.0


class CircuitBreakerEngine:
    """Evaluates section 16 breakers and latches any that trip."""

    def __init__(
        self,
        *,
        max_daily_loss: float = 0.015,
        max_weekly_loss: float = 0.03,
        max_monthly_drawdown: float = 0.06,
        max_weekly_drawdown: float = 0.05,
        max_daily_trades: int = 20,
        max_daily_orders: int = 50,
    ) -> None:
        self.max_daily_loss = max_daily_loss
        self.max_weekly_loss = max_weekly_loss
        self.max_monthly_drawdown = max_monthly_drawdown
        self.max_weekly_drawdown = max_weekly_drawdown
        self.max_daily_trades = max_daily_trades
        self.max_daily_orders = max_daily_orders
        self._state: BreakerState | None = None
        self._trips: list[BreakerTrip] = []

    # -- lifecycle ----------------------------------------------------------

    def start_session(self, equity: float, when: datetime | None = None) -> None:
        """Begin a session, rolling counters and clearing daily latches."""
        when = when or utc_now()
        today = when.date()
        week_start = today.fromordinal(today.toordinal() - today.weekday())
        month_start = today.replace(day=1)

        previous = self._state
        if previous is None:
            self._state = BreakerState(
                session_date=today,
                week_start=week_start,
                month_start=month_start,
                session_start_equity=equity,
                week_start_equity=equity,
                month_peak_equity=equity,
                week_peak_equity=equity,
            )
            return

        new_week = week_start != previous.week_start
        new_month = month_start != previous.month_start

        self._state = BreakerState(
            session_date=today,
            week_start=week_start,
            month_start=month_start,
            trades_today=0,
            orders_today=0,
            session_start_equity=equity,
            week_start_equity=equity if new_week else previous.week_start_equity,
            month_peak_equity=equity if new_month else max(previous.month_peak_equity, equity),
            week_peak_equity=equity if new_week else max(previous.week_peak_equity, equity),
        )

        # Daily latches clear at the session boundary; weekly and monthly
        # persist until their own period rolls over.
        self._trips = [
            trip
            for trip in self._trips
            if not (
                trip.scope is BreakerScope.DAILY
                or (trip.scope is BreakerScope.WEEKLY and new_week)
                or (trip.scope is BreakerScope.MONTHLY and new_month)
            )
        ]

    @property
    def state(self) -> BreakerState:
        if self._state is None:
            raise RuntimeError("circuit breakers not initialised; call start_session first")
        return self._state

    def record_trade(self, count: int = 1) -> None:
        self.state.trades_today += count

    def record_order(self, count: int = 1) -> None:
        self.state.orders_today += count

    def mark_equity(self, equity: float) -> None:
        state = self.state
        state.month_peak_equity = max(state.month_peak_equity, equity)
        state.week_peak_equity = max(state.week_peak_equity, equity)

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, equity: float) -> list[BreakerTrip]:
        """Check every breaker, latching new trips. Returns the new trips only."""
        state = self.state
        self.mark_equity(equity)
        new_trips: list[BreakerTrip] = []

        def trip(scope: BreakerScope, rule: str, value: float, limit: float, actions: tuple[BreakerAction, ...]) -> None:
            if any(t.rule == rule for t in self._trips):
                return  # already latched
            record = BreakerTrip(
                scope=scope,
                rule=rule,
                value=value,
                limit=limit,
                actions=actions,
                message=f"{rule}: {value:.4f} breached limit {limit:.4f}",
            )
            self._trips.append(record)
            new_trips.append(record)

        # -- daily ----------------------------------------------------------
        if state.session_start_equity > 0:
            daily_loss = (state.session_start_equity - equity) / state.session_start_equity
            if daily_loss > self.max_daily_loss:
                trip(
                    BreakerScope.DAILY,
                    "DAILY_LOSS",
                    daily_loss,
                    self.max_daily_loss,
                    (BreakerAction.HALT_NEW_TRADES, BreakerAction.CANCEL_NON_ESSENTIAL_ORDERS),
                )

        if state.trades_today > self.max_daily_trades:
            trip(
                BreakerScope.DAILY,
                "DAILY_TRADE_COUNT",
                float(state.trades_today),
                float(self.max_daily_trades),
                (BreakerAction.CANCEL_ALL_ORDERS, BreakerAction.HALT_NEW_TRADES),
            )

        if state.orders_today > self.max_daily_orders:
            trip(
                BreakerScope.DAILY,
                "DAILY_ORDER_COUNT",
                float(state.orders_today),
                float(self.max_daily_orders),
                (BreakerAction.CANCEL_NON_ESSENTIAL_ORDERS,),
            )

        # -- weekly ---------------------------------------------------------
        if state.week_start_equity > 0:
            weekly_loss = (state.week_start_equity - equity) / state.week_start_equity
            if weekly_loss > self.max_weekly_loss:
                trip(
                    BreakerScope.WEEKLY,
                    "WEEKLY_LOSS",
                    weekly_loss,
                    self.max_weekly_loss,
                    (BreakerAction.HALT_NEW_TRADES, BreakerAction.REDUCE_EXPOSURE_50),
                )

        if state.week_peak_equity > 0:
            weekly_dd = (state.week_peak_equity - equity) / state.week_peak_equity
            if weekly_dd > self.max_weekly_drawdown:
                trip(
                    BreakerScope.WEEKLY,
                    "WEEKLY_DRAWDOWN",
                    weekly_dd,
                    self.max_weekly_drawdown,
                    (BreakerAction.DEFENSIVE_MODE,),
                )

        # -- monthly --------------------------------------------------------
        if state.month_peak_equity > 0:
            monthly_dd = (state.month_peak_equity - equity) / state.month_peak_equity
            if monthly_dd > self.max_monthly_drawdown:
                trip(
                    BreakerScope.MONTHLY,
                    "MONTHLY_DRAWDOWN",
                    monthly_dd,
                    self.max_monthly_drawdown,
                    (BreakerAction.HALT_ALL_TRADING, BreakerAction.REQUIRE_HUMAN_REVIEW),
                )

        return new_trips

    # -- status -------------------------------------------------------------

    @property
    def trips(self) -> tuple[BreakerTrip, ...]:
        return tuple(self._trips)

    @property
    def active(self) -> bool:
        return bool(self._trips)

    @property
    def blocks_new_trades(self) -> bool:
        return any(t.halts_new_trades for t in self._trips)

    @property
    def requires_human_review(self) -> bool:
        return any(t.requires_human for t in self._trips)

    def actions(self) -> set[BreakerAction]:
        return {action for trip in self._trips for action in trip.actions}

    def clear(self, scopes: Iterable[BreakerScope] | None = None) -> None:
        """Clear latched trips after a documented human review."""
        if scopes is None:
            self._trips.clear()
            return
        wanted = set(scopes)
        self._trips = [t for t in self._trips if t.scope not in wanted]

    def status(self) -> dict[str, object]:
        return {
            "active": self.active,
            "blocks_new_trades": self.blocks_new_trades,
            "requires_human_review": self.requires_human_review,
            "trips": [
                {"scope": t.scope.value, "rule": t.rule, "value": round(t.value, 4), "limit": t.limit}
                for t in self._trips
            ],
            "trades_today": self._state.trades_today if self._state else 0,
            "orders_today": self._state.orders_today if self._state else 0,
        }
