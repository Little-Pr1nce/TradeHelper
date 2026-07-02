"""
数据质量评分与交易阻断。

可信交易建议的第一道门：如果价格、成交量、样本长度或实时价口径存在
明显问题，系统必须降低执行等级、压缩仓位，严重时阻断交易建议。
"""

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd


def evaluate_extended_liquidity_proxy(quote: dict | None, mode: str) -> dict:
    """在没有 Level 2 深度时，用可验证的延伸时段字段约束仓位。

    买一/卖一只能代表 top-of-book，不能冒充完整深度；若连最优报价也
    不可得，则退化为成交量代理或纯价格代理，并进一步压缩新开仓。
    """
    if mode != "pre":
        return {
            "applied": False,
            "proxy": "not_required",
            "position_multiplier": 1.0,
            "spread_pct": None,
            "warning": "",
        }

    data = quote or {}
    bid = float(data.get("bid", 0.0) or 0.0)
    ask = float(data.get("ask", 0.0) or 0.0)
    volume = float(data.get("volume", 0.0) or 0.0)
    if bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else None
        if spread_pct is not None and spread_pct <= 0.002:
            multiplier = 0.75
        elif spread_pct is not None and spread_pct <= 0.005:
            multiplier = 0.50
        else:
            multiplier = 0.25
        return {
            "applied": True,
            "proxy": "top_of_book",
            "position_multiplier": multiplier,
            "spread_pct": spread_pct,
            "warning": (
                f"延伸时段仅有买一/卖一，非完整盘口深度；当前价差"
                f"{float(spread_pct or 0):.2%}，新开仓按{multiplier:.0%}上限处理"
            ),
        }

    if volume > 0:
        return {
            "applied": True,
            "proxy": "volume",
            "position_multiplier": 0.50,
            "spread_pct": None,
            "warning": "延伸时段无盘口深度和买卖价差，使用报价新鲜度/成交量代理，新开仓按50%上限处理",
        }
    return {
        "applied": True,
        "proxy": "price_only",
        "position_multiplier": 0.25,
        "spread_pct": None,
        "warning": "延伸时段仅有最新价，无盘口深度、价差和有效成交量，新开仓按25%上限处理",
    }


@dataclass
class DataQualityReport:
    score: float = 100.0
    status: str = "ok"          # ok / watch / degraded / blocked
    action: str = "normal"      # normal / watch / reduce_position / block
    max_position_multiplier: float = 1.0
    block_new_entries: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": round(float(self.score), 2),
            "status": self.status,
            "action": self.action,
            "max_position_multiplier": round(float(self.max_position_multiplier), 3),
            "block_new_entries": self.block_new_entries,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "missing": list(self.missing),
            "notes": list(self.notes),
        }


def evaluate_data_quality(
    df: pd.DataFrame,
    *,
    current_price: float | None = None,
    news_df: pd.DataFrame | None = None,
    fundamental_data: dict | None = None,
    depth_available: bool = False,
    market: str = "",
    realtime_quote_quality: dict | None = None,
    listing_date: str = "",
    requested_start: str = "",
) -> DataQualityReport:
    """评估交易建议可依赖的数据质量。"""
    report = DataQualityReport()
    if df is None or df.empty:
        report.issues.append("K线数据为空")
        return _finalize(report)

    required = ["open", "high", "low", "close", "volume"]
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        report.issues.append(f"缺少必要K线字段: {', '.join(missing_cols)}")
        return _finalize(report)

    n = len(df)
    if n < 20:
        if listing_date:
            report.issues.append(
                f"新股上市后K线样本不足：{listing_date}上市，当前仅{n}条，少于20条"
            )
        else:
            report.issues.append(f"K线样本不足：仅{n}条，少于20条")
    elif n < 60:
        if listing_date:
            report.warnings.append(
                f"上市后K线样本偏少：{listing_date}上市，当前{n}条，策略审计可信度下降"
            )
        else:
            report.warnings.append(f"K线样本偏少：{n}条，低于60条，策略审计可信度下降")
    if listing_date and requested_start and listing_date > requested_start:
        report.notes.append(
            f"所选回测窗口早于上市日，已自动裁剪为 {listing_date} 至今，上市前数据未参与计算"
        )

    price_cols = ["open", "high", "low", "close"]
    numeric = df[price_cols + ["volume"]].apply(pd.to_numeric, errors="coerce")
    null_count = int(numeric[price_cols].isna().sum().sum())
    if null_count:
        ratio = null_count / max(n * len(price_cols), 1)
        if ratio > 0.02:
            report.issues.append(f"价格字段缺失/非数值比例过高：{ratio:.1%}")
        else:
            report.warnings.append(f"价格字段存在少量缺失/非数值：{null_count}处")

    non_positive = (numeric[price_cols] <= 0).sum().sum()
    if int(non_positive) > 0:
        report.issues.append(f"存在非正价格数据：{int(non_positive)}处")

    bad_ohlc = (
        (numeric["high"] < numeric["low"]) |
        (numeric["close"] > numeric["high"] * 1.001) |
        (numeric["close"] < numeric["low"] * 0.999) |
        (numeric["open"] > numeric["high"] * 1.001) |
        (numeric["open"] < numeric["low"] * 0.999)
    )
    bad_ohlc_count = int(bad_ohlc.fillna(False).sum())
    if bad_ohlc_count:
        report.issues.append(f"OHLC 价格关系异常：{bad_ohlc_count}条")

    zero_volume_ratio = float((numeric["volume"] <= 0).sum() / max(n, 1))
    if zero_volume_ratio > 0.2:
        report.warnings.append(f"成交量为0或缺失比例较高：{zero_volume_ratio:.1%}")
    elif zero_volume_ratio > 0:
        report.warnings.append(f"存在少量成交量为0或缺失：{zero_volume_ratio:.1%}")

    close = numeric["close"].dropna()
    if len(close) >= 2:
        returns = close.pct_change().abs().dropna()
        huge_jumps = int((returns > 0.60).sum())
        large_jumps = int((returns > 0.25).sum())
        if huge_jumps:
            report.issues.append(f"疑似复权/价格跳变异常：单日涨跌幅超过60%共{huge_jumps}次")
        elif large_jumps:
            report.warnings.append(f"存在较大单日跳变：超过25%共{large_jumps}次，需复核复权和数据源")

    if "date" in df.columns:
        dates = pd.to_datetime(df["date"], errors="coerce")
        if dates.isna().any():
            report.warnings.append("部分K线日期无法解析")
        if dates.duplicated().any():
            report.warnings.append("K线日期存在重复")
        clean_dates = dates.dropna()
        if len(clean_dates) >= 2 and not clean_dates.is_monotonic_increasing:
            report.warnings.append("K线日期不是递增顺序")

    last_close = float(close.iloc[-1]) if not close.empty else 0.0
    if current_price and current_price > 0 and last_close > 0:
        gap = abs(float(current_price) - last_close) / last_close
        if gap > 0.60:
            report.issues.append(f"实时价与最新K线收盘价偏离过大：{gap:.1%}")
        elif gap > 0.25:
            report.warnings.append(f"实时价与最新K线收盘价偏离较大：{gap:.1%}")

    if news_df is None or news_df.empty:
        report.missing.append("新闻数据缺失")
    if not fundamental_data:
        report.missing.append("基本面数据缺失")
    quote_quality = realtime_quote_quality or {}
    if market == "US" and not depth_available:
        if quote_quality.get("liquidity_proxy"):
            report.missing.append("Level 2盘口深度缺失（已启用延伸时段流动性代理）")
        else:
            report.missing.append("盘口/深度数据缺失")
    if quote_quality.get("required"):
        report.issues.extend(str(x) for x in (quote_quality.get("issues") or []))
        report.warnings.extend(str(x) for x in (quote_quality.get("warnings") or []))

    finalized = _finalize(report)
    proxy_multiplier = quote_quality.get("liquidity_position_multiplier")
    if (
        proxy_multiplier is not None
        and not finalized.block_new_entries
        and float(proxy_multiplier) < finalized.max_position_multiplier
    ):
        finalized.max_position_multiplier = max(0.0, float(proxy_multiplier))
        finalized.status = "degraded"
        finalized.action = "reduce_position"
        proxy_name = str(quote_quality.get("liquidity_proxy") or "unknown")
        finalized.notes.append(
            f"延伸时段流动性代理={proxy_name}，仓位上限按"
            f"{finalized.max_position_multiplier:.0%}执行"
        )
    return finalized


def _finalize(report: DataQualityReport) -> DataQualityReport:
    score = 100.0
    score -= 35.0 * len(report.issues)
    score -= 8.0 * len(report.warnings)
    score -= 4.0 * len(report.missing)
    report.score = max(0.0, min(100.0, score))

    if report.issues or report.score < 50:
        report.status = "blocked"
        report.action = "block"
        report.max_position_multiplier = 0.0
        report.block_new_entries = True
    elif report.score < 70:
        report.status = "degraded"
        report.action = "reduce_position"
        report.max_position_multiplier = 0.5
        report.block_new_entries = False
    elif report.score < 85:
        report.status = "watch"
        report.action = "watch"
        report.max_position_multiplier = 0.8
        report.block_new_entries = False
    else:
        report.status = "ok"
        report.action = "normal"
        report.max_position_multiplier = 1.0
        report.block_new_entries = False
    return report


def data_quality_markdown(report: dict | DataQualityReport | None) -> str:
    """渲染数据质量摘要，供操作方案/报告直接嵌入。"""
    if not report:
        return ""
    data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    status_map = {
        "ok": "✅ 可用",
        "watch": "🟡 观察",
        "degraded": "🟠 降级",
        "blocked": "🔴 阻断",
    }
    lines = [
        "### 🧾 数据质量与可信度闸门\n",
        f"- 数据质量评分：**{float(data.get('score', 0)):.0f}/100**（{status_map.get(data.get('status'), data.get('status'))}）",
        f"- 交易动作：**{data.get('action', 'normal')}**，最大仓位倍率：**{float(data.get('max_position_multiplier', 1.0)):.0%}**",
    ]
    issues = data.get("issues") or []
    warnings = data.get("warnings") or []
    missing = data.get("missing") or []
    notes = data.get("notes") or []
    if issues:
        lines.append("- 阻断问题：" + "；".join(str(x) for x in issues[:3]))
    if warnings:
        lines.append("- 降级警告：" + "；".join(str(x) for x in warnings[:3]))
    if missing:
        lines.append("- 缺失数据：" + "；".join(str(x) for x in missing[:4]))
    if notes:
        lines.append("- 数据口径：" + "；".join(str(x) for x in notes[:3]))
    lines.append("")
    return "\n".join(lines)
