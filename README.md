<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/flet-0.85-blue.svg" alt="Flet">
  <img src="https://img.shields.io/badge/Frontend-Flet%20(Flutter)-02569B?logo=flutter" alt="Frontend">
</p>

<h1 align="center">TradeHelper</h1>
<p align="center"><strong>轻量级跨平台股票分析助手</strong></p>
<p align="center">支持 A 股 & 美股 · 技术面分析 · 新闻情感分析 · 策略回测 · PDF 报告导出</p>

---

## 截图预览

<p align="center">
  <em>主分析页面——输入代码、选择周期和策略，一键生成完整分析报告</em><br>
  <em>（启动 <code>python main.py</code> 即可体验）</em>
</p>

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **多市场支持** | A 股（6 位数字代码） + 美股（字母代码），自动识别 |
| **技术面分析** | MA / MACD / RSI / 布林带 / KDJ，生成结构化摘要 |
| **新闻情感分析** | 基于 FinBERT 深度学习模型，精准识别金融新闻情绪 |
| **策略回测** | 内置 6 种策略，事件驱动模拟交易，输出收益率/夏普/回撤等指标 |
| **AI 报告生成** | 接入 OpenAI 兼容大模型，基于真实数据生成专业分析报告 |
| **PDF 导出** | 一键导出含 K 线图的中文 PDF 报告 |
| **报告评分** | 1-5 星评分系统，数据积累用于后续策略优化 |
| **本地存储** | SQLite 缓存股价数据和历史报告，离线可查 |
| **热插拔数据源** | 策略模式切换免费/自定义付费 API |

---

## 快速开始

### 环境要求

- Python >= 3.12
- pip >= 23.0
- 网络连接（首次运行时需从 HuggingFace 下载 FinBERT 模型 ~300MB）

### 安装

```bash
git clone <your-repo-url>
cd TradeHelper

python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt   # --pre
python main.py
```

### 首次使用

1. 启动后进入 **设置** 页，配置工作目录（数据存储路径）
2. （可选）配置大模型 API Key，获得 AI 增强报告
3. 返回 **分析** 页，输入股票代码（如 `600519` / `AAPL`）
4. 选择回测周期和交易策略，点击「开始分析」
5. 查看 K 线图 + 分析报告 → 导出 PDF → 评分

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **UI** | [Flet](https://flet.dev) (Flutter) | 跨平台桌面 UI，原生渲染 |
| **A 股数据** | [akshare](https://github.com/akfamily/akshare) | 免费开源金融数据接口 |
| **美股数据** | [yfinance](https://github.com/ranaroussi/yfinance) | Yahoo Finance 数据 |
| **技术指标** | pandas + numpy + ta | 向量化指标计算 |
| **情感分析** | [HuggingFace Transformers](https://huggingface.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis) | FinBERT 金融情感模型 |
| **大模型** | OpenAI 兼容 API | 灵活接入 GPT / DeepSeek / 千问等 |
| **K 线图** | [mplfinance](https://github.com/matplotlib/mplfinance) | 专业 K 线图+成交量+信号标注 |
| **PDF** | [reportlab](https://www.reportlab.com) | 纯 Python PDF 生成 |
| **数据库** | SQLite (WAL 模式) | 本地存储，零配置 |

---

## 项目结构

```
TradeHelper/
├── main.py                     # 应用入口
├── requirements.txt            # 依赖清单
├── DESIGN.md                   # 详细设计文档 + 扩展指南
├── config/
│   ├── settings.py             # 全局配置（单例 + JSON 持久化）
│   └── settings_ui.py          # 设置页面
├── data/
│   ├── models.py               # 数据模型（StockInfo / PriceData / Report / NewsItem）
│   ├── database.py             # SQLite CRUD（4 张表）
│   ├── stock_fetcher.py        # 数据获取（策略模式：免费 / 自定义 API）
│   └── news_fetcher.py         # 新闻获取（A 股东方财富 / 美股 Yahoo）
├── analysis/
│   ├── technical.py            # 技术指标计算（MA / MACD / RSI / 布林带 / KDJ）
│   ├── sentiment.py            # 新闻情感分析（FinBERT + 关键词降级方案）
│   ├── strategy.py             # 交易策略（6 种内置策略，可扩展）
│   └── backtest.py             # 回测引擎（模拟交易 + 绩效指标 + 操作建议）
├── report/
│   ├── chart.py                # K 线图生成（mplfinance）
│   ├── generator.py            # 报告生成（LLM 增强 / 模板回退）
│   └── pdf_exporter.py         # PDF 导出（reportlab + 中文字体）
├── ui/
│   ├── components.py           # 复用组件（StarRating / ProgressOverlay）
│   ├── main_page.py            # 主分析页
│   ├── history_page.py         # 历史报告页
│   └── settings_ui.py          # UI 设置页
└── utils/
    └── helpers.py              # 工具函数（代码校验 / 字体查找 / 日志）
```

---

## 内置交易策略

| 策略 | 短名 | 信号逻辑 | 适用场景 |
|------|------|---------|---------|
| 双均线交叉 | `ma_crossover` | MA5 金叉买入 / 死叉卖出 MA20 | 单边趋势市 |
| MACD | `macd` | DIF 上穿买入 / 下穿卖出 DEA | 趋势市 |
| RSI 超买超卖 | `rsi` | RSI < 30 买入 / > 70 卖出 | 震荡市波段 |
| 布林带回归 | `bollinger` | 跌破下轨买入 / 突破上轨卖出 | 均值回归 |
| 买入持有 | `buy_and_hold` | 期初买期末卖，中间不动 | 基准对照 |
| 三均线排列 | `triple_ma` | 多头排列买入 / 空头排列卖出 | 强趋势确认 |

---

## 回测绩效指标

| 指标 | 说明 |
|------|------|
| 总收益率 | 整个回测期的盈亏比例 |
| 年化收益率 | 折算为年度复利收益率 |
| 最大回撤 | 最坏情况下的峰值到谷值亏损 |
| 夏普比率 | 每单位风险换来的超额回报 |
| 胜率 | 买入卖出配对后盈利的比例 |

---

## 配置说明

配置文件位于 `~/.tradehelper/config.json`：

```json
{
  "work_dir": "~/TradeHelperData",
  "llm_base_url": "https://api.openai.com/v1",
  "llm_api_key": "",
  "llm_model": "gpt-4o",
  "data_source": "free",
  "custom_api_endpoint": "",
  "custom_api_key": ""
}
```

- 大模型 API 为**可选**配置：不填写时系统使用本地模板生成报告
- 数据源默认 `free`：A 股用 akshare，美股用 yfinance；选择 `custom` 可接入自有付费 API

---

## 扩展开发

项目为每个模块预留了明确的扩展点（搜索 `【扩展点】` 即可定位），包括：

- 新增交易策略（继承 `BaseStrategy` 基类）
- 接入自定义数据源（实现 `BaseStockFetcher` 接口）
- 替换情感分析模型（修改 `_get_pipeline()` 中的模型 ID）
- 增强回测引擎（滑点、仓位管理、多股票组合）
- 自定义 PDF 样式（字体、配色、页面尺寸）
- 新增 UI 页面（继承 `ft.Container`，注册导航）

详见 **[DESIGN.md 第十一章 · 扩展指南](./DESIGN.md#十一扩展指南)**。

---

## 文档

| 文档 | 说明 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 完整设计文档：架构、模块、数据流、DB 设计、扩展指南、快速开始与测试 |
| 代码注释 | 所有模块、类、方法均有中文文档注释，IDE 直接可读 |

---

## License

MIT © TradeHelper
