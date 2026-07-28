"""V2-8 的唯一组合编排入口。"""
from __future__ import annotations

from .evidence import build_portfolio_risk_snapshot
from .replacement import build_replacement_candidates


class PortfolioDecisionEngine:
    def decide(self, batch, generated_at):
        """组合决策入口；allocator 在下一步注入，避免 UI 复制业务逻辑。"""
        from .allocator import PortfolioAllocator
        snapshot=build_portfolio_risk_snapshot(valuation=batch.valuation,holding_risks=batch.holding_risks,correlation_snapshot=batch.correlation_snapshot,policy=batch.portfolio_policy,calculated_at=batch.as_of)
        from contracts import PortfolioDecisionBundle, PortfolioProfileDecision, stable_hash
        conservative,aggressive=PortfolioAllocator().allocate(batch,snapshot,generated_at)
        # replacement 是研究队列而非订单；重建 profile 只把它纳入不可变审计身份。
        def with_replacements(profile):
            replacements=build_replacement_candidates(profile,batch,generated_at)
            identity={"batch_id":profile.batch_id,"profile":profile.profile,"allocation_ids":tuple(item.allocation_id for item in profile.allocations),"group_ids":tuple(item.group_id for item in profile.reservation_groups),"holding_priority":profile.holding_priority_allocation_ids,"entry_priority":profile.entry_priority_allocation_ids,"blocked":profile.blocked_allocation_ids,"risk_snapshot":profile.current_risk_snapshot.risk_snapshot_id,"reservation":profile.reservation_snapshot,"replacement_ids":tuple(item.replacement_id for item in replacements),"grade":profile.evidence_grade,"reasons":profile.reason_codes}
            return PortfolioProfileDecision(stable_hash(identity),profile.batch_id,profile.profile,profile.allocations,profile.reservation_groups,profile.holding_priority_allocation_ids,profile.entry_priority_allocation_ids,profile.blocked_allocation_ids,profile.current_risk_snapshot,profile.reservation_snapshot,replacements,profile.evidence_grade,profile.reason_codes,generated_at)
        conservative,aggressive=with_replacements(conservative),with_replacements(aggressive)
        identity={"batch_id":batch.batch_id,"market":batch.market,"account_hash":batch.valuation.account_hash,"valuation_id":batch.valuation.valuation_id,"conservative":conservative.profile_decision_id,"aggressive":aggressive.profile_decision_id,"policy":batch.portfolio_policy.policy_version}
        return PortfolioDecisionBundle(stable_hash(identity),batch.batch_id,batch.market,batch.valuation.account_hash,batch.valuation.valuation_id,conservative,aggressive,batch.portfolio_policy.policy_version,generated_at)
