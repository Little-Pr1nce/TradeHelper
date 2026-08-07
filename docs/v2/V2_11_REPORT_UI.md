# TradeHelper V2-11 报告与 UI 层精确设计

> 状态：已完成并复审。本文是 V2-11 的规范性合同，优先级高于 `V2_REFACTOR_PLAN.md` 中的概念示例。实现建立在已完成并复审的 V2-1 至 V2-10 冻结合同之上。V2-11 只负责应用读模型、结构化报告、历史评估展示、任务进度、Tab1/Tab3/历史报告/设置页面和 HTML/PDF 导出；不得提前完成 V1 正式数据迁移、完整生产端到端接线、安装包发布或券商自动下单，这些属于 V2-12。

## 1. 阶段目标

V2-11 必须把 V2-0 至 V2-10 已经形成的可信事实和决策，转换成普通用户能够快速理解、可追溯、可重复渲染的交易工作台。

系统页面和报告必须稳定回答：

1. 现在应该买入、加仓、减仓、卖出、持有还是观察？
2. 如果现在不能操作，满足什么条件后可以操作？
3. 如果判断错误，止损在哪里、最大计划亏损是多少、计划何时失效？
4. 预测针对哪个目标交易日，上涨/震荡/下跌概率和收益区间是什么？
5. 预测、策略和最终联合决策过去分别表现如何，样本是否足够？
6. LLM 研究员提出了什么，系统确认、反驳、待验证或因数据无效无法判断的原因是什么？

核心原则：

```text
冻结业务事实
  -> PresentationInput
    -> deterministic ReportDocument / ViewModel
      -> Flet UI / Markdown / HTML / PDF

页面和渲染器只解释既有结果，不重新预测、不重新选策略、不重新计算风险。
```

V2-11 不是“让 LLM 写一篇更漂亮的报告”。主报告的结构、动作、条件、金额、指标和解释全部由代码确定性生成。V2-10 的结构化研究假设只作为独立章节展示，LLM 失败不能阻断确定性报告。

## 2. 固定展示链路

### 2.1 单股展示链路

```text
Instrument / Mode / Period
  + DataQualityReport / StockMetadata / Quote / FeatureSnapshot
  + ForecastResult[1/3/5/10]
  + TradingScenario
  + StrategyBundle
  + RiskDecisionBundle
  + OrderIntentBundle
  + LearningEvidence / Outcomes / Metrics
  + ResearchHypotheses / Validations / Outcomes
    -> SingleStockPresentationInput
      -> SingleStockReportBuilder
        -> ReportDocument
          -> Tab1 ViewModel / Markdown / HTML / PDF
```

### 2.2 组合展示链路

```text
AccountSnapshot
  + FrozenAccountValuation
  + PortfolioDecisionBundle
  + per-instrument SingleStockPresentationInput[]
  + WatchlistSnapshot
  + portfolio learning/research evidence
    -> PortfolioPresentationInput
      -> PortfolioReportBuilder
        -> ReportDocument
          -> Tab3 ViewModel / Markdown / HTML / PDF
```

### 2.3 历史评估链路

```text
ForecastOutcome / StrategyOutcome / JointOutcome
  + MetricSnapshot / CalibrationBin / RegimeSlice
  + ResearchOutcome / CandidateResearchOutcome
    -> HistoricalEvaluationQuery
      -> HistoricalEvaluationService
        -> HistoricalEvaluationView
          -> charts + tables + glossary + sample warnings
```

### 2.4 历史报告链路

```text
persisted ReportSnapshot
  + ReportFeedback[]
  + ReportExportArtifact[]
    -> ReportHistoryQuery
      -> history list
        -> full-width immutable report detail
```

打开旧报告不得触发重新抓取数据、重新预测、重新计算策略或重跑 LLM。旧报告显示的是当时冻结的报告快照。

### 2.5 可信报告的自然阅读顺序（2026-07-20 补充冻结）

Tab1 与 Tab3 的主报告必须按以下顺序逐层建立信任：

1. 基本信息与数据核对：公司、代码、市场、最新完成交易日、本次分析价格、价格来源；Tab3 还包括真实持仓、成本、现金和组合仓位。
2. 未来走势预测：明确未来几个交易日、目标日期、上涨/震荡/下跌概率、预计收益范围和通俗理由。
3. 策略选择与过去表现：说明预测形成了什么行情情景、采用哪种交易思路，并同时展示累计净收益、盈利次数、最大回撤和同期买入持有。
4. 保守与激进操作计划：当前动作、数量、触发条件、判断错误的退出位置、盈利处理、最大计划亏损和有效期。
5. 研究员观察：LLM 观点与代码验证结果独立展示，不能覆盖正式动作。
6. 最终结论：用普通中文汇总现在做什么、等待什么、哪里退出。
7. 历史可信度：最后分开核对预测是否准确、策略是否赚钱、完整链路是否有效；英文统计名只允许作为展开后的技术说明。

章节顺序不能被 renderer 或 LLM 改写。没有历史策略回放时必须直说“规则候选，尚未证明正期望”；新闻/基本面本次未取得时与行情完整度、预测模型质量分开表达。

## 3. 阶段边界

### 3.1 V2-11 负责

- 定义不可变 PresentationInput、ReportDocument、图表、表格、任务进度、历史报告和反馈合同。
- 使用已有 V2 repository 读取冻结 artifact，构建面向展示的读模型。
- 确定性生成单股报告和组合报告。
- 提供 Markdown、HTML、PDF 三种一致渲染。
- 实现 Tab1、历史报告、Tab3 和设置页的 V2 页面。
- 实现持仓和关注列表编辑，保存新的不可变账户/关注列表快照。
- 实现预测账、策略账、联合账和 LLM 独立账的历史评估页面。
- 实现分阶段进度、逐股进度、取消和后台学习状态展示。
- 实现报告检索、评分、比较、软归档和导出记录。
- migration 16、幂等持久化、强类型恢复和双市场展示测试。
- 对 1280×800、900×700 和 390×844 三种视口做可验证的布局检查。
- 页面必须能使用注入式 application ports 和已有冻结数据库 artifact 实际运行，不能只提交空页面、静态截图或无行为占位组件。

### 3.2 V2-11 不负责

- 修改 ForecastResult、TradingScenario、TradePlan、ExecutionDecision、OrderIntent 或 PortfolioDecisionBundle。
- 根据页面需要另写一套策略、仓位、风险收益比或预测计算。
- 将 LLM 自由文本作为报告主体，或让 LLM 决定执行动作和报告章节。
- 正式迁移 V1 数据库、V1 报告历史和 V1 用户配置。
- 完成所有真实 Provider 到完整页面的一次性生产编排；V2-12 负责最终 composition root 和端到端接线。
- macOS/Windows 安装包、GitHub Actions 发布和包体积优化。
- 自动下单、券商持仓同步、Level2 盘口或无证据的成交保证。
- Web 版正式发布；只要求 Flet 页面具备响应式约束和 390px 视口不破版。

## 4. 代码组织

建议按职责建立以下结构。允许在不破坏边界的前提下合并过小文件，但不得把报告、UI、数据库查询和业务计算重新塞回一个巨型 service。

```text

  contracts/
    presentation.py          # PresentationInput、ReportDocument、历史/任务合同

  application/
    ports.py                 # 注入式读模型、时钟、任务和导出端口
    tasks.py                 # 分阶段任务进度、取消和后台任务协调
    single_stock.py          # Tab1 展示输入编排，不重算业务事实
    portfolio.py             # Tab3 展示输入编排，不退化为 Tab1 批量版
    evaluation.py            # 三本账和 LLM 独立账读模型
    history.py               # 报告快照、反馈、比较和软归档
    settings.py              # V2Settings 能力校验和脱敏展示

  presentation/
    inputs.py                # 冻结输入构建和身份闭合
    report_builder.py        # ReportDocument 入口
    formatting.py            # Decimal、比例、日期、币种、缺失值
    reasons.py               # reason code -> 中文解释
    glossary.py              # 指标定义和阅读方法
    charts.py                # ChartSpec 和无全局状态图表生成
    sections/
      overview.py
      forecast.py
      plans.py
      risk.py
      facts.py
      evidence.py
      research.py
      evaluation.py
      glossary.py
    renderers/
      markdown.py
      html.py
      pdf.py

  ui/
    app.py
    pages/
      single_stock.py
      report_history.py
      portfolio.py
      settings.py
    components/
      action_desk.py
      forecast_table.py
      plan_table.py
      evaluation_charts.py
      progress_panel.py
      holding_editor.py
      watchlist_editor.py
      report_view.py

  data/
    migrations/schema.py     # migration 16
    repository.py            # report/watchlist/feedback/export + 读模型查询

tests/v2/
  presentation_helpers.py
  test_presentation_contracts.py
  test_presentation_inputs.py
  test_report_sections.py
  test_report_readability.py
  test_evaluation_views.py
  test_report_renderers.py
  test_report_repository.py
  test_task_progress.py
  test_ui_state_flow.py
  test_portfolio_editor_flow.py
  test_report_history_flow.py
  test_settings_flow.py
  test_presentation_architecture.py
  test_presentation_performance.py
  test_presentation_golden_cases.py
```

禁止：

- 从 `presentation/`、`ui/`、renderers 直接调用网络 Provider。
- 从 UI 直接拼 SQL 或跨表推导业务状态。
- 从报告字符串反向解析股票、动作、金额或模型状态。
- 复制 V1 `report/generator.py` 的“LLM 生成全文后正则替换章节”模式。
- V2 主链 import V1 `ui/`、`report/`、`services/` 或 `core/`。

## 5. 核心合同

所有合同使用冻结 dataclass、显式枚举、UTC 时间和稳定序列化。金额、价格、股数计算结果保持 `Decimal`；展示时才格式化。

### 5.1 PresentationInput

```text
SingleStockPresentationInput:
  presentation_id
  instrument
  analysis_mode
  as_of
  history_period
  metadata
  quote_snapshot?
  data_quality
  feature_snapshot
  forecasts[1d, 3d, 5d, 10d]
  scenario
  strategy_bundle
  risk_bundle
  order_intent_bundle
  learning_evidence[]
  forecast_outcomes[]
  strategy_outcomes[]
  joint_outcomes[]
  metric_snapshots[]
  research_hypotheses[]
  research_validations[]
  research_outcomes[]
  news_summary?
  fundamental_summary?
  source_artifact_refs[]
  built_at

PortfolioPresentationInput:
  presentation_id
  market
  analysis_mode
  as_of
  history_period
  account_snapshot
  frozen_account_valuation
  portfolio_decision_bundle
  instruments[]
  watchlist_snapshot
  portfolio_learning_evidence[]
  portfolio_research_evidence[]
  source_artifact_refs[]
  built_at
```

身份规则：

1. 单股输入的 instrument、market、analysis_mode、as_of 必须与所有上游 artifact 一致。
2. ForecastResult 必须覆盖规范要求的 1/3/5/10 日周期；缺失周期保留结构化 unavailable，不得伪造概率。
3. scenario、strategy、risk、order 必须通过各自已有 source ID 闭合到同一条决策链。
4. 组合输入必须使用同一 `AccountSnapshot + FrozenAccountValuation + PortfolioDecisionBundle`。
5. 组合中的单股输入必须属于同一市场和同一批次截止时点。
6. `source_artifact_refs` 必须覆盖报告中所有动作、金额、概率、结论和研究观察的来源。
7. 输入构建后不允许 renderer 查询数据库补字段。

### 5.2 ReportDocument

```text
ReportDocument:
  report_id
  report_kind               # single_stock / portfolio
  market
  instrument?
  analysis_mode
  as_of
  title
  subtitle
  summary
  sections[]
  glossary_entries[]
  source_artifact_refs[]
  schema_version
  renderer_version
  generated_at

ReportSection:
  section_id
  title
  purpose
  severity?
  blocks[]

ReportBlock:
  kind                      # text / callout / metric / table / chart / divider
  payload

ReportTable:
  table_id
  title
  columns[]
  rows[]
  empty_state?
  interpretation?

ReportTableRow:
  row_id
  cells[]
  severity?
  source_artifact_refs[]

ChartSpec:
  chart_id
  chart_kind
  title
  x_axis
  y_axis
  series[]
  baseline[]
  sample_count
  sample_range?
  interpretation
  empty_state?

MetricDefinition:
  metric_key
  display_name
  plain_language_definition
  preferred_direction
  minimum_sample_guidance
  unit?
```

`ReportDocument` 是产品合同，Markdown/HTML/PDF 只是渲染结果。相同输入、相同 builder 版本必须得到相同 canonical document；`generated_at`、导出路径等非业务字段不得污染业务身份。

`ReportDocument.generated_at` 必须来自 `PresentationInput.built_at` 或注入式冻结时钟，builder 内部不得直接调用当前时间。canonical document hash 排除导出路径，但不允许排除动作、数值、状态、解释、样本数或来源引用。

### 5.3 ReportSnapshot、反馈和导出

```text
ReportSnapshot:
  report_id
  report_document_json
  document_hash
  report_kind
  market
  instrument?
  analysis_mode
  as_of
  source_artifact_refs[]
  renderer_version
  archived
  created_at

ReportFeedback:
  feedback_id
  report_id
  rating                    # 1..5
  note?
  created_at

ReportExportArtifact:
  export_id
  report_id
  format                    # markdown / html / pdf
  path
  content_hash
  status
  error_code?
  created_at

WatchlistSnapshot:
  watchlist_id
  market
  instruments[]
  created_at
```

规则：

- 报告快照不可原地修改；重跑分析生成新 report。
- 评分采用 append-only；最新评分用于筛选，历史评分保留。
- “删除报告”只设置 `archived=true`，不得删除预测、策略、学习或研究证据。
- 导出失败不影响已保存报告，也不得留下成功状态的空文件记录。
- 关注列表采用不可变快照；最新快照是当前状态，旧快照用于追溯。

### 5.4 任务进度

```text
AnalysisStage:
  validate_input
  resolve_subject
  refresh_metadata
  refresh_market_data
  build_features
  forecast
  scenario
  strategy
  risk
  execution_preview
  portfolio_allocation
  research
  learning_update
  build_report
  persist_report
  completed

TaskStatus:
  queued / running / waiting / cancelling / cancelled / completed / failed

AnalysisTaskProgress:
  task_id
  stage
  status
  completed_units
  total_units
  instrument?
  message_code
  elapsed_seconds
  retry_at?
  cancellable
  background
  emitted_at
```

进度规则：

1. 前台收到命令后 250ms 内必须产生首个状态事件。
2. 同一任务的完成比例不得倒退，也不能在实际工作未完成时显示 100%。
3. Tab3 必须显示当前股票和已完成/总股票数。
4. Provider 限频显示等待原因和预计重试时间，不能只显示“正在分析”。
5. 取消在阶段边界和逐股边界协作执行；取消任务不得保存不完整报告。
6. 深度 OOF、候选优化和到期批处理为后台任务，不阻塞本次前台报告。
7. LLM 研究失败时，研究阶段标记 unavailable，确定性报告继续完成。

### 5.5 历史评估和查询合同

```text
HistoricalEvaluationQuery:
  market
  ledger_kind?              # forecast / strategy / joint / research
  instrument?
  horizon?
  model_version?
  strategy_id?
  market_regime_key?
  evidence_origin?
  date_from?
  date_to?
  include_unverifiable

HistoricalEvaluationView:
  query
  maturity_summary
  headline_metrics[]
  charts[]
  tables[]
  glossary_entries[]
  warnings[]
  source_artifact_refs[]
  built_at

ReportHistoryQuery:
  report_kind?
  market?
  instrument?
  analysis_mode?
  history_period?
  date_from?
  date_to?
  minimum_rating?
  include_archived
  page
  page_size

ReportHistoryPage:
  query
  items[]
  total_count
  has_next
```

查询必须通过 repository/application read model 完成。UI 只提交查询对象，不得自己遍历数据库 payload 或解释 outcome 状态。

### 5.6 固定枚举和原因代码

```text
ReportKind:
  single_stock / portfolio

ReportBlockKind:
  text / callout / metric / table / chart / divider

ChartKind:
  calibration / forecast_timeline / cumulative_performance / drawdown

ExportFormat:
  markdown / html / pdf

ExportStatus:
  pending / completed / failed

ReportSeverity:
  info / positive / warning / danger / unavailable
```

至少注册以下展示原因代码；未知上游 reason code 仍须保留原 code 和技术详情：

```text
PRESENTATION_IDENTITY_MISMATCH
PRESENTATION_SOURCE_MISSING
PRESENTATION_TARGET_DATE_INVALID
PRESENTATION_CURRENCY_MISMATCH
REPORT_MODEL_SAMPLE_INSUFFICIENT
REPORT_MODEL_UNDERPERFORMED_BASELINE
REPORT_MODEL_DATA_QUALITY_BLOCKED
REPORT_MODEL_DRIFTED
REPORT_MODEL_CONFIRMATION_PENDING
REPORT_TAKE_PROFIT_UNAVAILABLE
REPORT_HISTORY_SAMPLE_INSUFFICIENT
REPORT_HISTORY_UNAVAILABLE
REPORT_RESEARCH_UNAVAILABLE
REPORT_DATA_FIELD_MISSING
REPORT_PORTFOLIO_VALUATION_INCOMPLETE
REPORT_EXPORT_FAILED
REPORT_ARCHIVED
TASK_RATE_LIMIT_WAITING
TASK_CANCELLED
TASK_STAGE_FAILED
SETTINGS_CAPABILITY_UNAVAILABLE
```

## 6. 确定性报告规则

### 6.1 数据来源

- 当前动作只能来自 `ExecutionDecision` 或 `PortfolioAllocation`。
- 条件只能来自冻结 `TradePlan.ConditionExpression`。
- 当前订单预览只能来自 `OrderIntentBundle`。
- 最大亏损、仓位、股数和账户比例只能来自 V2-6/V2-8 的真实账户计算。
- 预测概率、收益区间、目标交易日和模型状态只能来自 ForecastResult/registry evidence。
- 历史结果只能来自 V2-9 已成熟 outcome/metric。
- LLM 观察只能来自 V2-10 hypothesis/validation/outcome，不得改写正式动作。

### 6.2 缺失和降级

- 缺失显示“暂无可靠数据”并给出原因代码的通俗解释，不能显示 0。
- 数据质量按股票隔离；组合内单股缺失不能污染其他股票。
- 没有止盈目标时，风险收益比显示“不可量化”，不得声称“风险收益比优秀”。
- 没有 Champion 时必须区分：
  - 样本不足；
  - 已评估但未跑赢基线；
  - 数据质量不足；
  - 模型失效或漂移；
  - 尚未到确认窗口。
- “未通过 OOF”必须同时显示原因、对执行的影响和下一步：

```text
预测可查看，但尚未通过样本外确认，因此不参与新开仓执行分级。
系统将在样本成熟或下一确认窗口重新评估。
```

这不等于“行情数据缺失”。

### 6.3 保守与激进

保守与激进方案可以共享同一触发价格，因为市场事实和预测方向必须一致。报告必须明确说明差别来自确认门槛、风险预算、批准股数或仓位，而不能为了视觉差异发明第二套买卖价格。

### 6.4 历史证据与当前动作

“历史上最优资产/策略”不等于“当前应买入或持有”。报告必须分开表达：

```text
历史证据：WDC 在已验证窗口中相对表现较好。
当前决定：当前持仓触发风险退出条件，因此本次仍建议减仓/卖出。
```

禁止把回测横向排名写成当前交易建议。

### 6.5 百分比和货币

- 概率、收益、Alpha、回撤等全部明确使用 `%`，例如 `+2.4%`。
- “收益中位数 80%”这类可能把分位数和收益率混淆的文字禁止出现。
- P10/P50/P90 必须显示为“预测收益区间分位”，不是置信度。
- 金额显示币种；A股 CNY、美股 USD，禁止跨币种合计为一个数字。
- 仓位按冻结估值计算，股票市值/账户权益不得超过 100%；若输入本身不闭合，显示估值错误并阻断组合摘要。

## 7. 固定报告结构

### 7.1 单股报告

1. **一分钟操作台**
   - 当前动作、执行等级、是否当前可执行。
   - 最重要的触发条件、止损/失效、最大计划亏损。
   - 数据时点、市场会话和计划有效期。
2. **独立市场预测**
   - 1/3/5/10 日目标交易日。
   - 上涨/震荡/下跌概率。
   - P10/P50/P90 收益区间。
   - Champion/Challenger/基线状态、OOF 状态和解释。
3. **当前与条件交易计划**
   - 买入/加仓、卖出/减仓、持有、失效四分支。
   - 保守与激进方案及差异说明。
4. **风险金额与风控结论**
   - 真实账户基础、批准股数/仓位、止损、最大亏损、集中度和规则限制。
5. **当前事实**
   - 价格、来源、时间、会话、数据质量。
   - 技术、新闻、基本面摘要；缺失原因明确。
6. **情景与策略证据**
   - 预测如何形成情景，情景如何选择策略家族。
   - 当前触发、待满足、冲突和不适用条件。
7. **研究员观察与系统验证**
   - `confirmed / refuted / pending / invalid_data` 全部可见。
   - 候选资格、是否进入 OOF、历史结果。
8. **历史验证与系统追踪**
   - 预测账、策略账、联合账分开。
   - 清楚展示何时预测、预测哪个目标日、实际结果和对错。
9. **术语和阅读方法**
   - 指标定义、样本阈值和“怎么看”。

### 7.2 组合报告

1. **组合概览**
   - 冻结账户权益、现金、持仓市值、总仓位、计划风险。
   - 币种和估值时点。
2. **逐股价格与关键事实**
   - 公司/代码、持仓或关注身份、完成日 K 日期、实际分析价和 Provider 来源。
   - 持仓成本盈亏、组合仓位及现价相对 MA20/60/120 的位置；盘后不得因 QuoteSnapshot 不适用而隐藏完成日 K 收盘价。
3. **今日优先处理**
   - 保护退出优先，再是持有管理和新增风险。
   - 每个动作包含股票、分析价、持仓上下文、策略家族、触发事实、股数和执行安排。
4. **保守与激进组合方案**
   - 分开显示最终分配、现金、heat 和集中度。
   - 保护线已被价格越过时必须显示“先退出/减仓”，不能误写为行情或历史样本不足。
5. **持仓风险表**
   - 数量、成本、现价、浮盈亏、集中度、可卖量、止损/锁利/禁止加仓。
6. **条件触发计划**
   - 每股一行展示当前结论、买/加、卖/减、持有、失效和双 profile；未持有股票不展开无意义的“不适用”退出行。
7. **关注股与替换机会**
   - 替换只表示研究/下一轮重分析候选，不能暗示已自动卖旧买新。
8. **逐股数据质量**
   - 来源、新鲜度、缺失能力和是否阻断该股。
9. **组合成分股独立预测**
   - 每股 1/3/5/10 日预测和模型验证状态；必须明确“行情完整”和“模型通过 OOF”是两项独立结论。
10. **研究员观察与系统验证**
   - 显示计划/实际分片数、成功数、失败原因、合规观察和系统验证状态；空结果、截断、结构拒绝和未配置不得混为一个状态。
11. **历史能力评估**
12. **术语和阅读方法**

第一页/首屏必须先显示可执行摘要。长技术说明、策略审计和指标细节放后面，不能让用户先穿过数页技术表格才能看到动作。

## 8. 天气预报式预测与追踪表

### 8.1 当前预测表

每一行至少包括：

| 字段 | 含义 |
|------|------|
| 股票 | 公司名和代码 |
| 预测时间 | 系统发行预测的时间 |
| 参考价 | 预测发行时可见的价格及来源 |
| 周期 | 1/3/5/10 个交易日 |
| 目标交易日 | 预测针对的具体日期 |
| 上涨/震荡/下跌概率 | 三类概率，总和为 100% |
| 预测收益区间 | P10/P50/P90 |
| 模型状态 | Champion/Challenger/基线及 OOF 状态 |
| 执行影响 | 是否参与情景/新开仓分级 |

“分离度”定义为最高方向概率减去第二高方向概率，只表示模型决断程度，不表示预测准确率。

### 8.2 已验证预测表

每一行至少包括：

| 字段 | 含义 |
|------|------|
| 股票 | 公司名和代码 |
| 预测时间 | 当时何时做出预测 |
| 参考价 | 当时价格 |
| 目标交易日 | 当时预测的是哪一天 |
| 主要预测 | 当时最可能方向和概率 |
| 预测收益区间 | 当时 P10/P50/P90 |
| 实际日期 | 真正用于验证的交易日 |
| 实际价格/收益 | 目标日结果 |
| 结果 | 正确/错误/待验证/不可验证 |
| 证据说明 | 数据来源、修订和不可验证原因 |

目标日未到时只能是“待验证”。目标日数据缺失、停牌、公司行动无法归一或证据冲突时是“不可验证”。禁止出现“7 月 5 日预测 7 月 2 日”这种发行时间晚于目标日的记录；发现身份倒置时必须拒绝构建展示输入。

只有 1 条成熟记录时，表格上方必须显示：

```text
当前仅有 1 条已验证样本，不能据此判断模型稳定性。
```

## 9. 历史评估

### 9.1 样本成熟度

| 成熟样本数 | 展示结论 |
|-----------|----------|
| 0 | 暂无已到期记录 |
| 1-9 | 样本积累中，不评价可靠性 |
| 10-29 | 可作观察，不允许模型优劣定论 |
| >=30 | 可进行模型比较，但仍需结合分层、区间和回撤 |

所有表格和图表必须显示样本数。样本不足时不能用颜色或措辞暗示已证明正期望。

### 9.2 固定图表

1. **概率校准曲线**
   - 横轴：预测置信度。
   - 纵轴：实际发生频率。
   - 对角线：理想校准基线。
   - 每个分箱显示样本数。
   - 一句话解释曲线在基线上方/下方的含义。
2. **预测结果时间线**
   - 横轴：目标交易日。
   - 预测 P50 为主线，P10-P90 为区间带。

主报告的历史可信度只展示按股票冻结的汇总和最近 20 条已验证预测。原始策略/联合事件保留在历史评估审计视图，不得在 Tab1/Tab3 主报告中展开数百行，也不得把同市场其他股票的联合结果归给当前股票。
   - 实际收益或价格为对照线/点。
   - 每个点可追溯到 forecast/outcome ID。
3. **策略/联合 OOF 累计表现**
   - 仅在 OOF 样本有效时显示。
   - 同时显示基准、累计收益和回撤。
   - 无有效 OOF 时显示空状态，不画伪曲线。

图表不得依赖可变的全局 `matplotlib.pyplot` 状态；同输入必须生成稳定 ChartSpec 和内容哈希。

### 9.3 固定表格

- 预测表现：按市场、股票、周期、模型和市场状态分层。
- 概率校准分箱。
- 策略结果：触发、成交、收益、最大不利波动、止盈/止损。
- 联合 OOF：预测 + 策略 + 风控 + 组合后的最终结果。
- 分周期联合结果。
- 逐事件审计：预测了什么、目标是哪天、实际如何。
- LLM 研究表现：独立显示，不能混入正式系统命中率。

### 9.4 指标解释

| 指标 | 通俗解释 | 阅读方向 |
|------|----------|----------|
| Brier | 概率预测和真实结果的平方误差 | 越低越好 |
| Log Loss | 对“非常自信但预测错误”惩罚更重 | 越低越好 |
| ECE | 模型说 70% 时，实际是否接近 70% | 越低越好 |
| 80% 区间命中 | 实际结果落入 P10-P90 区间的比例 | 样本充分后应接近 80% |
| Alpha | 相对基准的超额收益 | 正值更好，但需结合成本和样本 |
| Sharpe | 每单位波动获得的收益 | 越高通常越好，需结合回撤和样本 |
| 最大回撤 | 从阶段高点到低点的最大损失 | 绝对值越小越稳健 |
| Champion | 当前通过规则选择的生产模型 | 不代表永久最好 |
| 分离度 | 最高概率减第二高概率 | 越高越明确，不代表越准确 |
| OOF | 样本外验证，模拟当时未知未来的预测 | 未通过不等于数据一定缺失 |

## 10. Tab1 单股页面

### 10.1 输入状态

- 市场使用分段控件：美股 / A股。
- 股票输入支持代码和公司名联想，A股与美股流程一致。
- 联想输入防抖约 300ms，不得每次按键都请求远程 Provider。
- 分析模式明确为盘前、盘中、盘后。
- 回看周期使用选项控件，不允许自由文本造成非法值。
- “开始分析”为明确命令；运行后提供取消。
- 输入校验错误就地显示，不弹出不可追溯的通用错误。

### 10.2 结果状态

- 进入结果后报告占用主内容区全宽，不保留浪费空间的固定输入侧栏。
- 顶部保留返回/修改条件、重新分析、导出等命令。
- 一分钟操作台和预测表位于首屏。
- 详细章节使用页内导航或折叠目录，但不能把核心动作藏在折叠区。
- 重新分析保留上一次市场、股票、模式和周期输入。
- 研究员不可用只影响研究章节。

## 11. Tab3 组合页面

### 11.1 页面定位

Tab3 是组合级交易工作台，不是 Tab1 批量报告。输入态使用三个子视图：

1. 账户与持仓。
2. 关注列表。
3. 历史评估。

分析完成后进入全宽组合报告，不使用“左半边输入、右半边报告”的永久布局。

### 11.2 持仓编辑

- 每条已有持仓只有一个“编辑”入口，不重复提供语义重叠的“调整”。
- 可直接修改持股数量和成本价。
- 保存生成新的 `AccountSnapshot`，旧快照保持不变。
- 数量必须大于 0，成本价不得小于 0。
- 录入已有 A股持仓不强制整手，因为用户可能持有历史零股；下单规则仍由 V2-6/V2-7 校验。
- 删除表示从最新快照移除，不删除历史账户记录。
- 现金、持仓市值、权益和币种必须可见，禁止默认 10 万元。

### 11.3 关注列表

- 同一市场内唯一。
- 已持有股票不能同时作为普通关注股；系统应提示已在持仓中。
- 添加和删除生成新 `WatchlistSnapshot`。
- 股票检索与 Tab1 共享同一 lookup 服务，但不依赖 Tab1 曾经运行。

### 11.4 组合报告

- 保护退出和集中度风险优先排序。
- 组合总仓位使用冻结账户估值，必须闭合到 0%-100%。
- conservative/aggressive 分别显示最终分配，不能只显示单股最大批准量。
- 某一股票限频或缺数据时，显示该股等待/降级，其他股票继续完成。
- 跨市场账户分开运行；没有显式 FX 时禁止把 USD/CNY 合并为一个权益。
- 所有用户可见时间必须带“北京时间”或“美东时间”，HTML/Markdown/PDF 不得暴露原始 UTC ISO 字符串。
- 组合 LLM 研究按稳定股票分片后台运行，修订报告必须显示调用摘要；LLM 假设仍不能改写确定性动作。

## 12. 历史报告、比较和评分

- 默认列表展示时间、报告类型、市场、股票/组合、模式、数据时点、最新评分。
- 支持按市场、报告类型、股票、模式、周期、日期和评分筛选。
- 打开详情进入全宽阅读模式，返回后保留筛选条件和滚动位置。
- 最多比较 3 份报告，且必须是同报告类型和同市场。
- 比较展示冻结动作、预测、风险、数据质量和后续成熟结果的变化。
- 评分 1-5 分，可选备注；修改评分新增一条反馈记录。
- 软归档的报告默认隐藏，可通过筛选查看和恢复。

## 13. 设置页

设置页消费现有 `V2Settings`，不得另建第二套配置文件。

能力校验按功能和市场进行，不能用“所有 token 都必须存在”的全局开关：

| 场景 | 必需能力 |
|------|----------|
| 美股盘后 | Nasdaq 历史或可用 fallback；不要求 TickFlow 实时 token |
| 美股盘前/延伸时段 | Nasdaq.com 或 yfinance fallback |
| 美股盘中 | 美股 TickFlow 实时 |
| A股盘后 | A股 TickFlow 日 K |
| A股盘中 | A股 TickFlow 实时 |
| LLM 研究 | LLM 配置；缺失时只禁用研究章节 |

规则：

- token、密码和代理认证信息始终掩码显示。
- secret 不得进入报告、Prompt、日志、反馈或导出文件。
- 保存前可做格式和能力检查，但真实联网测试必须由用户显式触发。
- 数据库已打开后修改工作目录，需要提示重启或显式重新打开，不能静默让两个目录混用。

## 14. 渲染和导出

### 14.1 一致性

Markdown、HTML、PDF 和 Flet 页面必须消费同一 `ReportDocument`。允许布局适配，不允许改变动作、概率、金额、状态和样本结论。

### 14.2 HTML

- 单文件、自包含样式和图表资源。
- 表格在窄视口可横向滚动或转为标签行，不能截断关键列。
- 不依赖外部 CDN 才能阅读。
- 不嵌入 secret、工作目录或内部数据库路径。

### 14.3 PDF

- 支持中文字体 fallback。
- 多页表格重复表头。
- 长股票名、原因和条件自动换行。
- 不显示原始 Markdown 标记或 HTML 标签。
- 图表标题、轴、图例和样本说明清晰。

### 14.4 文件名

文件名必须清理非法字符，并至少包含：

```text
report_kind + market + instrument_or_portfolio + analysis_mode + as_of
```

## 15. UI 视觉和可用性约束

- 导航顺序保持用户认知：单股分析、历史报告、我的持仓、设置。
- 使用 Flet 内置图标和 tooltip；不使用 emoji 作为结构性图标。
- 模式使用 segmented control 或明确选项控件。
- 页面区块使用无框全宽布局；卡片仅用于重复条目、弹窗或真正需要边界的工具。
- 不使用卡片套卡片、装饰性渐变球、过大标题或营销式首屏。
- 输入框、按钮和表格列有稳定尺寸约束，状态变化不能推动布局跳动。
- 1280×800、900×700、390×844 均不得出现文本重叠、按钮截断或报告只占半屏。
- 390px 视口允许表格横向滚动，但首要动作、风险和目标日期必须无需横向滚动即可看到。

## 16. 性能预算

以下预算只测冻结输入后的本地展示计算，不包含 Provider、LLM 或完整上游决策耗时：

| 操作 | 预算 |
|------|------|
| 首个任务进度事件 | < 250ms |
| 单股 ReportDocument 构建 | < 0.5s |
| 50 股票组合 ReportDocument 构建 | < 1.5s |
| 标准 HTML 导出 | < 2s |
| 标准 PDF 导出 | < 5s |
| 历史列表 1000 条本地筛选/分页 | < 0.5s |

完整交互性能仍遵守总计划：缓存命中 Tab1 确定性主链 p95 < 5 秒，10 股票 Tab3 确定性主链 p95 < 20 秒。该完整链路最终在 V2-12 验收。

## 17. migration 16

migration 16 至少新增：

```text
watchlist_snapshots
watchlist_snapshot_members
report_snapshots
report_feedback
report_exports
```

要求：

1. migration 幂等，可重复执行。
2. report/document hash 冲突进入既有 quarantine 机制，不覆盖原记录。
3. snapshot 与成员在同一事务保存。
4. repository 重启后恢复强类型对象并复核索引列。
5. report source refs 必须引用已存在 artifact，或明确记录为 external display fact。
6. 不需要持久化每个瞬时进度事件；任务进度默认是进程内状态。若实现者选择持久化任务摘要，必须单独说明用途并避免把半成品报告当成完成报告。

## 18. Golden Cases

至少实现以下固定案例：

1. **AAPL 空仓**：预测可用但未通过 OOF；显示预测、原因、执行影响和等待条件，不生成虚假买入。
2. **FCX 持仓**：当前减仓计划与未来重新加仓条件同时存在；明确它们是不同条件分支，不是自相矛盾。
3. **GLW 持仓**：创新高后锁利，展示部分减仓、锁利线、剩余持仓条件和失效。
4. **WDC 持仓**：历史证据较好但当前触发退出；历史排名不得覆盖当前风险动作。
5. **A股 T+1**：卖出计划因当日买入不可卖被市场规则阻断，报告保留保护意图和可执行日期。
6. **新股样本不足**：上市时间短导致预测/回测样本不足，显示从上市日起裁剪，不显示数据质量异常。
7. **新闻/基本面缺失**：保持缺失和 provider 原因，不填 0，不阻断不依赖该字段的退出计划。
8. **11 股票组合限频**：一只股票进入 waiting/retry，其余股票继续，页面显示逐股进度。
9. **四种研究状态**：confirmed/refuted/pending/invalid_data 全部可见。
10. **已验证预测**：明确发行时间、参考价、目标日、实际价、实际收益和对错。
11. **无止盈目标**：风险收益比为“不可量化”。
12. **零历史样本**：显示空状态和如何积累样本，不绘制伪曲线。
13. **同触发价双方案**：保守/激进共享触发价，但股数、风险和确认要求不同。
14. **A股与美股同功能**：股票联想、单股报告、组合报告、历史评估和导出均有双市场 fixture。

Golden Case 的预期结果必须先写在测试 fixture 中，不能根据实现输出反写标准答案。

## 19. UX00-UX59 验收矩阵

每个编号必须对应一个唯一命名行为测试；可以增加无编号补充测试，但不得一个测试同时冒充多个编号。

### UX00-UX09：合同与持久化

| 编号 | 行为 |
|------|------|
| UX00 | 单股 PresentationInput 全链身份一致时可构建 |
| UX01 | 混入跨市场或跨时点 artifact 时拒绝 |
| UX02 | 报告所有结论均能闭合到 source artifact refs |
| UX03 | 同输入和版本生成相同 canonical ReportDocument |
| UX04 | ReportSnapshot 幂等保存且不可原地修改 |
| UX05 | ReportFeedback append-only，不修改报告正文 |
| UX06 | WatchlistSnapshot 不可变且最新状态可恢复 |
| UX07 | ChartSpec 可稳定序列化并产生内容哈希 |
| UX08 | secret/工作目录/内部路径不会进入报告合同 |
| UX09 | migration 16 重复执行和 repository 重启恢复通过 |

### UX10-UX19：报告正确性

| 编号 | 行为 |
|------|------|
| UX10 | 一分钟操作台只消费 ExecutionDecision/Allocation |
| UX11 | 买/加、卖/减、持有、失效四分支完整展示 |
| UX12 | 止盈缺失时风险收益比显示不可量化 |
| UX13 | 预测表展示发行时间、目标日、概率和 P10/P50/P90 |
| UX14 | 未通过 OOF 同时展示原因、执行影响和下一步 |
| UX15 | 同触发价的保守/激进方案正确解释差异 |
| UX16 | 历史质量排名与当前动作分开，不互相覆盖 |
| UX17 | LLM 四种验证状态和候选资格完整可见 |
| UX18 | 缺失新闻/基本面显示缺失而不是 0 |
| UX19 | 未注册 reason code 仍显示可追溯技术详情 |

### UX20-UX29：历史评估

| 编号 | 行为 |
|------|------|
| UX20 | 0/1-9/10-29/>=30 样本阈值文案正确 |
| UX21 | Brier/Log Loss/ECE/区间命中解释和方向正确 |
| UX22 | 校准图含对角基线、轴名称和分箱样本数 |
| UX23 | 预测时间线将预测区间与正确实际 outcome 关联 |
| UX24 | 发行时间晚于目标日或实际日错位时拒绝展示 |
| UX25 | 单条成熟记录显示不能判断可靠性的警告 |
| UX26 | 市场状态分层保留 regime 和样本数 |
| UX27 | 预测账、策略账、联合账分开展示 |
| UX28 | LLM 研究指标独立，不混入正式系统命中率 |
| UX29 | 空历史数据提供可理解空状态且不画伪图 |

### UX30-UX39：任务和 Tab1

| 编号 | 行为 |
|------|------|
| UX30 | fake port 下首个进度事件在 250ms 内产生 |
| UX31 | 同一任务进度单调不倒退 |
| UX32 | 限频状态显示 retry_at 和当前股票 |
| UX33 | 取消任务不持久化不完整报告 |
| UX34 | 后台 OOF/优化不阻塞前台 ReportDocument |
| UX35 | Tab1 结果状态为全宽报告 |
| UX36 | A股/美股股票联想使用同一交互流程 |
| UX37 | 三时段按能力矩阵启用/降级 |
| UX38 | 重新分析保留上一次输入 |
| UX39 | LLM 不可用时确定性报告仍完成 |

### UX40-UX49：Tab3

| 编号 | 行为 |
|------|------|
| UX40 | 已有持仓只有一个行内编辑入口 |
| UX41 | 修改数量/成本保存为新 AccountSnapshot |
| UX42 | 删除持仓不删除历史快照 |
| UX43 | 关注列表唯一且与持仓不重叠 |
| UX44 | A股/CNY 与美股/USD 账户隔离 |
| UX45 | 冻结估值闭合，总仓位不超过 100% |
| UX46 | 保护退出在组合优先级中先于新增风险 |
| UX47 | 替换机会明确为研究/重分析候选 |
| UX48 | 单股数据质量异常只影响该股 |
| UX49 | Tab3 结果为全宽报告，不保留永久左右分栏 |

### UX50-UX59：历史、导出、设置和质量

| 编号 | 行为 |
|------|------|
| UX50 | 历史筛选和全宽详情不触发重分析 |
| UX51 | 评分修改新增反馈记录 |
| UX52 | 报告比较最多 3 份且限制同类型/市场 |
| UX53 | HTML 自包含且同 ReportDocument 结果稳定 |
| UX54 | PDF 中文、表头、换行和图表可读 |
| UX55 | 导出失败不丢报告且记录失败状态 |
| UX56 | 设置页 secret 脱敏且按功能校验能力 |
| UX57 | presentation/UI 无 V1 业务 import、无网络和业务重算 |
| UX58 | 冻结输入下报告/历史/导出满足性能预算 |
| UX59 | 双市场、三时段和 1280/900/390 视口烟雾通过 |

## 20. 测试与验收

阶段专项：

```bash
venv/bin/python -m pytest \
  tests/v2/test_presentation_contracts.py \
  tests/v2/test_presentation_inputs.py \
  tests/v2/test_report_sections.py \
  tests/v2/test_report_readability.py \
  tests/v2/test_evaluation_views.py \
  tests/v2/test_report_renderers.py \
  tests/v2/test_report_repository.py \
  tests/v2/test_task_progress.py \
  tests/v2/test_ui_state_flow.py \
  tests/v2/test_portfolio_editor_flow.py \
  tests/v2/test_report_history_flow.py \
  tests/v2/test_settings_flow.py \
  tests/v2/test_presentation_architecture.py \
  tests/v2/test_presentation_performance.py \
  tests/v2/test_presentation_golden_cases.py -q
```

全量回归：

```bash
venv/bin/python -m pytest tests/v2/ -q
venv/bin/python -m pytest tests/ -q
```

视觉验收：

1. 使用固定 fixture 启动 Flet 页面，不依赖网络。
2. 分别检查 1280×800、900×700、390×844。
3. 保存 Tab1 输入态、Tab1 结果态、Tab3 编辑态、Tab3 结果态、历史评估、PDF 页面的截图。
4. 检查非空渲染、首屏动作、文本换行、表格滚动、按钮可达和无重叠。

V2-11 完成标准：

1. UX00-UX59 一编号一行为全部通过。
2. 双市场 Golden Cases 全部通过。
3. migration 16、repository 重启和强类型恢复通过。
4. Markdown/HTML/PDF/Flet 对关键数字和状态一致。
5. 视觉验收无重叠、截断和永久半屏浪费。
6. V2 全量和项目全量测试通过。
7. 文档状态更新为“V2-11 已完成并复审”后停止，不得自动进入 V2-12。

## 21. 给实现者的停止条件

Terra 完成 V2-11 后必须停止并提交复审，不得顺手完成：

- V1 数据库正式迁移。
- V1 历史报告批量导入。
- 完整真实 Provider 端到端生产编排。
- macOS/Windows 安装包和 GitHub Release。
- Web 部署。
- 券商自动下单。

这些工作统一在 V2-12 设计和复审后实施。

## 22. 2026-08-06 可执行建议语义补充

- 报告必须严格区分“当前动作”和“条件满足后的计划”。等待买入显示“当前不买入”，等待保护退出显示“当前继续持有并监测退出条件”；只有 `executable_now=true` 才能展示本次可下单数量。
- 保守与激进方案分别选择各自风险档案中的最高优先级决策，并按精确 `plan_id` 读取组合分配。不得按股票代码模糊匹配，也不得把另一条卖出计划的股数借给持有计划。
- 价格触发、技术确认、成交量确认、数据要求、执行复核和有效期必须分行展示。RSI、均线、MACD、量比等条件标注“系统自动监测，用户无需计算”，并在存在可靠观测值时显示当前状态。
- 超出合理波动范围的触发价显示为“远期观察”，同时展示距当前价的比例，并明确要求到达附近后重新分析；不得继续使用买入色或写成可直接挂单条件。
- 在线预测表现低于简单基准时，报告保留预测概率供诊断，但必须用通俗中文说明“已暂停影响新开仓”，不能仅展示内部状态码。
