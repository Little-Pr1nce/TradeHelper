"""
日期工具 — 回测周期计算和日期格式化。
"""

from datetime import datetime, date, timedelta


def _format_date(d: date | datetime | str) -> str:
    """将各种日期类型统一格式化为 'YYYY-MM-DD'。"""
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def get_backtest_dates(period: str) -> tuple[str, str]:
    """
    根据回测周期计算起始日期和结束日期。

    支持的周期：
      - "6m": 6 个月（约 180 天）
      - "1y": 1 年（约 365 天）
      - "3y": 3 年（约 1095 天）
    """
    today = date.today()
    periods = {"6m": 180, "1y": 365, "3y": 1095}
    days = periods.get(period, 365)
    start = today - timedelta(days=days)
    return _format_date(start), _format_date(today)
