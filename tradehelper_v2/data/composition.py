"""Production composition for the V2 data boundary.

This module is the only V2-1 location that knows provider URLs, SDKs and
credentials.  It imports no V1 module and never writes raw provider payloads to
the V2 database.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from threading import BoundedSemaphore
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.contracts.enums import Market

from .cache import DataCache
from .calendar import ExchangeTradingCalendar
from .rate_limit import SQLiteDailyRateBudget, SQLiteFinnhubRateBudget, SQLiteQuoteRateBudget
from .repository import SQLiteRepository
from .service import DataProviders, DataRefreshService
from .providers.adapters import AkshareAdapter, BaostockAdapter, FinnhubAdapter, FundamentalAdapter, NasdaqAdapter, TickFlowAdapter, YFinanceAdapter


_UTC = timezone.utc
_NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (compatible; TradeHelperV2/1.0)",
}


def utc_now() -> datetime:
    return datetime.now(_UTC)


def _records(frame: Any) -> list[dict[str, Any]]:
    """Normalize a pandas-like result without making pandas a V2 dependency."""
    if frame is None:
        return []
    if hasattr(frame, "iterrows"):
        rows: list[dict[str, Any]] = []
        for index, row in frame.iterrows():
            value = dict(row)
            value.setdefault("date", getattr(index, "date", lambda: index)().isoformat() if hasattr(index, "date") else str(index))
            rows.append(value)
        return rows
    if isinstance(frame, list):
        return [dict(row) for row in frame if isinstance(row, Mapping)]
    raise TypeError("provider response is not a tabular sequence")


def _a_baostock_code(code: str) -> str:
    return f"sh.{code}" if code.startswith(("5", "6", "9")) else f"sz.{code}"


class _TickFlowTransport:
    def __init__(self, settings: V2Settings) -> None:
        self.settings = settings
        self._clients: dict[Market, Any] = {}

    @staticmethod
    def _market_from_symbol(symbol: str) -> Market:
        return Market.US if symbol.upper().endswith(".US") else Market.A

    def _client(self, market: Market) -> Any:
        client = self._clients.get(market)
        if client is not None:
            return client
        from tickflow import TickFlow

        token = self.settings.stock_token_us if market is Market.US else self.settings.stock_token_a
        client = TickFlow(api_key=token, base_url="https://api.tickflow.org") if token else TickFlow.free()
        self._clients[market] = client
        return client

    def daily(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        market = self._market_from_symbol(symbol)
        # TickFlow's observed daily endpoint treats start_time as an exclusive
        # boundary. Request one calendar day earlier, then let DataRefreshService
        # enforce the exact requested/listing window before persistence.
        start_at = datetime.combine(start - timedelta(days=1), time.min, tzinfo=_UTC)
        end_at = datetime.combine(end, time.max, tzinfo=_UTC)
        days = max((end - start).days + 7, 10)
        frame = self._client(market).klines.get(
            symbol, period="1d", start_time=int(start_at.timestamp() * 1000),
            end_time=int(end_at.timestamp() * 1000), count=min(days * 2, 10_000), as_dataframe=True,
        )
        return _records(frame)

    def quote(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        market = self._market_from_symbol(symbols[0])
        if any(self._market_from_symbol(symbol) is not market for symbol in symbols):
            raise ValueError("TickFlow quote request cannot mix markets")
        response = self._client(market).quotes.get(symbols=symbols)
        return list(response) if isinstance(response, (list, tuple)) else [dict(response)]

    def metadata(self, symbol: str) -> dict[str, Any]:
        rows = self.daily(symbol, date.today() - timedelta(days=14), date.today())
        return rows[-1] if rows else {}


class _NasdaqTransport:
    def __init__(self) -> None:
        self._concurrency = BoundedSemaphore(2)

    def _get(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        import requests

        with self._concurrency:
            response = requests.get(f"https://api.nasdaq.com/api/{path}", params=params, headers=_NASDAQ_HEADERS, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("Nasdaq response must be a JSON object")
        return payload

    def quote(self, code: str) -> Mapping[str, Any]:
        return self._get(f"quote/{code}/info", {"assetclass": "stocks"})

    def daily(self, code: str, start: date, end: date) -> Mapping[str, Any]:
        return self._get(
            f"quote/{code}/historical",
            {"assetclass": "stocks", "fromdate": start.isoformat(), "todate": end.isoformat(), "limit": "5000"},
        )


class _YFinanceTransport:
    def daily(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        import yfinance as yf

        frame = yf.Ticker(code).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=True)
        return _records(frame)

    def quote(self, code: str) -> Mapping[str, Any]:
        import yfinance as yf

        frame = yf.Ticker(code).history(period="5d", interval="1m", prepost=True, auto_adjust=False)
        rows = _records(frame)
        if not rows:
            return {}
        row = rows[-1]
        return {"price": row.get("Close"), "timestamp": row.get("date")}

    def fundamentals(self, code: str) -> Mapping[str, Any]:
        import yfinance as yf

        payload = yf.Ticker(code).get_info()
        return payload if isinstance(payload, Mapping) else {}


class _FinnhubTransport:
    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self._concurrency = BoundedSemaphore(2)

    def _get(self, path: str, params: Mapping[str, str]) -> Any:
        if not self.token:
            raise RuntimeError("Finnhub token is not configured")
        import requests

        with self._concurrency:
            response = requests.get(
                f"https://finnhub.io/api/v1/{path}", params={**params, "token": self.token}, timeout=15
            )
        response.raise_for_status()
        return response.json()

    def profile(self, code: str) -> Mapping[str, Any]:
        payload = self._get("stock/profile2", {"symbol": code})
        return payload if isinstance(payload, Mapping) else {}

    def fundamentals(self, code: str) -> Mapping[str, Any]:
        payload = self._get("stock/metric", {"symbol": code, "metric": "all"})
        return payload if isinstance(payload, Mapping) else {}

    def news(self, code: str) -> list[Mapping[str, Any]]:
        payload = self._get(
            "company-news", {"symbol": code, "from": (date.today() - timedelta(days=30)).isoformat(), "to": date.today().isoformat()}
        )
        return payload if isinstance(payload, list) else []


class _BaostockTransport:
    def _query(self, callback: Callable[[Any], Any]) -> Any:
        import baostock as bs

        login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise RuntimeError(f"baostock login failed: {getattr(login, 'error_msg', '')}")
        try:
            return callback(bs)
        finally:
            bs.logout()

    @staticmethod
    def _first(result: Any) -> dict[str, Any]:
        if getattr(result, "error_code", "1") != "0" or not result.next():
            return {}
        return dict(zip(result.fields, result.get_row_data()))

    @staticmethod
    def _rows(result: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while getattr(result, "error_code", "1") == "0" and result.next():
            rows.append(dict(zip(result.fields, result.get_row_data())))
        return rows

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def metadata(self, code: str) -> dict[str, Any]:
        return self._query(lambda bs: self._first(bs.query_stock_basic(code=_a_baostock_code(code))))

    def listing(self, code: str) -> dict[str, Any]:
        return self.metadata(code)

    def fundamentals(self, code: str) -> dict[str, Any]:
        def callback(bs: Any) -> dict[str, Any]:
            today = date.today()
            valuation_result = bs.query_history_k_data_plus(
                _a_baostock_code(code), "date,peTTM,pbMRQ,psTTM,pcfNcfTTM",
                start_date=(today - timedelta(days=14)).isoformat(), end_date=today.isoformat(), frequency="d", adjustflag="2",
            )
            valuation_rows = self._rows(valuation_result)
            latest_valuation = valuation_rows[-1] if valuation_rows else {}
            fields: dict[str, dict[str, Any]] = {}

            def add(name: str, value: Any, unit: str | None, period_end: date | None) -> None:
                number = self._number(value)
                if number is not None:
                    fields[name] = {
                        "value": number, "unit": unit,
                        "period_end": period_end.isoformat() if period_end else None,
                        "published_at": None, "source": "baostock",
                    }

            valuation_date = date.fromisoformat(latest_valuation["date"]) if latest_valuation.get("date") else None
            for target, source in (("pe_ttm", "peTTM"), ("pb_mrq", "pbMRQ"), ("ps_ttm", "psTTM"), ("pcf_ncf_ttm", "pcfNcfTTM")):
                add(target, latest_valuation.get(source), "multiple", valuation_date)

            symbol = _a_baostock_code(code)
            report_year: int | None = None
            current_profit: dict[str, Any] = {}
            for year in range(today.year, today.year - 9, -1):
                rows = self._rows(bs.query_profit_data(code=symbol, year=year, quarter=4))
                if rows:
                    report_year, current_profit = year, rows[-1]
                    break
            if report_year is not None:
                period_end = date(report_year, 12, 31)
                add("roe", current_profit.get("roeAvg"), "ratio", period_end)
                add("gross_margin", current_profit.get("gpMargin"), "ratio", period_end)
                # MBRRevenue is baostock's main-business/total-revenue-style
                # measure.  It does not reconcile to the issuer's reported
                # operating revenue for every company, so it must not be used
                # to derive the canonical operating-revenue growth feature.
                add("main_business_revenue", current_profit.get("MBRevenue"), "CNY", period_end)
                balance_rows = self._rows(bs.query_balance_data(code=symbol, year=report_year, quarter=4))
                growth_rows = self._rows(bs.query_growth_data(code=symbol, year=report_year, quarter=4))
                if balance_rows:
                    add("debt_ratio", balance_rows[-1].get("liabilityToAsset"), "ratio", period_end)
                if growth_rows:
                    add("net_profit_yoy", growth_rows[-1].get("YOYNI"), "ratio", period_end)
            return {"fields": fields}
        return self._query(callback)


class _AkshareTransport:
    def fundamentals(self, code: str) -> dict[str, Any]:
        import akshare as ak

        if code.isdigit():
            market_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
            frame = ak.stock_financial_analysis_indicator_em(symbol=market_code, indicator="按报告期")
            rows = _records(frame)
            annual_rows = [
                row for row in rows
                if "年报" in str(row.get("REPORT_DATE_NAME") or row.get("REPORT_TYPE") or "")
            ]
            candidates = annual_rows or rows
            if not candidates:
                return {}
            latest = max(candidates, key=lambda row: str(row.get("REPORT_DATE") or "")[:10])
            period_end = str(latest.get("REPORT_DATE") or "")[:10] or None
            published_at = latest.get("NOTICE_DATE") or latest.get("UPDATE_DATE")
            fields: dict[str, dict[str, Any]] = {}
            for source, target in (
                ("ROEJQ", "weighted_roe_annual"),
                ("XSMLL", "gross_margin_annual"),
                ("TOTALOPERATEREVETZ", "revenue_yoy_annual"),
                ("PARENTNETPROFITTZ", "net_profit_yoy_annual"),
                ("ZCFZL", "debt_ratio_annual"),
            ):
                value = latest.get(source)
                if value is None or isinstance(value, bool):
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                fields[target] = {
                    "value": number,
                    "unit": "percent",
                    "period_end": period_end,
                    "published_at": published_at,
                    "source": "akshare",
                }
            return {"fields": fields}

        frame = ak.stock_financial_us_analysis_indicator_em(symbol=code, indicator="年报")
        rows = _records(frame)
        if not rows:
            return {}
        latest = max(
            rows,
            key=lambda row: str(
                row.get("日期") or row.get("date") or row.get("报告期")
                or row.get("REPORT_DATE") or row.get("REPORT_DATE_NAME") or ""
            )[:10],
        )
        raw_period = (
            latest.get("日期") or latest.get("date") or latest.get("报告期")
            or latest.get("REPORT_DATE") or latest.get("REPORT_DATE_NAME")
        )
        try:
            period_end = str(raw_period)[:10] if raw_period else None
            if period_end:
                date.fromisoformat(period_end)
        except ValueError:
            period_end = None
        fields = {
            str(name): {
                "value": value, "unit": None, "period_end": period_end,
                "published_at": None, "source": "akshare",
            }
            for name, value in latest.items()
            if name not in {"date", "日期", "报告期"}
            and isinstance(value, (str, int, float))
            and str(value).strip().lower() not in {"", "-", "--", "nan", "none", "null"}
        }
        return {"fields": fields}

    def news(self, code: str) -> list[dict[str, Any]]:
        import akshare as ak

        rows = _records(ak.stock_news_em(symbol=code))
        return [
            {"headline": row.get("新闻标题") or row.get("标题"), "source": row.get("文章来源") or "Eastmoney",
             "datetime": row.get("发布时间") or row.get("时间"), "summary": row.get("新闻内容") or row.get("内容")}
            for row in rows if row.get("新闻标题") or row.get("标题")
        ]


class _BaiduTransport:
    """Verified Baidu Gushitong valuation fallback for US stocks."""

    def fundamentals(self, code: str) -> dict[str, Any]:
        import requests

        fields: dict[str, dict[str, Any]] = {}
        for indicator, target in (("市盈率(TTM)", "pe_ttm"), ("市净率", "pb_mrq")):
            params = {
                "openapi": "1", "dspName": "iphone", "tn": "tangram", "client": "app",
                "query": indicator, "code": code, "resource_id": "51171", "market": "us",
                "tag": indicator, "chart_select": "近三年", "skip_industry": "1", "finClientType": "pc",
            }
            response = requests.get(
                f"https://gushitong.baidu.com/opendata?{urlencode(params)}",
                timeout=15, headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            body = payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["body"]
            if not body:
                continue
            latest = body[-1]
            period = str(latest[0])[:10]
            fields[target] = {
                "value": float(latest[1]), "unit": "multiple", "period_end": period,
                "published_at": None, "source": "baidu_gushitong",
            }
        return {"fields": fields}


def build_data_refresh_service(settings: V2Settings, repository: SQLiteRepository | None = None) -> DataRefreshService:
    """Build the real V2 data service; callers own the repository lifecycle."""
    repo = repository or SQLiteRepository(settings.database_path)
    tickflow_raw = _TickFlowTransport(settings)
    nasdaq_raw = _NasdaqTransport()
    yfinance_raw = _YFinanceTransport()
    finnhub_raw = _FinnhubTransport(settings.news_token_us)
    baostock_raw = _BaostockTransport()
    akshare_raw = _AkshareTransport()
    baidu_raw = _BaiduTransport()
    tickflow = TickFlowAdapter(tickflow_raw.daily, tickflow_raw.quote, tickflow_raw.metadata, utc_now)
    nasdaq = NasdaqAdapter(nasdaq_raw.quote, utc_now, nasdaq_raw.daily)
    yfinance = YFinanceAdapter(yfinance_raw.daily, yfinance_raw.quote, utc_now, fundamentals_call=yfinance_raw.fundamentals)
    finnhub = FinnhubAdapter(finnhub_raw.profile, finnhub_raw.fundamentals, finnhub_raw.news, utc_now)
    baostock = BaostockAdapter(baostock_raw.metadata, baostock_raw.listing, baostock_raw.fundamentals, utc_now)
    akshare = AkshareAdapter(akshare_raw.fundamentals, akshare_raw.news, utc_now)
    eastmoney_via_akshare = AkshareAdapter(akshare_raw.fundamentals, akshare_raw.news, utc_now, name="eastmoney_via_akshare")
    baidu = FundamentalAdapter(baidu_raw.fundamentals, utc_now, "baidu_gushitong")
    providers = DataProviders(
        tickflow_daily=tickflow.daily, nasdaq_daily=nasdaq.daily, yfinance_daily=yfinance.daily,
        tickflow_quote=tickflow.quote, tickflow_quotes=tickflow.quotes,
        nasdaq_extended_quote=nasdaq.quote, yfinance_extended_quote=yfinance.quote,
        tickflow_metadata=tickflow.metadata, baostock_metadata=baostock.metadata, finnhub_metadata=finnhub.metadata,
        baostock_listing_date=baostock.listing_date, finnhub_listing_date=finnhub.listing_date,
        baostock_fundamentals=baostock.fundamentals, akshare_fundamentals=akshare.fundamentals,
        finnhub_fundamentals=finnhub.fundamentals, yfinance_fundamentals=yfinance.fundamentals,
        baidu_fundamentals=baidu.fundamentals,
        eastmoney_news=eastmoney_via_akshare.news, finnhub_news=finnhub.news,
    )
    return DataRefreshService(
        providers, ExchangeTradingCalendar(), DataCache(), repo,
        daily_rate_budget=SQLiteDailyRateBudget(repo), quote_rate_budget=SQLiteQuoteRateBudget(repo),
        finnhub_rate_budget=SQLiteFinnhubRateBudget(repo),
    )
