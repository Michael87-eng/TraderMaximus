# Specification traceability

Every section of the TraderMaximus master specification, mapped to the code
that implements it and the tests that hold it in place.

`—` means the section is descriptive (identity, philosophy, mandate) and has
no single implementation site; where such a section imposes a *behaviour*, the
enforcing module is named.

| § | Section | Implementation | Tests |
|---|---|---|---|
| — | AI identity, primary objective | `README.md`, `tradermaximus/__init__.py` | — |
| 1 | System architecture | `orchestrator.py` | `test_orchestrator.py` |
| 2.1 | MCP server catalogue | `mcp/registry.py::CATALOGUE` (26 servers) | `TestToolRegistry` |
| 2.2 | Skills package | `mcp/registry.py::ToolRegistry.skills` | `TestToolRegistry` |
| 2.3 | Tool availability protocol | `mcp/registry.py::ToolRegistry.call` | `test_retry_then_success`, `test_fallback_is_used_when_primary_dies` |
| 3.1 | Data validation rules | `data/quality.py::DataQualityEngine` | `TestDataQuality` |
| 3.2 | Quality checks | `data/quality.py::QualityIssue` (17 issue types) | `TestDataQuality` |
| 3.3 | Quality failures → NO TRADE | `risk/engine.py::Rule.DATA_QUALITY` | `test_data_quality_failure_blocks_entry` |
| 3.4 | Provider priority + HALT | `data/providers.py::ProviderChain` | `TestProviderChain` |
| 4.1 | Supported indicators | `indicators/core.py`, `structure.py`, `patterns.py` | `TestKnownValues`, `TestStructure`, `TestPatterns` |
| 4.2 | Calculation rules | `indicators/engine.py::FeatureEngine` | `TestAlignmentAndWarmup`, `TestDeterminism` |
| 5.1 | Regime scoring algorithm | `regime/engine.py::WEIGHTS`, `_score_components` | `test_weights_sum_to_one` |
| 5.2 | Regime classification | `regime/engine.py::Regime`, `_classify` | `TestRegimeEngine` |
| 5.3 | Regime confidence | `regime/engine.py::RegimeState.size_multiplier` | `test_low_confidence_halves_size` |
| 5.4 | Strategy eligibility matrix | `regime/engine.py::is_strategy_type_allowed` | `test_high_volatility_tag_vetoes_breakouts` |
| 6.1 | Strategy formalisation | `strategy/spec.py::StrategySpec` | `TestStrategyLibrary` |
| 6.2.1–10 | Core strategy library | `strategy/library.py` (10 specs) | `test_all_ten_strategies_are_defined` |
| 6.3 | Strategy selection logic | `strategy/engine.py::StrategyEngine.select` | `TestStrategySelection` |
| 7.1 | Signal scoring components | `strategy/scoring.py::COMPONENT_MAX` | `test_components_sum_to_one_hundred` |
| 7.2 | Minimum entry thresholds | `regime/engine.py::MIN_SIGNAL_SCORE` | `test_minimum_score_takes_the_strictest_active_regime` |
| 8.1 | Hard risk limits | `risk/limits.py::HardLimits`, `AssetLimits` | `TestPositionSizing` |
| 8.2 | Risk rejection logic (22 rules) | `risk/engine.py::Rule`, `RiskEngine.authorize` | `TestRiskAuthorization` |
| 9.1 | Position sizing formula | `risk/sizing.py::PositionSizingEngine.size` | `test_risk_budget_drives_the_quantity` |
| 9.2 | Asset-specific calibration | `risk/limits.py::EQUITIES/CRYPTO/FOREX_LIMITS` | `test_unknown_volatility_uses_the_safest_band` |
| **10.1** | **Stop distance (V4 correction)** | `risk/stops.py::StopLossEngine.compute` | **`TestStopDistance` (11 tests)** |
| 10.2 | Stop models | `risk/stops.py::StopModel`, `ATR_MULTIPLIERS` | `test_atr_multiplier_scales_with_volatility` |
| 10.3 | Stop placement rules | `risk/trailing.py::TrailingStopEngine.update` | `test_stop_only_ever_tightens` |
| 11 | Take-profit ladders | `risk/targets.py::LADDERS` | `test_successful_execution_attaches_protection` |
| 12 | Trailing stop engine | `risk/trailing.py::STAGES` | `TestTrailingRatchet` |
| 13.1 | Portfolio metrics | `risk/portfolio.py::PortfolioRiskMetrics` | `TestPortfolioLimits` |
| **13.2** | **Expected Shortfall (V4 correction)** | `risk/portfolio.py::tail_risk` | **`TestExpectedShortfall` (8 tests)** |
| 13.3 | Portfolio risk rules | `risk/portfolio.py::PortfolioRiskEngine.check` | `test_concentrated_book_breaches_multiple_limits` |
| 14.1 | Order lifecycle states | `execution/states.py::OrderState` | `TestStateMachine` |
| 14.2 | Failure states | `execution/states.py::FAILURE_STATES` | `test_failure_states_are_reachable_from_live_states` |
| 14.3 | Recovery logic | `execution/states.py::RECOVERY_ACTIONS` | `test_network_failure_retries_then_gives_up`, `test_venue_rejection_is_not_retried` |
| 14.4 | Order submission safety | `execution/safety.py::run_checklist` | `TestSafetyChecklist` |
| 15.1 | Kill switch triggers | `risk/kill_switch.py::KillTrigger` (14 triggers) | `TestKillSwitch` |
| 15.2 | Kill switch actions | `risk/kill_switch.py::ACTION_SEQUENCE` | `test_multiple_triggers_are_all_recorded` |
| 15.3 | Emergency protection | `execution/engine.py::_emergency_flatten` | `test_failed_protection_flattens_the_position` |
| 16.1–3 | Daily/weekly/monthly breakers | `risk/circuit_breakers.py` | `TestCircuitBreakers` |
| 17.1 | Pre-execution checklist | `execution/safety.py` (17 checks) | `TestSafetyChecklist` |
| 17.2 | Order type rules | `execution/brokers/paper.py::_is_marketable` | `TestPaperBroker` |
| 17.3 | Slippage protection | `risk/engine.py::Rule.SLIPPAGE` | `test_execution_conditions_block_entry` |
| 18.1 | Liquidity requirements | `data/quality.py::MIN_LIQUIDITY` | `test_thin_liquidity_is_detected` |
| 18.2 | Market impact rules | `risk/sizing.py` caps, `brokers/paper.py` impact model | `test_impact_grows_with_participation` |
| 18.3 | Volatility scaling | `risk/sizing.py::volatility_adjustment` | `test_volatility_scaling_only_reduces` |
| 19.1 | Event classification | `news/events.py::EventCategory` | `test_headline_categorisation` |
| 19.2 | Event-driven behaviour | `news/events.py::EventEngine.posture` | `TestEventEngine` |
| 20.1 | Validation phases | `research/validation.py::Stage` | `TestValidation` |
| 20.2 | Acceptance criteria | `research/validation.py::AcceptanceCriteria` | `test_acceptance_gates_reject_a_weak_strategy` |
| 20.3 | Bias controls | `research/backtest.py` (point-in-time, next-bar fill, costs) | `test_backtest_runs_and_respects_point_in_time` |
| 20.4 | Walk-forward validation | `research/validation.py::walk_forward` | `test_walk_forward_windows` |
| 21.1 | Drift metrics | `research/drift.py::DriftThresholds` | `TestDrift` |
| 21.2 | Drift response | `research/drift.py::RESPONSES` | `test_severe_decay_disables_the_strategy` |
| 22.1 | Real-time monitoring | `observability/metrics.py::SystemHealth` | — |
| 22.2 | Audit logging | `observability/journal.py::Journal` | `test_journal_receives_critical_alerts` |
| 23.1 | Deployment pipeline | `research/validation.py::STAGE_ORDER` | `test_deployment_gate_advances_one_stage_at_a_time` |
| 23.2 | Gate requirements | `research/validation.py::StrategyValidator` | `test_failing_report_blocks_promotion` |
| 24 | AI self-critique | `strategy/spec.py::Evaluation` (named condition results) | `test_evaluation_names_the_failing_conditions` |
| 25.1 | Capital allocation | `risk/limits.py::max_exposure_total`, `cash_reserve` | — |
| 25.2–3 | Scaling and withdrawal | `config/system.yaml::cash_reserve` | — |
| 26.1 | Pre-market workflow | `orchestrator.py::pre_market` | `TestWorkflow` |
| 26.2 | Intraday workflow | `orchestrator.py::cycle` | `test_full_session_runs_and_reconciles` |
| 26.3 | Post-market workflow | `orchestrator.py::post_market` | `test_full_session_runs_and_reconciles` |
| 27 | Weekly review | `observability/journal.py::rejection_summary`, `metrics.py` | `test_rejections_are_aggregated_in_the_journal` |
| 28 | Monthly strategy review | `observability/metrics.py::compute_metrics`, `research/drift.py` | `TestMetrics`, `TestDrift` |
| 29.1 | Configuration precedence | `core/config.py::Layer`, `ConfigGovernor` | `TestConfigGovernance` |
| 29.2 | Configuration categories | `config/system.yaml` | `test_lower_layer_cannot_loosen` |
| 30 | Final mandate | `risk/engine.py` (sole authorisation gate) | `test_kill_switch_takes_precedence_over_every_other_reason` |
| 31.1 | Readiness checklist | `orchestrator.py::readiness_report` | `TestReadiness` |
| 31.2 | Continuous validation | `research/drift.py::StrategyMonitor` | `test_monitor_disables_and_reinstates_at_half_size` |
| 32 | System philosophy | — (see `README.md`) | — |
| 33 | Deployment instruction | Paper mode default; no `live` CLI subcommand | `test_no_live_subcommand_exists` |

## Coverage summary

- **26** MCP servers catalogued with fallback chains
- **10** strategies, each a machine-testable spec
- **13** market regimes with a concurrent-tag eligibility matrix
- **22** risk rejection rules (27 ordered checks on the approval path)
- **14** kill-switch triggers, **17** pre-execution safety checks
- **179** tests, all passing

## Sections carrying deliberate interpretation

§5.1 (trend slope, correlation direction), §5.2 (regime as a set), §7.1
(non-equity rescaling), §13.1 (concentration denominator, diversification
ratio), §20.1 (bootstrap vs shuffle), §31.1 (MCP applicability). Each is
argued in [DECISIONS.md](DECISIONS.md).
