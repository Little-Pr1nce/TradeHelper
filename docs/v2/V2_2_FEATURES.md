# TradeHelper V2-2 特征层实施规范

> 本文件是 V2-2 的规范性合同、实现任务和 Golden Cases。实现发生冲突时，本文件优先于 `V2_REFACTOR_PLAN.md` 中的概念示例。V2-2 只实现特征层，不得提前实现预测、情景、策略、风控、组合、LLM 或 UI。

## 1. 阶段目标

把 V2-1 的可审计事实转换成同一份、可复现、无未来数据的 `FeatureSnapshot`，供后续预测和策略共同读取。

V2-2 必须做到：

1. 同一输入、同一截止时点、同一特征版本得到完全相同的值和哈希。
2. 历史快照只能使用当时已经完成并可见的日K、新闻和基本面。
3. 盘前/盘中实时价只进入 `current.*`，不得伪装成正式日K或进入历史训练特征。
4. 缺失、样本不足、不可用和不适用必须区分，不能填 `0`、中性值或默认50分。
5. A股和美股使用相同特征合同；市场差异只存在于输入事实、交易日历和字段归一化注册表。
6. 特征层不产生买卖动作、预测概率、执行等级、仓位或风险金额。

## 2. 架构边界

允许依赖：

```text
tradehelper_v2.features -> contracts + V2 data repository/calendar + Python/NumPy
```

禁止：

- import V1 `indicators/`、`alpha/`、`core/`、`strategies/` 或 `services/`。
- 在特征层调用网络 Provider、LLM、预测模型或策略。
- 复制 V1 的 `Final_Score`、IC/IR 动态选权或买卖阈值作为事实特征。
- 使用完整历史样本统计量回写过去特征。
- 把当前 quote 追加成一根正式日K。

V1 代码只用于核对公式和回归案例；V2 必须按本文件重新实现。

## 3. 文件范围

V2-2 建议只新增下列职责文件，避免再次形成巨型模块：

```text
tradehelper_v2/contracts/analysis.py
tradehelper_v2/features/__init__.py
tradehelper_v2/features/technical.py
tradehelper_v2/features/news.py
tradehelper_v2/features/fundamentals.py
tradehelper_v2/features/snapshot.py
tradehelper_v2/features/store.py
```

不要创建 V2-3 以后目录的空占位实现。市场/行业上下文暂由 `FeatureInputs.context` 接收可选事实；V2-1 尚无权威指数和行业序列时必须保持缺失，不在本阶段新增数据源。

## 4. 精确合同

### 4.1 枚举

```text
FeatureStatus:
  available
  missing
  insufficient_history
  stale
  blocked
  not_applicable

FeatureGroup:
  closed_technical
  current_market
  news
  fundamentals
  market_context

FeatureEvidenceMode:
  observed_snapshot             # 当时真实保存的输入快照
  reconstructed_history         # 使用当前canonical历史事实重建
```

### 4.2 FeatureValue

```text
FeatureValue:
  name: str                    # 固定小写点号命名，例如 closed.return_5
  value: float | int | bool | str | None
  status: FeatureStatus
  unit: str | None
  lookback: int | None         # 技术特征使用的交易日数量
  available_at: datetime
  sources: tuple[str, ...]
  model_eligible: bool
  reason: str | None
  schema_version: int = 1
```

不变量：

- `available` 必须有非 NaN、非无限值；其他状态的 `value` 必须为 `None`。
- 非数值特征默认 `model_eligible=False`，编码规则留给 V2-3 模型注册表。
- `current.*` 一律 `model_eligible=False`，只供后续情景/策略读取。
- `reason` 使用稳定代码，不写随意说明文本，例如 `SAMPLE_LT_120`、`NEWS_PROVIDER_EMPTY`。

### 4.3 FeatureInputs

```text
FeatureInputs:
  instrument: InstrumentId
  mode: DecisionMode
  cutoff_at: datetime
  bars: tuple[CanonicalBar, ...]
  quote: QuoteSnapshot | None
  news: tuple[NewsSnapshot, ...]
  news_status: ProviderStatus
  fundamentals: FundamentalSnapshot | None
  fundamentals_status: ProviderStatus
  data_quality: DataQualityReport
  evidence_mode: FeatureEvidenceMode
  context: Mapping[str, FeatureValue] = field(default_factory=dict)
```

输入必须已经通过 V2-1 合同。特征层仍需再次执行截止时点过滤，不能相信调用方已经过滤。
同一输入不得包含重复 `trading_date`；重复正式日K属于合同冲突，不能排序后继续计算。

### 4.4 FeatureSnapshot

```text
FeatureSnapshot:
  instrument: InstrumentId
  mode: DecisionMode
  cutoff_at: datetime
  latest_bar_date: date | None
  quote_observed_at: datetime | None
  feature_set_version: str     # V2-2 初始固定为 "2.2.0"
  evidence_mode: FeatureEvidenceMode
  values: tuple[FeatureValue, ...]  # 按 name 排序且名称唯一
  input_hash: str
  feature_hash: str
  generated_at: datetime
  schema_version: int = 1
```

哈希规则：

- 使用 V2 canonical JSON 和 SHA-256。
- `input_hash` 包含过滤后的 bars/news/fundamentals/quote、模式、截止时点和质量报告，不包含 `generated_at`。
- `feature_hash` 包含 `input_hash + feature_set_version + 排序后的 FeatureValue`，不包含 `generated_at`。
- `generated_at` 是实际构建时间，不得使用历史 `cutoff_at` 冒充快照生成时间。
- 相同输入在不同进程产生相同哈希；事实、截止时点、模式或算法版本变化必须改变相应哈希。

## 5. 截止时点规则

### 5.1 日K

- `eod`：只使用 `trading_date <= latest_completed_session(cutoff_at)` 的正式日K。
- `pre` / `intraday`：只使用当前交易所本地日期之前的正式日K；当日 quote 不得进入 closed 特征。
- 历史回放只使用目标截止日以前的bar。V2-1未保存日K的逐次修订历史，因此用当前canonical序列重建时必须标记 `reconstructed_history`，不能声称是当时真实留存；公司行动版本和来源必须进入 `input_hash`。只有输入事实当时已真实保存，才可标记 `observed_snapshot`。
- 上市日期裁剪沿用 V2-1 结果；样本不足返回 `insufficient_history`。

### 5.2 新闻

- 同时满足 `available_at <= cutoff_at` 和 `published_at <= cutoff_at` 才可进入快照。
- 新闻观察窗口固定为截止时点前30个自然日。
- `ProviderStatus.EMPTY` 表示确认无新闻；`UNAVAILABLE/TIMEOUT/RATE_LIMITED` 表示新闻事实不可用，两者不能混为一谈。
- 没有 FinBERT 标签/分数的新闻计入数量，不进入情绪均值。

### 5.3 基本面

- 只使用 `FundamentalSnapshot.available_at <= cutoff_at` 的最新快照。
- 每个字段还必须满足 `published_at is None or published_at <= cutoff_at`。
- `period_end` 不是可见时间，不能仅凭报告期把后来抓到的财务数字回填历史。
- 同一字段使用 V2-1 已选择并记录的来源，不在特征层再次联网补值。
- 当前实时分析应在本轮数据刷新完成后冻结 `cutoff_at`；不得把分析开始时间直接复用为特征截止时间，否则几秒后返回且刚被首次观察到的事实会被正确判为不可见。历史回放仍严格使用历史截止时点，不能放宽此规则。

基本面 canonical 化必须显式按“canonical 指标 + 来源 + 原始字段/期间”排序，不能依赖 `Mapping` 的字段顺序或字段名排序：

| canonical 指标 | 美股优先定义 | A股优先定义 |
|------|------|------|
| `pe_ttm/pb_mrq/ps_ttm` | Finnhub TTM/MRQ；yfinance 备用 | baostock 交易日估值 |
| `roe` | Finnhub `roeTTM` | 东方财富经 akshare 的年报加权平均 ROE；baostock `roeAvg` 仅为次级简单平均口径 |
| `gross_margin` | Finnhub `grossMarginTTM` | baostock 年报毛利率 |
| `revenue_growth_yoy` | Finnhub `revenueGrowthTTMYoy` | 东方财富经 akshare 的年报营业收入同比；不得用 baostock `MBRevenue` 自行推算 |
| `net_profit_growth_yoy` | Finnhub TTM；无已注册字段时由 yfinance `earningsGrowth` 补充 | baostock 年报净利润同比 |
| `debt_ratio` | Finnhub 债务/资产字段，缺失时保持缺失 | baostock 年报负债率 |

同一来源内也必须按期间优先，例如 Finnhub `peTTM > peNormalizedAnnual`、`pb > pbQuarterly`、`roeTTM > roeRfy`、`grossMarginTTM > grossMarginAnnual`。只有候选值通过单位、发布时间和数值有效性检查后才参与排序；主来源值无效时允许选择有效备用值。

### 5.4 实时快照

- FeatureBuilder 必须按当前 `mode + cutoff_at + observed_at` 重新判断报价新鲜度，不能沿用缓存中相对于旧截止时点计算的 `fresh` 状态。
- 只有重新判断为 `freshness_status=fresh` 且 `observed_at <= cutoff_at + 5分钟` 才生成 `current.*`；盘中最大年龄15分钟，其余模式45分钟。
- stale/future/missing_timestamp 报价生成对应缺失状态，不能退回抓取时间。
- `current.*` 不进入 `input_hash` 的 closed-only 子指纹，但必须进入完整 `input_hash` 和 `feature_hash`。

## 6. 初始特征集合

所有收益和比例都使用小数，例如 5% 为 `0.05`。所有滚动计算只使用截止时点以前的数据。

### 6.1 Closed technical

| 名称 | 公式 | 最小样本 |
|------|------|----------|
| `closed.return_1/5/20/60` | `close_t / close_(t-n) - 1` | `n+1` |
| `closed.ma_5/10/20/60/120` | 最近n日收盘算术均值 | `n` |
| `closed.ma_distance_5/10/20/60/120` | `close_t / ma_n - 1` | `n` |
| `closed.realized_vol_20/60` | 最近n个对数收益样本标准差 `ddof=1 * sqrt(252)` | `n+1` |
| `closed.rsi_14` | Wilder RSI，范围0-100 | 15 |
| `closed.atr_pct_14` | Wilder ATR14 / close_t | 15 |
| `closed.macd_dif_pct` | `(EMA12-EMA26)/close_t`，EMA以首个收盘为seed | 26 |
| `closed.macd_signal_pct` | DIF的EMA9 / close_t | 34 |
| `closed.macd_hist_pct` | `(DIF-signal)/close_t` | 34 |
| `closed.bb_pct_20` | `(close-lower)/(upper-lower)`，总体标准差 `ddof=0` | 20 |
| `closed.bb_width_20` | `(upper-lower)/mid` | 20 |
| `closed.volume_ratio_20` | 当日成交量 / 前20日平均成交量 | 21且均量>0 |
| `closed.gap_1` | `open_t / close_(t-1) - 1` | 2 |
| `closed.high_distance_20/60/252` | `close_t / max(high,n) - 1` | n |
| `closed.low_distance_20/60/252` | `close_t / min(low,n) - 1` | n |
| `closed.drawdown_252` | `close_t / max(close,n) - 1` | 最少60；不足252时lookback记录实际值 |

除 `drawdown_252` 明确允许60至251日降级窗口外，样本不足不得缩短周期或填0。

### 6.2 Current market

| 名称 | 公式/来源 |
|------|-----------|
| `current.price` | 新鲜 quote.price |
| `current.change_from_prev_close` | `price/prev_close-1` |
| `current.ma_distance_20/60/120` | `quote.price / closed.ma_n - 1` |
| `current.spread_pct` | `(ask-bid)/((ask+bid)/2)` |
| `current.volume_vs_daily_20` | quote.volume / 已完成日K前20日均量 |
| `current.retreat_from_session_high` | `price/high-1`，仅quote含high时 |

这些字段只描述当前事实，不生成“支撑有效”“应该锁利”等策略判断。

### 6.3 News

| 名称 | 规则 |
|------|------|
| `news.count_1d/7d/30d` | 对应自然日窗口内新闻数量 |
| `news.source_count_30d` | 30日内去重来源数量 |
| `news.sentiment_weighted_1d/7d` | signed FinBERT confidence 的时间衰减加权均值 |
| `news.sentiment_change` | `weighted_1d - weighted_7d` |
| `news.latest_age_hours` | 最新可见新闻距截止时点小时数 |
| `news.scored_ratio_30d` | 有有效FinBERT标签和分数的新闻占比 |

情绪符号：positive=`+score`、negative=`-score`、neutral=`0`。权重为 `relevance(缺失时1.0) * exp(-ln(2)*age_hours/24)`；分母为权重和。没有可评分新闻时情绪字段为 `missing`，不是0。
没有可见新闻时 `scored_ratio_30d` 为 `missing`；这是未定义的 `0/0`，不能写成0。新闻计数字段仍为0。

### 6.4 Fundamentals

初始规范名称：

```text
fund.pe_ttm
fund.pb_mrq
fund.ps_ttm
fund.roe
fund.gross_margin
fund.revenue_growth_yoy
fund.net_profit_growth_yoy
fund.debt_ratio
```

要求：

- 建立 `source + raw_field -> canonical_name + scale + unit` 白名单注册表。
- Finnhub百分数、baostock小数、yfinance小数和akshare字段必须按白名单转换；禁止用“绝对值大于1就除100”的猜测。
- F08 必须至少有一组测试从真实脱敏 Provider payload 经生产 parser 进入 FeatureSnapshot；不得只构造已经标准化的 `FundamentalValue`。
- `debtToEquity` 不是 `debt/assets`，不得直接映射为 `fund.debt_ratio`。
- 估值负值保留为事实，不自动改成缺失；是否可用于模型由V2-3决定。
- 历史估值分位需要历史 point-in-time 快照；V2-2 没有足够历史时保持缺失，不沿用V1默认50%。
- 不生成 `Fundamental_Score`、`Tech_Normalized_Score` 或 `Final_Score`。

### 6.5 Market context

V2-2 合同保留 `context.*` 命名空间，但当前没有权威指数/行业输入时统一为 `missing/CONTEXT_INPUT_UNAVAILABLE`。不得使用个股自身走势冒充市场或行业环境，也不得在本阶段偷偷新增Provider。

## 7. 持久化

新增 schema migration 5 和 `feature_snapshots`：

```text
instrument_key, code, market, exchange, mode, cutoff_at,
latest_bar_date, feature_set_version, evidence_mode, input_hash, feature_hash,
payload_json, generated_at, schema_version
```

唯一键：`instrument_key + mode + cutoff_at + feature_set_version + input_hash`。

- 相同唯一键/相同payload为幂等成功。
- 相同唯一键/不同payload为确定性冲突，原记录不覆盖并写入 quarantine。
- store 只接受完整 `FeatureSnapshot`，不保存 pandas/Provider dict。
- V2-2 不迁移V1历史特征；历史重建留给后续离线任务。

## 8. Golden Cases

测试文件固定为：

```text
tests/v2/test_feature_contracts.py
tests/v2/test_feature_technical.py
tests/v2/test_feature_point_in_time.py
tests/v2/test_feature_degradation.py
tests/v2/test_feature_store.py
tests/v2/test_feature_market_parity.py
```

### F00 合同与哈希稳定

同一输入构建两次，`input_hash/feature_hash`相同，`generated_at`可不同；values按name排序且唯一。改变一根bar、新闻available_at、基本面字段、模式、cutoff或特征版本，哈希必须变化。

### F01 收益和均线标准答案

使用固定20/60/120根线性价格序列，手工断言 `return_5`、`ma_20`、`ma_distance_20`，不能用实现函数生成预期值。

### F02 技术指标标准答案

使用固定OHLCV fixture，断言 RSI14、ATR14、MACD、Bollinger和volatility，容差 `1e-10`；与独立手算/固定常量比较。

### F03 样本不足

19根bar时MA20、BB20缺失；20根时可用；120根前MA120始终 `insufficient_history`。不得出现0或缩短周期。

### F04 盘中quote隔离

给定T-1日K和当日quote，`current.price/current.ma_distance_20`变化，但所有 `closed.*` 与closed子指纹不变；repository日K行数不变。

### F05 新闻无未来数据

一条新闻published=10:00、available=10:30：cutoff=10:15不可见，10:31可见。后来刷新同一新闻不得让早期历史快照看到它。

### F05B 历史证据等级

使用当前canonical日K重建历史cutoff时必须为 `reconstructed_history`；当时真实保存的完整输入才能为 `observed_snapshot`。两种证据模式必须进入哈希，禁止混为同一快照。

### F06 新闻空与失败不同

`EMPTY + ()` 产生count=0且情绪missing；`UNAVAILABLE + ()` 所有news特征为missing并带 `NEWS_PROVIDER_UNAVAILABLE`。

### F07 基本面无未来数据

period_end为上一季度、available_at晚于cutoff的字段不可见；available_at到达后才可见。缺字段不填0、不填行业中位数。

### F08 基本面单位归一

用Finnhub百分数、baostock小数、yfinance小数fixture得到相同canonical ratio；未知字段/未知单位必须missing并记录原因。

F08 还必须覆盖乱序字段和混合来源：Finnhub TTM/MRQ 字段不得被 Annual/Quarterly 字段或 yfinance 覆盖；A股加权 ROE/营业收入同比选择 akshare 明确定义字段，估值/毛利率/净利润同比/负债率保持 baostock 优先。baostock `MBRevenue` 与发行人营业收入不一致时不得生成 `revenue_growth_yoy`。

### F09 A股/美股合同对称

600519与AAPL使用相同OHLCV synthetic序列时，所有closed技术特征数值相同；差异只能来自输入事实，不允许按market分叉公式。

### F10 新股和停牌样本

SPCX式新股只有18根bar时仍生成快照，但长期特征不足；零成交量日不自动删除，volume特征按质量状态降级。

### F11 FeatureStore

幂等写入不增加行数；冲突不覆盖；按instrument/mode/cutoff/feature_set_version可取回相同合同对象；migration 5重复执行安全。FeatureStore 默认读取其绑定的特征版本，不能隐式跨版本选择。

### F12 架构边界

扫描 `tradehelper_v2/features`，禁止V1业务import、网络调用、LLM、预测、策略和UI依赖。

### F13 性能

本地500根bar、100条新闻、1份基本面生成1000次快照；参考开发机中位耗时不超过10ms/次，测试记录实际值，不能靠网络或缓存掩盖算法耗时。

## 9. 实施顺序

1. 先实现合同、序列化、哈希和合同测试。
2. 实现closed technical及标准答案测试。
3. 实现current隔离和point-in-time过滤。
4. 实现新闻与基本面归一化、缺失诊断。
5. 实现FeatureStore与migration 5。
6. 完成双市场、架构、性能和全项目回归。
7. 更新 `V2_REFACTOR_PLAN.md` 和能力清单后停止。

## 10. 验收命令

```bash
venv/bin/python -m pytest tests/v2/test_feature_contracts.py -q
venv/bin/python -m pytest tests/v2/test_feature_technical.py -q
venv/bin/python -m pytest tests/v2/test_feature_point_in_time.py -q
venv/bin/python -m pytest tests/v2/test_feature_degradation.py -q
venv/bin/python -m pytest tests/v2/test_feature_store.py -q
venv/bin/python -m pytest tests/v2/test_feature_market_parity.py -q
venv/bin/python -m pytest tests/v2/ -q
venv/bin/python -m pytest tests/ -q
```

## 11. 完成标准与停止边界

只有以下条件全部满足，V2-2才能标记完成：

- F00-F13全部通过。
- A股和美股Golden Cases均通过。
- 没有未来新闻/基本面/quote进入closed训练特征。
- 缺失特征没有0值、中性值或默认分位填充。
- FeatureStore迁移、幂等和冲突测试通过。
- V2与全项目回归通过并记录数量。
- 能力清单和阶段状态已更新。

完成后必须停止，不得顺手实现预测模型、ForecastResult、OOF选择、交易策略或UI。V2-3开始前另行复审并制定预测层精确合同。
