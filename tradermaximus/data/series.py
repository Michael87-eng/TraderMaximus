"""Candle series container.

A thin, immutable-ish wrapper that gives the indicator engine cheap column
access without pulling in pandas. Series are always sorted by timestamp and
carry the instrument they describe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Sequence

from ..core.types import Candle, Instrument, Timeframe, sorted_candles


@dataclass(frozen=True, slots=True)
class CandleSeries:
    instrument: Instrument
    timeframe: Timeframe
    candles: tuple[Candle, ...]

    @classmethod
    def build(
        cls,
        instrument: Instrument,
        timeframe: Timeframe,
        candles: Sequence[Candle],
    ) -> "CandleSeries":
        return cls(instrument, timeframe, tuple(sorted_candles(candles)))

    # -- sequence protocol --------------------------------------------------

    def __len__(self) -> int:
        return len(self.candles)

    def __iter__(self) -> Iterator[Candle]:
        return iter(self.candles)

    def __getitem__(self, index: int) -> Candle:
        return self.candles[index]

    # -- columns ------------------------------------------------------------

    @property
    def opens(self) -> list[float]:
        return [c.open for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        return [c.volume for c in self.candles]

    @property
    def timestamps(self) -> list[datetime]:
        return [c.timestamp for c in self.candles]

    @property
    def last(self) -> Candle:
        if not self.candles:
            raise IndexError("CandleSeries is empty")
        return self.candles[-1]

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    def tail(self, n: int) -> "CandleSeries":
        return CandleSeries(self.instrument, self.timeframe, self.candles[-n:] if n > 0 else ())

    def slice_until(self, timestamp: datetime) -> "CandleSeries":
        """Bars at or before ``timestamp``.

        The backtester relies on this to enforce point-in-time correctness: a
        decision may never see a bar that had not yet closed.
        """
        kept = tuple(c for c in self.candles if c.timestamp <= timestamp)
        return CandleSeries(self.instrument, self.timeframe, kept)

    def average_volume(self, periods: int = 50) -> float:
        window = self.volumes[-periods:]
        return sum(window) / len(window) if window else 0.0
