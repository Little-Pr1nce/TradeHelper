"""Shared news refresh, sentiment analysis, and cache orchestration."""

import logging
from collections import defaultdict

import pandas as pd

from config.settings import Settings
from data.database import Database
from data.models import NewsItem
from data.news_fetcher import fetch_news
from indicators.sentiment import analyze

logger = logging.getLogger(__name__)


def news_cache_hours(mode: str) -> float:
    """News freshness by decision horizon."""
    return {"intraday": 0.5, "pre": 1.0, "eod": 6.0}.get(mode, 6.0)


def fetch_stock_news_items(
    *,
    code: str,
    name: str,
    market: str,
    mode: str,
    db: Database | None = None,
    limit: int = 5,
    include_macro: bool = False,
) -> list[NewsItem]:
    """Independently refresh one stock's news and reuse existing labels."""
    db = db or Database()
    settings = Settings()
    items = fetch_news(
        name=name,
        code=code,
        market=market,
        news_token_us=settings.get("news_token_us", ""),
        news_token_a=settings.get("news_token_a", ""),
        limit=limit,
        include_macro=include_macro,
        cache_hours=news_cache_hours(mode),
    )

    state = db.get_news_refresh_state(code) or {}
    if state.get("status") == "empty":
        # API 失败/无结果时，历史缓存可以留在数据库供人工查看，但不能
        # 冒充本轮最新新闻进入 Alpha。
        return []

    existing = {
        (item.date, item.title): item
        for item in db.get_news(code, limit=max(limit * 10, 50))
    }
    for item in items:
        old = existing.get((item.date, item.title))
        if old and old.sentiment:
            item.sentiment = old.sentiment
            item.confidence = old.confidence
            if not item.content:
                item.content = old.content
            if not item.published_at:
                item.published_at = old.published_at
    return items


def analyze_and_store_news(
    news_by_code: dict[str, list[NewsItem]],
    *,
    db: Database | None = None,
) -> dict[str, list[NewsItem]]:
    """Analyze only unseen news in bounded batches, then persist all symbols."""
    db = db or Database()
    pending: list[NewsItem] = []
    seen: set[tuple[str, str, str]] = set()
    for items in news_by_code.values():
        for item in items:
            key = (item.code, item.date, item.title)
            if not item.sentiment and key not in seen:
                pending.append(item)
                seen.add(key)

    # Chinese translation has a 4096-token response cap; bounded batches avoid
    # one oversized translation request when Tab3 refreshes many stocks.
    for start in range(0, len(pending), 8):
        analyze(pending[start:start + 8])

    for items in news_by_code.values():
        if items:
            db.insert_news(items)
    return news_by_code


def refresh_stock_news(
    *,
    code: str,
    name: str,
    market: str,
    mode: str,
    db: Database | None = None,
    limit: int = 5,
    include_macro: bool = False,
) -> list[NewsItem]:
    """Single-stock convenience path used by Tab1."""
    db = db or Database()
    items = fetch_stock_news_items(
        code=code,
        name=name,
        market=market,
        mode=mode,
        db=db,
        limit=limit,
        include_macro=include_macro,
    )
    analyze_and_store_news({code: items}, db=db)
    return items


def news_items_to_df(items: list[NewsItem]) -> pd.DataFrame | None:
    """Build the confidence-weighted daily sentiment frame used by Alpha."""
    if not items:
        return None
    scores: dict[str, list[float]] = defaultdict(list)
    weights: dict[str, list[float]] = defaultdict(list)
    score_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
    for item in items:
        if not item.sentiment:
            continue
        confidence = item.confidence if item.confidence > 0 else 0.5
        weight = max(min(confidence, 1.0), 0.1)
        if item.is_macro:
            weight *= 0.5
        date_key = str(item.date)[:10]
        scores[date_key].append(score_map.get(item.sentiment, 0.0) * weight)
        weights[date_key].append(weight)
    rows = [
        {"date": day, "finbert_score": sum(values) / sum(weights[day])}
        for day, values in scores.items()
        if sum(weights[day]) > 0
    ]
    return pd.DataFrame(rows) if rows else None
