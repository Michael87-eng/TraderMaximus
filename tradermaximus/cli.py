"""Command-line interface for TraderMaximus.

Subcommands:

``readiness``  section 31.1 production-readiness checklist
``paper``      run a paper-trading session over the configured universe
``backtest``   run a backtest and print the section 20.2 validation gates
``limits``     print the resolved risk configuration and its provenance
``tools``      show MCP tool availability and the resulting skill set

Every command defaults to paper mode. There is no ``live`` subcommand: going
live is a deliberate configuration act, not a flag someone can reach for at
2am.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .core.config import SystemConfig
from .core.types import AssetClass, Instrument, Timeframe
from .data.providers import DEFAULT_INSTRUMENTS, ProviderChain, SyntheticProvider
from .execution.brokers.paper import MarketState, PaperBroker, PaperFillModel
from .mcp.registry import CATALOGUE, ToolRegistry
from .observability.journal import Journal
from .orchestrator import TraderMaximus
from .portfolio.book import PositionBook
from .regime.engine import MarketContext
from .research.backtest import BacktestConfig, Backtester
from .research.validation import StrategyValidator, monte_carlo
from .risk.limits import HardLimits

DEFAULT_UNIVERSE = ("AAPL", "MSFT", "NVDA", "XLE")


def _build_universe(symbols: Sequence[str]) -> list[Instrument]:
    out: list[Instrument] = []
    for symbol in symbols:
        instrument = DEFAULT_INSTRUMENTS.get(symbol)
        if instrument is None:
            instrument = Instrument(symbol, AssetClass.EQUITIES, "UNKNOWN", "UNKNOWN", 0.01, 1.0, 1.0)
        out.append(instrument)
    return out


def _build_system(args: argparse.Namespace, journal: Journal) -> TraderMaximus:
    config = SystemConfig.load(args.config) if args.config else SystemConfig.defaults()
    instruments = _build_universe(args.symbols)
    benchmark = Instrument("SPY", AssetClass.EQUITIES, "NYSE", "Index", 0.01, 1.0, 1.0)

    provider = SyntheticProvider(seed=args.seed, annual_volatility=0.22)
    chain = ProviderChain([provider], journal=journal)

    book = PositionBook(args.equity, cash_reserve=float(config.get("cash_reserve", 0.30)))
    broker = PaperBroker(
        starting_cash=args.equity,
        model=PaperFillModel(partial_fill_probability=0.0),
        seed=args.seed,
    )
    for instrument in instruments:
        series = chain.fetch(instrument, Timeframe.D1, 400).series
        broker.set_market(
            instrument.symbol,
            MarketState(price=series.last.close, bar_volume=series.average_volume(50), spread_bps=5.0),
        )

    return TraderMaximus(
        config=config,
        providers=chain,
        broker=broker,
        book=book,
        journal=journal,
        instruments=instruments,
        benchmark=benchmark,
        paper_mode=True,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_readiness(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    system = _build_system(args, journal)
    report = system.readiness_report()
    print(json.dumps(report, indent=2))
    if not report["ready"]:
        print("\nNOT READY. Resolve the failing checks before trading.", file=sys.stderr)
        return 1
    print("\nReady for paper trading.")
    return 0


def cmd_limits(args: argparse.Namespace) -> int:
    config = SystemConfig.load(args.config) if args.config else SystemConfig.defaults()
    limits = HardLimits.from_config(config)
    print("Resolved hard risk limits (spec section 8.1)\n")
    for field_name in (
        "max_risk_per_trade", "max_total_portfolio_risk", "max_portfolio_exposure",
        "max_daily_loss", "max_weekly_loss", "max_monthly_drawdown",
        "max_portfolio_drawdown", "max_var_95", "max_expected_shortfall_95",
        "max_positions_total", "max_sector_exposure", "max_correlated_exposure",
        "max_leverage", "max_daily_trades", "max_spread_bps", "max_slippage_bps",
    ):
        value = getattr(limits, field_name)
        provenance = config.governor.provenance(field_name)
        source = f"  [{provenance.layer.name} / {provenance.source}]" if provenance else ""
        print(f"  {field_name:32} {value!s:>10}{source}")

    violations = config.governor.violations
    if violations:
        print("\nRejected configuration overrides (section 29.1 precedence):")
        for violation in violations:
            print(f"  - {violation}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    journal = Journal()
    registry = ToolRegistry(journal=journal)
    print(f"MCP catalogue: {len(CATALOGUE)} servers (spec section 2.1)\n")
    by_category: dict[str, list[str]] = {}
    for spec in CATALOGUE:
        label = f"{spec.name}{' [REQUIRED]' if spec.required else ''}"
        if spec.fallbacks:
            label += f"  -> fallback: {', '.join(spec.fallbacks)}"
        by_category.setdefault(spec.category.value, []).append(label)
    for category, names in sorted(by_category.items()):
        print(f"{category}:")
        for name in names:
            print(f"  {name}")
    print(f"\nRegistered in this process: {registry.status_report()['registered']}")
    print("Register handlers via ToolRegistry.register(name, handler) before trading.")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    journal = Journal(args.journal, echo=True)
    system = _build_system(args, journal)

    context = MarketContext(
        breadth_index=args.breadth,
        new_highs_ratio=args.new_highs,
        correlation_change=0.10,
    )
    now = datetime.now(timezone.utc)

    print("=== PRE-MARKET (section 26.1) ===")
    pre = system.pre_market(now=now, market_context=context)
    print(pre.summary())
    if pre.regime:
        print(f"  regime: {json.dumps(pre.regime.as_dict())}")
    for symbol, report in sorted(pre.data_reports.items()):
        status = "OK" if report.passed else f"FAIL ({report.reason()})"
        print(f"  data {symbol}: score {report.score:.2f} {status}")
    if not pre.ready:
        print("\nPre-market checks failed; no trading this session.", file=sys.stderr)
        for blocker in pre.blockers:
            print(f"  - {blocker}", file=sys.stderr)
        return 1

    print(f"\n=== INTRADAY ({args.cycles} cycles, section 26.2) ===")
    for index in range(args.cycles):
        cycle = system.cycle(now=now + timedelta(minutes=15 * index), market_context=context)
        print(f"  cycle {index + 1}: {cycle.summary()}")
        if cycle.rejections:
            for rule, count in sorted(cycle.rejections.items(), key=lambda kv: -kv[1]):
                print(f"      rejected {count}x  {rule}")

    print("\n=== POST-MARKET (section 26.3) ===")
    post = system.post_market(now=now + timedelta(hours=7))
    print(post.summary())
    print(f"  execution quality: {json.dumps(dict(post.execution_quality))}")
    if post.breaches:
        print("  portfolio breaches:")
        for breach in post.breaches:
            print(f"    - {breach}")
    if post.rejections:
        print("  rejection summary:")
        for rule, count in sorted(post.rejections.items(), key=lambda kv: -kv[1]):
            print(f"    {count:4d}  {rule}")
    print(f"\n  journal events: {json.dumps(journal.counts())}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    instruments = _build_universe(args.symbols)
    provider = SyntheticProvider(seed=args.seed, annual_volatility=0.22)

    universe = {
        instrument: provider.generate(instrument, Timeframe.D1, args.bars)
        for instrument in instruments
    }
    benchmark = provider.generate(
        Instrument("SPY", AssetClass.EQUITIES), Timeframe.D1, args.bars
    )

    print(f"Backtesting {len(universe)} symbols over {args.bars} bars...")
    backtester = Backtester(
        BacktestConfig(starting_equity=args.equity, warmup_bars=args.warmup, seed=args.seed),
        journal=journal,
    )
    result = backtester.run(
        universe,
        benchmark=benchmark,
        market_context=MarketContext(
            breadth_index=args.breadth, new_highs_ratio=args.new_highs, correlation_change=0.10
        ),
    )

    print("\n=== RESULT ===")
    print(json.dumps(result.summary(), indent=2))

    if result.rejection_reasons:
        print("\n=== RISK REJECTIONS ===")
        for rule, count in sorted(result.rejection_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4d}  {rule}")

    print("\n=== VALIDATION GATES (section 20.2) ===")
    validator = StrategyValidator()
    report = validator.validate_backtest(result.metrics)
    print(report.summary())
    for check in report.checks:
        print(f"  {check}")

    simulation = monte_carlo(result.trades, starting_equity=args.equity)
    if simulation is not None:
        print("\n=== MONTE CARLO (bootstrap) ===")
        print(json.dumps(simulation.as_dict(), indent=2))
        mc_report = validator.validate_monte_carlo(simulation, result.metrics.max_drawdown)
        print(mc_report.summary())
        for check in mc_report.checks:
            print(f"  {check}")
    else:
        print("\nToo few trades for Monte Carlo analysis (need at least 10).")

    print(
        "\nNOTE: this run uses synthetic price data. It exercises the pipeline; "
        "it is NOT evidence of an edge. Validate on real history before paper trading."
    )
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradermaximus",
        description="TraderMaximus - risk-first algorithmic trading system (paper mode)",
    )
    parser.add_argument("--config", help="path to a YAML or JSON configuration file")
    parser.add_argument("--journal", help="path to a JSONL audit journal")
    parser.add_argument("--equity", type=float, default=100_000.0, help="starting equity")
    parser.add_argument("--seed", type=int, default=42, help="seed for reproducible synthetic data")
    parser.add_argument(
        "--symbols", nargs="+", default=list(DEFAULT_UNIVERSE), help="universe to trade"
    )
    parser.add_argument("--breadth", type=float, default=0.55, help="market breadth index, -1 to 1")
    parser.add_argument("--new-highs", type=float, default=0.60, dest="new_highs",
                        help="new highs ratio, 0 to 1")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("readiness", help="run the section 31.1 readiness checklist").set_defaults(
        func=cmd_readiness
    )
    sub.add_parser("limits", help="print resolved risk limits and their provenance").set_defaults(
        func=cmd_limits
    )
    sub.add_parser("tools", help="show the MCP catalogue and availability").set_defaults(func=cmd_tools)

    paper = sub.add_parser("paper", help="run a paper-trading session")
    paper.add_argument("--cycles", type=int, default=3, help="intraday decision cycles to run")
    paper.set_defaults(func=cmd_paper)

    backtest = sub.add_parser("backtest", help="run a backtest with validation gates")
    backtest.add_argument("--bars", type=int, default=700, help="bars of history to simulate")
    backtest.add_argument("--warmup", type=int, default=260, help="warm-up bars before trading")
    backtest.set_defaults(func=cmd_backtest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
