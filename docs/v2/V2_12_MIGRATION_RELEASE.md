# TradeHelper V2-12 迁移、端到端与发布精确设计

> 状态：代码完成并本机复审（2.0.0）；Windows 产物验收待对应 runner。本文是 TradeHelper 2.0 最后一个开发阶段的规范性合同，优先级高于 `V2_REFACTOR_PLAN.md` 中的概念示例。V2-12 已把 V2-1 至 V2-11 的层接成正式桌面应用，完成只读 V1 迁移审计、端到端离线验收、真实 Provider/LLM 验证和 macOS 包内 smoke。不得连接券商自动下单，不把 Web 部署纳入 2.0 发布门槛，也不得借迁移之名把不满足 V2 证据合同的旧记录写入正式学习账。

## 1. 最终目标

V2-12 完成后，用户从 `main.py` 或打包后的桌面程序启动 TradeHelper，必须真正运行以下主链，而不是测试 fixture、Fake port 或 V1 service：

```text
V2Settings + SQLiteRepository + ExchangeTradingCalendar
  -> V2 Provider composition
    -> 单股/组合 Application use case
      -> 数据 -> 特征 -> 预测 -> 情景 -> 策略 -> 风控
        -> 订单预览/组合分配 -> PresentationInput -> ReportDocument
          -> Flet / Markdown / HTML / PDF
```

后台链固定为：

```text
到期数据刷新
  -> 预测/策略/联合结果验证
    -> 分账指标
      -> 候选 OOF / 影子验证 / 晋升或回滚

LLM 冻结事实
  -> 结构化假设
    -> 确定性验证
      -> 研究报告修订 / 候选池
```

最终应用仍然必须稳定回答五个问题：

1. 现在是否可以买、卖、减仓、加仓、持有？
2. 如果现在不能操作，达到什么条件可以操作？
3. 如果判断错了，最大亏损是多少，在哪里失效？
4. 这个建议过去有没有正期望，可信度有多高？
5. 系统预测的是哪个目标日期、概率和收益区间，过去预测到底准不准？

没有可靠数据或账户时，系统必须明确回答“当前不可执行以及缺什么”，不能填入模拟本金、假行情、0 值基本面或 LLM 猜测来凑齐报告。

## 2. 规范优先级

实现冲突时按以下顺序处理：

1. `docs/v2/V2_12_MIGRATION_RELEASE.md`
2. V2-1 至 V2-11 对应阶段冻结规范
3. `DESIGN.md` 与 `V2_REFACTOR_PLAN.md`
4. `docs/V1_CAPABILITY_INVENTORY.md`
5. V1 参考代码

V2-12 不能放松任何已经冻结的硬约束。特别是：

- V2 主链不得 import V1 业务模块。
- A股和美股必须同等完成。
- Tab3 必须使用用户真实账户数据。
- 盘中/延伸时段行情不得写入正式日 K。
- 没有止损和最大亏损定义的新增风险计划不得可执行。
- OOF、研究员和后台优化不能阻塞前台确定性报告。
- LLM 不能直接生成订单、股数、仓位或覆盖风控结果。

## 3. 范围与非目标

### 3.1 V2-12 必须完成

- 正式 V2 composition root、应用生命周期和依赖关闭。
- Tab1/Tab3/历史报告/设置页的真实 application port 注入。
- 单股和组合的双市场、三时段端到端编排。
- 股票代码/公司名双市场检索与 canonical instrument 解析。
- FinBERT 新闻情感补全和打包模型校验。
- 前台确定性分析、后台研究员、到期验证和 OOF 调度。
- V1 设置、真实账户、持仓、关注列表和旧报告的受控迁移。
- migration 17、迁移预检、备份、隔离、幂等和失败恢复。
- V1 主链退出，`main.py`、打包配置和运行时只引用 V2。
- macOS 本地构建和启动冒烟。
- Windows 本地 bat 与 GitHub Actions 构建和启动冒烟。
- RL00-RL79、全量回归、真实 Provider、真实 LLM 和视觉验收。

### 3.2 V2-12 不负责

- 券商账户连接或自动下单。
- Level2/盘口深度的虚构替代。
- Web 正式部署。
- 为缩小安装包删除 FinBERT 或实际运行依赖。
- 把 V1 参数、回测缓存或历史建议直接晋升为 V2 Champion。
- 在发布阶段重新设计预测、策略、风控或学习算法。

## 4. 最终目录与依赖边界

新增代码优先按以下结构组织；只有存在真实职责时才建文件：

```text
tradehelper_v2/
  application/
    analysis.py          # 单股 use case
    portfolio_analysis.py# 组合 use case
    lookup.py            # 双市场代码/公司名检索
    background.py        # LLM、到期验证和 OOF 后台任务
  migration/
    contracts.py         # 迁移计划、项目、结果和原因码
    legacy_reader.py     # 只读 V1 SQLite/JSON reader；不 import V1
    planner.py           # 预检、冲突和迁移计划
    executor.py          # 事务写入 V2 与迁移审计
  runtime/
    container.py         # 唯一 production composition root
    lifecycle.py         # 启动、关闭、恢复和首次运行
    paths.py             # 开发/打包资源路径
    version.py           # 应用与 schema 版本
  release/
    smoke.py             # 打包产物无网络运行时冒烟
```

允许修改：

```text
main.py
tradehelper.spec
scripts/build_macos.sh
scripts/build_windows.bat
.github/workflows/build-windows.yml
requirements*.txt
```

禁止：

- `tradehelper_v2/runtime` import `core/`, `services/`, `data/`, `ui/` 等 V1 包。
- UI 直接实例化 Provider、SQLite 或业务引擎。
- migration reader 调用 V1 `Database`/`Settings` 单例。
- application use case 通过解析报告字符串传递业务状态。
- 每个页面各自创建 repository、模型注册表或线程池。

## 5. 新增运行时合同

以下合同必须是冻结 dataclass/enum，并使用稳定身份哈希；字段名允许在实现前做等价的小幅整理，但业务语义不能删减。

```text
AnalysisRunStatus:
  queued / running / deterministic_completed /
  background_pending / completed / cancelled / failed

SingleStockAnalysisCommand:
  command_id
  instrument
  mode                         # pre / intraday / eod
  history_period
  requested_at
  account_snapshot_id
  force_refresh

PortfolioAnalysisCommand:
  command_id
  market
  mode
  history_period
  requested_at
  account_snapshot_id
  watchlist_snapshot_id
  force_refresh

AnalysisRunResult:
  run_id
  command_id
  status
  deterministic_report_id
  research_report_id | None
  background_task_ids[]
  source_artifact_refs[]
  reason_codes[]
  started_at
  completed_at | None
```

命令身份必须包含市场、股票/组合、模式、回看周期、账户快照、关注列表快照和请求时点。相同命令重试可以复用已完成结果；不同账户或数据截止点不得误命中。

运行时还必须定义：

```text
RuntimeHealth:
  app_version
  schema_version
  settings_status
  database_status
  calendar_status
  finbert_status
  provider_capabilities[]
  migration_status
  checked_at

ReportRevisionLink:
  base_report_id
  revised_report_id
  revision_kind               # research_enriched
  invariant_section_hash
  created_at
```

`invariant_section_hash` 必须覆盖操作台、预测、交易计划、风控、事实和情景章节。LLM 修订只能新增/更新研究员章节，不能改变这些章节的字节级业务内容。

## 6. Production composition root

`RuntimeContainer` 是唯一正式装配点，生命周期内只创建一份：

- `V2Settings`
- `SQLiteRepository`
- `ExchangeTradingCalendar`
- `DataRefreshService`
- `FeatureBuilder`
- `ForecastRegistry` / `ForecastEngine` / `ForecastTrainer`
- `ScenarioPlanner`
- `StrategyEngine`
- `RiskOfficer`
- `OrderIntentFactory`
- `PortfolioDecisionEngine`
- `LearningEngine`
- `ResearchEngine` 和 LLM client factory
- 单线程 learning/OOF executor
- 有界 Provider executor
- V2-11 application services、页面和 export port

禁止页面切换时重新创建容器。关闭应用时依次：

1. 拒绝新任务。
2. 取消可取消的前台任务。
3. 等待正在提交的 SQLite 事务完成。
4. 停止后台 executor；未完成任务保留为可恢复状态。
5. 关闭 repository。

测试必须能注入冻结时钟、Fake Provider transport、临时路径和同步 executor，但生产默认值不能来自测试 helper。

## 7. 启动流程

正式启动顺序固定为：

```text
解析开发/打包资源路径
  -> 加载 V2Settings
    -> 检查并创建工作目录
      -> 以 0600/用户专属权限原子保存配置
        -> 打开 tradehelper_v2.db 并执行 schema 1..17
          -> 检查 V1 迁移状态
            -> 构建 RuntimeContainer
              -> 恢复限频/后台任务摘要
                -> 构建 Flet 页面并注入真实 ports
```

启动失败分为：

- **阻断**：V2 数据库损坏、工作目录不可写、schema 校验失败。
- **能力降级**：LLM 未配置、FinBERT 缺失、某个 Provider 不可用。
- **待迁移**：发现 V1 数据但用户尚未确认迁移。

能力降级不能让应用白屏。设置页和迁移预检必须始终可访问。

## 8. 双市场股票检索

V2-11 的 `lookup_port` 在本阶段必须接真实 `InstrumentLookupService`：

1. 精确合法代码先按市场规则构造 `InstrumentId`。
2. 本地 `StockMetadata` 缓存按代码和公司名检索。
3. 缓存无结果时调用市场对应的受限搜索 Provider。
4. 返回最多 20 条，按精确代码、名称前缀、名称包含稳定排序。
5. 结果必须包含 instrument、公司名、市场、交易所、来源和可见时点。
6. A股与美股使用同一 UI 交互；不得让 A股输入后无公司名返显。
7. 搜索空结果有短 TTL，不能永久缓存。

V1 迁入的美股若交易所未知，先保留 `US:UNKNOWN:CODE` 并标记待解析。在线检索确认交易所后，通过 `instrument_aliases` 原子生成新的账户/关注列表快照；不能原地篡改历史快照。

## 9. FinBERT 新闻补全

V2-1 Provider 只负责可信新闻事实，V2-12 必须补齐本地情感打分编排：

- 只处理 `finbert_label/score` 缺失且有可用标题/摘要的 `NewsSnapshot`。
- 模型输入、截断长度、模型目录哈希和版本必须冻结。
- 输出只能是 positive/neutral/negative 与 0..1 score。
- 原新闻来源、发布时间和首次可见时间不得改变。
- 相同新闻和模型版本幂等；模型升级产生新 enrichment 版本，不覆盖历史事实身份。
- FinBERT 不可用时保留未评分状态，新闻数量和文本仍可使用，但情感特征明确缺失。
- 前台不得重复加载模型；容器内懒加载一次。

打包冒烟必须真实加载内置模型并完成一条短文本推理，不能只检查目录存在。

## 10. Tab1 正式端到端流程

单股分析固定按以下顺序执行并逐阶段发布进度：

1. 校验命令和真实市场账户快照。
2. 解析 canonical instrument、元数据和上市日期。
3. 根据回看周期和上市日期读取缓存并刷新缺口日 K。
4. 按模式刷新当前行情：
   - 美股盘前/盘后延伸时段：Nasdaq.com -> yfinance。
   - 美股常规盘中：TickFlow。
   - A股盘中：TickFlow。
   - A股盘前无连续价格时不伪造盘前价。
5. 独立刷新新闻和基本面；Tab1 不依赖 Tab3 曾经运行。
6. 对新新闻执行 FinBERT enrichment。
7. 生成数据质量报告。
8. 生成 closed origin snapshot 和 current snapshot；正式预测只消费 closed origin。
9. 从注册表选择股票/行业/市场 Champion；不足时生成明确不可用或技术基线预测。
10. 保存四周期 ForecastResult。
11. 生成 TradingScenario、StrategyBundle、RiskDecisionBundle 和 OrderIntentBundle。
12. 构建 SingleStockPresentationInput 与确定性 ReportDocument，先保存再返回 UI。
13. 后台启动研究员、到期验证和 OOF 检查。

### 10.1 真实账户前置规则

Tab1 要给出股数、最大亏损和仓位，因此必须使用对应市场最新真实 `AccountSnapshot`。没有账户时：

- UI 明确引导用户先在“我的持仓”创建该市场账户。
- 不创建 0 元假账户、10 万元模拟账户或标准化研究账户。
- 可以显示数据检索预览，但不得生成声称可执行的完整交易报告。

用户真实填写 0 现金是合法账户；此时持仓保护计划仍可生成，新开仓批准股数为 0。

### 10.2 冷启动预测

首次分析尚无 Champion 时不能伪装成“数据缺失”：

- 使用已完成日 K 构造 point-in-time 技术训练样本缓存。
- 前台只允许有上限的经验基线准备，不执行完整候选搜索。
- 基线预测标为未通过 OOF，不参与新开仓 A/B 执行分级。
- 深度 OOF 在后台单线程运行，成功后只影响下一次分析。
- 样本确实不足时仍生成完整报告，明确“预测不可用、仅保留保护退出/观察计划”。

## 11. Tab3 正式端到端流程

组合分析不是循环调用 Tab1。固定流程为：

1. 冻结一个真实账户快照和一个关注列表快照。
2. 持仓优先、关注列表其次，按 canonical key 去重。
3. 批量刷新行情；日 K/新闻/基本面遵守各自限频与缓存策略。
4. 对每只股票独立运行数据、特征、预测、情景、策略和单股风控。
5. 单股失败只产生该股结构化失败结果，不取消其他股票。
6. 使用同一时点价格冻结组合估值；缺任一持仓价格时组合估值标记不完整。
7. 组合估值不完整时禁止新增风险，但保留已有持仓的保护退出条件。
8. 组合层统一处理现金、heat、集中度、相关性、共享退出和最终股数。
9. 保护退出优先于新增风险；关注股替换只作为重新分析候选。
10. 构建 PortfolioPresentationInput 和确定性组合报告。

免费 Provider 限额下，11 只或更多股票不能让前台等待整个限额窗口：

- 优先使用仍有效缓存和增量缺口。
- 当前配额内能刷新的股票立即完成。
- 超额股票进入持久化 refresh queue，并在本次报告中显示待刷新/降级。
- 不等待 10 分钟后才返回报告，也不把限频伪装成空数据。

## 12. LLM 与学习后台闭环

### 12.1 LLM

前台确定性报告不等待 LLM。流程固定为：

1. 主报告保存并返回 UI。
2. 后台从同一冻结 artifact 构造 ResearchFactManifest。
3. LLM 返回严格 JSON；失败只记录 invocation 状态。
4. 确定性 validator 生成 confirmed/refuted/pending/invalid_data。
5. 构建仅研究章节不同的 `research_enriched` 报告修订。
6. 校验 invariant section hash 相同后保存 revision link。
7. UI 显示“研究员已更新”，用户可切换修订；不静默覆盖旧报告。

任何 LLM 假设只能在后续 OOF/影子验证通过后影响未来版本，不能改变本次动作。

### 12.2 学习与 OOF

- 数据刷新后扫描到期 ForecastResult、OrderIntent 和联合运行。
- 到期事实验证、三本账更新和指标聚合使用单线程后台队列。
- 前台只读取上一次完整提交的模型/策略版本。
- 训练开始时固定数据截止点、参数空间和版本；取消不留下半个 Champion。
- 晋升/回滚完成后原子更新 registry，下一次分析生效。
- 后台错误不得删除原预测、计划、成交或历史模型。

## 13. V1 迁移原则

### 13.1 总原则

1. V1 数据库始终只读，迁移前后 SHA-256 和 mtime 不变。
2. 先预检、再备份、再生成 MigrationPlan、用户确认后执行。
3. 迁移写入 V2 数据库必须在事务内完成；失败全部回滚。
4. 每个源记录有 migration item 和明确结果，不静默丢弃。
5. 迁移可重复执行；同一 source fingerprint 不重复生成账户或归档。
6. 已存在 V2 数据时不自动覆盖，以 V2 已确认快照优先。
7. 迁移过程默认不访问网络；待解析股票在首次在线检索时处理。

### 13.2 迁移状态

```text
MigrationRunStatus:
  planned / awaiting_confirmation / running /
  completed / completed_with_quarantine / failed / cancelled

MigrationItemStatus:
  migrated / archived_only / skipped_duplicate /
  quarantined / rejected
```

固定原因码至少包含：

```text
MIGRATION_SOURCE_MISSING
MIGRATION_SCHEMA_UNKNOWN
MIGRATION_SOURCE_CHANGED_AFTER_PREFLIGHT
MIGRATION_INVALID_MARKET
MIGRATION_INVALID_INSTRUMENT
MIGRATION_EXCHANGE_UNRESOLVED
MIGRATION_INVALID_SHARES
MIGRATION_INVALID_COST
MIGRATION_INVALID_CASH
MIGRATION_HELD_REMOVED_FROM_WATCHLIST
MIGRATION_TARGET_ALREADY_NEWER
MIGRATION_LEGACY_EVIDENCE_UNTRUSTED
MIGRATION_PRE_LISTING_BAR_REJECTED
MIGRATION_REALTIME_BAR_REJECTED
MIGRATION_TRANSACTION_ROLLED_BACK
```

## 14. V1 数据迁移矩阵

| V1 数据 | V2 处理 | 是否进入正式决策/学习 |
|---|---|---|
| `config.json` 已知字段 | V2 配置为空时迁移；非空 V2 值优先 | 是，配置能力 |
| `account_balance` | 按 A/CNY、US/USD 生成现金；不当作账户权益 | 是 |
| `holdings` | 校验市场、代码、shares>0、cost>=0 后生成不可变账户快照 | 是 |
| `watchlist` | 去重，移除已持仓股票并记录原因 | 是 |
| `stocks` | 只作迁移名称/上市日期线索，标记 legacy source；首次联网重新核验 | 仅元数据线索 |
| `reports` | 原 Markdown、评分和路径进入 legacy report archive | 否；只读查看 |
| `price_history` | 不迁移；由 V2 权威 Provider 重新获取 | 否 |
| `intraday_price_history` | 不迁移，防止实时价污染正式事实 | 否 |
| `news_sentiment` | 不迁移；来源/可见时点/模型版本不完整 | 否 |
| V1 基本面缓存 | 不迁移；重新从正式 Provider 获取 | 否 |
| `forecast_log` / `prediction_log` | 只归档审计，不转换成 V2 ForecastOutcome | 否 |
| `trade_plan_log` / `joint_oof_runs` | 只归档审计，不转换成正式策略/联合账 | 否 |
| `per_stock_params` / 回测缓存 | 不迁移，必须由 V2 purged OOF 重建 | 否 |
| V1 LLM 观察 | 不晋升候选；必要时只归档文本 | 否 |

这项政策是为了可信度：旧数据“看起来有值”不等于满足 V2 point-in-time、来源和身份合同。不准确的历史证据不如没有。

## 15. migration 17

migration 17 至少新增：

```text
legacy_migration_runs
legacy_migration_items
legacy_report_archives
legacy_evidence_archives
instrument_aliases
analysis_runs
report_revision_links
```

约束：

- `source_fingerprint + migration_version` 唯一。
- migration item append-only；不得把 quarantined 原地改成 migrated，重试产生新 revision。
- legacy archive 不得被 ForecastRegistry、LearningEngine 或历史正式指标查询读取。
- analysis run 只保存任务摘要和最终报告引用，不把半成品当报告。
- report revision 的 base/revised 都必须存在，且 invariant section hash 相同。
- migration 17 幂等、checksum 稳定、repository 重启强类型恢复。

## 16. 首次运行与迁移交互

启动时分三类：

1. **新用户**：没有 V1 数据，创建 V2 配置和空数据库，进入设置/账户引导。
2. **V1 用户**：检测到 `config.json` 或 `tradehelper.db`，展示预检数量、冲突和备份位置；用户确认后迁移。
3. **已迁移用户**：直接打开最新 V2 状态，不重复弹窗。

迁移页必须显示：

- V1 文件路径与只读哈希。
- 可迁移账户/持仓/关注数量。
- 仅归档报告数量。
- 被拒绝的行情、预测、参数和原因。
- 目标 V2 数据是否已有更新记录。
- “开始迁移”“跳过”“导出预检”三个明确动作。

迁移失败不得启动旧 V1 主链兜底。用户可以修复问题后重试，或继续使用空 V2 环境。

## 17. V1 代码退出策略

仓库已有标签 `v1.0-final-before-v2`，V1 可从 Git 恢复。V2-12 分三步退出：

1. **接线期**：V1 源码仅作行为对照，禁止被 V2 import。
2. **迁移验收后**：`main.py`、spec、脚本和 CI 全部只运行 V2；迁移 reader 使用自身冻结 schema，不 import V1。
3. **最终清理**：从 V2 分支删除 V1 业务目录与 V1 运行测试，保留 `docs/archive/v1/`、V1 capability inventory、迁移 fixture 和 Git 标签。

最终应删除/退出打包的旧业务入口包括：

```text
alpha/ backtest/ config/ core/ data/ indicators/
report/ services/ strategies/ ui/ run_backtest.py
tests/ 下非 v2 的 V1 行为测试
```

删除前必须先运行一次 V1+V2 全量回归并记录结果；删除后运行 V2 最终矩阵。任何仍被 V2 需要的算法必须先以 V2 合同重建并已有测试，不能通过移动文件伪装解耦。

## 18. 发布与打包

### 18.1 通用

- 应用版本固定为 `2.0.0`，schema 版本为 17。
- `tradehelper.spec` 修复编码，入口只指向 V2 `main.py`。
- V1 源码、旧测试、开发缓存和本地数据库不得进入产物。
- FinBERT、Flet 图标、akshare 数据文件、交易日历、ReportLab 字体依赖必须显式收集。
- 生成 build manifest：Git commit、Python、平台、依赖锁哈希、模型哈希和构建时间。
- 运行依赖与开发依赖分离；发布必须使用受控约束文件，不能只依赖无限上浮范围。

### 18.2 打包产物冒烟

无网络 smoke 必须在临时 HOME/APPDATA 和临时工作目录执行：

1. 导入所有 V2 production 模块。
2. 创建 V2Settings 和 schema 17 数据库。
3. 构建 RuntimeContainer。
4. 加载交易日历。
5. 加载 FinBERT 并完成一条推理。
6. 用冻结 fixture 跑一条最小确定性报告链。
7. 导出 HTML 与 PDF。
8. 正常关闭并再次打开数据库。

smoke 不得读取开发者真实配置、真实数据库或调用网络。

### 18.3 macOS

- `scripts/build_macos.sh` 构建后必须执行 `.app` 内二进制 smoke，而不只检查目录存在。
- 检查 arm64/x86_64 目标声明、应用图标、版本和 bundle identifier。
- 未签名产物明确标注，不把 Gatekeeper 提示误判为应用逻辑失败。

### 18.4 Windows

- 本地 `build_windows.bat` 和 GitHub Actions 使用同一 spec 与 smoke 入口。
- CI 对 `V2.0`/发布 tag 可手动触发，不只监听 `main`。
- 必须验证 jaraco、akshare calendar、transformers/torch、ReportLab、Flet 和 DLL 依赖。
- GitHub 产物下载后执行 EXE smoke，再上传 artifact。

## 19. 失败、取消与恢复

- Provider 限频：显示股票、阶段和 `retry_at`，其余股票继续。
- Provider 超时：按冻结 fallback；全部失败时结构化降级。
- 用户取消：停止未开始阶段，不保存半成品 ReportDocument。
- 报告已保存后 LLM 失败：保留主报告，不生成空修订。
- 数据库提交前崩溃：事务回滚。
- 报告保存后 UI 崩溃：重启后从 analysis run 恢复报告。
- 后台 OOF 崩溃：旧 Champion 继续，候选保持未完成状态。
- 迁移中崩溃：V1 不变，V2 事务回滚，run 标记 failed/recoverable。

错误消息对用户使用中文说明；技术 reason code 保留用于审计。日志不能包含 token、LLM prompt 全文、账户完整快照或未脱敏路径。

## 20. 性能预算

时间分为算法、Provider、LLM 和排队四类，不能混成一个“总耗时”：

| 场景 | 预算 |
|---|---|
| 点击后首个进度 | <= 250ms |
| 缓存命中 Tab1 确定性主链 p95 | <= 5s |
| 缓存命中 10 股票 Tab3 确定性主链 p95 | <= 20s |
| 50 股票 Presentation/Report 构建 | <= 1.5s |
| UI 主线程单次同步阻塞 | <= 100ms |
| 后台 LLM/OOF 对前台完成时间影响 | 0ms 等待 |

冷启动历史样本构建、首次 FinBERT 加载和真实 Provider 延迟单列展示，不得通过延长前台无反馈时间隐藏。

## 21. RL00-RL79 验收矩阵

每个编号必须对应一个唯一具名测试；不能用一个大端到端测试冒充多个行为。

### RL00-RL09：运行时与合同

| 编号 | 固定行为 |
|---|---|
| RL00 | `main.py` production 路径只 import V2 |
| RL01 | RuntimeContainer 使用冻结依赖可完整构建 |
| RL02 | 启动按 settings -> schema -> migration -> UI 顺序 |
| RL03 | 关闭顺序停止任务并关闭 repository |
| RL04 | AnalysisCommand 身份包含账户/模式/截止点 |
| RL05 | 一次分析固定模型、策略、政策版本 |
| RL06 | 无真实账户拒绝生成可执行完整报告 |
| RL07 | runtime/application 不 import V1 业务模块 |
| RL08 | 空工作目录首次启动可进入设置页 |
| RL09 | migration 17 幂等且可重启恢复 |

### RL10-RL19：V1 配置与账户迁移

| 编号 | 固定行为 |
|---|---|
| RL10 | 预检只读且 V1 hash/mtime 不变 |
| RL11 | V1 不存在时形成新用户计划 |
| RL12 | V2 非空设置优先，V1 只补空字段 |
| RL13 | secret 迁移后不进入日志/报告/预检导出 |
| RL14 | V1 余额和持仓按 A/US 分成两个账户 |
| RL15 | 股数、成本和现金用 Decimal 数值无漂移 |
| RL16 | 非法市场/代码/股数/成本进入 quarantine |
| RL17 | 已持仓股票从 watchlist 移除并记录原因 |
| RL18 | 同 source fingerprint 重跑不重复写入 |
| RL19 | 迁移失败不改变已有 V2 与 V1 数据 |

### RL20-RL29：旧证据隔离

| 编号 | 固定行为 |
|---|---|
| RL20 | V1 `price_history` 不进入 V2 daily_bars |
| RL21 | V1 分钟/实时行情不进入正式日 K |
| RL22 | V1 新闻/基本面缓存不进入 V2 正式事实 |
| RL23 | V1 报告只进入 legacy archive 且可只读打开 |
| RL24 | V1 预测/策略/联合记录不进入三本正式账 |
| RL25 | V1 参数和回测缓存不进入候选/Champion |
| RL26 | 上市日前记录计数并拒绝 |
| RL27 | US UNKNOWN 股票通过 alias 生成新快照而非改历史 |
| RL28 | 已完成迁移重启后不重复弹窗 |
| RL29 | 备份清单包含路径、大小和 SHA-256 |

### RL30-RL39：Tab1 双市场三时段

| 编号 | 固定行为 |
|---|---|
| RL30 | 美股盘后真实全链生成下一交易日计划 |
| RL31 | 美股盘中 TickFlow 实时价只进 current snapshot |
| RL32 | 美股盘前 Nasdaq 延伸价进入当日条件计划 |
| RL33 | A股盘后真实全链生成下一交易日计划 |
| RL34 | A股盘中 TickFlow 与 T+1/涨跌停规则进入风控 |
| RL35 | A股盘前无实时价时使用 T-1 条件计划并明示 |
| RL36 | 新股按上市日期裁剪并样本不足降级 |
| RL37 | 新闻/基本面缺失不伪造且技术链仍可完成 |
| RL38 | 无 Champion 时基线/不可用状态准确且后台 OOF 不阻塞 |
| RL39 | 报告包含四分支、止损、最大亏损和目标日 |

### RL40-RL49：Tab3 组合全链

| 编号 | 固定行为 |
|---|---|
| RL40 | 11 只美股在限频下不等待完整窗口才返回 |
| RL41 | 11 只 A股第 11 只进入可恢复队列且报告完成 |
| RL42 | 冻结估值由真实现金+持仓市值闭合且仓位不超 100% |
| RL43 | 单股数据失败只降级该股 |
| RL44 | 保护退出排序先于新增风险 |
| RL45 | 替换机会仅为研究/重分析候选 |
| RL46 | 持仓与关注列表不重叠且快照不可变 |
| RL47 | 取消组合任务不保存半成品报告 |
| RL48 | Tab1/Tab3 共用缓存但各自独立刷新事实 |
| RL49 | 组合链任何位置都不构造模拟 10 万元本金 |

### RL50-RL59：后台、失败与恢复

| 编号 | 固定行为 |
|---|---|
| RL50 | 确定性报告完成不等待 LLM |
| RL51 | LLM 修订只能改变研究章节 |
| RL52 | LLM 超时/非法 JSON 保留主报告 |
| RL53 | 到期数据触发预测/策略/联合分账更新 |
| RL54 | 深度 OOF 单线程后台运行且只影响下一次分析 |
| RL55 | 限频和后台任务摘要重启可恢复 |
| RL56 | Provider 全部失败形成明确阻断/降级而非假值 |
| RL57 | 报告持久化前崩溃无历史半成品 |
| RL58 | 报告持久化后 UI 崩溃可恢复打开 |
| RL59 | 完成命令幂等重试不重复发行相同事件 |

### RL60-RL69：发布产物

| 编号 | 固定行为 |
|---|---|
| RL60 | macOS spec 收集 V2 运行依赖并排除 V1 |
| RL61 | Windows 本地与 CI 使用同一 spec/smoke |
| RL62 | 打包入口不 import V1 业务模块 |
| RL63 | jaraco、akshare calendar 和动态 metadata 可导入 |
| RL64 | 内置 FinBERT 真实加载并完成一条推理 |
| RL65 | 打包产物可导出中文 HTML/PDF |
| RL66 | 临时 HOME 的新用户启动不访问开发者数据 |
| RL67 | 旧用户预检、迁移、重启流程通过 |
| RL68 | 构建日志、运行日志和崩溃信息无 secret |
| RL69 | build manifest 版本、commit、依赖和模型 hash 完整 |

### RL70-RL79：最终验收

| 编号 | 固定行为 |
|---|---|
| RL70 | Tab1/Tab3 × A/US × pre/intraday/eod 12 格矩阵通过 |
| RL71 | 缓存命中 Tab1 p95 满足 5 秒预算 |
| RL72 | 冷启动耗时分解并持续显示进度 |
| RL73 | 缓存命中 10 股票 Tab3 p95 满足 20 秒预算 |
| RL74 | 并发读、串行写和任务取消不产生 database locked |
| RL75 | V1 业务目录退出后 V2 导入与测试通过 |
| RL76 | 受控依赖清单可在干净 Python 3.12 环境安装 |
| RL77 | A股/美股真实 Provider smoke 全部通过 |
| RL78 | 真实 LLM strict JSON/脱敏 smoke 通过 |
| RL79 | 最终报告在双市场均回答五个核心问题 |

## 22. 测试文件与命令

新增测试至少包括：

```text
tests/v2/test_runtime_composition.py
tests/v2/test_runtime_lifecycle.py
tests/v2/test_v1_to_v2_migration.py
tests/v2/test_legacy_evidence_isolation.py
tests/v2/test_e2e_single_stock.py
tests/v2/test_e2e_portfolio.py
tests/v2/test_e2e_mode_matrix.py
tests/v2/test_background_jobs.py
tests/v2/test_release_smoke.py
tests/v2/test_v1_retirement.py
tests/v2/test_interactive_performance.py
```

离线阶段验收：

```bash
venv/bin/python -m pytest tests/v2/test_runtime_*.py -q
venv/bin/python -m pytest tests/v2/test_v1_to_v2_migration.py tests/v2/test_legacy_evidence_isolation.py -q
venv/bin/python -m pytest tests/v2/test_e2e_*.py tests/v2/test_background_jobs.py -q
venv/bin/python -m pytest tests/v2/test_release_smoke.py tests/v2/test_v1_retirement.py -q
venv/bin/python -m pytest tests/v2/ -q
```

联网验收：

```bash
TRADEHELPER_LIVE_TESTS=1 TRADEHELPER_LIVE_USE_V1_SETTINGS=1 \
  venv/bin/python -m pytest tests/v2/integration/test_live_providers.py -q

TRADEHELPER_LLM_LIVE_TESTS=1 TRADEHELPER_LIVE_USE_V1_SETTINGS=1 \
  venv/bin/python -m pytest tests/v2/integration/test_live_llm_research.py -q
```

发布验收还必须执行 macOS 本地构建、Windows 本地 bat 构建和 GitHub Actions Windows 构建；不能用 Python 源码测试替代打包产物启动。

## 23. 实施顺序

严格按以下批次推进，每批完成并复审后再进入下一批：

1. **V2-12A：运行时合同与 migration 17**
   完成 RL00-RL09。
2. **V2-12B：V1 迁移与旧证据隔离**
   完成 RL10-RL29。
3. **V2-12C：lookup、FinBERT 与 production container**
   补齐真实 UI ports 和资源生命周期。
4. **V2-12D：Tab1 正式全链**
   完成 RL30-RL39。
5. **V2-12E：Tab3 正式全链**
   完成 RL40-RL49。
6. **V2-12F：LLM、学习、失败恢复**
   完成 RL50-RL59。
7. **V2-12G：V1 退出与跨平台发布**
   完成 RL60-RL69。
8. **V2-12H：最终矩阵与发布复审**
   完成 RL70-RL79、真实联网和安装包验收。

不得先删 V1 再补迁移 reader，不得先改打包再完成 production composition，也不得只把测试 helper 接到 UI 就声称端到端完成。

## 24. 完成定义

V2-12 以及 TradeHelper 2.0 只有同时满足以下条件才算完成：

1. RL00-RL79 一编号一行为全部通过。
2. 双市场、三时段、Tab1/Tab3 的 12 格矩阵通过。
3. V1 迁移只读、可预检、可备份、幂等、失败可恢复。
4. 旧行情、预测、策略和参数没有污染 V2 正式事实/学习账。
5. production `main.py` 不 import V1，V1 业务代码退出 V2 分支运行面。
6. 前台报告不等待 LLM/深度 OOF，后台修订不可改写正式动作。
7. macOS、Windows 本地和 GitHub Windows 产物都真实启动通过。
8. FinBERT、HTML/PDF、交易日历和动态依赖在打包产物中可用。
9. V2 全量、真实 Provider、真实 LLM、性能和视觉验收全部通过。
10. README/设计/计划/能力清单更新为“TradeHelper 2.0 已完成并复审”。

当前本机证据：RL00-RL79 验收 `91 passed`；V2/项目全量 `819 passed, 4 skipped`；3 条真实 Provider 与 1 条真实 LLM 测试显式开启后全部通过；真实 V1 数据库迁移 19,112 项且源库未改变；macOS 包内 runtime smoke 通过。Flet 内嵌 framework 的 PyInstaller 临时签名仍有警告，本机可运行不等于已完成 Developer ID 签名或公证。Windows spec、bat 与 CI 已统一，但第 7 条中的 Windows 本地和 GitHub Windows 产物启动必须在对应 runner 真实执行，因此在该证据产生前不宣称跨平台发布门槛全部完成。

V2-12 完成后不自动开始券商自动下单、Web 发布或 2.1 新功能；这些必须另立设计和风险评审。
