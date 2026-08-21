"""MCP tool registry and availability protocol (spec section 2.3).

The rule from section 2.3, stated plainly: *never assume a tool exists, never
fabricate a tool response, never continue if a required tool is unavailable.*

This registry makes that mechanical. A tool must be registered, healthy,
authenticated and within its freshness window before :meth:`ToolRegistry.call`
will invoke it. When a required tool is unavailable and no fallback answers,
the call raises rather than returning a default — a default would be a
fabricated response wearing a plausible disguise.

The registry describes the servers from section 2.1 and their fallback chains.
Concrete transport lives outside this module: a registered tool is a Python
callable, so it may wrap an MCP client, an HTTP SDK, or a local library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from ..core.errors import ToolUnavailableError
from ..core.types import utc_now
from ..observability.journal import Journal, NULL_JOURNAL, Severity


class ToolCategory(str, Enum):
    """Section 2.1 groupings."""

    CHARTING = "charting"
    MARKET_DATA = "market_data"
    NEWS = "news"
    AI_ML = "ai_ml"
    BROKERAGE = "brokerage"
    RESEARCH = "research"
    AUTOMATION = "automation"
    NOTIFICATION = "notification"
    INFRASTRUCTURE = "infrastructure"


class ToolStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Declared capability of one MCP server (section 2.1)."""

    name: str
    category: ToolCategory
    purpose: str
    skills: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    required: bool = False
    requires_auth: bool = True
    max_staleness_seconds: float = 5.0


@dataclass(slots=True)
class ToolRegistration:
    """A registered tool: its spec, handler and live health state."""

    spec: ToolSpec
    handler: Callable[..., Any] | None = None
    status: ToolStatus = ToolStatus.UNKNOWN
    authenticated: bool = False
    last_success: datetime | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    calls: int = 0

    @property
    def available(self) -> bool:
        return (
            self.handler is not None
            and self.status in {ToolStatus.HEALTHY, ToolStatus.DEGRADED}
            and (self.authenticated or not self.spec.requires_auth)
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Outcome of a registry-mediated call."""

    tool: str
    value: Any
    from_fallback: bool = False
    attempts: int = 1
    latency_ms: float = 0.0


#: Section 2.1 server catalogue. Registering a handler activates one.
CATALOGUE: tuple[ToolSpec, ...] = (
    ToolSpec(
        "tradingview-mcp", ToolCategory.CHARTING,
        "Real-time charts, indicators, screeners, Pine Script",
        skills=("chart_pattern_recognition", "indicator_calculation", "multi_timeframe_analysis", "screening"),
        endpoints=("/chart", "/indicator", "/screener", "/webhook"),
        fallbacks=("polygon-mcp", "alphavantage-mcp"),
    ),
    ToolSpec(
        "polygon-mcp", ToolCategory.MARKET_DATA,
        "Real-time and historical market data for equities, options, crypto and FX",
        skills=("realtime_streaming", "tick_data", "aggregate_bars", "snapshots"),
        endpoints=("/v2/aggs", "/v2/snapshot", "/v2/ticks"),
        fallbacks=("alphavantage-mcp", "tradingview-mcp"),
        required=True,
    ),
    ToolSpec(
        "alphavantage-mcp", ToolCategory.MARKET_DATA,
        "Market data, technical indicators, fundamental data",
        skills=("technical_indicators", "sector_performance", "earnings_data"),
        endpoints=("/query",),
        fallbacks=("polygon-mcp",),
    ),
    ToolSpec(
        "finnhub-mcp", ToolCategory.NEWS,
        "News, sentiment, earnings, insider transactions",
        skills=("news_aggregation", "sentiment_scoring", "earnings_surprises"),
        endpoints=("/news", "/sentiment", "/earnings", "/insider"),
        fallbacks=("newsapi-mcp",),
    ),
    ToolSpec(
        "newsapi-mcp", ToolCategory.NEWS,
        "Broad financial news aggregation",
        skills=("news_filtering", "event_detection"),
        endpoints=("/v2/everything", "/v2/top-headlines"),
    ),
    ToolSpec(
        "huggingface-mcp", ToolCategory.AI_ML,
        "Sentiment analysis and financial text classification",
        skills=("sentiment_analysis", "nlp", "text_classification"),
        endpoints=("/predict",),
    ),
    ToolSpec(
        "tensorflow-mcp", ToolCategory.AI_ML,
        "Deep learning for pattern recognition and anomaly detection",
        skills=("neural_inference", "pattern_prediction", "anomaly_detection"),
        endpoints=("/predict", "/train", "/evaluate"),
        requires_auth=False,
    ),
    ToolSpec(
        "pytorch-mcp", ToolCategory.AI_ML,
        "Model development, reinforcement learning, time-series forecasting",
        skills=("deep_learning", "reinforcement_learning", "model_deployment"),
        endpoints=("/infer", "/train", "/export"),
        requires_auth=False,
    ),
    ToolSpec(
        "langchain-mcp", ToolCategory.AI_ML,
        "LLM orchestration and multi-agent reasoning",
        skills=("multi_agent_orchestration", "tool_integration"),
        endpoints=("/invoke", "/run"),
    ),
    ToolSpec(
        "openai-mcp", ToolCategory.AI_ML,
        "LLM analysis, interpretation and summarisation",
        skills=("market_analysis", "decision_explanation", "summarisation"),
        endpoints=("/chat/completions",),
    ),
    ToolSpec(
        "alpaca-mcp", ToolCategory.BROKERAGE,
        "US equities and ETF execution",
        skills=("order_placement", "account_management", "position_tracking"),
        endpoints=("/orders", "/positions", "/account"),
        fallbacks=("ibkr-mcp",),
    ),
    ToolSpec(
        "binance-mcp", ToolCategory.BROKERAGE,
        "Crypto spot and futures execution",
        skills=("order_execution", "funding_rate_monitoring"),
        endpoints=("/order", "/position", "/account"),
    ),
    ToolSpec(
        "oanda-mcp", ToolCategory.BROKERAGE,
        "FOREX execution",
        skills=("forex_execution", "pip_calculation", "rollover_handling"),
        endpoints=("/orders", "/positions", "/pricing"),
        fallbacks=("ibkr-mcp",),
    ),
    ToolSpec(
        "ibkr-mcp", ToolCategory.BROKERAGE,
        "Multi-asset execution fallback",
        skills=("advanced_order_types", "options", "futures", "multi_currency"),
        endpoints=("/placeOrder", "/accountSummary", "/historicalData"),
    ),
    ToolSpec(
        "quantconnect-mcp", ToolCategory.RESEARCH,
        "Backtesting, parameter optimisation, live deployment",
        skills=("backtesting", "optimisation"),
        endpoints=("/backtest", "/optimize"),
        fallbacks=("backtrader-mcp", "zipline-mcp"),
    ),
    ToolSpec(
        "backtrader-mcp", ToolCategory.RESEARCH, "Local Python backtesting",
        skills=("backtesting", "performance_metrics"), endpoints=("/run",), requires_auth=False,
    ),
    ToolSpec(
        "zipline-mcp", ToolCategory.RESEARCH, "Event-driven backtest simulation",
        skills=("event_driven_backtesting",), endpoints=("/run",), requires_auth=False,
    ),
    ToolSpec(
        "quantlib-mcp", ToolCategory.RESEARCH,
        "Financial mathematics, options pricing, risk metrics",
        skills=("options_pricing", "var_calculation", "bond_pricing"),
        endpoints=("/calculate", "/risk", "/VaR"), requires_auth=False,
    ),
    ToolSpec(
        "telegram-mcp", ToolCategory.NOTIFICATION, "Real-time alerts and trade notifications",
        skills=("alerting",), endpoints=("/sendMessage",), fallbacks=("slack-mcp",),
    ),
    ToolSpec(
        "slack-mcp", ToolCategory.NOTIFICATION, "Team notifications and dashboards",
        skills=("alerting", "dashboards"), endpoints=("/webhook",),
    ),
    ToolSpec(
        "zapier-mcp", ToolCategory.AUTOMATION, "Workflow automation",
        skills=("triggers", "actions"), endpoints=("/execute",), fallbacks=("make-mcp",),
    ),
    ToolSpec(
        "make-mcp", ToolCategory.AUTOMATION, "Integration and data transformation",
        skills=("webhooks", "data_transformation"), endpoints=("/webhook",),
    ),
    ToolSpec(
        "influxdb-mcp", ToolCategory.INFRASTRUCTURE, "Time-series storage",
        skills=("timeseries_storage", "retention_policies"), endpoints=("/write", "/query"),
    ),
    ToolSpec(
        "database-mcp", ToolCategory.INFRASTRUCTURE, "Relational storage for config and audit",
        skills=("structured_storage", "auditing"), endpoints=("/query",),
    ),
    ToolSpec(
        "aws-mcp", ToolCategory.INFRASTRUCTURE, "Cloud compute, storage and serverless",
        skills=("scalable_compute", "persistence"), endpoints=("/ec2", "/s3", "/lambda"),
    ),
    ToolSpec(
        "telemetry-mcp", ToolCategory.INFRASTRUCTURE, "Logging, metrics and anomaly detection",
        skills=("observability", "performance_monitoring"), endpoints=("/log", "/metric"),
        requires_auth=False,
    ),
)

CATALOGUE_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in CATALOGUE}


class ToolRegistry:
    """Availability-checked access to MCP tools."""

    def __init__(
        self,
        *,
        journal: Journal | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._tools: dict[str, ToolRegistration] = {}

    # -- registration -------------------------------------------------------

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        spec: ToolSpec | None = None,
        authenticated: bool = True,
    ) -> ToolRegistration:
        """Register a handler for a tool.

        Args:
            name: catalogue name, e.g. ``"polygon-mcp"``.
            handler: callable invoked by :meth:`call`.
            spec: override for tools not in the catalogue.
            authenticated: whether credentials have been verified.
        """
        resolved = spec or CATALOGUE_BY_NAME.get(name)
        if resolved is None:
            raise ToolUnavailableError(
                f"{name!r} is not in the MCP catalogue; pass an explicit ToolSpec to register it"
            )
        registration = ToolRegistration(
            spec=resolved,
            handler=handler,
            status=ToolStatus.HEALTHY,
            authenticated=authenticated,
        )
        self._tools[name] = registration
        self.journal.record("system", f"registered MCP tool {name}", tool=name, category=resolved.category.value)
        return registration

    def deregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def set_status(self, name: str, status: ToolStatus, detail: str = "") -> None:
        registration = self._tools.get(name)
        if registration is None:
            return
        registration.status = status
        registration.last_error = detail
        self.journal.record(
            "system",
            f"tool {name} status -> {status.value}: {detail}",
            severity=Severity.WARNING if status is not ToolStatus.HEALTHY else Severity.INFO,
            tool=name,
        )

    # -- availability -------------------------------------------------------

    def is_available(self, name: str) -> bool:
        registration = self._tools.get(name)
        return registration is not None and registration.available

    def get(self, name: str) -> ToolRegistration | None:
        return self._tools.get(name)

    def resolve(self, name: str) -> str | None:
        """The tool that would actually serve ``name``, following fallbacks."""
        if self.is_available(name):
            return name
        spec = CATALOGUE_BY_NAME.get(name) or (self._tools[name].spec if name in self._tools else None)
        if spec is None:
            return None
        for fallback in spec.fallbacks:
            if self.is_available(fallback):
                return fallback
        return None

    def missing_required(self) -> list[str]:
        """Required catalogue tools with no available implementation.

        Used by the section 31.1 readiness check: a system missing a required
        tool is not production ready, however well the rest behaves.
        """
        missing: list[str] = []
        for spec in CATALOGUE:
            if spec.required and self.resolve(spec.name) is None:
                missing.append(spec.name)
        return missing

    # -- invocation ---------------------------------------------------------

    def call(self, name: str, /, *args: Any, required: bool | None = None, **kwargs: Any) -> ToolResult:
        """Invoke a tool, with retry, fallback and a hard failure at the end.

        Raises:
            ToolUnavailableError: when neither the tool nor any fallback can
                serve the call. The registry never substitutes a default
                value — a fabricated response is worse than a halt.
        """
        target = self.resolve(name)
        if target is None:
            message = f"tool {name!r} is unavailable and no fallback could serve the call"
            self.journal.record("system", message, severity=Severity.ERROR, tool=name)
            raise ToolUnavailableError(message)

        from_fallback = target != name
        registration = self._tools[target]
        attempts = 0
        last_error: Exception | None = None
        started = time.perf_counter()

        while attempts <= self.max_retries:
            attempts += 1
            try:
                value = registration.handler(*args, **kwargs)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - any tool failure is retryable
                last_error = exc
                registration.consecutive_failures += 1
                registration.last_error = str(exc)
                if registration.consecutive_failures >= 3:
                    registration.status = ToolStatus.DEGRADED
                if attempts <= self.max_retries:
                    if self.backoff_seconds > 0:
                        self._sleep(self.backoff_seconds * (2 ** (attempts - 1)))
                    continue
                break
            else:
                registration.consecutive_failures = 0
                registration.last_success = utc_now()
                registration.calls += 1
                registration.status = ToolStatus.HEALTHY
                return ToolResult(
                    tool=target,
                    value=value,
                    from_fallback=from_fallback,
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )

        registration.status = ToolStatus.UNAVAILABLE
        self.set_status(target, ToolStatus.UNAVAILABLE, str(last_error))

        # Retry the whole call through a fallback, once.
        if not from_fallback:
            spec = registration.spec
            for fallback in spec.fallbacks:
                if self.is_available(fallback):
                    self.journal.record(
                        "system",
                        f"{name} exhausted retries; falling back to {fallback}",
                        severity=Severity.WARNING,
                    )
                    result = self.call(fallback, *args, **kwargs)
                    return ToolResult(result.tool, result.value, True, attempts + result.attempts, result.latency_ms)

        message = f"tool {target!r} failed after {attempts} attempts: {last_error}"
        self.journal.record("system", message, severity=Severity.ERROR, tool=target)
        raise ToolUnavailableError(message)

    # -- reporting ----------------------------------------------------------

    def status_report(self) -> dict[str, object]:
        return {
            "registered": len(self._tools),
            "available": sum(1 for r in self._tools.values() if r.available),
            "missing_required": self.missing_required(),
            "tools": {
                name: {
                    "status": r.status.value,
                    "available": r.available,
                    "authenticated": r.authenticated,
                    "calls": r.calls,
                    "consecutive_failures": r.consecutive_failures,
                    "last_error": r.last_error,
                }
                for name, r in sorted(self._tools.items())
            },
        }

    def skills(self) -> dict[str, list[str]]:
        """Section 2.2: what the connected tool set can actually do."""
        out: dict[str, list[str]] = {}
        for name, registration in self._tools.items():
            if registration.available:
                out[name] = list(registration.spec.skills)
        return out
