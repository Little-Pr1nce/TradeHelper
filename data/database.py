"""
SQLite 数据库操作层

使用单例模式管理 SQLite 连接，提供以下功能：
  - 数据库初始化与自动建表（4 张核心表）
  - 股票信息 CRUD
  - 股价历史数据批量读写
  - 分析报告增删改查（含评分更新）
  - 新闻情感分析结果存储

表结构：
  stocks       — 股票基本信息缓存（主键：code）
  price_history — 日 K 线历史数据（联合主键：code + date）
  reports      — 分析报告记录（含用户评分，外键关联 stocks）
  news_sentiment — 新闻情感分析结果缓存

【扩展点】如需新增表或字段：
  1. 在 CREATE_TABLES_SQL 中添加 DDL 语句
  2. 在 data/models.py 中定义对应的 dataclass
  3. 在本模块添加对应的 CRUD 方法
"""

import json
import hashlib
import sqlite3
import logging
import math
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from data.models import (
    StockInfo, PriceData, IntradayBar, AnalysisReport, NewsItem,
    Holding, WatchItem, AccountBalance, PredictionLog, ResearchObservationLog,
    ForecastResult, ForecastModelVersion, TradePlanLog, JointOOFRun,
    FeatureContextSnapshot,
)
from config.settings import Settings

logger = logging.getLogger(__name__)


def _generated_market_datetime(generated_at: str, market: str) -> datetime | None:
    """Interpret legacy naive timestamps in the computer's local timezone."""
    try:
        value = datetime.fromisoformat(str(generated_at or ""))
        if value.tzinfo is None:
            value = value.astimezone()
        timezone = ZoneInfo(
            "Asia/Shanghai" if str(market).upper() == "A" else "America/New_York"
        )
        return value.astimezone(timezone)
    except (TypeError, ValueError):
        return None


def _generated_market_date(generated_at: str, market: str) -> str:
    value = _generated_market_datetime(generated_at, market)
    return value.date().isoformat() if value else ""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str, default: str = "''"):
    """SQLite 兼容的"加列如果不存在"——遍历 PRAGMA 列名。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}")
        logger.info(f"DB migrate: added column {table}.{column}")


def _wilson_lower_bound(successes: float, total: float, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial success rate."""
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


def _trade_plan_event_key(data: dict) -> str:
    """Build a stable key for one materially distinct user-visible plan."""
    mode = str(data.get("mode") or "eod")
    timestamp_minute = (
        int(data.get("signal_timestamp_ms") or 0) // 60_000
        if mode == "intraday" else 0
    )

    def number(name: str, digits: int) -> str:
        try:
            return f"{float(data.get(name) or 0.0):.{digits}f}"
        except (TypeError, ValueError):
            return f"{0.0:.{digits}f}"

    return "|".join((
        "v2",
        str(data.get("code") or "").upper(),
        mode,
        str(
            data.get("decision_session_date")
            or data.get("reference_date")
            or data.get("created_at")
            or ""
        )[:10],
        str(data.get("strategy_key") or "unknown"),
        str(data.get("strategy_version") or "unknown"),
        str(data.get("signal_intent") or "unknown"),
        str(data.get("action") or "watch"),
        str(data.get("execution_level") or "C"),
        number("trigger_price", 6),
        number("stop_loss", 6),
        number("take_profit", 6),
        number("position_pct", 6),
        number("max_loss_amount", 2),
        _account_snapshot_signature(data.get("account_snapshot_json")),
        str(timestamp_minute),
    ))


def _account_snapshot_signature(value) -> str:
    """Hash only account fields that materially change plan sizing or costs."""
    if isinstance(value, dict):
        snapshot = value
    else:
        try:
            snapshot = json.loads(value or "{}")
        except (TypeError, ValueError):
            snapshot = {}
    material = {
        key: snapshot.get(key)
        for key in ("account_equity", "cash", "shares", "cost_price")
        if key in snapshot
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _minute_session_is_complete(
    market: str, target_session: str, last_timestamp_ms: int,
    available_sessions: list[str],
) -> bool:
    """Require a later session or a bar near the official regular close."""
    if any(str(value) > target_session for value in available_sessions):
        return True
    try:
        from zoneinfo import ZoneInfo

        timezone = ZoneInfo(
            "Asia/Shanghai" if str(market).upper() == "A" else "America/New_York"
        )
        last_bar = datetime.fromtimestamp(int(last_timestamp_ms) / 1000, timezone)
        close_minutes = 15 * 60 if str(market).upper() == "A" else 16 * 60
        return (
            last_bar.date().isoformat() == target_session
            and last_bar.hour * 60 + last_bar.minute >= close_minutes - 10
        )
    except Exception:
        return False


# ======================== 数据库建表 DDL ========================

CREATE_TABLES_SQL = """
-- 股票基本信息表：缓存从 API 获取的股票元数据
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,           -- 股票代码（A 股 6 位数字 / 美股字母）
    name TEXT NOT NULL,              -- 股票名称
    market TEXT NOT NULL,            -- 市场："A" / "US"
    industry TEXT DEFAULT '',        -- 所属行业
    description TEXT DEFAULT '',     -- 公司简介
    listing_date TEXT DEFAULT '',    -- 当前证券上市日期（用于隔离上市前伪K线）
    update_time TEXT DEFAULT ''     -- 信息更新时间
);

-- 日 K 线历史数据表：存储股价 OHLCV 数据
CREATE TABLE IF NOT EXISTS price_history (
    code TEXT NOT NULL,              -- 股票代码
    date TEXT NOT NULL,              -- 交易日期 "YYYY-MM-DD"
    open REAL NOT NULL,              -- 开盘价
    high REAL NOT NULL,              -- 最高价
    low REAL NOT NULL,               -- 最低价
    close REAL NOT NULL,             -- 收盘价
    volume REAL NOT NULL,            -- 成交量
    PRIMARY KEY (code, date)         -- 同一天同一股票只有一条记录
);

-- 索引：加速按股票代码和日期查询
CREATE INDEX IF NOT EXISTS idx_price_code ON price_history(code);
CREATE INDEX IF NOT EXISTS idx_price_date ON price_history(date);

-- 分钟K只作为盘中方案的前瞻验证证据，禁止与正式日K混写。
CREATE TABLE IF NOT EXISTS intraday_price_history (
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0.0,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'supplemental',
    PRIMARY KEY (code, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_intraday_scope
ON intraday_price_history(code, session_date, timestamp_ms);

-- 分析报告表：存储每次分析生成的完整报告
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键
    code TEXT NOT NULL,                     -- 股票代码
    name TEXT NOT NULL,                     -- 股票名称（冗余，便于展示）
    market TEXT NOT NULL,                   -- 市场
    backtest_period TEXT NOT NULL,         -- 回测周期
    create_time TEXT NOT NULL,             -- 创建时间
    content TEXT NOT NULL,                  -- 报告正文（Markdown）
    chart_path TEXT DEFAULT '',            -- K 线图 PNG 路径（生成报告时写入）
    pdf_path TEXT DEFAULT '',              -- PDF 导出路径
    rating INTEGER DEFAULT NULL,           -- 用户评分 1-5
    rated_at TEXT DEFAULT ''              -- 评分时间
);

CREATE INDEX IF NOT EXISTS idx_reports_code ON reports(code);
CREATE INDEX IF NOT EXISTS idx_reports_time ON reports(create_time);

-- 新闻情感分析结果表：缓存分析与情感标签
CREATE TABLE IF NOT EXISTS news_sentiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键
    code TEXT NOT NULL,                     -- 关联股票代码
    date TEXT NOT NULL,                     -- 新闻日期
    title TEXT NOT NULL,                    -- 新闻标题
    source TEXT DEFAULT '',                 -- 新闻来源
    sentiment TEXT DEFAULT '',             -- 情感标签
    confidence REAL DEFAULT 0.0,           -- 置信度
    content TEXT DEFAULT '',               -- 新闻摘要
    is_macro INTEGER DEFAULT 0,            -- 是否宏观新闻
    published_at TEXT DEFAULT '',          -- 数据源发布时间
    fetched_at TEXT DEFAULT ''             -- 实际抓取时间，用于缓存失效
);

CREATE INDEX IF NOT EXISTS idx_news_code ON news_sentiment(code);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_sentiment(date);
-- 新闻刷新状态：即使数据源返回 0 条，也能记录已刷新，避免反复请求。
CREATE TABLE IF NOT EXISTS news_refresh_state (
    code TEXT PRIMARY KEY,
    market TEXT DEFAULT '',
    last_attempt_at TEXT DEFAULT '',
    last_success_at TEXT DEFAULT '',
    status TEXT DEFAULT '',
    item_count INTEGER DEFAULT 0
);

-- 用户持仓表
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    market TEXT DEFAULT 'US',
    shares REAL DEFAULT 0.0,
    cost_price REAL DEFAULT 0.0,
    UNIQUE(code)
);

CREATE INDEX IF NOT EXISTS idx_holdings_market ON holdings(market);

-- 关注股票表
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT DEFAULT '',
    market TEXT DEFAULT 'US'
);

CREATE INDEX IF NOT EXISTS idx_watchlist_market ON watchlist(market);

-- 账户余额表（单条记录 id=1）
CREATE TABLE IF NOT EXISTS account_balance (
    id INTEGER PRIMARY KEY DEFAULT 1,
    us_balance REAL DEFAULT 0.0,
    a_balance REAL DEFAULT 0.0
);

-- 确保单条记录存在
INSERT OR IGNORE INTO account_balance (id, us_balance, a_balance) VALUES (1, 0.0, 0.0);

-- 独立市场预测：只记录预测事实和到期结果，不混入交易动作。
CREATE TABLE IF NOT EXISTS forecast_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'eod',
    generated_at TEXT NOT NULL,
    data_cutoff TEXT NOT NULL,
    target_session_date TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    reference_price REAL NOT NULL,
    prob_up REAL NOT NULL,
    prob_flat REAL NOT NULL,
    prob_down REAL NOT NULL,
    expected_return_p10 REAL DEFAULT 0.0,
    expected_return_p50 REAL DEFAULT 0.0,
    expected_return_p90 REAL DEFAULT 0.0,
    direction TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    market_regime TEXT DEFAULT 'unknown',
    model_version TEXT NOT NULL,
    feature_snapshot_hash TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    calendar_source TEXT DEFAULT '',
    event_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    actual_price REAL DEFAULT 0.0,
    actual_return REAL DEFAULT 0.0,
    actual_direction TEXT DEFAULT '',
    correct INTEGER DEFAULT 0,
    brier_score REAL DEFAULT 0.0,
    interval_hit INTEGER DEFAULT 0,
    verified_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_forecast_due
ON forecast_log(status, target_session_date);
CREATE INDEX IF NOT EXISTS idx_forecast_scope
ON forecast_log(code, market, horizon, generated_at);

-- 新闻/基本面历史时点快照：只记录应用在当时真实看到的上下文。
CREATE TABLE IF NOT EXISTS feature_context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'eod',
    captured_at TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    news_score REAL DEFAULT 0.0,
    news_count INTEGER DEFAULT 0,
    news_latest_published_at TEXT DEFAULT '',
    news_sources_json TEXT NOT NULL DEFAULT '[]',
    fundamental_json TEXT NOT NULL DEFAULT '{}',
    fundamental_source TEXT DEFAULT '',
    quality_status TEXT NOT NULL DEFAULT 'empty',
    payload_hash TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_feature_context_scope
ON feature_context_snapshots(code, captured_at, effective_date);

CREATE TABLE IF NOT EXISTS forecast_model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL DEFAULT '*',
    market TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'challenger',
    params_json TEXT NOT NULL DEFAULT '{}',
    feature_set_json TEXT NOT NULL DEFAULT '[]',
    train_start TEXT DEFAULT '',
    train_end TEXT DEFAULT '',
    sample_count INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0.0,
    brier_score REAL DEFAULT 0.0,
    log_loss REAL DEFAULT 0.0,
    calibration_error REAL DEFAULT 0.0,
    baseline_brier REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    promoted_at TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    UNIQUE(market, horizon, version)
);
CREATE INDEX IF NOT EXISTS idx_forecast_model_status
ON forecast_model_versions(market, horizon, status);

-- 最终联合策略的嵌套样本外回放；与单独预测、单策略回测分账保存。
CREATE TABLE IF NOT EXISTS joint_oof_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    data_start TEXT NOT NULL,
    data_end TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    samples INTEGER DEFAULT 0,
    actionable_signals INTEGER DEFAULT 0,
    forecast_gate_active INTEGER DEFAULT 0,
    total_return REAL DEFAULT 0.0,
    annual_return REAL DEFAULT 0.0,
    benchmark_return REAL DEFAULT 0.0,
    excess_return REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    sharpe_ratio REAL DEFAULT 0.0,
    win_rate REAL DEFAULT 0.0,
    total_trades INTEGER DEFAULT 0,
    forecast_brier REAL DEFAULT 0.0,
    forecast_log_loss REAL DEFAULT 0.0,
    forecast_ece REAL DEFAULT 0.0,
    horizon_metrics_json TEXT NOT NULL DEFAULT '{}',
    calibration_json TEXT NOT NULL DEFAULT '[]',
    regime_metrics_json TEXT NOT NULL DEFAULT '{}',
    fold_summaries_json TEXT NOT NULL DEFAULT '[]',
    trace_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(code, data_end, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_joint_oof_scope
ON joint_oof_runs(market, code, data_end);

CREATE TABLE IF NOT EXISTS trade_plan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id INTEGER,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'eod',
    created_at TEXT NOT NULL,
    reference_date TEXT NOT NULL DEFAULT '',
    decision_session_date TEXT NOT NULL DEFAULT '',
    signal_timestamp_ms INTEGER DEFAULT 0,
    strategy_key TEXT DEFAULT '',
    strategy_version TEXT DEFAULT '',
    signal_intent TEXT DEFAULT '',
    action TEXT NOT NULL DEFAULT 'watch',
    execution_level TEXT DEFAULT 'C',
    trigger_price REAL DEFAULT 0.0,
    stop_loss REAL DEFAULT 0.0,
    take_profit REAL DEFAULT 0.0,
    position_pct REAL DEFAULT 0.0,
    max_loss_amount REAL DEFAULT 0.0,
    account_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    entry_price REAL DEFAULT 0.0,
    exit_price REAL DEFAULT 0.0,
    net_return REAL DEFAULT 0.0,
    max_favorable_excursion REAL DEFAULT 0.0,
    max_adverse_excursion REAL DEFAULT 0.0,
    opportunity_cost REAL DEFAULT 0.0,
    outcome TEXT DEFAULT '',
    evidence_sources TEXT DEFAULT '',
    evidence_quality TEXT DEFAULT '',
    evidence_bar_count INTEGER DEFAULT 0,
    evaluated_at TEXT DEFAULT '',
    event_key TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(forecast_id) REFERENCES forecast_log(id)
);
CREATE INDEX IF NOT EXISTS idx_trade_plan_scope
ON trade_plan_log(code, strategy_key, signal_intent, status);

-- 预测追踪表（预测→验证→反馈闭环）
CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'eod',
    report_id INTEGER,
    predict_time TEXT NOT NULL,
    reference_date TEXT DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    final_score REAL DEFAULT 0.0,
    predicted_price REAL DEFAULT 0.0,
    key_reason TEXT DEFAULT '',
    confidence TEXT DEFAULT '',
    conservative_entry REAL DEFAULT 0.0,
    aggressive_entry REAL DEFAULT 0.0,
    entry_mode TEXT DEFAULT 'reference',
    stop_loss REAL DEFAULT 0.0,
    take_profit REAL DEFAULT 0.0,
    verify_after_days INTEGER DEFAULT 5,
    validated INTEGER DEFAULT 0,
    actual_return REAL DEFAULT 0.0,
    underlying_return REAL DEFAULT 0.0,
    validation_price REAL DEFAULT 0.0,
    actual_entry_price REAL DEFAULT 0.0,
    actual_exit_type TEXT DEFAULT '',
    actual_exit_date TEXT DEFAULT '',
    max_favorable_excursion REAL DEFAULT 0.0,
    max_adverse_excursion REAL DEFAULT 0.0,
    actual_direction TEXT DEFAULT '',
    entry_triggered INTEGER DEFAULT 0,
    verified_at TEXT DEFAULT '',
    validation_end_date TEXT DEFAULT '',
    validation_status TEXT DEFAULT 'pending',
    validation_version INTEGER DEFAULT 2,
    strategy_name TEXT DEFAULT '',
    signal_action TEXT DEFAULT '',
    market_regime TEXT DEFAULT '',
    portfolio_snapshot TEXT DEFAULT '',
    event_key TEXT DEFAULT '',
    exit_review_status TEXT DEFAULT 'not_applicable',
    exit_return_1d REAL DEFAULT 0.0,
    exit_return_3d REAL DEFAULT 0.0,
    exit_return_5d REAL DEFAULT 0.0,
    exit_return_10d REAL DEFAULT 0.0,
    exit_return_20d REAL DEFAULT 0.0,
    exit_max_decline REAL DEFAULT 0.0,
    exit_max_rally REAL DEFAULT 0.0,
    exit_avoided_loss REAL DEFAULT 0.0,
    exit_opportunity_cost REAL DEFAULT 0.0,
    exit_quality TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_prediction_code ON prediction_log(code);
CREATE INDEX IF NOT EXISTS idx_prediction_validated ON prediction_log(validated, predict_time);

-- 策略变体回测缓存表：加速 expand_pool，避免重复计算
CREATE TABLE IF NOT EXISTS bt_variant_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    strategy_key TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    data_start TEXT NOT NULL,
    data_end TEXT NOT NULL,
    data_length INTEGER NOT NULL DEFAULT 0,
    sharpe_ratio REAL DEFAULT 0.0,
    total_return REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    win_rate REAL DEFAULT 0.0,
    total_trades INTEGER DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE(stock_code, strategy_key, params_json, data_start, data_end)
);
CREATE INDEX IF NOT EXISTS idx_bt_cache_lookup ON bt_variant_cache(stock_code, strategy_key, data_start, data_end);

-- 每股票每策略最佳参数表：自适应优化的核心存储
CREATE TABLE IF NOT EXISTS per_stock_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    strategy_key TEXT NOT NULL,
    best_params_json TEXT NOT NULL DEFAULT '{}',
    best_sharpe REAL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'audit_pass',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(stock_code, strategy_key)
);
CREATE INDEX IF NOT EXISTS idx_per_stock_lookup ON per_stock_params(stock_code, strategy_key);

-- 参数候选生命周期：walk-forward 候选必须跨数据窗口确认后才能晋升。
CREATE TABLE IF NOT EXISTS strategy_param_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    strategy_key TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    test_sharpe REAL DEFAULT 0.0,
    avg_oos_return REAL DEFAULT 0.0,
    avg_oos_excess_return REAL DEFAULT 0.0,
    avg_oos_sharpe REAL DEFAULT 0.0,
    oos_trades INTEGER DEFAULT 0,
    selected_windows INTEGER DEFAULT 0,
    positive_excess_windows INTEGER DEFAULT 0,
    risk_adjusted_windows INTEGER DEFAULT 0,
    qualified_windows INTEGER DEFAULT 0,
    promotion_path TEXT DEFAULT '',
    confirmations INTEGER DEFAULT 0,
    first_eligible_data_end TEXT DEFAULT '',
    last_data_end TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(stock_code, strategy_key, params_json)
);
CREATE INDEX IF NOT EXISTS idx_param_candidate_status
ON strategy_param_candidates(stock_code, strategy_key, status);

CREATE TABLE IF NOT EXISTS deep_optimization_runs (
    stock_code TEXT NOT NULL,
    data_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    variant_count INTEGER DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT DEFAULT '',
    error TEXT DEFAULT '',
    PRIMARY KEY(stock_code, data_end)
);

-- 研究员/系统观察形态记录：LLM 观察 → 系统确认 → 后续表现验证
CREATE TABLE IF NOT EXISTS research_observation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'eod',
    report_id INTEGER,
    observed_at TEXT NOT NULL DEFAULT '',
    pattern_type TEXT NOT NULL DEFAULT '',
    observation TEXT DEFAULT '',
    source TEXT DEFAULT '',
    system_status TEXT DEFAULT '',
    execution_level TEXT DEFAULT '',
    trigger_price REAL DEFAULT 0.0,
    stop_loss REAL DEFAULT 0.0,
    expected_direction TEXT DEFAULT '',
    llm_proposed INTEGER DEFAULT 0,
    market_regime TEXT DEFAULT '',
    event_key TEXT DEFAULT '',
    trigger_operator TEXT DEFAULT '',
    entry_triggered INTEGER DEFAULT 0,
    triggered_at TEXT DEFAULT '',
    validation_status TEXT DEFAULT 'pending',
    validated INTEGER DEFAULT 0,
    return_1d REAL DEFAULT 0.0,
    return_3d REAL DEFAULT 0.0,
    return_5d REAL DEFAULT 0.0,
    return_10d REAL DEFAULT 0.0,
    max_adverse_return REAL DEFAULT 0.0,
    hit_take_profit INTEGER DEFAULT 0,
    hit_stop_loss INTEGER DEFAULT 0,
    verified_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_research_obs_code ON research_observation_log(code, observed_at);
CREATE INDEX IF NOT EXISTS idx_research_obs_pattern ON research_observation_log(pattern_type, execution_level);
CREATE INDEX IF NOT EXISTS idx_research_obs_validated ON research_observation_log(validated, observed_at);
"""


def _dedupe_news(conn: sqlite3.Connection) -> int:
    """合并历史重复新闻，优先保留有 sentiment 且 confidence 更高的记录。"""
    before = conn.execute("SELECT COUNT(*) FROM news_sentiment").fetchone()[0]
    conn.execute("""
        DELETE FROM news_sentiment WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY code, date, title
                       ORDER BY (sentiment != '') DESC, confidence DESC, id
                       ) AS rn
                FROM news_sentiment
            ) WHERE rn > 1
        )
    """)
    removed = before - conn.execute("SELECT COUNT(*) FROM news_sentiment").fetchone()[0]
    if removed:
        logger.info(f"DB migrate: deduped {removed} duplicate news rows")
    return removed


def _ensure_unique_news_index(conn: sqlite3.Connection):
    """去重后再建唯一索引，避免旧库重复数据导致启动失败。"""
    _dedupe_news(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_unique "
        "ON news_sentiment(code, date, title)"
    )


def _prepare_research_observation_events(conn: sqlite3.Connection):
    """为旧观察记录生成稳定事件键并去重。

    同一股票、同一形态、同一天重复生成报告，不能被当成多个独立样本。
    """
    # 旧索引存在时，重新标准化 event_key 可能让两条旧记录在 UPDATE
    # 阶段先发生碰撞，尚未执行后续去重就抛 UNIQUE。迁移必须先移除索引。
    conn.execute("DROP INDEX IF EXISTS idx_research_obs_event")
    conn.execute(
        """UPDATE research_observation_log
           SET event_key = UPPER(code) || '|' ||
                           COALESCE(NULLIF(pattern_type, ''), 'general') || '|' ||
                           SUBSTR(observed_at, 1, 10)
           WHERE event_key IS NULL OR event_key = ''"""
    )
    conn.execute(
        """DELETE FROM research_observation_log
           WHERE id IN (
               SELECT id FROM (
                   SELECT id,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_key
                              ORDER BY validated DESC, id ASC
                          ) AS rn
                   FROM research_observation_log
                   WHERE event_key != ''
               ) WHERE rn > 1
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_research_obs_event
           ON research_observation_log(event_key) WHERE event_key != ''"""
    )


def _prepare_prediction_events(conn: sqlite3.Connection):
    """同一参考日、策略、模式和动作的重复报告只保留一个样本。"""
    # event_key 的定义曾加入 signal_action。旧库可能同时保存旧键与新键，
    # 它们在标准化 UPDATE 后才变成重复；先移除唯一索引，再去重并重建。
    conn.execute("DROP INDEX IF EXISTS idx_prediction_event")
    conn.execute(
        """UPDATE prediction_log
           SET event_key = UPPER(code) || '|' ||
                           COALESCE(NULLIF(strategy_name, ''), 'overall') || '|' ||
                           COALESCE(NULLIF(mode, ''), 'eod') || '|' ||
                           COALESCE(NULLIF(signal_action, ''), 'overall') || '|' ||
                           COALESCE(NULLIF(reference_date, ''), SUBSTR(predict_time, 1, 10))
        """
    )
    conn.execute(
        """DELETE FROM prediction_log
           WHERE id IN (
               SELECT id FROM (
                   SELECT id,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_key
                              ORDER BY validated DESC, id ASC
                          ) AS rn
                   FROM prediction_log
                   WHERE event_key != ''
               ) WHERE rn > 1
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_event
           ON prediction_log(event_key) WHERE event_key != ''"""
    )


def _prepare_trade_plan_events(conn: sqlite3.Connection):
    """同一股票、模式、日期、策略和动作只保留一份正式方案。"""
    conn.execute("DROP INDEX IF EXISTS idx_trade_plan_event")
    conn.execute(
        """UPDATE trade_plan_log
           SET event_key = UPPER(code) || '|' ||
                           COALESCE(NULLIF(mode, ''), 'eod') || '|' ||
                           COALESCE(NULLIF(reference_date, ''), SUBSTR(created_at, 1, 10)) || '|' ||
                           COALESCE(NULLIF(strategy_key, ''), 'unknown') || '|' ||
                           COALESCE(NULLIF(signal_intent, ''), 'unknown') || '|' ||
                           COALESCE(NULLIF(action, ''), 'watch')
           WHERE event_key IS NULL OR event_key = ''"""
    )
    conn.execute(
        """DELETE FROM trade_plan_log
           WHERE id IN (
               SELECT id FROM (
                   SELECT id,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_key
                              ORDER BY CASE status WHEN 'evaluated' THEN 0 ELSE 1 END, id ASC
                          ) AS rn
                   FROM trade_plan_log WHERE event_key != ''
               ) WHERE rn > 1
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_plan_event
           ON trade_plan_log(event_key) WHERE event_key != ''"""
    )


class Database:
    """
    SQLite 数据库单例类。

    使用方式：
        db = Database.init()              # 初始化（使用默认路径）
        db = Database.init("/custom/path/tradehelper.db")  # 自定义路径
        db = Database()                   # 后续任意位置获取实例

    线程安全：使用 check_same_thread=False 允许多线程共享连接。
    WAL 模式：允许读写并发，提升性能。
    """

    _instance: Optional["Database"] = None  # 单例实例

    def __new__(cls):
        """确保全局只有一个 Database 实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conn = None
            cls._instance._local = threading.local()
            # 写操作锁：分析线程与 UI 线程共享同一连接时序列化写入，
            # 防止 "database is locked" 错误（WAL 仅保护读读并发）。
            cls._instance._write_lock = threading.Lock()
        return cls._instance

    @classmethod
    def init(cls, db_path: str | None = None) -> "Database":
        """
        初始化数据库连接并建表。

        Args:
            db_path: SQLite 文件路径，默认从 Settings 中读取

        Returns:
            Database 单例实例
        """
        instance = cls()
        if db_path is None:
            db_path = Settings().db_path
        instance._connect(db_path)
        return instance

    def _open(self, db_path: str) -> sqlite3.Connection:
        """
        打开一个新的 SQLite 连接并应用全部 PRAGMA / 建表 / schema 迁移。

        所有连接入口（init / 懒加载重连）都走这里，避免重连时丢失 PRAGMA。
        """
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")    # Write-Ahead Logging
        conn.execute("PRAGMA foreign_keys=ON")      # 启用外键约束
        conn.executescript(CREATE_TABLES_SQL)       # 自动建表
        # 老版本数据库 schema 迁移
        _ensure_column(conn, "reports", "chart_path", "TEXT", "''")
        _ensure_column(conn, "stocks", "listing_date", "TEXT", "''")
        _ensure_column(conn, "reports", "mode", "TEXT", "'eod'")
        _ensure_column(conn, "reports", "prediction_data", "TEXT", "''")
        _ensure_column(conn, "news_sentiment", "content", "TEXT", "''")
        _ensure_column(conn, "news_sentiment", "is_macro", "INTEGER", "0")
        _ensure_column(conn, "news_sentiment", "published_at", "TEXT", "''")
        _ensure_column(conn, "news_sentiment", "fetched_at", "TEXT", "''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_fetched_at "
            "ON news_sentiment(code, fetched_at)"
        )
        _ensure_column(conn, "prediction_log", "strategy_name", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "signal_action", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "market_regime", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "take_profit", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "entry_mode", "TEXT", "'reference'")
        _ensure_column(conn, "prediction_log", "reference_date", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "underlying_return", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "validation_price", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "actual_entry_price", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "actual_exit_type", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "actual_exit_date", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "max_favorable_excursion", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "max_adverse_excursion", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "validation_end_date", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "validation_status", "TEXT", "'pending'")
        # 迁移时旧记录必须保留 v1，防止错误验证结果继续训练风控官。
        _ensure_column(conn, "prediction_log", "validation_version", "INTEGER", "1")
        _ensure_column(conn, "prediction_log", "portfolio_snapshot", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "event_key", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "exit_review_status", "TEXT", "'not_applicable'")
        _ensure_column(conn, "prediction_log", "exit_return_1d", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_return_3d", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_return_5d", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_return_10d", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_return_20d", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_max_decline", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_max_rally", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_avoided_loss", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_opportunity_cost", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "exit_quality", "TEXT", "''")
        # v2 早期记录没有保存实际开盘入场价和退出类型。下一交易日开盘
        # 可以从正式K线可靠恢复；退出类型只在价位与止损/止盈一致时回填。
        conn.execute(
            """UPDATE prediction_log
               SET actual_entry_price=(
                   SELECT ph.open FROM price_history ph
                   WHERE ph.code=prediction_log.code
                     AND ph.date>COALESCE(NULLIF(prediction_log.reference_date, ''),
                                          SUBSTR(prediction_log.predict_time, 1, 10))
                   ORDER BY ph.date ASC LIMIT 1)
               WHERE validated=1 AND entry_mode='next_open'
                 AND COALESCE(actual_entry_price, 0)<=0"""
        )
        conn.execute(
            """UPDATE prediction_log
               SET actual_exit_type=CASE
                   WHEN entry_triggered=0 THEN 'not_triggered'
                   WHEN stop_loss>0 AND ABS(validation_price-stop_loss)
                        <= MAX(0.01, ABS(stop_loss)*0.001) THEN 'stop_loss'
                   WHEN take_profit>0 AND ABS(validation_price-take_profit)
                        <= MAX(0.01, ABS(take_profit)*0.001) THEN 'take_profit'
                   ELSE 'legacy_validation' END,
                   actual_exit_date=COALESCE(NULLIF(actual_exit_date, ''), validation_end_date)
               WHERE validated=1 AND COALESCE(actual_exit_type, '')=''"""
        )
        conn.execute(
            """UPDATE prediction_log
               SET signal_action = CASE
                   WHEN direction='bullish' THEN 'buy'
                   WHEN direction='bearish' THEN 'sell'
                   ELSE '' END
               WHERE strategy_name != '' AND (signal_action IS NULL OR signal_action='')"""
        )
        conn.execute(
            """UPDATE prediction_log SET exit_review_status='pending'
               WHERE signal_action='sell'
                 AND (exit_review_status IS NULL OR exit_review_status='not_applicable')"""
        )
        # 旧版会从自由文本中正则提取任意数字作为“入场价”，同时仍把
        # entry_mode 标为 reference。这类记录无法证明真实入场条件，必须
        # 保留审计痕迹但退出正确率、收益和策略健康度学习。
        conn.execute(
            """UPDATE prediction_log
               SET validated=-1,
                   validation_status='legacy_unverifiable',
                   exit_review_status=CASE
                       WHEN signal_action='sell' THEN 'unsupported'
                       ELSE exit_review_status END
               WHERE validated=1 AND entry_mode='reference'
                 AND (COALESCE(conservative_entry, 0)>0
                      OR COALESCE(aggressive_entry, 0)>0)"""
        )
        _prepare_prediction_events(conn)
        _ensure_column(conn, "research_observation_log", "event_key", "TEXT", "''")
        _ensure_column(conn, "research_observation_log", "trigger_operator", "TEXT", "''")
        _ensure_column(conn, "research_observation_log", "entry_triggered", "INTEGER", "0")
        _ensure_column(conn, "research_observation_log", "triggered_at", "TEXT", "''")
        # 旧记录没有触发语义，必须隔离，不能自动参与新版学习。
        _ensure_column(conn, "research_observation_log", "validation_status", "TEXT", "'unsupported'")
        _prepare_research_observation_events(conn)
        _ensure_column(conn, "forecast_model_versions", "stock_code", "TEXT", "'*'")
        _ensure_column(conn, "forecast_model_versions", "log_loss", "REAL", "0.0")
        _ensure_column(conn, "joint_oof_runs", "horizon_metrics_json", "TEXT", "'{}'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_model_stock_status "
            "ON forecast_model_versions(stock_code, market, horizon, status)"
        )
        _ensure_column(conn, "trade_plan_log", "event_key", "TEXT", "''")
        _ensure_column(conn, "trade_plan_log", "reference_date", "TEXT", "''")
        _ensure_column(conn, "trade_plan_log", "decision_session_date", "TEXT", "''")
        _ensure_column(conn, "trade_plan_log", "signal_timestamp_ms", "INTEGER", "0")
        _ensure_column(conn, "trade_plan_log", "evidence_sources", "TEXT", "''")
        _ensure_column(conn, "trade_plan_log", "evidence_quality", "TEXT", "''")
        _ensure_column(conn, "trade_plan_log", "evidence_bar_count", "INTEGER", "0")
        conn.execute(
            """UPDATE trade_plan_log
               SET decision_session_date = COALESCE(
                   NULLIF(decision_session_date, ''),
                   NULLIF(reference_date, ''),
                   SUBSTR(created_at, 1, 10)
               )
               WHERE decision_session_date IS NULL OR decision_session_date = ''"""
        )
        _prepare_trade_plan_events(conn)
        _ensure_column(conn, "strategy_param_candidates", "avg_oos_excess_return", "REAL", "0.0")
        _ensure_column(conn, "strategy_param_candidates", "positive_excess_windows", "INTEGER", "0")
        _ensure_column(conn, "strategy_param_candidates", "risk_adjusted_windows", "INTEGER", "0")
        _ensure_column(conn, "strategy_param_candidates", "qualified_windows", "INTEGER", "0")
        _ensure_column(conn, "strategy_param_candidates", "promotion_path", "TEXT", "''")
        _ensure_column(conn, "strategy_param_candidates", "first_eligible_data_end", "TEXT", "''")
        # 索引必须在列迁移完成后创建（旧库没有 strategy_name 列，不能放在 DDL 里）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prediction_strategy "
            "ON prediction_log(code, strategy_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prediction_regime "
            "ON prediction_log(code, market_regime)"
        )
        _ensure_unique_news_index(conn)
        conn.commit()
        return conn

    def _connect(self, db_path: str):
        """对外建立连接入口（首次 init 时调用）。"""
        self._conn = self._open(db_path)
        self._db_path = db_path
        self._local.conn = self._conn
        self._local.db_path = db_path
        try:
            self.cleanup_stale_cache()
        except Exception:
            pass  # 表可能还没建（首次启动），忽略
        logger.info(f"Database connected: {db_path}")

    @property
    def conn(self) -> sqlite3.Connection:
        """获取当前线程连接；schema 迁移只由主初始化连接执行。"""
        db_path = getattr(self, "_db_path", None) or Settings().db_path
        thread_conn = getattr(self._local, "conn", None)
        if thread_conn is not None and getattr(self._local, "db_path", None) == db_path:
            return thread_conn
        if thread_conn is not None:
            try:
                thread_conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

        if self._conn is None:
            # 主连接被显式关闭后的兼容重连仍需检查 schema。
            self._conn = self._open(db_path)
            self._db_path = db_path
            self._local.conn = self._conn
            self._local.db_path = db_path
            logger.info(f"Database reconnected: {db_path}")
            return self._conn

        # sqlite3 连接对象不能被多个线程同时调用。WAL 支持多连接并发，
        # 因此工作线程使用独立连接，避免 InterfaceError/API misuse。
        thread_conn = sqlite3.connect(db_path, check_same_thread=False)
        thread_conn.row_factory = sqlite3.Row
        thread_conn.execute("PRAGMA foreign_keys=ON")
        thread_conn.execute("PRAGMA busy_timeout=5000")
        self._local.conn = thread_conn
        self._local.db_path = db_path
        return thread_conn

    def execute(self, sql: str, params=None):
        """执行 SQL 语句的快捷方法（仅适用于读；写请走 _execute_write）。"""
        return self.conn.execute(sql, params or ())

    def _execute_write(self, sql: str, params=None):
        """串行化执行单条写 SQL，并在同一锁内提交。"""
        with self._write_lock:
            cursor = self.conn.execute(sql, params or ())
            self.conn.commit()
            return cursor

    def _executemany_write(self, sql: str, seq_of_params):
        """串行化执行批量写 SQL。"""
        with self._write_lock:
            self.conn.executemany(sql, seq_of_params)
            self.conn.commit()


    # ======================== 股票信息 ========================

    def upsert_stock(self, stock: StockInfo):
        """
        插入或更新股票信息（使用 INSERT OR REPLACE）。

        Args:
            stock: StockInfo 实例
        """
        sql = """INSERT OR REPLACE INTO stocks
                 (code, name, market, industry, description, listing_date, update_time)
                 VALUES (?, ?, ?, ?, ?, ?, ?)"""
        self._execute_write(sql, (stock.code, stock.name, stock.market,
                                  stock.industry, stock.description,
                                  stock.listing_date,
                                  stock.update_time))

    def get_stock(self, code: str) -> StockInfo | None:
        """读取股票元数据，包括用于历史窗口裁剪的上市日期。"""
        row = self.execute(
            "SELECT * FROM stocks WHERE code = ?", (code.upper(),)
        ).fetchone()
        return StockInfo.from_dict(dict(row)) if row else None


    # ======================== 股价历史 ========================

    def insert_prices(self, prices: list[PriceData]):
        """
        批量插入股价数据（使用 INSERT OR REPLACE 避免重复）。

        Args:
            prices: PriceData 列表
        """
        if not prices:
            return
        sql = """INSERT OR REPLACE INTO price_history (code, date, open, high, low, close, volume)
                 VALUES (?, ?, ?, ?, ?, ?, ?)"""
        data = [(p.code, p.date, p.open, p.high, p.low, p.close, p.volume)
                for p in prices]
        self._executemany_write(sql, data)

    def get_prices(self, code: str, start_date: str = "", end_date: str = "") -> list[PriceData]:
        """
        按代码和日期范围查询股价数据。

        Args:
            code: 股票代码
            start_date: 起始日期（可选，含）
            end_date: 结束日期（可选，含）

        Returns:
            按日期升序排列的 PriceData 列表
        """
        conditions = ["code = ?"]
        params = [code]
        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)
        sql = f"SELECT * FROM price_history WHERE {' AND '.join(conditions)} ORDER BY date ASC"
        rows = self.execute(sql, params).fetchall()
        return [PriceData.from_dict(dict(r)) for r in rows]

    def clear_price_history(self, code: str) -> int:
        """删除单只股票整段缓存；用于发现污染后由可信数据源完整重建。"""
        cursor = self._execute_write(
            "DELETE FROM price_history WHERE code=?", (code.upper(),)
        )
        return int(cursor.rowcount or 0)

    # ======================== 分钟K前瞻证据 ========================

    def insert_intraday_bars(self, bars: list[IntradayBar]) -> int:
        """Insert validated minute bars without touching daily price history."""
        valid = []
        for bar in bars or []:
            try:
                values = [float(bar.open), float(bar.high), float(bar.low), float(bar.close)]
                if (
                    int(bar.timestamp_ms) <= 0 or min(values) <= 0
                    or values[1] < max(values[0], values[2], values[3])
                    or values[2] > min(values[0], values[1], values[3])
                ):
                    continue
                valid.append((
                    bar.code.upper(), bar.market, int(bar.timestamp_ms),
                    bar.session_date, values[0], values[1], values[2], values[3],
                    max(float(bar.volume or 0.0), 0.0), bar.source,
                    bar.fetched_at or datetime.now().isoformat(),
                    bar.quality_status or "supplemental",
                ))
            except (TypeError, ValueError):
                continue
        if not valid:
            return 0
        self._executemany_write(
            """INSERT INTO intraday_price_history
               (code, market, timestamp_ms, session_date, open, high, low, close,
                volume, source, fetched_at, quality_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, timestamp_ms) DO UPDATE SET
                 market=excluded.market,
                 session_date=excluded.session_date,
                 open=excluded.open,
                 high=excluded.high,
                 low=excluded.low,
                 close=excluded.close,
                 volume=excluded.volume,
                 source=excluded.source,
                 fetched_at=excluded.fetched_at,
                 quality_status=excluded.quality_status
               WHERE intraday_price_history.quality_status!='provider'
                  OR excluded.quality_status='provider'""",
            valid,
        )
        return len(valid)

    def get_intraday_bars(
        self, code: str, *, start_ms: int = 0, end_ms: int = 0,
    ) -> list[IntradayBar]:
        clauses = ["code=?"]
        params: list = [code.upper()]
        if start_ms:
            clauses.append("timestamp_ms>=?")
            params.append(int(start_ms))
        if end_ms:
            clauses.append("timestamp_ms<=?")
            params.append(int(end_ms))
        rows = self.execute(
            "SELECT * FROM intraday_price_history WHERE "
            + " AND ".join(clauses) + " ORDER BY timestamp_ms",
            tuple(params),
        ).fetchall()
        return [IntradayBar(**dict(row)) for row in rows]

    def get_intraday_coverage(self, code: str) -> dict:
        row = self.execute(
            """SELECT COUNT(*) count, COUNT(DISTINCT session_date) sessions,
                      MIN(timestamp_ms) first_ms, MAX(timestamp_ms) last_ms,
                      GROUP_CONCAT(DISTINCT source) sources
               FROM intraday_price_history WHERE code=?""",
            (code.upper(),),
        ).fetchone()
        return dict(row) if row else {
            "count": 0, "sessions": 0, "first_ms": 0, "last_ms": 0, "sources": "",
        }

    def get_intraday_evidence_overview(self, market: str) -> list[dict]:
        bars = self.execute(
            """SELECT code, COUNT(*) bar_count, COUNT(DISTINCT session_date) sessions,
                      MIN(session_date) first_session, MAX(session_date) last_session,
                      GROUP_CONCAT(DISTINCT source) sources
               FROM intraday_price_history WHERE market=? GROUP BY code""",
            (market,),
        ).fetchall()
        plans = self.execute(
            """SELECT code,
                      SUM(CASE WHEN status='pending_intraday' THEN 1 ELSE 0 END) pending,
                      SUM(CASE WHEN status='evaluated' THEN 1 ELSE 0 END) evaluated
               FROM trade_plan_log WHERE market=? AND mode='intraday' GROUP BY code""",
            (market,),
        ).fetchall()
        result = {row["code"]: dict(row) for row in bars}
        for row in plans:
            item = result.setdefault(row["code"], {
                "code": row["code"], "bar_count": 0, "sessions": 0,
                "first_session": "", "last_session": "", "sources": "",
            })
            item.update({"pending": int(row["pending"] or 0), "evaluated": int(row["evaluated"] or 0)})
        for item in result.values():
            item.setdefault("pending", 0)
            item.setdefault("evaluated", 0)
        return sorted(result.values(), key=lambda item: (-int(item["bar_count"]), item["code"]))


    # ======================== 分析报告 ========================

    def insert_report(self, report: AnalysisReport) -> int:
        """
        插入一条分析报告记录。

        Args:
            report: AnalysisReport 实例

        Returns:
            新记录的 ID（数据库自增主键）
        """
        sql = """INSERT INTO reports
                 (code, name, market, backtest_period, create_time, content,
                  chart_path, pdf_path, rating, rated_at, mode, prediction_data)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        cursor = self._execute_write(sql, (
            report.code, report.name, report.market, report.backtest_period,
            report.create_time, report.content,
            report.chart_path or "", report.pdf_path or "",
            report.rating, report.rated_at or "",
            report.mode or "eod", report.prediction_data or ""
        ))
        return cursor.lastrowid

    def update_report_content(self, report_id: int, content: str):
        """更新已落库报告正文，用于追加依赖本次写入结果的追踪章节。"""
        self._execute_write(
            "UPDATE reports SET content = ? WHERE id = ?",
            (content, int(report_id)),
        )


    def get_all_reports(self, limit: int = 50) -> list[AnalysisReport]:
        """
        获取所有报告（按时间倒序）。

        Args:
            limit: 最大返回数量

        Returns:
            AnalysisReport 列表
        """
        rows = self.execute(
            "SELECT * FROM reports ORDER BY create_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [AnalysisReport.from_dict(dict(r)) for r in rows]

    def get_reports_by_code(self, code: str, mode: str | None = None,
                            since_hours: int | None = None) -> list[AnalysisReport]:
        """
        按股票代码筛选报告。

        Args:
            code: 股票代码
            mode: 可选，过滤分析模式（"eod"/"intraday"/"pre"）
            since_hours: 可选，仅返回最近 N 小时内的报告

        Returns:
            该股票的报告列表（时间倒序）
        """
        conditions = ["code = ?"]
        params: list = [code]

        if mode:
            conditions.append("mode = ?")
            params.append(mode)

        if since_hours is not None:
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat()
            conditions.append("create_time >= ?")
            params.append(cutoff)

        where = " AND ".join(conditions)
        rows = self.execute(
            f"SELECT * FROM reports WHERE {where} ORDER BY create_time DESC", tuple(params)
        ).fetchall()
        return [AnalysisReport.from_dict(dict(r)) for r in rows]

    def update_report_rating(self, report_id: int, rating: int):
        """
        更新报告的用户评分。

        评分数据存储后可用于：
          - 分析用户偏好的策略类型
          - 为后续策略参数优化提供反馈信号

        Args:
            report_id: 报告 ID
            rating: 评分（1-5）
        """
        now = datetime.now().isoformat()
        self._execute_write(
            "UPDATE reports SET rating = ?, rated_at = ? WHERE id = ?",
            (rating, now, report_id)
        )

    def update_report_pdf(self, report_id: int, pdf_path: str):
        """
        更新报告的 PDF 文件路径（PDF 导出后调用）。

        Args:
            report_id: 报告 ID
            pdf_path: PDF 文件的绝对路径
        """
        self._execute_write(
            "UPDATE reports SET pdf_path = ? WHERE id = ?",
            (pdf_path, report_id)
        )

    def delete_report(self, report_id: int):
        """删除指定报告（注意：不会删除磁盘上的 PDF 文件）。"""
        self._execute_write("DELETE FROM reports WHERE id = ?", (report_id,))

    def filter_reports(
        self,
        code: str = "",
        market: str = "",
        mode: str = "",
        period: str = "",
        min_rating: int | None = None,
        limit: int = 100,
    ) -> list[AnalysisReport]:
        """按多条件筛选报告。"""
        conditions = []
        params: list = []
        if code:
            conditions.append("code LIKE ?")
            params.append(f"%{code.upper()}%")
        if market:
            conditions.append("market = ?")
            params.append(market)
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if period:
            conditions.append("backtest_period = ?")
            params.append(period)
        if min_rating is not None:
            conditions.append("rating >= ?")
            params.append(min_rating)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = self.execute(
            f"SELECT * FROM reports {where} ORDER BY create_time DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [AnalysisReport.from_dict(dict(r)) for r in rows]

    # ======================== 我的持仓 ========================

    def list_holdings(self, market: str = "") -> list[Holding]:
        """列出持仓，可按市场筛选。"""
        if market:
            rows = self.execute(
                "SELECT * FROM holdings WHERE market = ? ORDER BY code ASC", (market,),
            ).fetchall()
        else:
            rows = self.execute("SELECT * FROM holdings ORDER BY market ASC, code ASC").fetchall()
        return [Holding.from_dict(dict(r)) for r in rows]

    def upsert_holding(self, holding: Holding):
        """插入或更新持仓。"""
        self._execute_write(
            """INSERT INTO holdings (code, name, market, shares, cost_price)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, market=excluded.market,
                 shares=excluded.shares, cost_price=excluded.cost_price""",
            (
                holding.code.upper(), holding.name, holding.market,
                holding.shares, holding.cost_price,
            ),
        )

    def delete_holding(self, holding_id: int):
        self._execute_write("DELETE FROM holdings WHERE id = ?", (holding_id,))

    def list_watchlist(self, market: str = "") -> list[WatchItem]:
        """列出关注股票，可按市场筛选。"""
        if market:
            rows = self.execute(
                "SELECT * FROM watchlist WHERE market = ? ORDER BY code ASC", (market,),
            ).fetchall()
        else:
            rows = self.execute("SELECT * FROM watchlist ORDER BY market ASC, code ASC").fetchall()
        return [WatchItem.from_dict(dict(r)) for r in rows]

    def upsert_watch_item(self, item: WatchItem):
        """插入或更新关注股票。"""
        self._execute_write(
            """INSERT INTO watchlist (code, name, market)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, market=excluded.market""",
            (item.code.upper(), item.name, item.market),
        )

    def delete_watch_item(self, item_id: int):
        self._execute_write("DELETE FROM watchlist WHERE id = ?", (item_id,))

    def get_balance(self) -> AccountBalance:
        """获取账户余额（单条记录）。"""
        row = self.execute("SELECT * FROM account_balance WHERE id = 1").fetchone()
        if row:
            return AccountBalance.from_dict(dict(row))
        return AccountBalance(id=1, us_balance=0.0, a_balance=0.0)

    def save_balance(self, balance: AccountBalance):
        """保存账户余额。"""
        self._execute_write(
            """INSERT OR REPLACE INTO account_balance (id, us_balance, a_balance)
               VALUES (1, ?, ?)""",
            (balance.us_balance, balance.a_balance),
        )


    def insert_news(self, news_list: list[NewsItem]):
        """
        批量 upsert 新闻（按 code+date+title 去重，更新情感标签与正文）。

        Args:
            news_list: NewsItem 列表
        """
        if not news_list:
            return
        sql = """INSERT INTO news_sentiment
                 (code, date, title, source, content, sentiment, confidence,
                  is_macro, published_at, fetched_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(code, date, title) DO UPDATE SET
                   source=excluded.source,
                   content=CASE WHEN excluded.content != '' THEN excluded.content ELSE news_sentiment.content END,
                   sentiment=CASE WHEN excluded.sentiment != '' THEN excluded.sentiment ELSE news_sentiment.sentiment END,
                   confidence=CASE WHEN excluded.sentiment != '' THEN excluded.confidence ELSE news_sentiment.confidence END,
                   is_macro=excluded.is_macro,
                   published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE news_sentiment.published_at END,
                   fetched_at=CASE WHEN excluded.fetched_at != '' THEN excluded.fetched_at ELSE news_sentiment.fetched_at END"""
        data = [(n.code, n.date, n.title, n.source, n.content or "",
                 n.sentiment, n.confidence, 1 if getattr(n, 'is_macro', False) else 0,
                 getattr(n, "published_at", "") or "",
                 getattr(n, "fetched_at", "") or "")
                for n in news_list]
        self._executemany_write(sql, data)

    def get_news(self, code: str, limit: int = 20) -> list[NewsItem]:
        """
        获取某股票最近的新闻情感记录。

        Args:
            code: 股票代码
            limit: 最大返回条数

        Returns:
            NewsItem 列表（按日期倒序）
        """
        rows = self.execute(
            "SELECT * FROM news_sentiment WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, limit)
        ).fetchall()
        return [NewsItem.from_dict(dict(r)) for r in rows]

    def get_recent_news_with_sentiment(
        self, code: str, hours: int = 24, limit: int = 50
    ) -> list[NewsItem]:
        """
        获取指定时间窗口内、且已经完成情感分析（sentiment 非空）的新闻。

        用于新闻 24 小时复用：避免每次分析都重新拉取并跑 FinBERT。

        Args:
            code: 股票代码
            hours: 时间窗口（小时），默认 24h
            limit: 最大返回条数

        Returns:
            NewsItem 列表（按日期倒序）；窗口内无缓存时返回空列表
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.execute(
            """SELECT * FROM news_sentiment
               WHERE code = ? AND fetched_at >= ? AND sentiment != ''
               ORDER BY published_at DESC, date DESC LIMIT ?""",
            (code, cutoff, limit)
        ).fetchall()
        return [NewsItem.from_dict(dict(r)) for r in rows]

    def get_news_refresh_state(self, code: str) -> dict | None:
        row = self.execute(
            "SELECT * FROM news_refresh_state WHERE code = ?", (code.upper(),)
        ).fetchone()
        return dict(row) if row else None

    def save_news_refresh_state(
        self,
        code: str,
        market: str,
        *,
        attempted_at: str,
        status: str,
        item_count: int,
    ) -> None:
        success_at = attempted_at if status in ("success", "empty") else ""
        self._execute_write(
            """INSERT INTO news_refresh_state
               (code, market, last_attempt_at, last_success_at, status, item_count)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 market=excluded.market,
                 last_attempt_at=excluded.last_attempt_at,
                 last_success_at=CASE
                   WHEN excluded.last_success_at != '' THEN excluded.last_success_at
                   ELSE news_refresh_state.last_success_at END,
                 status=excluded.status,
                 item_count=excluded.item_count""",
            (
                code.upper(), market, attempted_at, success_at,
                status, int(item_count),
            ),
        )

    # ═══════════════════════════════════════════════════════════════
    # 独立预测与交易方案双闭环
    # ═══════════════════════════════════════════════════════════════

    def insert_feature_context_snapshot(
        self, snapshot: FeatureContextSnapshot,
    ) -> int:
        """Freeze one point-in-time context without rewriting earlier snapshots."""
        data = snapshot.to_dict()
        data.pop("id", None)
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        cursor = self._execute_write(
            f"INSERT OR IGNORE INTO feature_context_snapshots ({columns}) "
            f"VALUES ({placeholders})",
            tuple(data.values()),
        )
        if cursor.rowcount:
            snapshot.id = int(cursor.lastrowid or 0)
            return snapshot.id
        row = self.execute(
            "SELECT id FROM feature_context_snapshots WHERE event_key=?",
            (snapshot.event_key,),
        ).fetchone()
        snapshot.id = int(row["id"] or 0) if row else 0
        return snapshot.id

    def get_feature_context_snapshots(
        self, code: str, *, before_at: str = "", limit: int = 100,
    ) -> list[FeatureContextSnapshot]:
        clauses = ["code=?"]
        params: list = [code.upper()]
        if before_at:
            clauses.append("captured_at<=?")
            params.append(before_at)
        params.append(max(int(limit), 1))
        rows = self.execute(
            "SELECT * FROM feature_context_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [FeatureContextSnapshot(**dict(row)) for row in rows]

    def get_feature_context_overview(self, market: str) -> list[dict]:
        rows = self.execute(
            """SELECT code, COUNT(*) snapshot_count,
                      SUM(CASE WHEN news_count>0 THEN 1 ELSE 0 END) news_snapshots,
                      SUM(CASE WHEN fundamental_source NOT IN ('', 'default') THEN 1 ELSE 0 END) fundamental_snapshots,
                      MIN(captured_at) first_captured_at,
                      MAX(captured_at) last_captured_at,
                      GROUP_CONCAT(DISTINCT fundamental_source) fundamental_sources
               FROM feature_context_snapshots WHERE market=?
               GROUP BY code ORDER BY snapshot_count DESC, code""",
            (market,),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_forecast(self, forecast: ForecastResult) -> int:
        """冻结一条预测；重复事件返回原记录，不覆盖预测字段。"""
        probabilities = [
            float(forecast.prob_up),
            float(forecast.prob_flat),
            float(forecast.prob_down),
        ]
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities):
            raise ValueError("预测概率必须是 0 到 1 的有限数")
        if abs(sum(probabilities) - 1.0) > 1e-6:
            raise ValueError("上涨、震荡、下跌概率之和必须等于 1")
        if not forecast.event_key:
            raise ValueError("预测必须包含稳定 event_key")
        if not forecast.target_session_date or not forecast.data_cutoff:
            raise ValueError("预测必须包含数据截止日和目标交易日")

        data = forecast.to_dict()
        data.pop("id", None)
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        cursor = self._execute_write(
            f"INSERT OR IGNORE INTO forecast_log ({columns}) VALUES ({placeholders})",
            tuple(data.values()),
        )
        if cursor.rowcount:
            forecast.id = int(cursor.lastrowid or 0)
            return forecast.id
        row = self.execute(
            "SELECT id FROM forecast_log WHERE event_key=?", (forecast.event_key,)
        ).fetchone()
        forecast.id = int(row["id"] or 0) if row else 0
        return forecast.id

    def insert_forecasts(self, forecasts: list[ForecastResult]) -> list[int]:
        return [self.insert_forecast(item) for item in forecasts]

    def get_forecasts(
        self,
        *,
        code: str = "",
        market: str = "",
        status: str = "",
        horizon: int = 0,
        limit: int = 100,
    ) -> list[ForecastResult]:
        clauses, params = [], []
        if code:
            clauses.append("code=?")
            params.append(code.upper())
        if market:
            clauses.append("market=?")
            params.append(market)
        if status:
            clauses.append("status=?")
            params.append(status)
        if horizon:
            clauses.append("horizon=?")
            params.append(int(horizon))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.execute(
            f"SELECT * FROM forecast_log {where} ORDER BY generated_at DESC, horizon ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ForecastResult.from_dict(dict(row)) for row in rows]

    def verify_due_forecasts(
        self,
        *,
        code: str = "",
        as_of_date: str = "",
    ) -> list[ForecastResult]:
        """用目标交易日正式收盘价验证预测，缺失时保持 pending。"""
        cutoff = (as_of_date or datetime.now().date().isoformat())[:10]
        params: list = [cutoff]
        code_filter = ""
        if code:
            code_filter = " AND code=?"
            params.append(code.upper())
        impossible = self.execute(
            "SELECT id, market, generated_at, data_cutoff, target_session_date "
            "FROM forecast_log "
            "WHERE status IN ('pending', 'verified')",
        ).fetchall()
        quarantined = 0
        for item in impossible:
            generated_date = _generated_market_date(
                item["generated_at"], item["market"],
            )
            target_date = str(item["target_session_date"] or "")
            stale_origin = False
            generated_dt = _generated_market_datetime(
                item["generated_at"], item["market"],
            )
            data_cutoff = str(item["data_cutoff"] or "")
            if generated_dt and data_cutoff:
                try:
                    from utils.trading_calendar import latest_completed_session
                    stale_origin = data_cutoff < latest_completed_session(
                        item["market"], as_of=generated_dt,
                    )
                except Exception:
                    stale_origin = False
            if (
                (generated_date and target_date and target_date < generated_date)
                or stale_origin
            ):
                self._execute_write(
                    """UPDATE forecast_log SET status='unsupported', verified_at=?
                       WHERE id=? AND status IN ('pending', 'verified')""",
                    (datetime.now().isoformat(), item["id"]),
                )
                quarantined += 1
        if quarantined:
            logger.warning(
                "已隔离 %d 条目标日期倒置或数据截止日过期的预测记录，不参与评估",
                quarantined,
            )

        rows = self.execute(
            "SELECT * FROM forecast_log WHERE status='pending' "
            "AND target_session_date<=?" + code_filter + " ORDER BY target_session_date, id",
            tuple(params),
        ).fetchall()
        verified: list[ForecastResult] = []
        now = datetime.now().isoformat()
        for row in rows:
            forecast = ForecastResult.from_dict(dict(row))
            price_row = self.execute(
                "SELECT close FROM price_history WHERE code=? AND date=?",
                (forecast.code, forecast.target_session_date),
            ).fetchone()
            if not price_row or forecast.reference_price <= 0:
                continue
            actual_price = float(price_row["close"] or 0.0)
            if actual_price <= 0:
                continue
            actual_return = actual_price / forecast.reference_price - 1.0
            actual_direction = (
                "bullish" if actual_return > 0.01
                else "bearish" if actual_return < -0.01
                else "neutral"
            )
            outcomes = {
                "bullish": 1.0 if actual_direction == "bullish" else 0.0,
                "neutral": 1.0 if actual_direction == "neutral" else 0.0,
                "bearish": 1.0 if actual_direction == "bearish" else 0.0,
            }
            brier = (
                (forecast.prob_up - outcomes["bullish"]) ** 2
                + (forecast.prob_flat - outcomes["neutral"]) ** 2
                + (forecast.prob_down - outcomes["bearish"]) ** 2
            )
            interval_hit = int(
                forecast.expected_return_p10 <= actual_return
                <= forecast.expected_return_p90
            )
            self._execute_write(
                """UPDATE forecast_log SET status='verified', actual_price=?,
                   actual_return=?, actual_direction=?, correct=?, brier_score=?,
                   interval_hit=?, verified_at=? WHERE id=? AND status='pending'""",
                (
                    actual_price, actual_return, actual_direction,
                    int(forecast.direction == actual_direction), brier,
                    interval_hit, now, forecast.id,
                ),
            )
            refreshed = self.execute(
                "SELECT * FROM forecast_log WHERE id=?", (forecast.id,)
            ).fetchone()
            if refreshed:
                verified.append(ForecastResult.from_dict(dict(refreshed)))
        return verified

    def get_forecast_metrics(
        self,
        *,
        market: str = "",
        code: str = "",
        horizon: int = 0,
        model_version: str = "",
    ) -> dict:
        clauses = ["status='verified'"]
        params: list = []
        for column, value in (
            ("market", market), ("code", code.upper() if code else ""),
            ("model_version", model_version),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if horizon:
            clauses.append("horizon=?")
            params.append(int(horizon))
        rows = self.execute(
            "SELECT correct, brier_score, interval_hit, actual_direction, "
            "prob_up, prob_flat, prob_down, market_regime "
            f"FROM forecast_log WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
        if not rows:
            return {
                "samples": 0, "accuracy": 0.0, "brier_score": 0.0,
                "baseline_brier": 0.0, "log_loss": 0.0, "ece": 0.0,
                "interval_coverage": 0.0, "calibration_bins": [],
                "regime_metrics": {},
            }
        count = len(rows)
        direction_counts = {
            key: sum(1 for row in rows if row["actual_direction"] == key)
            for key in ("bullish", "neutral", "bearish")
        }
        frequencies = [direction_counts[key] / count for key in direction_counts]
        baseline_brier = 1.0 - sum(value * value for value in frequencies)
        from core.forecast_engine import multiclass_log_loss, probability_diagnostics

        direction_index = {"bullish": 0, "neutral": 1, "bearish": 2}
        probabilities = [
            [float(row["prob_up"]), float(row["prob_flat"]), float(row["prob_down"])]
            for row in rows
        ]
        actual_indices = [direction_index.get(str(row["actual_direction"]), 1) for row in rows]
        diagnostics = probability_diagnostics(
            probabilities, actual_indices,
            regimes=[str(row["market_regime"] or "unknown") for row in rows],
        )
        return {
            "samples": count,
            "accuracy": sum(int(row["correct"] or 0) for row in rows) / count,
            "brier_score": sum(float(row["brier_score"] or 0.0) for row in rows) / count,
            "baseline_brier": baseline_brier,
            "log_loss": sum(
                multiclass_log_loss(probability, actual)
                for probability, actual in zip(probabilities, actual_indices)
            ) / count,
            "ece": diagnostics["ece"],
            "interval_coverage": sum(int(row["interval_hit"] or 0) for row in rows) / count,
            "calibration_bins": diagnostics["calibration_bins"],
            "regime_metrics": diagnostics["regime_metrics"],
        }

    def save_forecast_model_version(self, version: ForecastModelVersion) -> int:
        data = version.to_dict()
        data.pop("id", None)
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        updates = ", ".join(
            f"{key}=excluded.{key}" for key in data if key not in ("market", "horizon", "version")
        )
        cursor = self._execute_write(
            f"INSERT INTO forecast_model_versions ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(market, horizon, version) DO UPDATE SET {updates}",
            tuple(data.values()),
        )
        row = self.execute(
            "SELECT id FROM forecast_model_versions WHERE market=? AND horizon=? AND version=?",
            (version.market, version.horizon, version.version),
        ).fetchone()
        return int(row["id"] or cursor.lastrowid or 0) if row else 0

    def get_forecast_champion(
        self, market: str, horizon: int, stock_code: str = "",
    ) -> ForecastModelVersion | None:
        scope = stock_code.upper() if stock_code else "*"
        row = self.execute(
            """SELECT * FROM forecast_model_versions
               WHERE market=? AND horizon=? AND status='champion'
                 AND stock_code IN (?, '*')
               ORDER BY CASE WHEN stock_code=? THEN 0 ELSE 1 END,
                        promoted_at DESC, id DESC LIMIT 1""",
            (market, int(horizon), scope, scope),
        ).fetchone()
        if not row:
            return None
        values = dict(row)
        return ForecastModelVersion(**{
            key: values.get(key) for key in ForecastModelVersion.__dataclass_fields__
        })

    def promote_forecast_model(
        self, market: str, horizon: int, version: str, stock_code: str = "",
    ) -> None:
        """原子晋升 Challenger，并保留旧 Champion 供审计与回滚。"""
        now = datetime.now().isoformat()
        scope = stock_code.upper() if stock_code else "*"
        with self._write_lock:
            conn = self.conn
            row = conn.execute(
                """SELECT id FROM forecast_model_versions
                   WHERE market=? AND horizon=? AND version=? AND stock_code=?""",
                (market, int(horizon), version, scope),
            ).fetchone()
            if not row:
                raise ValueError(f"预测模型候选不存在: {market}/{horizon}/{version}")
            conn.execute(
                """UPDATE forecast_model_versions SET status='retired'
                   WHERE market=? AND horizon=? AND stock_code=?
                     AND status='champion' AND version!=?""",
                (market, int(horizon), scope, version),
            )
            conn.execute(
                """UPDATE forecast_model_versions SET status='champion', promoted_at=?
                   WHERE market=? AND horizon=? AND version=? AND stock_code=?""",
                (now, market, int(horizon), version, scope),
            )
            conn.commit()

    def rollback_forecast_model(
        self, market: str, horizon: int, version: str, stock_code: str, reason: str,
    ) -> None:
        """在线概率表现显著退化时退出执行链；保留记录供审计。"""
        self._execute_write(
            """UPDATE forecast_model_versions
               SET status='rolled_back', reason=?
               WHERE market=? AND horizon=? AND version=? AND stock_code=?
                 AND status='champion'""",
            (reason, market, int(horizon), version, stock_code.upper()),
        )

    def save_joint_oof_run(self, run: JointOOFRun | dict) -> int:
        """Persist one combined-policy OOF audit, replacing the same cutoff run."""
        values = run.to_dict() if hasattr(run, "to_dict") else dict(run)
        values.pop("id", None)
        for source, target, default in (
            ("horizon_metrics", "horizon_metrics_json", {}),
            ("calibration_bins", "calibration_json", []),
            ("regime_metrics", "regime_metrics_json", {}),
            ("fold_summaries", "fold_summaries_json", []),
            ("trace", "trace_json", []),
        ):
            if source in values:
                values[target] = json.dumps(values.pop(source), ensure_ascii=False)
            elif not isinstance(values.get(target), str):
                values[target] = json.dumps(values.get(target, default), ensure_ascii=False)
        values["code"] = str(values.get("code") or "").upper()
        values["created_at"] = values.get("created_at") or datetime.now().isoformat()
        columns = ", ".join(values.keys())
        placeholders = ", ".join(["?"] * len(values))
        updates = ", ".join(
            f"{key}=excluded.{key}"
            for key in values if key not in ("code", "data_end", "policy_version")
        )
        self._execute_write(
            f"INSERT INTO joint_oof_runs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(code, data_end, policy_version) DO UPDATE SET {updates}",
            tuple(values.values()),
        )
        row = self.execute(
            "SELECT id FROM joint_oof_runs WHERE code=? AND data_end=? AND policy_version=?",
            (values["code"], values["data_end"], values["policy_version"]),
        ).fetchone()
        return int(row["id"] or 0) if row else 0

    def has_joint_oof_run(
        self, code: str, data_end: str, policy_version: str = "",
    ) -> bool:
        sql = "SELECT 1 FROM joint_oof_runs WHERE code=? AND data_end=?"
        params: list = [code.upper(), data_end]
        if policy_version:
            sql += " AND policy_version=?"
            params.append(policy_version)
        row = self.execute(sql + " LIMIT 1", tuple(params)).fetchone()
        return bool(row)

    def get_joint_oof_runs(
        self, *, market: str = "", code: str = "", policy_version: str = "",
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if market:
            clauses.append("market=?")
            params.append(market)
        if code:
            clauses.append("code=?")
            params.append(code.upper())
        if policy_version:
            clauses.append("policy_version=?")
            params.append(policy_version)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.execute(
            "SELECT * FROM joint_oof_runs" + where
            + " ORDER BY data_end DESC, id DESC LIMIT ?",
            tuple(params + [max(int(limit), 1)]),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key, default in (
                ("horizon_metrics_json", {}),
                ("calibration_json", []), ("regime_metrics_json", {}),
                ("fold_summaries_json", []), ("trace_json", []),
            ):
                try:
                    item[key[:-5] if key.endswith("_json") else key] = json.loads(
                        item.get(key) or json.dumps(default)
                    )
                except (TypeError, ValueError):
                    item[key[:-5] if key.endswith("_json") else key] = default
            result.append(item)
        return result

    def get_joint_oof_health(self, code: str) -> dict:
        """Return latest combined-policy audit plus conservative drift signals."""
        latest_rows = self.get_joint_oof_runs(code=code, limit=1)
        if not latest_rows:
            return {}
        latest = dict(latest_rows[0])
        runs = self.get_joint_oof_runs(
            code=code,
            policy_version=str(latest.get("policy_version") or ""),
            limit=2,
        )
        reasons = []
        status = "stable"
        previous = runs[1] if len(runs) > 1 else None
        if previous:
            excess_delta = float(latest.get("excess_return", 0) or 0) - float(
                previous.get("excess_return", 0) or 0
            )
            brier_delta = float(latest.get("forecast_brier", 0) or 0) - float(
                previous.get("forecast_brier", 0) or 0
            )
            latest["excess_return_delta"] = excess_delta
            latest["forecast_brier_delta"] = brier_delta
            if excess_delta <= -0.05:
                reasons.append(f"超额收益较上次下降{abs(excess_delta):.1%}")
            previous_brier = float(previous.get("forecast_brier", 0) or 0)
            if previous_brier > 0 and brier_delta >= max(0.03, previous_brier * 0.10):
                reasons.append(f"预测Brier较上次恶化{brier_delta:+.3f}")
        trades = int(latest.get("total_trades", 0) or 0)
        if (
            trades >= 8
            and float(latest.get("total_return", 0) or 0) <= -0.03
            and float(latest.get("excess_return", 0) or 0) <= -0.03
        ):
            status = "critical"
            reasons.append("最新联合OOF超额收益为负")
        elif reasons:
            status = "warning"
        latest["drift_status"] = status
        latest["drift_reasons"] = reasons
        return latest

    def insert_trade_plan(self, plan: TradePlanLog) -> int:
        data = plan.to_dict()
        data.pop("id", None)
        data["decision_session_date"] = str(
            data.get("decision_session_date")
            or data.get("reference_date")
            or data.get("created_at")
            or ""
        )[:10]
        data["event_key"] = data.get("event_key") or _trade_plan_event_key(data)
        if isinstance(data.get("account_snapshot_json"), dict):
            data["account_snapshot_json"] = json.dumps(
                data["account_snapshot_json"], ensure_ascii=False, sort_keys=True
            )
        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        cursor = self._execute_write(
            f"INSERT OR IGNORE INTO trade_plan_log ({columns}) VALUES ({placeholders})",
            tuple(data.values()),
        )
        if cursor.rowcount:
            plan.id = int(cursor.lastrowid or 0)
            return plan.id
        row = self.execute(
            "SELECT id FROM trade_plan_log WHERE event_key=?", (data["event_key"],)
        ).fetchone()
        plan.id = int(row["id"] or 0) if row else 0
        return plan.id

    def verify_due_trade_plans(self, *, code: str = "", window: int = 5) -> int:
        """分别复盘交易方案；只评价当时 A/B 级可执行动作。"""
        params: list = []
        code_filter = ""
        if code:
            code_filter = " AND code=?"
            params.append(code.upper())
        rows = self.execute(
            "SELECT * FROM trade_plan_log WHERE status='pending'" + code_filter,
            tuple(params),
        ).fetchall()
        updated = 0
        now = datetime.now().isoformat()
        for row in rows:
            plan = dict(row)
            origin = str(plan.get("reference_date") or plan.get("created_at") or "")[:10]
            bars = self.execute(
                """SELECT date, open, high, low, close FROM price_history
                   WHERE code=? AND date>? ORDER BY date ASC LIMIT ?""",
                (plan["code"], origin, max(1, int(window))),
            ).fetchall()
            if len(bars) < max(1, int(window)):
                continue
            entry = float(bars[0]["open"] or 0.0)
            if entry <= 0:
                continue
            action = str(plan.get("action") or "")
            stop = float(plan.get("stop_loss") or 0.0)
            take_profit = float(plan.get("take_profit") or 0.0)
            exit_price = float(bars[-1]["close"] or 0.0)
            outcome = "window_close"
            if action == "buy":
                exit_index = len(bars) - 1
                is_a_share = str(plan.get("market") or "").upper() == "A"
                for index, bar in enumerate(bars):
                    open_price = float(bar["open"] or 0.0)
                    low = float(bar["low"] or 0.0)
                    high = float(bar["high"] or 0.0)
                    # A股新建仓当日不可卖出；当日止损只能记录风险，次日起才能成交。
                    if is_a_share and index == 0:
                        continue
                    # 同一根 K 线同时触及时按止损优先，避免乐观路径假设。
                    if stop > 0 and (open_price <= stop or low <= stop):
                        exit_price = open_price if open_price <= stop else stop
                        outcome = "stop_loss"
                        exit_index = index
                        break
                    if take_profit > entry and high >= take_profit:
                        exit_price = take_profit
                        outcome = "take_profit"
                        exit_index = index
                        break
                active_bars = bars[:exit_index + 1]
                raw_path = [float(bar["high"]) / entry - 1.0 for bar in active_bars]
                adverse_path = [float(bar["low"]) / entry - 1.0 for bar in active_bars]
                gross_return = exit_price / entry - 1.0
                mfe, mae = max(raw_path), min(adverse_path)
                opportunity_cost = 0.0
            elif action == "sell":
                final_return = float(bars[-1]["close"]) / entry - 1.0
                gross_return = -final_return
                mfe = max(entry / float(bar["low"]) - 1.0 for bar in bars if float(bar["low"]) > 0)
                mae = min(entry / float(bar["high"]) - 1.0 for bar in bars if float(bar["high"]) > 0)
                opportunity_cost = max(final_return, 0.0)
                outcome = "effective_exit" if final_return < 0 else "premature_exit"
                exit_price = float(bars[-1]["close"])
            else:
                continue

            from utils.market_rules import get_market_rules
            rules = get_market_rules(plan["market"], code=plan["code"])
            try:
                snapshot = json.loads(plan.get("account_snapshot_json") or "{}")
            except (TypeError, ValueError):
                snapshot = {}
            if action == "buy":
                position_value = (
                    float(snapshot.get("account_equity", 0.0) or 0.0)
                    * float(plan.get("position_pct", 0.0) or 0.0)
                )
                if position_value > 0:
                    from utils.market_rules import estimate_round_trip_cost
                    estimated_cost = estimate_round_trip_cost(
                        position_value, plan["market"]
                    ) / position_value
                else:
                    estimated_cost = rules.round_trip_cost_pct
            else:
                position_value = (
                    float(snapshot.get("shares", 0.0) or 0.0) * entry
                )
                if position_value > 0:
                    one_side_amount = (
                        position_value * float(rules.slippage)
                        + max(position_value * float(rules.commission), float(rules.min_commission))
                        + position_value * float(rules.sell_tax)
                    )
                    estimated_cost = one_side_amount / position_value
                else:
                    estimated_cost = (
                        float(rules.slippage)
                        + float(rules.commission)
                        + float(rules.sell_tax)
                    )
            net_return = gross_return - estimated_cost
            self._execute_write(
                """UPDATE trade_plan_log SET status='evaluated', entry_price=?,
                   exit_price=?, net_return=?, max_favorable_excursion=?,
                   max_adverse_excursion=?, opportunity_cost=?, outcome=?,
                   evidence_sources='daily_price_history',
                   evidence_quality='provider', evidence_bar_count=?,
                   evaluated_at=? WHERE id=? AND status='pending'""",
                (
                    entry, exit_price, net_return, mfe, mae,
                    opportunity_cost, outcome, len(bars), now, plan["id"],
                ),
            )
            updated += 1
        return updated

    def verify_due_intraday_trade_plans(self, *, code: str = "") -> int:
        """Verify prospective intraday plans only from timestamped minute bars."""
        clauses = ["status='pending_intraday'"]
        params: list = []
        if code:
            clauses.append("code=?")
            params.append(code.upper())
        plans = self.execute(
            "SELECT * FROM trade_plan_log WHERE " + " AND ".join(clauses)
            + " ORDER BY signal_timestamp_ms, id",
            tuple(params),
        ).fetchall()
        updated = 0
        for raw_plan in plans:
            plan = dict(raw_plan)
            signal_ms = int(plan.get("signal_timestamp_ms") or 0)
            if signal_ms <= 0:
                continue
            bars = [dict(row) for row in self.execute(
                """SELECT * FROM intraday_price_history
                   WHERE code=? AND timestamp_ms>? ORDER BY timestamp_ms LIMIT 10000""",
                (plan["code"], signal_ms),
            ).fetchall()]
            if not bars:
                continue
            entry_bar = bars[0]
            entry_session = str(entry_bar["session_date"])
            action = str(plan.get("action") or "")
            market = str(plan.get("market") or "").upper()
            session_dates = list(dict.fromkeys(str(bar["session_date"]) for bar in bars))
            if action == "buy" and market == "A":
                # A股可在信号后立即买入，但当日买入的仓位次日才能卖出。
                # 因此保留买入日路径，直到下一交易日收盘才完成复盘。
                target_session = next(
                    (value for value in session_dates if value > entry_session), ""
                )
                if not target_session:
                    continue
                active_bars = [
                    bar for bar in bars
                    if str(bar["session_date"]) in (entry_session, target_session)
                ]
            else:
                target_session = entry_session
                active_bars = [
                    bar for bar in bars if str(bar["session_date"]) == target_session
                ]
            if not active_bars or not _minute_session_is_complete(
                market, target_session, active_bars[-1]["timestamp_ms"], session_dates,
            ):
                continue

            entry = float(entry_bar["open"] or 0.0)
            if entry <= 0:
                continue
            stop = float(plan.get("stop_loss") or 0.0)
            take_profit = float(plan.get("take_profit") or 0.0)
            exit_price = float(active_bars[-1]["close"] or 0.0)
            outcome = "session_close"
            if action == "buy":
                exit_index = len(active_bars) - 1
                for index, bar in enumerate(active_bars):
                    if market == "A" and str(bar["session_date"]) == entry_session:
                        continue
                    open_price = float(bar["open"] or 0.0)
                    low = float(bar["low"] or 0.0)
                    high = float(bar["high"] or 0.0)
                    if stop > 0 and (open_price <= stop or low <= stop):
                        exit_price = open_price if open_price <= stop else stop
                        outcome = "stop_loss"
                        exit_index = index
                        break
                    if take_profit > entry and high >= take_profit:
                        exit_price = take_profit
                        outcome = "take_profit"
                        exit_index = index
                        break
                path = active_bars[:exit_index + 1]
                evidence_bars = path
                gross_return = exit_price / entry - 1.0
                mfe = max(float(bar["high"]) / entry - 1.0 for bar in path)
                mae = min(float(bar["low"]) / entry - 1.0 for bar in path)
                opportunity_cost = 0.0
            elif action == "sell":
                evidence_bars = active_bars
                final_return = exit_price / entry - 1.0
                gross_return = -final_return
                mfe = max(
                    entry / float(bar["low"]) - 1.0
                    for bar in active_bars if float(bar["low"]) > 0
                )
                mae = min(
                    entry / float(bar["high"]) - 1.0
                    for bar in active_bars if float(bar["high"]) > 0
                )
                opportunity_cost = max(final_return, 0.0)
                outcome = "effective_exit" if final_return < 0 else "premature_exit"
            else:
                continue

            from utils.market_rules import estimate_round_trip_cost, get_market_rules

            rules = get_market_rules(market, code=plan["code"])
            try:
                snapshot = json.loads(plan.get("account_snapshot_json") or "{}")
            except (TypeError, ValueError):
                snapshot = {}
            if action == "buy":
                position_value = (
                    float(snapshot.get("account_equity", 0.0) or 0.0)
                    * float(plan.get("position_pct", 0.0) or 0.0)
                )
                estimated_cost = (
                    estimate_round_trip_cost(position_value, market) / position_value
                    if position_value > 0 else float(rules.round_trip_cost_pct)
                )
            else:
                position_value = float(snapshot.get("shares", 0.0) or 0.0) * entry
                if position_value > 0:
                    one_side_amount = (
                        position_value * float(rules.slippage)
                        + max(
                            position_value * float(rules.commission),
                            float(rules.min_commission),
                        )
                        + position_value * float(rules.sell_tax)
                    )
                    estimated_cost = one_side_amount / position_value
                else:
                    estimated_cost = (
                        float(rules.slippage)
                        + float(rules.commission)
                        + float(rules.sell_tax)
                    )
            evidence_sources = ",".join(sorted({
                str(bar.get("source") or "unknown") for bar in evidence_bars
            }))
            evidence_qualities = {
                str(bar.get("quality_status") or "supplemental")
                for bar in evidence_bars
            }
            evidence_quality = (
                "provider" if evidence_qualities == {"provider"}
                else "supplemental" if evidence_qualities == {"supplemental"}
                else "mixed"
            )
            self._execute_write(
                """UPDATE trade_plan_log SET status='evaluated', entry_price=?,
                   exit_price=?, net_return=?, max_favorable_excursion=?,
                   max_adverse_excursion=?, opportunity_cost=?, outcome=?,
                   evidence_sources=?, evidence_quality=?, evidence_bar_count=?,
                   evaluated_at=? WHERE id=? AND status='pending_intraday'""",
                (
                    entry, exit_price, gross_return - estimated_cost, mfe, mae,
                    opportunity_cost, outcome, evidence_sources, evidence_quality,
                    len(evidence_bars), datetime.now().isoformat(), plan["id"],
                ),
            )
            updated += 1
        return updated

    def get_trade_plan_metrics(self, *, market: str = "", code: str = "") -> list[dict]:
        clauses = ["status='evaluated'"]
        params: list = []
        if market:
            clauses.append("market=?")
            params.append(market)
        if code:
            clauses.append("code=?")
            params.append(code.upper())
        rows = self.execute(
            "SELECT * FROM trade_plan_log WHERE " + " AND ".join(clauses),
            tuple(params),
        ).fetchall()
        grouped: dict[tuple[str, str, str, str], list[dict]] = {}
        for raw in rows:
            row = dict(raw)
            key = (
                str(row.get("code") or ""), str(row.get("strategy_key") or ""),
                str(row.get("signal_intent") or ""), str(row.get("action") or ""),
            )
            grouped.setdefault(key, []).append(row)
        result = []
        for (symbol, strategy, intent, action), samples in grouped.items():
            by_day: dict[str, list[dict]] = {}
            for sample in samples:
                session = str(
                    sample.get("decision_session_date")
                    or sample.get("reference_date")
                    or sample.get("created_at")
                    or ""
                )[:10]
                by_day.setdefault(session, []).append(sample)
            daily = []
            for session_samples in by_day.values():
                weights = [{
                    "provider": 1.0, "supplemental": 0.60, "mixed": 0.75,
                }.get(str(item.get("evidence_quality") or ""), 1.0) for item in session_samples]
                denominator = sum(weights) or 1.0
                daily_item = {
                    field: sum(
                        float(item.get(field) or 0.0) * weight
                        for item, weight in zip(session_samples, weights)
                    ) / denominator
                    for field in (
                        "net_return", "max_favorable_excursion",
                        "max_adverse_excursion", "opportunity_cost",
                    )
                }
                daily_item["weight"] = max(weights) if weights else 1.0
                daily.append(daily_item)
            count = len(samples)
            independent_days = len(daily)
            effective_days = sum(float(item["weight"]) for item in daily)
            sources = sorted({
                source.strip()
                for item in samples
                for source in str(item.get("evidence_sources") or "").split(",")
                if source.strip()
            })
            result.append({
                "code": symbol, "strategy_key": strategy,
                "signal_intent": intent, "action": action,
                "count": count, "independent_days": independent_days,
                "effective_days": round(effective_days, 2),
                "avg_net_return": (
                    sum(item["net_return"] * item["weight"] for item in daily)
                    / effective_days if effective_days > 0 else 0.0
                ),
                "positive_rate": (
                    sum(item["weight"] for item in daily if item["net_return"] > 0)
                    / effective_days if effective_days > 0 else 0.0
                ),
                "avg_mfe": (
                    sum(item["max_favorable_excursion"] * item["weight"] for item in daily)
                    / effective_days if effective_days > 0 else 0.0
                ),
                "avg_mae": (
                    sum(item["max_adverse_excursion"] * item["weight"] for item in daily)
                    / effective_days if effective_days > 0 else 0.0
                ),
                "avg_opportunity_cost": (
                    sum(item["opportunity_cost"] * item["weight"] for item in daily)
                    / effective_days if effective_days > 0 else 0.0
                ),
                "intraday_count": sum(str(item.get("mode")) == "intraday" for item in samples),
                "provider_evidence_count": sum(str(item.get("evidence_quality")) == "provider" for item in samples),
                "supplemental_evidence_count": sum(
                    str(item.get("evidence_quality")) in ("supplemental", "mixed")
                    for item in samples
                ),
                "evidence_sources": ",".join(sources),
            })
        return sorted(
            result,
            key=lambda item: (-int(item["independent_days"]), -float(item["avg_net_return"])),
        )

    # ═══════════════════════════════════════════════════════════════
    # 预测追踪 (prediction_log) CRUD
    # ═══════════════════════════════════════════════════════════════

    def insert_prediction(self, pred: PredictionLog) -> int:
        d = pred.to_dict()
        d.pop("id", None)
        if d.get("validated") == 1 and d.get("validation_status") == "pending":
            d["validation_status"] = "verified"
        reference_day = str(
            d.get("reference_date") or d.get("predict_time") or ""
        )[:10]
        d["event_key"] = d.get("event_key") or (
            f"{str(d.get('code') or '').upper()}|"
            f"{d.get('strategy_name') or 'overall'}|{d.get('mode') or 'eod'}|"
            f"{d.get('signal_action') or 'overall'}|"
            f"{reference_day}"
        )
        # 防护：旧数据库还没有 strategy_name 列时，移除该字段
        cols = {r[1] for r in self.execute("PRAGMA table_info(prediction_log)").fetchall()}
        if "strategy_name" not in cols:
            d.pop("strategy_name", None)
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        cursor = self._execute_write(
            f"INSERT OR IGNORE INTO prediction_log ({columns}) VALUES ({placeholders})",
            tuple(d.values()),
        )
        if cursor.rowcount:
            return cursor.lastrowid or 0
        row = self.execute(
            "SELECT id FROM prediction_log WHERE event_key=?", (d["event_key"],)
        ).fetchone()
        return int(row["id"] or 0) if row else 0

    def delete_prediction(self, pred_id: int):
        """删除一条预测记录。"""
        self._execute_write("DELETE FROM prediction_log WHERE id = ?", (pred_id,))

    def clear_predictions(self):
        """清空预测追踪表（调试用）。"""
        self._execute_write("DELETE FROM prediction_log")

    def _future_price_bars(self, code: str, predict_date: str, days: int) -> list[dict]:
        rows = self.execute(
            """SELECT date, open, high, low, close FROM price_history
               WHERE code = ? AND date > ?
               ORDER BY date ASC LIMIT ?""",
            (code, predict_date, max(int(days), 1)),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _directional_return(direction: str, raw_return: float) -> float:
        if direction == "bullish":
            return raw_return
        if direction == "bearish":
            return -raw_return
        return 0.0

    def _recover_legacy_reference_date(self, pred: PredictionLog) -> str:
        """用旧记录的预测价反查正式 K 线；不能唯一可靠匹配时放弃。"""
        if pred.code.startswith("PORTFOLIO_") or pred.predicted_price <= 0:
            return ""
        rows = self.execute(
            """SELECT date, close FROM price_history
               WHERE code=? AND date <= ?
               ORDER BY date DESC LIMIT 10""",
            (pred.code, pred.predict_time[:10]),
        ).fetchall()
        matches = []
        for row in rows:
            close = float(row["close"] or 0.0)
            relative_error = abs(close - pred.predicted_price) / pred.predicted_price
            if relative_error <= 0.001:
                matches.append(str(row["date"])[:10])
        return matches[0] if len(matches) == 1 else ""

    def _verify_portfolio_prediction(self, pred: PredictionLog) -> bool:
        """按持仓快照的第 N 个交易日净值验证组合预测。"""
        import json

        try:
            snapshot = json.loads(pred.portfolio_snapshot or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        holdings = snapshot.get("holdings") or []
        cash = float(snapshot.get("cash", 0.0) or 0.0)
        start_value = float(pred.predicted_price or snapshot.get("equity", 0.0) or 0.0)
        if start_value <= 0 or not holdings:
            pred.validation_status = "unsupported"
            pred.validation_version = 2
            pred.validated = -1
            return True

        end_value = cash
        end_dates = []
        for item in holdings:
            code = str(item.get("code") or "")
            shares = float(item.get("shares", 0.0) or 0.0)
            cutoff = pred.reference_date or pred.predict_time[:10]
            bars = self._future_price_bars(code, cutoff, pred.verify_after_days)
            if shares <= 0 or len(bars) < max(int(pred.verify_after_days), 1):
                return False
            end_bar = bars[-1]
            end_value += shares * float(end_bar.get("close", 0.0) or 0.0)
            end_dates.append(str(end_bar.get("date", ""))[:10])

        raw_return = (end_value - start_value) / start_value
        pred.underlying_return = raw_return
        pred.actual_return = self._directional_return(pred.direction, raw_return)
        pred.validation_price = end_value
        pred.actual_exit_type = "portfolio_window"
        pred.actual_exit_date = max(end_dates) if end_dates else ""
        pred.actual_direction = (
            "bullish" if raw_return > 0.01 else "bearish" if raw_return < -0.01 else "neutral"
        )
        pred.entry_triggered = 1
        pred.validation_end_date = max(end_dates) if end_dates else ""
        pred.validation_status = "verified"
        pred.validation_version = 2
        pred.validated = 1
        return True

    def _verify_stock_prediction(self, pred: PredictionLog) -> bool:
        """按第 N 个交易日和真实触发路径验证单股建议。"""
        bars = self._future_price_bars(
            pred.code, pred.reference_date or pred.predict_time[:10], pred.verify_after_days
        )
        required = max(int(pred.verify_after_days), 1)
        if len(bars) < required:
            return False

        reference = float(pred.predicted_price or 0.0)
        if reference <= 0:
            pred.validation_status = "unsupported"
            pred.validation_version = 2
            pred.validated = -1
            return True

        end_bar = bars[-1]
        end_close = float(end_bar.get("close", 0.0) or 0.0)
        raw_return = (end_close - reference) / reference
        pred.underlying_return = raw_return
        pred.actual_direction = (
            "bullish" if raw_return > 0.01 else "bearish" if raw_return < -0.01 else "neutral"
        )
        pred.validation_end_date = str(end_bar.get("date", ""))[:10]

        direction = pred.direction
        if direction not in ("bullish", "bearish"):
            pred.actual_return = 0.0
            pred.validation_price = end_close
            pred.actual_entry_price = reference
            pred.actual_exit_type = "window_close"
            pred.actual_exit_date = pred.validation_end_date
            pred.entry_triggered = 1
            pred.validation_status = "verified"
            pred.validation_version = 2
            pred.validated = 1
            return True

        requested_entry = float(pred.conservative_entry or pred.aggressive_entry or 0.0)
        trigger_index = 0
        entry_price = reference
        if pred.entry_mode == "next_open":
            entry_price = float(bars[0].get("open", reference) or reference)
        elif pred.entry_mode == "signal_price":
            entry_price = reference
        elif requested_entry > 0:
            trigger_index = -1
            for i, bar in enumerate(bars):
                low = float(bar.get("low", 0.0) or 0.0)
                high = float(bar.get("high", 0.0) or 0.0)
                triggered = low <= requested_entry if direction == "bullish" else high >= requested_entry
                if triggered:
                    trigger_index = i
                    bar_open = float(bar.get("open", requested_entry) or requested_entry)
                    entry_price = (
                        min(requested_entry, bar_open)
                        if direction == "bullish"
                        else max(requested_entry, bar_open)
                    )
                    break
            if trigger_index < 0:
                pred.entry_triggered = 0
                pred.actual_return = 0.0
                pred.validation_price = end_close
                pred.actual_exit_type = "not_triggered"
                pred.actual_exit_date = pred.validation_end_date
                pred.validation_status = "not_triggered"
                pred.validation_version = 2
                pred.validated = 1
                return True

        pred.entry_triggered = 1
        pred.actual_entry_price = entry_price
        exit_price = end_close
        exit_type = "window_close"
        exit_date = pred.validation_end_date
        stop = float(pred.stop_loss or 0.0)
        take_profit = float(pred.take_profit or 0.0)
        favorable = []
        adverse = []
        for bar in bars[trigger_index:]:
            bar_open = float(bar.get("open", entry_price) or entry_price)
            high = float(bar.get("high", entry_price) or entry_price)
            low = float(bar.get("low", entry_price) or entry_price)
            if direction == "bullish":
                favorable.append((high - entry_price) / entry_price)
                adverse.append((low - entry_price) / entry_price)
                if stop > 0 and low <= stop:
                    exit_price = min(stop, bar_open)
                    exit_type = "stop_loss"
                    exit_date = str(bar.get("date", ""))[:10]
                    break
                if take_profit > 0 and high >= take_profit:
                    exit_price = max(take_profit, bar_open)
                    exit_type = "take_profit"
                    exit_date = str(bar.get("date", ""))[:10]
                    break
            else:
                favorable.append((entry_price - low) / entry_price)
                adverse.append((entry_price - high) / entry_price)
                if stop > 0 and high >= stop:
                    exit_price = max(stop, bar_open)
                    exit_type = "stop_loss"
                    exit_date = str(bar.get("date", ""))[:10]
                    break
                if take_profit > 0 and low <= take_profit:
                    exit_price = min(take_profit, bar_open)
                    exit_type = "take_profit"
                    exit_date = str(bar.get("date", ""))[:10]
                    break

        gross_return = (
            (exit_price - entry_price) / entry_price
            if direction == "bullish"
            else (entry_price - exit_price) / entry_price
        )
        from utils.market_rules import get_market_rules
        pred.actual_return = gross_return - get_market_rules(pred.market).round_trip_cost_pct
        pred.validation_price = exit_price
        pred.actual_exit_type = exit_type
        pred.actual_exit_date = exit_date
        pred.max_favorable_excursion = max(favorable, default=0.0)
        pred.max_adverse_excursion = min(adverse, default=0.0)
        pred.validation_status = "verified"
        pred.validation_version = 2
        pred.validated = 1
        return True

    def _save_prediction_validation(self, pred: PredictionLog):
        self._execute_write(
            """UPDATE prediction_log SET validated=?, reference_date=?, actual_return=?, underlying_return=?,
               validation_price=?, actual_entry_price=?, actual_exit_type=?, actual_exit_date=?,
               max_favorable_excursion=?, max_adverse_excursion=?,
               actual_direction=?, entry_triggered=?, verified_at=?, validation_end_date=?,
               validation_status=?, validation_version=? WHERE id=?""",
            (
                pred.validated, pred.reference_date, pred.actual_return, pred.underlying_return,
                pred.validation_price, pred.actual_entry_price,
                pred.actual_exit_type, pred.actual_exit_date,
                pred.max_favorable_excursion,
                pred.max_adverse_excursion, pred.actual_direction,
                pred.entry_triggered, pred.verified_at, pred.validation_end_date,
                pred.validation_status, pred.validation_version, pred.id,
            ),
        )

    def _verify_exit_review(self, pred: PredictionLog) -> bool:
        """对真实 sell/减仓信号做 1/3/5/10/20 日退出质量复盘。"""
        if pred.signal_action != "sell":
            return False
        bars = self._future_price_bars(
            pred.code, pred.reference_date or pred.predict_time[:10], 20
        )
        if len(bars) < 20:
            return False

        reference = float(pred.predicted_price or 0.0)
        if reference <= 0:
            pred.exit_review_status = "unsupported"
            self._save_exit_review(pred)
            return True
        execution_price = (
            float(bars[0].get("open", reference) or reference)
            if pred.entry_mode == "next_open" else reference
        )
        if execution_price <= 0:
            pred.exit_review_status = "unsupported"
            self._save_exit_review(pred)
            return True

        def _forward_return(days: int) -> float:
            close = float(bars[days - 1].get("close", execution_price) or execution_price)
            return (close - execution_price) / execution_price

        pred.exit_return_1d = _forward_return(1)
        pred.exit_return_3d = _forward_return(3)
        pred.exit_return_5d = _forward_return(5)
        pred.exit_return_10d = _forward_return(10)
        pred.exit_return_20d = _forward_return(20)
        lows = [float(b.get("low", execution_price) or execution_price) for b in bars]
        highs = [float(b.get("high", execution_price) or execution_price) for b in bars]
        pred.exit_max_decline = min((value - execution_price) / execution_price for value in lows)
        pred.exit_max_rally = max((value - execution_price) / execution_price for value in highs)

        from utils.market_rules import get_market_rules
        rules = get_market_rules(pred.market)
        one_way_exit_cost = rules.slippage + rules.commission + rules.sell_tax
        pred.exit_avoided_loss = max(-pred.exit_return_20d - one_way_exit_cost, 0.0)
        pred.exit_opportunity_cost = max(pred.exit_return_20d + one_way_exit_cost, 0.0)
        if pred.exit_avoided_loss >= 0.02 or pred.exit_max_decline <= -0.05:
            pred.exit_quality = "effective"
        elif pred.exit_opportunity_cost >= 0.03 or pred.exit_max_rally >= 0.05:
            pred.exit_quality = "premature"
        else:
            pred.exit_quality = "neutral"
        pred.exit_review_status = "verified"
        self._save_exit_review(pred)
        return True

    def _save_exit_review(self, pred: PredictionLog):
        self._execute_write(
            """UPDATE prediction_log SET exit_review_status=?, exit_return_1d=?,
               exit_return_3d=?, exit_return_5d=?, exit_return_10d=?, exit_return_20d=?,
               exit_max_decline=?, exit_max_rally=?, exit_avoided_loss=?,
               exit_opportunity_cost=?, exit_quality=? WHERE id=?""",
            (
                pred.exit_review_status, pred.exit_return_1d, pred.exit_return_3d,
                pred.exit_return_5d, pred.exit_return_10d, pred.exit_return_20d,
                pred.exit_max_decline, pred.exit_max_rally, pred.exit_avoided_loss,
                pred.exit_opportunity_cost, pred.exit_quality, pred.id,
            ),
        )

    def batch_verify_expired(self) -> int:
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE validated = 0 OR validation_version < 2
                  OR (signal_action='sell' AND exit_review_status='pending')"""
        ).fetchall()
        if not rows:
            return 0
        verified_count = 0
        for row in rows:
            pred = PredictionLog.from_dict(dict(row))
            updated = False
            if pred.validation_version < 2 and not pred.reference_date:
                pred.reference_date = self._recover_legacy_reference_date(pred)
                if not pred.reference_date:
                    pred.validation_status = "legacy_unverifiable"
                    pred.validation_version = 2
                    pred.validated = -1
                    pred.verified_at = datetime.now().isoformat()
                    self._save_prediction_validation(pred)
                    verified_count += 1
                    continue
            if pred.validated == 0 or pred.validation_version < 2:
                is_portfolio = pred.code.startswith("PORTFOLIO_")
                completed = (
                    self._verify_portfolio_prediction(pred)
                    if is_portfolio else self._verify_stock_prediction(pred)
                )
                if completed:
                    pred.verified_at = datetime.now().isoformat()
                    self._save_prediction_validation(pred)
                    updated = True
            if pred.exit_review_status == "pending" and not pred.code.startswith("PORTFOLIO_"):
                updated = self._verify_exit_review(pred) or updated
            if updated:
                verified_count += 1
        if verified_count > 0:
            logger.info(f"prediction_log 批量验证：{verified_count} 条")
        return verified_count

    def get_prediction_stats(self, code: str, limit: int = 10) -> "PredictionStats":
        from data.models import PredictionStats
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'
               ORDER BY predict_time DESC""",
            (code,),
        ).fetchall()
        stats = PredictionStats(code=code, strategy_sample_count=len(rows))
        if not rows:
            return stats
        unique_rows = []
        seen_events = set()
        for row in rows:
            event = (
                str(row["reference_date"] or row["predict_time"] or "")[:10],
                str(row["direction"] or ""),
                str(row["signal_action"] or ""),
            )
            if event in seen_events:
                continue
            seen_events.add(event)
            unique_rows.append(row)
        recent = unique_rows[:max(int(limit), 1)]

        def _accuracy(items) -> float:
            actionable = [
                row for row in items
                if row["direction"] in ("bullish", "bearish") and row["actual_direction"]
            ]
            correct = sum(
                1 for row in actionable
                if row["direction"] == row["actual_direction"]
            )
            return correct / len(actionable) if actionable else 0.0

        stats.total_predictions = len(unique_rows)
        stats.direction_accuracy_10 = _accuracy(recent)
        stats.direction_accuracy_all = _accuracy(unique_rows)
        returns = [float(row["actual_return"] or 0.0) for row in recent]
        stats.avg_predicted_return = sum(returns) / len(returns) if returns else 0.0
        if len(recent) >= 10:
            recent_5 = recent[:5]
            older_5 = recent[5:10]
            recent_acc = sum(
                1 for r in recent_5
                if r["direction"] and r["actual_direction"]
                and r["direction"] == r["actual_direction"]
            ) / 5
            older_acc = sum(
                1 for r in older_5
                if r["direction"] and r["actual_direction"]
                and r["direction"] == r["actual_direction"]
            ) / 5
            if recent_acc > older_acc + 0.1:
                stats.accuracy_trend = "improving"
            elif recent_acc < older_acc - 0.1:
                stats.accuracy_trend = "declining"
            else:
                stats.accuracy_trend = "stable"
        if stats.direction_accuracy_10 >= 0.60:
            stats.status = "reliable"
        elif stats.direction_accuracy_10 >= 0.45:
            stats.status = "unstable"
        else:
            stats.status = "unreliable"
        from datetime import datetime as _dt
        stats.updated_at = _dt.now().isoformat()
        return stats

    def get_prediction_stats_for_codes(
        self,
        codes: list[str],
        *,
        mode: str = "",
        limit: int = 10,
    ) -> "PredictionStats":
        """按一组组合成分股汇总真实预测统计。"""
        from data.models import PredictionStats

        normalized = list(dict.fromkeys(str(code).upper() for code in codes if code))
        stats = PredictionStats(code="PORTFOLIO_COMPONENTS")
        if not normalized:
            return stats
        placeholders = ",".join("?" for _ in normalized)
        mode_sql = " AND mode = ?" if mode else ""
        params: list = [*normalized]
        if mode:
            params.append(mode)
        rows = self.execute(
            f"""SELECT * FROM prediction_log
                WHERE code IN ({placeholders}) AND validated = 1
                  AND validation_version >= 2 AND validation_status = 'verified'
                  {mode_sql}
                ORDER BY predict_time DESC""",
            tuple(params),
        ).fetchall()
        stats.strategy_sample_count = len(rows)
        if not rows:
            return stats

        # 组合报告中的“正确率”按独立市场事件计算。同一股票、同一依据日、
        # 同一方向/动作即使被多个策略确认，也只能算一次机会；策略健康度
        # 仍在其他查询中保留逐策略样本，不受这里去重影响。
        unique_rows = []
        seen_events = set()
        for row in rows:
            event = (
                str(row["code"] or ""),
                str(row["mode"] or ""),
                str(row["reference_date"] or row["predict_time"] or "")[:10],
                str(row["direction"] or ""),
                str(row["signal_action"] or ""),
            )
            if event in seen_events:
                continue
            seen_events.add(event)
            unique_rows.append(row)
        stats.total_predictions = len(unique_rows)

        def _accuracy(items) -> float:
            actionable = [
                row for row in items
                if row["direction"] in ("bullish", "bearish")
                and row["actual_direction"]
            ]
            correct = sum(
                1 for row in actionable
                if row["direction"] == row["actual_direction"]
            )
            return correct / len(actionable) if actionable else 0.0

        recent = unique_rows[:max(int(limit), 1)]
        stats.direction_accuracy_10 = _accuracy(recent)
        stats.direction_accuracy_all = _accuracy(unique_rows)
        returns = [float(row["actual_return"] or 0.0) for row in recent]
        stats.avg_predicted_return = sum(returns) / len(returns) if returns else 0.0
        if len(recent) >= 10:
            recent_5 = _accuracy(recent[:5])
            older_5 = _accuracy(recent[5:10])
            stats.accuracy_trend = (
                "improving" if recent_5 > older_5 + 0.1
                else "declining" if recent_5 < older_5 - 0.1
                else "stable"
            )
        stats.status = (
            "reliable" if stats.direction_accuracy_10 >= 0.60
            else "unstable" if stats.direction_accuracy_10 >= 0.45
            else "unreliable"
        )
        stats.updated_at = datetime.now().isoformat()
        return stats

    def count_unverified_predictions_for_codes(
        self,
        codes: list[str],
        *,
        mode: str = "",
    ) -> int:
        normalized = list(dict.fromkeys(str(code).upper() for code in codes if code))
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        mode_sql = " AND mode = ?" if mode else ""
        params: list = [*normalized]
        if mode:
            params.append(mode)
        rows = self.execute(
            f"""SELECT code, mode, reference_date, predict_time, direction, signal_action
                FROM prediction_log
                WHERE code IN ({placeholders}) AND validated = 0 {mode_sql}""",
            tuple(params),
        ).fetchall()
        events = {
            (
                str(row["code"] or ""),
                str(row["mode"] or ""),
                str(row["reference_date"] or row["predict_time"] or "")[:10],
                str(row["direction"] or ""),
                str(row["signal_action"] or ""),
            )
            for row in rows
        }
        return len(events)

    def get_validated_predictions_for_codes(
        self,
        codes: list[str],
        *,
        mode: str = "",
        limit: int = 10,
    ) -> list["PredictionLog"]:
        normalized = list(dict.fromkeys(str(code).upper() for code in codes if code))
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        mode_sql = " AND mode = ?" if mode else ""
        params: list = [*normalized]
        if mode:
            params.append(mode)
        rows = self.execute(
            f"""SELECT * FROM prediction_log
                WHERE code IN ({placeholders}) AND validated = 1
                  AND validation_version >= 2 AND validation_status = 'verified'
                  {mode_sql}
                ORDER BY predict_time DESC""",
            tuple(params),
        ).fetchall()
        return self._dedupe_prediction_events(rows, limit)

    @staticmethod
    def _dedupe_prediction_events(rows, limit: int) -> list[PredictionLog]:
        """面向用户的预测结果按独立事件展示，不重复罗列策略样本。"""
        result = []
        seen = set()
        for row in rows:
            event = (
                str(row["code"] or ""),
                str(row["mode"] or ""),
                str(row["reference_date"] or row["predict_time"] or "")[:10],
                str(row["direction"] or ""),
                str(row["signal_action"] or ""),
            )
            if event in seen:
                continue
            seen.add(event)
            result.append(PredictionLog.from_dict(dict(row)))
            if len(result) >= max(int(limit), 1):
                break
        return result

    def get_latest_unverified_prediction(self, code: str) -> "PredictionLog | None":
        row = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND validated = 0
               ORDER BY predict_time DESC LIMIT 1""",
            (code,),
        ).fetchone()
        if row:
            return PredictionLog.from_dict(dict(row))
        return None

    def list_prediction_codes(self, market: str) -> list[str]:
        """返回某市场已有预测的单股代码，供历史评估独立于持仓展示。"""
        rows = self.execute(
            """SELECT code, MAX(predict_time) AS latest
               FROM prediction_log
               WHERE market = ? AND code NOT LIKE 'PORTFOLIO_%'
               GROUP BY code ORDER BY latest DESC""",
            (market,),
        ).fetchall()
        return [str(row["code"] or "") for row in rows if row["code"]]

    def get_prediction_status_counts(self, code: str) -> dict[str, int]:
        """返回已验证、待验证和不可验证数量。"""
        row = self.execute(
            """SELECT
                   SUM(CASE WHEN validated=1 AND validation_version>=2
                                  AND validation_status='verified' THEN 1 ELSE 0 END) AS verified,
                   SUM(CASE WHEN validated=0 THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN validated<0 OR validation_status IN
                                  ('unsupported', 'legacy_unverifiable') THEN 1 ELSE 0 END) AS unsupported
               FROM prediction_log WHERE code=?""",
            (code,),
        ).fetchone()
        return {
            "verified": int(row["verified"] or 0) if row else 0,
            "pending": int(row["pending"] or 0) if row else 0,
            "unsupported": int(row["unsupported"] or 0) if row else 0,
        }

    def get_validated_predictions(self, code: str, limit: int = 10) -> list[PredictionLog]:
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'
               ORDER BY predict_time DESC""",
            (code,),
        ).fetchall()
        return self._dedupe_prediction_events(rows, limit)

    def get_strategy_prediction_stats(
        self, code: str, strategy_name: str, limit: int = 20
    ) -> "PredictionStats":
        """按策略+股票统计预测准确率（供策略池持续优化使用）。"""
        from data.models import PredictionStats
        # 防护：旧数据库可能还没有 strategy_name 列
        cols = {r[1] for r in self.execute("PRAGMA table_info(prediction_log)").fetchall()}
        if "strategy_name" not in cols:
            return PredictionStats(code=f"{code}#{strategy_name}")
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND strategy_name = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'
                 AND direction IN ('bullish', 'bearish')
               ORDER BY predict_time DESC LIMIT ?""",
            (code, strategy_name, limit),
        ).fetchall()
        stats = PredictionStats(code=f"{code}#{strategy_name}", total_predictions=len(rows))
        if not rows:
            return stats
        correct = sum(
            1 for r in rows
            if r["direction"] and r["actual_direction"]
            and r["direction"] == r["actual_direction"]
        )
        stats.direction_accuracy_10 = correct / len(rows) if rows else 0.0
        stats.direction_accuracy_all = stats.direction_accuracy_10  # 无全量数据时用近期近似
        returns = [float(r["actual_return"] or 0.0) for r in rows]
        stats.avg_predicted_return = sum(returns) / len(returns) if returns else 0.0
        # 趋势判断
        if len(rows) >= 10:
            recent_5 = rows[:5]
            older_5 = rows[5:10]
            recent_acc = sum(
                1 for r in recent_5
                if r["direction"] and r["actual_direction"]
                and r["direction"] == r["actual_direction"]
            ) / 5 if len(recent_5) == 5 else 0
            older_acc = sum(
                1 for r in older_5
                if r["direction"] and r["actual_direction"]
                and r["direction"] == r["actual_direction"]
            ) / 5 if len(older_5) == 5 else 0
            if recent_acc > older_acc + 0.1:
                stats.accuracy_trend = "improving"
            elif recent_acc < older_acc - 0.1:
                stats.accuracy_trend = "declining"
            else:
                stats.accuracy_trend = "stable"
        if stats.direction_accuracy_10 >= 0.60:
            stats.status = "reliable"
        elif stats.direction_accuracy_10 >= 0.45:
            stats.status = "unstable"
        else:
            stats.status = "unreliable"
        from datetime import datetime as _dt
        stats.updated_at = _dt.now().isoformat()
        return stats

    def get_prediction_evaluation_panel(self, code: str) -> dict:
        """真实历史预测评估面板：整体、按策略、按行情状态聚合。"""
        cols = {r[1] for r in self.execute("PRAGMA table_info(prediction_log)").fetchall()}
        has_regime = "market_regime" in cols
        has_strategy = "strategy_name" in cols
        has_action = "signal_action" in cols

        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'
               ORDER BY predict_time DESC""",
            (code,),
        ).fetchall()
        dict_rows = [dict(r) for r in rows]

        def _summarize(items: list[dict], label: str) -> dict:
            actionable = [r for r in items if r.get("direction") in ("bullish", "bearish")]
            total = len(actionable)
            correct = sum(
                1 for r in actionable
                if r.get("direction") and r.get("actual_direction")
                and r.get("direction") == r.get("actual_direction")
            )
            returns = [float(r.get("actual_return") or 0.0) for r in actionable]
            avg_return = sum(returns) / len(returns) if returns else 0.0
            accuracy = correct / total if total else 0.0
            expectancy = "positive" if total >= 3 and accuracy >= 0.5 and avg_return > 0 else (
                "negative" if total >= 3 and (accuracy < 0.45 or avg_return < 0) else "insufficient"
            )
            return {
                "label": label,
                "count": total,
                "accuracy": accuracy,
                "avg_return": avg_return,
                "expectancy": expectancy,
            }

        def _group(field: str, fallback: str) -> list[dict]:
            groups: dict[str, list[dict]] = {}
            for r in dict_rows:
                key = (r.get(field) or fallback) if field in r else fallback
                groups.setdefault(key, []).append(r)
            summaries = [_summarize(items, key) for key, items in groups.items()]
            return sorted(summaries, key=lambda x: (x["count"], x["accuracy"], x["avg_return"]), reverse=True)

        return {
            "overall": _summarize(dict_rows, code),
            "by_strategy": _group("strategy_name", "整体预测") if has_strategy else [],
            "by_regime": _group("market_regime", "unknown") if has_regime else [],
            "by_action": _group("signal_action", "unknown") if has_action else [],
            "exit_reviews": self.get_exit_review_report(code),
        }

    def get_exit_review_report(self, code: str) -> list[dict]:
        """按策略汇总卖出后 20 日的退出质量。"""
        rows = self.execute(
            """SELECT strategy_name, COUNT(*) AS cnt,
                      AVG(exit_return_1d) AS avg_1d,
                      AVG(exit_return_5d) AS avg_5d,
                      AVG(exit_return_10d) AS avg_10d,
                      AVG(exit_return_20d) AS avg_20d,
                      AVG(exit_avoided_loss) AS avoided_loss,
                      AVG(exit_opportunity_cost) AS opportunity_cost,
                      AVG(exit_max_decline) AS max_decline,
                      AVG(exit_max_rally) AS max_rally,
                      SUM(CASE WHEN exit_quality='effective' THEN 1 ELSE 0 END) AS effective_cnt,
                      SUM(CASE WHEN exit_quality='premature' THEN 1 ELSE 0 END) AS premature_cnt
               FROM prediction_log
               WHERE code=? AND signal_action='sell'
                 AND exit_review_status='verified' AND strategy_name!=''
               GROUP BY strategy_name ORDER BY cnt DESC""",
            (code,),
        ).fetchall()
        result = []
        for row in rows:
            total = int(row["cnt"] or 0)
            result.append({
                "strategy_name": row["strategy_name"],
                "count": total,
                "avg_return_1d": float(row["avg_1d"] or 0.0),
                "avg_return_5d": float(row["avg_5d"] or 0.0),
                "avg_return_10d": float(row["avg_10d"] or 0.0),
                "avg_return_20d": float(row["avg_20d"] or 0.0),
                "avg_avoided_loss": float(row["avoided_loss"] or 0.0),
                "avg_opportunity_cost": float(row["opportunity_cost"] or 0.0),
                "avg_max_decline": float(row["max_decline"] or 0.0),
                "avg_max_rally": float(row["max_rally"] or 0.0),
                "effective_rate": float(row["effective_cnt"] or 0) / total if total else 0.0,
                "premature_rate": float(row["premature_cnt"] or 0) / total if total else 0.0,
                "sample_status": "ok" if total >= 8 else "thin" if total >= 3 else "insufficient",
            })
        return result

    def get_strategy_health_report(self, code: str) -> list[dict]:
        """按独立交易日和证据质量统计策略健康度。"""
        rows = self.execute(
            """SELECT strategy_key AS strategy_name, action AS signal_action,
                      reference_date, decision_session_date, created_at,
                      evaluated_at, net_return,
                      evidence_quality
               FROM trade_plan_log
               WHERE code = ? AND status='evaluated' AND strategy_key != ''
               ORDER BY evaluated_at ASC, id ASC""",
            (code,),
        ).fetchall()

        grouped: dict[tuple[str, str], list[dict]] = {}
        for raw in rows:
            row = dict(raw)
            grouped.setdefault(
                (str(row["strategy_name"]), str(row["signal_action"] or "unknown")), []
            ).append(row)

        result = []
        for (sname, signal_action), samples in grouped.items():
            daily: dict[str, list[tuple[float, float, str]]] = {}
            for sample in samples:
                session = str(
                    sample.get("decision_session_date")
                    or sample.get("reference_date")
                    or sample.get("created_at")
                    or ""
                )[:10]
                quality = str(sample.get("evidence_quality") or "")
                quality_weight = {
                    "provider": 1.0, "supplemental": 0.60, "mixed": 0.75,
                }.get(quality, 1.0)
                daily.setdefault(session, []).append((
                    float(sample.get("net_return") or 0.0), quality_weight,
                    str(sample.get("evaluated_at") or sample.get("created_at") or ""),
                ))
            independent = []
            for session, values in daily.items():
                denominator = sum(value[1] for value in values) or 1.0
                daily_return = sum(value[0] * value[1] for value in values) / denominator
                independent.append({
                    "session": session,
                    "return": daily_return,
                    "weight": max(value[1] for value in values),
                    "evaluated_at": max(value[2] for value in values),
                })
            independent.sort(key=lambda item: (item["session"], item["evaluated_at"]))
            total = len(independent)
            if total < 3:
                continue
            effective_samples = sum(float(item["weight"]) for item in independent)
            weighted_correct = sum(
                float(item["weight"]) for item in independent if item["return"] > 0
            )
            accuracy = weighted_correct / effective_samples if effective_samples > 0 else 0
            avg_return = (
                sum(item["return"] * item["weight"] for item in independent)
                / effective_samples if effective_samples > 0 else 0.0
            )
            confidence_lower = _wilson_lower_bound(weighted_correct, effective_samples)

            recent = independent[-5:]
            recent_weight = sum(float(item["weight"]) for item in recent)
            recent_correct = sum(
                float(item["weight"]) for item in recent if item["return"] > 0
            )
            recent_acc = recent_correct / recent_weight if recent_weight > 0 else 0
            recent_avg_return = (
                sum(float(item["return"]) * float(item["weight"]) for item in recent)
                / recent_weight if recent_weight > 0 else 0.0
            )

            sample_status = (
                "insufficient" if effective_samples < 5
                else "thin" if effective_samples < 8 else "ok"
            )
            risk_note = ""

            # 判定：小样本不允许直接可靠；负收益或置信下界过低会触发降级。
            if sample_status == "insufficient":
                action = "watch"
                status = "unstable"
                risk_note = "历史样本不足，不能作为强执行依据"
            elif confidence_lower < 0.30 or avg_return < -0.02 or (recent_acc < 0.30 and total >= 5):
                action = "demote"
                status = "unreliable"
                if avg_return < -0.02:
                    risk_note = "历史平均实际收益为负"
                elif confidence_lower < 0.30:
                    risk_note = "净盈利率置信下界过低"
                else:
                    risk_note = "近期正确率明显恶化"
            elif sample_status == "thin":
                action = "watch"
                status = "unstable"
                risk_note = "有效样本不足8个，不能标记为可靠策略"
            elif accuracy >= 0.60 and recent_acc >= 0.50 and confidence_lower >= 0.35 and avg_return >= 0:
                action = "keep"
                status = "reliable"
            elif accuracy >= 0.45 and recent_acc >= 0.40:
                action = "watch"
                status = "unstable"
            else:
                action = "watch"
                status = "unstable"
                risk_note = "历史表现尚未达到可执行置信度"

            result.append({
                "strategy_name": sname,
                "signal_action": signal_action,
                "total": total,
                "raw_total": len(samples),
                "effective_samples": round(effective_samples, 2),
                "accuracy": round(accuracy, 3),
                "recent_accuracy": round(recent_acc, 3),
                "confidence_lower_95": round(confidence_lower, 3),
                "avg_return": round(avg_return, 4),
                "recent_avg_return": round(recent_avg_return, 4),
                "sample_status": sample_status,
                "trend": "declining" if recent_acc < accuracy - 0.1 else (
                    "improving" if recent_acc > accuracy + 0.1 else "stable"
                ),
                "status": status,
                "action": action,
                "risk_note": risk_note,
            })

        return result

    # ═══════════════════════════════════════════════════════════════
    # 研究员观察/形态表现闭环 (research_observation_log)
    # ═══════════════════════════════════════════════════════════════

    def insert_research_observation(self, obs: ResearchObservationLog) -> int:
        """写入一条研究员/系统观察形态记录。"""
        d = obs.to_dict()
        d.pop("id", None)
        observed_day = str(d.get("observed_at") or "")[:10]
        d["event_key"] = d.get("event_key") or (
            f"{str(d.get('code') or '').upper()}|{d.get('pattern_type') or 'general'}|{observed_day}"
        )
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        cursor = self._execute_write(
            f"INSERT OR IGNORE INTO research_observation_log ({columns}) VALUES ({placeholders})",
            tuple(d.values()),
        )
        if cursor.rowcount:
            return cursor.lastrowid or 0
        row = self.execute(
            "SELECT id FROM research_observation_log WHERE event_key=?",
            (d["event_key"],),
        ).fetchone()
        return int(row["id"] or 0) if row else 0

    def batch_verify_research_observations(self) -> int:
        """只验证真正触发且已积累足够后续 K 线的观察记录。"""
        rows = self.execute(
            """SELECT * FROM research_observation_log
               WHERE validated = 0 AND trigger_price > 0
                 AND validation_status IN ('pending', 'triggered')"""
        ).fetchall()
        verified = 0
        for row in rows:
            obs = ResearchObservationLog.from_dict(dict(row))
            future = self.execute(
                """SELECT date, high, low, close
                   FROM price_history
                   WHERE code = ? AND date > ?
                   ORDER BY date ASC LIMIT 20""",
                (obs.code, obs.observed_at[:10]),
            ).fetchall()
            trigger = float(obs.trigger_price or 0)
            if trigger <= 0:
                continue

            operator = obs.trigger_operator or ""
            trigger_idx = -1 if obs.entry_triggered else None
            if trigger_idx is None and operator in ("cross_above", "cross_below"):
                # 条件计划默认 5 个交易日有效，过期未触发不计收益样本。
                for idx, bar in enumerate(future[:5]):
                    high = float(bar["high"] or 0)
                    low = float(bar["low"] or 0)
                    if operator == "cross_above" and high >= trigger:
                        trigger_idx = idx
                        break
                    if operator == "cross_below" and low <= trigger:
                        trigger_idx = idx
                        break
                if trigger_idx is None:
                    if len(future) >= 5:
                        self._execute_write(
                            """UPDATE research_observation_log
                               SET validated=1, validation_status='not_triggered', verified_at=?
                               WHERE id=?""",
                            (datetime.now().isoformat(), obs.id),
                        )
                    continue
            elif trigger_idx is None:
                # 旧版/中性/被反驳的观察没有可执行触发语义，隔离不学习。
                self._execute_write(
                    """UPDATE research_observation_log
                       SET validated=1, validation_status='unsupported', verified_at=?
                       WHERE id=?""",
                    (datetime.now().isoformat(), obs.id),
                )
                continue

            evaluation = future if trigger_idx == -1 else future[trigger_idx + 1:]
            if len(evaluation) < 10:
                continue
            evaluation = evaluation[:10]
            closes = [float(r["close"] or 0) for r in evaluation]
            highs = [float(r["high"] or 0) for r in evaluation]
            lows = [float(r["low"] or 0) for r in evaluation]
            triggered_at = (
                obs.observed_at[:10] if trigger_idx == -1
                else str(future[trigger_idx]["date"] or "")[:10]
            )

            def ret(day: int) -> float:
                idx = max(0, min(day - 1, len(closes) - 1))
                return (closes[idx] - trigger) / trigger if closes[idx] > 0 else 0.0

            obs.return_1d = ret(1)
            obs.return_3d = ret(3)
            obs.return_5d = ret(5)
            obs.return_10d = ret(10)

            if obs.expected_direction == "bearish":
                obs.max_adverse_return = (trigger - max(highs)) / trigger if highs else 0.0
                obs.hit_stop_loss = (
                    1 if obs.stop_loss > trigger and max(highs) >= obs.stop_loss else 0
                )
            else:
                obs.max_adverse_return = (min(lows) - trigger) / trigger if lows else 0.0
                obs.hit_stop_loss = 1 if obs.stop_loss > 0 and min(lows) <= obs.stop_loss else 0

            obs.validated = 1
            obs.entry_triggered = 1
            obs.triggered_at = triggered_at
            obs.validation_status = "verified"
            obs.verified_at = datetime.now().isoformat()
            self._execute_write(
                """UPDATE research_observation_log
                   SET validated=1, return_1d=?, return_3d=?, return_5d=?, return_10d=?,
                       max_adverse_return=?, hit_take_profit=?, hit_stop_loss=?, verified_at=?,
                       entry_triggered=1, triggered_at=?, validation_status='verified'
                   WHERE id=?""",
                (
                    obs.return_1d, obs.return_3d, obs.return_5d, obs.return_10d,
                    obs.max_adverse_return, obs.hit_take_profit, obs.hit_stop_loss,
                    obs.verified_at, obs.triggered_at, obs.id,
                ),
            )
            verified += 1
        if verified:
            logger.info(f"research_observation_log 批量验证：{verified} 条")
        return verified

    def get_research_observation_stats(
        self,
        code: str = "",
        pattern_type: str = "",
        execution_level: str = "",
    ) -> dict:
        """聚合观察形态表现，供风控官/报告显示历史正期望状态。"""
        conditions = [
            "validated = 1", "entry_triggered = 1",
            "validation_status = 'verified'",
        ]
        params: list = []
        if code:
            conditions.append("code = ?")
            params.append(code)
        if pattern_type:
            conditions.append("pattern_type = ?")
            params.append(pattern_type)
        if execution_level:
            conditions.append("execution_level = ?")
            params.append(execution_level)

        rows = self.execute(
            f"""SELECT * FROM research_observation_log
                WHERE {' AND '.join(conditions)}
                ORDER BY observed_at DESC""",
            tuple(params),
        ).fetchall()
        items = [dict(r) for r in rows]
        total = len(items)
        if total == 0:
            return {
                "count": 0,
                "win_rate_5d": 0.0,
                "avg_return_5d": 0.0,
                "avg_return_10d": 0.0,
                "avg_adverse": 0.0,
                "expectancy": "insufficient",
                "llm_count": 0,
                "llm_win_rate_5d": 0.0,
                "llm_avg_directional_5d": 0.0,
                "llm_expectancy": "insufficient",
            }

        def favorable(r: dict, horizon: str = "return_5d") -> bool:
            value = float(r.get(horizon) or 0.0)
            if r.get("expected_direction") == "bearish":
                return value < 0
            return value > 0

        win_rate = sum(1 for r in items if favorable(r)) / total
        avg_5d = sum(float(r.get("return_5d") or 0) for r in items) / total
        avg_10d = sum(float(r.get("return_10d") or 0) for r in items) / total
        avg_adverse = sum(float(r.get("max_adverse_return") or 0) for r in items) / total
        directional_avg = sum(
            -float(r.get("return_5d") or 0)
            if r.get("expected_direction") == "bearish"
            else float(r.get("return_5d") or 0)
            for r in items
        ) / total
        expectancy = "positive" if total >= 3 and win_rate >= 0.5 and directional_avg > 0 else (
            "negative" if total >= 3 and (win_rate < 0.45 or directional_avg < 0) else "insufficient"
        )
        llm_items = [item for item in items if int(item.get("llm_proposed") or 0) == 1]
        llm_count = len(llm_items)
        llm_wins = sum(1 for item in llm_items if favorable(item))
        llm_win_rate = llm_wins / llm_count if llm_count else 0.0
        llm_directional_avg = (
            sum(
                -float(item.get("return_5d") or 0)
                if item.get("expected_direction") == "bearish"
                else float(item.get("return_5d") or 0)
                for item in llm_items
            ) / llm_count if llm_count else 0.0
        )
        llm_expectancy = (
            "positive" if llm_count >= 3 and llm_win_rate >= 0.5 and llm_directional_avg > 0
            else "negative" if llm_count >= 3 and (
                llm_win_rate < 0.45 or llm_directional_avg < 0
            ) else "insufficient"
        )
        return {
            "count": total,
            "win_rate_5d": round(win_rate, 4),
            "avg_return_5d": round(avg_5d, 4),
            "avg_return_10d": round(avg_10d, 4),
            "avg_adverse": round(avg_adverse, 4),
            "expectancy": expectancy,
            "llm_count": llm_count,
            "llm_win_rate_5d": round(llm_win_rate, 4),
            "llm_avg_directional_5d": round(llm_directional_avg, 4),
            "llm_expectancy": llm_expectancy,
        }

    def get_research_observation_overview(self, market: str = "", limit: int = 20) -> list[dict]:
        """按股票+形态+执行等级聚合观察形态历史表现。"""
        conditions = [
            "validated = 1", "entry_triggered = 1",
            "validation_status = 'verified'",
        ]
        params: list = []
        if market:
            conditions.append("market = ?")
            params.append(market)
        sql = f"""SELECT code, name, pattern_type, execution_level,
                         COUNT(*) as cnt,
                         SUM(CASE WHEN llm_proposed=1 THEN 1 ELSE 0 END) as llm_cnt,
                         SUM(CASE
                             WHEN (expected_direction = 'bearish' AND return_5d < 0)
                                  OR (expected_direction != 'bearish' AND return_5d > 0)
                             THEN 1 ELSE 0 END) as wins_5d,
                         SUM(CASE
                             WHEN llm_proposed=1 AND (
                                 (expected_direction = 'bearish' AND return_5d < 0)
                                 OR (expected_direction != 'bearish' AND return_5d > 0))
                             THEN 1 ELSE 0 END) as llm_wins_5d,
                         AVG(return_5d) as avg_return_5d,
                         AVG(CASE WHEN expected_direction = 'bearish'
                                  THEN -return_5d ELSE return_5d END) as avg_directional_5d,
                         AVG(return_10d) as avg_return_10d,
                         AVG(max_adverse_return) as avg_adverse
                  FROM research_observation_log
                  WHERE {' AND '.join(conditions)}
                  GROUP BY code, pattern_type, execution_level
                  ORDER BY cnt DESC, wins_5d DESC
                  LIMIT ?"""
        params.append(limit)
        rows = self.execute(sql, tuple(params)).fetchall()
        overview = []
        for row in rows:
            count = int(row["cnt"] or 0)
            wins = int(row["wins_5d"] or 0)
            win_rate = wins / count if count else 0.0
            avg_5d = float(row["avg_return_5d"] or 0.0)
            llm_count = int(row["llm_cnt"] or 0)
            llm_win_rate = int(row["llm_wins_5d"] or 0) / llm_count if llm_count else 0.0
            directional_avg = float(row["avg_directional_5d"] or 0.0)
            expectancy = "positive" if count >= 3 and win_rate >= 0.5 and directional_avg > 0 else (
                "negative" if count >= 3 and (win_rate < 0.45 or directional_avg < 0) else "insufficient"
            )
            overview.append({
                "code": row["code"] or "",
                "name": row["name"] or row["code"] or "",
                "pattern_type": row["pattern_type"] or "",
                "execution_level": row["execution_level"] or "",
                "count": count,
                "llm_count": llm_count,
                "llm_win_rate_5d": round(llm_win_rate, 4),
                "win_rate_5d": round(win_rate, 4),
                "avg_return_5d": round(avg_5d, 4),
                "avg_return_10d": round(float(row["avg_return_10d"] or 0.0), 4),
                "avg_adverse": round(float(row["avg_adverse"] or 0.0), 4),
                "expectancy": expectancy,
            })
        return overview

    # ═══════════════════════════════════════════════════════════════
    # 策略池缓存 (bt_variant_cache) + 最佳参数 (per_stock_params) CRUD
    # ═══════════════════════════════════════════════════════════════

    def get_cached_backtest(
        self, stock_code: str, strategy_key: str,
        params_json: str, data_start: str, data_end: str,
        data_length: int | None = None,
    ) -> dict | None:
        """查询缓存的回测结果。返回 result_json 字典或 None。"""
        length_clause = " AND data_length=?" if data_length is not None else ""
        params = [stock_code, strategy_key, params_json, data_start, data_end]
        if data_length is not None:
            params.append(data_length)
        row = self.execute(
            """SELECT result_json FROM bt_variant_cache
               WHERE stock_code=? AND strategy_key=? AND params_json=?
                 AND data_start=? AND data_end=?""" + length_clause,
            tuple(params),
        ).fetchone()
        if row:
            import json
            return json.loads(row["result_json"])
        return None

    def save_backtest_cache(
        self, stock_code: str, strategy_key: str,
        params_json: str, data_start: str, data_end: str,
        data_length: int, sharpe_ratio: float, total_return: float,
        max_drawdown: float, win_rate: float, total_trades: int,
        result_json: str,
    ):
        """写入回测缓存。"""
        from datetime import datetime as _dt
        self._execute_write(
            """INSERT OR REPLACE INTO bt_variant_cache
               (stock_code, strategy_key, params_json, data_start, data_end,
                data_length, sharpe_ratio, total_return, max_drawdown,
                win_rate, total_trades, result_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (stock_code, strategy_key, params_json, data_start, data_end,
             data_length, sharpe_ratio, total_return, max_drawdown,
             win_rate, total_trades, result_json,
             _dt.now().isoformat()),
        )

    def get_best_params(
        self, stock_code: str, strategy_key: str,
    ) -> dict | None:
        """查询某股票某策略的最佳参数。"""
        row = self.execute(
            """SELECT best_params_json, best_sharpe, source, updated_at
               FROM per_stock_params WHERE stock_code=? AND strategy_key=?""",
            (stock_code, strategy_key),
        ).fetchone()
        if row:
            import json
            return {
                "params": json.loads(row["best_params_json"]),
                "sharpe": row["best_sharpe"],
                "source": row["source"],
                "updated_at": row["updated_at"],
            }
        return None

    def save_best_params(
        self, stock_code: str, strategy_key: str,
        params_json: str, sharpe: float, source: str = "audit_pass",
    ):
        """保存/更新最佳参数。auto_tuned 有保护。"""
        from datetime import datetime as _dt
        now = _dt.now().isoformat()
        existing = self.execute(
            """SELECT best_sharpe, source FROM per_stock_params
               WHERE stock_code=? AND strategy_key=?""",
            (stock_code, strategy_key),
        ).fetchone()

        if source == "auto_tuned" and existing and existing["source"] == "auto_tuned":
            if sharpe < (existing["best_sharpe"] or 0) + 0.1:
                return  # 小波动不覆盖自适应结果

        self._execute_write(
            """INSERT OR REPLACE INTO per_stock_params
               (stock_code, strategy_key, best_params_json, best_sharpe,
                source, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (stock_code, strategy_key, params_json, sharpe, source, now),
        )

    def record_strategy_param_candidate(
        self,
        *,
        stock_code: str,
        strategy_key: str,
        params_json: str,
        test_sharpe: float,
        walk_forward: dict,
        data_end: str,
        min_confirmations: int = 3,
        min_paper_days: int = 20,
    ) -> dict:
        """记录候选并在跨窗口重复通过后晋升，禁止单次回测直接覆盖冠军。"""
        now = datetime.now().isoformat()
        avg_oos_return = float(walk_forward.get("avg_oos_return", 0.0) or 0.0)
        avg_oos_excess_return = float(
            walk_forward.get("avg_oos_excess_return", 0.0) or 0.0
        )
        avg_oos_sharpe = float(walk_forward.get("avg_oos_sharpe", 0.0) or 0.0)
        oos_trades = int(walk_forward.get("oos_trades", 0) or 0)
        selected_windows = int(walk_forward.get("selected_windows", 0) or 0)
        positive_excess_windows = int(
            walk_forward.get("positive_excess_windows", 0) or 0
        )
        risk_adjusted_windows = int(
            walk_forward.get("risk_adjusted_windows", 0) or 0
        )
        qualified_windows = int(
            walk_forward.get(
                "qualified_windows",
                max(positive_excess_windows, risk_adjusted_windows),
            ) or 0
        )
        promotion_path = str(walk_forward.get("promotion_path") or "")
        required_qualified_windows = max(1, (selected_windows + 1) // 2)
        eligible = (
            bool(walk_forward.get("pass_oos"))
            and avg_oos_return > 0
            and avg_oos_sharpe >= 0
            and oos_trades >= 3
            and selected_windows >= 1
            and qualified_windows >= required_qualified_windows
        )
        row = self.execute(
            """SELECT confirmations, last_data_end, status, first_eligible_data_end
               FROM strategy_param_candidates
               WHERE stock_code=? AND strategy_key=? AND params_json=?""",
            (stock_code, strategy_key, params_json),
        ).fetchone()
        confirmations = int(row["confirmations"] or 0) if row else 0
        if eligible and (not row or row["last_data_end"] != data_end):
            confirmations += 1
        first_eligible_data_end = str(
            (row["first_eligible_data_end"] if row else "") or ""
        )
        if not eligible:
            confirmations = 0
            first_eligible_data_end = ""
        elif not first_eligible_data_end:
            first_eligible_data_end = data_end
        paper_days = 0
        try:
            first_day = datetime.fromisoformat(first_eligible_data_end[:10]).date()
            current_day = datetime.fromisoformat(data_end[:10]).date()
            paper_days = max((current_day - first_day).days, 0)
        except (TypeError, ValueError):
            paper_days = 0
        paper_complete = paper_days >= max(int(min_paper_days), 0)
        if not eligible:
            status = "rejected"
            reason = "样本外正收益、超额/风险调整优势、夏普或交易数未同时达标"
        elif confirmations < min_confirmations:
            status = "candidate"
            reason = f"等待不同数据截止日确认（{confirmations}/{min_confirmations}）"
        elif not paper_complete:
            status = "paper"
            reason = f"影子观察期{paper_days}/{min_paper_days}天，暂不晋升"
        else:
            status = "candidate"
            reason = "达到双通道样本外和影子观察晋升门槛"
        created_at = now
        if row:
            old = self.execute(
                """SELECT created_at FROM strategy_param_candidates
                   WHERE stock_code=? AND strategy_key=? AND params_json=?""",
                (stock_code, strategy_key, params_json),
            ).fetchone()
            created_at = old["created_at"] or now
        self._execute_write(
            """INSERT OR REPLACE INTO strategy_param_candidates
               (stock_code, strategy_key, params_json, test_sharpe,
                avg_oos_return, avg_oos_excess_return, avg_oos_sharpe,
                oos_trades, selected_windows, positive_excess_windows,
                risk_adjusted_windows, qualified_windows, promotion_path,
                confirmations, first_eligible_data_end, last_data_end,
                status, reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stock_code, strategy_key, params_json, float(test_sharpe),
                avg_oos_return, avg_oos_excess_return, avg_oos_sharpe,
                oos_trades, selected_windows, positive_excess_windows,
                risk_adjusted_windows, qualified_windows, promotion_path,
                confirmations, first_eligible_data_end, data_end,
                status, reason, created_at, now,
            ),
        )

        current = self.get_best_params(stock_code, strategy_key)
        current_sharpe = float(current.get("sharpe", -999.0)) if current else -999.0
        current_source = str(current.get("source", "")) if current else ""
        improves_champion = test_sharpe >= current_sharpe + 0.15
        can_promote = eligible and confirmations >= min_confirmations and paper_complete and (
            current is None or current_source == "demoted" or improves_champion
        )
        if can_promote:
            self._execute_write(
                """UPDATE strategy_param_candidates SET status='superseded', updated_at=?
                   WHERE stock_code=? AND strategy_key=? AND status='champion'
                     AND params_json != ?""",
                (now, stock_code, strategy_key, params_json),
            )
            self.save_best_params(
                stock_code, strategy_key, params_json,
                float(test_sharpe), source="auto_tuned",
            )
            self._execute_write(
                """UPDATE strategy_param_candidates
                   SET status='champion', reason='跨窗口确认后晋升', updated_at=?
                   WHERE stock_code=? AND strategy_key=? AND params_json=?""",
                (now, stock_code, strategy_key, params_json),
            )
            status = "champion"

        return {
            "status": status,
            "confirmations": confirmations,
            "paper_days": paper_days,
            "avg_oos_excess_return": avg_oos_excess_return,
            "promotion_path": promotion_path,
            "eligible": eligible,
            "promoted": status == "champion",
        }

    def get_strategy_param_candidates(
        self, stock_code: str, strategy_key: str = "",
    ) -> list[dict]:
        conditions = ["stock_code=?"]
        params: list = [stock_code]
        if strategy_key:
            conditions.append("strategy_key=?")
            params.append(strategy_key)
        rows = self.execute(
            f"""SELECT * FROM strategy_param_candidates
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_strategy_demoted(
        self,
        stock_code: str,
        strategy_key: str,
        reason: str = "",
    ):
        """把负期望策略写入降级记忆，后续只允许通过重新验证恢复。"""
        import json
        current = self.get_best_params(stock_code, strategy_key)
        if current and current.get("source") != "demoted":
            current_json = json.dumps(current.get("params") or {}, sort_keys=True)
            self._execute_write(
                """UPDATE strategy_param_candidates
                   SET status='rolled_back', reason=?, updated_at=?
                   WHERE stock_code=? AND strategy_key=? AND params_json=?""",
                (
                    reason or "实盘健康度转负，撤销冠军参数",
                    datetime.now().isoformat(), stock_code, strategy_key, current_json,
                ),
            )
        self.save_best_params(
            stock_code=stock_code,
            strategy_key=strategy_key,
            params_json=json.dumps({"demoted_reason": reason}, sort_keys=True),
            sharpe=-999.0,
            source="demoted",
        )

    def apply_strategy_health_feedback(
        self,
        stock_code: str,
        health_report: list[dict],
    ) -> dict:
        """把策略健康度回灌到 per_stock_params，形成自动降级/待重训闭环。"""
        if not stock_code or not health_report:
            return {"demoted": 0, "watched": 0}
        demoted = 0
        watched = 0
        for h in health_report:
            strategy_key = str(h.get("strategy_name") or "").strip()
            if not strategy_key:
                continue
            # 参数冠军主要控制入场。退出样本单独约束 sell 信号排序，
            # 不能因卖出时机不佳而误删同策略仍有效的买入参数。
            if h.get("signal_action") == "sell":
                if h.get("action") == "watch":
                    watched += 1
                continue
            action = h.get("action")
            if action == "demote":
                parts = []
                if h.get("total") is not None:
                    parts.append(f"样本{int(h.get('total') or 0)}")
                if h.get("accuracy") is not None:
                    parts.append(f"净盈利率{float(h.get('accuracy') or 0):.0%}")
                if h.get("confidence_lower_95") is not None:
                    parts.append(f"95%下界{float(h.get('confidence_lower_95') or 0):.0%}")
                if h.get("avg_return") is not None:
                    parts.append(f"均值收益{float(h.get('avg_return') or 0):+.2%}")
                if h.get("risk_note"):
                    parts.append(str(h.get("risk_note")))
                self.mark_strategy_demoted(stock_code, strategy_key, "，".join(parts))
                demoted += 1
            elif action == "watch":
                watched += 1
        return {"demoted": demoted, "watched": watched}

    def cleanup_stale_cache(self, days: int = 30):
        """清理过期回测缓存。"""
        self._execute_write(
            f"DELETE FROM bt_variant_cache WHERE created_at < date('now', '-{days} days')"
        )

    def has_recent_deep_optimization(
        self,
        stock_code: str,
        data_end: str,
        hours: int = 24,
    ) -> bool:
        """同一股票/数据截止日已有足量参数回测缓存时，不重复启动深度优化。"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        row = self.execute(
            """SELECT status, started_at, finished_at FROM deep_optimization_runs
               WHERE stock_code=? AND data_end=?""",
            (stock_code, data_end),
        ).fetchone()
        if not row:
            return False
        if row["status"] == "complete" and (row["finished_at"] or "") >= cutoff:
            return True
        running_cutoff = (datetime.now() - timedelta(hours=6)).isoformat()
        return row["status"] == "running" and (row["started_at"] or "") >= running_cutoff

    def mark_deep_optimization_started(self, stock_code: str, data_end: str):
        now = datetime.now().isoformat()
        self._execute_write(
            """INSERT OR REPLACE INTO deep_optimization_runs
               (stock_code, data_end, status, variant_count, started_at, finished_at, error)
               VALUES (?, ?, 'running', 0, ?, '', '')""",
            (stock_code, data_end, now),
        )

    def mark_deep_optimization_finished(
        self,
        stock_code: str,
        data_end: str,
        *,
        variant_count: int = 0,
        error: str = "",
    ):
        self._execute_write(
            """UPDATE deep_optimization_runs
               SET status=?, variant_count=?, finished_at=?, error=?
               WHERE stock_code=? AND data_end=?""",
            (
                "failed" if error else "complete",
                int(variant_count), datetime.now().isoformat(), error[:500],
                stock_code, data_end,
            ),
        )

    def is_strategy_demoted(self, stock_code: str, strategy_key: str) -> bool:
        """检查策略是否被永久降级。"""
        row = self.execute(
            """SELECT source FROM per_stock_params
               WHERE stock_code=? AND strategy_key=? AND source='demoted'""",
            (stock_code, strategy_key),
        ).fetchone()
        return row is not None
