"""Tab3 编辑命令：只生成新的冻结账户/关注列表快照。"""
from __future__ import annotations
from decimal import Decimal
from tradehelper_v2.contracts import AccountSnapshot, ContractViolation, Market, PositionSnapshot, WatchlistSnapshot, stable_hash

def priority_allocations(profile_decision):
    """按 V2-8 已冻结的优先 ID 排序，保护退出绝不被新增风险覆盖。"""
    by_id={item.allocation_id:item for item in profile_decision.allocations}
    ids=tuple(profile_decision.holding_priority_allocation_ids)+tuple(profile_decision.entry_priority_allocation_ids)
    return tuple(by_id[item] for item in ids if item in by_id)

def replacement_research_candidates(profile_decision):
    """替换仅是重新研究候选，不能在 UI 被解释为自动交易指令。"""
    return tuple({"source":item.source_instrument,"target":item.target_instrument,"reanalysis_required":True,"label":"研究/下一轮重分析候选"} for item in profile_decision.replacement_candidates)

def member_data_quality(input_value):
    """逐股隔离质量状态，组合其余成员不随单个失败而丢失。"""
    return tuple((item.instrument,item.data_quality) for item in input_value.instruments)

class PortfolioEditor:
    def __init__(self, repository, clock): self._repository=repository; self._clock=clock
    def create_account(self, *, market, cash):
        market = market if isinstance(market, Market) else Market(str(market))
        cash = Decimal(str(cash))
        if cash < 0: raise ContractViolation("cash cannot be negative")
        snapshot = AccountSnapshot(market, "CNY" if market is Market.A else "USD", cash, (), self._clock())
        self._repository.save_account_snapshot(snapshot); return snapshot
    def save_position(self, account, *, instrument, shares, cost_price):
        shares=Decimal(str(shares)); cost=Decimal(str(cost_price))
        if shares<=0 or cost<0 or instrument.market is not account.market: raise ContractViolation("invalid position edit")
        at=self._clock(); positions={item.instrument:item for item in account.positions}; positions[instrument]=PositionSnapshot(instrument,shares,cost,at)
        snapshot=AccountSnapshot(account.market,account.currency,account.cash,tuple(positions.values()),at); self._repository.save_account_snapshot(snapshot); return snapshot
    def save_cash(self, account, *, cash):
        cash=Decimal(str(cash))
        if cash<0: raise ContractViolation("cash cannot be negative")
        snapshot=AccountSnapshot(account.market,account.currency,cash,account.positions,self._clock()); self._repository.save_account_snapshot(snapshot); return snapshot
    def remove_position(self, account, *, instrument):
        at=self._clock(); snapshot=AccountSnapshot(account.market,account.currency,account.cash,tuple(item for item in account.positions if item.instrument!=instrument),at); self._repository.save_account_snapshot(snapshot); return snapshot
    def save_watchlist(self, *, market, instruments, held_instruments=()):
        values=tuple(sorted(set(instruments),key=lambda item:item.stable_key))
        if set(values)&set(held_instruments): raise ContractViolation("held instrument cannot be ordinary watchlist member")
        at=self._clock(); identity={"market":market,"instruments":values,"created":at}; snapshot=WatchlistSnapshot(stable_hash(identity),market,values,at); self._repository.save_watchlist_snapshot(snapshot); return snapshot
