"""
核心分析管道 — CLI 和 UI 共用的纯计算逻辑。

将数据获取（I/O）与计算（纯函数）严格分离。
此模块不执行任何网络请求或数据库操作。
"""

import logging

import pandas as pd
from dataclasses import dataclass, field

from indicators.technical import calc_all_indicators
from alpha.scoring import calc_final_score
from strategies import get_execution_strategy
from backtest.engine import BacktestEngine, BacktestConfig
from backtest.analytics import compare_strategies, compute_rank_ic

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """一次完整分析的结构化结果（纯数据，无行为）。"""
    df: pd.DataFrame                          # 含技术指标 + Final_Score 的完整 DataFrame
    backtest: dict                            # key=策略名, value=BacktestResult
    comparison: dict                          # compare_strategies() 的输出
    rank_ic: dict                             # Rank IC 统计 {rank_ic_mean, ic_ir}
    benchmark_return: float = 0.0             # 买入持有基准收益


def run_pipeline(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    initial_capital: float = 100000.0,
    market: str = "A",
    strategy_names: list[str] | None = None,
    w_tech: float = 0.6,
    w_news: float = 0.4,
):
    """
    执行完整的量化分析计算管道（纯计算，无 I/O）。

    管道步骤：
      ┌──────────────────────────────────────────────┐
      │ ① 预计算 7 个技术指标（MA/MACD/RSI/布林/KDJ）    │
      │ ② Alpha 多因子打分（Z-Score + tanh + 加权合成）  │
      │ ③ Rank IC 计算（因子有效性检验）                 │
      │ ④ 三策略并行回测（A/B/C，T+1 撮合）              │
      └──────────────────────────────────────────────┘

    Args:
        df: 原始 OHLCV DataFrame（必须含 date/open/high/low/close/volume）
        news_df: 新闻情感 DataFrame（date + finbert_score），可选
        initial_capital: 初始资金（默认 10 万）
        market: "A" 或 "US"，影响涨跌停规则
        strategy_names: 策略列表，默认 ["A", "B", "C"]
        w_tech: 技术面权重
        w_news: 新闻面权重

    Returns:
        AnalysisResult 包含全部计算结果
    """
    if strategy_names is None:
        strategy_names = ["A", "B", "C"]

    logger.info("=" * 50)
    logger.info(f"管道启动: {len(df)} 条K线, 市场={market}, 策略={strategy_names}")

    # ---- ① 技术指标预计算 ----
    logger.info("  [管道 1/4] 计算技术指标（MA/MACD/RSI/布林带/KDJ）...")
    df = calc_all_indicators(df)
    indicator_cols = [c for c in df.columns if c not in
                      ("date", "open", "high", "low", "close", "volume", "code")]
    logger.info(f"  [管道 1/4] 完成，共 {len(indicator_cols)} 个指标列")

    # ---- ② Alpha 因子打分 ----
    logger.info(f"  [管道 2/4] Alpha 多因子打分 (w_tech={w_tech}, w_news={w_news})...")
    df = calc_final_score(df, news_df, w_tech=w_tech, w_news=w_news)

    # ---- ③ Rank IC 计算 ----
    logger.info("  [管道 3/4] 计算 Rank IC（因子有效性）...")
    rank_ic = {"rank_ic_mean": 0.0, "ic_ir": 0.0}
    if "close" in df.columns:
        forward_returns = df["close"].pct_change().shift(-1)
        scores_aligned = df["Final_Score"].iloc[:-1]
        returns_aligned = forward_returns.iloc[:-1]
        rank_ic = compute_rank_ic(scores_aligned, returns_aligned)
        logger.info(
            f"  [管道 3/4] Rank IC 均值={rank_ic['rank_ic_mean']:.4f}, "
            f"IC_IR={rank_ic['ic_ir']:.4f}"
        )
    else:
        logger.warning("  [管道 3/4] 缺少 close 列，跳过 Rank IC 计算")

    # ---- ④ 基准收益 ----
    benchmark_return = 0.0
    if len(df) > 1:
        benchmark_return = (
            (float(df["close"].iloc[-1]) - float(df["close"].iloc[0]))
            / float(df["close"].iloc[0])
        )

    # ---- ⑤ 三策略并行回测 ----
    logger.info(f"  [管道 4/4] 三策略并行回测 (初始资金={initial_capital:,.0f})...")
    config = BacktestConfig(initial_capital=initial_capital)
    if market == "US":
        # 美股不设涨跌停限制
        config.broker.limit_up_pct = 999.0
        config.broker.limit_down_pct = 999.0

    engine = BacktestEngine(config)
    strategies = [get_execution_strategy(name) for name in strategy_names]
    results = engine.run_multi(df, strategies, news_df)
    comparison = compare_strategies(results)

    # 打印各策略概要
    for name, r in results.items():
        logger.info(
            f"  [管道 4/4] {name}: 收益={r.total_return*100:+.2f}%, "
            f"夏普={r.sharpe_ratio:.2f}, 回撤={r.max_drawdown*100:.2f}%, "
            f"交易={r.total_trades}次"
        )

    logger.info(f"管道完成: 基准收益={benchmark_return*100:+.2f}%")
    logger.info("=" * 50)

    return AnalysisResult(
        df=df,
        backtest=results,
        comparison=comparison,
        rank_ic=rank_ic,
        benchmark_return=benchmark_return,
    )
