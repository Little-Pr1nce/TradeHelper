"""
股票数据获取模块

统一股票行情数据源：
  - TickFlowFetcher: 第一且唯一的股市信息/K线/盘中报价来源
  - Nasdaq.com API: 美股盘前/盘后延伸交易时段价格（首选，免费无 Key）
  - yfinance: 美股盘前/盘后降级方案（Nasdaq 不可用时自动切换）

新闻源不在本模块处理：A 股使用 akshare，美股使用 Finnhub。
数据源由 get_stock_fetcher(market) 自动选择。
"""

import logging
import os
import re
import sys
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

def _detect_system_proxy() -> str:
    """跨平台系统 HTTP 代理检测。"""
    if sys.platform == "darwin":
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

    elif sys.platform == "win32":
        for env_key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            val = os.environ.get(env_key, "").strip()
            if val:
                return val
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            ) as key:
                proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if proxy_enable:
                    proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    if proxy_server:
                        return f"http://{proxy_server}"
        except Exception:
            pass
        return ""

    else:
        for env_key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            val = os.environ.get(env_key, "").strip()
            if val:
                return val
        return ""


def _resolve_proxy_url() -> str:
    """优先 Settings，否则回退系统 HTTP 代理检测。"""
    from config.settings import Settings
    configured = (Settings().get("proxy", "") or "").strip()
    if configured:
        return configured
    detected = _detect_system_proxy()
    if detected:
        logger.debug(f"Using system HTTP proxy: {detected}")
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

    来源：Settings.proxy → 系统 HTTP 代理检测。
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



def fetch_nasdaq_extended_quote(code: str) -> dict | None:
    """从 Nasdaq.com 公开 API 获取美股盘前/盘后延伸时段价格。

    免费、无需 API Key，覆盖 NASDAQ + NYSE 全量美股。
    primaryData = 当前最新报价（盘前/盘中/盘后），isRealTime 标识是否实时。
    secondaryData = 上一交易日收盘数据（prev_close 来源）。
    """
    try:
        import urllib.request
        import json

        url = f"https://api.nasdaq.com/api/quote/{code.upper()}/info?assetclass=stocks"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data.get("data"):
            logger.warning(f"Nasdaq.com 返回空数据 ({code})")
            return None

        inner = data["data"]
        primary = inner.get("primaryData") or {}
        secondary = inner.get("secondaryData") or {}

        # 最新价格
        price_str = primary.get("lastSalePrice", "")
        if not price_str or price_str == "NA":
            logger.warning(f"Nasdaq.com 无有效价格 ({code}): lastSalePrice={price_str}")
            return None
        price = float(price_str.replace("$", "").replace(",", ""))

        # 前收盘价（取 secondaryData，即上一交易日常规时段收盘价）
        prev_close = 0.0
        prev_str = secondary.get("lastSalePrice", "")
        if prev_str and prev_str.startswith("$"):
            prev_close = float(prev_str.replace("$", "").replace(",", ""))

        # 涨跌
        change = price - prev_close if prev_close > 0 else 0.0
        change_pct = round(change / prev_close, 6) if prev_close > 0 else 0.0

        # 成交量（盘前/盘后也有成交量）
        vol_str = primary.get("volume", "0")
        volume = 0
        try:
            volume = int(float(vol_str.replace(",", "")))
        except (ValueError, TypeError):
            pass

        # 时间戳：解析 lastTradeTimestamp 例如 "Jun 22, 2026 4:19 AM ET"
        ts = 0
        ts_str = primary.get("lastTradeTimestamp", "")
        if ts_str:
            try:
                from datetime import datetime as _dt
                # 去掉时区后缀 " ET"
                ts_clean = ts_str.replace(" ET", "").replace("Closed at ", "")
                parsed = _dt.strptime(ts_clean, "%b %d, %Y %I:%M %p")
                ts = int(parsed.timestamp() * 1000)
            except Exception:
                pass

        market_status = inner.get("marketStatus", "Unknown")

        logger.info(
            f"Nasdaq.com 延伸时段 ({code}): price={price:.2f}, "
            f"prev_close={prev_close:.2f}, change_pct={change_pct:.4%}, "
            f"实时={primary.get('isRealTime', False)}, 时段={market_status}"
        )

        return {
            "code": code.upper(),
            "latest": round(price, 2),
            "price": round(price, 2),
            # Nasdaq /info 不提供盘前盘后的 open/high/low，填 0
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "prev_close": round(prev_close, 2),
            "change": round(change, 2),
            "change_pct": change_pct,
            "volume": volume,
            "amount": 0,
            "timestamp": ts,
            "status": 0,
            "vwap": 0,
        }
    except Exception as e:
        logger.warning(f"Nasdaq.com 延伸时段获取失败 ({code}): {e}")
        return None


def _fetch_yfinance_extended_quote(code: str) -> dict | None:
    """用 yfinance 获取美股盘前/盘后延伸时段价格（降级方案）。

    使用 period=\"5d\" 而非 \"1d\"，以兼容凌晨时段（美东 00:00-04:00）
    「今天」尚未开盘导致 yfinance 返回空 DataFrame 的边界情况。
    """
    try:
        import yfinance as yf

        _apply_proxy()
        ticker = yf.Ticker(code.upper())
        hist = ticker.history(period="5d", interval="1m", prepost=True)
        if hist is None or hist.empty:
            logger.debug(f"yfinance 无延伸时段数据 ({code}): DataFrame 为空")
            return None

        # 价格取最新 bar（含无成交量的盘前/盘后报价）
        price_bar = hist.iloc[-1]
        price = float(price_bar["Close"])

        # 确定当日数据范围
        latest_day = price_bar.name.date() if hasattr(price_bar.name, "date") else None
        if latest_day:
            day_mask = hist.index.date == latest_day
            day_hist = hist[day_mask]
        else:
            day_hist = hist

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
        ts = int(price_bar.name.timestamp() * 1000) if hasattr(price_bar, "name") else 0

        logger.info(
            f"yfinance 延伸时段 ({code}): price={price:.2f}, "
            f"prev_close={prev_close:.2f}, change_pct={change_pct:.4%}"
        )

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
        logger.warning(f"yfinance 延伸时段获取失败 ({code}): {e}")
        return None


def fetch_us_extended_quote(code: str) -> dict | None:
    """获取美股盘前/盘后延伸时段价格。

    策略：Nasdaq.com 公开 API 优先（免费、无需 Key、覆盖 NASDAQ+NYSE），
    不可用时自动降级到 yfinance。
    """
    # 优先 Nasdaq.com
    result = fetch_nasdaq_extended_quote(code)
    if result is not None:
        return result

    # 降级 yfinance
    logger.info(f"Nasdaq.com 不可用，降级到 yfinance ({code})")
    return _fetch_yfinance_extended_quote(code)

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
        self._quote_cache: dict[str, tuple[float, dict]] = {}  # (timestamp, data)
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

        # 30 秒内同一代码直接返回缓存（避免 fetch_stock_tick + fetch_quote 双重调用）
        now_ts = time.time()
        if code in self._quote_cache:
            cached_ts, cached_data = self._quote_cache[code]
            if now_ts - cached_ts < 30:
                return cached_data

        try:
            symbol = self._to_symbol(code)
            quotes = self._tf.quotes.get(symbols=[symbol])
            if not quotes:
                return None
            q = quotes[0]

            latest = float(q.get("last_price", 0))
            prev_close = float(q.get("prev_close", 0))
            own_chp = (latest - prev_close) / prev_close if latest and prev_close else 0.0

            result = {
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
            self._quote_cache[code] = (time.time(), result)
            return result
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


def check_extended_quote_available(code: str = "AAPL") -> dict:
    """检测美股延伸时段接口是否可用（Nasdaq.com 优先 → yfinance 降级）。"""
    try:
        quote = fetch_us_extended_quote(code)
        return {
            "source": "Nasdaq.com (yfinance fallback)",
            "ok": quote is not None and quote.get("latest", 0) > 0,
            "extended_quote_ok": quote is not None,
            "error": "",
        }
    except Exception as e:
        return {"source": "Nasdaq.com (yfinance fallback)", "ok": False, "error": str(e)}

def fetch_cached_prices(
    code: str, market: str, start: str, end: str,
    db=None, min_records: int = 0,
) -> "pd.DataFrame | None":
    """缓存优先的 K 线获取：DB 缓存 → 增量拉取 → 全量拉取降级。

    与 Tab1 _fetch_prices / Tab3 analyze_portfolio 原来的逻辑完全一致。

    Args:
        code: 股票代码
        market: "US" / "A"
        start: 起始日期
        end: 结束日期
        db: Database 实例（必传）
        min_records: 最少需要的 K 线条数（不足则返回 None）

    Returns:
        排序好的 DataFrame（含 date 列为 datetime），数据不足则返回 None
    """
    import pandas as pd
    from datetime import date as dt_date, timedelta
    from data.database import Database

    if db is None:
        db = Database()

    prices = db.get_prices(code, start, end)
    fetcher = get_stock_fetcher(market)

    if not prices:
        logger.info(f"{code} 缓存为空，联网拉取 {start}~{end}")
        new_prices = fetcher.fetch_price_history(code, start, end)
        if new_prices:
            db.insert_prices(new_prices)
        prices = db.get_prices(code, start, end)
    else:
        last_date = prices[-1].date
        logger.info(f"{code} 缓存: {len(prices)} 条 ({prices[0].date}~{last_date})")

        next_day = (dt_date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
        today_str = dt_date.today().isoformat()
        logger.info(f"{code} 检查增量 {next_day}~{today_str}")
        new_prices = fetcher.fetch_price_history(code, next_day, today_str)
        latest_new_date = max((p.date for p in new_prices), default="")
        if latest_new_date > last_date:
            db.insert_prices(new_prices)
            prices = db.get_prices(code, start, end)
            new_count = sum(1 for p in new_prices if p.date > last_date)
            logger.info(f"{code} 增量获取 {new_count} 条新数据（最新 {latest_new_date}）")
        else:
            logger.info(f"{code} 无增量数据（缓存最新 {last_date}，数据源最新 {latest_new_date or '无'}）")

    if not prices or (min_records > 0 and len(prices) < min_records):
        return None

    df = pd.DataFrame([p.to_dict() for p in prices])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


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
