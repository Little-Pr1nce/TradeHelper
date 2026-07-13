"""Deterministic V2 data-quality and freshness rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Iterable

from tradehelper_v2.contracts.enums import (
    DecisionMode,
    FreshnessStatus,
    Market,
    QualityAction,
    QualitySeverity,
    QualityStatus,
)
from tradehelper_v2.contracts.market_data import CanonicalBar, FundamentalSnapshot, QuoteSnapshot, ensure_utc
from tradehelper_v2.contracts.quality import DataCapabilities, DataQualityIssue, DataQualityReport


def effective_start_date(requested_start: date, listing_date: date | None) -> date:
    return max(requested_start, listing_date) if listing_date is not None else requested_start


def assess_fundamental_quality(snapshot: FundamentalSnapshot) -> FundamentalSnapshot:
    """Grade usable factor families instead of treating any scalar field as complete fundamentals."""
    names = {name.lower().replace("_", "") for name in snapshot.fields}

    def contains(*patterns: str) -> bool:
        return any(any(pattern in name for pattern in patterns) for name in names)

    covered = sum((
        contains("pe", "pb", "ps", "priceearnings", "pricetobook"),
        contains("roe", "grossmargin", "profitmargin"),
        contains("revenueyoy", "revenuegrowth", "netprofityoy", "yoyni", "earningsgrowth"),
        contains("debtratio", "liabilitytoasset", "debttoequity", "totaldebt"),
    ))
    quality = (
        QualityStatus.OK if covered == 4 else
        QualityStatus.WATCH if covered >= 2 else
        QualityStatus.DEGRADED if covered == 1 else
        QualityStatus.BLOCKED
    )
    return replace(snapshot, quality_status=quality)


def assess_quote_freshness(
    quote: QuoteSnapshot,
    mode: DecisionMode,
    as_of: datetime,
) -> QuoteSnapshot:
    """Set freshness from the policy's 15/45-minute boundaries."""
    now = ensure_utc(as_of, "as_of")
    if quote.freshness_status is FreshnessStatus.MISSING_TIMESTAMP:
        return quote
    maximum_age = timedelta(minutes=15 if mode is DecisionMode.INTRADAY else 45)
    delta = now - quote.observed_at
    if delta < timedelta(minutes=-5):
        status = FreshnessStatus.FUTURE
    elif delta > maximum_age:
        status = FreshnessStatus.STALE
    else:
        status = FreshnessStatus.FRESH
    return replace(quote, freshness_status=status)


def _issue(
    code: str,
    severity: QualitySeverity,
    message: str,
    field: str | None = None,
    source: str | None = None,
) -> DataQualityIssue:
    return DataQualityIssue(code=code, severity=severity, field=field, message=message, source=source)


def _capabilities(
    bars: tuple[CanonicalBar, ...],
    quote: QuoteSnapshot | None,
    news_available: bool,
    fundamentals_available: bool,
) -> DataCapabilities:
    count = len(bars)
    quote_fields = quote.available_fields if quote is not None and quote.freshness_status is FreshnessStatus.FRESH else frozenset()
    return DataCapabilities(
        daily_price=count >= 1,
        short_technical_20=count >= 20,
        medium_technical_60=count >= 60,
        ma120=count >= 120,
        realtime_price="price" in quote_fields,
        intraday_ohlc={"open", "high", "low"}.issubset(quote_fields),
        volume="volume" in quote_fields,
        bid_ask={"bid", "ask"}.issubset(quote_fields),
        news=news_available,
        fundamentals=fundamentals_available,
    )


def _finalize(
    issues: Iterable[DataQualityIssue],
    capabilities: DataCapabilities,
    evaluated_at: datetime,
) -> DataQualityReport:
    unique = {issue.dedupe_key: issue for issue in issues}
    ordered = tuple(sorted(unique.values(), key=lambda issue: issue.dedupe_key))
    score = 100.0
    for issue in ordered:
        if issue.severity is QualitySeverity.BLOCK:
            score -= 35.0
        elif issue.severity is QualitySeverity.WARNING:
            score -= 8.0
        elif issue.severity is QualitySeverity.OPTIONAL_MISSING:
            score -= 4.0
    score = max(0.0, min(100.0, score))
    if any(issue.severity is QualitySeverity.BLOCK for issue in ordered):
        return DataQualityReport(
            status=QualityStatus.BLOCKED,
            action=QualityAction.BLOCK_NEW_ENTRIES,
            score=score,
            max_position_multiplier=0.0,
            block_new_entries=True,
            issues=ordered,
            capabilities=capabilities,
            evaluated_at=evaluated_at,
        )
    if score < 70.0:
        status, action, multiplier = QualityStatus.DEGRADED, QualityAction.REDUCE_POSITION, 0.5
    elif score < 85.0:
        status, action, multiplier = QualityStatus.WATCH, QualityAction.WATCH, 0.8
    else:
        status, action, multiplier = QualityStatus.OK, QualityAction.NORMAL, 1.0
    return DataQualityReport(
        status=status,
        action=action,
        score=score,
        max_position_multiplier=multiplier,
        block_new_entries=False,
        issues=ordered,
        capabilities=capabilities,
        evaluated_at=evaluated_at,
    )


def evaluate_data_quality(
    bars: Iterable[CanonicalBar],
    *,
    market: Market,
    mode: DecisionMode,
    as_of: datetime,
    quote: QuoteSnapshot | None = None,
    news_available: bool = False,
    fundamentals_available: bool = False,
    listing_date: date | None = None,
    requested_start: date | None = None,
    calendar=None,
) -> DataQualityReport:
    """Evaluate observable facts without making a trading recommendation."""
    normalized_bars = tuple(bars)
    issues: list[DataQualityIssue] = []
    if not normalized_bars:
        issues.append(_issue("EMPTY_DAILY_BARS", QualitySeverity.BLOCK, "正式日K为空", "daily_bars"))
    else:
        dates = [bar.trading_date for bar in normalized_bars]
        if dates != sorted(dates):
            issues.append(_issue("DATE_ORDER_INVALID", QualitySeverity.WARNING, "日K输入日期非递增", "trading_date"))
        if len(dates) != len(set(dates)):
            issues.append(_issue("CONFLICTING_DUPLICATE_BAR", QualitySeverity.BLOCK, "日K存在重复交易日", "trading_date"))
        if calendar is not None:
            for bar in normalized_bars:
                if not calendar.is_session(market, bar.trading_date):
                    issues.append(
                        _issue(
                            "TRADING_DATE_INVALID",
                            QualitySeverity.BLOCK,
                            f"{bar.trading_date.isoformat()} 不是正式交易日",
                            "trading_date",
                            bar.source,
                        )
                    )
        if listing_date is not None:
            before_listing = [bar for bar in normalized_bars if bar.trading_date < listing_date]
            if before_listing:
                issues.append(
                    _issue(
                        "BEFORE_LISTING_DATE",
                        QualitySeverity.BLOCK,
                        "日K包含上市日前记录",
                        "trading_date",
                    )
                )
        else:
            issues.append(_issue("LISTING_DATE_MISSING", QualitySeverity.WARNING, "上市日期缺失", "listing_date"))
        if requested_start is not None and listing_date is not None and listing_date > requested_start:
            issues.append(
                _issue(
                    "LISTING_WINDOW_CLIPPED",
                    QualitySeverity.INFO,
                    "请求窗口早于上市日期，已裁剪",
                    "listing_date",
                )
            )
        count = len(normalized_bars)
        if count < 20:
            issues.append(_issue("SAMPLE_LT_20", QualitySeverity.WARNING, "有效日K少于20条", "sample"))
        if count < 60:
            issues.append(_issue("SAMPLE_LT_60", QualitySeverity.WARNING, "有效日K少于60条", "sample"))
        if count < 120:
            issues.append(_issue("SAMPLE_LT_120", QualitySeverity.WARNING, "有效日K少于120条", "sample"))
        zero_ratio = sum(bar.volume == 0 for bar in normalized_bars) / count
        if zero_ratio > 0.2:
            issues.append(_issue("ZERO_VOLUME_RATIO_HIGH", QualitySeverity.WARNING, "零成交量比例超过20%", "volume"))
        for previous, current in zip(normalized_bars, normalized_bars[1:]):
            change = abs(current.close - previous.close) / previous.close
            if change > 0.25:
                issues.append(
                    _issue(
                        "PRICE_JUMP_REVIEW",
                        QualitySeverity.WARNING,
                        f"相邻收盘价变化{change:.1%}，需要复权复核",
                        "close",
                        current.source,
                    )
                )
    realtime_required = mode is DecisionMode.INTRADAY or (mode is DecisionMode.PRE and market is Market.US)
    normalized_quote = quote
    if quote is not None and mode is not DecisionMode.EOD:
        normalized_quote = assess_quote_freshness(quote, mode, as_of)
    if realtime_required:
        if normalized_quote is None:
            issues.append(_issue("REALTIME_PRICE_MISSING", QualitySeverity.BLOCK, "当前时段实时报价缺失", "price"))
        elif normalized_quote.freshness_status is FreshnessStatus.MISSING_TIMESTAMP:
            issues.append(_issue("REALTIME_TIMESTAMP_MISSING", QualitySeverity.BLOCK, "实时报价缺少供应商时间戳", "observed_at", normalized_quote.source))
        elif normalized_quote.freshness_status is FreshnessStatus.STALE:
            issues.append(_issue("REALTIME_STALE", QualitySeverity.BLOCK, "实时报价已过期", "observed_at", normalized_quote.source))
        elif normalized_quote.freshness_status is FreshnessStatus.FUTURE:
            issues.append(_issue("REALTIME_FUTURE_TIMESTAMP", QualitySeverity.BLOCK, "实时报价时间戳异常领先", "observed_at", normalized_quote.source))
    if normalized_quote is not None and normalized_quote.freshness_status is FreshnessStatus.FRESH:
        fields = normalized_quote.available_fields
        ohlc_fields = {"open", "high", "low"}
        if fields.intersection(ohlc_fields) and not ohlc_fields.issubset(fields):
            issues.append(_issue("QUOTE_OHLC_PARTIAL", QualitySeverity.WARNING, "报价OHLC不完整", "ohlc", normalized_quote.source))
        elif not fields.intersection(ohlc_fields):
            issues.append(_issue("QUOTE_OHLC_PARTIAL", QualitySeverity.WARNING, "报价没有可验证OHLC", "ohlc", normalized_quote.source))
        if "volume" not in fields:
            issues.append(_issue("QUOTE_VOLUME_MISSING", QualitySeverity.WARNING, "报价缺少成交量", "volume", normalized_quote.source))
        if market is Market.US and mode is DecisionMode.PRE and not {"bid", "ask"}.issubset(fields):
            issues.append(_issue("BID_ASK_MISSING", QualitySeverity.OPTIONAL_MISSING, "延伸时段缺少买卖价", "bid_ask", normalized_quote.source))
    if not news_available:
        issues.append(_issue("NEWS_MISSING", QualitySeverity.OPTIONAL_MISSING, "新闻缺失", "news"))
    if not fundamentals_available:
        issues.append(_issue("FUNDAMENTALS_MISSING", QualitySeverity.OPTIONAL_MISSING, "基本面缺失", "fundamentals"))
    capabilities = _capabilities(normalized_bars, normalized_quote, news_available, fundamentals_available)
    return _finalize(issues, capabilities, ensure_utc(as_of, "as_of"))


def extended_liquidity_multiplier(quote: QuoteSnapshot | None) -> float:
    """Return the V2 policy's price-depth proxy multiplier."""
    if quote is None or quote.freshness_status is not FreshnessStatus.FRESH:
        return 0.0
    if quote.bid is not None and quote.ask is not None:
        midpoint = (quote.bid + quote.ask) / 2.0
        spread = (quote.ask - quote.bid) / midpoint if midpoint > 0 else 1.0
        if spread <= 0.002:
            return 0.75
        if spread <= 0.005:
            return 0.50
        return 0.25
    if quote.volume is not None and quote.volume > 0:
        return 0.50
    return 0.25
