"""LE23-LE25 / LE54-LE56：候选比较与受控搜索空间。"""
from tradehelper_v2.learning.optimizer import evidence_scope, forecast_promotion_decision

def test_brier_improvement_cannot_bypass_calibration_guardrails():
    assert forecast_promotion_decision(paired_brier_improvement=.01,log_loss_ratio=1.01,ece=.2,baseline_ece=.1,interval_coverage=.8,confirmation_samples=20,direction_classes=('up','down'))=='reject'

def test_industry_and_market_are_observation_only_fallbacks():
    assert evidence_scope(stock_samples=5,industry_samples=30,market_samples=100)=='industry_fallback'
    assert evidence_scope(stock_samples=5,industry_samples=5,market_samples=100)=='market_fallback'
