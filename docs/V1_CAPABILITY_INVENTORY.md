# TradeHelper V1 能力资产清单

> 本清单用于防止 TradeHelper 2.0 重构时丢失 1.x 已经验证过的有效能力。2.0 不是推倒 V1 心血，而是把 V1 的精华迁移到更清晰的预测 -> 情景 -> 策略 -> 风控 -> 学习架构中。

## 使用规则

1. 每个 V2 阶段开始前，先检查本清单中对应能力。
2. 每个 V2 阶段完成后，更新“V2 状态”和“验证测试”。
3. 能力迁移不能只靠报告观察，必须有单元测试或端到端测试保护。
4. 标记为“必须迁移”的能力，未经用户明确确认不得删除。
5. 如果 V2 采用不同实现，也必须保留同等或更强的业务效果。

“V2 目标位置”默认指 `tradehelper_v2/` 包下的对应模块；例如 `data/contracts.py` 表示 `tradehelper_v2/data/contracts.py` 或等效的 V2 合同模块。

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
| A股/美股同等支持 | 不能只服务美股用户 | `data/stock_fetcher.py`, `utils/market.py` | `data/contracts.py`, `data/providers/`, `risk/market_rules.py` | 待迁移 | `tests/v2/test_data_contracts.py` |
| 美股历史/盘中 TickFlow | 盘中实时数据主源 | `data/stock_fetcher.py` | `data/providers/tickflow.py` | 待迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股延伸时段 Nasdaq -> yfinance | TickFlow 没有延伸时段，避免旧价误判 | `data/stock_fetcher.py`, `services/portfolio_service.py` | `data/providers/us_extended.py` | 待迁移 | `tests/v2/test_e2e_us_extended_hours.py` |
| 美股基本面 Finnhub -> fallback | 基本面来源明确、可降级 | `alpha/fundamental.py`, `data/finnhub_client.py` | `data/providers/fundamentals.py` | 待迁移 | `tests/v2/test_provider_fallbacks.py` |
| 美股上市日期 Finnhub profile2 | 新股不能用上市前假历史 | `data/stock_fetcher.py` | `data/providers/listing.py` | 待迁移 | `tests/v2/test_data_quality.py` |
| A股历史/盘中 TickFlow | A股用户核心行情源 | `data/stock_fetcher.py` | `data/providers/tickflow.py` | 待迁移 | `tests/v2/test_e2e_a_share.py` |
| A股基本面 baostock -> akshare -> LLM | A股基本面不能缺席 | `alpha/fundamental.py` | `data/providers/fundamentals.py` | 待迁移 | `tests/v2/test_provider_fallbacks.py` |
| A股上市日期 baostock | A股新股样本裁剪 | `data/stock_fetcher.py` | `data/providers/listing.py` | 待迁移 | `tests/v2/test_data_quality.py` |
| 数据源降级记录 | 用户知道数据从哪里来、为什么降级 | `core/data_quality.py`, services | `data/quality.py` | 待迁移 | `tests/v2/test_data_quality.py` |
| 新闻 empty TTL | API 返回空不能永久阻断新闻 | `services/news_service.py` | `data/providers/news.py` | 待迁移 | `tests/v2/test_provider_fallbacks.py` |
| Tab1/Tab3 独立刷新 | 页面不能互相依赖 | `services/news_service.py`, `portfolio_service.py` | `data/repository.py`, use cases | 待迁移 | `tests/v2/test_data_contracts.py` |
| 实时价不写日 K | 避免盘中价污染收盘价 | `core/pipeline.py`, `data/stock_fetcher.py` | `data/repository.py`, `data/quality.py` | 待迁移 | `tests/v2/test_data_quality.py` |

## P0 Tab1 / Tab3 功能边界

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| Tab1 单股完整研究 | 单只股票深度分析入口 | `services/analysis_service.py`, `ui/main_page.py` | V2 single-stock use case | 待迁移 | `tests/v2/test_e2e_single_stock.py` |
| Tab1 三时段模式 | 盘前/盘中/盘后语义不同 | `analysis_service.py`, `core/pipeline.py` | V2 use cases + data session | 待迁移 | `tests/v2/test_e2e_single_stock.py` |
| Tab3 组合工作台 | 真实持仓决策，不是批量单股 | `services/portfolio_service.py`, `ui/portfolio_page.py` | V2 portfolio use case | 待迁移 | `tests/v2/test_e2e_portfolio.py` |
| Tab3 真实余额/持仓/成本 | 禁止虚构10万本金 | `data/database.py`, `portfolio_service.py` | `data/contracts.py`, `risk/sizing.py` | 待迁移 | `tests/v2/test_risk_officer.py` |
| Tab3 持仓行内编辑 | 用户卖一部分后可直接修改 | `ui/portfolio_page.py` | V2 UI portfolio component | 待迁移 | `tests/v2/test_ui_state_flow.py` |
| Tab3 组合集中度/风险容量 | 单票集中度、现金和剩余风险 | `portfolio_service.py` | `risk/sizing.py`, portfolio use case | 待迁移 | `tests/v2/test_position_sizing.py` |
| Tab3 关注股替换机会 | 组合视角比较持仓与关注股 | `portfolio_service.py` | portfolio scenario/plans | 待迁移 | `tests/v2/test_e2e_portfolio.py` |
| Tab3 历史评估 | 用户看系统能力是否变好 | `ui/portfolio_page.py`, `portfolio_service.py` | `learning/`, V2 UI | 待迁移 | `tests/v2/test_learning_ledgers.py` |

## P0 策略与风控资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| StrategyDecision-first | 回测和当前信号同一路径 | `strategies/base.py` | `strategies/engine.py` / `TradePlan` | 待迁移 | `tests/v2/test_trade_plan_contract.py` |
| decision_to_orders | 不维护两套买卖逻辑 | `strategies/base.py` | V2 order adapter or broker boundary | 待迁移 | `tests/v2/test_strategy_engine_by_scenario.py` |
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
| 跳空止损按开盘价 | 回测更接近真实风险 | `backtest/broker.py` | V2 broker boundary | 待迁移 | `tests/v2/test_strategy_*.py` |
| 动态滑点 | 高波动/低流动性不乐观 | `backtest/broker.py` | V2 execution assumptions | 待迁移 | `tests/v2/test_market_rules_v2.py` |
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
| 股票级自优化 | 不同股票适合不同模型/策略 | `per_stock_params`, forecast versions | `learning/optimizer.py` | 待迁移 | `tests/v2/test_stock_specific_optimizer.py` |

## P1 LLM 研究员资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| LLM 不能直接下单 | 防止不可复现建议 | `report/prompts.py`, docs | `learning/hypothesis_lab.py`, reports | 待迁移 | `tests/v2/test_llm_hypothesis_parser.py` |
| LLM 观察候选池 | 保留模型发现的新点子 | `services/research_observations.py` | `learning/hypothesis_lab.py` | 待迁移 | `tests/v2/test_hypothesis_validation.py` |
| 系统确认/反驳/待验证 | 分歧可见，不静默删除 | `research_observations.py` | reports + risk | 待迁移 | `tests/v2/test_hypothesis_validation.py` |
| LLM 和系统规则分账 | 不让系统规则冒领 LLM 命中率 | `research_observation_log` | `learning/hypothesis_lab.py` | 待迁移 | `tests/v2/test_hypothesis_promotion.py` |
| 有效假设沉淀候选模板 | 让系统越运行越聪明 | `research_observation_log` | `learning/hypothesis_lab.py` | 待迁移 | `tests/v2/test_hypothesis_promotion.py` |

## P1 UI 与报告资产

| 能力 | V1 价值 | V1 位置 | V2 目标位置 | V2 状态 | 验证测试 |
|------|---------|---------|-------------|---------|----------|
| 全宽报告阅读 | 减少左右布局浪费 | `ui/main_page.py`, `ui/portfolio_page.py` | V2 UI pages | 待迁移 | `tests/v2/test_ui_state_flow.py` |
| 报告一分钟操作台 | 用户先看最该做什么 | `services/portfolio_service.py` | `reports/sections/` | 待迁移 | `tests/v2/test_report_sections.py` |
| 预测表通俗化 | 像天气预报一样看预测对错 | `report/prompts.py` | `reports/sections/forecast.py` | 待迁移 | `tests/v2/test_report_sections.py` |
| 历史评估图表和表格 | 用户评估系统能力 | `ui/portfolio_page.py` | V2 UI history | 待迁移 | `tests/v2/test_report_readability_snapshots.py` |
| HTML/PDF 导出 | 用户保存报告 | `report/html_enhancer.py`, `pdf_exporter.py` | `reports/renderer.py` | 待迁移 | `tests/v2/test_report_sections.py` |

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
