"""
核心分析管道 — CLI 和 UI 共用的纯计算逻辑。

将数据获取（I/O）与计算（纯函数）严格分离。
此模块不执行任何网络请求或数据库操作。
"""

import logging

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime

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
    rank_ic: dict                             # 1日 Rank IC 统计
    rank_ic_5d: dict = field(default_factory=lambda: {"rank_ic_mean": 0.0, "ic_ir": 0.0})
    rank_ic_10d: dict = field(default_factory=lambda: {"rank_ic_mean": 0.0, "ic_ir": 0.0})
    benchmark_return: float = 0.0
    validation: dict = field(default_factory=dict)
    fundamental_data: dict | None = None


def run_pipeline(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    initial_capital: float = 100000.0,
    market: str = "A",
    strategy_names: list[str] | None = None,
    w_tech: float = 0.6,
    w_news: float = 0.4,
    fundamental_data: dict | None = None,
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
        strategy_names = ["A", "B", "C", "D", "E", "F"]

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
    df = calc_final_score(df, news_df, w_tech=w_tech, w_news=w_news,
                          fundamental_data=fundamental_data)

    # ---- ③ Rank IC 计算（多周期：1/5/10 日远期收益） ----
    logger.info("  [管道 3/4] 计算多周期 Rank IC（因子有效性）...")
    rank_ic = {"rank_ic_mean": 0.0, "ic_ir": 0.0}
    rank_ic_5d = {"rank_ic_mean": 0.0, "ic_ir": 0.0}
    rank_ic_10d = {"rank_ic_mean": 0.0, "ic_ir": 0.0}

    if "close" in df.columns:
        scores_aligned = df["Final_Score"].iloc[:-10]  # 最短对齐到 10 日

        # 1 日 Rank IC
        fwd_1d = df["close"].pct_change().shift(-1).iloc[:-10]
        rank_ic = compute_rank_ic(scores_aligned, fwd_1d)

        # 5 日 Rank IC
        fwd_5d = (df["close"].shift(-5) / df["close"] - 1).iloc[:-10]
        rank_ic_5d = compute_rank_ic(scores_aligned, fwd_5d)

        # 10 日 Rank IC
        fwd_10d = (df["close"].shift(-10) / df["close"] - 1).iloc[:-10]
        rank_ic_10d = compute_rank_ic(scores_aligned, fwd_10d)

        logger.info(
            f"  [管道 3/4] Rank IC: "
            f"1日={rank_ic['rank_ic_mean']:+.4f} "
            f"5日={rank_ic_5d['rank_ic_mean']:+.4f} "
            f"10日={rank_ic_10d['rank_ic_mean']:+.4f}"
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

    # 提取因子检验结果
    validation = {}
    norm_cols = [c for c in df.columns if c.endswith("_norm")]
    if norm_cols and "close" in df.columns:
        from alpha.validation import validate_factors
        validation = validate_factors(df)

    return AnalysisResult(
        df=df,
        backtest=results,
        comparison=comparison,
        rank_ic=rank_ic,
        rank_ic_5d=rank_ic_5d,
        rank_ic_10d=rank_ic_10d,
        benchmark_return=benchmark_return,
        validation=validation,
        fundamental_data=fundamental_data,
    )


# ============================================================
# 盘中 / 盘前快照计算（纯函数，无 I/O）
#
# 这些函数接收 T-1 日完整分析结果 + 实时数据，
# 输出结构化的 Markdown 快照文本，供报告生成模块嵌入。
#
# 核心设计原则：
#   - 不重新计算技术指标 / Alpha / 回测
#   - 只做「实时价 vs T-1 日关键点位」的位置判断
#   - 对每个数据点给出解读，不是只罗列数值
# ============================================================

@dataclass
class IntradaySnapshot:
    """盘中实时快照（结构化数据 + Markdown 文本）。"""
    markdown: str                  # 格式化后的 Markdown 文本
    latest_price: float = 0.0
    change_pct: float = 0.0
    depth_imbalance: float = 1.0
    depth_score: float = 0.0
    t1_final_score: float = 0.0
    t1_macd_status: str = ""
    t1_rsi: float = 50.0
    session: str = "intraday"


@dataclass
class PremarketSnapshot:
    """盘前快照（结构化数据 + Markdown 文本）。"""
    markdown: str                  # 格式化后的 Markdown 文本
    pre_price: float = 0.0
    pre_change_pct: float = 0.0
    nq_change_pct: float = 0.0
    es_change_pct: float = 0.0
    t1_final_score: float = 0.0
    session: str = "pre"


def compute_intraday_snapshot(
    pipeline_result: "AnalysisResult",
    realtime_quote: dict,
    depth_factor: dict | None = None,
    today_news: list | None = None,
    session: str = "intraday",
    market: str = "US",
) -> IntradaySnapshot:
    """
    计算盘中实时位置快照（美股专属差异化解读）。

    美股 24 小时交易，盘中数据具备完整参考价值：
      - 实时价 vs T-1 均线：当下多空力量对比的直接体现
      - 盘口买卖比：实时博弈信号，>1.2 买盘主导，<0.8 卖盘主导
      - 成交量（量比）：>1.5 资金活跃，<0.5 观望
      - 盘口与价格背离时需特别标注（如买盘大但价格跌=托盘非进攻）
      - 日内走势形态：高开低走/低开高走/窄幅震荡
    """
    df = pipeline_result.df
    if df.empty or len(df) < 20:
        return IntradaySnapshot(
            markdown="> ⚠️ T-1 日数据不足，无法生成盘中快照。",
            session=session,
        )

    last = df.iloc[-1]  # T-1 日最后一行
    latest = realtime_quote.get("latest", 0)
    prev_close = realtime_quote.get("prev_close", 0) or 1
    change_pct = realtime_quote.get("change_pct", 0)
    volume_today = realtime_quote.get("volume", 0)

    if latest <= 0:
        return IntradaySnapshot(
            markdown="> ⚠️ 无法获取实时报价，盘中快照不可用。",
            session=session,
        )

    # ── 辅助函数：计算当前价 vs 某价位的偏离百分比 ──
    def _vs(value: float, label: str, extra: str = "") -> str:
        """返回「距某价位 x%」的解读文本。"""
        if not value or value <= 0 or pd.isna(value):
            return "—"
        pct = (latest - value) / value * 100
        direction = "上方" if pct > 0 else "下方"
        return f"{pct:+.2f}%（{value:.2f} {direction}）"

    def _vs_ma(ma_col: str, label: str) -> dict:
        """计算当前价 vs 均线的偏离，返回 {value, pct, relation} 供表格使用。"""
        val = last.get(ma_col)
        if val is None or pd.isna(val) or val <= 0:
            return {"value_str": "—", "pct": 0, "interpret": "数据不可用"}
        pct = (latest - float(val)) / float(val) * 100
        if pct > 2:
            interpret = f"✓ 高于{label} {pct:+.1f}%，短期强势"
        elif pct > 0:
            interpret = f"▸ 略高于{label}，趋势保持"
        elif pct > -2:
            interpret = f"▸ 略低于{label}，轻微走弱"
        elif pct > -5:
            interpret = f"⚠️ 跌破{label} {pct:.1f}%，短期转弱"
        else:
            interpret = f"🔴 大幅低于{label} {pct:.1f}%，趋势破位风险"
        return {
            "value_str": f"{float(val):.2f}（{pct:+.2f}%）",
            "pct": pct,
            "interpret": interpret,
        }

    # ── 均线位置 ──
    ma5 = _vs_ma("ma_5", "MA5")
    ma10 = _vs_ma("ma_10", "MA10")
    ma20 = _vs_ma("ma_20", "MA20")
    ma60 = _vs_ma("ma_60", "MA60")

    # ── 布林带位置 ──
    bb_upper = last.get("bb_upper")
    bb_lower = last.get("bb_lower")
    bb_mid = last.get("bb_mid")
    bb_str = "—"
    bb_interpret = "数据不可用"
    if (bb_upper and bb_lower and bb_mid
            and pd.notna(bb_upper) and pd.notna(bb_lower) and pd.notna(bb_mid)
            and float(bb_upper) != float(bb_lower)):
        upper_v, lower_v, mid_v = float(bb_upper), float(bb_lower), float(bb_mid)
        bb_position = (latest - lower_v) / (upper_v - lower_v) * 100
        bb_str = f"{bb_position:.0f}%（上轨 {upper_v:.2f} / 下轨 {lower_v:.2f}）"
        if latest > upper_v:
            bb_interpret = "🔴 突破上轨，短期过热，注意回调压力"
        elif latest < lower_v:
            bb_interpret = "🟢 跌破下轨，短期超卖，关注反弹机会"
        elif bb_position > 70:
            bb_interpret = f"▸ 偏上轨区域({bb_position:.0f}%)，短期偏强运行"
        elif bb_position < 30:
            bb_interpret = f"▸ 偏下轨区域({bb_position:.0f}%)，短期偏弱，接近支撑"
        elif bb_position > 40:
            bb_interpret = f"▸ 中线偏上({bb_position:.0f}%)，健康运行"
        else:
            bb_interpret = f"▸ 中线偏下({bb_position:.0f}%)，动能不足"

    # ── 日内状态 ──
    intraday_high = realtime_quote.get("high", 0)
    intraday_low = realtime_quote.get("low", 0)
    intraday_range = ""
    if intraday_high and intraday_low and prev_close:
        rng_pct = (intraday_high - intraday_low) / prev_close * 100
        if rng_pct > 5:
            intraday_range = f"{rng_pct:.1f}%（剧烈波动，注意风险）"
        elif rng_pct > 3:
            intraday_range = f"{rng_pct:.1f}%（波动较大）"
        elif rng_pct > 1:
            intraday_range = f"{rng_pct:.1f}%（波动正常）"
        else:
            intraday_range = f"{rng_pct:.1f}%（窄幅震荡）"

    # ── 量比 ──
    vol_ratio_str = "—"
    if df["volume"].notna().any():
        avg_vol_5d = float(df["volume"].iloc[-6:-1].mean()) if len(df) >= 6 else 0
        if avg_vol_5d > 0:
            vol_ratio = volume_today / avg_vol_5d
            if vol_ratio > 2.0:
                vol_ratio_str = f"{vol_ratio:.2f}x（显著放量，市场分歧加大）"
            elif vol_ratio > 1.2:
                vol_ratio_str = f"{vol_ratio:.2f}x（温和放量，参与度提升）"
            elif vol_ratio > 0.5:
                vol_ratio_str = f"{vol_ratio:.2f}x（正常偏缩量）"
            else:
                vol_ratio_str = f"{vol_ratio:.2f}x（极度缩量，观望浓厚）"

    # ── 盘口 ──（美股专属：增加背离分析）
    depth_imbalance = 1.0
    depth_score = 0.0
    depth_str = "数据不可用"
    if depth_factor and depth_factor.get("available"):
        depth_imbalance = depth_factor.get("imbalance", 1.0)
        depth_score = depth_factor.get("depth_score", 0.0)
        bid_v = depth_factor.get("bid_volume", 0)
        ask_v = depth_factor.get("ask_volume", 0)

        # 背离检测：盘口方向 vs 价格方向
        divergence = ""
        if market == "US":
            if depth_imbalance > 1.2 and change_pct < -0.003:
                divergence = " ⚠️ 盘口与价格背离：挂单买盘占优但价格下跌，说明托盘非进攻，主动性卖单仍在成交价上占优"
            elif depth_imbalance < 0.8 and change_pct > 0.003:
                divergence = " ⚠️ 盘口与价格背离：卖盘挂单占优但价格上涨，说明被动卖单被主动买盘消化，偏多信号"

        if depth_imbalance > 3.0:
            depth_str = f"🟢 极端买盘占优（{depth_imbalance:.2f}:1）{divergence}"
        elif depth_imbalance > 1.5:
            depth_str = f"🟢 买盘显著占优（{depth_imbalance:.2f}:1），买方主动性强{divergence}"
        elif depth_imbalance > 1.2:
            depth_str = f"▸ 买盘占优（{depth_imbalance:.2f}:1），偏多信号{divergence}"
        elif depth_imbalance > 1.05:
            depth_str = f"▸ 微幅偏买（{depth_imbalance:.2f}:1），倾向不够强"
        elif depth_imbalance > 0.95:
            depth_str = f"▸ 基本平衡（{depth_imbalance:.2f}:1），买卖力量相当"
        elif depth_imbalance > 0.80:
            depth_str = f"▸ 微幅偏卖（{depth_imbalance:.2f}:1），卖方略主动"
        elif depth_imbalance > 0.67:
            depth_str = f"⚠️ 卖盘占优（{depth_imbalance:.2f}:1），偏空信号{divergence}"
        elif depth_imbalance > 0.33:
            depth_str = f"🔴 卖盘显著占优（{depth_imbalance:.2f}:1），卖方主导{divergence}"
        else:
            depth_str = f"🔴 极端卖盘占优（{depth_imbalance:.2f}:1）{divergence}"

    # ── T-1 日关键信号 ──
    t1_final_score = float(last.get("Final_Score", 0)) if pd.notna(last.get("Final_Score")) else 0.0
    dif = last.get("dif")
    dea = last.get("dea")
    if pd.notna(dif) and pd.notna(dea):
        t1_macd_status = "金叉（偏多）" if float(dif) > float(dea) else "死叉（偏空）"
    else:
        t1_macd_status = "数据不可用"
    t1_rsi = float(last.get("rsi", 50)) if pd.notna(last.get("rsi")) else 50.0
    k_val = last.get("k")
    d_val = last.get("d")
    if pd.notna(k_val) and pd.notna(d_val):
        t1_kdj = f"K={float(k_val):.1f} D={float(d_val):.1f}（{'K>D 偏多' if float(k_val) > float(d_val) else 'K<D 偏空'}）"
    else:
        t1_kdj = "数据不可用"

    # ADX
    adx_val = None
    atr_val = None
    try:
        import ta
        try:
            adx_series = ta.trend.ADXIndicator(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
            ).adx()
            if not adx_series.empty and pd.notna(adx_series.iloc[-1]):
                adx_val = float(adx_series.iloc[-1])
        except Exception:
            pass
        try:
            atr_series = ta.volatility.AverageTrueRange(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
            ).average_true_range()
            if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
                atr_val = float(atr_series.iloc[-1])
        except Exception:
            pass
    except ImportError:
        pass

    adx_text = "数据不可用"
    if adx_val is not None:
        if adx_val > 25:
            adx_text = f"{adx_val:.1f}（趋势明显，方向性信号可信度较高）"
        elif adx_val > 20:
            adx_text = f"{adx_val:.1f}（趋势中性，方向正在形成）"
        else:
            adx_text = f"{adx_val:.1f}（震荡为主，趋势信号可靠性偏低）"
    atr_text = "数据不可用"
    if atr_val is not None and latest > 0:
        atr_pct = atr_val / latest * 100
        atr_text = f"{atr_val:.2f}（约占股价 {atr_pct:.2f}%）"

    # ── 构建 Markdown ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t1_date = ""
    if "date" in df.columns:
        t1_dt = df["date"].iloc[-1]
        try:
            t1_date = pd.Timestamp(t1_dt).strftime("%Y-%m-%d")
        except Exception:
            t1_date = str(t1_dt)[:10]

    lines = [
        f"## ⚡ 盘中实时快照",
        f"",
        f"> ⏰ 快照时间：{now_str} | 分析基底：T-1 日（{t1_date}）收盘后完整分析",
        f"",
        f"### 实时价格位置",
        f"",
        f"| 项目 | 数值 | 解读 |",
        f"|------|------|------|",
        f"| 最新价 | **{latest:.2f}**（{change_pct:+.2%}） | — |",
    ]

    # 均线行
    ma_items = [
        ("MA5（5日均线）", ma5),
        ("MA10（10日均线）", ma10),
        ("MA20（20日均线）", ma20),
        ("MA60（60日均线）", ma60),
    ]
    for label, data in ma_items:
        if data["value_str"] != "—":
            lines.append(f"| {label} | {data['value_str']} | {data['interpret']} |")

    # 布林
    if bb_str != "—":
        lines.append(f"| 布林带位置 | {bb_str} | {bb_interpret} |")

    # 日内
    if intraday_range:
        lines.append(f"| 日内振幅 | {intraday_range} | — |")
    if vol_ratio_str != "—":
        lines.append(f"| 量比（vs 5日均量） | {vol_ratio_str} | — |")

    # 盘口
    if depth_factor and depth_factor.get("available"):
        lines.append(f"| 盘口买卖比 | 买 {depth_factor.get('bid_volume', 0):,.0f} / 卖 {depth_factor.get('ask_volume', 0):,.0f} = {depth_imbalance:.2f} | {depth_str} |")

    lines.append(f"")

    # ── 盘中走势形态（美股专属：从当天开盘→最新价推断） ──
    if market == "US" and realtime_quote.get("open", 0) > 0:
        open_price = realtime_quote["open"]
        high_price = realtime_quote.get("high", 0)
        low_price = realtime_quote.get("low", 0)
        prev_close = realtime_quote.get("prev_close", 1)

        open_gap = (open_price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        range_from_open = (latest - open_price) / open_price * 100 if open_price > 0 else 0

        # 形态判断
        if open_gap > 1.0 and range_from_open < -0.5:
            pattern = f"高开低走——开盘跳涨{open_gap:+.1f}%后持续回落至{range_from_open:+.1f}%，典型「利好兑现」或「冲高遇阻」格局"
        elif open_gap > 1.0 and range_from_open > 0:
            pattern = f"高开高走——开盘跳涨{open_gap:+.1f}%，日内继续走强至{range_from_open:+.1f}%，强势确认"
        elif open_gap < -1.0 and range_from_open > 0.5:
            pattern = f"低开高走——开盘跳跌{open_gap:+.1f}%后反弹{range_from_open:+.1f}%，有资金逢低吸纳"
        elif open_gap < -1.0 and range_from_open < -0.5:
            pattern = f"低开低走——开盘跳跌{open_gap:+.1f}%，日内持续走弱至{range_from_open:+.1f}%，空方主导"
        elif abs(open_gap) < 0.5 and abs(range_from_open) < 0.5:
            pattern = f"窄幅震荡——开盘基本平开，日内振幅仅{abs(range_from_open):.1f}%，方向不明确"
        else:
            pattern = f"开盘涨跌{open_gap:+.1f}%，当前偏离开盘{range_from_open:+.1f}%"

        high_low_range = (high_price - low_price) / prev_close * 100 if prev_close > 0 and high_price > 0 and low_price > 0 else 0
        pattern += f"，日内振幅{high_low_range:.1f}%"

        lines.append(f"### 盘中走势形态")
        lines.append(f"")
        lines.append(f"开盘 {open_price:.2f}（{open_gap:+.1f}%）→ 最高 {high_price:.2f} → 最低 {low_price:.2f} → 最新 {latest:.2f}")
        lines.append(f"")
        lines.append(f"**形态：{pattern}**")
        lines.append(f"")

    lines.append(f"### T-1 日关键信号回顾")
    lines.append(f"")
    lines.append(f"| 指标 | T-1 日数值 | 信号解读 |")
    lines.append(f"|------|-----------|---------|")
    score_label = "偏多" if t1_final_score > 0.15 else ("偏空" if t1_final_score < -0.15 else "中性")
    lines.append(f"| Alpha Final_Score | {t1_final_score:+.3f}（{score_label}） | 多因子模型对 T-1 日收盘的综合评估 |")
    lines.append(f"| MACD | DIF={float(dif):.2f} DEA={float(dea):.2f} | {t1_macd_status} |")
    rsi_label = "超买" if t1_rsi > 70 else ("超卖" if t1_rsi < 30 else "中性")
    lines.append(f"| RSI(14) | {t1_rsi:.1f}（{rsi_label}） | 短期动量状态 |")
    lines.append(f"| KDJ | {t1_kdj} | 随机指标方向 |")
    lines.append(f"| ADX（趋势强度） | {adx_text} | — |")
    lines.append(f"| ATR（波动率） | {atr_text} | — |")

    # 今日增量新闻
    if today_news:
        lines.append(f"")
        lines.append(f"### 今日增量新闻")
        lines.append(f"")
        lines.append(f"| 时间 | 标题 | 情感 |")
        lines.append(f"|------|------|------|")
        for n in today_news[:5]:
            sentiment = getattr(n, "sentiment", "") or "未分析"
            sentiment_label = {"positive": "正面", "negative": "负面", "neutral": "中性"}.get(sentiment, sentiment)
            title = getattr(n, "title", "")[:60]
            date_str = str(getattr(n, "date", ""))[:16]
            lines.append(f"| {date_str} | {title} | {sentiment_label} |")

    markdown = "\n".join(lines)
    logger.info(
        f"盘中快照: 最新价={latest:.2f} ({change_pct:+.2%}), "
        f"vs MA20={ma20.get('pct', 0):+.2f}%, "
        f"盘口={depth_imbalance:.2f}, T-1 Score={t1_final_score:+.3f}"
    )

    return IntradaySnapshot(
        markdown=markdown,
        latest_price=latest,
        change_pct=change_pct,
        depth_imbalance=depth_imbalance,
        depth_score=depth_score,
        t1_final_score=t1_final_score,
        t1_macd_status=t1_macd_status,
        t1_rsi=t1_rsi,
        session=session,
    )


def compute_premarket_snapshot(
    pipeline_result: "AnalysisResult",
    stock_tick: dict,
    futures_data: dict[str, dict],
    overnight_news: list | None = None,
    session: str = "pre",
    market: str = "US",
) -> PremarketSnapshot:
    """
    计算盘前快照（美股专属）。

    美股 24 小时交易，盘前数据具备参考价值：
      - 盘前价格 + 成交量反映隔夜资金意图
      - 盘口买卖比方向（非绝对值）反映隔夜挂单方向
      - 期货是盘前最重要的方向锚——期货走势是判断开盘方向最可靠的信号
      - 盘前价格与期货相对强弱判断是否有独立资金行为
      - 距均线的跳空幅度预示开盘后回踩/延续概率
    """
    df = pipeline_result.df
    pre_price = stock_tick.get("latest", 0)

    # 通过 quote 推算涨跌幅（tick 只有 latest 没有 prev_close）
    pre_change_pct = 0.0
    prev_close = 1.0
    if pre_price > 0 and pipeline_result.df is not None and len(pipeline_result.df) > 0:
        t1_close = float(pipeline_result.df["close"].iloc[-1])
        if t1_close > 0:
            pre_change_pct = (pre_price - t1_close) / t1_close
            prev_close = t1_close

    # ── 期货解读 ──
    nq = futures_data.get("NQ", {})
    es = futures_data.get("ES", {})

    nq_change = nq.get("change_pct", 0) if nq else 0  # 小数：0.0069 = 0.69%
    es_change = es.get("change_pct", 0) if es else 0

    def _pct_str(val: float) -> str:
        """小数 → 百分数显示字符串，如 0.0069 → '+0.69%'。"""
        return f"{val * 100:+.2f}%"

    def _future_assess(chg: float, name: str) -> str:
        """对期货涨跌给出宏观情绪解读。chg 为小数（0.01 = 1%）。"""
        if chg > 0.01:
            return f"🟢 {name}涨{_pct_str(chg)}，宏观情绪显著偏暖，利好开盘"
        elif chg > 0.005:
            return f"▸ {name}涨{_pct_str(chg)}，情绪偏暖"
        elif chg > 0.001:
            return f"▸ {name}微涨{_pct_str(chg)}，方向不明确"
        elif chg > -0.001:
            return f"▸ {name}基本持平{_pct_str(chg)}，无明显方向"
        elif chg > -0.005:
            return f"▸ {name}微跌{_pct_str(chg)}，情绪偏冷"
        elif chg > -0.01:
            return f"⚠️ {name}跌{_pct_str(chg)}，宏观情绪偏冷，谨慎开盘"
        else:
            return f"🔴 {name}跌{_pct_str(chg)}，宏观情绪显著偏空"

    def _describe_kline_trend(kline_list: list | None, label: str) -> str:
        """分析分时 K 线的短期走势形态。"""
        if not kline_list or len(kline_list) < 3:
            return f"数据不足，无法判断{label}盘前走势"
        closes = [bar.get("c", bar.get("close", 0)) for bar in kline_list]
        opens = [bar.get("o", bar.get("open", 0)) for bar in kline_list]
        first = closes[0] if closes[0] else opens[0]
        last = closes[-1] if closes[-1] else opens[-1]
        if first <= 0 or last <= 0:
            return f"{label}走势数据异常"

        start_end_pct = (last - first) / first * 100
        # 看内部结构：逐根 bar 的 O→C 方向
        up_bars = sum(1 for i in range(len(kline_list))
                      if closes[i] and opens[i] and closes[i] >= opens[i])
        down_bars = len(kline_list) - up_bars

        direction = "上行" if start_end_pct > 0.3 else (
            "下行" if start_end_pct < -0.3 else "横盘震荡")
        struct = ""
        if up_bars > down_bars * 1.5:
            struct = "阳线居多，买方逐步主导"
        elif down_bars > up_bars * 1.5:
            struct = "阴线居多，卖方压力较大"
        else:
            struct = "阴阳交替，多空拉锯"

        if start_end_pct > 1.0:
            detail = f"稳步走高 ↗（{start_end_pct:+.2f}%），{struct}"
        elif start_end_pct > 0.3:
            detail = f"小幅上行 ↗（{start_end_pct:+.2f}%），{struct}"
        elif start_end_pct < -1.0:
            detail = f"持续走低 ↘（{start_end_pct:+.2f}%），{struct}"
        elif start_end_pct < -0.3:
            detail = f"小幅下行 ↘（{start_end_pct:+.2f}%），{struct}"
        else:
            detail = f"横盘整理 →（{start_end_pct:+.2f}%），{struct}"
        return f"{direction}，{detail}"

    nq_kline = (nq or {}).get("kline_5min", [])
    es_kline = (es or {}).get("kline_5min", [])

    # ── 个股盘前解读 ──
    def _pre_stock_assess(chg: float, nq_chg: float, es_chg: float) -> str:
        """评估个股盘前表现相对于期货强弱。chg 均为小数（0.01 = 1%）。"""
        avg_future = (nq_chg + es_chg) / 2 if nq_chg and es_chg else 0
        diff = chg - avg_future
        if diff > 0.01:
            return (f"盘前涨跌{_pct_str(chg)}，显著强于期货整体（{_pct_str(avg_future)}），"
                    f"表明资金对该股有独立买入意愿，非被动跟涨")
        elif diff > 0.003:
            return f"盘前涨跌{_pct_str(chg)}，略强于期货（{_pct_str(avg_future)}），属于偏强跟随"
        elif diff > -0.003:
            return f"盘前涨跌{_pct_str(chg)}，与期货整体（{_pct_str(avg_future)}）基本同步，无独立方向"
        elif diff > -0.01:
            return (f"盘前涨跌{_pct_str(chg)}，弱于期货整体（{_pct_str(avg_future)}），"
                    f"可能有独立利空或资金减仓")
        else:
            return (f"盘前涨跌{_pct_str(chg)}，显著弱于期货（{_pct_str(avg_future)}），"
                    f"需警惕独立利空")

    # ── T-1 日关键信号 ──
    last = df.iloc[-1] if len(df) > 0 else None
    t1_final_score = 0.0
    t1_macd_status = ""
    if last is not None:
        t1_final_score = float(last.get("Final_Score", 0)) if pd.notna(last.get("Final_Score")) else 0.0
        dif = last.get("dif")
        dea = last.get("dea")
        if pd.notna(dif) and pd.notna(dea):
            t1_macd_status = "金叉（偏多）" if float(dif) > float(dea) else "死叉（偏空）"

    # ── 构建 Markdown ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t1_date = ""
    if last is not None and "date" in df.columns:
        t1_dt = df["date"].iloc[-1]
        try:
            t1_date = pd.Timestamp(t1_dt).strftime("%Y-%m-%d")
        except Exception:
            t1_date = str(t1_dt)[:10]

    lines = [
        f"## ⚡ 盘前快照",
        f"",
        f"> 🌅 快照时间：{now_str} | 分析基底：T-1 日（{t1_date}）收盘后完整分析",
        f"",
        f"### 期货风向标",
        f"",
        f"盘前时段，指数现货尚未开盘，期货是判断宏观情绪的核心参考。",
        f"纳指期货(NQ)代表科技成长股方向，标普期货(ES)代表大市值蓝筹整体。",
        f"",
        f"| 期货 | 最新价 | 涨跌幅 | 成交量 | 解读 |",
        f"|------|--------|--------|--------|------|",
    ]

    if nq:
        lines.append(
            f"| 纳指期货(NQ) | {nq.get('latest', 0):.2f} | "
            f"{_pct_str(nq_change)} | {nq.get('volume', 0):,.0f} | "
            f"{_future_assess(nq_change, '纳指期货')} |"
        )
    else:
        lines.append(f"| 纳指期货(NQ) | 数据不可用 | — | — | 请确认 itick token 有效 |")

    if es:
        lines.append(
            f"| 标普期货(ES) | {es.get('latest', 0):.2f} | "
            f"{_pct_str(es_change)} | {es.get('volume', 0):,.0f} | "
            f"{_future_assess(es_change, '标普期货')} |"
        )
    else:
        lines.append(f"| 标普期货(ES) | 数据不可用 | — | — | 请确认 itick token 有效 |")

    lines.append(f"")
    lines.append(f"### 期货盘前走势（5分钟K线，最近1小时）")
    lines.append(f"")
    lines.append(f"| 期货 | 走势描述 |")
    lines.append(f"|------|---------|")
    lines.append(f"| NQ | {_describe_kline_trend(nq_kline, 'NQ')} |")
    lines.append(f"| ES | {_describe_kline_trend(es_kline, 'ES')} |")

    lines.append(f"")
    lines.append(f"### 个股盘前")
    lines.append(f"")
    lines.append(f"| 项目 | 数值 | 盘前解读 |")
    lines.append(f"|------|------|---------|")
    lines.append(f"| 盘前价格 | {pre_price:.2f}（{_pct_str(pre_change_pct)}） | {_pre_stock_assess(pre_change_pct, nq_change, es_change)} |")
    pre_vol = stock_tick.get("volume", 0)
    if pre_vol > 200000:
        vol_label = "高度活跃——开盘波动可能剧烈"
    elif pre_vol > 100000:
        vol_label = "较活跃——盘前方向可信度较高"
    elif pre_vol > 10000:
        vol_label = "有成交——盘前方向有参考意义但需盘中确认"
    else:
        vol_label = "极低——盘前价格方向信号偏弱"
    lines.append(f"| 盘前成交量 | {pre_vol:,.0f} 股（{vol_label}） | — |")

    # 跳空均线分析
    if last is not None:
        for ma_col, ma_name in [("ma_5", "MA5"), ("ma_10", "MA10"), ("ma_20", "MA20")]:
            ma_val = last.get(ma_col)
            if ma_val is not None and pd.notna(ma_val) and ma_val > 0:
                gap = (pre_price - float(ma_val)) / float(ma_val) * 100
                if abs(gap) > 2:
                    interpretation = f"大幅跳空（{gap:+.1f}%），开盘后回踩/测试均线的概率较高"
                elif abs(gap) > 1:
                    interpretation = f"跳空{ma_name}（{gap:+.1f}%），开盘后需观察能否站稳"
                else:
                    interpretation = f"贴近{ma_name}（{gap:+.1f}%），均线将提供开盘支撑/压力参考"
                lines.append(f"| 距 {ma_name}（T-1 日 {float(ma_val):.2f}） | {gap:+.1f}% | {interpretation} |")
                break  # 只显示最关键的一条（MA5）避免表格过长

    te = stock_tick.get("trading_phase", -1)
    te_label = {0: "常规交易", 1: "盘前交易", 2: "盘后交易"}.get(te, f"未知({te})")
    lines.append(f"| 交易时段 | {te_label} | itick tick 数据直接标注 |")

    lines.append(f"")
    lines.append(f"### T-1 日关键信号回顾")
    lines.append(f"")
    lines.append(f"| 指标 | T-1 日数值 | 信号解读 |")
    lines.append(f"|------|-----------|---------|")
    score_label = "偏多" if t1_final_score > 0.15 else ("偏空" if t1_final_score < -0.15 else "中性")
    lines.append(f"| Alpha Final_Score | {t1_final_score:+.3f}（{score_label}） | 多因子模型对 T-1 日收盘的综合评估 |")
    lines.append(f"| MACD | {t1_macd_status} | T-1 日趋势方向 |")

    # 隔夜新闻
    if overnight_news:
        lines.append(f"")
        lines.append(f"### 隔夜重点新闻")
        lines.append(f"")
        lines.append(f"| 时间 | 标题 | 情感 |")
        lines.append(f"|------|------|------|")
        for n in overnight_news[:8]:
            sentiment = getattr(n, "sentiment", "") or "未分析"
            sentiment_label = {"positive": "正面", "negative": "负面", "neutral": "中性"}.get(sentiment, sentiment)
            title = getattr(n, "title", "")[:80]
            date_str = str(getattr(n, "date", ""))[:16]
            lines.append(f"| {date_str} | {title} | {sentiment_label} |")

    markdown = "\n".join(lines)
    logger.info(
        f"盘前快照: 盘前价={pre_price:.2f} ({pre_change_pct:+.2%}), "
        f"NQ={nq_change:+.2f}% ES={es_change:+.2f}%, T-1 Score={t1_final_score:+.3f}"
    )

    return PremarketSnapshot(
        markdown=markdown,
        pre_price=pre_price,
        pre_change_pct=pre_change_pct,
        nq_change_pct=nq_change,
        es_change_pct=es_change,
        t1_final_score=t1_final_score,
        session=session,
    )
