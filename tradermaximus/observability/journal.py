"""Audit journal (spec section 22.2).

Every decision, execution, risk verdict, data event and regime change is
appended here as one JSON object per line. The journal is the system's memory:
drift detection, the post-market review and any post-mortem all read from it.

Writes are append-only and flushed immediately — a crash must not cost the
record of the order that caused it.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


class EventType(str, Enum):
    TRADE_DECISION = "trade_decision"
    EXECUTION = "execution"
    RISK_DECISION = "risk_decision"
    SYSTEM = "system"
    DATA = "data"
    REGIME = "regime"
    POSITION = "position"
    RECONCILIATION = "reconciliation"
    DRIFT = "drift"
    ALERT = "alert"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _encode(value: Any) -> Any:
    """Best-effort JSON encoding that never loses an event to a type error."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _encode(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode(v) for v in value]
    return repr(value)


class Journal:
    """Append-only structured event log.

    Args:
        path: JSONL destination. ``None`` keeps events in memory only, which is
            what backtests and unit tests use.
        echo: also print events at or above :class:`Severity` ``WARNING``.
        retain: how many events to keep in the in-memory ring for querying.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        echo: bool = False,
        retain: int = 10_000,
    ) -> None:
        self.path = Path(path) if path else None
        self.echo = echo
        self.retain = retain
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing ------------------------------------------------------------

    def record(
        self,
        event_type: EventType | str,
        message: str,
        *,
        severity: Severity | str = Severity.INFO,
        **fields: Any,
    ) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type.value if isinstance(event_type, EventType) else str(event_type),
            "severity": severity.value if isinstance(severity, Severity) else str(severity),
            "message": message,
            **{k: _encode(v) for k, v in fields.items()},
        }
        with self._lock:
            self._counts[event["type"]] += 1
            self._events.append(event)
            if len(self._events) > self.retain:
                del self._events[: len(self._events) - self.retain]
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        if self.echo and event["severity"] in {Severity.WARNING.value, Severity.ERROR.value, Severity.CRITICAL.value}:
            print(f"[{event['severity'].upper()}] {event['type']}: {message}")
        return event

    # Convenience wrappers keep call sites readable at the point of decision.

    def trade_decision(self, symbol: str, decision: Any, reasoning: str, **fields: Any) -> dict[str, Any]:
        return self.record(
            EventType.TRADE_DECISION,
            reasoning,
            symbol=symbol,
            decision=decision,
            **fields,
        )

    def risk_decision(self, approved: bool, reason: str, **fields: Any) -> dict[str, Any]:
        return self.record(
            EventType.RISK_DECISION,
            reason,
            severity=Severity.INFO if approved else Severity.WARNING,
            approved=approved,
            **fields,
        )

    def execution(self, order_id: str, state: Any, **fields: Any) -> dict[str, Any]:
        return self.record(EventType.EXECUTION, f"order {order_id} -> {state}", order_id=order_id, state=state, **fields)

    def data_event(self, source: str, quality_score: float, issues: Iterable[str] = (), **fields: Any) -> dict[str, Any]:
        issues = list(issues)
        return self.record(
            EventType.DATA,
            f"{source} quality={quality_score:.3f}",
            severity=Severity.WARNING if issues else Severity.INFO,
            source=source,
            quality_score=quality_score,
            issues=issues,
            **fields,
        )

    def regime_change(self, previous: str, new: str, confidence: float, **fields: Any) -> dict[str, Any]:
        return self.record(
            EventType.REGIME,
            f"regime {previous} -> {new}",
            previous_regime=previous,
            new_regime=new,
            confidence_score=confidence,
            **fields,
        )

    def alert(self, message: str, severity: Severity | str = Severity.CRITICAL, **fields: Any) -> dict[str, Any]:
        return self.record(EventType.ALERT, message, severity=severity, **fields)

    # -- reading ------------------------------------------------------------

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            return iter(list(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def __bool__(self) -> bool:
        """A journal is always truthy.

        Without this, ``__len__`` makes an empty journal falsy, and the
        idiom ``journal or NULL_JOURNAL`` silently swaps a caller's real
        journal for the shared in-memory one — losing the entire audit
        trail precisely when nothing has been logged yet.
        """
        return True

    def events(
        self,
        event_type: EventType | str | None = None,
        severity: Severity | str | None = None,
    ) -> list[dict[str, Any]]:
        wanted_type = event_type.value if isinstance(event_type, EventType) else event_type
        wanted_sev = severity.value if isinstance(severity, Severity) else severity
        with self._lock:
            out = list(self._events)
        if wanted_type is not None:
            out = [e for e in out if e["type"] == wanted_type]
        if wanted_sev is not None:
            out = [e for e in out if e["severity"] == wanted_sev]
        return out

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def rejection_summary(self) -> dict[str, int]:
        """Rejected-trade reasons aggregated by rule id (section 27.1)."""
        summary: Counter[str] = Counter()
        for event in self.events(EventType.RISK_DECISION):
            if not event.get("approved", True):
                summary[str(event.get("rule", "unspecified"))] += 1
        return dict(summary)


NULL_JOURNAL = Journal()
"""Shared in-memory journal for contexts that do not configure one."""
