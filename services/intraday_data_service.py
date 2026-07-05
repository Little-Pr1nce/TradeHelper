"""Prospective minute-bar evidence for intraday trade-plan verification.

Minute bars are stored separately from official daily history.  Provider
failures never synthesize bars from quotes or daily high/low values.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from config.settings import Settings
from data.database import Database
from data.models import IntradayBar


logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="minute-evidence")
_inflight: set[tuple[str, str]] = set()
_lock = threading.Lock()
_tickflow_minute_allowed: dict[str, bool | None] = {"US": None, "A": None}


def schedule_intraday_capture(codes: list[str], market: str) -> bool:
    """Capture recent minute bars in the background and verify due plans."""
    normalized = sorted({str(code).upper() for code in codes if code})
    if not normalized:
        return False
    key = (market, ",".join(normalized))
    with _lock:
        if key in _inflight:
            return False
        _inflight.add(key)

    def _run():
        try:
            db = Database()
            for code in normalized:
                result = capture_intraday_evidence(code, market, db=db)
                logger.info(
                    "分钟K前瞻采集 %s: source=%s bars=%d warning=%s",
                    code, result["source"], result["inserted"], result["warning"],
                )
                db.verify_due_intraday_trade_plans(code=code)
        except Exception as exc:
            logger.warning(f"分钟K前瞻采集失败: {exc}", exc_info=True)
        finally:
            with _lock:
                _inflight.discard(key)

    _executor.submit(_run)
    return True


def capture_intraday_evidence(
    code: str,
    market: str,
    *,
    db: Database | None = None,
    lookback_days: int = 5,
) -> dict:
    """Fetch recent minute bars from the best available source and persist them."""
    bars, source, warning = fetch_recent_intraday_bars(
        code, market, lookback_days=lookback_days,
    )
    inserted = (db or Database()).insert_intraday_bars(bars)
    return {
        "code": code.upper(), "market": market, "source": source,
        "fetched": len(bars), "inserted": inserted, "warning": warning,
    }


def fetch_recent_intraday_bars(
    code: str, market: str, *, lookback_days: int = 5,
) -> tuple[list[IntradayBar], str, str]:
    """Use TickFlow when licensed, then a clearly-labelled supplemental source."""
    market = str(market).upper()
    code = code.upper()
    bars, warning = _fetch_tickflow_minutes(code, market, lookback_days)
    if bars:
        return bars, "tickflow", warning
    if market == "US":
        fallback = _fetch_yfinance_minutes(code)
        return fallback, "yfinance", warning or "TickFlow历史分钟K不可用，使用近期补充源"
    fallback = _fetch_akshare_minutes(code, lookback_days)
    return fallback, "akshare_eastmoney", warning or "TickFlow历史分钟K不可用，使用近期补充源"


def _fetch_tickflow_minutes(
    code: str, market: str, lookback_days: int,
) -> tuple[list[IntradayBar], str]:
    allowed = _tickflow_minute_allowed.get(market)
    if allowed is False:
        return [], "当前TickFlow令牌无历史分钟K权限"
    token_key = "stock_token_us" if market == "US" else "stock_token_a"
    token = str(Settings().get(token_key, "") or "").strip()
    if not token:
        return [], "未配置TickFlow令牌"
    try:
        from data.stock_fetcher import TickFlowFetcher

        fetcher = TickFlowFetcher(token)
        tz = _market_timezone(market)
        end = datetime.now(tz)
        start = end - timedelta(days=max(int(lookback_days), 1) + 2)
        frame = fetcher._tf.klines.get(
            fetcher._to_symbol(code), period="1m", count=10000,
            start_time=int(start.timestamp() * 1000),
            end_time=int(end.timestamp() * 1000), as_dataframe=True,
        )
        bars = normalize_intraday_frame(
            frame, code=code, market=market, source="tickflow",
            quality_status="provider",
        )
        _tickflow_minute_allowed[market] = True
        return bars, ""
    except Exception as exc:
        if "权限" in str(exc) or exc.__class__.__name__ == "PermissionError":
            _tickflow_minute_allowed[market] = False
            return [], "当前TickFlow令牌无历史分钟K权限"
        return [], f"TickFlow分钟K暂时失败: {type(exc).__name__}"


def _fetch_yfinance_minutes(code: str) -> list[IntradayBar]:
    try:
        import yfinance as yf
        from data.stock_fetcher import _apply_proxy

        _apply_proxy()
        frame = yf.Ticker(code).history(period="5d", interval="1m", prepost=False)
        return normalize_intraday_frame(
            frame, code=code, market="US", source="yfinance",
            quality_status="supplemental",
        )
    except Exception as exc:
        logger.warning(f"yfinance分钟K获取失败 ({code}): {exc}")
        return []


def _fetch_akshare_minutes(code: str, lookback_days: int) -> list[IntradayBar]:
    try:
        import akshare as ak

        tz = _market_timezone("A")
        end = datetime.now(tz)
        start = end - timedelta(days=max(int(lookback_days), 1) + 3)
        frame = ak.stock_zh_a_hist_min_em(
            symbol=code,
            start_date=start.strftime("%Y-%m-%d 09:30:00"),
            end_date=end.strftime("%Y-%m-%d 15:00:00"),
            period="1", adjust="",
        )
        return normalize_intraday_frame(
            frame, code=code, market="A", source="akshare_eastmoney",
            quality_status="supplemental",
        )
    except Exception as exc:
        logger.warning(f"AKShare分钟K获取失败 ({code}): {exc}")
        return []


def normalize_intraday_frame(
    frame: pd.DataFrame | None,
    *,
    code: str,
    market: str,
    source: str,
    quality_status: str,
) -> list[IntradayBar]:
    """Normalize provider frames, enforce regular sessions and OHLC validity."""
    if frame is None or frame.empty:
        return []
    work = frame.copy()
    chinese = {
        "时间": "bar_time", "日期": "bar_time", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
    }
    work = work.rename(columns={key: value for key, value in chinese.items() if key in work.columns})
    work = work.rename(columns={
        key: key.lower() for key in work.columns
        if str(key).lower() in {"open", "high", "low", "close", "volume"}
    })
    if "bar_time" in work.columns:
        timestamps = pd.to_datetime(work.pop("bar_time"), errors="coerce")
    else:
        timestamps = pd.to_datetime(work.index, errors="coerce")
    tz = _market_timezone(market)
    try:
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
        else:
            timestamps = timestamps.dt.tz_convert(tz)
    except AttributeError:
        index = pd.DatetimeIndex(timestamps)
        timestamps = (
            index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
            if index.tz is None else index.tz_convert(tz)
        )
    # Series may retain a provider-specific index; positional iteration below
    # must stay aligned with work.iterrows().
    timestamps = pd.DatetimeIndex(timestamps)
    fetched_at = datetime.now().astimezone().isoformat()
    bars = []
    for position, (_, row) in enumerate(work.iterrows()):
        try:
            timestamp = timestamps[position]
            if pd.isna(timestamp) or not _in_regular_session(timestamp, market):
                continue
            values = {
                name: float(row[name]) for name in ("open", "high", "low", "close")
            }
            if (
                min(values.values()) <= 0
                or values["high"] < max(values["open"], values["low"], values["close"])
                or values["low"] > min(values["open"], values["high"], values["close"])
            ):
                continue
            volume = float(row.get("volume", 0.0) or 0.0)
            bars.append(IntradayBar(
                code=code.upper(), market=market,
                timestamp_ms=int(timestamp.timestamp() * 1000),
                session_date=timestamp.date().isoformat(),
                open=values["open"], high=values["high"], low=values["low"],
                close=values["close"], volume=max(volume, 0.0), source=source,
                fetched_at=fetched_at, quality_status=quality_status,
            ))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return bars


def _market_timezone(market: str) -> ZoneInfo:
    return ZoneInfo("Asia/Shanghai" if str(market).upper() == "A" else "America/New_York")


def _in_regular_session(timestamp, market: str) -> bool:
    minutes = timestamp.hour * 60 + timestamp.minute
    if str(market).upper() == "A":
        return (570 <= minutes <= 690) or (780 <= minutes <= 900)
    return 570 <= minutes <= 960
