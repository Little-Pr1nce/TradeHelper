"""无全局绘图状态的 ChartSpec 工厂。"""
from __future__ import annotations
from tradehelper_v2.contracts import ChartKind, ChartSpec, stable_hash

def chart_id(kind, title, series, baseline, samples):
    return stable_hash({"kind":kind,"title":title,"series":series,"baseline":baseline,"samples":samples})

def empty_chart(kind: ChartKind, title: str, *, interpretation: str, empty_state: str) -> ChartSpec:
    axes = {
        ChartKind.CALIBRATION: ("预测置信度", "实际发生频率"),
        ChartKind.FORECAST_TIMELINE: ("目标交易日", "预测/实际收益"),
        ChartKind.CUMULATIVE_PERFORMANCE: ("到期/成交日期", "累计收益"),
        ChartKind.DRAWDOWN: ("到期/成交日期", "回撤"),
    }
    x_axis, y_axis = axes[kind]
    return ChartSpec(chart_id(kind,title,(),(),0),kind,title,x_axis,y_axis,(),(),0,None,interpretation,empty_state)
