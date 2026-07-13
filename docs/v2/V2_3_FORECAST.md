# TradeHelper V2-3 预测层规范

> 状态：设计完成，待实现。本文档是 V2-3 的规范性合同。实现者必须先完成本文列出的测试，不得根据实现结果反向修改 Golden Cases。发生冲突时，本文件优先于 `V2_REFACTOR_PLAN.md` 中的概念示例；V2-0/V2-1 数据合同和 V2-2 特征合同继续有效。

## 1. 阶段目标和边界

V2-3 只负责回答：

1. 从哪个已完成交易日开始预测？
2. 未来第 1/3/5/10 个交易日是哪一天？
3. 到目标日收盘，上涨、震荡、下跌的概率分别是多少？
4. 目标日收益的 P10/P50/P90 区间是多少？
5. 当前输出来自股票、行业、市场 Champion，还是仅来自未验证基线？
6. 过去的 OOF 概率准确性和校准是否足以参与后续执行证据？

V2-3 不负责：

- 不输出买入、卖出、加仓、减仓或持有指令。
- 不生成触发价、止损、止盈、仓位或风险金额。
- 不读取账户、持仓、成本价或可用现金。
- 不使用 LLM 生成概率、收益区间或模型参数。
- 不实现情景、策略、风控、成交仿真、组合分配、预测到期验证、报告或 UI。
- 不把单次实盘结果自动晋升为 Champion，不自动修改 Python 源码。

允许新增的主要边界：

```text
tradehelper_v2/contracts/forecast.py
tradehelper_v2/forecast/
  labels.py
  feature_sets.py
  preprocessing.py
  models.py
  diagnostics.py
  trainer.py
  registry.py
  engine.py
tradehelper_v2/data/migrations/schema.py       # migration 6
tradehelper_v2/data/repository.py              # 预测版本/评估/结果存储
tests/v2/test_forecast_*.py
```

不得创建 `scenario/`、`strategies/`、`risk/`、`learning/`、`reports/` 或 `ui/` 的占位实现。

## 2. 核心原则

1. **预测和策略分离**：预测只描述未来分布，后续 V2-4 才把它翻译成交易情景。
2. **目标日明确**：horizon 表示交易日数量，不是自然日数量。
3. **无未来数据**：每个 OOF 原点只能使用该原点已经到期的标签和当时可见的特征。
4. **股票级自优化**：每只股票、每个 horizon 独立维护模型状态；不能用 AAPL 的 Champion 直接冒充 MU 的 Champion。
5. **概率优先**：主要审计指标固定为多分类 Brier，Log Loss 和 ECE 是校准护栏，不根据结果临时挑指标。
6. **完整但不伪造**：没有可靠模型时仍返回结构化状态和基线证据；没有可用样本时概率与区间为 `None`，不得填 1/3 或 0。
7. **不隐式混模**：股票、行业、市场模型按层级选择；除非混合模型本身通过 OOF，否则不能临时加权平均。
8. **可复现**：训练样本、特征集合、标签政策、预处理、模型参数、随机种子和 artifact 都必须版本化并哈希。

## 3. 精确合同

合同继续使用 `dataclass(frozen=True, slots=True)`、字符串枚举、UTC 时间、有限 float 和 canonical JSON。

### 3.1 枚举

```text
ForecastDirection:
  bullish
  neutral
  bearish

ForecastAvailability:
  available
  insufficient_sample
  data_blocked
  calendar_unavailable
  no_eligible_model

ForecastScope:
  stock
  industry
  market
  baseline

ModelFamily:
  empirical
  analog
  multinomial_logistic
  probability_tree
  ensemble
  regime_analog

ModelLifecycle:
  candidate
  challenger
  champion
  drifted
  retired

ValidationStatus:
  not_evaluated
  insufficient_sample
  evaluated_not_better
  calibration_failed
  selection_passed
  confirmation_passed
  drifted
```

生命周期和验证结论必须分开保存。`challenger` 不等于 `confirmation_passed`，`champion` 必须对应 `confirmation_passed`。

### 3.2 DirectionProbabilities

```text
DirectionProbabilities:
  bullish: float
  neutral: float
  bearish: float
```

不变量：

- 每项在 `[0, 1]` 内且为有限值。
- 三项之和与 1 的误差不超过 `1e-9`。
- 禁止在合同构造时静默归一化错误输入。

### 3.3 ReturnDistribution

```text
ReturnDistribution:
  p10: float
  p50: float
  p90: float
  method: str
```

不变量为 `p10 <= p50 <= p90`。初始 `method` 只允许：

```text
empirical
analog_weighted
class_mixture
ensemble_mixture
```

### 3.4 ForecastDriver

```text
ForecastDriver:
  feature_name: str
  observed_value: float
  winning_probability_effect: float
  direction: ForecastDirection
  rank: int
```

驱动解释使用模型无关的局部替换法：把一个特征替换成训练折中位数，重新预测，计算原始获胜类别概率减去替换后概率。按绝对值取前 5 个。它只解释模型敏感度，不得写成因果结论或交易理由。

### 3.5 ForecastRequest

```text
ForecastRequest:
  feature_snapshot: FeatureSnapshot
  reference_bar: CanonicalBar
  horizons: tuple[int, ...] = (1, 3, 5, 10)
  requested_at: datetime
```

不变量：

- `reference_bar.instrument == feature_snapshot.instrument`。
- `reference_bar.trading_date == feature_snapshot.latest_bar_date`。
- `reference_bar` 必须是前复权、已完成正式日 K。
- `feature_snapshot.mode` 必须是 `DecisionMode.EOD`，其 cutoff 必须对应 reference session 收盘后的合法 point-in-time 截止时点。
- horizons 去重后只能来自 `{1, 3, 5, 10}`，输出按升序。
- `current.*` 即使存在于 FeatureSnapshot，也不得进入预测模型输入。
- 当前 quote 不能替代 `reference_bar.close`。盘前/盘中分析复用最近完成交易日的 EOD 预测；实时价、隔夜增量新闻和报告运行模式留给 V2-4/V2-5 作为情景输入。
- V2-3 不训练盘前/盘中专属模型。没有可回放的历史同时间点快照时，把当前增量事实塞进只用 EOD 历史训练的模型会造成训练/推理口径漂移。

### 3.6 ForecastResult

```text
ForecastResult:
  instrument: InstrumentId
  cutoff_at: datetime
  origin_session_date: date
  target_session_date: date
  horizon: int
  reference_price: float
  availability: ForecastAvailability
  probabilities: DirectionProbabilities | None
  return_distribution: ReturnDistribution | None
  direction: ForecastDirection | None
  confidence_margin: float | None
  model_scope: ForecastScope
  scope_key: str
  model_family: ModelFamily
  model_version: str
  lifecycle: ModelLifecycle
  validation_status: ValidationStatus
  execution_eligible: bool
  feature_set_id: str
  feature_set_version: str
  model_input_hash: str
  training_data_hash: str | None
  sample_count: int
  oof_sample_count: int
  drivers: tuple[ForecastDriver, ...]
  calendar_source: str
  reason: str | None
  event_key: str
  generated_at: datetime
  schema_version: int = 1
```

不变量：

- `availability=available` 时，probabilities、return_distribution、direction 和 confidence_margin 必须存在。
- 其他状态下 probabilities、return_distribution、direction 和 confidence_margin 必须为 `None`。
- `direction` 是最大概率类别；并列时固定优先级为 `neutral > bearish > bullish`，避免无证据时偏向做多。
- `confidence_margin = 最大概率 - 第二大概率`，范围 `[0, 1]`。
- `execution_eligible=True` 仅允许 `lifecycle=champion` 且 `validation_status=confirmation_passed`。
- baseline、industry/market 未确认模型、challenger、drifted 和 insufficient 输出都不能参与后续 A 级执行证据。
- `event_key` 固定为：

```text
instrument.stable_key|origin_session_date|target_session_date|horizon|model_version|model_input_hash
```

同一已完成日 K、同一模型和同一模型输入在盘前/盘中重复分析时不得产生多条逻辑预测。

`ForecastResult` 是独立于页面和报告时段的“天气预测事实”，因此不保存 `requested_mode`。调用方的盘前/盘中/盘后模式属于 V2-4 情景上下文。盘前和盘中如果 origin 都是 T-1，必须读取同一个 ForecastResult；盘后只有在当日正式日 K 完成后才以当日为新 origin 发行下一份预测。

### 3.7 ModelSpec 和 ModelVersion

```text
ModelSpec:
  spec_id: str
  family: ModelFamily
  feature_set_id: str
  hyperparameters: Mapping[str, int | float | str | bool]
  primary_metric: str = "multiclass_brier"
  label_policy_version: str = "direction_v1_vol_scaled"
  preprocessing_version: str = "robust_missing_v1"
  complexity_rank: int

ForecastModelVersion:
  version: str
  scope: ForecastScope
  scope_key: str
  market: Market
  horizon: int
  spec: ModelSpec
  lifecycle: ModelLifecycle
  validation_status: ValidationStatus
  training_start: date
  training_end: date
  selection_start: date | None
  selection_end: date | None
  confirmation_start: date | None
  confirmation_end: date | None
  training_data_hash: str
  artifact_format: str
  artifact_hash: str
  artifact: bytes
  random_seed: int
  created_at: datetime
  promoted_at: datetime | None
  schema_version: int = 1
```

artifact 只允许 canonical JSON 后使用 zlib 压缩，`artifact_format=json+zlib-v1`。禁止 pickle、joblib 或反序列化可执行对象。Logistic 保存 scaler、缺失中位数、缺失指示列、系数、截距和温度；tree 保存节点数组；analog 保存标准化训练向量、标签、收益和权重。

### 3.8 ForecastTrainingSample

训练器不能直接接收一张随意拼接的 DataFrame。标签构造层必须先生成不可变样本合同：

```text
ForecastTrainingSample:
  instrument: InstrumentId
  scope_membership: Mapping[ForecastScope, str]
  origin_session_date: date
  target_session_date: date
  horizon: int
  reference_price: float
  target_price: float
  future_return: float
  flat_band: float
  direction: ForecastDirection
  feature_snapshot: FeatureSnapshot
  feature_hash: str
  evidence_mode: FeatureEvidenceMode
  matured_at: date
  schema_version: int = 1
```

不变量：target 必须是 origin 后第 horizon 个正式交易日；`matured_at=target_session_date`；reference/target 价格来自同市场、同标的、同一前复权 canonical 序列；FeatureSnapshot 的 instrument/latest_bar_date 必须匹配样本。训练器只接收按 origin 排序且无重复 `(instrument, origin, horizon)` 的样本。

样本装配只能读取调用方提供的 FeatureStore、canonical bars、交易日历和 point-in-time metadata，不得在训练期间联网。技术历史可以调用 V2-2 FeatureBuilder 重建并标记 `reconstructed_history`；新闻和基本面只能读取 cutoff 当时已经存在的 repository snapshot。

## 4. 预测目标和标签

### 4.1 目标交易日

- `origin_session_date` 是 `reference_bar.trading_date`。
- `target_session_date` 是对应交易所日历中 origin 之后第 `horizon` 个正式交易日。
- 美股使用美股交易所日历，A股使用 A股交易所日历；不能使用 `date + timedelta(days=horizon)`。
- 日历不可用时返回 `calendar_unavailable`，不得猜目标日期。
- 盘前/盘中使用 T-1 为 origin；盘后使用当日已完成正式日 K 为 origin。

### 4.2 收益定义

```text
future_return(t, h) = adjusted_close(target_session) / adjusted_close(origin_session) - 1
```

origin 与 target 必须使用同一前复权 canonical 序列。标签构造不得使用盘中 quote、未完成日 K 或不同复权口径。

### 4.3 波动率自适应中性区间

V1 固定 `±1%` 会让低波股票过度分类为方向、让高波股票把噪音当趋势。V2-3 固定使用：

```text
daily_sigma(t) = closed.realized_vol_20(t) / sqrt(252)
flat_band(t, h) = clip(0.35 * daily_sigma(t) * sqrt(h), 0.005, 0.04)

bullish: future_return >  flat_band
bearish: future_return < -flat_band
neutral: 其他
```

- `flat_band` 只使用 origin 当时可见的波动率。
- `closed.realized_vol_20` 不可用时，该样本不能生成正式方向标签。
- 标签政策版本固定为 `direction_v1_vol_scaled`，修改公式必须升级版本并重新 OOF，不能覆盖旧模型。

## 5. 模型输入和预处理

### 5.1 初始 FeatureSet

`technical_core_v1`：

```text
closed.return_1/5/20/60
closed.ma_distance_5/10/20/60/120
closed.realized_vol_20/60
closed.rsi_14
closed.atr_pct_14
closed.macd_dif_pct/signal_pct/hist_pct
closed.bb_pct_20/width_20
closed.volume_ratio_20
closed.gap_1
closed.high_distance_20/60/252
closed.low_distance_20/60/252
closed.drawdown_252
```

`news_v1`：V2-2 全部 `news.*` 数值特征。

`fundamentals_v1`：V2-2 全部八个 `fund.*` 数值特征。

候选特征集合固定为：

```text
tech                         technical_core_v1
tech_news                    technical_core_v1 + news_v1
tech_fund                    technical_core_v1 + fundamentals_v1
full                         technical_core_v1 + news_v1 + fundamentals_v1 + 可用context数值特征
```

context 当前缺失时不能用个股自身走势冒充。一个候选只有在历史样本中扩展特征组覆盖率达到 60% 时才进入评估；否则状态为 `insufficient_sample`，tech 候选仍可继续。

### 5.2 模型可用性

- 只读取 `model_eligible=True` 且名称在 ModelSpec 白名单中的 FeatureValue。
- FeatureSnapshot 中不存在、非 available、非数值或非有限值的特征保持缺失。
- `current.*`、原始价格型 `closed.ma_*`、字符串状态和 LLM 观察不得进入 V2-3 初始模型。
- `closed.ma_*` 是绝对价格且跨股票不可比；只允许 `ma_distance_*` 进入模型。
- 估值负值保留为数值事实，不能在模型入口变成缺失。

### 5.3 缺失与缩放

预处理版本 `robust_missing_v1`：

1. 每个 OOF 折只用训练折计算中位数、Q1、Q3。
2. 缺失数值以训练折中位数供模型计算，同时为该字段追加一个 `is_missing` 指示列。
3. 这只是模型内部变换，不得回写 FeatureSnapshot，也不得在报告中显示为真实值。
4. `scaled = clip((x - median) / max(IQR, 1e-12), -8, 8)`。
5. 训练折全缺失的字段从该 ModelSpec 本折移除；如果剩余有效原始特征少于 5 个，该折不产生预测。
6. 测试折、selection、confirmation 和当前推理都只能复用训练折参数。

`model_input_hash` 包含 instrument、origin_session、feature_set/version、预处理版本，以及按名称排序的原始值、status、model_eligible 和 sources；不包含 `current.*`、requested_at、generated_at 或未选择的特征。

## 6. 候选模型池

### 6.1 依赖决策

Multinomial Logistic 和 DecisionTree 使用 scikit-learn 的成熟实现，不再复制 V1 手写梯度下降和手写树。实现时允许新增唯一的预测依赖：

```text
scikit-learn>=1.5,<2.0
```

不得在 V2-3 引入 XGBoost、LightGBM、神经网络、AutoML 或 GPU 依赖。打包体积变化必须记录在阶段状态。

### 6.2 受控候选

单个 scope + horizon 最多评估 20 个 ModelSpec：

| family | 参数空间 | 允许特征集 |
|------|------|------|
| empirical | Laplace alpha=1 | tech，仅作基线，不参与候选数量 |
| analog | k=40/80，距离权重 `1/(d+1e-6)` | tech/full |
| multinomial_logistic | C=0.1/1.0，L2，max_iter=1000 | tech/tech_news/tech_fund/full |
| probability_tree | max_depth=2/3，min_samples_leaf=max(15, 2%训练样本) | tech/full |
| ensemble | analog80 + logisticC0.1，固定 0.5/0.5 | tech/full |
| regime_analog | k=40/80，仅匹配同 regime | tech |

若组合超过 20，按 ModelSpec 的固定 `complexity_rank` 截断，不能根据结果动态增加候选。

### 6.3 Regime 定义

regime 只用于 `regime_analog`，不是市场大盘事实：

```text
trend_sign = sign(closed.ma_distance_20)
vol_bucket = 训练折 closed.realized_vol_20 的 33%/67% 分位桶
regime = down_or_flat/up × low/mid/high_vol
```

分位点只用训练折计算。当前 regime 历史成熟样本少于 30 时，regime_analog 本折不可用。报告必须称为“个股技术状态”，不能称为市场状态。

### 6.4 概率校准

- 原始概率先裁剪到 `[1e-12, 1-1e-12]`。
- 每个 OOF 训练窗口最后 20% 的已成熟样本作为内部校准段，至少 30 条；其余样本训练基础模型。
- 校准段不参与基础模型拟合。
- 使用单参数 temperature scaling，`T` 限制在 `[0.5, 5.0]`，最小化校准段 Log Loss。
- 校准样本不足时不拟合温度，`T=1`，模型仍可评估但不能仅凭 ECE 优势晋升。

### 6.5 收益区间

- empirical：训练成熟收益等权分位数。
- analog/regime_analog：近邻距离权重分位数。
- logistic/tree：对训练收益使用 `weight_i = current_probability[label_i] / class_count[label_i]`，再计算加权分位数。
- ensemble：使用两个成员的收益权重混合，权重与概率混合一致。
- 任一类别训练计数为 0 时使用 Laplace 平滑概率，但该类别没有收益样本时不能伪造收益；退回全部训练收益等权分布并记录 `RETURN_CLASS_EMPTY_FALLBACK`。

## 7. OOF 与无泄漏训练

### 7.1 成熟标签条件

对于预测原点 `o`，训练样本 `s` 只有在以下条件同时成立时可用：

```text
s.origin_session < o.origin_session
s.target_session <= o.origin_session
s.feature_snapshot.mode == DecisionMode.EOD
s.feature_snapshot.cutoff_at <= s.origin_session 收盘后的合法 EOD 截止时点
```

第二条是 purge 规则：即使样本 origin 在过去，只要它的 h 日结果在当前原点还没到期，就不能进入训练。

### 7.2 OOF 结构

- 使用按交易日排序的 expanding-window walk-forward。
- stock 最少 80 条成熟训练样本后才能产生第一个 OOF 点。
- industry/market 最少 200 条成熟训练样本，且至少覆盖 5 只股票。
- 可评估 OOF 点少于 60 时为 `insufficient_sample`。
- 最后 `max(30, ceil(OOF点数 × 25%))` 个点为 confirmation，其余为 selection；selection 也必须至少 30 点。
- 所有候选只在 selection 比较。只能把 selection 排名第一的一个候选带入 confirmation。
- confirmation 不能用于改特征、改标签、调参数、换主要指标或选择另一个候选。
- 同一天的跨股票样本必须作为一个时间组移动，不能把同日股票随机拆到训练和测试两侧。

### 7.3 基线

每个 OOF 点都用该点之前已成熟标签构造经验基线：

```text
p(class) = (class_count + 1) / (total_count + 3)
```

收益区间使用成熟历史收益等权分位数。基线是比较对象和无 Champion 时的观察输出，永远 `execution_eligible=False`。

### 7.4 股票、行业和市场范围

- stock scope_key 使用 `instrument.stable_key`。
- industry scope_key 使用 `market + 经过来源验证的行业名`；行业缺失时不得使用代码前缀猜行业。
- market scope_key 使用 `Market.value`。
- industry/market 训练必须保留 instrument id；同一日期按组 OOF，防止横截面未来泄漏。
- 当前标的可以参与行业/市场历史训练，但其每个样本仍必须满足成熟标签条件。
- 行业归属也必须满足 point-in-time：分类事实的 `fetched_at/available_at` 晚于历史 origin 时，不能把今天的行业标签回填过去。V2-1 没有历史行业快照的标的不能参与正式 industry Champion 训练；允许明确标记为 reconstructed 的研究评估，但不能 confirmation 或 execution eligible。

## 8. 诊断与晋升

### 8.1 指标定义

固定输出：

```text
multiclass_brier = mean(sum((p_k - y_k)^2 for k in 3 classes))
log_loss = mean(-log(clip(p_actual, 1e-12, 1)))
direction_accuracy = argmax正确数量 / 样本数
ece = 10个固定等宽置信度分箱的 top-label ECE，空箱忽略
interval_80_coverage = actual_return落在[P10,P90]的比例
```

同时记录每类样本数、预测类分布、平均 confidence margin、样本起止日期和按技术 regime 分层结果。任何指标都必须带样本数。

### 8.2 配对时间块 Bootstrap

- 比较序列为 `baseline_brier_i - candidate_brier_i`，正数表示候选更好。
- block size 固定为 `max(5, horizon)`。
- 重采样 1000 次。
- seed 由 `scope_key + horizon + spec_id + selection/confirmation + training_data_hash` 的 SHA-256 前 8 字节生成。
- 输出均值、80% CI、90% CI，不允许每次运行随机变化。

### 8.3 Selection 通过条件

必须全部满足：

1. selection 样本数至少 30。
2. candidate Brier 严格小于 baseline Brier。
3. Brier 改进至少达到 `max(0.005, baseline_brier × 1%)`。
4. candidate Log Loss 不高于 baseline `+2%`。
5. candidate ECE 不高于 `max(0.15, baseline_ece + 0.03)`。
6. 80% 区间命中率位于 `[0.65, 0.95]`；区间过宽也不能算校准好。
7. Bootstrap 80% CI 下界不低于 `-0.002`。

通过后状态为 `challenger + selection_passed`。多个通过者按以下顺序唯一选出：Brier、Log Loss、ECE、complexity_rank、spec_id。

### 8.4 Champion 确认条件

selection 唯一胜者在 untouched confirmation 中必须全部满足：

1. confirmation 样本数至少 30。
2. Brier 改进为正。
3. 满足以下任一证据路径：
   - 配对 Bootstrap 80% CI 下界大于 0；或
   - Brier 相对改善至少 5%，且 80% CI 下界不低于 `-0.002`。
4. Log Loss 不高于 baseline `+2%`。
5. ECE 不高于 `max(0.15, baseline_ece + 0.03)`。
6. 80% 区间命中率位于 `[0.65, 0.95]`。
7. 三个方向中至少两个方向在 confirmation 出现；单一行情样本不能形成正式 Champion。

未通过时：

- 样本不足：`insufficient_sample`。
- Brier/Bootstrap 未通过：`evaluated_not_better`。
- Brier 通过但 Log Loss/ECE/区间失败：`calibration_failed`。
- 不能把上述三种都显示成“OOF未通过”。

### 8.5 关于“跑赢基准”

这里比较的是概率预测相对历史频率基线的 Brier，不是要求交易收益跑赢牛市买入持有。预测层不评价交易收益、Alpha、Sharpe 或最大回撤；这些属于策略和联合账。该规则不会因为大牛市难以跑赢指数而机械淘汰预测模型。

## 9. Registry 和当前推理

### 9.1 唯一性

同一 `(market, scope, scope_key, horizon)` 最多一个 champion。晋升必须在一个数据库事务内：

1. 校验候选 artifact/hash/confirmation。
2. 把旧 champion 改为 retired。
3. 把新版本改为 champion。
4. 写 promotion event。

不可原地修改旧 ModelVersion。

### 9.2 选择层级

当前预测按以下顺序选择第一个未 drifted 的 confirmed champion：

```text
stock champion
  -> industry champion
    -> market champion
      -> stock empirical baseline
        -> market empirical baseline
          -> unavailable
```

- 不自动混合不同 scope。
- industry/market Champion 作为 fallback 时，ForecastResult 使用其真实 scope，并 `execution_eligible=False`，直到后续 V2-4/V2-6 明确如何使用跨股票证据。
- stock empirical baseline 至少需要 20 条成熟样本。
- market empirical baseline 至少需要 100 条成熟样本且覆盖 5 只股票。
- 所有基线都必须标记 `lifecycle=candidate`、`validation_status=not_evaluated`。
- 没有任何基线样本时返回 `insufficient_sample`，概率和收益区间为 None。

### 9.3 同股票多周期

1/3/5/10 日拥有独立版本、样本、指标和状态。1日 Champion 不得使 5日结果变成已验证。报告层以后可以展示多周期共识，但 V2-3 不合并方向。

## 10. 持久化和 migration 6

新增：

```text
forecast_model_versions
forecast_model_evaluations
forecast_snapshots
forecast_promotion_events
```

最低约束：

- model version 唯一，artifact hash 必须匹配 artifact bytes。
- evaluation 唯一键包含 model_version + phase + data_hash。
- forecast snapshot 以 event_key 唯一，重复写入幂等；相同 event_key 不同 payload 进入 quarantine，不覆盖。
- promotion events 只追加，不更新。
- repository 读取默认要求明确 horizon/scope/version，禁止“取最新一条”代替明确查询。
- migration 6 重复执行安全，并保留 migration 1-5 数据。
- V2-3 只保存预测发行事实，不验证实际结果；实际价格、对错和预测账归 V2-9。

## 11. 确定性和性能

- 所有随机 seed 从稳定哈希派生，禁止使用当前时间。
- 相同 FeatureSnapshots、ModelSpec、日历和版本必须产生相同概率、区间、drivers 和 hashes。
- Logistic 必须检查收敛状态；未收敛候选记为评估失败，不保存为可用模型。
- Tree 必须固定 random_state，禁止不受控并行。
- 候选评估初始使用单线程，避免桌面应用并发占满 CPU。
- 单只股票 500 个历史快照、4 个 horizon、最多 20 个候选的确定性测试目标为 30 秒内；性能测试不得联网。
- 深度训练将来可放后台，但 V2-3 当前 trainer 必须支持进度回调和取消检查，不实现 UI。

## 12. 数据质量和降级

- `FeatureEvidenceMode.RECONSTRUCTED_HISTORY` 可以用于 technical-only OOF，但模型版本必须记录证据模式。
- 新闻/基本面只有当对应历史 snapshot 在当时已存在时才参与；今天抓到的数据不能回填过去。
- 数据质量 `blocked` 或 reference bar 不匹配时返回 `data_blocked`。
- 新股样本不足不删除；返回 `insufficient_sample` 并保留各 horizon 所需样本数。
- 某个扩展特征组覆盖不足只淘汰相应候选，不影响 technical-only 候选。
- A股和美股使用完全相同的标签、指标、晋升和模型公式；差异只允许来自交易日历、数据事实和 scope。

## 13. Golden Cases

测试命名与规范预期固定如下：

### FC00 合同不变量

概率和不为1、区间乱序、非法 horizon、错误 reference bar、champion 未 confirmation 却 execution eligible 均必须抛 `ContractViolation`。

### FC01 交易日目标

给定含周末和市场假期的 A股/美股日历，1/3/5/10 日目标必须是对应市场的正式交易日，不能用自然日相加。

### FC02 盘前盘中起点一致

同一 T-1 EOD FeatureSnapshot 和同一模型，盘前与盘中 quote 不同仍读取相同 `ForecastResult`，并得到相同 `model_input_hash`、概率和 event_key；ForecastResult 不携带报告模式，当前价不得进入预测。

### FC03 波动率标签

手工计算 low-vol/high-vol 两个 origin 的 flat_band 和三分类标签，结果必须与公式一致；不得退回固定 ±1%。

### FC04 标签成熟

h=5 时，origin 之前四天产生但尚未到期的样本不得进入训练；加入第5个完成交易日后才可用。

### FC05 预处理无泄漏

测试点含极端值时，训练中位数/IQR 不变化；缺失指示列存在，imputed 值不写回 FeatureSnapshot。

### FC06 候选边界

模型只读取显式 FeatureSet；`current.*`、绝对 MA、未登记字段和 LLM 文本均不能进入矩阵。

### FC07 Selection/Confirmation 隔离

构造候选 A 在 selection 最优、候选 B 在 confirmation 最优；系统只能确认 A，不能用 confirmation 换成 B。

### FC08 未跑赢不是样本不足

样本数充足但 Brier 不优于 baseline，状态必须为 `evaluated_not_better`。

### FC09 校准失败

Brier 改善但 Log Loss/ECE 或区间护栏失败，状态必须为 `calibration_failed`，不能晋升。

### FC10 可预测 synthetic

构造只由 `closed.return_5` 决定未来类别的稳定序列，至少一个预注册候选通过 selection 和 confirmation，成为 stock champion。

### FC11 随机 synthetic

随机标签下不得产生 champion；重复运行结果、Bootstrap CI 和状态完全一致。

### FC12 层级 fallback

股票无 Champion 时依次选择行业、市场、经验基线；输出真实 scope，跨股票 fallback 不得标记为强执行证据。

### FC13 多周期隔离

同一股票只有 h=1 通过时，h=3/5/10 仍保持各自状态。

### FC14 双市场对称

A股与美股使用相同 synthetic 特征和等价交易日日历时，标签、模型概率和诊断相同；代码不能按 market 分叉算法。

### FC15 持久化

migration 6 幂等；ModelVersion/ForecastResult 幂等写入；冲突 quarantine；promotion 原子替换唯一 Champion。

### FC16 artifact 安全

artifact 是 canonical JSON+zlib，可校验 hash；禁止 pickle/joblib，损坏 artifact 必须拒绝加载。

### FC17 真实 FeatureSnapshot smoke

使用 V2-2 已保存的 AAPL 和 600519 FeatureSnapshot 运行预测入口。允许因历史样本不足返回 baseline/insufficient，但不得报错、不得跨市场、不得读取网络或 V1 模块。

### FC18 性能和取消

500 点 synthetic 评估满足性能目标；取消回调触发后停止候选搜索，不写半成品 Champion。

## 14. 测试文件

至少新增：

```text
tests/v2/test_forecast_contracts.py
tests/v2/test_forecast_labels.py
tests/v2/test_forecast_feature_sets.py
tests/v2/test_forecast_oof_no_leakage.py
tests/v2/test_forecast_models.py
tests/v2/test_forecast_diagnostics.py
tests/v2/test_forecast_model_registry.py
tests/v2/test_forecast_fallback_hierarchy.py
tests/v2/test_forecast_repository.py
tests/v2/test_forecast_market_parity.py
tests/v2/test_forecast_performance.py
```

架构边界测试必须禁止 `tradehelper_v2/forecast` 导入 V1 `core/forecast_engine.py`、策略、回测、LLM、UI 和报告模块。模型测试不得联网。

## 15. 实施顺序

1. 先实现 forecast contracts、canonical 序列化和 FC00-FC02。
2. 实现标签、目标交易日和成熟样本构造，完成 FC03-FC05。
3. 实现 FeatureSet、训练折预处理和模型 artifact，不先写自动晋升。
4. 实现 empirical/analog/logistic/tree/ensemble/regime 候选和确定性测试。
5. 实现 walk-forward selection/confirmation、指标和 Bootstrap。
6. 实现 registry、fallback 和 atomic promotion。
7. 实现 migration 6、repository 和 forecast snapshot 幂等存储。
8. 完成 FC00-FC18、V2 全量、全项目回归和真实 FeatureSnapshot smoke。
9. 更新 README、V1 能力清单和 V2_REFACTOR_PLAN 阶段状态后停止。

不得为了让 synthetic 测试通过而在测试后修改标签、晋升阈值或候选空间。若设计常量确实需要调整，必须先修改本文、解释金融与统计原因，再同步固定预期。

## 16. 验收命令

```bash
venv/bin/python -m pytest tests/v2/test_forecast_*.py -q
venv/bin/python -m pytest tests/v2/ -q
venv/bin/python -m pytest tests/ -q
```

完成 V2-3 后必须停止。V2-4 情景层开始前另行制定 `TradingScenario` 精确合同，不得在 ForecastResult 中提前塞入交易动作。
