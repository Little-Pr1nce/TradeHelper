"""交易所 session 服务与确定性测试日历；预测目标日绝不退化为自然日。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from tradehelper_v2.contracts.enums import Exchange, Market
from tradehelper_v2.contracts.scenario import DecisionSession


class TradingCalendarUnavailable(RuntimeError):
    pass


class TradingCalendar(Protocol):
    def is_session(self, market: Market, value: date) -> bool: ...

    def target_dates(self, market: Market, as_of: date, horizons: tuple[int, ...]) -> dict[int, date]: ...

    def latest_completed_session(self, market: Market, as_of: datetime) -> date: ...
    def session_window(self, market: Market, exchange: Exchange, session_date: date) -> DecisionSession: ...
    def next_session(self, market: Market, exchange: Exchange, after_date: date) -> date: ...
    def session_containing(self, market: Market, exchange: Exchange, as_of: datetime) -> DecisionSession | None: ...


@dataclass(frozen=True, slots=True)
class StaticTradingCalendar:
    sessions: tuple[date, ...]
    completed_sessions: tuple[date, ...] | None = None
    windows: dict[tuple[Market, Exchange, date], DecisionSession] | None = None

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

    def session_window(self, market: Market, exchange: Exchange, session_date: date) -> DecisionSession:
        if self.windows and (market, exchange, session_date) in self.windows:return self.windows[(market,exchange,session_date)]
        raise TradingCalendarUnavailable("injected calendar has no explicit session window")
    def next_session(self, market: Market, exchange: Exchange, after_date: date) -> date:
        values=[item for item in self.sessions if item>after_date]
        if not values: raise TradingCalendarUnavailable("injected calendar has no next session")
        return values[0]
    def session_containing(self, market: Market, exchange: Exchange, as_of: datetime) -> DecisionSession | None:
        if as_of.tzinfo is None:
            raise TradingCalendarUnavailable("as_of must be timezone-aware")
        zone = ZoneInfo("Asia/Shanghai" if market is Market.A else "America/New_York")
        local_date = as_of.astimezone(zone).date()
        if local_date not in self.sessions:
            return None
        window=self.session_window(market,exchange,local_date)
        return window if window.regular_open<=as_of<=window.regular_close else None


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

    def _exchange(self, exchange: Exchange) -> str:
        # exchange_calendars exposes the mainland session calendar as XSHG.
        # Shenzhen and Beijing use the same trading-day/session window for the
        # decisions modeled here; instrument market rules remain exchange-specific.
        if exchange in {Exchange.XSHG, Exchange.XSHE, Exchange.XBSE}: return "XSHG"
        if exchange in {Exchange.XNYS, Exchange.XNAS, Exchange.UNKNOWN}: return "XNYS"
        raise TradingCalendarUnavailable("unknown exchange has no session calendar")

    def session_window(self, market: Market, exchange: Exchange, session_date: date) -> DecisionSession:
        """从交易所日历读取真实开收盘和午间 break，覆盖半日市/DST。"""
        try:
            import pandas as pd
            calendar = self._calendar(market) if self._exchange(exchange) == ("XSHG" if market is Market.A else "XNYS") else __import__("exchange_calendars").get_calendar(self._exchange(exchange))
            label = session_date.isoformat()
            if not calendar.is_session(label): raise TradingCalendarUnavailable("requested date is not a session")
            opened = calendar.session_open(label).to_pydatetime(); closed = calendar.session_close(label).to_pydatetime()
            start = calendar.session_break_start(label); end = calendar.session_break_end(label)
            breaks = () if pd.isna(start) or pd.isna(end) else ((start.to_pydatetime(), end.to_pydatetime()),)
            return DecisionSession(market, exchange, session_date, opened, closed, breaks, f"exchange_calendars:{self._exchange(exchange)}")
        except TradingCalendarUnavailable: raise
        except Exception as exc: raise TradingCalendarUnavailable("unable to load exchange session window") from exc

    def next_session(self, market: Market, exchange: Exchange, after_date: date) -> date:
        try:
            calendar = __import__("exchange_calendars").get_calendar(self._exchange(exchange))
            sessions = calendar.sessions_in_range((after_date + timedelta(days=1)).isoformat(), (after_date + timedelta(days=14)).isoformat())
            if len(sessions) == 0: raise TradingCalendarUnavailable("no next session")
            return sessions[0].date()
        except TradingCalendarUnavailable: raise
        except Exception as exc: raise TradingCalendarUnavailable("unable to calculate next session") from exc

    def session_containing(self, market: Market, exchange: Exchange, as_of: datetime) -> DecisionSession | None:
        if as_of.tzinfo is None: raise TradingCalendarUnavailable("as_of must be timezone-aware")
        local_date = as_of.astimezone(self._zone(market)).date()
        if not self.is_session(market, local_date):
            return None
        window = self.session_window(market, exchange, local_date)
        return window if window.regular_open <= as_of.astimezone(timezone.utc) <= window.regular_close else None
