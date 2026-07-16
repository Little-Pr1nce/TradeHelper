"""V2-9 唯一学习入口；调用方提供冻结 ForecastResult 与已完成日K。"""
from __future__ import annotations

from tradehelper_v2.contracts import (EvidenceOrigin, ForecastOutcome, LearningEvidenceGrade, OutcomeStatus, stable_hash)
from .maturity import MaturityResolver
from .metrics import forecast_event_metrics
from .scenario import scenario_outcome
from .strategy import strategy_outcome

class LearningEngine:
    def evaluate_forecast(self, forecast, bars, *, origin=EvidenceOrigin.ISSUED_ONLINE, evaluated_at, previous_evidence=None, market_regime_key=None, **maturity_options):
        """把冻结 ForecastResult 与到期日K连接；不会改写原预测事实。"""
        evidence=MaturityResolver().resolve(forecast,bars,evaluated_at=evaluated_at,previous=previous_evidence,**maturity_options)
        if forecast.availability.value!="available" or evidence.status is not OutcomeStatus.MATURED:
            identity={"forecast_event_key":forecast.event_key,"origin":origin,"maturity":evidence.evidence_id,"status":evidence.status,"actual_return":None,"revision":evidence.evidence_id,"reasons":("LEARNING_FORECAST_UNAVAILABLE_NOT_SCORED",)}
            return ForecastOutcome(stable_hash(identity),forecast.event_key,forecast.instrument,forecast.origin_session_date,forecast.target_session_date or forecast.origin_session_date,forecast.horizon,forecast.model_scope,forecast.scope_key,forecast.model_family.value,forecast.model_version,forecast.feature_set_id,forecast.model_input_hash,forecast.training_data_hash,origin,evidence.evidence_id,None,None,None,None,None,None,None,None,None,None,None,None,None,None,evidence.status,LearningEvidenceGrade.INSUFFICIENT,("LEARNING_FORECAST_UNAVAILABLE_NOT_SCORED",),evaluated_at,evaluated_at)
        distribution=forecast.return_distribution; metrics=forecast_event_metrics(forecast.probabilities,evidence.actual_direction,distribution.p10,distribution.p50,distribution.p90,float(evidence.actual_return))
        origin_reason={EvidenceOrigin.ISSUED_ONLINE:"LEARNING_ISSUED_ONLINE",EvidenceOrigin.RECONSTRUCTED_OOF:"LEARNING_RECONSTRUCTED_OOF",EvidenceOrigin.SHADOW_ONLINE:"LEARNING_SHADOW_ONLY"}[origin]
        reasons=("LEARNING_FORECAST_SCORED",origin_reason)
        identity={"forecast_event_key":forecast.event_key,"origin":origin,"maturity":evidence.evidence_id,"status":OutcomeStatus.MATURED,"actual_return":evidence.actual_return,"revision":evidence.evidence_id,"reasons":tuple(sorted(reasons))}
        return ForecastOutcome(stable_hash(identity),forecast.event_key,forecast.instrument,forecast.origin_session_date,forecast.target_session_date,forecast.horizon,forecast.model_scope,forecast.scope_key,forecast.model_family.value,forecast.model_version,forecast.feature_set_id,forecast.model_input_hash,forecast.training_data_hash,origin,evidence.evidence_id,forecast.direction,forecast.probabilities,distribution.p10,distribution.p50,distribution.p90,evidence.actual_direction,evidence.actual_return,evidence.target_price,metrics["direction_correct"],metrics["brier"],metrics["log_loss"],metrics["interval_hit"],metrics["absolute_return_error"],market_regime_key,OutcomeStatus.MATURED,LearningEvidenceGrade.HIGH,reasons,evaluated_at,evaluated_at)

    def evaluate_scenario(self, scenario, forecast_outcomes, *, origin=EvidenceOrigin.ISSUED_ONLINE, generated_at):
        return scenario_outcome(scenario=scenario,forecast_outcomes=forecast_outcomes,evidence_origin=origin,generated_at=generated_at)

    def evaluate_strategy(self, **kwargs):
        """显式委托 V2-7 fill 证据构造策略账，避免另起一条成交路径。"""
        return strategy_outcome(**kwargs)
