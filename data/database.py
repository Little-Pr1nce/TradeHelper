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
from datetime import datetime
from typing import Optional

from data.models import StockInfo, PriceData, AnalysisReport, NewsItem
from config.settings import Settings

logger = logging.getLogger(__name__)


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
"""


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

    def _connect(self, db_path: str):
        """
        建立 SQLite 连接并执行建表语句。

        - WAL 模式：提高并发读写性能
        - row_factory：查询结果以 Row 对象返回（支持字典转换）
        """
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")    # Write-Ahead Logging
        self._conn.execute("PRAGMA foreign_keys=ON")      # 启用外键约束
        self._conn.executescript(CREATE_TABLES_SQL)       # 自动建表
        self._conn.commit()
        logger.info(f"Database connected: {db_path}")

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接（懒加载：如已关闭则自动重连）。"""
        if self._conn is None:
            self._conn = sqlite3.connect(Settings().db_path)
        return self._conn

    def execute(self, sql: str, params=None):
        """执行 SQL 语句的快捷方法。"""
        return self.conn.execute(sql, params or ())

    def commit(self):
        """提交事务。"""
        self.conn.commit()

    def close(self):
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ======================== 股票信息 ========================

    def upsert_stock(self, stock: StockInfo):
        """
        插入或更新股票信息（使用 INSERT OR REPLACE）。

        Args:
            stock: StockInfo 实例
        """
        sql = """INSERT OR REPLACE INTO stocks (code, name, market, industry, description, update_time)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        self.execute(sql, (stock.code, stock.name, stock.market,
                           stock.industry, stock.description,
                           stock.update_time))
        self.commit()

    def get_stock(self, code: str) -> Optional[StockInfo]:
        """
        按代码查询股票信息。

        Args:
            code: 股票代码

        Returns:
            StockInfo 实例，不存在则返回 None
        """
        row = self.execute("SELECT * FROM stocks WHERE code = ?", (code,)).fetchone()
        if row:
            return StockInfo.from_dict(dict(row))
        return None

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
        self.conn.executemany(sql, data)
        self.commit()

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

    def get_latest_price_date(self, code: str) -> str:
        """
        获取某股票在数据库中的最新数据日期。

        用于判断是否需要从 API 增量拉取新数据。

        Args:
            code: 股票代码

        Returns:
            最新日期字符串，无数据则返回空字符串
        """
        row = self.execute(
            "SELECT MAX(date) FROM price_history WHERE code = ?", (code,)
        ).fetchone()
        return row[0] or ""

    def get_recent_prices(self, code: str, days: int = 30) -> list[PriceData]:
        """
        获取最近 N 天的股价数据（按时间升序返回）。

        Args:
            code: 股票代码
            days: 天数

        Returns:
            PriceData 列表（升序）
        """
        sql = """SELECT * FROM price_history WHERE code = ?
                 ORDER BY date DESC LIMIT ?"""
        rows = self.execute(sql, (code, days)).fetchall()
        prices = [PriceData.from_dict(dict(r)) for r in rows]
        prices.reverse()  # 降序 → 升序
        return prices

    # ======================== 分析报告 ========================

    def insert_report(self, report: AnalysisReport) -> int:
        """
        插入一条分析报告记录。

        Args:
            report: AnalysisReport 实例

        Returns:
            新记录的 ID（数据库自增主键）
        """
        sql = """INSERT INTO reports (code, name, market, backtest_period, create_time, content, pdf_path, rating, rated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        cursor = self.execute(sql, (
            report.code, report.name, report.market, report.backtest_period,
            report.create_time, report.content, report.pdf_path or "",
            report.rating, report.rated_at or ""
        ))
        self.commit()
        return cursor.lastrowid

    def get_report(self, report_id: int) -> Optional[AnalysisReport]:
        """
        按 ID 获取单条报告。

        Args:
            report_id: 报告 ID

        Returns:
            AnalysisReport 实例或 None
        """
        row = self.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row:
            return AnalysisReport.from_dict(dict(row))
        return None

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

    def get_reports_by_code(self, code: str) -> list[AnalysisReport]:
        """
        按股票代码筛选报告。

        Args:
            code: 股票代码

        Returns:
            该股票的所有报告（时间倒序）
        """
        rows = self.execute(
            "SELECT * FROM reports WHERE code = ? ORDER BY create_time DESC", (code,)
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
        self.execute(
            "UPDATE reports SET rating = ?, rated_at = ? WHERE id = ?",
            (rating, now, report_id)
        )
        self.commit()

    def update_report_pdf(self, report_id: int, pdf_path: str):
        """
        更新报告的 PDF 文件路径（PDF 导出后调用）。

        Args:
            report_id: 报告 ID
            pdf_path: PDF 文件的绝对路径
        """
        self.execute(
            "UPDATE reports SET pdf_path = ? WHERE id = ?",
            (pdf_path, report_id)
        )
        self.commit()

    def delete_report(self, report_id: int):
        """
        删除指定报告（注意：不会删除磁盘上的 PDF 文件）。

        Args:
            report_id: 报告 ID
        """
        self.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        self.commit()

    # ======================== 新闻情感 ========================

    def insert_news(self, news_list: list[NewsItem]):
        """
        批量插入新闻情感分析结果。

        Args:
            news_list: NewsItem 列表
        """
        if not news_list:
            return
        sql = """INSERT OR REPLACE INTO news_sentiment (code, date, title, source, sentiment, confidence)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        data = [(n.code, n.date, n.title, n.source, n.sentiment, n.confidence)
                for n in news_list]
        self.conn.executemany(sql, data)
        self.commit()

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
