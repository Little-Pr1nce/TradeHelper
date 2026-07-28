# TradeHelper V2-9 学习层精确设计

> 状态：已实现并复审。本文是 V2-9 的规范性合同。实现建立在已完成并复审的 V2-3 预测、V2-4 情景、V2-5 策略、V2-6 风控、V2-7 成交仿真和 V2-8 组合决策合同之上。V2-9 只负责到期验证、分账归因、历史 OOF 回放、健康度和受控候选生命周期；不得提前实现 V2-10 LLM、V2-11 UI/报告、券商自动下单或自动改写生产源码。

## 1. 阶段目标

V2-9 必须让系统稳定、可审计地回答：

1. 某次预测具体预测了哪个目标交易日，实际结果是什么，概率是否可信？
2. 在当时预测和情景已经给定的前提下，策略计划是否触发、是否有正期望？
3. 风控、成交约束和组合分配分别减少了多少损失，或牺牲了多少收益？
4. 当前股票、策略、参数和市场状态的证据是否足以支持 A/B/C/D 分级？
5. 哪个候选版本可以进入影子观察、晋升、回滚或停止新增风险？

核心原则：

```text
预测越来越准 != 强行提高方向正确率
策略表现越来越好 != 在同一历史区间反复调参
自动优化 != 保证收益单调上升
```

系统只接受成熟样本和样本外证据。候选没有改善时保留旧版本；所有可靠版本都失效时，停止新增风险或降为观察，不能为了“始终有结果”启用负期望候选。

## 2. 固定学习链路

### 2.1 在线到期验证

```text
已发行 ForecastResult / TradingScenario / TradePlan
  + ExecutionDecision / PortfolioAllocation / OrderIntent
    + target session 后才可见的 CanonicalBar / ExecutionEvent
      -> MaturityResolver
        -> ForecastOutcome
        -> ScenarioOutcome
        -> StrategyOutcome
        -> JointOutcome
          -> 三本账汇总
            -> PlanEvidenceSnapshot / 健康度
```

### 2.2 历史 OOF 回放

```text
point-in-time 历史事实
  -> purged walk-forward folds
    -> 每折只用训练前缀选择预测模型和策略参数
      -> 冻结该折版本
        -> 测试折复放 V2-3 -> V2-8 同一主链
          -> V2-7 历史成交仿真
            -> OOF 三本账与配对反事实归因
```

在线到期记录和历史重建 OOF 必须分别标记，不能合并冒充真实历史预测次数。当前分析前台不等待候选搜索；V2-9 只提供可取消的确定性学习任务合同，任务调度和进度 UI 留给 V2-11/12。

## 3. 阶段边界

### 3.1 V2-9 负责

- 交易所日历驱动的 1/3/5/10 日预测到期验证。
- 预测账、策略账、联合账的不可变事件、修订和汇总。
- Forecast、Scenario、Strategy、Risk、Execution、Portfolio 六层分离归因。
- 单股票优先、行业/市场只作样本不足 fallback 的健康度。
- 使用同一 TradePlan、ExecutionDecision、PortfolioAllocation 和 V2-7 成交路径做历史 OOF。
- 预测 Brier、Log Loss、ECE、区间命中和分市场状态表现。
- 策略净收益、胜率、MAE/MFE、退出质量、Sharpe、Calmar 和回撤。
- 联合组合净收益、基准、Alpha、回撤、风险利用和成交差异。
- 候选、影子、挑战者、Champion、漂移、回滚和停用生命周期。
- 只在预注册搜索空间内调整模型配置、特征子集、策略参数和软政策。
- migration 13 建表、纠错 migration 14、幂等持久化、修订链、原子晋升和强类型恢复。

### 3.2 V2-9 不负责

- 发明新的模型算法、技术指标、策略模板或条件 DSL 算子。
- 读取 LLM 文本并生成候选；这属于 V2-10。
- 修改历史 ForecastResult、TradePlan、ExecutionDecision 或 PortfolioDecisionBundle。
- 用未来数据重算并覆盖当时的预测、情景或计划。
- 取消止损、最大亏损、账户权益、数据质量、市场规则或 A股 T+1 等硬约束。
- 把盘中/延伸快照写入正式日 K。
- 将行业或其他股票样本冒充当前股票 A 级证据。
- 把未触发计划计为成功交易，或把拒单计为策略亏损。
- 生成报告、图表、自然语言解释或页面。
- 直接连接券商或宣称真实成交。

## 4. 代码组织

```text

  contracts/
    learning.py          # 不可变结果、账本、候选、生命周期合同
  learning/
    __init__.py
    maturity.py          # 目标交易日、成熟度、修订事实解析
    metrics.py           # 概率、收益、回撤、置信区间纯函数
    ledgers.py           # 三本账写入输入和切片聚合
    attribution.py       # 六层配对反事实归因
    replay.py            # purged walk-forward 全链 OOF 编排
    evidence.py          # ledger -> PlanEvidenceSnapshot
    optimizer.py         # 预注册搜索空间候选评估
    lifecycle.py         # 影子、晋升、漂移、回滚、停用
    engine.py            # 单一学习入口，不访问 UI/LLM
  data/
    migrations/schema.py # migration 13 建表；migration 14 修正成熟度唯一身份
    repository.py        # outcome/ledger/candidate 原子持久化

tests/v2/
  learning_helpers.py
  test_learning_contracts.py
  test_learning_maturity.py
  test_learning_metrics.py
  test_learning_ledgers.py
  test_learning_attribution.py
  test_learning_replay.py
  test_learning_evidence.py
  test_learning_optimizer.py
  test_learning_lifecycle.py
  test_learning_repository.py
  test_learning_architecture.py
  test_learning_performance.py
  test_learning_golden_cases.py
```

禁止复制 V1 `prediction_log`、`trade_plan_log` 或 `core/joint_oof.py`。V1 只用于提取已验证的指标口径和回归案例；V2 必须围绕冻结合同重建。

## 5. 枚举与原因代码

```text
EvidenceOrigin:
  issued_online / reconstructed_oof / shadow_online

OutcomeStatus:
  pending / matured / unverifiable / conflicting / superseded

LearningEvidenceGrade:
  high / medium / low / insufficient

LedgerKind:
  forecast / strategy / joint

CandidateKind:
  forecast_configuration
  scenario_soft_policy
  strategy_parameter_set
  risk_soft_policy
  portfolio_soft_policy
  execution_soft_policy

CandidateScope:
  stock / industry / market

JointOutcomeKind:
  recommendation_replay / policy_oof / broker_observed

CandidateLifecycle:
  candidate / challenger / shadow / champion / drifted / retired / rolled_back

PromotionDecision:
  hold / promote_to_shadow / promote_to_challenger / promote_to_champion / reject / rollback / suspend_new_risk

LearningRunStatus:
  pending / running / completed / failed / cancelled
```

至少注册以下原因代码；业务身份内必须排序去重：

```text
LEARNING_PENDING_TARGET_SESSION
LEARNING_MATURED
LEARNING_TARGET_BAR_MISSING
LEARNING_TARGET_BAR_NOT_FINAL
LEARNING_CALENDAR_UNAVAILABLE
LEARNING_LISTING_WINDOW_INSUFFICIENT
LEARNING_ADJUSTMENT_MISMATCH
LEARNING_EVIDENCE_CONFLICT
LEARNING_REVISION_SUPERSEDED
LEARNING_DUPLICATE_IGNORED
LEARNING_ISSUED_ONLINE
LEARNING_RECONSTRUCTED_OOF
LEARNING_SHADOW_ONLY
LEARNING_FORECAST_SCORED
LEARNING_FORECAST_UNAVAILABLE_NOT_SCORED
LEARNING_SCENARIO_ATTRIBUTED
LEARNING_PLAN_NOT_TRIGGERED
LEARNING_PLAN_TRIGGERED
LEARNING_ORDER_REJECTED
LEARNING_ORDER_FILLED
LEARNING_DAILY_PATH_AMBIGUOUS
LEARNING_EXECUTION_EVIDENCE_LOW
LEARNING_PORTFOLIO_SEQUENTIAL_REPLAY
LEARNING_COUNTERFACTUAL_PAIRED
LEARNING_COUNTERFACTUAL_UNAVAILABLE
LEARNING_STOCK_SCOPE
LEARNING_INDUSTRY_FALLBACK
LEARNING_MARKET_FALLBACK
LEARNING_SAMPLE_INSUFFICIENT
LEARNING_POSITIVE_UNCERTAIN
LEARNING_RELIABLE_POSITIVE
LEARNING_NEGATIVE_EXPECTATION
LEARNING_DRIFT_DETECTED
LEARNING_CANDIDATE_WITHIN_BOUNDS
LEARNING_CANDIDATE_OUT_OF_BOUNDS
LEARNING_SELECTION_PASSED
LEARNING_CONFIRMATION_PASSED
LEARNING_SHADOW_PASSED
LEARNING_PROMOTED
LEARNING_REJECTED
LEARNING_ROLLED_BACK
LEARNING_NEW_RISK_SUSPENDED
LEARNING_HARD_CONSTRAINT_IMMUTABLE
LEARNING_SOURCE_CODE_IMMUTABLE
```

## 6. 学习政策 LearningPolicy

`LearningPolicy` 必须不可变、带 `policy_version` 和 `parameter_hash`，默认值冻结为：

```text
ledger_version = learning_ledger_v1
forecast_horizons = (1, 3, 5, 10)
calibration_bins = 10
bootstrap_draws = 1000
bootstrap_block_min = 5
min_observation_samples = 10
min_reliable_samples = 30
min_oof_folds = 3
min_confirmation_samples = 20
min_shadow_samples = 20
strategy_evaluation_horizons = (1, 3, 5, 10)
selection_fraction = 0.60
embargo_sessions = max(horizon, 5)
drift_recent_samples = 30
drift_reference_samples = 60
max_candidate_count_per_scope = 20
max_foreground_learning_ms = 0
```

硬边界：

- `min_reliable_samples` 不得低于 30。
- OOF 至少 3 个顺序折；不能随机打乱时间序列。
- 训练样本必须在该折测试起点前成熟，并额外剔除 embargo 窗口。
- 候选池每个股票/周期/类别最多 20 个，避免无界数据挖掘。
- 随机种子由候选身份和数据哈希稳定派生。
- 所有 Bootstrap 使用时间块，不能把相邻交易日当独立样本。
- 学习任务不得阻塞当前分析主链。

## 7. 到期事实与修订

### 7.1 MaturityEvidence

必须保存：

```text
evidence_id
instrument
origin_session_date
target_session_date
reference_adjustment_mode
reference_price
target_bar_key
target_price
actual_return
actual_direction
flat_band
bar_source
bar_payload_hash
bar_fetched_at
available_at
evaluated_at
status
evidence_grade
revision
supersedes_evidence_id
reason_codes
```

规则：

1. 目标日来自 ForecastResult 已冻结的 `target_session_date`，不得用自然日加 N。
2. 仅使用目标交易日已完成、与参考价相同复权口径的正式日 K 收盘价。
3. `target_bar.fetched_at/available_at` 必须不晚于 `evaluated_at`，但必须晚于目标会话完成。
4. 预测发行时不可用的数据不能回填到原预测，只能进入 outcome。
5. 新股上市日前记录、非正式 K、实时快照和复权冲突均不可验证。
6. Provider 后续修订时新增 revision，并令旧 revision 为 `superseded`；汇总只计最新无冲突 revision 一次。
7. 同一 revision 身份相同但 payload 不同必须 quarantine，不能静默覆盖。

## 8. 预测账 ForecastLedger

### 8.1 ForecastOutcome

每个可用 ForecastResult 到期后生成一条结果：

```text
forecast_outcome_id
forecast_event_key
instrument / market
origin_session_date / target_session_date / horizon
model_scope / scope_key / model_family / model_version
feature_set_id / model_input_hash / training_data_hash
evidence_origin
maturity_evidence_id
predicted_direction
probabilities
predicted_p10 / predicted_p50 / predicted_p90
actual_direction / actual_return / actual_price
direction_correct
event_brier
event_log_loss
interval_hit
absolute_return_error
market_regime_key
status / evidence_grade / reason_codes
evaluated_at / generated_at
```

单事件公式：

```text
event_brier = sum((p_class - y_class)^2 for 3 classes)
event_log_loss = -log(max(p_actual_class, 1e-15))
interval_hit = p10 <= actual_return <= p90
absolute_return_error = abs(p50 - actual_return)
```

不可用 ForecastResult 只记录覆盖率和不可用原因，不计算零分，也不进入模型准确率分母。

### 8.2 预测汇总

至少按以下切片聚合，所有指标必须带样本数和数据截止日：

```text
instrument + horizon + model_version
instrument + horizon + market_regime_key
industry + horizon + model_version
market + horizon + model_version
```

指标固定为：Brier、Log Loss、方向正确率、ECE、80%区间命中、P50 绝对误差、覆盖率和时间块 Bootstrap 区间。Brier 是主指标；方向正确率只能辅助阅读，不能单独晋升模型。

预测模型比较必须在完全相同的 OOF 事件集合上与经验基线配对。沿用 V2-3 校准护栏：候选不能以更差的 Log Loss、ECE 或异常区间覆盖换取轻微 Brier 改善。

## 9. 情景归因 ScenarioOutcome

情景层不单独做“赚钱”评价。`ScenarioOutcome` 必须引用原 `scenario_id` 和四个 horizon 的 ForecastOutcome，评价：

- 当时多周期概率是否被翻译为正确的主要 bias/state。
- 当前价覆盖是否在当时可见且没有污染 EOD 预测。
- mixed/transitional 是否诚实表达周期分歧。
- 该情景允许的策略家族是否与冻结政策一致。

情景正确性使用版本化 `ScenarioOutcomePolicy` 映射实现，不用自然语言关键词。任一关键 horizon 未成熟时保持 pending；不可验证不按错误计。

## 10. 策略账 StrategyLedger

### 10.1 评价对象

策略账评价“给定当时 Scenario 后，这个 TradePlan 本身是否有效”，不得使用最终组合股数冒充策略质量。已发行计划使用当时 V2-6 已批准的单计划股数做隔离建议回放；若 V2-6 为 C/D 或 0 股，只记录不可执行原因，不构造虚假成交。候选参数的 reconstructed OOF 必须使用版本化 `ReplayAccountPolicy` 和逐折账户状态重新经过 V2-6，禁止读取用户当前账户，也禁止用默认 10 万元结果生成当前建议。

`StrategyOutcome` 必须保存：

```text
strategy_outcome_id
plan_id / scenario_id / decision_id
instrument / action / family
strategy_id / strategy_version / parameter_hash / profile
evidence_origin
evaluation_horizon / target_session_date
valid_from / expires_at
trigger_state / trigger_at
fill_outcome / fill_price / filled_shares
exit_type / exit_at / exit_price
gross_return / net_return / benchmark_return / excess_return
max_adverse_excursion / max_favorable_excursion
holding_sessions / commission / tax / slippage
exit_avoided_loss / exit_opportunity_cost / exit_quality
execution_evidence_grade
status / reason_codes
```

### 10.2 入场计划口径

- 未触发：记录 `not_triggered`，不算胜、不算亏，不进入已成交收益均值。
- 触发但拒单：归入 Execution，不记为策略亏损。
- 成交：使用同一 V2-7 触发、费用、滑点和市场规则路径。
- 有明确止损/止盈：按事件顺序先发生者退出。
- 单条已发行入场计划没有量化止盈时，不得事后发明动态止盈；在各 `evaluation_horizon` 的目标日完成日 K 收盘，标记 `window_close`。
- `window_close` 只有在提供可审计的预计总退出成本时才能计算净收益；该成本必须覆盖预计佣金、税费和滑点，缺失时为 `unverifiable`，不得把卖出成本静默当作零。
- 全链 policy OOF 每个会话重新运行 V2-3 至 V2-8，因此后续会话真实生成的减仓/卖出 TradePlan 可以退出；这与单条计划到期评价分开。
- 日 K 同时穿越止损和止盈但无分钟顺序时，沿用 V2-7 adverse path，证据为 low，不能单独支持晋升路径敏感策略。

每条计划分别生成 1/3/5/10 日 outcome，身份包含 `evaluation_horizon`，不同周期不能重复计数。`PlanEvidenceSnapshot` 的主评价周期由静态版本化映射选择：趋势延续/突破为 10 日，回调/MA120/均值回归为 5 日，普通退出/保护退出为 5 日；其他周期作为稳定性护栏展示，不能现场挑表现最好的周期。

### 10.3 退出计划口径

退出策略独立评价：

```text
exit_avoided_loss = max(0, -post_exit_underlying_return)
exit_opportunity_cost = max(0, post_exit_underlying_return)
exit_quality = exit_avoided_loss - exit_opportunity_cost - exit_friction
```

分别记录 1/3/5/10 日退出后标的收益。风险退出、普通退出和入场策略不得混在同一健康度中。

### 10.4 策略汇总

按 `instrument + strategy_id + strategy_version + parameter_hash + profile + action_family + regime` 聚合：

- 已触发数、已成交数、拒单数、未触发数。
- 平均/中位净收益、胜率、盈亏比、期望值。
- MAE、MFE、最大回撤、Sharpe、Sortino、Calmar。
- 交易成本占毛收益比例。
- 时间块 Bootstrap 80%/95% 区间。
- 入场与退出分别对应的基准比较。

少于 10 个 OOF 成交样本为 unavailable；10-29 为 insufficient；至少 30 且净期望为正、80%置信下界不低于 0 才可形成 `reliable_positive`。该状态必须与 V2-6 `PlanEvidenceSnapshot` 规则一致。

## 11. 联合账 JointLedger

### 11.1 评价对象

联合账评价用户最终能看到的链路：

```text
Forecast -> Scenario -> TradePlan -> ExecutionDecision
  -> PortfolioAllocation -> OrderIntent -> ExecutionRun
```

`JointOutcome` 至少保存：

```text
joint_outcome_id
outcome_kind
portfolio_bundle_id / profile / batch_id
account_hash / valuation_id / market / currency
ordered_allocation_ids / intent_ids / execution_run_ids
evidence_origin / replay_window
starting_equity / ending_equity / net_cash_flow
time_weighted_return / benchmark_return / alpha
max_drawdown / volatility / Sharpe / Calmar
realized_friction / planned_loss / realized_loss
entry_count / exit_count / rejected_count
risk_contribution / execution_contribution / portfolio_contribution
status / evidence_grade / reason_codes
```

不同币种、市场和 profile 独立记账；未经可靠 FX 不合并。保守和激进方案是互斥反事实，不得相加。

没有券商成交回执时，`issued_online` 只能生成 `recommendation_replay`，表示“若按 V2-7 假设成交，建议会怎样”，不得称为用户真实盈亏。`policy_oof` 表示历史样本外政策回放。只有未来接入可信券商回执后才能生成 `broker_observed`；V2-9 不实现该连接。

### 11.2 顺序回放

- 同一交易日严格按事件时间和 V2-8 操作优先级更新现金、持仓和可卖数量。
- 卖出未成交前不得复用回款。
- A股 T+1、整手买入、零股全退、涨跌停和费用继续由 V2-7 执行。
- 组合收益使用 time-weighted return；只有明确外部现金流时才做现金流调整。
- 基准必须与市场、币种和同一测试窗口一致；缺少基准时 Alpha 保持缺失。

## 12. 六层配对归因

归因必须比较同一事件集合、同一价格路径和同一费用版本：

| 层 | 事实问题 | 主指标 | 允许优化 |
|---|---|---|---|
| Forecast | 概率和区间是否可信 | Brier/Log Loss/ECE/coverage | 注册模型、特征子集、校准参数 |
| Scenario | 多周期预测是否翻译正确 | state/bias 一致性 | 预注册情景软阈值 |
| Strategy | 给定情景后计划是否有正期望 | net expectancy/drawdown | 预注册模板参数 |
| Risk | 风控缩量/驳回是否改善尾部风险 | avoided loss - opportunity cost | 软倍率，不动硬约束 |
| Execution | 费用和成交假设造成多少差异 | fill gap/friction | 有真实证据时的软滑点参数 |
| Portfolio | 跨股票分配是否改善组合 | TWR/alpha/drawdown/heat | 排序与相关性软参数 |

固定反事实：

```text
strategy_path: V2-6 approved_shares 的隔离成交结果
risk_path: 同一计划在冻结硬约束下，比较当前软缩量与允许范围内的基准软政策
execution_path: 同股数理论中间价成交 vs V2-7 费用/滑点/规则成交
portfolio_path: V2-6 单票批准集合 vs V2-8 最终顺序分配
joint_path: V2-8 最终股数的顺序成交结果
```

无法构造合法反事实时必须写 `counterfactual_unavailable`，不能填 0。硬约束永远保留在所有反事实中，因此不会用“取消止损或突破仓位上限后赚得更多”证明风控有害。

## 13. OOF 回放合同

### 13.1 FoldDefinition

每折保存：

```text
fold_id
market / scope / scope_key
train_start / train_end
embargo_start / embargo_end
test_start / test_end
data_cutoff_at
training_event_hash
selected_forecast_versions
selected_strategy_parameter_hashes
risk_policy_version / execution_policy_version / portfolio_policy_version
```

规则：

1. 只允许 expanding-window 或明确版本化 rolling-window。
2. 所有训练标签必须在 `train_end` 前成熟。
3. `embargo_sessions >= max(预测周期, 5)`，避免重叠标签泄漏。
4. selection 和 confirmation 时段不重叠；最终只在未参与选择的测试折记 OOF。
5. 股票上市日期裁剪训练与测试窗口；上市前数据绝不进入回放。
6. 行业成员关系必须是 point-in-time；当前行业标签不能回填历史。
7. 每折从冻结 FeatureSnapshot 重放，不可用今天重新下载的新闻/基本面替代历史快照。
8. reconstructed OOF 不得显示为“系统当时真的预测过”。

`ReplayAccountPolicy` 是 OOF 请求的必填项且没有默认本金，必须声明 `user_frozen_snapshot` 或 `standardized_research_notional`、初始现金、币种、外部现金流和 policy version。标准化研究本金只用于回放可比性，绝不能进入当前仓位建议，也不能在 UI 中冒充用户账户；没有可审计账户路径时，组合联合 OOF 保持 unavailable，单计划百分比收益仍可独立评价。

### 13.2 回放完备性

预测模型没有 Champion 时，可以输出经验基线和不可执行状态；策略仍输出观察/条件计划。回放不能因为没有候选通过而中断整个股票，必须记录“评估过但未改善/样本不足/数据不可验证”的具体原因。

## 14. 股票绑定与分层 fallback

学习结果优先级固定为：

```text
股票 + 周期 + 模型/策略版本 + 市场状态
  -> 股票 + 周期 + 模型/策略版本
    -> point-in-time 行业 + 周期（仅观察 fallback）
      -> 市场 + 周期（仅基线 fallback）
```

- 股票证据不足时，行业/市场结果可以帮助生成候选，但不能把当前股票直接升为 A。
- 一个股票的负期望不得自动降权其他股票的同名策略。
- 行业样本必须来自当时已知成员关系；缺失时跳过行业层。
- A股和美股的学习状态、交易日历、费用和基准完全隔离。
- 同一股票代码在不同市场/交易所必须用 `InstrumentId.stable_key` 隔离。

生产投影键固定为：预测 `market + scope + scope_key + horizon`；策略 `instrument + strategy_id + action_family + profile`；风险/成交/组合软政策 `market + profile`。股票策略没有健康 Champion 时回退到冻结默认 StrategySpec 并保持 B/观察证据，不得拿其他股票 Champion 直接执行。

## 15. 候选搜索空间

### 15.1 允许自动变化

- V2-3 已注册 ModelSpec 的模型族、特征子集和受控超参数。
- V2-5 已注册 StrategySpec 的参数，且必须落在静态 `OptimizationSpace` 边界内。
- 情景、风控、成交、组合政策中明确标记为 `soft_tunable` 的字段。
- 候选权重和温度校准参数。

### 15.2 禁止自动变化

- 新增 Python 文件、函数、模型族、指标、策略模板或 DSL 算子。
- 修改止损必需性、风险金额硬上限、账户权益来源、数据质量门槛。
- 修改 A股 T+1、整手、涨跌停或市场费用事实。
- 将 LLM 文本、新闻摘要或无来源财务数字直接写成参数。
- 为当前全量历史最优而现场生成无界参数。

每个 `OptimizationSpace` 必须列出参数类型、最小值、最大值、步长、默认值和是否按市场不同。未知参数直接拒绝。

## 16. 候选晋升、漂移与回滚

### 16.1 生命周期

```text
candidate
  -> selection OOF 通过
    -> challenger
      -> confirmation OOF 通过
        -> shadow
          -> 到达 min_shadow_samples 且不恶化硬护栏
            -> champion
```

ForecastModelVersion 仍是不可变模型 artifact；`LearningCandidateVersion` 记录其部署生命周期。只有 `PromotionEvent` 原子切换后，ForecastRegistry/策略政策投影才使用新 Champion。

### 16.2 预测晋升

- 主指标：与相同 OOF 事件经验基线配对后的 Brier 改善。
- 护栏：Log Loss 不恶化超过 2%；ECE 不超过 `max(0.15, baseline + 0.03)`；80%区间命中在 65%-95%。
- confirmation 至少 20 条且含至少两个实际方向类别。
- 方向正确率提高不能抵消概率校准显著恶化。

### 16.3 策略晋升

候选扣除费用和滑点后必须：

- OOF 成交样本至少 30，至少 3 个顺序折。
- 平均净收益为正，时间块 Bootstrap 80%下界不低于 0。
- 不出现灾难性回撤或单折主导全部收益。
- confirmation 和 shadow 均不破坏风险硬护栏。
- 通过以下至少一条：

```text
绝对超额通道：多数 OOF 折超额收益为正，confirmation 仍成立。
风险调整通道：保留 >=80% 基准收益，最大回撤降低 >=30%，Sharpe 提高 >=0.2。
```

牛市中不机械要求所有策略跑赢买入持有；但仅降低回撤、没有合理收益保留也不能晋升。

### 16.4 漂移与回滚

- 最近至少 30 个成熟样本与前 60 个参考样本比较。
- 预测层监控 Brier、Log Loss、ECE；策略/联合层监控净期望、回撤和拒单率。
- 漂移只创建 `drifted` 事件，不直接覆盖历史版本。
- 有上一个健康 Champion 时原子回滚；没有时停止新增风险，保护性退出仍有效。
- 回滚不得删除失败版本、证据窗口或失败原因。

## 17. PlanEvidenceSnapshot 输出

V2-9 必须从策略账产生 V2-6 已定义的 `PlanEvidenceSnapshot`：

- 身份绑定 `instrument + strategy_id + strategy_version + parameter_hash + profile`。
- `data_cutoff_at` 只能覆盖已成熟 outcome。
- `sample_count` 与 `oof_sample_count` 分开。
- `expected_net_return/confidence/win_rate/MAE` 来自同一切片。
- conflicting 优先于貌似正常的汇总。
- 30 个股票级 OOF 样本和非负置信下界才可 reliable_positive。
- 行业/市场 fallback 只能产生 insufficient/unavailable 解释，不生成股票级可靠正期望。

同一个当前分析不得消费在 `as_of` 之后评估出的 evidence，防止历史回放偷看未来。

## 18. Repository 与 migration 13/14

migration 13 新增：

```text
maturity_evidence
forecast_outcomes
scenario_outcomes
strategy_outcomes
joint_outcomes
learning_metric_snapshots
learning_replay_runs
learning_folds
learning_candidate_versions
learning_promotion_events
learning_deployments
plan_evidence_snapshots
```

持久化规则：

- 每张事实表同时保存结构化索引列、`payload_json`、payload hash、`generated_at` 和 schema version。
- outcome 业务主键为 `subject_event_key + evidence_origin + policy_version + revision` 的稳定哈希。
- 同一业务主键相同 payload 幂等；不同 payload quarantine 并拒绝聚合。
- 修订通过 `supersedes_id` 形成无环链；同一 subject 只能有一个 active revision。
- 晋升、旧 Champion 退休和投影切换必须单事务完成。
- 每个生产投影键只能有一个 active Champion，`learning_deployments` 必须以该键建立唯一约束。
- repository 重启恢复后对象、Decimal、枚举、UTC 时间和哈希必须完全相等。
- migration 13 在新库、已有 migration 12 库和重复启动时均安全；不得读取或修改 V1 数据库。
- migration 14 将 `maturity_evidence` 唯一身份修正为 `instrument + origin_session_date + target_session_date + revision`，避免多个不同起点预测落在同一目标日时互相冲突。
- `SCHEMA_VERSION` 必须等于实际最高 migration，并由测试直接断言。

## 19. 并发、任务与性能

- `LearningRun` 保存输入截止日、任务类型、候选集合哈希、状态、取消原因和结果哈希。
- 同一 scope/cutoff/kind 同时只允许一个 active run；重复请求返回现有任务身份。
- 取消只停止未提交计算，不得留下半个 ledger 或半次晋升。
- 单次 outcome maturity 目标 <20ms（不含 I/O）。
- 10,000 条 ledger 聚合目标 <1秒。
- 100 个候选的指标比较目标 <2秒，不含完整 OOF 主链回放。
- 完整 OOF 属于后台任务，必须支持 fold 边界取消和断点后幂等重跑。
- 性能测试使用 synthetic 冻结数据，不访问网络和用户数据库。

## 20. 双市场要求

- A股和美股都必须覆盖 1/3/5/10 目标日成熟、三本账、OOF 和晋升测试。
- 目标交易日使用各自交易所日历；不能把美国休市日套到 A股或反之。
- A股历史回放使用 TickFlow 正式日 K 与 A股规则；美股使用 Nasdaq 历史 OHLCV 路由和美股规则。
- 数据 Provider 只属于上游事实层；学习层测试默认使用冻结 Provider/fixture，不重复请求网络。
- 真实 Provider 冒烟沿用 V2-1 已建立的显式开关，不因学习候选数量扩大网络调用。

## 21. 架构禁止项

学习层及其合同禁止出现：

```text
guaranteed_profit
always_profitable
rewrite_source_code
disable_stop_loss
default_account_equity
future_feature_backfill
random_time_split
industry_as_stock_evidence
untriggered_trade_success
expected_sale_cash_as_filled
llm_generated_parameter
```

`learning/` 可 import contracts、forecast diagnostics/trainer、scenario policy、strategies registry、risk evidence、execution simulator、portfolio engine 和 repository 接口。预测、策略、风控、成交、组合层不得反向 import learning；当前主链通过显式传入 `PlanEvidenceSnapshot` 消费学习投影。

## 22. Golden Cases LE00-LE59

```text
LE00 learning 合同、枚举、原因代码、UTC、有限数值和稳定哈希
LE01 issued_online、reconstructed_oof、shadow_online 严格分离
LE02 outcome 只引用冻结上游事件，不能改原预测/计划
LE03 相同 outcome 幂等，冲突 payload quarantine
LE04 revision 只计最新 active 版本且修订链无环
LE05 generated_at 不改变业务身份
LE06 不可用预测只计覆盖率，不按零分计准确率
LE07 股票/行业/市场/交易所 stable_key 隔离
LE08 A股和美股币种、日历、费用、基准隔离
LE09 学习层不 import V1、UI、report、LLM 或网络 Provider

LE10 目标日由 ForecastResult/交易所日历确定，不按自然日
LE11 目标会话未结束保持 pending
LE12 正式目标日 K 缺失为 unverifiable，不寻找未来任意一天替代
LE13 实时/延伸快照不能作为正式目标收盘
LE14 reference/target 复权口径冲突拒绝验证
LE15 上市日前数据拒绝且短历史诚实降级
LE16 corporate action 修订创建 superseding revision
LE17 actual_return 和 actual_direction 可由价格/flat_band 复算
LE18 evidence available_at 晚于 evaluated_at 拒绝
LE19 maturity 批处理单股失败不污染其他股票

LE20 单事件 Brier、Log Loss、方向、区间命中公式正确
LE21 ECE 使用10个最大置信度分箱且空箱不参与
LE22 预测汇总带样本数、cutoff 和时间块置信区间
LE23 候选与基线必须使用完全相同 OOF 事件集合
LE24 Brier 改善但 Log Loss/ECE 恶化的预测候选不能晋升
LE25 方向正确率高不能替代概率校准
LE26 1/3/5/10 horizon 分账，不重复计样本
LE27 regime 切片只使用 origin 时已知状态
LE28 scenario outcome 等全部关键 horizon 成熟后再归因
LE29 mixed 情景不能被二元涨跌标签机械判错

LE30 未触发 TradePlan 不算成功或亏损交易
LE31 触发但 Broker 拒单归 Execution，不记策略亏损
LE32 策略账使用 V2-6 单票批准量，不使用组合最终股数
LE33 策略回放与当前路径共享 TradePlan -> OrderIntent -> V2-7
LE34 费用、滑点、税费后净收益可复算
LE35 日K止盈止损同穿按 adverse path 且证据 low
LE36 单条计划无量化止盈不事后发明；全链OOF只采用后续实际生成的退出计划
LE37 入场、普通退出、保护退出健康度分离
LE38 卖出后1/3/5/10日避免损失和机会成本公式正确
LE39 少于30个股票级 OOF 样本不能 reliable_positive

LE40 联合回放按事件顺序更新现金、持仓和可卖数量
LE41 未成交卖出回款不能供后续买入
LE42 conservative/aggressive 分账且不相加
LE43 recommendation_replay/policy_oof 分离，且组合 TWR、基准、Alpha、回撤和费用可复算
LE44 缺可靠基准时 Alpha 保持缺失，不填0
LE45 strategy/risk/execution/portfolio 配对反事实使用同一事件路径
LE46 非法反事实保持 unavailable，不取消硬约束
LE47 预测正确策略亏损只降策略/联合，不冒充预测失败
LE48 预测错误但条件未触发，保留策略容错证据
LE49 风控减少损失与牺牲收益分别记录

LE50 OOF 使用顺序 fold、maturity purge 和 embargo，无未来泄漏
LE51 reconstructed OOF 不冒充系统当时已发行预测
LE52 单股票负期望不污染其他股票
LE53 行业 fallback 可观察但不能生成股票 A 级证据
LE54 参数越界、未知字段和超过20候选拒绝
LE55 自动优化不改源码、硬风控、账户权益或市场规则
LE56 牛市风险调整通道可晋升稳健候选，高回撤不能只靠总收益晋升
LE57 confirmation/shadow 未通过不替换 Champion
LE58 漂移触发可回滚；无健康版本时停止新增风险但保留保护退出
LE59 migration 13/14 原子/幂等/恢复、双市场和性能边界
```

每个编号必须有唯一具名可执行测试；不能用循环编号、注释或一条 smoke 冒充多个行为。

## 23. 测试命令

```bash
venv/bin/python -m pytest tests/v2/test_learning_contracts.py tests/v2/test_learning_maturity.py tests/v2/test_learning_metrics.py tests/v2/test_learning_ledgers.py tests/v2/test_learning_attribution.py tests/v2/test_learning_replay.py tests/v2/test_learning_evidence.py tests/v2/test_learning_optimizer.py tests/v2/test_learning_lifecycle.py tests/v2/test_learning_repository.py tests/v2/test_learning_architecture.py tests/v2/test_learning_performance.py tests/v2/test_learning_golden_cases.py tests/v2/test_schema_migrations.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

V2-9 测试默认离线，不访问用户数据库。真实 Provider 冒烟不是 V2-9 新职责，但 V2 全量中已有 3 条显式联网冒烟仍必须单独启用并通过。

## 24. 实施顺序

1. LE00-LE09：learning contracts、原因代码、身份、架构边界。
2. LE10-LE19：MaturityResolver、目标日事实、修订链。
3. LE20-LE29：ForecastLedger、概率指标、ScenarioOutcome。
4. LE30-LE39：StrategyLedger、V2-7 同路径回放、退出质量。
5. LE40-LE49：JointLedger、顺序组合回放和六层配对归因。
6. LE50-LE55：purged walk-forward、股票绑定、搜索空间。
7. LE56-LE58：晋升、影子、漂移、回滚和停止新增风险。
8. LE59：migration 13/14、repository、双市场和性能。
9. 跑 V2-9 专项、V2 全量、项目全量和现有真实 Provider 冒烟，更新阶段状态后停止。

## 25. 阶段停止点

V2-9 完成后停止。不得顺手实现：

- LLM 假设解析、事实确认或模板孵化。
- 历史评估图表、报告解释、Tab1/Tab3 页面或后台任务 UI。
- 全新模型算法、策略模板或 DSL 算子。
- 券商连接、真实订单状态或自动执行。

开始 V2-10 前必须另行冻结 LLM 假设类型、结构化 DSL、事实验证、候选转正和研究观察可见性合同。

## 26. 完成复审记录

V2-9 最终复审修复了动态波动率方向标签冻结、成熟度 origin/target/revision 身份、OOF 与在线样本隔离、部分成交、窗口退出成本、联合账聚合、真实 TWR 现金流路径、候选生命周期与部署回滚等问题。固定全链回放还会验证四周期预测、情景、策略、风控、组合分配、成交事实与 outcome 的跨层身份，拒绝用空返回值或无关联对象伪造完整 OOF。LE00-LE59 保持 60 个唯一编号测试，并增加真实 V2-5→V2-8 成交链、冲突、截止日、全链身份闭合和回滚回归。

最终验证：

```text
学习专项：99 passed in 1.42s
V2 全量：541 passed, 3 skipped in 41.68s
项目全量：801 passed, 3 skipped in 74.16s
真实 Provider 冒烟：3 passed in 30.33s
```

默认全量中的 3 个 skip 均为显式联网 Provider 测试，已经使用本地 V1 配置桥接单独启用并通过。V2-9 复审后停止，未进入 V2-10、V2-11 或 V2-12。
