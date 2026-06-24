"""
预测追踪可信度测试。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import Database
from data.models import PredictionLog
from services.analysis_service import AnalysisService


class DummyPipelineResult:
    signal_check = [
        {
            "key": "A",
            "variant": "A_v1",
            "signal": "buy",
            "entry_price": 101.5,
            "stop_loss": 93.4,
        }
    ]


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def test_prediction_trade_levels_come_from_signal_check():
    entry, stop, strategy = AnalysisService._prediction_trade_levels(DummyPipelineResult())

    assert entry == 101.5
    assert stop == 93.4
    assert strategy == "A"


def test_save_prediction_prefers_structured_entry_over_report_regex():
    db = _fresh_db()

    pred_id = AnalysisService._save_prediction(
        code="AAPL",
        market="US",
        mode="eod",
        direction="bullish",
        final_score=0.4,
        predicted_price=100.0,
        report_content="入场价: $999.00",
        conservative_entry=101.5,
        stop_loss=93.4,
        strategy_name="A",
    )

    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()
    assert row["conservative_entry"] == 101.5
    assert row["stop_loss"] == 93.4
    assert row["strategy_name"] == "A"


def test_prediction_evaluation_panel_groups_strategy_and_regime():
    db = _fresh_db()
    for i in range(3):
        db.insert_prediction(PredictionLog(
            code="AAPL",
            market="US",
            mode="eod",
            predict_time=f"2026-01-0{i+1}T00:00:00",
            direction="bullish",
            actual_direction="bullish",
            actual_return=0.02,
            validated=1,
            strategy_name="A",
            market_regime="trending",
        ))

    panel = db.get_prediction_evaluation_panel("AAPL")

    assert panel["overall"]["expectancy"] == "positive"
    assert panel["by_strategy"][0]["label"] == "A"
    assert panel["by_regime"][0]["label"] == "trending"


if __name__ == "__main__":
    test_prediction_trade_levels_come_from_signal_check()
    test_save_prediction_prefers_structured_entry_over_report_regex()
    test_prediction_evaluation_panel_groups_strategy_and_regime()
    print("3/3 passed")
