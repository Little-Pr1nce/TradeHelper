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
from data.models import PredictionLog, PriceData, TradePlanLog
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


def test_prediction_signals_follow_current_action_not_backtest_trade():
    pipeline = type("Pipeline", (), {"signal_check": [
        {
            "key": "Q", "signal": "sell", "execution_level": "B",
            "entry_price": 207.14, "stop_loss": 209.0, "take_profit": 190.0,
        },
        {
            "key": "A", "signal": "buy", "execution_level": "C",
            "entry_price": 100.0, "stop_loss": 92.0,
        },
    ]})()

    records = AnalysisService._prediction_signals(pipeline)

    assert len(records) == 1
    assert records[0]["strategy_name"] == "Q"
    assert records[0]["direction"] == "bearish"
    assert records[0]["stop_loss"] == 0.0
    assert records[0]["take_profit"] == 0.0
    assert records[0]["action"] == "sell"


def test_sell_signal_records_multi_horizon_exit_quality():
    db = _fresh_db()
    prices = []
    for i in range(1, 21):
        open_price = 101.0 - i
        close = 100.0 - i
        prices.append(PriceData(
            "AAPL", f"2026-01-{i + 1:02d}", open_price,
            open_price + 1.0, close - 1.0, close, 1000,
        ))
    db.insert_prices(prices)
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="intraday",
        predict_time="2026-01-01T12:00:00", reference_date="2026-01-01",
        direction="bearish", signal_action="sell", strategy_name="Q",
        predicted_price=100.0, entry_mode="signal_price", verify_after_days=1,
        exit_review_status="pending",
    ))

    assert db.batch_verify_expired() == 1
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()
    report = db.get_exit_review_report("AAPL")

    assert round(row["exit_return_1d"], 4) == -0.01
    assert round(row["exit_return_5d"], 4) == -0.05
    assert round(row["exit_return_10d"], 4) == -0.10
    assert round(row["exit_return_20d"], 4) == -0.20
    assert row["exit_quality"] == "effective"
    assert row["exit_avoided_loss"] > 0.19
    assert report[0]["strategy_name"] == "Q"
    assert report[0]["effective_rate"] == 1.0


def test_rising_after_sell_is_classified_as_premature():
    db = _fresh_db()
    db.insert_prices([
        PriceData(
            "AAPL", f"2026-02-{i + 1:02d}", 99.0 + i,
            101.0 + i, 98.0 + i, 100.0 + i, 1000,
        )
        for i in range(1, 21)
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="intraday",
        predict_time="2026-02-01T12:00:00", reference_date="2026-02-01",
        direction="bearish", signal_action="sell", strategy_name="Q",
        predicted_price=100.0, entry_mode="signal_price", verify_after_days=1,
        exit_review_status="pending",
    ))

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()

    assert row["exit_quality"] == "premature"
    assert row["exit_opportunity_cost"] > 0.20


def test_strategy_health_splits_buy_and_sell_samples():
    db = _fresh_db()
    for action, net_return in (
        ("buy", 0.02),
        ("sell", -0.02),
    ):
        for i in range(3):
            db.insert_trade_plan(TradePlanLog(
                code="AAPL", market="US", mode="eod",
                created_at=f"2026-03-{i + 1 + (10 if action == 'sell' else 0):02d}T00:00:00",
                strategy_key="A", signal_intent="alpha_entry" if action == "buy" else "alpha_exit",
                action=action, status="evaluated", net_return=net_return,
                evaluated_at=f"2026-04-{i+1:02d}T00:00:00",
            ))

    rows = db.get_strategy_health_report("AAPL")

    assert {row["signal_action"] for row in rows} == {"buy", "sell"}
    buy = next(row for row in rows if row["signal_action"] == "buy")
    sell = next(row for row in rows if row["signal_action"] == "sell")
    assert buy["avg_return"] > 0
    assert sell["avg_return"] < 0


def test_legacy_prediction_service_write_is_disabled():
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
        signal_action="buy",
    )

    assert pred_id is None
    assert db.execute("SELECT COUNT(*) n FROM prediction_log").fetchone()["n"] == 0


def test_same_prediction_event_is_not_counted_twice():
    db = _fresh_db()
    base = dict(
        code="AAPL", market="US", mode="eod",
        reference_date="2026-01-05", direction="bullish",
        strategy_name="A", predicted_price=100.0,
    )
    first_id = db.insert_prediction(PredictionLog(
        predict_time="2026-01-05T16:01:00", **base,
    ))
    second_id = db.insert_prediction(PredictionLog(
        predict_time="2026-01-05T16:10:00", **base,
    ))

    assert second_id == first_id
    count = db.execute(
        "SELECT COUNT(*) AS cnt FROM prediction_log WHERE code='AAPL' AND strategy_name='A'"
    ).fetchone()["cnt"]
    assert count == 1


def test_buy_and_sell_same_day_are_distinct_events():
    db = _fresh_db()
    base = dict(
        code="AAPL", market="US", mode="intraday",
        reference_date="2026-01-05", strategy_name="A",
        predicted_price=100.0,
    )
    buy_id = db.insert_prediction(PredictionLog(
        predict_time="2026-01-05T10:00:00", direction="bullish",
        signal_action="buy", **base,
    ))
    sell_id = db.insert_prediction(PredictionLog(
        predict_time="2026-01-05T15:00:00", direction="bearish",
        signal_action="sell", exit_review_status="pending", **base,
    ))

    assert buy_id != sell_id
    count = db.execute(
        "SELECT COUNT(*) AS cnt FROM prediction_log WHERE code='AAPL' AND strategy_name='A'"
    ).fetchone()["cnt"]
    assert count == 2


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
    assert row["actual_entry_price"] == 100.0
    assert row["actual_exit_type"] == "window_close"
    assert row["actual_exit_date"] == "2026-01-05"
    assert row["validation_version"] == 2


def test_prediction_validation_records_real_entry_and_stop_exit():
    db = _fresh_db()
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 100, 104, 90, 95, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="AAPL", market="US", mode="pre",
        predict_time="2026-01-01T08:00:00", reference_date="2026-01-01",
        direction="bullish", predicted_price=99.0,
        conservative_entry=99.0, entry_mode="next_open",
        stop_loss=96.0, verify_after_days=1,
    ))

    db.batch_verify_expired()
    row = db.execute("SELECT * FROM prediction_log WHERE id=?", (pred_id,)).fetchone()
    assert row["actual_entry_price"] == 100.0
    assert row["actual_exit_type"] == "stop_loss"
    assert row["actual_exit_date"] == "2026-01-02"
    assert row["validation_price"] == 96.0


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
        db.insert_trade_plan(TradePlanLog(
            code="AAPL",
            market="US",
            mode="eod",
            created_at=f"2026-01-0{i+1}T00:00:00",
            strategy_key="SmallPerfect", signal_intent="alpha_entry",
            action="buy", net_return=0.01, status="evaluated",
            evaluated_at=f"2026-01-{i+10:02d}T00:00:00",
        ))
    for i in range(5):
        db.insert_trade_plan(TradePlanLog(
            code="AAPL",
            market="US",
            mode="eod",
            created_at=f"2026-02-0{i+1}T00:00:00",
            strategy_key="BadExpectancy", signal_intent="alpha_entry",
            action="buy", net_return=-0.03, status="evaluated",
            evaluated_at=f"2026-02-{i+10:02d}T00:00:00",
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
    assert "平均净表现" in section
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


def test_sell_health_does_not_demote_entry_parameters():
    db = _fresh_db()
    feedback = db.apply_strategy_health_feedback("AAPL", [{
        "strategy_name": "A", "signal_action": "sell",
        "action": "demote", "avg_return": -0.05,
    }])

    assert feedback["demoted"] == 0
    assert db.get_best_params("AAPL", "A") is None


def test_strategy_health_uses_independent_days_and_quality_weighted_samples():
    db = _fresh_db()
    for i in range(6):
        db.insert_trade_plan(TradePlanLog(
            code="AAPL", market="US", mode="intraday",
            created_at=f"2026-03-01T10:{i:02d}:00", reference_date="2026-03-01",
            strategy_key="Repeated", signal_intent="alpha_entry", action="buy",
            status="evaluated", net_return=0.02, evidence_quality="provider",
            evaluated_at=f"2026-03-01T16:{i:02d}:00", event_key=f"repeat-{i}",
        ))
    for i in range(5):
        db.insert_trade_plan(TradePlanLog(
            code="AAPL", market="US", mode="intraday",
            created_at=f"2026-04-{i+1:02d}T10:00:00",
            reference_date=f"2026-04-{i+1:02d}",
            strategy_key="Supplemental", signal_intent="alpha_entry", action="buy",
            status="evaluated", net_return=-0.03, evidence_quality="supplemental",
            evaluated_at=f"2026-04-{i+1:02d}T16:00:00", event_key=f"supp-{i}",
        ))

    health = {row["strategy_name"]: row for row in db.get_strategy_health_report("AAPL")}
    assert "Repeated" not in health
    assert health["Supplemental"]["total"] == 5
    assert health["Supplemental"]["effective_samples"] == 3.0
    assert health["Supplemental"]["sample_status"] == "insufficient"
    assert health["Supplemental"]["action"] == "watch"


def test_strategy_health_confidence_uses_effective_sample_weight():
    db = _fresh_db()
    for quality, strategy in (("provider", "Provider"), ("supplemental", "Supplemental")):
        for i in range(8):
            db.insert_trade_plan(TradePlanLog(
                code="AAPL", market="US", mode="eod",
                created_at=f"2026-05-{i+1:02d}T16:00:00",
                decision_session_date=f"2026-05-{i+1:02d}",
                strategy_key=strategy, signal_intent="alpha_entry", action="buy",
                status="evaluated", net_return=0.02, evidence_quality=quality,
                evaluated_at=f"2026-05-{i+10:02d}T16:00:00",
            ))

    health = {row["strategy_name"]: row for row in db.get_strategy_health_report("AAPL")}
    assert health["Provider"]["effective_samples"] == 8.0
    assert health["Supplemental"]["effective_samples"] == 4.8
    assert (
        health["Supplemental"]["confidence_lower_95"]
        < health["Provider"]["confidence_lower_95"]
    )
    assert health["Supplemental"]["sample_status"] == "insufficient"


def test_thin_perfect_history_cannot_be_marked_reliable():
    db = _fresh_db()
    for i in range(5):
        db.insert_trade_plan(TradePlanLog(
            code="AAPL", market="US", mode="eod",
            created_at=f"2026-06-{i+1:02d}T16:00:00",
            decision_session_date=f"2026-06-{i+1:02d}",
            strategy_key="ThinPerfect", signal_intent="alpha_entry", action="buy",
            status="evaluated", net_return=0.02, evidence_quality="provider",
            event_key=f"thin-{i}",
        ))

    row = next(
        item for item in db.get_strategy_health_report("AAPL")
        if item["strategy_name"] == "ThinPerfect"
    )
    assert row["sample_status"] == "thin"
    assert row["action"] == "watch"
    assert row["status"] == "unstable"


def test_trade_plan_panel_weights_evidence_across_sessions():
    db = _fresh_db()
    for index, (net_return, quality) in enumerate((
        (0.10, "provider"), (-0.10, "supplemental"),
    )):
        db.insert_trade_plan(TradePlanLog(
            code="AAPL", market="US", mode="eod",
            created_at=f"2026-07-0{index+1}T16:00:00",
            decision_session_date=f"2026-07-0{index+1}",
            strategy_key="Weighted", signal_intent="alpha_entry", action="buy",
            status="evaluated", net_return=net_return, evidence_quality=quality,
            event_key=f"weighted-{index}",
        ))

    row = db.get_trade_plan_metrics(code="AAPL")[0]
    assert row["independent_days"] == 2
    assert row["effective_days"] == 1.6
    assert round(row["avg_net_return"], 4) == 0.025
    assert round(row["positive_rate"], 3) == 0.625


def test_prediction_event_migration_dedupes_before_recreating_unique_index():
    db = _fresh_db()
    db_path = db._db_path
    db._execute_write("DROP INDEX IF EXISTS idx_prediction_event")
    rows = [
        (
            "AAPL", "US", "eod", "2026-06-29T16:00:00", "2026-06-29",
            "bullish", "A", "buy", "legacy-key", 0,
        ),
        (
            "AAPL", "US", "eod", "2026-06-29T17:00:00", "2026-06-29",
            "bullish", "A", "buy", "new-key", 1,
        ),
    ]
    db._executemany_write(
        """INSERT INTO prediction_log
           (code, market, mode, predict_time, reference_date, direction,
            strategy_name, signal_action, event_key, validated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    db._execute_write(
        """CREATE UNIQUE INDEX idx_prediction_event
           ON prediction_log(event_key) WHERE event_key != ''"""
    )
    db.conn.close()
    db._conn = None

    reopened = Database.init(db_path)
    remaining = reopened.execute(
        """SELECT COUNT(*) AS n, MAX(validated) AS validated
           FROM prediction_log WHERE code='AAPL' AND strategy_name='A'"""
    ).fetchone()

    assert remaining["n"] == 1
    assert remaining["validated"] == 1


def test_legacy_regex_entry_is_quarantined_on_reopen():
    db = _fresh_db()
    db_path = db._db_path
    pred_id = db.insert_prediction(PredictionLog(
        code="MU", market="US", mode="eod",
        predict_time="2026-06-23T16:04:00", reference_date="2026-06-23",
        direction="bearish", actual_direction="bearish",
        predicted_price=1211.38, conservative_entry=12.0,
        entry_mode="reference", actual_return=-0.06,
        validated=1, validation_status="verified", validation_version=2,
    ))
    db.conn.close()
    db._conn = None

    reopened = Database.init(db_path)
    row = reopened.execute(
        "SELECT validated, validation_status FROM prediction_log WHERE id=?",
        (pred_id,),
    ).fetchone()

    assert row["validated"] == -1
    assert row["validation_status"] == "legacy_unverifiable"
    assert reopened.get_prediction_stats("MU").total_predictions == 0


def test_existing_v2_prediction_backfills_entry_and_exit_semantics():
    db = _fresh_db()
    db_path = db._db_path
    db.insert_prices([
        PriceData("GLW", "2026-07-01", 239.62, 240.87, 218.11, 220.695, 1000),
    ])
    pred_id = db.insert_prediction(PredictionLog(
        code="GLW", market="US", mode="pre", strategy_name="G",
        signal_action="buy", predict_time="2026-07-01T17:57:00",
        reference_date="2026-06-30", direction="bullish",
        predicted_price=246.93, entry_mode="next_open", stop_loss=229.31,
        validation_price=229.31, validation_end_date="2026-07-01",
        entry_triggered=1, validated=1,
        validation_status="verified", validation_version=2,
    ))
    db.conn.close()
    db._conn = None

    reopened = Database.init(db_path)
    row = reopened.execute(
        "SELECT * FROM prediction_log WHERE id=?", (pred_id,)
    ).fetchone()
    assert row["actual_entry_price"] == 239.62
    assert row["actual_exit_type"] == "stop_loss"
    assert row["actual_exit_date"] == "2026-07-01"


if __name__ == "__main__":
    test_prediction_trade_levels_come_from_signal_check()
    test_prediction_signals_follow_current_action_not_backtest_trade()
    test_sell_signal_records_multi_horizon_exit_quality()
    test_rising_after_sell_is_classified_as_premature()
    test_strategy_health_splits_buy_and_sell_samples()
    test_legacy_prediction_service_write_is_disabled()
    test_same_prediction_event_is_not_counted_twice()
    test_buy_and_sell_same_day_are_distinct_events()
    test_prediction_validation_uses_nth_close_not_max_close()
    test_prediction_validation_records_real_entry_and_stop_exit()
    test_bearish_prediction_uses_direction_adjusted_net_return()
    test_untriggered_plan_is_not_counted_as_strategy_expectancy()
    test_portfolio_prediction_validates_snapshot_equity()
    test_legacy_prediction_is_only_revalidated_when_reference_bar_matches()
    test_prediction_evaluation_panel_groups_strategy_and_regime()
    test_strategy_health_uses_sample_confidence_and_expectancy()
    test_strategy_health_feedback_marks_demoted_params()
    test_sell_health_does_not_demote_entry_parameters()
    test_strategy_health_uses_independent_days_and_quality_weighted_samples()
    test_strategy_health_confidence_uses_effective_sample_weight()
    test_thin_perfect_history_cannot_be_marked_reliable()
    test_trade_plan_panel_weights_evidence_across_sessions()
    test_prediction_event_migration_dedupes_before_recreating_unique_index()
    test_legacy_regex_entry_is_quarantined_on_reopen()
    test_existing_v2_prediction_backfills_entry_and_exit_semantics()
    print("25/25 passed")
