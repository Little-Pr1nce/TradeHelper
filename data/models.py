"""
数据模型定义模块

使用 Python dataclass 定义应用中的核心数据结构：
  - StockInfo:   股票基本信息（代码、名称、市场、行业、简介）
  - PriceData:   单日股价数据（OHLCV）
  - AnalysisReport: 分析报告记录（含用户评分）
  - NewsItem:    新闻条目（含情感分析结果）

所有模型均提供 to_dict() / from_dict() 方法，
方便与 SQLite 数据库、JSON 序列化之间转换。

【扩展点】如需新增字段，直接在对应 dataclass 中声明即可，
to_dict/from_dict 通过 __dataclass_fields__ 自动适配。
"""

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class StockInfo:
    """
    股票基本信息。

    字段说明：
      - code:         股票代码（A 股 6 位数字 / 美股字母）
      - name:         股票名称（如"贵州茅台"、"Apple Inc."）
      - market:       市场标识（"A" / "US"）
      - industry:     所属行业（如"白酒"、"Technology"）
      - description:  公司简介或主营业务描述
      - update_time:  信息更新时间（ISO 格式）
    """
    code: str
    name: str
    market: str
    industry: str = ""
    description: str = ""
    update_time: str = ""

    def to_dict(self) -> dict:
        """转为字典，用于数据库插入和 JSON 序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StockInfo":
        """从字典（如数据库查询结果）构造 StockInfo 实例。"""
        return cls(**{k: d.get(k, "") for k in cls.__dataclass_fields__})


@dataclass
class PriceData:
    """
    单日股价数据（OHLCV 格式）。

    字段说明：
      - code:   股票代码
      - date:   交易日期（"YYYY-MM-DD"）
      - open:   开盘价
      - high:   最高价
      - low:    最低价
      - close:  收盘价
      - volume: 成交量（股）
    """
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PriceData":
        """从字典构造 PriceData（价格字段必须存在）。"""
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


@dataclass
class AnalysisReport:
    """
    分析报告记录。

    字段说明：
      - id:              数据库自增主键（新建时可为 None）
      - code:            股票代码
      - name:            股票名称
      - market:          市场标识
      - backtest_period: 回测周期（"3m"/"6m"/"1y"/"3y"）
      - create_time:     报告生成时间（ISO 格式）
      - content:         报告正文（Markdown 格式）
      - pdf_path:        PDF 文件路径（导出后填充）
      - rating:          用户评分（1-5，未评分时为 None）
      - rated_at:        评分时间

    【扩展点】rating 字段为后续策略优化提供反馈数据。
    后续可根据评分高的报告提取特征，用于优化策略参数权重。
    """
    id: Optional[int] = None
    code: str = ""
    name: str = ""
    market: str = ""
    backtest_period: str = ""
    create_time: str = ""
    content: str = ""
    chart_path: str = ""
    pdf_path: str = ""
    rating: Optional[int] = None
    rated_at: str = ""
    mode: str = "eod"              # 分析模式: "eod" / "intraday" / "pre"
    prediction_data: str = ""      # JSON 格式的结构化预测数据（盘前报告使用）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisReport":
        # 缺失字段使用 dataclass 自身的默认值，避免字符串字段被回填成 None
        result = {}
        for name, field in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field.default
        return cls(**result)


@dataclass
class NewsItem:
    """
    新闻条目（含情感分析结果）。

    字段说明：
      - code:       关联股票代码
      - date:       新闻发布日期
      - title:      新闻标题
      - source:     新闻来源（如"东方财富"、"Reuters"）
      - content:    新闻正文摘要（LLM 返回，用于情感分析）
      - sentiment:  情感分类结果（"positive"/"negative"/"neutral"，分析后填充）
      - confidence: 情感分类置信度（0.0-1.0，分析后填充）
    """
    code: str
    date: str
    title: str
    source: str = ""
    content: str = ""
    sentiment: str = ""
    confidence: float = 0.0
    is_macro: bool = False  # 是否为宏观新闻（非个股新闻）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsItem":
        # 缺失字段统一使用 dataclass 自身的默认值
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class Holding:
    """用户持仓（我的持仓页面）。"""
    id: Optional[int] = None
    code: str = ""
    name: str = ""
    market: str = "US"
    shares: float = 0.0
    cost_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Holding":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class WatchItem:
    """关注股票（我的持仓页面）。"""
    id: Optional[int] = None
    code: str = ""
    name: str = ""
    market: str = "US"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WatchItem":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class AccountBalance:
    """账户余额（单条记录，id=1）。"""
    id: Optional[int] = None
    us_balance: float = 0.0
    a_balance: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AccountBalance":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class PredictionLog:
    """预测追踪记录 — 每次分析的核心判断结构化存储，用于后续验证。"""
    id: Optional[int] = None
    code: str = ""                     # 股票代码（组合用 "PORTFOLIO_US" / "PORTFOLIO_A"）
    market: str = ""                   # US / A
    mode: str = "eod"                  # eod / intraday / pre / portfolio
    report_id: Optional[int] = None    # 关联 reports.id
    predict_time: str = ""             # 预测生成时间 ISO 格式
    direction: str = ""                # bullish / bearish / neutral
    final_score: float = 0.0           # 当时的 Final_Score
    predicted_price: float = 0.0       # 预测时的参考价格
    key_reason: str = ""               # 核心判断依据（50 字中文摘要）
    confidence: str = ""               # high / medium / low
    conservative_entry: float = 0.0    # 保守方案入场价
    aggressive_entry: float = 0.0      # 激进方案入场价
    stop_loss: float = 0.0             # 止损价
    verify_after_days: int = 5         # 几个交易日后验证
    validated: int = 0                 # 是否已验证
    actual_return: float = 0.0         # 实际收益%（验证后填入）
    actual_direction: str = ""         # 实际方向（验证后填入）
    entry_triggered: int = 0           # 入场价是否触发
    verified_at: str = ""              # 验证时间
    strategy_name: str = ""            # 策略标识（"A"/"B"/...），空字符串=整体预测
    market_regime: str = ""            # 预测时的行情状态（trending/ranging/...）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PredictionLog":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class PredictionStats:
    """从 prediction_log 聚合的预测绩效统计。"""
    code: str = ""                     # 股票代码或组合标识
    total_predictions: int = 0         # 累计预测次数
    direction_accuracy_10: float = 0.0 # 近 10 次方向正确率
    direction_accuracy_all: float = 0.0# 全部历史方向正确率
    avg_predicted_return: float = 0.0  # 平均预测收益
    accuracy_trend: str = "stable"     # improving / stable / declining
    status: str = "reliable"           # reliable / unstable / unreliable
    updated_at: str = ""               # 计算时间

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PredictionStats":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)
