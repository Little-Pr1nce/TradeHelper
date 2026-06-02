<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

<h1 align="center">TradeHelper</h1>
<p align="center"><strong>专业量化股票分析助手</strong></p>
<p align="center">A 股 & 美股 · Alpha 多因子打分 · 三策略回测 · 防偷窥 T+1 撮合 · AI 报告生成</p>

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **多市场支持** | A 股 + 美股自动识别，支持代码或公司名称搜索 |
| **多数据源** | 免费（akshare / Finnhub）/ 付费（itick）一键切换，K 线增量更新 |
| **技术指标** | MA / MACD / RSI / 布林带 / KDJ，向量化预计算 |
| **Alpha 多因子打分** | 7 指标 Z-Score + tanh + IC/IR 因子检验 + FinBERT 情感 + 基本面因子（akshare 真实数据） |
| **三策略回测** | 阈值趋势跟踪 / 波动率均值回归 / 动量新闻共振，T+1 防偷窥撮合 |
| **新闻情感分析** | FinBERT 中文关键词兜底 + LLM 新闻获取 + 策略 Provider 模式 |
| **盘口因子** | itick 实时买卖力量对比，影响综合建议 |
| **AI 报告生成** | DeepSeek / GPT / Ollama 兼容，含多策略对比、因子检验、基本面、盘口、AI 综合分析预测 |
| **报告导出** | 一键导出 HTML（含内嵌 K 线图），浏览器直接打开 |
| **报告评分** | 1-5 星评分，积累反馈数据 |
| **本地缓存** | SQLite 缓存股价和新闻，智能判断是否过期 |

---

## 快速开始

### 环境要求

- Python >= 3.12
- pip >= 23.0
- 网络连接（首次需下载 FinBERT 模型 ~300MB）

### 安装

```bash
git clone <repo-url>
cd TradeHelper

python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 首次使用

1. 启动后进入 **设置** 页，配置工作目录
2. 配置大模型 API（DeepSeek / GPT / Ollama 均可）
3. 如需美股新闻，可配置 Finnhub API Key（免费注册 https://finnhub.io）
4. 如使用付费数据源，填入 itick Token
5. 返回 **分析** 页，输入股票代码或公司名称（如 `600519`/`茅台`/`NVDA`/`英伟达`）
6. 选择回测周期和数据源，点击「开始分析」
7. 查看 K 线图 + Alpha 因子得分 + 三策略回测对比 + AI 报告
8. 点击「导出为文件」→ 弹窗选择用浏览器打开

---

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  UI 层 (ui/main_page.py)         CLI 层 (run_backtest.py)│
│  纯展示：Flet 控件渲染              纯展示：print 输出     │
├──────────────────────────────────────────────────────────┤
│               Services 层 (services/)                     │
│         AnalysisService — 业务编排 & 工作流控制           │
│     搜索 → 取数(含缓存) → 管道 → 图表 → 报告 → 持久化     │
├──────────────────────────────────────────────────────────┤
│         Core 层 (core/pipeline.py + types.py)             │
│      纯计算管道：技术指标 → Alpha 打分 → 三策略回测       │
├──────────────────┬───────────────────────────────────────┤
│  计算引擎层       │  支撑层                                │
│  alpha/          │  data/     SQLite + 多源数据 API       │
│  strategies/     │  config/   全局配置                     │
│  backtest/       │  utils/    市场/日期/字体/日志          │
│  indicators/     │  report/   图表 + 报告 + 导出           │
│                  │  data_adapters/  扩展预留               │
└──────────────────┴───────────────────────────────────────┘
```

### 分层职责

| 层 | 目录 | 职责 | 依赖方向 |
|----|------|------|----------|
| **展示层** | `ui/`, `run_backtest.py` | 渲染 UI / 打印输出 | → Services |
| **服务层** | `services/` | 编排工作流、I/O 调度、缓存策略 | → Core + Data |
| **核心层** | `core/` | 纯计算管道，无 I/O、无副作用 | → 计算引擎 |
| **计算引擎** | `alpha/` `strategies/` `backtest/` | 因子打分、策略信号、回测撮合 | → 无 |
| **指标计算** | `indicators/` | 技术指标、情感分析 | → 无 |
| **数据层** | `data/` | SQLite CRUD、多源数据获取 | → Config |
| **支撑层** | `config/` `utils/` `report/` | 配置、工具、图表、报告 | → 横向 |

---

## 项目结构

```
TradeHelper/
├── main.py                       # 桌面应用入口
├── run_backtest.py               # CLI 回测脚本
├── requirements.txt
├── README.md
├── README_BACKTEST.md            # 回测系统详细文档
├── DESIGN.md                     # 完整设计文档
│
├── core/                         # 公共管道（UI / CLI 共用）
│   ├── pipeline.py               #   分析管道：指标 → 打分 → 回测
│   └── types.py                  #   核心数据类型
│
├── services/                     # 业务编排层
│   └── analysis_service.py       #   完整分析工作流
│
├── alpha/                        # Alpha 多因子打分
│   ├── scoring.py                #   纯函数：Z-Score + tanh + 加权合成 + 因子检验
│   ├── validation.py             #   因子 IC/IR 有效性检验（D 级剔除 / C 级半权）
│   ├── fundamental.py            #   基本面因子（akshare 真实数据 / LLM 兜底）
│   ├── fundamental_llm.py        #   LLM PE/PB 估算（美股百度接口不稳定时的兜底）
│   └── depth_factor.py           #   实时盘口因子（itick）
│
├── strategies/                   # 交易执行策略库
│   ├── base.py                   #   基类 + Order/Fill + compute_atr()
│   ├── threshold_trend.py        #   策略 A：阈值滞后带趋势跟踪
│   ├── mean_reversion.py         #   策略 B：波动率自适应均值回归
│   └── momentum_news.py          #   策略 C：动量突破 + 新闻共振确认
│
├── backtest/                     # 事件驱动回测引擎
│   ├── broker.py                 #   T+1 撮合（滑点/涨跌停/流动性/硬止损/时间止损）
│   ├── engine.py                 #   回测主循环 + 多策略并行
│   └── analytics.py              #   绩效指标 + Rank IC + 对比图表
│
├── indicators/                   # 技术指标 & 情感分析
│   ├── technical.py              #   MA / MACD / RSI / 布林带 / KDJ
│   ├── sentiment.py              #   FinBERT 三阶段加载 + 中文关键词兜底
│   └── constants.py              #   共享常量
│
├── data/                         # 数据持久化层
│   ├── models.py                 #   数据模型（StockInfo / PriceData 等）
│   ├── database.py               #   SQLite CRUD + schema 迁移 + 新闻去重
│   ├── stock_fetcher.py          #   股价获取（FreeStockFetcher / ItickStockFetcher）
│   ├── news_fetcher.py           #   新闻获取（缓存 → Finnhub → LLM 补充 → 历史降级）
│   ├── news_providers.py         #   新闻源策略：A 股东方财富 / 美股 Finnhub
│   └── finnhub_client.py         #   Finnhub API 客户端（搜索/信息/新闻/基本面）
│
├── report/                       # 报告生成
│   ├── prompts.py                #   LLM 提示词模板
│   ├── generator.py              #   报告生成（LLM / 回退模板）含 _clean_llm_output()
│   ├── chart.py                  #   K 线图（mplfinance）
│   └── pdf_exporter.py           #   PDF 导出（reportlab，含表格渲染）
│
├── config/
│   └── settings.py               # 全局配置（单例 + JSON 持久化）
│
├── utils/                        # 工具函数（单文件单职责）
│   ├── market.py                 #   市场识别 + 股票搜索
│   ├── dates.py                  #   回测周期计算
│   ├── fonts.py                  #   中文字体查找
│   └── logging.py                #   日志配置
│
├── ui/                           # Flet 桌面界面
│   ├── main_page.py              #   主分析页（纯 UI）
│   ├── history_page.py           #   历史报告页
│   ├── settings_ui.py            #   设置页（含 Finnhub / itick / LLM 配置）
│   └── components.py             #   复用组件（StarRating）
│
├── data_adapters/                # 扩展预留（仅接口定义）
│   └── __init__.py
│
└── tests/                        # 单元测试（42 个）
    ├── test_scoring.py           #   Alpha 打分模型
    ├── test_strategies.py        #   三策略
    └── test_backtest.py          #   回测引擎
```

---

## 数据流

```
用户输入（代码或名称）
  │
  ├─ 1. 股票搜索 (utils/market.py)     → 中文名 → 代码
  ├─ 2. 数据获取 (data/stock_fetcher.py) → 免费(akshare/itick) / 付费(itick)，K 线增量更新
  ├─ 3. 新闻获取 (data/news_fetcher.py)  → 缓存 → Finnhub Provider → LLM 补充 → 历史降级
  ├─ 4. 技术指标 (indicators/technical.py) → 7 个指标向量化
  ├─ 5. 情感分析 (indicators/sentiment.py) → FinBERT + 中文关键词兜底
  ├─ 6. Alpha 打分 (alpha/scoring.py)  → 因子检验 + 扩展权重合成（技术 35%+风格 15%+基本面 25%+新闻 25%）
  ├─ 7. 基本面 (alpha/fundamental.py)   → Finnhub / akshare 真实数据 / LLM 估算，三级降级
  ├─ 8. 三策略回测 (backtest/engine.py) → T 日信号 → T+1 撮合 → 风控，美股不限涨跌停
  ├─ 9. 盘口数据 (alpha/depth_factor.py) → itick 买卖比，仅影响报告展示
  └─ 10. 报告生成 (report/generator.py) → LLM 全量数据交叉分析 + AI 预测 / 本地模板兜底 → 导出 HTML
```

---

## Alpha 多因子打分模型

### 公式

$$FinalScore = \sum_{i} w_i \cdot S_i$$

| 因子类别 | 权重 | 来源 |
|---------|:--:|------|
| 技术面（7 指标，IC/IR 检验后加权） | 35% | K 线 |
| 风格（PE/PB 分位） | 15% | akshare / LLM |
| 基本面（ROE/毛利率等） | 25% | akshare / LLM |
| 新闻面（FinBERT） | 25% | LLM 新闻 + FinBERT |

无基本面数据时自动切换为：技术 60% + 新闻 40%（无新闻时技术 100%）。因子经 IC/IR 检验动态调权，D 级剔除、C 级半权。Rank IC 和 benchmark 传入 LLM 用于综合判断。

### 7 个技术指标

| 指标 | 含义 | 方向 |
|------|------|:--:|
| RSI | 超买超卖 | 高=偏空，低=偏多 |
| DIF | MACD 快线 | 正=多头 |
| MACD 柱 | 动能强度 | 正=增强 |
| 布林 %B | 价格在带中位置 | >1=突破上轨 |
| K | KDJ 快线 | 短期动量 |
| D | KDJ 慢线 | 中期动量 |
| J | KDJ 辅助线 | 极端值检测 |

### 因子有效性检验

每个技术因子做 IC/IR 检验：
- A 级（\|IC\|≥0.10 + \|IR\|≥1.0）：全权
- B 级（\|IC\|≥0.06 + \|IR\|≥0.5）：全权
- C 级（仅一项通过）：半权
- D 级（都不达标）：剔除

---

## 三种交易策略

| 策略 | 代号 | 开仓条件 | 平仓条件 | 冷却期 | 仓位管理 |
|------|:--:|---------|---------|:--:|---------|
| **阈值趋势跟踪** | A | Score > 0.6 | Score < 0.3 | 3 日 | 2% / 2×ATR |
| **波动率均值回归** | B | Score < -0.5 且低波 | Score > 0.2 或浮盈 3×ATR | 5 日 | 反波动率 [1%-4%] |
| **动量新闻共振** | C | Score > 0.7 + FinBERT > 0.8 + 突破20日高 | Score < 0.4 或移动止盈 | 2 日 | 金字塔 ≤3% |

**通用硬风控**：-8% 止损 · 10 日时间止损 · A 股涨跌停过滤（美股不设限）· 单笔 ≤ 5% 日成交量

---

## 回测引擎

### 撮合时序（防偷窥）

```
T 日收盘   → 读 Final_Score → 策略 generate_orders()
T+1 开盘   → open × (1+0.3%) → 撮合成交
T+1 盘中   → High/Low 检查   → 硬止损 / 移动止盈 / 时间止损
T+1 收盘   → 更新净值        → 记录权益曲线
```

### 绩效指标

| 指标 | 说明 |
|------|------|
| 总收益率 / 年化收益率 | 回测期盈亏及年化复利 |
| 最大回撤 | 峰值到谷值的最大亏损 |
| 夏普比率 / Calmar 比率 | 风险调整后收益 |
| 胜率 / 盈亏比 | 盈利交易占比及平均盈亏比 |
| Rank IC / IC_IR | 因子预测有效性 |
| 策略相关性 | 三策略日收益相关系数 |

---

## 配置说明

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
|------|------|
| `stock_data_token` | itick API Token（付费，K线/信息/盘口） |
| `news_token_us` | Finnhub API Key（免费注册 https://finnhub.io，美股搜索/信息/新闻/基本面，60 次/分钟） |
| `news_token_a` | A 股额外新闻 Token（预留，如 Tushare） |
| `llm_*` | 大模型配置，不填则用本地模板 |
| `proxy` | 代理地址（海外服务用） |

---

## 新闻获取降级链路

```
cache(24h) → AkshareEastMoney(A股) / Finnhub(美股) → LLM 补充 → 历史缓存兜底
```

A 股走东方财富免费 akshare 接口，美股走 Finnhub API（需 `finnhub_api_key`），不足时 LLM 补充，确保始终有新闻可用。

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **UI** | Flet (Flutter) |
| **A 股数据** | akshare（东方财富）/ itick |
| **美股数据** | itick（K线/信息/盘口）/ Finnhub（搜索/信息/新闻/基本面） |
| **技术指标** | pandas + numpy |
| **情感分析** | FinBERT (HuggingFace) + 中文关键词兜底 |
| **大模型** | OpenAI 兼容 API（DeepSeek / GPT / Ollama） |
| **K 线图** | mplfinance |
| **数据库** | SQLite (WAL) |

---

## 扩展开发

| 扩展目标 | 入口 | 方式 |
|---------|------|------|
| 新增因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` | 添加指标列名 |
| 新增策略 | `strategies/` | 继承 `BaseExecutionStrategy`，注册到 `__init__.py` |
| 替换情感模型 | `indicators/sentiment.py` → `_get_pipeline()` | 修改模型 ID |
| 接入新数据源 | `data/stock_fetcher.py` → `BaseStockFetcher` | 实现接口 |
| 新增新闻源 | `data/news_providers.py` → `BaseNewsProvider` | 继承 Provider |
| 新增配置项 | `config/settings.py` → `DEFAULT_CONFIG` + `settings_ui.py` 添加输入框 |
| 新 UI 页面 | `ui/` | 继承 `ft.Container`，在 `main.py` 注册 |
| 调整提示词 | `report/prompts.py` | 修改 `SYSTEM_PROMPT` |

---

## 文档

| 文档 | 说明 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 完整设计文档 |
| [README_BACKTEST.md](./README_BACKTEST.md) | 回测系统详细说明 |

## License

MIT © TradeHelper
