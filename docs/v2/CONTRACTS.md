# TradeHelper V2-0/V2-1 合同规范

> 本文件是 V2-0 测试基础设施和 V2-1 数据层的规范性合同。实现者不得仅凭 `V2_REFACTOR_PLAN.md` 中的示例字段自行推断类型。发生冲突时，本文件与 [POLICIES.md](./POLICIES.md)、[GOLDEN_CASES.md](./GOLDEN_CASES.md) 优先于实施计划中的示例，V1 代码仅作为参考。

## 1. 本阶段边界

V2-0/V2-1 只允许实现：

```text
tradehelper_v2/contracts/
tradehelper_v2/config/
tradehelper_v2/data/
tests/v2/ 中对应测试与 fixture
```

不得提前实现预测、情景、策略、风控、成交仿真、组合决策、学习、LLM、报告或 UI。可以为后续合同保留文档，不得创建无行为的占位业务类。

V2 数据层必须可以在完全不导入 V1 业务模块的情况下运行。V1 外部 I/O 客户端若确实临时复用，只能由 `tradehelper_v2/data/compatibility.py` 单点导入，并在 `V2_REFACTOR_PLAN.md` 记录退出条件。V2-0/V2-1 默认目标是重新实现适配器，不启用 shim。

当前阶段建议文件边界固定为：

```text
tradehelper_v2/
  contracts/
    enums.py
    market_data.py
    account.py
    providers.py
    quality.py
  config/settings.py
  data/
    calendar.py
    cache.py
    quality.py
    repository.py
    service.py
    migrations/schema.py
    providers/
      base.py
      tickflow.py
      listing.py
      fundamentals.py
      news.py
      us_extended.py
      us_daily_fallback.py
```

允许把非常小且同属一个职责的模块合并，但不得把 Provider 网络调用、缓存、质量判断和 SQLite 读写重新塞进一个巨型 service 文件。任何偏离都要在阶段状态说明原因。

## 2. Python 与序列化约定

- Python 版本固定为 3.12。
- 合同使用标准库 `dataclass(frozen=True, slots=True)`、`Enum` 和类型注解，不新增 Pydantic 依赖。
- 市场价格、收益率、概率和指标使用有限 `float`；禁止 `NaN`、`inf` 和 `-inf`。
- 账户现金、权益、成本金额和持股数量使用 `Decimal`，从外部值转换时必须使用 `Decimal(str(value))`，禁止从二进制 `float` 直接构造。
- 比例统一使用小数：`0.25` 表示 25%，取值范围在合同中显式限定。
- 日期使用 `datetime.date`；时间戳使用带时区的 `datetime`，进入合同后统一转换为 UTC。
- JSON 中日期使用 `YYYY-MM-DD`，UTC 时间使用带 `Z` 的 ISO 8601，`Decimal` 使用字符串。
- 所有持久化对象必须带 `schema_version: int`，V2-1 初始值固定为 `1`。
- 所有集合序列化前按稳定键排序；哈希使用 UTF-8、字段名排序、无多余空格的 canonical JSON。

## 3. 基础枚举

实现以下字符串枚举，值不得自行改名：

```text
Market:             A / US
Exchange:           XSHG / XSHE / XBSE / XNYS / XNAS / UNKNOWN
DecisionMode:       pre / intraday / eod
TradingSession:     pre / regular / post / closed
AdjustmentMode:     front_adjusted
FreshnessStatus:    fresh / stale / future / missing_timestamp / not_required
ProviderStatus:     ok / empty / rate_limited / timeout / unavailable / invalid_payload
QualitySeverity:    block / warning / optional_missing / info
QualityStatus:      ok / watch / degraded / blocked
QualityAction:      normal / watch / reduce_position / block_new_entries
```

V2-1 正式日 K 只接受 `front_adjusted`。Provider 返回其他口径时必须先转换并记录转换来源；无法可靠转换时返回 `invalid_payload`，不得混入 canonical bars。

## 4. 标的合同

### 4.1 InstrumentId

```text
InstrumentId:
  code: str
  market: Market
  exchange: Exchange
```

不变量：

- A股代码为 6 位数字。
- `6/5/9` 开头默认 `XSHG`，`0/1/2/3` 开头默认 `XSHE`，`4/8` 开头默认 `XBSE`。
- 美股代码转大写，允许字母、数字、`.`、`-`、`^`，长度 1 至 16；无法确认交易所时使用 `UNKNOWN`。
- `market=A` 不允许美股代码，`market=US` 不允许 6 位纯数字 A股代码。
- 稳定键为 `market:exchange:code`，例如 `US:XNAS:AAPL`、`A:XSHG:600519`。

### 4.2 StockMetadata

```text
StockMetadata:
  instrument: InstrumentId
  name: str
  industry: str | None
  description: str | None
  listing_date: date | None
  source: str
  fetched_at: datetime
  schema_version: int = 1
```

`name` 不能为空。上市日期未获取到时必须为 `None`，不得用 `1900-01-01`、当前日期或空字符串伪造。

## 5. 行情合同

### 5.1 CanonicalBar

```text
CanonicalBar:
  instrument: InstrumentId
  trading_date: date
  open: float
  high: float
  low: float
  close: float
  volume: int
  adjustment_mode: AdjustmentMode
  source: str
  fetched_at: datetime
  corporate_action_version: str | None
  schema_version: int = 1
```

不变量：

- 四个价格均为正有限数。
- `high >= max(open, close)`，`low <= min(open, close)`，允许的相对浮点误差为 `1e-6`。
- `volume` 以股为单位，为非负整数；A股 Provider 返回“手”时适配器必须乘以 100。
- `trading_date` 必须是对应市场的正式交易日。
- 同一 `instrument + trading_date + adjustment_mode` 只能有一条正式记录。
- Bar 只代表已完成的正式交易日。盘中临时 OHLC、延伸时段价和未完成日线不得构造成 `CanonicalBar`。
- 单日涨跌幅很大不构成 OHLC 异常；价格跳变由数据质量层结合复权和公司行动单独判断。

### 5.2 QuoteSnapshot

```text
QuoteSnapshot:
  instrument: InstrumentId
  session: TradingSession
  price: float
  prev_close: float | None
  open: float | None
  high: float | None
  low: float | None
  volume: int | None
  bid: float | None
  ask: float | None
  observed_at: datetime
  fetched_at: datetime
  source: str
  freshness_status: FreshnessStatus
  schema_version: int = 1
```

不变量：

- `price` 为正有限数。
- Provider 未返回的字段使用 `None`，禁止填 `0`。
- `bid/ask` 同时存在时必须满足 `0 < bid <= ask`。
- `open/high/low` 同时存在时遵循 OHLC 关系；只有部分字段时不补造其余字段。
- `available_fields` 由非空字段派生，不作为可由 Provider 随意声明的输入。
- Quote 只能进入当前决策快照、独立报价缓存或审计事件，任何代码路径都不得把 Quote 写入正式日 K 表。

### 5.3 IntradayBar

V2-1 只定义合同和独立 repository，不要求接入长期分钟数据源：

```text
IntradayBar:
  instrument: InstrumentId
  observed_at: datetime
  session_date: date
  open/high/low/close: float
  volume: int | None
  source: str
  evidence_quality: str
  fetched_at: datetime
  schema_version: int = 1
```

分钟 K 必须写入独立表，禁止与 `CanonicalBar` 共表。`evidence_quality` 初始允许 `provider`、`supplemental`、`unknown`。

## 6. 新闻与基本面合同

### 6.1 NewsSnapshot

```text
NewsSnapshot:
  instrument: InstrumentId
  title: str
  source: str
  published_at: datetime
  available_at: datetime
  fetched_at: datetime
  content: str | None
  is_macro: bool
  finbert_label: positive / neutral / negative | None
  finbert_score: float | None
  relevance: float | None
  schema_version: int = 1
```

`available_at` 表示系统最早实际获得该新闻的时间，不得使用后来抓取的新闻回填过去。概率字段范围为 `[0, 1]`。新闻唯一键为 `instrument + published_at + normalized_title + source`。

### 6.2 FundamentalValue 与 FundamentalSnapshot

```text
FundamentalValue:
  value: float | str | None
  unit: str | None
  period_end: date | None
  published_at: datetime | None
  source: str

FundamentalSnapshot:
  instrument: InstrumentId
  fields: Mapping[str, FundamentalValue]
  available_at: datetime
  fetched_at: datetime
  provider: str
  quality_status: QualityStatus
  schema_version: int = 1
```

每个字段都必须保留自身来源和财报期间。LLM 不是 Provider，不得创建或补全 `FundamentalValue.value`。无法获取的数据保持缺失。

## 7. 账户合同

```text
PositionSnapshot:
  instrument: InstrumentId
  shares: Decimal
  cost_price: Decimal
  captured_at: datetime

AccountSnapshot:
  market: Market
  currency: USD / CNY
  cash: Decimal
  positions: tuple[PositionSnapshot, ...]
  captured_at: datetime
  schema_version: int = 1
```

不变量：

- A股账户币种固定为 CNY，美股账户币种固定为 USD。
- `cash/shares/cost_price` 不得为负；已清仓股票应删除持仓，不保存 `shares=0` 的活动持仓。
- `account_equity` 不作为用户输入字段，由同一批冻结价格计算：`cash + sum(shares * frozen_price)`。
- 无报价时不得用成本价冒充现值。该持仓估值状态为不完整，组合可执行计划必须降级。
- 真实账户为 0 时保持 0；任何层都不得回退到 100000 或其他虚构本金。
- V2-1 不合并 USD/CNY 账户；未提供可靠汇率时分别分析。

## 8. Provider 合同

```text
ProviderAttempt:
  provider: str
  status: ProviderStatus
  started_at: datetime
  finished_at: datetime
  error_code: str | None
  message: str | None

ProviderResult[T]:
  value: T | None
  status: ProviderStatus
  selected_source: str | None
  attempts: tuple[ProviderAttempt, ...]
  fallback_reason: str | None
  fetched_at: datetime
  retry_at: datetime | None
```

批量报价值使用：

```text
QuoteBatch:
  quotes: Mapping[InstrumentId, QuoteSnapshot]
  failures: Mapping[InstrumentId, ProviderStatus]
```

`status=ok` 时 `value` 必须存在；其他状态不得附带伪造值。fallback 成功时顶层状态为 `ok`，同时保留失败主源的 attempt 和 `fallback_reason`。

`status=ok` 时 `retry_at=None`。受限 Provider 可以在 `status=rate_limited` 时提供严格晚于 `fetched_at` 的 `retry_at`；调用方不得把这种状态解释为空数据。

组合正式日K刷新使用：

```text
DailyBarsRequest:
  instrument: InstrumentId
  requested_start: date
  requested_end: date
  listing_date: date | None

DailyBarsBatchResult:
  results: Mapping[InstrumentId, ProviderResult[tuple[CanonicalBar, ...]]]
  pending_retry_at: Mapping[InstrumentId, datetime]
  completed_at: datetime
```

`pending_retry_at` 只能包含 `rate_limited` 标的，表示下一轮可安全尝试的时间，不是“没有历史数据”。

Provider 适配器只负责原始返回到合同的转换，不负责指标、预测、策略、仓位或报告。

### 8.1 Provider Protocol

使用 `typing.Protocol` 定义以下接口；所有方法都返回 `ProviderResult`，不抛出可预期的网络/空结果异常到上层：

```text
MetadataProvider.fetch_metadata(instrument, as_of) -> ProviderResult[StockMetadata]
ListingProvider.fetch_listing_date(instrument, as_of) -> ProviderResult[date]
DailyBarProvider.fetch_daily_bars(instrument, start, end, as_of) -> ProviderResult[tuple[CanonicalBar, ...]]
QuoteProvider.fetch_quotes(instruments, mode, as_of) -> ProviderResult[QuoteBatch]
NewsProvider.fetch_news(instrument, start_at, end_at, as_of) -> ProviderResult[tuple[NewsSnapshot, ...]]
FundamentalProvider.fetch_fundamentals(instrument, as_of) -> ProviderResult[FundamentalSnapshot]
```

`as_of` 必须由调用方注入，Provider 内部不得直接读取系统当前时间。Quote 批量接口必须保留逐股结果与逐股失败，不能因一只失败丢弃整批成功结果。

### 8.2 DataService 与 Cache

数据路由、fallback 和缓存统一由 `DataService` 编排：

```text
DataService.get_metadata(instrument, as_of)
DataService.get_listing_date(instrument, as_of)
DataService.get_daily_bars(instrument, requested_start, requested_end, as_of)
DataService.get_daily_bars_batch(requests, as_of)
DataService.get_quotes(instruments, mode, as_of)
DataService.get_news(instrument, mode, as_of)
DataService.get_fundamentals(instrument, as_of)
DataService.build_account_snapshot(market, as_of)
```

`get_daily_bars_batch` 每轮最多消耗 10 次 TickFlow 单标的 **A股** 日K请求；超出部分A股必须进入 `pending_retry_at`。美股已完成日K使用 Nasdaq 历史主源，再按 yfinance、TickFlow 降级。`DataService` 只返回合同/ProviderResult，不生成 FeatureSnapshot、预测或交易计划。Tab1/Tab3 后续都调用这一公共服务，不调用彼此。

Cache 接口：

```text
CacheEntry[T]:
  value: T | None
  status: ProviderStatus
  cached_at: datetime
  expires_at: datetime
  source: str | None

DataCache.get(key, as_of) -> CacheEntry | None
DataCache.put(key, entry) -> None
```

`as_of < expires_at` 才算命中；`as_of == expires_at` 必须重新刷新。Cache key 至少包含 instrument、数据类型、DecisionMode、provider 和影响返回的查询窗口。

## 9. 数据质量合同

```text
DataQualityIssue:
  code: str
  severity: QualitySeverity
  field: str | None
  message: str
  source: str | None

DataCapabilities:
  daily_price: bool
  short_technical_20: bool
  medium_technical_60: bool
  ma120: bool
  realtime_price: bool
  intraday_ohlc: bool
  volume: bool
  bid_ask: bool
  news: bool
  fundamentals: bool

DataQualityReport:
  status: QualityStatus
  action: QualityAction
  score: float
  max_position_multiplier: float
  block_new_entries: bool
  issues: tuple[DataQualityIssue, ...]
  capabilities: DataCapabilities
  evaluated_at: datetime
  schema_version: int = 1
```

`DataQualityReport` 是数据事实，不生成买卖结论。能力按实际字段与样本计算，不得用一个总分声称所有策略均可用。具体评分和状态规则见 [POLICIES.md](./POLICIES.md)。

## 10. Repository 合同

实现以下边界，不允许 UI 或后续业务层直接执行 SQL：

```text
MarketDataRepository:
  upsert_daily_bars(bars)
  list_daily_bars(instrument, start, end, adjustment_mode)
  upsert_intraday_bars(bars)
  list_intraday_bars(instrument, start_at, end_at)
  save_quote_snapshot(quote)
  get_latest_quote(instrument, session)
  quarantine_daily_bars(instrument, before_date, reason)

ReferenceDataRepository:
  upsert_stock_metadata(metadata)
  get_stock_metadata(instrument)
  upsert_news(items)
  list_news_as_of(instrument, as_of)
  upsert_fundamental_snapshot(snapshot)
  get_fundamentals_as_of(instrument, as_of)

AccountRepository:
  save_account_snapshot(snapshot)
  get_latest_account_snapshot(market)
```

迁移预检合同：

```text
MigrationPreflight:
  source_path: str
  source_exists: bool
  source_schema_detected: bool
  table_counts: Mapping[str, int]
  migratable_counts: Mapping[str, int]
  conflict_counts: Mapping[str, int]
  warnings: tuple[str, ...]
  read_only: bool = True
  evaluated_at: datetime
  schema_version: int = 1
```

规则：

- V2 开发期数据库固定为 `{work_dir}/tradehelper_v2.db`，V1 `{work_dir}/tradehelper.db` 只读保留。
- V2-1 创建 `schema_migrations`；版本 `1`（基础事实表）、版本 `2`（Provider 配额事件、日K续跑任务、日K跨源漂移审计）、版本 `3`（新闻/基本面/元数据/上市日期续跑任务）和版本 `4`（队列任务身份去除状态字段、修正旧新闻可见时间）都必须幂等。
- V2-1 不执行完整 V1 数据导入；只提供只读探测和迁移预检。正式导入在 V2-12 完成。
- `Decimal` 在 SQLite 中使用 canonical 字符串 `TEXT` 保存，读取后恢复为 Decimal；不得以 REAL 保存账户金额和持股数量。
- 写操作使用事务；批量写任一合同校验失败时整批回滚。
- 日 K、分钟 K、报价快照必须物理分表。
- 上市日前的已有日 K 进入 quarantine，不得继续参与查询、训练或回测。
- cache 与正式事实表分离；过期 cache 不因存在于 SQLite 就自动成为当前事实。
- TickFlow A股日K配额事件与待续跑任务写入 V2 独立数据库。`rate_limited` 表示可恢复的供应商状态，不得写成空日K或质量失败。
- 新闻、基本面、元数据和上市日期被 Provider 限流时写入 `provider_refresh_queue`；下一安全窗口必须由数据层自动续跑，不能要求用户手工重跑整份分析。
- 同一任务完成后再次入队必须复用并重新激活原任务身份，不得因 `status` 变化制造重复任务或唯一键冲突；可恢复失败最多自动尝试 5 次，之后进入 `failed` 等待人工审计。
- 日K跨源比较仅写 `daily_bar_drift_records` 审计记录，不得由比较器直接覆盖正式 `daily_bars` 主序列。

## 11. 配置合同

V2 配置沿用跨平台标准目录，V2 开发期配置文件名为 `config_v2.json`。至少支持：

```text
work_dir
llm_base_url
llm_api_key
llm_model
stock_token_us
stock_token_a
news_token_us
news_token_a
finbert_model_path
llm_enable_thinking
```

V2-0/V2-1 的数据测试不要求 LLM 配置。数据层不得因为缺少 LLM Key 而拒绝行情、新闻或基本面测试。API Key、Token 和代理凭据不得写入日志、报告、测试快照或数据库审计事件。

## 12. 稳定性要求

- 合同校验错误使用明确异常类型，不返回静默默认值。
- 枚举、字段名、序列化格式和稳定键变更必须提升 `schema_version` 并提供迁移。
- 同一输入、冻结时钟和冻结 Provider 返回必须产生完全相同的 canonical JSON 与哈希。
- V2-0 必须用架构测试禁止非法 V1 import；V2-1 必须用 Golden Cases 验证合同，而不是根据实现反写预期结果。
