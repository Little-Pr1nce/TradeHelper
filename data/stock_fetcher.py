"""
股票数据获取模块

采用策略模式提供多数据源支持：
  - FreeStockFetcher: 免费数据源（A 股用 akshare，美股用 yfinance + akshare 兜底）
  - CustomStockFetcher: 用户自定义付费 API（占位，待扩展）

【扩展点】添加新的数据源：继承 BaseStockFetcher，在 get_stock_fetcher() 中注册。
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd

from data.models import StockInfo, PriceData

logger = logging.getLogger(__name__)


class BaseStockFetcher(ABC):
    """股票数据获取器抽象基类。"""

    @abstractmethod
    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        pass

    @abstractmethod
    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        pass


def _apply_proxy():
    """
    仅将代理应用到 yfinance（海外服务）。
    akshare 是东方财富国内接口，不走代理直连。
    不清除也不设置全局环境变量，避免影响其他库。
    """
    from config.settings import Settings
    proxy = Settings().get("proxy", "")
    if proxy:
        try:
            import yfinance as yf
            import requests
            # 用 session 隔离代理，不影响全局
            session = requests.Session()
            session.proxies = {"http": proxy, "https": proxy}
            yf.set_config(session=session)
        except Exception:
            pass


def _retry(func, max_retries=3, label=""):
    """带退避重试的通用调用。"""
    delay = 2
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            msg = str(e)
            is_rate = any(k in msg for k in ["Rate limited", "Too Many Requests", "rate limited"])
            is_rst = any(k in msg for k in ["RemoteDisconnected", "Connection aborted", "EOF occurred"])
            if (is_rate or is_rst) and attempt < max_retries - 1:
                logger.info(f"{label} retry {attempt + 1}/{max_retries} in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 1.5, 15)
                continue
            raise


class FreeStockFetcher(BaseStockFetcher):
    """免费数据源：A 股用 akshare，美股用 yfinance（失败则用 akshare 兜底）。"""

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        from utils.helpers import detect_market
        market = detect_market(code)
        _apply_proxy()
        try:
            if market == "A":
                return self._fetch_a_stock_info(code)
            elif market == "US":
                return self._fetch_us_stock_info(code)
        except Exception as e:
            logger.error(f"Failed to fetch stock info for {code}: {e}")
        return None

    def _fetch_a_stock_info(self, code: str) -> Optional[StockInfo]:
        try:
            import akshare as ak
            df = ak.stock_individual_info_em(symbol=code)
            if df is None or df.empty:
                return None
            info_dict = dict(zip(df["item"], df["value"]))
            name = info_dict.get("股票简称", code)
            industry = info_dict.get("行业", "")
            desc_items = []
            for key in ["主营业务", "公司简介", "经营范围"]:
                val = info_dict.get(key, "")
                if val:
                    desc_items.append(f"{key}：{val}")
            description = "；".join(desc_items) if desc_items else ""
            return StockInfo(
                code=code, name=name, market="A",
                industry=industry, description=description,
                update_time=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch A-stock info for {code}: {e}")
            return None

    def _fetch_us_stock_info(self, code: str) -> Optional[StockInfo]:
        try:
            import yfinance as yf
            info = _retry(lambda: yf.Ticker(code).info, max_retries=2, label="yf.info")
            if not info or not (info.get("shortName") or info.get("longName")):
                logger.warning(f"No info found for US stock {code}")
                return None
            name = info.get("shortName") or info.get("longName") or code
            industry = info.get("industry") or info.get("sector") or ""
            description = (info.get("longBusinessSummary") or "")[:2000]
            return StockInfo(
                code=code, name=name, market="US",
                industry=industry, description=description,
                update_time=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch US stock info for {code}: {e}")
            return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        from utils.helpers import detect_market
        market = detect_market(code)
        _apply_proxy()
        df = None
        try:
            if market == "A":
                df = self._fetch_a_price_history(code, start_date, end_date)
            elif market == "US":
                # 优先用 akshare（国内可用），yfinance 做备选（需 VPN）
                df = self._fetch_us_price_akshare(code, start_date, end_date)
                if df is None or df.empty:
                    logger.info(f"akshare failed, trying yfinance for US stock {code}...")
                    df = self._fetch_us_price_history(code, start_date, end_date)
        except Exception as e:
            logger.error(f"Failed to fetch price history for {code}: {e}")
            return []

        if df is None or df.empty:
            return []

        df = df.rename(columns=str.lower)
        prices = []
        for _, row in df.iterrows():
            try:
                prices.append(PriceData(
                    code=code,
                    date=str(row["date"])[:10],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
            except (KeyError, ValueError):
                continue
        return prices

    # ---- A 股 K 线 (akshare) ----
    def _fetch_a_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        try:
            import akshare as ak
            df = _retry(lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq",
            ), max_retries=3, label="akshare.A")
            if df is None or df.empty:
                return None
            df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                     "最低": "low", "收盘": "close", "成交量": "volume"})
            df["volume"] = df["volume"].astype(float)
            df["date"] = df["date"].astype(str)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed A-stock prices for {code}: {e}")
            return None

    # ---- 美股 K 线 (yfinance) ----
    def _fetch_us_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf
            df = _retry(lambda: yf.Ticker(code).history(start=start_date, end=end_date),
                       max_retries=3, label="yf.history")
            if df is None or df.empty:
                return None
            df = df.reset_index()
            df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                     "Low": "low", "Close": "close", "Volume": "volume"})
            df["date"] = df["date"].astype(str)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed US stock prices for {code}: {e}")
            return None

    # ---- 美股 K 线 (akshare 兜底) ----
    def _fetch_us_price_akshare(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        try:
            import akshare as ak
            sd = start_date.replace("-", "")
            ed = end_date.replace("-", "")
            df = _retry(lambda: ak.stock_us_hist(
                symbol=code, period="daily",
                start_date=sd, end_date=ed, adjust="qfq",
            ), max_retries=2, label="akshare.US")
            if df is None or (hasattr(df, 'empty') and df.empty):
                return None
            if not isinstance(df, pd.DataFrame):
                return None
            df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                     "最低": "low", "收盘": "close", "成交量": "volume"})
            df["volume"] = df["volume"].astype(float)
            df["date"] = df["date"].astype(str)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"Failed akshare US prices for {code}: {e}")
            return None


class CustomStockFetcher(BaseStockFetcher):
    """用户自定义付费 API 数据源（占位）。"""

    def __init__(self, api_endpoint: str, api_key: str):
        self.api_endpoint = api_endpoint
        self.api_key = api_key

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        return []


def get_stock_fetcher() -> BaseStockFetcher:
    """根据配置返回数据源实例。"""
    from config.settings import Settings
    settings = Settings()
    if settings.get("data_source", "free") == "custom":
        return CustomStockFetcher(
            api_endpoint=settings.get("custom_api_endpoint", ""),
            api_key=settings.get("custom_api_key", ""),
        )
    return FreeStockFetcher()
