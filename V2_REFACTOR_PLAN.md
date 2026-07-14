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
| `AGENTS.md` | Codex 本地工作约定 |
| `CLAUDE.md` | Claude Code 本地工作约定 |

V2-0/V2-1 冲突优先级：三份基础规范 > 本计划中的概念示例 > V1 能力清单 > V1 参考代码。V2-2、V2-3、V2-4 分别以对应阶段规范为准；当前实现只授权 V2-4，完成后必须停止并等待复审。

## 1. V2 分层结构

为避免 V1/V2 逻辑混在一起，2.0 新代码优先放入独立 `tradehelper_v2/` 包。V1 代码只作为参考实现、算法来源、测试样本和回归对照；V2 主链路不直接 import V1 的耦合业务模块。复用的是 V1 中验证过的处理逻辑和算法思想，而不是把 V1 代码换个目录继续运行。

若极少数外部 I/O client 在早期阶段确实需要临时借用，必须满足三条：

1. 只能通过显式 compatibility shim 调用，不能散落在 V2 业务逻辑里。
2. 必须在阶段计划中写明替换目标和退出条件。
3. 预测、情景、策略、风控、学习主链路不得依赖 shim。

```text
tradehelper_v2/
  __init__.py
  app.py                  # V2 composition root，组装依赖和 Flet 页面

  contracts/
    market_data.py        # Bar/Quote/News/Fundamental/Account
    analysis.py           # Feature/Forecast/Scenario
    decisions.py          # TradePlan/DecisionBundle/ExecutionDecision/PortfolioDecision
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
  decision_mode: premarket / intraday / postmarket
  risk_profile: conservative / aggressive
```

策略输出统一为：

```text
TradePlan:
  plan_id
  action: buy / add / sell / reduce / hold / watch / invalid
  scenario_id
  forecast_horizon
  strategy_family
  trigger_condition
  trigger_price
  stop_loss
  take_profit
  take_profit_mode: fixed / dynamic / conditional / none
  position_pct
  max_loss_amount
  invalidation
  valid_session
  valid_from
  expires_at
  evidence
  missing_conditions
```

一次分析的最终策略输出不是单条 `TradePlan`，而是：

```text
DecisionBundle:
  current_state
  forecast_scenario
  entry_or_add_plans[]
  reduce_or_exit_plans[]
  hold_condition
  invalidation
  conservative_profile
  aggressive_profile
```

保守与激进档案必须使用同一组事实、预测和策略方向。二者只允许在确认门槛、最大风险金额和仓位上不同；若触发价相同，报告必须直说“同一触发条件，不同风险预算”，不能伪造两套价格。

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
7. 同一次分析必须同时给出买入/加仓、卖出/减仓、持有和失效分支，且分支互斥、可解释。
8. 盘前计划只在当日常规会话有效，盘中计划只在当日剩余会话有效，盘后计划只在下一交易日有效。
9. 保守/激进方案不得给出互相矛盾的方向判断；同触发价时必须通过仓位和风险预算体现差异。

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

执行等级固定定义为：

| 等级 | 标准 | 动作 |
|------|------|------|
| A | 事实成立 + 风险可控 + 当前股票/策略有可靠正期望 | 可执行 |
| B | 事实成立 + 风险可控 + 样本或历史证据不足 | 小仓验证 |
| C | 事实成立，但历史期望、风险容量或预测一致性不支持 | 仅观察 |
| D | 数据冲突、关键事实缺失或计划不可成交 | 驳回 |

风控参数分为两类：止损、最大亏损、账户权益、数据质量和市场硬规则不可优化；集中度软上限、风险缩放和确认阈值只有经过 OOF 及在线确认后才能在预设范围内调整。

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
7. 历史反馈不能把 D 级数据冲突升级，也不能取消硬止损或市场规则。
8. 保守/激进档案都受同一账户总风险和单票集中度硬上限约束。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_risk_*.py tests/v2/test_position_sizing.py -q
```

## 8. 成交与仿真层

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

### 11.1 目标

LLM 的价值从“写报告”升级为“提出研究假设”，但所有假设必须结构化和验证。

LLM 建议拆解为：

```text
ForecastHypothesis:
  pattern, expected_direction, horizon, evidence_text

ModelHypothesis:
  feature_set, model_family, regime_scope, expected_improvement, evidence_text

StrategyHypothesis:
  trigger_condition, action, stop_loss_rule, take_profit_rule, invalidation
```

结构化假设必须落入受控 DSL/注册表，只能使用已注册事实字段、比较算子、时间窗口和风险规则。LLM 可以建议新增候选算子，但未经确定性实现、单元测试和 OOF 验证，不能自动生成或修改生产源码。

每条观察必须保留 `confirmed / refuted / pending / invalid_data` 状态、系统证据和下一步验证条件。风控官只决定能否执行，不能静默删除研究员观察；报告必须让用户看到 LLM 与系统的分歧。

### 11.2 转正流程

```text
LLM 原始观察/模型建议
  -> 结构化假设
    -> 事实验证
      -> 历史 OOF 回放
        -> 股票级/行业级表现统计
          -> 候选预测特征、注册模型配置或候选策略模板
            -> Champion/正式策略晋升
```

### 11.3 测试方案

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
6. LLM 无来源的财务数字不能进入特征、事实验证或报告确定性表格。
7. 假设晋升只创建可回滚的候选版本，不能越过数据质量、止损和账户风险闸门。
8. LLM 建议的新模型只能先映射为已注册模型族和特征配置；全新算法必须由确定性代码实现并补测试后才能进入候选池。
9. confirmed/refuted/pending/invalid_data 四种状态都能进入报告，且保留系统证据，不会静默丢弃。

验收标准：

```text
venv/bin/python -m pytest tests/v2/test_llm_hypothesis_*.py tests/v2/test_hypothesis_*.py -q
```

## 12. UI 与报告重构

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
- 新增 `DecisionBundle`、保守/激进风险档案、计划会话和有效期，恢复 V1 已确认的完整条件计划语义。
- 明确预测主要指标与校准护栏、策略绝对超额/风险调整双通道、五层效果归因和受控可回滚优化。
- 补充交易所日历、时区、复权/公司行动、缺失字段、逐股质量隔离、provider 配额、幂等迁移和 V1 数据保护。
- 增加 Tab1/Tab3 × A股/美股 × 盘前/盘中/盘后完整验收矩阵，以及前台性能和 macOS/Windows 发布烟雾标准。
- 新增 `docs/v2/CONTRACTS.md`、`POLICIES.md`、`GOLDEN_CASES.md`，固定 V2-0/V2-1 的类型、数据源路由、缓存/质量常量、独立数据库边界及标准答案。
- 修正 `AGENTS.md` 主流程，明确当前决策链与共享验证链；当前实现授权只到 V2-1。
- 本次仅完成设计文档补强；V2-0 至 V2-12 实现状态仍为未开始。

### 2026-07-10 V2-0 完成：测试基础设施

- 新增独立 `tradehelper_v2/` 包及 `tests/v2/`，测试使用固定时钟、注入日历、脚本化 Provider 和临时 SQLite，默认不读取用户工作目录或访问网络。
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

| 阶段 | 状态 | 说明 |
|------|------|------|
| V2-0 测试基础设施 | 已完成 | Golden G00-G04、架构边界、冻结时钟、双市场 fixture 与性能基线已落地 |
| V2-1 数据层 | 已完成 | Golden G10-G29/G30-G63、Provider fixture、路由、时点语义、质量、独立 repository、持久化配额续跑、并发、日K跨源漂移审计及真实 Provider smoke 均已通过 |
| V2-2 特征层 | 已完成 | FeatureSnapshot、F00-F13、双市场点时特征、migration 5/FeatureStore、架构边界、性能及全量回归已通过 |
| V2-3 预测层 | 已完成并复审 | Forecast contracts、波动率标签、FeatureSet/校准、JSON+zlib artifact、20候选、maturity-purged OOF、registry 回退/重启恢复、migration 6/7 和预测快照幂等读写已通过 FC00-FC18；不生成 TradePlan |
| V2-4 情景层 | 已完成并复审 | TradingScenario 合同、多周期归并、来源/时效降级、当前事实覆盖、三时段会话、策略家族兼容性、migration 8、强校验持久化和 SC00-SC21 共46条测试已通过；不生成 TradePlan |
| V2-5 策略层 | 未开始 | 等 TradingScenario 稳定 |
| V2-6 风控层 | 未开始 | 可并行梳理合同，但实现等 TradePlan 稳定 |
| V2-7 成交仿真层 | 未开始 | 等 ExecutionDecision 和市场规则稳定 |
| V2-8 组合决策层 | 未开始 | 等单股执行决策和冻结估值合同稳定 |
| V2-9 学习层 | 未开始 | 等预测、计划、风控和成交事件合同稳定 |
| V2-10 LLM 假设层 | 未开始 | 可复用 V1 observation，但需拆预测/策略假设并限制为 DSL |
| V2-11 报告/UI | 未开始 | 最后做展示，不再用报告反推计算正确性 |
| V2-12 迁移/端到端/发布 | 未开始 | 每层单测通过后执行完整矩阵与跨平台烟雾 |

## 16. 当前下一步：V2-5 策略层设计

V2-4 已完成复审并冻结。开始 V2-5 前必须先制定 StrategyInput、TradePlan、条件表达式、保守/激进差异和策略迁移清单的精确合同；不得把 TradingScenario 直接当作交易指令，也不得提前实现风控、组合决策、LLM 或 UI。
