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
| A 股数据 | akshare（免费）/ itick（付费） |
| 美股数据 | itick（K线/信息/盘口）/ Finnhub（搜索/信息/新闻/基本面） |
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
│   └── depth_factor.py           #   实时盘口因子（itick）
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
│   ├── stock_fetcher.py          #   股价获取（Free/Itick）
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
  → 获取股价 (data/stock_fetcher.py)    SQLite 缓存 → 增量更新 itick
  → 获取新闻 (data/news_fetcher.py)     Finnhub → LLM 补充 → 历史降级
  → 获取基本面 (alpha/fundamental.py)   Finnhub metric → akshare → LLM 兜底
  → Alpha 打分 (alpha/scoring.py)      7 指标 Z-Score + 因子检验(IC/IR) + 扩展权重
  → 三策略回测 (backtest/engine.py)    T+1 撮合，美股不限涨跌停
  → 盘口数据 (alpha/depth_factor.py)   实时买卖比（仅 itick）
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
- 通过比较 itick 返回的最新年份与缓存最新年份判断是否有新数据，非交易日自动跳过

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

注：美股 YfinanceNewsProvider 已废弃，美股新闻统一走 Finnhub。

### 8.3 情感分析

- FinBERT 分析新闻标题
- FinBERT 全判 neutral（中文新闻常见）→ 自动切换中文关键词匹配（50+ 词库）
- 分析结果写入数据库，24h 内复用

---

## 九、数据源切换

### 9.1 股价数据

| 数据源 | settings | 类 |
|--------|---------|------|
| 免费 | `stock_data_token` 为空 | FreeStockFetcher（akshare，美股需 `news_token_us` 辅助） |
| 付费 | `stock_data_token` 有值 | ItickStockFetcher（A 股 + 美股 K 线/信息/盘口） |

注：`data_source` 和 `paid_api_token` 字段已废弃，改为读取 `stock_data_token` 和 `news_token_us`。

### 9.2 基本面数据

| 市场 | PE/PB | 财务指标 | 优先 |
|------|-------|---------|------|
| A 股 | akshare stock_value_em | akshare stock_financial_analysis_indicator | akshare |
| 美股 | Finnhub /stock/metric series | Finnhub /stock/metric metric | Finnhub → akshare/百度 → LLM 兜底 |

---

## 十、配置说明

`~/.tradehelper/config.json`：

```json
{
  "work_dir": "~/TradeHelperData",
  "llm_base_url": "https://api.deepseek.com",
  "llm_api_key": "sk-...",
  "llm_model": "deepseek-v4-flash",
  "stock_data_token": "",
  "news_token_us": "",
  "news_token_a": "",
  "proxy": ""
}
```

| 配置项 | 说明 |
|--------|------|
| `stock_data_token` | itick API Token（付费，K线/信息/盘口） |
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

- 股价：缓存存在 → 增量拉取（缓存最新日期次日 ~ today）；缓存为空 → 全量拉取。itick 按日期比较判断是否有新数据。
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
