# TradeHelper V2-7 成交仿真层精确设计

> 状态：已完成并复审（2026-07-15）。本文是 V2-7 的规范性合同。实现建立在已冻结的 [V2_5_STRATEGIES.md](./V2_5_STRATEGIES.md) 和 [V2_6_RISK.md](./V2_6_RISK.md) 之上，不修改 TradePlan 或放宽 ExecutionDecision，也不包含 V2-8 组合分配、V2-9 学习或券商自动下单。

## 1. 阶段目标

V2-7 负责把同一份 `TradePlan + ExecutionDecision` 转成可审计的订单意图，并用当前快照或历史价格事件回答：

1. 风控批准的计划是否能生成订单意图，为什么？
2. 条件何时触发、失效或到期？
3. 当前只能做订单预览，还是历史证据足以模拟成交？
4. 理论成交价、股数、费用、滑点和未成交数量分别是多少？
5. 跳空止损、A股 T+1、涨跌停、停牌和流动性约束如何影响结果？
6. 结果使用了报价、分钟K还是日K，路径是否可验证？

本阶段必须保留三层事实：

```text
TradePlan                    策略想做什么
ExecutionDecision            风控最多允许做多少
FillEvidence                 市场证据表明理论上发生了什么
```

三层不能互相覆盖。策略计划正确但市场无法成交，必须同时保留计划、风控决策和拒绝/不可验证结果。

## 2. 明确不做的事情

V2-7 不负责：

- 不连接券商，不发送真实订单，不保存 broker_order_id。
- 不决定多股票之间如何争用现金，不排序关注股，不做相关性预算。
- 不把 C/D 决策升级为订单，不增加 V2-6 批准股数。
- 不重新预测、不重新选策略、不修改触发价、止损、止盈或失效条件。
- 不根据成交结果自动调整模型或策略参数。
- 不新增或伪造分钟数据源。当前生产数据只有已存在的 QuoteSnapshot、CanonicalBar 和可选 IntradayBar。
- 不把当前订单预览保存成真实成交。
- 不把日K的 high/low 顺序解释成真实盘中路径。

## 3. 固定主链路和所有权

```text
TradePlan + ExecutionDecision
  -> OrderIntentFactory
    -> OrderIntentBuildRecord + OrderIntent
      -> TriggerEngine
        -> CurrentOrderPreview
        -> HistoricalFillSimulator
          -> TriggerEvaluation + FillEvidence + ExecutionRun
```

V2-8 后续可以传入更小的最终组合股数，但只能通过 `OrderIntentRequest.requested_shares` 缩小 V2-6 上限，不能自行复制成交规则。

| 对象 | 负责 | 不负责 |
|---|---|---|
| TradePlan | action、条件、触发、止损、止盈、失效、有效期 | 股数、成交 |
| ExecutionDecision | A/B/C/D、最大批准股数、风险上限 | 订单类型、实际成交价 |
| OrderIntent | 把计划和批准量冻结成可执行意图 | 判断市场是否成交 |
| TriggerEvaluation | 条件在价格事件序列中的触发/失效/到期事实 | 费用、组合分配 |
| FillEvidence | 理论成交、拒绝、部分成交或不可验证的证据 | 改写策略和风控 |
| ExecutionRun | 一次纯内存仿真的输入输出身份 | 学习层归因 |

## 4. 代码位置

```text
tradehelper_v2/
  contracts/
    execution.py             # V2-7 枚举和不可变合同
  execution/
    __init__.py
    orders.py                # plan + decision -> intent
    trigger_engine.py        # 条件事件序列求值
    costs.py                 # Decimal 费用、滑点和容量模型
    market_rules.py          # 最终一手、价格限制、T+1 和交易状态检查
    preview.py               # 当前订单预览，不产生 FillEvidence.FILLED
    simulator.py             # 历史成交仿真和单标的状态转换
```

不得 import V1 `backtest/`、`strategies/`、`core/` 或 `utils/market_rules.py`。V1 只作为测试案例和算法经验来源。

## 5. 基础枚举

```text
OrderSide: buy / sell
OrderStyle: market_on_activation
IntentState: ready / staged
IntentBuildStatus: created / no_order
ExecutionMode: current_preview / historical_replay
EventGranularity: quote / intraday_bar / daily_bar
TradingStatus: open / suspended / unknown

TriggerState:
  ready / triggered / not_triggered / invalidated / expired / unverifiable

FillOutcome:
  preview_only / filled / partial / rejected /
  not_triggered / invalidated / expired / unverifiable

PathAssumption:
  exact_sequence / point_snapshot / gap_at_open /
  conservative_stop_first / strict_unknown

ExecutionEvidenceGrade:
  high / medium / low / insufficient
```

`market_on_activation` 表示条件成立后按市场可成交价格模拟，不表示系统已向券商发送市价单。

## 6. 原因代码

业务逻辑只能使用注册代码，至少包含：

```text
EXEC_INTENT_CREATED
EXEC_NO_ORDER_LEVEL_C
EXEC_NO_ORDER_LEVEL_D
EXEC_NO_ORDER_ACTION
EXEC_NO_APPROVED_SHARES
EXEC_REQUESTED_SHARES_REDUCED
EXEC_DECISION_STALE
EXEC_PLAN_EXPIRED
EXEC_TRIGGERED
EXEC_NOT_TRIGGERED
EXEC_INVALIDATED
EXEC_SEQUENCE_REQUIRED
EXEC_SEQUENCE_AMBIGUOUS
EXEC_DAILY_RANGE_ONLY
EXEC_CURRENT_PREVIEW_ONLY
EXEC_FRESH_QUOTE_REQUIRED
EXEC_GAP_TRIGGERED
EXEC_GAP_STOP
EXEC_STOP_TRIGGERED
EXEC_TAKE_PROFIT_TRIGGERED
EXEC_STOP_FIRST_CONSERVATIVE
EXEC_T1_BLOCKED
EXEC_PARTIAL_SELLABLE
EXEC_LIMIT_QUEUE_UNVERIFIABLE
EXEC_SUSPENDED
EXEC_TRADING_STATUS_UNKNOWN
EXEC_NO_TRADABLE_VOLUME
EXEC_CASH_REDUCED
EXEC_CASH_INSUFFICIENT
EXEC_POSITION_MISMATCH
EXEC_LOT_ROUNDED
EXEC_LIQUIDITY_CAPPED
EXEC_LIQUIDITY_EVIDENCE_MISSING
EXEC_NO_LEVEL2_DEPTH
EXEC_BASE_SLIPPAGE_APPLIED
EXEC_VOLATILITY_SLIPPAGE_APPLIED
EXEC_LIQUIDITY_SLIPPAGE_APPLIED
EXEC_COMMISSION_APPLIED
EXEC_SELL_TAX_APPLIED
EXEC_PARTIAL_FILL
EXEC_FULL_FILL
EXEC_UNFILLED_REMAINDER
EXEC_EVIDENCE_HIGH
EXEC_EVIDENCE_MEDIUM
EXEC_EVIDENCE_LOW
EXEC_EVIDENCE_INSUFFICIENT
EXEC_HISTORICAL_ONLY
EXEC_HARD_LIMIT_IMMUTABLE
```

原因代码排序去重。自然语言解释留给 V2-11。

## 7. OrderIntentRequest

```text
OrderIntentRequest:
  trade_plan: TradePlan
  execution_decision: ExecutionDecision
  risk_decision_bundle: RiskDecisionBundle
  requested_shares: Decimal | None
  requested_at
  execution_policy: ExecutionPolicy
  schema_version = 1
```

不变量：

1. plan_id、instrument、scenario_id、action、quantity_intent 必须一致。
2. execution_decision 必须属于 risk_decision_bundle，且 bundle 的 instrument、scenario_id、strategy_bundle_id、账户、估值、质量、市场规则和风控政策身份全部一致。
3. decision.level 只有 A/B 才能创建意图。
4. disposition 只有 approved_now/conditionally_approved 才能创建意图。
5. `requested_shares=None` 时使用 approved_shares。
6. `0 < requested_shares <= approved_shares`，并按市场 lot 向下取整。
7. requested_shares 只能缩小，不能被执行层再次放大。
8. decision/account/valuation/quality/evidence/rules/policy 身份原样进入意图。
9. C/D、hold、watch 或 0 股必须产生 `OrderIntentBuildRecord(no_order)`，不得抛弃记录。

`OrderIntentFactory.build_bundle(risk_decision_bundle, plans_by_id, requested_shares_by_decision_id, requested_at, execution_policy)` 必须为 bundle 中每条 decision 调用同一单条构造逻辑。缺 plan、重复 decision_id 或多余 requested key 都是合同错误，不能静默跳过。

OrderIntentFactory 必须注入 exchange calendar；不得用本机日期、固定周一至周五或字符串运算推导 EOD 的下一交易会话。

## 8. OrderIntent

```text
OrderIntent:
  intent_id
  event_key
  instrument
  scenario_id
  strategy_bundle_id
  risk_bundle_id
  plan_id
  decision_id
  profile
  action
  quantity_intent
  side
  order_style = market_on_activation
  state: ready / staged
  requested_shares: Decimal
  risk_approved_shares: Decimal
  trigger_condition
  confirmation_condition
  invalidation_condition
  trigger_level
  stop
  take_profit
  valid_from
  expires_at
  earliest_execution_at
  account_hash
  valuation_id
  quality_hash
  evidence_hash
  market_rule_version
  risk_policy_version
  execution_policy_version
  generated_at
  schema_version = 1
```

规则：

- approved_now -> ready；conditionally_approved -> staged。
- ready 仍需检查报价新鲜度、账户状态和市场规则，不等于已成交。
- `ExecutionDecision.entry_price` 只是 V2-6 sizing 参考价，不是限价单价格，不写入 order limit。
- buy/add -> side=buy；reduce/sell -> side=sell。
- stop/take profit 只能复制 TradePlan，不得基于成交价重新发明目标。
- earliest_execution_at 不早于 valid_from。
- EOD 计划的 earliest_execution_at 必须是下一交易会话开盘，不能是 T 日收盘。
- generated_at 不进入 intent_id；requested_shares、decision_id 和 execution_policy_version 必须进入。

## 9. OrderIntentBuildRecord 与完整性

```text
OrderIntentBuildRecord:
  build_id
  decision_id
  plan_id
  status: created / no_order
  intent_id: str | None
  reason_codes
  generated_at
  schema_version = 1

OrderIntentBundle:
  intent_bundle_id
  risk_bundle_id
  records
  intents
  generated_at
  schema_version = 1
```

RiskDecisionBundle 每个 decision 必须恰好有一个 build record。创建意图和不创建意图都可审计，C/D 不会从链路中消失。

## 10. ExecutionPolicy

```text
ExecutionPolicy:
  policy_version = execution_policy_v1
  base_slippage = 0.003
  volatility_threshold = 0.30
  volatility_factor = 0.01
  max_volatility_extra = 0.007
  free_participation = 0.01
  max_participation = 0.05
  max_liquidity_extra = 0.005
  missing_liquidity_reserve = 0.005
  ambiguity_mode = strict
  us_fill_quantum = 0.0001
  a_fill_quantum = 0.01
  currency_quantum = 0.01
  hard_constraint_version = execution_hard_v1
  parameter_hash
```

硬约束：

- 不允许未来事件、T 日盘后计划在 T 日成交、超过风控股数、负股数或 0 股成交。
- 不允许取消 A股 T+1、一手、停牌、涨跌停队列不确定性和账户/持仓检查。
- 不允许 strict 模式使用日K伪造盘中顺序。
- 不允许把当前 preview 标记为 historical fill。

V2-9 只能在预设范围内调整软滑点参数，不能关闭硬约束。

## 11. 统一价格事件

V2-7 不直接在业务逻辑中分别处理 QuoteSnapshot、IntradayBar 和 CanonicalBar。适配器先生成：

```text
ExecutionEvent:
  event_id
  instrument
  session_date
  interval_start
  interval_end
  granularity
  open/high/low/close: Decimal
  volume: Decimal | None
  previous_close: Decimal | None
  bid/ask: Decimal | None
  trading_status
  source
  source_evidence_quality
  available_at
  generated_at
```

不变量：

- 事件按 interval_start、interval_end、event_id 严格排序且不得重叠。
- available_at 不得晚于回放时点；历史回放不得读取事后修订前不可见的数据。
- quote 退化为一个点：open=high=low=close=price。
- 日K interval_start/interval_end 由注入交易日历给出，不从本机日期猜测。
- volume=None 表示缺失；volume=0 是真实值，但不能单独命名为“停牌”。
- bid/ask 没有数量，不得解释成盘口深度或成交保证。
- IntradayBar 只能证明 bar 粒度的 OHLC，不能证明 bar 内部先后顺序。

当前数据能力边界：

- 常规盘中实时点价：TickFlow QuoteSnapshot。
- 美股延伸时段点价：Nasdaq/yfinance QuoteSnapshot。
- 已完成日K：A股 TickFlow，美股 Nasdaq -> yfinance -> TickFlow。
- IntradayBar 已有合同和存储，但当前没有保证可用的生产分钟 Provider。

因此，V2-7 的分钟序列测试使用固定 synthetic fixture；生产数据缺少分钟序列时必须降级，不能新增隐式网络依赖。

## 12. ExecutionState

成交仿真是纯函数，不修改 AccountSnapshot：

```text
ExecutionState:
  market
  currency
  cash: Decimal
  position_shares: Decimal
  sellable_shares: Decimal | None
  average_cost: Decimal | None
  acquired_session_date: date | None
  active_stop: Decimal | None
  active_take_profit: Decimal | None
  captured_at
  source
```

输出 `ExecutionStateDelta`，由后续调用方应用。V2-7 只处理单标的变化，不汇总组合权益。

规则：

- state 必须与 decision.account_hash 对应的冻结账户事实一致，或由历史 runner 显式生成并带 source hash。
- 当前状态比 risk decision 更新时，只能缩量、复检或拒绝，不能扩大。
- 实际滑点导致现金不足时按 lot 逐手缩量；不足一手则拒绝。
- sell 不超过 position_shares 和 sellable_shares。
- A股当日买入形成的份额在同一 session_date 不可卖。

## 13. TriggerEngine

### 13.1 冻结事实

TriggerEngine 从 TradePlan.evaluations 重建计划时点的 closed/静态事实，只允许价格事件覆盖 `current.*` 执行事实。禁止用未来日计算的新 RSI、均线或新闻回写旧计划。

若同一静态特征在 plan evaluations 中出现冲突值，结果为 unverifiable，不取任意一个值。

### 13.2 条件支持

- snapshot 比较：GT/GTE/LT/LTE/BETWEEN/EQUALS。
- crossing：必须有计划时点前值和后续事件值。
- session OHLC：只能由对应会话完整事件提供。
- session volume：只能在 volume 可用时确认。
- ALL/ANY/NOT 保持 V2-5 三值逻辑，并增加 unverifiable 传播。

### 13.3 状态顺序

每个事件按以下顺序处理：

1. 检查事件是否早于 earliest_execution_at，早于则忽略。
2. 检查 expires_at；到期且未触发 -> expired。
3. 检查 invalidation；触发前失效 -> invalidated。
4. 检查 trigger + confirmation。
5. 证据不足以判断顺序 -> unverifiable，而不是猜测 triggered。
6. 触发后停止重新解释原始入场条件。

同一事件内 entry trigger 与 invalidation 都可能成立且无更细序列时，strict 模式返回 unverifiable，不成交。不得选择对策略有利的顺序。

### 13.4 crossing 与跳空

- `crosses_above`: previous <= level 且 current > level。
- `crosses_below`: previous >= level 且 current < level。
- 下一会话开盘直接越过 trigger，可由计划时点前值 + open 构成 `gap_at_open`。
- 只有日K high/low 穿越、但 open 未越过时，crossing 顺序不可验证。

### 13.5 TriggerEvaluation

```text
TriggerEvaluation:
  trigger_evaluation_id
  event_key
  intent_id
  state: TriggerState
  evaluated_event_ids
  event_batch_hash
  trigger_event_id: str | None
  invalidation_event_id: str | None
  evaluated_from
  evaluated_to
  triggered_at: datetime | None
  invalidated_at: datetime | None
  source
  granularity: EventGranularity | None
  path_assumption: PathAssumption
  evidence_grade: ExecutionEvidenceGrade
  execution_policy_version
  reason_codes
  generated_at
  schema_version = 1
```

不变量：

- triggered 只能有 triggered_at/trigger_event_id；invalidated 只能有 invalidated_at/invalidation_event_id。
- ready/not_triggered/expired/unverifiable 不得伪造触发时间。
- evaluated_event_ids 必须保持实际求值顺序；其 hash 进入身份。
- trigger_evaluation_id 由 intent、事件批次、终态、路径、证据等级和执行政策身份确定，generated_at 不进入身份。

## 14. CurrentOrderPreview

```text
CurrentOrderPreview:
  preview_id
  intent_id
  status: ready / staged / recheck_required / rejected / expired
  reference_price
  estimated_fill_low/high
  estimated_costs
  requested_shares
  max_preview_shares
  evidence_grade
  reason_codes
  observed_at
  generated_at
```

规则：

- preview 永远不产生 filled outcome。
- ready 需要新鲜 quote、账户/估值/市场规则身份仍有效，以及当前状态允许。
- staged 只展示触发条件、理论容量和成本区间。
- 单个 QuoteSnapshot 只能证明当前点，不能证明此前是否先触发止损或止盈。
- 美股 PRE 只有 price 时为低证据；有 bid/ask 仍无深度，不能声称必然成交。
- A股 PRE 没有连续盘前价时保持 staged/recheck_required。
- EOD 计划必须 staged 到下一会话。

## 15. 历史成交仿真

```text
HistoricalSimulationRequest:
  order_intent
  execution_state
  events
  market_rules
  execution_policy
  liquidity_evidence
  replay_as_of

ExecutionRun:
  run_id
  intent_id
  mode: ExecutionMode
  initial_state_hash
  event_batch_hash
  replay_as_of
  market_rule_version
  execution_policy_version
  trigger_evaluation_id
  fill_ids
  final_state_delta
  outcome: FillOutcome
  evidence_grade
  reason_codes
  generated_at
  schema_version = 1
```

`run_id` 先由 intent_id、mode、initial_state_hash、event_batch_hash、replay_as_of、market_rule_version 和 execution_policy_version 确定；`FillEvidence` 再引用该 run_id。`fill_ids` 不参与 run_id，避免 run/fill 循环身份，但持久化读取时必须双向复核。相同输入产生不同结果属于确定性冲突并进入 quarantine。

### 15.1 防未来函数

- EOD T 日计划忽略 T 日全部事件，最早使用 T+1 正式会话 open。
- PRE 计划只使用 valid_from 后事件。
- INTRADAY 计划只使用计划 as_of 后事件；不得使用该分钟 bar 在 as_of 后才形成的 high/low 回填当前判断。
- liquidity evidence 和 volatility evidence 的 cutoff 必须早于执行事件。
- replay_as_of 之前尚未 available 的修订数据不得进入事件批次。

### 15.2 原始成交价

long buy/add：

```text
开盘已越过触发价: raw_price = open
盘中事件精确穿越: raw_price = trigger level
```

sell/reduce 或持仓保护退出：

```text
开盘低于 stop: raw_price = open                 # gap stop
盘中精确穿越 stop: raw_price = stop
开盘高于 take profit: raw_price = open
盘中精确穿越 take profit: raw_price = take profit
普通退出条件在事件点触发: raw_price = event price
```

raw_price 之后再应用方向不利的滑点。buy 向上，sell 向下。

### 15.3 同一 bar 多触发

- 已持仓进入 bar 前，stop 和 take profit 同时被日K触碰：使用 stop-first 保守下界，grade=low，path=conservative_stop_first。
- 新开仓 trigger、stop 在同一日K范围内但无分钟顺序：strict 模式 outcome=unverifiable，不创建虚构往返成交。
- 分钟 bar 内同时触碰多个阈值仍无 bar 内顺序：同样 unverifiable；只有跨 bar 顺序才是 exact_sequence。
- conservative bound 可作为独立低质量研究结果，但不得冒充 verified fill，也不得用于 A 级学习证据。

### 15.4 保护条件持续性

buy/add 成交后，TradePlan 的 stop/take profit 复制到 `ExecutionStateDelta`。后续会话可由下一次模拟继续检查。V2-7 不自动创建新策略退出观点，只执行既有保护条款或后续 TradePlan。

## 16. 费用与滑点

所有金额和股数使用 Decimal。

### 16.1 流动性证据

```text
LiquidityEvidence:
  median_daily_volume_20: Decimal | None
  annualized_volatility_20: Decimal | None
  cutoff_at
  source
  evidence_hash
```

不得使用成交日收盘后才知道的全日 volume 决定开盘订单是否可成交。容量代理只能使用事件前已经冻结的 trailing volume。

### 16.2 股数容量

```text
participation = requested_shares / median_daily_volume_20
max_liquidity_shares = floor(median_daily_volume_20 * 5% / lot_size) * lot_size
fillable_shares = min(requested_shares, max_liquidity_shares)
```

- participation <= 1%：无额外流动性滑点。
- 1% < participation <= 5%：流动性滑点从 0 线性增加到 0.5%。
- participation > 5%：最多模拟 5% ADV，剩余记为 unfilled。
- trailing volume 缺失：不凭空声称深度，增加 0.5% 不确定性预留，grade 最高 low。

### 16.3 滑点

```text
volatility_extra = min(max(volatility - 30%, 0) * 0.01, 0.7%)
liquidity_extra = min(max((participation - 1%) / 4%, 0) * 0.5%, 0.5%)
slippage = 0.3% + volatility_extra + liquidity_extra
```

缺 liquidity evidence 时：

```text
slippage = 0.3% + volatility_extra + 0.5%
```

成交价：

```text
buy_fill  = raw_price * (1 + slippage)
sell_fill = raw_price * (1 - slippage)
```

A股价格按 0.01 对买入向上、卖出向下保守量化；美股模拟成交保留 0.0001。展示层再格式化。

### 16.4 费用

沿用执行时生效的 MarketRuleSet：

```text
commission = max(gross_value * commission_rate, minimum_commission)
sell_tax = gross_value * sell_tax_rate on sell only
total_fee = commission + sell_tax
buy_cash_delta = -(gross_value + total_fee)
sell_cash_delta = gross_value - total_fee
```

费用按货币最小 0.01 向上量化。V2-7 不声称覆盖所有券商个性化费率；rule source/version 必须显示。

## 17. 市场规则最终检查

### 17.1 A股

- buy/add 按 100 股向下取整。
- partial reduce 按 100 股向下取整。
- full exit 可卖出全部零股尾数。
- acquired_session_date 等于当前 session_date 的份额不可卖。
- 涨停买入、跌停卖出在没有排队/深度证据时 outcome=unverifiable 或 rejected，不模拟必然成交。
- price limit 使用 previous_close、分类和 0.01 tick 计算；分类 unknown 接近 4.9% 时不可验证。
- trading_status=suspended 时拒绝；status=unknown 不得写“停牌”。
- volume=0 且无其他成交证据时使用 EXEC_NO_TRADABLE_VOLUME，不使用 EXEC_SUSPENDED。

### 17.2 美股

- lot_size=1，不套用 A股 T+1 当日卖出限制或价格限制。
- 延伸时段 quote 只做 preview；无分钟/成交序列时不生成 historical filled。
- bid/ask 无 size 时只作为 top-of-book 价格证据，始终记录 EXEC_NO_LEVEL2_DEPTH。

## 18. FillEvidence

```text
FillEvidence:
  fill_id
  event_key
  run_id
  intent_id
  decision_id
  plan_id
  instrument
  action
  side
  outcome
  requested_shares
  filled_shares
  unfilled_shares
  raw_price
  slippage_rate
  fill_price
  gross_value
  commission
  sell_tax
  total_fee
  cash_delta
  triggered_at
  filled_at
  source
  granularity
  path_assumption
  evidence_grade
  market_rule_version
  execution_policy_version
  reason_codes
  generated_at
  schema_version = 1
```

不变量：

- preview_only/not_triggered/invalidated/expired/unverifiable/rejected 的 filled_shares=0，成交金额字段为 None 或 0，不能混用。
- filled/partial 的 filled_shares>0，且不超过 intent.requested_shares。
- partial 必须 unfilled_shares>0；filled 必须 unfilled_shares=0。
- buy cash_delta<0，sell cash_delta>0；费用非负。
- fill_price 必须由 raw_price、slippage 和量化规则复算。
- source/granularity/path/evidence grade 必须完整。
- generated_at 不进入 fill_id；事件、意图、政策、价格、股数和费用进入身份。

## 19. 证据等级

| 等级 | 最低条件 | 可用于什么 |
|---|---|---|
| high | 跨事件顺序明确的 provider intraday bars，字段完整 | 精确路径回放候选 |
| medium | 下一会话开盘 gap、单一阈值且日K无顺序冲突 | 常规历史成交估计 |
| low | 日K stop-first 保守下界、supplemental intraday、缺 ADV | 敏感性/下界，不得冒充精确成交 |
| insufficient | 触发顺序、价格、交易状态或市场规则不足 | 不产生 verified fill |

QuoteSnapshot 当前预览不是 historical fill 证据。即使 grade=high，也只是当前点价可靠，不代表订单已成交。

## 20. 三时段语义

| 模式 | 意图 | 当前预览 | 历史回放 |
|---|---|---|---|
| EOD | staged，下一会话生效 | 不可标记 ready-to-fill | T+1 open 起处理 |
| US PRE | staged/recheck | Nasdaq/yfinance 价格可估成本 | 无事件序列时不可 filled |
| A PRE | staged/recheck | 无连续盘前价是正常缺失 | 开盘后事件起处理 |
| INTRADAY | ready 或 staged | 新鲜 TickFlow quote 可确认当前点 | 只使用 plan.as_of 后的事件 |

盘前、盘中、盘后三种模式必须走同一个 OrderIntentFactory 和 TriggerEngine，只允许事件适配器不同。

## 21. 当前与历史路径一致性

同一 plan/decision/requested_shares 必须产生同一业务 OrderIntent。当前路径和历史路径只能在证据消费者上不同：

```text
OrderIntent -> CurrentOrderPreview
OrderIntent -> HistoricalFillSimulator
```

禁止：

- 当前路径直接读 TradePlan，历史路径读另一套策略信号。
- 当前路径使用 V2-6 approved_shares，历史路径重新用默认本金算股数。
- 历史路径跳过 C/D 决策并假设策略都能成交。
- 根据回测结果反向修改 intent trigger 或 stop。

## 22. 持久化与 migration 11

新增：

```text
order_intents:
  intent_id PK, event_key UNIQUE, instrument_key, risk_bundle_id, plan_id, decision_id,
  profile, action, state, requested_shares, payload_json, generated_at, schema_version

order_intent_build_records:
  build_id PK, event_key UNIQUE, decision_id, plan_id, status,
  intent_id, payload_json, generated_at, schema_version

trigger_evaluations:
  trigger_evaluation_id PK, event_key UNIQUE, intent_id, state,
  triggered_at, evidence_grade, payload_json, generated_at, schema_version

execution_runs:
  run_id PK, event_key UNIQUE, intent_id, mode, initial_state_hash,
  event_batch_hash, outcome, evidence_grade, payload_json, generated_at, schema_version

fill_evidence:
  fill_id PK, event_key UNIQUE, run_id, intent_id, instrument_key,
  outcome, filled_at, payload_json, generated_at, schema_version
```

Repository 提供 save/get/list，要求：

- migration 11 可重复执行，不修改 migration 1-10 checksum。
- 同业务 payload 仅 generated_at 不同为幂等。
- 同 event_key 不同 payload 进入 quarantine，不覆盖。
- 读取重建强类型合同，并复核所有索引列和嵌套引用。
- preview 不写入 fill_evidence。
- run 与 fill 的 intent_id、事件批次、政策和规则身份必须一致。
- ExecutionRun 的 run_id 不包含 fill_ids；repository 必须复核 run.fill_ids 与 fill_evidence.run_id 双向一致，缺失或多余引用均为损坏数据。

## 23. 性能边界

- OrderIntentFactory 1000 个 plan/decision 纯内存转换目标 < 1 秒。
- 1000 个日K事件单意图触发回放目标 < 1 秒。
- 10000 个 IntradayBar 事件单意图回放目标 < 2 秒。
- 不在事件循环中访问网络或数据库。
- Repository 批量写入使用单事务，不能逐条打开连接。

性能阈值使用宽松 CI 上限，另记录本机基线，避免系统负载偶发误报。

## 24. 测试文件

```text
tests/v2/execution_helpers.py
tests/v2/test_order_intent.py
tests/v2/test_trigger_engine.py
tests/v2/test_execution_costs.py
tests/v2/test_fill_simulator.py
tests/v2/test_execution_market_rules.py
tests/v2/test_execution_repository.py
tests/v2/test_execution_parity.py
tests/v2/test_execution_architecture.py
tests/v2/test_execution_performance.py
tests/v2/test_schema_migrations.py
```

测试不得联网、不得读取用户数据库、不得 import V1。固定 fixture 覆盖 A股、美股、flat、held、PRE、INTRADAY、EOD、quote、intraday bar、daily bar、T+1、涨跌停和数据缺失。

## 25. Golden Cases EX00-EX49

```text
EX00 合同、Decimal、哈希、generated_at 幂等和注册原因代码
EX01 A/B approved/conditional 生成 ready/staged intent
EX02 C/D/hold/watch/0股生成 no_order record，不静默丢失
EX03 requested_shares 默认 approved_shares，只能缩小不能扩大
EX04 plan/decision/risk bundle/account/规则身份不一致抛 ContractViolation
EX05 entry_price 不被转换成 limit price
EX06 同一 plan/decision 当前与历史路径产生同一 intent
EX07 EOD T 日计划 earliest_execution_at 为 T+1 open
EX08 PRE/INTRADAY/EOD intent state 与有效期正确
EX09 过期计划不能生成可激活意图

EX10 snapshot 比较条件事件求值正确
EX11 crosses_above/below 使用前后事件，不用单点猜 crossing
EX12 gap open 越过 trigger 可确定触发
EX13 触发前 invalidation 优先
EX14 trigger 与 invalidation 同 bar 无序列为 unverifiable
EX15 confirmation 缺失不触发
EX16 session OHLC/volume 证据要求正确
EX17 事件早于 valid_from 被忽略
EX18 expires_at 后为 expired
EX19 future available_at 事件不得进入历史回放

EX20 EOD 绝不使用 T 日 bar 成交
EX21 开盘跌破 stop 使用更差 open，再应用卖出滑点
EX22 盘中跨 bar 穿越 stop 使用 stop 原始价
EX23 开盘越过 take profit 使用 open 原始价
EX24 已持仓日K同时触发 stop/take 使用 stop-first 低质量下界
EX25 新开仓 trigger/stop 同日无分钟顺序不伪造成交
EX26 分钟 bar 内双触发仍不可验证，跨 bar 才可确定
EX27 strict_unknown 不产生 fill；保守下界与 verified fill 分开
EX28 buy 成交复制 stop/take 到 state delta
EX29 current preview 永不生成 filled outcome

EX30 基础滑点固定进入审计
EX31 高波动滑点不低于低波动
EX32 1%-5% ADV 流动性滑点单调增加
EX33 超过5% ADV只部分模拟，剩余明确记录
EX34 缺 ADV 增加预留且 evidence 最高 low
EX35 buy/sell 滑点方向不利
EX36 A/美股佣金、最低佣金和A股卖出税可复算
EX37 费用和成交价量化方向保守
EX38 滑点后现金不足逐手缩量，不强制一手
EX39 fill 数量绝不超过 risk approved/requested/position/sellable

EX40 A股 buy/add 与 partial reduce 按100股，full exit 可卖零股
EX41 A股同日买入 T+1 阻止保护卖出但保留原因
EX42 A股涨停买/跌停卖无队列证据不伪造成交
EX43 classification unknown 接近最严格边界不可验证
EX44 volume=0 不自动命名停牌；显式 suspended 才使用停牌原因
EX45 美股不套用A股 T+1、涨跌停和100股一手
EX46 quote/bid-ask 无 depth 不声称成交保证
EX47 单股票证据不足不污染另一股票 run
EX48 migration 11、幂等、冲突 quarantine、强类型重启恢复
EX49 架构、禁止字段、性能和 execution_hard_v1 不可关闭
```

验收命令：

```bash
venv/bin/python -m pytest tests/v2/test_order_intent.py tests/v2/test_trigger_engine.py tests/v2/test_execution_costs.py tests/v2/test_fill_simulator.py tests/v2/test_execution_market_rules.py tests/v2/test_execution_repository.py tests/v2/test_execution_preview.py tests/v2/test_execution_smoke.py tests/v2/test_execution_parity.py tests/v2/test_execution_architecture.py tests/v2/test_execution_performance.py tests/v2/test_execution_golden_cases.py tests/v2/test_schema_migrations.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

## 26. 实施顺序

1. EX00-EX09：execution contracts、OrderIntentFactory 和完整 build records。
2. EX10-EX19：统一 ExecutionEvent、事件条件求值和时间边界。
3. EX30-EX39：Decimal cost/slippage/liquidity 和纯函数 state delta。
4. EX20-EX29：CurrentOrderPreview 与 HistoricalFillSimulator。
5. EX40-EX47：A/美股最终规则、证据降级和跨股票隔离。
6. EX48：migration 11 和 repository。
7. EX49：架构、禁止字段、硬约束和性能。
8. 运行 V2-7 专项、V2 全量、项目全量，更新阶段状态和剩余风险。

## 27. 阶段停止点

V2-7 完成后停止。不得顺手实现：

- 多股票最终分配、组合现金锁定、相关性或替换排序。
- 策略/预测/成交学习、OOF 晋升或参数优化。
- LLM 假设生成和解释。
- 报告、UI 或券商自动交易。

开始 V2-8 前必须另行冻结组合输入批次、现金争用、跨股票优先级、相关性、最终股数和替换机会合同。

## 28. 完成记录

- EX00-EX49 已全部落为具名可执行验收矩阵；成交层专项 `125 passed`。
- V2 全量回归 `389 passed, 3 skipped`，项目全量回归 `649 passed, 3 skipped`。
- 默认关闭的 3 条真实 Provider 冒烟测试使用本地脱敏配置显式启用后为 `3 passed`。
- 复审修复了跨股票/账户快照串线、未来事件与流动性证据、冻结静态条件丢失、跳空仍按触发价成交、同 bar 顺序伪造、A股 T+1/涨跌停边界、成交状态增量和 run/fill 非原子写入。
- 已知边界保持显式降级：缺分钟事件时不声称盘中路径可验证；缺 Level2/队列证据时不保证成交；当前预览不产生历史 Fill。
