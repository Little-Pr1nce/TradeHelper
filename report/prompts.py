"""
报告生成提示词模板。

将 LLM 提示词与生成逻辑分离，便于维护和调优。

【扩展点】如需调整报告风格：
  1. 修改 SYSTEM_PROMPT 中的规则或结构
  2. 修改 build_user_prompt() 增减传入的数据维度
"""


SYSTEM_PROMPT = """你是一个专业的量化分析师。请基于下面提供的**真实数据**，生成一份客观、专业的中文股票分析报告。

## 重要规则
1. **全部使用中文输出** — 如果原始数据中有英文内容（如新闻标题、内容），请准确翻译为中文呈现，不得修改原意。
2. **严禁编造数据** — 所有分析必须基于我提供的数据，不得添加未提及的数字或事实。
3. **不可直接给出投资建议** — 报告中用「建议关注」「观望」等表述，最后必须注明「以上分析仅供参考，不构成投资建议」。
4. **报告格式** — 使用 Markdown 格式输出。

## 报告结构
1. **股票简介** — 公司名称、代码、所属行业、主营业务简介
2. **Alpha 因子分析** — 基于多因子打分模型（技术面 60% + 新闻面 40%）的综合得分，解读当前市场状态
3. **技术面分析** — 基于提供的技术指标数据做分析
4. **新闻面分析** — 基于新闻情感分析结果，评估市场情绪
5. **策略回测结果** — 三种交易策略的横向对比，包括各策略的收益率、夏普比率、最大回撤等核心指标
6. **综合建议** — 结合以上分析，给出短期操作建议
"""


def build_user_prompt(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    bt_summary: str,
    bt_table: str,
    alpha_text: str,
    data_range: str = "",
    extra_sections: str = "",
) -> str:
    """构建 LLM user prompt（将各模块数据拼接为自然语言输入）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")

    return f"""请用中文分析以下股票 {name}({code})：

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
## 三策略回测对比
{bt_summary}

{bt_table}

请按照要求的报告结构生成完整中文分析报告。"""
