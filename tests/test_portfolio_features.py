"""
组合与防抖相关测试。
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import Database
from data.models import (
    AnalysisReport,
    Holding,
    WatchItem,
    AccountBalance,
    PriceData,
    PredictionLog,
    StockInfo,
    ResearchObservationLog,
)
from core.pipeline import AnalysisResult
from indicators.technical import calc_all_indicators
from services.analysis_service import _single_stock_research_item
from services.portfolio_service import (
    PortfolioService,
    _build_portfolio_operation_summary,
    _compute_portfolio_risk_snapshot,
)
from services.research_observations import (
    apply_history_feedback,
    build_research_history_markdown,
    build_research_confirmation_section,
    parse_llm_observations,
    ResearchObservation,
)
from services.signal_stabilizer import SignalStabilizer


def fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def test_filter_reports_by_market_mode_period_rating():
    db = fresh_db()
    db.insert_report(AnalysisReport(
        code="AAPL", name="Apple", market="US", backtest_period="1y",
        create_time=datetime.now().isoformat(), content="x", rating=5, mode="intraday",
    ))
    db.insert_report(AnalysisReport(
        code="600519", name="茅台", market="A", backtest_period="6m",
        create_time=datetime.now().isoformat(), content="y", rating=3, mode="eod",
    ))
    reports = db.filter_reports(market="US", mode="intraday", period="1y", min_rating=4)
    assert len(reports) == 1
    assert reports[0].code == "AAPL"


def test_signal_stabilizer_reuses_small_move():
    db = fresh_db()
    db.insert_report(AnalysisReport(
        code="AAPL", name="Apple", market="US", backtest_period="1y",
        create_time=datetime.now().isoformat(),
        content="| 最新价 | **100.00**（+0.10%） |",
        mode="intraday",
    ))
    decision = SignalStabilizer(tolerance_pct=0.003, min_interval_minutes=10).should_emit("AAPL", 100.1)
    assert decision.should_emit is False
    assert decision.previous_report is not None


def test_holdings_watchlist_balance_crud():
    db = fresh_db()
    # 测试持仓
    db.upsert_holding(Holding(
        code="AAPL", name="Apple", market="US", shares=100, cost_price=150.0,
    ))
    holdings = db.list_holdings("US")
    assert len(holdings) == 1
    assert holdings[0].code == "AAPL"

    # 测试关注
    db.upsert_watch_item(WatchItem(code="NVDA", name="NVIDIA", market="US"))
    watchlist = db.list_watchlist("US")
    assert len(watchlist) == 1
    assert watchlist[0].code == "NVDA"

    # 测试余额
    db.save_balance(AccountBalance(us_balance=50000.0, a_balance=100000.0))
    b = db.get_balance()
    assert b.us_balance == 50000.0
    assert b.a_balance == 100000.0


def test_portfolio_summary_adds_risk_overlay_and_compacts_no_signal():
    holding = Holding(code="SOFI", name="SoFi", market="US", shares=20, cost_price=31.99)
    watch = WatchItem(code="MU", name="Micron", market="US")
    holdings_data = [{
        "holding": holding,
        "current_price": 17.62,
        "alpha_score": -0.20,
        "technical": "MACD死叉，空头排列",
        "signal_check": [{
            "signal": "no_signal",
            "name": "K 死扛回本",
            "audit": "PASS",
            "rank_score": 70,
            "no_signal_reason": "Score百分位不足",
        }],
    }]
    watchlist_data = [{
        "watch_item": watch,
        "current_price": 1018.99,
        "alpha_score": 0.09,
        "technical": "震荡",
        "signal_check": [{
            "signal": "no_signal",
            "name": "K 死扛回本",
            "audit": "PASS",
            "rank_score": 70,
            "no_signal_reason": "Score百分位=47%, Final_Score=+0.090",
        }],
    }]

    md = _build_portfolio_operation_summary(
        holdings_data, watchlist_data, "US", AccountBalance(us_balance=37.98), 8873.0
    )

    assert "一分钟操作台" in md
    assert "报告模式" in md
    assert "持仓风控提示" in md
    assert "SOFI" in md
    assert "建议清仓/强制复核" in md
    assert "无买入信号的股票" in md
    assert "| Micron（MU） | 关注 | $1018.99 | K 死扛回本 | ✅ |" in md
    assert "##### 📍 关键价位速查" not in md


def test_portfolio_summary_shows_data_quality_gate():
    watch = WatchItem(code="BAD", name="BadData", market="US")
    watchlist_data = [{
        "watch_item": watch,
        "current_price": 10.0,
        "alpha_score": 0.2,
        "technical": "震荡",
        "signal_check": [],
        "data_quality": {
            "score": 42,
            "status": "blocked",
            "action": "block",
            "max_position_multiplier": 0.0,
            "issues": ["OHLC 价格关系异常"],
            "warnings": [],
            "missing": [],
        },
    }]

    md = _build_portfolio_operation_summary(
        [], watchlist_data, "US", AccountBalance(us_balance=10000), 10000
    )

    assert "数据质量概览" in md
    assert "BadData（BAD）" in md
    assert "42/100" in md
    assert "OHLC 价格关系异常" in md
    assert "$10.00（待复核）" in md
    assert "数据质量阻断，禁止新开仓/加仓" in md
    assert "修复K线缓存并重新生成报告后再判断" in md


def test_portfolio_summary_flags_profit_lock_and_ma120_support():
    glw = Holding(code="GLW", name="Corning", market="US", shares=10, cost_price=180.22)
    fcx = Holding(code="FCX", name="Freeport", market="US", shares=35, cost_price=50.55)
    holdings_data = [
        {
            "holding": glw,
            "current_price": 207.14,
            "alpha_score": 0.05,
            "technical": "金叉",
            "technical_marker": {
                "high": 217.09,
                "low": 190.93,
                "close": 207.14,
                "high_120": 217.09,
                "ma_120": 147.98,
            },
            "signal_check": [],
        },
        {
            "holding": fcx,
            "current_price": 61.16,
            "alpha_score": -0.30,
            "technical": "震荡",
            "technical_marker": {
                "high": 62.79,
                "low": 61.11,
                "close": 61.245,
                "ma_60": 65.0,
                "ma_120": 62.26,
            },
            "signal_check": [],
        },
    ]

    md = _build_portfolio_operation_summary(
        holdings_data, [], "US", AccountBalance(us_balance=37.98), 8873.0, mode="intraday"
    )

    assert "盘中触发交易计划" in md
    assert "建议部分止盈/上移止损" in md
    assert "冲高回落锁利线" in md
    assert "当日最高 217.09" in md
    assert "关键支撑/反弹候选" in md
    assert "FCX" in md
    assert "需重新站回 62.26" in md


def test_portfolio_risk_snapshot_limits_concentration_and_correlation():
    dates = pd.date_range("2026-01-01", periods=40, freq="B")
    base = pd.Series(range(100, 140), dtype=float)
    frames = {
        code: pd.DataFrame({"date": dates, "close": base * scale})
        for code, scale in (("AAA", 1.0), ("BBB", 2.0), ("CCC", 1.5))
    }
    holdings_data = [
        {
            "holding": Holding(code="AAA", name="AAA", market="US", shares=40, cost_price=90),
            "current_price": 100.0,
            "position_value": 4000.0,
            "signal_check": [],
        },
        {
            "holding": Holding(code="BBB", name="BBB", market="US", shares=15, cost_price=180),
            "current_price": 200.0,
            "position_value": 3000.0,
            "signal_check": [],
        },
    ]
    snapshot = _compute_portfolio_risk_snapshot(holdings_data, frames, 10000.0)

    assert snapshot["max_code"] == "AAA"
    assert snapshot["max_weight"] == 0.4
    assert snapshot["gross_exposure"] == 0.7
    assert round(snapshot["new_position_capacity_pct"], 4) == 0.2
    assert snapshot["high_corr_pairs"]

    watch = WatchItem(code="CCC", name="CCC", market="US")
    md = _build_portfolio_operation_summary(
        holdings_data,
        [{
            "watch_item": watch,
            "current_price": 150.0,
            "position_value": 0.0,
            "signal_check": [{
                "signal": "buy", "position_pct": 0.40,
                "execution_level": "A", "name": "测试策略",
            }],
            "operation_plan": "",
        }],
        "US",
        AccountBalance(us_balance=3000.0),
        10000.0,
        mode="intraday",
        price_frames=frames,
    )

    assert "组合风险预算" in md
    assert "AAA / BBB" in md
    assert "高相关持仓已达到组合上限" in md


def test_research_observations_parse_and_confirm_llm_candidates():
    llm_report = """
### 研究员观察候选
| 股票 | LLM观察 | 依据 |
|------|------|------|
| Corning（GLW） | 冲高后回落，适合锁利观察 | 当日高点接近阶段高点 |
| Freeport（FCX） | 多次触碰 MA120 后反弹 | 当前接近半年线 |
| SNDK | 动量很强可追 | 短线价格强 |
"""
    parsed = parse_llm_observations(llm_report)
    assert [p.code for p in parsed] == ["GLW", "FCX", "SNDK"]

    glw = Holding(code="GLW", name="Corning", market="US", shares=10, cost_price=180.22)
    fcx = Holding(code="FCX", name="Freeport", market="US", shares=35, cost_price=50.55)
    sndk = WatchItem(code="SNDK", name="SanDisk", market="US")
    holdings_data = [
        {
            "holding": glw,
            "current_price": 207.14,
            "alpha_score": 0.05,
            "technical_marker": {
                "high": 217.09,
                "low": 190.93,
                "close": 207.14,
                "high_120": 217.09,
                "ma_120": 147.98,
            },
            "signal_check": [],
        },
        {
            "holding": fcx,
            "current_price": 61.16,
            "alpha_score": -0.30,
            "technical_marker": {
                "high": 62.79,
                "low": 61.11,
                "close": 61.245,
                "ma_60": 65.0,
                "ma_120": 62.26,
            },
            "signal_check": [],
        },
    ]
    watchlist_data = [{
        "watch_item": sndk,
        "current_price": 120.0,
        "alpha_score": -0.40,
        "technical_marker": {
            "high": 121.0,
            "close": 120.0,
            "ma_20": 105.0,
            "ma_60": 95.0,
        },
        "signal_check": [],
    }]

    section = build_research_confirmation_section(holdings_data, watchlist_data, llm_report)

    assert "研究员观察 vs 系统确认" in section
    assert "Corning（GLW）" in section
    assert "已确认 | A" in section
    assert "Freeport（FCX）" in section
    assert "待验证 | C" in section
    assert "SanDisk（SNDK）" in section
    assert "系统反驳 | D" in section


def test_research_observation_log_verifies_future_returns():
    db = fresh_db()
    obs_id = db.insert_research_observation(ResearchObservationLog(
        code="FCX",
        name="Freeport",
        market="US",
        mode="intraday",
        observed_at="2026-06-01T10:00:00",
        pattern_type="ma120_support",
        observation="触碰 MA120 后反弹观察",
        source="LLM",
        system_status="已确认",
        execution_level="B",
        trigger_price=100.0,
        stop_loss=96.0,
        expected_direction="bullish",
        llm_proposed=1,
    ))
    assert obs_id > 0

    prices = []
    closes = [101, 102, 103, 102, 105, 106, 107, 108, 107, 110]
    for i, close in enumerate(closes, start=1):
        prices.append(PriceData(
            code="FCX",
            date=f"2026-06-{i+1:02d}",
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000000,
        ))
    db.insert_prices(prices)

    assert db.batch_verify_research_observations() == 1
    row = db.execute("SELECT * FROM research_observation_log WHERE id=?", (obs_id,)).fetchone()
    assert row["validated"] == 1
    assert round(row["return_5d"], 4) == 0.05
    assert round(row["return_10d"], 4) == 0.10
    assert row["hit_stop_loss"] == 0

    stats = db.get_research_observation_stats(code="FCX", pattern_type="ma120_support")
    assert stats["count"] == 1
    assert stats["win_rate_5d"] == 1.0
    assert stats["expectancy"] == "insufficient"

    md = build_research_history_markdown(
        [ResearchObservation(
            code="FCX",
            name="Freeport",
            observation="价格触碰 MA120 附近",
            pattern_type="ma120_support",
        )],
        lambda code, pattern: db.get_research_observation_stats(code=code, pattern_type=pattern),
    )
    assert "观察形态历史表现" in md
    assert "MA120支撑" in md
    assert "100%" in md


def test_research_history_feedback_demotes_negative_expectancy():
    db = fresh_db()
    for i in range(3):
        db.insert_research_observation(ResearchObservationLog(
            code="FCX",
            name="Freeport",
            market="US",
            mode="intraday",
            observed_at=f"2026-05-0{i+1}T10:00:00",
            pattern_type="ma120_support",
            observation="触碰 MA120 后反弹观察",
            source="LLM",
            system_status="已确认",
            execution_level="B",
            trigger_price=100.0,
            stop_loss=96.0,
            expected_direction="bullish",
            llm_proposed=1,
            validated=1,
            return_5d=-0.04,
            return_10d=-0.06,
            max_adverse_return=-0.08,
        ))

    obs = ResearchObservation(
        code="FCX",
        name="Freeport",
        observation="价格触碰 MA120 附近",
        pattern_type="ma120_support",
        system_status="已确认",
        execution_level="B",
        reason="低点触碰 MA120",
    )
    [updated] = apply_history_feedback(
        [obs],
        lambda code, pattern: db.get_research_observation_stats(code=code, pattern_type=pattern),
    )

    assert updated.execution_level == "C"
    assert updated.system_status == "历史负期望降级"
    assert "风控官降级" in updated.reason


def test_historical_evaluation_panel_summarizes_predictions_and_patterns():
    db = fresh_db()
    db.upsert_holding(Holding(code="AAPL", name="Apple", market="US", shares=10, cost_price=180.0))
    for i in range(3):
        db.insert_prediction(PredictionLog(
            code="AAPL",
            market="US",
            mode="eod",
            predict_time=f"2026-05-0{i+1}T16:00:00",
            direction="bullish",
            predicted_price=100.0,
            validated=1,
            actual_return=0.02 + i * 0.01,
            actual_direction="bullish",
            strategy_name="MA120SupportRebound",
            market_regime="ranging",
        ))
        db.insert_research_observation(ResearchObservationLog(
            code="AAPL",
            name="Apple",
            market="US",
            mode="eod",
            observed_at=f"2026-05-0{i+1}T16:00:00",
            pattern_type="ma120_support",
            observation="触碰 MA120 后反弹",
            system_status="已确认",
            execution_level="B",
            trigger_price=100.0,
            stop_loss=96.0,
            expected_direction="bullish",
            validated=1,
            return_5d=0.03,
            return_10d=0.05,
            max_adverse_return=-0.015,
        ))

    md = PortfolioService().build_historical_evaluation_panel("US")

    assert "历史预测评估面板" in md
    assert "Apple（AAPL）" in md
    assert "100%" in md
    assert "正期望" in md
    assert "观察形态表现" in md
    assert "MA120支撑" in md


def test_single_stock_research_item_feeds_observation_confirmation():
    rows = []
    dates = pd.date_range("2026-01-01", periods=130, freq="B")
    for date in dates:
        rows.append({
            "date": date,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000000,
            "Final_Score": 0.0,
        })
    rows[-1].update({"low": 99.0, "close": 101.0})
    df = calc_all_indicators(pd.DataFrame(rows))
    df["Final_Score"] = 0.0
    result = AnalysisResult(
        df=df,
        backtest={},
        comparison={},
        rank_ic={},
        signal_check=[],
    )
    item = _single_stock_research_item(
        StockInfo(code="AAPL", name="Apple", market="US"),
        result,
    )

    section = build_research_confirmation_section(
        [],
        [item],
        "| 股票 | LLM观察 |\n|------|------|\n| Apple（AAPL） | 触碰 MA120 后反弹 |\n",
    )

    assert "研究员观察 vs 系统确认" in section
    assert "Apple（AAPL）" in section
    assert "MA120" in section


if __name__ == "__main__":
    tests = [
        test_filter_reports_by_market_mode_period_rating,
        test_signal_stabilizer_reuses_small_move,
        test_holdings_watchlist_balance_crud,
        test_portfolio_summary_adds_risk_overlay_and_compacts_no_signal,
        test_portfolio_summary_shows_data_quality_gate,
        test_portfolio_summary_flags_profit_lock_and_ma120_support,
        test_portfolio_risk_snapshot_limits_concentration_and_correlation,
        test_research_observations_parse_and_confirm_llm_candidates,
        test_research_observation_log_verifies_future_returns,
        test_research_history_feedback_demotes_negative_expectancy,
        test_historical_evaluation_panel_summarizes_predictions_and_patterns,
        test_single_stock_research_item_feeds_observation_confirmation,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
