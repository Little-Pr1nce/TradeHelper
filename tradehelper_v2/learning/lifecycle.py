"""候选/影子/Champion 生命周期；任何部署切换由 repository 原子执行。"""
from __future__ import annotations
from statistics import mean
from tradehelper_v2.contracts import CandidateLifecycle, ContractViolation, PromotionDecision

_NEXT={CandidateLifecycle.CANDIDATE:{PromotionDecision.PROMOTE_TO_CHALLENGER:CandidateLifecycle.CHALLENGER,PromotionDecision.REJECT:CandidateLifecycle.RETIRED},CandidateLifecycle.CHALLENGER:{PromotionDecision.PROMOTE_TO_SHADOW:CandidateLifecycle.SHADOW,PromotionDecision.REJECT:CandidateLifecycle.RETIRED},CandidateLifecycle.SHADOW:{PromotionDecision.PROMOTE_TO_CHAMPION:CandidateLifecycle.CHAMPION,PromotionDecision.REJECT:CandidateLifecycle.RETIRED},CandidateLifecycle.CHAMPION:{PromotionDecision.ROLLBACK:CandidateLifecycle.ROLLED_BACK,PromotionDecision.SUSPEND_NEW_RISK:CandidateLifecycle.DRIFTED}}
def next_lifecycle(current, decision):
    if decision is PromotionDecision.HOLD:
        return current
    try:
        return _NEXT[current][decision]
    except KeyError as exc:
        raise ContractViolation("illegal learning lifecycle transition") from exc

def drift_decision(*, recent_values, reference_values, higher_is_worse, has_healthy_previous_champion, relative_threshold=.05, absolute_threshold=.002):
    """只把漂移转成不可变生命周期事件；从不覆盖旧 Champion 事实。"""
    recent=tuple(float(item) for item in recent_values); reference=tuple(float(item) for item in reference_values)
    if len(recent)<30 or len(reference)<60: return PromotionDecision.HOLD
    recent_mean, reference_mean = mean(recent), mean(reference)
    threshold=max(abs(reference_mean)*relative_threshold,absolute_threshold)
    degraded=(recent_mean-reference_mean)>threshold if higher_is_worse else (reference_mean-recent_mean)>threshold
    if not degraded: return PromotionDecision.HOLD
    return PromotionDecision.ROLLBACK if has_healthy_previous_champion else PromotionDecision.SUSPEND_NEW_RISK
