"""
绩效分析模块。

提供回测绩效指标计算和多策略横向对比功能：
  - 年化收益、夏普比率、最大回撤、Calmar 比率
  - 胜率、盈亏比、平均持仓周期
  - Rank IC 均值及 IC_IR（因子有效性）
  - 策略相关性矩阵
  - 多策略净值曲线叠加图
"""

import logging
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.025


def compute_metrics(
    equity_curve: list[dict],
    trades: list[dict],
    initial_capital: float,
    benchmark_return: float = 0.0,
    trading_days: int = 252,
) -> dict:
    """
    计算全部绩效指标。

    Returns:
        包含所有指标的 dict
    """
    if not equity_curve:
        return _empty_metrics()

    equities = [e["equity"] for e in equity_curve]

    total_return = (equities[-1] - initial_capital) / initial_capital if equities else 0.0

    # 年化收益
    if trading_days > 0 and total_return > -1:
        annual_return = (1 + total_return) ** (TRADING_DAYS_PER_YEAR / trading_days) - 1
    else:
        annual_return = 0.0

    max_dd = _calc_max_drawdown(equities)
    sharpe = _calc_sharpe(equities)
    calmar = annual_return / max_dd if max_dd > 0 else 0.0

    win_rate, profit_loss_ratio, avg_holding = _calc_trade_stats(trades)

    # 超额收益 (Alpha)
    excess_return = total_return - benchmark_return

    return {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "win_rate": round(win_rate, 4),
        "profit_loss_ratio": round(profit_loss_ratio, 2),
        "avg_holding_days": round(avg_holding, 1),
        "excess_return": round(excess_return, 4),
        "benchmark_return": round(benchmark_return, 4),
    }


def compute_rank_ic(scores: pd.Series, forward_returns: pd.Series) -> dict:
    """
    计算因子 Rank IC 和 IC_IR。

    Rank IC = Spearman 相关系数（Final_Score 与次日收益率）
    IC_IR = mean(Rank_IC) / std(Rank_IC)

    Args:
        scores: 每日 Final_Score（对齐到 T 日）
        forward_returns: T+1 日收益率

    Returns:
        {"rank_ic_mean": float, "ic_ir": float, "ic_series": list}
    """
    if len(scores) < 2 or len(forward_returns) < 2:
        return {"rank_ic_mean": 0.0, "ic_ir": 0.0, "ic_series": []}

    # 对齐到相同索引
    common_idx = scores.dropna().index.intersection(forward_returns.dropna().index)
    if len(common_idx) < 10:
        return {"rank_ic_mean": 0.0, "ic_ir": 0.0, "ic_series": []}

    s = scores.loc[common_idx]
    f = forward_returns.loc[common_idx]

    # 滚动 20 日 Rank IC
    ic_series = []
    for i in range(20, len(common_idx)):
        s_window = s.iloc[i - 20:i]
        f_window = f.iloc[i - 20:i]
        # 手动 Spearman 秩相关，避免依赖 scipy
        ic = _spearman_rank_ic(s_window, f_window)
        ic_series.append(ic)

    if not ic_series:
        return {"rank_ic_mean": 0.0, "ic_ir": 0.0, "ic_series": []}

    ic_arr = np.array([x for x in ic_series if not pd.isna(x)])
    if len(ic_arr) == 0:
        return {"rank_ic_mean": 0.0, "ic_ir": 0.0, "ic_series": []}

    ic_mean = float(np.mean(ic_arr))
    ic_std = float(np.std(ic_arr, ddof=1))
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0

    return {
        "rank_ic_mean": round(ic_mean, 4),
        "ic_ir": round(ic_ir, 4),
        "ic_series": [round(x, 4) for x in ic_arr],
    }


def compare_strategies(results: dict) -> dict:
    """
    多策略横向对比。

    Returns:
        {
            "summary_table": list[dict],
            "correlation_matrix": pd.DataFrame,
            "daily_returns": dict[str, list],
        }
    """
    summary = []
    daily_returns = {}

    for name, result in results.items():
        summary.append({
            "策略": name,
            "年化收益": f"{result.annual_return*100:+.2f}%",
            "夏普比率": f"{result.sharpe_ratio:.2f}",
            "最大回撤": f"{result.max_drawdown*100:.2f}%",
            "Calmar比率": f"{result.calmar_ratio:.2f}",
            "胜率": f"{result.win_rate*100:.1f}%",
            "盈亏比": f"{result.profit_loss_ratio:.2f}",
            "交易次数": str(result.total_trades),
            "总收益": f"{result.total_return*100:+.2f}%",
            "平均持仓(天)": f"{result.avg_holding_days:.0f}",
        })

        # 提取日收益率序列
        if result.equity_curve:
            eq = [e["equity"] for e in result.equity_curve]
            rets = []
            for i in range(1, len(eq)):
                if eq[i - 1] > 0:
                    rets.append((eq[i] - eq[i - 1]) / eq[i - 1])
                else:
                    rets.append(0.0)
            daily_returns[name] = rets

    # 策略日收益率相关性矩阵
    corr_matrix = None
    if len(daily_returns) >= 2:
        min_len = min(len(v) for v in daily_returns.values())
        aligned = {}
        for name, rets in daily_returns.items():
            aligned[name] = rets[:min_len]
        corr_df = pd.DataFrame(aligned)
        corr_matrix = corr_df.corr()

    return {
        "summary_table": summary,
        "correlation_matrix": corr_matrix,
        "daily_returns": daily_returns,
    }


def plot_comparison(results: dict, benchmark_equity: list[float] | None = None,
                    save_path: str = "") -> str:
    """
    绘制多策略净值曲线叠加图。

    Args:
        results: 策略回测结果字典
        benchmark_equity: 买入持有基准净值曲线
        save_path: 图片保存路径，为空则自动生成

    Returns:
        图片文件路径
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # 尝试使用中文字体
        try:
            from utils.fonts import get_chinese_font_path
            font_path = get_chinese_font_path()
            if font_path:
                from matplotlib.font_manager import FontProperties
                font_prop = FontProperties(fname=font_path)
                plt.rcParams["font.family"] = font_prop.get_name()
        except Exception:
            pass

        plt.rcParams["axes.unicode_minus"] = False
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))

        # ---- 子图 1：净值曲线 ----
        ax1 = axes[0, 0]
        colors = ["#2196F3", "#4CAF50", "#FF9800"]

        for idx, (name, result) in enumerate(results.items()):
            if result.equity_curve:
                eq = [e["equity"] / result.initial_capital for e in result.equity_curve]
                ax1.plot(eq, label=name, color=colors[idx % len(colors)], linewidth=1.5)

        if benchmark_equity:
            ax1.plot(benchmark_equity, label="Buy & Hold", color="gray",
                     linestyle="--", linewidth=1)

        ax1.set_title("净值曲线对比", fontsize=13, fontweight="bold")
        ax1.set_xlabel("交易日")
        ax1.set_ylabel("净值")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        ax1.axhline(y=1.0, color="black", linestyle=":", alpha=0.5)

        # ---- 子图 2：回撤曲线 ----
        ax2 = axes[0, 1]
        for idx, (name, result) in enumerate(results.items()):
            if result.equity_curve:
                eq = [e["equity"] for e in result.equity_curve]
                dd = _calc_drawdown_series(eq)
                ax2.fill_between(range(len(dd)), 0, [d * 100 for d in dd],
                                 label=name, color=colors[idx % len(colors)], alpha=0.3)
                ax2.plot([d * 100 for d in dd], color=colors[idx % len(colors)], linewidth=1)

        ax2.set_title("回撤曲线", fontsize=13, fontweight="bold")
        ax2.set_xlabel("交易日")
        ax2.set_ylabel("回撤 (%)")
        ax2.legend(loc="lower left")
        ax2.grid(True, alpha=0.3)
        ax2.invert_yaxis()

        # ---- 子图 3：核心指标柱状图 ----
        ax3 = axes[1, 0]
        names = list(results.keys())
        x = np.arange(len(names))
        width = 0.2

        metrics_data = {
            "年化收益(%)": [r.annual_return * 100 for r in results.values()],
            "夏普比率": [r.sharpe_ratio for r in results.values()],
            "最大回撤(%)": [-r.max_drawdown * 100 for r in results.values()],
        }

        for i, (label, values) in enumerate(metrics_data.items()):
            bars = ax3.bar(x + i * width, values, width, label=label, alpha=0.85)

        ax3.set_title("核心指标对比", fontsize=13, fontweight="bold")
        ax3.set_xticks(x + width)
        ax3.set_xticklabels(names)
        ax3.legend(loc="best")
        ax3.grid(True, alpha=0.3, axis="y")
        ax3.axhline(y=0, color="black", linewidth=0.5)

        # ---- 子图 4：策略相关性热力图 ----
        ax4 = axes[1, 1]
        comparison = compare_strategies(results)
        corr = comparison.get("correlation_matrix")
        if corr is not None and not corr.empty:
            im = ax4.imshow(corr.values, cmap="RdYlGn", vmin=-1, vmax=1, aspect="auto")
            ax4.set_xticks(range(len(corr.columns)))
            ax4.set_yticks(range(len(corr.index)))
            ax4.set_xticklabels(corr.columns, rotation=45)
            ax4.set_yticklabels(corr.index)
            for i in range(len(corr.index)):
                for j in range(len(corr.columns)):
                    ax4.text(j, i, f"{corr.values[i, j]:.2f}",
                             ha="center", va="center", fontsize=10)
            plt.colorbar(im, ax=ax4, shrink=0.8)
        ax4.set_title("策略日收益率相关性", fontsize=13, fontweight="bold")

        plt.tight_layout()

        if not save_path:
            from config.settings import Settings
            save_path = f"{Settings().chart_dir}/backtest_comparison.png"

        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info(f"回测对比图已保存: {save_path}")
        return save_path

    except ImportError:
        logger.warning("matplotlib 不可用，跳过图表绘制")
        return ""


def _spearman_rank_ic(a: pd.Series, b: pd.Series) -> float:
    """手动计算 Spearman 秩相关系数，不依赖 scipy。"""
    a_rank = a.rank()
    b_rank = b.rank()
    n = len(a_rank)
    if n < 3:
        return 0.0
    # Pearson corr of ranks = Spearman
    mean_a, mean_b = a_rank.mean(), b_rank.mean()
    num = ((a_rank - mean_a) * (b_rank - mean_b)).sum()
    den = np.sqrt(((a_rank - mean_a) ** 2).sum() * ((b_rank - mean_b) ** 2).sum())
    if den == 0:
        return 0.0
    return float(num / den)


# ======================== 内部工具函数 ========================

def _calc_max_drawdown(equities: list[float]) -> float:
    if not equities:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for val in equities:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calc_drawdown_series(equities: list[float]) -> list[float]:
    """计算回撤序列。"""
    if not equities:
        return []
    peak = equities[0]
    dd_series = []
    for val in equities:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        dd_series.append(dd)
    return dd_series


def _calc_sharpe(equities: list[float]) -> float:
    if len(equities) < 2:
        return 0.0
    returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] != 0:
            returns.append((equities[i] - equities[i - 1]) / equities[i - 1])
    if not returns or np.std(returns) == 0:
        return 0.0
    avg_ret = np.mean(returns)
    std_ret = np.std(returns, ddof=1)
    daily_rf = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
    return float((avg_ret - daily_rf) / std_ret * np.sqrt(TRADING_DAYS_PER_YEAR))


def _calc_trade_stats(trades: list[dict]) -> tuple[float, float, float]:
    """
    计算胜率、盈亏比、平均持仓天数。

    Returns:
        (win_rate, profit_loss_ratio, avg_holding_days)
    """
    if not trades:
        return 0.0, 0.0, 0.0

    completed = [t for t in trades if "pnl" in t]
    if not completed:
        return 0.0, 0.0, 0.0

    wins = [t for t in completed if t.get("pnl", 0) > 0]
    losses = [t for t in completed if t.get("pnl", 0) <= 0]

    win_rate = len(wins) / len(completed)

    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 1
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

    # 平均持仓天数
    holding_days = []
    for t in completed:
        try:
            entry = pd.Timestamp(t.get("entry_date", ""))
            exit_ = pd.Timestamp(t.get("exit_date", ""))
            holding_days.append((exit_ - entry).days)
        except Exception:
            pass
    avg_holding = float(np.mean(holding_days)) if holding_days else 0.0

    return win_rate, float(profit_loss_ratio), avg_holding


def _empty_metrics() -> dict:
    return {
        "total_return": 0.0, "annual_return": 0.0,
        "max_drawdown": 0.0, "sharpe_ratio": 0.0,
        "calmar_ratio": 0.0, "win_rate": 0.0,
        "profit_loss_ratio": 0.0, "avg_holding_days": 0.0,
        "excess_return": 0.0, "benchmark_return": 0.0,
    }
