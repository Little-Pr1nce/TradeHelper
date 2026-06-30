# TradeHelper

基于 Python 3.12 和 Flet 的跨平台股票分析桌面应用，面向 A 股与美股，提供单股研究、组合持仓管理、条件触发交易计划、策略回测、历史预测评估和 HTML/PDF 报告。

> 当前基线：2026-06-30。系统的目标是提高决策的一致性、可解释性和风险可控性，不承诺盈利，也不能替代券商成交回报或用户的最终决策。

## 当前能力

| 模块 | 当前实现 |
|------|----------|
| 单股分析（Tab1） | 盘前、盘中、盘后三种模式；技术面、基本面、新闻、策略审计、条件计划和 LLM 研究员解读 |
| 我的持仓（Tab3） | 真实账户余额、持仓成本、行内编辑、关注列表、组合风险预算、调仓优先级和全宽报告 |
| 条件化建议 | 买入/加仓、卖出/减仓、持有、失效条件、止损、止盈、最大亏损金额和仓位比例 |
| 风控官 | A/B/C/D 执行等级，结合事实一致性、数据质量、账户风险、样本外审计和历史期望 |
| 20 个策略 | A-H、O、I-N，以及 P-T 条件触发与持仓风控覆盖策略 |
| 回测可信度 | `StrategyDecision -> Order` 统一实盘/回测路径，T+1 撮合、动态滑点、跳空止损、市场规则和 Bootstrap 区间 |
| 自动优化 | walk-forward 参数候选、跨窗口确认、晋升、回滚、负期望恢复候选和后台深度优化 |
| 历史学习 | 买入与退出信号分开复盘，记录 1/3/5/10/20 日表现、MFE/MAE、策略健康度和形态表现 |
| 数据质量 | OHLC、样本量、上市日期、实时价新鲜度、新闻时效和因子验证覆盖率共同形成交易闸门 |
| 跨平台打包 | macOS `.app`、Windows 目录版 `.exe`，Windows 构建包含运行时烟雾测试 |

## 快速开始

### 环境要求

- Python 3.12+
- macOS、Windows 或 Linux
- TickFlow API Key：盘中实时行情需要；免费层可获取日 K 线
- Finnhub API Key：美股新闻、基本面和上市日期建议配置
- OpenAI 兼容 LLM：当前首次配置要求填写，用于研究员解读

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Web 模式仍属于实验功能：

```bash
flet run main.py --web
```

### 配置位置

配置文件位于系统标准应用目录：

- macOS：`~/Library/Application Support/TradeHelper/config.json`
- Windows：`%APPDATA%\TradeHelper\config.json`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/TradeHelper/config.json`

主要字段：`work_dir`、`llm_base_url`、`llm_api_key`、`llm_model`、`stock_token_us`、`stock_token_a`、`news_token_us`、`news_token_a`、`llm_enable_thinking`。

## 四个页面

| 页面 | 定位 |
|------|------|
| 分析 | 单只股票的完整研究工作台；生成后进入 K 线和报告全宽阅读区 |
| 历史报告 | 查询、查看、评分和导出已生成报告 |
| 我的持仓 | 组合级工作台；管理余额、持仓和关注列表，并生成组合操作手册 |
| 设置 | 工作目录、LLM、行情和新闻数据源配置 |

Tab3 不是 Tab1 的简单批量版。它额外考虑成本价、浮盈亏、现金、单票集中度、组合相关性、禁止加仓、减仓/止盈/止损和关注股替换机会。

## 三种分析模式

| 模式 | 决策数据 | 输出重点 |
|------|----------|----------|
| 盘中 | 正式历史 K 线 + 当次实时 OHLCV 内存快照 | 当日可执行条件；实时快照不会写入 `price_history` |
| 盘前 | 盘前价 + T-1 历史数据 | 开盘后的触发、止损、失效和两套风险方案 |
| 盘后 | 当日已完成收盘数据 | 下一交易日的条件计划和历史验证 |

## 策略系统

所有注册策略原生返回 `StrategyDecision`：

```text
action + execution_level + trigger_price + stop_loss + take_profit
+ max_loss_amount + position_pct + invalidation + missing_conditions
```

回测、Tab1、Tab3 和报告都通过同一个 `decision_to_orders()` 边界生成订单。

| 组别 | 策略 |
|------|------|
| A-H | 百分位趋势、均值回归、新闻动量、布林突破、Dual Thrust、海龟 ATR、均线交叉、MA60 趋势 |
| O | 趋势满仓对标基准 |
| I-N | 追涨、抄底、回本、趋势回调、关键反转、均线粘合突破 |
| P-T | MA120 支撑、冲高锁利、持仓风险、反抽失败退出、统一条件触发计划 |

P/T 对所有分析追加条件观察；Q/R/S 只在用户存在真实持仓时加载。覆盖策略由类的 `overlay_scope` 元数据自动发现。

## 执行等级

| 等级 | 含义 | 默认动作 |
|------|------|----------|
| A | 事实成立、风险可控、历史证据支持 | 可执行，但仍由用户确认 |
| B | 事实成立、风险可控、样本或历史证据不足 | 小仓验证 |
| C | 事实成立但风险/历史期望不支持 | 仅观察 |
| D | 数据冲突、实时价失效或事实不可验证 | 驳回 |

A 级买入必须有与当前股票和策略匹配的正期望历史证据。硬止损和风险减仓不会因为买入样本不足而被阻止。

## Alpha 模型

技术面由 RSI、DIF、MACD 柱、布林 `%B`、K/D/J 七个因子组成，使用滚动标准化、`tanh` 压缩和 IC/IR 验证。未知 `?` 级因子保留研究先验权重，但报告会显示“未验证”；验证覆盖率低于 50% 时降低执行可信度和仓位上限。

有基本面时，最新时点使用行情自适应权重：

| 行情 | 技术 | 风格 | 基本面 | 新闻 |
|------|-----:|-----:|-------:|-----:|
| 强趋势高波 | 40% | 5% | 30% | 25% |
| 慢涨/弱趋势 | 38% | 10% | 27% | 25% |
| 震荡/过渡 | 35% | 15% | 25% | 25% |

历史回测只使用当时可得的技术面和衰减新闻得分；当前基本面、盘口和实时快照只影响最新决策点，避免把今天的信息写回过去。

## 数据源

| 数据 | A 股 | 美股 |
|------|------|------|
| 日 K 线 | TickFlow | TickFlow |
| 盘中实时价 | TickFlow | TickFlow |
| 盘前/盘后价 | 不适用 | Nasdaq.com，失败后 yfinance |
| 基本面 | baostock，失败后 akshare/LLM | Finnhub，失败后 yfinance/akshare/百度/LLM |
| 新闻 | 东方财富（akshare） | Finnhub 个股新闻 + 市场新闻 |
| 上市日期 | baostock | Finnhub `profile2` |

上市日期会限制历史拉取和缓存读取范围，上市前数据不参与分析。Tab1 与 Tab3 共用新闻缓存，但各自执行新鲜度检查和主动刷新，不互相依赖。新闻 TTL 按时段区分：盘中约 30 分钟、盘前约 1 小时、盘后约 6 小时。

## 系统架构

```text
UI (ui/)
  -> Services (services/analysis_service.py, portfolio_service.py)
    -> Core (pipeline, signal_check, audit, strategy_pool, data_quality)
      -> Engines (alpha, indicators, strategies, backtest)
        -> Support (data, report, config, utils)
```

前台使用已晋升正式参数快速生成报告；几十组候选参数的 walk-forward 深度优化在报告返回后由后台单线程执行，避免 Tab1/Tab3 同步等待。

## 数据库

SQLite 使用 WAL 模式，当前 14 张表：

`stocks`、`price_history`、`reports`、`news_sentiment`、`news_refresh_state`、`holdings`、`watchlist`、`account_balance`、`prediction_log`、`bt_variant_cache`、`per_stock_params`、`strategy_param_candidates`、`deep_optimization_runs`、`research_observation_log`。

数据库初始化会自动建表和执行兼容迁移。预测与研究员观察使用稳定 `event_key` 去重，重复运行同一事件不会虚增学习样本。

## 测试

项目测试文件可由 pytest 运行，也都支持直接执行：

```bash
venv/bin/python -m pytest tests/ -q

# 与打包环境一致的无 pytest 入口
for f in tests/test_*.py; do venv/bin/python "$f" || exit 1; done
```

当前基线为 **173 个测试通过**，覆盖数据源边界、新闻缓存、Alpha 因果性、20 个策略、Decision-first 路径、撮合、策略审计、参数生命周期、预测追踪、组合功能和可信度摘要。

## 打包

```bash
bash scripts/build_macos.sh       # dist/mac/TradeHelper.app
scripts\build_windows.bat         # dist\win\TradeHelper\TradeHelper.exe
```

Windows GitHub Actions 使用 `.github/workflows/build-windows.yml`。本地 Windows 脚本和远程工作流都会在产物上传前执行打包运行时烟雾测试。macOS 打包流程保持独立，不受 Windows hidden imports 调整影响。

## 文档

| 文件 | 用途 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 当前架构、数据流、核心约束和扩展点 |
| [README_BACKTEST.md](./README_BACKTEST.md) | 回测、审计、撮合和参数生命周期 |
| [UPGRADE_PLAN.md](./UPGRADE_PLAN.md) | 五阶段可信交易建议升级主线和完成度 |
| [AGENTS.md](./AGENTS.md) | 本仓库编码代理工作约定 |
| [CLAUDE.md](./CLAUDE.md) | Claude Code 项目上下文 |
| [OPTIMIZATION_REPORT.md](./OPTIMIZATION_REPORT.md) | 早期审计历史及问题处理状态，不代表当前代码现状 |

## 当前未完成重点

- LLM 观察候选的更多形态模板、命中率图表和 UI 明细查询。
- 历史预测评估面板的全局筛选、钻取和图表化。
- 更长模拟盘观察期和相对基准超额收益门槛。
- 可靠的停复牌/ST 数据，以及美股延伸时段流动性细化。
- Web 版完善和打包体积优化。

详细完成度以 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md) 为准。
