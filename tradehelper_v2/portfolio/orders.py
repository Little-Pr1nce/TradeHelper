"""将一个已选组合 profile 的最终股数原样交给 V2-7 订单工厂。"""
from __future__ import annotations

from tradehelper_v2.contracts import ContractViolation, RiskProfile
from tradehelper_v2.execution.orders import OrderIntentFactory


class PortfolioOrderAssembler:
    @staticmethod
    def build(portfolio_bundle, selected_profile, plans_by_id, risk_bundles, calendar, execution_policy, requested_at, *, decision_mode=None):
        profile = selected_profile if isinstance(selected_profile, RiskProfile) else RiskProfile(str(selected_profile))
        decision = portfolio_bundle.conservative if profile is RiskProfile.CONSERVATIVE else portfolio_bundle.aggressive
        allocations={item.decision_id:item for item in decision.allocations}
        factory=OrderIntentFactory(calendar); output=[]
        for bundle in risk_bundles:
            request_map={}
            for item in bundle.decisions:
                allocation=allocations.get(item.decision_id)
                # 每个 RiskDecisionBundle 同时携带两档 profile 决定；未选档必须
                # 显式为 0，不能省略后让 V2-7 回退到风险层 approved_shares。
                request_map[item.decision_id] = allocation.final_requested_shares if allocation is not None else 0
            plan_map={item.plan_id:plans_by_id[item.plan_id] for item in bundle.decisions if item.plan_id in plans_by_id}
            if len(plan_map)!=len({item.plan_id for item in bundle.decisions}): raise ContractViolation("missing plan for portfolio order assembly")
            output.append(factory.build_bundle(bundle,plan_map,request_map,requested_at,execution_policy,decision_mode=decision_mode))
        return tuple(output)
