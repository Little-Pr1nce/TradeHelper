"""
Finnhub API 客户端 — 美股新闻、基本面、公司信息。

封装 Finnhub 免费档可用的 3 个接口：
  - /stock/profile2       → 公司基本信息（名称、行业、市值）
  - /company-news         → 公司新闻
  - /stock/metric?metric=all → 估值与基本面指标（PE/PB/EPS/Dividend/Beta 等）

免费档限速 60 次/分钟，内部通过请求间隔控制。
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta

from data.models import NewsItem
from data.stock_fetcher import _retry

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"

# 60 次/分钟 → 每秒最多 1 次，保守取 1.5s/次
_MIN_INTERVAL = 1.5
_last_request_time: float = 0.0
_rate_limit_lock = threading.Lock()


def _rate_limit():
    """简单限速：距离上一次请求至少间隔 _MIN_INTERVAL 秒。"""
    global _last_request_time
    # Tab3 会并发预取多只股票；限速窗口必须跨线程串行，否则首轮元数据
    # 补全会在同一时刻打出多次请求并触发 Finnhub 429。
    with _rate_limit_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _get(token: str, path: str, params: dict | None = None, max_retries: int = 3) -> dict:
    """带限速 + 重试的 GET 请求。"""
    import requests

    if params is None:
        params = {}
    params["token"] = token
    url = f"{BASE_URL}{path}"

    def _call():
        _rate_limit()
        resp = requests.get(url, params=params, timeout=15)
        if not resp.ok:
            logger.warning(f"Finnhub HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Finnhub API error: {data['error']}")
        return data

    return _retry(_call, max_retries=max_retries, label=f"Finnhub {path}")


# ── 公司信息 ──


def fetch_company_profile(token: str, code: str) -> dict | None:
    """
    GET /stock/profile2 → 公司基本信息。

    Returns:
        {
            "name": str, "country": str, "currency": str,
            "exchange": str, "industry": str, "marketCap": float,
            "webUrl": str, "logo": str, "ipoDate": str,
        }
    """
    try:
        data = _get(token, "/stock/profile2", {"symbol": code.upper()})
        if not data or not data.get("name"):
            return None
        return data
    except Exception as e:
        logger.warning(f"Finnhub profile2 失败 ({code}): {e}")
        return None


# ── 新闻 ──


def fetch_company_news(token: str, code: str, limit: int = 5) -> list[NewsItem]:
    """
    GET /company-news → 近期公司新闻。

    Args:
        token: Finnhub API Key
        code: 股票代码
        limit: 最多返回条数

    Returns:
        NewsItem 列表
    """
    try:
        end = date.today()
        start = end - timedelta(days=30)
        data = _get(token, "/company-news", {
            "symbol": code.upper().split(".")[0],
            "from": start.isoformat(),
            "to": end.isoformat(),
        })
        if not isinstance(data, list):
            logger.warning(f"Finnhub company-news 返回非数组: {type(data).__name__}")
            return []

        items: list[NewsItem] = []
        for entry in data[:limit * 2]:
            title = str(entry.get("headline", "")).strip()
            if not title:
                continue

            ts = entry.get("datetime")
            if isinstance(ts, (int, float)) and ts > 0:
                news_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            else:
                news_date = date.today().isoformat()

            items.append(NewsItem(
                code=code,
                date=news_date,
                title=title,
                source=str(entry.get("source", "Finnhub")).strip() or "Finnhub",
                content=str(entry.get("summary", "")).strip()[:500],
            ))
            if len(items) >= limit:
                break

        logger.info(f"Finnhub 新闻: {code} 获取 {len(items)} 条")
        return items

    except Exception as e:
        logger.warning(f"Finnhub company-news 失败 ({code}): {e}")
        return []


# ── 美股搜索 ──


def search_stock(token: str, keyword: str, limit: int = 10) -> list[dict]:
    """
    GET /search → 美股名称/代码搜索。

    Args:
        token: Finnhub API Key
        keyword: 搜索关键词
        limit: 最多返回条数

    Returns:
        [{"code": str, "name": str, "market": "US"}, ...]
    """
    try:
        data = _get(token, "/search", {"q": keyword.strip()})
        if not isinstance(data, dict):
            logger.warning(f"Finnhub /search 返回非字典: {type(data).__name__}")
            return []

        results: list[dict] = []
        for item in data.get("result", [])[:limit]:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or "." in symbol:
                continue
            name = str(item.get("description", "")).strip() or symbol
            results.append({"code": symbol, "name": name, "market": "US"})

        logger.info(f"Finnhub 搜索: '{keyword}' → {len(results)} 条")
        return results

    except Exception as e:
        logger.warning(f"Finnhub /search 失败 ('{keyword}'): {e}")
        return []


# ── 基本面 / 估值指标 ──


def fetch_basic_metrics(token: str, code: str) -> dict | None:
    """
    GET /stock/metric?metric=all → 估值与基本面指标。

    Returns:
        原始 JSON 响应（含 metric、metricType、series 三个字段）。

    metric 字段常用 key：
      - peBasicExclExtraTTM  → PE(TTM)
      - pbAnnual             → PB
      - epsBasicExclExtraTTM → EPS
      - beta                 → Beta
      - marketCapitalization → 市值
      - dividendYieldIndicatedAnnual → 股息率
      - roeRfy               → ROE（可能需要从 series 推断）
      - grossMarginTTM       → 毛利率

    series 字段包含各指标的时序快照（用于计算 PE/PB 分位）。
    """
    try:
        data = _get(token, "/stock/metric", {"symbol": code.upper(), "metric": "all"})
        if not data or not data.get("metric"):
            return None
        logger.info(f"Finnhub 基本面: {code} 获取成功")
        return data
    except Exception as e:
        logger.warning(f"Finnhub metric 失败 ({code}): {e}")
        return None


# ── 同类股票 / 竞争对手 ──


def fetch_peers(token: str, code: str) -> list[str]:
    """
    GET /stock/peers → 同类股票代码列表。

    免费档可用，60 次/分钟。

    Args:
        token: Finnhub API Key
        code:  美股代码（如 AMD）

    Returns:
        同类股票代码列表（含自身），如 ['NVDA', 'AVGO', 'AMD', ...]
    """
    try:
        data = _get(token, "/stock/peers", {"symbol": code.upper()})
        if isinstance(data, list):
            logger.info(f"Finnhub peers: {code} → {len(data)} 只同类股")
            return data
        logger.warning(f"Finnhub /stock/peers 返回非列表: {type(data).__name__}")
        return []
    except Exception as e:
        logger.warning(f"Finnhub /stock/peers 失败 ({code}): {e}")
        return []
