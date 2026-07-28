"""News features with explicit provider-state and point-in-time semantics."""

from __future__ import annotations

from datetime import datetime, timedelta
import math

from contracts import FeatureStatus, FeatureValue, NewsSnapshot, ProviderStatus


_FAILURE_REASONS = {
    ProviderStatus.UNAVAILABLE: "NEWS_PROVIDER_UNAVAILABLE",
    ProviderStatus.TIMEOUT: "NEWS_PROVIDER_TIMEOUT",
    ProviderStatus.RATE_LIMITED: "NEWS_PROVIDER_RATE_LIMITED",
    ProviderStatus.INVALID_PAYLOAD: "NEWS_PROVIDER_INVALID_PAYLOAD",
}


def _feature(name: str, value: float | int | None, at: datetime, sources: tuple[str, ...], *, reason: str | None = None) -> FeatureValue:
    return FeatureValue(name, value, FeatureStatus.AVAILABLE if value is not None else FeatureStatus.MISSING,
                        "ratio" if name.startswith("news.sentiment") or name.endswith("ratio_30d") else None,
                        None, at, sources, True, reason)


def news_features(items: tuple[NewsSnapshot, ...], status: ProviderStatus, cutoff_at: datetime) -> tuple[FeatureValue, ...]:
    names = ("news.count_1d", "news.count_7d", "news.count_30d", "news.source_count_30d",
             "news.sentiment_weighted_1d", "news.sentiment_weighted_7d", "news.sentiment_change",
             "news.latest_age_hours", "news.scored_ratio_30d")
    if status is not ProviderStatus.OK and status is not ProviderStatus.EMPTY:
        reason = _FAILURE_REASONS.get(status, "NEWS_PROVIDER_UNAVAILABLE")
        return tuple(FeatureValue(name, None, FeatureStatus.MISSING, None, None, cutoff_at, (), True, reason) for name in names)
    visible = tuple(item for item in items if item.available_at <= cutoff_at and item.published_at <= cutoff_at and cutoff_at - item.published_at <= timedelta(days=30))
    sources = tuple(sorted({item.source for item in visible}))
    def in_days(days: int) -> tuple[NewsSnapshot, ...]:
        return tuple(item for item in visible if cutoff_at - item.published_at <= timedelta(days=days))
    one_day, seven_day = in_days(1), in_days(7)

    def sentiment(window: tuple[NewsSnapshot, ...]) -> float | None:
        weighted_sum = weight_total = 0.0
        for item in window:
            if item.finbert_label is None or item.finbert_score is None:
                continue
            signed = item.finbert_score if item.finbert_label == "positive" else -item.finbert_score if item.finbert_label == "negative" else 0.0
            age_hours = (cutoff_at - item.published_at).total_seconds() / 3600.0
            weight = (item.relevance if item.relevance is not None else 1.0) * math.exp(-math.log(2.0) * age_hours / 24.0)
            weighted_sum += signed * weight
            weight_total += weight
        return weighted_sum / weight_total if weight_total else None

    sentiment_1, sentiment_7 = sentiment(one_day), sentiment(seven_day)
    scored = sum(1 for item in visible if item.finbert_label is not None and item.finbert_score is not None)
    latest_age = min(((cutoff_at - item.published_at).total_seconds() / 3600.0 for item in visible), default=None)
    empty_reason = "NEWS_PROVIDER_EMPTY" if status is ProviderStatus.EMPTY else "NEWS_NO_VISIBLE_ITEMS"
    return (
        _feature("news.count_1d", len(one_day), cutoff_at, sources),
        _feature("news.count_7d", len(seven_day), cutoff_at, sources),
        _feature("news.count_30d", len(visible), cutoff_at, sources),
        _feature("news.source_count_30d", len({item.source for item in visible}), cutoff_at, sources),
        _feature("news.sentiment_weighted_1d", sentiment_1, cutoff_at, sources, reason="NEWS_NO_SCORED_ITEMS" if sentiment_1 is None else None),
        _feature("news.sentiment_weighted_7d", sentiment_7, cutoff_at, sources, reason="NEWS_NO_SCORED_ITEMS" if sentiment_7 is None else None),
        _feature("news.sentiment_change", sentiment_1 - sentiment_7 if sentiment_1 is not None and sentiment_7 is not None else None, cutoff_at, sources, reason="NEWS_NO_SCORED_ITEMS" if sentiment_1 is None or sentiment_7 is None else None),
        _feature("news.latest_age_hours", latest_age, cutoff_at, sources, reason=empty_reason if latest_age is None else None),
        _feature("news.scored_ratio_30d", scored / len(visible) if visible else None, cutoff_at, sources,
                 reason=empty_reason if not visible else None),
    )
