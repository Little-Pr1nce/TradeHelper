"""FeatureSnapshot 编排：按截止时点过滤事实、计算哈希并纯本地组装特征。

本模块不调用 Provider；它只消费已取得的事实，保证同一输入在任何机器上
生成相同特征快照，并明确区分观察快照与历史重建证据。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from tradehelper_v2.contracts import (
    FeatureInputs,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    FreshnessStatus,
    ContractViolation,
    stable_hash,
)
from tradehelper_v2.data.calendar import TradingCalendar

from .fundamentals import fundamental_features
from .news import news_features
from .technical import closed_features, current_features


FEATURE_SET_VERSION = "2.2.0"


def _bar_payload(bar):
    return bar.to_dict()


def _news_payload(item):
    return {
        "title": item.title, "source": item.source, "published_at": item.published_at,
        "available_at": item.available_at, "fetched_at": item.fetched_at, "content": item.content,
        "is_macro": item.is_macro, "finbert_label": item.finbert_label,
        "finbert_score": item.finbert_score, "relevance": item.relevance,
        "schema_version": item.schema_version,
    }


def _fundamental_payload(snapshot, cutoff_at: datetime):
    if snapshot is None or snapshot.available_at > cutoff_at:
        return None
    fields = {
        name: {
            "value": field.value, "unit": field.unit, "period_end": field.period_end,
            "published_at": field.published_at, "source": field.source,
        }
        for name, field in snapshot.fields.items()
        if field.published_at is None or field.published_at <= cutoff_at
    }
    return {
        "fields": fields, "available_at": snapshot.available_at, "fetched_at": snapshot.fetched_at,
        "provider": snapshot.provider, "quality_status": snapshot.quality_status.value,
        "schema_version": snapshot.schema_version,
    }


def _quality_payload(report):
    return {
        "status": report.status.value, "action": report.action.value, "score": report.score,
        "max_position_multiplier": report.max_position_multiplier, "block_new_entries": report.block_new_entries,
        "issues": tuple({"code": issue.code, "severity": issue.severity.value, "field": issue.field,
                          "message": issue.message, "source": issue.source} for issue in report.issues),
        "capabilities": report.capabilities, "evaluated_at": report.evaluated_at,
        "schema_version": report.schema_version,
    }


class FeatureBuilder:
    """在不联网且不越过特征层边界的前提下构建点时特征事实。"""

    def __init__(
        self,
        calendar: TradingCalendar,
        *,
        feature_set_version: str = FEATURE_SET_VERSION,
        observed_input_verifier: Callable[[FeatureInputs], bool] | None = None,
    ) -> None:
        self._calendar = calendar
        self._feature_set_version = feature_set_version
        self._observed_input_verifier = observed_input_verifier

    @staticmethod
    def _local_date(inputs: FeatureInputs):
        zone = ZoneInfo("Asia/Shanghai" if inputs.instrument.market.value == "A" else "America/New_York")
        return inputs.cutoff_at.astimezone(zone).date()

    def _filtered_bars(self, inputs: FeatureInputs):
        """EOD 只取已收盘 session；盘前/盘中不能偷看当日未收盘日 K。"""
        if inputs.mode.value == "eod":
            latest = self._calendar.latest_completed_session(inputs.instrument.market, inputs.cutoff_at)
        else:
            latest = self._local_date(inputs)
            # pre/intraday always use dates strictly before their local trading day.
            latest = latest - timedelta(days=1)
        return tuple(sorted((bar for bar in inputs.bars if bar.trading_date <= latest), key=lambda bar: bar.trading_date))

    @staticmethod
    def _filtered_news(inputs: FeatureInputs):
        return tuple(sorted((item for item in inputs.news if item.available_at <= inputs.cutoff_at and item.published_at <= inputs.cutoff_at),
                            key=lambda item: (item.available_at, item.published_at, item.stable_key)))

    @staticmethod
    def _quote_for_current(inputs: FeatureInputs):
        quote = inputs.quote
        if quote is None:
            return None
        if quote.freshness_status is FreshnessStatus.MISSING_TIMESTAMP:
            return quote
        age = inputs.cutoff_at - quote.observed_at
        maximum_age = timedelta(minutes=15 if inputs.mode.value == "intraday" else 45)
        if age < timedelta(minutes=-5):
            status = FreshnessStatus.FUTURE
        elif age > maximum_age:
            status = FreshnessStatus.STALE
        else:
            status = FreshnessStatus.FRESH
        return replace(quote, freshness_status=status)

    def closed_input_hash(self, inputs: FeatureInputs) -> str:
        """Fingerprint auditable closed-bar inputs without quote/current facts."""
        bars = self._filtered_bars(inputs)
        return stable_hash({
            "instrument": inputs.instrument.to_dict(), "mode": inputs.mode.value, "cutoff_at": inputs.cutoff_at,
            "bars": tuple(_bar_payload(bar) for bar in bars), "data_quality": _quality_payload(inputs.data_quality),
            "evidence_mode": inputs.evidence_mode.value,
        })

    @staticmethod
    def _context_values(inputs: FeatureInputs) -> tuple[FeatureValue, ...]:
        defaults = {
            "context.market": FeatureValue("context.market", None, FeatureStatus.MISSING, None, None, inputs.cutoff_at, (), False, "CONTEXT_INPUT_UNAVAILABLE"),
            "context.industry": FeatureValue("context.industry", None, FeatureStatus.MISSING, None, None, inputs.cutoff_at, (), False, "CONTEXT_INPUT_UNAVAILABLE"),
        }
        defaults.update(inputs.context)
        return tuple(defaults.values())

    def build(self, inputs: FeatureInputs, *, generated_at: datetime | None = None) -> FeatureSnapshot:
        """组装并哈希快照；缺失保留状态，绝不以零或中性值伪造事实。"""
        if inputs.evidence_mode.value == "observed_snapshot" and (
            self._observed_input_verifier is None or not self._observed_input_verifier(inputs)
        ):
            raise ContractViolation("OBSERVED_INPUT_EVIDENCE_UNVERIFIED")
        bars = self._filtered_bars(inputs)
        news = self._filtered_news(inputs)
        quote = self._quote_for_current(inputs)
        fundamentals = inputs.fundamentals if inputs.fundamentals is not None and inputs.fundamentals.available_at <= inputs.cutoff_at else None
        full_input = {
            "instrument": inputs.instrument.to_dict(), "mode": inputs.mode.value, "cutoff_at": inputs.cutoff_at,
            "bars": tuple(_bar_payload(bar) for bar in bars),
            "quote": quote.to_dict() if quote is not None else None,
            "news": tuple(_news_payload(item) for item in news), "news_status": inputs.news_status.value,
            "fundamentals": _fundamental_payload(fundamentals, inputs.cutoff_at),
            "fundamentals_status": inputs.fundamentals_status.value,
            "data_quality": _quality_payload(inputs.data_quality), "evidence_mode": inputs.evidence_mode.value,
            "context": {name: value.to_dict() for name, value in inputs.context.items()},
        }
        input_hash = stable_hash(full_input)
        volume_quality_degraded = any(issue.code == "ZERO_VOLUME_RATIO_HIGH" for issue in inputs.data_quality.issues)
        values = list(closed_features(bars, inputs.cutoff_at, volume_quality_degraded=volume_quality_degraded))
        values.extend(current_features(quote, tuple(values), inputs.cutoff_at, bars, volume_quality_degraded=volume_quality_degraded))
        values.extend(news_features(news, inputs.news_status, inputs.cutoff_at))
        values.extend(fundamental_features(fundamentals, inputs.fundamentals_status, inputs.cutoff_at))
        values.extend(self._context_values(inputs))
        ordered = tuple(sorted(values, key=lambda item: item.name))
        feature_hash = stable_hash({
            "input_hash": input_hash, "feature_set_version": self._feature_set_version,
            "values": tuple(item.to_dict() for item in ordered),
        })
        return FeatureSnapshot(
            instrument=inputs.instrument, mode=inputs.mode, cutoff_at=inputs.cutoff_at,
            latest_bar_date=bars[-1].trading_date if bars else None,
            quote_observed_at=quote.observed_at if quote is not None else None,
            feature_set_version=self._feature_set_version, evidence_mode=inputs.evidence_mode,
            values=ordered, input_hash=input_hash, feature_hash=feature_hash,
            generated_at=generated_at or datetime.now(timezone.utc),
        )
