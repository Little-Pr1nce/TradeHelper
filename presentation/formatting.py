"""展示格式化只在最后一公里发生，不参与任何业务计算。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_BEIJING = timezone(timedelta(hours=8))


def format_datetime(value: datetime | None, market: object | None = None, *, seconds: bool = False) -> str:
    """Render a UTC contract timestamp in an explicit user-facing market zone."""
    if value is None:
        return "未设置"
    market_value = getattr(market, "value", market)
    if market_value == "US":
        try:
            zone = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            zone = timezone(timedelta(hours=-5))
        label = "美东时间"
    else:
        zone = _BEIJING
        label = "北京时间"
    pattern = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return f"{value.astimezone(zone).strftime(pattern)} {label}"


def format_percent(value: object | None, *, signed: bool=False) -> str:
    if value is None:return "暂无可靠数据"
    numeric=Decimal(str(value))*Decimal("100")
    prefix="+" if signed and numeric>0 else ""
    return f"{prefix}{numeric.quantize(Decimal('0.1'))}%"


def format_money(value: object | None, currency: str) -> str:
    if value is None:return "暂无可靠数据"
    symbol="¥" if currency=="CNY" else "$"
    return f"{symbol}{Decimal(str(value)).quantize(Decimal('0.01')):,.2f}"


def format_value(value: object | None) -> str:
    if value is None:return "暂无可靠数据"
    if isinstance(value,bool):return "是" if value else "否"
    return str(value)
