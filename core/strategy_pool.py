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
import hashlib
import json
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

# 模块级回测结果缓存：key = (stock_code, base_key, params_hash, first_date, last_date, length)
# 同一 session 内重复跑相同 stock+params 直接命中
_variant_bt_cache: dict[str, "BacktestResult"] = {}


def _cache_params_json(
    params: dict,
    df: "pd.DataFrame",
    initial_capital: float,
) -> str:
    """缓存上下文包含参数、资金和 Alpha 序列，避免跨口径误复用。"""
    payload = dict(params or {})
    payload["__capital"] = round(float(initial_capital), 2)
    if "Final_Score" in df.columns:
        scores = pd.to_numeric(df["Final_Score"], errors="coerce").fillna(0.0)
        raw = scores.round(6).to_numpy(dtype="float64").tobytes()
        payload["__score_sig"] = hashlib.sha1(raw).hexdigest()[:16]
    return json.dumps(payload, sort_keys=True)


def _make_cache_key(
    stock_code: str, base_key: str, params: dict, df: "pd.DataFrame"
) -> str:
    """生成缓存键。"""
    first = str(df["date"].iloc[0])[:10] if "date" in df.columns else ""
    last = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
    param_str = ",".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "default"
    return f"{stock_code}|{base_key}|{param_str}|{first}|{last}|{len(df)}"


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
    walk_forward: dict = field(default_factory=dict)  # variant_label -> OOS 统计


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

        vt_list = generate_variants([key], max_per_strategy=max_variants_per)

        if best and best["source"] != "demoted":
            # per_stock_params 只作为额外候选，不能替代完整策略池；
            # 否则会把过去全样本优化结果变成唯一方案，放大过拟合风险。
            kwargs = best["params"]
            existing = {tuple(sorted(v.params.items())) for v in vt_list}
            if tuple(sorted(kwargs.items())) not in existing:
                strategy = get_execution_strategy(key, **kwargs)
                vt_list.append(StrategyVariant(
                    base_key=key,
                    variant_label=f"{key}_saved",
                    strategy=strategy,
                    params=kwargs,
                    is_default=False,
                ))
            params_hit_count += 1
        elif best and best["source"] == "demoted":
            recovery = _generate_recovery_variants(key, vt_list)
            if recovery:
                vt_list.extend(recovery)
                logger.info(
                    f"策略池: {stock_code} {key} 历史负期望，"
                    f"追加 {len(recovery)} 个保守恢复候选"
                )

        variants.extend(vt_list)

    if params_hit_count:
        logger.info(f"策略池: per_stock_params 命中 {params_hit_count} 个策略，"
                    f"展开 {len(variants) - params_hit_count} 个")
    else:
        logger.info(f"策略池: 生成 {len(variants)} 个变体，开始回测...")

    # ── 2. 批量回测（优先读缓存）──
    config = BacktestConfig(initial_capital=initial_capital)
    engine = BacktestEngine(config)

    bt_results: dict[str, BacktestResult] = {}
    cache_hits = 0

    for v in variants:
        try:
            params_json = _cache_params_json(v.params, df, initial_capital)

            # 2a. 查 bt_variant_cache（SQLite 持久化）
            cached = None
            if db and stock_code:
                cached = db.get_cached_backtest(
                    stock_code, v.base_key, params_json, data_start, data_end,
                    data_length=data_len)
                if cached:
                    bt_results[v.variant_label] = _reconstruct_result(cached)
                    cache_hits += 1
                    continue

            # 2b. 查内存缓存
            ck = _make_cache_key(
                stock_code, v.base_key, json.loads(params_json), df
            )
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
    audit_meta: dict[str, dict] = {}
    for v in variants:
        if v.variant_label in bt_results:
            bt = bt_results[v.variant_label]
            audit_results[v.variant_label] = bt
            audit_meta[v.variant_label] = {
                "name": v.strategy.name,
                "suitable_regimes": list(v.strategy.suitable_regimes),
            }

    audit = None
    pass_variants: list[StrategyVariant] = []
    cond_variants: list[StrategyVariant] = []
    walk_forward: dict = {}

    if audit_results:
        walk_forward = _run_walk_forward_selection(
            df=df,
            variants=[v for v in variants if v.variant_label in bt_results],
            market=market,
            initial_capital=initial_capital,
            news_df=news_df,
        )

        audit = run_strategy_audit(
            df=df,
            strategy_keys=list(audit_results.keys()),
            backtest_results=audit_results,
            initial_capital=initial_capital,
            split_ratio=split_ratio,
            strategy_meta=audit_meta,
        )

        # ── 4. 筛选 + 更新 per_stock_params ──
        if audit and audit.entries:
            verdict_map: dict[str, str] = {}
            for e in audit.entries:
                verdict_map[e.strategy_key] = e.verdict

            for v in variants:
                verdict = verdict_map.get(v.variant_label, "FAIL")
                if walk_forward:
                    wf = walk_forward.get(v.variant_label)
                    if not wf or not wf.get("pass_oos", False):
                        continue
                if verdict == "PASS":
                    pass_variants.append(v)
                elif verdict == "CONDITIONAL":
                    cond_variants.append(v)

            pass_variants, cond_variants = _select_representative_variants(
                pass_variants, cond_variants, audit.entries
            )

            # 写 per_stock_params：对每个 PASS 策略存最佳参数
            if db and stock_code:
                allowed_labels = {v.variant_label for v in (pass_variants + cond_variants)}
                _update_per_stock_params(
                    db, stock_code, variants, bt_results,
                    audit.entries,
                    allowed_labels=allowed_labels,
                    walk_forward=walk_forward,
                    data_end=data_end,
                )

            logger.info(
                f"审计筛选: PASS={len(pass_variants)}, "
                f"CONDITIONAL={len(cond_variants)}, "
                f"FAIL={len(variants) - len(pass_variants) - len(cond_variants)}, "
                f"WF={'启用' if walk_forward else '跳过'}"
            )

    return StrategyPoolResult(
        variants=variants,
        backtest_results=bt_results,
        audit_report=audit,
        pass_variants=pass_variants,
        conditional_variants=cond_variants,
        total_backtests=len(bt_results),
        walk_forward=walk_forward,
    )


# ══════════════════════════════════════════════════════════════════
# 辅助函数：序列化 / 反序列化 / per_stock_params 更新
# ══════════════════════════════════════════════════════════════════

def _run_walk_forward_selection(
    df: pd.DataFrame,
    variants: list[StrategyVariant],
    market: str,
    initial_capital: float,
    news_df: pd.DataFrame | None = None,
    train_len: int | None = None,
    test_len: int | None = None,
) -> dict[str, dict]:
    """
    滚动 walk-forward 参数选择。

    每个窗口只在训练段选择同一 base_key 下表现最好的参数，再把该参数拿到
    后续测试段验证。只有被训练段选中、且样本外平均收益为正的变体才能通过。
    """
    n = len(df)
    if n < 160 or not variants:
        return {}

    train_len = train_len or max(80, int(n * 0.45))
    test_len = test_len or max(30, int(n * 0.15))
    if train_len + test_len > n:
        return {}

    config = BacktestConfig(initial_capital=initial_capital)
    engine = BacktestEngine(config)

    by_base: dict[str, list[StrategyVariant]] = {}
    for v in variants:
        by_base.setdefault(v.base_key, []).append(v)

    stats: dict[str, dict] = {
        v.variant_label: {
            "base_key": v.base_key,
            "selected_windows": 0,
            "oos_returns": [],
            "oos_sharpes": [],
            "oos_trades": 0,
            "pass_oos": False,
        }
        for v in variants
    }

    window_count = 0
    start = 0
    while start + train_len + test_len <= n:
        train_df = df.iloc[start:start + train_len].copy()
        test_df = df.iloc[start + train_len:start + train_len + test_len].copy()
        train_news = _slice_news(news_df, train_df)
        test_news = _slice_news(news_df, test_df)
        window_count += 1

        for base_key, candidates in by_base.items():
            scored: list[tuple[float, float, StrategyVariant]] = []
            for v in candidates:
                try:
                    r = engine.run(train_df.copy(), v.strategy, train_news)
                    if r.total_trades <= 0:
                        score = -999.0
                    else:
                        score = r.sharpe_ratio + max(r.total_return, -1.0)
                    scored.append((score, r.total_return, v))
                except Exception as e:
                    logger.debug(f"WF 训练失败 {v.variant_label}: {e}")

            if not scored:
                continue
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            selected = scored[0][2]

            try:
                oos = engine.run(test_df.copy(), selected.strategy, test_news)
                st = stats[selected.variant_label]
                st["selected_windows"] += 1
                st["oos_returns"].append(float(oos.total_return))
                st["oos_sharpes"].append(float(oos.sharpe_ratio))
                st["oos_trades"] += int(oos.total_trades)
            except Exception as e:
                logger.debug(f"WF 验证失败 {selected.variant_label}: {e}")

        start += test_len

    if window_count == 0:
        return {}

    result: dict[str, dict] = {}
    for label, st in stats.items():
        selected = int(st["selected_windows"])
        returns = st["oos_returns"]
        sharpes = st["oos_sharpes"]
        avg_return = float(np.mean(returns)) if returns else 0.0
        avg_sharpe = float(np.mean(sharpes)) if sharpes else 0.0
        pass_oos = selected > 0 and st["oos_trades"] > 0 and avg_return > 0 and avg_sharpe >= 0
        result[label] = {
            "base_key": st["base_key"],
            "selected_windows": selected,
            "avg_oos_return": avg_return,
            "avg_oos_sharpe": avg_sharpe,
            "oos_trades": st["oos_trades"],
            "pass_oos": pass_oos,
        }

    passed = sum(1 for v in result.values() if v["pass_oos"])
    logger.info(f"Walk-forward 参数验证: {passed}/{len(result)} 变体样本外通过")
    return result


def _slice_news(news_df: pd.DataFrame | None, price_df: pd.DataFrame) -> pd.DataFrame | None:
    """按价格窗口裁剪新闻数据；没有 date 列时原样返回。"""
    if news_df is None or news_df.empty or "date" not in news_df.columns or "date" not in price_df.columns:
        return news_df
    try:
        start = pd.to_datetime(price_df["date"].iloc[0])
        end = pd.to_datetime(price_df["date"].iloc[-1])
        dates = pd.to_datetime(news_df["date"], errors="coerce")
        return news_df.loc[(dates >= start) & (dates <= end)].copy()
    except Exception:
        return news_df


def _strict_param_value(base_key: str, name: str, values: list, default):
    """为负期望恢复候选选择更保守的参数值。"""
    if not values:
        return default
    low = values[0]
    high = values[-1]
    if name in ("risk_budget", "invest_pct", "hard_stop_pct", "atr_mult_stop", "atr_trail_mult"):
        return low
    if name in ("finbert_min", "k1", "exit_pct"):
        return high
    if name == "vol_percentile":
        return low
    if name == "entry_pct":
        return low if base_key == "B" else high
    return default


def _generate_recovery_variants(
    strategy_key: str,
    existing_variants: list[StrategyVariant],
    max_recovery: int = 3,
) -> list[StrategyVariant]:
    """为历史负期望策略生成更保守的自动恢复候选。"""
    base = get_execution_strategy(strategy_key)
    tunable = base.tunable_params()
    if not tunable:
        return []

    defaults = {p["name"]: p["default"] for p in tunable}
    strict = {
        p["name"]: _strict_param_value(strategy_key, p["name"], p.get("values", []), p["default"])
        for p in tunable
    }
    candidates: list[dict] = [strict]

    # 单参数保守变体：给 walk-forward 留出不过度收紧的恢复路径。
    for p in tunable:
        name = p["name"]
        val = strict.get(name, defaults.get(name))
        if val != defaults.get(name):
            one = dict(defaults)
            one[name] = val
            candidates.append(one)

    existing = {tuple(sorted(v.params.items())) for v in existing_variants}
    result: list[StrategyVariant] = []
    seen: set[tuple] = set()
    for params in candidates:
        key = tuple(sorted(params.items()))
        if key in existing or key in seen:
            continue
        seen.add(key)
        try:
            result.append(StrategyVariant(
                base_key=strategy_key,
                variant_label=f"{strategy_key}_recover_{len(result) + 1}",
                strategy=get_execution_strategy(strategy_key, **params),
                params=params,
                is_default=False,
            ))
        except Exception as e:
            logger.debug(f"恢复候选生成失败 {strategy_key} {params}: {e}")
        if len(result) >= max_recovery:
            break
    return result


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
    allowed_labels: set[str] | None = None,
    walk_forward: dict[str, dict] | None = None,
    data_end: str = "",
):
    """把 PASS+WF 参数登记为候选；跨窗口确认后才写入正式参数。"""
    import json

    # 按 base_key 分组，每组只存 test_sharpe 最高的 PASS 变体
    for e in audit_entries:
        if e.verdict != "PASS":
            continue
        vl = e.strategy_key
        if allowed_labels is not None and vl not in allowed_labels:
            continue
        # 找到对应的 variant
        v = next((v for v in variants if v.variant_label == vl), None)
        if not v or not v.params:
            continue
        try:
            wf = (walk_forward or {}).get(v.variant_label) or {}
            if not wf:
                continue
            params_json = json.dumps(v.params, sort_keys=True)
            lifecycle = db.record_strategy_param_candidate(
                stock_code=stock_code,
                strategy_key=v.base_key,
                params_json=params_json,
                test_sharpe=e.test_sharpe,
                walk_forward=wf,
                data_end=data_end,
            )
            if lifecycle.get("promoted"):
                logger.info(
                    f"参数候选晋升: {stock_code} {v.base_key} "
                    f"使用 {v.variant_label}，确认{lifecycle['confirmations']}次，"
                    f"test_sharpe={e.test_sharpe:.2f}"
                )
            else:
                logger.info(
                    f"参数候选观察: {stock_code} {v.base_key} {v.variant_label}，"
                    f"状态={lifecycle['status']}，确认{lifecycle['confirmations']}次"
                )
        except Exception:
            pass


def _select_representative_variants(
    pass_variants: list[StrategyVariant],
    cond_variants: list[StrategyVariant],
    audit_entries: list,
) -> tuple[list[StrategyVariant], list[StrategyVariant]]:
    """
    每个基础策略只保留一个代表变体进入信号检查。

    多参数扫描会天然制造多重检验问题：同一策略的多个参数版本如果同时
    进入操作层，会让报告看起来像“很多策略同意”，实际只是同一逻辑的
    多个近邻参数。这里按验证期夏普选每个 base_key 的最佳代表。
    """
    entry_map = {getattr(e, "strategy_key", ""): e for e in audit_entries}

    def _score(v: StrategyVariant) -> tuple[float, float]:
        e = entry_map.get(v.variant_label)
        if not e:
            return (0.0, 0.0)
        return (float(getattr(e, "test_sharpe", 0.0)), float(getattr(e, "test_return", 0.0)))

    selected_pass: dict[str, StrategyVariant] = {}
    for v in pass_variants:
        cur = selected_pass.get(v.base_key)
        if cur is None or _score(v) > _score(cur):
            selected_pass[v.base_key] = v

    selected_cond: dict[str, StrategyVariant] = {}
    for v in cond_variants:
        if v.base_key in selected_pass:
            continue
        cur = selected_cond.get(v.base_key)
        if cur is None or _score(v) > _score(cur):
            selected_cond[v.base_key] = v

    return list(selected_pass.values()), list(selected_cond.values())


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
