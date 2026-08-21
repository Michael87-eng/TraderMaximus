"""Order lifecycle state machine (spec section 14.1-14.3).

The lifecycle is enforced, not merely documented: :func:`can_transition`
rejects any move the spec does not sanction. That matters most for the
``FILLED -> PROTECTIVE_ORDER_ATTACHED -> POSITION_RECONCILED`` stretch, where
skipping a step means a live position with no stop — the condition section 15.1
treats as a kill-switch trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping

from ..core.types import utc_now


class OrderState(str, Enum):
    """Section 14.1 happy path plus section 14.2 failure states."""

    INITIALIZED = "INITIALIZED"
    VALIDATED = "VALIDATED"
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    BROKER_ACKNOWLEDGED = "BROKER_ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTIVE_ORDER_ATTACHED = "PROTECTIVE_ORDER_ATTACHED"
    POSITION_RECONCILED = "POSITION_RECONCILED"
    MONITORED = "MONITORED"
    EXITED = "EXITED"
    RECONCILED = "RECONCILED"
    JOURNALED = "JOURNALED"

    # Failure states (section 14.2)
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    BROKER_ERROR = "BROKER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    PROTECTIVE_ORDER_FAILURE = "PROTECTIVE_ORDER_FAILURE"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    KILLED = "KILLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_failure(self) -> bool:
        return self in FAILURE_STATES

    @property
    def is_live(self) -> bool:
        """The order is working at the broker and may still fill."""
        return self in {
            OrderState.ORDER_SUBMITTED,
            OrderState.BROKER_ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
        }


FAILURE_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.TIMEOUT,
        OrderState.BROKER_ERROR,
        OrderState.NETWORK_ERROR,
        OrderState.DUPLICATE_ORDER,
        OrderState.PRICE_DEVIATION,
        OrderState.PROTECTIVE_ORDER_FAILURE,
        OrderState.RECONCILIATION_FAILURE,
        OrderState.KILLED,
    }
)

TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.JOURNALED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.KILLED,
    }
)

#: Sanctioned transitions. Any failure state is reachable from any live state,
#: which is added programmatically below rather than written out.
_HAPPY_PATH: Mapping[OrderState, frozenset[OrderState]] = {
    OrderState.INITIALIZED: frozenset({OrderState.VALIDATED}),
    OrderState.VALIDATED: frozenset({OrderState.RISK_APPROVED}),
    OrderState.RISK_APPROVED: frozenset({OrderState.ORDER_CREATED}),
    OrderState.ORDER_CREATED: frozenset({OrderState.ORDER_SUBMITTED}),
    OrderState.ORDER_SUBMITTED: frozenset({OrderState.BROKER_ACKNOWLEDGED}),
    OrderState.BROKER_ACKNOWLEDGED: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED}),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED}),
    OrderState.FILLED: frozenset({OrderState.PROTECTIVE_ORDER_ATTACHED}),
    OrderState.PROTECTIVE_ORDER_ATTACHED: frozenset({OrderState.POSITION_RECONCILED}),
    OrderState.POSITION_RECONCILED: frozenset({OrderState.MONITORED}),
    OrderState.MONITORED: frozenset({OrderState.MONITORED, OrderState.EXITED}),
    OrderState.EXITED: frozenset({OrderState.RECONCILED}),
    OrderState.RECONCILED: frozenset({OrderState.JOURNALED}),
    OrderState.JOURNALED: frozenset(),
}

#: States from which a failure may still occur.
_FAILURE_REACHABLE_FROM: frozenset[OrderState] = frozenset(
    {
        OrderState.INITIALIZED,
        OrderState.VALIDATED,
        OrderState.RISK_APPROVED,
        OrderState.ORDER_CREATED,
        OrderState.ORDER_SUBMITTED,
        OrderState.BROKER_ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.PROTECTIVE_ORDER_ATTACHED,
        OrderState.POSITION_RECONCILED,
        OrderState.MONITORED,
        OrderState.EXITED,
    }
)


def allowed_transitions(state: OrderState) -> frozenset[OrderState]:
    allowed = _HAPPY_PATH.get(state, frozenset())
    if state in _FAILURE_REACHABLE_FROM:
        allowed = allowed | FAILURE_STATES
    if state in FAILURE_STATES and state not in TERMINAL_STATES:
        # A recoverable failure may be journalled or retried from creation.
        allowed = allowed | {OrderState.JOURNALED, OrderState.ORDER_CREATED}
    return allowed


def can_transition(current: OrderState, target: OrderState) -> bool:
    return target in allowed_transitions(current)


@dataclass(frozen=True, slots=True)
class StateTransition:
    from_state: OrderState
    to_state: OrderState
    at: datetime = field(default_factory=utc_now)
    detail: str = ""


class InvalidTransition(Exception):
    """Raised when code attempts a lifecycle move the spec does not permit."""


@dataclass(slots=True)
class OrderLifecycle:
    """Tracks one order's state and full transition history."""

    order_id: str
    state: OrderState = OrderState.INITIALIZED
    history: list[StateTransition] = field(default_factory=list)

    def advance(self, target: OrderState, detail: str = "") -> StateTransition:
        if not can_transition(self.state, target):
            raise InvalidTransition(
                f"order {self.order_id}: cannot move {self.state.value} -> {target.value}; "
                f"allowed: {sorted(s.value for s in allowed_transitions(self.state))}"
            )
        transition = StateTransition(self.state, target, detail=detail)
        self.history.append(transition)
        self.state = target
        return transition

    def force(self, target: OrderState, detail: str = "") -> StateTransition:
        """Force a transition. Only for kill-switch and reconciliation halts."""
        transition = StateTransition(self.state, target, detail=f"FORCED: {detail}")
        self.history.append(transition)
        self.state = target
        return transition

    @property
    def is_protected(self) -> bool:
        """Whether a protective order has been confirmed attached."""
        return any(
            t.to_state is OrderState.PROTECTIVE_ORDER_ATTACHED for t in self.history
        )

    def path(self) -> list[str]:
        return [self.history[0].from_state.value] + [t.to_state.value for t in self.history] if self.history else [
            self.state.value
        ]


#: Section 14.3 recovery table.
RECOVERY_ACTIONS: Mapping[OrderState, str] = {
    OrderState.REJECTED: "cancel, investigate the reason, do not retry automatically",
    OrderState.CANCELLED: "verify cancellation, do not proceed",
    OrderState.TIMEOUT: "retry once, then cancel and investigate",
    OrderState.PARTIALLY_FILLED: "check remaining size, adjust the position or cancel the remainder",
    OrderState.BROKER_ERROR: "retry with exponential backoff up to 3 times, then cancel",
    OrderState.NETWORK_ERROR: "retry with exponential backoff up to 3 times, then halt",
    OrderState.DUPLICATE_ORDER: "verify execution, reconcile the position, do not duplicate",
    OrderState.PRICE_DEVIATION: "verify the price, adjust or cancel, log the deviation",
    OrderState.PROTECTIVE_ORDER_FAILURE: "verify the position, reconcile manually, alert the operator",
    OrderState.RECONCILIATION_FAILURE: "HALT, investigate, resolve before continuing",
    OrderState.KILLED: "full system halt, require human review",
}
