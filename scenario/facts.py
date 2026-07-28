"""Point-in-time fact deltas issued after the forecast origin."""

from __future__ import annotations

from contracts import (
    ProviderStatus,
    ScenarioFactKind,
    ScenarioFactUpdate,
    stable_hash,
)
from features.news import news_features


def _changed_news_features(before, after) -> tuple[str, ...]:
    previous = {
        item.name: (item.value, item.status, item.sources, item.reason)
        for item in before
    }
    return tuple(
        sorted(
            item.name
            for item in after
            if previous.get(item.name) != (item.value, item.status, item.sources, item.reason)
        )
    )


def build_fact_updates(
    origin_cutoff,
    as_of,
    visible_news,
    origin_fundamentals,
    current_fundamentals,
):
    updates = []
    ordered_news = tuple(
        sorted(visible_news, key=lambda item: (item.available_at, item.published_at, item.stable_key))
    )
    known_news = [item for item in ordered_news if item.available_at <= origin_cutoff]
    for item in ordered_news:
        if not (origin_cutoff < item.available_at <= as_of):
            continue
        before = news_features(tuple(known_news), ProviderStatus.OK, as_of)
        after = news_features(tuple((*known_news, item)), ProviderStatus.OK, as_of)
        affected = _changed_news_features(before, after)
        known_news.append(item)
        if not affected:
            continue
        updates.append(
            ScenarioFactUpdate(
                ScenarioFactKind.NEWS,
                item.stable_key,
                item.available_at,
                item.source,
                stable_hash(item),
                affected,
            )
        )
    old = {} if origin_fundamentals is None else origin_fundamentals.fields
    if (
        current_fundamentals
        and origin_cutoff < current_fundamentals.available_at <= as_of
    ):
        for name, value in current_fundamentals.fields.items():
            if name in {
                "pe_ttm",
                "pb_mrq",
                "ps_ttm",
                "roe",
                "gross_margin",
                "revenue_growth_yoy",
                "net_profit_growth_yoy",
                "debt_ratio",
            } and old.get(name) != value:
                key = (
                    f"{current_fundamentals.provider}|{name}|"
                    f"{value.period_end}|{value.published_at}"
                )
                updates.append(
                    ScenarioFactUpdate(
                        ScenarioFactKind.FUNDAMENTAL,
                        key,
                        current_fundamentals.available_at,
                        current_fundamentals.provider,
                        stable_hash(value),
                        (f"fund.{name}",),
                    )
                )
    return tuple(
        sorted(updates, key=lambda item: (item.available_at, item.kind.value, item.stable_key))
    )
