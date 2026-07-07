"""
组合与防抖相关测试。
"""

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
from core.data_quality import evaluate_data_quality
from indicators.technical import calc_all_indicators
from services.analysis_service import AnalysisService, _single_stock_research_item
from services.portfolio_service import (
    PortfolioService,
    _build_historical_evaluation_markdown,
    _build_portfolio_operation_summary,
    _compute_portfolio_risk_snapshot,
    _estimate_account_equity,
    _evaluate_realtime_quote_quality,
    _fetch_portfolio_realtime_quote,
    _quote_payload,
)
from services.research_observations import (
    apply_history_feedback,
    build_research_confirmation_markdown,
    build_research_history_markdown,
    build_research_confirmation_section,
    parse_llm_observations,
    ResearchObservation,
)
from report.prompts import build_prediction_footer
from report.html_enhancer import fold_long_html_tables
from report.pdf_exporter import _build_pdf_table, _get_styles, _inline_md_to_html
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


def test_realtime_quote_keeps_ohlcv_and_checks_each_stock_freshness():
    now = 1_800_000_000.0
    payload = _quote_payload(
        price=207.14,
        source="TickFlow实时报价",
        session="intraday",
        timestamp=int((now - 60) * 1000),
        bar={
            "open": 194.74, "high": 217.09, "low": 190.93,
            "volume": 2_000_000, "prev_close": 195.0,
        },
    )
    quality = _evaluate_realtime_quote_quality(payload, "intraday", now_epoch=now)

    assert payload["high"] == 217.09
    assert payload["low"] == 190.93
    assert payload["volume"] == 2_000_000
    assert quality["fresh"] is True
    assert quality["ohlc_complete"] is True
    assert quality["issues"] == []

    stale = dict(payload, timestamp=int((now - 3600) * 1000))
    stale_quality = _evaluate_realtime_quote_quality(stale, "intraday", now_epoch=now)
    assert stale_quality["fresh"] is False
    assert any("过期" in issue for issue in stale_quality["issues"])

    missing_quality = _evaluate_realtime_quote_quality(None, "intraday", now_epoch=now)
    assert missing_quality["issues"]


def test_premarket_quote_uses_extended_source_without_touching_tickflow():
    class FailIfCalledFetcher:
        def fetch_quote(self, code):
            raise AssertionError("premarket must not call TickFlow")

    extended = {
        "price": 281.60,
        "timestamp": int(datetime(
            2026, 6, 30, 6, 54,
            tzinfo=ZoneInfo("America/New_York"),
        ).timestamp() * 1000),
        "source": "Nasdaq.com",
        "prev_close": 280.0,
    }
    with patch("data.stock_fetcher.fetch_us_extended_quote", return_value=extended):
        quote = _fetch_portfolio_realtime_quote(
            "AAPL", "US", FailIfCalledFetcher(), mode="pre"
        )
    assert quote["price"] == 281.60
    assert quote["source"] == "Nasdaq.com延伸时段"
    assert quote["session"] == "pre"


def test_extended_quote_without_depth_uses_conservative_liquidity_proxy():
    now = 1_800_000_000.0
    quote = _quote_payload(
        price=100.0,
        source="Nasdaq.com延伸时段",
        session="pre",
        timestamp=int((now - 60) * 1000),
        bar={"volume": 120_000, "prev_close": 99.0},
    )
    quality = _evaluate_realtime_quote_quality(quote, "pre", now_epoch=now)

    assert quality["liquidity_proxy"] == "volume"
    assert quality["liquidity_position_multiplier"] == 0.5
    assert any("无盘口深度" in item for item in quality["warnings"])

    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    df = pd.DataFrame({
        "date": dates,
        "open": [100.0] * 80,
        "high": [101.0] * 80,
        "low": [99.0] * 80,
        "close": [100.0] * 80,
        "volume": [1_000_000.0] * 80,
    })
    report = evaluate_data_quality(
        df,
        current_price=100.0,
        news_df=pd.DataFrame({"sentiment": [0.0]}),
        fundamental_data={"pe": 20.0},
        market="US",
        realtime_quote_quality=quality,
    )
    assert report.max_position_multiplier == 0.5
    assert report.action == "reduce_position"
    assert any("流动性代理=volume" in item for item in report.notes)


def test_extended_top_of_book_spread_controls_position_cap():
    now = 1_800_000_000.0
    tight = _quote_payload(
        price=100.0, source="paid top-of-book", session="pre",
        timestamp=int((now - 30) * 1000),
        bar={"bid": 99.95, "ask": 100.05, "volume": 1},
    )
    wide = dict(tight, bid=99.0, ask=101.0)

    tight_quality = _evaluate_realtime_quote_quality(tight, "pre", now_epoch=now)
    wide_quality = _evaluate_realtime_quote_quality(wide, "pre", now_epoch=now)
    assert tight_quality["liquidity_proxy"] == "top_of_book"
    assert tight_quality["liquidity_position_multiplier"] == 0.75
    assert wide_quality["liquidity_position_multiplier"] == 0.25

def test_missing_batch_quote_does_not_fall_back_to_individual_request():
    class FailIfCalledFetcher:
        def fetch_quote(self, code):
            raise AssertionError("batch miss must remain a visible data-quality failure")

    quote = _fetch_portfolio_realtime_quote(
        "AAPL", "US", FailIfCalledFetcher(), mode="intraday",
        prefetched_quote=None, prefetch_attempted=True,
    )
    assert quote is None


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


def test_a_share_code_returns_company_name_with_online_and_fallback_paths():
    fresh_db()
    with patch(
        "services.portfolio_service.search_a_stock",
        return_value=[{"code": "600000", "name": "浦发银行", "market": "A"}],
    ):
        assert PortfolioService.search_stock("600000")["name"] == "浦发银行"

    with (
        patch("services.portfolio_service.search_a_stock", return_value=[]),
        patch(
            "services.portfolio_service.search_a_stock_fallback",
            return_value=[{"code": "600519", "name": "贵州茅台", "market": "A"}],
        ),
    ):
        assert PortfolioService.search_stock("600519")["name"] == "贵州茅台"


def test_tab1_a_share_info_fills_name_when_tickflow_only_returns_code():
    fresh_db()
    fetcher = SimpleNamespace(
        fetch_stock_info=lambda code: StockInfo(code=code, name=code, market="A")
    )
    with (
        patch("services.analysis_service.get_stock_fetcher", return_value=fetcher),
        patch(
            "services.analysis_service.search_a_stock_fallback",
            return_value=[{"code": "600519", "name": "贵州茅台", "market": "A"}],
        ),
        patch(
            "alpha.fundamental._fetch_stock_industry_baostock",
            return_value="白酒",
        ),
    ):
        info = AnalysisService()._fetch_stock_info("600519", "A")

    assert info.name == "贵州茅台"
    assert info.industry == "白酒"
    assert Database().get_stock("600519").name == "贵州茅台"


def test_database_uses_independent_connections_for_parallel_reads():
    db = fresh_db()
    db.upsert_stock(StockInfo(code="FCX", name="Freeport", market="US"))
    db.insert_prices([
        PriceData(
            code="FCX", date="2026-06-29", open=60.0, high=62.0,
            low=59.0, close=61.62, volume=1_000_000,
        )
    ])

    def read_bundle(_):
        return db.get_stock("FCX"), db.get_prices("FCX"), db.get_news("FCX")

    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(read_bundle, range(200)))

    assert len(rows) == 200
    assert all(stock and prices and prices[0].close == 61.62 for stock, prices, _ in rows)


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
    assert "减仓后何时允许重新加回" in md
    assert "未来买入/重新加回条件" in md
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


def test_portfolio_zero_capacity_blocks_buy_signal_everywhere():
    holding = Holding(
        code="FULL", name="FullPosition", market="US",
        shares=100, cost_price=100.0,
    )
    watch = WatchItem(code="NEW", name="NewCandidate", market="US")
    md = _build_portfolio_operation_summary(
        [{
            "holding": holding,
            "current_price": 100.0,
            "position_value": 10000.0,
            "signal_check": [],
        }],
        [{
            "watch_item": watch,
            "current_price": 50.0,
            "position_value": 0.0,
            "signal_check": [{
                "signal": "buy", "position_pct": 0.40,
                "entry_price": 50.0, "stop_loss": 45.0,
                "execution_level": "A", "name": "测试策略",
            }],
            "operation_plan": "RAW_EXECUTION_SHOULD_NOT_APPEAR",
        }],
        "US",
        AccountBalance(us_balance=100.0),
        10000.0,
        mode="intraday",
    )

    assert "剩余新增容量 | 0.0%" in md
    assert "| 3 | 买入/加仓 | 0 | 1 个策略候选被组合容量/可用资金闸门阻断 |" in md
    assert "禁止新增仓位" in md
    assert "买入信号候选（组合风控禁止执行）" in md
    assert "其中 0 只当前允许执行" in md
    assert "RAW_EXECUTION_SHOULD_NOT_APPEAR" not in md


def test_html_export_folds_only_long_tables():
    short = "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
    rows = "".join(f"<tr><td>{i}</td></tr>" for i in range(9))
    long_table = f"<table><tr><th>A</th></tr>{rows}</table>"

    rendered = fold_long_html_tables(short + long_table, min_data_rows=8)

    assert rendered.count('class="report-table-fold"') == 1
    assert "展开完整表格（9 行）" in rendered
    assert short in rendered


def test_pdf_tables_wrap_cells_and_normalize_literal_bold_tags():
    rows = [
        ["股票", "研究员观察", "系统理由", "下一步", "执行级别", "备注"],
        ["AAPL", "很长的观察说明" * 8, "<b>我存疑</b>：需要继续验证" * 4,
         "等待条件", "C", "非交易指令"],
    ]
    table = _build_pdf_table(rows, _get_styles())

    assert abs(sum(table._colWidths) - 460.0) < 0.01
    assert all(hasattr(cell, "wrap") for row in table._cellvalues for cell in row)
    assert _inline_md_to_html("<b>我存疑</b>") == "<b>我存疑</b>"


def test_portfolio_summary_removes_repeated_per_stock_signal_table():
    watch = WatchItem(code="NEW", name="NewCandidate", market="US")
    md = _build_portfolio_operation_summary(
        [],
        [{
            "watch_item": watch,
            "current_price": 50.0,
            "position_value": 0.0,
            "signal_check": [{
                "signal": "buy", "position_pct": 0.05,
                "entry_price": 50.0, "stop_loss": 45.0,
                "execution_level": "B", "name": "测试策略",
            }],
            "operation_plan": (
                "## 🎯 系统操作方案（代码生成）\n\n"
                "### 🛡️ 保守方案\n\n保留内容\n\n"
                "### 📡 全策略信号状态\n\nSHOULD_BE_REMOVED\n"
            ),
        }],
        "US",
        AccountBalance(us_balance=10000.0),
        10000.0,
        mode="intraday",
    )

    assert "保留内容" in md
    assert "SHOULD_BE_REMOVED" not in md


def test_account_equity_and_exposure_use_same_frozen_prices():
    holding = Holding(
        code="FCX", name="Freeport", market="US", shares=55, cost_price=54.788
    )
    frame = pd.DataFrame({"close": [61.62]})
    equity, marks = _estimate_account_equity(
        [holding], 61.18, {"FCX": frame}
    )

    assert marks["FCX"] == 61.62
    assert round(equity, 2) == 3450.28
    snapshot = _compute_portfolio_risk_snapshot(
        [{"holding": holding, "current_price": marks["FCX"]}],
        {"FCX": frame},
        equity,
    )
    assert snapshot["gross_exposure"] < 1.0


def test_research_observations_parse_and_confirm_llm_candidates():
    llm_report = """
### 研究员观察候选
| 股票 | LLM观察 | 依据 |
|------|------|------|
| Corning（GLW） | 冲高后回落，适合锁利观察 | 当日高点接近阶段高点 |
| Freeport（FCX） | 多次触碰 MA120 后反弹 | 当前接近半年线 |
| **SNDK** | 动量很强可追 | 短线价格强 |
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
    assert "已确认 | B" in section
    assert "Freeport（FCX）" in section
    assert "待验证 | C" in section
    assert "SanDisk（SNDK）" in section
    assert "系统反驳 | D" in section


def test_llm_observation_templates_are_canonical_and_scored_separately():
    parsed = parse_llm_observations("""
### 研究员观察候选
| 股票 | LLM观察 |
|------|------|
| AAPL | 创新高后假突破并回落 |
| AMD | RSI超卖并触碰布林下轨 |
| MSFT | 上升趋势回踩MA20 |
""")
    assert [item.pattern_type for item in parsed] == [
        "failed_breakout", "oversold_reversal", "trend_pullback",
    ]

    observation = ResearchObservation(
        code="AAPL", name="Apple", observation="创新高后假突破并回落",
        pattern_type="failed_breakout", system_status="已确认",
        execution_level="B", reason="事实成立", llm_proposed=1,
    )
    [updated] = apply_history_feedback([observation], lambda code, pattern: {
        "count": 10, "expectancy": "positive", "win_rate_5d": 0.8,
        "avg_return_5d": 0.04,
        "llm_count": 4, "llm_expectancy": "negative",
        "llm_win_rate_5d": 0.25, "llm_avg_directional_5d": -0.03,
    })
    assert updated.execution_level == "C"
    assert updated.system_status == "历史负期望降级"
    assert "LLM" not in updated.reason or "风控官降级" in updated.reason


def test_history_cannot_upgrade_an_observation_without_confirmed_risk():
    observation = ResearchObservation(
        code="AMD", name="AMD", observation="上升趋势回落到MA20附近",
        pattern_type="trend_pullback", system_status="待验证",
        execution_level="C", trigger_price=100.0, stop_loss=0.0,
        expected_direction="bullish", reason="缺少明确止损",
    )
    [updated] = apply_history_feedback([observation], lambda code, pattern: {
        "count": 12, "expectancy": "positive", "win_rate_5d": 0.75,
        "avg_return_5d": 0.03,
    })

    assert updated.execution_level == "C"
    assert "保持观察级别" in updated.reason
    parsed = parse_llm_observations("""
### 研究员观察候选
| 股票 | LLM观察 |
|------|------|
| AMD | 上升趋势回落到MA20附近 |
""")
    assert parsed[0].pattern_type == "trend_pullback"


def test_negative_history_never_upgrades_a_d_level_data_conflict():
    observation = ResearchObservation(
        code="AMD", name="AMD", observation="缺少当前价格",
        pattern_type="momentum", system_status="数据冲突",
        execution_level="D", reason="缺少价格",
    )
    [updated] = apply_history_feedback([observation], lambda code, pattern: {
        "count": 8, "expectancy": "negative", "win_rate_5d": 0.25,
        "avg_return_5d": -0.04,
    })

    assert updated.execution_level == "D"
    assert updated.system_status == "数据冲突"
    assert "维持D级驳回" in updated.reason


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
        trigger_operator="immediate",
        entry_triggered=1,
        validation_status="triggered",
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
            entry_triggered=1,
            validation_status="verified",
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
            entry_triggered=1,
            validation_status="verified",
        ))

    md = PortfolioService().build_historical_evaluation_panel("US")

    assert "历史预测评估面板" in md
    assert "Apple（AAPL）" in md
    assert "100%" in md
    assert "正期望" in md
    assert "观察形态表现" in md
    assert "MA120支撑" in md


def test_probability_calibration_and_joint_oof_are_explained_without_fake_zero_score():
    markdown = _build_historical_evaluation_markdown(
        forecast_rows=[{
            "label": "Apple（AAPL）", "horizon": 1,
            "verified": 0, "pending": 2, "accuracy": 0.0,
            "brier_score": 0.0, "log_loss": 0.0, "ece": 0.0,
            "interval_coverage": 0.0,
        }],
        plan_rows=[], prediction_rows=[], observation_rows=[], market="US",
        forecast_summary={
            "calibration_bins": [{
                "lower": 0.4, "upper": 0.6, "count": 8,
                "mean_confidence": 0.52, "accuracy": 0.50, "gap": 0.02,
            }],
            "regime_metrics": {"ranging": {
                "samples": 8, "accuracy": 0.5, "brier_score": 0.6,
                "log_loss": 1.0, "ece": 0.02,
            }},
        },
        joint_rows=[{
            "code": "AAPL", "data_start": "2026-01-01", "data_end": "2026-03-31",
            "samples": 60, "total_trades": 1, "total_return": 0.01,
            "benchmark_return": 0.03, "excess_return": -0.02,
            "sharpe_ratio": 0.2, "max_drawdown": 0.01,
            "drift_status": "warning",
            "trace": [{
                "date": "2026-03-20", "action": "watch", "execution_level": "C",
                "forecasts": {"1": {
                    "direction": "bullish", "actual_direction": "bearish",
                    "target_date": "2026-03-23", "correct": 0,
                }},
            }],
        }],
        context_rows=[{
            "code": "AAPL", "snapshot_count": 3, "news_snapshots": 3,
            "fundamental_snapshots": 2, "fundamental_sources": "finnhub",
            "first_captured_at": "2026-03-01T10:00:00",
            "last_captured_at": "2026-03-20T10:00:00",
        }],
    )

    assert "Log Loss" in markdown
    assert "预测校准曲线" in markdown
    assert "不同市场状态" in markdown
    assert "最终建议链样本外回放（联合OOF）" in markdown
    assert "样本外逐事件明细（联合OOF最近记录）" in markdown
    assert "三步阅读法" in markdown
    assert "校准图怎么读" in markdown
    assert "逐条追责预测、策略、风控和成交" in markdown
    assert "2026-03-23" in markdown
    assert "新闻/基本面历史时点快照" in markdown
    assert "样本不足" in markdown
    assert "当前没有有效到期样本" in markdown
    assert "| Apple（AAPL） | 待验证2 | — | — |" in markdown

    empty_markdown = _build_historical_evaluation_markdown(
        [], [], [], [], "US", forecast_summary={}, joint_rows=[],
    )
    assert "校准图保留灰色理想线" in empty_markdown
    assert "暂时无法判断模型在趋势、震荡或高波动市场中的表现" in empty_markdown

    stale_markdown = _build_historical_evaluation_markdown(
        forecast_rows=[
            {
                "label": "SNDK", "horizon": horizon,
                "verified": 0, "pending": 1, "unsupported": 1,
                "accuracy": 0.0, "brier_score": 0.0,
                "log_loss": 0.0, "ece": 0.0, "interval_coverage": 0.0,
            }
            for horizon in (1, 3, 5)
        ],
        plan_rows=[], prediction_rows=[], observation_rows=[], market="US",
        forecast_summary={}, joint_rows=[],
    )
    assert "| 0 | 3 | 3 | 不能，继续积累 |" in stale_markdown
    assert "| SNDK | 待验证1 / 已隔离1 | 待验证1 / 已隔离1 | 待验证1 / 已隔离1 |" in stale_markdown


def test_empty_forecast_metrics_still_render_calibration_axes():
    db = fresh_db()
    service = PortfolioService()
    with (
        tempfile.TemporaryDirectory() as chart_dir,
        patch("config.settings.Settings") as settings_cls,
    ):
        settings_cls.return_value.chart_dir = chart_dir
        path = service.generate_forecast_calibration_chart("US")
        assert path.endswith("forecast_calibration_US.png")
        assert os.path.exists(path)
        assert os.path.getsize(path) > 1000


def test_a_share_evaluation_includes_tab1_predictions_without_holdings():
    db = fresh_db()
    db.upsert_stock(StockInfo(code="600519", name="贵州茅台", market="A"))
    db.insert_prediction(PredictionLog(
        code="600519", market="A", mode="eod", strategy_name="A",
        signal_action="buy", predict_time="2026-06-30T15:30:00",
        reference_date="2026-06-30", direction="bullish",
        predicted_price=1500.0, validated=0,
    ))

    markdown = PortfolioService().build_historical_evaluation_panel("A")

    assert "A股历史预测评估面板" in markdown
    assert "贵州茅台（600519）" in markdown
    assert "| 已验证 | 待验证 | 不可验证 |" in markdown
    assert "| 贵州茅台（600519） | 0 | 1 | 0 |" in markdown


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


def test_research_observation_same_event_is_deduplicated():
    db = fresh_db()
    first = ResearchObservationLog(
        code="FCX", pattern_type="ma120_support",
        observed_at="2026-06-01T10:00:00", trigger_price=100.0,
        expected_direction="bullish", trigger_operator="cross_above",
    )
    second = ResearchObservationLog(
        code="FCX", pattern_type="ma120_support",
        observed_at="2026-06-01T14:00:00", trigger_price=101.0,
        expected_direction="bullish", trigger_operator="cross_above",
    )

    first_id = db.insert_research_observation(first)
    second_id = db.insert_research_observation(second)

    assert second_id == first_id
    count = db.execute(
        "SELECT COUNT(*) AS cnt FROM research_observation_log WHERE code='FCX'"
    ).fetchone()["cnt"]
    assert count == 1


def test_untriggered_observation_does_not_enter_learning_stats():
    db = fresh_db()
    obs_id = db.insert_research_observation(ResearchObservationLog(
        code="FCX", pattern_type="ma120_support",
        observed_at="2026-06-01T10:00:00", trigger_price=120.0,
        stop_loss=115.0, expected_direction="bullish",
        trigger_operator="cross_above", validation_status="pending",
    ))
    db.insert_prices([
        PriceData(
            code="FCX", date=f"2026-06-{day:02d}", open=100.0,
            high=102.0, low=98.0, close=100.0, volume=1000000,
        )
        for day in range(2, 12)
    ])

    assert db.batch_verify_research_observations() == 0
    row = db.execute(
        "SELECT validation_status, entry_triggered FROM research_observation_log WHERE id=?",
        (obs_id,),
    ).fetchone()
    assert row["validation_status"] == "not_triggered"
    assert row["entry_triggered"] == 0
    stats = db.get_research_observation_stats(code="FCX", pattern_type="ma120_support")
    assert stats["count"] == 0


def test_research_confirmation_keeps_full_observation_text():
    observation = (
        "基本面处于历史极端低位，MA120 支撑近在咫尺，"
        "建议系统复核释放资金后是否满足小仓验证的全部条件"
    )
    markdown = build_research_confirmation_markdown([
        ResearchObservation(
            code="NVDA", name="NVIDIA", observation=observation,
            system_status="已确认", execution_level="B",
        )
    ])

    assert observation in markdown
    assert "近..." not in markdown


def test_portfolio_tracking_counts_component_predictions_after_write():
    db = fresh_db()
    rows = [
        PredictionLog(
            code="AAPL", market="US", mode="pre", strategy_name="A",
            signal_action="buy", predict_time="2026-06-20T08:00:00",
            reference_date="2026-06-19", direction="bullish",
            actual_direction="bullish", actual_return=0.03,
            underlying_return=0.04, predicted_price=100.0,
            actual_entry_price=101.0, validation_price=104.0,
            actual_exit_type="window_close", actual_exit_date="2026-06-22",
            validation_end_date="2026-06-22",
            validated=1, validation_status="verified", validation_version=2,
        ),
        PredictionLog(
            code="AAPL", market="US", mode="pre", strategy_name="H",
            signal_action="buy", predict_time="2026-06-20T08:01:00",
            reference_date="2026-06-19", direction="bullish",
            actual_direction="bullish", actual_return=0.025,
            underlying_return=0.04, predicted_price=100.0,
            actual_entry_price=101.0, validation_price=103.5,
            actual_exit_type="take_profit", actual_exit_date="2026-06-22",
            validation_end_date="2026-06-22",
            validated=1, validation_status="verified", validation_version=2,
        ),
        PredictionLog(
            code="AAPL", market="US", mode="pre", strategy_name="B",
            signal_action="buy", predict_time="2026-06-30T08:00:00",
            reference_date="2026-06-29", direction="bullish", validated=0,
        ),
        PredictionLog(
            code="NVDA", market="US", mode="pre", strategy_name="C",
            signal_action="sell", predict_time="2026-06-30T08:01:00",
            reference_date="2026-06-29", direction="bearish", validated=0,
        ),
        PredictionLog(
            code="AAPL", market="US", mode="eod", strategy_name="D",
            signal_action="buy", predict_time="2026-06-30T20:00:00",
            reference_date="2026-06-29", direction="bullish", validated=0,
        ),
    ]
    for row in rows:
        db.insert_prediction(row)

    stats = db.get_prediction_stats_for_codes(["AAPL", "NVDA"], mode="pre")
    pending = db.count_unverified_predictions_for_codes(
        ["AAPL", "NVDA"], mode="pre"
    )
    validated = db.get_validated_predictions_for_codes(
        ["AAPL", "NVDA"], mode="pre", limit=5
    )
    footer = build_prediction_footer(
        "PORTFOLIO_US", stats, validated, unverified_count=pending,
        scope_label="当前组合成分股，盘前模式",
    )

    assert stats.total_predictions == 1
    assert stats.strategy_sample_count == 2
    assert pending == 2
    assert len(validated) == 1
    assert "累计已验证独立事件：1 次" in footer
    assert "待验证独立事件：2 个" in footer
    assert "统计范围：当前组合成分股，盘前模式" in footer
    assert "历史方向预测结果（非本次交易建议）" in footer
    assert "预测指定目标交易日的收盘方向，不预测精确目标价" in footer
    assert "| AAPL | 2026-06-20 08:01 | 预测2026-06-22收盘价将高于$100.00 | 2026-06-22实际收盘$104.00（+4.00%，上涨） | 对 |" in footer

    report_id = db.insert_report(AnalysisReport(
        code="PORTFOLIO_US", name="US", market="US", backtest_period="1y",
        create_time="2026-06-30T22:00:00", content="before", mode="pre",
    ))
    db.update_report_content(report_id, "after")
    assert db.execute(
        "SELECT content FROM reports WHERE id=?", (report_id,)
    ).fetchone()["content"] == "after"


if __name__ == "__main__":
    tests = [
        test_filter_reports_by_market_mode_period_rating,
        test_signal_stabilizer_reuses_small_move,
        test_realtime_quote_keeps_ohlcv_and_checks_each_stock_freshness,
        test_premarket_quote_uses_extended_source_without_touching_tickflow,
        test_extended_quote_without_depth_uses_conservative_liquidity_proxy,
        test_extended_top_of_book_spread_controls_position_cap,
        test_missing_batch_quote_does_not_fall_back_to_individual_request,
        test_holdings_watchlist_balance_crud,
        test_a_share_code_returns_company_name_with_online_and_fallback_paths,
        test_tab1_a_share_info_fills_name_when_tickflow_only_returns_code,
        test_database_uses_independent_connections_for_parallel_reads,
        test_portfolio_summary_adds_risk_overlay_and_compacts_no_signal,
        test_portfolio_summary_shows_data_quality_gate,
        test_portfolio_summary_flags_profit_lock_and_ma120_support,
        test_portfolio_risk_snapshot_limits_concentration_and_correlation,
        test_portfolio_zero_capacity_blocks_buy_signal_everywhere,
        test_html_export_folds_only_long_tables,
        test_pdf_tables_wrap_cells_and_normalize_literal_bold_tags,
        test_portfolio_summary_removes_repeated_per_stock_signal_table,
        test_account_equity_and_exposure_use_same_frozen_prices,
        test_research_observations_parse_and_confirm_llm_candidates,
        test_llm_observation_templates_are_canonical_and_scored_separately,
        test_history_cannot_upgrade_an_observation_without_confirmed_risk,
        test_negative_history_never_upgrades_a_d_level_data_conflict,
        test_research_observation_log_verifies_future_returns,
        test_research_history_feedback_demotes_negative_expectancy,
        test_historical_evaluation_panel_summarizes_predictions_and_patterns,
        test_probability_calibration_and_joint_oof_are_explained_without_fake_zero_score,
        test_empty_forecast_metrics_still_render_calibration_axes,
        test_a_share_evaluation_includes_tab1_predictions_without_holdings,
        test_single_stock_research_item_feeds_observation_confirmation,
        test_research_observation_same_event_is_deduplicated,
        test_untriggered_observation_does_not_enter_learning_stats,
        test_research_confirmation_keeps_full_observation_text,
        test_portfolio_tracking_counts_component_predictions_after_write,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
