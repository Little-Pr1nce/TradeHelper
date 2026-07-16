"""LE09：学习层保持在冻结合同边界内。"""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def test_learning_layer_does_not_depend_on_v1_ui_llm_or_network_providers():
    text='\n'.join(path.read_text() for path in (ROOT/'tradehelper_v2'/'learning').glob('*.py'))
    for forbidden in ('services.portfolio_service','core.joint_oof','import flet','report.','llm','providers.'):
        assert forbidden not in text
    for forbidden in ('guaranteed_profit','always_profitable','rewrite_source_code','disable_stop_loss','default_account_equity','future_feature_backfill','random_time_split','industry_as_stock_evidence','untriggered_trade_success','expected_sale_cash_as_filled','llm_generated_parameter'):
        assert forbidden not in text
