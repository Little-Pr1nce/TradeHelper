"""
股票数据获取模块

统一股票行情数据源：
  - TickFlowFetcher: 第一且唯一的股市信息/K线/盘中报价来源
  - yfinance: 仅用于美股盘前/盘后延伸交易时段价格补充

新闻源不在本模块处理：A 股使用 akshare，美股使用 Finnhub。
数据源由 get_stock_fetcher(market) 自动选择。
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



def fetch_us_extended_quote(code: str) -> dict | None:
    """用 yfinance 获取美股盘前/盘后延伸时段价格。

    使用 period=\"5d\" 而非 \"1d\"，以兼容凌晨时段（美东 00:00-04:00）
    「今天」尚未开盘导致 yfinance 返回空 DataFrame 的边界情况。
    """
    try:
        import yfinance as yf

        _apply_proxy()
        ticker = yf.Ticker(code.upper())
        hist = ticker.history(period="5d", interval="1m", prepost=True)
        if hist is None or hist.empty:
            return None

        # 取最近一个有数据的交易日的 1 分钟 bar
        hist_with_vol = hist[hist["Volume"] > 0]
        if hist_with_vol.empty:
            return None

        latest_bar = hist_with_vol.iloc[-1]
        latest_day = latest_bar.name.date() if hasattr(latest_bar.name, "date") else None
        if latest_day:
            day_mask = hist.index.date == latest_day
            day_hist = hist[day_mask]
        else:
            day_hist = hist

        price = float(latest_bar["Close"])
        volume = int(day_hist["Volume"].sum())
        day_open = float(day_hist.iloc[0]["Open"])
        day_high = float(day_hist["High"].max())
        day_low = float(day_hist["Low"].min())

        # 前收盘：最近有数据的那天之前一个交易日的收盘价
        prev_close = 0.0
        try:
            prev_hist = ticker.history(period="5d")
            if prev_hist is not None and not prev_hist.empty and len(prev_hist) >= 2:
                prev_close = float(prev_hist.iloc[-2]["Close"])
        except Exception:
            pass

        change_pct = (price - prev_close) / prev_close if prev_close > 0 else 0.0
        ts = int(latest_bar.name.timestamp() * 1000) if hasattr(latest_bar, "name") else 0
        return {
            "code": code.upper(),
            "latest": round(price, 2),
            "price": round(price, 2),
            "open": round(day_open, 2),
            "high": round(day_high, 2),
            "low": round(day_low, 2),
            "prev_close": round(prev_close, 2),
            "change": round(price - prev_close, 2) if prev_close > 0 else 0.0,
            "change_pct": round(change_pct, 6),
            "volume": volume,
            "amount": 0,
            "timestamp": ts,
            "status": 0,
            "vwap": 0,
        }
    except Exception as e:
        logger.warning(f"yfinance 延伸时段数据获取失败 ({code}): {e}")
        return None

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

        Returns 标准报价格式：
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
        用 quote 最新价模拟。
        不设置 trading_phase，交由 detect_session() 用 timestamp
        或本地时间推断实际交易时段。
        """
        quote = self.fetch_quote(code)
        if not quote:
            return None
        return {
            "latest": quote["latest"],
            "volume": quote["volume"],
            "timestamp": quote["timestamp"],
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



def check_tickflow_available(market: str = "US", code: str | None = None) -> dict:
    """检测 TickFlow K线/实时接口是否可用，返回结构化状态。"""
    code = code or ("AAPL" if market == "US" else "600519")
    try:
        fetcher = get_stock_fetcher(market)
        info = fetcher.fetch_stock_info(code)
        prices = fetcher.fetch_price_history(code, "2024-01-01", "2099-12-31")
        quote = fetcher.fetch_quote(code) if getattr(fetcher, "has_realtime", False) else None
        return {
            "source": "TickFlow",
            "ok": bool(info or prices),
            "info_ok": info is not None,
            "history_ok": bool(prices),
            "realtime_ok": quote is not None,
            "has_realtime": getattr(fetcher, "has_realtime", False),
            "error": "",
        }
    except Exception as e:
        return {"source": "TickFlow", "ok": False, "error": str(e)}


def check_yfinance_available(code: str = "AAPL") -> dict:
    """检测 yfinance 美股延伸时段接口是否可用。"""
    try:
        quote = fetch_us_extended_quote(code)
        return {
            "source": "yfinance",
            "ok": quote is not None and quote.get("latest", 0) > 0,
            "extended_quote_ok": quote is not None,
            "error": "",
        }
    except Exception as e:
        return {"source": "yfinance", "ok": False, "error": str(e)}

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
