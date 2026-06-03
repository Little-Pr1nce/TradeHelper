"""
事件驱动回测引擎。

严格按照 T 日信号 → T+1 日开盘撮合 → T+1 日盘中风控检查的时序执行。

防偷窥铁律：
  1. T 日收盘后读取 Final_Score → 仅可使用 T 日及以前数据
  2. T+1 日开盘价撮合 → 不允许使用 T+1 日收盘价
  3. T+1 日盘中 High/Low 检查止损 → 不允许收盘后才补止损
  4. 所有因子在回测循环前预计算完毕 → 循环内禁止动态计算

支持单策略回测和多策略并行对比。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np

from strategies.base import BaseExecutionStrategy, StrategyContext, Position
from strategies import get_execution_strategy
from backtest.broker import Broker, BrokerConfig, Account
from backtest.analytics import compute_metrics

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """回测引擎配置。"""
    initial_capital: float = 100000.0
    broker: BrokerConfig = field(default_factory=BrokerConfig)


@dataclass
class BacktestResult:
    """单次回测结果（结构化数据，供报告和分析使用）。"""
    strategy_name: str
    initial_capital: float
    final_equity: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe_ratio: float
    calmar_ratio: float
    win_rate: float
    profit_loss_ratio: float
    avg_holding_days: float
    total_trades: int
    equity_curve: list[dict]
    trades: list[dict]
    fills: list
    metrics: dict = field(default_factory=dict)


class BacktestEngine:
    """
    事件驱动回测引擎。

    循环遍历每根 K 线，模拟：
      T 日收盘  → 策略判断开/平仓 → 生成 Order
      T+1 开盘  → Broker 撮合成交（含滑点）
      T+1 盘中  → 检查止损/止盈/时间止损
      T+1 收盘  → 更新权益曲线和持仓状态
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.broker = Broker(self.config.broker)

    def run(
        self,
        df: pd.DataFrame,
        strategy: BaseExecutionStrategy,
        news_df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """
        执行单策略回测。

        Args:
            df: 股价历史数据（需含 OHLCV + Final_Score）
            strategy: 交易策略实例
            news_df: 新闻情感数据（已对齐，可选）

        Returns:
            BacktestResult 包含完整回测结果
        """
        if df is None or df.empty:
            logger.warning(f"策略 {strategy.name} 收到空数据，返回空结果")
            return self._empty_result(strategy.name)

        df = df.reset_index(drop=True).copy()
        if "Final_Score" not in df.columns:
            raise ValueError("DataFrame 缺少 Final_Score 列，请先运行 alpha/scoring.py")

        # 初始化账户状态
        account = Account(initial_capital=self.config.initial_capital)
        cooldown_until = -1          # 冷却期结束的 K 线索引（-1 表示无冷却）
        market = self._detect_market(df)

        # 美股不设涨跌停
        if market == "US":
            self.broker.config.limit_up_pct = 999.0
            self.broker.config.limit_down_pct = 999.0

        logger.info(f"回测开始: {strategy.name}, {len(df)} 条K线, "
                     f"初始资金={self.config.initial_capital:,.0f}")

        n = len(df)
        bar_count = 0  # 已处理 K 线计数（用于每 50 根打印进度）

        # ---- 逐日回测主循环 ----
        for i in range(n - 1):  # -1 因为每次需要 T+1 的 bar
            t_bar = df.iloc[i]       # T 日数据
            t1_bar = df.iloc[i + 1]  # T+1 日数据
            t_date = str(t_bar.get("date", ""))[:10]
            t1_date = str(t1_bar.get("date", ""))[:10]
            prev_close = float(t_bar["close"])

            bar_count += 1
            if bar_count % 50 == 0:
                logger.info(f"  回测进度: {bar_count}/{n-1} 根K线已处理")

            # ── T 日收盘：策略产生信号 ──
            context = StrategyContext(
                date=t_date,
                equity=account.equity,
                cash=account.cash,
                position=account.position or Position(),
                market=market,
                cooldown_until=cooldown_until,
                holding_days=self._calc_holding_days(account, t_date),
            )

            try:
                orders = strategy.generate_orders(df.iloc[:i + 1], context)
            except Exception as e:
                logger.warning(f"策略 {strategy.name} 在 {t_date} 异常: {e}")
                orders = []

            # ── T+1 日开盘：撮合订单 ──
            for order in orders:
                fill = self.broker.execute_order(order, t1_bar, account, prev_close)
                if fill is None:
                    continue  # 订单被拒绝（涨跌停/停牌/资金不足）

                if order.action == "buy":
                    cooldown_until = self._calc_cooldown(strategy, i + 1, "sell")
                    account.trades.append({
                        "entry_date": fill.date,
                        "entry_price": fill.price,
                        "shares": fill.shares,
                        "signal_date": order.date,
                        "reason": order.reason,
                    })
                    logger.debug(
                        f"  [{t1_date}] 买入 {fill.shares}股 @ {fill.price:.2f} | {order.reason}"
                    )

                elif order.action == "sell":
                    # 记录卖出明细
                    if account.trades:
                        last_trade = account.trades[-1]
                        if "exit_date" not in last_trade:
                            last_trade["exit_date"] = fill.date
                            last_trade["exit_price"] = fill.price
                            last_trade["exit_reason"] = order.reason
                            pnl = (fill.price - last_trade["entry_price"]) * last_trade["shares"]
                            last_trade["pnl"] = round(pnl, 2)
                            last_trade["return_pct"] = round(
                                (fill.price - last_trade["entry_price"])
                                / last_trade["entry_price"] * 100, 2
                            )
                    cooldown_until = self._calc_cooldown(strategy, i + 1, "sell")
                    logger.debug(
                        f"  [{t1_date}] 卖出 {fill.shares}股 @ {fill.price:.2f} | {order.reason}"
                    )

            # ── T+1 日盘中：检查止损/止盈 ──
            stop_fills = self.broker.check_intraday_stops(
                t1_bar, account, prev_close, t1_date
            )
            for sf in stop_fills:
                if account.trades:
                    last_trade = account.trades[-1]
                    if "exit_date" not in last_trade:
                        last_trade["exit_date"] = sf.date
                        last_trade["exit_price"] = sf.price
                        last_trade["exit_reason"] = sf.reason
                        pnl = (sf.price - last_trade["entry_price"]) * last_trade["shares"]
                        last_trade["pnl"] = round(pnl, 2)
                        last_trade["return_pct"] = round(
                            (sf.price - last_trade["entry_price"])
                            / last_trade["entry_price"] * 100, 2
                        )
                logger.info(f"  [{t1_date}] ⚠️ {sf.reason}")

            # ── T+1 日收盘：更新账户状态 ──
            self.broker.update_daily(t1_bar, account)

        # ---- 回测结束：强制平仓（如果还有持仓） ----
        if account.position and account.position.shares > 0:
            last_close = float(df["close"].iloc[-1])
            last_date = str(df["date"].iloc[-1])[:10]
            fill_value = last_close * account.position.shares
            commission = fill_value * self.config.broker.commission
            account.cash += fill_value - commission
            if account.trades:
                last_trade = account.trades[-1]
                if "exit_date" not in last_trade:
                    last_trade["exit_date"] = last_date
                    last_trade["exit_price"] = last_close
                    last_trade["exit_reason"] = "回测结束强制平仓"
                    pnl = (last_close - last_trade["entry_price"]) * last_trade["shares"]
                    last_trade["pnl"] = round(pnl, 2)
                    last_trade["return_pct"] = round(
                        (last_close - last_trade["entry_price"])
                        / last_trade["entry_price"] * 100, 2
                    )
            account.position = None
            logger.info(f"  [{last_date}] 回测结束，强制平仓 @ {last_close:.2f}")

        # 恢复默认涨跌停配置
        if market == "US":
            self.broker.config.limit_up_pct = 0.099
            self.broker.config.limit_down_pct = 0.099

        final_equity = account.cash

        # ---- 计算绩效指标 ----
        benchmark_return = 0.0
        if len(df) > 1:
            benchmark_return = (
                (float(df["close"].iloc[-1]) - float(df["close"].iloc[0]))
                / float(df["close"].iloc[0])
            )

        metrics = compute_metrics(
            equity_curve=account.equity_curve,
            trades=account.trades,
            initial_capital=self.config.initial_capital,
            benchmark_return=benchmark_return,
            trading_days=len(df),
        )

        logger.info(
            f"回测完成: {strategy.name} | 收益={metrics['total_return']*100:+.2f}%, "
            f"夏普={metrics['sharpe_ratio']:.2f}, 回撤={metrics['max_drawdown']*100:.2f}%, "
            f"交易={len(account.trades)}次"
        )

        return BacktestResult(
            strategy_name=strategy.name,
            initial_capital=self.config.initial_capital,
            final_equity=round(final_equity, 2),
            total_return=metrics["total_return"],
            annual_return=metrics["annual_return"],
            max_drawdown=metrics["max_drawdown"],
            sharpe_ratio=metrics["sharpe_ratio"],
            calmar_ratio=metrics["calmar_ratio"],
            win_rate=metrics["win_rate"],
            profit_loss_ratio=metrics["profit_loss_ratio"],
            avg_holding_days=metrics.get("avg_holding_days", 0),
            total_trades=len(account.trades),
            equity_curve=account.equity_curve,
            trades=account.trades,
            fills=account.fills,
            metrics=metrics,
        )

    def run_multi(
        self,
        df: pd.DataFrame,
        strategies: list[BaseExecutionStrategy],
        news_df: pd.DataFrame | None = None,
    ) -> dict[str, BacktestResult]:
        """并行运行多个策略，返回 {策略名: BacktestResult} 字典。"""
        logger.info(f"多策略回测: {len(strategies)} 个策略")
        results = {}
        for strategy in strategies:
            logger.info(f"  开始运行: {strategy.name}")
            result = self.run(df.copy(), strategy, news_df)
            results[strategy.name] = result
        return results

    def run_all(
        self, df: pd.DataFrame, news_df: pd.DataFrame | None = None,
    ) -> dict[str, BacktestResult]:
        """运行全部三种策略（A / B / C）。"""
        logger.info("运行全部三策略回测 (A / B / C)")
        return self.run_multi(df, [
            get_execution_strategy("A"),
            get_execution_strategy("B"),
            get_execution_strategy("C"),
        ], news_df)

    # ======================== 内部辅助方法 ========================

    def _empty_result(self, name: str) -> BacktestResult:
        """返回空的回测结果（数据不足时）。"""
        return BacktestResult(
            strategy_name=name,
            initial_capital=self.config.initial_capital,
            final_equity=self.config.initial_capital,
            total_return=0.0, annual_return=0.0,
            max_drawdown=0.0, sharpe_ratio=0.0, calmar_ratio=0.0,
            win_rate=0.0, profit_loss_ratio=0.0,
            avg_holding_days=0, total_trades=0,
            equity_curve=[], trades=[], fills=[],
        )

    @staticmethod
    def _detect_market(df: pd.DataFrame) -> str:
        """从 DataFrame 推断市场类型（6 位数字 = A 股，否则 = 美股）。"""
        code = str(df.iloc[0].get("code", "")) if "code" in df.columns else ""
        if code and code.isdigit() and len(code) == 6:
            return "A"
        return "US"

    @staticmethod
    def _calc_holding_days(account: Account, current_date: str) -> int:
        """计算当前持仓的持有天数（用于时间止损判断）。"""
        if not account.position or not account.position.entry_date:
            return 0
        try:
            entry = pd.Timestamp(account.position.entry_date)
            current = pd.Timestamp(current_date)
            return (current - entry).days
        except Exception:
            return 0

    @staticmethod
    def _calc_cooldown(strategy: BaseExecutionStrategy, current_idx: int,
                       action: str) -> int:
        """
        计算冷却期结束的 K 线索引。

        冷却期规则：卖出后必须间隔 N 根 K 线才能再次开仓。
          - 策略 A：3 根
          - 策略 B：5 根
          - 策略 C：2 根
        """
        if action != "sell":
            return -1  # 只有卖出后才设冷却期
        cooldown_map = {
            "ThresholdTrendStrategy": 3,
            "MeanReversionStrategy": 5,
            "MomentumNewsStrategy": 2,
            "BollingerBreakoutStrategy": 3,
            "DualThrustStrategy": 3,
            "TurtleATRStrategy": 5,
        }
        cls_name = type(strategy).__name__
        bars = cooldown_map.get(cls_name, 3)
        return current_idx + bars
