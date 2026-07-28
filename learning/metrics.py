"""学习层纯指标，不读取数据库、网络或当前时间。"""
from __future__ import annotations
from math import log
from math import sqrt
from statistics import mean, median
from random import Random
from contracts import DirectionProbabilities, ForecastDirection, stable_hash

def forecast_event_metrics(probabilities: DirectionProbabilities, actual: ForecastDirection, p10: float, p50: float, p90: float, actual_return: float):
    labels=(ForecastDirection.BULLISH, ForecastDirection.NEUTRAL, ForecastDirection.BEARISH)
    values=(probabilities.bullish, probabilities.neutral, probabilities.bearish)
    brier=sum((probability-(1.0 if direction is actual else 0.0))**2 for probability,direction in zip(values,labels))
    actual_probability=probabilities.for_direction(actual)
    priority={ForecastDirection.BULLISH:0,ForecastDirection.BEARISH:1,ForecastDirection.NEUTRAL:2}
    predicted=sorted(zip(values,labels),key=lambda item:(item[0],priority[item[1]]),reverse=True)[0][1]
    return {"brier":brier,"log_loss":-log(max(actual_probability,1e-15)),"direction_correct":predicted is actual,"interval_hit":p10<=actual_return<=p90,"absolute_return_error":abs(p50-actual_return)}

def expected_ece(events, bins=10):
    """最大置信度分箱 ECE；空箱不参与。"""
    events=tuple(events)
    if not events: return None
    bucket=[[] for _ in range(bins)]
    for probabilities,actual in events:
        confidence=max(probabilities.bullish,probabilities.neutral,probabilities.bearish)
        priority={ForecastDirection.BULLISH:0,ForecastDirection.BEARISH:1,ForecastDirection.NEUTRAL:2}
        predicted=sorted(((probabilities.bullish,ForecastDirection.BULLISH),(probabilities.neutral,ForecastDirection.NEUTRAL),(probabilities.bearish,ForecastDirection.BEARISH)),key=lambda item:(item[0],priority[item[1]]),reverse=True)[0][1]
        bucket[min(int(confidence*bins),bins-1)].append((confidence,predicted is actual))
    total=len(events)
    return sum(len(values)/total*abs(mean(value[0] for value in values)-mean(float(value[1]) for value in values)) for values in bucket if values)

def summarize_forecasts(outcomes, *, cutoff_at, bootstrap_draws=1000, bootstrap_block_min=5):
    """只聚合成熟且被评分的结果；不可用事件只进入 coverage 分母。"""
    visible=[item for item in outcomes if item.evaluated_at<=cutoff_at]
    matured=[item for item in visible if item.status.value=="matured" and item.event_brier is not None]
    values=lambda field:[getattr(item,field) for item in matured]
    briers=values("event_brier")
    seed=int(stable_hash(tuple(item.forecast_outcome_id for item in matured))[:8],16) if matured else 0
    return {"sample_count":len(matured),"coverage":len(matured)/len(visible) if visible else 0.0,"brier":mean(briers) if matured else None,"log_loss":mean(values("event_log_loss")) if matured else None,"direction_accuracy":mean(float(item.direction_correct) for item in matured) if matured else None,"ece":expected_ece(((item.probabilities,item.actual_direction) for item in matured)),"interval_coverage":mean(float(item.interval_hit) for item in matured) if matured else None,"p50_absolute_error":mean(values("absolute_return_error")) if matured else None,"brier_interval":block_bootstrap_interval(briers,draws=bootstrap_draws,block_min=bootstrap_block_min,seed=seed) if matured else None,"cutoff_at":cutoff_at}

def strategy_summary(net_returns, *, bootstrap_draws=1000, bootstrap_block_min=5, seed=0):
    """策略账只汇总真实成交的净收益；样本不足保持 unavailable。"""
    values=tuple(float(value) for value in net_returns)
    if not values:return {"sample_count":0,"status":"unavailable"}
    average=mean(values); downside=[min(value,0.0) for value in values]; volatility=sqrt(mean((value-average)**2 for value in values))
    peak=0.0; wealth=1.0; drawdown=0.0
    for value in values:
        wealth*=1+value; peak=max(peak,wealth); drawdown=min(drawdown,wealth/peak-1 if peak else 0.0)
    interval=block_bootstrap_interval(values,draws=bootstrap_draws,block_min=bootstrap_block_min,seed=seed)
    status="unavailable" if len(values)<10 else "insufficient" if len(values)<30 else "reliable_positive" if average>0 and interval[0]>=0 else "evaluated"
    return {"sample_count":len(values),"status":status,"mean_net_return":average,"median_net_return":median(values),"win_rate":mean(float(value>0) for value in values),"max_drawdown":drawdown,"sharpe":None if volatility==0 else average/volatility,"sortino":None if not any(downside) else average/sqrt(mean(value*value for value in downside)),"calmar":None if drawdown==0 else average/abs(drawdown),"bootstrap_interval_80":interval}

def block_bootstrap_interval(values, *, draws=1000, block_min=5, seed=0, lower=.10, upper=.90):
    """时间块 bootstrap，避免把相邻交易日错误视为独立样本。"""
    values=tuple(float(value) for value in values)
    if not values:return None
    size=len(values); block=min(max(1,block_min),size); random=Random(seed); means=[]
    # 用循环前缀和计算每个连续块，避免为每次 draw 分配 O(n) 的样本列表。
    doubled=values+values; prefix=[0.0]
    for value in doubled: prefix.append(prefix[-1]+value)
    for _ in range(draws):
        total=0.0; remaining=size
        while remaining:
            width=min(block,remaining); start=random.randrange(size)
            total += prefix[start+width]-prefix[start]
            remaining -= width
        means.append(total/size)
    means.sort(); return means[int((len(means)-1)*lower)],means[int((len(means)-1)*upper)]
