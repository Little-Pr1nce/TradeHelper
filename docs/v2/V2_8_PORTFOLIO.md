# TradeHelper V2-8 组合决策层精确设计

> 状态：设计冻结，待实现。本文是 V2-8 的规范性合同。实现必须建立在已完成并复审的 [V2_6_RISK.md](./V2_6_RISK.md) 与 [V2_7_EXECUTION.md](./V2_7_EXECUTION.md) 之上；不得修改单股预测、情景、TradePlan 或 ExecutionDecision 的业务结论，也不得提前实现 V2-9 学习、V2-10 LLM、V2-11 UI/报告或券商自动下单。

## 1. 阶段目标

V2-8 负责把 Tab3 同一账户、同一市场、同一分析时点的单股风险决定统一为组合级操作顺序和最终股数。它必须回答：

1. 哪些持仓风险必须优先处理？
2. 多个买入/加仓候选同时成立时，真实现金和风险容量分给谁？
3. 单票上限、总仓位、组合风险和高相关暴露分别限制了多少股？
4. 哪些关注股只能继续观察，哪些可能在退出成交后成为替换研究候选？
5. 保守与激进方案分别预留多少现金和最大计划亏损？

V2-8 不重新判断股票涨跌，不重新生成策略，也不连接券商。它只消费冻结事实和上游决定。

## 2. 固定运行顺序

Tab3 的生产主链固定为：

```text
同市场 AccountSnapshot + FrozenAccountValuation
  + 每只股票 TradingScenario + TradePlan + RiskDecisionBundle
  + 持仓风险快照 + 点时相关性证据
    -> PortfolioInputBatch
      -> PortfolioAllocator
        -> conservative PortfolioProfileDecision
        -> aggressive PortfolioProfileDecision
          -> 用户选择一个 profile
            -> PortfolioOrderAssembler
              -> final_requested_shares_by_decision_id
                -> V2-7 OrderIntentFactory
                  -> OrderIntent / no_order build records
```

关键约束：

- V2-6 `approved_shares` 是单计划上限，不是 Tab3 最终股数。
- V2-8 只能把它缩小到 `final_requested_shares`，不能放大。
- V2-7 必须在 V2-8 分配之后生成 Tab3 最终 OrderIntent；不能先生成最终订单再回写仓位。
- conservative 与 aggressive 是两套互斥方案，不能把两套股数相加。
- 预计卖出回笼资金在真实成交前不进入本轮可用现金。
- V2-7 当前预览和历史成交仍消费同一份最终 OrderIntent，不新增组合专用信号路径。

## 3. 阶段边界

### 3.1 V2-8 负责

- 单市场组合输入批次冻结和身份校验。
- 持仓/关注角色、保护退出、普通退出、买入、加仓、持有和观察的组合级排序。
- 真实现金争用、单票仓位、股票总仓位、组合 heat、高相关邻域和 A股整手约束下的最终股数。
- 多个相似入场候选去重，避免同一股票重复占用预算。
- 多个保护性退出计划的共享持股预留组。
- 当前组合风险指标、入场预留指标和替换研究候选。
- conservative/aggressive 两套独立组合结果。
- migration 12、幂等持久化、冲突隔离和强类型恢复。

### 3.2 V2-8 不负责

- 获取行情、新闻、基本面或分钟 K。
- 重新计算特征、预测、情景或策略触发条件。
- 把 C/D 级计划升级成可执行计划。
- 修改 V2-6 止损、最大亏损、A/B/C/D 或市场资格。
- 用组合排名反向修改单股历史证据。
- 假设卖出已经成交并提前复用资金。
- 评价实际组合收益、晋升参数或自动学习；这些属于 V2-9。
- 生成自然语言报告、UI 布局或 LLM 解释。
- 真实券商并发订单、资金锁定或原子成交。

## 4. 代码组织

只新增以下主模块，避免把组合逻辑散落到 risk、execution 或 UI：

```text
tradehelper_v2/
  contracts/
    portfolio.py          # V2-8 不可变合同、枚举、原因代码
  portfolio/
    __init__.py
    engine.py             # 单一组合决策入口，编排双 profile 但不访问 I/O
    evidence.py           # 持仓风险与相关性点时快照构建
    ranking.py            # 结构化字典序排序，不使用文本长度
    allocator.py          # 组合约束和最终股数
    replacement.py        # 只生成替换研究候选
    orders.py             # 分配结果 -> V2-7 requested_shares
  data/
    migrations/schema.py  # migration 12
    repository.py         # V2-8 save/get/list 与原子批写

tests/v2/
  portfolio_helpers.py
  test_portfolio_contracts.py
  test_portfolio_engine.py
  test_portfolio_evidence.py
  test_portfolio_ranking.py
  test_portfolio_allocator.py
  test_portfolio_replacements.py
  test_portfolio_orders.py
  test_portfolio_repository.py
  test_portfolio_architecture.py
  test_portfolio_performance.py
  test_portfolio_golden_cases.py
```

禁止复制 V1 `services/portfolio_service.py`。V1 只用于核对业务经验和固定案例。

## 5. 枚举和原因代码

```text
PortfolioRole:
  holding / watchlist

HoldingRiskStatus:
  quantified / unquantified / breached

CorrelationStatus:
  complete / partial / unavailable

AllocationStatus:
  allocated_now
  reserved_conditional
  shared_exit_reservation
  monitor_only
  blocked
  no_order

ReplacementStatus:
  research_after_exit
  watch_only
  rejected

PortfolioEvidenceGrade:
  high / medium / low / insufficient
```

V2-8 注册原因代码，排序去重后进入不可变身份：

```text
PORTFOLIO_ALLOCATED
PORTFOLIO_CONDITIONAL_RESERVATION
PORTFOLIO_PROTECTIVE_EXIT_PRIORITY
PORTFOLIO_EXIT_RESERVATION_SHARED
PORTFOLIO_EXIT_STATE_RECHECK_REQUIRED
PORTFOLIO_NO_ORDER_UPSTREAM
PORTFOLIO_NOT_SELECTED
PORTFOLIO_DUPLICATE_ENTRY_SUPPRESSED
PORTFOLIO_CASH_LIMITED
PORTFOLIO_SINGLE_POSITION_LIMITED
PORTFOLIO_TOTAL_EXPOSURE_LIMITED
PORTFOLIO_HEAT_LIMITED
PORTFOLIO_HEAT_EXHAUSTED
PORTFOLIO_HOLDING_RISK_UNKNOWN
PORTFOLIO_STOP_ALREADY_BREACHED
PORTFOLIO_HIGH_CORRELATION_LIMITED
PORTFOLIO_CORRELATION_EVIDENCE_MISSING
PORTFOLIO_CORRELATION_MULTIPLIER_APPLIED
PORTFOLIO_LOT_ROUNDED
PORTFOLIO_ZERO_CAPACITY
PORTFOLIO_INCOMPLETE_VALUATION
PORTFOLIO_EQUITY_ZERO
PORTFOLIO_PROFILE_SEPARATED
PORTFOLIO_EXIT_PROCEEDS_NOT_REUSED
PORTFOLIO_REPLACEMENT_RESEARCH_ONLY
PORTFOLIO_REPLACEMENT_SOURCE_EXIT_REQUIRED
PORTFOLIO_REPLACEMENT_TARGET_NOT_QUALIFIED
PORTFOLIO_HHI_WARNING
PORTFOLIO_VOLATILITY_UNAVAILABLE
PORTFOLIO_EVIDENCE_HIGH
PORTFOLIO_EVIDENCE_MEDIUM
PORTFOLIO_EVIDENCE_LOW
PORTFOLIO_EVIDENCE_INSUFFICIENT
PORTFOLIO_HARD_CONSTRAINT_IMMUTABLE
```

自然语言解释留给 V2-11，组合层不能保存“优质资产”“强烈推荐”等评价文字。

## 6. PortfolioPolicy

```text
PortfolioPolicy:
  policy_version = portfolio_policy_v1
  conservative_heat_cap = Decimal("0.04")
  aggressive_heat_cap = Decimal("0.06")
  absolute_heat_hard_cap = Decimal("0.08")
  high_correlation_threshold = Decimal("0.75")
  high_correlation_group_cap = Decimal("0.35")
  hhi_warning = Decimal("0.25")
  correlation_lookback_sessions = 90
  minimum_correlation_samples = 20
  unknown_correlation_multiplier = Decimal("0.50")
  annualization_sessions = 252
  allocation_method = lexicographic_waterfall_v1
  hard_constraint_version = portfolio_hard_constraints_v1
  parameter_hash
```

政策解释：

- heat 是所有持仓到各自有效止损的计划亏损，加本次新分配的增量计划亏损，占冻结权益的比例。
- 4%/6% 是 conservative/aggressive 的初始组合 heat 上限；8% 是不可被自动优化突破的绝对红线。
- V2-6 单票 25% 和股票总仓位 90% 继续是硬约束，V2-8 不复制另一套可漂移数值；请求必须携带同一 `RiskPolicy` 并验证版本/hash。
- 相关系数不低于 0.75 的直接邻域合计仓位不超过 35%，延续 V1 已验证规则。
- 相关性缺失不等于 0；新入场批准股数先乘 0.50，再继续应用其他限制。
- HHI 0.25 只产生组合分散警告，不单独强制卖出。
- 软参数未来只能由 V2-9 在 OOF/影子证据下生成候选版本；硬约束不能取消。

## 7. PortfolioCandidate

```text
PortfolioCandidate:
  candidate_id
  role: holding / watchlist
  trading_scenario: TradingScenario
  trade_plan: TradePlan
  execution_decision: ExecutionDecision
  plan_evidence: PlanEvidenceSnapshot | None
  market_rules: MarketRuleSet
  generated_at
  schema_version = 1
```

不变量：

1. scenario、plan、decision 的 instrument/scenario_id/plan_id 必须完全一致。
2. decision 的 action/quantity/profile 必须与 plan 一致。
3. market_rules 的市场、交易所和版本必须与 decision 一致，并在批次 `as_of` 有效。
4. evidence 若存在，instrument、strategy_id、strategy_version 和 parameter_hash 必须与 plan/decision 对应；`evidence.profile` 允许为 None，否则必须等于 decision.profile。
5. evidence 缺失时保留 candidate，但排序证据等级降低；不能补造期望收益。
6. role=holding 时 instrument 必须存在于 AccountSnapshot；role=watchlist 时不得已有持仓。
7. candidate 不能携带 LLM 文本、历史回测排行榜字符串或默认本金。
8. candidate_id 由 role、scenario_id、plan_id、decision_id、evidence_id 和 rule_version 生成；generated_at 不进入身份。

## 8. PortfolioInputBatch

```text
PortfolioInputBatch:
  batch_id
  market
  currency
  mode
  account_snapshot: AccountSnapshot
  valuation: FrozenAccountValuation
  risk_policy: RiskPolicy
  portfolio_policy: PortfolioPolicy
  risk_bundles: tuple[RiskDecisionBundle, ...]
  candidates: tuple[PortfolioCandidate, ...]
  watchlist: tuple[InstrumentId, ...]
  holding_risks: tuple[HoldingRiskSnapshot, ...]
  correlation_snapshot: PortfolioCorrelationSnapshot
  as_of
  generated_at
  schema_version = 1
```

批次不变量：

1. 一个批次只允许一个市场和一种账户币种：A股/CNY 或美股/USD。
2. A股和美股 Tab3 分别构建批次；没有可靠 FX 快照时禁止跨币种合并。
3. valuation.account_hash 必须等于 `stable_hash(account_snapshot)`，valuation_id 必须与所有 RiskDecisionBundle 一致。
4. 所有 risk bundle 必须来自同一账户、估值、模式时点和 risk policy。
5. candidates 必须一一覆盖所有 risk bundle decisions；C/D、hold、watch 和零股决定也不能丢失。
6. account captured_at、valuation_at、scenario.as_of、条件 evaluated_at、evidence cutoff/evaluated_at、holding risk captured_at 和 correlation cutoff_at 不得晚于 as_of；generated_at 是计算审计时间，可以晚于 as_of，但不能早于其源事实时间，也不进入业务身份。
7. watchlist 唯一、同市场、不得和 active positions 重复。
8. holding_risks 必须一一覆盖 active positions，不能多也不能少。
9. candidate instrument 必须属于 active positions 或 watchlist，不能混入外部股票。
10. COMPLETE valuation 才允许新增风险；INCOMPLETE/UNAVAILABLE 仍保留退出和观察，但所有 buy/add 分配为 0。
11. equity=0 时不允许使用默认 10 万或成本价估值；buy/add 分配为 0。
12. 所有 candidate 的 scenario.mode 必须等于 batch.mode 且 scenario.as_of 必须等于 batch.as_of；plan evaluations 和上游 decision 必须引用该 scenario 的同一冻结事实，不能混入另一次刷新结果。
13. correlation_snapshot.universe 必须恰好等于 active positions 与 watchlist 的并集，不能漏股或混入外部股票。
14. batch_id 由账户、估值、全部候选、风险快照、相关性、两套政策和 as_of 生成。

空持仓且空关注列表是合法批次，输出空组合结果，而不是异常。

## 9. HoldingRiskSnapshot

```text
HoldingRiskSnapshot:
  holding_risk_id
  instrument
  shares
  reference_price
  market_value
  stop_price: Decimal | None
  exit_friction_reserve: Decimal
  planned_loss_amount: Decimal | None
  status: quantified / unquantified / breached
  source_plan_id: str | None
  source_decision_id: str | None
  captured_at
  generated_at
```

计算规则：

```text
planned_loss_amount = max(reference_price - stop_price, 0) * shares
                      + exit_friction_reserve
```

- reference_price、shares 和 market_value 必须与同一 FrozenAccountValuation 一致。
- stop 来自当时已有的保护条款或同一批 TradePlan，组合层不能发明止损。
- HoldingRiskBuilder 只接受 `protective_decision_ids` 引用的 A/B full-exit TradePlan；价格依次取有效 `plan.stop.level` 或保护性 `plan.trigger_level`。reduce 不能冒充整仓止损，C/D 或缺价格的计划不能量化整仓风险。
- 同一持仓存在多个合格 full-exit 保护价时，取最低有效价格计算 heat，保留对应 source_plan_id/source_decision_id；这是保守损失上界，不按最高止损美化风险。
- stop < reference_price -> quantified，并按上式保存 planned_loss_amount。
- stop >= reference_price -> breached；planned_loss_amount=None，因为已经失效的 stop 不再构成未来亏损下界。
- 没有可量化 stop -> unquantified。
- 任一 active holding 为 unquantified 或 breached 时，组合 heat 分别为 incomplete 或 breached，本轮 buy/add 全部为 0；退出计划不受阻断。
- gap 可能使真实亏损超过 stop 计划值；结果必须保留 V2-6/V2-7 的跳空警告，不能称作绝对最大损失。

## 10. 点时相关性证据

### 10.1 InstrumentReturnRisk

```text
InstrumentReturnRisk:
  instrument
  sample_count
  start_session_date
  end_session_date
  annualized_volatility: Decimal | None
  adjustment_mode
  source_bar_hash
```

### 10.2 CorrelationPair

```text
CorrelationPair:
  left
  right
  coefficient: Decimal | None
  overlapping_samples
  status: complete / unavailable
```

### 10.3 PortfolioCorrelationSnapshot

```text
PortfolioCorrelationSnapshot:
  correlation_snapshot_id
  market
  universe
  instrument_risks
  pairs
  lookback_sessions = 90
  minimum_samples = 20
  return_method = simple_daily_close_return_v1
  annualization_sessions = 252
  cutoff_at
  status: complete / partial / unavailable
  source_batch_hash
  generated_at
```

规则：

- 只读取 cutoff_at 当时已经完成、可见的 CanonicalBar；不得读取盘中实时价或未来收盘。
- 同一 instrument 的序列必须使用一致复权口径；公司行动版本冲突时该股票证据不可用。
- 每只股票取最近 90 个有效会话，日简单收益率 `close_t / close_(t-1) - 1`。
- 每对股票按交易日期内连接；重叠收益少于 20 条时 coefficient=None。
- coefficient 使用 Pearson，量化为 Decimal 字符串保存；不得把缺失填成 0。
- 年化波动为日收益样本标准差乘 `sqrt(252)`；样本不足时为 None。
- 组合层不得联网；相关性快照由 use case 在进入 allocator 前构建。
- 新股历史不足时进入 partial/unknown multiplier，不因为“样本不足”伪造低相关。
- universe 必须恰好覆盖当前批次 active positions 与 watchlist；instrument_risks 和 pairs 只能引用该 universe。

## 11. 当前组合风险快照

```text
PortfolioRiskSnapshot:
  risk_snapshot_id
  market
  valuation_id
  equity
  cash
  invested_value
  invested_pct
  weights_by_instrument
  max_position_instrument
  max_position_pct
  hhi
  portfolio_annualized_volatility: Decimal | None
  planned_loss_amount: Decimal | None
  planned_loss_pct: Decimal | None
  high_correlation_pairs
  heat_status: complete / incomplete / breached
  evidence_grade
  reason_codes
  calculated_at
```

公式：

```text
weight_i = market_value_i / frozen_equity
invested_pct = sum(market_value_i) / frozen_equity
HHI = sum(weight_i ** 2)
planned_loss = sum(all HoldingRiskSnapshot.planned_loss_amount)  # 仅全部 quantified 时
planned_loss_pct = planned_loss / frozen_equity                  # 否则两者均为 None
```

任一 holding breached 时 heat_status=breached；否则任一 holding unquantified 时 heat_status=incomplete；只有全部 active holdings quantified 时才为 complete。不能只求已知部分的和再伪装成完整组合 heat。

组合年化波动仅在所有持仓波动和所需 pair correlation 可用时计算：

```text
variance = sum_i sum_j weight_i * weight_j * vol_i * vol_j * corr_ij
annualized_volatility = sqrt(max(variance, 0))
```

缺证据时 volatility=None，不使用 0。

## 12. 结构化排序

禁止使用 reason 文本长度、报告段落长度或 LLM 用词评分。排序使用可审计字典序，不把不同量纲强行加权成一个神秘总分。

### 12.1 持仓处理优先级

从高到低：

1. `protective_decision_ids` 中的 sell/reduce。
2. stop 已 breached 的持仓退出。
3. 其他 A/B sell。
4. 其他 A/B reduce。
5. hold。
6. add 进入新增风险候选池，不和退出优先级混排。

同一层内依次比较：

```text
approved_now > conditionally_approved
A > B > C > D
eligible > partially_eligible > recheck_required > blocked
current_position_pct descending
max_loss_amount descending
decision_id ascending  # 稳定最终 tie-break
```

保护性退出不因预测偏多、证据样本不足或关注股排名更低而消失。

### 12.2 买入/加仓排序

先按 instrument/profile 去重，只保留一个主入场候选参与资金分配；其他候选保留为 `monitor_only + PORTFOLIO_DUPLICATE_ENTRY_SUPPRESSED`。

主候选按以下字典序选择：

```text
ExecutionLevel: A > B
disposition: approved_now > conditionally_approved
EvidenceStatus:
  reliable_positive
  > positive_uncertain
  > insufficient_sample
  > unavailable
  > negative
  > conflicting
current_position_pct ascending, None last
confidence_low descending, None last
expected_net_return descending, None last
win_rate descending, None last
loss_ratio ascending
friction_ratio ascending
decision_id ascending
```

其中：

```text
loss_ratio = incremental_planned_loss / planned_position_value
friction_ratio = friction_reserve / planned_position_value
```

分母缺失或不大于 0 时对应指标排最后，不填 0。C/D、negative/conflicting 不得进入可分配池，即使排序字段看似较好。

每个 allocation 保存全部 `rank_components` 和最终 `rank`，V2-11 可以直接解释“为什么排在前面”。

## 13. PortfolioAllocator

### 13.1 两套 profile 分开运行

对 conservative 和 aggressive 分别运行完整算法：

- 只读取对应 profile 的 ExecutionDecision。
- 使用对应 4%/6% heat cap。
- 当前组合事实相同，但预留股数和现金独立。
- 两套结果不能共享 remaining_cash 可变状态。
- 用户最终只能选择一套进入 PortfolioOrderAssembler。

### 13.2 退出计划

- A/B sell/reduce 的 `final_requested_shares` 默认等于 V2-6 approved_shares，不受现金、总仓位、heat 或相关性限制缩小。
- 仍然受 V2-6 approved_shares、可卖数量和 V2-7 T+1/停牌/涨跌停最终检查限制。
- 同一 instrument/profile 的多个退出计划组成 `PortfolioReservationGroup(side=sell)`。
- group 的 `max_aggregate_shares` 不超过当前 position 和已知 sellable shares。
- 每个保护退出条件都保留，但 group 语义为 `first_fill_consumes_then_recheck`；不能把多个条件的股数相加成可卖总量。
- V2-8 不实现并发券商订单。V2-9 联合回放必须按事件顺序更新组合状态后再检查同组下一意图。

### 13.3 新增风险前置闸门

以下任一成立时，所有 buy/add 为 0，但候选和原因继续保存：

- valuation 非 COMPLETE。
- equity <= 0。
- 任一 active holding risk 为 unquantified 或 breached。
- 当前 planned_loss_pct 已达到对应 profile heat cap。
- 当前 invested_pct 已达到 90%。
- 当前 cash <= 0。

stop breached 的持仓必须排在退出首位；在真实成交并刷新账户，或新的有效保护 stop 重新量化风险前，不能批准任何新增风险。

### 13.4 可部署现金

```text
exposure_capacity = max(equity * 0.90 - invested_value, 0)
deployable_cash = min(frozen_cash, exposure_capacity)
```

不减去“预计卖出回笼金额”，也不使用成本价。每个买入候选的现金预留：

```text
reserved_cash(q) = cash_required(q, entry_price, market_rules)
```

`cash_required` 必须复用 V2-6 `risk.sizing.cash_required` 的 Decimal 公式，包含买入佣金、最低佣金和买入滑点预留。不得把 approved_shares 对应的 friction_reserve 除成固定每股成本，因为缩量后最低佣金不是线性的。

entry_price、stop_price 或 market rules 缺失的 A/B entry candidate 不得分配，进入 blocked；不能用当前报告价补造。

### 13.5 逐候选上限

按买入排序逐个计算以下股数上限，全部使用 Decimal 并按 MarketRuleSet.lot_size 向下取整：

```text
risk_approved_cap = decision.approved_shares
cash_cap = max_lot_q(cash_required(q, entry_price, market_rules) <= remaining_cash)
single_position_cap = floor_lot((equity * 0.25 - current_value) / entry_price)
total_exposure_cap = floor_lot((equity * 0.90 - current_invested - reserved_notional) / entry_price)
heat_cap = max_lot_q(incremental_loss(q) <= remaining_heat_amount)
correlation_cap = floor_lot((equity * 0.35 - correlation_neighborhood_value) / entry_price)
```

最终：

```text
base_cap = min(risk_approved_cap, cash_cap, single_position_cap,
               total_exposure_cap, heat_cap, correlation_cap)

if correlation evidence needed for this candidate is missing:
    base_cap = floor_lot(base_cap * 0.50)

final_requested_shares = max(base_cap, 0)
```

其中：

```text
incremental_loss(q) = q * (entry_price - stop_price)
                      + friction_reserve(q, entry_price, stop_price, market_rules)

correlation_neighborhood_value = current_candidate_position_value
                               + known_high_corr_existing_and_reserved_value
                               + unknown_pair_existing_and_reserved_value
```

`friction_reserve` 必须复用 V2-6 Decimal 公式。`max_lot_q` 先以单调上界估算，再按 lot 递减到完整公式满足约束；不能用线性每股成本绕过最低佣金。

细则：

- decision.incremental_planned_loss 只用于核对 approved_shares 的上游结果；缩量后的 cash、friction 和 incremental loss 必须按 q 重新计算。
- incremental loss 为 0 只允许保护性退出；entry 的风险值缺失或不正数时分配为 0。
- correlated exposure 是与当前 candidate coefficient >=0.75 的现有持仓市值，加本轮已经预留的直接高相关候选名义金额。
- coefficient <0.75，包括负相关，不进入 35% 邻域；但不提供“对冲保证”。
- 与 candidate 的 pair 缺失时，该 instrument 的现有/已预留名义金额按未知邻域计入 35% 上限，并额外对 base_cap 应用 0.50 multiplier；不能把缺失当作无限相关性容量。
- 任何上限为负按 0 处理。
- A股不足 100 股返回 0；美股按 rule lot，当前规则为整数股。
- 不能为了“至少给一手”把 0 强制变成一个 lot。

### 13.6 预留快照

V2-8 输出 `PortfolioReservationSnapshot`，不能把它命名为成交后估值：

```text
PortfolioReservationSnapshot:
  profile
  frozen_equity
  frozen_cash
  deployable_cash
  reserved_entry_cash
  remaining_cash
  reserved_entry_notional
  projected_invested_pct_at_reference_price
  current_planned_loss
  reserved_incremental_loss
  projected_heat_pct
  exit_release_estimate  # 只展示，不进入 remaining_cash
  evidence_grade
  reason_codes
```

`projected_*` 只表示按 V2-6 reference price 的预留约束，不代表真实成交价、真实盈亏或未来账户权益。

## 14. PortfolioAllocation

```text
PortfolioAllocation:
  allocation_id
  batch_id
  profile
  candidate_id
  instrument
  plan_id
  decision_id
  action
  level
  status
  rank
  rank_components
  approved_shares
  final_requested_shares
  current_position_value
  reference_entry_price
  reserved_cash
  reserved_incremental_loss
  estimated_position_pct
  reservation_group_id: str | None
  binding_constraints
  reason_codes
  generated_at
```

不变量：

- `0 <= final_requested_shares <= approved_shares`。
- buy/add 的 reserved_cash 和 incremental loss 可复算。
- blocked/monitor_only/no_order 的 final_requested_shares=0。
- allocated_now 只能对应 approved_now A/B。
- reserved_conditional 只能对应 conditionally_approved A/B。
- shared_exit_reservation 只能对应 sell/reduce A/B，且引用有效 sell group。
- C/D、hold/watch 必须保留 allocation 记录但不能有股数。
- rank_components 只保存结构化数值/枚举，不保存自然语言。

## 15. PortfolioReservationGroup

```text
PortfolioReservationGroup:
  group_id
  batch_id
  profile
  instrument
  side
  member_allocation_ids
  max_aggregate_shares
  consumption_policy = first_fill_consumes_then_recheck_v1
  reason_codes
```

- 当前 V2-8 只为同股票多个退出计划建立 sell group。
- member requested_shares 可以分别保留，但组合审计不得把它们相加。
- group 不允许跨 instrument、profile 或 side。
- 该结构是 V2-9 联合回放的必要输入，不代表已经支持券商并发订单。

## 16. PortfolioProfileDecision 与 Bundle

```text
PortfolioProfileDecision:
  profile_decision_id
  batch_id
  profile
  allocations
  reservation_groups
  holding_priority_allocation_ids
  entry_priority_allocation_ids
  blocked_allocation_ids
  current_risk_snapshot
  reservation_snapshot
  replacement_candidates
  evidence_grade
  reason_codes
  generated_at

PortfolioDecisionBundle:
  portfolio_bundle_id
  batch_id
  market
  account_hash
  valuation_id
  conservative
  aggressive
  portfolio_policy_version
  generated_at
  schema_version = 1
```

所有 tuple 按稳定 ID 排序；priority id tuple 保持业务排序，不再二次排序。bundle identity 不包含 generated_at。

### 16.1 单一编排入口

```text
PortfolioDecisionEngine.decide(
  batch: PortfolioInputBatch,
  generated_at,
) -> PortfolioDecisionBundle
```

- 先构建同一份 current risk snapshot，再分别调用 conservative/aggressive allocator，最后生成各自 replacement candidates。
- 两个 profile 共享不可变输入事实，不能共享 remaining cash、remaining heat 或其他可变分配状态。
- `generated_at` 由调用方注入，只用于审计，不进入业务身份；相同 batch 重跑必须得到相同 bundle/allocation/group/replacement IDs。
- engine、allocator、ranking 和 replacement 均不得访问数据库、网络、系统当前时间或 V1 模块。
- UI/use case 以后只能调用该入口或读取其持久化结果，不能在页面中再实现一套组合排序。

## 17. 替换研究候选

替换不是本轮订单，更不能成为强迫卖出现有持仓的理由。

```text
ReplacementCandidate:
  replacement_id
  profile
  source_instrument
  source_exit_allocation_id
  target_instrument
  target_entry_allocation_id
  status
  source_exit_reason_codes
  target_rank_components
  estimated_release_amount
  target_required_cash
  funding_shortfall_after_current_cash
  reanalysis_required = true
  reason_codes
```

生成条件：

1. source 必须是当前持仓，并且已经有独立 A/B sell/reduce allocation；不能因为 target 更好而反向创造 source 卖出。
2. target 必须是无持仓关注股，具有 A/B buy、非 negative/conflicting 证据和有效止损。
3. target 因当前现金/总仓位不足未完全分配时，才有替换研究意义。
4. estimated_release_amount 只按 source reference price 估算并明确标记；不得写入本轮 available cash。
5. source 真正成交后必须重新刷新 AccountSnapshot、估值、行情、策略和风险，再决定 target；不得自动串联卖出和买入。
6. 输出用“替换研究候选”，禁止使用“最优质资产”或“卖掉 A 买 B 必赚”。

## 18. PortfolioOrderAssembler

```text
PortfolioOrderAssembler.build(
  portfolio_bundle,
  selected_profile,
  plans_by_id,
  risk_bundles,
  calendar,
  execution_policy,
  requested_at,
) -> tuple[OrderIntentBundle, ...]
```

规则：

1. selected_profile 必须明确，只能选 conservative 或 aggressive 之一。
2. 为每个 risk bundle 构造完整 `requested_shares_by_decision_id`。
3. 对 allocated/reserved/shared_exit 使用 final_requested_shares。
4. 对 blocked/monitor/no_order 和未选中的 A/B candidate 显式传 0；不能省略后让 V2-7 回退 approved_shares。
5. 调用现有 V2-7 OrderIntentFactory，不复制其身份、整手、EOD 或 no_order 逻辑。
6. 最终 OrderIntent 的 requested_shares 必须与 PortfolioAllocation 完全一致。
7. additive integration 允许给 V2-7 新增 `EXEC_PORTFOLIO_NOT_ALLOCATED`：当 risk approved_shares>0 但组合 requested_shares=0 时使用；不得错误写成“风控未批准股数”。
8. 任何意图都必须保留原 account_hash、valuation_id、quality/evidence/rule/policy identity。
9. sell reservation group 随 PortfolioDecisionBundle 一同交给后续联合回放；当前不声称支持自动券商并发执行。
10. 每个 RiskDecisionBundle 的 requested map 必须显式覆盖全部 decision_id；所选 profile 使用对应 allocation，另一 profile 以及未选候选一律传 0，禁止触发 V2-7 的 None -> approved_shares 回退。
11. additive integration 必须修正 V2-7 OrderIntentFactory 的 A股 full-exit 取整：SELL/FULL_EXIT 且 requested_shares 等于批准的全部退出量时保留零股；buy/add/partial reduce 继续按 lot 向下取整。

## 19. 双市场规则

### A股

- 一个批次仅 CNY/A股。
- buy/add 与部分 reduce 的 final shares 按 candidate MarketRuleSet.lot_size 向下取整，通常为 100 股；计算结果小于一手时为 0，不能强制变成一手。
- full exit 保留 V2-6 批准的全部可卖股数，即使包含零股；V2-7 OrderIntentFactory 与最终市场检查都必须保留该语义。组合层不能把 150 股全退错误缩成 100 股，也不能把 150 股部分减仓错误扩大为全退。
- T+1、涨跌停、ST/板块分类、停牌和最低佣金仍由 V2-6/V2-7 处理。
- 同日买入不会因为组合有现金而变为可卖。

### 美股

- 一个批次仅 USD/美股。
- 当前按整数股 lot=1；不得套用 A股 100 股、T+1 当日禁卖或涨跌停。
- 盘前/盘后只形成低证据预留和 preview；无深度时不保证成交。

### 禁止跨市场合并

用户同时有 A股和美股账户时，调用 allocator 两次。V2-8 不接收 FX=1 的默认值，不计算跨币种相关性，也不让一个市场的现金资助另一个市场。

## 20. 证据等级

组合证据等级取最弱必要证据，不按平均分掩盖缺失：

```text
HIGH:
  valuation complete；所有持仓风险 quantified；所需相关性完整；上游决定无 recheck

MEDIUM:
  valuation/risk complete；部分候选为 conditional 或相关性样本刚达到门槛

LOW:
  相关性部分缺失并应用 0.50；或市场状态要求触发时复检

INSUFFICIENT:
  valuation incomplete/equity zero/持仓风险未知/身份冲突
```

单股证据不足只影响该 candidate 的排序/分配；valuation 或 active holding risk 不完整属于账户级约束，可以阻断全部新增风险，但不能删除其他股票退出计划。

## 21. migration 12 与 Repository

新增表：

```text
portfolio_input_batches:
  batch_id PK, event_key UNIQUE, market, currency, mode,
  account_hash, valuation_id, as_of, policy_version,
  payload_json, generated_at, schema_version

portfolio_decision_bundles:
  portfolio_bundle_id PK, event_key UNIQUE, batch_id, market,
  account_hash, valuation_id, policy_version,
  payload_json, generated_at, schema_version

portfolio_allocations:
  allocation_id PK, event_key UNIQUE, portfolio_bundle_id, batch_id,
  profile, instrument_key, decision_id, action, status,
  final_requested_shares, payload_json, generated_at, schema_version

portfolio_reservation_groups:
  group_id PK, event_key UNIQUE, portfolio_bundle_id, profile,
  instrument_key, side, max_aggregate_shares,
  payload_json, generated_at, schema_version

portfolio_replacement_candidates:
  replacement_id PK, event_key UNIQUE, portfolio_bundle_id, profile,
  source_instrument_key, target_instrument_key, status,
  payload_json, generated_at, schema_version
```

要求：

- migration 12 只新增，不修改 migration 1-11 SQL/checksum。
- apply_schema 重复执行幂等，schema_migrations version=12 只有一条。
- repository 提供 save/get/list，读取时重建强类型合同并复核所有索引列。
- `save_portfolio_result(batch, bundle)` 必须在单事务中保存 input batch、bundle、allocations、groups 和 replacements；任一失败全部回滚。
- 同 ID/同 event_key 同业务 payload（仅 generated_at 不同）为 idempotent。
- 同 ID 或 event_key 不同 payload 进入 quarantine，不覆盖 canonical 记录。
- 子记录集合必须与 bundle 双向完全一致；缺失、多余或引用其他 bundle 都视为损坏。
- 不保存可变 dict、float 金额、自然语言解释或 OrderIntent 的重复副本。

## 22. 禁止字段与架构检查

portfolio 合同和模块禁止：

```text
default_capital
fake_equity
llm_score
reason_text_score
guaranteed_profit
best_asset
expected_sale_cash_as_available
auto_execute
broker_order_id
```

架构边界：

- `portfolio/` 可以 import contracts、V2-7 OrderIntentFactory 和纯日历接口。
- `risk/`、`strategies/`、`forecast/` 不得反向 import portfolio。
- allocator 不得 import V1 `services/portfolio_service.py`、UI、report、learning 或网络 Provider。
- 排序和分配循环内不得访问数据库或网络。

## 23. 性能边界

- 100 个股票、500 个 candidate 的排序和双 profile 分配目标 <1 秒。
- 100 个股票的已冻结 correlation snapshot 消费目标 <200ms；相关性序列构建单独测试，不在 allocator 循环中重复计算。
- 500 allocations 的 repository 原子写入采用单事务，目标 <2 秒。
- 性能测试使用宽松 CI 上限并记录本机基线，不访问网络和用户数据库。

## 24. Golden Cases PO00-PO49

```text
PO00 合同、Decimal、枚举、哈希、generated_at 幂等和原因代码注册
PO01 单批次只允许一个市场/币种
PO02 AccountSnapshot、account_hash、valuation_id 必须一致
PO03 valuation 不完整只阻断新增风险，不删除退出
PO04 scenario/plan/decision/evidence/rule 身份错配拒绝
PO05 任一输入证据晚于 as_of 拒绝
PO06 watchlist 唯一且不能与 active holding 重复
PO07 candidates 完整覆盖 risk decisions，C/D/hold/watch 不丢失
PO08 真实 equity=0 不使用默认本金
PO09 batch/bundle/allocation 身份稳定，generated_at 不改变业务 ID

PO10 保护退出排在所有新增风险之前
PO11 预测偏多或样本不足不删除保护退出
PO12 C/D 和 no_order 生成 final_shares=0 的审计 allocation
PO13 同 instrument/profile 只选一个主 entry 参与资金分配
PO14 重复策略机会不重复占用现金和 heat
PO15 final_requested_shares 永不超过 V2-6 approved_shares
PO16 buy/add/部分reduce 按各自 MarketRuleSet lot 向下取整且0不强制一手；V2-7订单工厂和最终检查均保留A股full exit批准零股
PO17 预计卖出回笼资金不进入本轮 deployable_cash
PO18 多候选现金争用按排序逐个用含最低佣金的完整公式预留且总额不超 frozen cash
PO19 股票总仓位预留后不超过 90%

PO20 单票预留后不超过 25%
PO21 conservative/aggressive 按缩量后重算 friction/loss，heat 分别不超过4%/6%且绝不超过8%
PO22 active holding 风险未知时所有 entry 为0，退出保留
PO23 stop 已跌破时 planned loss 不伪装为0、退出优先且所有 entry 为0
PO24 correlation 只使用 cutoff 前完整日K和一致复权口径
PO25 pair 重叠样本少于20时 coefficient 保持缺失
PO26 corr>=0.75 的直接邻域预留后不超过35%
PO27 缺相关性时未知邻域仍受35%上限并应用0.50，不能把缺失当0
PO28 负相关/低相关不套35%高相关上限，也不宣称对冲保证
PO29 HHI>=0.25 产生警告但不凭 HHI 自动卖出

PO30 entry 排序 A 优先于 B
PO31 reliable_positive 优先于 uncertain/insufficient/unavailable
PO32 confidence_low、expected return、win rate 都来自结构化 evidence
PO33 loss/friction ratio 可复算，缺失排最后
PO34 完全相同 rank components 由 decision_id 稳定打破平局
PO35 holding/watchlist 角色不串线，watchlist 不生成 add/reduce
PO36 普通买入不会压过保护退出的操作顺序
PO37 replacement source 必须先有独立退出决定
PO38 replacement target 必须是合格 A/B 无持仓 entry
PO39 replacement 不自动复用卖出资金，成交后必须重算

PO40 输出不含“最优质资产/保证盈利”等字段
PO41 容量为0时候选可见但 allocation 明确 blocked
PO42 conservative/aggressive 现金、heat 和股数状态完全隔离
PO43 当前组合 equity/仓位/HHI 使用同一 FrozenAccountValuation 可复算
PO44 reservation snapshot 不把退出估算当成交后估值
PO45 PortfolioOrderAssembler 把每条 final shares 原样交给 V2-7
PO46 未选中的 A/B 显式传0并记录 EXEC_PORTFOLIO_NOT_ALLOCATED
PO47 同股票多个退出计划共享 reservation group，不重复累计可卖股数
PO48 migration 12、原子批写、幂等、quarantine 和强类型重启恢复
PO49 双市场隔离、架构禁止项和500 candidate 性能边界
```

每个编号必须有唯一具名可执行测试；不能只写注释、循环编号或用一条 smoke 冒充多个行为。

## 25. 测试命令

```bash
venv/bin/python -m pytest tests/v2/test_portfolio_contracts.py tests/v2/test_portfolio_engine.py tests/v2/test_portfolio_evidence.py tests/v2/test_portfolio_ranking.py tests/v2/test_portfolio_allocator.py tests/v2/test_portfolio_replacements.py tests/v2/test_portfolio_orders.py tests/v2/test_portfolio_repository.py tests/v2/test_portfolio_architecture.py tests/v2/test_portfolio_performance.py tests/v2/test_portfolio_golden_cases.py tests/v2/test_schema_migrations.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

V2-8 是纯冻结输入层，不需要新增真实 Provider 冒烟测试；但完整 V2 回归中现有 A股/美股 Provider tests 不能被破坏。测试不得联网、不得读取用户数据库、不得 import V1。

## 26. 实施顺序

1. PO00-PO09：portfolio contracts、Policy、输入批次、单一 engine 入口与完整身份链。
2. PO24-PO29：持仓风险、点时相关性和当前组合风险快照。
3. PO30-PO36：结构化 ranking 与候选去重。
4. PO10-PO23：双 profile allocator、退出优先和五类容量约束。
5. PO37-PO44：替换研究候选和 reservation snapshot。
6. PO45-PO47：PortfolioOrderAssembler 与 V2-7 additive integration。
7. PO48：migration 12 和 repository 原子写入。
8. PO49：架构与性能。
9. 跑 V2-8 专项、V2 全量、项目全量，更新阶段状态后停止。

## 27. 阶段停止点

V2-8 完成后停止。不得顺手实现：

- 预测账、策略账、组合联合账或到期收益归因。
- 参数晋升、回滚、自动调优或历史健康度。
- LLM 研究假设、解释或自然语言排序。
- Tab3 页面、报告排版或券商自动下单。

开始 V2-9 前必须另行冻结预测账、策略账、风险账、成交账、组合账的事件键、到期验证、反事实归因和防重复计样本合同。
