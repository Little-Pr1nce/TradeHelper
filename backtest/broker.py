"""
撮合模拟器 (Broker)

模拟真实交易环境的订单执行，包括：
  - T+1 开盘价成交 + 千分之三滑点
  - 涨跌停过滤（A 股 ±10%）
  - 流动性约束（单笔 ≤ 日成交量 5%）
  - 停牌处理
  - 佣金计算（万三）
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np

from strategies.base import Order, Fill, Position

logger = logging.getLogger(__name__)


@dataclass
class BrokerConfig:
    """撮合器配置。"""
    slippage: float = 0.003          # 千分之三基础滑点
    volatility_slippage_threshold: float = 0.30  # 年化波动超过30%后上调滑点
    volatility_slippage_factor: float = 0.01     # 每100%超额年化波动增加1%滑点
    max_dynamic_slippage: float = 0.007           # 波动附加滑点最高0.7%
    max_volume_ratio: float = 0.05   # 单笔不超过日成交量 5%
    extra_slippage: float = 0.005    # 超量额外惩罚滑点
    commission: float = 0.0003       # 万三佣金
    min_commission: float = 0.0      # A股单边最低佣金
    sell_tax: float = 0.0            # 卖出税费
    t_plus_one: bool = False         # A股当日买入不可当日卖出
    limit_up_pct: float = 0.099      # 涨停幅度（A 股 10%，美股设大值）
    limit_down_pct: float = 0.099    # 跌停幅度
    hard_stop_pct: float = 0.08      # 硬止损比例（-8%）
    time_stop_days: int = 10         # 时间止损天数
    min_shares: int = 100            # 最小交易单位：A 股 100，美股 1（由 BacktestEngine 按市场设置）


@dataclass
class Account:
    """账户状态。"""
    initial_capital: float = 100000.0
    cash: float | None = None
    last_equity: float | None = None
    position: Optional[Position] = None
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self):
        if self.cash is None:
            self.cash = self.initial_capital
        if self.last_equity is None:
            self.last_equity = self.initial_capital

    @property
    def equity(self) -> float:
        return float(self.last_equity if self.last_equity is not None else (self.cash or 0.0))


class Broker:
    """撮合模拟器 — 模拟 T+1 日订单执行。"""

    def __init__(self, config: BrokerConfig | None = None):
        self.config = config or BrokerConfig()

    def _commission(self, value: float) -> float:
        if value <= 0:
            return 0.0
        return max(value * self.config.commission, self.config.min_commission)

    def _effective_slippage(
        self,
        shares: int,
        volume: float,
        recent_volatility: float,
    ) -> float:
        """根据下单时已知的历史波动和成交量计算滑点。"""
        cfg = self.config
        excess_vol = max(float(recent_volatility or 0.0) - cfg.volatility_slippage_threshold, 0.0)
        volatility_extra = min(
            excess_vol * cfg.volatility_slippage_factor,
            cfg.max_dynamic_slippage,
        )
        liquidity_extra = (
            cfg.extra_slippage
            if volume > 0 and shares > volume * cfg.max_volume_ratio
            else 0.0
        )
        return cfg.slippage + volatility_extra + liquidity_extra

    def execute_buy(self, order: Order, bar_t1: pd.Series,
                    account: Account, prev_close: float,
                    recent_volatility: float = 0.0) -> Fill | None:
        """
        以 T+1 日开盘价执行买入订单。

        返回 Fill 对象，若被拒绝则返回 None。
        """
        cfg = self.config
        open_price = float(bar_t1["open"])
        volume_t1 = float(bar_t1.get("volume", 0))

        if order.shares < cfg.min_shares:
            logger.debug(f"{order.date}: 买入订单作废 — 股数不足最小交易单位 shares={order.shares}")
            return None

        # 1. 涨跌停过滤
        if self._is_limit_up(open_price, prev_close):
            logger.debug(f"{order.date}: 买入订单作废 — T+1 涨停 open={open_price:.2f}")
            return None

        # 2. 停牌检查
        if self._is_suspended(bar_t1):
            logger.debug(f"{order.date}: 买入订单作废 — 停牌")
            return None

        # 3. 流动性约束
        effective_slippage = self._effective_slippage(
            order.shares, volume_t1, recent_volatility
        )
        if volume_t1 > 0 and order.shares > volume_t1 * cfg.max_volume_ratio:
            logger.debug(
                f"{order.date}: 成交量超标 shares={order.shares} > "
                f"5%×vol={volume_t1*0.05:.0f}，应用额外滑点"
            )

        # 4. 成交价 = open × (1 + 滑点)
        fill_price = open_price * (1 + effective_slippage)
        fill_value = fill_price * order.shares
        commission = self._commission(fill_value)
        total_cost = fill_value + commission
        slippage_cost = fill_value - open_price * order.shares

        # 5. 资金检查
        if total_cost > account.cash:
            max_shares = int(account.cash / (fill_price * (1 + cfg.commission)) / cfg.min_shares) * cfg.min_shares
            if max_shares < cfg.min_shares:
                logger.debug(f"{order.date}: 买入订单作废 — 资金不足")
                return None
            order.shares = max_shares
            fill_value = fill_price * order.shares
            commission = self._commission(fill_value)
            total_cost = fill_value + commission
            slippage_cost = fill_value - open_price * order.shares
            while total_cost > account.cash and order.shares >= cfg.min_shares:
                order.shares -= cfg.min_shares
                fill_value = fill_price * order.shares
                commission = self._commission(fill_value)
                total_cost = fill_value + commission
                slippage_cost = fill_value - open_price * order.shares
            if order.shares < cfg.min_shares:
                logger.debug(f"{order.date}: 买入订单作废 — 资金不足最低佣金要求")
                return None

        # 6. 扣除资金，更新持仓
        account.cash -= total_cost

        # 策略级 Broker 参数覆盖
        pos_time_stop = order.time_stop_days if order.time_stop_days > 0 else 0
        pos_hard_stop = order.hard_stop_pct if order.hard_stop_pct > 0 else 0.0

        new_position = Position(
            shares=order.shares,
            avg_cost=fill_price,
            entry_date=str(bar_t1.get("date", ""))[:10],
            entry_price=fill_price,
            highest_close=fill_price,
            stop_loss=order.stop_loss if order.stop_loss > 0
                       else fill_price * (1 - cfg.hard_stop_pct),
            added_position=False,
            time_stop_days=pos_time_stop,
            hard_stop_pct=pos_hard_stop,
        )
        account.position = new_position

        fill = Fill(
            date=str(bar_t1.get("date", ""))[:10],
            order_date=order.date,
            action="buy",
            price=round(fill_price, 4),
            shares=order.shares,
            value=round(fill_value, 2),
            commission=round(commission, 2),
            slippage_cost=round(slippage_cost, 2),
            reason=order.reason,
        )
        account.fills.append(fill)

        logger.debug(
            f"{fill.date}: 买入 {fill.shares}股 @ {fill.price:.2f}, "
            f"金额={fill.value:.2f}, 佣金={fill.commission:.2f}"
        )
        return fill

    def execute_sell(self, order: Order, bar_t1: pd.Series,
                     account: Account, prev_close: float,
                     recent_volatility: float = 0.0) -> Fill | None:
        """以 T+1 日开盘价执行卖出订单。"""
        cfg = self.config
        open_price = float(bar_t1["open"])

        if not account.position or account.position.shares <= 0:
            return None

        trade_date = str(bar_t1.get("date", ""))[:10]
        if cfg.t_plus_one and account.position.entry_date == trade_date:
            logger.debug(f"{order.date}: 卖出订单作废 — A股T+1限制")
            return None

        shares_to_sell = min(order.shares, account.position.shares)

        # 涨跌停过滤
        if self._is_limit_down(open_price, prev_close):
            logger.debug(f"{order.date}: 卖出订单作废 — T+1 跌停 open={open_price:.2f}")
            return None

        if self._is_suspended(bar_t1):
            logger.debug(f"{order.date}: 卖出订单作废 — 停牌")
            return None

        volume_t1 = float(bar_t1.get("volume", 0) or 0)
        effective_slippage = self._effective_slippage(
            shares_to_sell, volume_t1, recent_volatility
        )
        fill_price = open_price * (1 - effective_slippage)
        fill_value = fill_price * shares_to_sell
        commission = self._commission(fill_value)
        sell_tax = fill_value * cfg.sell_tax
        total_fee = commission + sell_tax
        slippage_cost = open_price * shares_to_sell - fill_value

        account.cash += fill_value - total_fee
        if shares_to_sell >= account.position.shares:
            account.position = None
        else:
            account.position.shares -= shares_to_sell

        fill = Fill(
            date=str(bar_t1.get("date", ""))[:10],
            order_date=order.date,
            action="sell",
            price=round(fill_price, 4),
            shares=shares_to_sell,
            value=round(fill_value, 2),
            commission=round(total_fee, 2),
            slippage_cost=round(slippage_cost, 2),
            reason=order.reason,
        )
        account.fills.append(fill)

        logger.debug(
            f"{fill.date}: 卖出 {fill.shares}股 @ {fill.price:.2f}, "
            f"金额={fill.value:.2f}"
        )
        return fill

    def execute_order(self, order: Order, bar_t1: pd.Series,
                      account: Account, prev_close: float,
                      recent_volatility: float = 0.0) -> Fill | None:
        """统一订单执行入口。"""
        if order.action == "buy":
            return self.execute_buy(
                order, bar_t1, account, prev_close, recent_volatility
            )
        elif order.action == "sell":
            return self.execute_sell(
                order, bar_t1, account, prev_close, recent_volatility
            )
        return None

    def check_intraday_stops(self, bar_t1: pd.Series, account: Account,
                             prev_close: float, current_date: str) -> list[Fill]:
        """
        使用 T+1 日 High/Low 检查止损/止盈/时间止损是否触发。

        若触发则立即平仓（以止损价或 High/Low 成交）。
        """
        if not account.position or account.position.shares <= 0:
            return []

        cfg = self.config
        pos = account.position
        if cfg.t_plus_one and pos.entry_date == str(current_date)[:10]:
            return []
        open_price = float(bar_t1["open"])
        high = float(bar_t1["high"])
        low = float(bar_t1["low"])
        close = float(bar_t1["close"])

        exit_reason = None
        exit_price = None

        # 硬止损：优先使用策略级配置，否则用 Broker 默认 8%
        hard_stop_pct = pos.hard_stop_pct if pos.hard_stop_pct > 0 else cfg.hard_stop_pct
        hard_stop = pos.entry_price * (1 - hard_stop_pct)
        if low <= hard_stop:
            exit_reason = f"硬止损触发 low({low:.2f}) <= hard_stop({hard_stop:.2f})"
            # 日线回测无法假设跳空低开后仍能按更高的止损价成交。
            exit_price = min(open_price, hard_stop)
            if open_price < hard_stop:
                exit_reason += f"；跳空按开盘价({open_price:.2f})成交"

        # 移动止盈（最高点回撤 2×ATR，由策略设置）
        elif pos.stop_loss > 0 and low <= pos.stop_loss:
            exit_reason = f"止损触发 low({low:.2f}) <= stop({pos.stop_loss:.2f})"
            exit_price = min(open_price, pos.stop_loss)
            if open_price < pos.stop_loss:
                exit_reason += f"；跳空按开盘价({open_price:.2f})成交"

        # 时间止损：优先使用策略级配置，否则用 Broker 默认 10 天
        elif pos.entry_date:
            try:
                entry_dt = pd.Timestamp(pos.entry_date)
                current_dt = pd.Timestamp(current_date)
                holding = (current_dt - entry_dt).days
                time_stop = pos.time_stop_days if pos.time_stop_days > 0 else cfg.time_stop_days
                if holding >= time_stop:
                    exit_reason = f"时间止损 持仓{holding}天 ≥ {time_stop}天"
                    exit_price = close
            except Exception:
                pass

        if exit_reason and exit_price:
            fill_value = exit_price * pos.shares
            commission = self._commission(fill_value)
            sell_tax = fill_value * cfg.sell_tax
            total_fee = commission + sell_tax

            account.cash += fill_value - total_fee
            account.position = None

            fill = Fill(
                date=current_date,
                order_date=current_date,
                action="sell",
                price=round(exit_price, 4),
                shares=pos.shares,
                value=round(fill_value, 2),
                commission=round(total_fee, 2),
                slippage_cost=0,
                reason=exit_reason,
            )
            account.fills.append(fill)
            logger.debug(f"{current_date}: {exit_reason}")
            return [fill]

        return []

    def update_daily(self, bar: pd.Series, account: Account):
        """日终更新：记录权益曲线、更新持仓最高价。"""
        close = float(bar["close"])
        date_str = str(bar.get("date", ""))[:10]

        if account.position:
            account.position.highest_close = max(
                account.position.highest_close, close
            )

        position_value = 0.0
        if account.position and account.position.shares > 0:
            position_value = account.position.shares * close

        total_equity = account.cash + position_value
        account.last_equity = total_equity
        account.equity_curve.append({
            "date": date_str,
            "equity": round(total_equity, 2),
            "cash": round(account.cash, 2),
            "position_value": round(position_value, 2),
            "shares": account.position.shares if account.position else 0,
        })

    def _is_limit_up(self, open_price: float, prev_close: float) -> bool:
        if prev_close <= 0:
            return False
        return open_price >= prev_close * (1 + self.config.limit_up_pct - 1e-8)

    def _is_limit_down(self, open_price: float, prev_close: float) -> bool:
        if prev_close <= 0:
            return False
        return open_price <= prev_close * (1 - self.config.limit_down_pct + 1e-8)

    def _is_suspended(self, bar: pd.Series) -> bool:
        """仅在数据明确声明停牌或明确给出零成交量时判定停牌。"""
        if bool(bar.get("is_suspended", False)):
            return True
        raw_volume = bar.get("volume", np.nan)
        if raw_volume is None or pd.isna(raw_volume):
            return False
        return float(raw_volume) <= 0
