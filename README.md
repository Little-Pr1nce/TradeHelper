# TradeHelper 2.0

TradeHelper 是 Python 3.12 + Flet 的 A 股/美股分析桌面应用。当前 `V2.0` 分支用于重构下一代可信交易决策系统。

2.0 的目标不是继续堆叠 1.x 功能，而是把系统重建为更清晰的交易决策链：

```text
当前决策：数据事实 -> 特征快照 -> 预测情景 -> 条件计划 -> 风控分级 -> 单股/组合决策 -> 报告/UI
共享验证：TradePlan + 风控结果 -> 订单预览/成交仿真/到期验证 -> 三本账 -> 复盘学习
```

## 当前状态

- 当前分支：`V2.0`
- 2.0 实施计划：[V2_REFACTOR_PLAN.md](./V2_REFACTOR_PLAN.md)
- 2.0 架构入口：[DESIGN.md](./DESIGN.md)
- V1 能力资产清单：[docs/V1_CAPABILITY_INVENTORY.md](./docs/V1_CAPABILITY_INVENTORY.md)
- V2-0/V2-1 精确合同：[docs/v2/CONTRACTS.md](./docs/v2/CONTRACTS.md)
- V2-0/V2-1 确定政策：[docs/v2/POLICIES.md](./docs/v2/POLICIES.md)
- V2-0/V2-1 标准案例：[docs/v2/GOLDEN_CASES.md](./docs/v2/GOLDEN_CASES.md)
- V2-2 特征层规范：[docs/v2/V2_2_FEATURES.md](./docs/v2/V2_2_FEATURES.md)
- V2-3 预测层规范：[docs/v2/V2_3_FORECAST.md](./docs/v2/V2_3_FORECAST.md)
- V2-4 情景层规范：[docs/v2/V2_4_SCENARIOS.md](./docs/v2/V2_4_SCENARIOS.md)
- V2-5 策略层规范：[docs/v2/V2_5_STRATEGIES.md](./docs/v2/V2_5_STRATEGIES.md)
- V2-6 风控层规范：[docs/v2/V2_6_RISK.md](./docs/v2/V2_6_RISK.md)
- V2-7 成交仿真层规范：[docs/v2/V2_7_EXECUTION.md](./docs/v2/V2_7_EXECUTION.md)
- V2-8 组合决策层规范：[docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md)
- V2-9 学习层规范：[docs/v2/V2_9_LEARNING.md](./docs/v2/V2_9_LEARNING.md)
- V2-10 LLM 假设层规范：[docs/v2/V2_10_LLM_HYPOTHESES.md](./docs/v2/V2_10_LLM_HYPOTHESES.md)
- V2-11 报告与 UI 层规范：[docs/v2/V2_11_REPORT_UI.md](./docs/v2/V2_11_REPORT_UI.md)
- 1.x 文档归档：[docs/archive/v1/](./docs/archive/v1/)

V2-0 至 V2-10 已完成并复审。V2-11 报告与 UI 层的精确设计已经冻结，当前授权按该规范实现结构化报告、历史评估、Tab1/Tab3、任务进度、历史报告、设置和导出；不得提前进入 V2-12 迁移/端到端/发布。

## 一以贯之的系统目标

无论 1.x 还是 2.0，TradeHelper 都要稳定回答五个问题：

1. 现在是否可以买、卖、减仓、加仓、持有？
2. 如果现在不能操作，达到什么条件可以操作？
3. 如果判断错了，最大亏损是多少，在哪里失效？
4. 这个建议过去有没有正期望，可信度有多高？
5. 系统预测的是哪个目标日期、概率和收益区间，过去预测到底准不准？

## 2.0 重构重点

1. 预测模型独立判断 1/3/5/10 日方向概率和收益区间。
2. 情景规划器把预测结果转成可交易环境。
3. 交易策略根据情景生成买入、加仓、减仓、卖出、持有或观察计划。
4. 风控官只负责事实、风险、仓位和历史证据检查。
5. 同一个 TradePlan 同时用于当前建议、订单预览和历史成交仿真，避免实盘与回测两套逻辑。
6. Tab3 由组合决策层负责跨股票排序、集中度、相关性、风险容量和替换机会。
7. 历史复盘拆成预测账、策略账和联合账，方便判断到底是哪一层失效。
8. LLM 作为研究假设生成器，不能直接生成可执行交易指令，也不能补造基本面事实。

## 不可丢失的 V1 硬约束

2.0 重构必须保留 1.x 开发中反复验证出的关键约束：

1. **A股和美股同等重要**：任何核心能力不能只做美股；数据合同、特征、预测、策略、风控、报告和测试都要覆盖 A 股与美股。
2. **Tab1 是单股完整研究**：必须覆盖单只股票的行情、技术、基本面、新闻、预测、策略、风控、历史证据和 LLM 观察。
3. **Tab3 不是 Tab1 批量版**：必须使用用户真实余额、持仓数量、成本、现金和关注列表，额外处理组合仓位、集中度、浮盈浮亏、禁止加仓、减仓/止盈/止损和替换机会。
4. **数据源原则不能漂移**：盘中实时、延伸时段、基本面、新闻和上市日期必须按市场走规定数据源，并记录来源、时间戳和降级原因。
5. **实时价不得污染历史日 K**：盘中和延伸时段快照只能进入当前决策快照，不能写成正式收盘价。

## 开发原则

- 从数据层往上重构，每层有独立合同和测试。
- 数据层未稳定前，不改预测、策略、报告或 UI。
- 不再依赖“看日志和看完整报告”验证功能正确性。
- 新功能必须能通过对应层级测试单独验证。
- V2 新代码优先放入独立 `tradehelper_v2/` 包。V1 代码作为参考实现、算法来源和回归对照，不作为 V2 主链路的直接运行依赖。

## 文档关系

| 文件 | 用途 |
|------|------|
| [README.md](./README.md) | 项目当前入口，说明目标、状态和常用命令 |
| [DESIGN.md](./DESIGN.md) | 2.0 架构设计，说明系统是什么、各层职责和边界 |
| [V2_REFACTOR_PLAN.md](./V2_REFACTOR_PLAN.md) | 2.0 实施计划，说明按什么顺序开发、每层怎么测试和验收 |
| [docs/V1_CAPABILITY_INVENTORY.md](./docs/V1_CAPABILITY_INVENTORY.md) | V1 能力资产清单，防止 2.0 重构时丢失已验证能力 |
| [docs/v2/CONTRACTS.md](./docs/v2/CONTRACTS.md) | V2-0/V2-1 类型、不变量、序列化和 repository 精确合同 |
| [docs/v2/POLICIES.md](./docs/v2/POLICIES.md) | V2-0/V2-1 数据源、缓存、质量和数据库确定政策 |
| [docs/v2/GOLDEN_CASES.md](./docs/v2/GOLDEN_CASES.md) | V2-0/V2-1 固定输入与预期结果，防止测试迁就实现 |
| [docs/v2/V2_2_FEATURES.md](./docs/v2/V2_2_FEATURES.md) | V2-2 point-in-time 特征合同、公式、实施顺序和 Golden Cases |
| [docs/v2/V2_3_FORECAST.md](./docs/v2/V2_3_FORECAST.md) | V2-3 预测目标、模型、OOF、注册、持久化和 Golden Cases |
| [docs/v2/V2_4_SCENARIOS.md](./docs/v2/V2_4_SCENARIOS.md) | V2-4 情景合同、多周期归并、三时段、降级、持久化和 Golden Cases |
| [docs/v2/V2_5_STRATEGIES.md](./docs/v2/V2_5_STRATEGIES.md) | V2-5 TradePlan、条件 DSL、策略模板、V1 迁移矩阵和 SP00-SP29 |
| [docs/v2/V2_6_RISK.md](./docs/v2/V2_6_RISK.md) | V2-6 风控合同、真实账户估值、A/B/C/D、sizing、双市场规则和 RK00-RK42 |
| [docs/v2/V2_7_EXECUTION.md](./docs/v2/V2_7_EXECUTION.md) | V2-7 OrderIntent、触发状态机、当前预览、历史成交、费用/滑点、migration 11 和 EX00-EX49 |
| [docs/v2/V2_8_PORTFOLIO.md](./docs/v2/V2_8_PORTFOLIO.md) | V2-8 组合批次、排序、现金/heat/相关性分配、最终股数、migration 12 和 PO00-PO49 |
| [docs/v2/V2_9_LEARNING.md](./docs/v2/V2_9_LEARNING.md) | V2-9 到期验证、三本账、六层归因、OOF、自优化生命周期、migration 13/14 和 LE00-LE59 |
| [docs/v2/V2_10_LLM_HYPOTHESES.md](./docs/v2/V2_10_LLM_HYPOTHESES.md) | V2-10 研究事实清单、严格 JSON 假设、确定性验证、候选桥接、migration 15 和 LL00-LL49 |
| [docs/v2/V2_11_REPORT_UI.md](./docs/v2/V2_11_REPORT_UI.md) | V2-11 展示输入、ReportDocument、Tab1/Tab3、历史评估、进度、导出、migration 16 和 UX00-UX59 |
| `AGENTS.md` | Codex 本地工作约定，被 `.gitignore` 忽略但保留在根目录 |
| `CLAUDE.md` | Claude Code 本地工作约定，被 `.gitignore` 忽略但保留在根目录 |

## 常用命令

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

完整测试：

```bash
venv/bin/python -m pytest tests/ -q
```

2.0 分层测试会逐步放在：

```text
tests/v2/
```

V2-9 已完成并复审：LE00-LE59 已逐号映射为 60 个独立行为测试，并补充真实 V2-5→V2-8 成交/组合链、截止日、冲突、revision 链、全链身份闭合和回滚测试。学习专项 `99 passed`，V2 全量 `541 passed, 3 skipped`，项目全量 `801 passed, 3 skipped`；3 条真实 Provider 冒烟显式启用后 `3 passed in 30.33s`。实现严格停止在 V2-9 学习层。

V2-10 已完成并复审：研究事实只能来自注册命名空间和同市场冻结 artifact，Tab3 按持仓优先稳定分片并限制模型只能回答当前分片标的；严格 JSON Schema、V2-5 同源三值验证、股票/行业/市场候选作用域绑定、V2-9 maturity/forecast/promotion 复盘和 migration 15 引用闭合均已补齐。LL00-LL49 一编号一行为测试，真实 Provider 与真实 LLM 冒烟均已显式执行通过。

V2-11 精确设计已冻结：主报告改为确定性的 `PresentationInput -> ReportDocument -> Flet/Markdown/HTML/PDF` 链路，禁止 LLM 写整篇报告或 UI 重新计算业务结论。规范固定了 Tab1/Tab3 全宽交互、天气预报式预测追踪、三本账历史评估、持仓行内编辑、逐股进度、历史报告、设置、migration 16、UX00-UX59 和双市场视觉验收；实现完成后必须停在 V2-11 等待复审。

## 历史资料

1.x 的 README、设计文档、升级计划、回测说明和优化审计已归档到 `docs/archive/v1/`。这些文件只用于追溯，不代表 2.0 当前设计。
