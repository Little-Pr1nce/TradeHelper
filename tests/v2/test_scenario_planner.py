from dataclasses import replace
from datetime import date, datetime, timezone

from tradehelper_v2.contracts import *
from tradehelper_v2.scenario import ScenarioPlanner
from tradehelper_v2.forecast.feature_sets import model_input_hash

NOW=datetime(2026,7,10,14,tzinfo=timezone.utc)

def _snapshot(instrument, mode, day, *, quote=None, cutoff=NOW, news_sentiment=0.0):
    values=(FeatureValue("closed.atr_pct_14",.02,FeatureStatus.AVAILABLE,None,14,cutoff,("fixture",),True,None),FeatureValue("news.sentiment_weighted_1d",news_sentiment,FeatureStatus.AVAILABLE,None,1,cutoff,("fixture",),True,None))
    return FeatureSnapshot(instrument,mode,cutoff,day,quote.observed_at if quote else None,"2.2.0",FeatureEvidenceMode.RECONSTRUCTED_HISTORY,values,stable_hash(("i",mode,day,quote.observed_at if quote else None,news_sentiment)),stable_hash(("f",mode,day,quote.observed_at if quote else None,news_sentiment)),cutoff)
def _forecast(instrument,horizon,direction="bullish",*,confirmed=True, input_hash="a"*64):
    origin=date(2026,7,10); target=date(2026,7,11+horizon)
    probs={"bullish":DirectionProbabilities(.7,.2,.1),"neutral":DirectionProbabilities(.2,.6,.2),"bearish":DirectionProbabilities(.1,.2,.7)}[direction]
    result={"bullish":ReturnDistribution(-.04,.03,.08,"empirical"),"neutral":ReturnDistribution(-.03,0,.03,"empirical"),"bearish":ReturnDistribution(-.08,-.03,.04,"empirical")}[direction]
    version=f"v{horizon}"; key="|".join((instrument.stable_key,origin.isoformat(),target.isoformat(),str(horizon),version,"a"*64))
    key="|".join((instrument.stable_key,origin.isoformat(),target.isoformat(),str(horizon),version,input_hash))
    margin=.4 if direction=="neutral" else .5
    return ForecastResult(instrument,NOW,origin,target,horizon,100.,ForecastAvailability.AVAILABLE,probs,result,ForecastDirection(direction),margin,ForecastScope.STOCK,instrument.stable_key,ModelFamily.ANALOG,version,ModelLifecycle.CHAMPION,ValidationStatus.CONFIRMATION_PASSED,confirmed,"tech","forecast_feature_sets_v1",input_hash,"b"*64,100,60,(),"fixture",None,key,NOW,label_flat_band=.005)
def _request(instrument, forecasts):
    origin=_snapshot(instrument,DecisionMode.EOD,date(2026,7,10)); current=_snapshot(instrument,DecisionMode.EOD,date(2026,7,10)); session=DecisionSession(instrument.market,instrument.exchange,forecasts[0].target_session_date,datetime(2026,7,12,13,30,tzinfo=timezone.utc),datetime(2026,7,12,20,tzinfo=timezone.utc),(),"fixture")
    quality=DataQualityReport(QualityStatus.OK,QualityAction.NORMAL,100.,1.,False,(),DataCapabilities(),NOW)
    forecasts=[_forecast(instrument,item.horizon,item.direction.value,confirmed=item.execution_eligible,input_hash=model_input_hash(origin,origin.latest_bar_date,"tech")) for item in forecasts]
    return ScenarioRequest(instrument,DecisionMode.EOD,NOW,origin,current,None,(),tuple(forecasts),quality,session)

def _quote(instrument, *, price=105.0, observed_at=NOW, session=TradingSession.PRE, source="nasdaq"):
    return QuoteSnapshot(instrument,session,price,100.0,None,None,None,None,None,None,observed_at,observed_at,source,FreshnessStatus.FRESH)

def _mode_request(request, mode, *, quote=None, as_of=NOW, news_sentiment=0.0, fact_updates=()):
    current=_snapshot(request.instrument,mode,request.origin_snapshot.latest_bar_date,quote=quote,cutoff=as_of,news_sentiment=news_sentiment)
    return replace(request,mode=mode,as_of=as_of,current_snapshot=current,current_quote=quote,fact_updates=fact_updates)

def test_sc01_aligned_bullish_scenario(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    assert scenario.bias is ScenarioBias.BULLISH and scenario.alignment is HorizonAlignment.ALIGNED and scenario.state is ScenarioState.BULLISH_CONTINUATION

def test_sc02_mixed_horizons_preserve_pullback(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,1,"bearish"),_forecast(us_instrument,3,"bearish"),_forecast(us_instrument,5),_forecast(us_instrument,10)]))
    assert scenario.state is ScenarioState.BULLISH_PULLBACK and scenario.entry_posture is EntryPosture.WAIT_CONFIRMATION

def test_sc03_bearish_structure_rebound_is_countertrend_only(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h,"bullish" if h in (1,3) else "bearish") for h in (1,3,5,10)]))
    assert scenario.bias is ScenarioBias.BEARISH
    assert scenario.state is ScenarioState.BEARISH_REBOUND
    assert scenario.entry_posture is EntryPosture.COUNTERTREND_CONFIRMATION
    assert StrategyFamily.TREND_CONTINUATION in scenario.blocked_strategy_families

def test_sc04_same_band_direction_conflict_is_observation_only(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,1,"bullish"),_forecast(us_instrument,3,"bearish"),_forecast(us_instrument,5),_forecast(us_instrument,10)]))
    assert scenario.tactical_signal is BandSignal.CONFLICT
    assert scenario.alignment is HorizonAlignment.CONFLICT
    assert scenario.state is ScenarioState.FORECAST_CONFLICT
    assert scenario.status is ScenarioStatus.OBSERVATION_ONLY

def test_sc05_neutral_forecasts_create_range_not_bullish(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h,"neutral") for h in (1,3,5,10)]))
    assert scenario.bias is ScenarioBias.RANGE
    assert scenario.state is ScenarioState.RANGE_BOUND
    assert StrategyFamily.RANGE_MEAN_REVERSION in scenario.allowed_strategy_families
    assert scenario.tactical_signal is BandSignal.RANGE

def test_sc05_directional_tactical_and_range_swing_stays_mixed(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h,"bullish" if h in (1,3) else "neutral") for h in (1,3,5,10)]))
    assert scenario.alignment is HorizonAlignment.MIXED
    assert scenario.bias is ScenarioBias.RANGE
    assert scenario.state is ScenarioState.MIXED

def test_sc06_probability_distribution_mismatch_and_weak_margin_are_weak(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    forecasts=list(request.forecasts)
    forecasts[0]=replace(forecasts[0],return_distribution=ReturnDistribution(-.08,-.01,.04,"empirical"))
    forecasts[1]=replace(forecasts[1],probabilities=DirectionProbabilities(.38,.32,.30),confidence_margin=.06)
    scenario=ScenarioPlanner().build(replace(request,forecasts=tuple(forecasts)))
    assert scenario.horizon_assessments[0].signal is HorizonSignal.WEAK
    assert "PROBABILITY_DISTRIBUTION_NOT_ALIGNED" in scenario.horizon_assessments[0].reason_codes
    assert scenario.horizon_assessments[1].signal is HorizonSignal.WEAK
    assert "FORECAST_MARGIN_WEAK" in scenario.horizon_assessments[1].reason_codes
