# TradeHelper 2.0 重构实施计划

> 分支：`V2.0`。本文档用于指导 2.0 版本从数据层到 UI/报告层的重构实施。目标不是继续给 1.0 打补丁，而是把已有经验重新组织成一套更清晰、可测试、可自优化的交易决策系统。

## 0. 2.0 核心原则

TradeHelper 的最终目标从 1.x 延续到 2.0，不因重构而改变。系统必须稳定回答五个问题：

1. 现在是否可以买、卖、减仓、加仓、持有？
2. 如果现在不能操作，达到什么条件可以操作？
3. 如果判断错了，最大亏损是多少，在哪里失效？
4. 这个建议过去有没有正期望，可信度有多高？
5. 系统预测的是哪个目标日期、概率和收益区间，过去预测到底准不准？

TradeHelper 2.0 的主流程固定为：

```text
数据事实
  -> 特征快照
    -> 预测模型判断未来情景
      -> 情景规划器生成交易环境
        -> 策略引擎按情景生成交易计划
          -> 风控官决定执行等级
            -> 报告/UI 展示
              -> 预测账/策略账/联合账复盘
                -> 股票级自优化
```

关键变化：

1. 预测和交易策略分账，但通过“情景规划器”强绑定。
2. 预测模型不再只使用价格动量，而要纳入技术、成交量、Alpha、新闻、基本面、市场/行业状态和 LLM 结构化观察。
3. 策略不再孤立生成买卖信号，而是根据预测情景生成计划。
4. 自优化必须绑定具体股票，同时允许市场级、行业级模型作为样本不足时的保守 fallback。
5. 每一步都必须有独立测试，不能靠看完整报告和日志判断功能是否正确。
6. LLM 不能直接生成交易指令，但可以提出预测假设和策略假设，进入候选池后由代码和历史 OOF 验证。

### 不可丢失的 V1 硬约束

2.0 每个阶段都必须显式检查以下约束：

1. A股和美股同等支持，不能只完成美股链路。
2. Tab1 是单股完整研究，Tab3 是组合级交易工作台，不是 Tab1 批量版。
3. Tab3 必须使用真实账户余额、持仓数量、成本价、现金和关注列表；不得虚构账户权益。
4. 数据源必须按市场路由：A股历史/盘中 TickFlow，A股基本面 baostock -> akshare -> LLM；美股历史/盘中 TickFlow，延伸时段 Nasdaq.com -> yfinance，基本面 Finnhub -> fallback。
5. 上市日期必须限制缓存读取、网络请求、回测窗口和预测训练窗口。
6. 盘中实时价和延伸时段快照不得写入正式日 K。
7. 每个阶段的测试必须至少有 A 股和美股各一个代表场景，除非该阶段与市场无关，并需在阶段状态中说明原因。

### 文档职责

| 文件 | 职责 |
|------|------|
| [README.md](./README.md) | 项目入口，给人快速了解当前状态 |
| [DESIGN.md](./DESIGN.md) | 2.0 架构设计，定义系统目标、分层职责和边界 |
| [V2_REFACTOR_PLAN.md](./V2_REFACTOR_PLAN.md) | 2.0 实施计划，定义开发顺序、测试方案、验收标准和阶段状态 |
| [docs/V1_CAPABILITY_INVENTORY.md](./docs/V1_CAPABILITY_INVENTORY.md) | V1 能力资产清单，作为每个 V2 阶段的迁移检查表 |
| `AGENTS.md` | Codex 本地工作约定 |
| `CLAUDE.md` | Claude Code 本地工作约定 |

## 1. V2 分层结构

为避免 V1/V2 逻辑混在一起，2.0 新代码优先放入独立 `tradehelper_v2/` 包。V1 代码只作为参考实现、算法来源、测试样本和回归对照；V2 主链路不直接 import V1 的耦合业务模块。复用的是 V1 中验证过的处理逻辑和算法思想，而不是把 V1 代码换个目录继续运行。

若极少数外部 I/O client 在早期阶段确实需要临时借用，必须满足三条：

1. 只能通过显式 compatibility shim 调用，不能散落在 V2 业务逻辑里。
2. 必须在阶段计划中写明替换目标和退出条件。
3. 预测、情景、策略、风控、学习主链路不得依赖 shim。

```text
tradehelper_v2/
  __init__.py

  contracts.py            # 全局合同：Bar/Quote/Feature/Forecast/Scenario/TradePlan/RiskDecision

  data/
    providers/            # TickFlow/Finnhub/baostock/Nasdaq/yfinance 等原始适配器
    compatibility.py      # 临时 I/O shim；不得承载 V2 业务逻辑
    quality.py            # 数据完整度、时效、上市日期、OHLC 约束
    repository.py         # SQLite 读写边界

  features/
    technical.py          # 价格、成交量、趋势、波动、形态
    alpha.py              # Final_Score 与因子有效性快照
    news.py               # 新闻情绪特征
    fundamentals.py       # 基本面特征
    market_context.py     # 大盘、行业、风险环境
    observations.py       # LLM/系统观察结构化特征
    store.py              # point-in-time FeatureSnapshot

  forecast/
    models.py             # analog/logistic/tree/ensemble/后续候选
    registry.py           # 股票级/行业级/市场级 Champion/Challenger
    trainer.py            # OOF 训练、选择/确认窗口
    engine.py             # 生成 ForecastResult
    diagnostics.py        # Brier/LogLoss/ECE/区间命中/分层表现

  scenario/
    planner.py            # ForecastResult -> TradingScenario

  strategies/
    templates.py          # 支撑反弹、突破、回踩、锁利、止损等模板
    engine.py             # TradingScenario -> TradePlan candidates

  risk/
    officer.py            # A/B/C/D 执行等级
    sizing.py             # 仓位、亏损金额、集中度、账户约束
    market_rules.py       # A股/美股市场规则

  learning/
    forecast_ledger.py    # 预测账
    strategy_ledger.py    # 策略账
    joint_ledger.py       # 联合账
    optimizer.py          # 股票级模型/策略升降级
    hypothesis_lab.py     # LLM 假设孵化

  reports/
    sections.py           # 报告分段
    renderer.py           # HTML/PDF

  use_cases/
    single_stock.py       # Tab1 新入口
    portfolio.py          # Tab3 新入口

  ui_models.py            # UI 显示模型，避免 UI 直接读计算细节
```

迁移期旧目录定位：

```text
data/                     # V1 数据源参考实现；V2 数据层重新定义合同后吸收经验
alpha/ indicators/         # V1 指标参考实现；V2 features 重新实现 point-in-time 快照
core/ services/            # V1 主流程参考实现；V2 use_cases 不直接依赖
strategies/ backtest/      # V1 策略与回测参考实现；V2 TradePlan/learning 重新实现
report/ ui/                # V1 展示参考实现；V2 reports/UI 重新组织
```

不再采用以下容易混淆的新旧混放结构：

```text
data/
  contracts.py            # CanonicalBar/QuoteSnapshot/NewsSnapshot/FundamentalSnapshot
features/
forecast/
scenario/
strategies/
risk/
learning/
reports/
```

## 2. 数据层重构

### 2.1 目标

建立统一、可测试的数据事实层。任何上层模块都不直接理解 TickFlow/Finnhub/baostock/Nasdaq/yfinance 的原始返回格式，只消费标准结构。

核心对象：

```text
CanonicalBar:
  code, market, date, open, high, low, close, volume, source, adjusted, fetched_at

QuoteSnapshot:
  code, market, session, price, open, high, low, volume, timestamp, source, freshness_status

NewsSnapshot:
  code, market, published_at, fetched_at, title, source, finbert_score, relevance

FundamentalSnapshot:
  code, market, effective_date, fetched_at, source, fields, quality_status

AccountSnapshot:
  cash, holdings, cost_basis, account_equity, captured_at
```

### 2.2 必须解决的问题

1. 日 K、实时价、延伸时段价、基本面、新闻都要有明确来源和时间戳。
2. 盘中实时快照只能进入内存决策快照，不得写入正式日 K。
3. 上市日期必须同时约束缓存读取、网络请求、回测窗口和预测训练窗口。
4. 数据缺失不能静默补假值；必须产生结构化 `DataQualityReport`。
5. Tab1/Tab3 可以共用缓存，但都必须独立触发行情、新闻和基本面刷新。
6. A 股和美股必须走各自规定数据源，不能为了实现方便只接一边。

### 2.3 测试方案

新增或重写测试：

```text
tests/v2/test_data_contracts.py
tests/v2/test_data_quality.py
tests/v2/test_market_data_repository.py
tests/v2/test_provider_fallbacks.py
```

测试内容：

1. OHLC 不成立时必须 blocked，正常大涨大跌不能被误判成 OHLC 异常。
2. 新股上市前数据必须被裁剪，不能进入训练/回测。
3. 美股盘中使用 TickFlow；盘前/盘后使用 Nasdaq -> yfinance fallback。
4. A股和美股代码识别、公司名返显、上市日期获取路径一致可测。
5. TickFlow 限频或失败时，日 K 可从 fallback 获取，但必须保留 source 和降级原因。
6. 新闻 empty 状态必须有 TTL，过期后可重新刷新。
7. 真实账户权益为 0 时，数据层提供 0，不允许上层回退到虚构本金。
8. Tab1 和 Tab3 都能独立触发行情、新闻和基本面刷新，不依赖另一页先运行。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_data_contracts.py tests/v2/test_data_quality.py -q
```

数据层未通过前，不进入预测层重构。

## 3. 特征层重构

### 3.1 目标

建立 point-in-time 特征快照，明确“当前时点可见什么”。预测、策略和 LLM 假设验证都使用同一份 `FeatureSnapshot`，避免各算各的。

特征分组：

```text
TechnicalFeatures:
  returns, momentum, ma_distance, volatility, volume_ratio, gap, support/resistance

AlphaFeatures:
  final_score, tech_score, news_score, fundamental_score, factor_validation_coverage

NewsFeatures:
  finbert_score, news_count, recency_hours, source_count, sentiment_change

FundamentalFeatures:
  valuation, growth, profitability, quality, leverage, source_quality

MarketContextFeatures:
  index_trend, sector_relative_strength, risk_regime, volatility_regime

ObservationFeatures:
  ma120_support, profit_lock_after_high, failed_breakout, llm_hypothesis_tags
```

### 3.2 必须解决的问题

1. 预测模型不能再只使用 `momentum_5/momentum_20/trend_20/volatility_20`。
2. 新闻和基本面只有在有真实历史抓取快照时才能进入 OOF；不能用今天的基本面反填历史。
3. 预测和策略必须读取同一份特征快照，保证报告解释与实际计算一致。

### 3.3 测试方案

新增测试：

```text
tests/v2/test_feature_snapshot.py
tests/v2/test_feature_no_lookahead.py
tests/v2/test_feature_degradation.py
```

测试内容：

1. 给定同一组 bars/news/fundamentals，特征快照稳定可复现。
2. 历史 OOF 只能读取当时已存在的新闻/基本面快照。
3. 新闻覆盖不足时，预测训练自动排除新闻特征，并在诊断里说明。
4. 基本面缺失时，不得生成默认好/坏结论，只能标记缺失。
5. `FeatureSnapshot.feature_hash` 变化必须能解释为输入事实变化。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_feature_snapshot.py tests/v2/test_feature_no_lookahead.py -q
```

## 4. 预测层重构

### 4.1 目标

预测层独立回答：

```text
未来 1/3/5/10 日上涨、震荡、下跌概率是多少？
收益 P10/P50/P90 是多少？
模型是 Champion、Challenger、样本不足，还是未跑赢基线？
这次预测主要依据哪些特征？
```

模型分三级：

```text
股票级模型：单只股票自己的规律
行业级模型：同板块样本补充
市场级模型：大盘风险环境 fallback
```

使用优先级：

```text
股票级 Champion
  -> 行业级 Champion 辅助
    -> 市场级 Champion 辅助
      -> 未验证观察模型，只展示不参与执行分级
```

### 4.2 候选模型池

第一批候选必须覆盖：

```text
价格相似状态 analog
正则化多分类 logistic
浅层概率树
集成模型
市场/行业条件模型
特征子集模型：技术-only、技术+新闻、技术+基本面、全特征
regime-specific 模型：趋势/震荡/高波动分层
```

### 4.3 自优化规则

每只股票、每个周期独立维护 Champion/Challenger。

晋升必须满足：

1. selection 和 confirmation 两段 OOF 都通过。
2. Brier/LogLoss/ECE/80%区间命中不劣于基线。
3. 改进的 Bootstrap 置信下界为正。
4. 不能靠单次报告或单次好运晋升。

回滚条件：

1. 在线 Brier 连续劣化。
2. ECE 明显恶化。
3. 某市场状态下持续失效。

### 4.4 测试方案

新增测试：

```text
tests/v2/test_forecast_feature_sets.py
tests/v2/test_forecast_oof_no_leakage.py
tests/v2/test_forecast_model_registry.py
tests/v2/test_forecast_fallback_hierarchy.py
tests/v2/test_forecast_diagnostics.py
```

测试内容：

1. 预测模型输入必须包含明确 feature_set，不允许隐式只用 close。
2. OOF 任一测试点不能读取未来 K 线、未来新闻、未来基本面。
3. 股票级 Champion 不存在时，系统能降级使用行业/市场观察模型，并清楚标注“不参与强执行”。
4. 样本足够但未跑赢基线时，报告必须写“未跑赢基线”，不能写成“样本不足”。
5. 同一股票不同周期的 Champion 互不污染。
6. synthetic 数据中，如果某个特征确实预测未来方向，模型能被 OOF 晋升。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_forecast_*.py -q
```

## 5. 情景规划层

### 5.1 目标

新增 `TradingScenario`，作为预测和策略之间的桥。

核心输出：

```text
TradingScenario:
  bias: bullish / bearish / range / uncertain
  horizon_alignment: aligned / mixed / conflict / unavailable
  allowed_strategy_families
  blocked_strategy_families
  entry_policy
  exit_policy
  confidence_note
  forecast_evidence
```

### 5.2 情景规则

示例：

```text
1/3/5日均偏多且无冲突：
  允许趋势延续、突破、回踩；禁止逆势做空式退出。

短期震荡、中期偏多：
  禁止追高；优先回踩确认和支撑反弹。

短中期偏空：
  禁止新开仓；已有持仓优先止损、减仓、锁利。

预测冲突：
  只输出条件计划，不给 A 级新开仓。

预测不可验证：
  策略可给观察计划，但预测不参与执行升级。
```

### 5.3 测试方案

新增测试：

```text
tests/v2/test_scenario_planner.py
```

测试内容：

1. 多周期预测一致偏多时，输出 bullish scenario。
2. 1日偏空、5日偏多时，输出 mixed，不允许追高。
3. 没有 Champion 预测时，输出 uncertain，不允许用预测升级策略。
4. bearish scenario 下，风险退出策略不被阻止。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_scenario_planner.py -q
```

## 6. 策略层重构

### 6.1 目标

策略输入从“只有历史 df/context”升级为：

```text
StrategyInput:
  feature_snapshot
  trading_scenario
  account_snapshot
  position_snapshot
  market_rules
```

策略输出统一为：

```text
TradePlan:
  action: buy / add / sell / reduce / hold / watch / invalid
  scenario_id
  strategy_family
  trigger_condition
  trigger_price
  stop_loss
  take_profit
  position_pct
  max_loss_amount
  invalidation
  evidence
  missing_conditions
```

### 6.2 策略家族

保留 1.0 中有效策略，但按情景重新归类：

```text
TrendContinuation      趋势延续
PullbackEntry          趋势回踩
MA120SupportRebound    半年线支撑
RangeSupportBounce     区间支撑反弹
BreakoutEntry          突破买入
ProfitLock             冲高回落锁利
FailedPullbackExit     反抽失败退出
StopLossExit           止损退出
PositionRiskControl    持仓风控
ConditionalWatch       条件观察
```

### 6.3 测试方案

新增测试：

```text
tests/v2/test_strategy_engine_by_scenario.py
tests/v2/test_trade_plan_contract.py
tests/v2/test_strategy_stock_specific_learning.py
```

测试内容：

1. bullish scenario 下允许回踩/突破计划。
2. bearish scenario 下不允许新开仓计划升级为 A。
3. range scenario 下优先支撑/压力计划。
4. 同一个策略在不同股票上的参数和健康度互相隔离。
5. 没有止损的买入计划不能进入 A/B。
6. 没有预测 Champion 时，策略仍必须给出“条件观察计划”，不能空白。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_strategy_*.py -q
```

## 7. 风控层重构

### 7.1 目标

风控官只决定能不能执行，不负责发明交易点子。

输入：

```text
TradePlan
DataQualityReport
ForecastEvidence
StrategyEvidence
AccountSnapshot
MarketRules
```

输出：

```text
ExecutionDecision:
  level: A / B / C / D
  executable: bool
  adjusted_position_pct
  max_loss_amount
  rejection_or_demotion_reasons
```

### 7.2 测试方案

新增测试：

```text
tests/v2/test_risk_officer.py
tests/v2/test_position_sizing.py
tests/v2/test_market_rules_v2.py
```

测试内容：

1. 数据冲突必须 D。
2. 无止损买入不能 A/B。
3. 用户权益为 0 时禁止新开仓，但不阻止卖出/减仓。
4. 单票集中度过高时禁止加仓。
5. A股一手、T+1、涨跌停约束生效。
6. 风险退出不因预测偏多而被阻止。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_risk_*.py tests/v2/test_position_sizing.py -q
```

## 8. 学习与自优化层

### 8.1 三本账

2.0 必须分开记录：

```text
ForecastLedger:
  预测了哪个目标日、方向概率、收益区间，实际结果如何。

StrategyLedger:
  在当时预测情景下，某策略给了什么计划，触发没有，盈亏如何。

JointLedger:
  预测 + 策略 + 风控官最终组合建议是否赚钱。
```

### 8.2 归因规则

```text
预测错 + 交易亏：
  优先降级预测模型，不直接否定策略。

预测对 + 交易亏：
  降级策略模板/参数或仓位规则。

预测对 + 策略对 + 风控后仍亏：
  检查滑点、止损、仓位、市场规则。

LLM 假设命中：
  进入股票级或行业级候选模板。
```

### 8.3 测试方案

新增测试：

```text
tests/v2/test_learning_ledgers.py
tests/v2/test_attribution_rules.py
tests/v2/test_stock_specific_optimizer.py
```

测试内容：

1. 同一份建议不能重复计样本。
2. 到期后预测只补实际结果，不改原预测。
3. 策略未触发不能算作成功交易。
4. 预测正确但策略亏钱时，只降级策略，不回滚预测 Champion。
5. 单股票负期望策略不能污染其他股票。
6. 行业级有效策略可以作为个股样本不足时的观察 fallback，但不能直接升为个股 A 级。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_learning_*.py tests/v2/test_attribution_rules.py -q
```

## 9. LLM 假设孵化层

### 9.1 目标

LLM 的价值从“写报告”升级为“提出研究假设”，但所有假设必须结构化和验证。

LLM 建议拆解为：

```text
ForecastHypothesis:
  pattern, expected_direction, horizon, evidence_text

StrategyHypothesis:
  trigger_condition, action, stop_loss_rule, take_profit_rule, invalidation
```

### 9.2 转正流程

```text
LLM 原始观察
  -> 结构化假设
    -> 事实验证
      -> 历史 OOF 回放
        -> 股票级/行业级表现统计
          -> 候选预测特征或候选策略模板
            -> Champion/正式策略晋升
```

### 9.3 测试方案

新增测试：

```text
tests/v2/test_llm_hypothesis_parser.py
tests/v2/test_hypothesis_validation.py
tests/v2/test_hypothesis_promotion.py
```

测试内容：

1. LLM 混合建议能拆成预测假设和策略假设。
2. 没有止损/触发价的 LLM 买入建议不能进入可执行层。
3. LLM 提出的形态必须有事实证据才能记录为 triggered。
4. 系统规则成功不能冒充 LLM 命中率。
5. 多次有效 LLM 假设可以沉淀成候选模板，但必须经过 OOF。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_llm_hypothesis_*.py tests/v2/test_hypothesis_*.py -q
```

## 10. UI 与报告重构

### 10.1 UI 原则

UI 按交易决策流程组织，不按技术模块堆叠。

Tab1 单股：

```text
1. 当前结论：能不能操作
2. 预测情景：未来 1/3/5/10 日概率
3. 交易计划：不同情景下如何做
4. 风险：亏多少、哪里错、仓位多少
5. 证据：预测账、策略账、联合账
6. 研究员观察：LLM 假设与系统确认
7. 附录：详细指标、回测、审计
```

Tab3 组合：

```text
1. 今日优先处理事项
2. 已持仓：减仓/锁利/止损/持有
3. 关注股：可替换/可等待机会
4. 账户风险：仓位、集中度、现金、最大计划亏损
5. 组合预测与情景
6. 历史能力评估
```

Tab3 必须保留组合级语义：

- 使用用户真实余额、现金、持仓数量和成本价。
- 计算单票集中度、总仓位、浮盈浮亏和剩余风险容量。
- 优先输出持仓风险处理顺序，再输出关注股替换机会。
- 持仓编辑必须支持修改已有持仓数量和成本价，不能要求删除后重录。
- A股组合和美股组合都必须可生成报告和历史评估。

### 10.2 报告原则

主报告必须让用户不看日志也能理解：

```text
系统预测了什么？
基于预测，策略准备怎么做？
风控官为什么允许/降级/驳回？
如果错了亏多少？
历史上预测和策略分别准不准？
LLM 有没有提出不同看法？
```

详细技术指标放附录，不能挤在操作结论前。

### 10.3 测试方案

新增测试：

```text
tests/v2/test_report_sections.py
tests/v2/test_report_readability_snapshots.py
tests/v2/test_ui_state_flow.py
```

测试内容：

1. 没有 Champion 时，报告显示“已评估但未跑赢基线/样本不足”，不能只写 OOF 未通过。
2. 报告必须同时出现预测结论、情景、交易计划、风控等级和历史证据。
3. 用户持仓信息必须影响 Tab3 计划，不允许虚构本金。
4. 历史评估页必须分开展示预测账、策略账、联合账。
5. HTML/PDF 关键表格字段不能缺股票、目标日期、实际结果。
6. Tab1 单股报告和 Tab3 组合报告都必须覆盖 A 股和美股示例。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_report_*.py tests/v2/test_ui_state_flow.py -q
```

## 11. 端到端测试方案

每一层通过后，再跑端到端。端到端不替代单元测试。

新增测试：

```text
tests/v2/test_e2e_single_stock.py
tests/v2/test_e2e_portfolio.py
tests/v2/test_e2e_a_share.py
tests/v2/test_e2e_us_extended_hours.py
```

场景：

1. 美股盘后单股：完整日 K + 新闻 + 基本面 -> 预测 -> 情景 -> 策略 -> 风控 -> 报告。
2. 美股盘前组合：Nasdaq 延伸时段报价 -> 持仓风控 -> 条件计划。
3. A股组合：TickFlow 日 K/盘中 -> A股交易规则 -> 报告。
4. 新股：上市日期裁剪 -> 样本不足降级 -> 不伪造历史。
5. 预测正确但策略亏损的 synthetic 场景：归因必须落到策略账。
6. 预测错误但策略避险盈利的 synthetic 场景：归因必须保留策略容错证据。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_e2e_*.py -q
venv/bin/python -m pytest tests/ -q
```

## 12. 实施顺序

2.0 严格从下往上做：

| 阶段 | 内容 | 主要交付物 | 必跑测试 |
|------|------|------|------|
| V2-0 | 测试基础设施 | `tests/v2/`、fixture、synthetic 数据生成器 | smoke |
| V2-1 | 数据层 | 数据合同、质量闸门、provider fallback、repository | `test_data_*` |
| V2-2 | 特征层 | FeatureSnapshot、无未来数据特征、缺失降级 | `test_feature_*` |
| V2-3 | 预测层 | 多特征预测、股票/行业/市场模型注册、OOF | `test_forecast_*` |
| V2-4 | 情景层 | TradingScenario | `test_scenario_*` |
| V2-5 | 策略层 | Scenario-driven TradePlan | `test_strategy_*` |
| V2-6 | 风控层 | ExecutionDecision、仓位和市场规则 | `test_risk_*` |
| V2-7 | 学习层 | 三本账、归因、自优化 | `test_learning_*` |
| V2-8 | LLM 假设层 | 假设解析、验证、候选晋升 | `test_llm_*` |
| V2-9 | 报告/UI | 新报告结构、历史评估、Tab1/Tab3 | `test_report_*`, `test_ui_*` |
| V2-10 | 端到端 | 单股/组合/A股/美股延伸时段 | `test_e2e_*`, full tests |

每个阶段完成必须更新本文档状态，并在提交说明中写明：

```text
完成了哪一层
新增/修改了哪些合同
跑了哪些测试
吸收了哪些 V1 能力资产
仍有哪些 V1 能力待迁移
未解决的风险是什么
下一层是否可以开始
```

## 13. 阶段状态

### 2026-07-07 前置整理

- 已将 1.x README、DESIGN、UPGRADE_PLAN、回测说明、优化审计和 Claude 旧说明归档到 `docs/archive/v1/`。
- 根目录只保留当前 V2 入口文档：`README.md`、`DESIGN.md`、`V2_REFACTOR_PLAN.md`，以及本地代理说明 `AGENTS.md`/`CLAUDE.md`。
- 已删除未被引用的 1.x 架构预留占位模块 `data_adapters/`。
- 已清理本地 `.DS_Store`、`__pycache__` 和 `.pytest_cache`，保留 `venv/build/dist/dist_data` 等不可从 git 恢复的本地产物。
- 完整测试通过：`260 passed`。

| 阶段 | 状态 | 说明 |
|------|------|------|
| V2-0 测试基础设施 | 未开始 | 先建立测试目录、fixture 和 synthetic 数据 |
| V2-1 数据层 | 未开始 | 第一批实施目标 |
| V2-2 特征层 | 未开始 | 等数据合同稳定 |
| V2-3 预测层 | 未开始 | 等 FeatureSnapshot 稳定 |
| V2-4 情景层 | 未开始 | 等 ForecastResult V2 稳定 |
| V2-5 策略层 | 未开始 | 等 TradingScenario 稳定 |
| V2-6 风控层 | 未开始 | 可并行梳理合同，但实现等 TradePlan 稳定 |
| V2-7 学习层 | 未开始 | 等三类事件合同稳定 |
| V2-8 LLM 假设层 | 未开始 | 可复用 V1 observation，但需拆预测/策略假设 |
| V2-9 报告/UI | 未开始 | 最后做展示，不再用报告反推计算正确性 |
| V2-10 端到端 | 未开始 | 每层单测通过后执行 |

## 14. 当前第一步

第一步只做 V2-0 和 V2-1：

1. 建立 `tests/v2/` 和 synthetic fixture。
2. 定义数据合同，不接 UI。
3. 以 V1 数据源经验为参考，重建 V2 标准数据对象；如短期临时借用外部 client，必须封装在 `tradehelper_v2.data.compatibility` 并写清退出条件。
4. 建立数据质量测试。
5. 跑通后再进入特征层。

在 V2-1 完成前，不重构预测、策略、报告或 UI。
