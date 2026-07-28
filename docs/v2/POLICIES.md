# TradeHelper V2-0/V2-1 确定性政策

> 本文件固定 V2-0/V2-1 的数据源路由、交易时段、缓存、质量和存储政策。实现不得使用“合理默认值”替代本文件中的明确规则。没有规定的行为应返回不支持或保持缺失，而不是自行扩展。

## 1. 实施授权边界

本轮只实施 V2-0 和 V2-1。通过全部对应测试并更新阶段状态后必须停止，不得顺手开发特征、预测、策略、风控、报告或 UI。

V2-0 完成条件：

- `` 与 `tests/v2/` 可导入。
- 冻结时钟、假交易日历、脚本化 Provider 和 A股/美股 fixture 可复用。
- 架构边界测试能发现 V2 对 V1 业务模块的非法 import。
- 性能基线只测本地确定性计算。

V2-1 完成条件：

- [CONTRACTS.md](./CONTRACTS.md) 中 V2-1 合同全部实现并有校验。
- 本文件的数据源路由、缓存、质量、上市日期和存储规则全部有测试。
- [GOLDEN_CASES.md](./GOLDEN_CASES.md) 中标记为 V2-0/V2-1 的案例全部通过。
- A股和美股覆盖对称；差异只能出现在 Provider、日历和市场适配层。
- 不存在实时价写入正式日 K、虚构 10 万本金、LLM 补造事实或 Tab1/Tab3 页面依赖。
- TickFlow、Nasdaq、yfinance、Finnhub 和 baostock/akshare 适配器均通过脱敏真实响应 fixture 解析测试。
- 配置可用时运行 opt-in 真实数据源烟雾；未运行或失败时阶段只能记录“工程测试通过，真实源验证待完成”，不能标记 V2-1 完成。

## 2. 架构依赖规则

允许的 import 方向：

```text
contracts -> Python 标准库
config    -> Python 标准库
data      -> contracts + config + 第三方数据 SDK
tests/v2  -> tradehelper + 测试库
```

禁止：

- `contracts` import `data` 或任何上层模块。
- `data` import V2 的 features/forecast/scenario/strategies/risk/reports/ui。
- V2 主链 import V1 的 `core`、`services`、`strategies`、`backtest`、`report`、`ui`、`alpha`、`indicators`、`utils` 或 `data` 业务模块。
- 通过动态 import、`sys.path` 修改或复制 V1 巨型函数规避边界测试。

唯一例外是 `data/compatibility.py`。V2-0/V2-1 默认不使用该例外；如确需启用，必须列出允许的单个外部 I/O 客户端、替换目标、删除日期和对应测试，且不得把 V1 返回对象泄漏到 V2 合同之外。

## 3. 数据源路由

Provider 解析测试使用保存在 `tests/v2/fixtures/providers/` 的脱敏真实响应，fixture 不得包含 Token、Cookie、用户路径或账户数据。默认单元测试不访问互联网。

可选真实源测试命令固定为：

```bash
TRADEHELPER_LIVE_TESTS=1 venv/bin/python -m pytest tests/v2/integration/test_live_providers.py -q
```

真实源测试读取本机 V2 设置但不得输出凭据。首次迁移开发期可以显式设置 `TRADEHELPER_LIVE_SETTINGS_PATH` 为本机已有 JSON 配置进行只读 smoke；这仅是测试输入，不是 V2 运行时对 V1 配置的依赖。至少验证 AAPL 和 600519：名称非空、最近完成日 K 日期不晚于权威日历、OHLC 合法、source/observed_at/fetched_at 完整、上市日期不晚于第一条合法日 K。延伸时段测试仅在美股实际 pre/post 时运行，否则标记 skip 而非伪造结果。

### 3.1 A股

| 用途 | 主源 | fallback | 主源失败后的行为 |
|------|------|----------|------------------|
| 代码和名称 | baostock | TickFlow（日K元数据兜底） | 保留 attempts；都失败则元数据不可用 |
| 上市日期 | baostock | 无 | `listing_date=None`，明确缺失 |
| 正式历史日 K | TickFlow 前复权 | 无 | 返回 unavailable，不静默换源 |
| 盘中实时 | TickFlow | 无 | 该股票实时决策阻断，其他股票继续 |
| 盘前 | 无连续盘前实时价 | 无 | 使用 T-1 正式收盘生成条件计划，不伪造报价 |
| 基本面 | baostock | akshare | 按字段语义补充；都失败则缺失，不调用 LLM 补值 |
| 新闻 | 东方财富（经 akshare 标准化） | 无独立次级源 | 空结果按负缓存 TTL 处理 |

A股基本面不是“baostock 整包成功后停止”的粗粒度 fallback。估值、毛利率、净利润同比和负债率优先使用 baostock；`MBRevenue` 只保留为 `main_business_revenue` 原始事实，不得推算 canonical 营业收入同比。官方加权平均 ROE 与营业收入同比使用东方财富财务指标（经 akshare）补充，并保留字段来源、报告期与公告时间。不同会计定义不得仅因字段名称相似而互相覆盖。

### 3.2 美股

| 用途 | 主源 | fallback | 主源失败后的行为 |
|------|------|----------|------------------|
| 代码和名称 | Finnhub profile2 | TickFlow（日K元数据兜底） | 保留来源与 fallback 原因 |
| 上市日期 | Finnhub profile2 | 无 | `listing_date=None`，明确缺失 |
| 正式历史日 K | Nasdaq 历史 OHLCV（经券商K线核验） | yfinance `auto_adjust=True` -> TickFlow 前复权 | 只接收最近已完成交易日及以前的数据，并保留 source/fallback 证据 |
| 常规盘中实时 | TickFlow | 无 | 禁止用 Nasdaq/yfinance 延伸报价冒充盘中价 |
| 盘前/盘后延伸报价 | Nasdaq.com | yfinance `prepost=True` | 都失败则延伸报价不可用 |
| 基本面 | Finnhub | yfinance -> akshare -> 百度可验证页面 | 每个字段保留来源；LLM 不能补值 |
| 新闻 | Finnhub 个股新闻 + 市场新闻 | 无 | 空结果按负缓存 TTL 处理 |

美股 Finnhub 原始 payload 即使覆盖四类名字，也不能仅凭模糊字段匹配判定 canonical 基本面完整。缺少已注册的净利润增长字段时继续使用 yfinance 按字段补充；`debtToEquity` 不等于债务/资产比，缺少可靠 `debt_ratio` 时保持缺失。

硬规则：

- TickFlow 不用于美股盘前或盘后延伸报价。
- Nasdaq 历史 OHLCV 是美股已完成日K的 V2 主源：对当前用户组合已完成券商K线、拆股股与新股交叉验证。它无显式复权参数，V2 必须保留 `source=nasdaq`、交易所日历与公司行动审计；不得与 TickFlow/yfinance 的单根日K静默混拼。
- Nasdaq/yfinance 延伸报价不用于常规盘中报价。
- yfinance 日 K fallback 只能覆盖已由交易所日历确认完成的会话。
- fallback 成功不隐藏主源失败；`ProviderResult.attempts` 必须保留完整顺序。
- LLM 不出现在任何 Provider 路由中。

## 4. 交易模式和时间

### 4.1 用户模式优先

用户显式选择的 `DecisionMode` 是本次分析口径，不允许旧报价时间戳把 `pre` 模式重新分类成前一日 `intraday`。

| DecisionMode | 数据截止口径 | 当前价要求 | 计划有效期 |
|--------------|--------------|------------|------------|
| `pre` | T-1 正式日 K + 当日盘前可见事实 | 美股需要延伸报价；A股可无连续报价 | 当日常规交易会话 |
| `intraday` | T-1 及以前正式日 K + 当前实时快照 | 必须有新鲜 TickFlow 价格 | 当日剩余常规会话 |
| `eod` | 最近已完成正式日 K | 不要求实时价 | 下一交易日常规会话 |

美股在盘后运行 `eod` 时，可附带 Nasdaq -> yfinance 盘后报价作为“延伸时段上下文”，但预测参考价、数据截止日和正式日 K 仍使用官方收盘；延伸价不得改写收盘价。

### 4.2 市场时区和日历

- A股时区：`Asia/Shanghai`；正式日历：`XSHG`，用于沪深北共同休市日判断。
- 美股时区：`America/New_York`；正式日历：`XNYS`，用于美股共同休市日判断。
- 合同内部时间统一 UTC；展示时才转换到交易所时区。
- 正式目标日只由 `exchange_calendars` 或注入的同等权威日历生成。
- 日历不可用时返回明确错误，不允许工作日近似进入正式预测或正式完成日判断。
- A股常规交易时段为 09:30-11:30、13:00-15:00；集合竞价只标记为 pre，不声称存在连续可成交价。
- 美股盘前 04:00-09:30、常规 09:30-16:00、盘后 16:00-20:00，均以美东时间表示。

## 5. 新鲜度、缓存和失败恢复

### 5.1 报价新鲜度

允许时间戳最多领先当前 UTC 5 分钟；超过则为 `future`，不可用。

| 报价用途 | 最大年龄 | 过期行为 |
|----------|----------|----------|
| 常规盘中 TickFlow | 15 分钟 | 该股票实时计划阻断 |
| 美股盘前/盘后延伸报价 | 45 分钟 | 只保留 T-1/正式收盘条件计划 |
| `eod` 正式收盘 | 由交易所日历确认 | 不使用 quote TTL |

### 5.2 数据缓存 TTL

| 数据 | TTL/失效规则 |
|------|--------------|
| 新闻 `intraday` | 30 分钟 |
| 新闻 `pre` | 60 分钟 |
| 新闻 `eod` | 6 小时 |
| 基本面成功结果 | 24 小时 |
| 元数据（名称/行业） | 7 天 |
| 权威上市日期 | 成功获取后长期有效；代码复用或来源冲突时重新验证 |
| empty/timeout 负缓存 | 5 分钟 |
| rate-limit 负缓存 | Provider 给出 `retry_at` 时缓存至该时刻；否则 5 分钟 |
| 已完成历史日 K | 按交易日缺口增量检查，不按普通 TTL 重拉全部历史 |

刷新失败时，历史缓存可供人工查看，但是否进入当次分析由 `available_at/fetched_at` 和用途判断；不得因为数据库里“有旧数据”就标记 fresh。

Provider 未返回可解析的报价观察时间时，允许用 `fetched_at` 保持对象结构完整，但 `freshness_status` 必须为 `missing_timestamp`，不得把抓取时间冒充市场观察时间。美股延伸主源出现缺失或过期时间戳时，数据层尝试 yfinance；两者均无新鲜时间证据时只保留不可执行事实。

新闻的 `published_at` 表示来源声称的发布时间，`available_at` 表示系统首次实际获得该新闻的时间，必须满足 `available_at >= published_at`。历史查询只允许读取 `available_at <= as_of` 的记录；重复刷新保留最早首次可见时间和最新抓取内容，防止回放使用未来新闻。

Tab1 和 Tab3 使用同一刷新服务和缓存，但二者都必须调用 `get_or_refresh`。缓存命中可以避免网络请求，页面调用顺序不得成为数据存在的前提。

### 5.3 Provider 配额

- TickFlow 实时报价每批最多 5 只、每个市场订阅每滚动 1 分钟最多 10 次请求。V2 组合入口将同市场标的合并成最多 50 只的一轮；A股与美股订阅独立，因此两次独立调用合计最多可尝试 100 只。超过单市场 50 只的部分明确返回 `rate_limited`，不得假装有实时价；同一分钟再次请求由 V2 持久化预算拦截，不向 Provider 发送第 11 次请求。
- TickFlow A股正式日K是单标的请求，每滚动 1 分钟最多 10 次。组合刷新必须先使用 V2 repository 的已有完成日K并只补尾部缺口；配额事件在 V2 repository 中全局共享，Tab1/Tab3/重启后的新服务实例不得各自重新计算额度。第11只及以后，A股写入持久化 `refresh_queue` 与 `pending_retry_at`，在下一配额窗口由 `refresh_due_daily_bars` 续跑，绝不保存为空日K或伪造质量失败。美股正式日K不消耗这一 TickFlow 配额，使用 Nasdaq 历史主源。
- Nasdaq 同时最多 2 个请求。
- Finnhub、新闻和基本面共享的网络预取同时最多 2 个请求。
- Finnhub profile、metric 与 company-news 共享每滚动 1 分钟 60 次的持久化总预算。冷缓存组合超过当前窗口时，后续元数据/上市日期/基本面/新闻写入 `provider_refresh_queue` 并在下一窗口由 `refresh_due_provider_facts` 自动补全；`rate_limited` 不得降级成 `unavailable` 或 `empty`。
- 默认单次网络超时 10 秒。
- 只对网络错误、429 和 5xx 重试；总尝试最多 3 次，默认退避 1 秒、2 秒。429 有 `Retry-After` 时遵守，但单次最多等待 30 秒。
- 单元测试注入无等待 retry scheduler，不实际 sleep。

### 5.4 完成日K跨源漂移审计

- 正常分析只使用既定主源/fallback 路由，不为比较而额外消耗配额。
- 维护任务可显式比较 Nasdaq 主序列与 yfinance/TickFlow 的同一美股已完成会话；只比较共同日期，记录 OHLC 最大绝对差、成交量比例、来源和观察时间。
- 价格差超过 `0.01` USD 或成交量比例偏离超过 `2%` 时标记 `drift`，否则标记 `match`。这只是可追溯的数据质量证据，不自动改变主源、复权口径或历史数据。

## 6. 上市日期和历史窗口

- 有权威上市日期时，有效开始日为 `max(requested_start, listing_date)`。
- 网络请求、缓存读取、指标输入、回测和预测训练都必须使用同一有效开始日。
- 已存在的上市日前日 K 移入 quarantine 并记录原因 `before_listing_date`，不得只在 UI 隐藏。
- Provider 新返回的日K也必须在入库前验证上市日期与交易所正式会话；上市日前记录以 `before_listing_date`、休市日记录以 `trading_date_not_in_exchange_calendar` 写入 quarantine。主源返回不等于可直接入库。
- 上市日期未知时不得猜测；保留全部来源可验证的历史，但质量报告增加 `LISTING_DATE_MISSING`。
- 新股样本不足是能力不足，不是数据造假。仍可提供当前价格和条件观察，但不能伪造 MA120 或长期 OOF 证据。

## 7. 数据质量确定规则

### 7.1 去重和评分

质量问题按 `code + field + source` 去重后评分：

```text
初始分 100
block            每项 -35，且最终状态强制 blocked
warning          每项 -8
optional_missing 每项 -4
info             不扣分
```

没有 block 时：

```text
score >= 85        ok / normal / multiplier 1.0
70 <= score < 85   watch / watch / multiplier 0.8
score < 70         degraded / reduce_position / multiplier 0.5
```

存在任一 block 时：`blocked / block_new_entries / multiplier 0.0`。数据质量只能维持或降低后续执行等级，不能升级建议。

### 7.2 日 K 问题代码

强制 block：

- `EMPTY_DAILY_BARS`
- `MISSING_REQUIRED_COLUMN`
- `NON_POSITIVE_PRICE`
- `INVALID_OHLC`
- `NEGATIVE_VOLUME`
- `CONFLICTING_DUPLICATE_BAR`
- `UNSUPPORTED_ADJUSTMENT_MODE`
- `TRADING_DATE_INVALID`

warning：

- `ZERO_VOLUME_RATIO_HIGH`：0 成交量比例大于 20%。
- `DATE_ORDER_INVALID`：输入顺序非递增；repository 可排序，但必须记录。
- `PRICE_JUMP_REVIEW`：相邻收盘绝对涨跌超过 25%。超过 60% 仍先作为 warning 并核对公司行动/来源；只有确认复权口径冲突或来源数据不一致时才升级为 block。
- `SAMPLE_LT_20`、`SAMPLE_LT_60`、`SAMPLE_LT_120`：分别影响能力，不把有效新股日 K 判为伪造数据。

能力：

```text
daily_price         至少 1 条有效日 K
short_technical_20  至少 20 条
medium_technical_60 至少 60 条
ma120               至少 120 条
```

### 7.3 报价问题代码

- `REALTIME_PRICE_MISSING`：需要实时价的模式下 block 该股票。
- `REALTIME_STALE`：超过模式最大年龄，block 该股票实时执行。
- `REALTIME_FUTURE_TIMESTAMP`：领先超过 5 分钟，block。
- `REALTIME_TIMESTAMP_MISSING`：Provider 未提供可验证观察时间，block 实时执行，不得用抓取时间替代。
- `QUOTE_OHLC_PARTIAL`：warning；价格条件仍可用，冲高回落/日内触线不可验证。
- `QUOTE_VOLUME_MISSING`：warning；依赖成交量的形态不可验证。
- `BID_ASK_MISSING`：延伸时段 optional_missing，不声称有盘口深度。

一只股票 blocked 不能阻断 Tab3 其他股票，也不能被其他股票的完整报价带着通过。

### 7.4 延伸时段流动性代理

没有 Level2 时不得虚构盘口。新开仓仓位倍率上限：

| 可用证据 | 条件 | 倍率上限 |
|----------|------|----------|
| bid/ask | spread <= 0.2% | 0.75 |
| bid/ask | 0.2% < spread <= 0.5% | 0.50 |
| bid/ask | spread > 0.5% | 0.25 |
| 只有有效 volume | 无 bid/ask | 0.50 |
| 只有新鲜 price | 无 bid/ask/volume | 0.25 |

这是流动性替代约束，不等同于真实盘口深度。

### 7.5 可选数据缺失

- 新闻缺失和基本面缺失分别产生 `optional_missing`，不得补默认利好/利空。
- 缺少新闻或基本面不使价格事实失效，但对应能力为 false。
- LLM 只能解释“缺失”，不能创建确定性字段或提升质量分。

## 8. Repository 和数据库政策

- V2 开发期只写 `{work_dir}/tradehelper_v2.db`。
- V1 `{work_dir}/tradehelper.db` 在 V2-0/V2-1 中只读，不执行 `ALTER/DELETE/UPDATE/INSERT`。
- V2 schema 当前迁移版本为 4；基础事实对象的合同 `schema_version` 仍为 1。数据库至少包含独立的 metadata、daily_bars、intraday_bars、quote_snapshots、news、fundamentals、account_snapshots、quarantine 和 schema_migrations 存储边界。
- 正式日 K 唯一键为 `instrument_key + trading_date + adjustment_mode`。
- 相同唯一键、相同 canonical payload 重复写入为幂等成功；不同 payload 为 conflicting duplicate，原记录不覆盖并进入 quarantine/冲突审计。
- Quote 写入 quote snapshot 后，daily bar 行数和内容必须完全不变。
- 批量写使用单事务，合同校验失败整批回滚。
- repository 返回值使用合同对象，不返回裸 sqlite row、Provider dict 或 pandas DataFrame。

V2-12 才执行正式 V1 数据导入。V2-1 只实现：

1. 识别 V1 数据库是否存在。
2. 只读统计可迁移记录数和冲突数。
3. 生成 migration preflight，不修改 V1/V2 正式事实。
4. V2 schema migration 自身可重复执行。

## 9. 账户事实政策

- V2-1 支持分别保存 A股 CNY 和美股 USD 账户快照。
- 用户没有录入余额和持仓时，账户事实为 0，不创建模拟本金。
- 账户权益只能由同一 `captured_at` 批次的冻结价格估值。
- 无报价持仓不得使用成本价冒充市值；报告必须携带不完整估值状态。
- 输入负现金、负股数、负成本价时合同直接报错。

## 10. 后续阶段预留常量

以下值来自 V1 已验证逻辑，用于防止后续阶段重新发明，但 V2-0/V2-1 不实现组合分配或成交：

```text
单票目标仓位硬上限             25%
高相关资产合计上限             35%
股票总仓位上限                 90%
高相关阈值                     correlation >= 0.75
高相关重仓组合最低合计权重     20%
单票 20% 开始警告，30% 为红线
HHI >= 0.25 提示集中
组合年化波动 >= 35% 提示高波动

US lot_size=1, base_slippage=0.3%, commission=0.03%
A  lot_size=100, T+1=true, base_slippage=0.3%, commission=0.03%,
   min_commission=5 CNY, sell_tax=0.05%
```

A股涨跌停初始规则：普通 9.9%、ST 4.9%、创业板/科创板 19.9%、北交所 29.9%。后续市场规则层必须版本化并允许权威规则更新。

V2-2 及以后在实施前仍需各自补充精确合同和 Golden Cases；本文件不授权根据上述预留常量提前开发策略或风控。
