"""Reconciliation engine (spec section 14, final mandate).

"Reconciliation is the verification. No reconciliation = no trust."

The broker is authoritative. When the internal book and the broker disagree,
the internal book is wrong by definition, and the disagreement is a halt
condition rather than something to patch over. This module measures the
disagreement and classifies its severity; acting on it is the orchestrator's
job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from ..core.types import Side, utc_now
from ..execution.brokers.base import BrokerPosition
from .book import PositionBook


class DiscrepancyType(str, Enum):
    MISSING_INTERNALLY = "position_at_broker_not_in_book"
    MISSING_AT_BROKER = "position_in_book_not_at_broker"
    QUANTITY_MISMATCH = "quantity_mismatch"
    SIDE_MISMATCH = "side_mismatch"
    PRICE_MISMATCH = "entry_price_mismatch"
    EQUITY_MISMATCH = "equity_mismatch"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    type: DiscrepancyType
    symbol: str
    internal: float | None
    broker: float | None
    detail: str

    @property
    def is_critical(self) -> bool:
        """Position-level disagreements are always critical.

        An unknown position is the section 15.1 ``unauthorized_position``
        trigger; a quantity mismatch means the system's risk arithmetic is
        being done on numbers that do not exist.
        """
        return self.type in {
            DiscrepancyType.MISSING_INTERNALLY,
            DiscrepancyType.MISSING_AT_BROKER,
            DiscrepancyType.QUANTITY_MISMATCH,
            DiscrepancyType.SIDE_MISMATCH,
        }

    def __str__(self) -> str:
        return f"{self.type.value} [{self.symbol}]: {self.detail}"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    matched: tuple[str, ...]
    discrepancies: tuple[Discrepancy, ...]
    internal_equity: float
    broker_equity: float
    checked_at: datetime = field(default_factory=utc_now)

    @property
    def reconciled(self) -> bool:
        return not self.discrepancies

    @property
    def has_critical(self) -> bool:
        return any(d.is_critical for d in self.discrepancies)

    @property
    def equity_deviation(self) -> float:
        if self.broker_equity == 0:
            return 0.0 if self.internal_equity == 0 else 1.0
        return abs(self.internal_equity - self.broker_equity) / abs(self.broker_equity)

    @property
    def max_position_deviation(self) -> float:
        """Largest relative quantity disagreement, for the kill-switch signal."""
        worst = 0.0
        for d in self.discrepancies:
            if d.type is DiscrepancyType.QUANTITY_MISMATCH and d.broker:
                worst = max(worst, abs((d.internal or 0.0) - d.broker) / abs(d.broker))
            elif d.type in {DiscrepancyType.MISSING_INTERNALLY, DiscrepancyType.MISSING_AT_BROKER}:
                worst = max(worst, 1.0)
        return worst

    def summary(self) -> str:
        if self.reconciled:
            return f"reconciled: {len(self.matched)} positions match, equity deviation {self.equity_deviation:.4%}"
        return f"{len(self.discrepancies)} discrepancies: " + "; ".join(str(d) for d in self.discrepancies[:5])

    def as_dict(self) -> dict[str, object]:
        return {
            "reconciled": self.reconciled,
            "has_critical": self.has_critical,
            "matched": list(self.matched),
            "discrepancies": [
                {"type": d.type.value, "symbol": d.symbol, "internal": d.internal, "broker": d.broker}
                for d in self.discrepancies
            ],
            "internal_equity": round(self.internal_equity, 2),
            "broker_equity": round(self.broker_equity, 2),
            "equity_deviation": round(self.equity_deviation, 6),
        }


class ReconciliationEngine:
    """Compares the internal book against broker state."""

    def __init__(
        self,
        *,
        quantity_tolerance: float = 1e-6,
        price_tolerance: float = 0.001,
        equity_tolerance: float = 0.005,
    ) -> None:
        self.quantity_tolerance = quantity_tolerance
        self.price_tolerance = price_tolerance
        self.equity_tolerance = equity_tolerance

    def reconcile(
        self,
        book: PositionBook,
        broker_positions: Sequence[BrokerPosition],
        broker_equity: float,
    ) -> ReconciliationReport:
        broker_by_symbol: Mapping[str, BrokerPosition] = {p.symbol: p for p in broker_positions}
        internal_by_symbol = {p.symbol: p for p in book.positions}

        matched: list[str] = []
        discrepancies: list[Discrepancy] = []

        for symbol, position in internal_by_symbol.items():
            broker = broker_by_symbol.get(symbol)
            if broker is None:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyType.MISSING_AT_BROKER,
                        symbol,
                        position.quantity,
                        None,
                        f"book holds {position.quantity:g} units; the broker reports no position",
                    )
                )
                continue

            # Compare signed quantities so a flipped side is caught explicitly.
            internal_signed = position.quantity * position.side.sign
            broker_signed = broker.quantity

            if internal_signed * broker_signed < 0:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyType.SIDE_MISMATCH,
                        symbol,
                        internal_signed,
                        broker_signed,
                        f"book is {position.side.value}, broker holds {broker_signed:g}",
                    )
                )
                continue

            if abs(abs(internal_signed) - abs(broker_signed)) > self.quantity_tolerance:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyType.QUANTITY_MISMATCH,
                        symbol,
                        internal_signed,
                        broker_signed,
                        f"book {internal_signed:g} vs broker {broker_signed:g}",
                    )
                )
                continue

            if broker.average_price > 0:
                deviation = abs(position.entry_price - broker.average_price) / broker.average_price
                if deviation > self.price_tolerance:
                    discrepancies.append(
                        Discrepancy(
                            DiscrepancyType.PRICE_MISMATCH,
                            symbol,
                            position.entry_price,
                            broker.average_price,
                            f"entry {position.entry_price:g} vs broker average {broker.average_price:g} "
                            f"({deviation:.2%})",
                        )
                    )
                    continue

            matched.append(symbol)

        for symbol, broker in broker_by_symbol.items():
            if symbol not in internal_by_symbol and abs(broker.quantity) > self.quantity_tolerance:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyType.MISSING_INTERNALLY,
                        symbol,
                        None,
                        broker.quantity,
                        f"broker holds {broker.quantity:g} units the book does not know about",
                    )
                )

        internal_equity = book.equity
        if broker_equity > 0:
            deviation = abs(internal_equity - broker_equity) / broker_equity
            if deviation > self.equity_tolerance:
                discrepancies.append(
                    Discrepancy(
                        DiscrepancyType.EQUITY_MISMATCH,
                        "ACCOUNT",
                        internal_equity,
                        broker_equity,
                        f"book equity {internal_equity:,.2f} vs broker {broker_equity:,.2f} ({deviation:.2%})",
                    )
                )

        return ReconciliationReport(
            matched=tuple(matched),
            discrepancies=tuple(discrepancies),
            internal_equity=internal_equity,
            broker_equity=broker_equity,
        )

    def unauthorized_symbols(self, report: ReconciliationReport) -> list[str]:
        """Symbols the broker holds that the system never opened."""
        return [d.symbol for d in report.discrepancies if d.type is DiscrepancyType.MISSING_INTERNALLY]
