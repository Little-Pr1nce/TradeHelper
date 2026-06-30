"""
新闻获取模块。

流程：
  1. 查数据库 → 24h 内已有 >= limit 条带情感标签的新闻 → 直接用缓存
  2. 否则 → 真实新闻 API（A 股东财 / 美股 Finnhub）
  3. API 也失败 → 降级使用历史缓存
"""

import logging
from datetime import datetime, timedelta, timezone

from data.models import NewsItem
from data.database import Database
from data.news_providers import fetch_from_providers, get_news_providers

logger = logging.getLogger(__name__)

_CACHE_HOURS = 24


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh_state_is_fresh(state: dict | None, cache_hours: float) -> bool:
    if not state or cache_hours <= 0:
        return False
    value = str(state.get("last_attempt_at") or "")
    try:
        attempted = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - attempted <= timedelta(hours=cache_hours)


def fetch_news(
    name: str, code: str, market: str,
    news_token_us: str = "",
    news_token_a: str = "",
    limit: int = 5,
    include_macro: bool = False,
    cache_hours: float = _CACHE_HOURS,
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
        include_macro: 是否同时获取宏观新闻（仅美股启用）

    Returns:
        NewsItem 列表（情感标签由上层 analyze() 填充后入库）
    """
    db = Database()

    # 1. 刷新状态与新闻条数分离。数据源只返回 0~2 条时，也应认为本轮
    # 已刷新，不能因为未凑满 limit 而在每个页面重复请求。
    cached = db.get_recent_news_with_sentiment(
        code, hours=max(int(cache_hours + 1), 1), limit=limit
    )
    refresh_state = db.get_news_refresh_state(code)
    if _refresh_state_is_fresh(refresh_state, cache_hours):
        logger.info(
            f"新闻刷新状态命中: {code}，{cache_hours:g}h 内已检查，"
            f"可用缓存 {len(cached)} 条"
        )
        return cached[:limit] if cached else _fallback_cache(db, code, limit, [])

    # 2. 真实新闻 API
    providers = get_news_providers(market, news_token_us, news_token_a)
    items = fetch_from_providers(providers, code, name, market, limit)

    # 2.5 宏观新闻（仅美股启用）
    macro_items: list[NewsItem] = []
    if include_macro and market == "US":
        from data.news_providers import fetch_macro_news
        macro_items = fetch_macro_news(market, news_token_us, limit=3)
        if macro_items:
            for item in macro_items:
                item.code = code
            logger.info(f"宏观新闻: {len(macro_items)} 条")

    if items or macro_items:
        result = items[:limit] if items else macro_items
        # 有个股新闻时，宏观新闻插入前几条之间做交叉
        if items and macro_items:
            # 交替排列：前3条个股 + 2条宏观 + 剩余个股
            merged = items[:3]
            merged.extend(macro_items[:2])
            merged.extend(items[3:])
            result = merged[:limit + 2]  # 稍微多取，LLM 会自行筛选
        fetched_at = _utc_now()
        for item in result:
            item.fetched_at = fetched_at
            if not item.published_at:
                item.published_at = item.date
        db.save_news_refresh_state(
            code, market, attempted_at=fetched_at,
            status="success", item_count=len(result),
        )
        logger.info(f"新闻总数（含宏观）: {len(result)} 条")
        return result

    # 3. 全部失败 → 降级缓存
    attempted_at = _utc_now()
    db.save_news_refresh_state(
        code, market, attempted_at=attempted_at,
        status="empty", item_count=0,
    )
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
