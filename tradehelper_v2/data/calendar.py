"""交易所 session 服务与确定性测试日历；预测目标日绝不退化为自然日。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from tradehelper_v2.contracts.enums import Market


class TradingCalendarUnavailable(RuntimeError):
    pass


class TradingCalendar(Protocol):
    def is_session(self, market: Market, value: date) -> bool: ...

    def target_dates(self, market: Market, as_of: date, horizons: tuple[int, ...]) -> dict[int, date]: ...

    def latest_completed_session(self, market: Market, as_of: datetime) -> date: ...


@dataclass(frozen=True, slots=True)
class StaticTradingCalendar:
    sessions: tuple[date, ...]
    completed_sessions: tuple[date, ...] | None = None

    def __post_init__(self) -> None:
        ordered = tuple(sorted(set(self.sessions)))
        object.__setattr__(self, "sessions", ordered)
        if self.completed_sessions is not None:
            object.__setattr__(self, "completed_sessions", tuple(sorted(set(self.completed_sessions))))

    def is_session(self, market: Market, value: date) -> bool:
        return value in self.sessions

    def target_dates(self, market: Market, as_of: date, horizons: tuple[int, ...]) -> dict[int, date]:
        requested = sorted({int(horizon) for horizon in horizons if int(horizon) > 0})
        future = [session for session in self.sessions if session > as_of]
        if not requested or len(future) < max(requested):
            raise TradingCalendarUnavailable("injected calendar has insufficient future sessions")
        return {horizon: future[horizon - 1] for horizon in requested}

    def latest_completed_session(self, market: Market, as_of: datetime) -> date:
        completed = self.completed_sessions if self.completed_sessions is not None else self.sessions
        candidates = [session for session in completed if session <= as_of.date()]
        if not candidates:
            raise TradingCalendarUnavailable("injected calendar has no completed session")
        return candidates[-1]


class ExchangeTradingCalendar:
    """Lazy exchange-calendars adapter used outside deterministic tests."""

    def _calendar(self, market: Market):
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise TradingCalendarUnavailable("exchange-calendars is unavailable") from exc
        return xcals.get_calendar("XSHG" if market is Market.A else "XNYS")

    @staticmethod
    def _zone(market: Market) -> ZoneInfo:
        return ZoneInfo("Asia/Shanghai" if market is Market.A else "America/New_York")

    def is_session(self, market: Market, value: date) -> bool:
        try:
            return bool(self._calendar(market).is_session(value.isoformat()))
        except Exception as exc:
            raise TradingCalendarUnavailable("unable to verify exchange session") from exc

    def target_dates(self, market: Market, as_of: date, horizons: tuple[int, ...]) -> dict[int, date]:
        requested = sorted({int(horizon) for horizon in horizons if int(horizon) > 0})
        if not requested:
            return {}
        try:
            calendar = self._calendar(market)
            end = as_of + timedelta(days=max(requested) * 4 + 20)
            sessions = calendar.sessions_in_range((as_of + timedelta(days=1)).isoformat(), end.isoformat())
            dates = [item.date() for item in sessions]
            if len(dates) < max(requested):
                raise TradingCalendarUnavailable("future exchange sessions are insufficient")
            return {horizon: dates[horizon - 1] for horizon in requested}
        except TradingCalendarUnavailable:
            raise
        except Exception as exc:
            raise TradingCalendarUnavailable("unable to calculate forecast target dates") from exc

    def latest_completed_session(self, market: Market, as_of: datetime) -> date:
        if as_of.tzinfo is None:
            raise TradingCalendarUnavailable("as_of must be timezone-aware")
        try:
            calendar = self._calendar(market)
            zone = self._zone(market)
            local_now = as_of.astimezone(zone)
            sessions = calendar.sessions_in_range((local_now.date() - timedelta(days=14)).isoformat(), local_now.date().isoformat())
            for session in reversed(sessions):
                close = calendar.session_close(session).to_pydatetime()
                close_utc = close.replace(tzinfo=timezone.utc) if close.tzinfo is None else close.astimezone(timezone.utc)
                if close_utc <= as_of.astimezone(timezone.utc):
                    return session.date()
        except Exception as exc:
            if isinstance(exc, TradingCalendarUnavailable):
                raise
            raise TradingCalendarUnavailable("unable to calculate latest completed session") from exc
        raise TradingCalendarUnavailable("no completed exchange session was found")
