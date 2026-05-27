"""
K 线图生成模块

使用 mplfinance 库生成专业的股票 K 线图，输出为 PNG 图片文件。

功能：
  - 蜡烛图 + 成交量柱状图
  - 叠加 MA5/MA10/MA20 均线
  - 标注买卖信号（红色三角↑买入，绿色三角↓卖出）
  - 中文字体支持（标题、坐标轴标签）

图片输出路径：{work_dir}/charts/{code}_{timestamp}.png

依赖：
  - mplfinance: K 线图绘制
  - matplotlib: 底层绑图和字体管理

【扩展点】自定义图表样式：
  1. 修改 style_params 中的 style 参数切换主题（如 "binance"、"yahoo"）
  2. 调整 figratio 和 figscale 改变图表尺寸
  3. 添加更多叠加指标（如布林带、MACD 副图）
  4. 修改颜色方案适配品牌风格
"""

import logging
import os
import warnings
from datetime import datetime

import pandas as pd
import numpy as np

from config.settings import Settings

# 必须在 import matplotlib 之前屏蔽字体告警
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def generate_kline_chart(
    df: pd.DataFrame,
    code: str,
    name: str,
    lookback_days: int = 90
) -> str | None:
    """
    生成股票 K 线图并保存为 PNG 文件。

    图表组成：
      - 主图：蜡烛图（红涨绿跌或自定义配色）
      - 叠加：MA5（蓝）、MA10（橙）、MA20（紫）移动均线
      - 买卖信号：红色上三角 = 买入，绿色下三角 = 卖出
      - 副图：成交量柱状图

    Args:
        df:       股价 DataFrame（需含 date, open, high, low, close, volume 列）
        code:     股票代码（用于文件命名和标题）
        name:     股票名称（用于图表标题）
        lookback_days: 图表显示的天数（默认 90 天，0 表示全部）

    Returns:
        生成的 PNG 文件路径，失败则返回 None
    """
    if df is None or df.empty:
        logger.warning(f"No data to generate chart for {code}")
        return None

    # 尝试导入 mplfinance（延迟导入，避免启动时的依赖检查）
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # 设置中文字体（在 import mplfinance 之前）
        from utils.fonts import get_chinese_font_path
        font_path = get_chinese_font_path()
        font_name = "sans-serif"
        if font_path:
            from matplotlib import font_manager
            font_manager.fontManager.addfont(font_path)
            font_prop = font_manager.FontProperties(fname=font_path)
            font_name = font_prop.get_name()
            plt.rcParams["font.sans-serif"] = [font_name]
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["axes.unicode_minus"] = False

        import mplfinance as mpf
    except ImportError:
        logger.error("mplfinance not installed")
        return None

    # ---- 数据预处理 ----
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    # 截取最近 lookback_days 天数据
    if lookback_days > 0 and len(df) > lookback_days:
        df = df.iloc[-lookback_days:]

    df = df.set_index("date")

    # 校验必需列
    required_cols = {"open", "high", "low", "close", "volume"}
    if not required_cols.issubset(df.columns):
        logger.error(f"Missing required columns for chart: {required_cols}")
        return None

    # 确保 OHLCV 列为浮点数
    df = df.astype({c: float for c in required_cols})

    # ---- 计算均线（用于叠加显示） ----
    has_ma5 = has_ma10 = has_ma20 = False
    if len(df) >= 5:
        df["MA5"] = df["close"].rolling(5).mean()
        has_ma5 = True
    if len(df) >= 10:
        df["MA10"] = df["close"].rolling(10).mean()
        has_ma10 = True
    if len(df) >= 20:
        df["MA20"] = df["close"].rolling(20).mean()
        has_ma20 = True

    # ---- 构建叠加图层 ----
    apds = []  # addplot 列表

    # 均线叠加
    for label, col, color in [
        ("MA5", "MA5", "blue"),
        ("MA10", "MA10", "orange"),
        ("MA20", "MA20", "purple"),
    ]:
        if col in df.columns:
            apds.append(mpf.make_addplot(
                df[col], color=color, width=0.8
            ))

    # 买卖信号标注
    if "signal" in df.columns:
        buy_mask = df["signal"] == "buy"
        sell_mask = df["signal"] == "sell"

        if buy_mask.any():
            buy_series = pd.Series(float("nan"), index=df.index)
            buy_series.loc[buy_mask] = df.loc[buy_mask, "close"] * 0.97
            apds.append(mpf.make_addplot(
                buy_series, type="scatter", markersize=80, marker="^", color="red"))

        if sell_mask.any():
            sell_series = pd.Series(float("nan"), index=df.index)
            sell_series.loc[sell_mask] = df.loc[sell_mask, "close"] * 1.03
            apds.append(mpf.make_addplot(
                sell_series, type="scatter", markersize=80, marker="v", color="green"))

    # ---- 构建 mplfinance 样式参数 ----
    title = f"{name}({code}) K线图"

    # 用自定义 style 覆盖字体
    rc_overrides = {"axes.unicode_minus": False}
    if font_path:
        rc_overrides["font.sans-serif"] = [font_name]
        rc_overrides["font.family"] = "sans-serif"
    custom_style = mpf.make_mpf_style(base_mpf_style="charles", rc=rc_overrides)

    style_params = {
        "type": "candle",
        "volume": True,
        "addplot": apds if apds else [],
        "title": title,
        "ylabel": "Price",
        "ylabel_lower": "Volume",
        "figratio": (14, 7),
        "figscale": 1.0,
        "style": custom_style,
    }

    # ---- 生成图片 ----
    try:
        chart_dir = Settings().chart_dir
        filename = f"{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        filepath = os.path.join(chart_dir, filename)

        mpf.plot(df, savefig=filepath, **style_params)
        plt.close("all")  # 清理 matplotlib 资源，避免内存泄漏
        logger.info(f"Chart saved: {filepath}")
        # 目录清理（按 code 保留 N 份 + 全局总数兜底）
        try:
            _prune_charts(chart_dir, code)
        except Exception as e:
            logger.warning(f"Chart prune failed: {e}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to generate chart: {e}")
        return None


def _prune_charts(chart_dir: str, code: str) -> None:
    """
    清理 charts/ 目录，避免无限增长。

    策略：
      1. 同一 code 下保留最近 MAX_CHARTS_PER_CODE 份，淘汰更早的；
      2. 整个目录如果仍超过 MAX_CHARTS_TOTAL，再按修改时间淘汰最早的。
    """
    from indicators.constants import MAX_CHARTS_PER_CODE, MAX_CHARTS_TOTAL
    if not os.path.isdir(chart_dir):
        return

    all_pngs = [
        os.path.join(chart_dir, f) for f in os.listdir(chart_dir)
        if f.endswith(".png") and os.path.isfile(os.path.join(chart_dir, f))
    ]

    # 1) 同 code：按修改时间排序，仅保留最近 N 份
    code_pngs = [p for p in all_pngs if os.path.basename(p).startswith(f"{code}_")]
    code_pngs.sort(key=os.path.getmtime, reverse=True)
    for old_path in code_pngs[MAX_CHARTS_PER_CODE:]:
        try:
            os.remove(old_path)
        except OSError:
            pass

    # 2) 全局兜底：超过总数上限时淘汰最早的
    remaining = [
        os.path.join(chart_dir, f) for f in os.listdir(chart_dir)
        if f.endswith(".png") and os.path.isfile(os.path.join(chart_dir, f))
    ]
    if len(remaining) > MAX_CHARTS_TOTAL:
        remaining.sort(key=os.path.getmtime)  # 最早在前
        for old_path in remaining[: len(remaining) - MAX_CHARTS_TOTAL]:
            try:
                os.remove(old_path)
            except OSError:
                pass
