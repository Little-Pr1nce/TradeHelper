"""V2-6 固定风控输入；只使用 V2 合同和合成冻结事实。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from strategy_helpers import NOW, strategy_input
from contracts import (
    AccountSnapshot, DataCapabilities, DataQualityReport, EvidenceStatus, FreshnessStatus,
    DecisionMode, Market, MarketState, PlanEvidenceSnapshot, PositionAvailability, PositionSnapshot,
    QualityAction, QualityStatus, RiskPolicy, RiskProfile, RiskRequest, ValuationPrice,
    ValuationPriceKind, stable_hash,
)
from risk import freeze_account_valuation
from risk.market_rules import default_market_rules
from strategies import StrategyEngine


def quality(*, status=QualityStatus.OK, action=QualityAction.NORMAL, multiplier=1.0, block=False):
    return DataQualityReport(status, action, 100.0, multiplier, block, (), DataCapabilities(), NOW)


def request_for(
    instrument,
    *,
    position=None,
    cash=Decimal("10000"),
    reference_price: float = 100.0,
    directions=None,
    quality_report=None,
    valuation_price: Decimal | None = None,
    availability: PositionAvailability | None = None,
    evidence: tuple[PlanEvidenceSnapshot, ...] = (),
    market_state: MarketState | None = None,
    policy: RiskPolicy | None = None,
    mode: DecisionMode = DecisionMode.EOD,
    quote_price: float | None = None,
    as_of: datetime = NOW,
):
    quality_report = quality_report or quality()
    input = strategy_input(
        instrument,
        position=position,
        reference_price=reference_price,
        directions=directions,
        quality_report=quality_report,
        mode=mode,
        quote_price=quote_price,
        as_of=as_of,
    )
    bundle = StrategyEngine().build(input, generated_at=NOW)
    account = AccountSnapshot(instrument.market, "CNY" if instrument.market is Market.A else "USD", cash, (position,) if position else (), NOW)
    prices = {}
    if position is not None and valuation_price is not None:
        prices[instrument] = ValuationPrice(
            instrument, valuation_price, as_of, "fixture",
            ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED,
        )
    valuation = freeze_account_valuation(account, prices, as_of, generated_at=NOW)
    return RiskRequest(
        instrument, bundle, input.trading_scenario, quality_report, account, valuation,
        availability, evidence, default_market_rules(instrument.market, instrument.exchange, NOW),
        market_state, policy or RiskPolicy(), as_of,
    )


def evidence_for(plan, status: EvidenceStatus, *, profile: RiskProfile | None = None) -> PlanEvidenceSnapshot:
    if status is EvidenceStatus.UNAVAILABLE:
        samples = oof = 0
        metrics = (None, None, None, None, None)
    elif status is EvidenceStatus.INSUFFICIENT_SAMPLE:
        samples = oof = 10
        metrics = (0.02, -0.01, 0.04, 0.55, -0.05)
    elif status is EvidenceStatus.NEGATIVE:
        samples = oof = 30
        metrics = (-0.01, -0.03, -0.001, 0.40, -0.12)
    elif status is EvidenceStatus.POSITIVE_UNCERTAIN:
        samples = oof = 30
        metrics = (0.02, -0.01, 0.04, 0.55, -0.05)
    else:
        samples = oof = 30
        metrics = (0.02, 0.005, 0.04, 0.60, -0.04)
    identity = {
        "instrument": plan.instrument, "strategy_id": plan.strategy_id,
        "strategy_version": plan.strategy_version, "parameter_hash": plan.parameter_hash,
        "profile": profile, "sample_count": samples, "oof_sample_count": oof,
        "metrics": metrics, "status": status, "source_ledger_version": "fixture_v1",
        "data_cutoff_at": NOW, "evaluated_at": NOW,
    }
    return PlanEvidenceSnapshot(
        stable_hash(identity), plan.instrument, plan.strategy_id, plan.strategy_version,
        plan.parameter_hash, profile, samples, oof, *metrics, status, "fixture_v1", NOW, NOW, NOW,
    )
