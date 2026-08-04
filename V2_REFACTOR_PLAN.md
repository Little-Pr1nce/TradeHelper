# TradeHelper 2.0 重构实施计划

> 分支：`V2.0`。本文档用于指导 2.0 版本从数据层到 UI/报告层的重构实施。目标不是继续给 1.0 打补丁，而是把已有经验重新组织成一套更清晰、可测试、可自优化的交易决策系统。

## 0. 2.0 核心原则

TradeHelper 的最终目标从 1.x 延续到 2.0，不因重构而改变。系统必须稳定回答五个问题：

1. 现在是否可以买、卖、减仓、加仓、持有？
2. 如果现在不能操作，达到什么条件可以操作？
3. 如果判断错了，最大亏损是多少，在哪里失效？
4. 这个建议过去有没有正期望，可信度有多高？
5. 系统预测的是哪个目标日期、概率和收益区间，过去预测到底准不准？

TradeHelper 2.0 的当前决策链固定为：

```text
数据事实 -> 特征快照 -> 预测模型 -> 情景规划器
  -> 策略引擎 -> 风控官 -> 单股/组合决策 -> 报告/UI
```

共享验证链固定为：

```text
TradePlan + ExecutionDecision
  -> 当前订单预览 / 历史成交仿真 / 到期事实验证
    -> 预测账 / 策略账 / 联合账
      -> 股票级自优化
```

关键变化：

1. 预测和交易策略分账，但通过“情景规划器”强绑定。
2. 预测模型不再只使用价格动量，而要纳入技术、成交量、Alpha、新闻、基本面、市场/行业状态和 LLM 结构化观察。
3. 策略不再孤立生成买卖信号，而是根据预测情景生成计划。
4. 自优化必须绑定具体股票，同时允许市场级、行业级模型作为样本不足时的保守 fallback。
5. 每一步都必须有独立测试，不能靠看完整报告和日志判断功能是否正确。
6. LLM 不能直接生成交易指令，但可以提出预测假设和策略假设，进入候选池后由代码和历史 OOF 验证。
7. 当前建议和历史回放必须消费同一份 `TradePlan`，不能维护两套信号或订单逻辑。
8. 自动优化只能晋升经过样本外验证的注册候选，不能自行改写生产源码，也不能取消硬风控。

### 不可丢失的 V1 硬约束

2.0 每个阶段都必须显式检查以下约束：

1. A股和美股同等支持，不能只完成美股链路。
2. Tab1 是单股完整研究，Tab3 是组合级交易工作台，不是 Tab1 批量版。
3. Tab3 必须使用真实账户余额、持仓数量、成本价、现金和关注列表；不得虚构账户权益。
4. 数据源必须按市场路由：A股历史/盘中 TickFlow，A股基本面 baostock -> akshare；美股已完成日K Nasdaq 历史 OHLCV -> yfinance -> TickFlow，常规盘中 TickFlow，延伸时段 Nasdaq.com -> yfinance，基本面 Finnhub -> yfinance -> akshare -> 百度可验证页面。LLM 只能解释有来源的事实，不能补造财务数字。
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
| [docs/v2/CONTRACTS.md](./docs/v2/CONTRACTS.md) | V2-0/V2-1 精确类型、不变量、序列化和 repository 合同 |
| [docs/v2/POLICIES.md](./docs/v2/POLICIES.md) | V2-0/V2-1 数据源、时段、缓存、质量和数据库确定政策 |
| [docs/v2/GOLDEN_CASES.md](./docs/v2/GOLDEN_CASES.md) | V2-0/V2-1 固定输入与预期结果，测试不得迁就实现 |
| [docs/v2/V2_2_FEATURES.md](./docs/v2/V2_2_FEATURES.md) | V2-2 特征合同、公式、缺失语义、存储和 Golden Cases |
| [docs/v2/V2_3_FORECAST.md](./docs/v2/V2_3_FORECAST.md) | V2-3 预测合同、标签、模型、OOF、注册、持久化和 Golden Cases |
| [docs/v2/V2_4_SCENARIOS.md](./docs/v2/V2_4_SCENARIOS.md) | V2-4 情景合同、多周期归并、三时段、策略家族政策、持久化和 Golden Cases |
| [docs/v2/V2_5_STRATEGIES.md](./docs/v2/V2_5_STRATEGIES.md) | V2-5 TradePlan、条件 DSL、策略模板、V1 迁移矩阵、持久化和 SP00-SP29 |
| [docs/v2/V2_6_RISK.md](./docs/v2/V2_6_RISK.md) | V2-6 ExecutionDecision、真实账户估值、A/B/C/D、sizing、市场规则、migration 10 和 RK00-RK42 |
| [docs/v2/V2_7_EXECUTION.md](./docs/v2/V2_7_EXECUTION.md) | V2-7 OrderIntent、触发状态机、当前预览、历史成交、费用/滑点、migration 11 和 EX00-EX49 |
| [docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md) | V2-8 组合批次、排序、现金/heat/相关性分配、最终股数、migration 12 和 PO00-PO49 |
| [docs/v2/V2_9_LEARNING.md](./docs/v2/V2_9_LEARNING.md) | V2-9 到期验证、三本账、六层归因、OOF、自优化生命周期、migration 13/14 和 LE00-LE59 |
| [docs/v2/V2_10_LLM_HYPOTHESES.md](./docs/v2/V2_10_LLM_HYPOTHESES.md) | V2-10 研究事实清单、严格 JSON、确定性验证、候选桥接、migration 15 和 LL00-LL49 |
| [docs/v2/V2_11_REPORT_UI.md](./docs/v2/V2_11_REPORT_UI.md) | V2-11 展示输入、ReportDocument、历史评估、Tab1/Tab3、任务进度、导出、migration 16 和 UX00-UX59 |
| [docs/v2/V2_12_MIGRATION_RELEASE.md](./docs/v2/V2_12_MIGRATION_RELEASE.md) | V2-12 production composition、V1 迁移、端到端矩阵、V1 退出、migration 17、发布和 RL00-RL79 |
| `AGENTS.md` | Codex 本地工作约定 |
| `CLAUDE.md` | Claude Code 本地工作约定 |

V2-0/V2-1 冲突优先级：三份基础规范 > 本计划中的概念示例 > V1 能力清单 > V1 参考代码。V2-2 至 V2-12 分别以对应阶段规范为准。V2-11 已完成并复审；V2-12 精确设计已冻结，是 TradeHelper 2.0 最后一个实施阶段。

## 1. V2 分层结构

V2 开发初期曾使用独立总包与 V1 隔离；V1 退出工作树后，正式代码改为按职责放在项目根目录一级包中。V1 代码只作为历史算法来源和回归对照，生产主链不直接 import V1 耦合模块。

若极少数外部 I/O client 在早期阶段确实需要临时借用，必须满足三条：

1. 只能通过显式 compatibility shim 调用，不能散落在 V2 业务逻辑里。
2. 必须在阶段计划中写明替换目标和退出条件。
3. 预测、情景、策略、风控、学习主链路不得依赖 shim。

```text
main.py                   # production composition 与桌面入口

  contracts/
    market_data.py        # Bar/Quote/News/Fundamental/Account
    analysis.py           # Feature/Forecast/Scenario
    decisions.py          # TradePlan/StrategyBundle/ExecutionDecision/PortfolioDecision
    learning.py           # 三本账和版本事件合同

  data/
    providers/            # TickFlow/Finnhub/baostock/Nasdaq/yfinance 等原始适配器
    migrations/
      schema.py           # V1 -> V2 幂等迁移、版本记录和错误历史隔离
    compatibility.py      # 临时 I/O shim；不得承载 V2 业务逻辑
    quality.py            # 数据完整度、时效、上市日期、OHLC 约束
    repository.py         # SQLite 读写边界

  config/
    settings.py           # 工作目录、provider/LLM 配置和首次运行校验

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
    policy.py             # 冻结的多周期归并和策略家族兼容规则
    facts.py              # 预测发行后的新闻/基本面点时事实差集

  strategies/
    templates/
      entries.py          # 趋势、突破、回踩和支撑入场
      exits.py            # 锁利、止损和反抽失败退出
      conditional.py      # 持有、观察和条件触发模板
    engine.py             # TradingScenario -> TradePlan candidates

  risk/
    officer.py            # A/B/C/D 执行等级
    sizing.py             # 仓位、亏损金额、集中度、账户约束
    market_rules.py       # A股/美股市场规则

  execution/
    orders.py             # TradePlan -> OrderIntent，当前建议与历史回放共用
    trigger_engine.py     # 触发、失效、止损、止盈和计划到期
    costs.py              # 费用、动态滑点和可审计流动性替代模型
    simulator.py          # 日K/分钟K成交仿真及证据质量

  portfolio/
    allocator.py          # 真实账户风险预算、跨股票资金分配
    ranking.py            # 持仓处理顺序、关注股替换和冲突消解

  learning/
    forecast_ledger.py    # 预测账
    strategy_ledger.py    # 策略账
    joint_ledger.py       # 联合账
    optimizer.py          # 股票级模型/策略升降级

  research/
    parser.py             # LLM 原文 -> Forecast/Model/StrategyHypothesis
    hypothesis_lab.py     # 事实验证、OOF 状态和候选版本
    registry.py           # 受控 DSL、已注册算子和模板

  reports/
    sections/
      overview.py         # 当前结论和一分钟操作台
      forecast.py         # 天气预报式预测与历史对错
      plans.py            # 条件计划、风险和组合排序
      evidence.py         # 三本账、审计和研究员观察
    renderer.py           # HTML/PDF

  use_cases/
    single_stock.py       # Tab1 新入口
    portfolio.py          # Tab3 新入口

  presentation/
    view_models.py        # UI 显示模型，避免 UI 直接读计算细节

  ui/
    pages/
      single_stock.py     # Tab1
      report_history.py   # 历史报告检索、筛选、查看和评分
      portfolio.py        # Tab3
      evaluation.py       # 预测账/策略账/联合账历史评估
      settings.py         # 工作目录、数据源、LLM 和代理配置
    components/           # 表格、图表、持仓编辑和通用状态组件
    task_state.py         # 分阶段进度、取消和后台优化状态
```

上图是 V2 最终目标结构，不代表 V2-0/V2-1 可以提前创建全部目录。当前阶段只创建 `contracts/`、`config/`、`data/` 和对应测试；禁止用空占位类提前宣称后续层已完成。

### 1.1 V2-0 测试基础设施

在实现数据层前先建立：

```text
tests/v2/conftest.py
tests/v2/fixtures/        # A股/美股、三时段、新股、异常OHLC、缺新闻/基本面、真实账户
tests/v2/test_architecture_boundaries.py
tests/v2/test_v2_smoke.py
tests/v2/test_performance_baseline.py
```

测试必须支持冻结时钟、模拟交易所日历、可编排 provider 成功/失败/限频，以及确定性的 synthetic 行情。架构边界测试禁止 V2 主链直接 import V1 的 `core/services/strategies/backtest/report/ui/alpha/indicators/utils/data` 业务模块；唯一例外是有退出条件的 `data.compatibility` shim。性能基线只测本地确定性计算，不把网络和 LLM 延迟混在算法耗时中。

V2-0 必须实现 [GOLDEN_CASES.md](./docs/v2/GOLDEN_CASES.md) 的 G00-G04，不能根据代码结果修改预期。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_architecture_boundaries.py tests/v2/test_v2_smoke.py tests/v2/test_performance_baseline.py -q
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

核心对象包括 `InstrumentId`、`StockMetadata`、`CanonicalBar`、`QuoteSnapshot`、`IntradayBar`、`NewsSnapshot`、`FundamentalSnapshot`、`AccountSnapshot`、`ProviderResult` 和 `DataQualityReport`。精确类型、不变量、可空字段、序列化和稳定键只以 [CONTRACTS.md](./docs/v2/CONTRACTS.md) 为准，本计划不再重复一套易漂移的简化字段表。

### 2.2 必须解决的问题

1. 日 K、实时价、延伸时段价、基本面、新闻都要有明确来源和时间戳。
2. 盘中实时快照只能进入内存决策快照，不得写入正式日 K。
3. 上市日期必须同时约束缓存读取、网络请求、回测窗口和预测训练窗口。
4. 数据缺失不能静默补假值；必须产生结构化 `DataQualityReport`。
5. Tab1/Tab3 可以共用缓存，但都必须独立触发行情、新闻和基本面刷新。
6. A 股和美股必须走各自规定数据源，不能为了实现方便只接一边。
7. 正式交易日、目标日期和会话边界必须由交易所日历与时区计算；日历不可用时不得静默生成正式预测。
8. 日 K 必须统一复权口径并记录公司行动版本，拆股、分红和代码变更不能作为普通波动进入特征。
9. 美股日 K fallback 只能补已完成交易日；A股没有受认可 fallback 时必须明确标记不可用。
10. V2 数据库 schema 必须版本化且可重复执行；V2-1 使用独立 `tradehelper_v2.db`，对 V1 `tradehelper.db` 只做只读迁移预检，正式数据导入留到 V2-12。
11. Provider 必须有批量、并发上限、超时、退避、TTL 和失败状态合同，不能靠页面调用顺序维持数据新鲜度。
12. Provider 未提供的字段必须保存为缺失而不是 0；数据质量按消费者所需字段判断，例如只有价格的延伸时段快照可用于价格条件，但不能确认依赖盘中高低点或成交量的形态。
13. Tab3 数据质量必须逐股隔离，一只股票缺价或过期只能阻断该股票，不能让其他股票把它“带着通过”，也不能拖垮整个组合。
14. 缓存键和 TTL 必须包含市场、数据类型、交易模式、provider 和抓取时点；刷新失败时旧缓存是否可用由新鲜度与用途决定，不能默认继续进入 Alpha。

### 2.3 测试方案

新增或重写测试：

```text
tests/v2/test_data_contracts.py
tests/v2/test_data_quality.py
tests/v2/test_market_data_repository.py
tests/v2/test_provider_fallbacks.py
tests/v2/test_provider_payload_parsing.py
tests/v2/test_trading_calendar.py
tests/v2/test_corporate_actions.py
tests/v2/test_schema_migrations.py
tests/v2/test_cache_policy.py
tests/v2/test_listing_date_policy.py
tests/v2/test_account_contracts.py
tests/v2/test_settings_contract.py
tests/v2/integration/test_live_providers.py
```

测试内容：

1. OHLC 不成立时必须 blocked，正常大涨大跌不能被误判成 OHLC 异常。
2. 新股上市前数据必须被裁剪，不能进入训练/回测。
3. 美股盘中使用 TickFlow；盘前/盘后使用 Nasdaq -> yfinance fallback。
4. A股和美股代码识别、公司名返显、上市日期获取路径一致可测。
5. Nasdaq 历史无结果时，美股按 yfinance -> TickFlow 补已完成交易日；A股 TickFlow 无结果时进入明确缺失状态，二者都必须保留 source 和降级原因。
6. 新闻 empty 状态必须有 TTL，过期后可重新刷新。
7. 真实账户权益为 0 时，数据层提供 0，不允许上层回退到虚构本金。
8. Tab1 和 Tab3 都能独立触发行情、新闻和基本面刷新，不依赖另一页先运行。
9. 同一时点的前复权历史与实时价口径一致，拆股不会制造虚假暴涨暴跌。
10. XNYS/XSHG 节假日、夏令时和目标交易日计算正确；日历不可用时正式预测被阻断。
11. V2 schema migration 重复执行不重复建表/记录；V1 迁移预检只读且不修改 V1/V2 正式数据。
12. Provider 限频、超时和空结果分别产生可恢复状态；empty TTL 到期后能重新刷新。
13. Nasdaq 只返回价格时，缺失 OHLCV 保持 `None`；价格条件可用，依赖高低点/成交量的事实验证被降级。
14. Tab3 某一股票报价缺失时仅该股的 realtime capability 被阻断，其他股票继续独立评估；V2-1 不提前生成 D 级或交易计划。
15. 不同交易模式和数据类型使用独立 TTL，过期缓存刷新失败时不会冒充最新事实。
16. [GOLDEN_CASES.md](./docs/v2/GOLDEN_CASES.md) 中已定义的 G10-G14、G20-G27、G30-G34、G40-G43、G50-G56、G60-G63 按测试映射全部通过。
17. 脱敏真实响应 fixture 解析通过；配置可用时真实 Provider 烟雾通过，否则阶段保持“真实源验证待完成”。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_data_contracts.py tests/v2/test_data_quality.py tests/v2/test_provider_fallbacks.py tests/v2/test_provider_payload_parsing.py tests/v2/test_cache_policy.py tests/v2/test_trading_calendar.py tests/v2/test_listing_date_policy.py tests/v2/test_corporate_actions.py tests/v2/test_market_data_repository.py tests/v2/test_schema_migrations.py tests/v2/test_account_contracts.py tests/v2/test_settings_contract.py -q
TRADEHELPER_LIVE_TESTS=1 venv/bin/python -m pytest tests/v2/integration/test_live_providers.py -q
```

数据层未通过前，不进入特征层；V2-1 通过后停止并等待复审。

## 3. 特征层重构

### 3.1 目标

建立 point-in-time 特征快照，明确“当前时点可见什么”。预测、策略和 LLM 假设验证都使用同一份 `FeatureSnapshot`，避免各算各的。

精确合同、算法口径、持久化和 Golden Cases 见 [docs/v2/V2_2_FEATURES.md](./docs/v2/V2_2_FEATURES.md)。本节只保留架构摘要。

特征分组：

```text
TechnicalFeatures:
  closed returns, ma_distance, volatility, volume, gap, high/low distance

CurrentMarketFeatures:
  fresh quote, current-to-MA distance, spread, session retreat（禁止进入训练）

NewsFeatures:
  finbert_score, news_count, recency_hours, source_count, sentiment_change

FundamentalFeatures:
  valuation, growth, profitability, quality, leverage, source_quality

MarketContextFeatures:
  只接收已有权威上下文事实；当前缺失时明确 unavailable
```

### 3.2 必须解决的问题

1. 预测模型不能再只使用 `momentum_5/momentum_20/trend_20/volatility_20`。
2. 新闻和基本面只有在有真实历史抓取快照时才能进入 OOF；不能用今天的基本面反填历史。
3. 预测和策略必须读取同一份特征快照，保证报告解释与实际计算一致。
4. `Final_Score`、支撑/锁利结论和 LLM 标签不属于 V2-2 事实特征，分别留给预测/策略/LLM 假设层。

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
2. 预先指定 Brier 或 Log Loss 为主要指标，另一项不得显著恶化；不得在看到结果后临时挑选指标。
3. ECE 和 80% 区间命中作为容忍区间内的校准护栏，不要求每项都机械地优于基线。
4. 主要指标相对基线的配对时间块 Bootstrap 改进下界为正，或达到预先定义的实质改善阈值并通过独立确认窗口。
5. 候选数量较多时必须控制模型选择偏差，selection 选型后只能在未参与选型的 confirmation 窗口确认。
6. 不能靠单次报告或单次好运晋升。

回滚条件：

1. 在线 Brier 连续劣化。
2. ECE 明显恶化。
3. 某市场状态下持续失效。

状态必须区分：

```text
insufficient_sample       样本不足，尚未形成统计结论
evaluated_not_better      已评估，但未实质优于基线
calibration_failed        方向可能有效，但概率不可信
drifted                   曾通过，在线表现已退化
champion                  已通过，可以参与执行证据
```

没有 Champion 时仍输出历史频率/市场级基线概率、收益区间和条件观察计划，但基线不能把新开仓升级为 A 级。

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
7. 一个候选仅在某个辅助指标占优、但主要指标或校准护栏失败时不能晋升。
8. 多候选选型不能复用 confirmation 窗口，避免模型选择泄漏。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_forecast_*.py -q
```

## 5. 情景规划层

V2-4 的规范性合同见 [docs/v2/V2_4_SCENARIOS.md](./docs/v2/V2_4_SCENARIOS.md)。该层固定保留 1/3 日战术轴与 5/10 日波段轴，把 ForecastResult、当前 FeatureSnapshot、数据质量和正式交易会话翻译为 TradingScenario；不得生成具体交易动作、价格条件、仓位或订单。

核心验收：

1. 多周期一致、回撤、反弹、震荡、冲突和无 Champion 都有确定结果。
2. 当前价格使用严格 remaining-return 公式，不篡改原预测。
3. 新新闻/基本面事实只形成 unmodeled update 和降级，不由情景层解释好坏。
4. A股/美股三时段使用同一情景算法，差异集中在 session 与认可 quote 来源。
5. 保护性退出家族不被任何预测、冲突或数据阻断删除。
6. migration 8、哈希、幂等读写、架构边界和 SC00-SC21 全部通过。

验收命令：

```text
venv/bin/python -m pytest tests/v2/test_scenario_*.py -q
```

## 6. V2-5 策略层

V2-5 的规范性合同见 [docs/v2/V2_5_STRATEGIES.md](./docs/v2/V2_5_STRATEGIES.md)。本节只保留阶段边界和验收入口；字段、不变量、公式、V1 迁移结论与 Golden Cases 以该规范为准。

### 6.1 输入、输出和边界

```text
FeatureSnapshot + TradingScenario + PositionSnapshot | None
  -> StrategyEngine
    -> StrategyBundle（四个完整分支）
      -> TradePlan[]
```

`StrategyInput` 不包含 AccountSnapshot、账户现金、组合权益、市场成交规则或历史策略健康度。`TradePlan` 只负责动作意图、结构化触发和确认条件、理论触发价、结构止损、止盈方式、持有/失效条件、证据、缺失条件和有效期，明确不包含：

```text
shares / position_pct / account_equity / max_loss_amount
execution_level / approved / order_type / fees / slippage
```

这些字段分别属于 V2-6 `ExecutionDecision` 和 V2-7 `OrderIntent`。策略层不能用默认本金补全风险金额，也不能把 A股一手、T+1、涨跌停或美股延伸时段成交规则散落到模板中。

一次分析必须固定输出 `entry_or_add`、`reduce_or_exit`、`hold`、`invalidation` 四个分支。空仓时明确标记持仓分支不适用；持仓时保护退出不能被预测分歧阻断；没有可执行候选时仍输出结构化条件观察计划。

### 6.2 首批模板与 V1 迁移

首批只实现九类可验证模板：

```text
TrendContinuation
TrendPullback
MA120SupportRebound
RangeMeanReversion
BreakoutConfirmation
ProfitLockAfterHigh
FailedReboundExit
ProtectiveExit
ConditionalObservation
```

V1 的重复趋势和均值回归策略合并为上述家族；DualThrust、TurtleATR、KeyReversal 因当前特征或事件证据不足暂缓；MomentumNews 等独立新闻交易策略等待 V2-9 OOF 证据；HoldUntilBreakeven 不迁移为可执行策略。完整逐项结论和 feature gap 见 V2-5 规范，不能为追求数量把旧策略机械复制进新架构。

保守与激进共享方向、基础触发、止损和失效事实。V2-5 只允许确认门槛不同；除 profiles 外完整业务 payload 相同时必须合并，触发价相同但确认条件不同时保留两条计划且不伪造价格差异。风险金额和仓位差异由 V2-6 基于真实账户生成。

### 6.3 条件、持久化和测试

条件使用可序列化三值 DSL，不使用 `eval`、lambda 或自然语言代替业务逻辑。`crosses_above/crosses_below` 没有事件序列时必须返回 pending_event，缺失事实必须返回 unknown。migration 9 持久化 TradePlan 和 StrategyBundle，并按业务身份幂等、冲突 quarantine、重启后强类型恢复。

SP00-SP29 覆盖合同身份、九类模板、四分支完备、保守/激进、持仓退出、A/美股等价语义、三时段能力边界、数据库、架构和性能。测试文件与固定预期见 V2-5 规范。

验收命令：

```text
venv/bin/python -m pytest tests/v2/test_trade_plan_contract.py tests/v2/test_strategy_*.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

## 7. 风控层重构

V2-6 的规范性合同见 [docs/v2/V2_6_RISK.md](./docs/v2/V2_6_RISK.md)。本节只保留阶段边界；字段、不变量、Decimal 公式、A/B/C/D 矩阵、双市场规则、migration 10 和 RK00-RK42 以该规范为准。

风控官接收完整 StrategyBundle、TradingScenario、DataQualityReport、真实 AccountSnapshot、同批 FrozenAccountValuation、持仓可卖数量、当前股票+策略历史证据和版本化 MarketRuleSet。它为每个 `plan_id + profile` 生成一条 ExecutionDecision，计算单计划最大批准股数和按止损假设的计划亏损，但不能修改 TradePlan 的 action、trigger、stop、take profit 或 invalidation。

固定边界：

- 没有账户、权益为 0 或估值不完整时不得填模拟本金；新开仓最多 C/0 股。
- A 只给当前股票+策略 OOF 可靠正期望；无样本/样本不足最高 B，负期望为 C，证据冲突为 D。A 不要求机械跑赢牛市基准。
- conservative 使用 1% 风险预算/20% 目标软上限，aggressive 使用 2%/25%；两者都受 25% 单票、90% 股票总仓位和同一硬止损约束。
- A股一手、T+1、涨跌停在本层做可执行性预检；订单、跳空、费用、滑点和实际成交属于 V2-7。
- 风险退出不受预测偏多、无 Champion 或历史样本不足阻止；市场当前不能成交时保留紧急 D，不静默删除。
- Tab3 多股票之间的现金争用、相关性和最终分配属于 V2-8；V2-8 只能缩小 V2-6 的单计划批准量。

验收命令：

```text
venv/bin/python -m pytest tests/v2/test_risk_*.py -q
venv/bin/python -m pytest tests/v2/ -q -rs
venv/bin/python -m pytest tests/ -q -rs
```

## 8. 成交与仿真层

V2-7 的规范性合同见 [docs/v2/V2_7_EXECUTION.md](./docs/v2/V2_7_EXECUTION.md)。本节只保留阶段边界；字段、不变量、事件顺序、Decimal 成本公式、双市场最终规则、migration 11 和 EX00-EX49 以该规范为准。

### 8.1 目标

当前建议、订单预览和历史回放必须从同一个 `TradePlan` 生成 `OrderIntent`。V2 不连接券商自动下单，但必须能以统一口径回答“计划是否触发、理论上何时成交、扣除成本后结果如何”。

```text
TradePlan
  -> OrderIntent
    -> TriggerEngine
      -> CurrentOrderPreview / HistoricalFillSimulator
        -> FillEvidence
```

`FillEvidence` 必须记录价格来源、证据粒度、成交假设、费用、滑点、拒单原因和不确定性。缺少分钟 K 时，盘中计划不得使用完整日 K 伪造触发顺序，只能保持待验证或使用明确标记为低质量的保守边界。

### 8.2 成交规则

1. T 日收盘产生的计划最早按 T+1 可成交价格回放，禁止用 T 日收盘前不可见信息成交。
2. 开盘已越过止损时按更差的开盘价成交；盘中穿越止损才按止损价。
3. 同一日 K 同时触碰止盈和止损且无分钟证据时，采用保守路径或标记顺序不可验证，不能选择对策略更有利的路径。
4. A股一手、T+1、涨跌停和费用必须由市场规则适配器执行。
5. 美股/A股费用、动态滑点和 OHLCV 流动性代理必须可审计；无 Level2 时不得虚构盘口深度。
6. 风险退出、普通退出和新开仓分别记录，卖出质量用避免损失减机会成本评估。

### 8.3 测试方案

```text
tests/v2/test_order_intent.py
tests/v2/test_trigger_engine.py
tests/v2/test_fill_simulator.py
tests/v2/test_execution_costs.py
```

测试内容：

1. 当前分析和历史仿真由同一 `TradePlan` 生成等价订单意图。
2. T/T+1、跳空止损、同日双触发、A股涨跌停和一手约束正确。
3. 高波动/低成交量时滑点不低于低波动/高成交量场景。
4. 缺分钟 K 的盘中计划保持待验证，不用日 K 伪造路径。
5. 订单被市场规则拒绝时，策略 Decision 和 Broker/Simulator 结果分别保留。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_order_intent.py tests/v2/test_trigger_engine.py tests/v2/test_fill_simulator.py tests/v2/test_execution_costs.py -q
```

## 9. 组合决策层

本节只保留总体目标。V2-8 的字段、算法、不变量、migration 和测试编号全部以 [docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md) 为准；下面的 `PortfolioDecision` 是早期概念示意，不能据此另建一套合同。

### 9.1 目标

Tab3 不是逐股报告拼接。组合决策层接收所有单股 `ExecutionDecision` 和同一时点冻结的 `AccountSnapshot`，统一生成：

```text
PortfolioDecision:
  frozen_valuation
  holdings_priority[]
  watchlist_priority[]
  replacement_candidates[]
  aggregate_risk_before / aggregate_risk_after
  remaining_risk_capacity
  allocation_reasons
  blocked_plans[]
```

### 9.2 规则

1. 账户权益、持仓市值、总仓位和集中度必须使用同一批冻结价格计算，不能混用成本价和现价。
2. 先处理止损、锁利、过度集中等持仓风险，再考虑关注股新开仓。
3. 资金分配同时受现金、单票上限、股票总仓位、组合相关性和计划最大亏损约束。
4. 持仓与关注股比较使用风险调整后的可执行计划质量，不能把历史回测收益最高直接称为“最优质资产”。
5. 多个相似策略或同一机会不能重复占用风险预算。
6. 不同币种账户必须显式分开估值，未提供可靠汇率时不能合并成一个虚假账户权益。

### 9.3 测试方案

```text
tests/v2/test_portfolio_allocator.py
tests/v2/test_portfolio_ranking.py
tests/v2/test_portfolio_frozen_valuation.py
```

测试内容：

1. 真实余额、现金、持仓数量和成本价改变时，组合计划随之改变。
2. 冻结现价计算的总仓位不因报价预取失败出现超过 100% 的伪值。
3. 风险退出优先于新开仓，高相关候选共享集中度上限。
4. 可用容量为 0 时，新开仓只能作为候选，不显示可执行金额。
5. A股和美股组合分别完成估值、排序和风险分配。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_portfolio_*.py -q
```

## 10. 学习与自优化层

### 10.1 三本账

2.0 必须分开记录：

```text
ForecastLedger:
  预测了哪个目标日、方向概率、收益区间，实际结果如何。

StrategyLedger:
  在当时预测情景下，某策略给了什么计划，触发没有，盈亏如何。

JointLedger:
  预测 + 策略 + 风控官最终组合建议是否赚钱。
```

### 10.2 归因规则

归因不能只按“方向对/错 + 盈/亏”二分。每个到期事件必须分别计算：

```text
ForecastAttribution:
  概率评分、方向、收益区间、校准和市场状态表现

ScenarioAttribution:
  多周期预测是否被正确翻译为 bullish/range/bearish/mixed

StrategyAttribution:
  给定当时情景后，触发、入场、退出和参数是否有正期望

RiskAttribution:
  风控调整相对未经风控计划减少了多少损失或牺牲了多少收益

ExecutionAttribution:
  费用、滑点、流动性和成交规则造成的理论/实际差异
```

只有对应层的反事实证据成立时才能降级该层。例如预测方向正确但概率严重失准，仍应影响预测模型；预测错误但策略因条件未触发而避免损失，应保留策略的容错证据；风控降低亏损时不能把结果完全记到策略名下。

### 10.3 策略晋升与回滚

策略不要求在所有牛市阶段机械地跑赢买入持有。扣除费用和滑点后，候选必须先满足正收益、最少交易数、非灾难性回撤和样本外稳定性，再允许通过以下任一通道：

```text
绝对超额通道：
  足够比例的 walk-forward 窗口取得正基准超额收益，且确认窗口仍成立。

风险调整通道：
  在强牛市中保留大部分基准收益，同时显著降低最大回撤并提高 Sharpe/Calmar。
```

默认初始门槛沿用 V1 已验证经验：风险调整通道至少保留 80% 基准收益、最大回撤降低 30%、Sharpe 提高 0.2；这些数字属于版本化软参数，只能经 OOF 和影子期证据调整。入场策略、普通退出和风险退出必须分开评价；退出质量使用“避免损失 - 机会成本”，不能用买入收益口径评价。

候选需要跨不同数据截止日确认并经过影子观察后才能晋升。在线健康度转负时回滚到上一个可用版本；没有可靠版本时停止新开仓或降为观察，不能为了“始终有结果”强行启用负期望策略。

### 10.4 测试方案

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
4. 预测、情景、策略、风控和成交归因分别记录，不能用单一方向标签决定谁升降级。
5. 单股票负期望策略不能污染其他股票。
6. 行业级有效策略可以作为个股样本不足时的观察 fallback，但不能直接升为个股 A 级。
7. 风控硬约束不可优化；软阈值只有在联合 OOF 与在线确认都改善时才能在预设边界内调整。
8. 自动优化只修改注册模型、特征子集、模板参数和权重，不生成或改写生产 Python 源码。
9. 任何晋升、回滚和停止交易都必须保存旧版本、证据窗口和可恢复状态。
10. 牛市中未跑赢基准、但满足风险调整通道的稳健策略可以进入候选；高收益高回撤策略不能只靠总收益晋升。
11. 买入和退出健康度互不污染，风险退出不因普通入场策略负期望而失效。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_learning_*.py tests/v2/test_attribution_rules.py -q
```

## 11. LLM 假设孵化层

V2-10 的精确合同见 [docs/v2/V2_10_LLM_HYPOTHESES.md](./docs/v2/V2_10_LLM_HYPOTHESES.md)。LLM 的价值从“写报告”升级为“提出研究假设”，输出固定拆为：

```text
forecast_pattern
model_configuration
strategy_configuration
system_challenge
implementation_proposal
```

LLM 只接收冻结的 `ResearchFactManifest`，只能引用 manifest 中的事实 ID，并以严格 JSON Schema 返回。代码使用现有三值 DSL 和注册表生成 `confirmed / refuted / pending / invalid_data`；四种状态都保留。confirmed 只说明当前事实成立，不表示可执行，也不获得 A/B/C/D。

只有能映射到已注册模型、特征集、StrategySpec、参数空间或 counterfactual 的假设，才可创建 V2-9 `CANDIDATE`；未知算法、特征、模板和 DSL 算子转换为 `implementation_required`，系统不得自动生成源码。候选晋升、影子、Champion 和回滚完全委托 V2-9，当前 TradePlan 和风控结果不因 LLM 文本改变。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_research_*.py tests/v2/test_schema_migrations.py -q
```

## 12. UI 与报告重构

V2-11 的精确合同、代码边界、migration 16、Golden Cases 和 UX00-UX59 见 [docs/v2/V2_11_REPORT_UI.md](./docs/v2/V2_11_REPORT_UI.md)。本节只保留产品摘要；实现发生冲突时以该规范为准。

### 12.1 UI 原则

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

### 12.2 报告原则

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

所有专业指标必须提供就地解释或“怎么看”说明。历史评估中的图表必须显示标题、样本范围、横纵轴、基线、当前值和一句话结论；预测记录必须直接说明“何时预测、预测哪个目标日、当时参考价、实际收盘价、方向是否正确”。

交互运行必须公开阶段进度：数据刷新、特征、预测、策略、风控、组合分配、报告生成分别可见。深度 OOF 和参数优化放入可取消的后台单线程任务，前台分析不得等待整轮候选搜索；后台结果只有完成验证后才影响下一次分析。

V2-0 先建立不含网络和 LLM 的性能基线，V2-11 发布前至少满足：点击分析后 500ms 内出现进度；缓存命中的 Tab1 确定性主链 p95 不超过 5 秒；10 只股票的 Tab3 确定性主链 p95 不超过 20 秒。外部供应商和 LLM 延迟单独统计，并受超时、并发和降级策略约束，不能混入算法性能掩盖瓶颈。

### 12.3 测试方案

新增测试：

```text
tests/v2/test_report_sections.py
tests/v2/test_report_readability_snapshots.py
tests/v2/test_ui_state_flow.py
tests/v2/test_report_history_flow.py
tests/v2/test_settings_flow.py
```

测试内容：

1. 没有 Champion 时，报告显示“已评估但未跑赢基线/样本不足”，不能只写 OOF 未通过。
2. 报告必须同时出现预测结论、情景、交易计划、风控等级和历史证据。
3. 用户持仓信息必须影响 Tab3 计划，不允许虚构本金。
4. 历史评估页必须分开展示预测账、策略账、联合账。
5. HTML/PDF 关键表格字段不能缺股票、目标日期、实际结果。
6. Tab1 单股报告和 Tab3 组合报告都必须覆盖 A 股和美股示例。
7. Brier、Log Loss、ECE、Alpha、Sharpe 和 OOF 状态都有通俗解释、样本数和阅读结论。
8. 图表横纵轴、基线、图例和空样本状态完整，不能只显示一张无说明的函数图。
9. 长任务逐阶段更新进度，后台优化不会阻塞前台报告，也不会使用未完成结果。
10. 非网络性能基准满足既定 p95 预算，供应商/LLM 等待时间在诊断中单独展示。
11. 历史报告可按股票、市场、模式、日期和评分检索，打开旧报告不会触发重新分析。
12. 新用户必须先完成工作目录和必填数据源配置；旧用户配置迁移后保持可用，敏感值不写入日志或报告。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_report_*.py tests/v2/test_ui_state_flow.py tests/v2/test_settings_flow.py -q
```

## 13. 端到端、迁移与发布测试方案

每一层通过后，再跑端到端。端到端不替代单元测试。

新增测试：

```text
tests/v2/test_e2e_single_stock.py
tests/v2/test_e2e_portfolio.py
tests/v2/test_e2e_a_share.py
tests/v2/test_e2e_us_extended_hours.py
tests/v2/test_e2e_mode_matrix.py
tests/v2/test_v1_to_v2_migration.py
tests/v2/test_interactive_performance.py
```

三时段验收矩阵：

| 页面 | 市场 | 盘前 | 盘中 | 盘后 |
|------|------|------|------|------|
| Tab1 | 美股 | Nasdaq -> yfinance、当日计划 | TickFlow 实时、当日剩余会话 | 正式收盘、下一交易日计划 |
| Tab1 | A股 | 无连续盘前价时用 T-1 条件计划并明示 | TickFlow 实时、A股规则 | 正式收盘、下一交易日计划 |
| Tab3 | 美股 | 冻结组合估值、持仓优先、延伸时段来源 | 批量 TickFlow、组合分配 | 下一交易日组合计划 |
| Tab3 | A股 | T-1 组合条件计划、不伪造盘前价 | 批量 TickFlow、T+1/涨跌停 | 下一交易日组合计划 |

核心场景：

1. 美股盘后单股：完整日 K + 新闻 + 基本面 -> 预测 -> 情景 -> 策略 -> 风控 -> 报告。
2. 美股盘前组合：Nasdaq 延伸时段报价 -> 持仓风控 -> 条件计划。
3. A股组合：TickFlow 日 K/盘中 -> A股交易规则 -> 报告。
4. 新股：上市日期裁剪 -> 样本不足降级 -> 不伪造历史。
5. 预测正确但策略亏损的 synthetic 场景：预测、情景、策略、风控和成交分别归因，不能简单只改一层。
6. 预测错误但策略避险盈利的 synthetic 场景：归因必须保留策略容错和风控贡献。
7. V1 真实账户、持仓、成本和有效历史证据迁移后数值不变；错误或无法恢复的旧记录隔离且不参与学习。
8. 迁移在新用户空库、旧用户数据库和已迁移数据库上都可重复安全执行。
9. 缓存命中时前台确定性分析不等待 OOF 搜索；供应商超时后能降级完成或给出明确阻断状态。

发布验收：

1. macOS 本地构建后启动应用并运行导入烟雾测试。
2. Windows 本地 bat 与 GitHub Actions 构建后都必须启动 EXE，验证 jaraco、akshare 数据文件、交易日历、FinBERT 和 HTML/PDF 导出依赖。
3. 打包体积可以后续优化，但不能通过删除实际运行依赖换取体积下降。
4. 发布前保存 V1 数据备份并执行只读迁移预检，迁移失败不得破坏原数据库。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_e2e_*.py -q
venv/bin/python -m pytest tests/ -q
```

## 14. 实施顺序

2.0 严格从下往上做：

| 阶段 | 内容 | 主要交付物 | 必跑测试 |
|------|------|------|------|
| V2-0 | 测试基础设施 | fixture、冻结时钟/provider、架构边界、性能基线 | smoke + boundary |
| V2-1 | 数据层 | 数据合同、质量闸门、provider fallback、repository | `test_data_*` |
| V2-2 | 特征层 | FeatureSnapshot、无未来数据特征、缺失降级 | `test_feature_*` |
| V2-3 | 预测层 | 多特征预测、股票/行业/市场模型注册、OOF | `test_forecast_*` |
| V2-4 | 情景层 | TradingScenario | `test_scenario_*` |
| V2-5 | 策略层 | Scenario-driven TradePlan | `test_strategy_*` |
| V2-6 | 风控层 | ExecutionDecision、仓位和市场规则 | `test_risk_*` |
| V2-7 | 成交仿真层 | OrderIntent、触发器、费用/滑点、成交证据 | `test_order_*`, `test_fill_*` |
| V2-8 | 组合决策层 | 冻结估值、排序、风险分配、替换机会 | `test_portfolio_*` |
| V2-9 | 学习层 | 三本账、分层归因、自优化 | `test_learning_*` |
| V2-10 | LLM 假设层 | 受控 DSL、验证、候选晋升 | `test_llm_*` |
| V2-11 | 报告/UI | 新报告结构、历史评估、Tab1/Tab3、进度 | `test_report_*`, `test_ui_*` |
| V2-12 | 迁移/端到端/发布 | V1 数据迁移、完整时段矩阵、macOS/Windows 烟雾 | `test_e2e_*`, migration, full tests |

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

## 15. 阶段状态

### 2026-07-07 前置整理

- 已将 1.x README、DESIGN、UPGRADE_PLAN、回测说明、优化审计和 Claude 旧说明归档到 `docs/archive/v1/`。
- 根目录只保留当前 V2 入口文档：`README.md`、`DESIGN.md`、`V2_REFACTOR_PLAN.md`，以及本地代理说明 `AGENTS.md`/`CLAUDE.md`。
- 已删除未被引用的 1.x 架构预留占位模块 `data_adapters/`。
- 已清理本地 `.DS_Store`、`__pycache__` 和 `.pytest_cache`，保留 `venv/build/dist/dist_data` 等不可从 git 恢复的本地产物。
- 完整测试通过：`260 passed`。

### 2026-07-10 设计复审补强

- 将单一 `contracts.py` 调整为按市场数据、分析、决策和学习分包，避免 V2 再形成合同巨型文件。
- 新增成交仿真层和组合决策层，明确同一 `TradePlan -> OrderIntent` 同时服务当前建议与历史回放。
- 规划 `StrategyBundle`、保守/激进档案、计划会话和有效期，恢复 V1 已确认的完整条件计划语义；精确合同随后在 V2-5 规范中冻结。
- 明确预测主要指标与校准护栏、策略绝对超额/风险调整双通道、五层效果归因和受控可回滚优化。
- 补充交易所日历、时区、复权/公司行动、缺失字段、逐股质量隔离、provider 配额、幂等迁移和 V1 数据保护。
- 增加 Tab1/Tab3 × A股/美股 × 盘前/盘中/盘后完整验收矩阵，以及前台性能和 macOS/Windows 发布烟雾标准。
- 新增 `docs/v2/CONTRACTS.md`、`POLICIES.md`、`GOLDEN_CASES.md`，固定 V2-0/V2-1 的类型、数据源路由、缓存/质量常量、独立数据库边界及标准答案。
- 修正 `AGENTS.md` 主流程，明确当前决策链与共享验证链；当前实现授权只到 V2-1。
- 本次仅完成设计文档补强；V2-0 至 V2-12 实现状态仍为未开始。

### 2026-07-10 V2-0 完成：测试基础设施

- 新增隔离的 V2 生产模块及 `tests/v2/`，测试使用固定时钟、注入日历、脚本化 Provider 和临时 SQLite，默认不读取用户工作目录或访问网络。
- 固定 A股 `600519/000001/430047` 与美股 `AAPL/BRK.B` 合同 fixture；架构边界测试会定位非法 V1 业务模块 import；本地 10,000 条日 K 合同性能基线通过。
- 吸收 V1 资产：双市场代码规范、旧系统与新系统隔离的教训。尚未迁移 V1 的特征、预测、策略、风控或页面实现。
- 验证：`venv/bin/python -m pytest tests/v2 -q` -> `53 passed, 1 skipped`；全项目回归未记录失败。

### 2026-07-13 V2-1 完成：数据层

- 实现不可变数据/账户/Provider/质量合同、确定性 JSON/哈希、独立 V2 schema 与只读 V1 迁移预检；开发期仅写 `tradehelper_v2.db`。
- 实现 TickFlow、Nasdaq、yfinance、Finnhub、baostock、akshare 的脱敏 payload 适配器和离线 fixture。数据路由为：美股盘中 TickFlow、美股延伸 Nasdaq -> yfinance、美股已完成日K Nasdaq 历史 -> yfinance -> TickFlow；A股日 K/盘中仅 TickFlow、盘前不伪造连续报价；基本面/新闻按市场路由，LLM 未进入 Provider 链。
- 实现上市日期窗口裁剪、上市日前记录 quarantine、独立 quote/intraday/daily 表、点时新闻/基本面查询、负缓存、数据质量/新鲜度和真实账户零权益合同。
- TickFlow 真实验证发现A股日K为单标的 `10/min` 配额：新增组合日K批次合同、`retry_at`、缓存优先的尾部增量拉取与A股待重试队列；配额状态不再伪装成空数据。美股已完成日K切换为 Nasdaq 历史主源，避免无必要消耗 TickFlow 美股日K配额。
- 完成真实源交叉审计与券商同日K核对：Nasdaq 历史端点与当前用户券商美股K线一致，覆盖 NVDA 拆股前后；LITE/SNDK 最新日存在可审计修订差，仍需保留 source/corporate-action 审计。baostock 与 TickFlow 在多数A股样本一致，600036 的长期前复权序列却系统性不同，券商口径与 TickFlow 一致，因此 baostock 不进入A股日K fallback。SPCX 的 TickFlow 返回上市日前及美股休市日零成交量伪K线，现已新增“上市日期 + 交易所日历”入库前闸门与 quarantine 审计。
- 吸收 V1 资产：TickFlow A股成交量“手”转“股”、美股延伸时段源边界、SPCX 新股窗口问题、Tab1/Tab3 共享缓存但独立刷新、实时价不得污染正式日 K。
- 真实源抽样验证：使用用户当前 4 只美股持仓、7 只关注股与随机 11 只A股，TickFlow 报价均成功；日K第11只均触发同一 `10/min` 配额，等待窗口后 SNDK/688981 可正常补回；Nasdaq 11/11 延伸报价、Finnhub 11/11 profile/基本面/新闻均成功。此验证不写入数据库。
- 新增 V2 独立 production composition root：不 import V1 业务模块，可从 `config_v2.json` 读取凭据；开发期 opt-in smoke 可显式提供已有 JSON 配置作为测试输入，但不构成 V2 对 V1 设置的运行时依赖。
- 新增跨 Tab/跨进程持久化 TickFlow A股日K与实时行情滚动配额、`refresh_queue` 自动续跑与日K跨源漂移审计。漂移记录不覆盖正式主序列；实际 TickFlow DataFrame 曾出现显示日号 `date` 与完整 `trade_date` 并存，解析器已固定优先完整交易日并补回归测试。
- 修复审计发现的报价与新闻时点语义：Nasdaq 真实嵌套 `primaryData` 响应可解析；无市场观察时间的报价保持 `missing_timestamp` 并阻断实时执行；新闻以系统首次获取时间作为 `available_at`，重复刷新不把历史可见时间推后，也不允许历史回放提前看到新闻。
- 基本面改为逐字段合并并保留字段来源与报告期：A股 baostock 提供估值、盈利、成长和杠杆事实，akshare 降级补充；美股按 Finnhub -> yfinance -> akshare -> 百度可验证估值页补全。字段家族覆盖不足时明确降级，不再用“有任意一个数值”冒充基本面完整。
- schema 升至迁移版本 4，修复完成任务重复入队的唯一键冲突；日K和 Provider 续跑任务复用稳定身份、可恢复失败退避重试、超过上限进入 failed。SQLite repository 的读写统一受可重入锁保护，并有多线程回归测试。
- 批量实时报价先复用一分钟内缓存和 repository 新鲜快照，只对缺失标的消耗 TickFlow 额度；Nasdaq/Finnhub 网络传输使用并发上限 2，避免 Tab3 预取无节制冲击免费接口。
- 真实 V2 composition smoke 已于 2026-07-12 扩展通过：AAPL 的 Finnhub 名称/上市日期/基本面/新闻、Nasdaq 完成日K，以及 Finnhub 缺失时 yfinance 基本面降级；600519 的 baostock 名称/上市日期/基本面、TickFlow 前复权完成日K与东方财富经 akshare 标准化新闻均通过，测试只写临时 V2 数据库且不输出凭据。
- 实测压力边界：V2 服务层美股 50/50、A股 50/50 常规时段报价均成功；每市场恰好消耗 TickFlow 的 10 次批量请求额度。确定性测试覆盖第51只不发送、同分钟第11个请求被持久化预算拦截、A股日K第11只自动续跑，以及 Finnhub 第61个共享请求的事实任务自动续跑。
- 2026-07-13 修复后真实 11 股复验：A股与美股常规实时报价均为 11/11 成功；美股 Nasdaq 已完成日K为 11/11 成功，每只返回 2026-07-01 至 2026-07-10 的 7 个有效交易日；A股 TickFlow 日K为前 10 只成功，第 11 只 `688981` 明确返回 `rate_limited` 并进入 pending。下一额度窗口单独补取 `688981` 成功，并发现 TickFlow `start_time` 为排他边界；生产请求已向前缓冲一天、服务层再按请求/上市日期裁剪，修复后 `688981` 正确返回 7月1日至10日的 8 个A股交易日。结论是批量报价超过 10 只不会失败，但 A股冷缓存日K仍受套餐单标的 `10/min` 物理上限，必须跨窗口续跑，不能宣称同一分钟全部完成。
- 确定性验证：`venv/bin/python -m pytest tests/v2/ -q` -> `73 passed, 2 skipped`；全项目回归 `venv/bin/python -m pytest tests/ -q` -> `333 passed, 2 skipped`。两个 skip 是默认关闭的 opt-in 真实 Provider smoke，不是失败；本轮 TickFlow 起始边界修改与定向测试已包含在 V2 全量结果中。
- 真实源验证：`TRADEHELPER_LIVE_TESTS=1 TRADEHELPER_LIVE_SETTINGS_PATH=... venv/bin/python -m pytest tests/v2/integration/test_live_providers.py -q -rs` -> `2 passed in 23.42s`。AAPL/600519 真实组合链路与美股 yfinance 基本面 fallback 均通过。
- 剩余风险：免费 Provider 本身仍可能临时限频、修订或不可用；V2-1 已把它们建模为可审计的状态、持久化续跑或明确降级，而不是伪造数据。按阶段纪律，开始 V2-2 前仍需先完成复审并补充 V2-2 精确合同和 Golden Cases。

### 2026-07-13 V2-2 完成：特征层

- 新增 `FeatureValue`、`FeatureInputs` 和 `FeatureSnapshot` 不可变合同；以 canonical JSON + SHA-256 固定输入与特征哈希，`generated_at` 不影响复现结果，并显式标记 `observed_snapshot` 或 `reconstructed_history` 证据等级。
- 实现纯本地的 closed technical、current market、news 和 fundamentals 特征。正式日K按交易模式和截止时点过滤；新鲜 quote 只进入 `current.*`；新闻按 `published_at + available_at`、基本面按快照和字段可见时间过滤，缺失/失败/样本不足均保留为结构化状态而非填充 0 或中性值。
- 实现 A股/美股共用公式，以及严格的 `source + raw_field + unit -> canonical_name + scale` 基本面注册表；未登记字段或单位明确缺失。`observed_snapshot` 必须由受信任的完整输入证据验证器确认，普通历史重建不能自行声称为观察快照；无市场/行业事实时固定产生 `context.market/context.industry = missing/CONTEXT_INPUT_UNAVAILABLE`。
- schema 升至 migration 5，新增 `feature_snapshots` 与幂等 `FeatureStore`；相同快照不重复写入，确定性冲突保留原记录并进入 quarantine。
- 吸收 V1 资产：多周期技术事实、实时价与收盘日K隔离、新闻/基本面点时可见性、缺失不伪装成中性值。`Final_Score` 和买卖判断仍按边界留在后续阶段。
- 审计修复：真实 Finnhub/yfinance/baostock/akshare payload 现在必须经生产 parser 后才能通过 F08；修复美股基本面原始字段和缺失单位无法进入 canonical 特征的问题，并移除 `debtToEquity -> debt_ratio` 的错误语义映射。未知字段和未知单位仍保持缺失。
- 审计修复：canonical 基本面不再依赖字段名排序，而是显式使用“canonical 指标 + 供应商 + 期间字段”优先级。美股选择 Finnhub TTM/MRQ 字段并保留 yfinance 有效降级；A股按字段组合 baostock 与东方财富经 akshare 的事实。baostock `MBRevenue` 与发行人营业收入口径不一致，不再推算 `revenue_growth_yoy`；A股 canonical 加权 ROE 和营业收入同比改用公告口径字段。
- 审计修复：FeatureBuilder 按当前模式和截止时点重新判断 quote 新鲜度；横盘 RSI 固定为中性50；无可见新闻时评分覆盖率保持缺失；重复交易日直接违反输入合同；`generated_at` 记录实际计算时间；FeatureStore 默认绑定明确特征版本。
- 真实基本面复核（2026-07-13）：测试只在内存中读取 V1 Finnhub token，不输出凭据且不形成 V2 生产依赖。AAPL canonical 为 PE 37.7827、PB 43.4893、PS 10.2587、TTM ROE 146.69%、毛利率 47.86%、TTM 营收同比 12.76%，Finnhub 缺失的净利润同比由 yfinance 补充为 21.8%；网页同期 PE/PB/PS 约 38.22/43.43/10.26，毛利率 47.86%，价格型估值差异来自取价时点。网页 ROE 为 141.47%，与 Finnhub TTM ROE 存在公式/平均权益口径差，系统保留 Finnhub 来源和期间，不宣称两者完全相同。没有可靠债务/资产比时保持缺失。600519 canonical 为 PE 18.301812、PB 5.588297、PS 8.635018、年报加权 ROE 32.53%、毛利率 91.1796%、营业收入同比 -1.2001%、净利润同比 -4.5049%、负债率 16.4154%；其中 ROE、营业收入同比和净利润同比与公司2025年年报披露的 32.53%、-1.21%、-4.53% 对齐。
- 验证：`venv/bin/python -m pytest tests/v2/ -q -rs` -> `102 passed, 3 skipped`；`venv/bin/python -m pytest tests/ -q -rs` -> `362 passed, 3 skipped`。三个 skip 是默认关闭的真实网络 smoke；显式使用 V1 本地凭据运行 `TRADEHELPER_LIVE_TESTS=1 TRADEHELPER_LIVE_USE_V1_SETTINGS=1 venv/bin/python -m pytest tests/v2/integration/test_live_providers.py -vv -rs` -> `3 passed in 44.69s`，覆盖 AAPL Finnhub/Nasdaq/新闻、600519 baostock/TickFlow/新闻、yfinance 基本面降级与 akshare 明确年报字段。
- 剩余风险：V2-1 的基础事实没有逐次修订历史，因此从当前 canonical 数据回放的历史快照必须保持 `reconstructed_history`；新闻 FinBERT 标签仅在已有事实提供时参与情绪均值；市场/行业上下文尚无权威输入，仍明确缺失。V2-3 之前需要复审预测层合同，不能直接把当前缺失特征编码或填补。

### 2026-07-14 V2-3 完成：预测层（复审修复通过）

- 新增不可变 ForecastRequest/Result、方向概率、收益区间、训练样本和模型版本合同；预测只接受 EOD 特征快照和前复权收盘 reference bar，固定支持 1/3/5/10 个交易日。
- 实现波动率缩放三分类标签、到期样本 purge、显式 FeatureSet、训练折中位数/IQR 缩放与缺失指示列。`current.*`、绝对 MA 和未登记文本不会进入矩阵。
- 实现经验基线、analog、logistic、概率树、ensemble 和真实 regime-analog 候选；修复近邻权重归一化，概率温度校准进入 artifact 和推理，Tree 叶节点下限、20 个候选及 class-mixture 收益区间均按规范执行；artifact 仅为 canonical JSON + zlib，不使用 pickle/joblib。
- 实现 expanding-window OOF、完整交易日 selection/confirmation 隔离、Brier/LogLoss/ECE/区间覆盖和固定种子的向量化时间块 bootstrap。模型每 20 个交易日重训、期间每日继续样本外预测；只有确认通过的 stock Champion 才能标记为 execution eligible。
- schema 升至 migration 7：migration 6 保持 checksum 不变，migration 7 增加模型样本证据并允许日历失败时目标日为空。模型、评估、预测结果和 Champion 均支持幂等读写与重启恢复；仅 generated_at 不同不会误入 quarantine。
- 验证：`venv/bin/python -m pytest tests/v2/test_forecast_*.py -q` -> `34 passed in 31.27s`；`venv/bin/python -m pytest tests/v2/ -q -rs` -> `137 passed, 3 skipped in 37.01s`；`venv/bin/python -m pytest tests/ -q -rs` -> `397 passed, 3 skipped in 67.23s`。FC00-FC18 已覆盖合同、日历、点时样本、校准、真实状态近邻、可预测/随机 synthetic、双市场对称、完整 fallback、重启持久化、双市场 FeatureSnapshot smoke、取消和性能；500 点×4 horizon×完整候选池为 `2 passed in 29.94s`。
- 剩余风险：正式行业/市场 Champion 仍依赖未来可获得的 point-in-time 行业历史；已有 `reconstructed_history` 不得被用于跨股票确认。V2-3 不验证到期事实、不记录预测/策略/联合账，均明确留给 V2-9。

### 2026-07-14 V2-4 设计完成：情景层（待实现）

- 新增规范 [docs/v2/V2_4_SCENARIOS.md](./docs/v2/V2_4_SCENARIOS.md)，固定 ScenarioRequest、DecisionSession、HorizonAssessment、CurrentOverlay 和 TradingScenario 合同。
- 多周期不再粗暴平均：1/3 日为战术轴，5/10 日为波段轴；短期回撤与波段偏多解释为 bullish_pullback，真正的同轴矛盾才标记 conflict。
- 当前价只转换剩余收益区间，不修改原预测；盘前/盘中新增新闻或基本面必须携带显式 ScenarioFactUpdate，标记为 unmodeled update 并要求重新确认，不能靠时间衰减后的特征变化猜测。
- 固定 A/美股三时段会话、日历不可用、无 Champion、数据质量阻断、策略家族兼容性和保护性退出不可阻断规则。
- 设计 migration 8、SC00-SC21、双市场对称、架构边界和性能标准；本阶段只完成文档，不包含 V2-4 实现代码。

### 2026-07-14 V2-4 完成并复审：情景层

- 新增不可变 `DecisionSession`、`ScenarioFactUpdate`、`ScenarioRequest`、`HorizonAssessment`、`CurrentOverlay` 和 `TradingScenario` 合同。情景层只输出预测环境与策略家族兼容性，未创建 TradePlan、订单、仓位、止损或风险评级。
- 实现 1/3 日战术轴、5/10 日波段轴的确定性归并：短期回撤与波段趋势可同时表达为 `bullish_pullback`/`bearish_rebound`，同轴冲突明确降级为 `forecast_conflict`，不做概率平均掩盖分歧。
- 实现三时段当前覆盖、quote 时效与会话匹配、剩余收益区间、ATR 偏离、显式新闻/基本面事实更新，以及新事实不改写 V2-3 ForecastResult 的边界。
- 扩展注入式与 exchange-calendars 会话窗口；schema 升至 migration 8，TradingScenario 以业务身份幂等写入，冲突 quarantine，重启后可按强类型读取。
- 复审修正身份与政策边界：ForecastResult.generated_at 不进入 forecast bundle/scenario 身份；波段震荡与战术方向组合保持 mixed；美股盘前只接受 Nasdaq/yfinance，A/美股盘中只接受 TickFlow；盘前陈旧报价至少 degraded，blocked/observation 姿态不会被覆盖规则弱化。
- 强化输入、输出和持久化合同：校验报价载荷、质量时间、注册特征、reason code、SHA-256、策略家族、不变量和 scenario 身份；读取时复核数据库索引列与业务 payload，日历故障不再伪装为普通休市。
- 显式新闻更新按加入该事实前后的特征语义差异记录 affected_features，不再把所有新闻固定写成 `news.count_1d`。
- 吸收 V1 资产：盘前/盘中/盘后会话边界、预测与当前事实隔离、保护性退出永不被预测阻断。尚未迁移具体策略形态、TradePlan、风控、成交与账户约束。
- 验证：V2-4 专项 SC00-SC21 `46 passed`；V2 全量 `183 passed, 3 skipped`；项目全量 `443 passed, 3 skipped`。被默认跳过的 3 条真实 Provider 冒烟测试使用 V1 本地配置显式启用后为 `3 passed`，覆盖双市场组合刷新、yfinance 美股基本面后备和 akshare A股年度字段后备。剩余边界：行业/市场预测仍只可作观察证据；新增事实只表示需要重新确认，不在情景层判断利多/利空。

### 2026-07-14 V2-5 设计完成：策略层（待实现）

- 新增规范 [docs/v2/V2_5_STRATEGIES.md](./docs/v2/V2_5_STRATEGIES.md)，冻结 StrategyInput、结构化条件 DSL、TradePlan、StrategyBundle、九类首批模板、migration 9 和 SP00-SP29。
- 明确策略与风控所有权：TradePlan 生成动作、触发、结构止损、止盈、失效和有效期；股数、仓位、账户最大亏损和 A/B/C/D 只能由 V2-6 使用真实账户生成。
- 完成 V1 策略迁移矩阵：合并重复策略，保留 MA120 支撑、冲高回落锁利、反抽失败退出和条件观察；对证据不足、需独立 OOF 或存在风险缺陷的策略明确暂缓/不迁移原因。
- 设计双市场与三时段对称验收、四分支完备、保护性退出不被预测阻断、无事件证据不伪造穿越，以及当前预览/历史仿真共用 TradePlan 的边界。本阶段只有设计文档，不包含 V2-5 实现代码。

### 2026-07-14 V2-5 完成并复审：策略层

- 新增不可变策略合同、三值条件 DSL、冻结注册表与九类首批模板；`StrategyEngine` 仅以 FeatureSnapshot、TradingScenario 和可选 PositionSnapshot 生成 TradePlan/StrategyBundle，不读取账户现金、网络或 V1 策略。
- 输出固定四分支；空仓明确标记持仓分支不适用，持仓始终保留保护退出、持有和失效分支。买入/加仓无结构止损时只能观察，事件穿越条件保留为 pending_event。
- schema 升至 migration 9，TradePlan/StrategyBundle 以业务身份幂等写入，冲突进入 quarantine，读取时重建强类型对象并复核索引列。
- 复审修复三值 DSL 的 `ALL/ANY` 强逻辑、趋势回踩上下界、持有/保护退出失效条件、参数实际生效、缺失特征结构化观察、2R 量化语义和策略动作合同；同一顶层条件只求值一次。
- 持久化幂等比较递归排除嵌套 `generated_at`，补齐 plan/bundle 索引列强校验、冲突 quarantine 和关闭数据库后的重启恢复测试。
- SP00-SP29 已逐项映射为可执行测试；SP27 使用不同发行时间绕过缓存，真实重建 1000 个完整 bundle 本机约 `1.30s`。
- 验证：V2-5 专项及 migration `38 passed`；V2 全量 `218 passed, 3 skipped`；项目全量 `478 passed, 3 skipped`。3 条默认关闭的真实 Provider 测试显式启用后 `3 passed in 40.96s`。剩余边界严格留给后续阶段：执行等级、股数、风险金额属于 V2-6，订单与触发成交属于 V2-7。

| 阶段 | 状态 | 说明 |
|------|------|------|
| V2-0 测试基础设施 | 已完成 | Golden G00-G04、架构边界、冻结时钟、双市场 fixture 与性能基线已落地 |
| V2-1 数据层 | 已完成 | Golden G10-G29/G30-G63、Provider fixture、路由、时点语义、质量、独立 repository、持久化配额续跑、并发、日K跨源漂移审计及真实 Provider smoke 均已通过 |
| V2-2 特征层 | 已完成 | FeatureSnapshot、F00-F13、双市场点时特征、migration 5/FeatureStore、架构边界、性能及全量回归已通过 |
| V2-3 预测层 | 已完成并复审 | Forecast contracts、波动率标签、FeatureSet/校准、JSON+zlib artifact、20候选、maturity-purged OOF、registry 回退/重启恢复、migration 6/7 和预测快照幂等读写已通过 FC00-FC18；不生成 TradePlan |
| V2-4 情景层 | 已完成并复审 | TradingScenario 合同、多周期归并、来源/时效降级、当前事实覆盖、三时段会话、策略家族兼容性、migration 8、强校验持久化和 SC00-SC21 共46条测试已通过；不生成 TradePlan |
| V2-5 策略层 | 已完成并复审 | TradePlan/条件 DSL、九类首批模板、四分支 StrategyBundle、migration 9、强类型持久化、双市场/三时段语义与 SP00-SP29 已通过；不包含 V2-6 风控或之后模块 |
| V2-6 风控层 | 已完成并复审 | ExecutionDecision、真实账户冻结估值、A/B/C/D、Decimal 单计划容量、风险成本预留、A/美股规则预检、migration 10 与 RK00-RK42 测试映射已完成；不包含 V2-7/V2-8 模块 |
| V2-7 成交仿真层 | 已完成并复审 | OrderIntent、冻结条件触发、当前预览、历史仿真、Decimal 成本、双市场最终检查、migration 11 与 EX00-EX49 均已通过；不包含 V2-8 组合分配 |
| V2-8 组合决策层 | 已完成并复审 | 不可变组合批次/风险快照/相关性证据、保护退出优先、双 profile 独立 waterfall、现金/heat/相关性约束、共享退出、替换研究候选、V2-7 最终股数装配及 migration 12 原子持久化已通过 PO00-PO49；实现严格止步于 V2-8。 |
| V2-9 学习层 | 已完成并复审 | 精确合同见 `docs/v2/V2_9_LEARNING.md`；已实现到期验证、三本账、六层归因、purged OOF、股票绑定自优化、候选生命周期、migration 13/14 和 LE00-LE59，止步于学习层 |
| V2-10 LLM 假设层 | 已完成并复审 | 精确合同见 `docs/v2/V2_10_LLM_HYPOTHESES.md`；冻结事实 manifest、严格 JSON 五类假设、确定性四态验证、注册候选桥接、独立 outcome/metric 账、migration 15、强类型恢复、LL00-LL49 与双市场失败降级均已通过；未进入 V2-11。 |
| V2-11 报告/UI | 已完成并复审 | 精确合同见 `docs/v2/V2_11_REPORT_UI.md`；确定性 PresentationInput/ReportDocument、Tab1/Tab3、历史评估、进度、历史快照/反馈/比较/归档、导出、migration 16、UX00-UX59 和双市场 Golden Cases 已通过，严格止步于本阶段 |
| V2-12 迁移/端到端/发布 | 代码完成，本机复审通过；待 Windows 产物验收 | 精确合同见 `docs/v2/V2_12_MIGRATION_RELEASE.md`；migration 17、只读 fingerprint/备份/事务迁移、旧证据隔离、唯一 production composition、双市场应用编排、FinBERT、后台 revision、V1 退出、RL00-RL79、真实 Provider/LLM 和 macOS 包内 smoke 已通过；Windows spec/CI 已就绪，实际产物须在 Windows runner 验收 |

### 2026-07-15 V2-6 设计完成：风控层（待实现）

- 新增规范 [docs/v2/V2_6_RISK.md](./docs/v2/V2_6_RISK.md)，冻结 RiskRequest、ExecutionDecision、RiskDecisionBundle、版本化 RiskPolicy、migration 10 和 RK00-RK42。
- 禁止默认本金：股数、仓位和计划亏损必须来自真实 AccountSnapshot 与同一批 ValuationPrice；任一活跃持仓缺价时不用成本价补齐权益。
- 固定 A/B/C/D 与条件批准语义：等待计划不冒充当前可执行，C/D 不静默删除，保护性退出不被偏多预测、无 Champion 或历史样本不足阻断。
- 明确单计划与组合边界：V2-6 只给出每个 plan/profile 的最大批准量；多股票现金争用、相关性、替换和最终分配留给 V2-8，且只能缩小 V2-6 上限。
- 双市场预检覆盖 A股一手、T+1、涨跌停与美股延伸时段流动性代理；订单、tick size、跳空、费用/滑点与成交证据仍属于 V2-7。

### 2026-07-15 V2-6 完成并复审：风控层

- 新增不可变风险合同、冻结真实账户估值、版本化市场规则与 RiskPolicy；金额、股数、摩擦预留和计划止损亏损均使用 Decimal，任何持仓缺价均保持估值不完整，绝不以成本价或默认本金补齐。
- `RiskOfficer` 为每个 TradePlan/profile 输出 A/B/C/D 与单计划最大批准量；等待计划仅条件批准，C/D 与保护退出完整保留，不能改写 TradePlan。
- schema 升至 migration 10；估值、ExecutionDecision 和 RiskDecisionBundle 支持幂等写入、冲突 quarantine 与强类型重建。
- 复审收紧风险合同：估值必须与账户币种、持仓集合、股数和汇总金额完全一致；策略、账户、行情模式、证据、规则和政策身份均进入强校验，C/D、条件批准和当前可执行状态不能互相混用。
- 复审修正执行语义：盘后计划只面向下一会话并要求重检；过期入口/退出全部驳回但保留保护退出；A股 T+1、零可卖、部分可卖、整手减仓、零股全卖和涨跌停分支均按规范处理。
- 复审修正 sizing 与审计：add 的最大亏损包含已有持仓到同一止损的风险；容量明确区分风险、现金、单票、总仓位和最低一手约束；减仓比例读取版本化政策；硬约束、软倍率、跳空风险和组合待分配均结构化记录。
- RK00-RK42 的 43 个唯一编号全部落为可执行测试；V2-6 专项 `49 passed`，V2 全量 `267 passed, 3 skipped`，项目全量 `527 passed, 3 skipped`。默认跳过的 3 条真实 Provider 冒烟测试使用 V1 本地配置显式启用后为 `3 passed in 24.63s`。

### 2026-07-15 V2-7 设计冻结：成交仿真层

- 新增规范 [docs/v2/V2_7_EXECUTION.md](./docs/v2/V2_7_EXECUTION.md)，冻结 OrderIntentRequest、OrderIntent、TriggerEvaluation、ExecutionRun、FillEvidence、ExecutionPolicy、migration 11 和 EX00-EX49。
- 当前订单预览与历史成交仿真必须从同一 `TradePlan + ExecutionDecision` 生成相同订单意图；C/D、零批准量和不适用动作保留结构化 `no_order` 记录，不静默删除。
- 固定无未来数据边界：盘后 T 日计划最早从 T+1 开盘求值；风控 `entry_price` 只是 sizing 参考价，不得转换为限价；历史事件必须在当时可见。
- 缺分钟顺序时不得用完整日K伪造盘中路径。同一 bar 触发与失效顺序不明时保持不可验证；已有持仓止盈/止损同日双触发可另给明确标记的保守 stop-first 下界，但不能冒充已验证成交。
- 费用、滑点、容量和现金缩量全部使用 Decimal；高波动、低流动性和缺 ADV 只能增加成本或降低证据等级，成交股数不能超过风控批准量、请求量、持仓量和可卖量。
- A股最终成交规则覆盖整手、零股全部退出、T+1、涨跌停和显式停牌；美股不得套用 A股规则。无 Level2/队列证据时不能保证涨跌停或延伸时段成交。
- 本阶段只设计单标的订单与成交证据。多股票现金争用、相关性、最终分配和替换排序仍属于 V2-8。

### 2026-07-15 V2-7 完成并复审：成交仿真层

- 同一 `TradePlan + ExecutionDecision` 生成唯一 OrderIntent，当前预览和历史回放只消费该意图；C/D、观察、零股和过期计划均保留 no_order 审计记录。
- 冻结静态条件、三值逻辑、crossing、跳空、失效、止损/止盈和同 bar 无序列场景统一由 TriggerEngine 求值；未来事件、未来流动性证据和跨股票/账户身份全部拒绝。
- Decimal 成本模型覆盖固定/波动率/ADV 滑点、佣金、最低佣金、A股卖出税、现金逐手缩量和 5% ADV 容量上限；买卖价格始终按不利方向量化。
- A股整手、零股全退、T+1、涨跌停队列与停牌，美股独立规则和无 Level2 降级均有测试；盘中实时价仍不写正式日K。
- migration 11 支持订单、构造记录、触发评估、run/fill 强类型恢复、幂等、冲突隔离及 run/fill 单事务写入。
- EX00-EX49 全部有可执行验收；成交专项 `125 passed`，V2 全量 `389 passed, 3 skipped`，项目全量 `649 passed, 3 skipped`，真实 Provider 冒烟显式启用后 `3 passed`。

### 2026-07-16 V2-8 设计冻结：组合决策层

- 新增规范 [docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md)，冻结 PortfolioInputBatch、PortfolioCandidate、HoldingRiskSnapshot、相关性/当前风险快照、组合分配结果、政策、migration 12 和 PO00-PO49。
- 固定执行顺序：V2-6 给出单计划最大批准量，V2-8 只做跨股票排序和缩量，V2-7 再从 final requested shares 构造订单意图；禁止维护组合专用第二套信号路径。
- 保护退出先于新增风险；预计卖出回款不能进入本轮可用现金。同股票多个退出计划共享持仓预留，不能重复卖出；替换只形成研究候选，不能自动串联卖出和买入。
- 组合容量同时受真实冻结现金、V2-6 单票/总仓位硬约束、conservative/aggressive heat、高相关邻域和市场整手规则限制；任何活跃持仓风险未知时阻断所有新增风险但保留退出。
- A股/CNY 和美股/USD 必须分批，禁止默认 FX=1、跨币种相关性或跨市场资金共用；相关性只使用 cutoff 前完成日K，样本不足保持缺失并降级，不能填 0。
- V2-8 已完成并复审；当前实现只授权 V2-9，不实现 LLM、UI/报告、券商自动下单或无 Level2 的成交保证。

## 16. 当前实施点：V2-8 已完成并复审

V2-8 已按 [docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md) 完成并复审：组合合同、冻结批次、组合风险快照、点时相关性、结构化排序、保护退出优先、双 profile 分配、共享退出、替换研究候选、V2-7 订单装配及 repository/migration 12 均已落地。复审修正了真实持仓 Decimal 精确比较、空/零权益/缺估值降级、相关邻域重复覆盖、退出回笼重复累计、替换候选资格、子记录原子回滚/强类型恢复和相关性查询性能。

PO00-PO49 已逐号映射为 50 个独立验收测试；V2-8 专项 `53 passed`，V2 全量 `442 passed, 3 skipped`（共 445 项），项目全量 `702 passed, 3 skipped`。默认关闭的 3 条真实 Provider 冒烟使用本地 V1 测试配置桥接显式启用后 `3 passed in 24.16s`。100 只股票、800 个真实上游 candidate 的双 profile 组合决策本机约 `0.05s`。V2-8 在 `b620bc7` 后形成完成基线；后续工作进入单独冻结的 V2-9 设计。

## 17. 当前实施点：V2-9 已完成并复审

V2-9 已按 [docs/v2/V2_9_LEARNING.md](./docs/v2/V2_9_LEARNING.md) 完成并复审。学习层将在线到期事实与历史重建 OOF 显式隔离，分别建立预测账、策略账和联合账，并对 Forecast、Scenario、Strategy、Risk、Execution、Portfolio 六层进行配对反事实归因。自动优化以股票为第一作用域，行业/市场只作样本不足的观察 fallback；候选只能调整预注册模型、特征子集、策略参数和软政策，不能改写源码或放宽硬约束。

落地内容包括不可变 learning contracts、目标会话 MaturityResolver 与无环 superseded revision 持久化、概率/策略/联合账和 PlanEvidenceSnapshot 投影、固定 V2-3→V2-8 purged walk-forward 主链与显式 ReplayAccountPolicy、候选/挑战者/影子/Champion/漂移/回滚生命周期、migration 13 及成熟度身份纠错 migration 14、强类型 repository 恢复，以及 LE00-LE59 的 60 个独立行为测试和额外真实链路回归。复审重点修复了动态波动率方向标签、同目标日不同预测来源冲突、OOF/在线样本混账、窗口退出成本被当作零、部分成交遗漏、联合账缺失、非法生命周期跳转和回滚未切换部署等问题；全链回放现在还会逐层验证四周期预测、情景、策略、风控、组合分配、成交事实和 outcome 的身份闭合，空壳阶段不能生成伪 OOF 证据。

最终验证：学习专项 `99 passed in 1.42s`；V2 全量 `541 passed, 3 skipped in 41.68s`；项目全量 `801 passed, 3 skipped in 74.16s`；默认关闭的 3 条真实 Provider 冒烟显式启用后 `3 passed in 30.33s`。3 个 skip 均为同一组显式联网测试，已单独执行通过。实现严格停止于 V2-9，未进入 V2-10 LLM 或 V2-11 UI/报告。

## 18. V2-10 完成并复审：LLM 假设层

V2-10 已按 [docs/v2/V2_10_LLM_HYPOTHESES.md](./docs/v2/V2_10_LLM_HYPOTHESES.md) 实现并复审。研究层以冻结 `ResearchFactManifest`、最小披露 canonical JSON Prompt 和注入式 client 协议接收模型输出；严格 parser 只允许五类假设和有限 predicate，拒绝 Markdown、未知字段、交易指令、价格/概率和跨 manifest 引用。

固定主链为：冻结上游事实 -> `ResearchFactManifest` -> 严格 JSON LLM 响应 -> 五类结构化假设 -> 确定性 DSL/注册表验证 -> V2-9 candidate bridge 与独立复盘。LLM 不接触账户金额、股数或 API Key，不生成当前交易指令；未知模型、特征、策略模板和算子只保存为 `implementation_required`，不能自动改写源码。migration 15 以同一事务保存 context、response revision、hypothesis、validation 和 candidate link，冲突隔离并支持强类型重启恢复。

已完成 LL00-LL49 一编号一行为测试、Tab1/Tab3 稳定分片、A股/美股事实隔离、响应 revision、prompt injection 防护、V2-5 同源三值验证、V2-9 候选/到期引用闭合和失败降级。首次复审补齐了分片输出标的白名单、全局事实去重、股票/行业/市场候选作用域绑定、同内容注册表校验、SHA-256 来源与 artifact 闭合、候选指标按 candidate 去重，以及 hypothesis -> candidate -> promotion 的数据库引用闭合。

二次深审进一步修复：直接投影 NewsSnapshot 的标题/规范摘要/情感/首次可见时间并限制每股 10 条；投影有来源 FundamentalSnapshot 字段；补齐 plan/condition/stop/take-profit/invalidation ID 和不含账户金额/股数的风控事实；异常响应继承请求 revision；候选 fallback business key 不再包含 response/hypothesis ID；显式拒绝取消止损/失效/有效期的覆盖；负数参数空间保持有序；instrument-less outcome 在合同边界拒绝；candidate outcome 限制为模型/策略/映射质疑；指标按声明 dimensions 实际筛选，无法证明成员归属时拒绝；client 成功缓存绑定 prompt hash 和 model。

最终验证结果：研究专项 `86 passed`；V2 全量 `624 passed, 4 skipped`；项目全量 `884 passed, 4 skipped`。4 条默认关闭的真实网络 smoke 使用本机 V1 配置桥接显式执行，结果 `4 passed in 58.03s`，覆盖 3 条真实数据 Provider 和 1 条真实 LLM 严格 JSON/脱敏链路。实现仍严格停止于 V2-10；V2-11 仅完成精确设计，尚未实现。

## 19. V2-11 完成并复审：报告与 UI 层

V2-11 已按 [docs/v2/V2_11_REPORT_UI.md](./docs/v2/V2_11_REPORT_UI.md) 完成并复审。展示主链固定为“冻结上游 artifact -> PresentationInput -> deterministic ReportDocument -> Flet/Markdown/HTML/PDF”，禁止 LLM 生成整篇报告、禁止 UI 访问 Provider、禁止 renderer 查询数据库补字段或重算预测/策略/风控。

设计冻结了单股与组合展示输入的身份闭合、天气预报式当前/历史预测表、预测账/策略账/联合账/LLM 独立账、Tab1/Tab3 全宽交互、持仓行内编辑和不可变账户快照、关注列表快照、逐股进度与取消、历史报告/评分/比较、设置能力矩阵、HTML/PDF 一致渲染、migration 16 和 UX00-UX59 一编号一行为验收。

阶段专项 `90 passed`，V2 全量 `714 passed, 4 skipped`，项目全量 `974 passed, 4 skipped`。默认跳过项为 3 条真实数据 Provider 和 1 条真实 LLM 集成测试；它们均已用本机 V1 配置桥接另行显式启用：Provider 为 `3 passed in 61.70s`，真实 LLM 严格 JSON、提示词脱敏和响应身份链为 `1 passed in 10.37s`。固定 A股/美股、盘前/盘中/盘后 Flet 构造烟雾通过；确定性 HTML 在 1280×800、900×700、390×844 三种真实浏览器视口完成视觉检查，正文无溢出或遮挡，移动端宽表限制在自身横向滚动容器内。V1 正式数据迁移、完整真实链端到端接线、macOS/Windows 发布和 Web 部署仍属于 V2-12。

## 20. V2-12 设计冻结：迁移、端到端与发布

V2-12 是 TradeHelper 2.0 的最后一个既定开发阶段，精确合同见 [docs/v2/V2_12_MIGRATION_RELEASE.md](./docs/v2/V2_12_MIGRATION_RELEASE.md)。本阶段不重写 V2-1 至 V2-11 的算法，而是建立唯一 production composition root，把数据、特征、预测、情景、策略、风控、成交、组合、学习、LLM 和展示层接成真正由桌面入口运行的主链。

实施拆为八批：运行时合同与 migration 17；V1 可信迁移与旧证据隔离；双市场 lookup、FinBERT 和 production container；Tab1 全链；Tab3 全链；LLM/学习后台与失败恢复；V1 运行面退出和跨平台打包；最终 12 格、联网、性能、视觉与安装包验收。V1 设置、真实账户、持仓、关注列表和旧报告可受控迁移；旧行情、预测、策略、回测参数和学习结果只能归档或重新获取，不能污染 V2 正式事实与三本账。

RL00-RL79 已按一编号一行为落地，阶段验收 `91 passed`；V2 与项目全量均为 `819 passed, 4 skipped`。默认跳过的 3 条真实 Provider 测试和 1 条真实 LLM 测试已分别显式开启并全部通过。本机真实 V1 数据库迁移 19,112 项，重复执行计划一致，源库 SHA-256 与 mtime 未改变；macOS 严格包内 runtime smoke 已通过。Flet 内嵌 framework 的 PyInstaller 临时签名仍有警告，因此 Developer ID 签名和公证不计为已完成。Windows 本地 bat 与 GitHub Actions 使用同一 spec/smoke，但实际 Windows 产物仍须由 Windows runner 验收。

## 21. V2-12 代码完成并本机复审：TradeHelper 2.0

V2-12 的生产实现已按冻结规范完成：`main.py -> RuntimeContainer -> V2 application ports -> PresentationInput -> ReportDocument -> Flet/Markdown/HTML/PDF` 已形成唯一生产路径。V1 数据只读预检、SHA-256/mtime 复核、备份、事务回滚、quarantine 和 migration 17 审计表均已实现；旧行情、预测、策略、回测参数和 LLM 只留 archive，不进入 V2 正式事实或三本账。V1 源码已从当前工作树退出，并由 `v1.0-final-before-v2` 标签保留。版本固定为 2.0.0；只有 Windows 真实产物验收尚需对应平台执行，后续自动下单、Web 发布和 V2.1 必须另立设计。

## 22. 2026-07-17 桌面启动兼容修复与 UI 复核

- 发现 migration 17 的关注列表迁移把数据库迁移号误写进 `WatchlistSnapshot.schema_version`，导致首次迁移成功、第二次启动强类型恢复时报 `stored watchlist columns do not match payload`。新增不可变 migration 18，仅把受影响的 `schema_version=17` 关注列表行修正为对象合同版本 1；迁移执行器的新写入也固定使用对象合同版本。既有 migration 17 不回写、不改 checksum。
- `main.py` 从已弃用的 `ft.app(target=...)` 切换为 `ft.run(...)`；现有用户数据库已实测自动升级到 schema 18，账户、持仓和关注列表无需清空。
- Tab1、Tab3、历史报告和设置页统一为浅色全宽交易工作台：顶部品牌/当前页面栏、底部图标导航、紧凑条件区、持仓行内编辑、全宽报告和一致的报告层级。已在 1280x800 与 390x844 视口实际渲染检查，未发现不可恢复的溢出或导航遮挡。
- 新增 V2 独立滚动日志 `logs/tradehelper_v2.log`：5MB 单文件、4 份备份、终端同步输出、用户私有权限和全消息/异常栈凭据脱敏。启动、schema/迁移状态、分析任务、数据刷新摘要、后台研究与学习结果均进入日志；V1 的旧 `tradehelper.log` 不再作为 V2 排障依据。
- 修复 Tab3 将“分析市场”隐式兼作账户切换且空账户重绘回美股的问题。账户区新增明确的“美股账户/A股账户”切换，独立状态同步加载对应币种现金、持仓和关注列表；未创建的市场保持选中并可直接创建真实账户，不会覆盖另一市场快照。
- 修复真实组合刷新中的时点误判：`requested_at` 继续冻结 Provider 请求边界，新闻/基本面首次可见时间和报价观察时间共同形成全组合统一 `analysis_cutoff`；数据质量、特征、预测、情景、策略、风控、组合分配和报告共享该截止时间。展示合同仍严格拒绝真正晚于截止时间的未来事实。逐股刷新/构建异常不再静默聚合，日志会记录股票和完整堆栈。
- 本轮专项回归 `30 passed`，日志专项 `2 passed`；项目全量 `827 passed, 4 skipped`。4 个 skip 仍为默认关闭的真实 Provider/LLM 联网测试，V2-12 复审阶段已经显式启用通过。

## 23. 2026-07-20 桌面交互、报告与预测主链补强

- 修复 Flet 不支持的 `Wrap` 控件、报告评分输入框无限扩展遮挡正文、临时 Web 重连错误关闭数据库等真实运行故障；分析、取消、保存、删除和导出均提供即时忙碌状态及成功/失败反馈。
- 桌面导航改为稳定左侧工作栏；Tab3 的账户、关注列表和历史评估改为单层分段工作区，持仓与报告不再被嵌套页签裁切。报告使用冻结 `ReportDocument` 的原生结构化控件渲染，宽表在自身区域横向滚动，页面只保留一个纵向滚动容器。
- 新增 `scripts/run_web_preview.sh`，使用 Flet 强制 Web Server 模式但不自动唤起系统浏览器，统一给出 `http://localhost:8765`。开发视觉检查不再使用会触发 Safari“仅限 HTTPS”失败页的 `flet run -w`。
- 单股和组合默认历史窗口从 3 个月调整为 1 年。3 个月数据不可能同时满足股票级最低训练样本与至少 60 个 OOF 点，短窗口现在明确标记为仅适合快速观察。
- 修复日 K 增量缓存只检查末端、不检查起点的问题：较短分析留下的缓存不能再错误满足更长窗口请求。真实 AAPL 一年请求从错误命中的 96 根补齐为 370 根，来源仍遵守美股 Nasdaq -> yfinance -> TickFlow 路由。
- 正式预测主链现在会从已完成日 K 构造有上限的 point-in-time 技术训练样本；首次无 Champion 时输出经验基线概率和 P10/P50/P90，并明确不参与新开仓强分级。深度候选 OOF 在后台单线程执行，只有 confirmation 通过才原子晋升并从下一次分析生效。
- 本机真实 AAPL 盘后分析约 `12.6s` 完成，四个预测周期均有概率和收益区间；随后后台 OOF 正常完成，本次候选因概率校准不合格未晋升，系统继续保留基线而不伪造正式模型。最终全量回归 `834 passed, 4 skipped`。

## 24. 2026-07-20 Tab3 真实报告与导出体验复核

- 使用用户当前美股账户、4 只持仓和 7 只关注股完成真实盘后组合分析，针对最终导出的 `portfolio_US_portfolio_eod_20260720T055028Z_af9ac4ea3e.html` 逐表复核，而不是只检查测试夹具。
- 修复利润锁定模板在盈利高点尚未达到门槛时仍生成计划的问题；旧逻辑可能产生“价格同时高于激活线且低于锁利线”的永不触发区间。现在只有高水位已满足盈利门槛才生成锁利计划，触发条件只表达从高点回撤。
- 持仓动作排序改为已触发计划优先；同为已触发时，全卖优先于减仓，保护退出优先于普通退出。报告当前动作与组合分配使用相同的 readiness/action 顺序，避免已触发锁利被尚未触发的保护计划遮盖。
- 通用保护性全卖恢复为成本硬止损；仅在成本未知时才以 MA60 作为保守兜底。MA60 趋势破坏继续由反抽失败退出处理，已有浮盈则由锁利策略减仓，避免所有跌破 MA60 的持仓被机械清仓。真实报告中 FCX 因锁利条件触发改为保守减 27 股/激进减 13 股，LITE、SPCX、WDC 的成本硬止损仍保留全卖保护。
- Tab3 首页每只股票、每种风险方案只展示一个首要动作；共享持仓的备选退出条件保留在详细计划中。退出不再显示 `$0.00` 回笼或“最大亏损缺失”，而是明确“不占用现金、退出不新增计划亏损、卖出回款本轮不复用”。
- 账户现金、本轮可新增、买入预留和预留后可新增已分列；`remaining_cash` 不再被误写为账户余额。盘后缺少 QuoteSnapshot 改为“使用已完成日K（无需实时快照）”，OOF、方向、质量、证据和组合原因码均使用中文解释。
- 历史能力评估不再逐行输出 `ForecastOutcome` 等内部对象名；按股票和预测/策略/联合账聚合记录数量、成熟状态、方向正确率或收益结果。真实 11 股报告由原先约 96 行不可解释证据缩为 11 行预测账汇总。
- HTML/Markdown/PDF 导出在点击后立即显示“正在导出”，完成或失败提示在报告结果页持续可见；成功后 macOS Finder、Windows 资源管理器或 Linux 文件管理器自动定位导出文件。
- 最终 V2 全量回归 `843 passed, 4 skipped`。4 项为默认关闭的真实 Provider/LLM 联网测试；本轮另以生产容器多次真实运行 11 股组合主链并成功导出报告。

## 25. 2026-07-20 Tab3 可用性与研究员链路复核

- 报告时间不再暴露 UTC ISO 字符串。组合摘要使用北京时间，HTML/Markdown/PDF 页眉按报告市场显示明确的美东时间或北京时间；有效期继续按对应交易市场时区显示。
- Tab3 新增“逐股价格与关键事实”，固定展示公司、代码、持仓/关注身份、最新完成 K 线日期、实际分析价、行情来源、成本盈亏、组合仓位和 MA20/60/120 相对位置。盘后价格统一读取预测发行时冻结的日 K 参考价，真实 11 股报告已显示 Nasdaq/yfinance 来源，不再因没有盘后 QuoteSnapshot 而误报“暂无可靠数据”。
- “下一交易时段优先处理”改为分析价、持仓上下文、动作、数量、策略家族、触发条件和执行安排；组合 heat 已越过保护线时直接显示“先退出/减仓”，不再误写成行情或历史样本不足。
- 条件计划压缩为每股一行；未持有股票明确写“无需卖出/无需展示持有条件”，不再机械输出四行“不适用”。预测 OOF 与行情完整度分开表达：完整 K 线只能证明可训练，模型仍须在未见数据上通过概率校准和确认窗口；未通过时保留经验基线观察，不能参与新开仓执行分级，保护退出不受影响。
- 修复组合分配器把持仓股票的观察级入口同时放入持仓队列和入口队列造成重复引用的问题。持仓优先队列现在只接收 SELL/REDUCE；观察级 BUY/ADD 仍展示具体触发条件，但只进入一次且不生成订单。
- Tab3 外部研究员从“仅 Tab1 可调”改为组合稳定分片调用。11 股按 10+1 分片；报告记录计划/实际分片数、成功数、失败原因和进入验证的观察数。提示合同增加系统质疑可引用对象目录，plan/decision 来源引用必须使用 artifact 类型，仍保持严格拒绝错误引用而不猜测修复。
- 真实生产复核报告为 `portfolio_US_portfolio_eod_20260720T072642Z_a0d4d3a458.html`：4 持仓 + 7 关注股全部显示价格和来源；优先动作显示 SPCX/WDC/LITE 成本保护卖出与 FCX 锁利减仓；组合风险明确为保护线已越过；研究员实际调用 2 个分片，1 个被长度限制截断，1 个成功并形成 1 条 SNDK 风控过严质疑，系统判为“待验证、仅观察”。
- 验证结果：V2 全量 `846 passed, 4 skipped`；默认关闭的 3 条真实 Provider 与 1 条真实 LLM 冒烟显式启用后为 `4 passed in 30.45s`。真实 Tab3 主链和增强报告均成功生成；免费/兼容 LLM 单次输出长度仍可能使大分片截断，报告已显式降级，不影响确定性主报告。

## 26. 当前 P0：预测可信度与策略历史回放

当前统一产品合同见 [docs/v2/TRUSTED_DECISION_CHAIN.md](./docs/v2/TRUSTED_DECISION_CHAIN.md)。后续预测增强、策略 OOF 接线和 Tab1/Tab3 报告重排必须共同遵守该合同，不能分别优化后再次形成断链。

### 已完成

- 股票级预测训练窗口扩展到约 5 年，并将最多 720 个历史 origin 用于后台 OOF；用户选择的报告展示窗口不再限制模型训练窗口。
- 固定候选池新增紧凑趋势/均值回归特征组、有限训练窗口和独立区间校准；仍保持预注册、有限搜索空间和 selection/confirmation 隔离。
- 预测确认分为两档：严格优于经验基线的正式通过，以及校准合格且不显著劣于基线的 `noninferior` 通过。后者只能作为 B 级小仓证据，不能宣称已发现 Alpha，也不能升级为 A 级新开仓。
- 后台将每个预注册候选在 selection/confirmation 段的 Brier、Log Loss、ECE、区间覆盖、正确率及对应基线保存到独立 `forecast_candidate_evaluations`；候选尚未形成可部署 artifact 时不虚构模型版本，也不违反正式模型表的外键。
- schema 升至 migration 19，使用独立 `forecast_validation_summaries` 保存每股/周期最新 OOF 结论；应用重启后恢复真实状态和原因，不把“校准失败/未优于基线”重置成“未评估”。
- 策略晋级继续使用两条并列通道：多数 OOF 分段取得绝对超额，或在正收益基准下保留至少 80% 的基准收益、最大回撤改善至少 30%、Sharpe 改善至少 0.20。牛市中不要求策略机械跑赢买入持有。
- 报告历史能力说明已明确同时展示系统收益、买入持有基准、Alpha、收益保留率、最大回撤和 Sharpe；禁止仅凭 Alpha 正负判断策略优劣。
- 修复预测候选选优目标错误：`ModelSpec.primary_metric` 已冻结为多分类概率误差，生产训练现在先按该主指标排序，再使用 Log Loss、校准误差和顺序分段稳定性作次级判断；不再让稳定性代理指标覆盖主目标。
- 状态近邻候选增加分层回退：同状态成熟样本达到 30 条时使用同状态近邻，否则回退到该股票的全历史近邻。候选必须覆盖与基线完全相同的 OOF 事件，不能再因稀有状态漏预测而必然淘汰。
- 生产分析已把同股票、同策略、同参数、同保守/激进档案的成熟策略结果投影为 `PlanEvidenceSnapshot`，持久化后传入风险官；可靠正期望、负期望、样本不足和冲突现在能真实影响 A/B/C/D，而不是固定传空历史证据。
- Tab3 组合候选不再把 `plan_evidence` 固定为 `None`；风险官使用的同一证据快照现在继续进入跨股票排序、资金分配和替换候选判断，避免单股已经识别正/负期望、组合层却再次按“无证据”排序。
- Tab1/Tab3 报告已按“基本信息与价格来源 -> 未来预测 -> 策略及历史收益/回撤 -> 保守/激进计划 -> LLM 观察 -> 最终结论 -> 历史可信度”重排。主视图改用中文直白字段，完整条件和技术指标保留为审计明细。
- 生产后台已接入股票级三折历史重放：每折保留 10 个交易日隔离带，只用训练截止日前已经到期的标签重新拟合预测模型，再复用正式 `ForecastResult -> TradingScenario -> TradePlan -> ExecutionDecision` 主链。
- 历史重放继续经过 V2-8 组合分配、V2-7 订单工厂和日 K 成交仿真；保守/激进方案分别计入费用、滑点、市场规则、成交拒绝、组合收益、买入持有基准和逐日回撤，并保存为 `RECONSTRUCTED_OOF` 联合账。
- 离线可比实验使用明确隔离的十万元标准研究账户，同时覆盖空仓候选和已有持仓两种状态；该金额不进入线上账户、风控仓位或用户报告的当前股数计算。
- 报告不再读取同市场全部原始联合事件并归给当前股票。后台按股票和保守/激进方案保存不可变汇总；报告只展示历史决策数、平均收益、胜率、同期买入持有、相对收益、最差回撤和风险调整表现，原始事件留在历史评估审计库。
- 单次策略结果的 5 日窗口可能重叠，报告只显示“每次信号平均收益/胜率/最差不利波动”，不再把重叠事件复合成伪累计收益或伪最大回撤；完整账户表现只读取联合回放。

### 真实验证结论

- 当前 18 个月本地样本的 10 只美股、4 个周期共 40 个股票级预测中，7 个周期通过新门槛，另有数据库中既有的 LITE 5 日 Champion；多数股票仍未形成可执行预测证据。
- 修复本轮选模目标前，AAPL 即使补取约 5 年、1,390 根 Nasdaq 日 K，四个周期仍全部未通过质量门槛；这次异常促使系统继续检查算法，而不是把失败简单归因于 K 线数量或降低标准。
- 2026-07-20 修复主指标排序并补齐状态近邻回退后，使用同一份 AAPL 1,390 根真实日 K、最近 720 个历史起点和未改变的 OOF 门槛重跑：3日与5日候选达到“校准合格且不劣于经验基线”的 B 级标准，1日与10日仍因确认/校准失败被拒绝。另行实测的“近似最优后优先稳定性”规则仅有1个周期通过，未保留。该变化不是降低质量门槛，正式报告也不得把 B 级说成发现了超额收益。
- 2026-07-20 使用同一份 AAPL 真实数据执行生产历史全链：1,390 根日 K、2,865 个四周期训练样本、3 个顺序测试折和 120 个历史决策时点，共形成 612 条策略结果、70 条实际触发成交结果及 480 条保守/激进完整链路结果。两档各 240 条，全部带逐日净值路径；该次真实运行未写入用户生产数据库。

### 尚未完成，必须继续作为 P0

- 当前已完成“单股票 + 标准研究账户”的生产历史联合重放，但尚未完成真实多股票同时竞争现金、相关性和持仓风险的连续组合 OOF 净值。当前结果适合判断股票绑定主链是否有效，不能冒充用户真实组合历史业绩。
- 策略/联合结果已经进入下一次分析的风控、排序和报告，但“按股票自动生成参数 challenger -> bootstrap 置信区间 -> shadow -> Champion/回滚”的生产调度仍需接线；完成前不能声称策略参数会自动越跑越好。
- 联合汇总已保存收益、基准、回撤和风险调整表现；成交数及 bootstrap 区间还需进入股票/市场状态/方案分层快照，再由绝对超额或风险调整双通道决定候选晋级。
- 预测模型下一轮应补充具有点时可见性的市场/行业上下文：美股市场及行业基准、A股宽基及行业基准必须对称支持；历史新闻和基本面只有在系统从当时起持续保存后才能进入未来训练，禁止用当前快照回填过去造成穿越。
- 完成标准不是“所有股票都有 Champion”，而是每只股票始终有明确的经验基线结果，同时正式模型只有在 OOF 证据成立时参与执行；无可预测性的股票必须诚实降级，不能靠增加模型数量或放宽质量门槛制造可信度。
- 后台重放在首次分析后异步运行，当前报告不会等待；同一股票下一次分析才读取成熟策略账和联合汇总。首次尚未完成时必须显示“历史回放尚未完成”，不得用当前规则说明冒充历史盈利证据。

## 27. 2026-07-23 三时段排障、日志与后台稳定性修复

- 根据生产日志定位并修复后台学习的成熟证据 revision 分叉：同一任务内共享起止交易日的多条预测现在依次继承刚保存的 active revision；已到期事实发生纠正时允许生成后继 revision，完全相同的成熟证据才跳过。
- 修复组合风险快照的 `Decimal` 权重尾差。各持仓权重独立除法可能使权重和与总投资比例相差一个最小精度单位；现在以总投资比例为事实源，将最大权重计算为其余权重的精确补数，避免真实 Tab3 因 `portfolio weights invalid` 中止。
- 分析日志已覆盖任务阶段耗时、逐股进度、元数据/上市日期、日 K 数量和日期范围、实时价格及观察时间、新闻数量、基本面字段数、各 Provider 尝试与失败原因、缓存回退、质量分数、全部质量问题、四周期预测、情景、策略计划、风控决策和订单预览。错误日志固定记录市场、模式、阶段、异常类型、消息和完整堆栈。
- 日志格式增加源文件和行号，支持通过 `TRADEHELPER_LOG_LEVEL=DEBUG` 临时开启诊断级别；继续执行凭据脱敏、5MB 轮转和私有文件权限。yfinance 在 fallback 空窗口上输出的误导性 `possibly delisted` 内部日志被抑制，TradeHelper 自己记录完整 Provider 尝试链。
- TickFlow 单股报价适配器不再经由丢失诊断元数据的批量结果二次包装，重试和失败原因可以进入分析日志。
- 真实美股盘前冒烟中，AAPL 由 Nasdaq 返回当前盘前时间的有效快照；在非正常交易时段强制调用盘中模式时，TickFlow 返回前一交易日时间戳，系统按过期行情阻断。生产日志中 MU 在正常交易时段也曾收到 TickFlow 的前一交易日时间戳，该次降级是可信风控行为，不能用旧价格伪装盘中实时行情。
- 历史日志中没有可对应的 V2 盘前失败记录，因此不虚构根因；A股/美股 × 盘前/盘中/盘后端到端模式矩阵均已回归通过。最终项目全量为 `865 passed, 4 skipped`；4 项仍是默认关闭的真实 Provider/LLM 联网冒烟，不是三时段功能测试。

## 28. 2026-07-28 正式目录结构收口

- V1 源码已经由 Git 标签归档并退出工作树，`tradehelper_v2/` 重构隔离目录完成使命；正式代码取消总包外壳。
- `application/`、`contracts/`、`data/`、`forecast/`、`strategies/`、`risk/`、`portfolio/`、`learning/`、`research/`、`presentation/`、`ui/` 等职责层直接成为项目根目录一级 Python 包。
- 入口、内部 import、测试、PyInstaller hidden imports、发布清单脚本、架构边界检查和现行设计文档统一使用根级模块；工作树不得重新出现 `tradehelper_v2/`、`tradehelper/` 兼容总包或双路径 import。
- 本次只迁移源码身份，不迁移用户数据身份。现有 `{work_dir}/tradehelper_v2.db`、`config_v2.json` 和 `logs/tradehelper_v2.log` 继续沿用，避免账户、持仓、学习证据、设置和排障历史被错误识别为新数据。
- 根级结构改变了 `__file__` 相对深度，FinBERT 和发布 manifest 的资源根定位已同步修正；PyInstaller 不再错误排除正式的 `config/`、`data/`、`strategies/`、`ui/`。
- 架构测试改为 AST 检查真实模块依赖，允许包内相对引用，不再用 `risk` 等普通变量名做字符串误判。最终全量 `865 passed, 4 skipped`；离线发布烟雾通过 schema 19、FinBERT 真实推理和 HTML/Markdown/PDF 渲染。

## 29. 2026-07-31 Tab3 报告、历史评估与研究员可用性优化

- Tab3 报告恢复 V1 已验证的“先结论、后证据”交易员阅读层级：第一屏固定为“一分钟操作台”，随后按卖出/减仓、买入/加仓、持有/观察分组；再依次核对价格事实、未来预测、策略历史和完整条件。每只持仓或关注股的预测、策略、买卖条件、保守/激进方案和历史可信度收进可展开的“逐股详细解读”，避免 11 只股票的长说明同时铺满页面。
- HTML 增加章节目录和编号、放大章节间距、统一蓝色层级、表格隔行底色和横向滚动边界；Flet 报告取消重复外层面板，结果区使用完整可用宽度，逐股解释在 HTML/Flet 中都默认折叠、按需展开。
- 报告不再把结构化交易计划重新拼成一行长句。Tab3 首屏最多展示 5 个动作卡片，触发与确认条件拆成编号步骤，保守/激进按最终组合批准股数并排；完整清单继续按动作分组。Tab1 的四周期预测和两种操作方案同步改为卡片，预测使用上涨/震荡/下跌概率条，退出、止盈、最大亏损和有效期分区展示。
- HTML/Flet 使用相同的信息层级；PDF 将动作、预测和方案压缩为两列分行结构，Markdown 对长条件强制换行。宽表只保留适合横向比较的事实和历史指标，长解释使用项目符号或折叠明细，不用装饰性图表制造信息量。
- 修复 Flet 长条件表格仍受 `112px` 固定行高限制导致文字与下一行重叠的问题。行高现在按每张表的项目数和估算换行数动态计算，短表保持紧凑，多行说明可完整撑开；回归测试显式防止恢复固定上限。
- 按动作分组的完整清单不再使用六列宽表，也不再把长句按分号机械切碎。Flet、HTML 和 PDF 统一改为逐股交易候选卡：股票/身份/分析价/动作在同一标题栏，正文按“系统判断 -> 触发/确认/执行/期限 -> 保守/激进最终股数”排列；原始比较符和内部特征名转换为交易者可读表达，未获批数量明确写“暂不下单”。
- 组合预测由“每股四周期四行”压缩为“每股一行、1/3/5/10 日并列”，历史可信度拆成预测表现、策略表现和最终联合表现三个摘要表；原始逐事件证据留在历史评估页，不再塞入主报告。
- 历史评估页自动加载并明确拆成“预测表现 / 策略表现”两个视图。预测侧展示概率误差、校准、区间命中和逐事件结果；策略侧展示策略收益、同期基准、相对收益、完整链路累计表现和回撤。明细限制为最近 200 条，避免数千条候选回放拖垮 UI。
- 修复真实 SQLite 历史评估查询漏传参数导致页面不可用的问题；学习快照按当前市场和股票范围过滤，不再混入其他市场。完整链路图只使用 `JointOutcome`，同一历史窗口的候选先聚合，再连接互不重叠窗口，禁止与策略信号重复复利。
- DeepSeek V4 显式遵守设置页“允许模型思考模式”开关：关闭时发送 `thinking=disabled` 并使用 4,000 输出预算；开启时发送 `thinking=enabled`、移除无效 temperature，并使用 12,000 输出预算。隐藏推理不持久化，最终 JSON 继续经过严格 parser、确定性 validator 和候选桥接。
- 组合研究从每分片 10 股降为 5 股，事实输入从无差别截取改为预测、情景、策略、风控、技术、新闻、基本面的分类配额；真实 5 股分片由约 46 万字符降到约 16 万字符。每只股票拥有独立证据与挑战对象白名单，禁止跨股票引用、伪造 bundle ID 和把文本事实用于数值比较。
- 修复真实 LLM 完整返回仍被机械合同拒绝的问题：predicate 中已经明确引用的 `fact_ref` 自动进入有效证据集合，不再要求模型在 `evidence_refs` 重复抄写；系统质疑只能引用冻结 manifest 中可见且同股票的 artifact，旧提示造成的 plan/strategy 类型标签偏差在 ID 和归属均可证明时规范化为通用 artifact。仍然拒绝未知 ID、跨股票引用、编造事实和无法证明归属的对象。
- 研究日志新增每个 LLM 分片的股票、模型、输出预算、完成状态、结束原因、响应字符数、token 用量、有效假设数和耗时。研究解析或验证失败继续只降级该分片，不阻断确定性预测、策略、风控和主报告。
- 真实 DeepSeek 对照验证完成：关闭思考模式的 11 股三分片均返回完整响应，其中有效分片产生已确认、待验证和工程建议；开启思考模式的真实 5 股分片在 12,000 预算内 `finish=stop`，5 条观察全部通过严格解析。最新生产失败的三个原始分片离线重放由 `1/3` 恢复为 `3/3`；修正后的真实联网严格 JSON/引用闭环再次通过 `1 passed in 24.53s`。最新项目全量回归为 `882 passed, 4 skipped`；4 项仍为默认关闭的联网冒烟，不是功能跳过。

## 30. 2026-08-03 七章节可信报告与 LLM 编辑员

- Tab1 与 Tab3 的主报告统一冻结为七个递进章节：`操作总结 -> 基本信息与数据核对 -> 各股票未来走势预测 -> 详细操作报告 -> 策略表现与回测依据 -> 研究员补充观察 -> 系统历史可信度`。用户先核对事实，再看预测、操作、历史依据和独立复盘，不再在多个重复章节间来回寻找结论。
- 第一屏只展示按卖出/减仓、买入/加仓、持有/观察分组的组合摘要，以及最多五张重点操作卡。每张卡只保留身份、分析价、冻结动作、一句话判断和下一条件；完整触发步骤、风险方案和逐股审计继续保留在后续章节，避免首屏再次变成长表。
- 颜色继续承担快速识别职责：买入/加仓、卖出/减仓、持有/观察使用稳定的动作色；预测继续使用上涨、震荡、下跌概率条。图表只表达真实概率和历史指标，不为装饰制造没有数据依据的曲线。
- 新增可选的 `LLM 报告编辑员`。它只接收系统已经冻结的股票、动作、判断、下一条件和保守/激进结果，输出一句话结论、最多三条理由和风险提醒；严格合同禁止修改股票或动作、禁止新增任何数字、禁止承诺收益。解析、引用或联网失败时自动回退到确定性中文解释，不影响预测、策略、风控和报告生成。
- `LLM 研究员` 与 `LLM 报告编辑员` 职责分离：研究员提出可验证的新观察，编辑员只负责把既有结论讲清楚；二者都不能生成或覆盖可执行 TradePlan。报告修订不变量继续校验所有确定性事实表，只有编辑说明卡允许变化。
- HTML、Flet 与 PDF 对新结构使用相同的信息层级：操作总结和详细动作使用彩色卡片，长条件使用分段列表或折叠明细。已对六股票组合在桌面宽度进行实际 HTML 截图复核，首屏无重叠、无横向挤压，操作重点可一眼识别。
- 最终项目全量回归为 `891 passed, 4 skipped`；4 项仍为默认关闭、需要显式联网密钥与开关的真实 Provider/LLM 冒烟。报告编辑员新增了单股/组合严格解析、动作不可变、禁止编造数字、思考模式开关、全组合覆盖和失败回退测试。
- 修复真实组合报告在批准股数为 `Decimal("55.0")` 时于报告阶段执行 `int("55.0")` 导致整轮分析失败的问题。报告现在直接消费结构化 `PortfolioAllocation`，不再从展示单元格反解析股数；整数股统一显示为 `55 股`，且保守/激进摘要共用同一精确格式化规则。
- 修复 Tab1/Tab3 分析进度只在窗口失焦后重绘的问题。后台分析回调不再直接修改 Flet 控件，而是通过当前 `Page.run_task()` 投递到页面事件循环；进度、完成、失败和研究员报告修订均沿同一路径主动刷新，窗口持续停留在当前页面时也会逐阶段更新。
- Tab3 的“逐股详细解读”不再使用统一最大行高的 `DataTable`。展开内容按“股票概况、预测如何转成方案、达到什么条件行动、风险与历史依据”四段自然排版；桌面端短信息双列、交易条件单列，移动端自动单列，买入/卖出/持有继续使用绿/红/橙色提示，从根源消除短行被最长条件撑出大片空白的问题。

## 31. 2026-08-04 独立能力评估与报告记录收口

- 左侧导航新增独立“能力评估”，并放在“报告记录”之前；Tab3 只保留账户持仓和关注列表，不再重复承载历史评估。原“历史报告”改名为“报告记录”，常用检索条件置于首层，评分、归档和比较收进“更多条件”。
- 能力评估固定为五个视图：`能力总览 / 单股预测 / 单股策略 / 组合预测 / 组合策略`。预测与策略、单股与组合分别评价，不能用其中一种能力替代另一种；总览先给四项能力的样本、核心结果和当前结论，再下钻明细。
- 单股查询支持直接输入 `MU`、`600519`、公司中文名或英文名；A股和美股共用同一查询合同和真实 SQLite 读路径。单股预测账同时接收 Tab1 与 Tab3 中该股票的预测，因此用户只运行 Tab3 时，组合内股票仍会在单股预测页面形成记录。
- 盘前、盘中、盘后三种分析模式独立验证，任何模式不得覆盖其他模式。同一股票、同一模式、同一目标交易日和同一周期存在多次分析时，只评估最后一次实际发行预测；“最后一次”按报告实际保存时间判断，不能使用可能相同的数据截止时间代替。
- 单股预测页合并 Tab1/Tab3 后在同一模式内取最新版本；组合预测页只统计 Tab3 实际发行记录，并在 Tab3 自身范围内取最新版本。预测明细固定按生成时间倒序，直接展示生成时间、模式、来源、目标交易日、最终预测、实际结果和对错。
- 策略表现分开显示“连续历史 OOF 回测”和“实际建议回放”。连续 OOF 用于衡量完整历史能力，不要求用户每天运行；实际建议回放只连接用户真正生成且到期的最后有效建议，用户未运行期间不得补造新建议或伪造连续实盘收益。
- 单股策略展示已复盘成交、胜率、净收益、同期买入持有、相对收益和不利波动；组合策略展示独立决策窗口、系统收益、基准收益、超额、最大回撤和 Sharpe。累计收益与回撤图只连接互不重叠的历史决策窗口，并按连续 OOF、实际建议和影子观察分系列。
- 修复冻结报告到预测事实的真实数据库关联：预测 `event_key` 含特殊分隔符时，报告来源引用会按展示合同哈希，评估服务必须用同一 `presentation_source_refs` 算法建立映射，禁止把哈希引用直接当数据库主键。美股与 A 股的报告、预测、到期结果串联测试均已覆盖。
- 历史指标快照只有在股票和周期维度明确匹配时才进入筛选结果；选择分析模式或报告来源后，缺少对应维度的旧快照必须排除，不能混入并冒充该模式能力。
- 能力评估不在程序启动时预加载。用户首次进入页面后，系统在后台读取冻结历史并显示明确进度；同一市场的原始历史投影在会话内复用，五个视图切换不重复反序列化全部账本，点击“查询”会主动失效缓存并读取最新结果。使用当前真实美股数据库只读验证，首次总览约 `8.6s`，随后预测/单股策略/组合策略视图约 `0.04s / 0.60s / 0.50s`。
- 所有预测生成时间使用明确的市场时区展示：美股显示美东时间，A股显示北京时间；日期筛选使用相同市场时区解释，避免 UTC 日期边界导致用户看到的日期与筛选口径不一致。
- 报告记录使用职责明确的两行筛选：第一行是市场、报告类型、股票和模式，第二行是日期范围、口径说明和筛选命令；较少使用的周期、评分、归档和比较保留在“更多条件”中。两个起止日期合并为一个原生日历范围控件，选中后使用紧凑日期文本，右侧图标负责清除。第一行按 `2/2/5/3` 的 12 栏比例响应式分配，空间不足时完整控件成组换行，禁止下拉文字被箭头裁切。报告结果增加列标题，“更多条件”横向拉伸并压缩无效留白。
- 能力评估筛选栏最终采用有明确职责的两行结构：第一行只放市场、模式、股票、周期和建议来源，第二行固定为日期范围、筛选口径说明和查询命令。两行由分隔线组织，不再为了强塞一行而缩窄控件，也不会由 Flet 随机换行。
- 修复“组合预测有明细但预测表现汇总为空”的来源投影错误。带 `ReportKind.PORTFOLIO` 的查询现在统一从 Tab3 实际发行预测提取成熟 outcome，账本样本数、按股票汇总、校准图和时间线使用同一记录集合。当前生产数据库副本复核由 `200 条明细 / 112 条成熟 / 0 行汇总` 修复为 `112 条成熟 / 10 只股票汇总`。
- 能力评估留空日期时，汇总、正确率和图表继续使用全部历史；逐次预测、策略与联合审计明细默认只展示最近30天。用户主动选择日期后，汇总和明细共同切换到所选区间。回归测试确认40天前的成熟预测仍进入全历史汇总、默认明细不展示，并可通过日期筛选重新调出。
- 策略统计口径完成强制拆分：买入/加仓只统计真实持有期交易净收益，卖出/减仓只统计“避免损失 - 踏空成本 - 交易摩擦”的退出质量。退出质量为正表示当时退出优于继续持有，但不是账户收益率，不能与入场收益平均或复利。
- `PlanEvidenceSnapshot` 增加可选动作身份；新分析按股票、策略、参数、风险档案、动作和周期提取历史证据。旧证据缺少动作字段时继续按旧身份只读恢复，不破坏已有用户数据库；新的买入与卖出证据不会再互相覆盖。
- 单股策略事件可能共享或重叠持有窗口，因此不再生成伪累计收益和伪回撤。累计收益、基准和回撤曲线只接受完整账户 `JointOutcome`；日期范围只筛选在该期间完成验证的记录，不代表用户从开始日连续持有到结束日。
- 本轮相关页面、报告、学习证据与渲染器定向回归通过；V2 全量回归为 `905 passed, 4 skipped in 86.19s`。另以当前生产数据库副本读取最近 200 条旧 `PlanEvidenceSnapshot`，全部兼容恢复。4 项仍为默认关闭、需要显式联网密钥与开关的真实 Provider/LLM 冒烟，不是功能失败。
