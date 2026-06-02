"""
新闻获取模块。

流程：
  1. 查数据库 → 24h 内已有 >= limit 条带情感标签的新闻 → 直接用缓存
  2. 否则 → 真实新闻 API（A 股东财 / 美股 Finnhub）
  3. API 也失败 → 降级使用历史缓存
"""

import logging
from datetime import date

from data.models import NewsItem
from data.database import Database
from data.news_providers import fetch_from_providers, get_news_providers

logger = logging.getLogger(__name__)

_CACHE_HOURS = 24


def fetch_news(
    name: str, code: str, market: str,
    news_token_us: str = "",
    news_token_a: str = "",
    limit: int = 5,
) -> list[NewsItem]:
    """
    获取股票新闻（缓存优先 → 真实 API → 历史降级）。

    Args:
        name: 股票名称
        code: 股票代码
        market: 市场 (A/US)
        news_token_us: 美股新闻数据源 Token（如 Finnhub）
        news_token_a: A 股新闻数据源 Token（如 Tushare）
        limit: 最大条数

    Returns:
        NewsItem 列表（情感标签由上层 analyze() 填充后入库）
    """
    db = Database()

    # 1. 优先使用 24h 内已完成情感分析的缓存
    cached = db.get_recent_news_with_sentiment(code, hours=_CACHE_HOURS, limit=limit)
    if len(cached) >= limit:
        logger.info(f"新闻缓存命中: {len(cached)} 条 (24h 内已分析)")
        return cached[:limit]

    # 2. 真实新闻 API
    providers = get_news_providers(market, news_token_us, news_token_a)
    items = fetch_from_providers(providers, code, name, market, limit)
    if items:
        logger.info(f"真实新闻 API: {len(items)} 条")
        return items

    # 3. 全部失败 → 降级缓存
    return _fallback_cache(db, code, limit, cached)


def _fallback_cache(
    db: Database, code: str, limit: int, partial: list[NewsItem],
) -> list[NewsItem]:
    """API 不可用时，合并已有缓存与历史新闻。"""
    if partial:
        return partial[:limit]

    stale = db.get_news(code, limit=limit)
    if stale:
        logger.info(f"降级使用历史缓存: {len(stale)} 条")
    return stale
