# TradeHelper V2-0/V2-1 Golden Cases

> 本文件给出 V2-0/V2-1 的标准输入与预期结果。测试必须从这些案例推导，不能为了适配实现而修改预期。确需调整时，先修改规范并记录金融或数据源依据，再修改代码。

## 1. 测试通用约定

- 所有时间使用冻结时钟，默认 `now=2026-07-10T16:00:00Z`。
- 网络 Provider 使用脚本化 fake，记录调用顺序、参数、次数和返回状态。
- 交易日历使用注入的 session 列表；单元测试不访问网络，也不依赖本机节假日数据库版本。
- SQLite 使用 `tmp_path`，禁止读取或写入用户真实 `tradehelper.db`。
- 随机行为固定 seed；V2-0/V2-1 原则上不需要随机算法。
- 每个 Golden Case 至少有一个明确断言失败时能指出业务规则，而不只断言“没有抛异常”。

## 2. V2-0 测试基础设施

### G00 架构非法依赖

输入：在临时 V2 文件中加入 `from core.pipeline import run_pipeline`。

预期：`test_architecture_boundaries.py` 失败，并报告文件、行号和禁止模块 `core`。

同样禁止的 V1 顶层模块：

```text
core services strategies backtest report ui alpha indicators utils data
```

`data/compatibility.py` 是唯一语法位置例外，但 V2-0/V2-1 默认测试要求该文件不导入 V1；启用前必须在计划中登记白名单。

### G01 合同确定性

输入：相同 Instrument、Bar 和冻结时间，以不同字典插入顺序构造两次 canonical JSON。

预期：序列化文本和 SHA-256 哈希完全相同；UTC 使用 `Z`；Decimal 使用字符串。

### G02 双市场 fixture

fixture 至少包含：

```text
A:XSHG:600519
A:XSHE:000001
A:XBSE:430047
US:XNAS:AAPL
US:UNKNOWN:BRK.B
```

预期：所有 fixture 可独立构造，不通过 V1 model；A股和美股分别有 pre/intraday/eod 时段样本。

### G03 测试隔离

输入：运行全部 `tests/v2/`。

预期：不访问互联网、不读取用户工作目录、不创建或修改 `tradehelper.db`、不要求 LLM Key。

### G04 本地性能基线

输入：构造并校验 10,000 条有效 `CanonicalBar`。

预期：参考开发机/CI 单进程耗时不超过 2 秒；测试输出实际耗时。若 CI 明显更慢，可在阶段状态记录机器基线后调整，但不能删除基准。

## 3. 标的与合同

### G10 代码标准化

| 输入 | 预期 |
|------|------|
| `600519`, A | `A:XSHG:600519` |
| `000001`, A | `A:XSHE:000001` |
| `430047`, A | `A:XBSE:430047` |
| `aapl`, US, XNAS | `US:XNAS:AAPL` |
| `brk.b`, US, unknown | `US:UNKNOWN:BRK.B` |

`AAPL`, A 和 `600519`, US 必须抛合同校验错误，不自动猜市场。

### G11 正常日 K

输入：`open=100, high=110, low=95, close=108, volume=12345`。

预期：合同有效，质量无 `INVALID_OHLC`，volume 保持 12345 股。

A股 Provider 原始 volume=123 手时，适配后 canonical volume 必须为 12300 股。

### G12 OHLC 硬异常

输入：`open=100, high=105, low=95, close=108`。

预期：合同或质量闸门产生 `INVALID_OHLC`，状态 blocked；不得因为股票波动大而放宽到可用。

### G13 大幅波动不是 OHLC 异常

输入：完整基线数据中有连续两日收盘 10 和 18，第二日 `open=12, high=19, low=11, close=18`；基线已提供上市日期、120 条有效日 K、新闻和基本面，无复权来源冲突。

预期：OHLC 合同有效；仅因该跳变产生一个 `PRICE_JUMP_REVIEW` warning，score=92，状态 ok；不得自动删除第二日或标记 blocked。

若两个来源对第二日复权口径冲突，才产生独立 block `UNSUPPORTED_ADJUSTMENT_MODE` 或来源冲突问题。

### G14 可选字段不填 0

输入：Nasdaq 报价只有 `price=217.5, prev_close=210, observed_at`。

预期：`open/high/low/volume/bid/ask` 全部为 `None`；`realtime_price=true`，`intraday_ohlc=false`，`volume=false`，`bid_ask=false`。价格条件可使用，冲高回落、放量和盘口形态不可确认。

## 4. 数据源路由

### G20 美股盘前 fallback

脚本：Nasdaq 返回 timeout，yfinance 返回有效盘前报价，TickFlow fake 若被调用则测试失败。

预期调用顺序：`nasdaq -> yfinance`。最终 `ProviderResult.status=ok`、`selected_source=yfinance`，attempts 保留 Nasdaq timeout，fallback_reason 非空。

### G21 美股常规盘中禁止错源

脚本：TickFlow 返回 rate_limited，Nasdaq/yfinance 都配置为可返回价格。

预期：只调用 TickFlow；最终实时价格不可用。禁止拿延伸时段接口冒充常规盘中价。

### G22 美股日 K fallback

假日历中已完成 session 为 `2026-07-08`、`2026-07-09`，当前 `2026-07-10` 常规会话尚未收盘。Nasdaq 历史返回 empty，yfinance 返回 7月8、9、10 三条。

预期：调用 `nasdaq -> yfinance`；只接收 7月8、9，丢弃未完成的 7月10；每条 source 为 yfinance，fallback 原因可审计。

### G23 A股日 K 无 fallback

脚本：TickFlow 返回 unavailable，其他 Provider 即使有数据也不得调用。

预期：A股日 K 结果 unavailable，没有 canonical bars，不调用 yfinance/akshare/baostock 日 K 替代。

### G24 A股盘前不伪造报价

输入：DecisionMode=pre，A股 T-1 日 K 存在，无实时连续盘前源。

预期：不调用连续报价 Provider；返回 `realtime_price=false` 和说明“使用 T-1 条件计划”。不得把 T-1 close 标记为当前实时价。

### G25 LLM 不是 Provider

脚本：所有基本面 Provider 均失败，LLM fake 可返回完整财务数字。

预期：LLM fake 调用次数为 0；FundamentalSnapshot 缺失，质量报告记录 fundamentals=false。

### G26 脱敏 Provider payload

`tests/v2/fixtures/providers/` 至少包含 TickFlow A股/美股日 K、TickFlow 批量报价、Nasdaq 价格-only 报价、yfinance 日 K/延伸报价、Finnhub profile2/基本面/新闻、baostock 上市日期/基本面和 akshare 新闻响应。

预期：所有 fixture 可离线解析为合同对象；source、时间、单位和缺失字段符合规范；fixture 中不存在 Token、Cookie、用户路径或账户信息。

### G27 真实 Provider 烟雾状态

未设置 `TRADEHELPER_LIVE_TESTS=1` 时 integration test 明确 skip。启用时验证 AAPL/600519 的名称、完成日 K、OHLC、来源、时间戳和上市日期。

预期：只有 deterministic suite 与 live smoke 都通过时，V2-1 状态可写“完成”；未运行 live smoke 时必须写“工程测试通过，真实源验证待完成”。

### G28 TickFlow 日K配额续跑

输入：同一轮组合刷新 11 只股票，TickFlow 单标的日K预算为 10 次；每只请求相同的已完成会话窗口。

预期：美股全部通过 Nasdaq 历史主源获取，不占 TickFlow A股日K预算。第11只若为A股，结果为 `rate_limited` 并出现在 `pending_retry_at`，时间严格晚于本轮开始。两种情况都不得写入空日K、不得标为 `EMPTY_DAILY_BARS`。

### G29 Provider 日K入库闸门

输入：Provider 返回一条早于权威上市日期的日K、一条交易所日历明确为休市日的日K，以及一条合法正式会话日K。

预期：只返回并写入合法日K；前两条分别以 `before_listing_date` 与 `trading_date_not_in_exchange_calendar` 进入 quarantine。不得因为它们来自主源而写入 `daily_bars`。

## 5. 时间、新鲜度与缓存

### G30 报价边界

冻结 now=`2026-07-10T16:00:00Z`：

| 模式 | observed_at | 预期 |
|------|-------------|------|
| intraday | 15:45Z | fresh，恰好15分钟可用 |
| intraday | 15:44:59Z | stale |
| pre | 15:15Z | fresh，恰好45分钟可用 |
| pre | 15:14:59Z | stale |
| intraday | 16:05Z | fresh，未来5分钟容忍边界 |
| intraday | 16:05:01Z | future，不可用 |

### G31 新闻 negative cache

第一次 intraday 新闻请求在 t0 返回 empty。

预期：t0+4分钟仍返回 empty 且不调用 Provider；t0+5分钟边界允许重新调用。重新成功后按 intraday 成功 TTL 30分钟缓存。

同理 pre 成功 TTL 60分钟，eod 成功 TTL 6小时。

### G32 Tab1/Tab3 无直接依赖

场景 A：全新缓存，只调用 Tab3 的数据刷新入口。

预期：Tab3 独立触发行情、新闻和基本面 get_or_refresh，并得到与先运行 Tab1 相同的 ProviderResult。

场景 B：Tab1 已运行且缓存 fresh，再调用 Tab3。

预期：Tab3 仍调用 get_or_refresh；可以命中共享缓存而不发网络请求。测试不得通过调用 Tab1 私有方法给 Tab3 填数据。

### G33 交易日历不可用

脚本：权威日历 Provider 抛 unavailable，weekday fallback 即使可计算也不允许正式使用。

预期：最近完成交易日/正式目标日返回明确错误；不得静默跳周末生成“可靠”日期。

### G34 注入日历的目标日期

注入 sessions：`2026-07-01, 2026-07-02, 2026-07-06, 2026-07-07, 2026-07-08, 2026-07-09`，as_of=`2026-07-01`。

预期：horizon 1/3/5 分别为 7月2日、7月7日、7月9日。证明 horizon 表示之后第 N 个交易日，不是自然日。

## 6. 上市日期与质量

### G40 IPO 窗口裁剪

标的 SPCX，权威上市日 `2026-06-11`，用户请求开始日 `2025-07-01`。

预期有效请求开始日为 `2026-06-11`；缓存中早于该日的记录进入 quarantine；查询、训练输入和回测输入均看不到上市日前记录。

若上市日至今只有 18 条有效日 K：`daily_price=true`，`short_technical_20=false`，不得伪造 MA20/MA120。

### G41 样本能力

| 有效日 K 数 | daily_price | short_technical_20 | medium_technical_60 | ma120 |
|-------------|-------------|--------------------|---------------------|-------|
| 0 | false | false | false | false |
| 1 | true | false | false | false |
| 20 | true | true | false | false |
| 60 | true | true | true | false |
| 120 | true | true | true | true |

样本不足产生 warning/能力降级，不把有效新股数据标记为 OHLC blocked。

### G42 质量分数

输入一：1个 warning + 2个 optional_missing，无 block。

预期：`score=84`，`status=watch`，`max_position_multiplier=0.8`。

输入二：1个 block，无其他问题。

预期：`score=65`，但状态强制 blocked，`block_new_entries=true`，multiplier=0。

输入三：4个 warning，无 block。

预期：`score=68`，status=degraded，multiplier=0.5。

### G43 Tab3 逐股隔离

AAPL 报价 fresh，MU 报价 stale。

预期：AAPL realtime_price=true；MU 产生 `REALTIME_STALE` 并只阻断 MU。组合结果保留两只股票各自报告，不产生“组合有一只报价正常，所以全部通过”。

## 7. Repository 与数据库

### G50 实时价不污染日 K

先写入 AAPL 7月9日正式日 K close=210，再保存 7月10日盘中 Quote price=217。

预期：daily bars 仍只有一条且 close=210；latest quote=217；不存在 7月10日伪日 K。

### G51 幂等日 K

同一 canonical bar 连续写两次。

预期：正式表一条记录，无 quarantine，无异常。

同一唯一键第二次写入 close 不同的 bar：

预期：原正式记录不被覆盖；新 payload 进入冲突审计/quarantine，返回 conflicting duplicate。

### G52 批量事务回滚

批量写入 9 条有效 bar 和 1 条 `high < close` 非法 bar。

预期：整个事务回滚，正式表新增 0 条；异常指出非法记录的 instrument/date。

### G53 V1/V2 数据库隔离

tmp work_dir 中预先创建 `tradehelper.db` 并写入校验哈希。初始化 V2 repository。

预期：创建/使用 `tradehelper_v2.db`；V1 文件字节哈希、mtime 和表内容均不变。

### G54 V2 schema 幂等

连续运行 schema migration v1 两次。

预期：第二次无重复表/列/索引错误；`schema_migrations` 中 version=1 只有一条；checksum 相同。

### G55 V1 迁移预检只读

V1 数据库包含 2 个持仓、3 个关注股、1 条余额和上市日前错误日 K。

预期：preflight 只返回计数、可迁移项和冲突/隔离项；不向 V2 导入，不修改 V1。正式导入留到 V2-12。

### G56 Point-in-time 查询

新闻 published_at=10:00、available_at=10:30；查询 as_of=10:15 和 10:31。

预期：10:15 查不到，10:31 可查到。

基本面字段同样按 `available_at`，不得只按 `period_end` 回填历史。

## 8. 账户与配置

### G60 零账户不虚构本金

没有余额和持仓记录。

预期：A股和美股 AccountSnapshot 各自 cash=0、positions=empty，计算权益为0；全项目搜索不得存在 V2 默认 `100000` 账户本金。

### G61 账户校验

输入负现金、负股数或负成本价。

预期：分别抛明确合同错误，不自动取绝对值、不改成0、不创建100股。

### G62 冻结估值缺价

现金 1000 USD，持仓 AAPL 10 股，成本 100，但没有当前冻结报价。

预期：不得用成本价计算 2000 USD 权益；账户事实保留现金与持仓，估值状态不完整。V2-1 不生成交易结论。

### G63 数据层不依赖 LLM 配置

配置只有 work_dir 和数据 Provider fake，无 LLM URL/Key/model。

预期：V2-0/V2-1 合同、repository、质量和 Provider 测试正常运行。LLM 缺失不能阻断数据层。

## 9. 测试文件映射

| Golden Cases | 建议测试文件 |
|--------------|--------------|
| G00-G04 | `test_architecture_boundaries.py`, `test_v2_smoke.py`, `test_performance_baseline.py` |
| G10-G14 | `test_data_contracts.py`, `test_data_quality.py` |
| G20-G29 | `test_provider_fallbacks.py`, `test_provider_payload_parsing.py`, `integration/test_live_providers.py` |
| G30-G34 | `test_cache_policy.py`, `test_trading_calendar.py` |
| G40-G43 | `test_data_quality.py`, `test_listing_date_policy.py` |
| G50-G56 | `test_market_data_repository.py`, `test_schema_migrations.py` |
| G60-G63 | `test_account_contracts.py`, `test_settings_contract.py` |

测试文件名可以按代码组织微调，但 Golden Case 编号和预期必须保留在测试名称或参数 id 中，便于审计阶段完成度。
