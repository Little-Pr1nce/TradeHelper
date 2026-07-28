"""Production historical replay for stock-bound strategy evidence.

The replay deliberately reuses the live ForecastResult -> TradingScenario ->
TradePlan -> ExecutionDecision -> OrderIntent path.  A standardized account is
used only inside reconstructed OOF research and can never size a live order.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from statistics import median

from contracts import (
    AccountSnapshot, AvailabilitySource, DecisionMode, EvidenceOrigin,
    EventGranularity, ExecutionEvent, ExecutionEvidenceGrade, ExecutionPolicy,
    ExecutionState, ForecastRequest, ForecastScope,
    FreshnessStatus, InstrumentId, LiquidityEvidence, Market, OutcomeStatus,
    OrderSide, PortfolioCandidate, PortfolioInputBatch, PortfolioPolicy,
    PortfolioRole, PositionAvailability, PositionSnapshot, RiskPolicy,
    RiskProfile, RiskRequest, StrategyInput, TradingStatus, ValuationPrice,
    ValuationPriceKind, stable_hash,
)
from contracts.learning import (
    JointOutcomeKind, LearningMetricSnapshot, LedgerKind,
)
from contracts.scenario import ScenarioRequest
from data.quality import evaluate_data_quality
from execution import HistoricalFillSimulator, OrderIntentFactory
from execution.costs import CostModel
from execution.simulator import HistoricalSimulationRequest
from forecast.engine import ForecastEngine
from forecast.registry import ForecastRegistry
from learning.joint import EquityPoint, replay_joint
from learning.strategy import strategy_outcome
from portfolio import (
    PortfolioDecisionEngine, PortfolioOrderAssembler,
    build_correlation_snapshot, build_holding_risks,
)
from risk import freeze_account_valuation
from risk.market_rules import default_market_rules
from strategies.registry import default_specs


_HORIZONS = (1, 3, 5, 10)
_TEST_ORIGINS_PER_FOLD = 40
_FOLD_COUNT = 3
_EMBARGO_SESSIONS = 10
_RESEARCH_EQUITY = Decimal("100000")


@dataclass(frozen=True, slots=True)
class HistoricalStrategyReplayResult:
    instrument: InstrumentId
    fold_count: int
    tested_origins: int
    outcomes: tuple
    joint_outcomes: tuple
    metric_snapshots: tuple[LearningMetricSnapshot, ...]
    validation_statuses: tuple[tuple[int, int, str], ...]

    @property
    def filled_count(self) -> int:
        return sum(
            item.status is OutcomeStatus.MATURED
            and item.fill_outcome in {"filled", "partial"}
            and item.net_return is not None
            for item in self.outcomes
        )


class HistoricalStrategyReplayer:
    """Generate reconstructed stock strategy evidence without future leakage."""

    def __init__(
        self, *, calendar, forecast_trainer, scenario_planner, strategy_engine,
        risk_officer,
    ) -> None:
        self.calendar = calendar
        self.forecast_trainer = forecast_trainer
        self.scenario_planner = scenario_planner
        self.strategy_engine = strategy_engine
        self.risk_officer = risk_officer
        self.intent_factory = OrderIntentFactory(calendar)
        self.fill_simulator = HistoricalFillSimulator()

    @staticmethod
    def _complete_origins(samples) -> tuple[date, ...]:
        horizons: dict[date, set[int]] = {}
        for sample in samples:
            horizons.setdefault(sample.origin_session_date, set()).add(sample.horizon)
        return tuple(sorted(day for day, values in horizons.items() if values == set(_HORIZONS)))

    @classmethod
    def _fold_origins(cls, samples) -> tuple[tuple[date, tuple[date, ...]], ...]:
        origins = cls._complete_origins(samples)
        required_test = _FOLD_COUNT * _TEST_ORIGINS_PER_FOLD
        # The trainer itself needs an 80-sample prefix plus at least 60 OOF
        # points. Keep additional room for the ten-session label embargo.
        minimum_prefix = 160
        if len(origins) < minimum_prefix + _EMBARGO_SESSIONS + required_test:
            return ()
        selected = origins[-required_test:]
        index = {day: position for position, day in enumerate(origins)}
        folds = []
        for offset in range(0, required_test, _TEST_ORIGINS_PER_FOLD):
            testing = selected[offset: offset + _TEST_ORIGINS_PER_FOLD]
            start_index = index[testing[0]]
            train_end = origins[start_index - _EMBARGO_SESSIONS - 1]
            folds.append((train_end, testing))
        return tuple(folds)

    def _fold_registry(self, instrument, training_samples):
        registry = ForecastRegistry()
        statuses = []
        for horizon in _HORIZONS:
            outcome = self.forecast_trainer.evaluate(
                training_samples,
                scope=ForecastScope.STOCK,
                scope_key=instrument.stable_key,
                horizon=horizon,
            )
            statuses.append((horizon, outcome.status.value))
            registry.record_validation(
                market=instrument.market, scope_key=instrument.stable_key,
                horizon=horizon, status=outcome.status, reason=outcome.reason,
            )
            if outcome.champion is not None and outcome.champion_model is not None:
                registry.promote(outcome.champion, outcome.champion_model)
        return registry, tuple(statuses)

    @staticmethod
    def _research_account(instrument, reference_price, as_of, *, held):
        currency = "CNY" if instrument.market is Market.A else "USD"
        if not held:
            return AccountSnapshot(instrument.market, currency, _RESEARCH_EQUITY, (), as_of)
        lot = Decimal("100") if instrument.market is Market.A else Decimal("1")
        target_value = _RESEARCH_EQUITY * Decimal("0.25")
        shares = (target_value / reference_price / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        shares = max(lot, shares)
        position = PositionSnapshot(instrument, shares, reference_price, as_of)
        cash = _RESEARCH_EQUITY - shares * reference_price
        return AccountSnapshot(instrument.market, currency, cash, (position,), as_of)

    @staticmethod
    def _liquidity(prefix, snapshot, cutoff_at):
        volumes = tuple(Decimal(str(item.volume)) for item in prefix[-20:] if item.volume is not None)
        daily_volume = None if not volumes else Decimal(str(median(volumes)))
        volatility = next(
            (item.value for item in snapshot.values if item.name == "closed.realized_vol_20" and item.value is not None),
            None,
        )
        payload = {
            "median_daily_volume_20": daily_volume,
            "annualized_volatility_20": None if volatility is None else Decimal(str(volatility)),
            "cutoff_at": cutoff_at,
            "source": "reconstructed_completed_daily_bars",
        }
        return LiquidityEvidence(
            payload["median_daily_volume_20"], payload["annualized_volatility_20"],
            cutoff_at, payload["source"], stable_hash(payload),
        )

    @staticmethod
    def _execution_event(instrument, bar, session, previous_close, generated_at):
        return ExecutionEvent(
            stable_hash((instrument.stable_key, bar.trading_date, "daily-replay")),
            instrument, bar.trading_date, session.regular_open, session.regular_close,
            EventGranularity.DAILY_BAR, Decimal(str(bar.open)), Decimal(str(bar.high)),
            Decimal(str(bar.low)), Decimal(str(bar.close)), Decimal(str(bar.volume)),
            Decimal(str(previous_close)), None, None, TradingStatus.OPEN,
            bar.source, "completed_daily_bar", session.regular_close, generated_at,
        )

    @staticmethod
    def _advance_state(state, delta, captured_at):
        sellable = state.sellable_shares
        if sellable is not None and delta.sellable_delta is not None:
            sellable += delta.sellable_delta
        return ExecutionState(
            state.market, state.currency, state.cash + delta.cash_delta,
            state.position_shares + delta.position_delta, sellable,
            delta.average_cost, delta.acquired_session_date,
            delta.active_stop, delta.active_take_profit, captured_at,
            state.source, account_hash=state.account_hash,
        )

    @staticmethod
    def _allocation_order(profile_decision):
        by_id = {item.allocation_id: item for item in profile_decision.allocations}
        preferred = (
            *profile_decision.holding_priority_allocation_ids,
            *profile_decision.entry_priority_allocation_ids,
        )
        ordered = [by_id[item] for item in preferred]
        used = set(preferred)
        ordered.extend(item for item in profile_decision.allocations if item.allocation_id not in used)
        return tuple(ordered)

    def _joint_outcomes(
        self, *, instrument, account, valuation, scenario, risk, plans, rules,
        prefix, event, liquidity, state, target_bar, path_bars, generated_at,
        held,
    ):
        role = PortfolioRole.HOLDING if held else PortfolioRole.WATCHLIST
        candidates = []
        for decision in risk.decisions:
            plan = plans[decision.plan_id]
            identity = {
                "role": role, "scenario_id": scenario.scenario_id,
                "plan_id": plan.plan_id, "decision_id": decision.decision_id,
                "evidence_id": None, "rule_version": rules.rule_version,
            }
            candidates.append(PortfolioCandidate(
                stable_hash(identity), role, scenario, plan, decision, None,
                rules, scenario.as_of,
            ))
        candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        portfolio_policy = PortfolioPolicy()
        risk_policy = RiskPolicy()
        visible_prefix = tuple(
            replace(item, fetched_at=min(item.fetched_at, scenario.as_of))
            for item in prefix
        )
        correlation = build_correlation_snapshot(
            market=instrument.market, universe=(instrument,),
            bars_by_instrument={instrument: visible_prefix}, policy=portfolio_policy,
            cutoff_at=scenario.as_of,
            source_batch_hash=stable_hash(tuple(item.stable_key for item in visible_prefix)),
            generated_at=scenario.as_of,
        )
        holding_risks = build_holding_risks(
            valuation=valuation, account=account, candidates=candidates,
            risk_bundles=(risk,), captured_at=scenario.as_of,
            generated_at=scenario.as_of,
        )
        watchlist = () if held else (instrument,)
        batch_identity = {
            "market": instrument.market, "currency": account.currency,
            "mode": scenario.mode, "account_hash": stable_hash(account),
            "valuation_id": valuation.valuation_id,
            "risk_policy": risk_policy.parameter_hash,
            "portfolio_policy": portfolio_policy.parameter_hash,
            "bundle_ids": (risk.risk_bundle_id,),
            "candidate_ids": tuple(item.candidate_id for item in candidates),
            "watchlist": watchlist,
            "holding_risk_ids": tuple(item.holding_risk_id for item in holding_risks),
            "correlation_snapshot_id": correlation.correlation_snapshot_id,
            "as_of": scenario.as_of,
        }
        batch = PortfolioInputBatch(
            stable_hash(batch_identity), instrument.market, account.currency,
            scenario.mode, account, valuation, risk_policy, portfolio_policy,
            (risk,), candidates, watchlist, holding_risks, correlation,
            scenario.as_of, scenario.as_of,
        )
        portfolio = PortfolioDecisionEngine().decide(batch, scenario.as_of)
        policy = ExecutionPolicy()
        benchmark = Decimal(str(target_bar.close)) / Decimal(str(prefix[-1].close)) - Decimal("1")
        output = []
        for profile in (RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE):
            profile_decision = (
                portfolio.conservative if profile is RiskProfile.CONSERVATIVE
                else portfolio.aggressive
            )
            allocations = self._allocation_order(profile_decision)
            order_bundles = PortfolioOrderAssembler.build(
                portfolio, profile, plans, (risk,), self.calendar, policy,
                scenario.as_of, decision_mode=DecisionMode.EOD,
            )
            intent_by_decision = {
                item.decision_id: item
                for bundle in order_bundles for item in bundle.intents
            }
            replay_state = state
            fills = []
            for allocation in allocations:
                intent = intent_by_decision.get(allocation.decision_id)
                if intent is None:
                    continue
                simulation = self.fill_simulator.simulate(HistoricalSimulationRequest(
                    intent, replay_state, (event,), rules, policy, liquidity,
                    event.interval_end,
                ))
                fills.extend(simulation.fills)
                replay_state = self._advance_state(
                    replay_state, simulation.run.final_state_delta, event.interval_end,
                )
            starting_equity = valuation.equity
            if starting_equity is None:
                continue
            points = [EquityPoint(scenario.as_of, starting_equity)]
            for bar in path_bars:
                session = self.calendar.session_window(
                    instrument.market, instrument.exchange, bar.trading_date,
                )
                equity = replay_state.cash + replay_state.position_shares * Decimal(str(bar.close))
                points.append(EquityPoint(session.regular_close, equity))
            current_loss = profile_decision.reservation_snapshot.current_planned_loss
            incremental = profile_decision.reservation_snapshot.reserved_incremental_loss
            planned_loss = incremental if current_loss is None and incremental else (
                None if current_loss is None else current_loss + incremental
            )
            starting_positions = (
                {instrument: account.positions[0].shares} if account.positions else {}
            )
            starting_prices = (
                {instrument: Decimal(str(prefix[-1].close))} if account.positions else {}
            )
            output.append(replay_joint(
                outcome_kind=JointOutcomeKind.POLICY_OOF,
                portfolio_bundle_id=portfolio.portfolio_bundle_id,
                profile=profile, batch_id=batch.batch_id,
                account_hash=stable_hash(account), valuation_id=valuation.valuation_id,
                market=instrument.market, currency=account.currency,
                starting_cash=account.cash, starting_positions=starting_positions,
                starting_prices=starting_prices, fills=tuple(fills),
                ending_prices={instrument: Decimal(str(target_bar.close))},
                evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
                benchmark_return=benchmark, planned_loss=planned_loss,
                generated_at=generated_at, ordered_allocations=allocations,
                equity_points=tuple(points),
            ))
        return tuple(output)

    @staticmethod
    def _joint_metric_snapshots(instrument, outcomes):
        snapshots = []
        for profile in (RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE):
            values = tuple(item for item in outcomes if item.profile == profile.value)
            if not values:
                continue
            count = len(values)
            mean_return = sum(
                (item.time_weighted_return for item in values), Decimal("0")
            ) / count
            benchmarks = tuple(
                item.benchmark_return for item in values
                if item.benchmark_return is not None
            )
            mean_benchmark = (
                None if not benchmarks
                else sum(benchmarks, Decimal("0")) / len(benchmarks)
            )
            sharpes = tuple(item.sharpe for item in values if item.sharpe is not None)
            metrics = tuple(sorted((
                ("alpha", None if mean_benchmark is None else float(mean_return - mean_benchmark)),
                ("mean_benchmark_return", None if mean_benchmark is None else float(mean_benchmark)),
                ("mean_net_return", float(mean_return)),
                ("max_drawdown", float(min(item.max_drawdown for item in values))),
                ("sharpe", None if not sharpes else float(sum(sharpes, Decimal("0")) / len(sharpes))),
                ("win_rate", sum(item.time_weighted_return > 0 for item in values) / count),
            )))
            cutoff = max(item.generated_at for item in values)
            scope = f"{instrument.stable_key}:joint:{profile.value}"
            identity = {
                "ledger": LedgerKind.JOINT, "scope": scope, "cutoff": cutoff,
                "sample_count": count, "metrics": metrics,
            }
            snapshots.append(LearningMetricSnapshot(
                stable_hash(identity), LedgerKind.JOINT, scope, cutoff, count,
                metrics, cutoff,
            ))
        return tuple(snapshots)

    def _account_outcomes(
        self, *, instrument, snapshot, scenario, quality, reference_bar,
        decision_bar, target_bar, prefix, path_bars, held, generated_at,
    ):
        as_of = snapshot.cutoff_at
        reference_price = Decimal(str(reference_bar.close))
        account = self._research_account(instrument, reference_price, as_of, held=held)
        price = ValuationPrice(
            instrument, reference_price, as_of, reference_bar.source,
            ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED,
        )
        valuation = freeze_account_valuation(account, {instrument: price}, as_of, generated_at=as_of)
        position = account.positions[0] if account.positions else None
        strategy = self.strategy_engine.build(
            StrategyInput(instrument, snapshot, scenario, position, default_specs(), "strategy_policy_v1", as_of),
            generated_at=as_of,
        )
        plans = {
            plan.plan_id: plan
            for branch in (strategy.entry_or_add, strategy.reduce_or_exit, strategy.hold)
            for plan in branch.plans
        }
        availability = None
        if position is not None:
            availability = PositionAvailability(
                instrument, position.shares, position.shares, as_of,
                AvailabilitySource.USER, (),
            )
        rules = default_market_rules(instrument.market, instrument.exchange, as_of)
        risk_policy = RiskPolicy()
        risk = self.risk_officer.assess(
            RiskRequest(
                instrument, strategy, scenario, quality, account, valuation,
                availability, (), rules, None, risk_policy, as_of,
            ),
            generated_at=as_of,
        )
        bundle = self.intent_factory.build_bundle(
            risk, plans, {}, as_of, ExecutionPolicy(), decision_mode=DecisionMode.EOD,
        )
        intent_by_decision = {item.decision_id: item for item in bundle.intents}
        decision_session = scenario.decision_session
        if decision_session is None or decision_bar is None:
            return ()
        event = self._execution_event(
            instrument, decision_bar, decision_session, reference_bar.close, generated_at,
        )
        liquidity = self._liquidity(prefix, snapshot, as_of)
        state = ExecutionState(
            instrument.market, account.currency, account.cash,
            position.shares if position else Decimal("0"),
            position.shares if position else Decimal("0"),
            position.cost_price if position else None,
            reference_bar.trading_date if position and instrument.market is Market.A else None,
            None, None, as_of, "standardized_reconstructed_oof",
            account_hash=stable_hash(account),
        )
        outputs = []
        policy = ExecutionPolicy()
        for decision in risk.decisions:
            plan = plans[decision.plan_id]
            intent = intent_by_decision.get(decision.decision_id)
            if intent is None:
                continue
            simulation = self.fill_simulator.simulate(
                HistoricalSimulationRequest(
                    intent, state, (event,), rules, policy, liquidity,
                    decision_session.regular_close,
                )
            )
            fill = simulation.fills[0]
            trigger_state = "triggered" if fill.outcome.value in {"filled", "partial"} else "not_triggered"
            benchmark = Decimal(str(target_bar.close)) / reference_price - Decimal("1")
            close_path = tuple(Decimal(str(item.close)) for item in path_bars)
            estimated_exit_cost = None
            if fill.fill_price is not None and fill.filled_shares > 0:
                exit_estimate = CostModel.estimate(
                    side=OrderSide.SELL,
                    raw_price=Decimal(str(target_bar.close)),
                    requested_shares=fill.filled_shares,
                    market_rules=rules,
                    policy=policy,
                    liquidity=liquidity,
                    event_at=generated_at,
                    evidence_grade=ExecutionEvidenceGrade.MEDIUM,
                )
                estimated_exit_cost = exit_estimate.total_fee
            outputs.append(strategy_outcome(
                plan=plan, decision=decision, horizon=5,
                target_session_date=target_bar.trading_date,
                evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
                trigger_state=trigger_state,
                fill=fill if trigger_state == "triggered" else None,
                target_close=Decimal(str(target_bar.close)),
                estimated_exit_cost=estimated_exit_cost,
                benchmark_return=benchmark,
                generated_at=generated_at,
                price_path=close_path,
                market_regime_key=scenario.state.value,
            ))
        joint = self._joint_outcomes(
            instrument=instrument, account=account, valuation=valuation,
            scenario=scenario, risk=risk, plans=plans, rules=rules, prefix=prefix,
            event=event, liquidity=liquidity, state=state, target_bar=target_bar,
            path_bars=path_bars, generated_at=generated_at, held=held,
        )
        return tuple(outputs), joint

    def run(self, instrument, bars, samples, *, listing_date=None, cancelled=lambda: False):
        ordered_bars = tuple(sorted(bars, key=lambda item: item.trading_date))
        bars_by_date = {item.trading_date: item for item in ordered_bars}
        samples = tuple(samples)
        folds = self._fold_origins(samples)
        if not folds:
            return HistoricalStrategyReplayResult(instrument, 0, 0, (), (), (), ())
        sample_by_origin = {}
        for sample in samples:
            sample_by_origin.setdefault(sample.origin_session_date, {})[sample.horizon] = sample
        outcomes = []
        joint_outcomes = []
        statuses = []
        tested = 0
        for fold_index, (train_end, test_origins) in enumerate(folds, start=1):
            if cancelled():
                break
            training = tuple(item for item in samples if item.target_session_date <= train_end)
            registry, fold_statuses = self._fold_registry(instrument, training)
            statuses.extend((fold_index, horizon, status) for horizon, status in fold_statuses)
            forecast_engine = ForecastEngine(self.calendar, registry)
            for origin in test_origins:
                if cancelled():
                    break
                grouped = sample_by_origin[origin]
                snapshot = grouped[1].feature_snapshot
                reference_bar = bars_by_date[origin]
                prefix = tuple(item for item in ordered_bars if item.trading_date <= origin)
                quality = evaluate_data_quality(
                    prefix, market=instrument.market, mode=DecisionMode.EOD,
                    as_of=snapshot.cutoff_at, news_available=False,
                    fundamentals_available=False, listing_date=listing_date,
                    calendar=self.calendar,
                )
                forecasts = forecast_engine.forecast(
                    ForecastRequest(snapshot, reference_bar, snapshot.cutoff_at, data_quality=quality),
                    samples=training,
                )
                decision_date = grouped[1].target_session_date
                decision_bar = bars_by_date.get(decision_date)
                target_bar = bars_by_date.get(grouped[5].target_session_date)
                if decision_bar is None or target_bar is None:
                    continue
                decision_session = self.calendar.session_window(
                    instrument.market, instrument.exchange, decision_date,
                )
                scenario = self.scenario_planner.build(
                    ScenarioRequest(
                        instrument, DecisionMode.EOD, snapshot.cutoff_at, snapshot,
                        snapshot, None, (), forecasts, quality, decision_session,
                    ),
                    generated_at=snapshot.cutoff_at,
                )
                path = tuple(
                    item for item in ordered_bars
                    if decision_date <= item.trading_date <= target_bar.trading_date
                )
                generated_at = datetime.combine(
                    target_bar.trading_date, datetime.max.time(), tzinfo=timezone.utc,
                )
                for held in (False, True):
                    strategy_values, joint_values = self._account_outcomes(
                        instrument=instrument, snapshot=snapshot, scenario=scenario,
                        quality=quality, reference_bar=reference_bar,
                        decision_bar=decision_bar, target_bar=target_bar, prefix=prefix,
                        path_bars=path, held=held, generated_at=generated_at,
                    )
                    outcomes.extend(strategy_values)
                    joint_outcomes.extend(joint_values)
                tested += 1
        joint_values = tuple(joint_outcomes)
        return HistoricalStrategyReplayResult(
            instrument, len(folds), tested, tuple(outcomes),
            joint_values, self._joint_metric_snapshots(instrument, joint_values),
            tuple(statuses),
        )
