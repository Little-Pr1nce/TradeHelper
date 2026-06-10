"""
股票数据获取模块

采用策略模式提供多数据源支持：
  - TickFlowFetcher: TickFlow 行情 API（A 股+美股，日K线免费，实时行情需 API Key）
  - FreeStockFetcher: 免费数据源（A 股 akshare，美股 Finnhub）
  - CustomStockFetcher: 用户自定义 API（占位，待扩展）

美股数据流：K 线 + 实时 → TickFlow；信息/新闻/基本面 → Finnhub (news_token_us)。
A 股数据流：K 线 + 实时 → TickFlow；新闻 → akshare；基本面 → akshare。

数据源由 get_stock_fetcher(market) 自动选择。
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
    """免费数据源：A 股新浪/雪球/东财多源降级；美股 Finnhub 信息（K 线建议用 TickFlowFetcher）。"""

    def __init__(self, finnhub_token: str = ""):
        self._finnhub_token = (finnhub_token or "").strip()

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        from utils.market import detect_market
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
        # ── 唯一来源：Finnhub /stock/profile2 ──
        if self._finnhub_token:
            try:
                from data.finnhub_client import fetch_company_profile
                profile = fetch_company_profile(self._finnhub_token, code)
                if profile:
                    name = profile.get("name") or code
                    industry = profile.get("industry") or profile.get("finnhubIndustry") or ""
                    market_cap = profile.get("marketCapitalization", 0)
                    description_parts = []
                    for key in ("exchange", "country", "currency"):
                        val = profile.get(key)
                        if val:
                            description_parts.append(f"{key}:{val}")
                    if market_cap:
                        description_parts.append(f"marketCap:{market_cap:.0f}")
                    logger.info(f"US stock info for {code} via Finnhub ({name})")
                    return StockInfo(
                        code=code, name=name, market="US",
                        industry=industry,
                        description="; ".join(description_parts)[:2000],
                        update_time=datetime.now().isoformat(),
                    )
            except Exception as e:
                logger.warning(f"Finnhub stock info failed for US {code}: {e}")

        logger.warning(f"No US stock info for {code} (no finnhub token or request failed)")
        return None

    def fetch_price_history(self, code: str, start_date: str, end_date: str) -> list[PriceData]:
        from utils.market import detect_market
        market = detect_market(code)
        df = None
        try:
            if market == "A":
                df = self._fetch_a_price_history(code, start_date, end_date)
            elif market == "US":
                raise RuntimeError(
                    f"美股 {code} 未配置数据源 Token。"
                    f"请在设置中填入 TickFlow API Key（tickflow.org 免费注册获取）。"
                )
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
      - GET /stock/quote   → 实时报价
      - GET /stock/tick    → 实时成交（含交易时段标识 te）
      - GET /future/quote  → 期货实时报价（盘前分析用）
      - GET /future/kline  → 期货历史 K 线（盘前分析用）
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

    # ── fetch_quote ──

    def fetch_quote(self, code: str) -> dict | None:
        """
        通过 itick /stock/quote 获取实时报价。

        Returns:
            {
                "code": str,       # 产品代码
                "latest": float,   # 最新价
                "open": float,     # 开盘价
                "high": float,     # 最高价
                "low": float,      # 最低价
                "prev_close": float,  # 前日收盘价
                "change": float,   # 涨跌额
                "change_pct": float,  # 涨跌幅百分比
                "volume": float,   # 成交量
                "amount": float,   # 成交额
                "timestamp": int,  # 时间戳（毫秒）
                "status": int,     # 交易状态 0:正常 1:停牌 2:退市 3:熔断
            }
            失败返回 None
        """
        from utils.market import detect_market
        market = detect_market(code)
        region = self._a_stock_region(code) if market == "A" else "US"

        try:
            data = self._get("/stock/quote", {"region": region, "code": code})
            d = data.get("data", {})
            if not d:
                return None

            latest = d.get("ld", 0)
            prev_close = d.get("p", 0)
            # 自己计算涨跌幅：不依赖 API 返回的 chp
            # （chp 的格式不稳定，有时是百分数 -73.0 有时是小数 -0.0073）
            if latest and prev_close and prev_close > 0:
                own_chp = (float(latest) - float(prev_close)) / float(prev_close)  # 小数
            else:
                own_chp = 0.0

            quote = {
                "code": str(d.get("s", code)),
                "latest": float(latest) if latest else 0.0,
                "open": float(d.get("o", 0)),
                "high": float(d.get("h", 0)),
                "low": float(d.get("l", 0)),
                "prev_close": float(prev_close) if prev_close else 0.0,
                "change": float(d.get("ch", 0)),
                "change_pct": round(own_chp, 6),  # 小数：0.0069 = 0.69%
                "volume": float(d.get("v", 0)),
                "amount": float(d.get("tu", 0)),
                "timestamp": d.get("t", 0),
                "status": d.get("ts", 0),
            }
            logger.info(
                f"itick 实时报价 ({code}): "
                f"最新价={quote['latest']:.2f}, 涨跌={quote['change_pct']:+.2%}"
            )
            return quote
        except Exception as e:
            logger.error(f"itick fetch_quote 失败 ({code}): {e}")
            return None

    # ── fetch_stock_tick ──

    def fetch_stock_tick(self, code: str) -> dict | None:
        """
        通过 itick /stock/tick 获取实时成交数据（含交易时段标识）。

        Returns:
            {
                "latest": float,         # 最新价
                "volume": float,         # 成交数量
                "timestamp": int,        # 时间戳（毫秒）
                "trading_phase": int,    # 0:常规交易 1:盘前交易 2:盘后交易
            }
            失败返回 None
        """
        from utils.market import detect_market
        market = detect_market(code)
        region = self._a_stock_region(code) if market == "A" else "US"

        try:
            data = self._get("/stock/tick", {"region": region, "code": code})
            d = data.get("data", {})
            if not d:
                return None

            tick = {
                "latest": float(d.get("ld", 0)),
                "volume": float(d.get("v", 0)),
                "timestamp": d.get("t", 0),
                "trading_phase": d.get("te", 0),  # 0:常规 1:盘前 2:盘后
            }
            phase_labels = {0: "常规交易", 1: "盘前交易", 2: "盘后交易"}
            logger.info(
                f"itick 实时成交 ({code}): "
                f"最新价={tick['latest']:.2f}, "
                f"交易时段={phase_labels.get(tick['trading_phase'], '未知')}"
            )
            return tick
        except Exception as e:
            logger.error(f"itick fetch_stock_tick 失败 ({code}): {e}")
            return None

    # ── fetch_future_quote ──

    def fetch_future_quote(self, region: str, code: str) -> dict | None:
        """
        通过 itick /future/quote 获取期货实时报价。

        Args:
            region: 市场代码（US/HK/CN）
            code:   期货代码（NQ=纳指, ES=标普）

        Returns:
            {
                "code": str,           # 产品代码
                "latest": float,       # 最新价
                "open": float,         # 开盘价
                "high": float,         # 最高价
                "low": float,          # 最低价
                "prev_close": float,   # 前日收盘价
                "change": float,       # 涨跌额
                "change_pct": float,   # 涨跌幅百分比（已归一化为小数）
                "volume": float,       # 成交量
                "amount": float,       # 成交额
                "timestamp": int,      # 时间戳（毫秒）
                "status": int,         # 交易状态
            }
            失败返回 None
        """
        try:
            data = self._get("/future/quote", {"region": region, "code": code})
            d = data.get("data", {})
            if not d:
                return None

            latest = d.get("ld", 0)
            prev_close = d.get("p", 0)
            # 自己计算涨跌幅，不依赖 API 的 chp
            # （itick 期货 chp 可能返回涨跌点数而非百分比，不可靠）
            if latest and prev_close and prev_close > 0:
                own_chp = (float(latest) - float(prev_close)) / float(prev_close)
            else:
                own_chp = 0.0

            quote = {
                "code": str(d.get("s", code)),
                "latest": float(latest) if latest else 0.0,
                "open": float(d.get("o", 0)),
                "high": float(d.get("h", 0)),
                "low": float(d.get("l", 0)),
                "prev_close": float(prev_close) if prev_close else 0.0,
                "change": float(d.get("ch", 0)),
                "change_pct": round(own_chp, 6),
                "volume": float(d.get("v", 0)),
                "amount": float(d.get("tu", 0)),
                "timestamp": d.get("t", 0),
                "status": d.get("ts", 0),
            }
            logger.info(
                f"itick 期货报价 ({code}): "
                f"最新价={quote['latest']:.2f}, 涨跌={quote['change_pct']:+.2%}"
            )
            return quote
        except Exception as e:
            logger.error(f"itick fetch_future_quote 失败 ({region}:{code}): {e}")
            return None

    # ── fetch_future_kline ──

    def fetch_future_kline(
        self, region: str, code: str,
        kType: int = 1, limit: int = 60,
    ) -> list[dict]:
        """
        通过 itick /future/kline 获取期货历史 K 线。

        Args:
            region: 市场代码（US/HK/CN）
            code:   期货代码（NQ=纳指, ES=标普）
            kType:  K线类型
                    1=1分钟, 2=5分钟, 3=15分钟, 4=30分钟,
                    5=1小时, 6=2小时, 7=4小时, 8=日K, 9=周K, 10=月K
            limit:  K线数量（默认 60）

        Returns:
            list[dict]: 每个 bar 含 {t, o, h, l, c, v, tu}
            失败返回空列表
        """
        try:
            data = self._get("/future/kline", {
                "region": region, "code": code,
                "kType": kType, "limit": limit,
            })
            bars = data.get("data", [])
            if not bars:
                logger.warning(f"itick 期货 K 线为空 ({region}:{code})")
                return []

            result = []
            for bar in bars:
                try:
                    result.append({
                        "t": bar.get("t", 0),      # 时间戳（毫秒）
                        "o": float(bar["o"]),       # 开盘价
                        "h": float(bar["h"]),       # 最高价
                        "l": float(bar["l"]),       # 最低价
                        "c": float(bar["c"]),       # 收盘价
                        "v": float(bar.get("v", 0)),  # 成交量
                        "tu": float(bar.get("tu", 0)), # 成交额
                    })
                except (KeyError, ValueError, TypeError):
                    continue

            logger.info(
                f"itick 期货 K 线 ({region}:{code}): "
                f"{len(result)} 条 (kType={kType})"
            )
            return result
        except Exception as e:
            logger.error(f"itick fetch_future_kline 失败 ({region}:{code}): {e}")
            return []


class TickFlowFetcher(BaseStockFetcher):
    """
    TickFlow 行情数据源，统一覆盖 A 股 + 美股。

    免费层（无需 API Key）：
      - 日 K 线（前复权），A 股+美股全量历史数据
      - 标的基本信息（名称、代码）
      - 不支持实时行情

    注册用户（配置 API Key）：
      - 以上全部 + 实时行情（A 股+美股 Level-1）

    标的代码格式：600519.SH / 000001.SZ / AAPL.US

    安装：pip install tickflow
    """

    # A 股代码 → TickFlow 后缀
    # 6=主板/科创板(SH), 5=ETF(SH), 9=B股(SH); 0/2/3=深市主板/创业板(SZ), 159=深市ETF(SZ)
    _A_SH_CODES = frozenset({"6", "5", "9"})

    def __init__(self, api_key: str = ""):
        from tickflow import TickFlow
        self._api_key = (api_key or "").strip()
        if self._api_key:
            self._tf = TickFlow(api_key=self._api_key, base_url="https://api.tickflow.org")
            logger.info("TickFlow 完整服务已初始化（日K线 + 实时行情）")
        else:
            self._tf = TickFlow.free()
            logger.info("TickFlow 免费服务已初始化（仅日K线，无实时行情）")

    @property
    def has_realtime(self) -> bool:
        """是否支持实时行情。"""
        return bool(self._api_key)

    @staticmethod
    def _to_symbol(code: str) -> str:
        """600519 → 600519.SH / AAPL → AAPL.US"""
        from utils.market import detect_market
        market = detect_market(code)
        if market == "A":
            prefix = code[0]
            if prefix in TickFlowFetcher._A_SH_CODES:
                suffix = "SH"
            elif code[:3] == "159":
                suffix = "SZ"
            else:
                suffix = "SZ"
            return f"{code}.{suffix}"
        elif market == "US":
            return f"{code}.US"
        return code

    # ── fetch_stock_info ──

    def fetch_stock_info(self, code: str) -> Optional[StockInfo]:
        """从日 K 线数据中获取股票名称（免费层可用）。"""
        from utils.market import detect_market
        try:
            df = self._tf.klines.get(self._to_symbol(code), period="1d", count=1,
                                     as_dataframe=True)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                name = str(row.get("name", code))
                market = detect_market(code)
                logger.info(f"TickFlow 股票信息: {name} ({code})")
                return StockInfo(
                    code=code, name=name, market=market,
                    update_time=datetime.now().isoformat(),
                )
        except Exception as e:
            logger.error(f"TickFlow fetch_stock_info 失败 ({code}): {e}")
        return None

    # ── fetch_price_history ──

    def fetch_price_history(self, code: str, start_date: str,
                            end_date: str) -> list[PriceData]:
        """通过 TickFlow 获取日 K 线（前复权）。"""
        try:
            from datetime import datetime as _dt
            d_start = _dt.strptime(start_date, "%Y-%m-%d")
            d_end = _dt.strptime(end_date, "%Y-%m-%d")
            days = max((d_end - d_start).days, 1)
            count = min(int(days * 1.4), 10000)
        except Exception:
            count = 1000

        try:
            symbol = self._to_symbol(code)
            df = self._tf.klines.get(symbol, period="1d", count=count,
                                     as_dataframe=True)
            if df is None or df.empty:
                logger.warning(f"TickFlow K 线为空 ({code})")
                return []

            prices = []
            is_a_share = code.isdigit() and len(code) == 6
            for _, row in df.iterrows():
                date_str = str(row.get("trade_date", ""))[:10]
                if date_str < start_date or date_str > end_date:
                    continue
                try:
                    vol = float(row["volume"])
                    # TickFlow A 股成交量单位为「手」（100 股），转为「股」与历史数据一致
                    if is_a_share:
                        vol *= 100
                    prices.append(PriceData(
                        code=code,
                        date=date_str,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=vol,
                    ))
                except (KeyError, ValueError, TypeError):
                    continue

            logger.info(f"TickFlow K 线: {len(prices)} 条 ({start_date}~{end_date})")
            return prices
        except Exception as e:
            logger.error(f"TickFlow fetch_price_history 失败 ({code}): {e}")
            return []

    # ── fetch_quote ──

    def fetch_quote(self, code: str) -> dict | None:
        """
        实时报价（需要 API Key）。

        Returns 格式与 itick quote 兼容：
            {code, latest, open, high, low, prev_close, change, change_pct,
             volume, amount, timestamp, status, vwap}
        """
        if not self.has_realtime:
            logger.warning("TickFlow 免费层不支持实时行情，需要 API Key")
            return None

        try:
            symbol = self._to_symbol(code)
            quotes = self._tf.quotes.get(symbols=[symbol])
            if not quotes:
                return None
            q = quotes[0]

            latest = float(q.get("last_price", 0))
            prev_close = float(q.get("prev_close", 0))
            own_chp = (latest - prev_close) / prev_close if latest and prev_close else 0.0

            return {
                "code": str(code),
                "latest": latest,
                "open": float(q.get("open", 0)),
                "high": float(q.get("high", 0)),
                "low": float(q.get("low", 0)),
                "prev_close": prev_close,
                "change": latest - prev_close,
                "change_pct": round(own_chp, 6),
                "volume": float(q.get("volume", 0)),
                "amount": float(q.get("amount", 0)),
                "timestamp": int(q.get("timestamp", 0)),
                "status": 0,
                "vwap": float(q.get("vwap", 0)) if q.get("vwap") else 0.0,
            }
        except Exception as e:
            logger.error(f"TickFlow fetch_quote 失败 ({code}): {e}")
            return None

    # ── fetch_stock_tick ──

    def fetch_stock_tick(self, code: str) -> dict | None:
        """
        实时成交数据。

        TickFlow 无独立 tick 接口（无 te 交易时段标识），
        用 quote 最新价模拟，trading_phase 固定为 0。
        实际交易时段由 session.py 本地时间推断兜底。
        """
        quote = self.fetch_quote(code)
        if not quote:
            return None
        return {
            "latest": quote["latest"],
            "volume": quote["volume"],
            "timestamp": quote["timestamp"],
            "trading_phase": 0,
        }

    # ── fetch_future_quote（ETF 替代方案） ──

    # 国际期货 → 对应 ETF 映射
    _FUTURE_ETF_MAP = {
        "NQ": "QQQ",    # 纳斯达克 100 ETF
        "ES": "SPY",    # 标普 500 ETF
    }

    def fetch_future_quote(self, region: str, code: str) -> dict | None:
        """
        期货实时报价 → ETF 替代。

        NQ/ES 国际期货用 QQQ/SPY ETF 盘前价格替代，
        对盘前分析预测准确度影响 ≈0。
        """
        etf_code = self._FUTURE_ETF_MAP.get(code, code)
        logger.info(f"TickFlow 期货报价→ETF: {region}:{code} → {etf_code}")
        return self.fetch_quote(etf_code)

    def fetch_future_kline(self, region: str, code: str,
                           kType: int = 2, limit: int = 12) -> list[dict]:
        """
        期货分钟 K 线 → 不再使用，返回空列表。

        盘前分析中 NQ/ES 5 分钟 K 线走势形态占 futures_score 权重仅 30%，
        去掉后影响微乎其微。
        """
        logger.info(f"TickFlow 不获取期货分钟K线 ({region}:{code})，"
                     f"futures_score 纯靠涨跌方向")
        return []


def get_stock_fetcher(market: str = "US") -> BaseStockFetcher:
    """
    根据市场选择数据源，统一使用 TickFlow。

    - 有 stock_token_us / stock_token_a → TickFlow 完整服务（日K + 实时行情）
    - 无 token → TickFlow 免费服务（仅日K线）

    如需实时行情，请在 https://tickflow.org 注册获取 API Key，
    填入设置页对应市场的「数据源 Token」。

    Args:
        market: "US" / "A"

    Returns:
        TickFlowFetcher 实例
    """
    from config.settings import Settings
    settings = Settings()

    if market == "US":
        api_key = settings.get("stock_token_us", "")
    else:
        api_key = settings.get("stock_token_a", "")

    label = "完整服务" if api_key else "免费服务"
    logger.info(f"{'美股' if market == 'US' else 'A股'}数据源: TickFlow {label}")
    return TickFlowFetcher(api_key=api_key)
