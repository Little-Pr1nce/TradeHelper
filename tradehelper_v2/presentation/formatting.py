"""展示格式化只在最后一公里发生，不参与任何业务计算。"""
from __future__ import annotations

from decimal import Decimal


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
