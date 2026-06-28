"""
预测追踪可信度测试。
"""

import os
import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import Database
from data.models import PredictionLog, PriceData
from report.prompts import build_strategy_health_section
from services.analysis_service import AnalysisService


class DummyPipelineResult:
    signal_check = [
        {
            "key": "A",
            "variant": "A_v1",
            "signal": "buy",
            "entry_price": 101.5,
            "stop_loss": 93.4,
            "take_profit": 118.0,
        }
    ]


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def test_prediction_trade_levels_come_from_signal_check():
    entry, stop, take_profit, strategy = AnalysisService._prediction_trade_levels(DummyPipelineResult())

    assert entry == 101.5
    assert stop == 93.4
    assert take_profit == 118.0
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


def test_prediction_validation_uses_nth_close_not_max_close():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 100, 155, 99, 150, 1000),
        PriceData("AAPL", "2026-01-05", 112, 115, 108, 110, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="eod",
        predict_time="2026-01-01T18:00:00",
        direction="bullish", predicted_price=100.0,
        verify_after_days=2,
    ))

    assert db.batch_verify_expired() == 1
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()

    assert row["validation_price"] == 110.0
    assert row["underlying_return"] == 0.10
    assert 0.09 < row["actual_return"] < 0.10
    assert row["validation_end_date"] == "2026-01-05"
    assert row["validation_version"] == 2


def test_bearish_prediction_uses_direction_adjusted_net_return():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 100, 101, 89, 90, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="intraday",
        predict_time="2026-01-01T18:00:00",
        direction="bearish", predicted_price=100.0,
        verify_after_days=1,
    ))

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()

    assert row["underlying_return"] == -0.10
    assert row["actual_direction"] == "bearish"
    assert 0.09 < row["actual_return"] < 0.10


def test_untriggered_plan_is_not_counted_as_strategy_expectancy():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 100, 105, 99, 103, 1000),
    ])
    db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="intraday",
        predict_time="2026-01-01T18:00:00",
        direction="bullish", predicted_price=100.0,
        conservative_entry=90.0,
        verify_after_days=1,
        strategy_name="A",
    ))

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log").fetchone()
    panel = db.get_prediction_evaluation_panel("AAPL")

    assert row["validation_status"] == "not_triggered"
    assert row["entry_triggered"] == 0
    assert panel["overall"]["count"] == 0


def test_portfolio_prediction_validates_snapshot_equity():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 110, 112, 108, 110, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="PORTFOLIO_US", market="US", mode="portfolio",
        predict_time="2026-01-03T08:00:00",
        reference_date="2026-01-01",
        direction="bullish",
        predicted_price=1100.0,
        verify_after_days=1,
        portfolio_snapshot=json.dumps({
            "equity": 1100.0,
            "cash": 100.0,
            "holdings": [{"code": "AAPL", "shares": 10}],
        }),
    ))

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()

    assert row["validation_status"] == "verified"
    assert row["validation_price"] == 1200.0
    assert round(row["underlying_return"], 6) == round(100 / 1100, 6)
    assert row["actual_return"] == row["underlying_return"]


def test_legacy_prediction_is_only_revalidated_when_reference_bar_matches():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-01", 99, 101, 98, 100, 1000),
        PriceData("AAPL", "2026-01-02", 101, 104, 100, 103, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="intraday",
        predict_time="2026-01-01T23:30:00",
        direction="bullish", predicted_price=100.0,
        verify_after_days=1,
    ))
    db._execute_write(
        """UPDATE prediction_log SET validation_version=1,
           reference_date='', validated=1, validation_status='pending' WHERE id=?""",
        (pred_id,),
    )

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()

    assert row["reference_date"] == "2026-01-01"
    assert row["validation_version"] == 2
    assert row["validation_status"] == "verified"


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


def test_strategy_health_uses_sample_confidence_and_expectancy():
    db = _fresh_db()
    for i in range(4):
        db.insert_prediction(PredictionLog(
            code="AAPL",
            market="US",
            mode="eod",
            predict_time=f"2026-01-0{i+1}T00:00:00",
            direction="bullish",
            actual_direction="bullish",
            actual_return=0.01,
            validated=1,
            strategy_name="SmallPerfect",
        ))
    for i in range(5):
        db.insert_prediction(PredictionLog(
            code="AAPL",
            market="US",
            mode="eod",
            predict_time=f"2026-02-0{i+1}T00:00:00",
            direction="bullish",
            actual_direction="bearish",
            actual_return=-0.03,
            validated=1,
            strategy_name="BadExpectancy",
        ))

    health = db.get_strategy_health_report("AAPL")
    by_name = {h["strategy_name"]: h for h in health}

    assert by_name["SmallPerfect"]["action"] == "watch"
    assert by_name["SmallPerfect"]["sample_status"] == "insufficient"
    assert by_name["SmallPerfect"]["confidence_lower_95"] < 1.0
    assert by_name["BadExpectancy"]["action"] == "demote"
    assert by_name["BadExpectancy"]["avg_return"] < 0

    section = build_strategy_health_section(health)
    assert "95%下界" in section
    assert "方向净收益" in section
    assert "历史平均实际收益为负" in section


def test_strategy_health_feedback_marks_demoted_params():
    db = _fresh_db()
    feedback = db.apply_strategy_health_feedback("AAPL", [{
        "strategy_name": "A",
        "action": "demote",
        "total": 5,
        "accuracy": 0.2,
        "confidence_lower_95": 0.04,
        "avg_return": -0.03,
        "risk_note": "历史平均实际收益为负",
    }])

    best = db.get_best_params("AAPL", "A")
    assert feedback["demoted"] == 1
    assert best["source"] == "demoted"
    assert db.is_strategy_demoted("AAPL", "A") is True
    assert "历史平均实际收益为负" in best["params"]["demoted_reason"]


if __name__ == "__main__":
    test_prediction_trade_levels_come_from_signal_check()
    test_save_prediction_prefers_structured_entry_over_report_regex()
    test_prediction_validation_uses_nth_close_not_max_close()
    test_bearish_prediction_uses_direction_adjusted_net_return()
    test_untriggered_plan_is_not_counted_as_strategy_expectancy()
    test_portfolio_prediction_validates_snapshot_equity()
    test_legacy_prediction_is_only_revalidated_when_reference_bar_matches()
    test_prediction_evaluation_panel_groups_strategy_and_regime()
    test_strategy_health_uses_sample_confidence_and_expectancy()
    test_strategy_health_feedback_marks_demoted_params()
    print("10/10 passed")
