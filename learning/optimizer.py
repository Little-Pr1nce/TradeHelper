"""预注册、有限搜索空间的候选筛选。"""
from __future__ import annotations
from itertools import product
from contracts import ContractViolation, stable_hash

def validate_candidate_parameters(space, parameters):
    """拒绝未知字段和越界值；学习层不能现场扩展搜索空间。"""
    if set(parameters)!=set(space): raise ContractViolation("candidate parameters must exactly match registered optimization space")
    for name,value in parameters.items():
        rule=space[name]
        if "type" in rule and not isinstance(value,rule["type"]): raise ContractViolation("candidate parameter has wrong registered type")
        if not rule["minimum"]<=value<=rule["maximum"] or (value-rule["minimum"])%rule["step"]!=0: raise ContractViolation("candidate parameter outside registered bounds")
    return tuple(sorted(parameters.items()))

def select_candidates(candidates, *, limit=20):
    if len(candidates)>limit: raise ContractViolation("candidate count exceeds learning policy limit")
    return tuple(sorted(candidates,key=lambda item:item.candidate_id))

def generate_candidate_grid(space, *, limit=20):
    """只在预注册离散边界内生成确定性候选，不现场扩展搜索空间。"""
    names=tuple(sorted(space))
    choices=[]
    for name in names:
        rule=space[name]
        current=rule["minimum"]; values=[]
        while current<=rule["maximum"]:
            values.append(current); current=current+rule["step"]
        choices.append(tuple(values))
    combinations=tuple(dict(zip(names,values)) for values in product(*choices))
    if len(combinations)>limit: raise ContractViolation("candidate grid exceeds learning policy limit")
    return combinations

def candidate_seed(*, candidate_id, data_hash):
    """搜索/Bootstrap 的随机性只由候选与冻结数据派生，支持确定性重跑。"""
    if len(data_hash)!=64: raise ContractViolation("candidate seed requires frozen data hash")
    return int(stable_hash({"candidate":candidate_id,"data":data_hash})[:16],16)

def paired_event_sets(candidate_events, baseline_events):
    """晋升只允许在完全相同的 OOF event 集合上比较。"""
    candidate=tuple(sorted(candidate_events)); baseline=tuple(sorted(baseline_events))
    if candidate!=baseline: raise ContractViolation("candidate and baseline require identical OOF events")
    return candidate

def forecast_promotion_decision(*, paired_brier_improvement, log_loss_ratio, ece, baseline_ece, interval_coverage, confirmation_samples, direction_classes):
    """预测候选不能以方向正确率或单一 Brier 微改善绕过校准护栏。"""
    if confirmation_samples<20 or len(set(direction_classes))<2: return "hold"
    if paired_brier_improvement<=0 or log_loss_ratio>1.02 or ece>max(.15,baseline_ece+.03) or not .65<=interval_coverage<=.95: return "reject"
    return "promote_to_challenger"

def strategy_promotion_decision(*, filled_oof_samples, fold_excess_returns, mean_net_return, bootstrap_lower_80, baseline_return, candidate_return, drawdown_reduction, sharpe_improvement):
    """策略晋升需净收益、三折和风险调整/绝对超额通道之一共同满足。

    牛市中的防守策略不必机械跑赢买入持有。基准收益为正时，候选保留
    至少 80% 收益、同时显著降低回撤并提高 Sharpe，也属于有效改进。
    """
    if filled_oof_samples<30 or len(fold_excess_returns)<3 or mean_net_return<=0 or bootstrap_lower_80<0: return "reject"
    absolute=sum(value>0 for value in fold_excess_returns)>len(fold_excess_returns)/2
    risk_adjusted=(
        baseline_return>0
        and candidate_return/baseline_return>=.80
        and drawdown_reduction>=.30
        and sharpe_improvement>=.20
    )
    return "promote_to_challenger" if absolute or risk_adjusted else "reject"

def confirmation_decision(*, samples, direction_classes=(), hard_guardrails_ok=True):
    """challenge 的确认段通过后才进入影子观察，不能直接替换 Champion。"""
    if not hard_guardrails_ok or samples<20 or (direction_classes and len(set(direction_classes))<2): return "reject"
    return "promote_to_shadow"

def shadow_decision(*, samples, hard_guardrails_ok, primary_metric_not_worse):
    """影子样本达标且硬护栏、主指标均未恶化后才允许进入 Champion。"""
    if samples<20: return "hold"
    if not hard_guardrails_ok or not primary_metric_not_worse: return "reject"
    return "promote_to_champion"

def evidence_scope(*, stock_samples, industry_samples, market_samples, min_reliable=30):
    """股票优先；行业/市场只能做观察 fallback，绝不产生股票 A 级证据。"""
    if stock_samples>=min_reliable:return "stock"
    if industry_samples>=min_reliable:return "industry_fallback"
    if market_samples>=min_reliable:return "market_fallback"
    return "insufficient"
