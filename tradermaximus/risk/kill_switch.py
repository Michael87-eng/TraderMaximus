"""Emergency kill switch (spec section 15).

The kill switch is the last line of defence, and it is deliberately hard to
turn off: it engages automatically on any section 15.1 trigger and can only be
released by an explicit human action recorded with a reason. Nothing in the
automated pipeline can re-enable it, because the conditions that trip it are
exactly the conditions under which the system's own judgement is least
trustworthy — corrupted state, broker disagreement, stale data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Sequence

from ..core.types import utc_now
from ..observability.journal import Journal, NULL_JOURNAL, Severity


class KillTrigger(str, Enum):
    """Section 15.1 triggers."""

    CATASTROPHIC_DRAWDOWN = "catastrophic_drawdown"
    UNEXPECTED_LEVERAGE = "unexpected_leverage"
    UNAUTHORIZED_POSITION = "unauthorized_position"
    BROKER_MISMATCH = "broker_position_mismatch"
    STALE_MARKET_DATA = "stale_market_data"
    REPEATED_ORDER_FAILURES = "repeated_order_failures"
    MISSING_PROTECTIVE_ORDER = "missing_protective_order"
    ABNORMAL_SLIPPAGE = "abnormal_slippage"
    EXCHANGE_INSTABILITY = "exchange_instability"
    PROVIDER_DISAGREEMENT = "provider_disagreement"
    INFRASTRUCTURE_COMPROMISE = "infrastructure_compromise"
    CORRUPTED_PORTFOLIO_STATE = "corrupted_portfolio_state"
    MAX_DAILY_LOSS = "max_daily_loss_reached"
    MANUAL = "manual_activation"


class KillAction(str, Enum):
    """Section 15.2 actions, executed in this order."""

    STOP_NEW_TRADES = "stop_new_trades"
    CANCEL_PENDING_ORDERS = "cancel_pending_orders"
    RECONCILE_POSITIONS = "reconcile_open_positions"
    PROTECT_CAPITAL = "protect_capital"
    ALERT_OPERATOR = "alert_operator"
    LOG_INCIDENT = "log_incident"
    DOCUMENT_STATE = "document_state"
    REQUIRE_HUMAN_REVIEW = "require_human_review_for_reenablement"


ACTION_SEQUENCE: tuple[KillAction, ...] = (
    KillAction.STOP_NEW_TRADES,
    KillAction.CANCEL_PENDING_ORDERS,
    KillAction.RECONCILE_POSITIONS,
    KillAction.PROTECT_CAPITAL,
    KillAction.ALERT_OPERATOR,
    KillAction.LOG_INCIDENT,
    KillAction.DOCUMENT_STATE,
    KillAction.REQUIRE_HUMAN_REVIEW,
)


@dataclass(frozen=True, slots=True)
class KillEvent:
    trigger: KillTrigger
    detail: str
    value: float | None = None
    threshold: float | None = None
    engaged_at: datetime = field(default_factory=utc_now)

    def __str__(self) -> str:
        return f"{self.trigger.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class SystemSignals:
    """Inputs the kill switch inspects each cycle.

    Every field is optional; a signal the system cannot currently observe must
    not be silently treated as healthy, so unknown values simply skip their
    check rather than passing it.
    """

    session_drawdown: float | None = None
    leverage: float | None = None
    daily_loss: float | None = None
    broker_position_mismatch: float | None = None
    data_age_seconds: float | None = None
    consecutive_order_failures: int = 0
    unprotected_position_seconds: float | None = None
    slippage_bps: float | None = None
    provider_disagreement: float | None = None
    unauthorized_symbols: Sequence[str] = ()
    exchange_unstable: bool = False
    infrastructure_compromised: bool = False
    portfolio_state_corrupt: bool = False


@dataclass(frozen=True, slots=True)
class KillThresholds:
    """Section 15.1 thresholds."""

    catastrophic_drawdown: float = 0.10
    max_leverage: float = 1.0
    max_daily_loss: float = 0.015
    broker_mismatch: float = 0.05
    stale_data_seconds: float = 300.0
    consecutive_order_failures: int = 3
    unprotected_position_seconds: float = 300.0
    abnormal_slippage_bps: float = 50.0
    provider_disagreement: float = 0.02


class KillSwitch:
    """Engages on any section 15.1 trigger; releases only on human review."""

    def __init__(
        self,
        thresholds: KillThresholds | None = None,
        *,
        journal: Journal | None = None,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.thresholds = thresholds or KillThresholds()
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.alert = alert
        self._events: list[KillEvent] = []
        self._engaged_at: datetime | None = None
        self._actions_taken: list[KillAction] = []

    # -- state --------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._engaged_at is not None

    @property
    def events(self) -> tuple[KillEvent, ...]:
        return tuple(self._events)

    @property
    def engaged_at(self) -> datetime | None:
        return self._engaged_at

    # -- evaluation ---------------------------------------------------------

    def check(self, signals: SystemSignals) -> list[KillEvent]:
        """Evaluate every trigger, engaging on the first breach found.

        Returns all triggers detected this cycle, not just the first, so an
        incident report names every contributing condition.
        """
        t = self.thresholds
        found: list[KillEvent] = []

        def add(trigger: KillTrigger, detail: str, value: float | None = None, threshold: float | None = None) -> None:
            found.append(KillEvent(trigger, detail, value, threshold))

        if signals.session_drawdown is not None and signals.session_drawdown > t.catastrophic_drawdown:
            add(
                KillTrigger.CATASTROPHIC_DRAWDOWN,
                f"session drawdown {signals.session_drawdown:.2%} exceeds {t.catastrophic_drawdown:.2%}",
                signals.session_drawdown,
                t.catastrophic_drawdown,
            )

        if signals.leverage is not None and signals.leverage > t.max_leverage:
            add(
                KillTrigger.UNEXPECTED_LEVERAGE,
                f"leverage {signals.leverage:.2f} exceeds {t.max_leverage:.2f}",
                signals.leverage,
                t.max_leverage,
            )

        if signals.daily_loss is not None and signals.daily_loss > t.max_daily_loss:
            add(
                KillTrigger.MAX_DAILY_LOSS,
                f"daily loss {signals.daily_loss:.2%} reached the {t.max_daily_loss:.2%} limit",
                signals.daily_loss,
                t.max_daily_loss,
            )

        if signals.broker_position_mismatch is not None and signals.broker_position_mismatch > t.broker_mismatch:
            add(
                KillTrigger.BROKER_MISMATCH,
                f"broker/internal mismatch {signals.broker_position_mismatch:.2%} exceeds {t.broker_mismatch:.2%}",
                signals.broker_position_mismatch,
                t.broker_mismatch,
            )

        if signals.data_age_seconds is not None and signals.data_age_seconds > t.stale_data_seconds:
            add(
                KillTrigger.STALE_MARKET_DATA,
                f"market data is {signals.data_age_seconds:.0f}s old, limit {t.stale_data_seconds:.0f}s",
                signals.data_age_seconds,
                t.stale_data_seconds,
            )

        if signals.consecutive_order_failures > t.consecutive_order_failures:
            add(
                KillTrigger.REPEATED_ORDER_FAILURES,
                f"{signals.consecutive_order_failures} consecutive order failures",
                float(signals.consecutive_order_failures),
                float(t.consecutive_order_failures),
            )

        if (
            signals.unprotected_position_seconds is not None
            and signals.unprotected_position_seconds > t.unprotected_position_seconds
        ):
            add(
                KillTrigger.MISSING_PROTECTIVE_ORDER,
                f"a position has been unprotected for {signals.unprotected_position_seconds:.0f}s",
                signals.unprotected_position_seconds,
                t.unprotected_position_seconds,
            )

        if signals.slippage_bps is not None and signals.slippage_bps > t.abnormal_slippage_bps:
            add(
                KillTrigger.ABNORMAL_SLIPPAGE,
                f"slippage {signals.slippage_bps:.1f}bps exceeds {t.abnormal_slippage_bps:.1f}bps",
                signals.slippage_bps,
                t.abnormal_slippage_bps,
            )

        if signals.provider_disagreement is not None and signals.provider_disagreement > t.provider_disagreement:
            add(
                KillTrigger.PROVIDER_DISAGREEMENT,
                f"data providers disagree by {signals.provider_disagreement:.2%}",
                signals.provider_disagreement,
                t.provider_disagreement,
            )

        if signals.unauthorized_symbols:
            add(
                KillTrigger.UNAUTHORIZED_POSITION,
                f"unauthorised positions detected: {', '.join(signals.unauthorized_symbols)}",
            )

        if signals.exchange_unstable:
            add(KillTrigger.EXCHANGE_INSTABILITY, "exchange reported unstable")
        if signals.infrastructure_compromised:
            add(KillTrigger.INFRASTRUCTURE_COMPROMISE, "infrastructure compromise detected")
        if signals.portfolio_state_corrupt:
            add(KillTrigger.CORRUPTED_PORTFOLIO_STATE, "portfolio state failed its integrity check")

        for event in found:
            self._engage(event)
        return found

    def engage(self, trigger: KillTrigger, detail: str) -> KillEvent:
        """Engage manually (operator action or an unrecoverable exception)."""
        event = KillEvent(trigger, detail)
        self._engage(event)
        return event

    def _engage(self, event: KillEvent) -> None:
        first = not self.active
        self._events.append(event)
        if first:
            self._engaged_at = event.engaged_at
            self._actions_taken = list(ACTION_SEQUENCE)

        self.journal.alert(
            f"KILL SWITCH: {event}",
            severity=Severity.CRITICAL,
            trigger=event.trigger.value,
            detail=event.detail,
            value=event.value,
            threshold=event.threshold,
            actions=[a.value for a in ACTION_SEQUENCE],
        )
        if self.alert is not None:
            try:
                self.alert(f"KILL SWITCH ENGAGED - {event}")
            except Exception as exc:  # noqa: BLE001 - alerting must never mask the kill
                self.journal.record(
                    "system",
                    f"alert delivery failed during kill switch engagement: {exc}",
                    severity=Severity.ERROR,
                )

    # -- release ------------------------------------------------------------

    def release(self, *, operator: str, reason: str) -> None:
        """Release after human review. Both arguments are mandatory and logged."""
        if not self.active:
            return
        if not operator.strip() or not reason.strip():
            raise ValueError("releasing the kill switch requires both an operator and a reason")
        self.journal.record(
            "system",
            f"kill switch released by {operator}: {reason}",
            severity=Severity.WARNING,
            operator=operator,
            reason=reason,
            events=[str(e) for e in self._events],
        )
        self._events.clear()
        self._engaged_at = None
        self._actions_taken.clear()

    # -- reporting ----------------------------------------------------------

    def incident_report(self) -> dict[str, object]:
        """Section 15.2 documentation of the incident."""
        return {
            "active": self.active,
            "engaged_at": self._engaged_at.isoformat() if self._engaged_at else None,
            "triggers": [
                {
                    "trigger": e.trigger.value,
                    "detail": e.detail,
                    "value": e.value,
                    "threshold": e.threshold,
                    "at": e.engaged_at.isoformat(),
                }
                for e in self._events
            ],
            "actions": [a.value for a in self._actions_taken],
            "requires_human_review": self.active,
        }
