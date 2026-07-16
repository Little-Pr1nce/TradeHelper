"""V2-10 的冻结映射注册表。

这里的注册表是研究层与既有 V2-3/V2-5/V2-9 合同之间唯一的窄接口。它只
描述已经由工程实现并测试过的名称、参数空间和反事实映射；绝不接受模型临时
发明的新模型、特征、策略或 DSL。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping

from tradehelper_v2.contracts import ModelFamily, StrategySpec, stable_hash


@dataclass(frozen=True, slots=True)
class ResearchMappingRegistry:
    """只读、版本化的研究映射；调用方必须显式提供生产注册内容。"""

    version: str = "research_mapping_v1"
    model_families: frozenset[str] = field(default_factory=lambda: frozenset(item.value for item in ModelFamily))
    feature_sets: frozenset[str] = field(default_factory=frozenset)
    model_parameter_spaces: Mapping[str, Mapping[str, Mapping[str, object]]] = field(default_factory=dict)
    strategies: Mapping[str, StrategySpec] = field(default_factory=dict)
    strategy_parameter_spaces: Mapping[str, Mapping[str, Mapping[str, object]]] = field(default_factory=dict)
    counterfactual_mappings: Mapping[str, str] = field(default_factory=dict)

    def model_is_registered(self, family: object, feature_set: object) -> bool:
        return isinstance(family, str) and isinstance(feature_set, str) and family in self.model_families and feature_set in self.feature_sets

    def model_parameters_valid(self, family: str, overrides: Mapping[str, object]) -> bool:
        return self._parameters_valid(self.model_parameter_spaces.get(family, {}),overrides)

    def strategy_is_registered(self, strategy_id: object) -> bool:
        return isinstance(strategy_id, str) and strategy_id in self.strategies and self.strategies[strategy_id].enabled

    def strategy_parameters_valid(self, strategy_id: str, overrides: Mapping[str, object]) -> bool:
        if self._cancels_protective_controls(overrides):
            return False
        return self._parameters_valid(self.strategy_parameter_spaces.get(strategy_id, {}),overrides)

    @staticmethod
    def _cancels_protective_controls(overrides: Mapping[str, object]) -> bool:
        disabled_tokens={"none","off","disabled","disable","false","no_stop","unbounded"}
        for raw_name,value in overrides.items():
            name=str(raw_name).strip().lower()
            if not any(token in name for token in ("stop","invalidation","validity","expiry","expires")):
                continue
            if value is None or value is False:
                return True
            if isinstance(value,str) and value.strip().lower() in disabled_tokens:
                return True
            if isinstance(value,(int,float)) and not isinstance(value,bool) and value<=0:
                return True
        return False

    @staticmethod
    def _parameters_valid(space,overrides):
        if not set(overrides).issubset(space):
            return False
        for name,value in overrides.items():
            rule=space[name]
            if "choices" in rule and value not in rule["choices"]:
                return False
            if isinstance(value,float) and not isfinite(value):
                return False
            if "minimum" in rule and (isinstance(value,bool) or not isinstance(value,(int,float)) or not rule["minimum"]<=value<=rule["maximum"]):
                return False
        return True

    def mapping_key(self, hypothesis_payload: Mapping[str, object]) -> str:
        """映射键只由已注册的机器可读字段构成，不包含 LLM 文本。"""
        return stable_hash({key: value for key, value in hypothesis_payload.items() if key not in {"research_rationale"}})


def default_research_registry() -> ResearchMappingRegistry:
    """从已经冻结的 V2-3/V2-5 注册内容构造生产研究白名单。"""
    from tradehelper_v2.forecast.feature_sets import feature_names
    from tradehelper_v2.strategies.registry import default_specs

    feature_sets=frozenset(name for name in ("tech","tech_news","tech_fund","full") if feature_names(name))
    model_spaces={
        ModelFamily.EMPIRICAL.value:{},
        ModelFamily.ANALOG.value:{"k":{"choices":(40,80)}},
        ModelFamily.MULTINOMIAL_LOGISTIC.value:{"C":{"choices":(0.1,1.0)}},
        ModelFamily.PROBABILITY_TREE.value:{"max_depth":{"choices":(2,3)}},
        ModelFamily.ENSEMBLE.value:{"weight":{"minimum":0.1,"maximum":0.9}},
        ModelFamily.REGIME_ANALOG.value:{"k":{"choices":(40,80)}},
    }
    specs={item.strategy_id:item for item in default_specs()}
    strategy_spaces={
        item.strategy_id:{
            name:(
                {"minimum":min(value*0.5,value*1.5),"maximum":max(value*0.5,value*1.5)}
                if isinstance(value,(int,float)) and not isinstance(value,bool) and value!=0
                else {"choices":(value,)}
            )
            for name,value in item.parameters.items()
        }
        for item in specs.values()
    }
    return ResearchMappingRegistry(
        feature_sets=feature_sets,model_parameter_spaces=model_spaces,
        strategies=specs,strategy_parameter_spaces=strategy_spaces,
    )
