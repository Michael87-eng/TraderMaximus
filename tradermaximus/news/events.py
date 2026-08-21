"""Financial news and event engine (spec section 19).

Scheduled high-impact events are known in advance, so the system's posture
around them is deterministic rather than reactive: size down 30 minutes before,
open nothing during, resume cautiously afterwards. Unscheduled events are
detected from news sentiment and volatility, and always resolve to *less* risk.

The engine returns a :class:`EventPosture` — an instruction, not an
observation — so the orchestrator has nothing to interpret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Sequence

from ..core.types import utc_now


class EventImpact(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EventCategory(str, Enum):
    """Section 19.1 categories."""

    MONETARY_POLICY = "monetary_policy"      # FOMC, rate decisions, Fed speeches
    EMPLOYMENT = "employment"                # NFP
    INFLATION = "inflation"                  # CPI, PPI
    GROWTH = "growth"                        # GDP
    EARNINGS = "earnings"
    CORPORATE = "corporate_announcement"
    REGULATORY = "regulatory"
    GEOPOLITICAL = "geopolitical"
    PROTOCOL_UPGRADE = "protocol_upgrade"    # crypto
    MARKET_DISRUPTION = "market_disruption"
    OTHER = "other"


#: Categories that always count as high impact, whatever the feed says.
ALWAYS_HIGH_IMPACT: frozenset[EventCategory] = frozenset(
    {
        EventCategory.MONETARY_POLICY,
        EventCategory.EMPLOYMENT,
        EventCategory.INFLATION,
        EventCategory.MARKET_DISRUPTION,
    }
)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """A scheduled or detected event."""

    name: str
    category: EventCategory
    scheduled_at: datetime | None = None
    impact: EventImpact = EventImpact.MEDIUM
    symbols: tuple[str, ...] = ()          # empty means market-wide
    sentiment: float | None = None          # -1 (bearish) to +1 (bullish)
    source: str = "unknown"
    detected_at: datetime = field(default_factory=utc_now)

    @property
    def is_high_impact(self) -> bool:
        return self.impact is EventImpact.HIGH or self.category in ALWAYS_HIGH_IMPACT

    @property
    def is_scheduled(self) -> bool:
        return self.scheduled_at is not None

    def affects(self, symbol: str) -> bool:
        return not self.symbols or symbol in self.symbols

    def minutes_until(self, now: datetime) -> float | None:
        if self.scheduled_at is None:
            return None
        return (self.scheduled_at - now).total_seconds() / 60.0


class Posture(str, Enum):
    """What the system is permitted to do right now."""

    NORMAL = "normal"
    REDUCED = "reduced"          # half size, no new positions
    NO_NEW_POSITIONS = "no_new_positions"
    DEFENSIVE = "defensive"      # close speculative positions
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class EventPosture:
    """The instruction derived from active events."""

    posture: Posture
    size_multiplier: float
    allow_new_positions: bool
    reason: str
    events: tuple[MarketEvent, ...] = ()

    @property
    def is_restricted(self) -> bool:
        return self.posture is not Posture.NORMAL

    def as_dict(self) -> dict[str, object]:
        return {
            "posture": self.posture.value,
            "size_multiplier": self.size_multiplier,
            "allow_new_positions": self.allow_new_positions,
            "reason": self.reason,
            "events": [e.name for e in self.events],
        }


NORMAL_POSTURE = EventPosture(Posture.NORMAL, 1.0, True, "no high-impact events active")


class EventEngine:
    """Tracks events and derives the trading posture (section 19.2)."""

    def __init__(
        self,
        *,
        pre_event_minutes: float = 30.0,
        post_event_minutes: float = 60.0,
        sentiment_threshold: float = 0.6,
    ) -> None:
        self.pre_event_minutes = pre_event_minutes
        self.post_event_minutes = post_event_minutes
        self.sentiment_threshold = sentiment_threshold
        self._events: list[MarketEvent] = []

    def add(self, event: MarketEvent) -> None:
        self._events.append(event)

    def extend(self, events: Iterable[MarketEvent]) -> None:
        self._events.extend(events)

    def clear(self) -> None:
        self._events.clear()

    @property
    def events(self) -> tuple[MarketEvent, ...]:
        return tuple(self._events)

    def upcoming(self, now: datetime | None = None, within_minutes: float = 1440.0) -> list[MarketEvent]:
        now = now or utc_now()
        out = []
        for event in self._events:
            minutes = event.minutes_until(now)
            if minutes is not None and 0 <= minutes <= within_minutes:
                out.append(event)
        return sorted(out, key=lambda e: e.scheduled_at or now)

    def posture(
        self,
        *,
        now: datetime | None = None,
        symbol: str | None = None,
        volatility_spike: float | None = None,
    ) -> EventPosture:
        """Derive the current posture for the market or one symbol."""
        now = now or utc_now()
        relevant = [e for e in self._events if e.is_high_impact and (symbol is None or e.affects(symbol))]

        in_window: list[MarketEvent] = []
        imminent: list[MarketEvent] = []
        aftermath: list[MarketEvent] = []

        for event in relevant:
            minutes = event.minutes_until(now)
            if minutes is None:
                # Unscheduled: treat as active for the post-event window.
                age = (now - event.detected_at).total_seconds() / 60.0
                if age <= self.post_event_minutes:
                    in_window.append(event)
                continue
            if 0 <= minutes <= self.pre_event_minutes:
                imminent.append(event)
            elif -self.post_event_minutes <= minutes < 0:
                aftermath.append(event)

        # An unscheduled high-impact event with a volatility spike is the most
        # severe case: the market is repricing and the system has no edge in it.
        if in_window and volatility_spike is not None and volatility_spike > 0.30:
            return EventPosture(
                Posture.DEFENSIVE,
                0.0,
                False,
                f"unscheduled high-impact event with a {volatility_spike:.0%} volatility spike; "
                "closing speculative exposure",
                tuple(in_window),
            )

        if in_window:
            return EventPosture(
                Posture.NO_NEW_POSITIONS,
                0.5,
                False,
                f"{len(in_window)} high-impact event(s) in progress: "
                + ", ".join(e.name for e in in_window),
                tuple(in_window),
            )

        if imminent:
            soonest = min(imminent, key=lambda e: e.minutes_until(now) or 0.0)
            return EventPosture(
                Posture.REDUCED,
                0.5,
                False,
                f"{soonest.name} in {soonest.minutes_until(now):.0f} minutes; "
                "halving size and opening no new positions",
                tuple(imminent),
            )

        if aftermath:
            elapsed = max(abs(e.minutes_until(now) or 0.0) for e in aftermath)
            return EventPosture(
                Posture.REDUCED,
                0.75,
                True,
                f"within {elapsed:.0f} minutes of a high-impact event; resuming with reduced size",
                tuple(aftermath),
            )

        return NORMAL_POSTURE

    # -- news interpretation -------------------------------------------------

    def detect_from_headlines(
        self,
        headlines: Sequence[tuple[str, float]],
        *,
        symbols: Sequence[str] = (),
        source: str = "news",
    ) -> list[MarketEvent]:
        """Turn (headline, sentiment) pairs into events.

        Only strongly-signed sentiment produces an event. Mild sentiment is
        noise, and reacting to it would make the system trade the news feed
        rather than the market.
        """
        detected: list[MarketEvent] = []
        for headline, sentiment in headlines:
            if abs(sentiment) < self.sentiment_threshold:
                continue
            category = _categorise(headline)
            detected.append(
                MarketEvent(
                    name=headline[:120],
                    category=category,
                    impact=EventImpact.HIGH if abs(sentiment) >= 0.8 else EventImpact.MEDIUM,
                    symbols=tuple(symbols),
                    sentiment=sentiment,
                    source=source,
                )
            )
        self.extend(detected)
        return detected


_KEYWORDS: tuple[tuple[EventCategory, tuple[str, ...]], ...] = (
    (EventCategory.MONETARY_POLICY, ("fomc", "fed ", "federal reserve", "interest rate", "rate decision", "ecb", "boe")),
    (EventCategory.EMPLOYMENT, ("nonfarm", "payroll", "jobs report", "unemployment")),
    (EventCategory.INFLATION, ("cpi", "ppi", "inflation")),
    (EventCategory.GROWTH, ("gdp", "recession")),
    (EventCategory.EARNINGS, ("earnings", "eps", "guidance", "quarterly results")),
    (EventCategory.REGULATORY, ("sec ", "regulator", "lawsuit", "investigation", "probe")),
    (EventCategory.GEOPOLITICAL, ("war", "sanction", "conflict", "tariff", "election")),
    (EventCategory.PROTOCOL_UPGRADE, ("hard fork", "upgrade", "halving", "mainnet")),
    (EventCategory.MARKET_DISRUPTION, ("halt", "circuit breaker", "outage", "flash crash")),
)


def _categorise(headline: str) -> EventCategory:
    lowered = headline.lower()
    for category, keywords in _KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return EventCategory.OTHER
