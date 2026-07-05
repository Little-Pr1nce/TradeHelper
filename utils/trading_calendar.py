"""交易所交易日历，用于在预测生成时冻结明确目标日期。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


class TradingCalendarUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingTargets:
    dates: dict[int, str]
    source: str
    reliable: bool


def forecast_target_dates(
    as_of: str | date | datetime,
    market: str,
    horizons: tuple[int, ...] = (1, 3, 5),
    *,
    allow_weekday_fallback: bool = False,
) -> TradingTargets:
    """返回 as_of 之后第 N 个真实交易日；正式预测禁止静默周末近似。"""
    as_of_date = _as_date(as_of)
    positive = sorted({int(h) for h in horizons if int(h) > 0})
    if not positive:
        return TradingTargets({}, "none", True)

    try:
        import exchange_calendars as xcals

        calendar_name = "XSHG" if str(market).upper() == "A" else "XNYS"
        calendar = xcals.get_calendar(calendar_name)
        start = as_of_date + timedelta(days=1)
        end = as_of_date + timedelta(days=max(positive) * 4 + 20)
        sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
        dates = [timestamp.date().isoformat() for timestamp in sessions]
        if len(dates) < max(positive):
            raise TradingCalendarUnavailable("交易日历返回的未来交易日不足")
        return TradingTargets(
            {h: dates[h - 1] for h in positive},
            f"exchange_calendars:{calendar_name}",
            True,
        )
    except Exception as exc:
        if not allow_weekday_fallback:
            raise TradingCalendarUnavailable(
                "缺少可靠交易日历，正式预测未生成；请安装 exchange-calendars"
            ) from exc

    dates = []
    cursor = as_of_date
    while len(dates) < max(positive):
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
    return TradingTargets(
        {h: dates[h - 1] for h in positive},
        "weekday_fallback",
        False,
    )


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    if not text:
        raise ValueError("as_of不能为空")
    return datetime.fromisoformat(text).date()
