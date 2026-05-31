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
import re
from datetime import datetime

from config.settings import Settings
from report.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


def _clean_llm_output(text: str) -> str:
    """清理 LLM 输出中的特殊 token 和残余指令。"""
    # 先移除整段残余指令
    text = re.sub(r"<\|im_start\|>.*", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|im_end\|>", "", text)
    text = re.sub(r"<\|endoftext\|>", "", text)
    # 再清理其他特殊 token
    text = re.sub(r"<\|[^|]+\|>", "", text)
    return text.strip()


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
    data_range: str = "",
    depth_factor: dict | None = None,
    validation: dict | None = None,
    fundamental_data: dict | None = None,
) -> str:
    """
    生成完整分析报告。

    Args:
        stock_info:         股票基本信息字典
        technical_summary:  技术面分析摘要（Markdown）
        news_aggregation:   新闻情感汇总字典
        backtest_results:   多策略回测结果 dict
        alpha_stats:        Alpha 因子得分统计
        data_range:         回测数据的实际日期范围（如 "2024-06-19 ~ 2026-05-26"）
    """
    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")

    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats, data_range, depth_factor,
            validation, fundamental_data,
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

    data_info = f"回测数据范围：{data_range}" if data_range else ""
    # 构建额外数据段（因子检验 + 基本面 + 盘口）
    extra = ""
    if validation:
        rows = []
        for col, v in validation.items():
            grade = v.get("grade", "?")
            mult = v.get("multiplier", 1.0)
            status = "全权" if mult >= 1.0 else ("半权" if mult >= 0.5 else "剔除")
            rows.append(f"| {col} | {v.get('samples', 0)} | {v.get('IC', 0):+.4f} | {v.get('IR', 0):+.2f} | {grade} | {status} |")
        if rows:
            extra += "\n## 因子有效性检验\n\n| 因子 | 样本数 | IC | IR | 评级 | 处置 |\n|------|--------|-----|------|------|------|\n" + "\n".join(rows) + "\n"
    if fundamental_data and fundamental_data.get("style_factors"):
        sf = fundamental_data["style_factors"]
        ff = fundamental_data["fundamental_factors"]
        extra += f"\n## 基本面与估值因子\n- PE(TTM)历史分位: {sf['pe_percentile']:.1%}\n- PB历史分位: {sf['pb_percentile']:.1%}\n- ROE: {ff['roe']:.1%}\n- 毛利率: {ff['gross_margin']:.1%}\n- 资产负债率: {ff['debt_ratio']:.1%}\n- 净利润同比: {ff['net_profit_yoy']:+.1%}\n- 营收同比: {ff['revenue_yoy']:+.1%}\n"
    if depth_factor and depth_factor.get("available"):
        d = depth_factor
        extra += f"\n## 实时盘口数据\n- 买盘总量: {d['bid_volume']:,.0f}\n- 卖盘总量: {d['ask_volume']:,.0f}\n- 买卖比: {d['imbalance']:.2f}\n- 盘口信号得分: {d['depth_score']:+.3f}\n"
    user_prompt = build_user_prompt(
        stock_info, technical_summary, news_aggregation,
        bt_summary, bt_table, alpha_text, data_info, extra,
    )

    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    # 调用 LLM API（OpenAI 兼容格式，Ollama 通过 /v1 端点同样支持）
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)
        logger.info(f"调用 LLM: model={model}")
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
                backtest_results, alpha_stats, data_range,
            )
        report = _clean_llm_output(report)
        logger.info(f"Report generated by LLM: {len(report)} chars")
        return report

    except Exception as e:
        logger.error(f"LLM report generation failed: {e}")
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats, data_range,
        )


def _generate_fallback_report(
    stock_info: dict,
    technical_summary: str,
    news_aggregation: dict,
    backtest_results: dict,
    alpha_stats: dict | None = None,
    data_range: str = "",
    depth_factor: dict | None = None,
    validation: dict | None = None,
    fundamental_data: dict | None = None,
) -> str:
    """生成完整中文分析报告（本地模板）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    market = stock_info.get("market", "")
    industry = stock_info.get("industry", "")
    description = stock_info.get("description", "")
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    market_name = "A股" if market == "A" else "美股"

    # 数据范围
    data_info = f"\n> 📅 回测数据范围：{data_range}" if data_range else ""
    if data_range:
        data_info += "\n> ⚠️ 实际数据范围可能因数据源限制与所选周期不完全一致"

    # ── Alpha 因子统计 ──
    alpha_str = ""
    if alpha_stats:
        alpha_str = (
            f"- 最新 Final_Score: **{alpha_stats.get('latest', 0):.3f}**\n"
            f"- 回测期均值: {alpha_stats.get('mean', 0):.3f}\n"
            f"- 标准差: {alpha_stats.get('std', 0):.3f}\n"
        )

    # ── 因子有效性检验 ──
    validation_str = ""
    if validation:
        rows = []
        for col, v in validation.items():
            grade = v.get("grade", "?")
            mult = v.get("multiplier", 1.0)
            status = "✓ 全权" if mult >= 1.0 else ("△ 半权" if mult >= 0.5 else "✗ 剔除")
            rows.append(
                f"| {col} | {v.get('samples', 0)} | {v.get('IC', 0):+.4f} | "
                f"{v.get('IR', 0):+.2f} | {grade} | {status} |"
            )
        if rows:
            validation_str = (
                "\n### 因子有效性检验\n\n"
                "| 因子 | 样本数 | IC | IR | 评级 | 处置 |\n"
                "|------|--------|-----|------|------|------|\n"
                + "\n".join(rows) + "\n"
            )

    # ── 基本面因子 ──
    fund_str = ""
    if fundamental_data and fundamental_data.get("style_factors"):
        sf = fundamental_data["style_factors"]
        ff = fundamental_data["fundamental_factors"]
        from alpha.fundamental import score_style_factor, score_fundamental_factor
        s_score = score_style_factor(sf["pe_percentile"], sf["pb_percentile"])
        f_score = score_fundamental_factor(**ff)

        fund_str = (
            f"\n### 基本面与估值因子\n\n"
            f"**风格因子**（估值分位，高=偏空，低=偏多）\n"
            f"| PE(TTM) 分位 | PB 分位 | 风格得分 |\n"
            f"|-------------|---------|----------|\n"
            f"| {sf['pe_percentile']:.1%} | {sf['pb_percentile']:.1%} | {s_score:+.3f} |\n\n"
            f"**基本面因子**（最新一期）\n"
            f"| ROE | 毛利率 | 资产负债率 | 净利同比 | 营收同比 | 基本面得分 |\n"
            f"|-----|--------|-----------|----------|----------|----------|\n"
            f"| {ff['roe']:.1%} | {ff['gross_margin']:.1%} | {ff['debt_ratio']:.1%} | "
            f"{ff['net_profit_yoy']:+.1%} | {ff['revenue_yoy']:+.1%} | {f_score:+.3f} |\n"
        )

    # 策略对比表
    bt_table = _build_backtest_markdown_table(backtest_results)
    bt_summary = _build_backtest_summary(backtest_results)

    # 综合建议
    recommendation = _derive_recommendation(backtest_results, alpha_stats, depth_factor)

    # 盘口信息
    depth_str = ""
    if depth_factor and depth_factor.get("available"):
        d = depth_factor
        depth_str = (
            f"\n---\n\n## 实时盘口分析\n\n"
            f"- 买盘总量：{d['bid_volume']:,.0f} 股\n"
            f"- 卖盘总量：{d['ask_volume']:,.0f} 股\n"
            f"- 买卖比：{d['imbalance']:.2f}"
            f"（{'买盘占优' if d['imbalance'] > 1.05 else '卖盘占优' if d['imbalance'] < 0.95 else '基本平衡'}）\n"
            f"- 盘口信号得分：{d['depth_score']:+.3f}\n"
        )

    report = f"""# {name}（{code}）分析报告

> 生成时间：{now}
> 市场：{market_name} | 行业：{industry}{data_info}

---

## 一、股票简介

{description if description else '暂无公司简介信息。'}

---

## 二、Alpha 因子分析（多因子量化模型）

权重：技术 35% + 风格 15% + 基本面 25% + 新闻 25%（含基本面时）
或无基本面时：技术 60% + 新闻 40%。因子经 IC/IR 检验，D 级剔除、C 级半权。

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

{depth_str}

---

## 六、综合建议

**{recommendation}**

---

> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。
"""
    logger.info("Report generated by fallback template")
    return report


def _derive_recommendation(backtest_results: dict, alpha_stats: dict | None,
                           depth_factor: dict | None = None) -> str:
    """基于回测结果、因子得分和盘口数据推导操作建议。"""
    if not backtest_results:
        return "数据不足，建议观望。"

    positive = sum(1 for r in backtest_results.values() if r.total_return > 0)
    total = len(backtest_results)
    latest_score = alpha_stats.get("latest", 0) if alpha_stats else 0

    # 盘口信号
    depth_note = ""
    if depth_factor and depth_factor.get("available"):
        d = depth_factor
        if d["imbalance"] > 1.2:
            depth_note = "，实时盘口买盘显著占优"
        elif d["imbalance"] < 0.8:
            depth_note = "，实时盘口卖盘显著占优"

    if positive == total and latest_score > 0.3:
        return f"多个策略回测表现正向，当前 Alpha 因子偏多{depth_note}，建议关注买入机会。"
    elif positive >= total / 2:
        return f"部分策略表现正向{depth_note}，建议谨慎关注，等待更明确的趋势信号。"
    elif latest_score < -0.3:
        return f"当前 Alpha 因子偏空，多数策略表现不佳{depth_note}，建议观望或减仓。"
    else:
        return f"策略表现分化{depth_note}，建议观望，等待趋势明朗后再做决策。"
