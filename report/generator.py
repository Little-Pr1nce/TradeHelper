"""
报告生成模块

负责将各分析模块的输出整合为完整的分析报告。

两种生成方式：
  1. LLM 生成（推荐）：
     - 调用 OpenAI 兼容 API，将技术面/新闻面/Alpha因子/多策略回测数据传给大模型
     - 通过 SYSTEM_PROMPT 约束模型仅基于提供数据分析
     - 输出结构化 Markdown 报告

  2. 回退生成（无 API 时）：
     - 使用 Python 字符串模板拼接各模块结果
     - 保证在大模型不可用时功能仍正常
"""

import logging
from datetime import datetime

from config.settings import Settings
from report.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


def _build_backtest_summary(bt_results: dict) -> str:
    """将多策略回测结果格式化为可读文本（供 LLM 和回退模板使用）。"""
    if not bt_results:
        return "回测数据不可用。"

    lines = []
    for name, r in bt_results.items():
        lines.append(
            f"- **{name}**: 总收益 {r.total_return*100:+.2f}%, "
            f"年化 {r.annual_return*100:+.2f}%, "
            f"最大回撤 {r.max_drawdown*100:.2f}%, "
            f"夏普比率 {r.sharpe_ratio:.2f}, "
            f"胜率 {r.win_rate*100:.0f}%, "
            f"交易 {r.total_trades} 次"
        )
    return "\n".join(lines)


def _build_backtest_markdown_table(bt_results: dict) -> str:
    """生成 Markdown 格式的策略对比表。"""
    if not bt_results:
        return ""

    header = "| 策略 | 总收益 | 年化收益 | 最大回撤 | 夏普比率 | Calmar | 胜率 | 交易次数 |"
    sep = "|------|--------|----------|----------|----------|--------|------|----------|"
    rows = []
    for name, r in bt_results.items():
        rows.append(
            f"| {name} | {r.total_return*100:+.2f}% | {r.annual_return*100:+.2f}% | "
            f"{r.max_drawdown*100:.2f}% | {r.sharpe_ratio:.2f} | {r.calmar_ratio:.2f} | "
            f"{r.win_rate*100:.0f}% | {r.total_trades} |"
        )
    return "\n".join([header, sep] + rows)


def generate_report(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    backtest_results: dict,
    alpha_stats: dict | None = None,
) -> str:
    """
    生成完整分析报告。

    Args:
        stock_info:         股票基本信息字典
        technical_summary:  技术面分析摘要（Markdown）
        news_aggregation:   新闻情感汇总字典
        backtest_results:   多策略回测结果 dict（key=策略名, value=BacktestResult）
        alpha_stats:        Alpha 因子得分统计（mean/std/latest）
    """
    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")

    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats,
        )

    # 构建 LLM 提示词
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    bt_summary = _build_backtest_summary(backtest_results)
    bt_table = _build_backtest_markdown_table(backtest_results)

    alpha_text = ""
    if alpha_stats:
        alpha_text = (
            f"最新 Final_Score: {alpha_stats.get('latest', 0):.3f}\n"
            f"回测期内均值: {alpha_stats.get('mean', 0):.3f}\n"
            f"标准差: {alpha_stats.get('std', 0):.3f}\n"
            f"注：Final_Score ∈ [-1, +1]，正值偏多，负值偏空。"
        )

    user_prompt = build_user_prompt(
        stock_info, technical_summary, news_aggregation,
        bt_summary, bt_table, alpha_text,
    )

    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    # 尝试调用 LLM API
    try:
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
                logger.warning(f"Ollama returned empty")
                return _generate_fallback_report(
                    stock_info, technical_summary, news_aggregation,
                    backtest_results, alpha_stats,
                )
            logger.info(f"LLM report generated: {len(report)} chars")
            return report

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
        if not report:
            logger.warning("LLM returned empty response, falling back to template")
            return _generate_fallback_report(
                stock_info, technical_summary, news_aggregation,
                backtest_results, alpha_stats,
            )
        logger.info("Report generated by LLM")
        return report

    except Exception as e:
        logger.error(f"LLM report generation failed: {e}")
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats,
        )


def _generate_fallback_report(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    backtest_results: dict,
    alpha_stats: dict | None = None,
) -> str:
    """生成回退报告（不依赖 LLM 的模板拼接方案）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    market = stock_info.get("market", "")
    industry = stock_info.get("industry", "")
    description = stock_info.get("description", "")

    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    market_name = "A股" if market == "A" else "美股"

    # Alpha 因子得分
    alpha_str = ""
    if alpha_stats:
        alpha_str = (
            f"\n### 因子得分统计\n"
            f"- 最新 Final_Score: **{alpha_stats.get('latest', 0):.3f}**\n"
            f"- 回测期均值: {alpha_stats.get('mean', 0):.3f}\n"
            f"- 标准差: {alpha_stats.get('std', 0):.3f}\n"
            f"- 得分范围: [-1, +1]，正值偏多，负值偏空\n"
        )

    # 策略对比表
    bt_table = _build_backtest_markdown_table(backtest_results)
    bt_summary = _build_backtest_summary(backtest_results)

    # 综合建议
    recommendation = _derive_recommendation(backtest_results, alpha_stats)

    report = f"""# {name}（{code}）分析报告

> 生成时间：{now}
> 市场：{market_name} | 行业：{industry}

---

## 一、股票简介

{description if description else '暂无公司简介信息。'}

---

## 二、Alpha 因子分析（多因子量化模型）

本报告采用多因子打分模型，综合技术面（权重 60%）与新闻情感面（权重 40%），
通过 Z-Score 标准化和 tanh 映射合成 Final_Score ∈ [-1, +1]。

{alpha_str}

---

## 三、技术面分析

{technical_summary}

---

## 四、新闻面分析

{news_text}

重点新闻：
{top_news}

---

## 五、策略回测结果

系统运行了三种量化交易策略进行回测对比：

{bt_table}

{bt_summary}

---

## 六、综合建议

**{recommendation}**

---

> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。
"""
    logger.info("Report generated by fallback template")
    return report


def _derive_recommendation(backtest_results: dict, alpha_stats: dict | None) -> str:
    """基于回测结果和因子得分推导操作建议。"""
    if not backtest_results:
        return "数据不足，建议观望。"

    # 统计正收益策略数量
    positive = sum(1 for r in backtest_results.values() if r.total_return > 0)
    total = len(backtest_results)

    latest_score = alpha_stats.get("latest", 0) if alpha_stats else 0

    if positive == total and latest_score > 0.3:
        return "多个策略回测表现正向，当前 Alpha 因子偏多，建议关注买入机会。"
    elif positive >= total / 2:
        return "部分策略表现正向，建议谨慎关注，等待更明确的趋势信号。"
    elif latest_score < -0.3:
        return "当前 Alpha 因子偏空，多数策略表现不佳，建议观望或减仓。"
    else:
        return "策略表现分化，建议观望，等待趋势明朗后再做决策。"
