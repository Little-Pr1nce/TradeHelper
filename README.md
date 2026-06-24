<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

<h1 align="center">TradeHelper</h1>
<p align="center"><strong>AI 驱动的量化股票分析助手 · 15 策略回测 · 策略审计 · 操作方案代码生成</strong></p>
<p align="center">A 股 & 美股 · Alpha 多因子 · 三时段分析 · 保守/激进双方案 · LLM 智能解读</p>

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **多市场** | A 股 + 美股自动识别，支持代码/名称搜索 |
| **三时段分析** | 盘后（完整报告）、盘中（实时快照+盘口）、盘前（期货情绪+情景推演） |
| **三时段联动** | 盘前预测 → 盘中验证 → 盘后总结，形成完整闭环 |
| **Alpha 多因子** | 7 技术指标 + 基本面 + 风格 + 新闻情感 + 盘口因子，IC/IR 检验 |
| **15 策略回测** | 9 量化(A-H,O) + 6 人类(I-N)，涵盖趋势/反转/突破/扛单等模式 |
| **策略审计引擎** | 时间切分验证（训练 70% / 测试 30%），PASS/COND/FAIL 三级判定 |
| **策略池扩展** | 2 可调参数 × 多值组合 → 61 变体，回测缓存加速 7x |
| **信号检查** | 每个策略实时检查入场条件，多维排序选出 Top 3 |
| **操作方案生成** | 保守/激进双方案，含精确价位、止损、仓位、触发条件诊断 |
| **行情自适应** | 自动检测市场状态（强趋势/慢涨/震荡/过渡），只运行适配策略 |
| **FinBERT 情感** | 金融文本情感分析 + 时间衰减加权（半衰期 1 天）+ 中文关键词兜底 |
| **AI 报告** | LLM 翻译量化方案为 K 线图语言，9 维度交叉验证，含情景推演与风险提示 |
| **策略健康度追踪** | prediction_log 驱动，持续追踪每策略准确率，自动 keep/watch/demote |
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

非必填项按需填写（美股/A 股数据源 Token、新闻 Token 等）。完成后点保存，分析功能解锁。

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
| 📊 **盘后分析** | K 线 + 新闻 + 基本面 | 完整报告（策略审计+操作方案+多因子+LLM 解读） |
| ⏱ **盘中分析** | 实时报价 + 盘口深度 | 实时价格 vs 均线位置、VWAP、日内动量、盘口买卖比 |
| 🌅 **盘前分析** | 期货 NQ/ES + 盘前价格 | 期货情绪得分、个股 vs 期货相对强弱、三种开盘情景推演 |

---

## 15 个交易策略

### 量化策略（A-H, O）

| 代号 | 名称 | 适配行情 | 开仓逻辑 |
|:--:|------|---------|---------|
| A | 百分位趋势跟踪 | 强趋势+慢涨 | Score ≥ 80%分位 |
| B | 波动率均值回归 | 震荡/过渡 | Score ≤ 20%分位 + 低波确认 |
| C | 动量+新闻共振 | 全行情通用 | Score ≥ 80%分位 + FinBERT > 0.3 + 突破5日高 |
| D | 布林带突破 | 震荡 | 收盘 > 布林上轨 + Score ≥ 70%分位 |
| E | Dual Thrust | 强趋势高波 | 收盘 > 上轨 + Score ≥ 70%分位 |
| F | 海龟 ATR 通道 | 强趋势高波 | 收盘 > 20日高 + Score ≥ 75%分位 |
| G | MA 交叉确认 | 慢涨 | MA5 > MA20 + Score > 50%分位 + 价 > MA60 |
| H | MA60 中长期趋势 | 强趋势+慢涨 | 价 > MA60 + MA60 向上 + Score ≥ 65%分位 |
| O | 趋势满仓持有 | 全行情 | 有仓位就持有（对标买入持有基准） |

### 人类策略（I-N）

| 代号 | 名称 | 类型 | 开仓逻辑 |
|:--:|------|------|---------|
| I | 追涨杀跌 | 新手 | MA5 金叉 MA20 买入 80%仓位，死叉卖出 |
| J | 抄底摸底 | 新手 | 收盘 < 布林下轨 + RSI < 30，抄底买入 |
| K | 死扛回本 | 新手 | 前日大跌后买入，不设止损，回本才卖 |
| L | 趋势回调 | 老手 | 上升趋势中回调至 MA20 买入 |
| M | 关键反转 | 老手 | 锤子线/吞没形态等 K 线反转信号 |
| N | 均线压缩突破 | 老手 | MA 多线粘合后放量突破 |

所有量化策略均使用 **Score 百分位**（滚动 252 日）替代固定阈值，无论牛熊市都能正常触发。

---

## 市场状态自适应

程序自动检测当前行情类型，跳过不适用的策略：

| 行情 | 检测依据 | 激活策略 |
|------|---------|---------|
| 强趋势+高波 | ADX > 25, ATR > 5% | A, E, F, H, O |
| 慢涨/弱趋势 | ADX > 25, ATR ≤ 5% | A, G, H, O |
| 震荡 | ADX < 20 | B, D |
| 趋势形成中 | 20 ≤ ADX ≤ 25 | B |
| 事件驱动 | — | C（全行情通用） |

人类策略（I-N）不做行情筛选，始终活跃。

---

## 8 层系统架构

```
① 数据层: K线(TickFlow) + 基本面(Finnhub/baostock) + 新闻 + Nasdaq延伸时段
② 因子层: 技术面(7指标) + 基本面 + 新闻(FinBERT) → Alpha Score + Rank IC
③ 回测层: 策略池回测 → 时间切分审计 → PASS/COND/FAIL 判定
④ 信号检查层: generate_orders() → 每个策略入场条件是否满足?
⑤ 策略排序层: 多维评分(审计40%+夏普30%+信号20%+置信度10%)
⑥ 操作方案层: Top 3 策略 → 保守/激进双方案(价位/止损/仓位)
⑦ LLM 解读层: 翻译系统方案为 K 线图语言 + 风险提示 + Plan B
⑧ 追踪反馈层: prediction_log → health_report → demote 惩罚 → 自适应
```

| 层级 | 职责 | 实现 |
|:--:|------|------|
| ①② | 数据获取与因子计算 | `services/analysis_service.py` → `core/pipeline.py` |
| ③ | 时间切分验证 | `core/strategy_audit.py`（PASS/COND/FAIL） |
| ③ | 策略池扩展+缓存 | `core/strategy_pool.py`（61 变体，SQLite 缓存 7x 加速） |
| ④⑤⑥ | 信号检查+排序+方案 | `core/signal_check.py`（保守/激进双方案） |
| ⑦ | LLM 翻译解读 | `report/generator.py` + `report/prompts.py` |
| ⑧ | 预测追踪+健康度 | `data/database.py`（prediction_log + health_report） |

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

## 代码架构

```
┌──────────────────────────────────────────────────────────┐
│  UI 层 (ui/main_page.py)      CLI 层 (run_backtest.py)   │
├──────────────────────────────────────────────────────────┤
│            Services 层 (analysis_service.py)              │
│        搜索 → 取数(含缓存) → 管道 → 报告 → 持久化         │
├──────────────────────────────────────────────────────────┤
│            Core 层                                        │
│    pipeline.py       — 技术指标 → Alpha 打分 → 回测       │
│    strategy_audit.py — 时间切分验证 PASS/COND/FAIL         │
│    strategy_pool.py  — 策略池扩展 + 缓存 + 自适应参数       │
│    signal_check.py   — 信号检查 + 排序 + 操作方案生成       │
├────────────────────┬─────────────────────────────────────┤
│ 计算引擎            │ 支撑层                               │
│ alpha/             │ data/     多源数据 + SQLite(10表)    │
│ strategies/ (15)   │ config/   全局配置单例                │
│ backtest/          │ report/   图表+LLM报告+PDF/HTML       │
│ indicators/        │ utils/    市场/日期/字体/日志          │
└────────────────────┴─────────────────────────────────────┘
```

---

## 报告结构

```
📊 一分钟速览（执行摘要）                            ← 代码生成
[LLM 第 1-7 章]                                      ← LLM 生成
📋 策略池审计（时间切分验证）                           ← 代码注入
🎯 系统操作方案（Top 3 + 关键价位 + 保守/激进双方案）     ← 代码生成
[LLM 第 8 章 — 翻译系统方案为人话]                     ← LLM 解读
🩺 策略健康度追踪（持续优化闭环）                       ← 代码追加
📈 系统追踪（预测验证记录）                             ← 代码追加
```

### LLM 角色边界

| ✅ LLM 做 | ❌ LLM 不做 |
|----------|-----------|
| 翻译系统方案的量化术语为 K 线图语言 | 编造操作方案中的价位 |
| 解释为什么选这些策略、两方案差异、风险 | 自己决定买卖时机 |
| 判断能否等到/做不到，给出利弊权衡 | 创造新的策略规则 |
| 建议新策略框架（需标注回测验证） | 代替回测引擎判断策略好坏 |
| 无信号时指出最接近触发策略 + 轻仓试探可能 | 脱离数据自编方案 |

---

## 数据源

| 市场 | K线+实时+盘口 | 新闻 | 基本面 | 延伸时段 |
|------|-------------|------|--------|---------|
| 美股 | TickFlow | Finnhub / yfinance | Finnhub → yfinance | Nasdaq.com → yfinance |
| A 股 | TickFlow | akshare 免费 | akshare / baostock | — |

配置文件存储于系统标准应用目录（macOS `~/Library/Application Support/`，Windows `%APPDATA%`，Linux `~/.config/`）下的 `TradeHelper/config.json`。

---

## 数据库（10 张表）

| 表 | 说明 |
|------|------|
| `stocks` | 股票基本信息缓存 |
| `price_history` | 日K线 OHLCV |
| `reports` | 分析报告记录 |
| `news_sentiment` | 新闻情感分析 |
| `holdings` | 用户持仓 |
| `watchlist` | 关注列表 |
| `account_balance` | 账户余额 |
| `prediction_log` | 预测追踪（含 strategy_name 列） |
| `bt_variant_cache` | 策略变体回测缓存（30 天清理） |
| `per_stock_params` | 每股票每策略最佳自适应参数 |

---

## 扩展开发

| 扩展目标 | 入口 | 方式 |
|---------|------|------|
| 新增策略 | `strategies/` | 继承 `BaseExecutionStrategy`，设置 `suitable_regimes`，注册到 `__init__.py` |
| 新增因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` | 添加指标列名 |
| 新增数据源 | `data/stock_fetcher.py` → `BaseStockFetcher` | 实现接口，`get_stock_fetcher()` 注册 |
| 新增配置项 | `config/settings.py` → `DEFAULT_CONFIG` | 同步更新 `settings_ui.py` |
| 替换情感模型 | `indicators/sentiment.py` → `_FINBERT_MODEL` | 修改模型 ID |
| 新增审计规则 | `core/strategy_audit.py` → `_evaluate_entry()` | 修改 PASS/COND/FAIL 判定条件 |

---

## 文档

| 文档 | 说明 |
|------|------|
| [DESIGN.md](./DESIGN.md) | 完整设计文档，含优化历史和后续计划 |
| [CLAUDE.md](./CLAUDE.md) | AI 辅助开发指南 |

## 待办


- **Web 版本**: flet run --web 模式完善
- **打包体积优化**: 当前 ~2GB（含 PyTorch），可探索 ONNX 量化

## License

MIT © TradeHelper
