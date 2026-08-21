"""Exception hierarchy for TraderMaximus.

Every failure mode in the pipeline maps to an explicit exception so that the
orchestrator can decide between *halt*, *retry* and *skip* without inspecting
error strings.
"""

from __future__ import annotations


class TraderMaximusError(Exception):
    """Base class for every error raised by the system."""


class ConfigurationError(TraderMaximusError):
    """Configuration is missing, malformed or violates governance precedence."""


class DataQualityError(TraderMaximusError):
    """Market data failed validation. Per spec section 3.3 this means NO TRADE."""


class ToolUnavailableError(TraderMaximusError):
    """A required MCP tool/provider is not connected, authenticated or healthy."""


class RiskRejection(TraderMaximusError):
    """The risk engine refused to authorise a trade.

    Carries the machine-readable rule id so the journal can aggregate rejection
    reasons without parsing prose.
    """

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message


class ExecutionError(TraderMaximusError):
    """Order submission, fill handling or protective-order attachment failed."""


class ReconciliationError(TraderMaximusError):
    """Broker state and internal state disagree. Always a halt condition."""


class KillSwitchActive(TraderMaximusError):
    """The kill switch is engaged; no new risk may be taken."""


class CircuitBreakerActive(TraderMaximusError):
    """A daily/weekly/monthly circuit breaker has tripped."""
