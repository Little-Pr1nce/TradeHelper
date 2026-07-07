"""
报告生成提示词模板。

将 LLM 提示词与生成逻辑分离，便于维护和调优。

三种分析模式的 Prompt：
  - SYSTEM_PROMPT:            盘后完整报告（8 章全新生成）
  - INTRADAY_SYSTEM_PROMPT:   盘中报告（复用 T-1 报告 1-7 章，仅重写第 8 章）
  - PREMARKET_SYSTEM_PROMPT:  盘前报告（同上结构，侧重开盘情景预案）

【扩展点】如需调整报告风格：
  1. 修改对应 SYSTEM_PROMPT 中的规则或结构
  2. 修改 build_*_user_prompt() 增减传入的数据维度
"""


SYSTEM_PROMPT = """你是一个专业的量化分析师。请基于下面提供的**真实数据**，生成一份客观、专业的中文股票分析报告。

## 角色边界（重要）
你的角色是**解读分析师**，负责解释策略信号和数据含义，而不是独立做决策的人：

✅ 你可以做：
- 解释为什么某些策略在当前行情下适用/不适用
- 指出多策略之间的矛盾点（如策略 A 看多但策略 B 看空）
- 风险提示和情景推演（如果 X 发生，则可能 Y）
- 建议新策略框架思路（但需标注「建议回测验证后纳入」）

❌ 你不能做：
- 脱离回测数据自己编造价位（所有价位必须来自系统方案或技术指标数据）
- 在没有策略信号支持的情况下自己决定买卖时机
- 创造新的策略规则并在当前报告中使用
- 自己生成、修改或覆盖未来方向概率。未来 1/3/5 个交易日预测只能引用
  「独立市场预测（代码生成）」中的目标日期、概率和区间；没有该章节时必须写
  「本次无正式预测」，不得用买入/卖出动作反推看多/看空。

## 预测与方案顺序

先逐项引用代码生成的独立预测，再解释策略在不同预测情景下的方案。预测是否正确
看方向准确率、Brier 分数和区间命中；策略是否正确看触发后的净收益、回撤和机会成本，
两者不得混为一个结论。止损、锁利和再平衡卖出是风险动作，不代表系统预测价格下跌。

## 如何写操作方案

Prompt 中会包含「## 🎯 系统操作方案（代码生成）」章节，这是量化模型自动生成的原始判断（策略信号、触发条件、关键价位等）。你的任务是把这些量化语言翻译成交易员能执行的 K 线图语言：

**必须覆盖保守和激进两套方案**（不管当前有无买入信号）：
- 🛡️ **保守方案**：有信号时选最稳健策略，严格条件低仓位；无信号时告诉用户等什么具体条件、盯哪个价位、概率多大
- 🚀 **激进方案**：有信号时选收益最高策略，可放宽条件；无信号时分析哪个策略最接近触发、能否轻仓试探、亏多少赚多少

**每套方案必须包含**：
- 看什么（图上要看到什么信号）
- 什么价位（大概在什么价位附近行动）
- 能不能做/等（概率判断，会不会等不到）
- 错了怎么办（止损）
- 为什么选这个（对比其他候选策略的取舍理由）

**禁止编造价位**——所有数字必须来自系统方案或技术指标数据，不得自己编。

## 重要规则
1. **全部使用中文输出** — 如果原始数据中有英文内容（如新闻标题、内容），请准确翻译为中文呈现，不得修改原意。
2. **严禁编造数据** — 所有分析必须基于我提供的数据。不要编造你没有数据的日期的事件（如今天还没发生的走势、跳空缺口等）。检查策略入场条件时（如涨跌幅、成交量），必须从技术面分析数据中找实际数值，找不到就写「数据未提供，无法判断」，不准编假数字。
3. **不可直接给出投资建议** — 报告中用「建议关注」「观望」等表述，最后必须注明「以上分析仅供参考，不构成投资建议」。
4. **报告格式** — 使用 Markdown 格式输出。
5. **必须逐条展示全部重点新闻** — 「重点新闻」数据段中的每一条新闻都必须在报告的「新闻面分析」章节中用 Markdown 表格逐条列出。表格格式如下：
6. **每个价位必须是精确数字，不是区间** — 入场价、止损价、止盈价、关键点位，全部给精确到小数点后两位的单一数字，不要给「$935-$950 区域」这种区间。每条价位后面注明它是怎么算出来的。
7. **禁忌词必须替换为数值描述** — 以下词汇禁止单独出现（必须写出数值标准）：「企稳」「放量」「缩量」「阳线」「阴线」「强势」「弱势」「恐慌性抛售」「探底回升」「回调到位」「确认支撑」。例如不能说「放量阳线企稳后入场」，必须说「当日涨幅 +2.3%、成交量较前5日均量放大40%、价格连续3日未创新低后，在 $935.01 入场」。
8. **同板块必须独立成章** — 如果用户 prompt 中提供了「同板块 Alpha 快速评分」数据，你必须输出独立的「## 九、同板块关注」章节（含 Markdown 表格逐条展示所有标的），**不得**将同板块内容合并到第十章综合建议中。就算同板块标的很少，也要单独成章。
9. **严格区分止盈类型** — 固定止盈可展示目标价并计算传统风险收益比；动态止盈必须原样解释移动公式，条件止盈必须原样解释退出条件，二者均注明“固定风险收益比不可量化”。系统没有主动止盈时要明确提示，严禁自行补造目标价或声称风险收益比优秀。

| 时间 | 标题 | 正文概述 | 情感标签 |
|------|------|---------|---------|
| 新闻日期 | 中文翻译后的标题 | 正文摘要（如有，简要概括为 1-2 句话） | 正面 / 负面 / 中性 |

表格下方再写一段整体的新闻面情绪评估分析。不得省略任何一条新闻，不得只给结论。

## 报告结构
1. **股票简介** — 公司名称、代码、所属行业、主营业务简介
2. **Alpha 因子分析** — 基于多因子打分模型（技术面 60% + 新闻面 40%）的综合得分，解读当前市场状态
3. **因子有效性检验** — 如果提供了各因子 IC/IR 评级表格，必须在报告中展示并解读（哪些因子有效、哪些被剔除）
4. **基本面与估值** — 如果提供了 PE/PB 分位、ROE、毛利率等数据，必须在报告中展示
5. **SWOT 竞争分析** — 基于财务数据和近期新闻，从优势(Strengths)、劣势(Weaknesses)、机会(Opportunities)、威胁(Threats)四个维度分析公司当前竞争力。**必须严格基于我提供的 SWOT 素材数据**，不可使用训练数据中的旧知识编造。如果某维度数据不足，标注「数据不足，无法判断」。
6. **技术面分析** — 基于提供的技术指标数据做分析
7. **新闻面分析** — 基于新闻情感分析结果评估市场情绪。你必须先逐条列出所有重点新闻，每条包含标题、内容摘要（如有）、出处和情感标签，然后再给出整体的情绪评估结论。
8. **策略回测结果** — 三种交易策略的横向对比，包括各策略的收益率、夏普比率、最大回撤等核心指标
9. **同板块关注** — 基于同板块标的的 Alpha 快速评分，给出横向对比排名和板块整体判断。如果提供了同板块数据，必须在报告中用 Markdown 表格逐条展示，不得省略任何标的。
10. **综合建议与短期预测（AI分析）** — 这是报告中最重要的部分。你必须交叉印证上面所有数据（因子得分、IC/IR 检验、基本面估值、SWOT 分析、技术指标、新闻情绪、回测绩效、同板块对比、Rank IC、基准收益、**盘口买卖比**、**实时报价**等），给出：
   8.1) **数据综合分析**：从多个维度交叉验证当前市场状态。你必须按以下格式输出：
   8.1) **数据综合分析**：从多个维度交叉验证当前市场状态。你必须按以下格式输出：

      **多维信号交叉表**（必须用 Markdown 表格呈现）：

      | 维度 | 信号及解读 | 方向一致性 |
      |------|-----------|-----------|
      | Alpha 因子 | Final_Score 当前值及含义（偏多/中性/偏空），结合 Rank IC 判断预测力 | 结合该维度数据，用 1-2 句话说明是否与整体方向判断一致，以及该维度对最终结论的支撑力度 |
      | 技术指标 | MACD 金叉/死叉 + RSI 超买超卖 + 布林带位置 + KDJ 状态，综合解读 | 结合该维度数据，用 1-2 句话说明各项指标综合指向偏多还是偏空，与整体方向是否吻合 |
      | 均线系统 | 股价与 MA5/MA10/MA20/MA60 的关系，均线排列（多头/空头/交织） | 结合该维度数据，用 1-2 句话描述均线排列状态是否支持当前趋势判断 |
      | 新闻情绪 | FinBERT 情感得分 + 近期新闻方向，市场情绪判断 | 结合该维度数据，用 1-2 句话说明新闻情绪偏向及与整体方向的一致性 |
      | 盘口数据 | 买卖比 + 买卖力量对比（买盘占优/卖盘占优/基本平衡） | 结合该维度数据，用 1-2 句话说明盘口买卖力量是否支持当前方向 |
      | 实时报价 | 当前涨跌幅 + 成交量，短期市场情绪的方向确认 | 结合该维度数据，用 1-2 句话说明实时盘面是否与判断方向一致 |
      | 基本面估值（如有） | PE/PB 分位是高是低，ROE/毛利率是否健康 | 结合该维度数据，用 1-2 句话说明基本面是否支持当前判断 |
      | SWOT 竞争分析（如有） | 公司当前的优势、劣势、机会、威胁，从竞争力角度评估中长期持有价值 | 结合该维度数据，用 1-2 句话说明公司竞争力是否支撑当前操作方向 |
      | 同板块对比（如有） | 同行业标的的 Alpha 排名和行情状态，判断个股在板块中的相对强弱 | 结合该维度数据，用 1-2 句话说明板块整体情绪和个股相对位置 |
      | 策略回测绩效 | 多策略的收益/夏普/回撤，哪个跑赢买入持有基准，当前市场环境更适合哪种风格 | 结合该维度数据，用 1-2 句话说明回测绩效是否支撑当前操作建议 |

      **交叉印证结论**：表格下面写一段 3-5 句话的总结，说明哪些维度信号一致、哪些存在矛盾、综合来看当前市场的确定性如何（一致性强/部分一致/信号混乱）。**盘口数据反映了当下买卖力量对比**，**实时报价显示了当前涨跌和市场情绪**，这些必须被纳入分析。
   8.2) **操作建议**：这是报告中最具可执行性的部分，必须详细、具体、可操作。请按以下格式输出：

      **当前判断**：短期方向（偏多/偏空/震荡观望），综合 Alpha 因子得分、技术指标信号、盘口买卖力量、新闻情绪等数据说明判断依据。

      **操作逻辑解读**：解释为什么得出上述判断和操作计划。你必须逐一解读以下关键参数，说明每个参数当前的含义以及它如何影响你的判断：
      - **Final_Score**（Alpha 多因子得分）：当前值是多少，处于什么水平（偏多/中性/偏空），对方向判断的贡献
      - **Rank IC**：当前 IC 值说明因子模型对未来收益的预测力如何（有效/弱/无效），据此判断当前建议的可靠性
      - **技术指标信号**：MACD 金叉/死叉、RSI 超买/超卖、布林带位置、KDJ 状态等，哪些指标支持你的判断、哪些不一致
      - **均线系统**：股价与 MA5/MA10/MA20/MA60 的关系，均线排列（多头/空头/交织），这对趋势判断意味着什么
      - **新闻情绪**：FinBERT 情感得分偏多/偏空/中性，对短期方向的支持程度
      - **盘口买卖比**：买盘/卖盘力量对比，是否在当下给出了方向确认或警示信号
      - **实时报价**：当前涨跌幅是否反映市场短期情绪，与你的判断是否一致
      - **基本面估值**（如有）：PE/PB 分位是否合理，ROE/毛利率等基本面是否健康
      - **SWOT 竞争分析**（如有）：公司的优势/劣势/机会/威胁对当前操作方向的影响——竞争力强则放大看多信心，威胁大则需降低仓位或提高止损
      - **同板块对比**（如有）：同行业其他标的的表现如何影响该股的判断——板块龙头领涨则跟涨概率高，板块普跌而个股独涨则需谨慎
      - **策略回测绩效**：多策略的收益/夏普/回撤表现，哪些策略跑赢了买入持有基准，说明当前市场环境更适合哪种策略风格（趋势跟踪/均值回归/动量共振）

      **🎯 策略操作转化**（必须输出）：从量化策略和人类策略中**各选**表现最好的，共输出 2-3 个操作方案。两类策略都要覆盖到。

      **⚠️ 你的操作建议必须严格基于回测数据中提供的「实际交易记录」**：每条策略下面都有它在回测期内的真实买卖记录（入场日期/价格/理由、离场日期/价格/理由）。你要参照这些真实的入场时机和价位来写当前的操作建议，而不是自己编造一个操作方案。

      对**量化策略（A-H、O）**——把量化逻辑翻译成人能执行的条件：
      - **策略名称**（回测收益 +XX%，夏普 X.XX）
      - **原策略逻辑**：1 句话讲清怎么赚钱
      - **回测买卖回顾**：简要概括该策略在回测期内实际怎么操作的（参考上面的交易记录，如「共 X 笔交易，分别在 XX 价位附近入场、XX 条件触发时离场」）
      - **人类执行版**：量化术语→看K线图能判断的条件（「百分位>80%」→「MACD金叉+RSI>60+站上MA20」；「ATR移动止盈」→「买入后涨30%止损上移成本价，涨50%上移盈利20%位置」）
      - **简化代价**：大概牺牲多少收益

      对**人类策略（I-N）**——这些本来就是给人做的，不需要翻译，直接基于回测记录告诉用户当前怎么操作：
      - **策略名称**（回测收益 +XX%，夏普 X.XX）
      - **策略逻辑**：1 句话
      - **回测买卖回顾**：概括该策略在回测期内实际买卖的时机和价位
      - **当前如何执行**：参照回测中的入场模式，基于当前数据，用户现在应该做什么——等待/买入/持有/卖出？给出当前对应的具体价位（如「回测在 MA20 附近入场，当前 MA20=$XX，等价格回调至此附近」）、仓位（X 成资金）、止损价、卖出条件

      **关键点位**：根据技术指标给出具体数值的支撑位和压力位：
      - 支撑位（列出 2-3 个，如 MA20、布林下轨、近期低点，给出精确价格）
      - 压力位（列出 2-3 个，如 MA5/MA10、布林上轨、近期高点，给出精确价格）

      **分批操作计划**（这是整份报告的最终操作结论，必须综合前面所有分析得出）：

      上面的策略转化给了你各个策略的操作视角，但分批操作计划不是简单复述某个策略——你要**综合以下全部信息**，给出一个最有说服力的整体操作方案：
      - 策略转化中各策略的入场/出场逻辑和价位
      - Alpha 因子方向和 Rank IC 预测力
      - 技术指标信号（MACD/RSI/布林/KDJ）
      - 均线排列和价格位置
      - 新闻情绪方向
      - 盘口买卖力量
      - 实时报价和涨跌幅
      - 基本面估值（如有）
      - SWOT 竞争分析（如有）
      - 同板块对比（如有）

      每一项都可能对操作计划产生影响，你需要综合判断并解释为什么做某个调整。

      分批操作计划以「**综合操作方案**」为标题，先写一段 **「决策依据」**（3-5 句话），然后综合策略转化中表现最好的几个策略的优点，融汇成两套方案：

      **🛡️ 保守方案** — 综合各策略中偏稳健的入场条件。每个价位、仓位、止损后面都必须跟一句「理由」，说清楚这个数字是参考了哪个策略的哪条规则、或者基于哪个数据算出来的。

      **🚀 激进方案** — 同上，但要额外说明：相对于保守方案，具体放宽了哪个条件、为什么在当前数据下敢放宽（是盘口买盘特别强？新闻极度利多？还是其他数据支持？）。

      两套方案都要覆盖：入场价位 + 理由、仓位 + 理由、止损价 + 理由、卖出条件 + 理由。不能只给数字不给原因。

      所有点位必须基于提供的数据推导，不得凭空编造。
   8.3) **独立预测解读**：只解读「独立市场预测（代码生成）」给出的明确目标交易日、上涨/震荡/下跌概率和收益区间，不得自行外推 1-4 周方向或另造置信度。若无正式预测，明确写“本次无正式预测”。

6. **隐式分隔标记**：在第 7 章（策略回测结果）和第 8 章（综合建议与短期预测）之间，必须插入一行 `<!-- SECTION_8_BOUNDARY -->`（独占一行，前后不加其他内容）。这个标记不会在渲染时显示，但能帮助系统后续识别报告结构。
"""


def build_executive_summary(
    audit_report,
    operation_plan_signal_count: tuple[int, int] | None = None,
    market_bias: str = "neutral",
    final_score: float = 0.0,
    health_status: str = "",
) -> str:
    """构建报告顶部「一分钟速览」执行摘要。

    Args:
        audit_report: StrategyAuditReport 或 None
        operation_plan_signal_count: (总策略数, 买入信号数)
        market_bias: 市场方向
        final_score: Alpha 得分
        health_status: 健康度状态简述
    """
    lines = ["## 📊 一分钟速览\n"]
    bias_emoji = {"bullish": "📈 偏多", "bearish": "📉 偏空", "neutral": "📊 中性"}
    bias = bias_emoji.get(market_bias, market_bias)

    total, buy = operation_plan_signal_count or (0, 0)

    # 当前判断
    score_desc = "偏多" if final_score > 0.05 else ("偏空" if final_score < -0.05 else "中性")
    if buy > 0:
        judgment = f"**当前判断**: {bias}（Final_Score={final_score:+.3f}，{score_desc}），{buy}/{total} 策略建议买入"
    else:
        judgment = f"**当前判断**: {bias}（Final_Score={final_score:+.3f}，{score_desc}），{total} 策略均未触发入场 → **观望**"

    lines.append(judgment)

    # 审计摘要
    if audit_report:
        s = audit_report.summary or {}
        lines.append(f"**策略审计**: PASS={s.get('pass', 0)}, COND={s.get('conditional', 0)}, FAIL={s.get('fail', 0)}, OVERFIT={s.get('overfit', 0)}")

    # 操作建议
    if buy > 0:
        lines.append("**操作建议**: 系统已生成保守+激进双方案（见文末），请关注入场条件和有效窗口。")
    else:
        lines.append("**操作建议**: 观望等待。关注策略入场条件何时满足，见文末「系统操作方案」章节。")

    # 风险
    risks = []
    if final_score < -0.05:
        risks.append("Alpha 偏空，回调风险较高")
    if audit_report and audit_report.summary.get("overfit", 0) >= 2:
        risks.append(f"{audit_report.summary['overfit']} 个策略疑似过拟合")
    if health_status and "降级" in health_status:
        risks.append(health_status)
    if risks:
        lines.append(f"**主要风险**: {'；'.join(risks)}")

    lines.append(f"\n> ⏱ 操作方案有效窗口: **5 个交易日** | 以上为系统自动生成摘要\n")
    return "\n".join(lines)


def build_trust_hard_summary(
    *,
    data_quality_reports: list[dict] | dict | None = None,
    audit_reports: list | object | None = None,
    signal_checks: list[dict] | None = None,
    prediction_stats=None,
    evaluation_panel: dict | None = None,
    forecast_metrics: dict | None = None,
    health_reports: list[dict] | None = None,
    scope: str = "单股",
) -> str:
    """构建报告顶部可信度硬摘要。

    这个章节只使用代码系统已经计算出的硬指标，不让 LLM 改写结论。
    """

    def _as_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _dq_dict(item):
        if not item:
            return {}
        if hasattr(item, "to_dict"):
            return item.to_dict()
        return dict(item)

    data_quality = [_dq_dict(x) for x in _as_list(data_quality_reports) if x]
    signals = signal_checks or []
    health = health_reports or []

    actionable_signals = []
    relevant_signal_keys: set[tuple[str, str]] = set()
    relevant_stock_codes: set[str] = set()
    for signal in signals:
        action = str(signal.get("signal", signal.get("action", "")) or "").lower()
        if action not in ("buy", "sell"):
            continue
        actionable_signals.append(signal)
        stock_code = str(signal.get("_stock_code", "") or "")
        if stock_code:
            relevant_stock_codes.add(stock_code)
        for field in ("name", "key", "variant", "strategy_name"):
            strategy_name = str(signal.get(field, "") or "")
            if strategy_name:
                relevant_signal_keys.add((stock_code, strategy_name))

    # 组合报告只让当前可执行信号对应股票的数据质量参与硬评级。
    rated_data_quality = data_quality
    if relevant_stock_codes and any(d.get("_stock_code") for d in data_quality):
        rated_data_quality = [
            d for d in data_quality if str(d.get("_stock_code", "")) in relevant_stock_codes
        ] or data_quality

    dq_status_rank = {"ok": 0, "watch": 1, "degraded": 2, "blocked": 3}
    dq_label = {
        "ok": "可执行",
        "watch": "观察执行",
        "degraded": "降级执行",
        "blocked": "阻断新开仓",
    }
    worst_dq = "unknown"
    avg_score = None
    all_worst_dq = "unknown"
    all_avg_score = None
    if data_quality:
        all_worst_dq = max(
            (str(x.get("status", "unknown")) for x in data_quality),
            key=lambda s: dq_status_rank.get(s, 9),
        )
        all_avg_score = sum(
            float(x.get("score", 0) or 0) for x in data_quality
        ) / len(data_quality)
    if rated_data_quality:
        worst_dq = max(
            (str(x.get("status", "unknown")) for x in rated_data_quality),
            key=lambda s: dq_status_rank.get(s, 9),
        )
        avg_score = sum(float(x.get("score", 0) or 0) for x in rated_data_quality) / max(len(rated_data_quality), 1)

    audit_summary = {"pass": 0, "conditional": 0, "fail": 0, "overfit": 0}
    for audit in _as_list(audit_reports):
        if not audit:
            continue
        summary = getattr(audit, "summary", None) or (audit.get("summary") if isinstance(audit, dict) else {}) or {}
        for key in audit_summary:
            audit_summary[key] += int(summary.get(key, 0) or 0)

    level_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    signal_counts = {"buy": 0, "sell": 0, "hold": 0, "watch": 0}
    for s in signals:
        level = str(s.get("execution_level", "") or "").upper()
        if level in level_counts:
            level_counts[level] += 1
        action = str(s.get("signal", s.get("action", "")) or "").lower()
        if action in signal_counts:
            signal_counts[action] += 1

    health_counts = {"keep": 0, "watch": 0, "demote": 0}
    relevant_health_counts = {"keep": 0, "watch": 0, "demote": 0}
    relevant_health_entries = []
    for h in health:
        action = str(h.get("action", "") or "")
        if action in health_counts:
            health_counts[action] += 1
        health_stock = str(h.get("stock_code", "") or "")
        health_name = str(h.get("strategy_name", "") or "")
        is_relevant = any(
            name == health_name and (not signal_stock or not health_stock or signal_stock == health_stock)
            for signal_stock, name in relevant_signal_keys
        )
        if is_relevant and action in relevant_health_counts:
            relevant_health_counts[action] += 1
            relevant_health_entries.append(h)

    expectancy = "样本不足"
    history_count = 0
    avg_return = None
    if relevant_health_entries:
        history_count = sum(int(item.get("total", 0) or 0) for item in relevant_health_entries)
        weighted_return = sum(
            float(item.get("avg_return", 0.0) or 0.0) * int(item.get("total", 0) or 0)
            for item in relevant_health_entries
        )
        avg_return = weighted_return / history_count if history_count else None
        if relevant_health_counts["demote"]:
            expectancy = "负期望"
        elif relevant_health_counts["keep"]:
            expectancy = "正期望"
        else:
            expectancy = "样本不足"
    elif evaluation_panel:
        overall = evaluation_panel.get("overall") or {}
        history_count = int(overall.get("count", 0) or 0)
        avg_return = float(overall.get("avg_return", 0.0) or 0.0)
        expectancy = {
            "positive": "正期望",
            "negative": "负期望",
            "insufficient": "样本不足",
        }.get(overall.get("expectancy", "insufficient"), overall.get("expectancy", "样本不足"))
    elif prediction_stats:
        ps = prediction_stats.to_dict() if hasattr(prediction_stats, "to_dict") else prediction_stats
        history_count = int(ps.get("total_predictions", 0) or 0)
        if history_count > 0:
            acc = float(ps.get("direction_accuracy_all", 0) or 0)
            expectancy = "方向胜率偏高" if acc >= 0.55 else ("方向胜率偏低" if acc < 0.45 else "方向胜率中性")

    blockers = []
    if worst_dq == "blocked":
        blockers.append("数据质量阻断")
    if expectancy == "负期望":
        blockers.append("历史预测负期望")
    if relevant_health_counts["demote"] > 0:
        blockers.append(f"当前信号关联的 {relevant_health_counts['demote']} 个策略已降级")
    current_overfit = sum(
        1 for s in actionable_signals
        if str(s.get("audit", s.get("audit_verdict", "")) or "").upper() == "OVERFIT"
    )
    if current_overfit > 0:
        blockers.append(f"当前信号中 {current_overfit} 个策略疑似过拟合")

    if worst_dq == "blocked":
        trust_level = "D 数据冲突/禁止新开仓"
    elif blockers:
        trust_level = "C 仅观察或小仓验证"
    elif expectancy in ("正期望", "方向胜率偏高") and level_counts["A"] > 0:
        trust_level = "A 可执行候选"
    elif history_count <= 0 or expectancy == "样本不足":
        trust_level = "B 样本不足，小仓验证"
    else:
        trust_level = "B 条件执行"

    lines = [
        "## 🧱 可信度硬摘要（代码生成）\n",
        f"> 这部分只来自数据质量、策略审计、历史验证和策略健康度，不由 LLM 生成交易结论。\n",
        f"- 覆盖范围：**{scope}**",
        f"- 综合可信等级：**{trust_level}**",
    ]
    if avg_score is not None:
        lines.append(
            f"- 当前机会数据质量：平均 **{avg_score:.0f}/100**，"
            f"最弱闸门：**{dq_label.get(worst_dq, worst_dq)}**"
        )
    else:
        lines.append("- 数据质量：暂无评分")
    if (
        all_avg_score is not None
        and (all_worst_dq != worst_dq or abs(all_avg_score - float(avg_score or 0)) >= 0.5)
    ):
        lines.append(
            f"- 全部覆盖标的数据质量：平均 **{all_avg_score:.0f}/100**，"
            f"最弱闸门：**{dq_label.get(all_worst_dq, all_worst_dq)}**；"
            "被阻断标的不参与当前机会评级，但仍需单独处理"
        )
    lines.append(
        "- 当前信号："
        f"A级 {level_counts['A']}、B级 {level_counts['B']}、C级 {level_counts['C']}、D级 {level_counts['D']}；"
        f"买入/加仓 {signal_counts['buy']}、卖出/减仓 {signal_counts['sell']}"
    )
    lines.append(
        "- 样本外审计："
        f"严格通过(PASS) {audit_summary['pass']}、有条件(COND) {audit_summary['conditional']}、"
        f"淘汰(FAIL) {audit_summary['fail']}、过拟合警示(OVERFIT) {audit_summary['overfit']}"
    )
    history_text = f"- 当前机会关联历史验证：{history_count} 次，结论：**{expectancy}**"
    if avg_return is not None and history_count > 0:
        history_text += f"，平均方向净收益 {avg_return:+.2%}"
    lines.append(history_text)
    if health:
        lines.append(
            "- 策略健康："
            f"保留 {health_counts['keep']}、观察 {health_counts['watch']}、降级 {health_counts['demote']}"
        )
        if health_counts["demote"] and not relevant_health_counts["demote"]:
            lines.append("- 背景提示：存在已降级策略，但与当前可执行信号无关，不降低本次执行等级")
    fm = forecast_metrics or {}
    forecast_samples = int(fm.get("samples", 0) or 0)
    if forecast_samples:
        brier = float(fm.get("brier_score", 0.0) or 0.0)
        baseline = float(fm.get("baseline_brier", 0.0) or 0.0)
        forecast_status = "优于历史频率基线" if baseline > 0 and brier < baseline else "尚未优于基线"
        lines.append(
            f"- 独立预测：{forecast_samples} 次，方向正确率 "
            f"{float(fm.get('accuracy', 0.0) or 0.0):.0%}，Brier {brier:.3f} "
            f"vs 基线 {baseline:.3f}（{forecast_status}）"
        )
    else:
        lines.append("- 独立预测：样本不足；未验证模型只展示概率，不调整执行等级")
    if blockers:
        lines.append(f"- 硬约束提醒：**{'；'.join(blockers)}**")
    lines.extend([
        "",
        "> **读法速查**：A=满足执行条件，B=证据不足仅小仓验证，C=只观察，D=数据或事实冲突而驳回。",
        "> **审计速查**：PASS/COND/FAIL统计的是“股票×策略变体”，不是订单数；"
        "OVERFIT是可能与前三类重叠的过拟合警示。",
    ])
    lines.append("")
    return "\n".join(lines)


def build_forecast_section(forecasts: list, *, title: str = "## 独立市场预测（代码生成）") -> str:
    """把冻结概率预测展示成用户能快速读懂的天气预报式表格。"""
    if not forecasts:
        return (
            f"{title}\n\n"
            "> 本次未生成正式预测。常见原因是历史样本不足或可靠交易日历不可用；"
            "系统不会用买卖信号反推预测。\n"
        )
    lines = [title, ""]
    lines.append(
        "> 先预测市场，再制定交易方案。预测在生成时已冻结，目标日到期后只补录实际结果。"
    )
    lines.append(
        "> **阅读顺序**：先看目标日和截止信息，再看模型状态；只有 Champion 才能参与执行分级，"
        "最后比较三类概率与收益区间宽度。"
    )
    lines.append("")
    codes = {
        (item.code if hasattr(item, "code") else str(item.get("code", "")))
        for item in forecasts
    }
    show_code = len(codes) > 1
    if show_code:
        lines.append("| 股票 | 目标 | 截止信息 | 上涨 | 震荡 | 下跌 | 收益中位数（80%区间） | 模型状态 |")
        lines.append("|------|------|------|------:|------:|------:|------:|------:|")
    else:
        lines.append("| 目标 | 截止信息 | 上涨 | 震荡 | 下跌 | 收益中位数（80%区间） | 模型状态 |")
        lines.append("|------|------|------:|------:|------:|------:|------:|")
    for item in forecasts:
        d = item.to_dict() if hasattr(item, "to_dict") else item
        model_version = str(d.get("model_version", "") or "")
        model_status = (
            "观察模型：尚未通过样本外（OOF）验证，不影响操作等级"
            if model_version.endswith("_unvalidated")
            else f"正式模型（Champion），方向分离度{float(d.get('confidence', 0) or 0):.1%}"
        )
        lines.append(
            f"| {(str(d.get('code', '')) + ' | ') if show_code else ''}"
            f"{d.get('target_session_date', '')}（{int(d.get('horizon', 0) or 0)}个交易日） "
            f"| {d.get('data_cutoff', '')} 收盘，{float(d.get('reference_price', 0) or 0):.2f} "
            f"| {float(d.get('prob_up', 0) or 0):.1%} "
            f"| {float(d.get('prob_flat', 0) or 0):.1%} "
            f"| {float(d.get('prob_down', 0) or 0):.1%} "
            f"| {float(d.get('expected_return_p50', 0) or 0):+.2%} "
            f"（{float(d.get('expected_return_p10', 0) or 0):+.2%} ~ "
            f"{float(d.get('expected_return_p90', 0) or 0):+.2%}） "
            f"| {model_status} |"
        )
    lines.extend([
        "",
        "- 上涨/下跌：相对参考价超过 ±1%；其余记为震荡。",
        "- 收益中位数是预测分布的 P50；括号内是 P10～P90，表示约80%的历史相似结果范围，不是“80%概率赚到中位数”。",
        "- 未通过 OOF：概率只供观察，不改变 A/B/C/D、仓位或操作；Champion：已通过隔离样本外验证。",
        "- 分离度=最高方向概率-第二高方向概率，表示方向区分程度，不是正确率或收益率。",
        "- 预测评价看方向正确率、Brier 概率误差和区间命中率；交易方案盈亏另行评价。",
        "",
    ])
    return "\n".join(lines)


def build_forecast_tracking_section(
    forecasts: list,
    metrics: dict | None = None,
    *,
    title: str = "## 独立预测验证",
) -> str:
    metrics = metrics or {}
    lines = [title, ""]
    if metrics.get("samples", 0):
        lines.append(
            f"- 已验证 {metrics['samples']} 次；方向正确率 {metrics.get('accuracy', 0):.1%}；"
            f"Brier 分数 {metrics.get('brier_score', 0):.3f}"
            f"（历史频率基线 {metrics.get('baseline_brier', 0):.3f}，越低越好）；"
            f"80%区间命中率 {metrics.get('interval_coverage', 0):.1%}。"
        )
        if int(metrics.get("samples", 0) or 0) < 10:
            lines.append("- 样本少于10次：当前指标只用于积累，不据此判断模型优劣或调整仓位。")
    else:
        lines.append("- 暂无到期的新版独立预测；运行分析会生成预测，目标交易日收盘入库后自动验证。")
    verified = [
        item for item in (forecasts or [])
        if (getattr(item, "status", "") if hasattr(item, "status") else item.get("status")) == "verified"
    ]
    if verified:
        lines.extend(["", "| 股票 | 何时预测 | 预测哪天 | 预测 | 实际 | 结果 |", "|------|------|------|------|------|:---:|"])
        mapping = {"bullish": "上涨", "neutral": "震荡", "bearish": "下跌"}
        for item in verified[:8]:
            d = item.to_dict() if hasattr(item, "to_dict") else item
            lines.append(
                f"| {d.get('code', '')} | {str(d.get('generated_at', ''))[:16].replace('T', ' ')} "
                f"| {d.get('target_session_date', '')} "
                f"| {mapping.get(d.get('direction'), d.get('direction', ''))} "
                f"| {float(d.get('actual_price', 0) or 0):.2f}（{float(d.get('actual_return', 0) or 0):+.2%}，"
                f"{mapping.get(d.get('actual_direction'), d.get('actual_direction', ''))}） "
                f"| {'对' if int(d.get('correct', 0) or 0) else '错'} |"
            )
    lines.append("")
    return "\n".join(lines)


def build_prediction_footer(code: str, prediction_stats,
                            validated_predictions: list,
                            unverified_count: int = 0,
                            evaluation_panel: dict | None = None,
                            scope_label: str = "") -> str:
    """构建预测追踪报告尾部 Markdown，供 Tab1/Tab3 直接拼接到报告末尾。"""
    lines = ["\n---\n", "## 旧版动作追踪（兼容数据）\n"]
    lines.append("> 本节是升级前按交易动作生成的兼容记录，不参与新版独立预测评分。\n")
    if scope_label:
        lines.append(f"> 统计范围：{scope_label}\n")

    if prediction_stats:
        ps = prediction_stats.to_dict() if hasattr(prediction_stats, 'to_dict') else (prediction_stats or {})
        total = ps.get('total_predictions', 0)
        if total > 0:
            lines.append(f"- 累计已验证独立事件：{total} 次")
            lines.append(f"- 近 10 个独立事件方向正确率：{ps.get('direction_accuracy_10', 0):.0%}")
            lines.append(f"- 全部历史正确率：{ps.get('direction_accuracy_all', 0):.0%}")
            lines.append(f"- 正确率趋势：{ps.get('accuracy_trend', 'stable')}")
        if unverified_count > 0:
            lines.append(f"- 待验证独立事件：{unverified_count} 个（验证窗口未到）")
        if total <= 0 and unverified_count <= 0:
            lines.append("- 暂无历史预测记录（首次分析）")
        lines.append("")

    if validated_predictions:
        lines.append("### 历史方向预测结果（非本次交易建议）\n")
        lines.append(
            "> **系统能力**：预测指定目标交易日的收盘方向，不预测精确目标价。"
            "目标日收盘价比预测时价格高超过1%记为上涨，低超过1%记为下跌，"
            "上下1%以内记为震荡。\n"
        )
        lines.append(
            "| 股票 | 何时预测 | 预测内容 | 实际结果 | 对错 |"
        )
        lines.append(
            "|------|------|------|------|:---:|"
        )
        for p in validated_predictions[:5]:
            d = p.to_dict() if hasattr(p, 'to_dict') else p
            direction = str(d.get("direction") or "")
            actual_direction = str(d.get("actual_direction") or "")
            direction_map = {"bullish": "上涨", "bearish": "下跌", "neutral": "震荡/中性"}
            currency = "¥" if d.get("market") == "A" else "$"
            time_str = str(d.get('predict_time') or '')[:16].replace("T", " ")
            target_date = str(d.get("validation_end_date") or "—")[:10]
            reference_price = float(d.get("predicted_price", 0) or 0)
            underlying_return = float(d.get("underlying_return", 0) or 0)
            actual_price = (
                reference_price * (1.0 + underlying_return)
                if reference_price > 0 else 0.0
            )
            if direction == "bullish":
                forecast_sentence = (
                    f"预测{target_date}收盘价将高于{currency}{reference_price:.2f}"
                )
            elif direction == "bearish":
                forecast_sentence = (
                    f"预测{target_date}收盘价将低于{currency}{reference_price:.2f}"
                )
            else:
                forecast_sentence = (
                    f"预测{target_date}收盘价与{currency}{reference_price:.2f}"
                    "相比保持在±1%内"
                )
            actual_text = (
                f"{target_date}实际收盘{currency}{actual_price:.2f}"
                f"（{underlying_return:+.2%}，"
                f"{direction_map.get(actual_direction, actual_direction or '未知')}）"
            )
            if direction not in ("bullish", "bearish"):
                verdict = "不判定"
            elif actual_direction and direction == actual_direction:
                verdict = "对"
            elif actual_direction:
                verdict = "错"
            else:
                verdict = "待确认"
            lines.append(
                f"| {d.get('code', '')} "
                f"| {time_str} "
                f"| {forecast_sentence} "
                f"| {actual_text} "
                f"| {verdict} |"
            )
        lines.append("")

    if evaluation_panel:
        def _status_text(expectancy: str) -> str:
            return {
                "positive": "正期望",
                "negative": "负期望",
                "insufficient": "样本不足",
            }.get(expectancy, expectancy or "样本不足")

        overall = evaluation_panel.get("overall") or {}
        if overall.get("count", 0) > 0:
            lines.append("### 真实历史预测评估")
            lines.append(
                f"- 整体：{overall.get('count', 0)} 次验证，"
                f"方向正确率 {overall.get('accuracy', 0):.0%}，"
                f"平均方向净收益 {overall.get('avg_return', 0):+.2%}，"
                f"结论：**{_status_text(overall.get('expectancy', 'insufficient'))}**"
            )

            by_strategy = [
                x for x in (evaluation_panel.get("by_strategy") or [])
                if x.get("label") and x.get("label") != "整体预测"
            ][:5]
            if by_strategy:
                lines.append("")
                lines.append("| 策略 | 验证次数 | 方向正确率 | 平均方向净收益 | 期望 |")
                lines.append("|------|------:|------:|------:|------|")
                for row in by_strategy:
                    lines.append(
                        f"| {row.get('label', '')[:24]} "
                        f"| {row.get('count', 0)} "
                        f"| {row.get('accuracy', 0):.0%} "
                        f"| {row.get('avg_return', 0):+.2%} "
                        f"| {_status_text(row.get('expectancy', 'insufficient'))} |"
                    )

            by_regime = [
                x for x in (evaluation_panel.get("by_regime") or [])
                if x.get("label") and x.get("label") != "unknown"
            ][:5]
            if by_regime:
                lines.append("")
                lines.append("| 行情状态 | 验证次数 | 方向正确率 | 平均方向净收益 | 期望 |")
                lines.append("|------|------:|------:|------:|------|")
                for row in by_regime:
                    lines.append(
                        f"| {row.get('label', '')} "
                        f"| {row.get('count', 0)} "
                        f"| {row.get('accuracy', 0):.0%} "
                        f"| {row.get('avg_return', 0):+.2%} "
                        f"| {_status_text(row.get('expectancy', 'insufficient'))} |"
                    )

            exit_reviews = (evaluation_panel.get("exit_reviews") or [])[:5]
            if exit_reviews:
                lines.append("")
                lines.append("#### 卖出后退出质量复盘")
                lines.append("| 策略 | 样本 | 5日涨跌 | 10日涨跌 | 20日涨跌 | 避免损失 | 机会成本 | 有效退出率 |")
                lines.append("|------|---:|---:|---:|---:|---:|---:|---:|")
                for row in exit_reviews:
                    lines.append(
                        f"| {row.get('strategy_name', '')[:20]} "
                        f"| {row.get('count', 0)} "
                        f"| {row.get('avg_return_5d', 0):+.2%} "
                        f"| {row.get('avg_return_10d', 0):+.2%} "
                        f"| {row.get('avg_return_20d', 0):+.2%} "
                        f"| {row.get('avg_avoided_loss', 0):.2%} "
                        f"| {row.get('avg_opportunity_cost', 0):.2%} "
                        f"| {row.get('effective_rate', 0):.0%} |"
                    )
            lines.append("")

    lines.append("*以上为系统自动追踪数据，仅供参考。*\n")
    return "\n".join(lines)


def build_strategy_audit_section(audit_report) -> str:
    """构建策略池审计 Markdown 章节（代码注入，非 LLM 生成）。

    从 StrategyAuditReport 生成审计表格和推荐建议。
    """
    if audit_report is None:
        return ""

    entries = getattr(audit_report, "entries", []) or []
    if not entries:
        return ""

    lines = ["\n---\n", "## 📋 策略池审计（时间切分验证）\n"]
    lines.append(f"> ⏱ 分割日期：{audit_report.split_date}")
    lines.append(f"> 训练期：{audit_report.train_period} | 验证期：{audit_report.test_period}")
    lines.append("")

    # ── 判定汇总 ──
    s = audit_report.summary or {}
    lines.append(f"**判定汇总**：✅ 通过 {s.get('pass', 0)} 个 "
                 f"| ⚠️ 有条件 {s.get('conditional', 0)} 个 "
                 f"| ❌ 淘汰 {s.get('fail', 0)} 个 "
                 f"| 🔴 过拟合 {s.get('overfit', 0)} 个")
    lines.append("")

    # ── 审计表格 ──
    lines.append("| 策略 | 判定 | 训练期 | | | | 验证期（样本外） | | | | 衰减 |")
    lines.append("|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    lines.append("| | | 交易 | 夏普 | 回撤 | 胜率 | 交易 | 夏普 | 回撤 | 胜率 | 夏普 |")
    lines.append("|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for e in entries:
        emoji = {"PASS": "✅", "CONDITIONAL": "⚠️", "FAIL": "❌"}.get(e.verdict, "❓")
        overfit_mark = " 🔴" if getattr(e, "overfit", False) else ""
        lines.append(
            f"| {e.strategy_key} {e.strategy_name[:12]} "
            f"| {emoji} {e.verdict}{overfit_mark} "
            f"| {e.train_trades} "
            f"| {e.train_sharpe:.2f} "
            f"| {e.train_drawdown*100:.0f}% "
            f"| {e.train_win_rate*100:.0f}% "
            f"| {e.test_trades} "
            f"| {e.test_sharpe:.2f} "
            f"| {e.test_drawdown*100:.0f}% "
            f"| {e.test_win_rate*100:.0f}% "
            f"| {e.sharpe_degradation*100:.0f}% |"
        )
    lines.append("")

    bootstrap_entries = [
        e for e in entries if getattr(e, "bootstrap_status", "") == "ok"
    ]
    if bootstrap_entries:
        lines.append("### 样本外分块 Bootstrap 风险")
        lines.append("| 策略 | 模拟数 | 正期望概率 | 收益95%区间 | 夏普95%区间 | 回撤P95 | 30%回撤概率 |")
        lines.append("|------|------:|------:|------:|------:|------:|------:|")
        for e in bootstrap_entries:
            lines.append(
                f"| {e.strategy_key} "
                f"| {e.bootstrap_samples} "
                f"| {e.positive_expectancy_prob:.0%} "
                f"| [{e.return_ci_low:+.1%}, {e.return_ci_high:+.1%}] "
                f"| [{e.sharpe_ci_low:+.2f}, {e.sharpe_ci_high:+.2f}] "
                f"| {e.drawdown_p95:.1%} "
                f"| {e.ruin_probability:.1%} |"
            )
        lines.append("")
    elif entries:
        lines.append("> Bootstrap：样本外交易或日收益不足，暂不输出概率，强结论自动降级。\n")

    # ── 判定逻辑说明 ──
    lines.append("**判定标准**：")
    lines.append(f"- ✅ PASS：训练期 ≥5 笔 AND 验证期 ≥3 笔 AND 验证夏普 ≥1.0 AND 验证回撤 ≤30% AND 验证胜率 ≥45%")
    lines.append(f"- ⚠️ CONDITIONAL：训练期 ≥3 笔 AND 验证期 ≥1 笔 AND 验证夏普 ≥0.5 AND 验证回撤 ≤40%")
    lines.append(f"- ❌ FAIL：不满足以上条件")
    lines.append(f"- 🔴 过拟合：验证夏普 < 训练夏普的 30%")
    lines.append("- Bootstrap：仅重采样样本外连续收益块；正期望概率不足或收益下界过低会降级")
    lines.append("")

    # ── 推荐建议 ──
    recs = getattr(audit_report, "recommendations", []) or []
    if recs:
        lines.append("**建议**：")
        for r in recs:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("*策略审计基于回测数据的时间切分验证，用于评估策略在样本外数据的表现。*\n")
    return "\n".join(lines)


def build_strategy_health_section(
    health_report: list[dict],
    param_candidates: list[dict] | None = None,
) -> str:
    """构建策略健康度追踪章节（持续优化闭环）。

    Args:
        health_report: Database.get_strategy_health_report() 的返回值
    """
    if not health_report and not param_candidates:
        return ""

    lines = ["\n---\n", "## 🩺 策略健康度追踪（持续优化闭环）\n"]
    if health_report:
        lines.append("> 基于新版交易方案复盘数据，先按独立交易日去重，再按分钟证据质量折算有效样本；统计扣成本正收益率、95%置信下界和平均净表现。\n")

        lines.append("| 策略 | 信号 | 独立日/有效样本 | 正收益率 | 95%下界 | 近期正收益率 | 平均净表现 | 趋势 | 状态 | 建议 |")
        lines.append("|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|------|------|")

    action_labels = {"keep": "✅ 保留", "watch": "⚠️ 观察", "demote": "🔻 降级"}
    status_labels = {"reliable": "可靠", "unstable": "不稳定", "unreliable": "不可靠"}

    for h in health_report or []:
        action = action_labels.get(h["action"], h["action"])
        status = status_labels.get(h["status"], h["status"])
        trend_emoji = {"improving": "📈", "stable": "➡️", "declining": "📉"}
        trend = f"{trend_emoji.get(h['trend'], '')} {h['trend']}"

        lines.append(
            f"| {h['strategy_name'][:20]} "
            f"| {'买入' if h.get('signal_action') == 'buy' else '卖出' if h.get('signal_action') == 'sell' else '未知'} "
            f"| {h['total']}/{float(h.get('effective_samples', h['total'])):.1f} "
            f"| {h['accuracy']:.0%} "
            f"| {float(h.get('confidence_lower_95', 0) or 0):.0%} "
            f"| {h['recent_accuracy']:.0%} "
            f"| {float(h.get('avg_return', 0) or 0):+.2%} "
            f"| {trend} "
            f"| {status} "
            f"| {action} |"
        )
    lines.append("")

    # 建议
    demotes = [h for h in (health_report or []) if h["action"] == "demote"]
    watches = [h for h in (health_report or []) if h["action"] == "watch"]
    if demotes:
        names = ", ".join(h["strategy_name"][:15] for h in demotes)
        reasons = "；".join(
            f"{h['strategy_name'][:12]}: {h.get('risk_note') or '历史置信度不足'}"
            for h in demotes[:3]
        )
        lines.append(f"⚠️ **建议降级**: {names} — {reasons}。建议从操作方案中排除，仅在回测表格中参考。")
    if watches:
        names = ", ".join(h["strategy_name"][:15] for h in watches)
        reasons = "；".join(
            f"{h['strategy_name'][:12]}: {h.get('risk_note') or '样本/置信度尚不足'}"
            for h in watches[:3]
        )
        lines.append(f"👀 **建议观察**: {names} — {reasons}。降低其在操作方案中的权重和仓位。")

    lines.append("")
    if param_candidates:
        import json
        status_labels = {
            "candidate": "观察中",
            "paper": "影子观察",
            "champion": "已晋升",
            "superseded": "已替代",
            "rolled_back": "已回滚",
            "rejected": "未通过",
        }
        lines.extend([
            "### 参数候选生命周期\n",
            "> 参数必须在不同数据截止日重复通过 walk-forward，才会替换正式参数。\n",
            "| 股票 | 策略 | 参数 | 确认 | OOS收益 | 超额收益 | 合格窗口 | 晋升通道 | OOS夏普 | 交易数 | 状态 |",
            "|------|------|------|:---:|------:|------:|:---:|------|------:|------:|------|",
        ])
        for row in param_candidates[:20]:
            try:
                params = json.loads(row.get("params_json") or "{}")
                params_text = ", ".join(f"{k}={v}" for k, v in params.items()) or "默认"
            except Exception:
                params_text = str(row.get("params_json") or "—")
            lines.append(
                f"| {row.get('stock_code', '—')} | {row.get('strategy_key', '—')} | "
                f"{params_text[:32]} | {int(row.get('confirmations', 0) or 0)} | "
                f"{float(row.get('avg_oos_return', 0) or 0):+.2%} | "
                f"{float(row.get('avg_oos_excess_return', 0) or 0):+.2%} | "
                f"{int(row.get('qualified_windows', 0) or 0)}/"
                f"{int(row.get('selected_windows', 0) or 0)} | "
                f"{ {'excess': '超额', 'risk_adjusted': '风险调整', 'mixed': '混合'}.get(str(row.get('promotion_path') or ''), '—') } | "
                f"{float(row.get('avg_oos_sharpe', 0) or 0):.2f} | "
                f"{int(row.get('oos_trades', 0) or 0)} | "
                f"{status_labels.get(row.get('status'), row.get('status', '—'))} |"
            )
        lines.append("")

    lines.append("*策略健康度与参数候选状态会随新版预测验证和样本外窗口持续更新。*\n")
    return "\n".join(lines)


def build_user_prompt(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    bt_summary: str,
    bt_table: str,
    alpha_text: str,
    data_range: str = "",
    extra_sections: str = "",
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
) -> str:
    """构建 LLM user prompt（将各模块数据拼接为自然语言输入）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")

    # ── SWOT 素材段 ──
    swot_section = ""
    if swot_data:
        fin = swot_data.get("financial", {})
        val = swot_data.get("valuation", {})
        swot_lines = [
            "## SWOT 分析素材（严格基于以下数据，不可编造）",
            "",
            f"- 公司名称：{swot_data.get('company_name', name)}",
            f"- 所属行业：{swot_data.get('industry', '未分类')}",
            f"- 当前行情：{swot_data.get('market_regime', 'unknown')}",
            "",
            "【财务数据】",
            f"- ROE：{fin.get('roe', 0):.1%}",
            f"- 毛利率（TTM/年报）：{fin.get('gross_margin', 0):.1%}（5年均值：{fin.get('gross_margin_5y', 0):.1%}）",
            f"- 资产负债率：{fin.get('debt_ratio', 0):.1%}",
            f"- 净利润同比增速：{fin.get('net_profit_yoy', 0):+.1%}（5年均值：{fin.get('net_profit_yoy_5y', 0):+.1%}）",
            f"- 营收同比增速：{fin.get('revenue_yoy', 0):+.1%}（5年均值：{fin.get('revenue_yoy_5y', 0):+.1%}）",
            "",
            "【估值数据】",
            f"- PE(TTM) 3年历史分位：{val.get('pe_percentile', 0.5):.1%}",
            f"- PB 3年历史分位：{val.get('pb_percentile', 0.5):.1%}",
            "",
        ]
        # 新闻摘要
        news_list = swot_data.get("news", [])
        if news_list:
            swot_lines.append("【近期新闻摘要】")
            for i, n in enumerate(news_list, 1):
                swot_lines.append(f"{i}. {n}")
            swot_lines.append("")
        else:
            swot_lines.extend(["【近期新闻摘要】", "（暂无最新新闻数据）", ""])

        swot_lines.extend([
            "**SWOT 分析要求**：",
            "- S（优势）/ W（劣势）：仅基于上述财务和估值数据推导",
            "- O（机会）/ T（威胁）：仅基于上述新闻摘要推导",
            "- 不要使用训练数据中的旧行业知识（如护城河、管理层评价等）",
            "- 如果某维度缺乏足够信息，请标注「数据不足，无法判断」",
            "- 输出格式：Markdown 表格，四列分别为「维度」「要素」「数据支撑」「置信度」",
            "",
        ])
        swot_section = "\n".join(swot_lines)

    # ── 同板块段 ──
    peer_section = ""
    if peer_data:
        peer_lines = [
            "## 同板块 Alpha 快速评分",
            "",
            "以下为同行业/同类股票的简化版 Alpha 评分（仅技术面，不含新闻和基本面），按 Final_Score 降序排列：",
            "",
            "| 排名 | 代码 | 名称 | Final_Score | 行情状态 | 关注建议 |",
            "|------|------|------|-------------|----------|----------|",
        ]
        for r in peer_data:
            peer_lines.append(
                f"| {r.get('rank', '-')} | {r.get('code', '')} "
                f"| {r.get('name', r.get('code', ''))} "
                f"| {r.get('final_score', 0):+.3f} "
                f"| {r.get('regime', 'unknown')} "
                f"| {r.get('verdict', '-')} |"
            )
        peer_lines.extend([
            "",
            "**同板块分析要求**：",
            "- 用上述数据生成「九、同板块关注」章节",
            "- 必须逐条展示所有标的（Markdown 表格），不得省略",
            "- 表格下方写一段板块整体判断（2-3 句话）",
            "- 标注与当前分析标的（即本报告主体）的对比",
            "",
        ])
        peer_section = "\n".join(peer_lines)

    # 时间上下文（区分 A 股 / 美股）
    from datetime import datetime
    now_cn = datetime.now()
    market_type = stock_info.get('market', 'US')

    # 提取数据截止日期
    data_end = data_range.split('~')[-1].strip() if data_range and '~' in data_range else '未知'

    if market_type == 'A':
        time_ctx = (
            f"现在是北京时间 {now_cn.strftime('%Y-%m-%d %H:%M')}。"
            f"A 股交易时段为工作日 9:30-11:30、13:00-15:00。"
        )
        report_desc = (
            f"本报告是一份**盘后（EOD）完整分析报告**，分析标的为 A 股。"
            f"你拥有的数据截止于 **{data_end}**（最近一个交易日收盘）。"
            f"你可以分析截止 {data_end} 的全部走势和数据。"
            f"**{data_end} 之后的数据你都没有——不要编造 {data_end} 之后的任何价格行为、走势或跳空缺口。**"
        )
    else:
        et_hour = (now_cn.hour - 12) % 24
        time_ctx = (
            f"现在是北京时间 {now_cn.strftime('%Y-%m-%d %H:%M')}，"
            f"对应美东时间约 {et_hour:02d}:{now_cn.minute:02d}。"
            f"美股交易时段为美东 9:30-16:00（夏令时北京时间 21:30-次日 04:00）。"
        )
        report_desc = (
            f"本报告是一份**盘后（EOD）完整分析报告**，分析标的为美股。"
            f"你拥有的数据截止于 **{data_end}**（最近一个交易日收盘）。"
            f"你可以分析截止 {data_end} 的全部走势和数据。"
            f"**{data_end} 之后的数据你都没有——不要编造 {data_end} 之后的任何价格行为、走势或跳空缺口。**"
            f"注意：当前北京时间已是 {now_cn.strftime('%Y-%m-%d')}，可能比 {data_end} 晚一天，"
            f"这是因为美股收盘时间在北京时间次日凌晨。不要因此混淆数据边界。"
        )

    return f"""请用中文分析以下股票 {name}({code})：

## ⚠️ 当前时间与报告类型
{time_ctx}
{report_desc}

## 回测数据范围
{data_range or '未提供'}
（注意：回测使用的实际数据日期范围如上，可能因数据源限制与用户选择周期不完全一致）

## 股票基本信息
- 名称：{name}
- 代码：{code}
- 市场：{stock_info.get('market', '')}
- 行业：{stock_info.get('industry', '')}
- 简介：{stock_info.get('description', '')}

## Alpha 因子得分（多因子量化模型）
{alpha_text}

## 技术面分析数据
{technical_summary}

## 新闻情感分析
{news_text}

重点新闻：
{top_news}

{extra_sections}
{swot_section}
{peer_section}
## 三策略回测对比
{bt_summary}

{bt_table}

请按照要求的报告结构生成完整中文分析报告。"""


# ============================================================
# 盘中分析 Prompt
# ============================================================

INTRADAY_SYSTEM_PROMPT = """你是一个专业的量化分析师，正在为一只美股生成**盘中实时操作参考**。

注意：以下提供了一份 T-1 日（上一个交易日）收盘后生成的完整分析报告（第 1-7 章），以及一份盘中实时快照数据（**纯数值，无任何预设解读**）。
你的任务是基于这些数据，**仅生成第八章「盘中操作参考（AI实时分析）」**，不要重复前面的章节。所有数据的含义需要你来判断，快照中的表格只提供数字不提供结论。

## 重要规则
1. **全部使用中文输出** — 如果原始数据中有英文内容，请翻译为中文，不得修改原意。
2. **严禁编造数据** — 所有分析必须基于我提供的数据。不要编造你没有数据的日期的事件（如今天还没发生的走势、跳空缺口等）。检查策略入场条件时（如涨跌幅、成交量），必须从技术面分析数据中找实际数值，找不到就写「数据未提供，无法判断」，不准编假数字。
3. **不可直接给出投资建议** — 用「建议关注」「可考虑」「观望」等表述，最后注明「以上分析仅供参考，不构成投资建议」。
4. **输出格式** — 只输出第八章的 Markdown 内容。开始于 `## 八、盘中操作参考（AI实时分析）`。
5. **每个结论都要有依据** — 对于每一个判断，必须说明是基于哪个数据的什么值推导出来的，不要只给结论。
6. **每个价位必须是精确数字，不是区间** — 入场价、止损价、止盈价、关键点位，全部给精确到小数点后两位的单一数字，不要给「$935-$950 区域」这种区间。每条价位后面注明它是怎么算出来的。

7. **禁忌词必须替换为数值描述** — 以下词汇禁止单独出现（必须写出数值标准）：「企稳」「放量」「缩量」「阳线」「阴线」「强势」「弱势」「恐慌性抛售」「探底回升」「回调到位」「确认支撑」。
8. **盘中数据时效声明** — 所有点位基于快照时刻的实时价格计算，会随行情变化。
9. **翻译系统操作方案** — prompt 中会包含「## 🎯 系统操作方案（代码生成）」章节（量化模型原始判断）。你的 8.2 节改为输出保守和激进两套方案，把量化条件翻译成交易员能看懂的 K 线图语言：看什么信号、什么价位、概率多大；有信号时选最稳/最优策略，无信号时指出最接近触发的策略和轻仓试探的可能。所有价位必须来自系统方案或技术指标，不得自编。
10. **严格区分止盈类型** — 固定止盈可展示目标价和传统风险收益比；动态/条件止盈只解释系统给出的公式或退出条件，并注明固定风险收益比不可量化。不得自行补造止盈价或评价风险收益比优秀。
11. **预测边界** — 只能引用「独立市场预测（代码生成）」的目标日期、概率和区间；不得用买卖动作反推方向，不得修改概率。风险退出、锁利和再平衡卖出不等于看空预测。

## 第八章结构要求

### 8.1 数据综合分析

你必须以下面的格式输出：

**盘中多维信号交叉表**（必须用 Markdown 表格呈现）：

| 维度 | 信号及解读 | 方向一致性 |
|------|-----------|-----------|
| Alpha 因子（T-1日） | 说明 Final_Score 当前值、处于偏多/中性/偏空哪个区间，结合 Rank IC 判断这个分数的可靠性 | 1-2 句话说明该维度是否与你的整体方向判断一致 |
| 技术指标（T-1日） | 综合 MACD 金叉/死叉、RSI 数值和区域、布林带位置、KDJ 状态，给出整体技术面解读 | 1-2 句话说明各项指标综合指向偏多还是偏空 |
| 均线系统（T-1日） | 说明 T-1 日收盘时均线排列状态（多头/空头/交织），各均线具体数值 | 1-2 句话描述均线排列是否支持趋势判断 |
| 盘中价格位置 | **核心维度**：当前最新价相对于 T-1 日各均线的位置关系，偏离百分比的含义，是否跌破/站上关键均线。必须给出解读而不只是罗列数值 | 1-2 句话说明当前价格相对 T-1 日技术框架是走强还是走弱 |
| 盘中走势形态 | 从开盘到最新的走势路径（高开低走/低开高走/窄幅震荡等），这对判断日内多空力量非常重要 | 1-2 句话说明走势形态与整体方向判断是否一致 |
| VWAP 位置 | 价格与成交量加权均价的关系——高于VWAP表示日内多头主导，低于VWAP表示空头主导 | 1-2 句话说明VWAP偏离是支持还是挑战当前判断 |
| 日内动量 | 开盘→最新涨跌幅及距日内高低点的位置——反映日内趋势强度和关键位有效性 | 1-2 句话说明日内动量方向 |
| 盘口数据 | 买卖比的实际含义——是确认方向还是形成背离？买卖比绝对值能否解释当前价格变动？ | 1-2 句话说明盘口是确认信号还是警示信号 |
| 新闻情绪 | FinBERT 情感得分 + 近期新闻方向 + 今日增量新闻（如有）的倾向 | 1-2 句话说明新闻情绪偏向及与整体方向的一致性 |
| 基本面估值（如有） | PE/PB 分位是高是低，ROE/毛利率是否健康 | 1-2 句话说明基本面是否支持当前判断 |
| 策略回测绩效（T-1日） | 多策略的收益/夏普/回撤对比，哪个跑赢了买入持有基准 | 1-2 句话说明回测绩效如何支撑当前操作建议 |
| 盘前预测验证（如有） | 盘前预测与实际盘中走势的对比——预测正确说明策略框架有效，预测偏差则需分析原因 | 1-2 句话说明盘前预测的一致性对当前判断的影响 |

**交叉印证结论**：表格下方写 3-5 句话的总结。要点：
- 哪些维度信号一致、哪些存在矛盾
- 盘中走势形态和盘口数据是最重要的实时维度——它们与 T-1 日技术框架是确认关系还是修正关系
- 如果盘中走势与盘口出现背离（如价格下跌但买盘巨大），必须分析背后的可能原因和两种演变路径
- 综合来看当前操作的确定性如何（高/中/低），不确定性的主要来源是什么

### 8.2 操作建议

**当前判断**：短期方向（偏多/偏空/震荡观望）。必须说明这个判断是如何综合 T-1 日 Alpha + 技术指标 + 盘中实时数据得出的。

**操作逻辑解读**：逐一解读以下关键参数：
- **T-1 Final_Score**（Alpha 多因子得分）：值是多少，偏多/中性/偏空
- **T-1 Rank IC**：因子预测力如何
- **T-1 技术指标**：MACD/RSI/布林/KDJ，哪些支持你的判断
- **盘中价格 vs 关键均线**：最新价与各均线的位置关系，这是最重要的盘中输入
- **盘中走势形态**：从开盘到现在的走势路径说明了什么日内多空博弈格局
- **VWAP 位置**：当前价 vs VWAP 的含义——多头主导还是空头主导，机构成本区在何处
- **日内动量**：开盘→最新涨跌幅，距日内高/低点的位置，判断日内趋势强度和关键位有效性
- **盘中盘口买卖比**：买卖力量是确认还是背离？结合盘中走势一起解读
- **日内涨跌和成交量**：涨跌幅含义，量比反映的市场参与度
- **新闻情绪**：FinBERT 得分 + 今日增量新闻
- **基本面估值**（如有）
- **策略回测绩效（T-1日）**

**🎯 策略操作转化**（必须输出）：从量化策略和人类策略中各选最优的，共输出 2 个方案。量化策略→翻译成人能执行的条件+简化代价；人类策略→直接告诉用户当前如何执行。

**🛡️ 保守方案** — 综合 T-1 回测策略中偏稳健的入场条件 + 盘中实时数据。每个价位、仓位、止损必须跟理由。

**🚀 激进方案** — 同上，说明相对于保守方案放宽了哪个条件、为什么盘中数据支持放宽。

两套方案覆盖：入场价位 + 理由、仓位 + 理由、止损价 + 理由、卖出条件 + 理由。

**关键点位**：
- 支撑位（2-3 个，精确价格）
- 压力位（2-3 个，精确价格）

**止损/止盈参考**：止损价 + 止盈价（精确价格）

### 8.3 短期走势预测

- 方向判断（偏多/偏空/震荡）及置信度（高/中/低）
- 核心依据（T-1 日趋势 + 盘中实时信号的交叉验证结果）
- 需密切观察的关键变量（如盘中是否收复 MA5、盘口是否回暖、量比是否上升）
- 矛盾情景分析：如果盘中与 T-1 判断冲突，给出两种演变路径
- **盘前预测验证**：如果提供了盘前预测验证数据（三时段联动），必须解读盘前预测的准确性——预测正确则增强当前判断的置信度，预测偏差则分析可能原因（是宏观情绪变化、个股独立事件、还是盘前流动性不足导致的价格跳变）

"""


def build_intraday_user_prompt(
    t1_report_content: str,
    snapshot_text: str,
    stock_info: dict,
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
    pre_report_content: str | None = None,
) -> str:
    """构建盘中分析的 LLM user prompt。

    Args:
        t1_report_content: T-1 日完整报告的全文（第 1-7 章）
        snapshot_text:     盘中实时快照的 Markdown 文本
        stock_info:        股票基本信息字典
        swot_data:         实时 SWOT 素材（可选）
        peer_data:         同板块快速评分（可选）
        pre_report_content: 盘前报告全文（用于承上启下，可选）
    """
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")

    # ── 补充分析段（仅作为 AI 分析的输入素材，不独立成章）──
    # T-1 报告已包含完整的 SWOT（第五章）和同板块（第九章）章节，
    # 此处仅提供最新数据供 AI 参考，避免章节重复。
    supplement_parts: list[str] = []

    if swot_data:
        fin = swot_data.get("financial", {})
        val = swot_data.get("valuation", {})
        news_list = swot_data.get("news", [])
        swot_lines = [
            "",
            "## 最新 SWOT 参考数据（T-1 报告第五章已有完整 SWOT，此处为增量参考）",
            f"- 行业：{swot_data.get('industry', '未分类')}",
            f"- ROE：{fin.get('roe', 0):.1%} | 毛利率：{fin.get('gross_margin', 0):.1%}（5Y均：{fin.get('gross_margin_5y', 0):.1%}）",
            f"- 净利润同比：{fin.get('net_profit_yoy', 0):+.1%}（5Y均：{fin.get('net_profit_yoy_5y', 0):+.1%}）| 营收同比：{fin.get('revenue_yoy', 0):+.1%}（5Y均：{fin.get('revenue_yoy_5y', 0):+.1%}）",
            f"- PE 分位：{val.get('pe_percentile', 0.5):.1%} | PB 分位：{val.get('pb_percentile', 0.5):.1%}",
        ]
        if news_list:
            swot_lines.append("- 近期新闻：" + "；".join(news_list[:3]))
        supplement_parts.append("\n".join(swot_lines))

    if peer_data:
        peer_lines = [
            "",
            "## 最新同板块参考数据（T-1 报告第九章已有完整分析，此处为增量参考）",
            "| 排名 | 代码 | Final_Score | 行情 | 建议 |",
            "|------|------|-------------|------|------|",
        ]
        for r in peer_data[:8]:
            peer_lines.append(
                f"| {r.get('rank', '-')} | {r['code']} "
                f"| {r.get('final_score', 0):+.3f} "
                f"| {r.get('regime', '?')} "
                f"| {r.get('verdict', '-')} |"
            )
        supplement_parts.append("\n".join(peer_lines))

    supplement = "\n".join(supplement_parts)

    # 检测 T-1 报告是否有完整章节
    t1_has_swot = "SWOT" in t1_report_content
    t1_has_peers = "同板块" in t1_report_content
    t1_is_full = t1_has_swot and t1_has_peers

    from datetime import datetime
    now_cn = datetime.now()
    et_hour = (now_cn.hour - 12) % 24

    return f"""请基于以下数据，为 {name}({code}) 生成盘中分析报告的第八章「盘中操作参考」。

## ⚠️ 当前时间与报告类型
现在是北京时间 {now_cn.strftime('%Y-%m-%d %H:%M')}，对应美东时间约 {et_hour:02d}:{now_cn.minute:02d}，美股正处于**盘中交易时段**。
本报告是一份**盘中实时操作参考**。快照数据截至此时刻。**你只能分析从开盘到此刻已经发生的走势**。不要编造此刻之后的价格行为——还没发生的事你不知道。

## T-1 日完整分析报告（第 1-7 章）

{t1_report_content}

---

## 盘前分析报告（操作策略参考）

{f'''{pre_report_content}''' if pre_report_content else '''（未提供盘前报告）'''}

---

{snapshot_text}

{supplement}

---

## 你的任务

仅输出 **## 八、盘中操作参考（AI实时分析）** 这一章。你必须参考盘前报告的操作策略，结合实际盘中走势进行更新——不是推翻，是拿盘中数据修正盘前的计划。

{f'''**重要**：T-1 报告中已包含完整的 SWOT 分析和同板块关注章节，请在分析中引用其结论，结合最新参考数据做交叉印证。不要重复生成这些章节。'''
if t1_is_full else
f'''**重要**：T-1 报告为自动生成的基础版，缺少 SWOT 和同板块分析。请在本章内用一小节补充 SWOT 竞争分析（四象限简表）和同板块关注（排名简表），基于上面的最新参考数据。'''}

**核心分析框架**：T-1 日报告提供了经过严谨计算的 Alpha 得分、技术指标、回测结果——这是判断中长期方向的锚。盘中快照提供了实时价格位置和盘口数据——这是判断短期进出时机的关键。你的工作是**将两者交叉验证**，给出有数据支撑的盘中操作参考。

**重要提醒**：
- 你必须自己解读盘中原始数据。快照提供的是原始数值（价格、均线偏离、盘口买卖比、走势路径等），基于数据给出有洞察的解读，而不是简单复述数值。
- 每个结论必须有数据支撑，说清楚「因为什么数据等于多少，所以得出什么判断」
- 关键点位必须用当前实时价换算，精确到小数点后两位
- 如果盘中数据与 T-1 日判断出现矛盾，不要回避，要分析原因和两种可能性
- 如果快照中包含「盘前预测验证」段，这是三时段联动的核心——请解读盘前预测的准确性，并据此调整当前判断的置信度"""


# ============================================================
# 盘前分析 Prompt
# ============================================================

PREMARKET_SYSTEM_PROMPT = """你是一个专业的量化分析师，正在为一只美股生成**盘前策略参考**。

注意：以下提供了一份 T-1 日（上一个交易日）收盘后生成的完整分析报告（第 1-7 章），以及盘前原始数据（**纯数值，无任何预设解读**：期货报价、期货 K 线、盘前价格、成交量、距均线跳空幅度、隔夜新闻）。
你的任务是基于这些数据，**仅生成第八章「盘前策略参考（AI分析）」**，不要重复前面的章节。所有数据的含义需要你来判断，快照中的表格只提供数字不提供结论。

## 重要规则
1. **全部使用中文输出** — 如果原始数据中有英文内容，请翻译为中文，不得修改原意。
2. **严禁编造数据** — 所有分析必须基于我提供的数据。不要编造你没有数据的日期的事件（如今天还没发生的走势、跳空缺口等）。检查策略入场条件时（如涨跌幅、成交量），必须从技术面分析数据中找实际数值，找不到就写「数据未提供，无法判断」，不准编假数字。
3. **不可直接给出投资建议** — 用「建议关注」「可考虑」「观望」等表述，最后注明「以上分析仅供参考，不构成投资建议」。
4. **输出格式** — 只输出第八章的 Markdown 内容。开始于 `## 八、盘前策略参考（AI分析）`。
5. **每个结论都要有依据** — 对于每一个判断，必须说明是基于哪个数据的什么值推导出来的，不要只给结论。
6. **每个价位必须是精确数字，不是区间** — 入场价、止损价、止盈价、关键点位，全部给精确到小数点后两位的单一数字，不要给「$935-$950 区域」这种区间。每条价位后面注明它是怎么算出来的。

7. **禁忌词必须替换为数值描述** — 以下词汇禁止单独出现（必须写出数值标准）：「企稳」「放量」「缩量」「阳线」「阴线」「强势」「弱势」「恐慌性抛售」「探底回升」「回调到位」「确认支撑」。
8. **盘前数据时效声明** — 所有分析基于盘前数据，开盘后可能因流动性变化而改变。
9. **翻译系统操作方案** — prompt 中会包含「## 🎯 系统操作方案（代码生成）」章节（量化模型原始判断）。你的操作建议部分应输出保守和激进两套方案，把量化条件翻译成交易员能看懂的 K 线图语言。所有价位必须来自系统方案或技术指标，不得自编。
10. **严格区分止盈类型** — 固定止盈可展示目标价和传统风险收益比；动态/条件止盈只解释系统给出的公式或退出条件，并注明固定风险收益比不可量化。不得自行补造止盈价或评价风险收益比优秀。
11. **预测边界** — 只能引用「独立市场预测（代码生成）」的目标日期、概率和区间；不得自行外推或修改概率。风险退出、锁利和再平衡卖出不等于看空预测。

## 第八章结构要求

### 8.1 盘前多维分析

你必须以下面的格式输出：

**盘前多维信号交叉表**（必须用 Markdown 表格呈现）：

| 维度 | 信号及解读 | 方向一致性 |
|------|-----------|-----------|
| 期货风向标 | 解读 NQ 和 ES 期货涨跌幅的宏观含义——数值+解读，不能只罗列数值 | 1-2 句话说明期货走势偏多还是偏空 |
| 个股盘前价格 | 盘前涨跌幅的实际含义、与期货相对强弱的判断（独立走强 vs 被动跟随 vs 独立走弱） | 1-2 句话说明个股盘前的资金动向 |
| 盘前跳空 vs 均线 | 盘前价格距 T-1 日 MA5 的跳空幅度，需要解读这个幅度对开盘后走势的含义 | 1-2 句话说明跳空幅度的开盘含义 |
| 盘前成交量 | 盘前成交量的含义——量越大开盘方向可信度越高 | 1-2 句话说明量对方向信号的确认程度 |
| Alpha 因子（T-1日） | Final_Score + Rank IC 的含义 | 1-2 句话说明 T-1 因子方向与盘前信号的吻合程度 |
| 技术指标（T-1日） | MACD/RSI/布林/KDJ/均线排列状态 | 1-2 句话说明 T-1 技术面对今日操作的指导 |
| 隔夜新闻 | 隔夜重大新闻方向及可能对开盘的驱动程度 | 1-2 句话说明新闻面的方向 |
| 基本面估值（如有） | PE/PB、ROE 的含义 | 1-2 句话说明基本面安全边际 |
| 策略回测绩效（T-1日） | 哪种策略风格有效 | 1-2 句话说明今日适合的策略风格 |

**🎯 策略操作转化**（必须输出）：量化策略和人类策略各选最优的，共 2 个方案。量化→翻译成人能执行的条件+简化代价；人类→直接给今日执行方案（操作方向、价位、仓位、止损、卖出条件）。

**盘前综合评估**：3-5 句话总结：
- 期货 + 盘前价格 + T-1 技术框架的一致性如何？如果矛盾，矛盾点在哪里？
- 今日开盘最可能的方向和幅度预判
- 主要风险点（期货突然跳水、隔夜重大利空、盘前方向被开盘流动性冲散等）

### 8.2 今日操作策略（承上启下）

T-1 盘后报告的保守/激进方案是**计划，还没有执行**。现在新的盘前数据来了，你的任务是根据这些新数据更新计划：哪些条件已经满足、哪些需要调整、哪些不再成立。不是推翻重写，是拿新数据修正旧计划。

### 8.3 开盘情景推演

你必须基于当前数据，对三种开盘情景进行概率推演和应对预案。三种情景的概率之和应为 100%。每个情景的操作预案必须与上面 8.2 的操作策略保持一致，不能各说各的。

**情景判断依据**：综合期货涨跌幅 + 期货分时走势 + 个股盘前相对强弱 + T-1 技术框架 + 隔夜新闻方向，逐一评估每种情景的驱动因素。

**格式要求（三种情景都必须输出）：**

**情景一：高开（开盘价高于前收盘 1% 以上）**
- 概率：XX%（基于数据的估算，说明核心依据）
- 触发条件：哪些因素会导致高开
- 判断：是真强势还是情绪脉冲？依据是什么？
- **操作预案**（必须给保守+激进两套，与 8.2 策略保持一致）：
  · 🛡️ 保守：持仓者方案 + 空仓者方案（精确价位 + 理由）
  · 🚀 激进：持仓者方案 + 空仓者方案（精确价位 + 理由）
- 关键观察点

**情景二：平开（开盘价在前收盘 ±1% 以内）**
- 概率：XX%（同上格式）
- 触发条件：...
- 判断：...
- **操作预案**（同上，🛡️保守 + 🚀激进各一套）
- 关键观察点：...

**情景三：低开（开盘价低于前收盘 1% 以上）**
- 概率：XX%（同上格式）
- 触发条件：...
- 判断：...
- **操作预案**（同上，🛡️保守 + 🚀激进各一套）
- 关键观察点：...

### 8.3 今日关键点位

- 支撑位（3个，精确价格+来源标注+含义）
- 压力位（3个，精确价格+来源标注+含义）

### 8.4 今日操作策略

- **策略风格建议**：基于 T-1 回测绩效+盘前信号的综合判断
- **仓位建议**：轻仓/中等/重仓+理由
- **核心纪律**：2-3 条必须遵守的操作纪律

"""


def build_premarket_user_prompt(
    t1_report_content: str,
    snapshot_text: str,
    stock_info: dict,
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
) -> str:
    """构建盘前分析的 LLM user prompt。

    Args:
        t1_report_content: T-1 日完整报告的全文（第 1-7 章）
        snapshot_text:     盘前快照的 Markdown 文本
        stock_info:        股票基本信息字典
        swot_data:         实时 SWOT 素材（可选）
        peer_data:         同板块快速评分（可选）
    """
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")

    # ── 补充分析段（仅作为 AI 分析的输入素材，不独立成章）──
    supplement_parts: list[str] = []

    if swot_data:
        fin = swot_data.get("financial", {})
        val = swot_data.get("valuation", {})
        news_list = swot_data.get("news", [])
        swot_lines = [
            "",
            "## 最新 SWOT 参考数据（T-1 报告第五章已有完整 SWOT，此处为增量参考）",
            f"- ROE：{fin.get('roe', 0):.1%} | 毛利率：{fin.get('gross_margin', 0):.1%}（5Y均：{fin.get('gross_margin_5y', 0):.1%}）",
            f"- 净利润同比：{fin.get('net_profit_yoy', 0):+.1%}（5Y均：{fin.get('net_profit_yoy_5y', 0):+.1%}）| 营收同比：{fin.get('revenue_yoy', 0):+.1%}（5Y均：{fin.get('revenue_yoy_5y', 0):+.1%}）",
            f"- PE 分位：{val.get('pe_percentile', 0.5):.1%} | PB 分位：{val.get('pb_percentile', 0.5):.1%}",
        ]
        if news_list:
            swot_lines.append("- 近期新闻：" + "；".join(news_list[:3]))
        supplement_parts.append("\n".join(swot_lines))

    if peer_data:
        peer_lines = [
            "",
            "## 最新同板块参考数据（T-1 报告第九章已有完整分析，此处为增量参考）",
            "| 排名 | 代码 | Final_Score | 行情 | 建议 |",
            "|------|------|-------------|------|------|",
        ]
        for r in peer_data[:8]:
            peer_lines.append(
                f"| {r.get('rank', '-')} | {r['code']} "
                f"| {r.get('final_score', 0):+.3f} "
                f"| {r.get('regime', '?')} "
                f"| {r.get('verdict', '-')} |"
            )
        supplement_parts.append("\n".join(peer_lines))

    supplement = "\n".join(supplement_parts)

    # 检测 T-1 报告是否有完整章节
    t1_has_swot = "SWOT" in t1_report_content
    t1_has_peers = "同板块" in t1_report_content
    t1_is_full = t1_has_swot and t1_has_peers

    from datetime import datetime
    now_cn = datetime.now()
    et_hour = (now_cn.hour - 12) % 24

    return f"""请基于以下数据，为 {name}({code}) 生成盘前分析报告的第八章「盘前策略参考」。

## ⚠️ 当前时间与报告类型
现在是北京时间 {now_cn.strftime('%Y-%m-%d %H:%M')}，对应美东时间约 {et_hour:02d}:{now_cn.minute:02d}，美股处于**盘前时段，尚未开盘**。
本报告是一份**盘前策略参考**。数据是盘前数据（期货、盘前报价、隔夜新闻）。**你只能做情景推演和预案**。不要描述今日盘中走势——还没开盘、还没发生。不要编造任何今日开盘后的价格行为、跳空缺口、盘中走势。你只能基于盘前数据推测开盘方向，并用「如果高开...」「如果平开...」「如果低开...」三种情景来表达。

{t1_report_content}

---

{snapshot_text}

{supplement}

---

## 你的任务

仅输出 **## 八、盘前策略参考（AI分析）** 这一章。

{f'''**重要**：T-1 报告中已包含完整的 SWOT 分析和同板块关注章节，请在其结论基础上结合最新参考数据做交叉印证。不要重复生成。'''
if t1_is_full else
f'''**重要**：T-1 报告为自动生成的基础版，缺少 SWOT 和同板块分析。请在本章内补充 SWOT 竞争分析（四象限简表）和同板块关注（排名简表），基于上面的最新参考数据。'''}

**核心分析框架**：盘前分析的独特之处在于——K 线还没有走出来，你需要在信息不完整的情况下做情景推演。T-1 日报告提供了中长期趋势框架，期货和盘前数据提供了短期方向线索。你的工作是**把两者结合，推演三种开盘情景并给出具体应对预案**。

**重要提醒**：
- 你必须自己解读盘前原始数据。快照提供的是原始数值（期货涨跌幅、盘前价格、跳空幅度、成交量等），基于数据给出有洞察的解读，而不是简单复述数值。
- 情景推演是关键——必须覆盖高开/平开/低开三种情况，每种给出概率估算（XX%），三种概率之和应为 100%
- 概率估算需有数据支撑：期货涨跌幅 + 个股 vs 期货强弱 + 跳空幅度 + 成交量
- 每个情景下的操作建议要具体到价位和条件，不要泛泛而谈
- 期货走势是判断开盘方向最重要的输入，必须重点解读并说明依据
- 盘前价格与期货的相对强弱关系，是判断个股是否有独立资金行为的关键"""


# ============================================================
#  持仓综合分析（我的持仓页面）
# ============================================================

PORTFOLIO_SYSTEM_PROMPT = """你是一个专业的全持仓分析师和资产配置顾问。请基于下面提供的**真实数据**，为用户生成一份全面的持仓综合分析报告。

你的报告中会**前置**一段「🎯 组合操作方案（代码生成）」——这是量化系统基于策略审计、实时信号和组合风控自动计算出来的，其中可能包含「条件触发交易计划」。**你的任务不是重写或替代这个方案，而是翻译它**：把量化语言变成 K 线图语言，解释为什么系统给出这些买入/卖出/持有触发条件、保守和激进方案的差异在哪里、执行时要注意什么风险。

## 重要规则
1. **全部使用中文输出** — 如果原始数据中有英文内容，请准确翻译为中文呈现，不得修改原意。
2. **严禁编造数据** — 所有分析必须基于我提供的数据。每只股票的每个数值（涨跌幅、技术指标、回测收益等）都必须来自我提供的数据。找不到就写「数据未提供，无法判断」，不准编假数字。
3. **每个结论必须有依据** — 不得只给光秃秃的结论。每个判断、每个操作建议，都必须附上分析过程和理由。
4. **不可直接给出投资建议** — 报告中用「建议关注」「可考虑」等表述，最后必须注明「以上分析仅供参考，不构成投资建议」。
5. **报告格式** — 使用 Markdown 格式输出。
6. **每个价位必须是精确数字，不是区间** — 入场价、止损价、止盈价、关键点位，全部给精确到小数点后两位的单一数字。每条价位后面注明它是怎么算出来的。
7. **禁忌词必须替换为数值描述** — 以下词汇禁止单独出现（必须写出数值标准）：「企稳」「放量」「缩量」「阳线」「阴线」「强势」「弱势」「恐慌性抛售」「探底回升」「回调到位」「确认支撑」。例如不能说「放量阳线企稳后入场」，必须说「当日涨幅 +2.3%、成交量较前5日均量放大40%、价格连续3日未创新低后，在 $935.01 入场」。
8. **⚠️ 不要自己写调仓方案** — 系统已生成了「🎯 组合操作方案（代码生成）」，包含每只股票的保守/激进策略、关键价位、触发条件。你的第 5 章是**翻译解读**：用通俗语言解释系统方案，帮助用户理解哪些条件触发买入、哪些条件触发卖出/减仓、哪些条件维持持有。严禁新增系统方案中没有的交易动作、股数、入场价、止损价、调仓比例或调整后持仓结构。
9. **执行表限制** — 只有当系统方案明确给出买入/卖出/减仓动作和股数时，才允许输出「调整后持仓结构」表格。没有明确动作时，只能输出「待观察清单」和「触发后再执行」，不得自行假设清仓、减仓 50%、买入几股等。
10. **研究员观察候选** — 你可以在报告末尾提出“观察候选”，但这些不是交易指令。必须使用固定标题 `### 研究员观察候选`，并输出 Markdown 表格，表头必须是：`| 股票 | LLM观察 | 依据 |`。每条观察只描述值得系统复核的事实或机会，不写买入股数/卖出股数/调仓比例。
11. **历史回测不是资产质量排名** — 横向表只表示某策略在该股票历史区间的拟合表现。严禁仅因最高历史收益/夏普就称某股票为“最优质资产”或推导当前持有结论；当前操作必须服从数据质量、持仓风控和当前信号共识。如二者冲突，必须明确写成“历史策略适配较好，但当前条件转弱/存在分歧”。
12. **风险收益比必须可计算** — 只有系统同时提供有效入场价、止损价和固定止盈价时，才能评价传统风险收益比。若系统采用动态止盈或条件退出，必须解释真实规则并注明“固定风险收益比不可量化”；若没有主动止盈，则明确说明仅有止损/时间退出。严禁使用“风险收益比/风险回报比极佳、优秀、有吸引力”等无数据判断。止损距离较近不等于风险收益比优秀。
13. **预测边界** — 组合中的未来判断只能逐股引用「组合独立市场预测（代码生成）」的目标日期、概率和区间。不得把买入、止损、锁利、减仓或调仓动作反推成市场预测，也不得自行修改概率。

## 报告结构
1. **账户概览** — 账户总资产（现金 + 持仓市值）、各市场持仓结构、行业分布、现金比例、集中度风险
2. **持仓个股逐只分析** — 每只持仓股：当前盈亏状态（从成本价算起）、技术面快照、行情状态（震荡/趋势）、因子有效性（Rank IC）、基本面估值（PE/PB/ROE）、最佳回测策略、与关注股的横向对比、持有/减仓/清仓建议及理由。**必须交叉印证：技术面、基本面、回测绩效、因子有效性四个维度，任一维度矛盾时须明确指出来。**
3. **关注股票逐只分析** — 每只关注股：技术面快照、行情状态、因子有效性、基本面估值、最佳回测策略、与当前持仓的横向对比、是否值得买入及理由。**判断逻辑与持仓股一致，确保每只股票的分析深度相同。**
4. **历史策略适配横向对照** — 展示各股票历史上风险调整后表现较好的策略，但明确这不是资产质量或当前买卖排名
5. **综合调仓方案解读** — 翻译解读系统已生成的「🎯 组合操作方案（代码生成）」：
    - **不要复述系统方案的内容**（用户已经看到了），直接给解读。
    - 🛡️ 保守方案解读：系统为什么选这些策略？保守方案的逻辑是什么？执行时要注意什么风险？等不到怎么办？
    - 🚀 激进方案解读：系统为什么选这些策略？激进方案的逻辑是什么？比保守多承担了什么风险？错了亏多少？
    - 系统方案中每只股票的价位和策略，解释它们为什么合理（或不合理）——你有权基于自己的分析提出质疑，但必须标注「系统方案建议 X，我的判断是 Y，因为 Z」
    - 如果系统方案只有「持仓风控提示」或「无买入信号」，你只能解释风险来源和后续观察条件，不得扩展成具体清仓/买入几股的执行方案。
    - **⚠️ 自检规则**：完成解读后，逐只对照第二章和第三章中给出的操作建议，检查你的解读是否与前面的分析一致。如有不一致，必须明确解释原因。
6. **研究员观察候选** — 只提出值得系统复核的候选观察；后续代码系统会生成「研究员观察 vs 系统确认」章节，对这些观察确认、降级或驳回。
"""



def build_portfolio_user_prompt(
    balance: dict,
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    period: str,
    mode: str = "eod",
) -> str:
    """构建持仓综合分析的用户提示词。

    Args:
        balance: {"us_balance": float, "a_balance": float}
        holdings_data: 每只持仓的完整分析数据列表，每个元素包含：
            - holding: Holding dataclass
            - current_price: float | None (最新价格)
            - price_date: str (价格对应的日期)
            - price_source: str (价格来源说明，如"K线收盘价（2026-06-17）"或"实时报价（2026-06-18 14:30:00）")
            - technical: str (技术面摘要)
            - backtest: dict (回测结果)
            - alpha_score: float | None
            - news_summary: str (新闻情感摘要)
            - market_regime: str (行情状态中文标签)
            - rank_ic_info: str (因子有效性 IC/IR)
            - fund_info: str (基本面 PE/PB/ROE/毛利率)
            - benchmark_return: float (买入持有基准收益)
            - regime_adapt_info: str (策略适配信息)
        watchlist_data: 每只关注股的完整分析数据，结构同上（无 holding 字段，用 watch_item 替代）
        market: "US" | "A"
        period: 回测周期
        mode: 分析模式
    """
    market_label = "美股" if market == "US" else "A股"
    currency = "$" if market == "US" else "¥"

    # ── 账户余额 ──
    if market == "US":
        cash = balance.get("us_balance", 0)
    else:
        cash = balance.get("a_balance", 0)

    lines = [
        f"## 当前时间与报告类型",
        f"这是一份**{market_label}持仓综合分析报告**（{period} 回测周期，分析模式={mode}）。",
        f"请基于以下所有数据，输出完整报告。",
        "",
        f"## 账户资金",
        f"- {market_label}可用资金：**{currency}{cash:,.2f}**",
        "",
    ]

    # ── 当前持仓 ──
    lines.append(f"## {market_label}当前持仓")
    lines.append("以下是用户当前持有的股票：")
    # 收集所有价格来源以便在下方标注
    price_sources = set()
    total_cost = 0.0
    total_market_value = 0.0
    for i, hd in enumerate(holdings_data, 1):
        h = hd["holding"]
        cp = hd.get("current_price")
        ps = hd.get("price_source", "K线收盘价")
        price_sources.add(ps)
        cost = h.shares * h.cost_price
        total_cost += cost
        pnl_str = ""
        market_value = 0
        if cp and cp > 0:
            market_value = h.shares * cp
            total_market_value += market_value
            pnl = (cp - h.cost_price) / h.cost_price
            pnl_str = f" | 现价={currency}{cp:.2f} | 市值={currency}{market_value:,.2f} | 浮盈/亏={pnl:+.2%} | 价格来源：{ps}"
        else:
            pnl_str = " | 现价=数据未获取 | 浮盈/亏=无法计算"

        lines.append(
            f"{i}. **{h.code} {h.name}** | 市场={market_label} | "
            f"持有 {h.shares:,.0f} 股 | 成本价={currency}{h.cost_price:.2f}"
            f"{pnl_str}"
        )

    lines.append(f"\n持仓总成本：{currency}{total_cost:,.2f}")
    if total_market_value > 0:
        lines.append(f"持仓总市值：{currency}{total_market_value:,.2f}")
        total_pnl = (total_market_value - total_cost) / total_cost if total_cost > 0 else 0
        lines.append(f"账户总资产（现金+持仓）：{currency}{cash + total_market_value:,.2f}")
        lines.append(f"整体浮盈/亏：{total_pnl:+.2%}")
    # 标注价格来源汇总
    if price_sources:
        lines.append(f"⚠️ 价格数据来源：{'、'.join(price_sources)}")
    lines.append("")

    # ── 持仓个股详细数据 ──
    lines.append("---")
    lines.append("## 持仓个股详细分析数据")
    for hd in holdings_data:
        h = hd["holding"]
        lines.append(f"### {h.code} {h.name}（持仓）")
        lines.append(f"- 持有数量：{h.shares:,.0f} 股")
        lines.append(f"- 成本价：{currency}{h.cost_price:.2f}")
        if hd.get("current_price"):
            cp = hd["current_price"]
            pnl = (cp - h.cost_price) / h.cost_price
            ps = hd.get("price_source", "K线收盘价")
            lines.append(f"- 当前价：{currency}{cp:.2f}（浮盈/亏 {pnl:+.2%}）| 价格来源：{ps}")
        if hd.get("alpha_score") is not None:
            lines.append(f"- Alpha Final_Score：{hd['alpha_score']:+.3f}")
        if hd.get("market_regime"):
            lines.append(f"- 行情状态：{hd['market_regime']}")
        if hd.get("rank_ic_info"):
            lines.append(f"- 因子有效性：{hd['rank_ic_info']}")
        if hd.get("fund_info"):
            lines.append(f"- {hd['fund_info']}")
        if hd.get("benchmark_return") is not None and hd["benchmark_return"] != 0:
            lines.append(f"- 买入持有基准收益：{hd['benchmark_return']*100:+.2f}%")
        if hd.get("regime_adapt_info"):
            lines.append(f"- 策略适配：{hd['regime_adapt_info']}")
        if hd.get("technical"):
            lines.append(f"- 技术面摘要：\n{hd['technical']}")
        if hd.get("backtest"):
            bt = hd["backtest"]
            lines.append("- 策略回测绩效（按验证质量仅保留前3项）：")
            ranked_bt = sorted(
                bt.items(),
                key=lambda item: (item[1].sharpe_ratio, item[1].total_return),
                reverse=True,
            )[:3]
            for strat_name, result in ranked_bt:
                lines.append(
                    f"  - {strat_name}：总收益={result.total_return*100:+.2f}% | "
                    f"年化={result.annual_return*100:+.2f}% | "
                    f"最大回撤={result.max_drawdown*100:.2f}% | "
                    f"夏普={result.sharpe_ratio:.2f} | 交易{result.total_trades}次"
                )
        if hd.get("news_summary"):
            lines.append(f"- 新闻情感摘要：{hd['news_summary']}")
        # ── 新架构：系统信号 + 审计数据 ──
        sc = hd.get("signal_check") or []
        buy_sc = [s for s in sc if s.get("signal") == "buy"]
        if buy_sc:
            strat_names = ", ".join(s.get("name", "?")[:15] for s in buy_sc[:3])
            lines.append(f"- 🔴 系统买入信号：**{len(buy_sc)} 个策略触发** → {strat_names}")
        else:
            lines.append(f"- ⚪ 系统买入信号：**0 个策略触发**（所有策略均不满足入场条件）")
        audit = hd.get("strategy_audit") or {}
        if audit:
            lines.append(f"- 策略审计：PASS={audit.get('pass', 0)}, COND={audit.get('conditional', 0)}, FAIL={audit.get('fail', 0)}")
        lines.append("")

    # ── 关注股票详细数据 ──
    if watchlist_data:
        lines.append("---")
        lines.append("## 关注股票详细分析数据")
        for wd in watchlist_data:
            w = wd["watch_item"]
            lines.append(f"### {w.code} {w.name}（关注）")
            lines.append(f"- 市场：{market_label}")
            if wd.get("current_price"):
                ps = wd.get("price_source", "K线收盘价")
                lines.append(f"- 当前价：{currency}{wd['current_price']:.2f} | 价格来源：{ps}")
            if wd.get("alpha_score") is not None:
                lines.append(f"- Alpha Final_Score：{wd['alpha_score']:+.3f}")
            if wd.get("market_regime"):
                lines.append(f"- 行情状态：{wd['market_regime']}")
            if wd.get("rank_ic_info"):
                lines.append(f"- 因子有效性：{wd['rank_ic_info']}")
            if wd.get("fund_info"):
                lines.append(f"- {wd['fund_info']}")
            if wd.get("benchmark_return") is not None and wd["benchmark_return"] != 0:
                lines.append(f"- 买入持有基准收益：{wd['benchmark_return']*100:+.2f}%")
            if wd.get("regime_adapt_info"):
                lines.append(f"- 策略适配：{wd['regime_adapt_info']}")
            if wd.get("technical"):
                lines.append(f"- 技术面摘要：\n{wd['technical']}")
            if wd.get("backtest"):
                bt = wd["backtest"]
                lines.append("- 策略回测绩效（按验证质量仅保留前3项）：")
                ranked_bt = sorted(
                    bt.items(),
                    key=lambda item: (item[1].sharpe_ratio, item[1].total_return),
                    reverse=True,
                )[:3]
                for strat_name, result in ranked_bt:
                    lines.append(
                        f"  - {strat_name}：总收益={result.total_return*100:+.2f}% | "
                        f"年化={result.annual_return*100:+.2f}% | "
                        f"最大回撤={result.max_drawdown*100:.2f}% | "
                        f"夏普={result.sharpe_ratio:.2f} | 交易{result.total_trades}次"
                    )
            if wd.get("news_summary"):
                lines.append(f"- 新闻情感摘要：{wd['news_summary']}")
            # ── 新架构：系统信号 + 审计数据 ──
            sc = wd.get("signal_check") or []
            buy_sc = [s for s in sc if s.get("signal") == "buy"]
            if buy_sc:
                strat_names = ", ".join(s.get("name", "?")[:15] for s in buy_sc[:3])
                lines.append(f"- 🔴 系统买入信号：**{len(buy_sc)} 个策略触发** → {strat_names}")
            else:
                lines.append(f"- ⚪ 系统买入信号：**0 个策略触发**")
            audit = wd.get("strategy_audit") or {}
            if audit:
                lines.append(f"- 策略审计：PASS={audit.get('pass', 0)}, COND={audit.get('conditional', 0)}, FAIL={audit.get('fail', 0)}")
            lines.append("")

    # ── 横向对比数据：历史模型适配，不作为当前资产质量排名 ──
    lines.append("---")
    lines.append("## 历史策略适配横向对照（非资产质量排名）")
    lines.append(
        "> 该表仅比较历史回测中的风险调整后表现。当前动作以数据质量、"
        "持仓风控和当前信号共识为准；不得由本表单独推导买卖。"
    )
    all_stocks = []
    for item, obj, item_type in [
        *[(hd, hd["holding"], "持仓") for hd in holdings_data],
        *[(wd, wd["watch_item"], "关注") for wd in watchlist_data],
    ]:
        backtests = item.get("backtest") or {}
        best_name, best_result = max(
            backtests.items(),
            key=lambda pair: (
                float(pair[1].sharpe_ratio),
                float(pair[1].total_return),
                -float(pair[1].max_drawdown),
            ),
            default=("—", None),
        )
        signals = item.get("signal_check") or []
        raw_sells = [s for s in signals if s.get("signal") == "sell"]
        from core.signal_check import select_actionable_sell_signals
        if select_actionable_sell_signals(raw_sells):
            current_state = "退出/减仓共识"
        elif raw_sells:
            current_state = "单策略退出分歧"
        elif any(s.get("signal") == "buy" for s in signals):
            current_state = "当前有买入信号"
        else:
            current_state = "持有/观察"
        all_stocks.append({
            "code": obj.code,
            "name": obj.name,
            "type": item_type,
            "alpha_score": item.get("alpha_score"),
            "strategy": best_name,
            "best_return": float(best_result.total_return) if best_result else 0.0,
            "best_sharpe": float(best_result.sharpe_ratio) if best_result else 0.0,
            "best_drawdown": float(best_result.max_drawdown) if best_result else 0.0,
            "current_state": current_state,
        })

    lines.append("| 类型 | 代码 | 名称 | 当前状态 | Alpha | 历史适配策略 | 收益 | 夏普 | 回撤 |")
    lines.append("|------|------|------|------|------:|------|------:|------:|------:|")
    for s in all_stocks:
        score_str = f"{s['alpha_score']:+.3f}" if s['alpha_score'] is not None else "N/A"
        lines.append(
            f"| {s['type']} | {s['code']} | {s['name']} | {s['current_state']} | "
            f"{score_str} | {s['strategy']} | {s['best_return']*100:+.2f}% | "
            f"{s['best_sharpe']:.2f} | {s['best_drawdown']*100:.2f}% |"
        )

    lines.append("")
    lines.append("---")
    lines.append("## 你的任务")
    lines.append(
        f"请基于以上全部真实数据，生成完整的{market_label}持仓综合分析报告（严格按照 SYSTEM_PROMPT 规定的结构输出）。"
    )
    lines.append("")
    lines.append("**核心要点**：")
    lines.append(
        "- 第 5 章只能翻译代码生成的组合操作方案；不得新增系统没有确认的买卖动作、股数或价格。"
    )
    lines.append(
        f"- 用户有 {currency}{cash:,.2f} 可用资金。每笔买入必须说明资金来源。"
    )
    lines.append(
        "- 持仓卖出/减仓必须由系统当前退出共识或持仓风控支持。历史回测可解释背景，"
        "但不能单独触发当前卖出，也不能仅凭某关注股历史收益更高就建议替换。"
    )
    lines.append(
        "- 关注股只有在当前系统买入信号、账户容量和数据质量同时允许时才能讨论执行；"
        "历史回测较好但当前未触发时只能列为观察。"
    )
    lines.append(
        "- 保守/激进差异必须服从系统方案。若二者来自同一触发策略，价格和止损可以相同，"
        "此时只解释仓位、账户风险预算和最大亏损的差异，不虚构第二套价位。"
    )
    lines.append(
        "- 系统只有固定止盈目标时才能计算传统风险收益比；动态止盈/条件退出必须解释规则并注明"
        "固定风险收益比不可量化，不得把止损距离较近解释为风险收益比优秀。"
    )

    return "\n".join(lines)
