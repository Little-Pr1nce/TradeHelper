"""
交易时段判断工具。

用于盘中/盘前分析时判断当前交易时段：
  - pre:      盘前交易时段
  - intraday: 常规交易时段（盘中）
  - post:     盘后交易时段
  - closed:   非交易时段 / 休市

判断优先级：
  1. stock_tick 的 trading_phase 字段（如 itick te 字段，最准确）
  2. stock_quote 的 timestamp 结合市场作息推断
  3. 本地当前时间 + 市场作息规则（兜底）
"""

import logging
from datetime import datetime, time
from typing import Optional

logger = logging.getLogger(__name__)

# 美股交易时间（美东）
_US_REGULAR_OPEN = time(9, 30)
_US_REGULAR_CLOSE = time(16, 0)
_US_PRE_OPEN = time(4, 0)     # 美股盘前从美东 4:00 开始
_US_POST_CLOSE = time(20, 0)   # 美股盘后到美东 20:00

# A 股交易时间（北京时间）
_A_REGULAR_OPEN = time(9, 30)
_A_REGULAR_CLOSE = time(15, 0)
# A 股集合竞价 9:15-9:25 可视为盘前


def detect_session(
    market: str,
    stock_tick: Optional[dict] = None,
    stock_quote: Optional[dict] = None,
) -> str:
    """
    判断当前交易时段。

    Args:
        market:      "A" / "US"
        stock_tick:  实时成交数据（含 trading_phase 字段），可选
        stock_quote: 实时报价数据，可选

    Returns:
        "pre" / "intraday" / "post" / "closed"
    """
    # —— 优先级 1：tick 的 te 字段，直接来自交易所 ——
    if stock_tick and stock_tick.get("trading_phase") is not None:
        te = stock_tick["trading_phase"]
        if te == 1:
            return "pre"
        elif te == 2:
            return "post"
        elif te == 0:
            return "intraday"

    # —— 优先级 2：quote 的 timestamp 推断 ——
    if stock_quote and stock_quote.get("timestamp"):
        ts = stock_quote["timestamp"]
        try:
            tick_time = datetime.fromtimestamp(ts / 1000)
            return _infer_by_time(tick_time, market)
        except (OSError, ValueError, OverflowError):
            pass

    # —— 优先级 3：当前本地时间 + 市场作息推断（不准确，仅兜底） ——
    now = datetime.now()
    logger.debug(f"使用本地时间推断交易时段: {now}")
    return _infer_by_time(now, market)


def _infer_by_time(dt: datetime, market: str) -> str:
    """根据时间和市场作息推断交易时段。"""
    if market == "US":
        # 美东时间转北京时间：美东 +12h（夏令时）/ +13h（冬令时）
        # 简化处理：直接按 UTC 时间判断
        t = dt.time()
        # 美股盘前：美东 4:00-9:30 = 北京时间 16:00-21:30（夏令时）或 17:00-22:30（冬令时）
        # 美股盘中：美东 9:30-16:00 = 北京时间 21:30-4:00 次日
        # 美股盘后：美东 16:00-20:00 = 北京时间 4:00-8:00 次日
        # 简单判断：通过本地时间区间（假设北京时间）
        pre_start = time(16, 0)
        pre_end = time(21, 30)
        regular_end = time(4, 0)   # 次日凌晨 4:00
        post_end = time(8, 0)

        if pre_start <= t < pre_end:
            return "pre"
        elif t >= pre_end or t < regular_end:
            return "intraday"
        elif regular_end <= t < post_end:
            return "post"
        else:
            return "closed"

    elif market == "A":
        t = dt.time()
        pre_start = time(9, 15)
        regular_open = time(9, 30)
        lunch_start = time(11, 30)
        lunch_end = time(13, 0)
        regular_close = time(15, 0)

        if pre_start <= t < regular_open:
            return "pre"
        elif (regular_open <= t < lunch_start) or (lunch_end <= t < regular_close):
            return "intraday"
        elif regular_close <= t < time(15, 30):
            return "post"
        else:
            return "closed"

    return "closed"


def session_label(session: str) -> str:
    """返回时段的用户可读标签。"""
    return {
        "pre": "盘前交易",
        "intraday": "常规交易（盘中）",
        "post": "盘后交易",
        "closed": "休市",
    }.get(session, "未知")


def _us_session_label(session: str) -> str:
    return {"pre": "盘前交易", "intraday": "常规交易", "post": "盘后交易", "closed": "休市"}.get(session, "未知")

def _a_session_label(session: str) -> str:
    return {"pre": "集合竞价", "intraday": "盘中交易", "post": "盘后交易", "closed": "休市"}.get(session, "未知")

def _recommended_mode(session: str) -> str:
    """根据时段返回推荐的分析模式。"""
    return {"pre": "pre", "intraday": "intraday", "post": "eod", "closed": "eod"}.get(session, "eod")

def _mode_tip(session: str) -> str:
    return {
        "pre": "适合跑「盘前预测」",
        "intraday": "适合跑「盘中实时分析」",
        "post": "适合跑「盘后分析」",
        "closed": "适合跑「盘后分析」",
    }.get(session, "")


def get_session_display(market: str) -> dict:
    """
    获取当前交易时段信息及建议，供 UI 展示。

    Returns:
        {
            "market": "A" / "US",
            "session": "pre" / "intraday" / "post" / "closed",
            "label": "盘前交易" / ...,
            "recommended": "pre" / "intraday" / "eod",
            "icon": "🌅" / "⏱" / "🌆" / "🌙",
            "tip": "建议跑...",
        }
    """
    session = detect_session(market)
    if market == "US":
        label = _us_session_label(session)
    else:
        label = _a_session_label(session)

    icon = {"pre": "🌅", "intraday": "⏱", "post": "🌆", "closed": "🌙"}.get(session, "❓")
    recommended = _recommended_mode(session)
    tip = _mode_tip(session)

    return {
        "market": market,
        "session": session,
        "label": label,
        "recommended": recommended,
        "icon": icon,
        "tip": tip,
    }


def is_us_pre_market(stock_tick: Optional[dict] = None) -> bool:
    """快速判断是否为美股盘前时段。"""
    if stock_tick:
        return stock_tick.get("trading_phase") == 1
    return detect_session("US") == "pre"


def is_intraday(market: str, stock_tick: Optional[dict] = None) -> bool:
    """快速判断是否在常规交易时段。"""
    return detect_session(market, stock_tick=stock_tick) == "intraday"
