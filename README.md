<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

<h1 align="center">TradeHelper</h1>
<p align="center"><strong>AI 驱动的量化股票分析助手</strong></p>
<p align="center">A 股 & 美股 · Alpha 多因子 · 7 策略回测 · 三时段分析 · AI 报告</p>

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **多市场** | A 股 + 美股自动识别，支持代码/名称搜索 |
| **三时段分析** | 盘后（完整 8 章报告）、盘中（实时快照+盘口）、盘前（期货情绪+情景推演） |
| **三时段联动** | 盘前预测 → 盘中验证 → 盘后总结，形成完整闭环 |
| **Alpha 多因子** | 7 技术指标 + 基本面 + 风格 + 新闻情感 + 盘口因子，IC/IR 检验 |
| **7 策略回测** | 趋势跟踪/均值回归/动量新闻/布林突破/DualThrust/海龟ATR/MA交叉 |
| **行情自适应** | 自动检测市场状态（强趋势/慢涨/震荡/过渡），只运行适配策略 |
| **FinBERT 情感** | 金融文本情感分析 + 时间衰减加权 + 中文关键词兜底 |
| **AI 报告** | LLM 交叉验证 9 个维度信号，情景推演，操作建议，精确价位 |
| **跨平台打包** | PyInstaller 打包 .app / .exe，内置 FinBERT 模型，开箱即用 |
| **首次配置引导** | 4 项必填配置，未完成时强制引导，按需校验数据源 |

---

## 快速开始

### 环境要求

- Python >= 3.12

### 安装

```bash
git clone <repo-url> && cd TradeHelper
python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 首次配置

启动后自动跳转到**设置**页，红色 `*` 标注的 4 项必填：

| 必填项 | 说明 | 示例 |
|--------|------|------|
| 工作目录 | 数据/报告/日志存放处 | `~/TradeHelper` |
| LLM Base URL | OpenAI 兼容 API 地址 | `https://api.deepseek.com` |
| LLM API Key | API 密钥 | `sk-...` |
| 模型名称 | 使用的模型 | `deepseek-chat` |

非必填项按需填写（美股/ A 股数据源 Token、新闻 Token 等）。完成后点保存，分析功能解锁。

### 打包（可选）

```bash
python scripts/prepare_model.py    # 预下载 FinBERT 模型（~300MB）
bash scripts/build_macos.sh        # macOS → dist/mac/TradeHelper.app
scripts\build_windows.bat          # Windows → dist/win/TradeHelper/
```

---

## 三种分析模式

| 模式 | 数据需求 | 核心能力 |
|------|---------|---------|
| 📊 **盘后分析** | K 线 + 新闻 + 基本面 | 完整 8 章报告，6 因子打分，7 策略回测 |
| ⏱ **盘中分析** | 实时报价 + 盘口深度 | 实时价格 vs 均线位置、VWAP、日内动量、盘口买卖比 |
| 🌅 **盘前分析** | 期货 NQ/ES + 盘前价格 | 期货情绪得分、个股 vs 期货相对强弱、三种开盘情景推演 |

---

## 7 个交易策略

| 代号 | 名称 | 适配行情 | 开仓逻辑 |
|:--:|------|---------|---------|
| A | 百分位趋势跟踪 | 强趋势+慢涨 | Score ≥ 80%分位 |
| B | 波动率均值回归 | 震荡/过渡 | Score ≤ 20%分位 + 低波确认 |
| C | 动量+新闻共振 | 全行情通用 | Score ≥ 80%分位 + FinBERT > 0.3 + 突破5日高 |
| D | 布林带突破 | 震荡 | 收盘 > 布林上轨 + Score ≥ 70%分位 |
| E | Dual Thrust | 强趋势高波 | 收盘 > 上轨 + Score ≥ 70%分位 |
| F | 海龟 ATR 通道 | 强趋势高波 | 收盘 > 20日高 + Score ≥ 75%分位 |
| G | MA 交叉确认 | 慢涨 | MA5 > MA20 + Score > 50%分位 + 价 > MA60 |

所有策略均使用 **Score 百分位**（滚动 252 日）替代固定阈值，无论牛熊市都能正常触发。

---

## 市场状态自适应

程序自动检测当前行情类型，跳过不适用的策略：

| 行情 | 检测依据 | 激活策略 |
|------|---------|---------|
| 强趋势+高波 | ADX > 25, ATR > 5% | A, E, F |
| 慢涨/弱趋势 | ADX > 25, ATR ≤ 5% | A, G |
| 震荡 | ADX < 20 | B, D |
| 趋势形成中 | 20 ≤ ADX ≤ 25 | B |
| 事件驱动 | — | C（全行情通用） |

---

## Alpha 多因子模型

| 因子 | 权重（有基本面） | 权重（无基本面） | 来源 |
|------|:--:|:--:|------|
| 技术面（7 指标） | 30% | 55% | K 线计算 |
| 风格（PE/PB） | 15% | — | akshare / Finnhub |
| 基本面（ROE/毛利率等） | 20% | — | akshare / Finnhub |
| 新闻情感（FinBERT） | 25% | 35% | FinBERT 模型 |
| 盘口因子 | 10% | 10% | TickFlow Level-1（盘口暂缺时自动回退） |

无盘口数据时权重自动回退。因子经 IC/IR 检验：D 级剔除、C 级半权。新闻情感带时间衰减（半衰期 1 天）。

---

## 架构

```
┌──────────────────────────────────────────────────────┐
│  UI 层 (ui/main_page.py)    CLI 层 (run_backtest.py) │
├──────────────────────────────────────────────────────┤
│          Services 层 (analysis_service.py)            │
│      搜索 → 取数(含缓存) → 管道 → 报告 → 持久化       │
├──────────────────────────────────────────────────────┤
│          Core 层 (pipeline.py)                        │
│     技术指标 → Alpha 打分 → 行情检测 → 策略过滤 → 回测 │
├──────────────────┬───────────────────────────────────┤
│ 计算引擎          │ 支撑层                             │
│ alpha/           │ data/    多源数据 + SQLite         │
│ strategies/ (7)  │ config/  全局配置单例               │
│ backtest/        │ report/  图表 + LLM报告 + PDF/HTML │
│ indicators/      │ utils/   市场/日期/字体/日志         │
└──────────────────┴───────────────────────────────────┘
```

---

## 数据源

| 市场 | K 线+实时+盘口 | 新闻 | 基本面 |
|------|-------------|------|--------|
| 美股 | stock_token_us（TickFlow） | news_token_us（Finnhub） | Finnhub |
| A 股 | stock_token_a（TickFlow） | akshare 免费 | akshare |

配置文件存储于系统标准应用目录（macOS `~/Library/Application Support/`，Windows `%APPDATA%`，Linux `~/.config/`）下的 `TradeHelper/config.json`。

---

## 扩展开发

| 扩展目标 | 入口 | 方式 |
|---------|------|------|
| 新增策略 | `strategies/` | 继承 `BaseExecutionStrategy`，设置 `suitable_regimes`，注册到 `__init__.py` |
| 新增因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` | 添加指标列名 |
| 新增数据源 | `data/stock_fetcher.py` → `BaseStockFetcher` | 实现接口，`get_stock_fetcher()` 注册 |
| 新增配置项 | `config/settings.py` → `DEFAULT_CONFIG` | 同步更新 `settings_ui.py` |
| 替换情感模型 | `indicators/sentiment.py` → `_FINBERT_MODEL` | 修改模型 ID |

---

## 文档

| 文档 | 说明 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 完整设计文档，含优化历史和后续计划 |
| [CLAUDE.md](./CLAUDE.md) | AI 辅助开发指南 |

## License

MIT © TradeHelper
