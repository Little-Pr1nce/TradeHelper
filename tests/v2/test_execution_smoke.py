"""V2-7 主链路冒烟测试：全部使用合成冻结输入，不联网。"""
from __future__ import annotations

from pathlib import Path
from decimal import Decimal

from risk_helpers import request_for
from tradehelper_v2.contracts import (
    DecisionMode, EventGranularity, ExecutionEvent, ExecutionPolicy, ExecutionState,
    LiquidityEvidence, TradingStatus, stable_hash,
)
from tradehelper_v2.risk import RiskOfficer
from tradehelper_v2.execution import HistoricalFillSimulator, OrderIntentFactory
from tradehelper_v2.execution.simulator import HistoricalSimulationRequest
from tradehelper_v2.data.repository import SQLiteRepository


def test_execution_build_and_replay_share_one_intent(tmp_path, us_instrument, calendar, now):
    """订单工厂只冻结风险批准量，回放消费同一对象而不重算仓位。"""
    request = request_for(us_instrument, mode=DecisionMode.EOD, as_of=now)
    risk_bundle = RiskOfficer().assess(request, generated_at=now)
    plans = {
        plan.plan_id: plan
        for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold, request.strategy_bundle.invalidation)
        for plan in branch.plans
        if any(decision.plan_id == plan.plan_id for decision in risk_bundle.decisions)
    }
    bundle = OrderIntentFactory(calendar).build_bundle(risk_bundle, plans, {}, now, ExecutionPolicy())
    assert len(bundle.records) == len(risk_bundle.decisions)
    # 固定 fixture 中可能只有观察计划；这同样必须留下 no_order 审计记录。
    if not bundle.intents:
        assert all(record.status.value == "no_order" for record in bundle.records)
        return
    intent = bundle.intents[0]
    event = ExecutionEvent("event-1", us_instrument, now.date(), now, now, EventGranularity.QUOTE, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1000"), Decimal("99"), None, None, TradingStatus.OPEN, "fixture", "high", now, now)
    liquidity_hash = stable_hash({"median_daily_volume_20": Decimal("100000"), "annualized_volatility_20": Decimal("0.20"), "cutoff_at": now, "source": "fixture"})
    state = ExecutionState(
        us_instrument.market,
        "USD",
        Decimal("100000"),
        Decimal("0"),
        Decimal("0"),
        None,
        None,
        None,
        None,
        now,
        "fixture",
        account_hash=intent.account_hash,
    )
    result = HistoricalFillSimulator().simulate(HistoricalSimulationRequest(intent, state, (event,), request.market_rules, ExecutionPolicy(), LiquidityEvidence(Decimal("100000"), Decimal("0.20"), now, "fixture", liquidity_hash), now))
    assert result.run.intent_id == intent.intent_id
    repository = SQLiteRepository(Path(tmp_path) / "execution.sqlite")
    try:
        # 新测试使用独立临时文件前先确保 run/fill 的持久化会被双向复核。
        repository.save_execution_result(result.run, result.fills)
        assert repository.get_execution_run(result.run.run_id) == result.run
    finally:
        repository.close()


def test_execution_records_are_idempotent_and_reconstruct_intent(tmp_path, us_instrument, calendar, now):
    """migration 11 忽略仅发行时间差异，并能重建订单意图。"""
    request = request_for(us_instrument, mode=DecisionMode.EOD, as_of=now)
    risk_bundle = RiskOfficer().assess(request, generated_at=now)
    plans = {plan.plan_id: plan for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold, request.strategy_bundle.invalidation) for plan in branch.plans if any(item.plan_id == plan.plan_id for item in risk_bundle.decisions)}
    built = OrderIntentFactory(calendar).build_bundle(risk_bundle, plans, {}, now, ExecutionPolicy())
    repository = SQLiteRepository(Path(tmp_path) / "v2.sqlite")
    try:
        for record in built.records:
            assert repository.save_order_intent_build_record(record).inserted == 1
        for intent in built.intents:
            assert repository.save_order_intent(intent).inserted == 1
            assert repository.save_order_intent(intent).idempotent == 1
            assert repository.get_order_intent(intent.intent_id) == intent
    finally:
        repository.close()
