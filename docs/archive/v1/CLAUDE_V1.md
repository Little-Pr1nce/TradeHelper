# CLAUDE.md

本文件为 Claude Code 在 TradeHelper 仓库中工作时提供当前项目约定。架构与业务细节以 [DESIGN.md](./DESIGN.md) 为准，五阶段进度以 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md) 为准。

## 项目概述

TradeHelper 是 Python 3.12 + Flet 的跨平台股票分析桌面应用，支持 A 股和美股的单股分析、组合持仓、新闻情感、20 策略回测、条件触发计划、历史预测评估和 HTML/PDF 导出。

当前基线：2026-07-05，14 个测试文件、249 个测试。

## 常用命令

```bash
# 安装与启动
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py

# Web（实验性）
flet run main.py --web

# 完整测试
venv/bin/python -m pytest tests/ -q

# 测试文件也支持直接执行
for f in tests/test_*.py; do venv/bin/python "$f" || exit 1; done

# 打包
bash scripts/build_macos.sh
scripts\build_windows.bat
```

修改 Python 后至少运行相关测试文件；涉及共享管道、策略接口、数据库或报告路径时运行完整测试集。

## 架构

```text
UI (ui/)
  -> Services (analysis_service, portfolio_service, news_service)
    -> Core (pipeline, signal_check, audit, strategy_pool, data_quality)
      -> Engines (alpha, indicators, strategies, backtest)
        -> Support (data, report, config, utils)
```

关键约束：

- `Settings` 和 `Database` 是单例，应用启动时初始化。
- 四个页面放在 Flet `Stack` 中，通过 `visible` 切换并保留状态。
- Service 层负责网络和数据库 I/O；`core/pipeline.py` 接收准备好的数据并执行计算。
- 历史 `df` 用于回测；含实时临时 K 线的 `decision_df` 用于当前建议和报告。
- 盘中实时快照只存在内存，禁止写入 `price_history`。
- Tab1 与 Tab3 可共用缓存，但不能互相依赖；两者都必须独立刷新新闻和行情。
- 前台使用正式参数快速分析；walk-forward 深度优化在报告返回后后台单线程执行。

## 策略接口

当前注册 20 个策略：A-H、O、I-N、P-T。所有策略必须原生实现：

- `generate_decision(df, context) -> StrategyDecision`
- `diagnose_no_signal(df, context)`
- `name`、`description`、`suitable_regimes`

`StrategyDecision` 支持 `buy/sell/hold/watch/invalid`，并携带执行等级、触发价、止损、止盈、最大亏损、仓位、失效条件和缺失条件。

订单只能通过 `decision_to_orders()` 生成。不要为回测和当前信号维护两套策略逻辑。

覆盖策略通过类元数据声明：

- `overlay_scope="always"`：P/T。
- `overlay_scope="position"`：Q/R/S。

新增覆盖策略不要在 pipeline 或 signal_check 中硬编码策略字母。

## 数据源原则

| 数据 | A 股 | 美股 |
|------|------|------|
| 历史/盘中行情 | TickFlow | TickFlow |
| 延伸时段 | 不适用 | Nasdaq.com -> yfinance |
| 基本面 | baostock -> akshare -> LLM | Finnhub -> yfinance/akshare/百度 -> LLM |
| 新闻 | 东方财富（akshare） | Finnhub 个股新闻 + 市场新闻 |
| 上市日期 | baostock | Finnhub profile2 |

所有降级都要记录来源和原因。上市日期必须限制缓存读取、网络请求和回测窗口，不得用上市前数据填充新股历史。

新闻通过 `services/news_service.py` 统一刷新：盘中/盘前/盘后 TTL 约为 30 分钟/1 小时/6 小时。空结果必须有过期时间，不能形成永久空缓存。

## Alpha 与可信度

- 技术因子：RSI、DIF、MACD 柱、布林 %B、K/D/J。
- IC/IR 等级 A/B 全权、C 半权、D 剔除、? 保留先验权重但标记未验证。
- 因子验证覆盖低于 50% 时进入数据质量观察闸门。
- 当前基本面、盘口和实时数据只能影响最新决策点，禁止回写历史分数。
- 新闻/基本面只能通过 `feature_context_snapshots` 的真实抓取时点进入历史研究；覆盖不足时禁止加入预测 OOF。
- 策略健康度按独立交易日去重并按证据质量折算有效样本，重复报告不能虚增胜率。
- A 级买入需要当前股票+策略的真实正期望证据；风险退出不受买入证据门槛阻止。
- LLM 可以提出观察，但不能直接生成可执行交易指令。

## 回测约束

- T 日收盘生成 Decision/Order，T+1 开盘撮合。
- 只能使用下单时点已知的数据。
- 跳空越过止损按开盘价成交；盘中穿越按止损价。
- 动态滑点只能使用历史波动和当日可得成交量。
- A 股交易单位、T+1、费用和涨跌幅由 `utils/market_rules.py` 统一提供。
- 参数只能经 walk-forward 候选、超额收益/风险调整双通道、跨窗口确认、20天影子观察、晋升和回滚流程进入正式参数。
- 预测模型 Challenger 只能来自受控候选空间，并同时通过选择/确认两个 OOF 窗口；联合 OOF 漂移只能降低新开仓，不能阻止风险退出。

## 数据库

SQLite WAL，当前 20 张表：

`stocks`、`price_history`、`intraday_price_history`、`reports`、`news_sentiment`、`news_refresh_state`、`holdings`、`watchlist`、`account_balance`、`forecast_log`、`feature_context_snapshots`、`forecast_model_versions`、`trade_plan_log`、`joint_oof_runs`、`prediction_log`、`bt_variant_cache`、`per_stock_params`、`strategy_param_candidates`、`deep_optimization_runs`、`research_observation_log`。

新增字段或表时：

1. 修改 `CREATE_TABLES_SQL`。
2. 为旧数据库添加幂等迁移。
3. 新用户和旧用户都必须走同一初始化路径。
4. 唯一索引创建前处理历史重复数据。
5. 添加迁移和 CRUD 测试。

## 配置

配置位于系统标准应用目录的 `TradeHelper/config.json`。当前字段：

`work_dir`、`llm_base_url`、`llm_api_key`、`llm_model`、`stock_token_us`、`stock_token_a`、`news_token_us`、`news_token_a`、`finbert_model_path`、`llm_enable_thinking`。

新增配置需同步 `config/settings.py::DEFAULT_CONFIG` 和 `ui/settings_ui.py`。

## 修改纪律

- 先读实际代码，注释和旧文档只能作为线索。
- 优先复用现有模块和结构化接口。
- 不在用户未要求时重置、覆盖或清理现有工作区改动。
- 数据、策略、账户权益或市场规则相关修改必须补回归测试。
- 报告中的风险收益比必须由真实止盈/止损计算；没有止盈目标时写“不可量化”。
- 用户权益为 0 时不得回退到虚构本金生成新开仓建议。
- 更新功能后同步 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md) 的落地状态和五阶段完成度。
