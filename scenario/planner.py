"""Pure deterministic translation from ForecastResult to TradingScenario."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from contracts import (
    BandSignal,
    ContractViolation,
    CurrentOverlay,
    CurrentPriceState,
    DecisionMode,
    EntryPosture,
    ExitPosture,
    ForecastEvidenceGrade,
    ForecastResult,
    ForecastSupportLevel,
    HorizonAlignment,
    HorizonAssessment,
    HorizonSignal,
    Market,
    NewsDeltaState,
    PriceLocation,
    ReturnDistribution,
    ScenarioBias,
    ScenarioFactKind,
    ScenarioRequest,
    ScenarioState,
    ScenarioStatus,
    StrategyFamily,
    TradingScenario,
    TradingSession,
    VolatilityShock,
    stable_hash,
)
from contracts.scenario import SCENARIO_REASON_CODES

from . import policy


def _codes(*codes: str) -> tuple[str, ...]:
    unknown = set(codes) - SCENARIO_REASON_CODES
    if unknown:
        raise ContractViolation(f"unregistered scenario reason codes: {sorted(unknown)}")
    return tuple(sorted(set(codes)))


def _feature(snapshot, name):
    return next((item for item in snapshot.values if item.name == name), None)


def _forecast_business_payload(result: ForecastResult) -> dict:
    payload = asdict(result)
    payload.pop("generated_at", None)
    return payload


class ScenarioPlanner:
    """Translate frozen facts without network access or forecast mutation."""

    def build(
        self,
        request: ScenarioRequest,
        *,
        generated_at: datetime | None = None,
    ) -> TradingScenario:
        price, price_state, source, observed_at = self._current_price(request)
        assessments = tuple(self._assessment(item, price) for item in request.forecasts)
        tactical = self._band(assessments[:2])
        swing = self._band(assessments[2:])
        alignment = self._alignment(tactical, swing)
        bias, state = self._state(tactical, swing, alignment)
        overlay = self._overlay(request, price, price_state, source, observed_at, assessments)
        support = self._support(assessments, alignment)
        status = self._status(request, support, alignment, overlay)
        allowed, entry, exit_posture = self._families(state, status, overlay, bias)
        blocked = tuple(item for item in StrategyFamily if item not in allowed)
        valid_from, expires_at = self._window(request)
        forecast_bundle_hash = stable_hash(
            tuple(
                (item.event_key, _forecast_business_payload(item))
                for item in request.forecasts
            )
        )
        fact_update_hash = stable_hash(request.fact_updates)
        quality_hash = stable_hash(request.data_quality)
        identity = {
            "instrument": request.instrument,
            "mode": request.mode.value,
            "as_of": request.as_of,
            "origin_session_date": request.origin_snapshot.latest_bar_date,
            "decision_session": request.decision_session,
            "valid_from": valid_from,
            "expires_at": expires_at,
            "forecast_bundle_hash": forecast_bundle_hash,
            "current_feature_hash": request.current_snapshot.feature_hash,
            "fact_update_hash": fact_update_hash,
            "quality_hash": quality_hash,
            "policy_version": request.policy_version,
        }
        scenario_id = stable_hash(identity)
        session_key = request.decision_session.session_date.isoformat() if request.decision_session else "calendar-unavailable"
        event_key = "|".join((request.instrument.stable_key, request.mode.value, session_key, scenario_id))
        reasons: list[str] = []
        for item in assessments:
            reasons.extend(item.reason_codes)
        reasons.extend(overlay.reason_codes)
        if alignment is HorizonAlignment.ALIGNED:
            reasons.append("TACTICAL_SWING_ALIGNED")
        elif alignment is HorizonAlignment.MIXED:
            reasons.append("TACTICAL_SWING_MIXED")
        elif alignment is HorizonAlignment.PARTIAL:
            reasons.append("HORIZON_COVERAGE_PARTIAL")
        elif alignment is HorizonAlignment.CONFLICT:
            reasons.append("HORIZON_CONFLICT")
        if request.data_quality.status.value == "blocked" or request.data_quality.block_new_entries:
            reasons.append("DATA_QUALITY_BLOCKED")
        elif request.data_quality.status.value == "degraded":
            reasons.append("DATA_QUALITY_DEGRADED")
        if entry is EntryPosture.WAIT_PULLBACK:
            reasons.append("ENTRY_WAIT_PULLBACK")
        elif entry is EntryPosture.WAIT_CONFIRMATION:
            reasons.append("ENTRY_WAIT_CONFIRMATION")
        elif entry is EntryPosture.OBSERVATION_ONLY:
            reasons.append("ENTRY_OBSERVATION_ONLY")
        elif entry is EntryPosture.BLOCKED:
            reasons.append("ENTRY_BLOCKED")
        reasons.append("PROTECTIVE_EXIT_PRESERVED")
        return TradingScenario(
            scenario_id, event_key, request.instrument, request.mode, request.as_of,
            request.origin_snapshot.latest_bar_date, request.decision_session, valid_from,
            expires_at, bias, tactical, swing, alignment, state, support, status,
            assessments, overlay, allowed, blocked, entry, exit_posture, _codes(*reasons),
            forecast_bundle_hash, request.current_snapshot.feature_hash, fact_update_hash,
            quality_hash, request.policy_version, generated_at or datetime.now(timezone.utc),
        )

    def _assessment(self, forecast: ForecastResult, price: float | None) -> HorizonAssessment:
        if forecast.availability.value != "available":
            code = {
                "insufficient_sample": "FORECAST_INSUFFICIENT_SAMPLE",
                "data_blocked": "FORECAST_DATA_BLOCKED",
                "calendar_unavailable": "FORECAST_CALENDAR_UNAVAILABLE",
                "no_eligible_model": "FORECAST_NO_ELIGIBLE_MODEL",
            }[forecast.availability.value]
            return HorizonAssessment(
                forecast.horizon, forecast.target_session_date, forecast.event_key,
                ForecastEvidenceGrade.UNAVAILABLE, HorizonSignal.UNAVAILABLE, None, None,
                None, None, PriceLocation.UNAVAILABLE, _codes(code),
            )
        if forecast.model_family.value == "empirical" or forecast.model_scope.value == "baseline":
            grade = ForecastEvidenceGrade.BASELINE_OBSERVATION
        elif forecast.model_scope.value == "stock" and forecast.execution_eligible:
            grade = ForecastEvidenceGrade.STOCK_CONFIRMED
        elif forecast.model_scope.value == "stock":
            grade = ForecastEvidenceGrade.STOCK_OBSERVATION
        else:
            grade = ForecastEvidenceGrade.CROSS_STOCK_OBSERVATION
        grade_reason = {
            ForecastEvidenceGrade.BASELINE_OBSERVATION: "FORECAST_BASELINE_OBSERVATION",
            ForecastEvidenceGrade.STOCK_CONFIRMED: "FORECAST_STOCK_CONFIRMED",
            ForecastEvidenceGrade.STOCK_OBSERVATION: "FORECAST_STOCK_OBSERVATION",
            ForecastEvidenceGrade.CROSS_STOCK_OBSERVATION: "FORECAST_CROSS_STOCK_OBSERVATION",
        }[grade]
        if forecast.validation_status.value == "noninferior_passed":
            grade_reason = "FORECAST_STOCK_NONINFERIOR"
        signal = HorizonSignal.WEAK
        extra_reason = "FORECAST_MARGIN_WEAK"
        if forecast.direction.value == "bullish" and forecast.confidence_margin >= policy.DIRECTIONAL_MARGIN and forecast.return_distribution.p50 > 0:
            signal = HorizonSignal.BULLISH
            extra_reason = None
        elif forecast.direction.value == "bearish" and forecast.confidence_margin >= policy.DIRECTIONAL_MARGIN and forecast.return_distribution.p50 < 0:
            signal = HorizonSignal.BEARISH
            extra_reason = None
        elif forecast.direction.value == "neutral" and forecast.confidence_margin >= policy.RANGE_MARGIN and forecast.return_distribution.p10 <= 0 <= forecast.return_distribution.p90:
            signal = HorizonSignal.RANGE
            extra_reason = None
        elif forecast.confidence_margin >= policy.DIRECTIONAL_MARGIN:
            extra_reason = "PROBABILITY_DISTRIBUTION_NOT_ALIGNED"
        realized = None if price is None else price / forecast.reference_price - 1
        if realized is None:
            location = PriceLocation.UNAVAILABLE
        elif realized < forecast.return_distribution.p10:
            location = PriceLocation.BELOW_P10
        elif realized > forecast.return_distribution.p90:
            location = PriceLocation.ABOVE_P90
        else:
            location = PriceLocation.INSIDE_INTERVAL
        remaining = None
        if realized is not None:
            remaining = ReturnDistribution(
                *((1 + value) / (1 + realized) - 1 for value in (
                    forecast.return_distribution.p10,
                    forecast.return_distribution.p50,
                    forecast.return_distribution.p90,
                )),
                forecast.return_distribution.method,
            )
        reasons = [grade_reason]
        if extra_reason:
            reasons.append(extra_reason)
        if location is PriceLocation.ABOVE_P90:
            reasons.append("PRICE_ABOVE_P90")
        elif location is PriceLocation.BELOW_P10:
            reasons.append("PRICE_BELOW_P10")
        return HorizonAssessment(
            forecast.horizon, forecast.target_session_date, forecast.event_key, grade,
            signal, forecast.probabilities, forecast.return_distribution, remaining,
            forecast.confidence_margin, location, _codes(*reasons),
        )

    @staticmethod
    def _band(items: tuple[HorizonAssessment, ...]) -> BandSignal:
        values = {item.signal for item in items}
        if HorizonSignal.BULLISH in values and HorizonSignal.BEARISH in values:
            return BandSignal.CONFLICT
        if HorizonSignal.BULLISH in values:
            return BandSignal.BULLISH
        if HorizonSignal.BEARISH in values:
            return BandSignal.BEARISH
        if HorizonSignal.RANGE in values:
            return BandSignal.RANGE
        return BandSignal.UNAVAILABLE if values == {HorizonSignal.UNAVAILABLE} else BandSignal.UNCERTAIN

    @staticmethod
    def _alignment(tactical: BandSignal, swing: BandSignal) -> HorizonAlignment:
        unavailable = {BandSignal.UNCERTAIN, BandSignal.UNAVAILABLE}
        if BandSignal.CONFLICT in (tactical, swing):
            return HorizonAlignment.CONFLICT
        if tactical == swing and tactical not in unavailable:
            return HorizonAlignment.ALIGNED
        if tactical not in unavailable and swing not in unavailable:
            return HorizonAlignment.MIXED
        if tactical in unavailable and swing in unavailable:
            return HorizonAlignment.UNAVAILABLE
        return HorizonAlignment.PARTIAL

    @staticmethod
    def _state(tactical: BandSignal, swing: BandSignal, alignment: HorizonAlignment):
        if alignment is HorizonAlignment.CONFLICT:
            return ScenarioBias.UNCERTAIN, ScenarioState.FORECAST_CONFLICT
        if tactical is BandSignal.BULLISH and swing is BandSignal.BULLISH:
            return ScenarioBias.BULLISH, ScenarioState.BULLISH_CONTINUATION
        if tactical is BandSignal.BEARISH and swing is BandSignal.BULLISH:
            return ScenarioBias.BULLISH, ScenarioState.BULLISH_PULLBACK
        if tactical is BandSignal.BEARISH and swing is BandSignal.BEARISH:
            return ScenarioBias.BEARISH, ScenarioState.BEARISH_CONTINUATION
        if tactical is BandSignal.BULLISH and swing is BandSignal.BEARISH:
            return ScenarioBias.BEARISH, ScenarioState.BEARISH_REBOUND
        if tactical is BandSignal.RANGE and swing in {BandSignal.RANGE, BandSignal.UNCERTAIN, BandSignal.UNAVAILABLE}:
            return ScenarioBias.RANGE, ScenarioState.RANGE_BOUND
        if swing is BandSignal.RANGE:
            return ScenarioBias.RANGE, ScenarioState.MIXED
        if swing in {BandSignal.BULLISH, BandSignal.BEARISH}:
            return ScenarioBias(swing.value), ScenarioState.MIXED
        if tactical in {BandSignal.BULLISH, BandSignal.BEARISH}:
            return ScenarioBias(tactical.value), ScenarioState.MIXED
        if tactical is BandSignal.RANGE:
            return ScenarioBias.RANGE, ScenarioState.RANGE_BOUND
        return ScenarioBias.UNCERTAIN, ScenarioState.UNCERTAIN

    @staticmethod
    def _current_price(request: ScenarioRequest):
        if request.mode is DecisionMode.EOD:
            return request.forecasts[0].reference_price, CurrentPriceState.REFERENCE_CLOSE, "reference_close", None
        quote = request.current_quote
        if quote is None:
            state = CurrentPriceState.EXPECTED_MISSING if request.mode is DecisionMode.PRE and request.instrument.market is Market.A else CurrentPriceState.STALE_OR_MISSING
            return None, state, None, None
        expected_session = TradingSession.PRE if request.mode is DecisionMode.PRE else TradingSession.REGULAR
        source = quote.source.strip().lower()
        if request.mode is DecisionMode.PRE:
            accepted_sources = policy.US_PRE_QUOTE_SOURCES if request.instrument.market is Market.US else frozenset()
            maximum = timedelta(minutes=policy.PRE_QUOTE_MAX_AGE_MINUTES)
        else:
            accepted_sources = policy.INTRADAY_QUOTE_SOURCES
            maximum = timedelta(minutes=policy.INTRADAY_QUOTE_MAX_AGE_MINUTES)
        age = request.as_of - quote.observed_at
        if quote.session is not expected_session or source not in accepted_sources or age > maximum or age < -timedelta(minutes=policy.QUOTE_FUTURE_TOLERANCE_MINUTES):
            return None, CurrentPriceState.STALE_OR_MISSING, None, None
        return quote.price, CurrentPriceState.FRESH_QUOTE, source, quote.observed_at

    def _overlay(self, request, price, state, source, observed_at, assessments):
        reference = request.forecasts[0].reference_price
        realized = None if price is None else price / reference - 1
        atr = _feature(request.origin_snapshot, "closed.atr_pct_14")
        threshold = max(policy.DEFAULT_SHOCK_FLOOR, policy.ATR_SHOCK_MULTIPLIER * float(atr.value)) if atr and atr.status.value == "available" else policy.DEFAULT_SHOCK_FLOOR
        shock = VolatilityShock.UNAVAILABLE if realized is None else VolatilityShock.BULLISH if realized >= threshold else VolatilityShock.BEARISH if realized <= -threshold else VolatilityShock.NONE
        old = _feature(request.origin_snapshot, "news.sentiment_weighted_1d")
        new = _feature(request.current_snapshot, "news.sentiment_weighted_1d")
        news_delta = NewsDeltaState.UNAVAILABLE if not old or not new or old.status.value != "available" or new.status.value != "available" else NewsDeltaState.POSITIVE if new.value - old.value >= policy.NEWS_DELTA_THRESHOLD else NewsDeltaState.NEGATIVE if new.value - old.value <= -policy.NEWS_DELTA_THRESHOLD else NewsDeltaState.UNCHANGED
        news_update = any(item.kind is ScenarioFactKind.NEWS for item in request.fact_updates)
        fundamental_update = any(item.kind is ScenarioFactKind.FUNDAMENTAL for item in request.fact_updates)
        reasons = [{
            CurrentPriceState.REFERENCE_CLOSE: "CURRENT_PRICE_REFERENCE_CLOSE",
            CurrentPriceState.FRESH_QUOTE: "CURRENT_PRICE_FRESH_QUOTE",
            CurrentPriceState.EXPECTED_MISSING: "CURRENT_PRICE_EXPECTED_MISSING",
            CurrentPriceState.STALE_OR_MISSING: "CURRENT_PRICE_STALE_OR_MISSING",
        }[state]]
        if shock is VolatilityShock.BULLISH:
            reasons.append("BULLISH_VOLATILITY_SHOCK")
        elif shock is VolatilityShock.BEARISH:
            reasons.append("BEARISH_VOLATILITY_SHOCK")
        if news_delta is NewsDeltaState.POSITIVE:
            reasons.append("NEWS_DELTA_POSITIVE")
        elif news_delta is NewsDeltaState.NEGATIVE:
            reasons.append("NEWS_DELTA_NEGATIVE")
        elif news_delta is NewsDeltaState.UNAVAILABLE:
            reasons.append("NEWS_DELTA_UNAVAILABLE")
        if news_update:
            reasons.append("NEWS_UPDATE_PRESENT")
        if fundamental_update:
            reasons.append("FUNDAMENTAL_UPDATE_PRESENT")
        if news_update or fundamental_update:
            reasons.append("UNMODELED_FACT_UPDATE")
        tactical_location = assessments[0].price_location
        if tactical_location is PriceLocation.UNAVAILABLE:
            tactical_location = assessments[1].price_location
        return CurrentOverlay(
            state, price, source, observed_at, realized, tactical_location, shock, news_delta,
            news_update, fundamental_update, len(request.fact_updates),
            news_update or fundamental_update, _codes(*reasons),
        )

    @staticmethod
    def _support(assessments, alignment):
        confirmed = [item for item in assessments if item.evidence_grade is ForecastEvidenceGrade.STOCK_CONFIRMED]
        if len(confirmed) >= 2 and any(item.horizon in (1, 3) for item in confirmed) and any(item.horizon in (5, 10) for item in confirmed) and alignment not in {HorizonAlignment.CONFLICT, HorizonAlignment.UNAVAILABLE}:
            return ForecastSupportLevel.CONFIRMED
        if confirmed:
            return ForecastSupportLevel.PARTIAL
        if any(item.evidence_grade is not ForecastEvidenceGrade.UNAVAILABLE for item in assessments):
            return ForecastSupportLevel.OBSERVATIONAL
        return ForecastSupportLevel.UNAVAILABLE

    @staticmethod
    def _status(request, support, alignment, overlay):
        if request.decision_session is None or request.data_quality.status.value == "blocked" or request.data_quality.block_new_entries:
            return ScenarioStatus.BLOCKED
        if request.mode is DecisionMode.INTRADAY and overlay.price_state is not CurrentPriceState.FRESH_QUOTE:
            return ScenarioStatus.BLOCKED
        if alignment is HorizonAlignment.CONFLICT or support in {ForecastSupportLevel.OBSERVATIONAL, ForecastSupportLevel.UNAVAILABLE}:
            return ScenarioStatus.OBSERVATION_ONLY
        if support is ForecastSupportLevel.PARTIAL or overlay.unmodeled_fact_update or overlay.tactical_price_location in {PriceLocation.BELOW_P10, PriceLocation.ABOVE_P90} or request.data_quality.status.value == "degraded" or (request.mode is DecisionMode.PRE and overlay.price_state is CurrentPriceState.STALE_OR_MISSING):
            return ScenarioStatus.DEGRADED
        return ScenarioStatus.READY

    @staticmethod
    def _families(state, status, overlay, bias):
        mapping = {
            ScenarioState.BULLISH_CONTINUATION: ({StrategyFamily.TREND_CONTINUATION, StrategyFamily.BREAKOUT_CONFIRMATION, StrategyFamily.PULLBACK_ENTRY, StrategyFamily.SUPPORT_REBOUND}, EntryPosture.FOLLOW_TREND, ExitPosture.STANDARD),
            ScenarioState.BULLISH_PULLBACK: ({StrategyFamily.PULLBACK_ENTRY, StrategyFamily.SUPPORT_REBOUND}, EntryPosture.WAIT_CONFIRMATION, ExitPosture.TIGHTEN_PROTECTION),
            ScenarioState.BEARISH_CONTINUATION: (set(), EntryPosture.BLOCKED, ExitPosture.PRIORITIZE_PROTECTION),
            ScenarioState.BEARISH_REBOUND: ({StrategyFamily.SUPPORT_REBOUND}, EntryPosture.COUNTERTREND_CONFIRMATION, ExitPosture.PRIORITIZE_PROTECTION),
            ScenarioState.RANGE_BOUND: ({StrategyFamily.RANGE_MEAN_REVERSION, StrategyFamily.SUPPORT_REBOUND, StrategyFamily.BREAKOUT_CONFIRMATION}, EntryPosture.RANGE_EXTREMES_ONLY, ExitPosture.STANDARD),
            ScenarioState.MIXED: ({StrategyFamily.PULLBACK_ENTRY, StrategyFamily.SUPPORT_REBOUND}, EntryPosture.WAIT_CONFIRMATION, ExitPosture.TIGHTEN_PROTECTION),
            ScenarioState.FORECAST_CONFLICT: (set(), EntryPosture.OBSERVATION_ONLY, ExitPosture.TIGHTEN_PROTECTION),
            ScenarioState.UNCERTAIN: (set(), EntryPosture.OBSERVATION_ONLY, ExitPosture.TIGHTEN_PROTECTION),
        }
        families, entry, exit_posture = mapping[state]
        families = set(families)
        base_blocks_entry = entry in {EntryPosture.BLOCKED, EntryPosture.OBSERVATION_ONLY}
        if overlay.tactical_price_location is PriceLocation.ABOVE_P90:
            families -= {StrategyFamily.TREND_CONTINUATION, StrategyFamily.BREAKOUT_CONFIRMATION}
            if not base_blocks_entry and bias is not ScenarioBias.BEARISH:
                entry = EntryPosture.WAIT_PULLBACK
        if bias is ScenarioBias.BULLISH and (overlay.tactical_price_location is PriceLocation.BELOW_P10 or overlay.volatility_shock is VolatilityShock.BEARISH):
            families -= {StrategyFamily.TREND_CONTINUATION, StrategyFamily.BREAKOUT_CONFIRMATION, StrategyFamily.PULLBACK_ENTRY, StrategyFamily.SUPPORT_REBOUND}
            if not base_blocks_entry:
                entry = EntryPosture.WAIT_CONFIRMATION
            exit_posture = ExitPosture.TIGHTEN_PROTECTION
        if bias is ScenarioBias.BEARISH and (overlay.tactical_price_location is PriceLocation.ABOVE_P90 or overlay.volatility_shock is VolatilityShock.BULLISH):
            families &= {StrategyFamily.SUPPORT_REBOUND}
            if not base_blocks_entry:
                entry = EntryPosture.COUNTERTREND_CONFIRMATION
        if (overlay.unmodeled_fact_update or overlay.price_state in {CurrentPriceState.EXPECTED_MISSING, CurrentPriceState.STALE_OR_MISSING}) and not base_blocks_entry:
            entry = EntryPosture.WAIT_CONFIRMATION
        if status is ScenarioStatus.OBSERVATION_ONLY:
            # OOF quality gates execution, not the user's right to see concrete
            # technical conditions. Keep non-conflicting families as C-level
            # observation candidates; StrategyEngine will force them to
            # OBSERVATION_ONLY and RiskOfficer will never emit an order.
            if state is ScenarioState.FORECAST_CONFLICT:
                families = set()
            elif state is ScenarioState.UNCERTAIN:
                families = {
                    StrategyFamily.BREAKOUT_CONFIRMATION,
                    StrategyFamily.PULLBACK_ENTRY,
                    StrategyFamily.SUPPORT_REBOUND,
                    StrategyFamily.RANGE_MEAN_REVERSION,
                }
            entry = EntryPosture.OBSERVATION_ONLY
        elif status is ScenarioStatus.BLOCKED:
            families = set()
            entry = EntryPosture.BLOCKED
        families |= {StrategyFamily.PROTECTIVE_EXIT, StrategyFamily.PROFIT_LOCK, StrategyFamily.FAILED_REBOUND_EXIT, StrategyFamily.OBSERVATION}
        return tuple(item for item in StrategyFamily if item in families), entry, exit_posture

    @staticmethod
    def _window(request):
        if request.decision_session is None:
            return None, None
        valid_from = request.decision_session.regular_open
        if request.mode is DecisionMode.INTRADAY:
            valid_from = max(request.as_of, valid_from)
            for left, right in request.decision_session.breaks:
                if left <= valid_from < right:
                    valid_from = right
        return valid_from, request.decision_session.regular_close
