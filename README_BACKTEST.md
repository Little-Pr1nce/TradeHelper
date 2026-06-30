# TradeHelper 回测与策略审计

> 当前基线：2026-06-30。本文件描述现在的 `strategies/`、`backtest/`、`core/strategy_audit.py` 和 `core/strategy_pool.py`，不是早期三策略实现。

## 1. 核心原则

1. 策略只输出 `StrategyDecision`，订单统一由 `decision_to_orders()` 生成。
2. 回测和当前信号共享同一条 Decision 路径，不维护平行的 buy/sell 逻辑。
3. T 日收盘产生订单，T+1 开盘撮合，禁止按 T 日收盘成交。
4. 参数晋升只使用 walk-forward 样本外证据，不使用全样本“最优值”。
5. 回测结果是历史条件模拟，不等于未来收益保证。

## 2. 运行方式

### UI 主路径

Tab1 和 Tab3 调用 `core.pipeline.run_pipeline()`：前台回测已晋升正式参数并生成当前计划，随后通过后台单线程调度深度参数优化。

### CLI Demo

```bash
venv/bin/python run_backtest.py \
  --code AAPL \
  --start 2024-01-01 \
  --end 2025-12-31 \
  --strategy A,B,C \
  --capital 100000
```

`run_backtest.py` 是轻量演示入口，直接拉取指定日期数据并绕过 Service 缓存。当前 `--strategy all` 仍默认 A/B/C；需要其他策略时显式传入字母列表。完整产品流程以 UI/Service 管道为准。

## 3. 策略接口

```python
decision = strategy.generate_decision(df_until_t, context)
orders = decision_to_orders(decision, context)
```

`StrategyDecision` 关键字段：

| 字段 | 含义 |
|------|------|
| `action` | `buy/sell/hold/watch/invalid` |
| `execution_level` | A 可执行、B 小仓、C 观察、D 驳回 |
| `trigger_price` | 触发或参考价格 |
| `stop_loss/take_profit` | 止损和止盈；没有止盈时不伪造风险收益比 |
| `max_loss_amount` | 按账户权益与持股计算的计划最大亏损 |
| `position_pct` | 建议仓位占真实账户权益比例 |
| `invalidation` | 计划失效条件 |
| `missing_conditions` | 当前未触发时还差的真实策略条件 |

所有 20 个策略都实现原生 Decision-first 和 `diagnose_no_signal()`。

## 4. 策略组

| 组别 | 定位 |
|------|------|
| A-H | 百分位趋势、均值回归、新闻动量、突破、海龟、均线与 MA60 趋势 |
| O | 趋势满仓对标基准，不等同于默认推荐策略 |
| I-N | 人类常见行为/形态策略，用于比较、审计与诊断 |
| P | MA120 支撑反弹，所有分析追加 |
| Q | 已有持仓的冲高回落锁利 |
| R | 已有持仓的成本、亏损和集中度风控 |
| S | 已有持仓跌破均线后的反抽失败退出 |
| T | 统一条件触发计划，所有分析追加 |

P/T 的 `overlay_scope="always"`，Q/R/S 的 `overlay_scope="position"`。覆盖策略不依赖历史审计通过才显示，但执行等级仍受数据质量和历史证据限制。

## 5. 回测时序

```text
T 日收盘
  -> df[:T] 生成 StrategyDecision
  -> decision_to_orders()

T+1 开盘
  -> 涨跌停/停牌/交易单位检查
  -> 动态滑点与佣金
  -> 买卖撮合

T+1 盘中
  -> High/Low 检查硬止损和策略止损
  -> 若开盘已越过止损，按更差的开盘价成交

T+1 收盘
  -> 更新最高收盘、权益曲线和持仓状态
```

策略在第 `i` 根 K 线只允许访问 `df.iloc[:i+1]`。未来数据可以预先存在于完整 DataFrame，但策略调用必须获得截止当前的切片。

## 6. 撮合约束

### 动态滑点

基础滑点为 0.3%，再按下单时已知的历史年化波动率增加，波动附加上限为 0.7%。订单超过当日成交量容量时增加流动性惩罚。

这仍是 Level-1/日线条件下的保守近似，不代表真实盘口冲击成本。

### 跳空止损

```text
open >= stop 且 low <= stop  -> stop 成交
open < stop                  -> open 成交
```

以前的 `max(stop, low)` 会在跳空场景高估成交价，当前已移除并有回归测试。

### 市场规则

规则统一来自 `utils/market_rules.py`：

| 规则 | A 股 | 美股 |
|------|------|------|
| 最小交易单位 | 100 股 | 1 股 |
| T+1 | 是 | 否 |
| 佣金 | 万三、最低 5 元 | 当前保守模型万三 |
| 卖出税费 | 0.05% | 0 |
| 涨跌幅 | 主板 10%、创业/科创 20%、北交所 30%、明确 ST 5% | 无 |

可靠的实时停复牌/ST 数据仍是待完善项。

## 7. Alpha 因果性

历史回测的 `Final_Score` 只使用当时可得的技术因子和衰减新闻。当前基本面、盘口、实时价和用完整样本计算的 IC/IR 调权只作用于最新决策点。

测试保证“修改未来价格不能重写过去的技术评分”。因子 `?` 表示样本不足：保留先验权重，但验证覆盖率会进入可信度闸门。

## 8. 绩效指标

`BacktestResult` 包含：

- 总收益、年化收益、夏普、Calmar、最大回撤。
- 胜率、盈亏比、平均持仓天数、交易次数。
- 权益曲线、成交记录、闭合交易和扩展 metrics。

策略比较还计算 Rank IC、基准买入持有收益和策略间相关性。报告必须区分回测绩效与当前可执行价格。

## 9. 样本外策略审计

`core/strategy_audit.py` 将时间序列前 70% 作为训练期、后 30% 作为验证期：

| 结果 | 含义 |
|------|------|
| PASS | 样本数、验证收益、夏普、胜率和回撤达到门槛 |
| CONDITIONAL | 有一定证据但不足以直接执行 |
| FAIL | 验证失败、过拟合或风险不可接受 |

审计额外执行 400 次循环分块 Bootstrap：

- 正期望概率。
- 收益和夏普 95% 区间。
- 最大回撤 P95。
- 30% 级别大回撤概率。

验证期日收益少于 20 条或交易少于 3 笔时，Bootstrap 状态为样本不足，不输出伪精度概率。

## 10. 参数候选生命周期

`core/strategy_pool.py` 扩展策略参数变体并运行 walk-forward。候选状态存储在 `strategy_param_candidates`：

```text
candidate
  -> confirmed across different data_end windows
  -> promoted
  -> replaced / rolled_back
```

晋升要求同时满足样本外收益、样本外夏普、交易次数和跨窗口确认。正式参数存入 `per_stock_params`。

当历史健康度转为负期望时：

1. 当前参数标记降级或回滚。
2. 前台不再把它当作可信冠军参数。
3. 系统生成更保守的 recovery 候选。
4. recovery 候选必须重新通过 walk-forward 才能晋升。

自动优化不会直接修改策略源码，也不保证所有策略最终都变成正期望。无法建立正期望证据的策略应保持降级或停止执行。

## 11. 缓存和后台优化

`bt_variant_cache` 的键包含股票、策略、参数、日期范围、账户资金和数据/Final Score 签名，防止错误复用。`deep_optimization_runs` 防止同股票、同数据截止日重复提交。

前台流程：正式参数回测、审计、当前信号、可信度摘要和报告。

后台流程：几十组参数变体、walk-forward、候选确认和晋升/回滚。后台为单线程，避免多个 Tab1/Tab3 任务同时争抢 CPU 和 SQLite。

## 12. 历史信号复盘

买入和卖出健康度分开：

- 买入：实际净收益、方向、MFE、MAE。
- 卖出：后续 1/3/5/10/20 日表现、继续下跌、反弹、避免损失和机会成本。

只有已触发、可验证且事件键唯一的记录进入学习。C/D 级观察不进入可执行策略健康度。

## 13. 测试

```bash
venv/bin/python tests/test_backtest.py
venv/bin/python tests/test_strategy_decisions.py
venv/bin/python tests/test_strategy_audit.py
venv/bin/python tests/test_strategy_pool.py
venv/bin/python tests/test_scoring.py

# 全部
venv/bin/python -m pytest tests/ -q
```

当前全项目基线为 173 个测试通过。回测相关回归覆盖：

- T/T+1 时序和无未来函数。
- Decision 到 Order 的统一转换。
- A 股交易单位、涨跌停、T+1、佣金和税费。
- 动态滑点、成交量未知态和跳空止损。
- 20 个策略原生 Decision 与未触发诊断。
- 样本外审计、Bootstrap 和参数候选生命周期。
