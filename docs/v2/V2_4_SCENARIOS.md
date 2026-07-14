# TradeHelper V2-4 情景层规范

> 状态：设计完成，待实现。本文档是 V2-4 的规范性合同和 Golden Cases。实现者不得根据实现结果反向修改标准答案。发生冲突时，本文件优先于 `V2_REFACTOR_PLAN.md` 中的概念示例；V2-0/V2-1 数据合同、V2-2 特征合同和 V2-3 预测合同继续有效。

## 1. 阶段目标

V2-4 只负责把独立预测事实翻译为结构化交易环境，回答：

1. 未来 1/3 日的战术方向与 5/10 日的波段方向是否一致？
2. 当前价格相对预测发行价和预测收益区间处于什么位置？
3. 预测发行后是否出现了模型当时没有看到的新事实？
4. 哪些策略家族与当前环境相容，哪些应被阻止？
5. 当前情景适用于哪个正式交易会话，证据能支持到什么程度？

V2-4 不负责：

- 不生成 buy/add/reduce/sell/hold 动作。
- 不计算触发价、止损、止盈、仓位、最大亏损或风险收益比。
- 不读取账户余额、持仓数量、成本价或组合集中度。
- 不执行回测、成交仿真、策略选型、风控评级或自动优化。
- 不调用 Provider，不刷新行情、新闻或基本面，不调用 LLM。
- 不修改 V2-3 概率，不把当前价格重新塞回 EOD 预测模型。
- 不实现 Tab1、Tab3、报告或 UI。

情景层是纯确定性翻译器：相同冻结输入、政策版本和交易日历必须得到相同 `TradingScenario`。

## 2. 架构边界

```text
V2-1 facts
  -> V2-2 origin EOD FeatureSnapshot
    -> V2-3 ForecastResult[1/3/5/10]

V2-1 current facts
  -> V2-2 current FeatureSnapshot

ForecastResult bundle + origin/current snapshots + quality + decision session
  -> V2-4 ScenarioPlanner
    -> TradingScenario
      -> V2-5 StrategyEngine
```

三个重要边界：

1. `ForecastResult` 是天气预报，`TradingScenario` 是天气对行军环境的解释，`TradePlan` 才是行军方案。
2. 情景可以允许或阻止某类策略，但不能生成具体交易动作。
3. Tab1 和 Tab3 对同一标的、同一时点必须复用同一 `TradingScenario`；组合排序和账户约束留给 V2-8。

## 3. 代码位置和依赖纪律

V2-4 实现只允许新增或修改：

```text
tradehelper_v2/contracts/scenario.py
tradehelper_v2/scenario/__init__.py
tradehelper_v2/scenario/planner.py
tradehelper_v2/scenario/policy.py
tradehelper_v2/scenario/facts.py
tradehelper_v2/data/calendar.py          # 仅扩展正式会话窗口合同
tradehelper_v2/data/migrations/schema.py # migration 8
tradehelper_v2/data/repository.py        # TradingScenario 幂等读写
tests/v2/test_scenario_*.py
```

禁止导入：

```text
V1 core/services/strategies/backtest/report/ui
tradehelper_v2/strategies
tradehelper_v2/risk
tradehelper_v2/execution
tradehelper_v2/portfolio
tradehelper_v2/learning
LLM clients
```

情景层可以依赖 V2 contracts、V2-2 FeatureSnapshot、V2-3 ForecastResult 和交易日历，不得形成反向依赖。

## 4. 枚举合同

```text
ScenarioBias:
  bullish
  bearish
  range
  uncertain

HorizonSignal:
  bullish
  bearish
  range
  weak
  unavailable

BandSignal:
  bullish
  bearish
  range
  uncertain
  conflict
  unavailable

HorizonAlignment:
  aligned
  mixed
  partial
  conflict
  unavailable

ScenarioState:
  bullish_continuation
  bullish_pullback
  bearish_continuation
  bearish_rebound
  range_bound
  mixed
  forecast_conflict
  uncertain

ForecastEvidenceGrade:
  stock_confirmed
  stock_observation
  cross_stock_observation
  baseline_observation
  unavailable

ForecastSupportLevel:
  confirmed
  partial
  observational
  unavailable

ScenarioStatus:
  ready
  degraded
  observation_only
  blocked

PriceLocation:
  below_p10
  inside_interval
  above_p90
  unavailable

CurrentPriceState:
  reference_close
  fresh_quote
  expected_missing
  stale_or_missing

NewsDeltaState:
  positive
  negative
  unchanged
  unavailable

EntryPosture:
  follow_trend
  wait_pullback
  wait_confirmation
  range_extremes_only
  countertrend_confirmation
  observation_only
  blocked

ExitPosture:
  standard
  tighten_protection
  prioritize_protection

StrategyFamily:
  trend_continuation
  breakout_confirmation
  pullback_entry
  support_rebound
  range_mean_reversion
  protective_exit
  profit_lock
  failed_rebound_exit
  observation

ScenarioFactKind:
  news
  fundamental
```

V2 初始只支持多头持股和现金管理，不在情景层引入做空策略家族。

## 5. 输入合同

### 5.1 DecisionSession

```text
DecisionSession:
  market: Market
  exchange: Exchange
  session_date: date
  regular_open: datetime
  regular_close: datetime
  breaks: tuple[(datetime, datetime), ...]
  source: str
  schema_version: int = 1
```

不变量：

- 所有时间必须带时区并保存为 UTC；`regular_open < regular_close`。
- breaks 必须排序、互不重叠并完全位于 open/close 之间。
- 会话必须来自交易所日历；不得以工作日加减或硬编码 09:30/16:00 代替。
- 美股半日市和夏令时必须使用日历真实时间。
- A股午间休市作为 break 保存；后续策略/成交层不得在 break 中声明可即时成交。

交易日历协议在 V2-4 增加：

```text
session_window(market, exchange, session_date) -> DecisionSession
next_session(market, exchange, after_date) -> date
session_containing(market, exchange, as_of) -> DecisionSession | None
```

测试日历必须显式注入窗口，不能让 Golden Cases 依赖真实网络或机器日期。

### 5.2 ScenarioFactUpdate

```text
ScenarioFactUpdate:
  kind: ScenarioFactKind
  stable_key: str
  available_at: datetime
  source: str
  payload_hash: str
  affected_features: tuple[str, ...]
  schema_version: int = 1
```

该合同只证明“预测发行后出现了一条新事实”，不判断利多或利空：

- news 的 stable_key 使用 V2-1 NewsSnapshot.stable_key，affected_features 只能在 `news.*`。
- fundamental 每个变化的 canonical field 生成一条 update；stable_key 必须绑定 provider、canonical field、period/published identity，affected_features 恰好包含对应一个 `fund.*`。
- available_at 必须满足 `origin_snapshot.cutoff_at < available_at <= request.as_of`。
- payload_hash 必须是对应 canonical V2 事实 payload 的 SHA-256，不接受自然语言摘要。
- affected_features 按名称排序且唯一，必须至少包含一个受影响的已注册特征。
- updates 按 `(available_at, kind, stable_key)` 排序且 `(kind, stable_key)` 唯一。
- 调用方必须从 V2 repository 做点时查询；Planner 不联网，也不通过特征值变化猜测是否出现新事实。

V2-4 提供纯函数装配器，但不自行访问 repository：

```text
build_fact_updates(
  origin_cutoff,
  as_of,
  visible_news,
  origin_fundamentals,
  current_fundamentals,
) -> tuple[ScenarioFactUpdate, ...]
```

- visible_news 由 `list_news_as_of(as_of)` 提供，只选 `available_at > origin_cutoff` 的记录。
- origin/current fundamentals 分别由两个明确 as_of 查询取得；只有 current.available_at > origin_cutoff 且 canonical payload 与 origin 不同才生成更新。
- affected_features 根据真实变化的已注册 canonical 字段逐字段生成，不能把整份财报无差别标成所有 fund 特征变化。
- 装配器只做点时差集和哈希，不解释方向，不产生 ScenarioState。

### 5.3 ScenarioRequest

```text
ScenarioRequest:
  instrument: InstrumentId
  mode: DecisionMode                    # pre / intraday / eod
  as_of: datetime
  origin_snapshot: FeatureSnapshot      # V2-3 使用的 EOD 快照
  current_snapshot: FeatureSnapshot     # 当前模式快照
  current_quote: QuoteSnapshot | None
  fact_updates: tuple[ScenarioFactUpdate, ...]
  forecasts: tuple[ForecastResult, ...] # 必须正好包含 1/3/5/10 日
  data_quality: DataQualityReport
  decision_session: DecisionSession | None
  policy_version: str = "scenario_policy_v1"
  schema_version: int = 1
```

输入不变量：

1. instrument、market、exchange 在所有输入中完全一致。
2. `origin_snapshot.mode=eod`，且 latest_bar_date 等于所有 ForecastResult 的 origin。
3. forecasts 必须正好一条对应 1/3/5/10 日，horizon 不可重复或缺失；不可用也必须由明确的 ForecastResult 表示。
4. 所有 forecast 的 reference_price、origin、feature_set_version 必须一致；每条 `model_input_hash` 必须能由 origin snapshot 和该结果的 feature_set 重算得到。
5. `current_snapshot.mode == request.mode`、feature_set_version 与 origin 一致，cutoff 不晚于 as_of，也不能早于 origin snapshot cutoff。
6. pre/intraday 的 closed technical 语义指纹必须与 origin snapshot 一致；指纹包含 name/value/status/unit/lookback/sources/model_eligible/reason，但排除随快照 cutoff 更新的 available_at。新闻、基本面和 current facts 可以更新。
7. eod 的 current snapshot 必须与新 origin snapshot 具有相同 feature_hash，且 quote_observed_at/current_quote 均为 None；不允许携带未完成日K或延伸时段实时价。
8. current_snapshot.quote_observed_at 非空时 current_quote 必须存在；current_quote 存在时必须匹配 instrument 和 quote_observed_at，`current.price` available 时必须与 quote.price 一致。Planner 仍须相对 as_of 重算 freshness，过期后不得继续使用 snapshot 中旧的 current facts。
9. data_quality.evaluated_at 不得晚于 as_of；其 canonical payload 进入 scenario 身份，调用方不得在构建过程中替换报告。
10. 任一 ForecastResult 有明确 h1 target 时，decision_session 必须存在且 session_date 等于 h1 target；只有四周期均为 calendar_unavailable 时才允许为 None。
11. fact_updates 必须满足 5.2 的点时窗口和排序；同一 update 不能重复输入。

合同冲突直接抛 `ContractViolation`，不能通过输出 uncertain 掩盖。真实缺失必须通过 ForecastAvailability、FeatureStatus、quote freshness 和 DataQualityReport 表达，此时仍输出完整情景。

## 6. 输出合同

### 6.1 HorizonAssessment

```text
HorizonAssessment:
  horizon: int
  target_session_date: date | None
  forecast_event_key: str
  evidence_grade: ForecastEvidenceGrade
  signal: HorizonSignal
  probabilities: DirectionProbabilities | None
  original_distribution: ReturnDistribution | None
  remaining_distribution: ReturnDistribution | None
  confidence_margin: float | None
  price_location: PriceLocation
  reason_codes: tuple[str, ...]
```

`remaining_distribution` 只是在存在新鲜当前价时，把原预测收益转换为“从当前价到目标日的剩余收益区间”；它不能覆盖原预测结果。

### 6.2 CurrentOverlay

```text
CurrentOverlay:
  price_state: CurrentPriceState
  current_price: float | None
  price_source: str | None
  observed_at: datetime | None
  realized_return_from_origin: float | None
  tactical_price_location: PriceLocation
  volatility_shock: bullish / bearish / none / unavailable
  news_delta: NewsDeltaState
  news_update_present: bool
  fundamental_update_present: bool
  fact_update_count: int
  unmodeled_fact_update: bool
  reason_codes: tuple[str, ...]
```

`unmodeled_fact_update=True` 表示预测发行后出现了新闻或基本面更新；它不代表更新一定利多或利空，只表示旧预测没有看到该事实。

### 6.3 TradingScenario

```text
TradingScenario:
  scenario_id: str
  event_key: str
  instrument: InstrumentId
  mode: DecisionMode
  as_of: datetime
  origin_session_date: date
  decision_session: DecisionSession | None
  valid_from: datetime | None
  expires_at: datetime | None
  bias: ScenarioBias
  tactical_signal: BandSignal           # 1/3 日
  swing_signal: BandSignal              # 5/10 日
  alignment: HorizonAlignment
  state: ScenarioState
  forecast_support: ForecastSupportLevel
  status: ScenarioStatus
  horizon_assessments: tuple[HorizonAssessment, ...]
  current_overlay: CurrentOverlay
  allowed_strategy_families: tuple[StrategyFamily, ...]
  blocked_strategy_families: tuple[StrategyFamily, ...]
  entry_posture: EntryPosture
  exit_posture: ExitPosture
  reason_codes: tuple[str, ...]
  forecast_bundle_hash: str
  current_feature_hash: str
  fact_update_hash: str
  quality_hash: str
  policy_version: str
  generated_at: datetime
  schema_version: int = 1
```

输出不变量：

- horizon assessments 固定按 1/3/5/10 排序。
- decision_session 存在时必须有 `regular_open <= valid_from < expires_at <= regular_close`；日历不可用时三者都为 None。
- allowed 与 blocked 互斥，且二者并集必须覆盖全部 StrategyFamily。
- `protective_exit`、`profit_lock`、`failed_rebound_exit`、`observation` 永远在 allowed 中；预测不能阻止持仓保护。
- `status=blocked` 只表示禁止新开仓情景，不得删除保护性退出家族。
- `scenario_id/event_key` 不包含 generated_at；相同冻结输入必须幂等。
- 不允许出现 action、trigger_price、stop_loss、take_profit、position_pct、shares、cash 或 max_loss_amount 字段。
- reason_codes 使用稳定大写代码并排序去重，不保存随意自然语言作为决策依据。
- allowed/blocked strategy families 按 StrategyFamily 声明顺序排列，不能依赖 set/dict 迭代顺序。
- `unmodeled_fact_update == (news_update_present or fundamental_update_present)`，fact_update_count 必须等于去重后的 updates 数量。

### 6.4 reason code 初始白名单

```text
FORECAST_STOCK_CONFIRMED
FORECAST_STOCK_OBSERVATION
FORECAST_CROSS_STOCK_OBSERVATION
FORECAST_BASELINE_OBSERVATION
FORECAST_INSUFFICIENT_SAMPLE
FORECAST_DATA_BLOCKED
FORECAST_CALENDAR_UNAVAILABLE
FORECAST_NO_ELIGIBLE_MODEL
FORECAST_MARGIN_WEAK
PROBABILITY_DISTRIBUTION_NOT_ALIGNED
TACTICAL_SWING_ALIGNED
TACTICAL_SWING_MIXED
HORIZON_COVERAGE_PARTIAL
HORIZON_CONFLICT
CURRENT_PRICE_REFERENCE_CLOSE
CURRENT_PRICE_FRESH_QUOTE
CURRENT_PRICE_EXPECTED_MISSING
CURRENT_PRICE_STALE_OR_MISSING
PRICE_ABOVE_P90
PRICE_BELOW_P10
BULLISH_VOLATILITY_SHOCK
BEARISH_VOLATILITY_SHOCK
NEWS_DELTA_POSITIVE
NEWS_DELTA_NEGATIVE
NEWS_DELTA_UNAVAILABLE
NEWS_UPDATE_PRESENT
FUNDAMENTAL_UPDATE_PRESENT
UNMODELED_FACT_UPDATE
DATA_QUALITY_DEGRADED
DATA_QUALITY_BLOCKED
ENTRY_WAIT_PULLBACK
ENTRY_WAIT_CONFIRMATION
ENTRY_OBSERVATION_ONLY
ENTRY_BLOCKED
PROTECTIVE_EXIT_PRESERVED
```

V2-4 初始实现只能使用本白名单。新增 code 必须先更新规范和对应 Golden Case，不能把动态价格、股票代码或 Provider 错误文本拼入 code。

## 7. 单周期解释规则

### 7.1 证据等级

| ForecastResult（按本表从上到下匹配） | evidence_grade |
|---|---|
| available + empirical family 或 baseline scope | baseline_observation |
| available + stock scope + execution_eligible | stock_confirmed |
| available + stock scope + 非 execution eligible | stock_observation |
| available + industry/market scope | cross_stock_observation |
| 其他 availability | unavailable |

行业/市场 Champion 即使本身通过 confirmation，也只能作为跨股票观察证据，不能升级为 `stock_confirmed`。

### 7.2 方向解释

冻结常量：

```text
DIRECTIONAL_MARGIN = 0.10
RANGE_MARGIN = 0.05
NEWS_DELTA_THRESHOLD = 0.15
DEFAULT_SHOCK_FLOOR = 0.03
ATR_SHOCK_MULTIPLIER = 2.0
INTRADAY_QUOTE_MAX_AGE_MINUTES = 15
PRE_QUOTE_MAX_AGE_MINUTES = 45
QUOTE_FUTURE_TOLERANCE_MINUTES = 5
```

按以下顺序解释每个 ForecastResult：

1. availability 非 available -> unavailable。
2. bullish 且 confidence_margin >= 0.10 且 p50 > 0 -> bullish。
3. bearish 且 confidence_margin >= 0.10 且 p50 < 0 -> bearish。
4. neutral 且 confidence_margin >= 0.05 且 `p10 <= 0 <= p90` -> range。
5. 其余 -> weak，并记录 `PROBABILITY_DISTRIBUTION_NOT_ALIGNED` 或 `FORECAST_MARGIN_WEAK`。

不能只看最大概率，也不能只看收益中位数；概率方向和收益分布必须一致。

## 8. 多周期组合规则

### 8.1 两条时间轴

```text
tactical band = 1日 + 3日
swing band = 5日 + 10日
```

每个 band 的确定性归并：

1. bullish 与 bearish 同时出现 -> conflict。
2. 至少一个 directional，另一个同向/weak/unavailable/range -> directional；证据覆盖记为 partial。
3. 没有 directional，但至少一个 range -> range。
4. 只有 weak -> uncertain。
5. 全部 unavailable -> unavailable。

不能用加权平均把 1日 bearish 和 10日 bullish 抹成 neutral。

### 8.2 alignment

| tactical | swing | alignment |
|---|---|---|
| 任一 band=conflict | 任意 | conflict |
| 两者同为 bullish/bearish/range | 同向 | aligned |
| 两者均可用但方向不同 | 不同 | mixed |
| 一方有效、另一方 uncertain/unavailable | - | partial |
| 两者都 uncertain/unavailable | - | unavailable |

短期 bearish、波段 bullish 是 `mixed`，不是错误；它表示上涨结构中的短期回撤。短期 bullish、波段 bearish 表示下跌结构中的反弹。

### 8.3 bias 与 state

1. alignment=conflict -> bias=uncertain，state=forecast_conflict。
2. swing 为 bullish/bearish 时，bias 优先跟随 swing；波段结构不能被单日噪声覆盖。
3. swing=range 时 bias=range；tactical directional 只形成 mixed 压力，不能把波段震荡改写成趋势。
4. swing uncertain/unavailable 而 tactical 为 directional 时，bias 跟随 tactical，但 alignment=partial。
5. 没有 directional 且至少一个 band=range -> bias=range。
6. 其余 -> bias=uncertain。

状态表：

| tactical | swing | state |
|---|---|---|
| bullish | bullish | bullish_continuation |
| bearish | bullish | bullish_pullback |
| bearish | bearish | bearish_continuation |
| bullish | bearish | bearish_rebound |
| range | range/uncertain/unavailable | range_bound |
| 任一 conflict | 任意 | forecast_conflict |
| 其他不同但非冲突 | - | mixed |
| 无有效方向或区间 | - | uncertain |

## 9. 当前价格和新增事实覆盖层

### 9.1 当前价格来源

- eod：使用 ForecastResult.reference_price，price_state=reference_close。
- 美股 pre：只接受新鲜 Nasdaq.com -> yfinance 延伸时段 quote；缺失或陈旧时为 stale_or_missing。
- A股 pre：没有认可连续盘前价属于 expected_missing，不伪造报价，也不因“预期缺失”破坏预测证据。
- A/美股 intraday：只接受新鲜 regular TickFlow quote；缺失或陈旧时禁止当前即时开仓情景。
- quote 不得写入正式日K，也不得改变 V2-3 event_key。
- Planner 必须相对 ScenarioRequest.as_of 再次计算 quote 年龄：intraday 最多15分钟、pre 最多45分钟，未来容忍最多5分钟；不能沿用相对于旧 snapshot cutoff 的 fresh 标记。
- intraday quote.session 必须为 regular；pre quote.session 必须为 pre。市场/时段不匹配的 quote 按 stale_or_missing 处理并记录质量 reason，不得跨时段冒用。

### 9.2 剩余收益区间

设原预测参考价为 `P0`、当前新鲜价为 `Pc`、原收益分位为 `rq`：

```text
realized = Pc / P0 - 1
remaining_q = (1 + rq) / (1 + realized) - 1
```

三个分位分别转换，保持顺序，不做线性相减；ReturnDistribution.method 保留原预测方法，HorizonAssessment 和 CurrentOverlay 已明确其当前价基准。eod 的 reference close 视为可用当前价，此时 remaining distribution 等于 original；pre/intraday 只有 fresh quote 才计算，expected_missing/stale_or_missing 时保持 None。

例：P0=100、Pc=105、原区间 `[-4%, 3%, 8%]`，剩余区间必须为约 `[-8.5714%, -1.9048%, 2.8571%]`。

`price_location` 仍按当前已实现收益与原 p10/p90 比较：小于 p10 为 below_p10，大于 p90 为 above_p90，否则 inside_interval。CurrentOverlay 的 tactical_price_location 优先使用 h1 assessment；h1 不可用时使用 h3，两者都不可用时为 unavailable。

### 9.3 波动冲击

```text
shock_threshold = max(2 * closed.atr_pct_14, 0.03)
```

ATR 缺失时只使用 3% floor。realized 大于等于 threshold 为 bullish shock，小于等于负 threshold 为 bearish shock，否则 none。该状态只描述价格偏离，不等于突破成功、止损触发或可交易信号。

### 9.4 新闻和基本面增量

- origin/current 的 `news.sentiment_weighted_1d` 都 available 时，差值 >=0.15 为 positive，<=-0.15 为 negative，否则 unchanged。
- 任一侧缺失时为 unavailable，不能把缺失当中性。
- `news_update_present` 和 `fundamental_update_present` 只由 ScenarioFactUpdate.kind 决定，不能通过两次 FeatureValue 的 value/status/available_at 差异猜测。
- news 特征随时间衰减、latest_age_hours 自然增长或滚动窗口移出，不构成新事实；没有 ScenarioFactUpdate 时不得因此设置 unmodeled update。
- 任一显式 news/fundamental update 存在时，`unmodeled_fact_update=True`，因为旧 ForecastResult 没有看到该事实。
- V2-4 不判断新财报“好/坏”，也不调用 LLM解释；V2-5 可要求重新确认条件。

## 10. 预测支持和降级

### 10.1 ForecastSupportLevel

```text
confirmed:
  至少两个 stock_confirmed horizon；
  tactical 至少一个且 swing 至少一个；
  alignment 不是 conflict/unavailable。

partial:
  至少一个 stock_confirmed horizon；
  但未同时满足 tactical 与 swing 的 confirmed 覆盖要求。

observational:
  没有 stock_confirmed；
  但至少一个 stock/cross-stock/baseline observation 可用。

unavailable:
  四个 horizon 全部不可用。
```

### 10.2 ScenarioStatus

按最严格规则决定：

1. decision_session 缺失、quality status=blocked 或 block_new_entries=True -> blocked。
2. intraday 缺少新鲜 current price -> blocked。
3. alignment=conflict 或 forecast_support=observational/unavailable -> observation_only。
4. forecast_support=partial、unmodeled_fact_update、当前价在 p10/p90 外、质量 degraded -> degraded。
5. 其他 confirmed 情景 -> ready。

`blocked` 和 `observation_only` 都只限制新开仓策略；保护性退出永远保留。

## 11. 策略家族政策

基础映射：

| ScenarioState | 允许的新开仓策略家族 | entry_posture | exit_posture |
|---|---|---|---|
| bullish_continuation | trend_continuation, breakout_confirmation, pullback_entry, support_rebound | follow_trend | standard |
| bullish_pullback | pullback_entry, support_rebound | wait_confirmation | tighten_protection |
| bearish_continuation | 无 | blocked | prioritize_protection |
| bearish_rebound | support_rebound | countertrend_confirmation | prioritize_protection |
| range_bound | range_mean_reversion, support_rebound, breakout_confirmation | range_extremes_only | standard |
| mixed | pullback_entry, support_rebound | wait_confirmation | tighten_protection |
| forecast_conflict | 无 | observation_only | tighten_protection |
| uncertain | 无 | observation_only | tighten_protection |

覆盖规则按顺序应用：

1. tactical price_location=above_p90：移除 trend_continuation 和 breakout_confirmation，entry_posture=wait_pullback。
2. bullish 情景出现 below_p10 或 bearish shock：移除所有趋势型新开仓，entry_posture=wait_confirmation，exit_posture 至少 tighten_protection。
3. bearish 情景出现 above_p90 或 bullish shock：仍不能升级为趋势做多，只可保留 countertrend confirmation。
4. unmodeled_fact_update：status 至少 degraded，entry_posture 至少 wait_confirmation。
5. price_state=expected_missing：预测支持不降级，但 entry_posture 至少 wait_confirmation；这是 A股盘前的正常能力边界。
6. pre 下 price_state=stale_or_missing：status 至少 degraded，entry_posture 至少 wait_confirmation。
7. status=observation_only/blocked：移除所有新开仓家族，只保留 observation 和退出家族。
8. protective_exit、profit_lock、failed_rebound_exit 始终允许；其中 protective_exit 和 profit_lock 不得被任何预测规则删除。

情景层只声明“家族兼容性”。V2-5 必须再次用具体形态事实判断是否生成 TradePlan，不能因为家族 allowed 就直接买入。

## 12. 三时段规则

### 12.1 pre

- origin 为最近完成交易日 T-1，复用其 V2-3 ForecastResult。
- decision session 是 as_of 之后第一个正式常规交易会话。
- valid_from=regular_open，expires_at=regular_close。
- 美股可使用延伸时段 quote 计算 remaining distribution；A股无连续盘前价时保持 expected_missing。
- 盘前新增新闻/财报形成 unmodeled update，但不改写预测概率。

### 12.2 intraday

- origin 仍是 T-1，不用未完成当日K重发预测。
- decision session 必须是包含 as_of 的正式会话；午间 break 仍属于同一 session，但后续计划不能在 break 中即时成交。
- fresh regular quote 进入 current overlay；缺失则禁止新开仓情景。
- valid_from 为 `max(as_of, 当前可交易 segment 起点)`；若 as_of 位于午间 break，则取下一 segment 起点。expires_at=regular_close。

### 12.3 eod

- 只有当日正式收盘已完成且 canonical bar 可用时，才能以 T 为新 origin。
- current price 等于 reference close，不使用盘后延伸价替代收盘价。
- decision session 是 T 之后下一个正式交易日。
- valid_from=下一会话 regular_open，expires_at=regular_close。
- 美股盘后延伸价若存在，只能在下一次 pre 情景中作为 current overlay，不能污染本次 EOD ForecastResult。

## 13. A股和美股差异边界

情景归并算法、概率阈值、remaining return 公式和证据等级对 A股/美股完全一致。市场差异只允许存在于：

1. DecisionSession 日历、时区、半日市和午间 break。
2. 当前 quote 的认可来源和 pre 是否适用。
3. 数据质量 capability。

A股 T+1、涨跌停、最小交易单位，美股延伸时段可成交性、税费和流动性属于 V2-5/V2-6/V2-7，V2-4 不提前实现。但情景必须保留 market/exchange/session/source，供后续层判断。

## 14. 确定性、哈希和持久化

### 14.1 哈希

```text
forecast_bundle_hash = hash(按 horizon 排序的 ForecastResult.event_key + 排除 generated_at 的完整业务 payload)
fact_update_hash = hash(按 available_at/kind/stable_key 排序的 ScenarioFactUpdate canonical payload)
quality_hash = hash(DataQualityReport canonical payload)
scenario_identity = {
  instrument,
  mode,
  as_of,
  origin_session_date,
  decision_session,
  valid_from,
  expires_at,
  forecast_bundle_hash,
  current_feature_hash,
  fact_update_hash,
  quality_hash,
  policy_version
}
scenario_id = sha256(canonical_json(scenario_identity))
event_key = instrument.stable_key + "|" + mode + "|" + (decision_session_date or "calendar-unavailable") + "|" + scenario_id
```

generated_at 不进入任何身份哈希。改变 quote、新闻、基本面、质量、预测版本、会话或 policy version 必须产生新 scenario_id。

### 14.2 migration 8

新增 `trading_scenarios`：

```text
scenario_id TEXT PRIMARY KEY
event_key TEXT UNIQUE NOT NULL
instrument_key TEXT NOT NULL
market TEXT NOT NULL
exchange TEXT NOT NULL
mode TEXT NOT NULL
origin_session_date TEXT NOT NULL
decision_session_date TEXT
forecast_bundle_hash TEXT NOT NULL
current_feature_hash TEXT NOT NULL
fact_update_hash TEXT NOT NULL
quality_hash TEXT NOT NULL
policy_version TEXT NOT NULL
payload_json TEXT NOT NULL
generated_at TEXT NOT NULL
schema_version INTEGER NOT NULL
```

Repository 必须提供：

```text
save_trading_scenario(scenario) -> idempotent / conflict
get_trading_scenario(scenario_id) -> TradingScenario | None
list_trading_scenarios(instrument, mode, decision_session_date | None) -> tuple[TradingScenario, ...]
```

- 同 scenario_id、同业务 payload、仅 generated_at 不同为幂等成功。
- 同 event_key 不同业务 payload 写 quarantine，不覆盖旧记录。
- 读取必须重建强类型合同并再次校验哈希。
- migration 8 重复执行安全，不修改 migration 1-7 checksum。

持久化是为了 V2-9 能分别评价“预测是否正确”和“情景翻译是否正确”，不是为了让旧情景跨会话继续生效。

## 15. Golden Cases

测试文件：

```text
tests/v2/test_scenario_contracts.py
tests/v2/test_scenario_planner.py
tests/v2/test_scenario_current_overlay.py
tests/v2/test_scenario_sessions.py
tests/v2/test_scenario_degradation.py
tests/v2/test_scenario_market_parity.py
tests/v2/test_scenario_repository.py
tests/v2/test_scenario_architecture.py
tests/v2/test_scenario_performance.py
```

### SC00 合同与身份稳定

相同输入重复构建，scenario_id/event_key/hash 完全相同，generated_at 可以不同；改变任一 forecast event、current feature、quality、decision session 或 policy version 必须改变身份。

### SC01 一致偏多

1/3/5/10 日均为强 bullish，至少 tactical 和 swing 各有一个 stock_confirmed：bias=bullish、alignment=aligned、state=bullish_continuation、support=confirmed。

### SC02 上涨结构中的回撤

1/3 日 bearish，5/10 日 bullish：alignment=mixed、bias=bullish、state=bullish_pullback；不得误判为 neutral，也不得允许无条件追高。

### SC03 下跌结构中的反弹

1/3 日 bullish，5/10 日 bearish：bias=bearish、state=bearish_rebound、entry_posture=countertrend_confirmation，trend_continuation 必须 blocked。

### SC04 同周期冲突

1日 bullish、3日 bearish：tactical=conflict、alignment=conflict、state=forecast_conflict、status=observation_only。

### SC05 震荡

四周期 neutral、margin>=0.05 且区间跨0：bias=range、state=range_bound；允许 range_mean_reversion，不允许直接把 neutral 当 bullish。

### SC06 概率与收益分布不一致

direction=bullish 但 p50<0，或 margin<0.10：HorizonSignal=weak，记录稳定 reason code，不能参与强方向归并。

### SC07 无 Champion

只有 industry/market/baseline 可用：仍输出概率观察和完整 scenario，但 forecast_support=observational、status=observation_only，不能升级新开仓。

### SC08 四周期不可用

样本不足或日历不可用：support=unavailable、bias=uncertain、state=uncertain；不得伪造 neutral 概率或目标日。样本不足但会话明确时 status=observation_only；日历不可用且 decision_session=None 时 status=blocked。

### SC09 剩余收益公式

P0=100、Pc=105、原区间[-4%,3%,8%]，断言剩余区间约[-8.5714%,-1.9048%,2.8571%]，不能直接减5个百分点。

### SC10 价格超出预测区间

current realized > tactical p90：price_location=above_p90，移除 trend_continuation/breakout_confirmation，entry_posture=wait_pullback。

### SC11 新事实覆盖

预测发行后存在显式 news/fundamental ScenarioFactUpdate：unmodeled_fact_update=True，ready 至少降为 degraded，不能修改原 ForecastResult。只有时间衰减导致 news 特征变化但无 update 时不得误降级。

### SC12 美股盘前

T-1 ForecastResult + 新鲜 Nasdaq 延伸 quote：origin 不变、current overlay 使用 quote、decision session 为下一常规会话；origin/current closed semantic hash 相同而完整 feature hash 可不同，日K repository 不增加记录。

### SC13 A股盘前

没有 quote 是 expected_missing，不伪造价格、不把它记成 Provider 故障；预测支持保持原等级，策略只能生成开盘后条件计划。

### SC14 盘中陈旧报价

intraday stale/missing quote：status=blocked、entry_posture=blocked，但 protective_exit/profit_lock 仍 allowed。

### SC15 盘后目标会话

周五 EOD 必须使用下一个真实交易会话，不得固定加一天；美股半日市 close 使用日历值，valid_from/expires_at 必须与该窗口一致。

### SC16 数据质量阻断隔离

Tab3 中单只股票 blocked 只产生该股票的 blocked scenario；其他股票相同输入结果不变。ScenarioPlanner 不读取组合或账户。

### SC17 保护性退出不可阻断

bullish、bearish、conflict、unavailable、blocked 五类情景中，protective_exit、profit_lock 和 failed_rebound_exit 始终 allowed。

### SC18 双市场对称

A股和美股使用等价 synthetic forecast/features/session 时，除 market/session/source 外，bias/alignment/state/policy 完全一致。

### SC19 persistence

migration 8 幂等；scenario 保存/读取强类型往返；仅 generated_at 变化仍幂等；同 event_key 不同 payload 进入 quarantine；重启后可读取。

### SC20 架构边界

情景包不得导入 V1、策略、风控、成交、组合、学习、LLM、UI；TradingScenario 序列化中不得出现交易动作、价格条件、仓位或账户字段。

### SC21 性能

纯内存构建 1000 个完整四周期情景本机目标小于1秒；性能测试不得联网或写数据库。

## 16. 测试顺序和验收

实现顺序：

1. 先写 SC00-SC08，建立 contracts、单周期和多周期纯函数。
2. 再写 SC09-SC11，实现 current overlay，不改 ForecastResult。
3. 写 SC12-SC15，扩展注入式 session calendar。
4. 写 SC16-SC18，固定质量隔离、保护性退出和双市场对称。
5. 写 SC19，增加 migration 8 和 repository。
6. 写 SC20-SC21，固定架构和性能。
7. 运行 V2-4、V2 全量、全项目回归并更新阶段状态。

命令：

```bash
venv/bin/python -m pytest tests/v2/test_scenario_*.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

验收必须同时满足：

- SC00-SC21 全通过。
- A股和美股 fixture 同时覆盖。
- V2-3 ForecastResult 字节级业务 payload 不被 V2-4 修改。
- 相同 scenario 输入结果确定且可持久化恢复。
- 没有任何 TradePlan、订单或仓位实现。
- README、DESIGN、V1 capability inventory 和 V2_REFACTOR_PLAN 状态同步。

## 17. 阶段完成后的停止点

V2-4 实现完成后必须停止。开始 V2-5 前另行制定 `StrategyInput`、`TradePlan`、条件表达式、保守/激进差异和策略迁移清单的精确合同。不得在情景层顺手迁移 V1 strategies，也不得用 allowed strategy family 冒充已生成交易建议。
