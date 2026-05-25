"""
回测引擎模块

轻量级事件驱动回测框架，模拟交易策略在历史数据上的表现。

核心功能：
  1. 按时间序列遍历 K 线数据
  2. 根据策略信号执行模拟交易（买入/卖出）
  3. 计算关键绩效指标：总收益、年化收益、最大回撤、夏普比率、胜率
  4. 生成回测摘要文本和操作建议

交易假设：
  - 初始资金默认为 100,000 元（可在初始化时修改）
  - 每次买入使用 95% 的可用资金（留 5% 作为缓冲）
  - 佣金费率 0.03%（万三，A 股实际费率参考）
  - 每次卖出清空所有持仓（简化处理）
  - 不考虑滑点、涨跌停限制、T+1 等约束

【扩展点】增强回测引擎：
  1. 添加滑点模型（如固定滑点、比例滑点）
  2. 支持仓位管理策略（如金字塔加仓、分批止盈）
  3. 添加更多绩效指标（如卡玛比率、索提诺比率、信息比率）
  4. 支持多股票组合回测
  5. 输出权益曲线数据用于可视化

【扩展点】调整推荐阈值：
  修改 _make_recommendation() 中的阈值参数，
  可定制不同风险偏好下的买入/卖出建议边界。
"""

import logging
from typing import Any

import pandas as pd
import numpy as np

from analysis.strategy import BaseStrategy

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    轻量级回测引擎。

    使用方式：
        engine = BacktestEngine(initial_capital=100000, commission=0.0003)
        result = engine.run(price_df, strategy)

    result 字典包含完整的回测指标和交易记录。
    """

    def __init__(self, initial_capital: float = 100000.0, commission: float = 0.0003):
        """
        初始化回测引擎。

        Args:
            initial_capital: 初始资金（默认 10 万）
            commission: 单边佣金费率（默认 0.03%）
        """
        self.initial_capital = initial_capital
        self.commission = commission

    def run(self, df: pd.DataFrame, strategy: BaseStrategy) -> dict[str, Any]:
        """
        执行回测。

        流程：
          1. 调用策略生成买卖信号
          2. 逐日模拟交易（现金+持仓）
          3. 计算绩效指标
          4. 生成报告摘要和建议

        Args:
            df: 股价历史数据 DataFrame（需包含 date, close 列）
            strategy: 交易策略实例

        Returns:
            包含所有回测指标和交易记录的字典
        """
        if df is None or df.empty:
            return self._empty_result()

        # 第一步：生成信号
        df = strategy.generate_signals(df)
        if df.empty:
            return self._empty_result()

        # 第二步：模拟交易
        return self._simulate(df)

    def _simulate(self, df: pd.DataFrame) -> dict[str, Any]:
        """
        逐日模拟交易流程（核心模拟逻辑）。

        交易规则：
          - 收到 "buy" 信号且持有现金 → 用 95% 资金买入
          - 收到 "sell" 信号且持有股票 → 全部卖出
          - 每次交易扣除佣金
          - 每日记录权益曲线（cash + shares × price）

        处理逻辑：
          - 连续同方向信号：第二个 buy 在已有持仓时忽略
          - 空仓可买，持仓可卖
          - 权益曲线用于后续计算最大回撤和夏普比率
        """
        df = df.reset_index(drop=True)
        df = df.copy()

        cash = self.initial_capital       # 当前现金
        shares = 0.0                       # 当前持股数
        trades = []                        # 交易记录列表
        equity_curve = []                  # 每日权益记录
        buy_signals = 0
        sell_signals = 0

        for i in range(len(df)):
            price = df["close"].iloc[i]
            signal = df.loc[df.index[i], "signal"]
            # 日期处理（兼容不同类型）
            date_str = str(df["date"].iloc[i])[:10] if "date" in df.columns else str(i)

            # ---- 处理买入信号 ----
            if signal == "buy" and cash > 0:
                buy_amount = cash * 0.95  # 用 95% 现金买入
                buy_shares = buy_amount / price
                cost = buy_amount * self.commission  # 佣金
                cash -= buy_amount + cost
                shares += buy_shares
                buy_signals += 1
                trades.append({
                    "date": date_str,
                    "action": "buy",
                    "price": round(price, 2),
                    "shares": round(buy_shares, 2),
                    "value": round(buy_amount, 2),
                })

            # ---- 处理卖出信号 ----
            elif signal == "sell" and shares > 0:
                sell_amount = shares * price
                cost = sell_amount * self.commission  # 佣金
                cash += sell_amount - cost
                shares = 0.0  # 清仓
                sell_signals += 1
                trades.append({
                    "date": date_str,
                    "action": "sell",
                    "price": round(price, 2),
                    "shares": round(shares, 2),
                    "value": round(sell_amount, 2),
                })

            # ---- 记录当日权益 ----
            equity = cash + shares * price
            equity_curve.append({
                "date": date_str,
                "equity": equity,
                "shares": round(shares, 2),
                "cash": round(cash, 2),
            })

        # ========== 计算绩效指标 ==========

        final_price = df["close"].iloc[-1]
        final_equity = cash + shares * final_price

        # 总收益率
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        # 年化收益率（按 252 个交易日折算）
        trading_days = len(df)
        if trading_days > 0 and total_return > -1:
            annual_return = (1 + total_return) ** (252 / trading_days) - 1
        else:
            annual_return = 0.0

        # 最大回撤
        max_drawdown = self._calc_max_drawdown([e["equity"] for e in equity_curve])

        # 夏普比率（基于日收益率计算，年化）
        sharpe = self._calc_sharpe([e["equity"] for e in equity_curve])

        # 胜率（盈利交易占总交易的比例）
        win_rate = self._calc_win_rate(trades) if trades else 0.0

        # ========== 生成摘要文本 ==========

        trade_summary = (
            f"初始资金: ¥{self.initial_capital:,.2f}\n"
            f"最终权益: ¥{final_equity:,.2f}\n"
            f"总收益率: {total_return*100:+.2f}%\n"
            f"年化收益率: {annual_return*100:+.2f}%\n"
            f"最大回撤: {max_drawdown*100:.2f}%\n"
            f"夏普比率: {sharpe:.2f}\n"
            f"胜率: {win_rate*100:.1f}%\n"
            f"交易次数: {len(trades)}（买入 {buy_signals} 次，卖出 {sell_signals} 次）"
        )

        recommendation = self._make_recommendation(total_return, max_drawdown, sharpe)

        # ========== 返回结果 ==========

        return {
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe, 4),
            "win_rate": round(win_rate, 4),
            "total_trades": len(trades),
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "trades": trades[-10:],         # 仅返回最近 10 条交易记录
            "trade_summary": trade_summary,
            "recommendation": recommendation,
        }

    def _empty_result(self) -> dict[str, Any]:
        """
        返回空结果（数据不足时的默认返回值）。

        所有指标预设为 0，提示用户数据不足。
        """
        return {
            "initial_capital": self.initial_capital,
            "final_equity": self.initial_capital,
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "buy_signals": 0,
            "sell_signals": 0,
            "trades": [],
            "trade_summary": "回测数据不足，无法生成结果。",
            "recommendation": "观望（数据不足，无法判断）",
        }

    @staticmethod
    def _calc_max_drawdown(equity_curve: list[float]) -> float:
        """
        计算最大回撤。

        最大回撤 = max{(峰值 - 谷值) / 峰值}
        反映策略在最坏情况下可能承受的亏损幅度。

        Args:
            equity_curve: 每日权益序列

        Returns:
            最大回撤比例（0-1 之间的小数）
        """
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val  # 更新峰值
            dd = (peak - val) / peak if peak > 0 else 0  # 当前回撤
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _calc_sharpe(equity_curve: list[float], risk_free: float = 0.025) -> float:
        """
        计算年化夏普比率。

        夏普比率 = (日均收益 - 无风险日收益) / 日收益标准差 × sqrt(252)

        解释：
          - > 1.0: 策略风险调整后收益良好
          - 0.5-1.0: 可接受
          - < 0.5: 风险调整后收益较弱

        Args:
            equity_curve: 每日权益序列
            risk_free: 年化无风险利率（默认 2.5%，约等于余额宝/国债）

        Returns:
            年化夏普比率
        """
        if len(equity_curve) < 2:
            return 0.0
        # 计算日收益率序列
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i-1] != 0:
                returns.append(
                    (equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
                )
        if not returns or np.std(returns) == 0:
            return 0.0
        avg_return = np.mean(returns)
        std_return = np.std(returns, ddof=1)  # 样本标准差
        # 年化: (日均超额收益 / 日波动) × sqrt(252)
        return (avg_return - risk_free / 252) / std_return * np.sqrt(252)

    @staticmethod
    def _calc_win_rate(trades: list[dict]) -> float:
        """
        计算交易胜率。

        胜率 = 盈利卖出次数 / 总卖出次数
        通过配对最近的买入价和卖出价来判断盈亏。

        Args:
            trades: 交易记录列表

        Returns:
            胜率（0-1 之间的比例）
        """
        buy_prices = {}
        wins = 0
        total = 0
        for t in trades:
            if t["action"] == "buy":
                buy_prices[t["date"]] = t["price"]
            elif t["action"] == "sell":
                total += 1
                # FIFO 配对：取最早的买入价
                for date_key in sorted(buy_prices.keys()):
                    buy_price = buy_prices.pop(date_key)
                    if t["price"] > buy_price:
                        wins += 1
                    break  # 一次只配对一笔
        return wins / total if total > 0 else 0.0

    @staticmethod
    def _make_recommendation(total_return: float, max_drawdown: float, sharpe: float) -> str:
        """
        根据回测指标生成操作建议。

        阈值说明：
          - 总收益率 > 10% 且夏普 > 1.0 且回撤 < 15% → 强烈买入
          - 总收益为正 且夏普 > 0.5              → 谨慎买入
          - 总收益 > -5%                          → 观望
          - 其他                                   → 卖出/回避

        【扩展点】可调整此方法的阈值，适配不同风险偏好。

        Args:
            total_return: 总收益率
            max_drawdown: 最大回撤
            sharpe: 年化夏普比率

        Returns:
            中文操作建议字符串
        """
        if total_return > 0.1 and sharpe > 1.0 and max_drawdown < 0.15:
            return "强烈买入 — 策略表现优异，收益稳定且回撤可控。"
        elif total_return > 0 and sharpe > 0.5:
            return "谨慎买入 — 策略整体盈利，但需注意风险控制。"
        elif total_return > -0.05:
            return "建议观望 — 策略表现平平，等待更明确的趋势信号。"
        else:
            return "建议卖出/回避 — 策略回测表现较差，不建议此时介入。"
