# TradeHelper - 股票分析助手 设计文档

## 一、项目概述

基于 Python 3.12 + Flet 框架的轻量级跨平台（Windows/macOS）桌面应用。
支持 A 股和美股的技术面分析、新闻情感分析、回测与报告生成。

---

## 二、技术选型

| 组件         | 技术方案                                                 |
|-------------|----------------------------------------------------------|
| UI 框架      | Flet (基于 Flutter，原生渲染，跨平台)                      |
| 数据存储     | SQLite（股价历史、分析报告、用户评分）                      |
| A 股数据     | akshare（开源免费），可选用户自定义收费 API                  |
| 美股数据     | yfinance（开源免费），可选用户自定义收费 API                 |
| 金融新闻     | A 股: akshare 东方财富新闻；美股: yfinance .news 属性       |
| 新闻情感分析 | HuggingFace `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` |
| K 线图       | mplfinance 生成图片                                      |
| 大模型 API   | OpenAI 兼容格式（可配置 base_url + api_key）              |
| PDF 导出     | reportlab（纯 Python，无需系统依赖，跨平台友好）             |
| 技术指标计算 | pandas + numpy + ta (technical analysis library)          |
| 回测引擎     | 自研轻量回测框架（基于 pandas 向量化计算）                  |

---

## 三、项目目录结构

```
TradeHelper/
├── main.py                     # 应用入口，Flet app 初始化
├── config/
│   ├── __init__.py
│   ├── settings.py             # 配置管理（JSON 文件持久化，工作目录/API/数据源选择）
│   └── settings_ui.py          # 设置页面 UI 视图
├── data/
│   ├── __init__.py
│   ├── database.py             # SQLite 数据库初始化、CRUD 操作封装
│   ├── models.py               # 数据模型定义（dataclass/ORM）
│   ├── stock_fetcher.py        # 股价数据获取（策略模式：免费/付费 API 切换）
│   └── news_fetcher.py         # 新闻数据获取（A 股/美股不同来源）
├── analysis/
│   ├── __init__.py
│   ├── technical.py            # 技术指标计算（MA, MACD, RSI, 布林带, KDJ）
│   ├── sentiment.py            # 新闻情感分析（FinBERT 模型推理）
│   ├── backtest.py             # 回测引擎（资金管理、信号执行、收益统计）
│   └── strategy.py            # 交易策略定义（初始：双均线交叉演示策略）
├── report/
│   ├── __init__.py
│   ├── generator.py            # 报告生成（调用 LLM 整合各模块分析结果）
│   ├── pdf_exporter.py         # PDF 导出（reportlab 排版）
│   └── chart.py                # K 线图生成（mplfinance，输出 PNG）
├── ui/
│   ├── __init__.py
│   ├── main_page.py            # 主分析页（股票代码输入、周期选择、分析触发）
│   ├── history_page.py         # 历史报告列表页（查看、评分、导出）
│   └── components.py           # 可复用 UI 组件（加载动画、状态提示等）
└── utils/
    ├── __init__.py
    └── helpers.py              # 工具函数（日期处理、股票代码校验、日志等）
```

---

## 四、模块详细设计

### 4.1 config/ - 配置管理层

**settings.py**
- 使用 JSON 文件持久化配置（存储在工作目录下 `config.json`）
- 配置项：
  ```json
  {
    "work_dir": "~/TradeHelperData",          // 工作目录
    "llm_base_url": "https://api.openai.com/v1",  // LLM API 地址
    "llm_api_key": "",                         // LLM API Key
    "llm_model": "gpt-4o",                     // 模型名称
    "data_source": "free",                     // "free" | "custom"
    "custom_api_endpoint": "",                 // 自定义数据 API 端点
    "custom_api_key": ""                       // 自定义数据 API Key
  }
  ```
- 首次启动时自动引导用户进入设置页
- 提供 `get(key)`, `set(key, value)`, `save()` 方法

### 4.2 data/ - 数据持久化层

**models.py** - 数据模型（使用 dataclass + 字典互转）
```python
@dataclass
class StockInfo:
    code: str           # 股票代码
    name: str           # 股票名称
    market: str         # "A" | "US"
    industry: str       # 所属行业
    description: str    # 公司简介

@dataclass
class PriceData:
    code: str
    date: str           # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class AnalysisReport:
    id: int
    code: str
    name: str
    market: str
    backtest_period: str   # "3m" | "6m" | "1y" | "3y"
    create_time: str
    content: str            # Markdown 格式报告内容
    pdf_path: str | None
    rating: int | None      # 用户评分 1-5
```

**database.py** - SQLite 操作
- 表结构：
  - `stocks` (code, name, market, industry, description, update_time)
  - `price_history` (code, date, open, high, low, close, volume, PRIMARY KEY(code, date))
  - `reports` (id, code, name, market, backtest_period, create_time, content, pdf_path, rating, rated_at)
  - `news_sentiment` (id, code, date, title, source, sentiment, confidence, PRIMARY KEY(id))
- 初始化自动建表
- 提供批量插入、查询、更新方法

**stock_fetcher.py** - 策略模式
- 抽象基类 `BaseStockFetcher` 定义接口
- `FreeStockFetcher`：akshare (A 股) + yfinance (美股)
- `CustomStockFetcher`：对接用户自定义 API
- 工厂函数 `get_stock_fetcher(source_type)` 返回对应实例

**news_fetcher.py**
- `fetch_news_a(code)`：通过 akshare 获取东方财富个股新闻
- `fetch_news_us(code)`：通过 yfinance Ticker.news 获取美股新闻
- 统一接口 `fetch_news(code, market, limit=10)`

### 4.3 analysis/ - 分析引擎层

**technical.py** - 技术指标计算（使用 ta 库 + pandas）
- `calc_ma(df, periods=[5, 10, 20, 60])`：移动均线
- `calc_macd(df)`：MACD（DIF, DEA, 柱状图）
- `calc_rsi(df, period=14)`：相对强弱指标
- `calc_bollinger(df, period=20)`：布林带（上轨、中轨、下轨）
- `calc_kdj(df, period=9)`：KDJ 指标
- `generate_signals(df)`：综合技术指标生成买卖信号
- `summarize(df)`：生成技术面分析摘要文本

**sentiment.py** - 新闻情感分析
- 模型路径：`mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis`
- 使用 HuggingFace transformers pipeline
- 标签映射：positive / neutral / negative
- `analyze(news_list)` → 返回每条新闻的情感标签和置信度
- `aggregate(results)` → 汇总整体情感倾向

**strategy.py** - 交易策略
- 抽象基类 `BaseStrategy` 定义接口
- `MACrossoverStrategy`：双均线交叉演示策略
  - 金叉（5 日线上穿 20 日线）→ 买入
  - 死叉（5 日线下穿 20 日线）→ 卖出
  - 参数可配置
- 扩展点：后续可添加更多策略（MACD 策略、RSI 策略等）

**backtest.py** - 回测引擎
- 输入：价格历史数据 + 策略实例 + 初始资金
- 流程：按时间序列遍历，策略生成信号，模拟执行交易
- 输出：
  - 总收益率
  - 年化收益率
  - 最大回撤
  - 夏普比率
  - 胜率
  - 交易明细列表

### 4.4 report/ - 报告生成层

**chart.py** - K 线图生成
- `generate_kline_chart(df, code, name)` → PNG 图片路径
- 使用 mplfinance 绘制包含 MA/成交量/K 线的标准图表
- 默认显示近 3 个月日 K 线，标注买卖信号

**generator.py** - 报告生成
- 整合以下输入，调用 LLM 生成结构化报告：
  - 股票基本信息（名称、代码、行业、简介）
  - 技术面分析摘要
  - 新闻情感分析结果
  - 回测结果数据
- 系统提示词约束报告结构（避免幻觉，严格要求基于提供的数据生成）
- 输出 Markdown 格式报告

**pdf_exporter.py** - PDF 导出
- 使用 reportlab 将 Markdown 报告 + K 线图转换为 PDF
- 支持中文字体（自动检测系统字体或嵌入开源字体）
- 结构：封面 → 股票概览 → K 线图 → 技术面分析 → 新闻情感分析 → 回测结果 → 建议

### 4.5 ui/ - 用户界面层

**main_page.py** - 主分析页
- 股票代码输入框（自动识别 A 股 6 位数字 / 美股字母代码）
- 回测周期下拉选择（3 个月 / 6 个月 / 1 年 / 3 年）
- 「开始分析」按钮 → 加载动画 → 显示分析进度 → 展示报告
- 报告展示区域（Markdown 渲染 + K 线图）
- 「导出 PDF」按钮
- 「评分」星级组件

**history_page.py** - 历史报告页
- 报告列表（按时间倒序）
- 每条显示：股票名称、代码、分析时间、当前评分
- 点击查看完整报告
- 支持重新评分（1-5 星）
- 支持重新导出 PDF
- 支持删除报告

**components.py**
- `ProgressOverlay`：全屏加载动画组件
- `StarRating`：星级评分组件
- `StockCodeInput`：股票代码输入组件（带校验）

### 4.6 utils/ - 工具层

- `is_valid_stock_code(code)`：校验股票代码格式
- `detect_market(code)`：自动识别 A 股/美股
- `format_date(d)`：日期格式化
- `get_chinese_font_path()`：自动查找系统中文字体路径
- `setup_logging(work_dir)`：配置日志

---

## 五、数据流

```
用户输入 [股票代码 + 回测周期]
        │
        ▼
  ┌─────────────────┐
  │  1. 获取股价数据  │  stock_fetcher → 优先从数据库读取，缺失部分从 API 拉取
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │  2. 获取新闻数据  │  news_fetcher → 存入 news_sentiment 表
  └────────┬────────┘
           ▼
  ┌─────────────────┐     ┌──────────────────┐
  │ 3. 技术指标计算  │     │ 4. 新闻情感分析    │
  │   technical.py  │     │   sentiment.py    │
  └────────┬────────┘     └────────┬─────────┘
           │                       │
           ▼                       ▼
  ┌─────────────────┐
  │  5. 回测运行     │  backtest.py + strategy.py
  └────────┬────────┘
           ▼
  ┌─────────────────┐     ┌──────────────────┐
  │ 6. 生成 K 线图   │     │ 7. LLM 生成报告   │
  │   chart.py      │     │   generator.py    │
  └────────┬────────┘     └────────┬─────────┘
           │                       │
           ▼                       ▼
  ┌─────────────────────────────────────┐
  │  8. 展示报告 + 可选导出 PDF + 可选评分 │
  └─────────────────────────────────────┘
```

---

## 六、数据库 ER 图

```
┌──────────────┐       ┌──────────────────┐
│    stocks    │       │  price_history    │
├──────────────┤       ├──────────────────┤
│ code (PK)    │──1:N─→│ code (PK, FK)     │
│ name         │       │ date (PK)         │
│ market       │       │ open              │
│ industry     │       │ high              │
│ description  │       │ low               │
│ update_time  │       │ close             │
└──────────────┘       │ volume            │
                       └──────────────────┘

┌──────────────┐       ┌──────────────────┐
│   reports    │       │ news_sentiment    │
├──────────────┤       ├──────────────────┤
│ id (PK, AI)  │       │ id (PK, AI)       │
│ code         │       │ code              │
│ name         │       │ date              │
│ market       │       │ title             │
│ period       │       │ source            │
│ create_time  │       │ sentiment         │
│ content      │       │ confidence        │
│ pdf_path     │       └──────────────────┘
│ rating       │
│ rated_at     │
└──────────────┘
```

---

## 七、UI 页面结构

```
┌──────────────────────────────────────────┐
│  TradeHelper - 股票分析助手          ⚙ 设置 │
├──────────────────────────────────────────┤
│                                          │
│  ┌──────────┐ ┌──────────┐              │
│  │ 股票代码  │ │ 回测周期  │ [开始分析]    │
│  └──────────┘ └──────────┘              │
│                                          │
│  ┌──────────── 分析报告 ─────────────┐   │
│  │                                  │   │
│  │  📊 K 线图                       │   │
│  │                                  │   │
│  │  📈 技术面分析                    │   │
│  │  📰 新闻情感分析                  │   │
│  │  💰 回测结果                      │   │
│  │  💡 综合建议                      │   │
│  │                                  │   │
│  │  [导出PDF]  [⭐评分]              │   │
│  └──────────────────────────────────┘   │
│                                          │
│  ── 底部导航栏 ────────────────────────  │
│  [📊 分析]  [📋 历史报告]                │
└──────────────────────────────────────────┘
```

---

## 八、FinBERT 模型下载位置

使用 `transformers` 库加载 `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` 模型时，文件会自动下载并缓存到：

| 操作系统  | 默认缓存路径                            |
|----------|----------------------------------------|
| macOS    | `~/.cache/huggingface/hub/`            |
| Windows  | `C:\Users\<用户名>\.cache\huggingface\hub\` |

模型大小约 **~300MB**（包含 PyTorch 模型权重 + tokenizer）。
首次运行时自动下载，后续使用缓存。

如需自定义缓存路径，可通过环境变量设置：
```bash
export HF_HOME=/your/custom/path
export TRANSFORMERS_CACHE=/your/custom/path
```

---

## 九、开发环境设置

```bash
# 1. 创建虚拟环境
python3.12 -m venv venv

# 2. 激活虚拟环境
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 运行应用
python main.py
```

---

## 十、依赖清单 (requirements.txt)

```
flet>=0.25.0
akshare>=1.14.0
yfinance>=0.2.40
mplfinance>=0.12.10
pandas>=2.2.0
numpy>=2.0.0
ta>=0.11.0
transformers>=4.40.0
torch>=2.3.0
openai>=1.30.0
reportlab>=4.2.0
matplotlib>=3.9.0
Pillow>=10.3.0
```

---

## 十一、扩展指南

本章详细列出每个模块的具体扩展方法和操作步骤。代码中所有扩展点均以 `【扩展点】` 注释标记，可在 IDE 中搜索快速定位。

---

### 11.1 交易策略（analysis/strategy.py）

**文件位置**：`analysis/strategy.py`

#### 策略与回测的协作关系

策略和回测引擎通过一个简单约定解耦，**互不依赖**：

```
策略的职责：在 DataFrame 的 "signal" 列填 "buy" / "sell" / ""
回测的职责：读 "signal" 列，逐日模拟资金变动，计算绩效指标
```

- **修改策略**：只需改 `strategy.py`，回测引擎无需任何改动
- **增强回测**（如添加滑点、T+1 限制）：只需改 `backtest.py`，已有策略全部自动受益
- **原因**：回测引擎的 `_simulate()` 方法只检查 `signal == "buy"` 或 `signal == "sell"`，不关心信号是如何生成的

#### 代码中使用方式

```python
from analysis.strategy import get_strategy
from analysis.backtest import BacktestEngine

# 按名称创建策略实例
strategy = get_strategy("ma_crossover")          # 默认参数
strategy = get_strategy("rsi", period=14, oversold=30, overbought=70)  # 自定义参数

# 生成信号
df_with_signals = strategy.generate_signals(price_df)

# 运行回测
engine = BacktestEngine(initial_capital=100000)   # 初始资金 10 万
result = engine.run(price_df, strategy)

# 查看结果
print(result["total_return"])     # 总收益率
print(result["sharpe_ratio"])     # 夏普比率
print(result["recommendation"])   # 操作建议
```

#### 现有策略一览

| 短名 | 策略类 | 信号逻辑 | 适用场景 |
|------|--------|---------|---------|
| `ma_crossover` | MACrossoverStrategy | MA5 金叉/死叉 MA20 | 单边趋势市 |
| `macd` | MACDStrategy | DIF 金叉/死叉 DEA | 趋势市（比均线更快响应） |
| `rsi` | RSIStrategy | RSI < 30 回升→买入，> 70 回落→卖出 | 震荡市波段操作 |
| `bollinger` | BollingerBandsStrategy | 跌破下轨→买入，突破上轨→卖出 | 均值回归行情 |
| `buy_and_hold` | BuyAndHoldStrategy | 期初买入，期末卖出，中间不动 | **基准对照** |
| `triple_ma` | TripleMACrossoverStrategy | 三均线多头/空头排列 | 强趋势确认 |

> `buy_and_hold` 是基准策略：用它跑回测得到的收益率，衡量其他策略是否跑赢了"不动"。

#### 策略参数说明

| 策略 | 构造参数 | 默认值 |
|------|---------|--------|
| MACrossoverStrategy | `fast_period=5, slow_period=20` | 5 日 / 20 日 |
| MACDStrategy | `fast=12, slow=26, signal=9` | 经典 MACD 参数 |
| RSIStrategy | `period=14, oversold=30, overbought=70` | 14 日 / 30-70 |
| BollingerBandsStrategy | `period=20, std_dev=2` | 20 日 / 2σ |
| BuyAndHoldStrategy | 无参数 | — |
| TripleMACrossoverStrategy | `fast=5, mid=10, slow=20` | 5/10/20 日 |

#### 如何添加新策略

1. 继承 `BaseStrategy` 抽象基类
2. 实现 `generate_signals(df)` — 在 DataFrame 的 `signal` 列填入 `"buy"` / `"sell"` / `""`
3. 实现 `name` 和 `description` 属性
4. 在 `_STRATEGIES` 字典中注册（key = 策略短名，value = 类引用）
5. 在 UI 的下拉框中添加选项（`ui/main_page.py` 的 `_strategy_dd`）

**示例 — RSI 超买超卖策略**：
```python
class RSIStrategy(BaseStrategy):
    """RSI < 30 买入，RSI > 70 卖出"""
    def __init__(self, period=14, oversold=30, overbought=70):
        self.period = period; self.oversold = oversold; self.overbought = overbought

    @property
    def name(self): return f"RSI策略(周期{self.period})"
    @property
    def description(self): return f"RSI < {self.oversold} 买入，RSI > {self.overbought} 卖出"

    def generate_signals(self, df):
        result = df.copy(); result["signal"] = ""
        result["rsi"] = self._calc_rsi(result["close"])
        for i in range(1, len(result)):
            if result["rsi"].iloc[i] < self.oversold:
                result.loc[result.index[i], "signal"] = "buy"
            elif result["rsi"].iloc[i] > self.overbought:
                result.loc[result.index[i], "signal"] = "sell"
        return result
```

---

### 11.2 数据源扩展（data/stock_fetcher.py）

**文件位置**：`data/stock_fetcher.py`

**如何添加新数据源**：
1. 继承 `BaseStockFetcher`，实现 `fetch_stock_info()` 和 `fetch_price_history()`
2. 返回格式必须统一为 `StockInfo` 和 `list[PriceData]`
3. 在 `config/settings.py` 的 `DEFAULT_CONFIG["data_source"]` 中新增选项键名
4. 在 `get_stock_fetcher()` 工厂函数中添加对应分支
5. 在 `ui/settings_ui.py` 的 `_data_source_dd` 下拉框中添加选项

**示例 — 接入东方财富直接 API**：
```python
class EastMoneyFetcher(BaseStockFetcher):
    def __init__(self, api_key=""):
        self.api_key = api_key
    def fetch_stock_info(self, code): ...
    def fetch_price_history(self, code, start, end): ...
```

**现有数据源**：
| 数据源 | 类名 | A 股引擎 | 美股引擎 |
|--------|------|---------|---------|
| `free` | FreeStockFetcher | akshare | yfinance |
| `custom` | CustomStockFetcher | 占位 | 占位 |

---

### 11.3 新闻源扩展（data/news_fetcher.py）

**文件位置**：`data/news_fetcher.py`

**如何添加新新闻源**：
1. 在 `fetch_news()` 函数中为新市场/新源添加分支
2. 实现对应的 `_fetch_news_xxx()` 函数，返回 `list[NewsItem]`
3. 要求：NewsItem 至少填充 `code`, `date`, `title`, `source` 字段
4. `sentiment` 和 `confidence` 字段由 `analysis/sentiment.py` 的 `analyze()` 统一填充

**可用第三方新闻源**：
| 数据源 | 接入方式 | 覆盖市场 |
|--------|---------|---------|
| NewsAPI | REST API（免费额度 100次/天） | 全球 |
| Finnhub | REST API（免费额度 60次/分） | 美股 |
| 新浪财经 | 网页抓取 | A 股 |
| 彭博 | 付费 API | 全球 |

**现有新闻源**：
| 市场 | 函数 | 数据来源 |
|------|------|---------|
| A 股 | _fetch_news_a() | akshare → 东方财富 |
| 美股 | _fetch_news_us() | yfinance → Yahoo Finance |

---

### 11.4 情感分析模型替换（analysis/sentiment.py）

**文件位置**：`analysis/sentiment.py`

**如何切换情感分析模型**：
1. 修改 `_get_pipeline()` 中的 `model_name` 为新的 HuggingFace 模型 ID
2. 如果新模型输出标签格式不同，在 `analyze()` 中调整标签映射逻辑
3. 如果不需要降级方案，可移除 `_SimpleFallbackAnalyzer`

**可选替代模型**：
| 模型 ID | 特点 |
|---------|------|
| `ProsusAI/finBERT` | 原始 FinBERT，更重但更全面 |
| `yiyanghkust/finbert-tone` | 专做语调分析 |
| `nlptown/bert-base-multilingual-uncased-sentiment` | 多语言支持 |
| `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` | 当前使用，轻量 |

**注意**：更换模型后可能需要调整 `analyze()` 中的标签映射（如 `LABEL_0` → `positive` 等），因不同模型的输出标签顺序可能不同。

---

### 11.5 技术指标扩展（analysis/technical.py）

**文件位置**：`analysis/technical.py`

**如何添加新指标**：
1. 编写计算函数（签名参考 `calc_rsi(df, period)`）
2. 在 `calc_all_indicators()` 中链式调用
3. 在 `summarize()` 中添加对应的分析文本段落
4. 如果新指标需要传入回测/图表的 DataFrame，确保在主流程中已计算

**可添加的常用指标**：
| 指标 | 含义 | 参考库 |
|------|------|--------|
| WR（威廉指标） | 超买超卖 | 自实现或 ta 库 |
| CCI（商品通道指数） | 趋势识别 | ta.trend.CCIIndicator |
| OBV（能量潮） | 量价关系 | ta.volume.OnBalanceVolumeIndicator |
| BIAS（乖离率） | 价格偏离 | 自实现 |
| 成交量加权均线 VWAP | 日内均价 | 自实现 |

---

### 11.6 回测引擎（analysis/backtest.py）

**文件位置**：`analysis/backtest.py`

#### 回测引擎工作原理

回测引擎模拟一个交易账户在历史数据上的表现：

```
初始状态：现金 100,000，持股 0

逐日遍历 K 线：
  if signal == "buy" and 有现金:
      用 95% 资金买入 → 扣佣金 (0.03%) → 记录交易
  elif signal == "sell" and 有股票:
      全部卖出 → 扣佣金 → 回收现金
  记录当日权益 = 现金 + 持股 × 收盘价

计算指标：总收益率 → 年化收益率 → 最大回撤 → 夏普比率 → 胜率
给出建议：强烈买入 / 谨慎买入 / 观望 / 卖出回避
```

#### 交易假设与限制

| 假设 | 默认值 | 说明 |
|------|--------|------|
| 初始资金 | ¥100,000 | 可在 `BacktestEngine(capital)` 中修改 |
| 仓位比例 | 95% | 每次用 95% 资金买入，留 5% 缓冲 |
| 佣金费率 | 0.03%（万三） | 单边收取，买卖均扣 |
| 卖出方式 | 全仓清空 | 简化处理，不支持分批止盈 |
| 未考虑 | T+1、涨跌停、滑点 | 后续可扩展 |

#### 代码中使用方式

```python
from analysis.backtest import BacktestEngine
from analysis.strategy import get_strategy

# 创建引擎
engine = BacktestEngine(
    initial_capital=100000.0,    # 初始资金
    commission=0.0003,           # 万三佣金
)

# 运行回测
strategy = get_strategy("ma_crossover")
result = engine.run(price_df, strategy)

# 返回的 result 字典包含
print(result["total_return"])       # 0.1523  → 总收益 15.23%
print(result["annual_return"])      # 0.0847  → 年化 8.47%
print(result["max_drawdown"])       # 0.123   → 最大回撤 12.3%
print(result["sharpe_ratio"])       # 1.25    → 夏普比率
print(result["win_rate"])           # 0.60    → 胜率 60%
print(result["total_trades"])       # 12      → 总交易次数
print(result["trades"])             # [...]   → 最近 10 条交易明细
print(result["recommendation"])     # "强烈买入" / "谨慎买入" / "观望" / "卖出回避"
print(result["trade_summary"])      # 格式化的中文摘要文本
```

#### 绩效指标详解

| 指标 | 计算公式 | 含义 |
|------|---------|------|
| 总收益率 | (最终权益 - 初始资金) / 初始资金 | 整个回测期的总盈亏比例 |
| 年化收益率 | (1 + 总收益)^(252/交易日) - 1 | 折算为年化后的收益率 |
| 最大回撤 | max((峰值 - 谷值) / 峰值) | 最坏情况下从最高点到最低点的亏损幅度 |
| 夏普比率 | (日均收益 - 无风险日收益) / 日波动 × √252 | 每承担 1 单位风险换来的超额回报，> 1.0 良好 |
| 胜率 | 卖出价 > 买入价的交易 / 总卖出次数 | 交易成功的比例 |

#### 操作建议阈值

```python
# backtest.py:354-361
# 当前阈值（可根据风险偏好调整）
if total_return > 0.10 and sharpe > 1.0 and max_drawdown < 0.15:
    → "强烈买入"
elif total_return > 0 and sharpe > 0.5:
    → "谨慎买入"
elif total_return > -0.05:
    → "观望"
else:
    → "卖出/回避"
```

#### 可增强的功能

| 增强方向 | 实现位置 | 说明 |
|---------|---------|------|
| 滑点模型 | `_simulate()` 方法 | 在成交价上加减滑点比例 |
| 仓位管理 | `_simulate()` 方法 | 分批建仓、金字塔加仓、分批止盈 |
| 多股票组合 | 新增 `run_portfolio()` | 同时回测多只股票 |
| 更多绩效指标 | 新增方法 | 卡玛比率、索提诺比率、信息比率 |
| 权益曲线可视化 | 返回 equity_curve 数据 | 供 UI 绘制权益走势图 |
| T+1 限制 | `_simulate()` 方法 | 当日买入次日才能卖出 |
| 涨跌停限制 | `_simulate()` 方法 | A 股 ±10% 涨跌停无法成交 |

---

### 11.7 报告模板扩展（report/generator.py）

**文件位置**：`report/generator.py`

**如何自定义报告格式**：
1. 修改 `SYSTEM_PROMPT` — 调整 LLM 生成报告的风格和结构要求
2. 修改 `user_prompt` — 增减传给 LLM 的数据维度
3. 修改 `_generate_fallback_report()` — 调整无 LLM 时的模板排版
4. 添加多语言支持 — `SYSTEM_PROMPT` 改为参数化（中文/英文）

**报告风格变体示例**：
```python
SYSTEM_PROMPT_SHORT = """生成简短分析（300 字以内），仅包含：
1. 当前趋势判断 2. 关键信号 3. 一句话建议"""
SYSTEM_PROMPT_DETAILED = """生成详细分析，额外包含：
- 同行业对比 - 估值分析 - 机构持仓变化"""
```

---

### 11.8 PDF 样式扩展（report/pdf_exporter.py）

**文件位置**：`report/pdf_exporter.py`

**可自定义项**：

| 项目 | 修改位置 | 说明 |
|------|---------|------|
| 字体/字号 | `_get_styles()` 中的 ParagraphStyle | 调整 fontSize / leading |
| 品牌配色 | `_get_styles()` 中的 HexColor | 修改 textColor |
| 页面尺寸 | `SimpleDocTemplate` 初始化 | A4 → Letter / A3 |
| 页眉页脚 | onPage 回调 | 添加公司名、页码 |
| Logo/水印 | `elements` 列表 | 插入 Image 或 PDFImage |
| K 线图尺寸 | Image() 中的 width/height | 调整图表大小 |
| 中文字体路径 | `utils/helpers.py` 的 `get_chinese_font_path()` | 添加更多字体搜索路径 |

---

### 11.9 K 线图样式扩展（report/chart.py）

**文件位置**：`report/chart.py`

**可自定义项**：

| 项目 | 修改位置 | 说明 |
|------|---------|------|
| 配色主题 | `style_params["style"]` | `"charles"` / `"binance"` / `"yahoo"` / `"blueskies"` |
| 图表尺寸 | `style_params["figratio"]` | 宽高比 |
| 新增副图指标 | `apds` 列表 | 添加布林带副图、MACD 柱副图、RSI 副图 |
| 买卖信号标记 | `buy_mask` / `sell_mask` | 修改标记颜色、大小或位置 |
| 均线周期 | `df["MA5"]` 等 | 在 `generate_kline_chart()` 中调整窗口期 |

---

### 11.10 UI 扩展（ui/）

**添加新页面**：
1. 继承 `ft.Container`，实现 `build()` 方法
2. 在 `main.py` 的 `main()` 函数中创建 Container 并加入 Stack
3. 在 NavigationBar 中添加新的 NavigationBarDestination

**添加新的 UI 组件**（`ui/components.py`）：
1. 继承 `ft.Container`
2. 实现 `build()` 方法返回控件树
3. 如需自定义属性，使用 `@property` 暴露

**可能的扩展页面**：
| 页面 | 功能 |
|------|------|
| Dashboard | 市场总览仪表盘（指数、热门股票） |
| Watchlist | 自选股监控列表 |
| ComparePage | 多股票对比分析 |
| StrategyTuning | 策略参数优化可视化 |

---

### 11.11 配置扩展（config/settings.py）

**文件位置**：`config/settings.py`

**如何新增配置项**：
1. 在 `DEFAULT_CONFIG` 字典中添加键值对
2. 在 `ui/settings_ui.py` 的 `build()` 方法中添加输入控件
3. 在 `_save_settings()` 方法中添加 `settings.set()` 调用

**可新增的配置项示例**：
```python
DEFAULT_CONFIG = {
    # ... 现有配置 ...
    "default_strategy": "ma_crossover",  # 默认策略
    "backtest_initial_capital": 100000,  # 回测初始资金
    "chart_style": "charles",            # K 线图默认配色
    "report_language": "zh",            # 报告语言
    "max_news_items": 15,               # 最大新闻数量
}
```

---

### 11.12 数据模型扩展（data/models.py）

**文件位置**：`data/models.py`

**如何新增字段**：
1. 在对应的 dataclass 中声明新字段（带默认值）
2. `to_dict()` / `from_dict()` 通过 `__dataclass_fields__` 自动适配，无需修改
3. 如果新增的字段需要存入数据库，在 `database.py` 的中添加对应列

**可扩展的数据字段示例**：
```python
@dataclass
class StockInfo:
    # ... 现有字段 ...
    pe_ratio: float = 0.0      # 市盈率
    market_cap: float = 0.0    # 总市值（亿元）
    dividend_yield: float = 0.0 # 股息率
```

---

### 11.13 评分反馈闭环设计

**数据流**：
```
用户评分(1-5) → reports.rating → 累积评分数据
                                    ↓
                          后续分析：高评分报告的
                          策略参数特征提取 → 权重优化
```

**实现思路**（待开发）：
1. 查询 rating >= 4 的高分报告对应的回测参数
2. 统计哪些策略/参数组合获得了高评分
3. 在 `_make_recommendation()` 或策略初始化时加权参考历史评分数据
4. 报表页面增加「推荐策略」模块，基于评分数据推荐最优参数

---

### 11.14 数据库扩展（data/database.py）

**文件位置**：`data/database.py`

**如何新增表**：
1. 在 `CREATE_TABLES_SQL` 中添加 CREATE TABLE 语句
2. 在 `data/models.py` 中定义对应的 dataclass
3. 在本模块添加对应的 CRUD 方法
4. 建议添加索引以优化查询性能

**可新增的表**：
| 表名 | 用途 |
|------|------|
| watchlist | 自选股列表 |
| alerts | 价格预警设置 |
| strategy_params | 策略参数历史记录（用于优化） |
| portfolio_history | 模拟持仓/调仓记录 |

---

### 11.15 总结：扩展点快速索引

| 扩展目标 | 文件 | 搜索标签 |
|---------|------|---------|
| 添加新交易策略 | `analysis/strategy.py` | `【扩展点】` -> `_STRATEGIES` |
| 添加新数据源 | `data/stock_fetcher.py` | `【扩展点】` -> `get_stock_fetcher()` |
| 添加新新闻源 | `data/news_fetcher.py` | `【扩展点】` -> `fetch_news()` |
| 切换情感分析模型 | `analysis/sentiment.py` | `【扩展点】` -> `_get_pipeline()` |
| 添加新技术指标 | `analysis/technical.py` | `【扩展点】` -> `calc_all_indicators()` |
| 增强回测引擎 | `analysis/backtest.py` | `【扩展点】` -> 各方法注释 |
| 自定义报告模板 | `report/generator.py` | `【扩展点】` -> `SYSTEM_PROMPT` |
| 自定义 PDF 样式 | `report/pdf_exporter.py` | `【扩展点】` -> `_get_styles()` |
| 自定义 K 线图样式 | `report/chart.py` | `【扩展点】` -> `style_params` |
| 新增配置项 | `config/settings.py` | `【扩展点】` -> `DEFAULT_CONFIG` |
| 新增数据字段 | `data/models.py` | `【扩展点】` -> 各 dataclass |
| 新增数据库表 | `data/database.py` | `【扩展点】` -> `CREATE_TABLES_SQL` |
| 新增 UI 组件 | `ui/components.py` | `【扩展点】` -> 类注释 |
| 新增页面 | `main.py` | `switch_page()` 函数 |
| 中文字体路径 | `utils/helpers.py` | `【扩展点】` -> `get_chinese_font_path()` |
| 回测周期选项 | `utils/helpers.py` | `【扩展点】` -> `get_backtest_dates()` |

---

## 十二、快速开始与测试

### 12.1 环境要求

| 依赖 | 版本要求 |
|------|---------|
| Python | >= 3.12 |
| pip | >= 23.0 |
| 操作系统 | macOS / Windows / Linux |
| 网络 | 需访问 PyPI、HuggingFace Hub（首次下载模型约 300MB） |

### 12.2 安装与启动

```bash
# 1. 进入项目目录
cd TradeHelper

# 2. 创建并激活虚拟环境
python3.12 -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# 3. 安装依赖（首次安装约需 3-5 分钟，含 PyTorch ~800MB）
pip install -r requirements.txt

# 4. 启动应用
python main.py
```

### 12.3 首次使用流程

1. 启动后自动弹出提示，引导进入**设置**页面
2. 在设置中配置：
   - **工作目录**（必填）：数据、报告、日志的存放路径
   - **大模型 API**（可选）：不填则使用本地模板生成报告，功能不受影响
3. 保存设置后返回**分析**页面
4. 输入股票代码（如 `600519` 贵州茅台 / `AAPL` 苹果）
5. 选择回测周期，点击「开始分析」
6. 等待分析完成后可：
   - 查看 K 线图和技术/新闻/回测分析报告
   - 导出 PDF 报告
   - 给报告评分（1-5 星）

### 12.4 测试方法

#### 12.4.1 模块级单元测试（推荐在项目成熟后补充）

项目采用分层解耦架构，每个模块可以独立导入测试：

```bash
# 激活虚拟环境后，在 Python 交互环境中测试各模块

# 测试股票代码校验
python -c "from utils.helpers import is_valid_stock_code; print(is_valid_stock_code('600519'))"

# 测试配置读写
python -c "
from config.settings import Settings
s = Settings.init('.test_config.json')
s.set('test_key', 'hello')
s.save()
print(s.get('test_key'))
"

# 测试数据库初始化（会自动在工作目录创建 tradehelper.db）
python -c "
from config.settings import Settings
from data.database import Database
Settings().set('work_dir', '/tmp/TradeHelperTest')
Database.init()
print('Database OK')
"

# 测试数据获取（A 股）
python -c "
from data.stock_fetcher import FreeStockFetcher
f = FreeStockFetcher()
info = f.fetch_stock_info('600519')
print(f'名称: {info.name}, 行业: {info.industry}')
"

# 测试数据获取（美股）
python -c "
from data.stock_fetcher import FreeStockFetcher
f = FreeStockFetcher()
prices = f.fetch_price_history('AAPL', '2024-01-01', '2024-12-31')
print(f'获取到 {len(prices)} 条数据')
"

# 测试技术指标计算
python -c "
import pandas as pd
from analysis.technical import calc_all_indicators, summarize
# 构造模拟数据
dates = pd.date_range('2024-01-01', periods=60, freq='B')
df = pd.DataFrame({
    'date': dates, 'open': 100, 'high': 102, 'low': 98,
    'close': [100 + i * 0.5 for i in range(60)], 'volume': 1e6
})
df = calc_all_indicators(df)
print(summarize(df, 'TEST'))
"

# 测试策略与回测
python -c "
import pandas as pd
from analysis.strategy import get_strategy
from analysis.backtest import BacktestEngine
dates = pd.date_range('2024-01-01', periods=120, freq='B')
close = [100.0]
import random; random.seed(42)
for _ in range(119): close.append(close[-1] * (1 + random.uniform(-0.03, 0.03)))
df = pd.DataFrame({
    'date': dates, 'open': [c*0.99 for c in close], 'high': [c*1.02 for c in close],
    'low': [c*0.98 for c in close], 'close': close, 'volume': [1e6]*120
})
s = get_strategy('ma_crossover')
result = BacktestEngine().run(df, s)
print(result['trade_summary'])
print(f'建议: {result[\"recommendation\"]}')
"

# 测试 K 线图生成
python -c "
import pandas as pd
from report.chart import generate_kline_chart
dates = pd.date_range('2024-10-01', periods=30, freq='B')
df = pd.DataFrame({
    'date': dates, 'open': 100, 'high': 102, 'low': 98,
    'close': [100 + i for i in range(30)], 'volume': 1e6,
})
path = generate_kline_chart(df, '600519', '贵州茅台')
print(f'Chart path: {path}')
"
```

#### 12.4.2 集成测试（模拟完整分析流程）

```bash
# 在 Python 环境中模拟一次完整的分析流程
python -c "
from config.settings import Settings
from data.database import Database
from data.stock_fetcher import FreeStockFetcher
from data.news_fetcher import fetch_news
from analysis.technical import calc_all_indicators, summarize
from analysis.sentiment import analyze, aggregate
from analysis.strategy import get_strategy
from analysis.backtest import BacktestEngine
from report.chart import generate_kline_chart
from report.generator import generate_report
import pandas as pd

# 设置临时工作目录
Settings().set('work_dir', '/tmp/TradeHelperTest')
Database.init()

# 获取数据
fetcher = FreeStockFetcher()
info = fetcher.fetch_stock_info('600519')
prices = fetcher.fetch_price_history('600519', '2023-12-01', '2024-12-01')
print(f'获取股票: {info.name}, 数据: {len(prices)}条')

# 指标计算
df = pd.DataFrame([p.to_dict() for p in prices])
df['date'] = pd.to_datetime(df['date'])
df = df.sort_values('date')
df = calc_all_indicators(df)

# 新闻情感
news = fetch_news('600519', 'A')
news = analyze(news)
agg = aggregate(news)
print(f'情感分析: {agg[\"summary\"]}')

# 回测
s = get_strategy('ma_crossover')
result = BacktestEngine().run(df, s)
print(f'回测: {result[\"trade_summary\"][:100]}...')

# 生成报告
tech = summarize(df, info.name)
report = generate_report(info.to_dict(), tech, agg, result)
print(f'报告生成完成，长度: {len(report)} 字')
print('=== 集成测试通过 ===')
"
```

#### 12.4.3 测试检查清单

| 测试项 | 验证方式 | 预期结果 |
|--------|---------|---------|
| 应用启动 | `python main.py` | 窗口正常显示，底部导航栏可见 |
| 设置保存 | 设置页修改→保存→关闭重启 | 配置持久化，重启后保持 |
| A 股分析 | 输入 `600519` → 分析 | 显示报告、K 线图、导出 PDF |
| 美股分析 | 输入 `AAPL` → 分析 | 显示报告、K 线图、导出 PDF |
| 无 API 模式 | 不填 LLM Key → 分析 | 生成基础模板报告（无 LLM 也能用） |
| 报告评分 | 分析完成→点星级评分 | 评分存入数据库，历史页可见 |
| 历史报告 | 切换到历史页 | 显示已有报告，可重新评分/导出/删除 |
| PDF 导出 | 点击导出 PDF | 生成中文 PDF 文件，含 K 线图 |
| 无效输入 | 输入 `abc123` → 分析 | 显示"无效的股票代码格式" |
| 情感模型 | 首次分析 | 自动下载 FinBERT 模型到 ~/.cache/ |
| 降级方案 | 断开网络 → 分析 | 关键词匹配替代模型推理，分析不中断 |

### 12.5 工作目录结构

首次分析后，工作目录下会自动生成以下文件：

```
~/TradeHelperData/               # 默认工作目录（可在设置中修改）
├── tradehelper.db               # SQLite 数据库
├── charts/                      # K 线图 PNG 文件
│   └── 600519_20250525_143022.png
├── reports/                     # PDF 报告文件
│   └── report_600519_20250525_143025.pdf
└── logs/                        # 运行日志
    └── tradehelper.log
```

### 12.6 常见问题

**Q: 启动报错 `No module named 'flet'`**
A: 未激活虚拟环境或未安装依赖。执行 `source venv/bin/activate && pip install -r requirements.txt`

**Q: 首次运行卡在"加载情感模型"很久**
A: FinBERT 模型约 300MB，首次从 HuggingFace 下载需等待 2-5 分钟（取决于网速）。后续使用缓存，秒级加载。

**Q: 下载模型报错网络超时**
A: 可设置 HuggingFace 镜像：`export HF_ENDPOINT=https://hf-mirror.com`

**Q: PDF 导出后中文显示为方块**
A: 系统未找到中文字体。手动安装中文字体，或在 `utils/helpers.py` 的 `get_chinese_font_path()` 中添加字体路径。

**Q: A 股数据获取失败**
A: akshare 接口有时会被限流或接口变更。升级 akshare：`pip install -U akshare`。也可在设置中切换到自定义 API。

**Q: 如何在 Windows 上运行**
A: 安装 Python 3.12 后，Git Bash 或 PowerShell 中执行相同命令。注意虚拟环境激活命令为 `venv\Scripts\activate`。
