# TradeHelper V2-6 风控层精确设计

> 状态：已实现并复审。本文是 V2-6 的规范性合同；实现建立在已冻结的 [V2_5_STRATEGIES.md](./V2_5_STRATEGIES.md) 之上，不修改 V2-5 的 TradePlan 交易观点，也不提前实现 V2-7 订单/成交、V2-8 组合分配或 V2-9 自动优化。

## 1. 阶段目标

V2-6 只完成一件事：使用真实账户、冻结估值、数据质量、历史证据和版本化市场规则，对 V2-5 的每一条 TradePlan 做执行分级和单计划风险容量计算。

本层必须回答：

1. 这条计划是 A、B、C 还是 D，为什么？
2. 条件当前是否已经满足；若未满足，是否可以保留为条件批准计划？
3. 使用真实账户后，最多允许多少股、占权益多少、预计在止损价退出时亏多少？
4. 当前持仓、现金、单票集中度、股票总仓位和已有止损风险是否允许买入或加仓？
5. A股一手、T+1、涨跌停和美股延伸时段证据是否使计划当前不可执行？
6. 风控为何批准、缩小、降级或驳回，哪些是不可优化的硬约束？

V2-6 不负责：

- 不生成新的买卖点子，不修改 TradePlan 的 action、trigger、stop、take profit、invalidation 或有效期。
- 不把等待中的条件标记成已经成交；waiting 计划只能得到条件批准。
- 不创建 OrderIntent，不模拟触发路径、跳空、费用、滑点或成交。
- 不在多个股票之间分配同一笔现金；V2-6 给出单计划最大批准量，V2-8 只能进一步缩小。
- 不计算策略 OOF、预测到期结果或自动调整政策；历史证据由调用方显式传入，V2-9 后续负责生产和优化。
- 不读取数据库、网络、V1 风控模块或 UI 状态。

## 2. 固定主链路和所有权

```text
StrategyBundle + TradingScenario
  + DataQualityReport
  + AccountSnapshot + FrozenAccountValuation
  + PositionAvailability
  + PlanEvidenceSnapshot
  + MarketRuleSet + MarketState
    -> RiskOfficer
      -> ExecutionDecision[] + RiskDecisionBundle
        -> V2-7 OrderIntent / FillSimulation
        -> V2-8 PortfolioDecision
```

| 对象 | 负责 | 不负责 |
|---|---|---|
| TradePlan | 动作、触发、止损、止盈、失效、有效期 | 账户 sizing、A/B/C/D |
| FrozenAccountValuation | 同一冻结批次的权益、市值、仓位 | 交易观点 |
| PlanEvidenceSnapshot | 当前股票+策略的 OOF/在线证据状态 | 修改策略参数 |
| MarketRuleSet | 版本化的一手、T+1、涨跌停和风险成本预留 | 实际撮合 |
| ExecutionDecision | 分级、条件批准、股数上限、计划亏损和原因 | 改写 TradePlan |
| V2-7 | 最终订单合法性、触发、费用、滑点和成交证据 | 放宽 V2-6 风险上限 |
| V2-8 | 多股票之间的现金和组合风险分配 | 把 C/D 升成 A/B |

关键原则：风控官不决定“看不看”。每条 TradePlan 都必须有对应决策；C/D 计划仍保留在输出中，不能静默删除。

## 3. 代码位置

```text

  contracts/
    risk.py                 # 风控输入、估值、证据、规则和决策合同
  risk/
    __init__.py
    policy.py               # risk_policy_v1 硬/软参数
    valuation.py            # AccountSnapshot + 冻结价格 -> FrozenAccountValuation
    market_rules.py         # A股/美股规则预检，不生成订单
    sizing.py               # Decimal 仓位和计划亏损计算
    officer.py              # RiskRequest -> RiskDecisionBundle
```

不要继续向 `core/signal_check.py` 或 V1 `utils/market_rules.py` 加补丁。V1 代码只作为回归线索，V2 风控主链路不得 import V1。

## 4. 基础枚举

```text
ExecutionLevel:
  A / B / C / D

DecisionDisposition:
  approved_now             # A/B 且当前触发、规则允许、批准股数 > 0
  conditionally_approved   # A/B，但需等待触发并在触发时重跑风控
  no_order_required        # hold，不产生订单但风险可监控
  observe                  # C，仅观察
  rejected                 # D，事实冲突/关键缺失/当前不可成交

RiskProfile:
  conservative / aggressive

EvidenceStatus:
  reliable_positive
  positive_uncertain
  insufficient_sample
  unavailable
  negative
  conflicting

ValuationStatus:
  complete / incomplete / unavailable

MarketEligibility:
  eligible / recheck_required / partially_eligible / blocked

AvailabilitySource:
  broker / user / internal_ledger / assumed_prior_day / unavailable

RiskConstraintKind:
  hard / soft
```

`approved_now` 不等于已经下单。它只表示当前冻结事实允许 V2-7 创建订单预览。

## 5. 风控原因代码

原因必须使用注册代码，不以自然语言决定业务逻辑。至少包含：

```text
RISK_APPROVED
RISK_CONDITIONALLY_APPROVED
RISK_SMALL_SAMPLE
RISK_NEGATIVE_EXPECTANCY
RISK_EVIDENCE_CONFLICT
RISK_COUNTERTREND_CAP
RISK_PLAN_OBSERVATION_ONLY
RISK_PLAN_NOT_TRIGGERED
RISK_PLAN_EXPIRED
RISK_ENTRY_STOP_MISSING
RISK_ENTRY_STOP_INVALID
RISK_ACCOUNT_MISSING
RISK_ACCOUNT_MARKET_MISMATCH
RISK_ACCOUNT_POSITION_MISMATCH
RISK_EQUITY_ZERO
RISK_VALUATION_INCOMPLETE
RISK_CASH_INSUFFICIENT
RISK_BUDGET_EXHAUSTED
RISK_MIN_LOT_EXCEEDS_CAPACITY
RISK_SINGLE_POSITION_CAP
RISK_TOTAL_STOCK_CAP
RISK_CONCENTRATION_WARNING
RISK_CONCENTRATION_REDLINE
RISK_ADD_BLOCKED_BY_EXISTING_RISK
RISK_DATA_BLOCKED
RISK_DATA_DEGRADED
RISK_QUALITY_MULTIPLIER_APPLIED
RISK_EXIT_PRESERVED
RISK_PROTECTIVE_EXIT_PRIORITY
RISK_POSITION_AVAILABILITY_UNKNOWN
RISK_T1_BLOCKED
RISK_PARTIAL_SELLABLE
RISK_PRICE_LIMIT_BLOCKED
RISK_A_CLASSIFICATION_UNKNOWN
RISK_MARKET_RECHECK_REQUIRED
RISK_EXTENDED_TOP_OF_BOOK_ONLY
RISK_EXTENDED_VOLUME_PROXY
RISK_EXTENDED_PRICE_ONLY
RISK_NO_LEVEL2_DEPTH
RISK_FRICTION_RESERVE_INCLUDED
RISK_GAP_LOSS_CAN_EXCEED_PLAN
RISK_PORTFOLIO_ALLOCATION_PENDING
RISK_HARD_CONSTRAINT_IMMUTABLE
```

原因代码排序去重；解释文本只能由后续报告层根据代码和结构化数值生成。

## 6. 冻结估值合同

### 6.1 ValuationPrice

```text
ValuationPrice:
  instrument: InstrumentId
  price: Decimal
  observed_at: datetime
  source: str
  price_kind: reference_close / fresh_quote
  freshness_status: FreshnessStatus
```

不变量：

- price 必须有限且大于 0。
- `fresh_quote` 必须是 fresh，且 `observed_at <= valuation_at`。
- 盘前/盘中价格只用于当前估值，不写入正式日 K。
- 不得用 cost_price、止损价、预测中位价或触发价冒充持仓当前市值。

### 6.2 FrozenAccountValuation

```text
FrozenAccountValuation:
  valuation_id
  event_key
  market
  currency
  account_hash
  price_batch_hash
  valuation_at
  status: ValuationStatus
  equity: Decimal | None
  cash: Decimal
  invested_value: Decimal | None
  invested_pct: float | None
  position_values: tuple[PositionValuation, ...]
  missing_price_instruments: tuple[InstrumentId, ...]
  generated_at
  schema_version = 1

PositionValuation:
  instrument
  shares
  price
  market_value
  position_pct
  unrealized_pnl_amount
  unrealized_pnl_pct: float | None
```

估值公式：

```text
market_value_i = shares_i * frozen_price_i
equity = cash + sum(market_value_i)
position_pct_i = market_value_i / equity
invested_pct = sum(market_value_i) / equity
```

不变量：

1. 所有金额和股数使用 `Decimal`；只在展示时格式化。
2. 所有持仓必须使用同一个 `valuation_at` 批次中可见的价格。
3. 任一活跃持仓缺价时 status=incomplete，equity/position_pct/invested_pct 为 None；不得用成本价补齐。
4. 空账户 cash=0、positions=() 时 equity=0 是真实事实，不创建 100000 等模拟本金。
5. A股 CNY 和美股 USD 分开估值；没有可靠汇率时不能合并。
6. valuation_id 排除 generated_at，对 account_hash、价格批次和 valuation_at 做稳定哈希。

## 7. 持仓可卖数量合同

```text
PositionAvailability:
  instrument
  total_shares: Decimal
  sellable_shares: Decimal | None
  as_of
  source: AvailabilitySource
  reason_codes
```

规则：

- total_shares 必须与 AccountSnapshot 中同股票持仓完全一致，否则合同冲突。
- 美股初始规则为 T+0；未提供 availability 时可用 total_shares，但必须记录规则来源。
- A股不得静默假设全部可卖。只有 broker/user/internal_ledger 或显式 `assumed_prior_day` 才能提供 sellable_shares。
- `assumed_prior_day` 必须由调用方明确确认“今日未买入该股票”，不能由 RiskOfficer 自己推断。
- A股 sellable_shares=None 时，卖出计划保留但 `recheck_required`；不能声称当前可全部卖出。
- full exit 可以卖出已有零股尾数；buy/add 和 partial reduce 按一手规则向下取整。

## 8. 历史证据合同

```text
PlanEvidenceSnapshot:
  evidence_id
  instrument
  strategy_id
  strategy_version
  parameter_hash
  profile: RiskProfile | None
  sample_count
  oof_sample_count
  expected_net_return
  confidence_low
  confidence_high
  win_rate
  max_adverse_excursion
  status: EvidenceStatus
  source_ledger_version
  data_cutoff_at
  evaluated_at
  generated_at
```

证据必须绑定具体股票、策略版本和参数哈希。行业、市场或其他股票的证据只能作为解释，不能把 A 升级条件冒充为已满足。`data_cutoff_at` 是证据使用的最新已到期结果时点；V2-9 必须对预测周期做 maturity purge，未到期收益不得进入证据。

初始分级：

| 状态 | 固定标准 | 入场最高等级 |
|---|---|---|
| reliable_positive | OOF >= 30、expected_net_return > 0、confidence_low >= 0、无冲突 | A |
| positive_uncertain | 点估计 > 0，但置信区间跨 0 | B |
| insufficient_sample | OOF 1-29 | B |
| unavailable | 没有 V2-9 证据 | B |
| negative | OOF >= 10 且点估计 < 0，或 OOF >= 30 且点估计 <= 0，或 confidence_high < 0 | C |
| conflicting | 同身份证据冲突/账本冲突 | D |

A 不要求在大牛市中机械跑赢买入持有。这里审查的是扣除风险预留后的单次交易正期望和稳定性，不使用“必须跑赢基准”作为唯一门槛。

EvidenceStatus 不是调用方可随意填写的标签：合同必须按上表复核状态。sample_count/oof_sample_count 必须非负且 OOF 不超过总样本；收益、胜率、MAE 和置信区间必须有限，胜率在 0-1，confidence_low <= confidence_high。字段不足时只能 unavailable，身份或账本冲突时 conflicting 优先。

V2-6 不计算上述证据。V2-9 未实现前，调用方只能传 unavailable/固定测试 fixture，因此真实运行中的新开仓最高通常为 B；这不是数据不全，而是诚实地表示历史证据尚未建立。

## 9. 市场规则合同

### 9.1 MarketRuleSet

```text
MarketRuleSet:
  rule_version
  market
  exchange
  lot_size
  same_day_sell_restricted
  commission_rate
  minimum_commission
  sell_tax_rate
  base_slippage_reserve
  price_limit_pct: float | None
  instrument_classification: ordinary / st / growth / star / bse / unknown
  source
  effective_from
  effective_to: datetime | None
```

初始冻结值：

```text
US: lot_size=1, same_day_sell_restricted=false, commission=0.03%, minimum=0,
    sell_tax=0, base_slippage_reserve=0.3%, price_limit=None

A:  lot_size=100, same_day_sell_restricted=true, commission=0.03%, minimum=5 CNY,
    sell_tax=0.05%, base_slippage_reserve=0.3%

A ordinary=9.9%, ST=4.9%, 创业板/科创板=19.9%, 北交所=29.9%
```

这些百分比是保守预检边界，不冒充交易所精确价格舍入。V2-7 必须按当日权威规则和 tick size 做最终订单校验。

`same_day_sell_restricted` 表示 A 股当日买入份额不可卖出，不表示证券结算周期。美股虽为 T+1 结算，但不因此套用 A 股的当日卖出限制。

### 9.2 MarketState

```text
MarketState:
  instrument
  mode
  session
  current_price
  previous_close
  bid
  ask
  volume
  observed_at
  source
  freshness_status
```

### 9.3 预检规则

- A股盘中达到跌停附近：sell 当前 blocked，但保护退出仍显示为紧急 D，不删除。
- A股盘中达到涨停附近：buy/add 当前 blocked。
- A股盘前和盘后无法提前知道下一交易日涨跌停是否阻塞，标记 recheck_required，不提前驳回条件计划。
- 分类 unknown 且价格变化未接近最严格 4.9% 边界时可继续；接近边界时 D，并要求补分类事实。
- 美股不使用 A股价格限制和一手规则。
- V2-6 只做规则预检；实际成交、跳空、排队和拒单属于 V2-7。

## 10. 延伸时段流动性替代约束

没有 Level2 时不得虚构盘口深度。美股 PRE 新开仓容量倍率上限：

| 可用证据 | 条件 | 倍率 |
|---|---|---|
| bid/ask | spread <= 0.2% | 0.75 |
| bid/ask | 0.2% < spread <= 0.5% | 0.50 |
| bid/ask | spread > 0.5% | 0.25 |
| 只有有效 volume | 无 bid/ask | 0.50 |
| 只有新鲜 price | 无 bid/ask/volume | 0.25 |

```text
spread = (ask - bid) / ((ask + bid) / 2)
```

该倍率同时限制风险预算和目标仓位预览。计划在常规会话真正触发时必须重跑风控；盘前倍率不是常规会话流动性的保证。

A股 PRE 没有连续实时价，不套用美股延伸时段倍率，只生成开盘条件并要求触发时复核。

## 11. RiskPolicy

```text
RiskPolicy:
  policy_version = risk_policy_v1
  conservative_risk_pct = 0.01
  aggressive_risk_pct = 0.02
  conservative_target_cap = 0.20
  aggressive_target_cap = 0.25
  single_position_hard_cap = 0.25
  total_stock_hard_cap = 0.90
  concentration_warning = 0.20
  concentration_redline = 0.30
  b_level_multiplier = 0.50
  countertrend_multiplier = 0.50
  conservative_reduce_fraction = 0.50
  aggressive_reduce_fraction = 0.25
  extended_liquidity_multipliers
  hard_constraint_version
  parameter_hash
```

硬约束：

- 真实账户权益、现金和持仓。
- buy/add 必须有 stop 且 stop < entry。
- 单笔风险硬上限 2%。
- 单票新开/加仓后硬上限 25%。
- 股票总仓位硬上限 90%。
- 数据冲突、A股 T+1、涨跌停和一手规则。
- 不得把成本价当现价，不得用默认本金。

软约束：

- conservative 1%/20%，aggressive 2%/25%。
- B 级、countertrend、数据降级和延伸时段倍率。
- 20% 集中度警告。

V2-9 只能在文档预设边界内优化软参数。不能修改硬上限、取消止损、伪造权益或绕过市场规则。

## 12. RiskRequest

```text
RiskRequest:
  instrument
  strategy_bundle: StrategyBundle
  trading_scenario: TradingScenario
  data_quality: DataQualityReport
  account_snapshot: AccountSnapshot | None
  valuation: FrozenAccountValuation | None
  position_availability: PositionAvailability | None
  evidence: tuple[PlanEvidenceSnapshot, ...]
  market_rules: MarketRuleSet
  market_state: MarketState | None
  policy: RiskPolicy
  as_of
  schema_version = 1
```

输入不变量：

1. instrument、bundle、scenario、account market 和 rules market 必须一致。
2. bundle.scenario_id 必须等于 scenario.scenario_id。
3. `stable_hash(data_quality)` 必须等于 scenario.quality_hash。
4. bundle 内所有 plan 的 position_hash 必须与 AccountSnapshot 目标持仓一致；空仓必须匹配 `hash("flat")`。
5. account_snapshot.captured_at、valuation.valuation_at、position_availability.as_of 和 market_state.observed_at 均不得晚于 as_of；valuation.account_hash 必须等于 `stable_hash(account_snapshot)`。
6. account/valuation 可以为 None，但所有新开仓只能 C、approved_shares=0；不得回退模拟本金。
7. 历史证据必须精确匹配 plan 的 instrument/strategy/version/parameter_hash；其 data_cutoff_at/evaluated_at/generated_at 均不得晚于 as_of，不匹配或未到期证据忽略并记录原因。
8. market_rules 必须在 effective_from/effective_to 内对 as_of 生效；历史回放不得使用今日规则覆盖当日规则。
9. 结构合同冲突直接抛 ContractViolation；现实世界的缺失、过期或市场阻塞生成 C/D 决策，不抛出后丢失计划。

## 13. ExecutionDecision

V2-5 一个 TradePlan 可以同时属于 conservative/aggressive。V2-6 必须按 `plan_id + profile` 各生成一条决策；不能把两个风险档案混成一个股数。

```text
ExecutionDecision:
  decision_id
  event_key
  instrument
  scenario_id
  bundle_id
  plan_id
  profile: RiskProfile
  action: PlanAction
  quantity_intent: QuantityIntent
  level: ExecutionLevel
  disposition: DecisionDisposition
  executable_now: bool
  recheck_at_trigger: bool
  approved_shares: Decimal
  blocked_shares: Decimal
  entry_price: Decimal | None
  stop_price: Decimal | None
  current_position_value: Decimal | None
  current_position_pct: float | None
  planned_position_value: Decimal | None
  post_trade_position_pct: float | None
  risk_budget_amount: Decimal | None
  incremental_planned_loss: Decimal | None
  total_position_planned_loss: Decimal | None
  max_loss_amount: Decimal | None
  friction_reserve: Decimal | None
  market_eligibility: MarketEligibility
  evidence_status: EvidenceStatus
  hard_constraints: tuple[ConstraintResult, ...]
  soft_adjustments: tuple[RiskAdjustment, ...]
  reason_codes
  valid_from
  expires_at
  account_hash: str | None
  valuation_id: str | None
  quality_hash
  evidence_hash
  market_rule_version
  risk_policy_version
  generated_at
  schema_version = 1
```

`max_loss_amount` 是“按计划止损价并包含保守摩擦预留的计划亏损”，不是券商保证的绝对最大亏损。跳空、跌停、停牌和流动性枯竭可能使真实亏损更大，因此所有入场决策必须带 `RISK_GAP_LOSS_CAN_EXCEED_PLAN`。

禁止字段：

- order_type、limit_order_price、fill_price、filled_at。
- realized_fee、realized_slippage、broker_order_id。
- portfolio_rank、replacement_target、correlation_budget。

## 14. RiskDecisionBundle

```text
RiskDecisionBundle:
  risk_bundle_id
  event_key
  instrument
  scenario_id
  strategy_bundle_id
  position_state
  decisions: tuple[ExecutionDecision, ...]
  conservative_decision_ids
  aggressive_decision_ids
  protective_decision_ids
  account_hash
  valuation_id
  quality_hash
  market_rule_version
  risk_policy_version
  generated_at
```

完备性：

- StrategyBundle 主分支中的每个 plan/profile 必须恰好有一条 ExecutionDecision。
- C/D 不删除；退出、持有、观察和失效计划全部保留。
- 同一 plan 的 conservative/aggressive action、trigger、stop 和 invalidation 仍引用同一 TradePlan，不复制或修改计划价格。
- protective decision ids 必须覆盖 protective_exit/profit_lock/failed_rebound_exit。

## 15. A/B/C/D 决策矩阵

### 15.1 buy/add

按以下顺序执行，前项不能被后项覆盖：

1. 计划/账户/估值/数据冲突、stop 缺失或 stop >= entry、市场当前 blocked -> D。
2. 账户未录入、equity=0、估值不完整、最小一手超容量、现金/集中度/总仓位无容量 -> C。
3. evidence=negative -> C；conflicting -> D。
4. evidence=reliable_positive 且其他事实通过 -> A。
5. positive_uncertain/insufficient/unavailable -> B。
6. countertrend 最高 B，并应用 0.50 倍率。
7. PlanReadiness=observation_only -> C；not_applicable -> D（合同通常应提前阻止）。
8. A/B + triggered + approved_shares>0 + market eligible -> approved_now。
9. A/B + waiting -> conditionally_approved、executable_now=false、recheck_at_trigger=true。

### 15.2 reduce/sell

- 不要求正期望证据，不因预测偏多、无 Champion 或 entry 被阻断而消失。
- triggered 且可卖数量/市场规则完整：full exit 为 A；partial risk reduction 为 A/B，取决于是否能满足计划数量。
- T+1 导致部分可卖：B + partially_eligible，批准可卖部分并显式记录 blocked_shares。
- T+1 全部不可卖、跌停、停牌或价格事实冲突：D + rejected，但保留 `RISK_PROTECTIVE_EXIT_PRIORITY`。
- waiting 的保护退出为 conditionally_approved，触发时重跑可卖数量和市场规则。
- 不允许用负面预测阻止止损或锁利。

### 15.3 hold/watch

- watch 默认 C/observe；它不是失败，也不能生成股数。
- hold 有有效保护线且当前风险可控时为 B/no_order_required。
- 持仓超过 25% 禁止 add；超过 30% 标记 redline。V2-6 不凭空创建减仓 TradePlan，实际组合减仓优先级由 V2-8 处理。
- hold 与 triggered protective exit 同时存在时，hold 降为 C，保护退出优先。

## 16. 仓位和计划亏损算法

### 16.1 入场基础值

```text
trigger_price = TradePlan.trigger_level.value

waiting:
  entry = trigger_price

triggered:
  executable_reference = fresh ask if present, otherwise fresh current_price
  entry = max(trigger_price, executable_reference) for buy/add

stop = TradePlan.stop.level.value
unit_structural_risk = entry - stop
```

buy/add 若任一值缺失、非正、非有限或 `stop >= entry`，直接 D。已触发计划没有新鲜可执行参考价时不得 `approved_now`，只能条件批准并要求 V2-7/触发时重算。`ExecutionDecision.entry_price` 记录用于 sizing 的 entry，原始触发价仍只存在 TradePlan；不允许风控回写触发价。

### 16.2 风险预算

```text
profile_pct = 1% conservative / 2% aggressive
evidence_multiplier = 1.0(A) / 0.5(B)
countertrend_multiplier = 0.5 if COUNTERTREND_ONLY else 1.0
quality_multiplier = DataQualityReport.max_position_multiplier
liquidity_multiplier = 第10章倍率，非 PRE 为 1.0

risk_budget = equity
              * profile_pct
              * evidence_multiplier
              * countertrend_multiplier
              * quality_multiplier
              * liquidity_multiplier
```

任意乘数不能超过 1；硬上限仍为 equity 的 2%。

### 16.3 摩擦风险预留

V2-6 不模拟真实成交，但 sizing 不能忽略成本：

```text
buy_commission = max(q * entry * commission_rate, minimum_commission)
sell_commission = max(q * stop * commission_rate, minimum_commission)
slippage_reserve = q * (entry + stop) * base_slippage_reserve
sell_tax = q * stop * sell_tax_rate
friction_reserve(q) = buy_commission + sell_commission + slippage_reserve + sell_tax
```

US minimum_commission=0。q=0 时所有预留为 0。

### 16.4 buy

```text
planned_loss(q) = q * (entry - stop) + friction_reserve(q)
cash_required(q) = q * entry + buy_commission + q * entry * base_slippage_reserve
post_value(q) = q * entry
```

### 16.5 add

为了避免“新加仓风险很小，但整只股票风险已经过大”：

```text
existing_trigger_risk = existing_shares * (entry - stop)
incremental_loss(q) = q * (entry - stop) + incremental_friction
total_position_planned_loss(q) = existing_trigger_risk + incremental_loss(q)
post_value(q) = current_position_value + q * entry
```

total_position_planned_loss 必须不超过 risk_budget；已有风险已耗尽预算时禁止 add。

### 16.6 容量上限

```text
profile_target_cap = 20% conservative / 25% aggressive
single_cap_value = equity * min(profile_target_cap, 25%)
stock_total_capacity = equity * 90% - invested_value
cash_capacity = cash - buy friction reserve
```

q 同时受风险预算、现金、单票上限和股票总仓位上限约束。计算步骤固定：

1. 求各约束的 raw shares 下界。
2. 取最小非负值。
3. buy/add 按 lot_size 向下取整。
4. 重新计算含最低佣金的 planned_loss/cash_required。
5. 若仍超限，每次减少一手直到全部满足或 q=0。

禁止 `max(..., lot_size)` 把 0 股强行变成一手。

### 16.7 reduce/sell

```text
reduce conservative = 持仓的 50%
reduce aggressive = 持仓的 25%
sell = 全部持仓
```

- partial reduce 按市场一手向下取整；结果为 0 时保持 0 并说明，不强制变成 full exit。
- full sell 可使用全部可卖股数，包括 A股零股尾数。
- approved_shares <= sellable_shares <= total_shares。
- blocked_shares 记录因 T+1 或可卖数量不足未批准的部分。

## 17. 数据质量和事实一致性

### 新开仓/加仓

- `status=blocked`、`block_new_entries=true` 或关键价格冲突 -> D。
- degraded/watch 使用 max_position_multiplier 降低预算，最高 B。
- 缺新闻或基本面本身不把价格计划变成 D；影响应已体现在预测/情景证据中。
- plan.missing_conditions 非空且涉及 trigger/stop/当前价格 -> C 或 D，不能 A/B。

### 退出

- 基本面、新闻、预测或回测证据缺失不能阻止保护退出。
- 当前价格、持仓股数或市场状态冲突可以阻止“当前可执行”，但必须保留紧急退出建议和原因。
- 单只股票数据质量差不能污染 RiskDecisionBundle 中其他股票；跨股票处理属于调用方/V2-8。

## 18. 保守/激进档案

同一 TradePlan 的两个档案：

| 项目 | 保守 | 激进 |
|---|---|---|
| 账户风险预算 | 1% | 2% |
| 单票目标软上限 | 20% | 25% |
| B级倍率 | 50% | 50% |
| partial reduce | 50% | 25% |
| 硬止损/硬上限/市场规则 | 相同 | 相同 |

激进不等于放宽止损、允许虚构权益或突破 25% 单票/90% 总仓位。两个档案不能产生相反 action；action 来自同一 TradePlan。

## 19. Tab1、Tab3 和组合边界

- Tab1：V2-6 可基于用户对应市场账户生成单计划最大批准股数；没有账户时只给 C 级观察和风险公式，不填模拟本金。
- Tab3：V2-6 对每只股票独立产生“单计划最大批准量”，不能同时占用现金。V2-8 接收全部决策后统一分配，最终量只能小于等于 V2-6 批准量。
- 单票集中度和当前总仓位在 V2-6 是硬预检；相关性、高相关资产合计上限、关注股替换和多计划风险占用属于 V2-8。
- 不同市场账户和币种分别运行；V2-6 不做 FX 合并。

## 20. 盘前、盘中、盘后

| 模式 | 风控行为 |
|---|---|
| EOD | 使用正式收盘冻结估值，条件计划面向下一会话；市场状态需触发时重检 |
| US PRE | 使用新鲜 Nasdaq/yfinance 延伸报价估值和流动性代理；常规会话触发时重跑 |
| A PRE | 没有连续盘前价是正常能力边界；只做条件容量预览，当前不可执行 |
| INTRADAY | 使用新鲜 TickFlow 价格和当前可卖数量；可生成 approved_now |

所有 waiting/盘前计划都必须 `recheck_at_trigger=true`。历史风控决策不能在账户现金、持仓或市场状态变化后直接复用。

## 21. 风控覆盖与“聪明但不越权”

为了避免“风控官太笨，把研究或策略好点子直接废弃”：

1. 风控对每条 TradePlan 生成决策，不删除计划。
2. C 表示事实存在但当前不应执行；D 表示事实/规则冲突，不表示研究观点无价值。
3. 所有降级必须区分 hard_constraints 和 soft_adjustments。
4. 风控只能把 approved_shares 缩小到 0，不能改 trigger/stop/take profit。
5. 风险退出不受预测偏多、无 Champion 或历史样本不足阻止。
6. 历史负期望可以把 entry 降为 C，不能取消止损、锁利或市场规则。
7. concentration redline 可以阻止 add 和发出组合风险标记，但不能在没有 TradePlan 时伪造一张卖单。

## 22. 身份、持久化和 migration 10

### 22.1 身份

```text
decision_identity = {
  plan_id, bundle_id, profile, level, disposition,
  approved_shares, blocked_shares,
  entry/stop/risk values,
  account_hash, valuation_id, quality_hash, evidence_hash,
  market_rule_version, risk_policy_version,
  hard constraints, soft adjustments,
  valid_from, expires_at
}

decision_id = hash(decision_identity)
event_key = instrument + decision_session + plan_id + profile + decision_id
```

generated_at 和解释文本不进入身份。账户、估值、证据、规则、政策或批准股数变化必须产生新 decision_id。

### 22.2 migration 10

新增：

```text
frozen_account_valuations:
  valuation_id PK, event_key UNIQUE, market, currency,
  account_hash, price_batch_hash, valuation_at,
  status, payload_json, generated_at, schema_version

execution_decisions:
  decision_id PK, event_key UNIQUE, instrument_key, scenario_id,
  bundle_id, plan_id, profile, level, disposition,
  account_hash, valuation_id, payload_json, generated_at, schema_version

risk_decision_bundles:
  risk_bundle_id PK, event_key UNIQUE, instrument_key, scenario_id,
  strategy_bundle_id, account_hash, valuation_id,
  payload_json, generated_at, schema_version
```

Repository：

```text
save/get/list_frozen_account_valuation
save/get/list_execution_decision
save/get/list_risk_decision_bundle
```

要求：

- 同业务 payload 仅 generated_at 不同为幂等。
- 同 event_key 不同 payload 进入 quarantine，不覆盖。
- 读取重建强类型合同，并复核所有索引列、身份哈希和嵌套 plan/profile 引用。
- migration 10 可重复执行，不修改 migration 1-9 checksum。

## 23. Golden Cases

```text
RK00 合同、哈希、Decimal、generated_at 幂等
RK01 无账户不虚构本金，新开仓为 C/0股
RK02 真实 equity=0 禁止新开仓但不伪造错误
RK03 持仓缺价导致估值不完整，不用成本价补齐
RK04 数据 blocked 阻止 entry，但保护退出保留
RK05 buy/add 无 stop 或 stop>=entry 为 D
RK06 waiting A/B 只能条件批准，触发时必须重跑
RK07 triggered + exact reliable positive evidence 可为 A
RK08 样本不足/无证据最高 B；未到期或决策时点后才可用的证据不参与分级
RK09 可靠负期望 entry 为 C
RK10 证据或事实冲突为 D
RK11 conservative 1% 与 aggressive 2% 风险预算
RK12 B级、countertrend、质量倍率逐项可审计
RK13 风险预算、现金、单票和总仓位取最小容量；已触发入场按不低于触发价的新鲜可执行价 sizing，缺价不得 approved_now
RK14 0股不会被一手规则强行变成100股
RK15 A股 buy/add 按100股向下取整；美股按1股
RK16 A股 full sell 可退出零股尾数
RK17 partial reduce 保守50%/激进25%，不足一手不变全卖
RK18 add 计入已有持仓到同一止损的总风险
RK19 已有风险耗尽预算时禁止 add
RK20 当前单票>=25%禁止 add，>=30%记录 redline
RK21 股票总仓位>=90%禁止新增风险
RK22 风险退出不因预测偏多/无Champion/负期望被阻止
RK23 protective exit triggered 时 hold 降为 C
RK24 A股 T+1 全部/部分不可卖处理正确
RK25 A股涨停阻止买入、跌停阻止卖出且保留紧急原因
RK26 A股分类未知只在接近最严格限制时阻断
RK27 美股不套用A股一手/T+1/涨跌停
RK28 延伸时段 bid/ask、volume、price-only 倍率正确
RK29 无Level2不生成盘口深度或成交保证
RK30 friction reserve 和计划亏损使用 Decimal 可复算
RK31 max_loss 明确为止损假设，不承诺跳空后的绝对上限
RK32 flat/held/held_unknown 动作与账户持仓一致
RK33 每个 plan/profile 恰好一个决策，C/D 不丢失
RK34 entry/exit 冲突时退出优先，entry 不被静默删除
RK35 A股/美股等价事实产生等价分级语义
RK36 PRE/INTRADAY/EOD 的 executable_now/recheck 正确
RK37 单股票缺失不污染另一股票请求
RK38 migration 10、幂等、冲突 quarantine、重启恢复
RK39 架构边界：无V1/成交/组合/学习/LLM/UI import
RK40 序列化不含 OrderIntent/fill/broker 字段
RK41 1000次纯内存风控 bundle 本机目标小于2秒
RK42 软参数可版本化，硬约束不可被替换或关闭
```

## 24. 测试文件

```text
tests/v2/risk_helpers.py
tests/v2/test_risk_contracts.py
tests/v2/test_risk_valuation.py
tests/v2/test_risk_officer.py
tests/v2/test_risk_sizing.py
tests/v2/test_risk_market_rules.py
tests/v2/test_risk_profiles.py
tests/v2/test_risk_market_parity.py
tests/v2/test_risk_repository.py
tests/v2/test_risk_architecture.py
tests/v2/test_risk_performance.py
tests/v2/test_risk_scenario_matrix.py
```

测试不得联网、不得读取真实数据库、不得用 V1 风控结果作为标准答案。固定 fixture 必须覆盖 A股、美股、flat、held、held_unknown、PRE、INTRADAY、EOD、数据缺失、T+1 和涨跌停。

验收命令：

```bash
venv/bin/python -m pytest tests/v2/test_risk_*.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

## 25. 实施顺序

1. RK00-RK05：建立 risk contracts、原因代码和严格身份。
2. RK01-RK03/RK30：实现 Decimal 冻结估值和计划亏损。
3. RK11-RK21：实现 policy、sizing 和 profile。
4. RK24-RK29：实现版本化双市场规则预检。
5. RK06-RK10/RK22-RK23/RK32-RK37：实现 RiskOfficer 决策矩阵和完备输出。
6. RK38：实现 migration 10 和 repository。
7. RK39-RK42：架构、序列化、性能和硬约束审计。
8. 运行 V2-6 专项、V2 全量和项目全量，更新阶段状态与剩余风险。

## 26. 阶段停止点

V2-6 完成后必须停止。不得顺手实现：

- OrderIntent、订单类型、触发监听、跳空成交、费用和滑点仿真。
- 多股票现金分配、相关性、关注股替换或组合排序。
- 风控效果学习、自动升降级或源码改写。
- LLM 风控裁决、报告或 UI。

开始 V2-7 前必须另行冻结订单意图、事件序列求值、市场 tick/lot 最终校验、跳空止损、同日双触发、费用/滑点和成交证据合同。
