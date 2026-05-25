"""
报告生成模块

负责将各分析模块的输出整合为完整的分析报告。

两种生成方式：
  1. LLM 生成（推荐）：
     - 调用 OpenAI 兼容 API，将技术面/新闻面/回测数据传给大模型
     - 通过 SYSTEM_PROMPT 约束模型仅基于提供数据分析，避免幻觉
     - 输出结构化 Markdown 报告

  2. 回退生成（无 API 时）：
     - 使用 Python 字符串模板拼接各模块结果
     - 生成格式统一的 Markdown 报告
     - 保证在大模型不可用时功能仍正常

【扩展点】自定义报告模板：
  1. 修改 SYSTEM_PROMPT 调整 LLM 的报告风格和格式要求
  2. 修改 user_prompt 增减传给 LLM 的数据维度
  3. 修改 _generate_fallback_report 调整回退报告的排版
  4. 添加多语言支持（英文报告模板）
"""

import logging
from datetime import datetime

from config.settings import Settings

logger = logging.getLogger(__name__)


# ======================== LLM 系统提示词 ========================

SYSTEM_PROMPT = """你是一个专业的股票分析师。请基于下面提供的**真实数据**，生成一份客观、专业的中文股票分析报告。

## 重要规则
1. **全部使用中文输出** — 报告内容必须全部是中文。如果原始数据中有英文内容（如公司简介、新闻标题），请翻译为中文后呈现。
2. **严禁编造数据** — 所有分析必须基于我提供的数据，不得添加未提及的数字或事实。
3. **发现不可直接给出投资建议** — 报告中用「买入信号/卖出信号」「建议关注/观望」等表述，最后必须注明「以上分析仅供参考，不构成投资建议」。
4. **报告格式** — 使用 Markdown 格式输出。

## 报告结构
1. **股票简介** — 公司名称、代码、所属行业、主营业务简介（英文内容请翻译为中文）
2. **近期 K 线走势** — 近两周价格走势描述（基于价格数据）
3. **技术面分析** — 基于提供的技术指标数据做分析
4. **新闻面分析** — 基于新闻情感分析结果，评估市场情绪（英文新闻标题请翻译为中文概括）
5. **回测结果** — 回测策略表现，包括收益率、最大回撤等
6. **综合建议** — 结合以上分析，给出明确的短期操作建议（买入/卖出/观望）及理由
"""

# 回退模板（LLM 不可用时使用）
_FALLBACK_TEMPLATE = """# {name}（{code}）分析报告

> 生成时间：{now}
> 市场：{market_name} | 行业：{industry}

---

## 一、股票简介

{description}

---

## 二、技术面分析

{technical_summary}

---

## 三、新闻面分析

{news_text}

重点新闻：
{top_news}

---

## 四、回测结果

{trade_summary}

---

## 五、综合建议

**{recommendation}**

---

> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。
"""


def generate_report(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    backtest_result: dict,
    chart_path: str = "",
) -> str:
    """
    生成完整分析报告。

    决策流程：
      - 如果配置了 LLM API Key → 调用大模型生成报告
      - 如果 API Key 为空或调用失败 → 使用回退模板生成报告

    Args:
        stock_info:        股票基本信息字典（StockInfo.to_dict()）
        technical_summary: 技术面分析摘要（Markdown 文本）
        news_aggregation:  新闻情感汇总字典（sentiment.aggregate() 输出）
        backtest_result:   回测结果字典（BacktestEngine.run() 输出）
        chart_path:        K 线图文件路径（可选，用于报告引用）

    Returns:
        Markdown 格式的完整分析报告
    """
    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")

    # 本地模型（Ollama 等）允许空 API Key
    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation, backtest_result
        )

    # 构建 LLM 提示词
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    trade_summary = backtest_result.get("trade_summary", "")
    recommendation = backtest_result.get("recommendation", "")
    user_prompt = f"""请用中文分析以下股票 {stock_info.get('name', '')}({stock_info.get('code', '')})，所有内容必须用中文呈现：

## 股票基本信息
- 名称：{stock_info.get('name', '')}
- 代码：{stock_info.get('code', '')}
- 市场：{stock_info.get('market', '')}
- 行业：{stock_info.get('industry', '')}
- 简介（英文原文，请翻译为中文）：{stock_info.get('description', '')}

## 技术面分析数据
{technical_summary}

## 新闻情感分析
{news_text}

重点新闻（英文标题请翻译为中文概括）：
{top_news}

## 回测结果
{trade_summary}

回测建议：{recommendation}

请按照要求的报告结构生成完整中文分析报告。"""

    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    # 尝试调用 LLM API
    try:
        # Ollama 原生 API（localhost:11434 时使用，更可靠）
        if "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
            import requests
            logger.info(f"Calling Ollama native API: model={model}")
            resp = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": model, "prompt": full_prompt, "stream": False},
                timeout=600,
            )
            data = resp.json()
            report = data.get("response", "")
            if not report:
                logger.warning(f"Ollama returned empty (done_reason={data.get('done_reason')})")
                return _generate_fallback_report(stock_info, technical_summary, news_aggregation, backtest_result)
            logger.info(f"LLM report generated: {len(report)} chars")
            return report

        # OpenAI 兼容 API（远程模型）
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)
        logger.info(f"Calling OpenAI-compatible API: model={model}")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        choice = response.choices[0]
        report = choice.message.content
        logger.info(f"LLM response: finish_reason={choice.finish_reason}, content_len={len(report) if report else 0}")
        if not report:
            logger.warning(f"LLM returned empty response (finish_reason={choice.finish_reason}), falling back to template")
            return _generate_fallback_report(
                stock_info, technical_summary, news_aggregation, backtest_result
            )
        logger.info("Report generated by LLM")
        return report

    except Exception as e:
        # LLM 调用失败 → 降级为回退报告
        logger.error(f"LLM report generation failed: {e}")
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation, backtest_result
        )


def _generate_fallback_report(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    backtest_result: dict,
) -> str:
    """
    生成回退报告（不依赖 LLM 的模板拼接方案）。

    使用 Python f-string 模板将各模块输出直接拼接为 Markdown 报告。
    结构清晰、格式统一，包含免责声明。

    Args:
        与 generate_report() 相同

    Returns:
        Markdown 格式的分析报告
    """
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    market = stock_info.get("market", "")
    industry = stock_info.get("industry", "")
    description = stock_info.get("description", "")

    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    trade_summary = backtest_result.get("trade_summary", "")
    recommendation = backtest_result.get("recommendation", "")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    market_name = "A股" if market == "A" else "美股"

    # 拼接报告（Markdown 模板）
    report = f"""# {name}（{code}）分析报告

> 生成时间：{now}
> 市场：{market_name} | 行业：{industry}

---

## 一、股票简介

{description if description else '暂无公司简介信息。'}

---

## 二、技术面分析

{technical_summary}

---

## 三、新闻面分析

{news_text}

重点新闻：
{top_news}

---

## 四、回测结果

{trade_summary}

---

## 五、综合建议

**{recommendation}**

---

> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。请结合自身风险承受能力做出投资决策。
"""
    logger.info("Report generated by fallback (no LLM)")
    return report
