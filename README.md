<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/flet-0.85-blue.svg" alt="Flet">
  <img src="https://img.shields.io/badge/Frontend-Flet%20(Flutter)-02569B?logo=flutter" alt="Frontend">
</p>

<h1 align="center">TradeHelper</h1>
<p align="center"><strong>专业量化股票分析助手</strong></p>
<p align="center">A 股 & 美股 · Alpha 多因子打分 · 三策略回测 · 防偷窥 T+1 撮合 · AI 报告生成</p>

---

## 截图预览

<p align="center">
  <em>主分析页面——输入代码或公司名称，一键生成含 K 线图和多策略回测对比的分析报告</em><br>
  <em>（启动 <code>python main.py</code> 即可体验）</em>
</p>

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **多市场支持** | A 股 + 美股自动识别，支持代码或公司名称搜索 |
| **技术指标** | MA / MACD / RSI / 布林带 / KDJ，向量化预计算 |
| **Alpha 多因子打分** | 7 个技术指标 Z-Score + tanh 标准化 + FinBERT 情感加权合成 |
| **三策略回测** | 阈值趋势跟踪 / 波动率均值回归 / 动量新闻共振，T+1 防偷窥撮合 |
| **新闻情感分析** | FinBERT 深度学习模型，三分类（正面/负面/中性） |
| **AI 报告生成** | OpenAI 兼容大模型，基于真实数据生成含多策略对比的专业报告 |
| **PDF 导出** | 一键导出含 K 线图的中文 PDF |
| **报告评分** | 1-5 星评分，积累反馈数据 |
| **本地缓存** | SQLite 缓存股价和新闻，24h 内不重复请求 |
| **CLI 模式** | `run_backtest.py` 命令行脚本，支持 `--strategy A/B/C/all` |

---

## 快速开始

### 环境要求

- Python >= 3.12
- pip >= 23.0
- 网络连接（首次需下载 FinBERT 模型 ~300MB）

### 安装

```bash
git clone <your-repo-url>
cd TradeHelper

python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 首次使用

1. 启动后进入 **设置** 页，配置工作目录
2. （可选）配置大模型 API Key
3. 返回 **分析** 页，输入股票代码或公司名称（如 `600519`/`茅台`/`AAPL`/`英伟达`）
4. 选择回测周期，点击「开始分析」
5. 查看 K 线图 + Alpha 因子得分 + 三策略回测对比 + AI 报告

### CLI 模式

```bash
# 运行全部三种策略
python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31

# 仅运行策略 A
python run_backtest.py --code AAPL --start 2024-01-01 --end 2024-12-31 --strategy A

# 自定义资金和权重
python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31 --capital 500000 --w-tech 0.7
```

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
│  alpha/          │  data/     SQLite + API 数据源         │
│  strategies/     │  config/   全局配置                     │
│  backtest/       │  utils/    市场/日期/字体/日志          │
│  indicators/     │  report/   图表 + 报告 + PDF            │
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
| **数据层** | `data/` | SQLite CRUD、API 数据获取 | → Config |
| **支撑层** | `config/` `utils/` `report/` | 配置、工具、图表、PDF | → 横向 |

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
├── core/                         # ★ 公共管道（UI / CLI 共用）
│   ├── pipeline.py               #   分析管道：指标 → 打分 → 回测
│   └── types.py                  #   核心数据类型（AlphaStats 等）
│
├── services/                     # ★ 业务编排层
│   └── analysis_service.py       #   完整分析工作流
│
├── alpha/                        # Alpha 多因子打分
│   └── scoring.py                #   纯函数：Z-Score + tanh + 加权合成
│
├── strategies/                   # 交易执行策略库
│   ├── base.py                   #   基类 + Order/Fill + compute_atr()
│   ├── threshold_trend.py        #   策略 A：阈值滞后带趋势跟踪
│   ├── mean_reversion.py         #   策略 B：波动率自适应均值回归
│   └── momentum_news.py          #   策略 C：动量突破 + 新闻共振确认
│
├── backtest/                     # 事件驱动回测引擎
│   ├── broker.py                 #   T+1 撮合（滑点/涨跌停/流动性/风控）
│   ├── engine.py                 #   回测主循环 + 多策略并行
│   └── analytics.py              #   绩效指标 + Rank IC + 对比图表
│
├── indicators/                   # 技术指标 & 情感分析
│   ├── technical.py              #   MA / MACD / RSI / 布林带 / KDJ
│   ├── sentiment.py              #   FinBERT 情感分析
│   └── constants.py              #   共享常量
│
├── data/                         # 数据持久化层
│   ├── models.py                 #   数据模型（StockInfo / PriceData 等）
│   ├── database.py               #   SQLite CRUD（4 张表）
│   ├── stock_fetcher.py          #   股价获取（策略模式：akshare / yfinance）
│   └── news_fetcher.py           #   新闻获取
│
├── report/                       # 报告生成
│   ├── prompts.py                #   LLM 提示词模板
│   ├── generator.py              #   报告生成（LLM / 回退模板）
│   ├── chart.py                  #   K 线图（mplfinance）
│   └── pdf_exporter.py           #   PDF 导出（reportlab）
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
│   ├── settings_ui.py            #   设置页
│   └── components.py             #   复用组件（StarRating）
│
├── data_adapters/                # 扩展预留（当前仅接口定义）
│   └── __init__.py
│
└── tests/                        # 单元测试（42 个）
    ├── test_scoring.py           #   Alpha 打分模型
    ├── test_strategies.py        #   三策略
    └── test_backtest.py          #   回测引擎
```

---

## 交易策略

### Alpha 多因子打分模型

$$FinalScore = 0.6 \times \underbrace{\text{mean}\big(\tanh(Z_i)\big)}_{\text{7个技术指标 Z-Score}} + 0.4 \times \underbrace{\text{FinBERT Score}}_{\text{新闻情感}}$$

- 7 个独立指标：RSI、DIF、MACD 柱、布林带 %B、K、D、J
- 滚动 Z-Score 标准化（窗口 60）+ tanh 压缩至 [-1, +1]
- 新闻面缺失 = 0（中性），严禁向前/后填充

### 三种执行策略

| 策略 | 代号 | 开仓条件 | 平仓条件 | 冷却期 | 仓位管理 |
|------|------|---------|---------|--------|---------|
| **阈值趋势跟踪** | A | Final_Score > 0.6 | Final_Score < 0.3 | 3 日 | 2% 净值 / 2×ATR |
| **波动率均值回归** | B | Score < -0.5 且低波(后30%) | Score > 0.2 或浮盈 3×ATR | 5 日 | 反波动率加权 [1%-4%] |
| **动量新闻共振** | C | Score > 0.7 + FinBERT > 0.8 + 突破20日高 | Score < 0.4 或移动止盈 | 2 日 | 金字塔加仓 ≤3% |

**通用硬风控**：-8% 硬止损 · 持仓 10 日时间止损 · A 股涨跌停过滤 · 单笔 ≤ 5% 日成交量

---

## 回测绩效指标

| 指标 | 说明 |
|------|------|
| 总收益率 / 年化收益率 | 回测期盈亏及年化复利 |
| 最大回撤 | 峰值到谷值的最大亏损幅度 |
| 夏普比率 | 每单位风险的超额回报 |
| Calmar 比率 | 年化收益 / 最大回撤 |
| 胜率 / 盈亏比 | 盈利交易占比及平均盈亏比例 |
| 平均持仓周期 | 从建仓到平仓的平均天数 |
| Rank IC / IC_IR | 因子与次日收益的秩相关系数及稳定性 |
| 策略相关性 | 三策略日收益率相关系数矩阵 |

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **UI** | Flet (Flutter) | 跨平台桌面 UI |
| **A 股数据** | akshare | 免费开源金融数据 |
| **美股数据** | yfinance | Yahoo Finance |
| **技术指标** | pandas + numpy | 向量化计算 |
| **情感分析** | FinBERT (HuggingFace) | 金融情感三分类 |
| **大模型** | OpenAI 兼容 API | GPT / DeepSeek / 千问 / Ollama |
| **K 线图** | mplfinance | 蜡烛图 + 成交量 + 信号标注 |
| **PDF** | reportlab | 纯 Python PDF |
| **数据库** | SQLite (WAL) | 本地零配置 |

---

## 配置说明

`~/.tradehelper/config.json`：

```json
{
  "work_dir": "~/TradeHelperData",
  "llm_base_url": "https://api.openai.com/v1",
  "llm_api_key": "",
  "llm_model": "gpt-4o",
  "data_source": "free",
  "proxy": ""
}
```

- 大模型 API 可选：不填则使用本地模板生成报告
- `data_source`: `"free"`（akshare + yfinance）/ `"custom"`（自有 API）

---

## 扩展开发

| 扩展目标 | 入口 | 方式 |
|---------|------|------|
| 新增因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` | 添加指标列名 |
| 新增策略 | `strategies/` | 继承 `BaseExecutionStrategy`，注册到 `__init__.py` |
| 替换情感模型 | `indicators/sentiment.py` → `_get_pipeline()` | 修改模型 ID |
| 接入新数据源 | `data/stock_fetcher.py` → `BaseStockFetcher` | 实现接口 |
| 新增配置项 | `config/settings.py` → `DEFAULT_CONFIG` | 添加键 |
| 新 UI 页面 | `ui/` | 继承 `ft.Container`，在 `main.py` 注册 |
| 接入实时数据 | `data_adapters/` | 实现 `DataAdapter` 接口 |
| 调整提示词 | `report/prompts.py` | 修改 `SYSTEM_PROMPT` |

---

## 文档

| 文档 | 说明 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 完整设计文档 |
| [README_BACKTEST.md](./README_BACKTEST.md) | 回测系统详细说明 |

## License

MIT © TradeHelper
