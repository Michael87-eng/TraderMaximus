"""Broker adapter interface.

Every venue — Alpaca, Binance, Oanda, IBKR — is reached through this
interface, so the execution engine has no venue-specific code. The interface
is deliberately small: submit, cancel, query positions, query account. Anything
richer would leak venue semantics into the engine.

Adapters must never invent a fill. A method that cannot reach the venue raises
:class:`~tradermaximus.core.errors.ExecutionError`; it does not return a
plausible-looking result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ...core.types import AssetClass, Fill, Order, OrderSide


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A position as the *broker* reports it — the authoritative record."""

    symbol: str
    quantity: float          # signed: negative is short
    average_price: float
    market_value: float = 0.0
    unrealised_pnl: float = 0.0


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class OrderAck:
    """Broker's response to a submission."""

    order_id: str
    accepted: bool
    broker_order_id: str = ""
    fills: tuple[Fill, ...] = ()
    remaining: float = 0.0
    message: str = ""
    latency_ms: float = 0.0

    @property
    def filled_quantity(self) -> float:
        return sum(f.quantity for f in self.fills)

    @property
    def is_complete(self) -> bool:
        return self.accepted and self.remaining <= 1e-12


class BrokerAdapter(ABC):
    """Interface every execution venue implements."""

    name: str = "broker"
    supported_assets: frozenset[AssetClass] = frozenset()
    is_paper: bool = True

    @abstractmethod
    def submit(self, order: Order) -> OrderAck:
        """Submit an order. Raises ExecutionError if the venue is unreachable."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """Cancel a working order. Returns whether it was cancelled."""

    @abstractmethod
    def positions(self) -> Sequence[BrokerPosition]:
        """Positions as the broker sees them, for reconciliation."""

    @abstractmethod
    def account(self) -> BrokerAccount:
        """Account equity, cash and buying power."""

    def supports(self, asset_class: AssetClass) -> bool:
        return asset_class in self.supported_assets

    def healthy(self) -> bool:
        return True

    def open_orders(self) -> Sequence[Order]:
        return ()
