"""
技术面分析模块

提供常用技术指标的计算函数和分析摘要生成：
  - 移动均线 (MA)：5/10/20/60 日
  - MACD：DIF、DEA、柱状图
  - RSI：相对强弱指标（14 日）
  - 布林带 (Bollinger Bands)：中轨、上轨、下轨
  - KDJ：随机指标
  - 综合信号：基于双均线交叉的买卖信号

所有函数接受 pandas DataFrame 并返回添加了新列的 DataFrame，
遵循"不修改原数据"原则（内部使用 copy()）。

数据格式要求：
  输入的 DataFrame 必须包含列: date, open, high, low, close, volume

【扩展点】添加新的技术指标：
  1. 参考现有函数的签名风格，编写新函数（如 calc_wr、calc_cci）
  2. 在 calc_all_indicators() 中添加调用
  3. 在 summarize() 中添加对应的分析文本生成逻辑
"""

import logging
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ======================== 移动均线 (MA) ========================

def calc_ma(df: pd.DataFrame, periods: list[int] | None = None) -> pd.DataFrame:
    """
    计算收盘价的移动均线。

    默认计算 5/10/20/60 日均线，分别对应：
      - MA5:  周线（短期趋势）
      - MA10: 双周线
      - MA20: 月线（中期趋势，常用于回测策略）
      - MA60: 季线（长期趋势参考）

    Args:
        df: 包含 close 列的 DataFrame
        periods: 均线周期列表，默认 [5, 10, 20, 60]

    Returns:
        添加了 ma_5, ma_10, ma_20, ma_60 列的 DataFrame
    """
    if periods is None:
        periods = [5, 10, 20, 60]
    result = df.copy()
    for p in periods:
        if len(result) >= p:
            result[f"ma_{p}"] = result["close"].rolling(window=p).mean()
    return result


# ======================== MACD ========================

def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    计算 MACD 指标（指数平滑异同移动平均线）。

    MACD 由三部分组成：
      - DIF (快线):  快速 EMA(12) - 慢速 EMA(26)
      - DEA (慢线):  DIF 的 9 日 EMA
      - MACD 柱:     2 × (DIF - DEA)，柱状图高度反映趋势强弱

    信号解读：
      - DIF 上穿 DEA → 金叉（看涨信号）
      - DIF 下穿 DEA → 死叉（看跌信号）
      - MACD 柱由绿转红 → 上涨动能减弱

    Args:
        df: 包含 close 列的 DataFrame
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    Returns:
        添加了 dif, dea, macd_bar 列的 DataFrame
    """
    result = df.copy()
    # 计算快慢 EMA
    result["ema_fast"] = result["close"].ewm(span=fast, adjust=False).mean()
    result["ema_slow"] = result["close"].ewm(span=slow, adjust=False).mean()
    # DIF = 快 EMA - 慢 EMA
    result["dif"] = result["ema_fast"] - result["ema_slow"]
    # DEA = DIF 的 signal 日 EMA
    result["dea"] = result["dif"].ewm(span=signal, adjust=False).mean()
    # MACD 柱 = 2 × (DIF - DEA)
    result["macd_bar"] = 2 * (result["dif"] - result["dea"])
    return result


# ======================== RSI (相对强弱指标) ========================

def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    计算 RSI（相对强弱指标）。

    RSI 衡量近期价格变动的速度和幅度，值域 0-100：
      - RSI > 70: 超买区域，可能回调
      - RSI < 30: 超卖区域，可能反弹
      - 30-70:    中性震荡区域

    使用 Wilder's Smoothing Method（指数平滑）计算。

    Args:
        df: 包含 close 列的 DataFrame
        period: RSI 周期（默认 14 天）

    Returns:
        添加了 rsi 列的 DataFrame
    """
    result = df.copy()
    # 计算每日价格变化
    delta = result["close"].diff()
    # 分离涨跌幅
    gain = delta.where(delta > 0, 0.0)   # 上涨部分
    loss = (-delta).where(delta < 0, 0.0)  # 下跌部分（取反）
    # 指数平滑平均
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    # RS = 平均涨幅 / 平均跌幅
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result["rsi"] = 100 - (100 / (1 + rs))
    return result


# ======================== 布林带 (Bollinger Bands) ========================

def calc_bollinger(df: pd.DataFrame, period: int = 20, std_dev: int = 2) -> pd.DataFrame:
    """
    计算布林带指标。

    布林带由三条线组成：
      - 中轨: 20 日均线
      - 上轨: 中轨 + 2 倍标准差
      - 下轨: 中轨 - 2 倍标准差

    辅助指标：
      - bb_width: 带宽（上轨-下轨），反映波动性
      - bb_pct:   价格在带宽中的位置百分比

    信号解读：
      - 价格触及上轨 → 短期偏强 / 可能回调
      - 价格触及下轨 → 短期偏弱 / 可能反弹
      - 带宽收窄 → 可能即将变盘

    Args:
        df: 包含 close 列的 DataFrame
        period: 均线周期（默认 20 日）
        std_dev: 标准差倍数（默认 2）

    Returns:
        添加了 bb_mid, bb_upper, bb_lower, bb_width, bb_pct 列的 DataFrame
    """
    result = df.copy()
    result["bb_mid"] = result["close"].rolling(window=period).mean()
    result["bb_std"] = result["close"].rolling(window=period).std()
    result["bb_upper"] = result["bb_mid"] + std_dev * result["bb_std"]
    result["bb_lower"] = result["bb_mid"] - std_dev * result["bb_std"]
    result["bb_width"] = result["bb_upper"] - result["bb_lower"]
    # 价格在带宽中的百分比位置（0=下轨，1=上轨）
    result["bb_pct"] = (result["close"] - result["bb_lower"]) / (result["bb_upper"] - result["bb_lower"])
    return result


# ======================== KDJ (随机指标) ========================

def calc_kdj(df: pd.DataFrame, period: int = 9) -> pd.DataFrame:
    """
    计算 KDJ 随机指标。

    KDJ 由三条线组成：
      - K 线: 快线，反映当前价格在近期高低价区间的位置
      - D 线: 慢线，K 的移动平均
      - J 线: 辅助线，3K - 2D，更敏感

    信号解读：
      - K > 80 且 D > 70: 超买区域
      - K < 20 且 D < 30: 超卖区域
      - K 上穿 D: 金叉（买入信号）
      - K 下穿 D: 死叉（卖出信号）

    Args:
        df: 包含 high, low, close 列的 DataFrame
        period: RSV 计算周期（默认 9 日）

    Returns:
        添加了 k, d, j 列的 DataFrame
    """
    result = df.copy()
    # 计算 period 内的最低价和最高价
    low_min = result["low"].rolling(window=period).min()
    high_max = result["high"].rolling(window=period).max()
    # RSV: 当前收盘价在区间内的位置百分比
    rsv = ((result["close"] - low_min) / (high_max - low_min + 1e-10)) * 100
    # K = RSV 的 3 日 EMA
    result["k"] = rsv.ewm(span=3, adjust=False).mean()
    # D = K 的 3 日 EMA
    result["d"] = result["k"].ewm(span=3, adjust=False).mean()
    # J = 3K - 2D
    result["j"] = 3 * result["k"] - 2 * result["d"]
    return result


# ======================== 综合计算 ========================

def calc_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    依次计算所有技术指标。

    调用顺序：MA → MACD → RSI → 布林带 → KDJ
    每个函数在前一个结果基础上添加新列，实现链式计算。

    【扩展点】添加新指标时在此函数中添加调用即可。

    Args:
        df: 原始股价 DataFrame

    Returns:
        包含所有技术指标列的 DataFrame
    """
    result = calc_ma(df)
    result = calc_macd(result)
    result = calc_rsi(result)
    result = calc_bollinger(result)
    result = calc_kdj(result)
    return result


# ======================== 分析摘要生成 ========================

def summarize(df: pd.DataFrame, name: str = "") -> str:
    """
    生成技术面分析摘要文本。

    根据最新计算的技术指标，输出 Markdown 格式的结构化分析，
    包含均线、MACD、RSI、布林带、KDJ 和近期涨跌幅信息。

    此摘要将被传入报告生成模块（LLM 或回退模板）。

    【扩展点】可调整阈值参数（如 RSI 超买/超卖线），
    或添加更多指标的解读逻辑。

    Args:
        df: 包含技术指标列的 DataFrame
        name: 股票名称（可选，用于标题）

    Returns:
        Markdown 格式的技术面分析摘要
    """
    if df.empty or len(df) < 20:
        return f"数据不足，无法生成{name}技术面分析摘要。"

    last = df.iloc[-1]  # 最新一行数据
    lines = [f"## {name}技术面分析\n"]

    # 可选：使用 ta 库计算 ADX（趋势强度）和 ATR（波动率），
    # 取最新值用于"趋势 / 震荡"判断（ta 不是必需依赖）。
    adx_last = atr_last = None
    try:
        import ta
        try:
            adx_series = ta.trend.ADXIndicator(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
            ).adx()
            if not adx_series.empty and pd.notna(adx_series.iloc[-1]):
                adx_last = float(adx_series.iloc[-1])
        except Exception:
            pass
        try:
            atr_series = ta.volatility.AverageTrueRange(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
            ).average_true_range()
            if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
                atr_last = float(atr_series.iloc[-1])
        except Exception:
            pass
    except ImportError:
        pass

    # ---- 均线系统 ----
    lines.append("### 均线系统")
    ma_fields = [("ma_5", "5日均线"), ("ma_10", "10日均线"), ("ma_20", "20日均线")]
    for field, label in ma_fields:
        if field in df.columns and pd.notna(last.get(field)):
            val = last[field]
            current_price = last.get("close", 0)
            relation = "上方" if current_price > val else "下方"
            lines.append(f"- **{label}**: {val:.2f}（股价位于均线{relation}）")

    # ---- MACD ----
    lines.append("\n### MACD")
    if "dif" in df.columns and "dea" in df.columns:
        dif = last.get("dif", 0)
        dea = last.get("dea", 0)
        macd_bar = last.get("macd_bar", 0)
        if pd.notna(dif) and pd.notna(dea):
            state = "金叉（看涨）" if dif > dea else "死叉（看跌）"
            lines.append(f"- DIF: {dif:.2f}")
            lines.append(f"- DEA: {dea:.2f}")
            lines.append(f"- MACD柱: {macd_bar:.2f}")
            lines.append(f"- 状态: {state}")

    # ---- RSI ----
    lines.append("\n### RSI")
    if "rsi" in df.columns and pd.notna(last.get("rsi")):
        rsi_val = last["rsi"]
        if rsi_val > 70:
            rsi_status = "超买区域，注意回调风险"
        elif rsi_val < 30:
            rsi_status = "超卖区域，可能存在反弹机会"
        else:
            rsi_status = "中性区域"
        lines.append(f"- RSI(14): {rsi_val:.1f}（{rsi_status}）")

    # ---- 布林带 ----
    lines.append("\n### 布林带")
    if "bb_upper" in df.columns and pd.notna(last.get("bb_upper")):
        upper = last["bb_upper"]
        lower = last["bb_lower"]
        mid = last["bb_mid"]
        close_price = last["close"]
        lines.append(f"- 上轨: {upper:.2f}")
        lines.append(f"- 中轨: {mid:.2f}")
        lines.append(f"- 下轨: {lower:.2f}")
        if close_price > upper:
            bb_status = "股价突破上轨，短期偏强"
        elif close_price < lower:
            bb_status = "股价跌破下轨，短期偏弱"
        else:
            bb_status = "股价在布林带内运行"
        lines.append(f"- 状态: {bb_status}")

    # ---- KDJ ----
    lines.append("\n### KDJ")
    if "k" in df.columns and pd.notna(last.get("k")):
        k_val = last["k"]
        d_val = last["d"]
        j_val = last["j"]
        lines.append(f"- K: {k_val:.2f}")
        lines.append(f"- D: {d_val:.2f}")
        lines.append(f"- J: {j_val:.2f}")

    # ---- 趋势强度 / 波动率（ADX / ATR） ----
    if adx_last is not None or atr_last is not None:
        lines.append("\n### 趋势强度 / 波动率")
        if adx_last is not None:
            if adx_last > 25:
                trend_label = "趋势明显"
            elif adx_last < 20:
                trend_label = "震荡为主"
            else:
                trend_label = "趋势中性"
            lines.append(f"- ADX: {adx_last:.1f}（{trend_label}）")
        if atr_last is not None:
            close_price = float(last.get("close", 0)) or 1
            lines.append(f"- ATR: {atr_last:.2f}（约占当前价 {atr_last / close_price * 100:.2f}%）")

    # ---- 近期涨跌幅 ----
    lines.append(f"\n### 近5日涨跌幅")
    if len(df) >= 5:
        recent_close = df["close"].values
        for i in range(1, min(6, len(recent_close))):
            change = (recent_close[-i] - recent_close[-i-1]) / recent_close[-i-1] * 100
            lines.append(f"- 第{i}日前: {change:+.2f}%")

    return "\n".join(lines)
