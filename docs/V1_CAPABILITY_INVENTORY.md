# TradeHelper V1 能力资产清单

> 本清单用于防止 TradeHelper 2.0 重构时丢失 1.x 已经验证过的有效能力。2.0 不是推倒 V1 心血，而是把 V1 的精华迁移到更清晰的预测 -> 情景 -> 策略 -> 风控 -> 成交/组合 -> 学习架构中。

## 使用规则

1. 每个 V2 阶段开始前，先检查本清单中对应能力。
2. 每个 V2 阶段完成后，更新“V2 状态”和“验证测试”。
3. 能力迁移不能只靠报告观察，必须有单元测试或端到端测试保护。
4. 标记为“必须迁移”的能力，未经用户明确确认不得删除。
5. 如果 V2 采用不同实现，也必须保留同等或更强的业务效果。
6. V2-0/V2-1 的精确实现以 [docs/v2/CONTRACTS.md](./v2/CONTRACTS.md)、[docs/v2/POLICIES.md](./v2/POLICIES.md) 和 [docs/v2/GOLDEN_CASES.md](./v2/GOLDEN_CASES.md) 为准；本清单只说明能力来源和迁移状态。

“V2 目标位置”默认指 `tradehelper_v2/` 包下的对应模块；例如 `contracts/market_data.py` 表示 `tradehelper_v2/contracts/market_data.py` 或等效的 V2 合同模块。

V2 迁移原则是“吸收 V1 的能力，不直接依赖 V1 的耦合代码”。V1 位置用于定位参考实现、业务经验和回归样本；V2 实现应通过清晰合同重新编写。若短期必须借用外部 I/O client，只能通过临时 compatibility shim，并要在阶段状态中写明退出条件。

## 状态说明

| 状态 | 含义 |
|------|------|
| 待迁移 | V1 有能力，V2 尚未承接 |
| 迁移中 | V2 正在重构吸收 |
| 已迁移 | V2 已有同等或更强实现，并有测试 |
| 暂缓 | V2 先不实现，但保留设计位置 |
| 废弃 | 明确不要继续保留，必须有原因 |

## P0 核心目标与用户价值

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| 回答五个核心问题 | 保证系统始终围绕交易决策服务 | `UPGRADE_PLAN_V1.md`, `README_V1.md` | `README.md`, `DESIGN.md`, `V2_REFACTOR_PLAN.md` | 已迁移 | 文档检查 |
| 当前动作判断 | 买/卖/减仓/加仓/持有/观察 | `core/signal_check.py`, `strategies/` | `scenario/`, `strategies/`, `risk/` | 待迁移 | `tests/v2/test_strategy_*` |
| 条件触发计划 | 不能操作时告诉用户等什么条件 | `strategies/conditional_trigger.py`, `services/portfolio_service.py` | `scenario/planner.py`, `strategies/engine.py` | 待迁移 | `tests/v2/test_scenario_planner.py` |
| 最大亏损和失效条件 | 用户知道错了亏多少、哪里错 | `strategies/base.py`, `core/signal_check.py` | `risk/sizing.py`, `risk/officer.py` | 待迁移 | `tests/v2/test_risk_officer.py` |
| 历史正期望和可信度 | 建议不能只靠当下判断 | `prediction_log`, `trade_plan_log`, `joint_oof_runs` | `learning/` 三本账 | 待迁移 | `tests/v2/test_learning_ledgers.py` |
| 明确目标日预测 | 预测必须说清预测哪一天 | `forecast_log`, `core/forecast_engine.py` | `forecast/engine.py`, `learning/forecast_ledger.py` | 待迁移 | `tests/v2/test_forecast_diagnostics.py` |

## P0 数据源与市场支持

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| A股/美股同等支持 | 不能只服务美股用户 | `data/stock_fetcher.py`, `utils/market.py` | `contracts/market_data.py`, `data/providers/`, `risk/market_rules.py` | 迁移中（数据层已迁移） | `tests/v2/test_data_contracts.py` |
| 美股盘中 TickFlow | 盘中实时数据主源 | `data/stock_fetcher.py` | `data/providers/tickflow.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股已完成日K Nasdaq 主源 | 用户券商K线与 Nasdaq 历史 OHLCV 核对一致；TickFlow 不再承担美股日K主源 | V1 未独立实现 | `data/providers/nasdaq.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股日K应急降级 | Nasdaq 无结果时 yfinance、再 TickFlow 仅补已完成交易日 | `data/stock_fetcher.py` | `data/providers/us_daily.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股延伸时段 Nasdaq -> yfinance | TickFlow 没有延伸时段，避免旧价误判 | `data/stock_fetcher.py`, `services/portfolio_service.py` | `data/providers/us_extended.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股基本面 Finnhub -> yfinance -> akshare -> 百度 | 基本面来源明确、可降级且逐字段保留来源 | `alpha/fundamental.py`, `data/finnhub_client.py` | `data/providers/fundamentals.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股上市日期 Finnhub profile2 | 新股不能用上市前假历史 | `data/stock_fetcher.py` | `data/providers/listing.py` | 已迁移 | `tests/v2/test_data_quality.py` |
| A股历史/盘中 TickFlow | A股用户核心行情源 | `data/stock_fetcher.py` | `data/providers/tickflow.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| A股日K失败显式降级 | 没有受认可 fallback 时不静默换源或补假值 | `data/stock_fetcher.py`, data quality | `data/providers/tickflow.py`, `data/quality.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| A股基本面 baostock -> akshare | A股基本面不能缺席；LLM 只能解释有来源事实 | `alpha/fundamental.py` | `data/providers/fundamentals.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| A股上市日期 baostock | A股新股样本裁剪 | `data/stock_fetcher.py` | `data/providers/listing.py` | 已迁移 | `tests/v2/test_data_quality.py` |
| 数据源降级记录 | 用户知道数据从哪里来、为什么降级 | `core/data_quality.py`, services | `data/quality.py` | 已迁移 | `tests/v2/test_data_quality.py` |
| 交易所日历和时区 | 正确计算目标交易日、节假日和夏令时 | `utils/trading_calendar.py` | `data/calendar.py` | 已迁移 | `tests/v2/test_trading_calendar.py` |
| 复权与公司行动口径 | 避免拆股、分红被误判为普通暴涨暴跌 | TickFlow 前复权、历史清洗逻辑 | `contracts/market_data.py`, `data/quality.py` | 已迁移 | `tests/v2/test_corporate_actions.py` |
| LLM 不补造基本面事实 | 财务数据缺失时宁缺毋滥 | prompts、基本面降级逻辑 | `data/providers/fundamentals.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 新闻 empty TTL | API 返回空不能永久阻断新闻 | `services/news_service.py` | `data/providers/news.py` | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| Provider 配额治理 | TickFlow 拆批、Nasdaq/Finnhub 并发上限、超时退避 | `data/stock_fetcher.py`, services | `data/providers/`, `data/quality.py` | 已迁移 | `tests/v2/test_rate_queue_and_drift.py` |
| 分模式缓存失效 | 盘前/盘中/盘后按数据类型刷新，失败缓存不冒充最新事实 | news service, portfolio prefetch | `data/repository.py`, providers | 已迁移 | `tests/v2/test_provider_fallbacks.py` |
| 缺失字段不填 0 | Nasdaq 缺 OHLCV 时保留能力边界，避免假数据 | quote quality gate | `contracts/market_data.py`, `data/quality.py` | 已迁移 | `tests/v2/test_data_quality.py` |
| Tab3 逐股质量隔离 | 单股缺价只阻断该股，不被组合其他股票带过 | `portfolio_service.py` | `data/quality.py`, `portfolio/` | 迁移中（数据批次已隔离） | `tests/v2/test_rate_queue_and_drift.py` |
| Tab1/Tab3 独立刷新 | 页面不能互相依赖 | `services/news_service.py`, `portfolio_service.py` | `data/repository.py`, use cases | 迁移中（共享刷新服务已迁移） | `tests/v2/test_provider_fallbacks.py` |
| 实时价不写日 K | 避免盘中价污染收盘价 | `core/pipeline.py`, `data/stock_fetcher.py` | `data/repository.py`, `data/quality.py` | 已迁移 | `tests/v2/test_market_data_repository.py` |
| SQLite 幂等迁移与去重 | 旧用户升级、新用户建库都能安全运行 | `data/database.py` migrations | `data/migrations/schema.py` | 已迁移 | `tests/v2/test_schema_migrations.py` |

## P0 Tab1 / Tab3 功能边界

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| Tab1 单股完整研究 | 单只股票深度分析入口 | `services/analysis_service.py`, `ui/main_page.py` | V2 single-stock use case | 待迁移 | `tests/v2/test_e2e_single_stock.py` |
| Tab1 三时段模式 | 盘前/盘中/盘后语义不同 | `analysis_service.py`, `core/pipeline.py` | V2 use cases + data session | 待迁移 | `tests/v2/test_e2e_single_stock.py` |
| Tab3 组合工作台 | 真实持仓决策，不是批量单股 | `services/portfolio_service.py`, `ui/portfolio_page.py` | V2 portfolio use case | 待迁移 | `tests/v2/test_e2e_portfolio.py` |
| Tab3 三时段模式 | 组合盘前/盘中/盘后使用对应数据和计划有效期 | `portfolio_service.py`, `core/pipeline.py` | `use_cases/portfolio.py`, `portfolio/` | 待迁移 | `tests/v2/test_e2e_mode_matrix.py` |
| Tab3 真实余额/持仓/成本 | 禁止虚构10万本金 | `data/database.py`, `portfolio_service.py` | `contracts/market_data.py`, `risk/sizing.py` | 待迁移 | `tests/v2/test_risk_officer.py` |
| Tab3 持仓行内编辑 | 用户卖一部分后可直接修改 | `ui/portfolio_page.py` | V2 UI portfolio component | 待迁移 | `tests/v2/test_ui_state_flow.py` |
| Tab3 组合集中度/风险容量 | 单票集中度、现金和剩余风险 | `portfolio_service.py` | `risk/sizing.py`, portfolio use case | 待迁移 | `tests/v2/test_position_sizing.py` |
| Tab3 冻结估值 | 同一批现价计算权益、市值和仓位，避免伪 103.6% | `portfolio_service.py` | `portfolio/allocator.py` | 待迁移 | `tests/v2/test_portfolio_frozen_valuation.py` |
| Tab3 跨股票排序与冲突消解 | 先处理风险，再比较关注股替换机会 | `portfolio_service.py` | `portfolio/ranking.py` | 待迁移 | `tests/v2/test_portfolio_ranking.py` |
| Tab3 关注股替换机会 | 组合视角比较持仓与关注股 | `portfolio_service.py` | portfolio scenario/plans | 待迁移 | `tests/v2/test_e2e_portfolio.py` |
| Tab3 历史评估 | 用户看系统能力是否变好 | `ui/portfolio_page.py`, `portfolio_service.py` | `learning/`, V2 UI | 待迁移 | `tests/v2/test_learning_ledgers.py` |

## P0 特征资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| 技术指标与多周期事实 | 收益、均线、趋势、波动、成交量、支撑压力的客观输入 | `indicators/technical.py`, `alpha/scoring.py` | `features/technical.py` | 已迁移 | `tests/v2/test_feature_technical.py` |
| 盘中价与正式日K隔离 | 实时条件不污染训练历史 | `core/pipeline.py` | `features/snapshot.py` | 已迁移 | `tests/v2/test_feature_point_in_time.py` |
| 新闻情绪时点对齐 | 历史预测不能看到后来抓取的新闻 | `indicators/sentiment.py`, `align_finbert_scores` | `features/news.py` | 已迁移 | `tests/v2/test_feature_point_in_time.py` |
| 基本面字段归一与时点对齐 | 不同来源单位一致且不回填未来财报 | `alpha/fundamental.py` | `features/fundamentals.py` | 已迁移 | `tests/v2/test_feature_degradation.py` |
| 缺失不填0/中性值 | 防止缺数据被模型误认为真实中性 | V1数据质量修复经验 | `FeatureStatus/FeatureValue` | 已迁移 | `tests/v2/test_feature_degradation.py` |
| 同一FeatureSnapshot供预测和策略使用 | 防止报告解释与实际计算两套事实 | V1 pipeline散布计算 | `features/snapshot.py` | 已迁移 | `tests/v2/test_feature_contracts.py` |
| 特征快照稳定哈希和持久化 | 支持OOF复现、审计和版本回滚 | V1无统一实现 | `features/store.py` | 已迁移 | `tests/v2/test_feature_store.py` |
| Final_Score边界重划 | 保留V1经验但不把交易观点冒充数据事实 | `alpha/scoring.py` | V2-3候选模型/后续策略，不进入V2-2 | 暂缓至后续层 | `tests/v2/test_forecast_*` |

## P0 策略与风控资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| StrategyDecision-first | 回测和当前信号同一路径 | `strategies/base.py` | `strategies/engine.py` / `TradePlan` | 待迁移 | `tests/v2/test_trade_plan_contract.py` |
| decision_to_orders | 不维护两套买卖逻辑 | `strategies/base.py` | `execution/orders.py` | 待迁移 | `tests/v2/test_order_intent.py` |
| 完整条件计划集合 | 同时给出当前状态、买/加、卖/减、持有和失效条件 | `conditional_trigger.py`, reports | `DecisionBundle`, `strategies/engine.py` | 待迁移 | `tests/v2/test_trade_plan_contract.py` |
| 保守/激进风险档案 | 同一事实和触发条件下只调整确认门槛、风险和仓位 | `core/signal_check.py`, reports | `DecisionBundle` risk profiles | 待迁移 | `tests/v2/test_trade_plan_contract.py` |
| 计划会话与有效期 | 盘前/盘中/盘后计划不会跨会话变成陈旧指令 | `trade_plan_log`, session helpers | `TradePlan.valid_from/expires_at` | 待迁移 | `tests/v2/test_e2e_mode_matrix.py` |
| A/B/C/D 执行等级 | 建议可执行性明确 | `core/signal_check.py` | `risk/officer.py` | 待迁移 | `tests/v2/test_risk_officer.py` |
| 风险退出不被预测阻止 | 止损/锁利优先于预测分歧 | `core/signal_check.py` | `risk/officer.py` | 待迁移 | `tests/v2/test_risk_officer.py` |
| MA120 支撑 | 人类交易者关心的支撑反弹 | `strategies/ma120_support.py` | `strategies/templates/` | 待迁移 | `tests/v2/test_strategy_engine_by_scenario.py` |
| 冲高回落锁利 | 持仓止盈/减仓关键策略 | `strategies/profit_lock.py` | `strategies/templates/` | 待迁移 | `tests/v2/test_strategy_engine_by_scenario.py` |
| 持仓风险管理 | 成本、亏损、集中度、禁止加仓 | `strategies/position_risk.py` | `risk/`, `strategies/templates/` | 待迁移 | `tests/v2/test_position_sizing.py` |
| 反抽失败退出 | 跌破均线后退出逻辑 | `strategies/pullback_failed_exit.py` | `strategies/templates/` | 待迁移 | `tests/v2/test_strategy_engine_by_scenario.py` |
| 条件触发策略 | 不操作时给等待条件 | `strategies/conditional_trigger.py` | `scenario/`, `strategies/engine.py` | 待迁移 | `tests/v2/test_scenario_planner.py` |
| 策略无信号诊断 | 告诉用户还差什么条件 | `diagnose_no_signal()` | `TradePlan.missing_conditions` | 待迁移 | `tests/v2/test_trade_plan_contract.py` |

## P0 回测、市场规则与审计资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| T 日决策、T+1 开盘成交 | 避免未来函数 | `backtest/engine.py` | V2 simulation/learning | 待迁移 | `tests/v2/test_learning_ledgers.py` |
| 当前/回放共用订单意图 | 同一 TradePlan 生成当前预览和历史成交，不维护双路径 | `StrategyDecision -> Order` | `execution/orders.py` | 待迁移 | `tests/v2/test_order_intent.py` |
| 跳空止损按开盘价 | 回测更接近真实风险 | `backtest/broker.py` | V2 broker boundary | 待迁移 | `tests/v2/test_strategy_*.py` |
| 动态滑点 | 高波动/低流动性不乐观 | `backtest/broker.py` | V2 execution assumptions | 待迁移 | `tests/v2/test_market_rules_v2.py` |
| 无分钟证据不伪造路径 | 盘中方案不能用整日K冒充信号后走势 | `intraday_bar_log`, trade plan verifier | `execution/simulator.py` | 待迁移 | `tests/v2/test_fill_simulator.py` |
| Decision/Broker 分账 | 保留策略建议与成交/拒单差异 | `joint_oof_runs` | `execution/`, `learning/joint_ledger.py` | 待迁移 | `tests/v2/test_fill_simulator.py` |
| A股一手、T+1、涨跌停、费用 | A股交易规则不能缺 | `utils/market_rules.py` | `risk/market_rules.py` | 待迁移 | `tests/v2/test_market_rules_v2.py` |
| 策略审计 Bootstrap | 样本外置信区间 | `core/strategy_audit.py` | `learning/strategy_ledger.py` | 待迁移 | `tests/v2/test_attribution_rules.py` |
| 参数 walk-forward 晋升 | 防止未来数据选参数 | `core/strategy_pool.py` | `learning/optimizer.py` | 待迁移 | `tests/v2/test_stock_specific_optimizer.py` |
| 负期望 recovery | 策略自我修复 | `core/strategy_pool.py` | `learning/optimizer.py` | 待迁移 | `tests/v2/test_stock_specific_optimizer.py` |

## P0 预测与学习资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| 独立 ForecastResult | 预测不由交易动作反推 | `core/forecast_engine.py` | `forecast/engine.py` | 待迁移 | `tests/v2/test_forecast_feature_sets.py` |
| 明确目标交易日 | 用户知道预测哪天 | `forecast_log` | `learning/forecast_ledger.py` | 待迁移 | `tests/v2/test_forecast_diagnostics.py` |
| OOF Champion/Challenger | 只让样本外通过模型参与执行 | `forecast_model_versions` | `forecast/registry.py` | 待迁移 | `tests/v2/test_forecast_model_registry.py` |
| Brier/LogLoss/ECE/区间命中 | 评价概率预测质量 | `core/forecast_engine.py`, DB metrics | `forecast/diagnostics.py` | 待迁移 | `tests/v2/test_forecast_diagnostics.py` |
| 三本账思路 | 区分预测错、策略错、联合错 | `forecast_log`, `trade_plan_log`, `joint_oof_runs` | `learning/` | 待迁移 | `tests/v2/test_learning_ledgers.py` |
| 联合 OOF | 预测+策略+风控整体回放 | `core/joint_oof.py` | `learning/joint_ledger.py` | 待迁移 | `tests/v2/test_attribution_rules.py` |
| 五层效果归因 | 分开评价预测、情景、策略、风控和成交贡献 | V1 三本账与 Decision/Broker 分账 | `learning/` attribution | 待迁移 | `tests/v2/test_attribution_rules.py` |
| 股票级自优化 | 不同股票适合不同模型/策略 | `per_stock_params`, forecast versions | `learning/optimizer.py` | 待迁移 | `tests/v2/test_stock_specific_optimizer.py` |
| 受控优化与可回滚版本 | 只调注册候选和参数，不自动改源码或取消硬风控 | model versions, strategy candidates | `learning/optimizer.py` | 待迁移 | `tests/v2/test_stock_specific_optimizer.py` |

## P1 LLM 研究员资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| LLM 不能直接下单 | 防止不可复现建议 | `report/prompts.py`, docs | `research/`, reports | 待迁移 | `tests/v2/test_llm_hypothesis_parser.py` |
| LLM 观察候选池 | 保留模型发现的新点子 | `services/research_observations.py` | `research/hypothesis_lab.py` | 待迁移 | `tests/v2/test_hypothesis_validation.py` |
| 系统确认/反驳/待验证 | 分歧可见，不静默删除 | `research_observations.py` | reports + risk | 待迁移 | `tests/v2/test_hypothesis_validation.py` |
| LLM 和系统规则分账 | 不让系统规则冒领 LLM 命中率 | `research_observation_log` | `research/hypothesis_lab.py` | 待迁移 | `tests/v2/test_hypothesis_promotion.py` |
| 有效假设沉淀候选模板 | 让系统越运行越聪明 | `research_observation_log` | `research/` + `learning/optimizer.py` | 待迁移 | `tests/v2/test_hypothesis_promotion.py` |

## P1 UI 与报告资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| 全宽报告阅读 | 减少左右布局浪费 | `ui/main_page.py`, `ui/portfolio_page.py` | V2 UI pages | 待迁移 | `tests/v2/test_ui_state_flow.py` |
| 报告一分钟操作台 | 用户先看最该做什么 | `services/portfolio_service.py` | `reports/sections/` | 待迁移 | `tests/v2/test_report_sections.py` |
| 预测表通俗化 | 像天气预报一样看预测对错 | `report/prompts.py` | `reports/sections/forecast.py` | 待迁移 | `tests/v2/test_report_sections.py` |
| 历史评估图表和表格 | 用户评估系统能力 | `ui/portfolio_page.py` | V2 UI history | 待迁移 | `tests/v2/test_report_readability_snapshots.py` |
| 指标解释和图表读法 | 显示样本数、横纵轴、基线、目标日和一句话结论 | V1 历史评估说明 | V2 reports/UI | 待迁移 | `tests/v2/test_report_readability_snapshots.py` |
| 分阶段进度与后台优化 | 前台不等待深度 OOF，用户知道运行到哪一步 | services background optimizer, Flet progress | V2 use cases/UI task model | 待迁移 | `tests/v2/test_ui_state_flow.py` |
| 前台性能预算 | 缓存命中主链与网络/LLM延迟分开度量 | V1 并发预取和后台优化 | V2 performance benchmark | 待迁移 | `tests/v2/test_interactive_performance.py` |
| HTML/PDF 导出 | 用户保存报告 | `report/html_enhancer.py`, `pdf_exporter.py` | `reports/renderer.py` | 待迁移 | `tests/v2/test_report_sections.py` |
| 历史报告检索与评分 | 按股票、市场、模式、日期和评分查阅旧报告 | `ui/history_page.py` | `ui/pages/report_history.py` | 待迁移 | `tests/v2/test_report_history_flow.py` |
| 首次运行与设置页 | 配置工作目录、行情 token、LLM、代理并控制页面可用性 | `ui/settings_ui.py`, `config/settings.py` | `config/settings.py`, `ui/pages/settings.py` | 待迁移 | `tests/v2/test_settings_flow.py` |
| macOS/Windows 运行烟雾 | 构建后实际启动，防止 jaraco/资源文件缺失 | `scripts/`, `.github/workflows/` | V2 release acceptance | 待迁移 | 构建后 smoke |

## P2/P3 可暂缓但需保留位置

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| Web 版 | 未来部署形态 | Flet web | UI layer | 暂缓 | 待定 |
| 打包体积优化 | 发行体验 | scripts/spec | packaging | 暂缓 | 构建测试 |
| 停复牌/ST 权威数据 | A股风险细节 | data quality TODO | data providers | 暂缓 | 待数据源 |
| Level2/盘口深度 | 流动性增强 | depth_factor | optional provider | 暂缓 | 待数据源 |

## 阶段迁移检查模板

每完成一个 V2 阶段，必须在 [V2_REFACTOR_PLAN.md](../V2_REFACTOR_PLAN.md) 中记录：

```text
阶段：
已吸收的 V1 能力：
仍待迁移的 V1 能力：
A股覆盖：
美股覆盖：
新增测试：
剩余风险：
```
