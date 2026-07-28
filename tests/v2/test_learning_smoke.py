"""V2-9 离线基础验收：成熟度、概率指标、migration 13 与双市场隔离。"""
from __future__ import annotations
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from contracts import (AdjustmentMode, CanonicalBar, DirectionProbabilities, ForecastAvailability, ForecastDirection, ForecastResult, ForecastScope, ModelFamily, ModelLifecycle, ValidationStatus)
from data.repository import SQLiteRepository
from data.migrations.schema import SCHEMA_VERSION
from learning import LearningEngine, MaturityResolver, forecast_event_metrics

def _forecast(instrument, now):
    origin=now.date()-timedelta(days=5); target=now.date()-timedelta(days=1); probs=DirectionProbabilities(.6,.2,.2)
    return ForecastResult(instrument,now,origin,target,1,100.,ForecastAvailability.AVAILABLE,probs,__import__('contracts',fromlist=['ReturnDistribution']).ReturnDistribution(-.02,.01,.04,'empirical'),ForecastDirection.BULLISH,.4,ForecastScope.STOCK,instrument.stable_key,ModelFamily.ANALOG,'learning-fixture',ModelLifecycle.CHAMPION,ValidationStatus.CONFIRMATION_PASSED,True,'set','v1','a'*64,'b'*64,40,30,(),'fixture',None,'|'.join((instrument.stable_key,origin.isoformat(),target.isoformat(),'1','learning-fixture','a'*64)),now,label_flat_band=.005)

def test_learning_maturity_and_probability_metrics_are_market_isolated(us_instrument,a_instrument,now):
    for instrument in (us_instrument,a_instrument):
        forecast=_forecast(instrument,now)
        bar=CanonicalBar(instrument,forecast.target_session_date,100.,103.,99.,102.,1000,AdjustmentMode.FRONT_ADJUSTED,'fixture',now)
        evidence=MaturityResolver().resolve(forecast,(bar,),evaluated_at=now)
        assert evidence.status.value=='matured'
        assert evidence.actual_return==Decimal('0.02')
        assert LearningEngine().evaluate_forecast(forecast,(bar,),evaluated_at=now).event_brier is not None
        revised=CanonicalBar(instrument,forecast.target_session_date,100.,104.,99.,103.,1000,AdjustmentMode.FRONT_ADJUSTED,'fixture-revision',now)
        revision=MaturityResolver().resolve(forecast,(revised,),evaluated_at=now,previous=evidence)
        assert revision.revision==2 and revision.supersedes_evidence_id==evidence.evidence_id
        assert MaturityResolver().supersede(evidence,generated_at=now).status.value=='superseded'
    values=forecast_event_metrics(DirectionProbabilities(.6,.2,.2),ForecastDirection.BULLISH,-.02,.01,.04,.02)
    assert round(values['brier'],2)==.24 and values['interval_hit']

def test_learning_migration_14_is_idempotent(tmp_path):
    repo=SQLiteRepository(Path(tmp_path)/'learning.sqlite')
    try:
        assert repo._connection.execute('select max(version) from schema_migrations').fetchone()[0]==SCHEMA_VERSION
        assert repo._connection.execute("select name from sqlite_master where type='table' and name='learning_candidate_versions'").fetchone()[0]=='learning_candidate_versions'
    finally: repo.close()
