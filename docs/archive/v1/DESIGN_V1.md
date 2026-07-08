# TradeHelper 设计文档

> 当前 1.x 架构基线：2026-07-03。升级进度与剩余工作以 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md) 为准。
> TradeHelper 2.0 的重构路线、分层合同和逐层测试计划以 [V2_REFACTOR_PLAN.md](./V2_REFACTOR_PLAN.md) 为准。

## 1. 设计目标

TradeHelper 是 Python 3.12 + Flet 的跨平台股票分析应用。它不只输出“买/卖/观望”，而是生成一份可复现的交易计划：

1. 当前可以买、卖、加仓、减仓还是持有。
2. 暂不能操作时，需要等待什么价格或形态。
3. 判断错误时在哪里失效、最大计划亏损是多少。
4. 同类建议过去是否有正期望，证据覆盖和可信等级是什么。

系统不能保证用户获利。设计目标是把数据口径、策略判断、风险预算和历史证据显式化，减少不可复现的主观判断。

## 2. 角色边界

| 角色 | 职责 | 不负责 |
|------|------|--------|
| 代码系统 | 计算事实、策略条件、风险金额、仓位、失效条件和历史统计 | 编造不可验证的市场事实 |
| LLM 研究员 | 解释报告、提出系统未覆盖的观察、质疑代码判断 | 直接生成可执行交易指令 |
| 风控官 | 检查事实、数据质量、风险和历史有效性，给出 A/B/C/D | 静默删除研究员观察 |
| 用户 | 结合自身约束最终执行、忽略或反馈 | 无条件跟单 |

LLM 观察会进入结构化候选池，经过代码事实验证和历史表现检查后展示“研究员观察 vs 系统确认”。分歧必须可见。

## 3. 分层架构

```text
main.py
  |
  +-- ui/                 Flet 页面与交互状态
  |     +-- main_page.py          Tab1 单股分析
  |     +-- history_page.py       Tab2 历史报告
  |     +-- portfolio_page.py     Tab3 我的持仓
  |     +-- settings_ui.py        Tab4 设置
  |
  +-- services/           I/O 与业务流程编排
  |     +-- analysis_service.py
  |     +-- portfolio_service.py
  |     +-- news_service.py
  |     +-- optimization_scheduler.py
  |     +-- forecast_service.py
  |     +-- research_observations.py
  |
  +-- core/               纯计算管道与可信度治理
        +-- forecast_engine.py     独立1/3/5交易日概率预测与OOF评估
  |     +-- pipeline.py
  |     +-- signal_check.py
  |     +-- strategy_audit.py
  |     +-- strategy_pool.py
  |     +-- data_quality.py
  |     +-- prediction_tracker.py
  |
  +-- alpha/ indicators/  因子、基本面、技术指标、FinBERT
  +-- strategies/         20 个 Decision-first 策略
  +-- backtest/           事件驱动回测、撮合和绩效
  +-- data/               TickFlow/Finnhub/baostock/新闻/SQLite
  +-- report/             图表、LLM 提示词、HTML/PDF
  +-- config/ utils/      配置、市场规则和通用工具
```

依赖方向保持 `UI -> Services -> Core -> Engines/Support`。`core/pipeline.py` 不主动发网络请求；外部 I/O 由 Service 层准备好后传入。

## 4. 页面定位

### 4.1 Tab1 单股分析

针对一只股票生成完整研究：数据质量、技术和 Alpha、基本面、新闻、策略回测、样本外审计、条件计划、研究员观察、历史预测与退出复盘。

页面分为“分析工作台”和“全宽报告阅读”两个状态，报告生成后不再让输入区长期占据阅读宽度。

### 4.2 Tab3 我的持仓

组合级分析，输入是用户真实余额、持股数量、成本和关注列表。它额外计算：

- 单票占比、股票总仓位、HHI 集中度和剩余容量。
- 近 90 日相关性和高相关组合上限。
- 浮盈亏、止盈、止损、禁止加仓和替换优先级。
- 真实账户权益下的最大亏损金额；权益为 0 时禁止虚构本金开仓。

持仓支持行内修改股数和成本价。

## 5. 三时段数据语义

| 模式 | 可用事实 | 决策方式 |
|------|----------|----------|
| `intraday` | T-1 正式历史 + 当次实时 OHLCV | 输出当日可触发条件 |
| `pre` | T-1 历史 + 盘前价/期指 | 输出开盘后的条件计划 |
| `eod` | 已完成日 K + 当日新闻/基本面 | 输出下一交易日计划 |

盘中实时数据只创建内存 `decision_df`，不写入 `price_history`。历史 `df` 专供回测和后台优化；报告、当前技术标记和信号判断统一使用 `decision_df`，避免实时判断与报告显示口径不一致。

## 6. 数据链路

```text
用户输入
  -> 代码识别/搜索
  -> 上市日期确认并裁剪请求窗口
  -> SQLite 正式日 K 缓存 + TickFlow 增量更新
  -> 新闻按时段 TTL 主动刷新 + 增量 FinBERT
  -> 基本面与实时/延伸时段报价
  -> 数据质量闸门
  -> 指标与 Alpha
  -> 正式参数回测 + 样本外审计
  -> 当前 StrategyDecision + 风控分级
  -> 代码交易计划 + LLM 研究员解读
  -> 预测/观察事件去重记录
  -> 后台 deep optimization
```

### 6.1 行情与基本面

| 数据 | A 股 | 美股 |
|------|------|------|
| 历史日 K | TickFlow | TickFlow |
| 盘中实时 | TickFlow quote/tick | TickFlow quote/tick |
| 延伸时段 | 不适用 | Nasdaq.com `/info`，失败后 yfinance |
| 基本面 | baostock -> akshare -> LLM | Finnhub -> yfinance/akshare/百度 -> LLM |
| 上市日期 | baostock | Finnhub `profile2` |

上市日期存入 `stocks.listing_date`。如果用户选择的回测窗口早于上市日，缓存读取、增量拉取、指标和回测都从上市日开始。新股样本不足是可信度降级，不是伪造历史数据来填满窗口。

### 6.2 新闻

Tab1 和 Tab3 通过 `services/news_service.py` 共用缓存协议，但各自主动刷新，互不依赖。`news_refresh_state` 同时记录空结果和失败状态，空状态也有 TTL，不会永久阻止后续刷新。

当前 TTL：盘中约 30 分钟、盘前约 1 小时、盘后约 6 小时。刷新失败时旧缓存可用于展示，但不会被伪装成当次新鲜 Alpha 输入。

Tab1/Tab3 按分析模式路由报价：美股盘前和盘后直接使用 Nasdaq.com -> yfinance，避免旧 TickFlow 时间戳误判时段；盘中使用 TickFlow，Tab3 多代码批量报价单批只消耗一次请求额度。批量缺失的股票必须进入数据质量闸门，禁止改用延伸时段报价补成“盘中价”。基本面成功缓存 24 小时，失败结果只缓存 5 分钟。

## 7. Alpha 模型

### 7.1 技术因子

七个技术因子：`rsi`、`dif`、`macd_bar`、`bb_pct`、`k`、`d`、`j`。

1. 每个因子滚动 Z-Score，窗口默认 60。
2. `tanh` 压缩到 `(-1, 1)`。
3. IC/IR 验证给出 A/B/C/D/?。
4. A/B 全权、C 半权、D 剔除；`?` 代表证据不足，保留先验权重但不视为已验证。
5. 已验证覆盖低于 50% 时，数据质量进入观察状态，建议等级和仓位下降。

只有最新决策点允许使用完整样本的 IC/IR 结果；不能把今天的验证权重回写到历史每一天。

### 7.2 最新时点权重

有完整基本面时采用行情自适应权重：

| 行情 | 技术 | 风格 | 基本面 | 新闻 |
|------|-----:|-----:|-------:|-----:|
| `trending_volatile` | 0.40 | 0.05 | 0.30 | 0.25 |
| `trending_steady` | 0.38 | 0.10 | 0.27 | 0.25 |
| `ranging/transitional` | 0.35 | 0.15 | 0.25 | 0.25 |

无基本面时默认 `0.6 * Tech + 0.4 * News`。盘口因子仅叠加于最新时点。历史区间只使用当时可得的技术和衰减新闻，防止当前基本面污染回测。

## 8. 策略接口

`BaseExecutionStrategy.generate_decision()` 返回：

```python
StrategyDecision(
    action="buy | sell | hold | watch | invalid",
    execution_level="A | B | C | D",
    trigger_price=0.0,
    stop_loss=0.0,
    take_profit=0.0,
    take_profit_pct=0.0,
    take_profit_mode="fixed | dynamic | conditional | none",
    take_profit_rule="",
    max_loss_amount=0.0,
    position_pct=0.0,
    invalidation="",
    missing_conditions=[],
    reason="",
)
```

所有 20 个策略都实现原生 Decision-first 路径和 `diagnose_no_signal()`。订单只由 `decision_to_orders()` 转换，回测与报告不维护第二套策略信号。

策略 P/T 为 `overlay_scope="always"`；Q/R/S 为 `overlay_scope="position"`。新增覆盖策略不需要在管道中再维护字母列表。

## 9. 回测和审计

### 9.1 事件时序

| 时间 | 动作 |
|------|------|
| T 日收盘 | 使用 T 日及以前数据生成 `StrategyDecision` 和订单 |
| T+1 开盘 | 按开盘价、滑点、佣金、成交量和市场规则撮合 |
| T+1 盘中 | 使用 High/Low 检查止损和止盈 |
| T+1 收盘 | 更新持仓最高价、权益曲线和交易记录 |

开盘已跳空越过止损线时按更差的开盘价成交；只有盘中穿越止损才按止损价成交。

### 9.2 市场规则

`utils/market_rules.py` 集中维护交易单位、费用、T+1 和涨跌幅：

- A 股：100 股一手、T+1、万三佣金、最低 5 元、卖出税费 0.05%。
- 主板默认 10%，创业板/科创板 20%，北交所 30%，明确 ST 标记时 5%。
- 美股：1 股交易单位，无涨跌停和 T+1 限制。
- 滑点由基础值、下单时已知历史波动率和成交量容量共同决定。

停复牌和 ST 的实时权威数据仍不完整，因此属于剩余风险。

### 9.3 样本外审计

策略审计按时间拆分训练 70% / 验证 30%，输出 PASS / CONDITIONAL / FAIL 和过拟合标记。验证期还运行 400 次循环分块 Bootstrap，输出正期望概率、收益和夏普 95% 区间、回撤 P95 与大回撤概率。样本不足时不输出伪精度。

## 10. 参数自动优化

前台只使用已晋升正式参数；深度优化由 `optimization_scheduler.py` 在后台单线程运行。

```text
参数变体
  -> walk-forward 样本外筛选
  -> strategy_param_candidates(candidate)
  -> 正绝对收益 +（正基准超额收益 或 风险调整优势）
  -> 跨 3 个数据截止日确认 + 20 天影子观察
  -> promoted / replaced
  -> 实盘健康度转负
  -> rolled_back / demoted
  -> 更保守 recovery 候选重新竞争
```

全样本“最优参数”扫描已停止作为晋升依据，避免未来数据选参数。

## 11. 历史学习闭环

`forecast_log` 保存与交易动作无关的 1/3/5 日概率预测，使用明确目标交易日和稳定事件键冻结；到期后只补录实际收盘、方向、Brier、Log Loss、ECE、校准分箱和区间命中。`forecast_model_versions` 按股票、市场和周期隔离 Champion/Challenger；受控候选包括相似行情、正则化多分类、平滑浅层概率树和集成模型。选择/确认 OOF 之间按预测周期增加标签成熟隔离带，两个窗口都必须优于历史频率基线，配对时间块 Bootstrap 的90%改进下界为正、ECE 不明显恶化且80%收益区间覆盖率至少70%后才能晋升。方向概率与 P10/P50/P90 使用同一个按类别概率加权的条件收益分布；在线 Brier 明显退化时自动回滚。

`feature_context_snapshots` 冻结 Tab1/Tab3 当次真实可见的新闻得分、发布时间、来源、基本面字段、供应商和抓取时点。相同内容去重、内容变化保留新版本，不按新闻发布日期反向回填；覆盖不足时这些字段继续排除出历史预测训练。

`trade_plan_log` 单独保存当时可执行的 A/B 级交易方案和账户快照。`reference_date` 表示信息截止日，`decision_session_date` 表示首次可执行的市场会话；事件键包含策略版本、触发价、止损、止盈、仓位、最大亏损、账户权益/现金/持股/成本快照和盘中生成分钟，因此相同方案去重、实质变化保留。盘前/盘后按下一交易日开盘及后续正式日K复盘净表现、MFE/MAE和退出机会成本；盘中以报告生成完成时间冻结建议，只读取 `intraday_price_history` 中该时点之后的分钟K，证据不足时保持待验证。A股盘中买入可在下一分钟入场，但当日不能退出，下一交易日才检查止盈止损。已验证方案保存证据来源、质量和K线数量，补充源不能覆盖已有供应商级分钟K。策略健康度和历史方案面板均按实际决策会话归组，并按证据质量跨交易日折算胜率、平均收益、有效样本和 Wilson 下界；有效样本少于8时不能标记为可靠。`prediction_log` 仅保留为旧版迁移兼容表，生产链路已停止新增。

`joint_oof_runs` 保存最终建议链的嵌套样本外审计。`joint_oof_v4_embargo_coherent` 在每个测试折只用此前训练段分别选择 1/3/5 日预测模型与参数、策略参数和策略家族，测试段通过多周期预测共识、正式信号检查、执行等级、动态仓位、Broker撮合和逐步累积的历史健康度运行；每个事件分别记录策略 Decision 与 Broker 成交/拒单结果，并与单独预测能力、单策略回测分账展示。漂移只比较相同 `policy_version` 的连续运行。

`research_observation_log` 保存 LLM/系统观察、触发算子、价格、止损、执行等级和后续表现。观察必须真实触发且达到验证窗口后，才能进入正负期望统计；LLM 与系统规则的命中率分账，系统规则样本不能提升 LLM 观察等级。

自动学习只能在预先定义的参数空间和升降级规则内运行，不允许模型自行修改生产代码。

## 12. 数据质量闸门

`core/data_quality.py` 检查：

- OHLC 关系、非正价格、缺失值和异常跳变。
- 样本长度、上市日期和请求区间。
- 实时报价是否存在、时间戳是否新鲜、OHLC 是否完整。
- 成交量、新闻、基本面和盘口可用性。
- 因子验证覆盖率。

严重冲突为 `blocked/D`，降级数据会裁剪仓位并下调执行等级。A 股或美股本身波动大不会直接被认定为 OHLC 异常；OHLC 异常指同一根 K 线内部 `high/low/open/close` 关系不成立，跨日跳变另有独立、保守的可信度检查。

## 13. 数据库

SQLite WAL，当前 20 张表：

| 分类 | 表 |
|------|----|
| 市场与报告 | `stocks`, `price_history`, `intraday_price_history`, `reports` |
| 新闻 | `news_sentiment`, `news_refresh_state` |
| 用户组合 | `holdings`, `watchlist`, `account_balance` |
| 学习闭环 | `forecast_log`, `feature_context_snapshots`, `forecast_model_versions`, `trade_plan_log`, `joint_oof_runs`, `prediction_log`, `research_observation_log` |
| 回测优化 | `bt_variant_cache`, `per_stock_params`, `strategy_param_candidates`, `deep_optimization_runs` |

`Database.init()` 会自动建表、补列、清理历史重复事件并建立唯一索引；新用户首次运行和旧用户升级都会执行同一初始化/迁移路径。

## 14. 报告结构

代码生成的事实和交易计划优先，LLM 负责研究解释：

1. 可信度硬摘要。
2. 当前可执行动作和一分钟操作台。
3. 条件触发计划、风险金额、仓位和失效条件。
4. 技术、Alpha、基本面、新闻和市场状态。
5. 持仓风控与组合风险。
6. 研究员观察 vs 系统确认。
7. 样本外审计、历史预测、退出复盘和参数健康度。
8. 保守/激进方案；固定止盈计算真实风险收益比，动态/条件止盈展示规则并标记固定比值不可量化，没有主动止盈时明确提示。

## 15. 扩展约定

| 目标 | 修改入口 |
|------|----------|
| 新增策略 | 继承 `BaseExecutionStrategy`，实现 Decision 和诊断，在 `strategies/__init__.py` 注册 |
| 新增覆盖策略 | 设置 `overlay_scope="always"` 或 `"position"` |
| 新增技术因子 | `alpha/scoring.py::INDICATOR_COLUMNS`，同步验证和测试 |
| 新增行情源 | 实现 `BaseStockFetcher` 并保持标准 OHLCV/时间语义 |
| 新增新闻源 | 继承 `BaseNewsProvider`，接入共享刷新状态 |
| 新增配置 | `DEFAULT_CONFIG` + `ui/settings_ui.py` |
| 新增页面 | `ft.Container` + `main.py` Stack/NavigationBar |
| 修改市场规则 | `utils/market_rules.py`，并补撮合与报告风险测试 |

## 16. 测试与发布

当前 14 个测试文件、249 个测试。除 pytest 外，每个测试文件都有直接执行入口：

```bash
venv/bin/python -m pytest tests/ -q
for f in tests/test_*.py; do venv/bin/python "$f" || exit 1; done
```

发布脚本：

```bash
bash scripts/build_macos.sh
scripts\build_windows.bat
```

Windows 本地与 GitHub Actions 产物必须通过 `TRADEHELPER_SMOKE_TEST=1` 运行时烟雾测试。涉及 UI 或报告排版时还应进行实际页面/HTML 视觉检查。
