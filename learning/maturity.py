"""交易所目标会话的到期事实解析。"""
from __future__ import annotations
from decimal import Decimal
from contracts import (CanonicalBar, ForecastAvailability, ForecastDirection, LearningEvidenceGrade, MaturityEvidence, OutcomeStatus, stable_hash)

class MaturityResolver:
    def resolve(self, forecast, bars, *, evaluated_at, flat_band=None, previous=None, reference_adjustment_mode="front_adjusted", listing_date=None, target_bar_is_final=True):
        """只接受目标日正式日 K；缺失不寻找未来替代日。"""
        if forecast.target_session_date is None:
            return self._pending(forecast,evaluated_at,"LEARNING_CALENDAR_UNAVAILABLE")
        if listing_date is not None and (forecast.origin_session_date < listing_date or forecast.target_session_date < listing_date):
            return self._unverifiable(forecast,evaluated_at,"LEARNING_LISTING_WINDOW_INSUFFICIENT")
        resolved_band = flat_band if flat_band is not None else getattr(forecast, "label_flat_band", None)
        if resolved_band is None:
            return self._unverifiable(forecast,evaluated_at,"LEARNING_LABEL_POLICY_UNAVAILABLE")
        resolved_band = Decimal(str(resolved_band))
        target=next((bar for bar in bars if bar.instrument==forecast.instrument and bar.trading_date==forecast.target_session_date),None)
        if target is None:
            if evaluated_at.date() <= forecast.target_session_date:
                return self._pending(forecast,evaluated_at,"LEARNING_PENDING_TARGET_SESSION")
            return self._unverifiable(forecast,evaluated_at,"LEARNING_TARGET_BAR_MISSING")
        if not target_bar_is_final or target.fetched_at>evaluated_at or target.fetched_at.date()<forecast.target_session_date:
            return self._unverifiable(forecast,evaluated_at,"LEARNING_TARGET_BAR_NOT_FINAL")
        if target.adjustment_mode.value != reference_adjustment_mode:
            return self._unverifiable(forecast,evaluated_at,"LEARNING_ADJUSTMENT_MISMATCH")
        reference=Decimal(str(forecast.reference_price)); price=Decimal(str(target.close)); returned=price/reference-Decimal("1")
        direction=ForecastDirection.BULLISH if returned>resolved_band else ForecastDirection.BEARISH if returned<-resolved_band else ForecastDirection.NEUTRAL
        payload_hash=stable_hash(target.to_dict())
        if previous and previous.bar_payload_hash==payload_hash: return previous
        revision=1 if previous is None else previous.revision+1; supersedes=None if previous is None else previous.evidence_id
        reasons=("LEARNING_MATURED",) if supersedes is None else ("LEARNING_MATURED","LEARNING_REVISION_SUPERSEDED")
        identity={"instrument":forecast.instrument,"origin":forecast.origin_session_date,"target":forecast.target_session_date,"reference_adjustment_mode":target.adjustment_mode.value,"reference":reference,"target_bar_key":target.stable_key,"target_price":price,"revision":revision,"supersedes":supersedes}
        return MaturityEvidence(stable_hash(identity),forecast.instrument,forecast.origin_session_date,forecast.target_session_date,target.adjustment_mode.value,reference,target.stable_key,price,returned,direction,resolved_band,target.source,payload_hash,target.fetched_at,target.fetched_at,evaluated_at,OutcomeStatus.MATURED,LearningEvidenceGrade.HIGH,revision,supersedes,reasons,evaluated_at)
    def _pending(self, forecast, at, reason): return self._missing(forecast,at,OutcomeStatus.PENDING,reason)
    def _unverifiable(self, forecast, at, reason): return self._missing(forecast,at,OutcomeStatus.UNVERIFIABLE,reason)
    def _missing(self,forecast,at,status,reason):
        target=forecast.target_session_date or forecast.origin_session_date
        identity={"instrument":forecast.instrument,"origin":forecast.origin_session_date,"target":target,"reference_adjustment_mode":"unavailable","reference":Decimal(str(forecast.reference_price)),"target_bar_key":None,"target_price":None,"revision":1,"supersedes":None}
        band=getattr(forecast,"label_flat_band",None)
        return MaturityEvidence(stable_hash(identity),forecast.instrument,forecast.origin_session_date,target,"unavailable",Decimal(str(forecast.reference_price)),None,None,None,None,Decimal(str(band)) if band is not None else Decimal("0"),None,None,None,None,at,status,LearningEvidenceGrade.INSUFFICIENT,1,None,(reason,),at)

    def supersede(self, evidence, *, generated_at):
        """旧 revision 保留原事实，但明确不能再进入任何聚合分母。"""
        reasons=tuple(sorted(set(evidence.reason_codes+("LEARNING_REVISION_SUPERSEDED",))))
        identity={"instrument":evidence.instrument,"origin":evidence.origin_session_date,"target":evidence.target_session_date,"reference_adjustment_mode":evidence.reference_adjustment_mode,"reference":evidence.reference_price,"target_bar_key":evidence.target_bar_key,"target_price":evidence.target_price,"revision":evidence.revision,"supersedes":evidence.supersedes_evidence_id}
        return MaturityEvidence(stable_hash(identity),evidence.instrument,evidence.origin_session_date,evidence.target_session_date,evidence.reference_adjustment_mode,evidence.reference_price,evidence.target_bar_key,evidence.target_price,evidence.actual_return,evidence.actual_direction,evidence.flat_band,evidence.bar_source,evidence.bar_payload_hash,evidence.bar_fetched_at,evidence.available_at,evidence.evaluated_at,OutcomeStatus.SUPERSEDED,evidence.evidence_grade,evidence.revision,evidence.supersedes_evidence_id,reasons,generated_at)
