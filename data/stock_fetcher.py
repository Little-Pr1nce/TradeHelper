"""
股票数据获取模块

采用策略模式提供多数据源支持：
  - FreeStockFetcher: 免费数据源（A 股多源降级，美股 yfinance + akshare 兜底）
  - CustomStockFetcher: 用户自定义付费 API（占位，待扩展）

【扩展点】添加新的数据源：继承 BaseStockFetcher，在 get_stock_fetcher() 中注册。
"""

import logging
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional, TypeVar

import pandas as pd

from data.models import StockInfo, PriceData

logger = logging.getLogger(__name__)

T = TypeVar("T")

_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)
_PROXY_STATE: dict = {"applied": None}


class BaseStockFetcher(ABC):
    """股票数据获取器抽象基类。"""

    @abstractmethod
    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        pass

    @abstractmethod
    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        pass


@contextmanager
def _without_system_proxy():
    """
    临时清除进程级代理环境变量，避免 akshare 国内接口被 macOS 系统代理误伤。

    requests 默认 trust_env=True，会读取 HTTP_PROXY 等环境变量及系统代理。
    """
    saved = {k: os.environ[k] for k in _PROXY_ENV_KEYS if k in os.environ}
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


def _detect_macos_http_proxy() -> str:
    """读取 macOS 系统 HTTP 代理（MonoProxy/Surge 等开启「系统代理」时可用）。"""
    try:
        out = subprocess.check_output(["scutil", "--proxy"], text=True, timeout=3)
        if "HTTPEnable : 1" not in out and "HTTPSEnable : 1" not in out:
            return ""
        host_m = re.search(r"HTTPProxy : (\S+)", out)
        port_m = re.search(r"HTTPPort : (\d+)", out)
        if host_m and port_m:
            return f"http://{host_m.group(1)}:{port_m.group(1)}"
    except Exception:
        pass
    return ""


def _resolve_proxy_url() -> str:
    """优先 Settings，否则回退 macOS 系统 HTTP 代理。"""
    from config.settings import Settings
    configured = (Settings().get("proxy", "") or "").strip()
    if configured:
        return configured
    detected = _detect_macos_http_proxy()
    if detected:
        logger.debug(f"Using macOS system HTTP proxy: {detected}")
    return detected


def _yfinance_proxy(proxy_url: str):
    """
    yfinance >= 1.4（curl_cffi）要求 proxy 为 dict，不能传字符串。
    """
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _apply_proxy():
    """
    将代理应用到 yfinance（海外服务）。

    来源：Settings.proxy → macOS 系统 HTTP 代理（8118 等）。
    """
    proxy_url = _resolve_proxy_url()
    if _PROXY_STATE["applied"] == proxy_url:
        return

    try:
        import yfinance as yf
        yf.config.network.proxy = _yfinance_proxy(proxy_url)
        _PROXY_STATE["applied"] = proxy_url
        if proxy_url:
            logger.info(f"yfinance proxy: {proxy_url}")
    except Exception as e:
        logger.warning(f"Failed to configure yfinance proxy: {e}")


def _retry(func: Callable[[], T], max_retries=3, label="") -> T:
    """带退避重试的通用调用。"""
    delay = 2
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            msg = str(e)
            is_rate = any(k in msg for k in ["Rate limited", "Too Many Requests", "rate limited"])
            is_rst = any(k in msg for k in [
                "RemoteDisconnected", "Connection aborted", "EOF occurred",
                "ProxyError", "Empty reply",
            ])
            if (is_rate or is_rst) and attempt < max_retries - 1:
                logger.info(f"{label} retry {attempt + 1}/{max_retries} in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 1.5, 15)
                continue
            raise


def _a_share_exchange(code: str) -> str:
    """返回 A 股新浪/雪球接口用的小写市场前缀 sh / sz。"""
    if code.startswith(("600", "601", "603", "605", "688")):
        return "sh"
    return "sz"


def _xq_symbol(code: str) -> str:
    """A 股雪球 symbol，如 SH603993。"""
    return f"{_a_share_exchange(code).upper()}{code}"


def _normalize_kline_df(df: pd.DataFrame) -> pd.DataFrame:
    """将各源 K 线统一为 date/open/high/low/close/volume 列，并处理缺失值。"""
    col_map = {
        "日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume",
    }
    df = df.rename(columns=col_map).copy()
    if "date" not in df.columns:
        raise ValueError("missing date column")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 关键字段任一为 NaN 则丢弃该行（确保技术指标和回测计算不出错）
    required_cols = ["date", "open", "high", "low", "close"]
    return df[required_cols + ["volume"]].dropna(subset=required_cols)


def _stock_info_from_xq(df: pd.DataFrame, code: str, market: str) -> StockInfo:
    info_dict = dict(zip(df["item"], df["value"]))
    name = (
        info_dict.get("org_short_name_cn")
        or info_dict.get("org_name_cn")
        or code
    )
    industry = str(info_dict.get("industry") or info_dict.get("affiliate_industry") or "")
    desc_parts = []
    for key in ("main_operation", "org_description", "business_scope"):
        val = info_dict.get(key)
        if val:
            desc_parts.append(str(val))
    description = "；".join(desc_parts)[:2000]
    return StockInfo(
        code=code, name=str(name), market=market,
        industry=industry, description=description,
        update_time=datetime.now().isoformat(),
    )


def _us_akshare_symbol_candidates(code: str) -> list[str]:
    """东财美股 hist 接口 secid 候选，形如 105.AAPL。"""
    code = code.upper()
    if "." in code:
        return [code]
    return [f"{prefix}.{code}" for prefix in ("105", "106", "107")]


class FreeStockFetcher(BaseStockFetcher):
    """免费数据源：A 股新浪/雪球/东财多源降级；美股 akshare + yfinance。"""

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        from utils.market import detect_market
        market = detect_market(code)
        try:
            if market == "A":
                return self._fetch_a_stock_info(code)
            elif market == "US":
                _apply_proxy()
                return self._fetch_us_stock_info(code)
        except Exception as e:
            logger.error(f"Failed to fetch stock info for {code}: {e}")
        return None

    def _fetch_a_stock_info(self, code: str) -> Optional[StockInfo]:
        with _without_system_proxy():
            for label, fetcher in (
                ("xueqiu", self._fetch_a_stock_info_xq),
                ("eastmoney", self._fetch_a_stock_info_em),
            ):
                try:
                    info = fetcher(code)
                    if info:
                        logger.info(f"A-stock info for {code} via {label}")
                        return info
                except Exception as e:
                    logger.warning(f"A-stock info {label} failed for {code}: {e}")
        return None

    def _fetch_a_stock_info_xq(self, code: str) -> Optional[StockInfo]:
        import akshare as ak
        df = ak.stock_individual_basic_info_xq(symbol=_xq_symbol(code))
        if df is None or df.empty:
            return None
        return _stock_info_from_xq(df, code, "A")

    def _fetch_a_stock_info_em(self, code: str) -> Optional[StockInfo]:
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

    def _fetch_us_stock_info(self, code: str) -> Optional[StockInfo]:
        try:
            import yfinance as yf
            info = _retry(lambda: yf.Ticker(code).info, max_retries=2, label="yf.info")
            if info and (info.get("shortName") or info.get("longName")):
                name = info.get("shortName") or info.get("longName") or code
                industry = info.get("industry") or info.get("sector") or ""
                description = (info.get("longBusinessSummary") or "")[:2000]
                return StockInfo(
                    code=code, name=name, market="US",
                    industry=industry, description=description,
                    update_time=datetime.now().isoformat(),
                )
        except Exception as e:
            logger.warning(f"yfinance info failed for US {code}: {e}")

        with _without_system_proxy():
            try:
                import akshare as ak
                df = ak.stock_individual_basic_info_us_xq(symbol=code.upper())
                if df is not None and not df.empty:
                    info = _stock_info_from_xq(df, code, "US")
                    logger.info(f"US stock info for {code} via xueqiu")
                    return info
            except Exception as e:
                logger.warning(f"xueqiu US info failed for {code}: {e}")
        return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        from utils.market import detect_market
        market = detect_market(code)
        df = None
        try:
            if market == "A":
                df = self._fetch_a_price_history(code, start_date, end_date)
            elif market == "US":
                _apply_proxy()
                with _without_system_proxy():
                    df = self._fetch_us_price_akshare(code, start_date, end_date)
                if df is None or df.empty:
                    logger.info(f"akshare failed, trying yfinance for US stock {code}...")
                    df = self._fetch_us_price_history(code, start_date, end_date)
        except Exception as e:
            logger.error(f"Failed to fetch price history for {code}: {e}")
            return []

        if df is None or df.empty:
            return []

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
            except (KeyError, ValueError, TypeError):
                continue
        return prices

    def _fetch_a_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        exchange = _a_share_exchange(code)

        with _without_system_proxy():
            try:
                import akshare as ak
                df = _retry(
                    lambda: ak.stock_zh_a_daily(
                        symbol=f"{exchange}{code}",
                        start_date=sd, end_date=ed, adjust="qfq",
                    ),
                    max_retries=2, label="akshare.sina",
                )
                if df is not None and not df.empty:
                    logger.info(f"A-stock prices for {code} via sina")
                    return _normalize_kline_df(df)
            except Exception as e:
                logger.warning(f"Sina A-stock prices failed for {code}: {e}")

            try:
                import akshare as ak
                df = _retry(
                    lambda: ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start_date, end_date=end_date, adjust="qfq",
                    ),
                    max_retries=2, label="akshare.em",
                )
                if df is not None and not df.empty:
                    logger.info(f"A-stock prices for {code} via eastmoney")
                    return _normalize_kline_df(df)
            except Exception as e:
                logger.error(f"Eastmoney A-stock prices failed for {code}: {e}")
        return None

    def _fetch_us_price_history(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf
            df = _retry(
                lambda: yf.Ticker(code).history(start=start_date, end=end_date),
                max_retries=3, label="yf.history",
            )
            if df is None or df.empty:
                return None
            df = df.reset_index()
            df = df.rename(columns={
                "Date": "date", "Open": "open", "High": "high",
                "Low": "low", "Close": "close", "Volume": "volume",
            })
            return _normalize_kline_df(df)
        except Exception as e:
            logger.error(f"Failed US stock prices for {code}: {e}")
            return None

    def _fetch_us_price_akshare(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        import akshare as ak
        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")

        for symbol in _us_akshare_symbol_candidates(code):
            try:
                df = _retry(
                    lambda sym=symbol: ak.stock_us_hist(
                        symbol=sym, period="daily",
                        start_date=sd, end_date=ed, adjust="qfq",
                    ),
                    max_retries=1, label=f"akshare.US.{symbol}",
                )
                if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
                    logger.info(f"US stock prices for {code} via akshare ({symbol})")
                    return _normalize_kline_df(df)
            except Exception as e:
                logger.debug(f"akshare US {symbol} failed: {e}")
                continue

        logger.error(f"Failed akshare US prices for {code} (tried {_us_akshare_symbol_candidates(code)})")
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


class ItickStockFetcher(BaseStockFetcher):
    """
    itick 付费数据源。

    API 文档: https://docs.itick.org

    接口:
      - GET /stock/info    → 股票基本信息
      - GET /stock/kline   → 历史 K 线（日线 kType=8）
    """

    BASE_URL = "https://api0.itick.org"

    # A 股代码前缀 → itick region 映射
    _A_SHARE_PREFIX = {
        "6": "SH",   # 600xxx/601xxx/603xxx/605xxx → 上海
        "0": "SZ",   # 000xxx/001xxx/002xxx → 深圳
        "3": "SZ",   # 300xxx → 深圳创业板
    }

    def __init__(self, token: str):
        self.token = token
        self._session = None

    def _get_session(self):
        """延迟创建 requests Session（避免 import 时加载）。"""
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False  # 不走系统代理，itick 直连
            self._session.headers.update({
                "accept": "application/json",
                "token": self.token,
            })
        return self._session

    @staticmethod
    def _a_stock_region(code: str) -> str:
        """A 股代码 → itick region（SH/SZ）。"""
        if code.isdigit() and len(code) == 6:
            prefix = code[0]
            return ItickStockFetcher._A_SHARE_PREFIX.get(prefix, "SH")
        return "SH"

    def _get(self, path: str, params: dict, max_retries: int = 3) -> dict:
        """带重试的 GET 请求。"""
        import time as _time
        session = self._get_session()
        url = f"{self.BASE_URL}{path}"
        delay = 1
        last_error = None
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params=params, timeout=30)
                # 非 200 时打印完整响应体帮助诊断
                if not resp.ok:
                    body = resp.text[:500]
                    logger.error(f"itick HTTP {resp.status_code}: {body}")
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"itick API 错误: code={data.get('code')}, msg={data.get('msg')}")
                return data
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(f"itick 请求重试 {attempt+1}/{max_retries} in {delay}s: {e}")
                    _time.sleep(delay)
                    delay = min(delay * 2, 10)
        raise last_error  # type: ignore

    # ── fetch_stock_info ──

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        """通过 itick /stock/info 获取股票基本信息。"""
        from utils.market import detect_market
        market = detect_market(code)
        region = self._a_stock_region(code) if market == "A" else "US"

        try:
            data = self._get("/stock/info", {"type": "stock", "region": region, "code": code})
            d = data.get("data", {})
            if not d:
                return None

            name = str(d.get("n", code))
            industry = str(d.get("i", "") or d.get("s", ""))
            description = str(d.get("bd", ""))[:2000]

            logger.info(f"itick 股票信息: {name} ({industry})")
            return StockInfo(
                code=code,
                name=name,
                market=market,
                industry=industry,
                description=description,
                update_time=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error(f"itick fetch_stock_info 失败 ({code}): {e}")
            return None

    # ── fetch_price_history ──

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        """通过 itick /stock/kline 获取日 K 线数据。"""
        from utils.market import detect_market
        market = detect_market(code)
        region = self._a_stock_region(code) if market == "A" else "US"

        # 估算需要的 K 线条数：日期跨度 + 20% 余量
        try:
            from datetime import datetime as _dt
            d_start = _dt.strptime(start_date, "%Y-%m-%d")
            d_end = _dt.strptime(end_date, "%Y-%m-%d")
            days = max((d_end - d_start).days, 1)
            limit = min(int(days * 1.4), 2000)  # 最多 2000 条
        except Exception:
            limit = 1000

        try:
            data = self._get("/stock/kline", {
                "region": region, "code": code,
                "kType": 8,        # 日 K 线
                "limit": limit,
            })
            bars = data.get("data", [])
            if not bars:
                logger.warning(f"itick K 线为空 ({code})")
                return []

            prices = []
            for bar in bars:
                # 时间戳（毫秒） → 日期字符串
                ts = bar.get("t", 0)
                date_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")

                # 按日期范围过滤
                if date_str < start_date or date_str > end_date:
                    continue

                try:
                    prices.append(PriceData(
                        code=code,
                        date=date_str,
                        open=float(bar["o"]),
                        high=float(bar["h"]),
                        low=float(bar["l"]),
                        close=float(bar["c"]),
                        volume=float(bar["v"]),
                    ))
                except (KeyError, ValueError, TypeError):
                    continue

            logger.info(f"itick K 线: {len(prices)} 条 ({start_date}~{end_date})")
            return prices
        except Exception as e:
            logger.error(f"itick fetch_price_history 失败 ({code}): {e}")
            return []


def get_stock_fetcher(data_source: str = "free") -> BaseStockFetcher:
    """
    根据配置返回数据源实例。

    Args:
        data_source: "free"（免费 akshare+yfinance）/ "custom"（付费 itick 等）

    Returns:
        BaseStockFetcher 实例
    """
    from config.settings import Settings
    settings = Settings()
    if data_source == "custom":
        token = settings.get("paid_api_token", "")
        if token:
            logger.info("使用付费数据源: itick")
            return ItickStockFetcher(token)
        # 有 custom_api_endpoint 的旧逻辑兼容
        if settings.get("custom_api_endpoint", ""):
            return CustomStockFetcher(
                api_endpoint=settings.get("custom_api_endpoint", ""),
                api_key=settings.get("custom_api_key", ""),
            )
        logger.warning("付费数据源已选择但未配置 token，降级为免费数据源")
    return FreeStockFetcher()
