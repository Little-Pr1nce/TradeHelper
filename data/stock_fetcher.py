"""
股票数据获取模块

采用策略模式 (Strategy Pattern) 提供多数据源支持：
  - FreeStockFetcher: 免费数据源（A 股用 akshare，美股用 yfinance）
  - CustomStockFetcher: 用户自定义付费 API（占位，待扩展）

抽象基类 BaseStockFetcher 定义了统一的数据获取接口，
通过工厂函数 get_stock_fetcher() 根据配置返回对应的数据源实例。

【扩展点】如何添加新的数据源：
  1. 继承 BaseStockFetcher，实现 fetch_stock_info() 和 fetch_price_history()
  2. 在 settings.py 的 data_source 配置中新增选项（如 "eastmoney"）
  3. 在 get_stock_fetcher() 中添加对应分支
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd

from data.models import StockInfo, PriceData
from data.database import Database

logger = logging.getLogger(__name__)


# ======================== 抽象基类 ========================

class BaseStockFetcher(ABC):
    """
    股票数据获取器抽象基类。

    所有数据源（免费/付费）必须实现以下两个方法：
      - fetch_stock_info: 获取股票基本信息
      - fetch_price_history: 获取历史 K 线数据
    """

    @abstractmethod
    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        """获取股票基本信息（代码、名称、行业、简介等）。"""
        pass

    @abstractmethod
    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        """获取指定时间范围内的日 K 线数据（OHLCV 格式）。"""
        pass


# ======================== 免费数据源实现 ========================

class FreeStockFetcher(BaseStockFetcher):
    """
    免费数据源实现。

    A 股数据：通过 akshare 库获取
      - 基本信息：akshare.stock_individual_info_em()
      - 历史 K 线：akshare.stock_zh_a_hist()（前复权）

    美股数据：通过 yfinance 库获取
      - 基本信息：yfinance.Ticker.info
      - 历史 K 线：yfinance.Ticker.history()

    注意：
      - akshare 有请求频率限制，短时间高频调用可能被限制
      - yfinance 依赖 Yahoo Finance API，某些股票可能数据不完整
    """

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        """
        获取股票基本信息（自动识别 A 股/美股并调用对应方法）。

        Args:
            code: 股票代码

        Returns:
            StockInfo 实例，失败则返回 None
        """
        from utils.helpers import detect_market
        market = detect_market(code)
        try:
            if market == "A":
                return self._fetch_a_stock_info(code)
            elif market == "US":
                return self._fetch_us_stock_info(code)
        except Exception as e:
            logger.error(f"Failed to fetch stock info for {code}: {e}")
        return None

    def _fetch_a_stock_info(self, code: str) -> Optional[StockInfo]:
        """
        通过 akshare 获取 A 股基本信息。

        使用 stock_individual_info_em() 接口，
        从返回的 DataFrame 中提取股票简称、行业、主营业务等字段。
        """
        try:
            import akshare as ak
            df = ak.stock_individual_info_em(symbol=code)
            if df is None or df.empty:
                logger.warning(f"No info found for A-stock {code}")
                return None
            # 将两列 DataFrame 转为字典 {item: value}
            info_dict = dict(zip(df["item"], df["value"]))

            name = info_dict.get("股票简称", code)
            industry = info_dict.get("行业", "")
            # 拼接主营业务、公司简介、经营范围作为描述文本
            desc_items = []
            for key in ["主营业务", "公司简介", "经营范围"]:
                val = info_dict.get(key, "")
                if val:
                    desc_items.append(f"{key}：{val}")
            description = "；".join(desc_items) if desc_items else ""

            return StockInfo(
                code=code,
                name=name,
                market="A",
                industry=industry,
                description=description,
                update_time=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch A-stock info for {code}: {e}")
            return None

    def _fetch_us_stock_info(self, code: str) -> Optional[StockInfo]:
        """
        通过 yfinance 获取美股基本信息。

        从 Ticker.info 字典中提取名称、行业、业务摘要。
        业务摘要超过 2000 字符时截断，避免存储过大文本。
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(code)
            info = ticker.info
            # 校验关键字段是否存在
            if not info or info.get("shortName") is None and info.get("longName") is None:
                logger.warning(f"No info found for US stock {code}")
                return None

            name = info.get("shortName") or info.get("longName") or code
            industry = info.get("industry") or info.get("sector") or ""
            description = info.get("longBusinessSummary") or ""
            # 限制描述长度，避免数据库字段过大
            if len(description) > 2000:
                description = description[:2000]

            return StockInfo(
                code=code,
                name=name,
                market="US",
                industry=industry,
                description=description,
                update_time=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch US stock info for {code}: {e}")
            return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        """
        获取历史股价数据（自动识别市场）。

        Args:
            code: 股票代码
            start_date: 起始日期 "YYYY-MM-DD"
            end_date: 结束日期 "YYYY-MM-DD"

        Returns:
            PriceData 列表
        """
        from utils.helpers import detect_market
        market = detect_market(code)
        try:
            if market == "A":
                df = self._fetch_a_price_history(code, start_date, end_date)
            elif market == "US":
                df = self._fetch_us_price_history(code, start_date, end_date)
            else:
                return []
        except Exception as e:
            logger.error(f"Failed to fetch price history for {code}: {e}")
            return []

        if df is None or df.empty:
            return []

        # 统一转换为 PriceData 列表
        prices = []
        for _, row in df.iterrows():
            prices.append(PriceData(
                code=code,
                date=str(row["date"])[:10],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
        return prices

    def _fetch_a_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        通过 akshare 获取 A 股日 K 线。

        使用前复权 (qfq) 数据，便于回测计算。
        字段映射：akshare 中文列名 → 标准英文列名。
        """
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",  # 前复权：使历史股价可比较
            )
            if df is None or df.empty:
                return None
            # 列名映射：中文 → 英文
            df = df.rename(columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
            })
            df["volume"] = df["volume"].astype(float)
            df["date"] = df["date"].astype(str)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed to fetch A-stock prices for {code}: {e}")
            return None

    def _fetch_us_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        通过 yfinance 获取美股日 K 线。

        注意：yfinance 返回的 DataFrame 索引为 Date 类型，
        需 reset_index 后提取为 date 列。
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(code)
            df = ticker.history(start=start_date, end=end_date)
            if df is None or df.empty:
                return None
            df = df.reset_index()
            df = df.rename(columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })
            df["date"] = df["date"].astype(str)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed to fetch US stock prices for {code}: {e}")
            return None


# ======================== 自定义数据源（占位） ========================

class CustomStockFetcher(BaseStockFetcher):
    """
    用户自定义付费 API 数据源（占位实现）。

    当用户在设置中选择"自定义 API"时使用。
    目前方法体为空，需要用户根据自己的 API 文档实现。

    【扩展点】实现自定义数据源：
      1. 在 __init__ 中接收 api_endpoint 和 api_key
      2. 实现 fetch_stock_info: 调用自定义 API 的股票详情端点
      3. 实现 fetch_price_history: 调用自定义 API 的 K 线端点
      4. 返回的 PriceData 需统一为 (code, date, open, high, low, close, volume) 格式

    示例：
        def fetch_price_history(self, code, start_date, end_date):
            import requests
            resp = requests.get(f"{self.api_endpoint}/kline", params={
                "symbol": code,
                "start": start_date,
                "end": end_date,
            }, headers={"Authorization": f"Bearer {self.api_key}"})
            data = resp.json()
            return [PriceData(...) for item in data["data"]]
    """

    def __init__(self, api_endpoint: str, api_key: str):
        self.api_endpoint = api_endpoint
        self.api_key = api_key

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        logger.info(f"Custom fetcher: stock info not implemented for {code}")
        return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        logger.info(f"Custom fetcher: price history not implemented for {code}")
        return []


# ======================== 工厂函数 ========================

def get_stock_fetcher() -> BaseStockFetcher:
    """
    根据 Settings 配置返回对应的数据源实例。

    【扩展点】新增数据源后在此添加分支：
        settings = Settings()
        source = settings.get("data_source", "free")
        if source == "my_new_source":
            return MyNewFetcher(...)
        ...

    Returns:
        数据源实例（默认 FreeStockFetcher）
    """
    from config.settings import Settings
    settings = Settings()
    source = settings.get("data_source", "free")
    if source == "custom":
        return CustomStockFetcher(
            api_endpoint=settings.get("custom_api_endpoint", ""),
            api_key=settings.get("custom_api_key", ""),
        )
    return FreeStockFetcher()
