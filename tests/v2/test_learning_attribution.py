"""LE45-LE49：反事实必须成对，缺失不能以零替代。"""
from decimal import Decimal
import pytest
from tradehelper_v2.contracts import ContractViolation
from tradehelper_v2.learning.attribution import CounterfactualObservation, execution_contribution, forecast_contribution, portfolio_contribution, risk_contribution, scenario_contribution, strategy_contribution

def _observation(value, *, path="a"*64):
    return CounterfactualObservation(value,("event",),path,"fees-v1","b"*64,"policy-v1")

def test_attribution_preserves_unavailable_counterfactuals():
    assert risk_contribution(_observation(Decimal('.1')),_observation(None))['status']=='unavailable'
    assert execution_contribution(_observation(Decimal('.1')),_observation(Decimal('.08')))['value']==Decimal('-.02')
    assert portfolio_contribution(_observation(Decimal('.1')),_observation(Decimal('.12')))['value']==Decimal('.02')
    assert forecast_contribution(_observation(Decimal('.08')),_observation(Decimal('.1')))['value']==Decimal('.02')
    assert scenario_contribution(_observation(Decimal('.12')),_observation(Decimal('.1')))['value']==Decimal('.02')
    assert strategy_contribution(_observation(Decimal('.12')),_observation(Decimal('.1')))['value']==Decimal('.02')
    with pytest.raises(ContractViolation):
        execution_contribution(_observation(Decimal('.1')),_observation(Decimal('.08'),path="c"*64))
