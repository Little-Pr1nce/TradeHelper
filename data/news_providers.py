"""
真实新闻数据源适配器。

策略模式，按市场选择数据源，LLM 仅作条数不足时的补充：

  A 股：akshare 东方财富 stock_news_em（免费，项目已有依赖）
  美股：Finnhub company-news（可选，需 API Key）
        → yfinance Ticker.news（免费，易限流，需代理）
  兜底：LLM 生成（news_fetcher 层调用）
"""

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

from data.models import NewsItem
from data.stock_fetcher import _apply_proxy, _retry, _without_system_proxy

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
            ))
            if len(items) >= limit:
                break

        logger.info(f"{self.name}: {code} 获取 {len(items)} 条")
        return items


class YfinanceNewsProvider(BaseNewsProvider):
    """美股 yfinance Ticker.news。"""

    @property
    def name(self) -> str:
        return "yfinance"

    def fetch(self, code: str, name: str, market: str, limit: int) -> list[NewsItem]:
        if market != "US":
            return []

        _apply_proxy()

        def _call():
            import yfinance as yf
            ticker = yf.Ticker(code.upper())
            return ticker.news or []

        try:
            raw = _retry(_call, max_retries=3, label=self.name)
        except Exception as e:
            logger.warning(f"{self.name} 获取失败: {e}")
            return []

        items: list[NewsItem] = []
        for entry in raw[:limit * 2]:
            item = _parse_yfinance_entry(entry, code)
            if item:
                items.append(item)
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
        for entry in raw[:limit * 2]:
            item = _parse_finnhub_entry(entry, code)
            if item:
                items.append(item)
            if len(items) >= limit:
                break

        logger.info(f"{self.name}: {code} 获取 {len(items)} 条")
        return items


def get_news_providers(market: str, finnhub_api_key: str = "") -> list[BaseNewsProvider]:
    """按市场返回优先级排序的新闻源列表。"""
    if market == "A":
        return [AkshareEastMoneyProvider()]
    providers: list[BaseNewsProvider] = []
    if finnhub_api_key:
        providers.append(FinnhubNewsProvider(finnhub_api_key))
    providers.append(YfinanceNewsProvider())
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


def _parse_yfinance_entry(entry: dict, code: str) -> NewsItem | None:
    title = str(entry.get("title", "")).strip()
    if not title:
        return None

    ts = entry.get("providerPublishTime") or entry.get("publishedAt")
    if isinstance(ts, (int, float)) and ts > 0:
        news_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    else:
        news_date = date.today().isoformat()

    source = str(
        entry.get("publisher") or entry.get("publisherName") or "Yahoo Finance"
    ).strip()
    summary = str(entry.get("summary", "")).strip()[:500]

    return NewsItem(code=code, date=news_date, title=title, source=source, content=summary)


def _parse_finnhub_entry(entry: dict, code: str) -> NewsItem | None:
    title = str(entry.get("headline", "")).strip()
    if not title:
        return None

    ts = entry.get("datetime")
    if isinstance(ts, (int, float)) and ts > 0:
        news_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    else:
        news_date = date.today().isoformat()

    return NewsItem(
        code=code,
        date=news_date,
        title=title,
        source=str(entry.get("source", "Finnhub")).strip() or "Finnhub",
        content=str(entry.get("summary", "")).strip()[:500],
    )
