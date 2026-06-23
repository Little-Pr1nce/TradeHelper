"""
策略池扩展与审计筛选引擎 — Phase 2 + 3

Phase 2 — 策略池扩展:
  对每个策略的 tunable_params 做智能组合采样（非全量网格），
  生成 3-7 个参数变体，使策略池从 14 个扩展到 40-60 个。

Phase 3 — 审计驱动自动筛选:
  所有变体经回测 → 时间切分审计 → 只有 PASS 策略进入操作方案层。
  CONDITIONAL 策略标记为「参考」，FAIL 策略直接排除。

设计原则:
  - 智能采样避免组合爆炸（2 参数 × 4 值 = 16 组合 → 采 5-6 个）
  - 复用现有 BacktestEngine 和 strategy_audit，不重复造轮子
  - 人类策略 I-N（无 tunable_params）保持原样，不生成变体
"""

import logging
import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies import get_execution_strategy
from strategies.base import BaseExecutionStrategy
from backtest.engine import BacktestEngine, BacktestConfig, BacktestResult
from core.strategy_audit import run_strategy_audit, StrategyAuditReport

logger = logging.getLogger(__name__)

# 每个策略最大变体数（防止组合爆炸）
MAX_VARIANTS_PER_STRATEGY = 7

# 模块级回测结果缓存：key = (base_key, params_hash, first_date, last_date, length)
# 同一 session 内重复跑相同 stock+params 直接命中
_variant_bt_cache: dict[str, "BacktestResult"] = {}


def _make_cache_key(
    base_key: str, params: dict, df: "pd.DataFrame"
) -> str:
    """生成缓存键。"""
    first = str(df["date"].iloc[0])[:10] if "date" in df.columns else ""
    last = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
    param_str = ",".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "default"
    return f"{base_key}|{param_str}|{first}|{last}|{len(df)}"


@dataclass
class StrategyVariant:
    """策略池中的一个变体。"""
    base_key: str                  # 原始策略键（"A", "B", ...）
    variant_label: str             # 变体标签（"A_v1", "A_v2" ...）
    strategy: BaseExecutionStrategy  # 已初始化的策略实例
    params: dict                   # 当前参数值 {param_name: value}
    is_default: bool = False       # 是否默认参数组合

    @property
    def name(self) -> str:
        return self.strategy.name

    @property
    def display(self) -> str:
        """人类可读的变体描述。"""
        if not self.params:
            return self.name
        param_str = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.variant_label}: {self.name} ({param_str})"


@dataclass
class StrategyPoolResult:
    """策略池扩展 + 审计的完整结果。"""
    variants: list[StrategyVariant]            # 所有生成的变体
    backtest_results: dict[str, BacktestResult]  # key = variant_label
    audit_report: StrategyAuditReport | None   # 审计报告（只含 PASS + CONDITIONAL）
    pass_variants: list[StrategyVariant]       # 通过审计的变体
    conditional_variants: list[StrategyVariant]  # 有条件通过的变体
    total_backtests: int = 0                   # 总共跑的回测数


# ══════════════════════════════════════════════════════════════════
# 变体生成
# ══════════════════════════════════════════════════════════════════

def generate_variants(
    strategy_keys: list[str],
    max_per_strategy: int = MAX_VARIANTS_PER_STRATEGY,
) -> list[StrategyVariant]:
    """
    为每个策略生成参数变体。

    Args:
        strategy_keys: 策略键列表（如 ["A", "B", ...]）
        max_per_strategy: 单个策略最大变体数

    Returns:
        所有变体列表（flattened）
    """
    all_variants: list[StrategyVariant] = []

    for key in strategy_keys:
        base = get_execution_strategy(key)
        tunable = base.tunable_params()

        if not tunable:
            # 无可调参数 → 保持原样
            all_variants.append(StrategyVariant(
                base_key=key,
                variant_label=key,
                strategy=base,
                params={},
                is_default=True,
            ))
            continue

        # 生成参数组合
        param_names = [p["name"] for p in tunable]
        param_values = [p["values"] for p in tunable]
        defaults = {p["name"]: p["default"] for p in tunable}

        # 全量笛卡尔积
        all_combos = list(itertools.product(*param_values))

        # 智能采样：如果组合数超过上限，做 stratified sampling
        if len(all_combos) <= max_per_strategy:
            sampled_combos = all_combos
        else:
            sampled_combos = _smart_sample(all_combos, param_values, defaults,
                                           tunable, max_per_strategy)

        for idx, combo in enumerate(sampled_combos):
            kwargs = dict(zip(param_names, combo))
            is_default = all(kwargs.get(k) == defaults.get(k) for k in param_names)
            strategy = get_execution_strategy(key, **kwargs)
            all_variants.append(StrategyVariant(
                base_key=key,
                variant_label=f"{key}_v{idx + 1}" if len(sampled_combos) > 1 else key,
                strategy=strategy,
                params=kwargs,
                is_default=is_default,
            ))

    logger.info(
        f"策略池扩展: {len(strategy_keys)} 基础策略 → "
        f"{len(all_variants)} 变体（上限 {max_per_strategy}/策略）"
    )
    return all_variants


def _smart_sample(
    all_combos: list[tuple],
    param_values: list[list],
    defaults: dict,
    tunable: list[dict],
    max_count: int,
) -> list[tuple]:
    """
    智能采样：优先保留默认值组合 + 边缘值 + 中间值均匀分布。

    策略：
      1. 始终包含默认组合
      2. 包含每个参数的极值（最小/最大）
      3. 其余用均匀间隔填充
    """
    param_names = [p["name"] for p in tunable]
    selected: list[tuple] = []

    # 1. 默认组合
    default_combo = tuple(defaults[p["name"]] for p in tunable)
    if default_combo in all_combos:
        selected.append(default_combo)

    # 2. 每个参数的极值组合
    for i, (name, vals) in enumerate(zip(param_names, param_values)):
        for extreme_val in (vals[0], vals[-1]):
            combo = list(default_combo)
            combo[i] = extreme_val
            t = tuple(combo)
            if t in all_combos and t not in selected:
                selected.append(t)
                if len(selected) >= max_count:
                    return selected

    # 3. 均匀采样填充到上限
    step = max(1, len(all_combos) // (max_count - len(selected)))
    for idx in range(0, len(all_combos), step):
        combo = all_combos[idx]
        if combo not in selected:
            selected.append(combo)
            if len(selected) >= max_count:
                break

    return selected[:max_count]


# ══════════════════════════════════════════════════════════════════
# 回测 + 审计 + 筛选
# ══════════════════════════════════════════════════════════════════

def expand_and_audit(
    df: pd.DataFrame,
    strategy_keys: list[str],
    market: str,
    stock_code: str = "",
    initial_capital: float = 100000.0,
    news_df: pd.DataFrame | None = None,
    split_ratio: float = 0.70,
    max_variants_per: int = MAX_VARIANTS_PER_STRATEGY,
    db: "Database | None" = None,
) -> StrategyPoolResult:
    """
    一步完成：变体生成 → 回测 → 审计 → 筛选。
    支持 SQLite 缓存 (bt_variant_cache) 和自适应最佳参数 (per_stock_params)。

    Args:
        stock_code: 股票代码（用于缓存键和 per_stock_params 查询）
        db: 数据库实例（用于缓存读写）
    """
    import json

    data_start = str(df["date"].iloc[0])[:10] if "date" in df.columns else ""
    data_end = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
    data_len = len(df)

    # ── 1. 生成变体（优先使用 per_stock_params 中的最佳参数）──
    variants: list[StrategyVariant] = []
    params_hit_count = 0

    for key in strategy_keys:
        best = None
        if db and stock_code:
            best = db.get_best_params(stock_code, key)

        if best and best["source"] != "demoted":
            # per_stock_params 有最佳参数 → 只用它，不展开
            kwargs = best["params"]
            strategy = get_execution_strategy(key, **kwargs)
            variants.append(StrategyVariant(
                base_key=key, variant_label=key,
                strategy=strategy, params=kwargs, is_default=True,
            ))
            params_hit_count += 1
        else:
            # 没有最佳参数，或已 demoted → 展开完整变体池
            vt_list = generate_variants([key], max_per_strategy=max_variants_per)
            variants.extend(vt_list)

    if params_hit_count:
        logger.info(f"策略池: per_stock_params 命中 {params_hit_count} 个策略，"
                    f"展开 {len(variants) - params_hit_count} 个")
    else:
        logger.info(f"策略池: 生成 {len(variants)} 个变体，开始回测...")

    # ── 2. 批量回测（优先读缓存）──
    config = BacktestConfig(initial_capital=initial_capital)
    if market == "US":
        config.broker.limit_up_pct = 999.0
        config.broker.limit_down_pct = 999.0
    engine = BacktestEngine(config)

    bt_results: dict[str, BacktestResult] = {}
    cache_hits = 0

    for v in variants:
        try:
            params_json = json.dumps(v.params, sort_keys=True) if v.params else "{}"

            # 2a. 查 bt_variant_cache（SQLite 持久化）
            cached = None
            if db and stock_code:
                cached = db.get_cached_backtest(
                    stock_code, v.base_key, params_json, data_start, data_end)
                if cached:
                    bt_results[v.variant_label] = _reconstruct_result(cached)
                    cache_hits += 1
                    continue

            # 2b. 查内存缓存
            ck = _make_cache_key(v.base_key, v.params, df)
            if ck in _variant_bt_cache:
                bt_results[v.variant_label] = _variant_bt_cache[ck]
                cache_hits += 1
                continue

            # 2c. 未命中 → 跑回测
            result = engine.run(df.copy(), v.strategy, news_df)
            bt_results[v.variant_label] = result
            _variant_bt_cache[ck] = result

            # 写入 SQLite 缓存
            if db and stock_code:
                try:
                    db.save_backtest_cache(
                        stock_code=stock_code, strategy_key=v.base_key,
                        params_json=params_json,
                        data_start=data_start, data_end=data_end,
                        data_length=data_len,
                        sharpe_ratio=result.sharpe_ratio,
                        total_return=result.total_return,
                        max_drawdown=result.max_drawdown,
                        win_rate=result.win_rate,
                        total_trades=result.total_trades,
                        result_json=json.dumps(_serialize_result(result)),
                    )
                except Exception:
                    pass  # 缓存写入失败不阻塞
        except Exception as e:
            logger.warning(f"回测失败 {v.variant_label}: {e}")

    logger.info(
        f"策略池回测完成: {len(bt_results)}/{len(variants)} 个成功"
        f"{'（' + str(cache_hits) + ' 个缓存命中）' if cache_hits else ''}"
    )

    # ── 3. 审计所有变体 ──
    audit_results: dict[str, BacktestResult] = {}
    key_to_variant_label: dict[str, str] = {}
    for v in variants:
        if v.variant_label in bt_results:
            bt = bt_results[v.variant_label]
            audit_results[bt.strategy_name] = bt
            key_to_variant_label[bt.strategy_name] = v.variant_label

    audit = None
    pass_variants: list[StrategyVariant] = []
    cond_variants: list[StrategyVariant] = []

    if audit_results:
        audit = run_strategy_audit(
            df=df,
            strategy_keys=list(key_to_variant_label.keys()),
            backtest_results=audit_results,
            initial_capital=initial_capital,
            split_ratio=split_ratio,
        )

        # ── 4. 筛选 + 更新 per_stock_params ──
        if audit and audit.entries:
            verdict_map: dict[str, str] = {}
            for e in audit.entries:
                vl = key_to_variant_label.get(e.strategy_name, e.strategy_key)
                verdict_map[vl] = e.verdict

            for v in variants:
                verdict = verdict_map.get(v.variant_label, "FAIL")
                if verdict == "PASS":
                    pass_variants.append(v)
                elif verdict == "CONDITIONAL":
                    cond_variants.append(v)

            # 写 per_stock_params：对每个 PASS 策略存最佳参数
            if db and stock_code:
                _update_per_stock_params(
                    db, stock_code, variants, bt_results,
                    audit.entries, key_to_variant_label)

            logger.info(
                f"审计筛选: PASS={len(pass_variants)}, "
                f"CONDITIONAL={len(cond_variants)}, "
                f"FAIL={len(variants) - len(pass_variants) - len(cond_variants)}"
            )

    return StrategyPoolResult(
        variants=variants,
        backtest_results=bt_results,
        audit_report=audit,
        pass_variants=pass_variants,
        conditional_variants=cond_variants,
        total_backtests=len(bt_results),
    )


# ══════════════════════════════════════════════════════════════════
# 辅助函数：序列化 / 反序列化 / per_stock_params 更新
# ══════════════════════════════════════════════════════════════════

def _serialize_result(result: BacktestResult) -> dict:
    """BacktestResult → JSON-safe dict。"""
    return {
        "strategy_name": result.strategy_name,
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "annual_return": result.annual_return,
        "max_drawdown": result.max_drawdown,
        "sharpe_ratio": result.sharpe_ratio,
        "calmar_ratio": result.calmar_ratio,
        "win_rate": result.win_rate,
        "profit_loss_ratio": result.profit_loss_ratio,
        "avg_holding_days": result.avg_holding_days,
        "total_trades": result.total_trades,
        "equity_curve": result.equity_curve or [],
        "trades": result.trades or [],
        "metrics": result.metrics or {},
    }


def _reconstruct_result(data: dict) -> BacktestResult:
    """dict → BacktestResult。"""
    return BacktestResult(
        strategy_name=data.get("strategy_name", ""),
        initial_capital=data.get("initial_capital", 100000.0),
        final_equity=data.get("final_equity", 100000.0),
        total_return=data.get("total_return", 0.0),
        annual_return=data.get("annual_return", 0.0),
        max_drawdown=data.get("max_drawdown", 0.0),
        sharpe_ratio=data.get("sharpe_ratio", 0.0),
        calmar_ratio=data.get("calmar_ratio", 0.0),
        win_rate=data.get("win_rate", 0.0),
        profit_loss_ratio=data.get("profit_loss_ratio", 0.0),
        avg_holding_days=data.get("avg_holding_days", 0.0),
        total_trades=data.get("total_trades", 0),
        equity_curve=data.get("equity_curve", []),
        trades=data.get("trades", []),
        fills=[],
        metrics=data.get("metrics", {}),
    )


def _update_per_stock_params(
    db: "Database",
    stock_code: str,
    variants: list[StrategyVariant],
    bt_results: dict[str, BacktestResult],
    audit_entries: list,
    key_to_variant_label: dict[str, str],
):
    """审计完成后，把 PASS 策略的最佳参数写入 per_stock_params。"""
    import json

    # 按 base_key 分组，每组只存 test_sharpe 最高的 PASS 变体
    for e in audit_entries:
        if e.verdict != "PASS":
            continue
        vl = key_to_variant_label.get(e.strategy_name, e.strategy_key)
        # 找到对应的 variant
        v = next((v for v in variants if v.variant_label == vl), None)
        if not v or not v.params:
            continue
        try:
            params_json = json.dumps(v.params, sort_keys=True)
            db.save_best_params(
                stock_code=stock_code,
                strategy_key=v.base_key,
                params_json=params_json,
                sharpe=e.test_sharpe,
                source="audit_pass",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════════════════════

def get_top_strategies(pool: StrategyPoolResult, top_n: int = 3) -> list[StrategyVariant]:
    """从策略池中获取 Top N PASS 策略（按验证夏普排序）。"""
    if not pool.audit_report or not pool.audit_report.entries:
        return pool.pass_variants[:top_n]

    # 按 test_sharpe 排序
    entry_map = {e.strategy_name: e for e in pool.audit_report.entries}
    scored = []
    for v in pool.pass_variants:
        bt = pool.backtest_results.get(v.variant_label)
        if bt:
            e = entry_map.get(bt.strategy_name)
            score = e.test_sharpe if e else 0.0
            scored.append((score, v))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored[:top_n]]
