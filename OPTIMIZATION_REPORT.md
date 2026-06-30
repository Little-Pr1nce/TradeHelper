# TradeHelper 代码逻辑优化报告

> **历史审计存档，不代表当前代码现状。** 本报告记录项目早期架构（约 5,000 行、旧 `analysis/` 信号实现）发现的问题。2026-06-30 的当前架构已经迁移到 `core/ + strategies/ + backtest/ + services/`，请先阅读 [README.md](./README.md)、[DESIGN.md](./DESIGN.md) 和 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md)。
>
> 当前基线：20 个 Decision-first 策略、14 张 SQLite 表、13 个测试文件、186 个测试通过。下面保留原始问题描述用于追溯，不应把其中的文件路径、策略数量或“尚无测试”等表述当作当前事实。

## 当前处理状态（2026-06-30）

| 早期问题类别 | 当前状态 |
|--------------|----------|
| 卖出股数、连续买入和回测时序 | 已由事件驱动 `BacktestEngine`、`Broker` 和 `StrategyDecision -> Order` 统一路径替代，并有回归测试 |
| 固定滑点和止损成交过于理想 | 已增加历史波动/成交量动态滑点；跳空越过止损按开盘价成交 |
| 报告图路径与报告记录 | `reports.chart_path/pdf_path` 已持久化，历史报告可读取 |
| 日期、损坏配置和数据库迁移 | 已增加跨平台配置恢复、幂等补列、去重后唯一索引和新旧用户迁移测试 |
| SQLite 跨线程 | 当前数据库层使用线程锁和 WAL；后台深度优化限制为单线程 |
| K 线和新闻缓存 | K 线按正式完成日增量更新并隔离实时快照；新闻有独立刷新状态和分时段 TTL |
| 新闻未持久化/反复 FinBERT | 已持久化并只分析新增项；Tab1/Tab3 独立刷新、共用缓存 |
| 策略重复算指标和 Python 百分位循环 | 指标统一预计算；滚动百分位已向量化 |
| 取消和长任务体验 | UI 有分阶段进度；参数深度优化移到报告返回后的后台任务 |
| 缺少测试 | 已建立 13 个可直接执行的测试文件，当前 186 个测试 |

仍在进行中的工作包括 Web 版、打包体积、可靠停复牌/ST 数据、美股延伸时段流动性和历史评估 UI 深度。具体状态见 [UPGRADE_PLAN.md](./UPGRADE_PLAN.md)。

> 评审范围：项目根目录下全部 Python 模块（约 5 000 行），聚焦"代码逻辑层"（正确性、健壮性、复杂度、模块边界、并发与资源），不涉及 UI 视觉与功能扩展。
> 评审日期：2026-05-26

---

## 一、整体评价

项目结构清晰，模块边界划分合理，"扩展点"标注友好，README/DESIGN 文档充足。
但深入到代码逻辑后，存在多处**会直接影响结果正确性**的缺陷（回测、报告写入、缓存命中），以及若干健壮性 / 性能问题。建议按下表优先级处理。

| 等级 | 类别 | 问题数 |
|------|------|--------|
| 🔴 P0 — 影响正确性的 Bug | 6 |
| 🟠 P1 — 健壮性 / 资源 | 8 |
| 🟡 P2 — 性能 / 重复实现 | 5 |
| 🟢 P3 — 可读性 / 死代码 | 6 |

---

## 二、P0 — 影响正确性的关键 Bug

### 1. `analysis/backtest.py` 卖出记录 `shares` 永远是 0
位置：`backtest.py:140-152`

```python
shares = 0.0  # 清仓        ← 在追加 trade 之前就清零
sell_signals += 1
trades.append({
    ...
    "shares": round(shares, 2),   ← 这里永远是 0
    ...
})
```

后果：交易明细里所有 sell 记录的 `shares` 都是 0，下游展示和审计都会失真。
修复：先在临时变量 `sold_shares = shares` 中保存，再清零：
```python
sold_shares = shares
cash += sell_amount - cost
shares = 0.0
trades.append({..., "shares": round(sold_shares, 2), ...})
```

### 2. 模拟器对"连续 buy 信号"的处理与注释不符
位置：`backtest.py:117-138`，注释见 `_simulate` docstring

注释写着 *"连续同方向信号：第二个 buy 在已有持仓时忽略"*，
但代码只判断 `cash > 0`。每次 buy 都会以剩余现金的 95% 再加仓，
导致一段上行行情里出现"3 次 buy + 1 次 sell"且仓位被无限拆分，
直接污染收益率、胜率和交易笔数。
修复：把入场条件改成 `signal == "buy" and shares == 0 and cash > 0`。

### 3. `analysis/sentiment.py` `local_files_only=True` 与 README 自相矛盾
位置：`sentiment.py:34`

```python
_sentiment_pipeline = pipeline(
    ...,
    local_files_only=True,    # ← 禁用任何下载
)
```

而 README 声明*"首次运行时需从 HuggingFace 下载 FinBERT 模型 ~300MB"*。
后果：在没有手动预热模型缓存的情况下，FinBERT 永远加载失败，
**情感分析永远跑兜底关键词方案**，用户却以为在用 FinBERT。
修复：去掉 `local_files_only=True`，并在加载日志里区分"首次下载/缓存命中/兜底"。

### 4. 历史报告页面 K 线图永远加载不到
位置：`ui/history_page.py:221-227`

```python
if report.pdf_path:
    chart_path = report.pdf_path.replace(".pdf", ".png")
```

实际命名规则：
- 图：`{code}_{ts1}.png`（`report/chart.py:182`）
- PDF：`report_{code}_{ts2}.pdf`（`report/pdf_exporter.py:186`）

两者前缀不同 + 时间戳不同，**永远不匹配**，历史详情页右侧 K 线图永远不可见。
修复：在数据库 `reports` 表新增 `chart_path` 字段，写报告时同步保存；或在 PDF/图命名时绑定同一时间戳。

### 5. `utils.helpers.get_backtest_dates` 长周期可能在闰年抛异常
位置：`helpers.py:158`

```python
start = today.replace(year=today.year - max(1, days // 365))
```

当今天是 2024-02-29 且 period=`"1y"` 时，`replace(year=2023)` 抛 `ValueError: day is out of range for month`。
另外，对于 `days < 365` 的分支，第 158 行的 `replace(...)` 是无效计算。
修复：统一使用 `today - timedelta(days=days)`，或对长周期使用 `dateutil.relativedelta`。

### 6. `config.Settings.init` JSON 损坏时抛 `UnboundLocalError`
位置：`settings.py:73-86`

```python
if cls._config_path.exists():
    try:
        ...
        instance = cls()
        instance._data.update(saved)
    except (json.JSONDecodeError, IOError):
        instance = cls()    # ← 缩进在 try 内？
else:
    ...
return instance
```

实际查看：`except` 内创建 `instance` 是缩进在 `if` 块里的，
但**该实例创建在 `try-except` 内部**，且 `instance._data.update(saved)` 抛错后不会重置 `_data`。更关键的是，没有 logger 提示用户配置文件损坏。
修复：包一层 try/except，损坏时改名备份并落到默认配置；同时打印 warning。

---

## 三、P1 — 健壮性 / 资源管理

### 1. `ui/main_page.py` 出现重复方法定义（dead override）
位置：`main_page.py:465-488`

`_show_result_error` 和 `_show_results` 各被定义了**两次**，第二次覆盖第一次。说明开发过程中曾经修过 bug 但没删旧版本。
- 第一次定义：`L465-475`（无打印、单一职责）
- 第二次定义：`L477-488`（多了 `print` + 异常打印）

后果：第一份代码完全是死代码，且 `print` 语句残留，看不出哪份是"权威实现"。
修复：删除前一份，将 `print` 改为 `logger.error` 或直接删除。

### 2. SQLite 连接全局共享，存在跨线程 race
位置：`data/database.py:142`

`check_same_thread=False` 让连接可以跨线程使用，但所有写入直接走 `self.conn.execute(...)` + `self.commit()`，
分析线程（写 stocks / price_history / news / reports）和 UI 线程（读 reports / 评分写入）共享同一连接，
WAL 只能保护读读并发，**写写**仍会偶发 `database is locked`。
修复：要么加 `threading.Lock` 包裹所有写操作，要么按线程懒加载新连接（`sqlite3.connect` 是廉价的）。

### 3. `Database._connect` 重连分支不开 WAL / 不开外键
位置：`data/database.py:154`

```python
@property
def conn(self):
    if self._conn is None:
        self._conn = sqlite3.connect(Settings().db_path)  # ← 没 PRAGMA
    return self._conn
```

修复：把 `_connect()` 主体抽成方法 `_open(path)`，两处都用它。

### 4. 价格缓存命中策略漏判"周期变长"
位置：`ui/main_page.py:343-364`

逻辑：
1. `prices = get_prices(code, start, end)`
2. 如果 `len(prices) < 5` 或 `prices[-1].date < today-7d` 才联网

问题：用户先选了 `3m`（DB 里只存了 90 天），再切到 `3y`，此时
`get_prices(code, start_3y, end)` 仍然返回 90 条，**`len >= 5` 且最新日期是今天**，于是被判为"缓存够用"，**直接拿 3 个月的数据当 3 年回测**，结果完全不可信。

修复：对每个区间用 `get_latest_price_date` + 与 `start_date` 对比，缺哪段补哪段；或在缓存校验里加上"实际首日 vs 期望首日"的对比。

### 5. 新闻从未持久化进 SQLite
`data/database.py` 建了 `news_sentiment` 表并提供 `insert_news()`，但 `news_fetcher.py` 和 `main_page.py` 都没调用它。每次分析都会重新拉取 + 重新跑 FinBERT，浪费时间 / 触发 API 限流。
修复：分析后调用 `Database().insert_news(news_list)`；fetch 前先查 DB（如 24 小时内的新闻直接复用）。

### 6. `analysis/sentiment.py` 单行 pipeline 调用没切批
位置：`sentiment.py:71`

把整个 `texts`（最多 15 条）一把丢给 transformers pipeline，配合 `max_length=512` + 中文标题，CPU 上 5–10 秒可控；但如果某天加大新闻量到 50–100 条，单次推理会卡死 UI 线程的回调。
建议：拆批 16 条一次，或显式限制 `batch_size=8`。

### 7. `report/generator.py` LLM 调用是阻塞 + 无流式
位置：`generator.py:166-204`

`requests.post` 和 `client.chat.completions.create` 都是 `timeout=600`（10 分钟）的同步调用。期间用户只能看一个静态进度条。如果 LLM 端挂掉，UI 线程一直卡到超时。
修复：
- 改为 `stream=True`，逐 token 写入 UI（Flet 的 Markdown 控件支持频繁 update）。
- 在分析 `_stop_flag` 路径里也要能取消请求（`requests.Session` + `request.get_response().close()`）。

### 8. 取消按钮无法真正中断耗时操作
位置：`ui/main_page.py` `_stop_flag` 检查点

`_stop_flag` 只在分析线程里"步骤之间"被检查，但每步内部（`fetch_price_history` / `analyze` / `generate_kline_chart` / `generate_report`）都是不可中断的同步调用。点 stop 后还要等当前步骤跑完，体感差。
修复：把"步骤"切成更小粒度（如 LLM 流式时每收到 chunk 检查），或用 `concurrent.futures` + 真正的取消 token。

---

## 四、P2 — 性能 / 重复实现

### 1. 策略类大量重复实现已有指标
位置：`analysis/strategy.py` 多处

`MACDStrategy.generate_signals` / `RSIStrategy.generate_signals` / `BollingerBandsStrategy.generate_signals` 都自己重新算了 EMA、RSI、布林带——而 `analysis/technical.calc_all_indicators` 已经把这些列加到了 DataFrame。

调用链中 `_run_analysis` 会先调 `calc_all_indicators`，再把 `df.copy()` 喂给 `strategy.generate_signals`，导致**每次分析都重复计算 1 次同样的指标**。

修复：策略只生成 signal，不再算指标；如果列缺失再算。或者把指标列名约定为接口（`dif`/`dea`/`rsi`/`bb_upper`...）。

### 2. 信号生成全是 Python for-loop，可向量化
`technical.generate_signals`、`MACrossoverStrategy`、`MACDStrategy`、`RSIStrategy`、`BollingerBandsStrategy`、`TripleMACrossoverStrategy` 全部使用：

```python
for i in range(1, len(result)):
    if result["x"].iloc[i] > ... and result["x"].iloc[i-1] <= ...:
        result.loc[result.index[i], "signal"] = "buy"
```

3 年日线 ≈ 750 行，慢不了；但每次都触发 pandas 的标签写入是不必要的开销。
修复：统一改为：
```python
prev_fast, prev_slow = ma_fast.shift(1), ma_slow.shift(1)
result["signal"] = np.where((ma_fast > ma_slow) & (prev_fast <= prev_slow), "buy",
                  np.where((ma_fast < ma_slow) & (prev_fast >= prev_slow), "sell", ""))
```

### 3. `technical.py` 中 `ta` 库副作用未使用
位置：`technical.py:304-332`

`summarize` 里算了 `adx` / `atr` 并写回 `df_ta`，但**这个 `df_ta` 是局部变量**，从未被读取或追加到摘要里，整段代码是计算空转。
修复：要么删掉，要么把 ADX/ATR 真的写进 summary（"趋势明显 / 震荡"判断的好抓手）。

### 4. `Database` 单例缺批量"upsert news after analyze"接口
导致每次新闻分析后逐条写入会变慢；可借助现有 `insert_news` 走批量。结合上面 P1#5 一并修。

### 5. K 线图每次都生成完整文件
位置：`report/chart.py`

`charts/` 目录会无限增长，没有清理策略。建议加按股票/按日期保留 N 份的策略，或在删除报告时联动删除对应 chart/PDF。

---

## 五、P3 — 可读性 / 死代码

| # | 位置 | 问题 |
|---|------|------|
| 1 | `analysis/technical.py:239-272` `generate_signals` | 没人调用，被各策略类内联实现替代 |
| 2 | `utils/helpers.py:18-41` `is_valid_stock_code` | 全局没人调用，逻辑被 `main_page.py` 的本地正则替代 |
| 3 | `utils/helpers.py:62-83` `format_date` | 仅在 `get_backtest_dates` 内自调，外部从未使用 |
| 4 | `data/models.py:147` `NewsItem.from_dict` | `confidence` 字段做特判，但其他数值字段（`AnalysisReport.rating`）也需要做特判却没做（数据库返回 `None` 时直接传给 `int` 没问题但语义不一致） |
| 5 | `report/pdf_exporter.py:249` 粗体识别 | 仅识别 `**xxx**` 整行的情况，行内 `**xx**`、列表里包含粗体都会原样输出 `**` |
| 6 | `report/pdf_exporter.py:_parse_markdown_sections` | 只按 `## / #` 切分，三级标题 `###` 全被并入正文，结构信息丢失 |

### 其他可读性建议
- `main_page.py:_run_analysis` 长达 100+ 行，建议拆分 `_fetch_data` / `_compute_indicators` / `_run_backtest` / `_persist_report` 四个私有方法。
- 数字魔法常量集中到 `analysis/constants.py`：`BUY_RATIO=0.95`、`COMMISSION=0.0003`、`TRADING_DAYS=252`、`SIGNAL_PLOT_OFFSET=0.03` 等。
- 日志格式不带 trace id / 股票代码上下文，查问题时多个分析交错难以归并。可在 `_run_analysis` 入口拼一个 `extra={"code": code}`，统一注入 logger。

---

## 六、安全 / 配置

1. **`config/config.json` 明文存 `llm_api_key`**：默认放在 `~/.tradehelper/`，权限 644，普通用户无害；若以后做团队共享要改为 keyring。
2. **`Settings.is_configured` 仅检查 LLM**：但 `data_source=custom` 下 `custom_api_key` 没空校验。`generate_report` 里 LLM 也只判 `api_key`，`base_url` 可被注入恶意端点导致 SSRF（本地工具风险低，但仍建议 allowlist scheme）。
3. **`stock_fetcher._apply_proxy`**：每次调用都 `yf.set_config(session=...)` 创建新 session，无清理；如果用户切换代理，旧 session 还活着。建议仅在首次设置一次，或显式 `yf.set_config(session=None)` 重置。

---

## 七、修复优先级建议

按照"投入产出比 + 用户感知"排序：

| 顺序 | 问题 | 工作量 | 收益 |
|------|------|------|------|
| 1 | P0-1 卖出 shares=0 | 3 行 | 交易明细立刻可信 |
| 2 | P0-2 重复 buy 加仓 | 5 行 | 回测指标立刻可信 |
| 3 | P0-3 FinBERT 不可下载 | 1 行 | 情感分析真正生效 |
| 4 | P0-4 历史 K 线不显示 | 加 1 列 + 写入 | 历史页可用性大幅提升 |
| 5 | P1-1 main_page 重复方法 | 删除 + 改 print | 减少误读 |
| 6 | P1-4 缓存命中漏判 | 30 行 | 长周期回测才准 |
| 7 | P1-5 新闻持久化 | 30 行 | 二次分析速度 5×↑ |
| 8 | P2-1 策略不再重算指标 | 80 行 | 单次分析提速 ~30% |
| 9 | P0-5 / P0-6 边角 bug | 各 5 行 | 防御性 |
| 10 | P1-7 LLM 流式 | 中 | 体验质变 |

---

## 八、附：建议的下一步

1. 引入 `tests/` 目录：至少为 `BacktestEngine`、`calc_*`、`get_backtest_dates`、`Settings.init` 各加一组样例数据下的快照测试（`pytest` + 固定随机种子）。
2. 引入 `ruff` + `mypy --strict-optional`：当前多处 `Optional` 但未做 None 检查（如 `_run_analysis` 里 `bt_result['total_return']` 假设永远存在）。
3. 按 DESIGN.md 的"扩展点"标注，把未实现的 `CustomStockFetcher` 写一个最小 demo（如调用一个 mock 服务器），否则该路径无法被发现回归。
4. 给 `_run_analysis` 加 step 计时日志（`time.perf_counter`），方便后续优化拍点。

---

> 报告完。如需将 P0 级修复合并为一个 patch，可基于本文件第二章逐条直接应用。
