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

import sqlite3
import logging
import math
import threading
from datetime import datetime
from typing import Optional

from data.models import (
    StockInfo, PriceData, AnalysisReport, NewsItem,
    Holding, WatchItem, AccountBalance, PredictionLog, ResearchObservationLog,
)
from config.settings import Settings

logger = logging.getLogger(__name__)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str, default: str = "''"):
    """SQLite 兼容的"加列如果不存在"——遍历 PRAGMA 列名。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}")
        logger.info(f"DB migrate: added column {table}.{column}")


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial success rate."""
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


# ======================== 数据库建表 DDL ========================

CREATE_TABLES_SQL = """
-- 股票基本信息表：缓存从 API 获取的股票元数据
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,           -- 股票代码（A 股 6 位数字 / 美股字母）
    name TEXT NOT NULL,              -- 股票名称
    market TEXT NOT NULL,            -- 市场："A" / "US"
    industry TEXT DEFAULT '',        -- 所属行业
    description TEXT DEFAULT '',     -- 公司简介
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
    confidence REAL DEFAULT 0.0           -- 置信度
);

CREATE INDEX IF NOT EXISTS idx_news_code ON news_sentiment(code);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_sentiment(date);

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
    max_favorable_excursion REAL DEFAULT 0.0,
    max_adverse_excursion REAL DEFAULT 0.0,
    actual_direction TEXT DEFAULT '',
    entry_triggered INTEGER DEFAULT 0,
    verified_at TEXT DEFAULT '',
    validation_end_date TEXT DEFAULT '',
    validation_status TEXT DEFAULT 'pending',
    validation_version INTEGER DEFAULT 2,
    strategy_name TEXT DEFAULT '',
    market_regime TEXT DEFAULT '',
    portfolio_snapshot TEXT DEFAULT ''
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
    avg_oos_sharpe REAL DEFAULT 0.0,
    oos_trades INTEGER DEFAULT 0,
    selected_windows INTEGER DEFAULT 0,
    confirmations INTEGER DEFAULT 0,
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
        _ensure_column(conn, "reports", "mode", "TEXT", "'eod'")
        _ensure_column(conn, "reports", "prediction_data", "TEXT", "''")
        _ensure_column(conn, "news_sentiment", "content", "TEXT", "''")
        _ensure_column(conn, "news_sentiment", "is_macro", "INTEGER", "0")
        _ensure_column(conn, "prediction_log", "strategy_name", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "market_regime", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "take_profit", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "entry_mode", "TEXT", "'reference'")
        _ensure_column(conn, "prediction_log", "reference_date", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "underlying_return", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "validation_price", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "max_favorable_excursion", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "max_adverse_excursion", "REAL", "0.0")
        _ensure_column(conn, "prediction_log", "validation_end_date", "TEXT", "''")
        _ensure_column(conn, "prediction_log", "validation_status", "TEXT", "'pending'")
        # 迁移时旧记录必须保留 v1，防止错误验证结果继续训练风控官。
        _ensure_column(conn, "prediction_log", "validation_version", "INTEGER", "1")
        _ensure_column(conn, "prediction_log", "portfolio_snapshot", "TEXT", "''")
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
        try:
            self.cleanup_stale_cache()
        except Exception:
            pass  # 表可能还没建（首次启动），忽略
        logger.info(f"Database connected: {db_path}")

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接（懒加载：如已关闭则自动重连，复用 _open 应用 PRAGMA）。"""
        if self._conn is None:
            db_path = getattr(self, "_db_path", None) or Settings().db_path
            self._conn = self._open(db_path)
            self._db_path = db_path
            logger.info(f"Database reconnected: {db_path}")
        return self._conn

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
        sql = """INSERT OR REPLACE INTO stocks (code, name, market, industry, description, update_time)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        self._execute_write(sql, (stock.code, stock.name, stock.market,
                                  stock.industry, stock.description,
                                  stock.update_time))


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
        sql = """INSERT INTO news_sentiment (code, date, title, source, content, sentiment, confidence, is_macro)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(code, date, title) DO UPDATE SET
                   source=excluded.source,
                   content=excluded.content,
                   sentiment=excluded.sentiment,
                   confidence=excluded.confidence,
                   is_macro=excluded.is_macro"""
        data = [(n.code, n.date, n.title, n.source, n.content or "",
                 n.sentiment, n.confidence, 1 if getattr(n, 'is_macro', False) else 0)
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
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d")
        rows = self.execute(
            """SELECT * FROM news_sentiment
               WHERE code = ? AND date >= ? AND sentiment != ''
               ORDER BY date DESC LIMIT ?""",
            (code, cutoff, limit)
        ).fetchall()
        return [NewsItem.from_dict(dict(r)) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # 预测追踪 (prediction_log) CRUD
    # ═══════════════════════════════════════════════════════════════

    def insert_prediction(self, pred: PredictionLog) -> int:
        d = pred.to_dict()
        d.pop("id", None)
        if d.get("validated") == 1 and d.get("validation_status") == "pending":
            d["validation_status"] = "verified"
        # 防护：旧数据库还没有 strategy_name 列时，移除该字段
        cols = {r[1] for r in self.execute("PRAGMA table_info(prediction_log)").fetchall()}
        if "strategy_name" not in cols:
            d.pop("strategy_name", None)
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        cursor = self._execute_write(
            f"INSERT INTO prediction_log ({columns}) VALUES ({placeholders})",
            tuple(d.values()),
        )
        return cursor.lastrowid or 0

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
                pred.validation_status = "not_triggered"
                pred.validation_version = 2
                pred.validated = 1
                return True

        pred.entry_triggered = 1
        exit_price = end_close
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
                    break
                if take_profit > 0 and high >= take_profit:
                    exit_price = max(take_profit, bar_open)
                    break
            else:
                favorable.append((entry_price - low) / entry_price)
                adverse.append((entry_price - high) / entry_price)
                if stop > 0 and high >= stop:
                    exit_price = max(stop, bar_open)
                    break
                if take_profit > 0 and low <= take_profit:
                    exit_price = min(take_profit, bar_open)
                    break

        gross_return = (
            (exit_price - entry_price) / entry_price
            if direction == "bullish"
            else (entry_price - exit_price) / entry_price
        )
        from utils.market_rules import get_market_rules
        pred.actual_return = gross_return - get_market_rules(pred.market).round_trip_cost_pct
        pred.validation_price = exit_price
        pred.max_favorable_excursion = max(favorable, default=0.0)
        pred.max_adverse_excursion = min(adverse, default=0.0)
        pred.validation_status = "verified"
        pred.validation_version = 2
        pred.validated = 1
        return True

    def _save_prediction_validation(self, pred: PredictionLog):
        self._execute_write(
            """UPDATE prediction_log SET validated=?, reference_date=?, actual_return=?, underlying_return=?,
               validation_price=?, max_favorable_excursion=?, max_adverse_excursion=?,
               actual_direction=?, entry_triggered=?, verified_at=?, validation_end_date=?,
               validation_status=?, validation_version=? WHERE id=?""",
            (
                pred.validated, pred.reference_date, pred.actual_return, pred.underlying_return,
                pred.validation_price, pred.max_favorable_excursion,
                pred.max_adverse_excursion, pred.actual_direction,
                pred.entry_triggered, pred.verified_at, pred.validation_end_date,
                pred.validation_status, pred.validation_version, pred.id,
            ),
        )

    def batch_verify_expired(self) -> int:
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE validated = 0 OR validation_version < 2"""
        ).fetchall()
        if not rows:
            return 0
        verified_count = 0
        for row in rows:
            pred = PredictionLog.from_dict(dict(row))
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
            is_portfolio = pred.code.startswith("PORTFOLIO_")
            completed = (
                self._verify_portfolio_prediction(pred)
                if is_portfolio else self._verify_stock_prediction(pred)
            )
            if completed:
                pred.verified_at = datetime.now().isoformat()
                self._save_prediction_validation(pred)
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
               ORDER BY predict_time DESC LIMIT ?""",
            (code, limit),
        ).fetchall()
        stats = PredictionStats(code=code, total_predictions=len(rows))
        if not rows:
            return stats
        correct = sum(
            1 for r in rows
            if r["direction"] and r["actual_direction"]
            and r["direction"] == r["actual_direction"]
        )
        stats.direction_accuracy_10 = correct / len(rows) if rows else 0.0
        returns = [float(r["actual_return"] or 0.0) for r in rows]
        stats.avg_predicted_return = sum(returns) / len(returns) if returns else 0.0
        all_rows = self.execute(
            """SELECT * FROM prediction_log WHERE code = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'""",
            (code,),
        ).fetchall()
        all_correct = sum(
            1 for r in all_rows
            if r["direction"] and r["actual_direction"]
            and r["direction"] == r["actual_direction"]
        )
        stats.total_predictions = len(all_rows)
        stats.direction_accuracy_all = all_correct / len(all_rows) if all_rows else 0.0
        if len(rows) >= 10:
            recent_5 = rows[:5]
            older_5 = rows[5:10]
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

    def get_validated_predictions(self, code: str, limit: int = 10) -> list[PredictionLog]:
        rows = self.execute(
            """SELECT * FROM prediction_log
               WHERE code = ? AND validated = 1
                 AND validation_version >= 2 AND validation_status = 'verified'
               ORDER BY predict_time DESC LIMIT ?""",
            (code, limit),
        ).fetchall()
        return [PredictionLog.from_dict(dict(r)) for r in rows]

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
        }

    def get_strategy_health_report(self, code: str) -> list[dict]:
        """持续优化：按策略统计预测准确率，返回健康报告。"""
        # 防护：旧数据库可能还没有 strategy_name 列
        cols = {r[1] for r in self.execute("PRAGMA table_info(prediction_log)").fetchall()}
        if "strategy_name" not in cols:
            return []

        rows = self.execute(
            """SELECT strategy_name, COUNT(*) as cnt,
                      SUM(CASE WHEN actual_return > 0 THEN 1 ELSE 0 END) as correct_cnt,
                      AVG(COALESCE(actual_return, 0.0)) as avg_return
               FROM prediction_log
               WHERE code = ? AND validated = 1 AND strategy_name != ''
                 AND validation_version >= 2 AND validation_status = 'verified'
                 AND direction IN ('bullish', 'bearish')
               GROUP BY strategy_name
               HAVING cnt >= 3
               ORDER BY cnt DESC""",
            (code,),
        ).fetchall()

        result = []
        for row in rows:
            sname = row["strategy_name"]
            total = row["cnt"]
            correct = row["correct_cnt"] or 0
            accuracy = correct / total if total > 0 else 0
            avg_return = float(row["avg_return"] or 0.0)
            confidence_lower = _wilson_lower_bound(correct, total)

            # 判断趋势：对比最近 5 条 vs 全部
            recent = self.execute(
                """SELECT direction, actual_direction, actual_return FROM prediction_log
                   WHERE code=? AND strategy_name=? AND validated=1
                     AND validation_version >= 2 AND validation_status='verified'
                     AND direction IN ('bullish', 'bearish')
                   ORDER BY predict_time DESC LIMIT 5""",
                (code, sname),
            ).fetchall()
            recent_correct = sum(
                1 for r in recent if float(r["actual_return"] or 0.0) > 0
            )
            recent_acc = recent_correct / len(recent) if recent else 0
            recent_returns = [float(r["actual_return"] or 0.0) for r in recent]
            recent_avg_return = sum(recent_returns) / len(recent_returns) if recent_returns else 0.0

            sample_status = "insufficient" if total < 5 else ("thin" if total < 8 else "ok")
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
                "total": total,
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
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        cursor = self._execute_write(
            f"INSERT INTO research_observation_log ({columns}) VALUES ({placeholders})",
            tuple(d.values()),
        )
        return cursor.lastrowid or 0

    def batch_verify_research_observations(self) -> int:
        """验证已积累足够后续 K 线的观察记录。"""
        rows = self.execute(
            """SELECT * FROM research_observation_log
               WHERE validated = 0 AND trigger_price > 0"""
        ).fetchall()
        verified = 0
        for row in rows:
            obs = ResearchObservationLog.from_dict(dict(row))
            future = self.execute(
                """SELECT date, high, low, close
                   FROM price_history
                   WHERE code = ? AND date > ?
                   ORDER BY date ASC LIMIT 10""",
                (obs.code, obs.observed_at[:10]),
            ).fetchall()
            if len(future) < 10:
                continue

            closes = [float(r["close"] or 0) for r in future]
            highs = [float(r["high"] or 0) for r in future]
            lows = [float(r["low"] or 0) for r in future]
            trigger = float(obs.trigger_price or 0)
            if trigger <= 0:
                continue

            def ret(day: int) -> float:
                idx = max(0, min(day - 1, len(closes) - 1))
                return (closes[idx] - trigger) / trigger if closes[idx] > 0 else 0.0

            obs.return_1d = ret(1)
            obs.return_3d = ret(3)
            obs.return_5d = ret(5)
            obs.return_10d = ret(10)

            if obs.expected_direction == "bearish":
                obs.max_adverse_return = (max(highs) - trigger) / trigger if highs else 0.0
                obs.hit_stop_loss = 1 if obs.stop_loss > 0 and max(highs) >= obs.stop_loss else 0
                obs.hit_take_profit = 1 if min(lows) < trigger else 0
            else:
                obs.max_adverse_return = (min(lows) - trigger) / trigger if lows else 0.0
                obs.hit_stop_loss = 1 if obs.stop_loss > 0 and min(lows) <= obs.stop_loss else 0
                obs.hit_take_profit = 1 if max(highs) > trigger else 0

            obs.validated = 1
            obs.verified_at = datetime.now().isoformat()
            self._execute_write(
                """UPDATE research_observation_log
                   SET validated=1, return_1d=?, return_3d=?, return_5d=?, return_10d=?,
                       max_adverse_return=?, hit_take_profit=?, hit_stop_loss=?, verified_at=?
                   WHERE id=?""",
                (
                    obs.return_1d, obs.return_3d, obs.return_5d, obs.return_10d,
                    obs.max_adverse_return, obs.hit_take_profit, obs.hit_stop_loss,
                    obs.verified_at, obs.id,
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
        conditions = ["validated = 1"]
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
        directional_avg = avg_5d
        if items and items[0].get("expected_direction") == "bearish":
            directional_avg = -avg_5d
        expectancy = "positive" if total >= 3 and win_rate >= 0.5 and directional_avg > 0 else (
            "negative" if total >= 3 and (win_rate < 0.45 or directional_avg < 0) else "insufficient"
        )
        return {
            "count": total,
            "win_rate_5d": round(win_rate, 4),
            "avg_return_5d": round(avg_5d, 4),
            "avg_return_10d": round(avg_10d, 4),
            "avg_adverse": round(avg_adverse, 4),
            "expectancy": expectancy,
        }

    def get_research_observation_overview(self, market: str = "", limit: int = 20) -> list[dict]:
        """按股票+形态+执行等级聚合观察形态历史表现。"""
        conditions = ["validated = 1"]
        params: list = []
        if market:
            conditions.append("market = ?")
            params.append(market)
        sql = f"""SELECT code, name, pattern_type, execution_level,
                         COUNT(*) as cnt,
                         SUM(CASE
                             WHEN (expected_direction = 'bearish' AND return_5d < 0)
                                  OR (expected_direction != 'bearish' AND return_5d > 0)
                             THEN 1 ELSE 0 END) as wins_5d,
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
        min_confirmations: int = 2,
    ) -> dict:
        """记录候选并在跨窗口重复通过后晋升，禁止单次回测直接覆盖冠军。"""
        now = datetime.now().isoformat()
        avg_oos_return = float(walk_forward.get("avg_oos_return", 0.0) or 0.0)
        avg_oos_sharpe = float(walk_forward.get("avg_oos_sharpe", 0.0) or 0.0)
        oos_trades = int(walk_forward.get("oos_trades", 0) or 0)
        selected_windows = int(walk_forward.get("selected_windows", 0) or 0)
        eligible = (
            bool(walk_forward.get("pass_oos"))
            and avg_oos_return > 0
            and avg_oos_sharpe >= 0
            and oos_trades >= 3
            and selected_windows >= 1
        )
        row = self.execute(
            """SELECT confirmations, last_data_end, status FROM strategy_param_candidates
               WHERE stock_code=? AND strategy_key=? AND params_json=?""",
            (stock_code, strategy_key, params_json),
        ).fetchone()
        confirmations = int(row["confirmations"] or 0) if row else 0
        if eligible and (not row or row["last_data_end"] != data_end):
            confirmations += 1
        status = "candidate" if eligible else "rejected"
        reason = (
            "等待不同数据截止日再次确认"
            if eligible and confirmations < min_confirmations
            else "样本外收益/夏普/交易数未同时达标"
            if not eligible else "达到晋升复核门槛"
        )
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
                avg_oos_return, avg_oos_sharpe, oos_trades, selected_windows,
                confirmations, last_data_end, status, reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stock_code, strategy_key, params_json, float(test_sharpe),
                avg_oos_return, avg_oos_sharpe, oos_trades, selected_windows,
                confirmations, data_end, status, reason, created_at, now,
            ),
        )

        current = self.get_best_params(stock_code, strategy_key)
        current_sharpe = float(current.get("sharpe", -999.0)) if current else -999.0
        current_source = str(current.get("source", "")) if current else ""
        improves_champion = test_sharpe >= current_sharpe + 0.15
        can_promote = eligible and confirmations >= min_confirmations and (
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
