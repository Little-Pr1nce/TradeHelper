# TradeHelper 设计文档

## 一、项目概述

基于 Python 3.12 + Flet 框架的轻量级跨平台桌面量化分析应用。
支持 A 股和美股的技术面分析、多因子 Alpha 打分、三策略回测、新闻情感分析与报告生成。

---

## 二、技术选型

| 组件 | 技术方案 |
|------|---------|
| UI 框架 | Flet (Flutter) |
| 数据存储 | SQLite (WAL) |
| A 股数据 | TickFlow（行情）+ akshare（新闻/基本面） |
| 美股数据 | TickFlow（盘中/K线）+ yfinance（盘前盘后）+ Finnhub（新闻/基本面） |
| 金融新闻 | A 股: akshare 东方财富；美股: Finnhub → LLM 补充 |
| 新闻情感分析 | FinBERT + 中文关键词兜底 |
| K 线图 | mplfinance |
| 大模型 API | OpenAI 兼容格式（DeepSeek / GPT / Ollama） |
| 报告导出 | 内嵌 K 线图的 HTML |
| 技术指标 | pandas + numpy |
| 报告内容 | LLM 全量数据交叉分析 + AI 综合建议 + 短期走势预测 |

---

## 三、目录结构

```
TradeHelper/
├── main.py                       # 应用入口
├── run_backtest.py               # CLI 回测脚本
│
├── core/                         # 公共管道
│   ├── pipeline.py               #   分析管道
│   └── types.py                  #   核心数据类型
│
├── services/                     # 业务编排层
│   └── analysis_service.py       #   分析工作流
│
├── alpha/                        # Alpha 多因子打分
│   ├── scoring.py                #   打分合成
│   ├── validation.py             #   因子 IC/IR 检验
│   ├── fundamental.py            #   基本面因子（Finnhub/akshare 优先，LLM 兜底）
│   ├── fundamental_llm.py        #   LLM PE/PB 兜底
│   └── depth_factor.py           #   实时盘口因子（TickFlow 可用时）
│
├── strategies/                   # 交易执行策略库
│   ├── base.py                   #   基类 + compute_atr
│   ├── threshold_trend.py        #   策略 A
│   ├── mean_reversion.py         #   策略 B
│   └── momentum_news.py          #   策略 C
│
├── backtest/                     # 事件驱动回测引擎
│   ├── broker.py                 #   撮合 + 风控
│   ├── engine.py                 #   回测主循环
│   └── analytics.py              #   绩效 + 对比
│
├── indicators/                   # 技术指标 & 情感分析
│   ├── technical.py              #   MA/MACD/RSI/布林/KDJ
│   ├── sentiment.py              #   FinBERT
│   └── constants.py              #   常量
│
├── data/                         # 数据层
│   ├── models.py                 #   数据模型
│   ├── database.py               #   SQLite CRUD
│   ├── stock_fetcher.py          #   股价获取（TickFlow + yfinance延伸时段）
│   ├── news_fetcher.py           #   新闻获取 + LLM 补充
│   ├── news_providers.py         #   新闻源策略
│   └── finnhub_client.py         #   Finnhub API 客户端
│
├── report/                       # 报告生成
│   ├── prompts.py                #   LLM 提示词
│   ├── generator.py              #   报告生成
│   ├── chart.py                  #   K 线图
│   └── pdf_exporter.py           #   PDF/HTML 导出
│
├── config/
│   └── settings.py               # 全局配置 + 单例
│
├── utils/                        # 工具函数
│   ├── market.py                 #   市场识别 + 搜索
│   ├── dates.py                  #   日期计算
│   ├── fonts.py                  #   中文字体
│   └── logging.py                #   日志
│
├── ui/                           # Flet 界面
│   ├── main_page.py              #   主分析页
│   ├── history_page.py           #   历史报告页
│   ├── settings_ui.py            #   设置页
│   └── components.py             #   StarRating
│
├── data_adapters/                # 扩展预留
└── tests/                        # 单元测试
```

---

## 四、核心架构

### 4.1 分层架构

```
┌──────────────────────────────────────────┐
│  UI 层 (ui/)       CLI 层 (run_backtest) │  ← 纯展示
├──────────────────────────────────────────┤
│        Services 层 (services/)            │  ← 业务编排
├──────────────────────────────────────────┤
│        Core 层 (core/pipeline.py)         │  ← 纯计算
├─────────────┬────────────────────────────┤
│  计算引擎    │  支撑层                     │
│  alpha/     │  indicators/ data/ report/ │
│  strategies/│  config/ utils/             │
│  backtest/  │                            │
└─────────────┴────────────────────────────┘
```

### 4.2 核心数据流

```
用户输入
  → 搜索代码 (utils/market.py)         Finnhub /search(美股) + 本地字典
  → 获取股价 (data/stock_fetcher.py)    SQLite 缓存 → 增量更新 TickFlow
  → 获取新闻 (data/news_fetcher.py)     Finnhub → LLM 补充 → 历史降级
  → 获取基本面 (alpha/fundamental.py)   Finnhub metric → akshare → LLM 兜底
  → Alpha 打分 (alpha/scoring.py)      7 指标 Z-Score + 因子检验(IC/IR) + 扩展权重
  → 三策略回测 (backtest/engine.py)    T+1 撮合，美股不限涨跌停
  → 盘口数据 (alpha/depth_factor.py)   实时买卖比（数据源支持时）
  → 报告生成 (report/generator.py)     LLM 全量数据交叉分析 + AI 综合建议与预测 / 模板兜底
  → 导出 HTML (ui/main_page.py)
```

---

## 五、Alpha 多因子打分模型

### 5.1 因子组成

| 类别 | 权重 | 因子 | 来源 |
|------|:--:|------|------|
| 技术面 | 35% | RSI / DIF / MACD 柱 / 布林 %B / K / D / J | 股价 K 线 |
| 风格 | 15% | PE(TTM) 3 年分位 / PB 3 年分位 | akshare / LLM |
| 基本面 | 25% | ROE / 毛利率 / 资产负债率 / 净利同比 / 营收同比 | akshare / LLM |
| 新闻面 | 25% | FinBERT 情感得分 | LLM 新闻 + FinBERT |

### 5.2 技术面处理

1. 7 指标各自滚动 Z-Score（窗口 60，min_periods=15）
2. tanh 压缩至 (-1, +1)
3. IC/IR 因子有效性检验（A-D 评级）
4. D 级剔除、C 级半权后等权平均

### 5.3 基本面处理

- A 股：akshare `stock_value_em`（PE/PB）+ `stock_financial_analysis_indicator`（财务）
- 美股：Finnhub `/stock/metric?metric=all`（优先）→ akshare `stock_financial_us_analysis_indicator_em`（财务）+ 百度 API（PE/PB）→ LLM 兜底
- 数据源在日志中标注：`基本面(Finnhub)`、`基本面(akshare)`、`基本面(LLM)` 或 `基本面(partial)`

### 5.4 报告综合建议与短期预测

- LLM 模式下，综合建议改为由大模型基于全量数据（10 个数据维度）进行交叉分析，产出三部分：
  a) **数据综合分析**：交叉印证因子/技术面/新闻/基本面/回测/盘口各维度
  b) **操作建议**：短期方向判断 + 关键参考点位
  c) **短期走势预测**：未来 1-4 周走势判断 + 置信度说明
- 无 LLM 时，`_derive_recommendation()` 基于策略收益、Rank IC、基准对比、盘口等维度拼接本地建议
- 传给 LLM 的全量数据包括：Alpha 得分统计、因子 IC/IR 检验表、基本面估值、技术摘要、新闻情感、三策略回测对比、Rank IC、基准收益、盘口数据

### 5.5 K 线缓存增量更新

- 缓存为空 → 全量拉取 (start~end)
- 缓存存在 → 从缓存最新日期次日至 today 做增量拉取
- 通过比较 TickFlow 返回的最新日期与缓存最新日期判断是否有新数据，非交易日自动跳过

---

## 六、交易策略

### 策略 A：阈值滞后带趋势跟踪

- 开仓：Final_Score > 0.6
- 平仓：Final_Score < 0.3
- 冷却期：3 根 K 线
- 仓位：2% 净值 / 2×ATR(14)

### 策略 B：波动率自适应均值回归

- 开仓：Final_Score < -0.5 且 20 日波动率处于后 30% 分位
- 平仓：Final_Score > 0.2 或浮盈 ≥ 3×ATR
- 冷却期：5 根 K 线
- 仓位：反波动率加权 [1%-4%]

### 策略 C：动量突破 + 新闻共振

- 开仓：Final_Score > 0.7 + FinBERT > 0.8 + 突破 20 日高点
- 平仓：Final_Score < 0.4 或移动止盈（最高点 - 2×ATR）
- 冷却期：2 根 K 线
- 仓位：金字塔加仓（首次 1% + 加仓 0.5%，总敞口 ≤ 3%）

### 通用硬风控

- -8% 无条件止损
- 持仓 10 个交易日时间止损
- A 股涨跌停过滤（美股不设涨跌停限制）
- 单笔 ≤ 5% 日成交量（超量加 0.5% 额外滑点）

---

## 七、回测引擎

### 7.1 撮合时序

| 时间点 | 动作 | 数据 |
|--------|------|------|
| T 日收盘 | 读 Final_Score → 策略判断 → 生成 Order | T 日及以前 |
| T+1 开盘 | open × (1 + 0.3%) 撮合 | T+1 Open |
| T+1 盘中 | High/Low 止损/止盈检查 | T+1 Intraday |
| T+1 收盘 | 更新净值 + 日志 | — |

### 7.2 防偷窥约束

- 禁止 T 日收盘价成交（强制 T+1 开盘价）
- 因子在回测循环前全部预计算
- 新闻缺失填 0 不填充（禁止未来信息泄露）
- Alpha 模型为纯函数，无全局状态

---

## 八、新闻获取系统

### 8.1 降级链路

```
cache(24h) → API Provider → LLM 补充 → 历史缓存兜底
```

### 8.2 Provider 策略

| 市场 | Provider | 免费 |
|------|---------|:--:|
| A 股 | AkshareEastMoneyProvider | ✓ |
| 美股 | FinnhubNewsProvider | ✓（需 `news_token_us`） |
| 通用 | LLM 补充 | ✓（需大模型配置） |

注：美股新闻统一走 Finnhub；yfinance 仅用于盘前/盘后价格。

### 8.3 情感分析

- FinBERT 分析新闻标题
- FinBERT 全判 neutral（中文新闻常见）→ 自动切换中文关键词匹配（50+ 词库）
- 分析结果写入数据库，24h 内复用

---

## 九、数据源切换

### 9.1 股价数据

| 数据源 | settings | 类 |
|--------|---------|------|
| 免费 | `stock_token_us/a` 为空 | TickFlow 免费 K 线 |
| 注册 | `stock_token_us/a` 有值 | TickFlow K 线 + 盘中实时行情 |

注：行情统一读取 `stock_token_us` / `stock_token_a`，新闻读取 `news_token_us`。

### 9.2 基本面数据

| 市场 | PE/PB | 财务指标 | 优先 |
|------|-------|---------|------|
| A 股 | akshare stock_value_em | akshare stock_financial_analysis_indicator | akshare |
| 美股 | Finnhub /stock/metric series | Finnhub /stock/metric metric | Finnhub → akshare/百度 → LLM 兜底 |

---

## 十、配置说明

`系统标准应用配置目录/TradeHelper/config.json`：

```json
{
  "work_dir": "~/TradeHelper",
  "llm_base_url": "https://api.deepseek.com",
  "llm_api_key": "sk-...",
  "llm_model": "deepseek-v4-flash",
  "stock_token_us": "",
  "stock_token_a": "",
  "news_token_us": "",
  "news_token_a": "",
  "proxy": ""
}
```

| 配置项 | 说明 |
|--------|------|
| `stock_token_us/a` | TickFlow API Key（K线/盘中实时行情） |
| `news_token_us` | Finnhub API Key（免费注册，美股搜索/信息/新闻/基本面） |
| `news_token_a` | A 股额外新闻 Token（预留，如 Tushare） |
| `proxy` | 代理地址（yfinance 等海外服务用） |

注：`data_source`、`paid_api_token`、`finnhub_api_key` 字段已废弃。

---

## 十一、数据库设计

### 4 张表

| 表 | 主键 | 说明 |
|----|------|------|
| stocks | code | 股票基本信息缓存 |
| price_history | (code, date) | OHLCV 日 K 线 |
| reports | id (自增) | 分析报告 + 评分 |
| news_sentiment | id (自增) + UNIQUE(code,date,title) | 新闻 + 情感标签 + 内容 |

### 缓存策略

- 股价：缓存存在 → 增量拉取（缓存最新日期次日 ~ today）；缓存为空 → 全量拉取。TickFlow 按日期比较判断是否有新数据。
- 新闻：24h 内已分析 → 缓存命中
- 新闻去重：启动时自动合并重复 (code, date, title)

---

## 十二、扩展点

| 扩展目标 | 入口 |
|---------|------|
| 新增技术因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` |
| 新增策略 | `strategies/__init__.py` → `_STRATEGY_REGISTRY` |
| 新增新闻源 | `data/news_providers.py` → 继承 `BaseNewsProvider` |
| 替换情感模型 | `indicators/sentiment.py` → `_FINBERT_MODEL` |
| 新增配置项 | `config/settings.py` → `DEFAULT_CONFIG` + `settings_ui.py` 输入框 |
| 新增 UI 页面 | 继承 `ft.Container`，在 `main.py` Stack 注册 |
| 接入实时数据 | `data_adapters/__init__.py` → 实现 `DataAdapter` 接口 |
| 调整报告提示词 | `report/prompts.py` → 修改 `SYSTEM_PROMPT` 或 `build_user_prompt()` |

---

## 第十三章：优化历史

### 2026-06 优化批次

#### 1. 三时段联动闭环
盘前报告存储结构化预测数据（pre_price, futures_score 等），盘中分析自动读取并生成「盘前预测验证」小节，比较预测方向 vs 实际开盘方向，标注「预测正确/部分正确/偏差」。LLM 在第八章中交叉解读验证结果，调整置信度。

#### 2. 盘口/期货数据量化入评分
- **盘口因子**：depth_score 以 10% 权重入 Final_Score（无盘口时自动回退）
- **期货情绪**：futures_score = 0.7 × tanh(涨跌幅 × 30) + 0.3 × K 线趋势得分

#### 3. 盘中动量信号
新增 VWAP 偏离（价格 vs 成交量加权均价）和日内动量（开盘→最新、距高低点），这些是机构交易者的关键参考指标。

#### 4. 新闻情感时间衰减
半衰期 1 天指数衰减：`weight = exp(-ln(2) × days_ago / 1.0)`。同一天多条新闻加权平均，近期新闻比旧新闻更有影响力。

#### 5. 代码层去除解读
快照函数 (`compute_intraday_snapshot`, `compute_premarket_snapshot`) 中所有硬编码的 if/else 解读文本（如「短期强势」「买盘显著占优」等）全部移除。表格从 3 列（项目/数值/解读）改为 2 列（项目/数值），全部解读由 LLM 完成。瘦身 ~200 行。

#### 6. 市场状态自适应策略选择
- `detect_market_regime()` 从 3 种扩展为 5 种行情（trending_volatile / trending_steady / ranging / transitional / unknown）
- 7 个策略各标注 `suitable_regimes`，回测时自动过滤不适配策略
- 新增策略 G（MA 交叉确认），覆盖慢涨行情缺口
- 报告中展示行情判断 + 策略适配表

#### 7. 首次配置引导
- 4 项必填（工作目录/LLM URL/Key/模型），红色 * 标注
- 首次启动强制跳设置页，分析/历史 tab 灰色禁用
- 保存时验证必填项，缺失具体提示
- 分析时按市场校验数据源 Token（美股需 stock_token_us + news_token_us，A 股需 stock_token_a）

#### 8. 配置文件跨平台
- macOS: `~/Library/Application Support/TradeHelper/config.json`
- Windows: `%APPDATA%/TradeHelper/config.json`
- Linux: `~/.config/TradeHelper/config.json`
- 固定路径，不随 work_dir 变动

#### 9. 数据源 Token 拆分
`get_stock_fetcher(market)` 按市场读取 `stock_token_us`（美股）或 `stock_token_a`（A 股），统一返回 TickFlow。

#### 10. 跨平台打包
PyInstaller + 内置 FinBERT 模型（`prepare_model.py` 从 HF 缓存导出）+ Mac/Win 构建脚本。

### 后续优化路线图

| 优先级 | 项目 | 说明 |
|--------|------|------|
| P0 | 数据适配器完善 | 统一 TickFlow/yfinance 可用性检查和错误提示 |
| P0 | 期货数据替代 | NQ/ES → QQQ/SPY ETF 盘前数据 |
| P0 | 历史预测评估 UI 面板 | 独立展示 prediction_log 统计，按股票/策略/市场状态/分析模式查看胜率、平均收益和正期望状态 |
| P1 | A 股盘前/盘中 | 当前仅美股支持 |
| P1 | 策略参数自动调优 | 已有 walk-forward 基础，继续完善多窗口稳定性和参数漂移告警 |
| P2 | Web 版完善 | Flet Web 模式 |
| P3 | 打包体积优化 | ONNX 量化 PyTorch 模型 |

### 既有功能待优化清单

以下项目不属于新功能扩展，而是现有交易建议链路继续提升可信度需要处理的逻辑与算法优化：

| 优先级 | 项目 | 说明 |
|--------|------|------|
| P0 | LLM 建议边界 | LLM 只能解释代码生成的方向、入场、止损、仓位和失效条件，不得自行改写交易计划 |
| P0 | 数据完整度降级 | 基本面、新闻、实时价、成交量或历史 K 线缺失时，自动降低信号强度和最大仓位 |
| P0 | 样本不足降级 | 策略审计、walk-forward、历史预测验证样本不足时，统一标记为不可评价/低可信 |
| P0 | 实时价重算风控 | 盘前/盘中报告复用 T-1 方案时，必须用当前价重算仓位、风险金额和失效条件 |
| P0 | 策略审计置信区间 | 在夏普/胜率点估计之外加入交易次数惩罚、置信区间或 bootstrap 稳健性检查 |
| P1 | Final_Score 权重反馈 | 用预测追踪结果逐步校准技术面、新闻、基本面权重，长期无效因子自动降权 |
| P1 | 新闻质量控制 | 强化新闻去重、宏观/个股权重、低置信新闻降权和事件风险识别 |
| P1 | 回测/实盘口径隔离 | 报告中明确区分历史验证表现和当前可执行价格，避免混用收盘价与实时价 |
| P1 | 持仓状态细分 | Tab3 对已有持仓区分继续持有、减仓、止损、禁止加仓，而不只输出 buy/sell/no_signal |
| P1 | 缓存失效规则 | 重大新闻、价格跳变或市场状态变化时强制重算，避免复用过期 T-1/盘前报告 |
| P2 | 异常数据质量评分 | 对零价格、缺量、复权异常、日期错位、异常跳空等统一评分并阻断低质量交易建议 |
| P2 | 市场规则集中化 | 将 A 股/美股一手、涨跌停、时段、税费、价格单位等规则集中到 market rules 模块 |
| P2 | 统一置信度评分 | 建立 confidence_score，综合数据完整度、策略审计、walk-forward、历史预测和市场状态 |
