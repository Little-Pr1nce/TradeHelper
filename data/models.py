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
    listing_date: str = ""
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
class IntradayBar:
    """Immutable minute-level evidence, isolated from daily price history."""
    code: str
    market: str
    timestamp_ms: int
    session_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    source: str = ""
    fetched_at: str = ""
    quality_status: str = "supplemental"

    def to_dict(self) -> dict:
        return asdict(self)


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
    published_at: str = ""  # 数据源发布时间（ISO，尽量保留到秒）
    fetched_at: str = ""    # 本次从数据源获取时间（UTC ISO），用于缓存失效

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
    reference_date: str = ""           # 预测所依据的最后正式交易日
    direction: str = ""                # bullish / bearish / neutral
    final_score: float = 0.0           # 当时的 Final_Score
    predicted_price: float = 0.0       # 预测时的参考价格
    key_reason: str = ""               # 核心判断依据（50 字中文摘要）
    confidence: str = ""               # high / medium / low
    conservative_entry: float = 0.0    # 保守方案入场价
    aggressive_entry: float = 0.0      # 激进方案入场价
    entry_mode: str = "reference"       # reference/signal_price/next_open/conditional
    stop_loss: float = 0.0             # 止损价
    take_profit: float = 0.0            # 止盈价
    verify_after_days: int = 5         # 几个交易日后验证
    validated: int = 0                 # 是否已验证
    actual_return: float = 0.0         # 方向调整、扣估算成本后的建议收益
    underlying_return: float = 0.0     # 标的从评价基准到验证价的原始涨跌幅
    validation_price: float = 0.0      # 第 N 个交易日或风控退出的验证价格
    actual_entry_price: float = 0.0    # 验证时按口径得到的实际入场价
    actual_exit_type: str = ""         # stop_loss/take_profit/window_close/...
    actual_exit_date: str = ""         # 实际退出日；到期验证时等于验证截止日
    max_favorable_excursion: float = 0.0  # 验证窗口最大有利波动（方向调整）
    max_adverse_excursion: float = 0.0    # 验证窗口最大不利波动（负值）
    actual_direction: str = ""         # 实际方向（验证后填入）
    entry_triggered: int = 0           # 入场价是否触发
    verified_at: str = ""              # 验证时间
    validation_end_date: str = ""      # 实际使用的第 N 个交易日
    validation_status: str = "pending" # pending/verified/not_triggered/unsupported
    validation_version: int = 2        # 验证口径版本；v1 旧记录不参与健康度
    strategy_name: str = ""            # 策略标识（"A"/"B"/...），空字符串=整体预测
    signal_action: str = ""             # buy / sell；用于拆分进出场健康度
    market_regime: str = ""            # 预测时的行情状态（trending/ranging/...）
    portfolio_snapshot: str = ""       # 组合预测的现金与持仓 JSON 快照
    event_key: str = ""                # 同日同策略预测事件去重键
    exit_review_status: str = "not_applicable"  # pending/verified/not_applicable
    exit_return_1d: float = 0.0         # 卖出执行价到后续收盘的标的原始收益
    exit_return_3d: float = 0.0
    exit_return_5d: float = 0.0
    exit_return_10d: float = 0.0
    exit_return_20d: float = 0.0
    exit_max_decline: float = 0.0       # 卖出后20日内相对执行价最大下跌
    exit_max_rally: float = 0.0         # 卖出后20日内相对执行价最大上涨
    exit_avoided_loss: float = 0.0      # 20日口径避免损失，扣单边退出成本
    exit_opportunity_cost: float = 0.0  # 20日口径过早卖出的机会成本
    exit_quality: str = ""              # effective/premature/neutral

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
    strategy_sample_count: int = 0     # 去重前的股票×策略验证样本数
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


@dataclass
class ForecastResult:
    """独立市场预测；不从买卖动作反推，生成后不可改写预测字段。"""
    id: Optional[int] = None
    code: str = ""
    market: str = ""
    mode: str = "eod"
    generated_at: str = ""
    data_cutoff: str = ""
    target_session_date: str = ""
    horizon: int = 1
    reference_price: float = 0.0
    prob_up: float = 0.0
    prob_flat: float = 0.0
    prob_down: float = 0.0
    expected_return_p10: float = 0.0
    expected_return_p50: float = 0.0
    expected_return_p90: float = 0.0
    direction: str = "neutral"
    confidence: float = 0.0
    market_regime: str = "unknown"
    model_version: str = "analog_v1"
    feature_snapshot_hash: str = ""
    sample_count: int = 0
    calendar_source: str = ""
    event_key: str = ""
    status: str = "pending"       # pending / verified / unsupported
    actual_price: float = 0.0
    actual_return: float = 0.0
    actual_direction: str = ""
    correct: int = 0
    brier_score: float = 0.0
    interval_hit: int = 0
    verified_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ForecastResult":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)


@dataclass
class FeatureContextSnapshot:
    """Point-in-time news/fundamental context frozen at analysis delivery."""
    id: Optional[int] = None
    code: str = ""
    market: str = ""
    mode: str = "eod"
    captured_at: str = ""
    effective_date: str = ""
    news_score: float = 0.0
    news_count: int = 0
    news_latest_published_at: str = ""
    news_sources_json: str = "[]"
    fundamental_json: str = "{}"
    fundamental_source: str = ""
    quality_status: str = "empty"
    payload_hash: str = ""
    event_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ForecastModelVersion:
    """预测模型 Champion/Challenger 版本记录。"""
    id: Optional[int] = None
    stock_code: str = "*"
    market: str = ""
    horizon: int = 1
    version: str = ""
    status: str = "challenger"
    params_json: str = "{}"
    feature_set_json: str = "[]"
    train_start: str = ""
    train_end: str = ""
    sample_count: int = 0
    accuracy: float = 0.0
    brier_score: float = 0.0
    log_loss: float = 0.0
    calibration_error: float = 0.0
    baseline_brier: float = 0.0
    created_at: str = ""
    promoted_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JointOOFRun:
    """One immutable audit run of the combined forecast/strategy/risk policy."""
    id: Optional[int] = None
    code: str = ""
    market: str = ""
    data_start: str = ""
    data_end: str = ""
    policy_version: str = "joint_oof_v3_multimodel"
    samples: int = 0
    actionable_signals: int = 0
    forecast_gate_active: int = 0
    total_return: float = 0.0
    annual_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    forecast_brier: float = 0.0
    forecast_log_loss: float = 0.0
    forecast_ece: float = 0.0
    horizon_metrics_json: str = "{}"
    calibration_json: str = "[]"
    regime_metrics_json: str = "{}"
    fold_summaries_json: str = "[]"
    trace_json: str = "[]"
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradePlanLog:
    """交易方案及其结果；与ForecastResult分开评价。"""
    id: Optional[int] = None
    forecast_id: Optional[int] = None
    code: str = ""
    market: str = ""
    mode: str = "eod"
    created_at: str = ""
    reference_date: str = ""
    decision_session_date: str = ""
    signal_timestamp_ms: int = 0
    strategy_key: str = ""
    strategy_version: str = ""
    signal_intent: str = ""       # alpha_entry/alpha_exit/risk_exit/...
    action: str = "watch"
    execution_level: str = "C"
    trigger_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_pct: float = 0.0
    max_loss_amount: float = 0.0
    account_snapshot_json: str = "{}"
    status: str = "pending"
    entry_price: float = 0.0
    exit_price: float = 0.0
    net_return: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    opportunity_cost: float = 0.0
    outcome: str = ""
    evidence_sources: str = ""
    evidence_quality: str = ""
    evidence_bar_count: int = 0
    evaluated_at: str = ""
    event_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchObservationLog:
    """研究员/系统观察形态记录，用于风控官历史验证和自我升级。"""
    id: Optional[int] = None
    code: str = ""
    name: str = ""
    market: str = ""
    mode: str = "eod"
    report_id: Optional[int] = None
    observed_at: str = ""
    pattern_type: str = ""
    observation: str = ""
    source: str = ""
    system_status: str = ""
    execution_level: str = ""
    trigger_price: float = 0.0
    stop_loss: float = 0.0
    expected_direction: str = ""       # bullish / bearish / neutral
    llm_proposed: int = 0
    market_regime: str = ""
    event_key: str = ""                  # code|pattern|observed_day，用于事件去重
    trigger_operator: str = ""            # immediate/cross_above/cross_below
    entry_triggered: int = 0
    triggered_at: str = ""
    validation_status: str = "pending"    # pending/triggered/verified/not_triggered/unsupported
    validated: int = 0
    return_1d: float = 0.0
    return_3d: float = 0.0
    return_5d: float = 0.0
    return_10d: float = 0.0
    max_adverse_return: float = 0.0
    hit_take_profit: int = 0
    hit_stop_loss: int = 0
    verified_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchObservationLog":
        result = {}
        for name, field_def in cls.__dataclass_fields__.items():
            value = d.get(name)
            result[name] = value if value is not None else field_def.default
        return cls(**result)
