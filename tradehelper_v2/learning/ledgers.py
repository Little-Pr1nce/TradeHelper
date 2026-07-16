"""三本账的不可变切片汇总。"""
from __future__ import annotations

from collections import defaultdict

from tradehelper_v2.contracts import OutcomeStatus, stable_hash

from .metrics import strategy_summary, summarize_forecasts


def forecast_ledger(outcomes, *, cutoff_at, industry_membership=None):
    """按股票、origin regime、point-in-time 行业和市场切片。"""
    groups = defaultdict(list)
    membership = industry_membership or {}
    visible = tuple(item for item in outcomes if item.evaluated_at <= cutoff_at)
    for item in visible:
        common = (item.horizon, item.model_version, item.evidence_origin.value)
        groups[("instrument", item.instrument.stable_key, *common)].append(item)
        if item.market_regime_key is not None:
            groups[("regime", item.instrument.stable_key, item.horizon, item.market_regime_key, item.model_version, item.evidence_origin.value)].append(item)
        industry = membership.get(item.forecast_outcome_id)
        if industry is not None:
            industry_key, available_at = industry
            if available_at <= cutoff_at:
                groups[("industry", industry_key, *common)].append(item)
        groups[("market", item.instrument.market.value, *common)].append(item)
    return {key: summarize_forecasts(value, cutoff_at=cutoff_at) for key, value in groups.items()}


def strategy_ledger(outcomes, *, cutoff_at=None):
    """按完整策略身份、周期、市场状态和证据来源隔离汇总。"""
    groups = defaultdict(
        lambda: {
            "triggered": 0,
            "filled": 0,
            "rejected": 0,
            "not_triggered": 0,
            "pending": 0,
            "unverifiable": 0,
            "conflicting": 0,
            "net_returns": [],
            "mae": [],
            "mfe": [],
            "friction": [],
            "entry": 0,
            "ordinary_exit": 0,
            "protective_exit": 0,
        }
    )
    for item in outcomes:
        evaluated_at = getattr(item, "evaluated_at", getattr(item, "generated_at", None))
        if cutoff_at is not None and (evaluated_at is None or evaluated_at > cutoff_at):
            continue
        action_family = "protective_exit" if item.family in {"protective_exit", "risk_exit"} else "ordinary_exit" if item.action in {"sell", "reduce"} else "entry"
        key = (
            item.instrument.stable_key,
            item.strategy_id,
            item.strategy_version,
            item.parameter_hash,
            item.profile,
            action_family,
            item.evaluation_horizon,
            item.market_regime_key,
            item.evidence_origin.value,
        )
        value = groups[key]
        if item.status is not OutcomeStatus.MATURED:
            value[item.status.value] += 1
            continue
        if item.trigger_state == "not_triggered":
            value["not_triggered"] += 1
        elif item.fill_outcome == "rejected":
            value["rejected"] += 1
        else:
            value["triggered"] += 1
            if item.fill_outcome in {"filled", "partial"}:
                value["filled"] += 1
                if item.net_return is not None:
                    value["net_returns"].append(item.net_return)
                if item.mae is not None:
                    value["mae"].append(item.mae)
                if item.mfe is not None:
                    value["mfe"].append(item.mfe)
                value["friction"].append(sum((part or 0 for part in (item.commission, item.tax, item.slippage)), 0))
        value[action_family] += 1
    for key, value in groups.items():
        value["summary"] = strategy_summary(value["net_returns"], seed=int(stable_hash(key)[:8], 16))
    return dict(groups)


def joint_ledger(outcomes, *, cutoff_at):
    """按市场、方案、结果类型和证据来源汇总成熟联合结果。

    历史 OOF 结果使用冻结的 replay_window 判断 point-in-time 可见性；在线结果
    没有历史窗口时使用发行时间。不同市场、币种和 profile 永不混账。
    """
    groups = defaultdict(list)
    for item in outcomes:
        visible = (
            item.replay_window is not None
            and item.replay_window[1] <= cutoff_at.date()
        ) or (
            item.replay_window is None
            and item.generated_at <= cutoff_at
        )
        if not visible or item.status is not OutcomeStatus.MATURED:
            continue
        key = (
            item.market.value,
            item.currency,
            item.profile,
            item.outcome_kind.value,
            item.evidence_origin.value,
        )
        groups[key].append(item)

    summaries = {}
    for key, values in groups.items():
        returns = tuple(float(item.time_weighted_return) for item in values)
        alphas = tuple(float(item.alpha) for item in values if item.alpha is not None)
        drawdowns = tuple(float(item.max_drawdown) for item in values)
        summaries[key] = {
            "sample_count": len(values),
            "mean_twr": sum(returns) / len(returns),
            "mean_alpha": None if not alphas else sum(alphas) / len(alphas),
            "worst_drawdown": min(drawdowns),
            "total_friction": sum((item.realized_friction for item in values), 0),
            "entry_count": sum(item.entry_count for item in values),
            "exit_count": sum(item.exit_count for item in values),
            "rejected_count": sum(item.rejected_count for item in values),
            "cutoff_at": cutoff_at,
        }
    return summaries
