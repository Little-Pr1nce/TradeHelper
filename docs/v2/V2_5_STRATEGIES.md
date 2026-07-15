# TradeHelper V2-5 策略层精确设计

> 状态：实现完成并复审通过。本文是 V2-5 的规范性合同；实现不得用 V1 `strategies/` 作为运行依赖。V2-4 的输入边界以 [V2_4_SCENARIOS.md](./V2_4_SCENARIOS.md) 为准。

## 1. 阶段目标

V2-5 只完成一件事：把冻结的 `FeatureSnapshot + TradingScenario + PositionSnapshot` 翻译为可复现、可等待、可回放的 `TradePlan`。

本层必须回答：

1. 当前事实下，哪些买入、加仓、减仓、卖出、持有或观察计划与情景相容？
2. 尚未触发时，具体等待哪个价格和哪些确认条件？
3. 入场计划的结构止损、止盈方式和失效条件是什么？
4. 当前缺少哪些事实，因此只能等待或观察？
5. 同一计划如何同时供当前预览和后续历史成交仿真使用？

本层不负责：

- 不决定 A/B/C/D 执行等级。
- 不计算股数、仓位比例、账户最大亏损金额或组合风险预算。
- 不处理 A股 T+1、涨跌停、最小交易单位、税费、滑点和成交路径。
- 不回测、不选择 Champion 策略、不自动调参。
- 不做 Tab3 跨股票排序或关注股替换。
- 不调用 LLM，不读取 V1 策略对象，不生成订单。

上述能力分别属于 V2-6、V2-7、V2-8、V2-9 和 V2-10。

## 2. 固定主链路和所有权

```text
FeatureSnapshot + TradingScenario + PositionSnapshot
  -> StrategyEngine
    -> TradePlan[] + StrategyBundle
      -> V2-6 RiskOfficer
        -> ExecutionDecision（等级、股数、仓位、最大亏损）
          -> V2-7 OrderIntent / FillSimulation
```

职责必须严格分开：

| 对象 | 负责 | 不负责 |
|---|---|---|
| TradingScenario | 方向环境、证据等级、允许策略家族 | 具体交易价格 |
| TradePlan | 动作意图、触发条件、结构止损、止盈、失效、有效期 | 账户 sizing 和执行等级 |
| ExecutionDecision | A/B/C/D、批准/拒绝、股数、仓位、最大亏损 | 修改交易点子 |
| OrderIntent | 把批准计划转换成市场规则下的订单意图 | 重新计算策略 |

策略层不能因为没有账户权益而填一个默认本金。最终报告要求的最大亏损金额必须来自 V2-6 的真实账户计算。

## 3. 代码位置

```text
tradehelper_v2/
  contracts/
    strategy.py          # 条件 DSL、TradePlan、StrategyBundle
  strategies/
    __init__.py
    policy.py            # 冻结阈值和 strategy_policy_version
    registry.py          # StrategySpec 注册表
    conditions.py        # 三值条件求值，不使用 eval
    engine.py            # StrategyInput -> StrategyBundle
    templates/
      trend.py           # 趋势延续、回踩、突破
      support.py         # MA120、区间支撑
      exits.py           # 保护止损、锁利、反抽失败
      observation.py     # 完备条件观察
```

不要为每个 V1 策略机械新建文件。相同家族的规则放在同一模板模块，通过 `StrategySpec` 和版本区分。

## 4. 基础枚举

```text
PlanAction:
  buy / add / reduce / sell / hold / watch

QuantityIntent:
  open / add / partial_exit / full_exit / keep / none

PlanReadiness:
  triggered          # 当前冻结事实已经满足触发和必需确认
  waiting            # 条件明确但尚未满足，未来可触发
  observation_only   # 关键事实缺失或情景只允许观察
  not_applicable     # 例如空仓时的卖出分支

ConditionResult:
  true / false / unknown / pending_event / not_applicable

ConditionOperator:
  gt / gte / lt / lte / between / equals
  crosses_above / crosses_below
  all / any / not

TakeProfitMode:
  fixed / risk_multiple / dynamic / conditional / none

StopMode:
  hard_price / close_confirmation / structure_invalidation

PlanProfile:
  conservative / aggressive

PositionState:
  flat / held_profit / held_loss / held_flat / held_unknown
```

V2 初始只支持多头持股和现金管理，不引入做空、融券或期权动作。

## 5. 条件 DSL

条件必须是结构化、可序列化的表达式，禁止保存 Python 表达式、lambda、`eval` 字符串或只有自然语言的条件。

### 5.1 Operand

```text
ConditionOperand:
  kind: feature / constant / derived_level
  key: str
  value: float | str | bool | None
  unit: price / ratio / index / boolean / None
  source_features: tuple[str, ...]
```

- `feature` 只引用已注册 `FeatureSnapshot` 字段。
- `constant` 必须来自带版本的 `StrategySpec.parameters`。
- `derived_level` 必须保存数值、计算代码和源特征，不能只保存结果。

### 5.2 ConditionExpression

```text
ConditionExpression:
  condition_id: str
  operator: ConditionOperator
  left: ConditionOperand | None
  right: ConditionOperand | None
  lower: ConditionOperand | None
  upper: ConditionOperand | None
  children: tuple[ConditionExpression, ...]
  evidence_requirement: snapshot / event_sequence / session_ohlc / session_volume
  reason_code: str
  schema_version: int = 1
```

不变量：

- 比较节点必须有合法操作数；`all/any/not` 只能使用 children。
- `between` 必须满足 lower <= upper。
- `crosses_above/crosses_below` 需要事件序列；V2-5 当前快照不能伪造“已经穿越”。
- 条件缺失返回 `unknown`，不能把缺失值当作 0、False 或中性。
- 需要未来行情才能判断的穿越返回 `pending_event`，不是错误。
- condition_id 对排除解释文本后的业务 payload 做稳定哈希。

### 5.3 当前求值

```text
ObservedValue:
  key: str
  value: float | str | bool | None
  status: FeatureStatus | ConditionResult
  available_at: datetime | None

ConditionEvaluation:
  condition_id
  result: ConditionResult
  observed_values: tuple[ObservedValue, ...]
  missing_features: tuple[str, ...]
  evaluated_at
```

`evaluated_at` 固定等于 `StrategyInput.as_of`。V2-5 只记录计划发行时的冻结求值；未来是否真正触发由 V2-7 使用同一条件表达式和后续事件判断，不能原地改写 TradePlan。

`PlanReadiness` 规则：

1. 必需条件均 true -> triggered。
2. 存在 false 或 pending_event，且无关键 unknown -> waiting。
3. 任一必需特征 unknown/blocked/stale -> observation_only。
4. 分支与持仓状态不适用 -> not_applicable。
5. 合同冲突直接抛 `ContractViolation`，不能生成一条貌似正常的 invalid 建议。

## 6. 价格和保护合同

### 6.1 DerivedPriceLevel

```text
DerivedPriceLevel:
  level_id
  value: float
  role: trigger / stop / take_profit / support / resistance / invalidation
  calculation_code
  calculation_version
  source_features: tuple[str, ...]
  source_scenario_id
```

数值必须有限且大于 0。市场 tick size 舍入属于 V2-6/V2-7，V2-5 保留未舍入的理论价。

### 6.2 StopSpec

```text
StopSpec:
  mode: StopMode
  level: DerivedPriceLevel | None
  condition: ConditionExpression
  reason_code
```

所有 `buy/add` 计划必须有可量化的保护价，并满足 `stop < worst_entry_price`。缺少保护价时计划只能是 `observation_only`，V2-6 不得升级为 A/B。

### 6.3 TakeProfitSpec

```text
TakeProfitSpec:
  mode: TakeProfitMode
  level: DerivedPriceLevel | None
  risk_multiple: float | None
  condition: ConditionExpression | None
  reason_code
```

- fixed/risk_multiple 必须能算出明确目标价。
- dynamic/conditional 必须有结构化退出条件。
- none 可以保留，但风险收益比必须写“不可量化”，不得声称优秀。
- `reduce/sell` 不要求再定义盈利目标。

## 7. 输入合同

```text
StrategyInput:
  instrument: InstrumentId
  feature_snapshot: FeatureSnapshot
  trading_scenario: TradingScenario
  position_snapshot: PositionSnapshot | None
  strategy_specs: tuple[StrategySpec, ...]
  policy_version: str = "strategy_policy_v1"
  as_of: datetime
  schema_version: int = 1
```

输入不变量：

1. instrument 与 feature/scenario/position 完全一致。
2. feature_snapshot.feature_hash 等于 scenario.current_feature_hash。
3. `feature_snapshot.mode == scenario.mode`，且 `feature_snapshot.cutoff_at == as_of == scenario.as_of`；输出计划的有效窗口必须继承或收窄 scenario 窗口。
4. position_snapshot.captured_at 不得晚于 as_of；没有持仓使用 None，不能构造 0 股 PositionSnapshot。
5. specs 必须来自注册表，id/version/parameter hash 唯一且稳定。
6. `as_of` 必须等于 scenario.as_of；若 scenario 在该冻结时点已无合法决策会话，则只能生成 observation_only。历史回放不得用进程当前墙钟时间判断旧 scenario “已经过期”。
7. StrategyEngine 不访问数据库、不联网、不读取账户现金、不读取 V1 模块。

为什么不传 `AccountSnapshot`：账户权益、现金、组合持仓和 sizing 属于 V2-6/V2-8。V2-5 只需要当前股票是否持有、股数和成本，用于区分 buy/add/reduce/sell 和生成持仓保护条件。

`PositionState` 的确定规则：没有 PositionSnapshot 为 flat；成本价 <= 0 或可用决策价缺失为 held_unknown；否则以 `epsilon=max(0.0025, 0.25*closed.atr_pct_14)` 为中性带，浮动收益高于 epsilon 为 held_profit，低于 -epsilon 为 held_loss，其余为 held_flat。

## 8. StrategySpec

```text
StrategySpec:
  strategy_id
  strategy_version
  family: StrategyFamily
  position_applicability: flat / held / both
  supported_actions: tuple[PlanAction, ...]
  allowed_states: tuple[ScenarioState, ...]
  required_features: tuple[str, ...]
  optional_features: tuple[str, ...]
  parameters: Mapping[str, float | int | bool | str]
  parameter_hash
  enabled: bool
  schema_version: int = 1
```

初始参数是代码注册的冻结候选，不从历史结果现场调参。V2-9 只能在预注册范围内生成新 parameter set，并通过样本外验证后晋升；不得改写生产源码。

### 8.1 Reason codes

条件、计划和 bundle 只能使用冻结白名单；不得把自然语言、股票代码或数值拼进 reason code。初始 `STRATEGY_REASON_CODES`：

```text
PLAN_TRIGGERED
PLAN_WAITING
PLAN_OBSERVATION_ONLY
BRANCH_NOT_APPLICABLE
SCENARIO_ENTRY_BLOCKED
SCENARIO_OBSERVATION_ONLY
TREND_STRUCTURE_CONFIRMED
TREND_REENTRY_PENDING
PULLBACK_ZONE_REACHED
PULLBACK_RECLAIM_PENDING
MA120_SUPPORT_ZONE_REACHED
MA120_RECLAIM_PENDING
RANGE_LOWER_ZONE_REACHED
RANGE_RECLAIM_PENDING
BREAKOUT_LEVEL_PENDING
BREAKOUT_VOLUME_CONFIRMED
PROFIT_LOCK_TRIGGERED
PROFIT_LOCK_PENDING
FAILED_REBOUND_PENDING
FAILED_REBOUND_TRIGGERED
PROTECTIVE_EXIT_TRIGGERED
PROTECTIVE_EXIT_PENDING
STOP_LEVEL_UNAVAILABLE
TAKE_PROFIT_UNQUANTIFIED
FEATURE_MISSING
FEATURE_STALE
FEATURE_BLOCKED
FEATURE_INSUFFICIENT_HISTORY
CURRENT_PRICE_EXPECTED_MISSING
EVENT_SEQUENCE_REQUIRED
SESSION_OHLC_REQUIRED
SESSION_VOLUME_REQUIRED
POSITION_COST_UNKNOWN
COUNTERTREND_ONLY
UNMODELED_FACT_UPDATE
CALENDAR_UNAVAILABLE
ENTRY_EXIT_CONFLICT
PROFILES_MERGED
```

具体缺失字段、观察值、阈值和来源保存在 `ObservedValue`、`missing_conditions` 和 `DerivedPriceLevel`，不通过发明新 reason code 表达。

## 9. TradePlan

```text
TradePlan:
  plan_id
  event_key
  instrument
  scenario_id
  strategy_id
  strategy_version
  parameter_hash
  family
  action: PlanAction
  quantity_intent: QuantityIntent
  profiles: tuple[PlanProfile, ...]
  readiness: PlanReadiness
  trigger_condition: ConditionExpression
  confirmation_condition: ConditionExpression | None
  trigger_level: DerivedPriceLevel | None
  stop: StopSpec | None
  take_profit: TakeProfitSpec | None
  hold_condition: ConditionExpression | None
  invalidation_condition: ConditionExpression
  evaluations: tuple[ConditionEvaluation, ...]
  evidence_features: tuple[str, ...]
  missing_conditions: tuple[str, ...]
  reason_codes: tuple[str, ...]
  valid_from
  expires_at
  position_hash: str
  policy_version
  generated_at
  schema_version: int = 1
```

TradePlan 明确不包含：

```text
shares / position_pct / cash / account_equity / max_loss_amount
execution_level / approved / order_type / fees / slippage
```

这些字段只能出现在后续 ExecutionDecision 或 OrderIntent。

输出不变量：

- plan 有效期必须与 scenario 有效窗口相同或更短，不能跨会话。
- triggered/waiting 计划必须有非空 valid_from/expires_at；没有合法会话窗口时只能 observation_only，并继承 scenario 的空窗口。
- action=buy 仅适用于 flat；action=add/reduce/sell/hold 仅适用于 held。
- reduce 使用 partial_exit，sell 使用 full_exit。
- buy/add 必须有 stop 和 invalidation；止损不成立时 readiness 只能 observation_only。
- reason_codes、missing_conditions、profiles、features 必须排序去重。
- 保护性退出计划不能因 forecast_support 不足而消失。
- generated_at 不进入 plan_id/event_key。
- evaluations 必须覆盖 trigger、confirmation、stop/hold 和 invalidation 中实际存在的顶层条件，并按 condition_id 排序；相同 scenario 下求值不一致属于合同冲突。

## 10. StrategyBundle

```text
StrategyBranch:
  branch: entry_or_add / reduce_or_exit / hold / invalidation
  plans: tuple[TradePlan, ...]
  readiness
  not_applicable_reason: str | None

StrategyBundle:
  bundle_id
  event_key
  instrument
  scenario_id
  position_state
  entry_or_add: StrategyBranch
  reduce_or_exit: StrategyBranch
  hold: StrategyBranch
  invalidation: StrategyBranch
  conservative_plan_ids: tuple[str, ...]
  aggressive_plan_ids: tuple[str, ...]
  conflict_state: none / entry_exit_both_triggered
  reason_codes
  policy_version
  generated_at
```

完备性规则：

- 四个分支永远存在，不能返回空白报告。
- 空仓时 reduce/exit/hold 明确为 not_applicable；不得伪造卖出计划。
- 持仓时至少生成保护退出、持有和失效分支。
- 没有可用新开仓策略时生成条件观察计划，说明缺什么和等待什么。
- 若 entry 与 exit 同时 triggered，保留退出，将新开仓降为 observation_only，并标记 conflict；策略层不自行“投票”。
- branch readiness 按 `triggered > waiting > observation_only > not_applicable` 聚合；仅当分支内没有计划且业务上确实不适用时才可使用 not_applicable。
- conservative/aggressive plan ids 必须引用 bundle 内计划，按 plan_id 排序去重；空仓或持仓状态不能引用不适用动作。

## 11. 保守与激进方案

保守和激进共享同一个市场方向、基础触发、止损逻辑和失效事实。差异只允许是：

| 分支 | 保守 | 激进 |
|---|---|---|
| 新开仓 | 必需条件 + 全部可用确认条件 | 必需条件 + 最少确认条件 |
| 加仓 | 只在原持仓未触发保护且趋势重新确认 | 可在结构支撑确认后较早触发 |
| 锁利/减仓 | 较早保护利润 | 等待更深的结构失效 |
| 风险金额/仓位 | V2-6 较低 | V2-6 较高，但仍受同一硬上限 |

如果两个档案除 profiles 外的完整业务 payload（包括 confirmation、stop、take profit 和 invalidation）完全相同，只生成一个 `TradePlan`，profiles 同时包含两者。若触发价相同但确认条件不同，则保留两条计划并明确展示确认差异；不能为制造差异而伪造两套价格。

## 12. 初始模板和冻结规则

共同定义：

```text
P0   = 冻结已完成日K参考收盘价
Pobs = current_overlay 在 reference_close/fresh_quote 状态下的 current_price；其他状态为 None
ATR = closed.atr_pct_14 * P0
R   = trigger_price - stop_price

ma20/ma60/ma120 = closed.ma_20/60/120
rsi14           = closed.rsi_14
macd_hist       = closed.macd_hist_pct
bb_pct20        = closed.bb_pct_20
bb_width20      = closed.bb_width_20
closed_vol20    = closed.volume_ratio_20
current_vol20   = current.volume_vs_daily_20
```

P0 的确定顺序和校验固定如下：

1. `current_overlay.price_state=reference_close` 时使用 current_price。
2. fresh quote 且 realized_return_from_origin 可用时，以 `current_price / (1 + realized_return_from_origin)` 还原。
3. 从每组可用的 `closed.ma_5/10/20/60/120 * (1 + closed.ma_distance_n)` 还原已完成日K收盘。
4. 所有可用候选按 `abs(a-b) <= max(1e-8, 1e-8*max(abs(a),abs(b)))` 必须一致，否则抛 ContractViolation。
5. 无 P0 时所有依赖价格的 entry/add 只能 observation_only；不能再读数据库、ForecastResult 或网络补值。

A股盘前可以用 P0 计算开盘后的未来条件价，但 Pobs 仍为 None，不能把条件标为当前已经触发。下文公式中的 `price` 在当前求值时一律指 Pobs；只有派生未来价位时才可使用 P0。

### 12.1 TrendContinuation v1

- family：trend_continuation。
- 情景：bullish_continuation；status 不得 blocked/observation_only。
- required：ma20、ma60、atr、macd_hist。
- 结构：ma20 > ma60、price > ma20、macd_hist >= 0。
- trigger：`max(P0, ma20 * 1.002)`。
- stop：`min(ma60, trigger - 1.5 * ATR)`。
- take profit：`trigger + 2R`，同时保留 MA20 跌破的 conditional exit。

### 12.2 TrendPullback v1

- family：pullback_entry。
- 情景：bullish_continuation 或 bullish_pullback。
- 回踩区：`abs(price / ma20 - 1) <= max(0.01, 0.5 * atr_pct)`，且 price >= ma60、RSI 在 40 到 65。
- trigger：重新站上 `ma20 * 1.002`。
- stop：`min(ma60, ma20 - 1.25 * ATR)`。
- take profit：20日高点与 `trigger + 2R` 中较高且可验证者；缺失时使用 2R。

### 12.3 MA120SupportRebound v1

- family：support_rebound。
- 情景：允许 support_rebound 的 bullish_pullback、bearish_rebound、range_bound 或 mixed。
- 支撑区：`abs(price / ma120 - 1) <= max(0.015, 0.75 * atr_pct)`。
- trigger：触及支撑区后重新站上 `ma120 * 1.005`；没有事件序列时为 waiting。
- stop：`ma120 * (1 - max(0.02, 1.25 * atr_pct))`。
- bearish bias 下只能是 countertrend plan，不得标为趋势追涨。

### 12.4 RangeMeanReversion v1

- family：range_mean_reversion。
- 情景：range_bound。
- required：ma20、bb_pct20、bb_width20、rsi14、atr。
- 下轨：`ma20 * (1 - bb_width20 / 2)`；上轨同理。
- trigger：bb_pct <= 0.20、RSI <= 40，且价格重新站上 `lower_band * 1.005`。
- stop：`lower_band - ATR`。
- take profit：ma20 为第一目标、upper_band 为条件目标。

### 12.5 BreakoutConfirmation v1

- family：breakout_confirmation。
- 情景：bullish_continuation 或允许该家族的 range_bound。
- 20日高点：`P0 / (1 + closed.high_distance_20)`；分母必须大于 0。
- trigger：`high20 * 1.003`。
- volume confirmation：EOD 使用 closed_vol20，盘中使用 current_vol20，未来会话保存 session_volume 条件；量比 >= 1.20 才确认。量缺失时保守和激进方案都不得假装放量成立。
- stop：`breakout_level - 1.5 * ATR`。
- take profit：2R；跌回 breakout_level 下方为结构失效。

### 12.6 ProfitLockAfterHigh v1

- family：profit_lock，仅持仓。
- 峰值优先使用可验证 session high；否则使用20日高点。
- 启用条件：相对成本浮盈 >= `max(0.08, 3 * atr_pct)`。
- retreat threshold：`max(0.02, atr_pct)`。
- 从可验证峰值回撤达到阈值 -> reduce/partial_exit。
- session high 由 `current.price / (1 + current.retreat_from_session_high)` 还原；只有两个字段均 available 且分母大于0时可用。
- 成本价 <= 0、峰值或当前价缺失时生成等待/观察条件，不得声称已经盈利或冲高回落。

### 12.7 FailedReboundExit v1

- family：failed_rebound_exit，仅持仓。
- bearish_continuation/bearish_rebound 或 exit_posture=prioritize_protection 时启用。
- 跌破 MA20 后反抽未站稳并再次 crosses_below MA20 -> reduce。
- 进一步跌破 MA60 -> sell/full_exit。
- 缺事件序列时保存未来条件，不用单根收盘价伪造“反抽失败”。

### 12.8 ProtectiveExit v1

- family：protective_exit，仅持仓，任何 scenario 都运行。
- 候选保护线：正成本价的 `cost_price * 0.92` 与可用 `ma60 * 0.99` 中更高的有效值；无有效候选时只能 observation_only。
- 当前价已低于保护线时 sell 条件 triggered；否则为未来 hard-price 条件。
- V2-6 只能据此降级、拒绝或缩小仓位，不能改写或放宽该结构保护线；若未来需要账户级紧急退出，必须作为独立、可审计的风控覆盖合同设计。

### 12.9 ConditionalObservation v1

- family：observation，任何输入都运行。
- 汇总所有兼容模板的 missing conditions 和最近触发水平。
- 无 Champion、A股盘前缺价、缺成交量或样本不足时仍输出明确观察计划。
- observation 不得伪装为 buy/add，也不携带虚构 stop 或收益承诺。

## 13. 特征、新闻和基本面边界

- 策略只能读取当前 FeatureSnapshot 中已有字段，不能重新拉取行情或自己计算另一套指标。
- V2-5 初始模板以结构价格、均线、ATR、布林、RSI、MACD 和可验证量能为条件。
- 新闻和基本面已进入预测层；`unmodeled_fact_update=True` 时策略必须要求重新确认，不能自行解释利多利空。
- MomentumNews 不在首批可执行模板中。新闻方向策略必须等 V2-9 有独立 OOF 证据后再注册。
- 基本面缺失不阻断保护退出；也不能用缺失基本面拒绝止损。

## 14. Engine 顺序

1. 校验 StrategyInput、scenario 冻结时点和决策会话；不读取墙钟时间。
2. 按 StrategyFamily 声明顺序筛选注册模板。
3. 对 held position 无条件加入 protective/profit_lock/failed_rebound 模板。
4. 提取 feature map，缺失状态原样保留。
5. 模板生成原始计划和条件求值。
6. 删除完全相同业务 payload 的重复计划；不按 reason 文本排序。
7. 应用 conservative/aggressive confirmation overlay。
8. 检查 entry/exit 同时触发冲突，退出优先但不删除分歧证据。
9. 补齐四个 StrategyBranch。
10. 计算稳定身份并持久化。

策略模板之间不通过总分投票决定唯一答案。V2-5 保留候选计划；V2-6 用风险和证据分级，V2-8 再做组合排序。

## 15. A股、美股和三时段

策略公式对 A股和美股相同，市场差异只体现在输入能力和后续执行规则：

| 模式 | 行为 |
|---|---|
| EOD | 使用正式收盘 FeatureSnapshot，计划在下一 decision session 有效 |
| US PRE | 新鲜 Nasdaq/yfinance 价可求值价格条件；缺 OHLCV 的形态保持 unknown |
| A PRE | expected_missing 是正常能力边界；生成开盘后触发条件，不标记当前触发 |
| INTRADAY | 只使用 fresh TickFlow current facts；缺价时新开仓 observation_only，保护退出条件仍保留 |

A股 T+1、涨跌停和一手规则在 V2-6/V2-7 执行，不得散落到模板公式。美股延伸时段可否成交也留给 V2-7；V2-5 盘前只做常规会话计划。

## 16. V1 策略迁移矩阵

| V1 实现 | V2-5 处理 | 目标 |
|---|---|---|
| ThresholdTrend、MA60Trend、TrendRider、MACrossover | 合并 | TrendContinuation 的结构确认，不复制四套重复策略 |
| TrendPullback | 迁移 | TrendPullback v1 |
| MA120SupportRebound | 迁移 | MA120SupportRebound v1 |
| MeanReversion、PickBottom | 合并 | RangeMeanReversion / support rebound |
| BollingerBreakout、MACompressionBreakout | 合并 | BreakoutConfirmation v1 |
| DualThrust、TurtleATR | 暂缓 | 当前 FeatureSnapshot 缺精确通道路径；先列 feature gap，不做近似冒充 |
| MomentumNews、ChaseMomentum | 暂缓 | 等 V2-9 独立新闻策略 OOF |
| ProfitLockAfterHigh | 迁移 | ProfitLockAfterHigh v1 |
| PullbackFailedExit | 迁移 | FailedReboundExit v1 |
| PositionRiskManagement | 拆分 | 结构退出进 V2-5；集中度、现金和 sizing 进 V2-6/V2-8 |
| ConditionalTrigger | 迁移 | ConditionalObservation + 完整四分支 |
| KeyReversal | 暂缓 | 缺事件序列证据，不用单快照近似 |
| HoldUntilBreakeven | 不迁移为可执行策略 | 锚定成本且可能放任亏损；只保留历史对照，不得覆盖保护止损 |

“暂缓”不是丢失：必须记录 feature gap，并在未来增加特征/事件证据后作为新候选重新设计和 OOF 验证。

## 17. 身份和持久化

### 17.1 哈希

```text
position_hash = hash(PositionSnapshot canonical payload) or hash("flat")
plan_identity = {
  instrument, scenario_id, strategy_id, strategy_version, parameter_hash,
  family, action, quantity_intent, profiles,
  trigger/confirmation/stop/take_profit/hold/invalidation,
  valid_from, expires_at, position_hash, policy_version
}
plan_id = sha256(canonical_json(plan_identity))
session_identity = decision_session.session_date or "calendar-unavailable"
event_key = instrument.stable_key + session_identity + strategy_id + action + plan_id

bundle_id = hash(scenario_id + position_hash + ordered plan_ids + branch metadata + policy_version)
bundle_event_key = instrument.stable_key + session_identity + bundle_id
```

generated_at、自然语言解释和发行时求值结果不进入业务身份；求值必须由同一 scenario 和条件确定。改变 scenario、持仓、条件、参数、止损、止盈或 policy version 必须产生新身份。

### 17.2 migration 9

新增：

```text
trade_plans:
  plan_id PK, event_key UNIQUE, instrument_key, scenario_id,
  strategy_id, strategy_version, family, action, readiness,
  decision_session_date, payload_json, generated_at, schema_version

strategy_bundles:
  bundle_id PK, event_key UNIQUE, instrument_key, scenario_id,
  position_hash, payload_json, generated_at, schema_version
```

Repository：

```text
save_trade_plan / get_trade_plan / list_trade_plans
save_strategy_bundle / get_strategy_bundle / list_strategy_bundles
```

同业务 payload 仅 generated_at 不同为幂等；同 event_key 不同 payload 进入 quarantine，不覆盖。读取必须重建强类型合同并复核索引列和哈希。

## 18. Golden Cases

```text
SP00 合同、P0一致性、哈希和 generated_at 幂等
SP01 bullish continuation 生成趋势计划
SP02 bullish pullback 只生成回踩/支撑计划
SP03 bearish continuation 禁止新开仓但保留退出
SP04 bearish rebound 只能 countertrend support
SP05 range 生成均值回归和区间突破条件
SP06 forecast conflict 仅观察和保护退出
SP07 无 Champion 仍有完整条件观察
SP08 MA120 触及并重新站回生成条件计划
SP09 MA120 未触及时说明距离和缺失条件
SP10 冲高回落持仓生成 partial profit lock
SP11 无 session high 不伪造冲高回落
SP12 反抽失败需要 event_sequence
SP13 跌破 MA60 生成 full exit
SP14 所有 buy/add 均有 stop；无 stop 只能观察；顶层条件求值完整
SP15 fixed 2R 的价格与 stop 一致
SP16 dynamic/conditional 止盈不可伪造风险收益比
SP17 conservative/aggressive 同方向且不伪造价格
SP18 相同触发时合并 profiles
SP19 flat/held/held_unknown 四分支完备且动作适用
SP20 entry/exit 同时触发时退出优先并保留冲突
SP21 A股盘前无价只生成开盘条件
SP22 美股盘前缺 OHLCV 不确认量价形态
SP23 A/美股等价特征产生等价策略语义
SP24 单股质量缺失不污染另一股票
SP25 migration 9、幂等、冲突 quarantine、重启恢复
SP26 架构边界：无 V1/风控/成交/组合/学习/LLM/UI import
SP27 性能：1000 次纯内存完整 bundle 本机目标小于 1.5 秒
SP28 TradePlan 序列化不含账户 sizing、执行等级和订单字段
SP29 V1 迁移矩阵中的首批模板均有测试映射
```

## 19. 测试文件

```text
tests/v2/test_trade_plan_contract.py
tests/v2/test_strategy_conditions.py
tests/v2/test_strategy_engine_by_scenario.py
tests/v2/test_strategy_entry_templates.py
tests/v2/test_strategy_exit_templates.py
tests/v2/test_strategy_profiles.py
tests/v2/test_strategy_market_parity.py
tests/v2/test_strategy_repository.py
tests/v2/test_strategy_architecture.py
tests/v2/test_strategy_performance.py
tests/v2/test_strategy_scenario_matrix.py
```

测试不得联网、不得读取真实数据库、不得用 V1 策略生成期望答案。Golden Case 先固定输入与预期，再实现代码。

验收命令：

```bash
venv/bin/python -m pytest tests/v2/test_trade_plan_contract.py tests/v2/test_strategy_*.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

## 20. 实施顺序

1. SP00、SP14、SP28：先建立合同、条件 DSL 和分层边界。
2. SP01-SP07：实现 registry、engine 和 scenario family 路由。
3. SP08-SP16：实现首批入场/退出模板。
4. SP17-SP24：实现 profiles、分支完备、双市场与隔离。
5. SP25：migration 9 和 repository。
6. SP26-SP29：架构、性能和 V1 迁移审计。
7. 运行 V2-5、V2 全量和全项目回归，更新阶段状态。

## 21. 阶段停止点

V2-5 完成后必须停止。不得顺手实现：

- A/B/C/D、shares、position_pct、max_loss_amount。
- 市场 lot、T+1、涨跌停、税费、滑点和 OrderIntent。
- Tab3 组合排序、策略历史健康度、自优化、LLM 或 UI。

开始 V2-6 前必须另行制定 `ExecutionDecision`、真实账户估值、风险预算、硬/软约束、执行等级和市场规则的精确规范。

## 22. 实现验证记录

- P0：保护退出/持有/观察失效条件与触发条件已解耦；趋势回踩必须同时满足 MA20 区间上下界。
- P1：StrategySpec 参数实际进入公式；缺失、陈旧、阻断和历史不足状态进入结构化观察；三值 DSL、持久化嵌套时间戳幂等和动作合同已收紧。
- SP00-SP29 均有直接测试映射；1000 次无缓存完整 bundle 构建约 `1.30s`。
- 2026-07-14 验证：V2-5 专项及 migration `38 passed`；V2 全量 `218 passed, 3 skipped`；项目全量 `478 passed, 3 skipped`；真实 Provider 门控测试 `3 passed in 40.96s`。
