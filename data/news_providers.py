"""
真实新闻数据源适配器。

策略模式，按市场选择数据源，LLM 仅作条数不足时的补充：

  A 股：akshare 东方财富 stock_news_em（免费，项目已有依赖）
  美股：Finnhub company-news（需 API Key）
        Finnhub market-news（宏观新闻，与个股新闻一起做情感分析）
  API 不可用时降级使用本地历史缓存
"""

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone

from data.models import NewsItem
from data.stock_fetcher import _retry, _without_system_proxy

logger = logging.getLogger(__name__)


class BaseNewsProvider(ABC):
    """新闻数据源抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称，用于日志。"""

    @abstractmethod
    def fetch(self, code: str, name: str, market: str, limit: int) -> list[NewsItem]:
        pass


class AkshareEastMoneyProvider(BaseNewsProvider):
    """A 股东方财富个股新闻（akshare.stock_news_em）。"""

    @property
    def name(self) -> str:
        return "东方财富(akshare)"

    def fetch(self, code: str, name: str, market: str, limit: int) -> list[NewsItem]:
        if market != "A":
            return []

        def _call():
            import akshare as ak
            with _without_system_proxy():
                return ak.stock_news_em(symbol=code)

        try:
            df = _retry(_call, max_retries=2, label=self.name)
        except Exception as e:
            logger.warning(f"{self.name} 获取失败: {e}")
            return []

        if df is None or df.empty:
            return []

        items: list[NewsItem] = []
        for _, row in df.head(limit * 2).iterrows():
            title = str(row.get("新闻标题", "")).strip()
            if not title:
                continue
            pub = str(row.get("发布时间", "")).strip()
            news_date = pub[:10] if len(pub) >= 10 else date.today().isoformat()
            items.append(NewsItem(
                code=code,
                date=news_date,
                title=title,
                source=str(row.get("文章来源", "东方财富")).strip() or "东方财富",
                content=str(row.get("新闻内容", "")).strip()[:500],
                published_at=pub,
            ))
            if len(items) >= limit:
                break

        logger.info(f"{self.name}: {code} 获取 {len(items)} 条")
        return items


class FinnhubNewsProvider(BaseNewsProvider):
    """美股 Finnhub company-news（免费档 60 次/分钟，需 API Key）。"""

    @property
    def name(self) -> str:
        return "Finnhub"

    def __init__(self, api_key: str):
        self._api_key = (api_key or "").strip()

    def fetch(self, code: str, name: str, market: str, limit: int) -> list[NewsItem]:
        if market != "US" or not self._api_key:
            return []

        end = date.today()
        start = end - timedelta(days=30)
        url = "https://finnhub.io/api/v1/company-news"
        params = {
            "symbol": code.upper().split(".")[0],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": self._api_key,
        }

        def _call():
            import requests
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected response: {type(data).__name__}")
            return data

        try:
            raw = _retry(_call, max_retries=2, label=self.name)
        except Exception as e:
            logger.warning(f"{self.name} 获取失败: {e}")
            return []

        items: list[NewsItem] = []
        for entry in raw[:limit * 3]:  # 多取一些以应对相关性过滤
            item = _parse_finnhub_entry(entry, code)
            if item and _is_company_relevant(item.title, item.content, name, code):
                items.append(item)
            if len(items) >= limit:
                break

        logger.info(f"{self.name}: {code} 获取 {len(items)} 条")
        return items


def get_news_providers(market: str, news_token_us: str = "", news_token_a: str = "") -> list[BaseNewsProvider]:
    """按市场返回优先级排序的新闻源列表。"""
    if market == "A":
        providers: list[BaseNewsProvider] = [AkshareEastMoneyProvider()]
        # A 股新闻如有额外 Token（如 Tushare），在这里添加新 Provider
        return providers
    providers: list[BaseNewsProvider] = []
    if news_token_us:
        providers.append(FinnhubNewsProvider(news_token_us))
    return providers


def fetch_from_providers(
    providers: list[BaseNewsProvider],
    code: str, name: str, market: str, limit: int,
) -> list[NewsItem]:
    """依次尝试各数据源，合并去重直至凑够 limit 条。"""
    merged: list[NewsItem] = []
    seen: set[tuple[str, str]] = set()

    for provider in providers:
        if len(merged) >= limit:
            break
        try:
            batch = provider.fetch(code, name, market, limit - len(merged))
        except Exception as e:
            logger.warning(f"{provider.name} 异常: {e}")
            continue
        for item in batch:
            key = (item.date, item.title)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                break

    merged.sort(key=lambda n: n.date, reverse=True)
    return merged[:limit]


def _parse_finnhub_entry(entry: dict, code: str) -> NewsItem | None:
    title = str(entry.get("headline", "")).strip()
    if not title:
        return None

    ts = entry.get("datetime")
    if isinstance(ts, (int, float)) and ts > 0:
        news_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        published_at = datetime.fromtimestamp(ts, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        news_date = date.today().isoformat()
        published_at = ""

    return NewsItem(
        code=code,
        date=news_date,
        title=title,
        source=str(entry.get("source", "Finnhub")).strip() or "Finnhub",
        content=str(entry.get("summary", "")).strip()[:500],
        published_at=published_at,
    )


def _is_company_relevant(title: str, content: str, name: str, code: str) -> bool:
    """检查新闻是否与目标公司相关。"""
    text = (title + " " + content).lower()
    code_lower = code.lower()
    # 公司代码出现在标题或正文中
    if code_lower in text:
        return True
    # 公司名称的至少一个关键词出现在标题中（标题是强信号）
    keywords = name.lower().split()
    title_lower = title.lower()
    for kw in keywords:
        if len(kw) > 2 and kw in title_lower:
            return True
    return False


# ============================================================
# 美股宏观新闻（Finnhub general news）
# ============================================================

class FinnhubMarketNewsProvider(BaseNewsProvider):
    """美股宏观新闻 — Finnhub /news?category=general。

    获取全市场宏观新闻（利率决议、CPI、地缘政治、行业政策等），
    作为个股新闻的补充，让新闻面分析不只依赖公司层面消息。
    """

    @property
    def name(self) -> str:
        return "Finnhub宏观"

    def __init__(self, api_key: str):
        self._api_key = (api_key or "").strip()

    def fetch(self, code: str, name: str, market: str, limit: int) -> list[NewsItem]:
        if market != "US" or not self._api_key:
            return []

        url = "https://finnhub.io/api/v1/news"
        params = {
            "category": "general",
            "token": self._api_key,
        }

        def _call():
            import requests
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected response: {type(data).__name__}")
            return data

        try:
            raw = _retry(_call, max_retries=2, label=self.name)
        except Exception as e:
            logger.warning(f"{self.name} 获取失败: {e}")
            return []

        items: list[NewsItem] = []
        for entry in raw[:limit * 2]:
            title = str(entry.get("headline", "")).strip()
            if not title:
                continue
            ts = entry.get("datetime")
            if isinstance(ts, (int, float)) and ts > 0:
                news_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                published_at = datetime.fromtimestamp(ts, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            else:
                news_date = date.today().isoformat()
                published_at = ""

            items.append(NewsItem(
                code=code,
                date=news_date,
                title=title,
                source=str(entry.get("source", "Finnhub")).strip() or "Finnhub",
                content=str(entry.get("summary", "")).strip()[:500],
                is_macro=True,
                published_at=published_at,
            ))
            if len(items) >= limit:
                break

        logger.info(f"{self.name}: 获取 {len(items)} 条宏观新闻")
        return items


def fetch_macro_news(
    market: str,
    news_token_us: str = "",
    limit: int = 5,
) -> list[NewsItem]:
    """
    获取宏观新闻（仅美股启用）。

    Args:
        market: 市场 (A/US)
        news_token_us: Finnhub API Key
        limit: 最大条数

    Returns:
        NewsItem 列表（is_macro=True），A 股返回空列表
    """
    if market != "US" or not news_token_us:
        return []
    provider = FinnhubMarketNewsProvider(news_token_us)
    # 宏观新闻与具体股票无关，传空 code/name
    try:
        return provider.fetch(code="", name="", market=market, limit=limit)
    except Exception as e:
        logger.warning(f"宏观新闻获取失败: {e}")
        return []
