"""V2-8 合成冻结批次；不联网，也不依赖 V1 组合服务。"""
from __future__ import annotations

from decimal import Decimal

from itertools import combinations

from risk_helpers import NOW, quality, request_for
from strategy_helpers import strategy_input
from contracts import (
    AccountSnapshot, CorrelationPair, CorrelationStatus, DecisionMode, FreshnessStatus,
    InstrumentReturnRisk, Market, PortfolioCandidate,
    PortfolioCorrelationSnapshot, PortfolioInputBatch, PortfolioPolicy,
    PortfolioRole, RiskPolicy, RiskRequest, ValuationPrice, ValuationPriceKind,
    stable_hash,
)
from portfolio import build_holding_risks
from risk import RiskOfficer, freeze_account_valuation
from risk.market_rules import default_market_rules
from strategies import StrategyEngine


def portfolio_batch(instrument, *, position=None, cash=Decimal("10000"), valuation_price=Decimal("100"), availability=None):
    request = request_for(
        instrument, position=position, cash=cash, reference_price=100.0,
        valuation_price=valuation_price if position is not None else None,
        availability=availability, as_of=NOW,
    )
    risk_bundle = RiskOfficer().assess(request, generated_at=NOW)
    plans = {plan.plan_id: plan for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold, request.strategy_bundle.invalidation) for plan in branch.plans}
    candidates = []
    for decision in risk_bundle.decisions:
        plan = plans[decision.plan_id]
        role = PortfolioRole.HOLDING if position is not None else PortfolioRole.WATCHLIST
        identity = {"role": role, "scenario_id": request.trading_scenario.scenario_id,
                    "plan_id": plan.plan_id, "decision_id": decision.decision_id,
                    "evidence_id": None, "rule_version": request.market_rules.rule_version}
        candidates.append(PortfolioCandidate(stable_hash(identity), role, request.trading_scenario, plan, decision, None, request.market_rules, NOW))
    corr_identity = {"market": instrument.market, "universe": (instrument,),
                     "instrument_risks": (InstrumentReturnRisk(instrument, 0, None, None, None, "none", "fixture-bars"),),
                     "pairs": (), "lookback": 90, "minimum": 20,
                     "method": "simple_daily_close_return_v1", "annualization": 252,
                     "cutoff_at": NOW, "status": CorrelationStatus.UNAVAILABLE, "source_batch_hash": "fixture-bars"}
    correlation = PortfolioCorrelationSnapshot(stable_hash(corr_identity), instrument.market, (instrument,), corr_identity["instrument_risks"], (), 90, 20, "simple_daily_close_return_v1", 252, NOW, CorrelationStatus.UNAVAILABLE, "fixture-bars", NOW)
    policy = PortfolioPolicy()
    risk_policy = RiskPolicy()
    holding_risks = build_holding_risks(
        valuation=request.valuation, account=request.account_snapshot, candidates=tuple(candidates),
        risk_bundles=(risk_bundle,), captured_at=NOW, generated_at=NOW,
    )
    watchlist = () if position is not None else (instrument,)
    identity = {"market": instrument.market, "currency": request.account_snapshot.currency, "mode": request.trading_scenario.mode,
                "account_hash": stable_hash(request.account_snapshot), "valuation_id": request.valuation.valuation_id,
                "risk_policy": risk_policy.parameter_hash, "portfolio_policy": policy.parameter_hash,
                "bundle_ids": (risk_bundle.risk_bundle_id,), "candidate_ids": tuple(sorted(item.candidate_id for item in candidates)),
                "watchlist": watchlist, "holding_risk_ids": tuple(item.holding_risk_id for item in holding_risks), "correlation_snapshot_id": correlation.correlation_snapshot_id, "as_of": NOW}
    return PortfolioInputBatch(stable_hash(identity), instrument.market, request.account_snapshot.currency, request.trading_scenario.mode,
                               request.account_snapshot, request.valuation, risk_policy, policy, (risk_bundle,), tuple(candidates),
                               watchlist, holding_risks, correlation, NOW, NOW)


def portfolio_batch_many(instruments, *, positions=(), cash=Decimal("10000"), valuation_prices=None):
    """构造同市场共享账户的真实多股票 V2-5/V2-6 输出。"""
    instruments = tuple(instruments)
    if not instruments:
        raise ValueError("one non-empty market is required")
    market = instruments[0].market
    if any(item.market is not market for item in instruments):
        raise ValueError("one non-empty market is required")
    position_map = {item.instrument: item for item in positions}
    account = AccountSnapshot(market, "CNY" if market is Market.A else "USD", cash, tuple(positions), NOW)
    if valuation_prices is None:
        valuation_prices = {item.instrument: Decimal("100") for item in positions}
    prices = {
        instrument: ValuationPrice(instrument, price, NOW, "fixture", ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
        for instrument, price in valuation_prices.items()
    }
    valuation = freeze_account_valuation(account, prices, NOW, generated_at=NOW)
    q = quality(); risk_policy = RiskPolicy(); policy = PortfolioPolicy()
    bundles = []; candidates = []
    for instrument in instruments:
        input_value = strategy_input(instrument, position=position_map.get(instrument), quality_report=q, as_of=NOW)
        strategy_bundle = StrategyEngine().build(input_value, generated_at=NOW)
        rules = default_market_rules(instrument.market, instrument.exchange, NOW)
        request = RiskRequest(instrument, strategy_bundle, input_value.trading_scenario, q, account, valuation,
                              None, (), rules, None, risk_policy, NOW)
        risk_bundle = RiskOfficer().assess(request, generated_at=NOW)
        bundles.append(risk_bundle)
        plans = {plan.plan_id: plan for branch in (strategy_bundle.entry_or_add, strategy_bundle.reduce_or_exit,
                                                    strategy_bundle.hold, strategy_bundle.invalidation)
                 for plan in branch.plans}
        role = PortfolioRole.HOLDING if instrument in position_map else PortfolioRole.WATCHLIST
        for decision in risk_bundle.decisions:
            plan = plans[decision.plan_id]
            identity = {"role": role, "scenario_id": input_value.trading_scenario.scenario_id,
                        "plan_id": plan.plan_id, "decision_id": decision.decision_id,
                        "evidence_id": None, "rule_version": rules.rule_version}
            candidates.append(PortfolioCandidate(stable_hash(identity), role, input_value.trading_scenario,
                                                  plan, decision, None, rules, NOW))
    universe = tuple(sorted(instruments, key=lambda item: item.stable_key))
    risks = tuple(InstrumentReturnRisk(item, 0, None, None, None, "none", "fixture-bars") for item in universe)
    pairs = tuple(CorrelationPair(left, right, None, 0, CorrelationStatus.UNAVAILABLE)
                  for left, right in combinations(universe, 2))
    corr_identity = {"market": market, "universe": universe, "instrument_risks": risks, "pairs": pairs,
                     "lookback": 90, "minimum": 20, "method": "simple_daily_close_return_v1",
                     "annualization": 252, "cutoff_at": NOW, "status": CorrelationStatus.UNAVAILABLE,
                     "source_batch_hash": "fixture-bars"}
    correlation = PortfolioCorrelationSnapshot(stable_hash(corr_identity), market, universe, risks, pairs, 90, 20,
                                               "simple_daily_close_return_v1", 252, NOW,
                                               CorrelationStatus.UNAVAILABLE, "fixture-bars", NOW)
    holding_risks = build_holding_risks(valuation=valuation, account=account, candidates=tuple(candidates),
                                        risk_bundles=tuple(bundles), captured_at=NOW, generated_at=NOW)
    watchlist = tuple(item for item in universe if item not in position_map)
    bundles = tuple(sorted(bundles, key=lambda item: item.risk_bundle_id))
    candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    identity = {"market": market, "currency": account.currency, "mode": candidates[0].trading_scenario.mode,
                "account_hash": stable_hash(account), "valuation_id": valuation.valuation_id,
                "risk_policy": risk_policy.parameter_hash, "portfolio_policy": policy.parameter_hash,
                "bundle_ids": tuple(item.risk_bundle_id for item in bundles),
                "candidate_ids": tuple(item.candidate_id for item in candidates), "watchlist": watchlist,
                "holding_risk_ids": tuple(item.holding_risk_id for item in holding_risks),
                "correlation_snapshot_id": correlation.correlation_snapshot_id, "as_of": NOW}
    return PortfolioInputBatch(stable_hash(identity), market, account.currency, candidates[0].trading_scenario.mode,
                               account, valuation, risk_policy, policy, bundles, candidates, watchlist,
                               holding_risks, correlation, NOW, NOW)


def correlation_for(universe, *, coefficient=Decimal("0"), samples=30):
    universe = tuple(sorted(universe, key=lambda item: item.stable_key))
    market = universe[0].market
    risks = tuple(InstrumentReturnRisk(item, samples, NOW.date(), NOW.date(), Decimal("0.20"),
                                       "front_adjusted", "fixture-complete") for item in universe)
    pairs = tuple(CorrelationPair(left, right, coefficient, samples, CorrelationStatus.COMPLETE)
                  for left, right in combinations(universe, 2))
    identity = {"market": market, "universe": universe, "instrument_risks": risks, "pairs": pairs,
                "lookback": 90, "minimum": 20, "method": "simple_daily_close_return_v1",
                "annualization": 252, "cutoff_at": NOW, "status": CorrelationStatus.COMPLETE,
                "source_batch_hash": "fixture-complete"}
    return PortfolioCorrelationSnapshot(stable_hash(identity), market, universe, risks, pairs, 90, 20,
                                        "simple_daily_close_return_v1", 252, NOW,
                                        CorrelationStatus.COMPLETE, "fixture-complete", NOW)


def rebuild_batch(batch, **changes):
    values = {name: getattr(batch, name) for name in batch.__dataclass_fields__ if name != "batch_id"}
    values.update(changes)
    bundles = tuple(sorted(values["risk_bundles"], key=lambda item: item.risk_bundle_id))
    candidates = tuple(sorted(values["candidates"], key=lambda item: item.candidate_id))
    watchlist = tuple(sorted(values["watchlist"], key=lambda item: item.stable_key))
    risks = tuple(sorted(values["holding_risks"], key=lambda item: item.instrument.stable_key))
    identity = {"market": values["market"], "currency": values["account_snapshot"].currency,
                "mode": values["mode"], "account_hash": stable_hash(values["account_snapshot"]),
                "valuation_id": values["valuation"].valuation_id,
                "risk_policy": values["risk_policy"].parameter_hash,
                "portfolio_policy": values["portfolio_policy"].parameter_hash,
                "bundle_ids": tuple(item.risk_bundle_id for item in bundles),
                "candidate_ids": tuple(item.candidate_id for item in candidates), "watchlist": watchlist,
                "holding_risk_ids": tuple(item.holding_risk_id for item in risks),
                "correlation_snapshot_id": values["correlation_snapshot"].correlation_snapshot_id,
                "as_of": values["as_of"]}
    return PortfolioInputBatch(stable_hash(identity), values["market"], values["currency"], values["mode"],
                               values["account_snapshot"], values["valuation"], values["risk_policy"],
                               values["portfolio_policy"], bundles, candidates, watchlist, risks,
                               values["correlation_snapshot"], values["as_of"], values["generated_at"],
                               values["schema_version"])


def empty_portfolio_batch(market=Market.US, *, cash=Decimal("1000")):
    account = AccountSnapshot(market, "CNY" if market is Market.A else "USD", cash, (), NOW)
    valuation = freeze_account_valuation(account, {}, NOW, generated_at=NOW)
    policy = PortfolioPolicy(); risk_policy = RiskPolicy()
    corr_identity = {"market": market, "universe": (), "instrument_risks": (), "pairs": (),
                     "lookback": 90, "minimum": 20, "method": "simple_daily_close_return_v1",
                     "annualization": 252, "cutoff_at": NOW, "status": CorrelationStatus.UNAVAILABLE,
                     "source_batch_hash": "empty"}
    correlation = PortfolioCorrelationSnapshot(stable_hash(corr_identity), market, (), (), (), 90, 20,
                                               "simple_daily_close_return_v1", 252, NOW,
                                               CorrelationStatus.UNAVAILABLE, "empty", NOW)
    identity = {"market": market, "currency": account.currency, "mode": DecisionMode.EOD,
                "account_hash": stable_hash(account), "valuation_id": valuation.valuation_id,
                "risk_policy": risk_policy.parameter_hash, "portfolio_policy": policy.parameter_hash,
                "bundle_ids": (), "candidate_ids": (), "watchlist": (), "holding_risk_ids": (),
                "correlation_snapshot_id": correlation.correlation_snapshot_id, "as_of": NOW}
    return PortfolioInputBatch(stable_hash(identity), market, account.currency, DecisionMode.EOD, account,
                               valuation, risk_policy, policy, (), (), (), (), correlation, NOW, NOW)
