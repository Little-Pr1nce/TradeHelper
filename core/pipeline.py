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
    market_regime: str = "unknown"            # 当前行情类型
    active_strategies: list = field(default_factory=list)   # 当前激活的策略名
    skipped_strategies: list = field(default_factory=list)  # 被跳过的策略名
    param_tuning: dict = field(default_factory=dict)        # 参数调优结果


def run_pipeline(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    initial_capital: float = 100000.0,
    market: str = "A",
    strategy_names: list[str] | None = None,
    w_tech: float = 0.6,
    w_news: float = 0.4,
    fundamental_data: dict | None = None,
    validation_mode: str = "eod",
    depth_score: float = 0.0,
    depth_available: bool = False,
    skip_param_tuning: bool = False,
):
    """
    执行完整的量化分析计算管道（纯计算，无 I/O）。

    管道步骤：
      ┌──────────────────────────────────────────────┐
      │ ① 预计算 7 个技术指标（MA/MACD/RSI/布林/KDJ）    │
      │ ② Alpha 多因子打分（Z-Score + tanh + 加权合成）  │
      │ ③ Rank IC 计算（因子有效性检验）                 │
      │ ④ 多策略并行回测（T+1 撮合）                    │
      │ ⑤ 策略参数扫参调优（可选，skip_param_tuning=True 跳过）│
      └──────────────────────────────────────────────┘

    Args:
        df: 原始 OHLCV DataFrame（必须含 date/open/high/low/close/volume）
        news_df: 新闻情感 DataFrame（date + finbert_score），可选
        initial_capital: 初始资金（默认 10 万）
        market: "A" 或 "US"，影响涨跌停规则
        strategy_names: 策略列表，默认 ["A", "B", "C"]
        w_tech: 技术面权重
        w_news: 新闻面权重
        skip_param_tuning: True 时跳过⑤参数扫参调优（如：同板块快速分析）

    Returns:
        AnalysisResult 包含全部计算结果
    """
    if strategy_names is None:
        strategy_names = ["A", "B", "C", "D", "E", "F", "G", "H", "O",
                          "I", "J", "K", "L", "M", "N"]

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
                          fundamental_data=fundamental_data,
                          validation_mode=validation_mode,
                          depth_score=depth_score,
                          depth_available=depth_available)

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

    # ---- ⑤ 行情检测 + 策略过滤 ----
    from alpha.scoring import detect_market_regime
    market_regime, _ = detect_market_regime(df)
    active_strategy_keys = []
    skipped_strategy_keys = []
    active_strategies = []  # Strategy 实例列表
    for name in strategy_names:
        s = get_execution_strategy(name)
        if not s.suitable_regimes or market_regime in s.suitable_regimes:
            active_strategies.append(s)
            active_strategy_keys.append(name)
        else:
            skipped_strategy_keys.append(name)
            logger.info(f"  [策略] {name} ({s.name}) 不适合 {market_regime} 行情，跳过")

    logger.info(f"  [管道 4/4] 行情={market_regime}, 激活策略={[s.name for s in active_strategies]}, 跳过={skipped_strategy_keys}")
    logger.info(f"  策略回测 (初始资金={initial_capital:,.0f})...")
    config = BacktestConfig(initial_capital=initial_capital)
    if market == "US":
        config.broker.limit_up_pct = 999.0
        config.broker.limit_down_pct = 999.0

    engine = BacktestEngine(config)
    results = engine.run_multi(df, active_strategies, news_df)
    comparison = compare_strategies(results)

    for name, r in results.items():
        logger.info(
            f"  [管道 4/4] {name}: 收益={r.total_return*100:+.2f}%, "
            f"夏普={r.sharpe_ratio:.2f}, 回撤={r.max_drawdown*100:.2f}%, "
            f"交易={r.total_trades}次"
        )

    # ---- ⑥ 参数扫参调优 ----
    param_tuning = {}
    if not skip_param_tuning:
        logger.info("  [管道 5/5] 策略参数扫参调优...")
        for name in active_strategy_keys:
            base = get_execution_strategy(name)
            tunable = base.tunable_params()
            if not tunable:
                continue
            # 只调第一个参数
            p = tunable[0]
            best_val = p["default"]
            best_score = -999.0
            for val in p["values"]:
                kwargs = {p["name"]: val}
                s = get_execution_strategy(name, **kwargs)
                test_result = engine.run(df.copy(), s, news_df)
                score = test_result.sharpe_ratio if test_result.total_trades > 0 else -999.0
                if score > best_score:
                    best_score = score
                    best_val = val
            param_tuning[name] = {
                "param": p["name"], "default": p["default"],
                "best_value": best_val,
                "default_sharpe": results.get(base.name, results.get(name, None)),
            }
            default_r = results.get(base.name)
            best_str = (f"默认 entry={p['default']:.2f} → {default_r.total_return*100:+.1f}%/{default_r.sharpe_ratio:.2f}"
                        if default_r else f"默认 entry={p['default']:.2f}")
            logger.info(f"  [调优] {name}: {best_str} | 最优 entry={best_val:.2f} → 夏普={best_score:.2f}")

    logger.info(f"管道完成: 基准收益={benchmark_return*100:+.2f}%")
    logger.info("=" * 50)

    # 提取因子检验结果
    validation = {}
    norm_cols = [c for c in df.columns if c.endswith("_norm")]
    if norm_cols and "close" in df.columns:
        from alpha.validation import validate_factors
        validation = validate_factors(df, mode=validation_mode)

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
        market_regime=market_regime,
        active_strategies=active_strategy_keys,
        skipped_strategies=skipped_strategy_keys,
        param_tuning=param_tuning,
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
    futures_score: float = 0.0     # 期货宏观情绪因子得分 [-1, +1]
    t1_final_score: float = 0.0
    session: str = "pre"


def compute_intraday_snapshot(
    pipeline_result: "AnalysisResult",
    realtime_quote: dict,
    depth_factor: dict | None = None,
    today_news: list | None = None,
    session: str = "intraday",
    market: str = "US",
    premarket_prediction_json: str | None = None,
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

    # ── 辅助函数：计算当前价 vs 均线的偏离（纯数值，无解读） ──
    def _vs_ma_value(ma_col: str) -> dict:
        """返回均线价格 + 偏离百分比（纯数据）。"""
        val = last.get(ma_col)
        if val is None or pd.isna(val) or val <= 0:
            return {"value_str": "—", "pct": 0}
        pct = (latest - float(val)) / float(val) * 100
        return {
            "value_str": f"{float(val):.2f}（偏离 {pct:+.2f}%）",
            "pct": pct,
        }

    # ── 均线位置（纯数值） ──
    ma5 = _vs_ma_value("ma_5")
    ma10 = _vs_ma_value("ma_10")
    ma20 = _vs_ma_value("ma_20")
    ma60 = _vs_ma_value("ma_60")

    # ── 布林带位置（纯数值） ──
    bb_upper = last.get("bb_upper")
    bb_lower = last.get("bb_lower")
    bb_mid = last.get("bb_mid")
    bb_position = None
    bb_str = "—"
    bb_upper_v = bb_lower_v = 0.0
    if (bb_upper and bb_lower and bb_mid
            and pd.notna(bb_upper) and pd.notna(bb_lower) and pd.notna(bb_mid)
            and float(bb_upper) != float(bb_lower)):
        bb_upper_v, bb_lower_v = float(bb_upper), float(bb_lower)
        bb_position = (latest - bb_lower_v) / (bb_upper_v - bb_lower_v) * 100
        bb_str = f"{bb_position:.0f}%（上轨 {bb_upper_v:.2f} / 下轨 {bb_lower_v:.2f}）"

    # ── 日内振幅（纯数值） ──
    intraday_high = realtime_quote.get("high", 0)
    intraday_low = realtime_quote.get("low", 0)
    intraday_range_pct = 0.0
    if intraday_high and intraday_low and prev_close:
        intraday_range_pct = (intraday_high - intraday_low) / prev_close * 100

    # ── 量比（纯数值） ──
    vol_ratio = None
    vol_ratio_val = 0.0
    if df["volume"].notna().any():
        avg_vol_5d = float(df["volume"].iloc[-6:-1].mean()) if len(df) >= 6 else 0
        if avg_vol_5d > 0:
            vol_ratio_val = volume_today / avg_vol_5d
            vol_ratio = f"{vol_ratio_val:.2f}x"

    # ── 盘口（纯数值） ──
    depth_imbalance = 1.0
    depth_score = 0.0
    depth_bid_vol = 0.0
    depth_ask_vol = 0.0
    depth_available = False
    if depth_factor and depth_factor.get("available"):
        depth_imbalance = depth_factor.get("imbalance", 1.0)
        depth_score = depth_factor.get("depth_score", 0.0)
        depth_bid_vol = depth_factor.get("bid_volume", 0)
        depth_ask_vol = depth_factor.get("ask_volume", 0)
        depth_available = True

    # ── VWAP 偏离（纯数值） ──
    vwap_val = realtime_quote.get("vwap", 0)
    vwap_pct = 0.0
    if vwap_val and vwap_val > 0 and latest > 0:
        vwap_pct = (latest - vwap_val) / vwap_val * 100

    # ── 日内动量（纯数值） ──
    open_price = realtime_quote.get("open", 0)
    otc = 0.0  # open-to-current
    htc = 0.0  # high-to-current
    ltc = 0.0  # low-to-current
    if open_price > 0 and latest > 0:
        otc = (latest - open_price) / open_price * 100
        if intraday_high and intraday_high > 0:
            htc = (latest - intraday_high) / intraday_high * 100
        if intraday_low and intraday_low > 0:
            ltc = (latest - intraday_low) / intraday_low * 100

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
        f"| 项目 | 数值 |",
        f"|------|------|",
        f"| 最新价 | **{latest:.2f}**（{change_pct:+.2%}） |",
    ]

    # 均线行
    for label, data in [("MA5（5日均线）", ma5), ("MA10（10日均线）", ma10),
                         ("MA20（20日均线）", ma20), ("MA60（60日均线）", ma60)]:
        if data["value_str"] != "—":
            lines.append(f"| {label} | {data['value_str']} |")

    # 布林
    if bb_str != "—":
        lines.append(f"| 布林带位置 | {bb_str} |")

    # 日内振幅
    if intraday_range_pct > 0:
        lines.append(f"| 日内振幅 | {intraday_range_pct:.1f}% |")

    # 量比
    if vol_ratio:
        lines.append(f"| 量比（vs 5日均量） | {vol_ratio} |")

    # VWAP
    if vwap_val > 0:
        lines.append(f"| VWAP 偏离 | {vwap_pct:+.2f}%（VWAP={vwap_val:.2f}） |")

    # 日内动量
    momentum_parts = []
    if abs(otc) > 0.001:
        momentum_parts.append(f"开盘→最新 {otc:+.2f}%")
    if abs(htc) > 0.001:
        momentum_parts.append(f"距高点 {htc:+.1f}%")
    if abs(ltc) > 0.001:
        momentum_parts.append(f"距低点 {ltc:+.1f}%")
    if momentum_parts:
        lines.append(f"| 日内动量 | {' / '.join(momentum_parts)} |")

    # 盘口
    if depth_available:
        lines.append(f"| 盘口买卖比 | 买 {depth_bid_vol:,.0f} / 卖 {depth_ask_vol:,.0f} = {depth_imbalance:.2f}（得分 {depth_score:+.3f}） |")

    lines.append(f"")

    # ── 盘中走势形态（纯数据） ──
    if market == "US" and realtime_quote.get("open", 0) > 0:
        open_price = realtime_quote["open"]
        high_price = realtime_quote.get("high", 0)
        low_price = realtime_quote.get("low", 0)

        open_gap = (open_price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        range_from_open = (latest - open_price) / open_price * 100 if open_price > 0 else 0

        lines.append(f"### 盘中走势数据")
        lines.append(f"")
        lines.append(f"| 价位 | 价格 | 涨跌幅 |")
        lines.append(f"|------|------|--------|")
        lines.append(f"| 前收盘 | {prev_close:.2f} | — |")
        lines.append(f"| 开盘 | {open_price:.2f} | {open_gap:+.1f}%（跳空） |")
        lines.append(f"| 最高 | {high_price:.2f} | {(high_price - prev_close) / prev_close * 100:+.1f}% |")
        lines.append(f"| 最低 | {low_price:.2f} | {(low_price - prev_close) / prev_close * 100:+.1f}% |")
        lines.append(f"| 最新 | {latest:.2f} | {change_pct:+.2%} |")
        lines.append(f"| 开盘→最新 | — | {range_from_open:+.2f}% |")
        lines.append(f"")

    lines.append(f"### T-1 日关键信号回顾")
    lines.append(f"")
    lines.append(f"| 指标 | T-1 日数值 |")
    lines.append(f"|------|-----------|")
    lines.append(f"| Alpha Final_Score | {t1_final_score:+.3f} |")
    lines.append(f"| MACD | DIF={float(dif):.2f} DEA={float(dea):.2f} ({t1_macd_status}) |")
    lines.append(f"| RSI(14) | {t1_rsi:.1f} |")
    lines.append(f"| KDJ | {t1_kdj} |")
    lines.append(f"| ADX（趋势强度） | {adx_text} |")
    lines.append(f"| ATR（波动率） | {atr_text} |")

    # ── 盘前预测验证（三时段联动闭环） ──
    if premarket_prediction_json and market == "US":
        try:
            import json
            pred = json.loads(premarket_prediction_json)
            pre_price = pred.get("pre_price", 0)
            pre_change = pred.get("pre_change_pct", 0)
            futures_score = pred.get("futures_score", 0)
            t1_score = pred.get("t1_final_score", 0)

            if pre_price > 0 and prev_close > 0:
                open_gap = (open_price - prev_close) / prev_close * 100 if open_price > 0 else 0

                # 判断盘前预测方向
                if futures_score > 0.15:
                    pre_bias = "偏多"
                elif futures_score < -0.15:
                    pre_bias = "偏空"
                else:
                    pre_bias = "中性"

                # 判断实际开盘方向
                if open_gap > 1.0:
                    actual_open = "高开"
                elif open_gap < -1.0:
                    actual_open = "低开"
                else:
                    actual_open = "平开"

                # 验证盘前预测
                if (pre_bias == "偏多" and actual_open == "高开") or \
                   (pre_bias == "偏空" and actual_open == "低开") or \
                   (pre_bias == "中性" and actual_open == "平开"):
                    verdict = "✓ 预测正确"
                    verdict_detail = f"盘前{pre_bias}判断与{actual_open}实际一致"
                elif pre_bias == "中性":
                    verdict = "▸ 盘前中性"
                    verdict_detail = f"盘前方向不明确，实际{actual_open}"
                else:
                    verdict = "⚠️ 预测偏差"
                    verdict_detail = f"盘前判断{pre_bias}，实际{actual_open}，需关注偏差原因"

                # 盘前价 vs 实际开盘价偏差
                pre_vs_open = (open_price - pre_price) / pre_price * 100 if pre_price > 0 else 0

                lines.append(f"### 🔮 盘前预测验证（三时段联动）")
                lines.append(f"")
                lines.append(f"| 项目 | 盘前预测 | 实际盘中 | 验证 |")
                lines.append(f"|------|---------|---------|------|")
                lines.append(f"| 开盘方向 | ETF 宏观得分 {futures_score:+.3f}（{pre_bias}） | {actual_open}（{open_gap:+.1f}%） | {verdict} |")
                lines.append(f"| 盘前价格 | {pre_price:.2f}（{pre_change:+.2%}） | 开盘价 {open_price:.2f} | 偏差 {pre_vs_open:+.2f}% |")
                lines.append(f"| T-1 Alpha | {t1_score:+.3f} | — | 趋势框架参考 |")
                lines.append(f"")
                lines.append(f"**验证结论**：{verdict_detail}。")
                if abs(pre_vs_open) > 1.0:
                    lines.append(f"盘前价与实际开盘偏差 {abs(pre_vs_open):.1f}%，超过 1%，开盘前流动性较低可能导致预测偏离。")
                lines.append(f"")
        except Exception as e:
            logger.debug(f"盘前预测解析跳过: {e}")

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
    stock_quote: dict | None = None,
) -> PremarketSnapshot:
    """
    计算盘前快照（美股 + A 股）。

    - 美股：QQQ/SPY ETF 盘前价格 → 宏观情绪参考
    - A 股：沪深300/上证50 ETF 实时价 → 宏观情绪参考
    - 盘前价格 + 成交量反映隔夜资金意图
    - 盘前价格与 ETF 相对强弱判断是否有独立资金行为
    - 距均线的跳空幅度预示开盘后回踩/延续概率
    """
    df = pipeline_result.df

    # 市场标签
    if market == "A":
        etf1_label, etf1_name = "510300", "沪深300 ETF"
        etf2_label, etf2_name = "510050", "上证50 ETF"
    else:
        etf1_label, etf1_name = "QQQ", "纳指ETF"
        etf2_label, etf2_name = "SPY", "标普ETF"

    pre_price = stock_tick.get("latest", 0)

    # 通过 quote 推算涨跌幅（tick 只有 latest 没有 prev_close）
    pre_change_pct = 0.0
    prev_close = 1.0
    if pre_price > 0 and pipeline_result.df is not None and len(pipeline_result.df) > 0:
        t1_close = float(pipeline_result.df["close"].iloc[-1])
        if t1_close > 0:
            pre_change_pct = (pre_price - t1_close) / t1_close
            prev_close = t1_close

    # ── ETF 宏观情绪解读 ──
    nq = futures_data.get("NQ", {})
    es = futures_data.get("ES", {})

    nq_change = nq.get("change_pct", 0) if nq else 0  # 小数：0.0069 = 0.69%
    es_change = es.get("change_pct", 0) if es else 0

    def _pct_str(val: float) -> str:
        """小数 → 百分数显示字符串，如 0.0069 → '+0.69%'。"""
        return f"{val * 100:+.2f}%"

    nq_kline = (nq or {}).get("kline_5min", [])
    es_kline = (es or {}).get("kline_5min", [])

    # ── 期货宏观情绪因子得分 ──
    from alpha.scoring import score_futures_factor, _compute_kline_trend
    nq_trend = _compute_kline_trend(nq_kline)
    es_trend = _compute_kline_trend(es_kline)
    futures_score = score_futures_factor(
        nq_change_pct=nq_change,
        es_change_pct=es_change,
        nq_kline_trend=nq_trend,
        es_kline_trend=es_trend,
    )
    logger.info(f"  期货因子得分: NQ={nq_change:+.4f} ES={es_change:+.4f} "
                f"trend(NQ={nq_trend:+.2f} ES={es_trend:+.2f}) → {futures_score:+.3f}")

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

    # 个股与ETF的差值（供 LLM 判断个股相对强弱）
    avg_future_change = (nq_change + es_change) / 2 if nq_change and es_change else 0
    pre_vs_future_diff = pre_change_pct - avg_future_change

    lines = [
        f"## ⚡ 盘前快照",
        f"",
        f"> 🌅 快照时间：{now_str} | 分析基底：T-1 日（{t1_date}）收盘后完整分析",
        f"",
        f"### ETF 宏观风向标",
        f"",
        f"| 标的 | 最新价 | 涨跌幅 | 成交量 |",
        f"|------|--------|--------|--------|",
    ]

    if nq:
        lines.append(
            f"| {etf1_label}（{etf1_name}） | {nq.get('latest', 0):.2f} | "
            f"{_pct_str(nq_change)} | {nq.get('volume', 0):,.0f} |"
        )
    else:
        lines.append(f"| {etf1_label}（{etf1_name}） | 数据不可用 | — | — |")

    if es:
        lines.append(
            f"| {etf2_label}（{etf2_name}） | {es.get('latest', 0):.2f} | "
            f"{_pct_str(es_change)} | {es.get('volume', 0):,.0f} |"
        )
    else:
        lines.append(f"| {etf2_label}（{etf2_name}） | 数据不可用 | — | — |")

    lines.append(f"")
    lines.append(f"**ETF 宏观情绪得分**: {futures_score:+.3f}")
    lines.append(f"")

    # ETF 分钟线走势形态 — 仅在数据可用时展示（TickFlow 免费层不支持分钟线）
    has_kline_data = (nq_kline and len(nq_kline) >= 3) or (es_kline and len(es_kline) >= 3)
    if has_kline_data:
        lines.append(f"### ETF 盘前走势（5分钟K线，最近1小时）")
        lines.append(f"")
        lines.append(f"| 标的 | 首根K收盘 | 末根K收盘 | 涨跌 | 阳线数/总根数 |")
        lines.append(f"|------|----------|----------|------|--------------|")
        for label, kline in [(etf1_label, nq_kline), (etf2_label, es_kline)]:
            if kline and len(kline) >= 3:
                closes = [bar.get("c", bar.get("close", 0)) for bar in kline]
                opens = [bar.get("o", bar.get("open", 0)) for bar in kline]
                up_bars = sum(1 for i in range(len(kline))
                              if closes[i] and opens[i] and closes[i] >= opens[i])
                first_c = closes[0] if closes[0] else opens[0]
                last_c = closes[-1] if closes[-1] else opens[-1]
                kline_chg = (last_c - first_c) / first_c * 100 if first_c > 0 else 0
                lines.append(f"| {label} | {first_c:.2f} | {last_c:.2f} | {kline_chg:+.2f}% | {up_bars}/{len(kline)} |")
            else:
                lines.append(f"| {label} | — | — | — | — |")
        lines.append(f"")

    lines.append(f"### 个股盘前")
    lines.append(f"")
    lines.append(f"| 项目 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 盘前价格 | {pre_price:.2f}（{_pct_str(pre_change_pct)}） |")
    lines.append(f"| ETF 平均涨跌 | {_pct_str(avg_future_change)} |")
    lines.append(f"| 个股 vs ETF 差值 | {_pct_str(pre_vs_future_diff)} |")
    # 成交量优先用 quote 的累计量（tick 只返回最近一笔）
    pre_vol = (stock_quote or stock_tick).get("volume", 0)
    lines.append(f"| 盘前成交量 | {pre_vol:,.0f} 股 |")

    # 跳空均线（只展示 MA5 数值）
    if last is not None:
        ma_val = last.get("ma_5")
        if ma_val is not None and pd.notna(ma_val) and ma_val > 0:
            gap = (pre_price - float(ma_val)) / float(ma_val) * 100
            lines.append(f"| 距 MA5（T-1={float(ma_val):.2f}） | {gap:+.1f}% |")

    te = stock_tick.get("trading_phase", -1)
    te_label = {0: "常规交易", 1: "盘前交易", 2: "盘后交易"}.get(te, f"未知({te})")
    lines.append(f"| 交易时段 | {te_label} |")

    lines.append(f"")
    lines.append(f"### T-1 日关键信号回顾")
    lines.append(f"")
    lines.append(f"| 指标 | T-1 日数值 |")
    lines.append(f"|------|-----------|")
    lines.append(f"| Alpha Final_Score | {t1_final_score:+.3f} |")
    lines.append(f"| MACD | {t1_macd_status} |")

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
        futures_score=futures_score,
        t1_final_score=t1_final_score,
        session=session,
    )
