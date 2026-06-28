"""
市场交易规则集中化。

用于报告风控估算、策略仓位换算和后续回测撮合统一口径。
这里的成本是保守估算，不替代券商真实成交回报。
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class MarketRules:
    market: str
    lot_size: int
    slippage: float
    commission: float
    min_commission: float
    sell_tax: float
    limit_up_pct: float
    limit_down_pct: float
    t_plus_one: bool

    @property
    def round_trip_cost_pct(self) -> float:
        """买入+卖出的大致摩擦成本比例。"""
        return self.slippage * 2 + self.commission * 2 + self.sell_tax


_US_RULES = MarketRules(
    market="US",
    lot_size=1,
    slippage=0.003,
    commission=0.0003,
    min_commission=0.0,
    sell_tax=0.0,
    limit_up_pct=999.0,
    limit_down_pct=999.0,
    t_plus_one=False,
)

_A_RULES = MarketRules(
    market="A",
    lot_size=100,
    slippage=0.003,
    commission=0.0003,
    min_commission=5.0,
    sell_tax=0.0005,
    limit_up_pct=0.099,
    limit_down_pct=0.099,
    t_plus_one=True,
)


def get_a_share_limit_pct(code: str = "", is_st: bool = False) -> float:
    """返回可由代码/标记可靠识别的 A 股日涨跌幅限制。"""
    normalized = str(code or "").split(".")[0].strip()
    if is_st:
        return 0.049
    if normalized.startswith(("300", "301", "688", "689")):
        return 0.199
    if normalized.startswith(("4", "8")):
        return 0.299
    return 0.099


def get_market_rules(
    market: str,
    code: str = "",
    is_st: bool = False,
) -> MarketRules:
    if (market or "").upper() != "A":
        return _US_RULES
    limit = get_a_share_limit_pct(code, is_st=is_st)
    return replace(_A_RULES, limit_up_pct=limit, limit_down_pct=limit)


def estimate_round_trip_cost(position_value: float, market: str) -> float:
    """估算一次建仓+退出的摩擦成本金额。"""
    if position_value <= 0:
        return 0.0
    rules = get_market_rules(market)
    buy_commission = max(position_value * rules.commission, rules.min_commission)
    sell_commission = max(position_value * rules.commission, rules.min_commission)
    slippage = position_value * rules.slippage * 2
    sell_tax = position_value * rules.sell_tax
    return buy_commission + sell_commission + slippage + sell_tax


def estimate_planned_loss_with_cost(
    account_equity: float,
    position_pct: float,
    entry: float,
    stop_loss: float,
    market: str,
) -> float:
    """计划止损金额 + 估算双边滑点/佣金/税费。"""
    if account_equity <= 0 or position_pct <= 0 or entry <= 0:
        return 0.0
    position_value = account_equity * position_pct
    stop_loss_pct = max(entry - stop_loss, 0.0) / entry if stop_loss > 0 else 0.0
    return position_value * stop_loss_pct + estimate_round_trip_cost(position_value, market)
