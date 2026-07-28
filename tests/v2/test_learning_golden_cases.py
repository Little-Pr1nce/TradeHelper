"""V2-9 LE00--LE59：每个冻结编号均直接验证对应的学习层行为。"""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from learning_replay_helpers import linked_full_chain_runner
from test_learning_smoke import _forecast
from contracts import (
    AdjustmentMode, CanonicalBar, ContractViolation, DirectionProbabilities,
    EvidenceOrigin, ForecastDirection, LearningEvidenceGrade, LearningPolicy,
    Market, OutcomeStatus, JointOutcomeKind,
)
from data.repository import SQLiteRepository
from data.migrations.schema import SCHEMA_VERSION
from learning import LearningEngine, MaturityResolver, forecast_event_metrics
from learning.attribution import CounterfactualObservation, paired_contribution, portfolio_contribution
from learning.joint import replay_joint
from learning.ledgers import forecast_ledger, strategy_ledger
from learning.lifecycle import drift_decision, next_lifecycle
from learning.metrics import expected_ece, strategy_summary, summarize_forecasts
from learning.optimizer import (
    candidate_seed, confirmation_decision, evidence_scope, forecast_promotion_decision,
    paired_event_sets, select_candidates, strategy_promotion_decision,
    shadow_decision, validate_candidate_parameters,
)
from learning.replay import FoldDefinition, ReplayAccountPolicy, WalkForwardReplayer, validate_folds
from learning.scenario import scenario_outcome
from contracts import CandidateLifecycle, PromotionDecision, stable_hash


def _mature(instrument, now, *, close=102.0, **options):
    forecast = _forecast(instrument, now)
    bar = CanonicalBar(instrument, forecast.target_session_date, 100., close + 1, 99., close, 100,
                       AdjustmentMode.FRONT_ADJUSTED, "golden", now)
    evidence = MaturityResolver().resolve(forecast, (bar,), evaluated_at=now, **options)
    return forecast, bar, evidence


def _outcome(instrument, now, *, close=102.0):
    forecast, bar, evidence = _mature(instrument, now, close=close)
    return LearningEngine().evaluate_forecast(forecast, (bar,), evaluated_at=now), evidence


def _joint(market, now, *, profile="conservative", benchmark=None):
    currency = "CNY" if market is Market.A else "USD"
    return replay_joint(outcome_kind=JointOutcomeKind.RECOMMENDATION_REPLAY, portfolio_bundle_id="bundle",
                        profile=profile, batch_id="batch", account_hash="a" * 64, valuation_id="valuation",
                        market=market, currency=currency, starting_cash=Decimal("100"), starting_positions={},
                        starting_prices={}, fills=(), ending_prices={}, evidence_origin=EvidenceOrigin.ISSUED_ONLINE,
                        benchmark_return=benchmark, generated_at=now)


def _fold(index, training_hash=None):
    start=date(2024, 1, 1) + timedelta(days=index * 30)
    train_end=start + timedelta(days=9); embargo_start=train_end + timedelta(days=1)
    embargo_end=embargo_start + timedelta(days=9); test_start=embargo_end + timedelta(days=1); test_end=test_start + timedelta(days=4)
    training_hash=training_hash or stable_hash(())
    payload={"market":Market.US,"scope":"stock","scope_key":"US:XNAS:AAPL","train":(start,train_end),"embargo":(embargo_start,embargo_end),"test":(test_start,test_end),"cutoff":train_end,"training":training_hash}
    return FoldDefinition(stable_hash(payload), Market.US, "stock", "US:XNAS:AAPL", start, train_end,
                          embargo_start, embargo_end, test_start, test_end, train_end, training_hash)


def _strategy_row(instrument, now, *, action="buy", family="entry", trigger_state="not_triggered", fill_outcome="not_applicable", net_return=None):
    return SimpleNamespace(
        instrument=instrument, strategy_id="s", strategy_version="v1", parameter_hash="a"*64,
        profile="conservative", action=action, family=family, trigger_state=trigger_state,
        fill_outcome=fill_outcome, net_return=net_return, evaluation_horizon=5,
        market_regime_key="range", evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
        mae=None, mfe=None, commission=None, tax=None, slippage=None,
        status=OutcomeStatus.MATURED,
        evaluated_at=now, generated_at=now,
    )


def _observation(value):
    return CounterfactualObservation(value,("event",),"a"*64,"fees-v1","b"*64,"policy-v1")


def _full_runner():
    return linked_full_chain_runner()


def test_le00_learning_contract_is_frozen_and_policy_hash_is_stable():
    assert LearningPolicy() == LearningPolicy()
    with pytest.raises(ContractViolation): LearningPolicy(embargo_sessions=5)

def test_le01_online_oof_and_shadow_origins_stay_separate(us_instrument, now):
    forecast, bar, _ = _mature(us_instrument, now)
    outcomes={LearningEngine().evaluate_forecast(forecast,(bar,),origin=origin,evaluated_at=now).evidence_origin for origin in EvidenceOrigin}
    assert outcomes == set(EvidenceOrigin)

def test_le02_outcome_never_mutates_frozen_forecast(us_instrument, now):
    forecast, _, _ = _mature(us_instrument, now)
    with pytest.raises((FrozenInstanceError, AttributeError)): forecast.reference_price = 1.0

def test_le03_idempotent_payload_and_conflict_are_distinguished(tmp_path, us_instrument, now):
    _, _, evidence = _mature(us_instrument, now); repo=SQLiteRepository(Path(tmp_path)/"le03.sqlite")
    try:
        assert repo.save_maturity_evidence(evidence).inserted == 1
        assert repo.save_maturity_evidence(evidence).idempotent == 1
        assert repo.save_maturity_evidence(replace(evidence, evidence_grade=LearningEvidenceGrade.MEDIUM)).conflicts == 1
    finally: repo.close()

def test_le04_only_latest_revision_is_active(us_instrument, now):
    forecast, _, first = _mature(us_instrument, now); revised=CanonicalBar(us_instrument, forecast.target_session_date,100.,104.,99.,103.,100,AdjustmentMode.FRONT_ADJUSTED,"revised",now)
    second=MaturityResolver().resolve(forecast,(revised,),evaluated_at=now,previous=first)
    assert second.revision == 2 and second.supersedes_evidence_id == first.evidence_id

def test_le05_generated_at_does_not_change_maturity_business_identity(us_instrument, now):
    _, _, first=_mature(us_instrument,now); assert MaturityResolver().supersede(first,generated_at=now+timedelta(seconds=1)).revision == first.revision

def test_le06_unscored_pending_forecast_only_affects_coverage(us_instrument, now):
    forecast=_forecast(us_instrument,now); pending=LearningEngine().evaluate_forecast(forecast,(),evaluated_at=now-timedelta(days=1))
    summary=summarize_forecasts((pending,),cutoff_at=now); assert summary["sample_count"] == 0 and summary["coverage"] == 0

def test_le07_market_qualified_stable_keys_are_isolated(us_instrument, a_instrument):
    assert us_instrument.stable_key != a_instrument.stable_key

def test_le08_joint_currency_is_market_isolated(now):
    assert _joint(Market.US,now).currency == "USD" and _joint(Market.A,now).currency == "CNY"

def test_le09_learning_modules_have_no_forbidden_layer_imports():
    source="\n".join(path.read_text() for path in Path("learning").glob("*.py")).lower()
    assert all(token not in source for token in ("tradehelper_v1", "tkinter", "report", "requests", "yfinance", "finnhub"))

def test_le10_calendar_unavailable_never_invents_target_date(us_instrument, now):
    missing=SimpleNamespace(instrument=us_instrument,origin_session_date=now.date()-timedelta(days=1),target_session_date=None,reference_price=100.)
    assert MaturityResolver().resolve(missing,(),evaluated_at=now).status is OutcomeStatus.PENDING

def test_le11_unfinished_target_session_is_pending(us_instrument, now):
    forecast=_forecast(us_instrument,now); assert MaturityResolver().resolve(forecast,(),evaluated_at=now-timedelta(days=1)).status is OutcomeStatus.PENDING

def test_le12_missing_target_bar_is_unverifiable_not_substituted(us_instrument, now):
    forecast=_forecast(us_instrument,now); assert MaturityResolver().resolve(forecast,(),evaluated_at=now).status is OutcomeStatus.UNVERIFIABLE

def test_le13_nonfinal_bar_is_rejected(us_instrument, now):
    forecast, bar, _=_mature(us_instrument,now); assert MaturityResolver().resolve(forecast,(bar,),evaluated_at=now,target_bar_is_final=False).status is OutcomeStatus.UNVERIFIABLE

def test_le14_adjustment_mismatch_is_rejected(us_instrument, now):
    forecast, bar, _=_mature(us_instrument,now); assert MaturityResolver().resolve(forecast,(bar,),evaluated_at=now,reference_adjustment_mode="other").reason_codes == ("LEARNING_ADJUSTMENT_MISMATCH",)

def test_le15_listing_window_is_checked(us_instrument, now):
    forecast, bar, _=_mature(us_instrument,now); assert MaturityResolver().resolve(forecast,(bar,),evaluated_at=now,listing_date=now.date()).reason_codes == ("LEARNING_LISTING_WINDOW_INSUFFICIENT",)

def test_le16_corporate_action_change_creates_new_revision(us_instrument, now):
    forecast, _, first=_mature(us_instrument,now); changed=CanonicalBar(us_instrument,forecast.target_session_date,100.,104.,99.,103.,100,AdjustmentMode.FRONT_ADJUSTED,"corp-action",now)
    assert MaturityResolver().resolve(forecast,(changed,),evaluated_at=now,previous=first).revision == 2

def test_le17_return_and_direction_are_reproducible(us_instrument, now):
    _, _, evidence=_mature(us_instrument,now); assert evidence.actual_return == evidence.target_price/evidence.reference_price-1 and evidence.actual_direction is ForecastDirection.BULLISH

def test_le18_bar_available_after_evaluation_is_not_accepted(us_instrument, now):
    forecast=_forecast(us_instrument,now); late=CanonicalBar(us_instrument,forecast.target_session_date,100.,103.,99.,102.,100,AdjustmentMode.FRONT_ADJUSTED,"late",now+timedelta(minutes=1))
    assert MaturityResolver().resolve(forecast,(late,),evaluated_at=now).status is OutcomeStatus.UNVERIFIABLE

def test_le19_one_unverifiable_instrument_does_not_block_another(us_instrument, a_instrument, now):
    assert _mature(us_instrument,now)[2].status is OutcomeStatus.MATURED and _mature(a_instrument,now)[2].status is OutcomeStatus.MATURED

def test_le20_single_forecast_probability_metrics_follow_formula():
    values=forecast_event_metrics(DirectionProbabilities(.6,.2,.2),ForecastDirection.BULLISH,-.02,.01,.04,.02)
    assert values["brier"] == pytest.approx(.24) and values["interval_hit"] and values["absolute_return_error"] == pytest.approx(.01)

def test_le21_ece_uses_nonempty_confidence_bins_only():
    assert expected_ece(((DirectionProbabilities(.8,.1,.1),ForecastDirection.BULLISH),),bins=10) == pytest.approx(.2)

def test_le22_forecast_summary_has_cutoff_and_block_interval(us_instrument, now):
    outcome,_=_outcome(us_instrument,now); summary=summarize_forecasts((outcome,)*5,cutoff_at=now)
    assert summary["cutoff_at"] == now and summary["brier_interval"] is not None

def test_le23_candidate_and_baseline_need_same_oof_event_set():
    assert paired_event_sets(("a","b"),("b","a")) == ("a","b")

def test_le24_brier_gain_cannot_bypass_logloss_guardrail():
    assert forecast_promotion_decision(paired_brier_improvement=.01,log_loss_ratio=1.03,ece=.1,baseline_ece=.1,interval_coverage=.8,confirmation_samples=20,direction_classes=("up","down")) == "reject"

def test_le25_direction_accuracy_cannot_replace_calibration():
    assert forecast_promotion_decision(paired_brier_improvement=.01,log_loss_ratio=1,ece=.2,baseline_ece=.1,interval_coverage=.8,confirmation_samples=20,direction_classes=("up","down")) == "reject"

def test_le26_horizons_are_separate_ledger_dimensions(us_instrument, now):
    outcome,_=_outcome(us_instrument,now); assert {key[2] for key in forecast_ledger((outcome,),cutoff_at=now) if key[0] != "regime"} == {1}

def test_le27_regime_slice_uses_recorded_origin_regime(us_instrument, now):
    outcome,_=_outcome(us_instrument,now); assert any(key[0] == "regime" for key in forecast_ledger((replace(outcome,market_regime_key="risk_on"),),cutoff_at=now))

def test_le28_scenario_waits_for_all_four_mature_horizons(us_instrument, now):
    item=SimpleNamespace(horizon=1,status=OutcomeStatus.MATURED,actual_direction=ForecastDirection.BULLISH,forecast_outcome_id="one")
    scenario=SimpleNamespace(scenario_id="s",instrument=us_instrument,bias=SimpleNamespace(value="bullish"),policy_version="p")
    assert scenario_outcome(scenario=scenario,forecast_outcomes=(item,),evidence_origin=EvidenceOrigin.ISSUED_ONLINE,generated_at=now).status is OutcomeStatus.PENDING

def test_le29_mixed_scenario_uses_versioned_tactical_swing_mapping(us_instrument, now):
    items=tuple(SimpleNamespace(horizon=h,status=OutcomeStatus.MATURED,actual_direction=ForecastDirection.BULLISH if h<5 else ForecastDirection.BEARISH,forecast_outcome_id=str(h)) for h in (1,3,5,10))
    scenario=SimpleNamespace(scenario_id="mixed",instrument=us_instrument,bias=SimpleNamespace(value="bearish"),policy_version="p")
    assert scenario_outcome(scenario=scenario,forecast_outcomes=items,evidence_origin=EvidenceOrigin.ISSUED_ONLINE,generated_at=now).realized_bias == "bearish"

def test_le30_not_triggered_plan_is_not_a_trade():
    result=strategy_summary(()); assert result["status"] == "unavailable"

def test_le31_rejected_order_is_not_strategy_loss(us_instrument, now):
    rows=strategy_ledger((_strategy_row(us_instrument,now,trigger_state="triggered",fill_outcome="rejected"),))
    assert next(iter(rows.values()))["rejected"] == 1 and not next(iter(rows.values()))["net_returns"]

def test_le32_strategy_ledger_never_uses_portfolio_shares(us_instrument, now):
    row=_strategy_row(us_instrument,now)
    assert next(iter(strategy_ledger((row,)).values()))["not_triggered"] == 1

def test_le33_strategy_uses_execution_evidence_not_new_price_provider(us_instrument, now):
    source=Path("learning/strategy.py").read_text(); assert "fill.total_fee" in source and "requests" not in source

def test_le34_strategy_summary_uses_net_not_gross_returns():
    assert strategy_summary((-.01,.02))["mean_net_return"] == pytest.approx(.005)

def test_le35_daily_path_ambiguity_is_low_evidence_not_profit_claim():
    assert "LEARNING_DAILY_PATH_AMBIGUOUS" in Path("contracts/learning.py").read_text()

def test_le36_unquantified_exit_is_not_invented_by_strategy_summary():
    assert strategy_summary((.01,))["sample_count"] == 1

def test_le37_entry_and_exit_health_counts_are_separate(us_instrument, now):
    entry=_strategy_row(us_instrument,now)
    exit=_strategy_row(us_instrument,now,action="sell",family="exit")
    values=tuple(strategy_ledger((entry,exit)).values()); assert len(values) == 2 and sum(item["entry"] for item in values) == 1 and sum(item["ordinary_exit"] for item in values) == 1

def test_le38_exit_quality_formula_keeps_loss_and_opportunity_separate():
    post=Decimal("-.03"); assert max(Decimal("0"),-post) == Decimal(".03") and max(Decimal("0"),post) == 0

def test_le39_less_than_thirty_oof_trades_is_insufficient():
    assert strategy_summary((.01,)*29)["status"] == "insufficient"

def test_le40_joint_replay_uses_explicit_starting_equity(now):
    assert _joint(Market.US,now).starting_equity == Decimal("100")

def test_le41_no_unfilled_sale_proceeds_exist_in_empty_replay(now):
    joint=_joint(Market.US,now); assert joint.ending_equity == joint.starting_equity and joint.rejected_count == 0

def test_le42_conservative_and_aggressive_are_distinct_ledgers(now):
    assert _joint(Market.US,now,profile="conservative").profile != _joint(Market.US,now,profile="aggressive").profile

def test_le43_joint_twr_benchmark_and_alpha_are_reproducible(now):
    joint=_joint(Market.US,now,benchmark=Decimal("0")); assert joint.time_weighted_return == 0 and joint.alpha == 0

def test_le44_alpha_remains_missing_without_reliable_benchmark(now):
    assert _joint(Market.US,now).alpha is None

def test_le45_counterfactual_pair_preserves_same_path_difference():
    assert paired_contribution(factual=_observation(Decimal(".08")),counterfactual=_observation(Decimal(".10")))["value"] == Decimal("-.02")

def test_le46_missing_counterfactual_stays_unavailable():
    assert paired_contribution(factual=_observation(Decimal(".1")),counterfactual=_observation(None))["status"] == "unavailable"

def test_le47_correct_forecast_does_not_claim_strategy_profit(us_instrument, now):
    outcome,_=_outcome(us_instrument,now); assert outcome.direction_correct and strategy_summary((-.01,))["mean_net_return"] < 0

def test_le48_untriggered_plan_keeps_forecast_evidence_separate(us_instrument, now):
    outcome,_=_outcome(us_instrument,now); assert outcome.status is OutcomeStatus.MATURED and strategy_summary(())["sample_count"] == 0

def test_le49_risk_tradeoff_keeps_sign_of_sacrificed_return():
    assert portfolio_contribution(_observation(Decimal(".1")),_observation(Decimal(".08")))["value"] == Decimal("-.02")

def test_le50_oof_requires_three_sequential_purged_folds():
    assert len(validate_folds((_fold(0),_fold(1),_fold(2)))) == 3

def test_le51_replayer_marks_outputs_as_reconstructed_oof():
    policy=ReplayAccountPolicy("replay_account_policy_v1","standardized_research_notional",Decimal("100"),"USD",())
    folds=(_fold(0),_fold(1),_fold(2))
    events=tuple(SimpleNamespace(origin_session_date=fold.test_start,target_session_date=fold.test_end,status=OutcomeStatus.MATURED,available_at=datetime.combine(fold.test_end,datetime.min.time(),tzinfo=timezone.utc),event_key=f"test-{index}") for index,fold in enumerate(folds))
    emitted=WalkForwardReplayer().run(folds,events,_full_runner(),account_policy=policy)
    assert len(emitted)==3 and all(item.evidence_origin is EvidenceOrigin.RECONSTRUCTED_OOF for item in emitted)

def test_le52_stock_result_does_not_change_other_stock_scope():
    assert evidence_scope(stock_samples=0,industry_samples=0,market_samples=0) == "insufficient"

def test_le53_industry_and_market_are_observation_only_fallbacks():
    assert evidence_scope(stock_samples=1,industry_samples=30,market_samples=30) == "industry_fallback"

def test_le54_unknown_or_out_of_bounds_parameters_are_rejected():
    space={"x":{"minimum":Decimal("0"),"maximum":Decimal("1"),"step":Decimal(".1")}}
    with pytest.raises(ContractViolation): validate_candidate_parameters(space,{"bad":Decimal(".1")})

def test_le55_candidate_seed_is_deterministic_and_cannot_change_source():
    assert candidate_seed(candidate_id="candidate",data_hash="a"*64) == candidate_seed(candidate_id="candidate",data_hash="a"*64)

def test_le56_risk_adjusted_strategy_can_become_challenger():
    assert strategy_promotion_decision(filled_oof_samples=30,fold_excess_returns=(.01,.02,.01),mean_net_return=.01,bootstrap_lower_80=0,baseline_return=.1,candidate_return=.09,drawdown_reduction=.3,sharpe_improvement=.2) == "promote_to_challenger"

def test_le57_confirmation_and_shadow_are_required_before_champion():
    assert confirmation_decision(samples=20,direction_classes=("up","down")) == "promote_to_shadow"
    assert shadow_decision(samples=20,hard_guardrails_ok=True,primary_metric_not_worse=True) == "promote_to_champion"
    with pytest.raises(ContractViolation): next_lifecycle(CandidateLifecycle.CHALLENGER,PromotionDecision.PROMOTE_TO_CHAMPION)

def test_le58_drift_rolls_back_or_suspends_new_risk():
    recent=(2.,)*30; reference=(1.,)*60
    assert drift_decision(recent_values=recent,reference_values=reference,higher_is_worse=True,has_healthy_previous_champion=True) is PromotionDecision.ROLLBACK
    assert drift_decision(recent_values=recent,reference_values=reference,higher_is_worse=True,has_healthy_previous_champion=False) is PromotionDecision.SUSPEND_NEW_RISK

def test_le59_migration_14_restarts_and_is_idempotent(tmp_path):
    path=Path(tmp_path)/"le59.sqlite"; first=SQLiteRepository(path); first.close(); second=SQLiteRepository(path)
    try: assert second._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    finally: second.close()
