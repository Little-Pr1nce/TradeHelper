# TradeHelper 量化回测系统

基于 `Alpha 多因子打分 → 交易执行策略 → T+1 防偷窥回测` 三层标准量化架构。

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# CLI 模式：运行全部三种策略
python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31

# 仅运行策略 A
python run_backtest.py --code AAPL --start 2024-01-01 --end 2024-12-31 --strategy A

# 自定义资金和因子权重
python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31 \
    --capital 500000 --w-tech 0.7 --w-news 0.3

# 桌面 UI 模式
python main.py
```

---

## 项目结构

```
TradeHelper/
├── main.py                       # 桌面应用入口（Flet）
├── run_backtest.py               # CLI 回测脚本
│
├── core/                         # 公共管道（UI / CLI 共用）
│   ├── pipeline.py               #   分析管道：指标 → 打分 → 回测
│   └── types.py                  #   核心数据类型
│
├── services/                     # 业务编排层
│   └── analysis_service.py       #   完整分析工作流（搜索→取数→计算→图表→报告→持久化）
│
├── alpha/                        # Alpha 多因子打分
│   └── scoring.py                #   纯函数：Z-Score + tanh + FinBERT 加权合成
│
├── strategies/                   # 交易执行策略库
│   ├── base.py                   #   基类 + 数据类 + compute_atr()
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
│   ├── sentiment.py              #   FinBERT 三阶段加载（缓存→下载→降级）
│   └── constants.py              #   共享常量
│
├── data/                         # 数据持久化层
│   ├── models.py                 #   数据模型（StockInfo / PriceData 等）
│   ├── database.py               #   SQLite CRUD（4 张表）
│   ├── stock_fetcher.py          #   股价获取（akshare / yfinance）
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
├── ui/                           # Flet 桌面界面（纯展示）
│   ├── main_page.py              #   主分析页
│   ├── history_page.py           #   历史报告页
│   ├── settings_ui.py            #   设置页
│   └── components.py             #   复用组件
│
├── data_adapters/                # 扩展预留（仅接口定义）
├── tests/                        # 单元测试（42 个）
├── README.md                     # 项目总览
└── README_BACKTEST.md            # 本文件
```

---

## 架构分层

```
┌──────────────────────────────────────────────┐
│  UI 层 (ui/)           CLI 层 (run_backtest) │  ← 纯展示
├──────────────────────────────────────────────┤
│          Services 层 (services/)              │  ← 业务编排
│     AnalysisService: 搜索→取数→管道→报告      │
├──────────────────────────────────────────────┤
│          Core 层 (core/pipeline.py)           │  ← 纯计算管道
│     技术指标 → Alpha 打分 → 三策略回测         │
├──────────────┬───────────────────────────────┤
│  计算引擎     │  支撑层                        │
│  alpha/      │  indicators/  技术指标+情感     │
│  strategies/ │  data/        SQLite + API     │
│  backtest/   │  report/      图表+报告+PDF     │
│              │  config/      全局配置          │
│              │  utils/       市场/日期/字体     │
└──────────────┴───────────────────────────────┘
```

**依赖方向**：UI → Services → Core → 计算引擎，单向、无循环。

---

## 数据流

```
用户输入（代码或名称）
  │
  ├─ 1. 股票搜索 (utils/market.py)
  │     └─ 中文名 → 代码（本地映射 + 在线搜索）
  │
  ├─ 2. 数据获取 (data/stock_fetcher.py)
  │     ├─ A 股: akshare（新浪 → 雪球 → 东财三级降级）
  │     ├─ 美股: yfinance + akshare 兜底
  │     └─ 输出: date/open/high/low/close/volume（6 个标准 OHLCV 字段）
  │
  ├─ 3. 数据缓存 (data/database.py)
  │     └─ SQLite 存储，增量更新，24h 内不重复请求
  │
  ├─ 4. 技术指标 (indicators/technical.py)
  │     └─ MA(5/10/20/60) + MACD + RSI + 布林带 + KDJ（19 个指标列）
  │
  ├─ 5. 新闻情感 (data/news_fetcher.py → indicators/sentiment.py)
  │     └─ FinBERT 三阶段加载 → 逐日情感得分
  │
  ├─ 6. Alpha 打分 (alpha/scoring.py)
  │     ├─ 7 指标滚动 Z-Score（窗口 60）→ tanh 压缩
  │     ├─ FinBERT 得分按日期对齐（缺失 = 0）
  │     └─ Final_Score = 0.6 × Tech + 0.4 × FinBERT，Clip [-1, +1]
  │
  ├─ 7. 策略回测 (strategies/ → backtest/engine.py)
  │     ├─ T 日收盘：读 Final_Score → 策略判断 → 生成 Order
  │     ├─ T+1 开盘：open × (1 + 0.3% 滑点) 撮合
  │     ├─ T+1 盘中：High/Low 检查止损/止盈/时间止损
  │     └─ T+1 收盘：更新权益曲线
  │
  ├─ 8. 绩效分析 (backtest/analytics.py)
  │     ├─ 收益/夏普/回撤/Calmar/胜率/盈亏比
  │     ├─ Rank IC + IC_IR（因子有效性）
  │     └─ 三策略日收益率相关性矩阵
  │
  └─ 9. 报告生成 (report/generator.py)
        ├─ LLM 可用 → OpenAI 兼容 API 生成中文报告
        └─ LLM 不可用 → 本地模板回退
```

---

## Alpha 多因子打分模型

### 公式

$$FinalScore = 0.6 \times \underbrace{\frac{1}{7}\sum_{i=1}^{7}\tanh\big(Z\_score(indicator_i, 60)\big)}_{\text{技术面归一化得分}} + 0.4 \times \underbrace{FinBERT\_Score}_{\text{新闻情感得分}}$$

### 7 个独立指标

| 指标 | 来源 | 维度 |
|------|------|------|
| RSI | 相对强弱指标 | 超买超卖 |
| DIF | MACD 快线 | 趋势方向 |
| MACD 柱 | MACD 柱状图 | 趋势强度 |
| 布林带 %B | 价格在布林带中的位置 | 波动位置 |
| K | KDJ 快线 | 短期动量 |
| D | KDJ 慢线 | 中期动量 |
| J | KDJ 辅助线 | 极端值检测 |

### 处理细节

- **Z-Score 窗口**：60 个交易日（约一个季度），min_periods = 15 保证早期有值
- **tanh 而非 clip**：平滑非线性压缩，保持排序信息，渐进压缩至 ±1
- **新闻缺失**：严格填 0（中性），严禁 ffill/bfill 未来信息泄露
- **权重约束**：w_tech + w_news = 1.0，代码中强制校验

---

## 三种交易策略

### 策略对照表

| 属性 | 策略 A（阈值趋势） | 策略 B（均值回归） | 策略 C（动量共振） |
|------|-------------------|-------------------|-------------------|
| **核心思想** | 滞后带过滤噪音 | 低波环境中抄底 | 三重确认降假突破 |
| **开仓条件** | Score > 0.6 | Score < -0.5 且低波 | Score > 0.7 + FinBERT > 0.8 + 突破前高 |
| **平仓条件** | Score < 0.3 | Score > 0.2 或浮盈 3×ATR | Score < 0.4 或移动止盈 |
| **冷却期** | 3 根 K 线 | 5 根 K 线 | 2 根 K 线 |
| **仓位模型** | 固定风险 2% | 反波动率加权 [1%-4%] | 金字塔加仓 ≤3% |
| **止损距离** | 2 × ATR(14) | 2 × ATR(14) | 2 × ATR(14)（移动） |
| **适用场景** | 趋势市基准 | 震荡市反弹 | 右侧高胜率 |

### 通用硬风控（三策略共享，Broker 层执行）

| 风控类型 | 规则 | 触发时机 |
|---------|------|----------|
| **硬止损** | 浮亏 -8%，无条件平仓 | T+1 盘中 Low 触及 |
| **时间止损** | 持仓 10 个交易日，强制平仓 | T+1 收盘时检查 |
| **涨跌停过滤** | A 股涨停不买入，跌停不卖出 | T+1 开盘前检查 |
| **流动性约束** | 单笔 ≤ 5% 日成交量，超标加 0.5% 滑点 | T+1 撮合时检查 |
| **停牌处理** | 成交量为 0 视为停牌，跳过 | T+1 开盘前检查 |

---

## 回测引擎（防偷窥铁律）

### 撮合时序

```
T 日收盘    →  读 Final_Score  →  策略 generate_orders()
                                  ↓
T+1 开盘    →  open × (1+0.3%)  →  撮合成交
                                  ↓
T+1 盘中    →  High/Low 检查    →  硬止损 / 移动止盈 / 时间止损
                                  ↓
T+1 收盘    →  更新净值         →  记录权益曲线
```

### 防偷窥约束

| 规则 | 实现方式 |
|------|---------|
| 禁止使用 T 日收盘价成交 | 强制使用 T+1 日 open，代码中 `i` 和 `i+1` 严格分离 |
| 禁止因子动态计算 | 所有指标在回测循环前通过 `run_pipeline()` 预计算完毕 |
| 禁止未来信息泄露 | 新闻缺失填 0 不填充，冷却期基于平仓日索引 |
| 禁止 Alpha 模型存储状态 | `calc_final_score()` 是纯函数，无全局变量 |

### 回测循环伪代码

```python
for i in range(n - 1):              # 遍历每根 K 线
    t_bar = df.iloc[i]              # T 日数据
    t1_bar = df.iloc[i + 1]         # T+1 日数据

    # T 日收盘：策略判断
    orders = strategy.generate_orders(df[:i+1], context)

    # T+1 日开盘：撮合订单
    for order in orders:
        broker.execute_order(order, t1_bar, account)

    # T+1 日盘中：风控检查
    broker.check_intraday_stops(t1_bar, account)

    # T+1 日收盘：更新状态
    broker.update_daily(t1_bar, account)
```

---

## FinBERT 新闻情感分析

### 三阶段加载策略

```
_get_pipeline()
  │
  ├─ 阶段 1: local_files_only=True    ← 本地缓存命中，秒级加载
  │    └─ 失败 → 进入阶段 2
  │
  ├─ 阶段 2: local_files_only=False   ← 联网下载 ~300MB
  │    └─ 失败 → 进入降级方案
  │
  └─ 降级方案: _SimpleFallbackAnalyzer ← 中英文关键词匹配，始终可用
```

### 得分规则

- 当日有新闻 → FinBERT 三分类（positive/negative/neutral）→ 映射为 +1/-1/0
- 当日无新闻 → 得分 = 0（中性，表示「无信息即无偏向」）
- **严禁向前填充或向后插值**

---

## 使用方法

### Python API

```python
from core.pipeline import run_pipeline
from strategies import get_execution_strategy
from backtest.engine import BacktestEngine, BacktestConfig

# 方式 1：使用管道（推荐，自动完成全流程）
result = run_pipeline(df, news_df, market="A")
print(f"策略 A 收益: {result.backtest['策略A'].total_return:.2%}")

# 方式 2：手动控制每一步
df = calc_all_indicators(df)
df = calc_final_score(df, news_df)
strategy = get_execution_strategy("A", entry_threshold=0.7)
engine = BacktestEngine(BacktestConfig(initial_capital=100000))
result = engine.run(df, strategy)

# 方式 3：Service 层（含缓存和持久化）
from services.analysis_service import AnalysisService, AnalysisRequest
service = AnalysisService()
response = service.analyze(
    AnalysisRequest(raw_input="茅台", market="A", period="3m"),
    on_progress=lambda msg: print(msg),
)
```

### 切换策略

```python
from strategies import get_execution_strategy

# 按代号切换
strategy = get_execution_strategy("A")   # 阈值趋势
strategy = get_execution_strategy("B")   # 均值回归
strategy = get_execution_strategy("C")   # 动量共振

# 自定义参数
strategy = get_execution_strategy("A",
    entry_threshold=0.7,     # 提高开仓阈值
    cooldown_bars=5,         # 延长冷却期
    risk_budget=0.01,        # 降低风险预算
)
```

### 替换新闻数据源

```python
from alpha.scoring import calc_final_score

# 自定义新闻 DataFrame（只需 date + finbert_score 两列）
my_news = pd.DataFrame({
    "date": ["2024-01-02", "2024-01-05"],
    "finbert_score": [0.8, -0.3],
})
df = calc_final_score(price_df, my_news)
```

### 添加自定义策略

```python
from strategies.base import BaseExecutionStrategy, Order, StrategyContext, compute_atr

class MyStrategy(BaseExecutionStrategy):
    @property
    def name(self) -> str:
        return "我的自定义策略"

    @property
    def description(self) -> str:
        return "基于 XX 条件的交易策略"

    def generate_orders(self, df, context) -> list[Order]:
        # 实现你的交易逻辑
        ...

# 注册到策略表
from strategies import _STRATEGY_REGISTRY
_STRATEGY_REGISTRY["my_strategy"] = MyStrategy
```

---

## 绩效指标说明

| 指标 | 公式 / 说明 |
|------|------------|
| **总收益率** | (最终权益 - 初始资金) / 初始资金 |
| **年化收益率** | (1 + 总收益)^(252/交易日数) - 1 |
| **最大回撤** | max{(峰值 - 谷值) / 峰值} |
| **夏普比率** | (日均超额收益 / 日波动) × √252 |
| **Calmar 比率** | 年化收益 / 最大回撤 |
| **胜率** | 盈利交易次数 / 总交易次数 |
| **盈亏比** | 平均盈利金额 / 平均亏损金额 |
| **平均持仓** | Σ(平仓日 - 开仓日) / 交易次数 |
| **Rank IC** | Spearman 相关系数(Final_Score, T+1 收益率) 的滚动均值 |
| **IC_IR** | Rank IC 均值 / Rank IC 标准差（> 0.5 表示因子有效） |

---

## API 数据源

### OHLCV 字段覆盖

| 字段 | A 股（akshare） | 美股（yfinance） | 用途 |
|------|:--:|:--:|------|
| `date` | ✓ | ✓ | 时间轴对齐 |
| `open` | ✓ | ✓ | T+1 撮合价 |
| `high` | ✓ | ✓ | KDJ / ATR / 止损检查 |
| `low` | ✓ | ✓ | KDJ / ATR / 止损检查 |
| `close` | ✓ | ✓ | MA/MACD/RSI/布林/ATR/权益 |
| `volume` | ✓ | ✓ | 流动性约束（≤ 5%） |

**结论**：6 个标准 OHLCV 字段完全满足所有技术指标和回测引擎的计算需求。

### 数据源降级链

```
A 股:  akshare(新浪) → akshare(东财) → 失败
美股:  yfinance → akshare(雪球) → 失败
```

---

## 扩展点

| 扩展目标 | 入口文件 | 方式 |
|---------|---------|------|
| 新增技术因子 | `alpha/scoring.py` → `INDICATOR_COLUMNS` | 添加指标列名 |
| 新增策略 | `strategies/__init__.py` → `_STRATEGY_REGISTRY` | 继承 `BaseExecutionStrategy` |
| 调整因子权重 | `alpha/scoring.py` → `DEFAULT_W_TECH` / `DEFAULT_W_NEWS` | 修改权重（约束 = 1.0） |
| 替换情感模型 | `indicators/sentiment.py` → `_FINBERT_MODEL` | 修改模型 ID |
| 调整提示词 | `report/prompts.py` → `SYSTEM_PROMPT` | 修改提示词模板 |
| 接入新数据源 | `data/stock_fetcher.py` → `BaseStockFetcher` | 实现新 Fetcher 类 |
| 新增 UI 页面 | `ui/` | 继承 `ft.Container`，在 `main.py` 注册 |
| 实时数据适配 | `data_adapters/__init__.py` | 实现 `DataAdapter` 接口 |
| 组合级风控 | `data_adapters/__init__.py` | 实现 `GlobalRiskManager` 接口 |

---

## 运行测试

```bash
python -m pytest tests/ -v

# 或直接运行
python -c "
from tests.test_scoring import *
from tests.test_strategies import *
from tests.test_backtest import *
# ... 运行 42 个测试
"
```

测试覆盖：
- `test_scoring.py` (14 个)：纯函数特性、输出范围、权重约束、新闻对齐
- `test_strategies.py` (16 个)：开平仓条件、冷却期、仓位计算、三重确认
- `test_backtest.py` (12 个)：T+1 时序、滑点计算、涨跌停过滤、端到端

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

- `llm_api_key` 留空时使用本地模板回退，不影响核心分析功能
- `data_source`: `"free"`（akshare + yfinance）/ `"custom"`（自有 API）
- `proxy`: 代理地址，用于访问海外数据源（yfinance 等）
