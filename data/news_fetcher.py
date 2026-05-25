"""
金融新闻获取模块

根据股票所属市场自动选择新闻数据源：
  - A 股：通过 akshare.stock_news_em() 获取东方财富个股新闻
  - 美股：通过 yfinance.Ticker.news 获取 Yahoo Finance 新闻

返回统一的 NewsItem 列表，供情感分析模块 (analysis/sentiment.py) 使用。

【扩展点】添加新新闻源：
  1. 在 fetch_news() 中为新市场添加分支
  2. 实现对应的 _fetch_news_xxx() 函数
  3. 可用的第三方新闻源：NewsAPI、Finnhub、新浪财经等

数据格式统一：所有源返回的 NewsItem 都包含 (title, date, source) 三个核心字段。
"""

import logging
from datetime import datetime, date

from data.models import NewsItem
from data.database import Database

logger = logging.getLogger(__name__)


def fetch_news(code: str, market: str, limit: int = 15) -> list[NewsItem]:
    """
    获取指定股票的最新新闻。

    根据市场自动选择数据源，返回统一格式的 NewsItem 列表。

    Args:
        code: 股票代码
        market: 市场标识 ("A" / "US")
        limit: 最大返回条数（默认 15 条）

    Returns:
        NewsItem 列表（尚未填充 sentiment 和 confidence 字段，
        需调用 analysis/sentiment.py 的 analyze() 完成情感分析）
    """
    if market == "A":
        return _fetch_news_a(code, limit)
    elif market == "US":
        return _fetch_news_us(code, limit)
    return []


def _fetch_news_a(code: str, limit: int = 15) -> list[NewsItem]:
    """
    通过 akshare 获取 A 股个股新闻。

    数据来源：东方财富财经新闻
    接口：akshare.stock_news_em(symbol=code)
    返回字段：发布日期、标题、来源

    注意：
      - akshare 的新闻接口返回的列名可能因版本不同而变化，
        代码使用 .get() 做容错处理。
      - 如果某条新闻缺少日期，回退到当天日期。
    """
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            logger.warning(f"No news found for A-stock {code}")
            return []

        news_list = []
        for _, row in df.head(limit).iterrows():
            news_list.append(NewsItem(
                code=code,
                # 日期容错：优先取"发布时间"列，否则取今天
                date=str(row.get("发布时间", date.today().isoformat()))[:10],
                title=str(row.get("标题", row.get("title", ""))),
                source=str(row.get("文章来源", row.get("source", ""))),
            ))
        logger.info(f"Fetched {len(news_list)} news for A-stock {code}")
        return news_list
    except Exception as e:
        logger.error(f"Failed to fetch news for A-stock {code}: {e}")
        return []


def _fetch_news_us(code: str, limit: int = 15) -> list[NewsItem]:
    """
    通过 yfinance 获取美股新闻。

    数据来源：Yahoo Finance 聚合新闻
    接口：yfinance.Ticker.news
    返回字段：pubDate (Unix 时间戳), title, provider.displayName

    注意：
      - yfinance .news 返回的是列表嵌套字典结构，需逐层提取。
      - pubDate 为 Unix 时间戳（秒），需转换为日期字符串。
      - 部分新闻可能缺少日期，回退到当天日期。
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(code)
        raw_news = ticker.news
        if not raw_news:
            logger.warning(f"No news found for US stock {code}")
            return []

        news_list = []
        for item in raw_news[:limit]:
            content = item.get("content", {})
            pub_date = content.get("pubDate", 0)
            # pubDate 可能是 Unix 时间戳（int）或字符串
            if isinstance(pub_date, (int, float)):
                date_str = datetime.fromtimestamp(pub_date).strftime("%Y-%m-%d")
            else:
                date_str = str(pub_date)[:10] if pub_date else date.today().isoformat()
            news_list.append(NewsItem(
                code=code,
                date=date_str,
                title=content.get("title", ""),
                source=content.get("provider", {}).get("displayName", ""),
            ))
        logger.info(f"Fetched {len(news_list)} news for US stock {code}")
        return news_list
    except Exception as e:
        logger.error(f"Failed to fetch news for US stock {code}: {e}")
        return []
