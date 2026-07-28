"""Strict source/field/unit registry for point-in-time fundamental facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contracts import FeatureStatus, FeatureValue, FundamentalSnapshot, ProviderStatus


_NAMES = ("pe_ttm", "pb_mrq", "ps_ttm", "roe", "gross_margin", "revenue_growth_yoy", "net_profit_growth_yoy", "debt_ratio")
_RATIO_NAMES = frozenset({"roe", "gross_margin", "revenue_growth_yoy", "net_profit_growth_yoy", "debt_ratio"})

# Primary providers always win over supplemental providers when both expose a
# valid fact for the same canonical feature.  This order is independent of raw
# field spelling and input mapping order.
_SOURCE_PRIORITY = {
    "finnhub": 0,
    "baostock": 0,
    "yfinance": 10,
    "akshare": 20,
    "baidu_gushitong": 30,
}

# Source precedence is canonical-feature specific where provider definitions
# differ.  For A shares, baostock's roeAvg is a simple average-equity return,
# while Akshare's ROEJQ carries the issuer-disclosed weighted annual ROE.
_CANONICAL_SOURCE_PRIORITY = {
    ("roe", "akshare"): 0,
    ("roe", "baostock"): 10,
    ("revenue_growth_yoy", "akshare"): 0,
    ("revenue_growth_yoy", "baostock"): 10,
}

# Within one provider, prefer the field whose accounting period matches the
# canonical feature.  Unlisted aliases remain deterministic but lower priority.
_FIELD_PRIORITY = {
    ("finnhub", "peTTM"): 0,
    ("finnhub", "peNormalizedAnnual"): 10,
    ("finnhub", "pb"): 0,
    ("finnhub", "pbQuarterly"): 10,
    ("finnhub", "psTTM"): 0,
    ("finnhub", "roeTTM"): 0,
    ("finnhub", "roeRfy"): 10,
    ("finnhub", "grossMarginTTM"): 0,
    ("finnhub", "grossMarginAnnual"): 10,
    ("finnhub", "revenueGrowthTTMYoy"): 0,
    ("finnhub", "revenueGrowthQuarterlyYoy"): 10,
    ("finnhub", "netIncomeGrowthTTMYoy"): 0,
    ("finnhub", "netIncomeGrowthQuarterlyYoy"): 10,
    ("finnhub", "totalDebtToTotalAssetsQuarterly"): 0,
    ("finnhub", "totalDebtToTotalAssetsAnnual"): 10,
    ("akshare", "weighted_roe_annual"): 0,
    ("akshare", "revenue_yoy_annual"): 0,
    ("akshare", "gross_margin_annual"): 0,
    ("akshare", "net_profit_yoy_annual"): 0,
    ("akshare", "debt_ratio_annual"): 0,
}


@dataclass(frozen=True, slots=True)
class FundamentalRule:
    canonical_name: str
    scale: float
    expected_units: frozenset[str | None]


def _rule(canonical_name: str, scale: float, *units: str | None) -> FundamentalRule:
    return FundamentalRule(canonical_name, scale, frozenset(units))


# No fallback by value magnitude is permitted.  A new source field has no feature
# until this table and a fixture explicitly document its scale and unit.
_REGISTRY: dict[tuple[str, str], FundamentalRule] = {
    **{("baostock", field): _rule(name, 1.0, "multiple") for field, name in (("pe_ttm", "pe_ttm"), ("pb_mrq", "pb_mrq"), ("ps_ttm", "ps_ttm"))},
    **{("baostock", field): _rule(name, 1.0, "ratio", "decimal") for field, name in (("roe", "roe"), ("gross_margin", "gross_margin"), ("revenue_yoy", "revenue_growth_yoy"), ("net_profit_yoy", "net_profit_growth_yoy"), ("debt_ratio", "debt_ratio"))},
    # yfinance ``get_info`` exposes documented decimal ratios without a unit
    # field.  debtToEquity is deliberately excluded: it is not debt/assets.
    **{("yfinance", field): _rule(name, 1.0, None, "ratio", "decimal") for field, name in (("returnOnEquity", "roe"), ("grossMargins", "gross_margin"), ("revenueGrowth", "revenue_growth_yoy"), ("earningsGrowth", "net_profit_growth_yoy"))},
    **{("yfinance", field): _rule(name, 1.0, None, "multiple") for field, name in (("trailingPE", "pe_ttm"), ("priceToBook", "pb_mrq"), ("priceToSalesTrailing12Months", "ps_ttm"))},
    **{("finnhub", field): _rule(name, 0.01, "percent", "%") for field, name in (("roe", "roe"), ("gross_margin", "gross_margin"), ("revenue_yoy", "revenue_growth_yoy"), ("net_profit_yoy", "net_profit_growth_yoy"), ("debt_ratio", "debt_ratio"))},
    **{("finnhub", field): _rule(name, 1.0, "multiple") for field, name in (("pe_ttm", "pe_ttm"), ("pb_mrq", "pb_mrq"), ("ps_ttm", "ps_ttm"))},
    # Finnhub /stock/metric returns flat metric names and no per-field unit.
    **{("finnhub", field): _rule(name, 1.0, None) for field, name in (("peNormalizedAnnual", "pe_ttm"), ("peTTM", "pe_ttm"), ("pb", "pb_mrq"), ("pbQuarterly", "pb_mrq"), ("psTTM", "ps_ttm"))},
    **{("finnhub", field): _rule(name, 0.01, None) for field, name in (("roeRfy", "roe"), ("roeTTM", "roe"), ("grossMarginAnnual", "gross_margin"), ("grossMarginTTM", "gross_margin"), ("revenueGrowthTTMYoy", "revenue_growth_yoy"), ("revenueGrowthQuarterlyYoy", "revenue_growth_yoy"), ("netIncomeGrowthTTMYoy", "net_profit_growth_yoy"), ("netIncomeGrowthQuarterlyYoy", "net_profit_growth_yoy"), ("totalDebtToTotalAssetsAnnual", "debt_ratio"), ("totalDebtToTotalAssetsQuarterly", "debt_ratio"))},
    **{("akshare", field): _rule(name, 0.01, "percent", "%") for field, name in (("roe", "roe"), ("gross_margin", "gross_margin"), ("revenue_yoy", "revenue_growth_yoy"), ("net_profit_yoy", "net_profit_growth_yoy"), ("debt_ratio", "debt_ratio"))},
    **{("akshare", field): _rule(name, 1.0, "multiple") for field, name in (("pe_ttm", "pe_ttm"), ("pb_mrq", "pb_mrq"), ("ps_ttm", "ps_ttm"))},
    **{("akshare", field): _rule(name, 0.01, None, "percent", "%") for field, name in (("净资产收益率(%)", "roe"), ("销售毛利率(%)", "gross_margin"), ("主营业务收入增长率(%)", "revenue_growth_yoy"), ("营业收入增长率(%)", "revenue_growth_yoy"), ("净利润增长率(%)", "net_profit_growth_yoy"), ("资产负债率(%)", "debt_ratio"))},
    **{("akshare", field): _rule(name, 0.01, "percent", "%") for field, name in (("weighted_roe_annual", "roe"), ("gross_margin_annual", "gross_margin"), ("revenue_yoy_annual", "revenue_growth_yoy"), ("net_profit_yoy_annual", "net_profit_growth_yoy"), ("debt_ratio_annual", "debt_ratio"))},
    ("baidu_gushitong", "pe_ttm"): _rule("pe_ttm", 1.0, "multiple"),
    ("baidu_gushitong", "pb_mrq"): _rule("pb_mrq", 1.0, "multiple"),
}

_FAILURES = {
    ProviderStatus.UNAVAILABLE: "FUND_PROVIDER_UNAVAILABLE", ProviderStatus.TIMEOUT: "FUND_PROVIDER_TIMEOUT",
    ProviderStatus.RATE_LIMITED: "FUND_PROVIDER_RATE_LIMITED", ProviderStatus.INVALID_PAYLOAD: "FUND_PROVIDER_INVALID_PAYLOAD",
}


def fundamental_features(snapshot: FundamentalSnapshot | None, status: ProviderStatus, cutoff_at: datetime) -> tuple[FeatureValue, ...]:
    if snapshot is None or snapshot.available_at > cutoff_at:
        reason = _FAILURES.get(status, "FUNDAMENTALS_MISSING") if status is not ProviderStatus.OK else "FUNDAMENTALS_NOT_AVAILABLE_AT_CUTOFF"
        return tuple(FeatureValue(f"fund.{name}", None, FeatureStatus.MISSING, None, None, cutoff_at, (), True, reason) for name in _NAMES)
    values: dict[str, FeatureValue] = {}
    selected_ranks: dict[str, tuple[int, int, str]] = {}
    unavailable_reasons: dict[str, str] = {}
    for raw_name, raw in snapshot.fields.items():
        rule = _REGISTRY.get((raw.source, raw_name))
        if rule is None:
            continue
        if raw.value is None or not isinstance(raw.value, (int, float)):
            unavailable_reasons.setdefault(rule.canonical_name, "FUND_VALUE_MISSING")
            continue
        if raw.published_at is not None and raw.published_at > cutoff_at:
            unavailable_reasons.setdefault(rule.canonical_name, "FUND_FIELD_NOT_AVAILABLE_AT_CUTOFF")
            continue
        if raw.unit not in rule.expected_units:
            unavailable_reasons.setdefault(rule.canonical_name, "FUND_UNIT_UNSUPPORTED")
            continue
        rank = (
            _CANONICAL_SOURCE_PRIORITY.get(
                (rule.canonical_name, raw.source),
                _SOURCE_PRIORITY.get(raw.source, 100),
            ),
            _FIELD_PRIORITY.get((raw.source, raw_name), 100),
            raw_name,
        )
        if rule.canonical_name in selected_ranks and rank >= selected_ranks[rule.canonical_name]:
            continue
        selected_ranks[rule.canonical_name] = rank
        values[rule.canonical_name] = FeatureValue(
            f"fund.{rule.canonical_name}", float(raw.value) * rule.scale, FeatureStatus.AVAILABLE,
            "ratio" if rule.canonical_name in _RATIO_NAMES else "multiple", None, cutoff_at,
            (raw.source,), True, None,
        )
    return tuple(values.get(name, FeatureValue(f"fund.{name}", None, FeatureStatus.MISSING, None, None,
                                                cutoff_at, (), True, unavailable_reasons.get(name, "FUND_FIELD_MISSING"))) for name in _NAMES)
