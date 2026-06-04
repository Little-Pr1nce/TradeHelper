"""
报告生成模块

负责将各分析模块的输出整合为完整的分析报告。

三种分析模式的报告生成：
  1. 盘后（eod）：
     - generate_report() — LLM 全新生成 8 章（现有逻辑）
     - _generate_fallback_report() — 本地模板兜底

  2. 盘中（intraday）：
     - generate_intraday_report() — T-1 报告 1-7 章复用 + LLM 重写第 8 章
     - _build_intraday_fallback_ch8() — 本地模板兜底

  3. 盘前（pre）：
     - generate_premarket_report() — T-1 报告 1-7 章复用 + LLM 重写第 8 章
     - _build_premarket_fallback_ch8() — 本地模板兜底

两种生成方式：
  1. LLM 生成（推荐）：
     - 调用 OpenAI 兼容 API
     - 通过 SYSTEM_PROMPT 约束模型仅基于提供数据分析
     - 输出结构化 Markdown 报告

  2. 回退生成（无 API 时）：
     - 使用 Python 字符串模板拼接
     - 保证在大模型不可用时功能仍正常
"""

import logging
import re
from datetime import datetime

from config.settings import Settings
from report.prompts import (
    SYSTEM_PROMPT, build_user_prompt,
    INTRADAY_SYSTEM_PROMPT, build_intraday_user_prompt,
    PREMARKET_SYSTEM_PROMPT, build_premarket_user_prompt,
)

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
    rank_ic: dict | None = None,
    rank_ic_5d: dict | None = None,
    rank_ic_10d: dict | None = None,
    benchmark_return: float = 0.0,
    realtime_quote: dict | None = None,
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
            validation, fundamental_data, rank_ic, benchmark_return,
            realtime_quote=realtime_quote,
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
    if rank_ic:
        extra += f"\n## 因子模型整体有效性（Rank IC — 多周期）\n" \
                 f"| 周期 | Rank IC 均值 | IC_IR | 解读 |\n" \
                 f"|------|-------------|-------|------|\n" \
                 f"| 1 日 | {rank_ic.get('rank_ic_mean', 0):+.4f} | {rank_ic.get('ic_ir', 0):+.2f} | "
        ic1 = rank_ic.get('rank_ic_mean', 0)
        extra += ("短期预测力偏多" if ic1 > 0.05 else ("短期预测力偏空" if ic1 < -0.05 else "短期预测力中性")) + " |\n"
        if rank_ic_5d:
            extra += f"| 5 日 | {rank_ic_5d.get('rank_ic_mean', 0):+.4f} | {rank_ic_5d.get('ic_ir', 0):+.2f} | "
            ic5 = rank_ic_5d.get('rank_ic_mean', 0)
            extra += ("中期预测力偏多" if ic5 > 0.05 else ("中期预测力偏空" if ic5 < -0.05 else "中期预测力中性")) + " |\n"
        if rank_ic_10d:
            extra += f"| 10 日 | {rank_ic_10d.get('rank_ic_mean', 0):+.4f} | {rank_ic_10d.get('ic_ir', 0):+.2f} | "
            ic10 = rank_ic_10d.get('rank_ic_mean', 0)
            extra += ("中长期预测力偏多" if ic10 > 0.05 else ("中长期预测力偏空" if ic10 < -0.05 else "中长期预测力中性")) + " |\n"
        extra += (
            f"\n注：Rank IC > 0.05 表示因子在该周期有正向预测力；"
            f"< -0.05 表示有反向预测力（短期可能为均值回归）；"
            f"IC_IR > 0.5 表示预测能力稳定。\n"
            f"不同周期的 IC 符号可能不同——短期均值回归（IC 为负）与中长期趋势跟随（IC 为正）可同时存在。\n"
        )
    if benchmark_return:
        extra += f"\n## 基准收益\n" \
                 f"- 买入持有收益（同期）: {benchmark_return*100:+.2f}%\n" \
                 f"注：用于对比策略是否跑赢被动持有。\n"
    if realtime_quote:
        status_map = {0: "正常交易", 1: "停牌", 2: "退市", 3: "熔断"}
        ts = realtime_quote.get("timestamp", 0)
        time_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else "未知"
        extra += f"\n## 实时报价\n" \
                 f"- 最新价: {realtime_quote['latest']:.2f}（{realtime_quote['change_pct']:+.2%}）\n" \
                 f"- 开盘: {realtime_quote['open']:.2f} | 最高: {realtime_quote['high']:.2f} | 最低: {realtime_quote['low']:.2f}\n" \
                 f"- 前收盘: {realtime_quote['prev_close']:.2f} | 成交量: {realtime_quote['volume']:,.0f}\n" \
                 f"- 状态: {status_map.get(realtime_quote.get('status', 0), '未知')} | 更新时间: {time_str}\n"
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
            max_tokens=16000,
        )
        choice = response.choices[0]
        finish = choice.finish_reason
        if finish and finish != "stop":
            logger.warning(f"LLM 输出提前结束，finish_reason={finish}，报告可能不完整")
        else:
            logger.info(f"LLM 输出完成，finish_reason={finish}")
        report = _clean_llm_output(choice.message.content)
        if not report:
            logger.warning("LLM returned empty response, falling back to template")
            return _generate_fallback_report(
                stock_info, technical_summary, news_aggregation,
                backtest_results, alpha_stats, data_range,
                depth_factor=depth_factor, validation=validation,
                fundamental_data=fundamental_data, rank_ic=rank_ic,
                rank_ic_5d=rank_ic_5d, rank_ic_10d=rank_ic_10d,
                benchmark_return=benchmark_return,
                realtime_quote=realtime_quote,
            )
        # 确保 LLM 输出包含隐式分隔标记（兼容忘记加标记的 LLM）
        report = _ensure_section_marker(report)
        logger.info(f"Report generated by LLM: {len(report)} chars")
        return report

    except Exception as e:
        logger.error(f"LLM report generation failed: {e}")
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats, data_range,
            depth_factor=depth_factor, validation=validation,
            fundamental_data=fundamental_data, rank_ic=rank_ic,
            rank_ic_5d=rank_ic_5d, rank_ic_10d=rank_ic_10d,
            benchmark_return=benchmark_return,
            realtime_quote=realtime_quote,
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
    rank_ic: dict | None = None,
    rank_ic_5d: dict | None = None,
    rank_ic_10d: dict | None = None,
    benchmark_return: float = 0.0,
    realtime_quote: dict | None = None,
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
    recommendation = _derive_recommendation(backtest_results, alpha_stats, depth_factor,
                                             rank_ic=rank_ic, benchmark_return=benchmark_return)

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

    # 实时报价
    quote_str = ""
    if realtime_quote:
        status_map = {0: "正常交易", 1: "停牌", 2: "退市", 3: "熔断"}
        ts = realtime_quote.get("timestamp", 0)
        time_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else "未知"
        quote_str = (
            f"\n---\n\n## 实时报价\n\n"
            f"- 最新价：**{realtime_quote['latest']:.2f}**（{realtime_quote['change_pct']:+.2%}）\n"
            f"- 开盘：{realtime_quote['open']:.2f} | 最高：{realtime_quote['high']:.2f} | 最低：{realtime_quote['low']:.2f}\n"
            f"- 前收盘：{realtime_quote['prev_close']:.2f} | 成交量：{realtime_quote['volume']:,.0f}\n"
            f"- 状态：{status_map.get(realtime_quote.get('status', 0), '未知')} | 更新时间：{time_str}\n"
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

{quote_str}

---

{SECTION_8_MARKER}

## 六、综合建议

**{recommendation}**

---

> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。
"""
    logger.info("Report generated by fallback template")
    return report


def _derive_recommendation(backtest_results: dict, alpha_stats: dict | None,
                           depth_factor: dict | None = None,
                           rank_ic: dict | None = None,
                           benchmark_return: float = 0.0) -> str:
    """基于回测结果、因子得分和盘口数据推导操作建议。"""
    if not backtest_results:
        return "数据不足，建议观望。"

    positive = sum(1 for r in backtest_results.values() if r.total_return > 0)
    total = len(backtest_results)
    latest_score = alpha_stats.get("latest", 0) if alpha_stats else 0

    # Rank IC
    rank_ic_val = rank_ic.get("rank_ic_mean", 0) if rank_ic else 0

    # 盘口信号
    depth_note = ""
    if depth_factor and depth_factor.get("available"):
        d = depth_factor
        if d["imbalance"] > 1.2:
            depth_note = "，实时盘口买盘显著占优"
        elif d["imbalance"] < 0.8:
            depth_note = "，实时盘口卖盘显著占优"

    parts = []
    if positive == total and latest_score > 0.3:
        parts.append("多个策略回测表现正向，当前 Alpha 因子偏多")
    elif positive >= total / 2:
        parts.append("部分策略表现正向，建议谨慎关注")
    elif latest_score < -0.3:
        parts.append("当前 Alpha 因子偏空，多数策略表现不佳")
    else:
        parts.append("策略表现分化")

    if benchmark_return:
        avg_strategy_return = sum(r.total_return for r in backtest_results.values()) / total
        if avg_strategy_return > benchmark_return:
            parts.append(f"策略平均收益({avg_strategy_return*100:+.2f}%)跑赢买入持有({benchmark_return*100:+.2f}%)")
        else:
            parts.append(f"策略平均收益({avg_strategy_return*100:+.2f}%)未跑赢买入持有({benchmark_return*100:+.2f}%)")

    if rank_ic_val > 0.05:
        parts.append("Rank IC 显示因子对后市有一定预测力")
    elif rank_ic_val < 0:
        parts.append("Rank IC 为负，因子当前对后市预测力较弱")

    if depth_note:
        parts.append(depth_note.strip("，"))

    return "，".join(parts) + "，建议观望。"


# ============================================================
# T-1 报告章节分割
# ============================================================

# 章节 8 分割点的隐式标记，用于鲁棒分割
SECTION_8_MARKER = "<!-- SECTION_8_BOUNDARY -->"


def _ensure_section_marker(report: str) -> str:
    """确保报告在章节 7/8 之间包含隐式分隔标记。"""
    if SECTION_8_MARKER in report:
        return report
    # 尝试用 _split_t1_report 找到分割点，然后插入标记
    ch1_7, ch8 = _split_t1_report(report)
    if ch8:
        return f"{ch1_7}\n\n{SECTION_8_MARKER}\n\n{ch8}"
    return report


def _split_t1_report(report_content: str) -> tuple[str, str]:
    """
    将 T-1 日完整报告分割为 (前7章, 第8章后的内容)。

    分割策略（按优先级）：
      1. 隐式标记 <!-- SECTION_8_BOUNDARY -->（最可靠）
      2. 正则匹配章节标题：## 八、## 8、## 第八部分、## Chapter 8 等
      3. 降级：在报告末尾 1/3 处寻找最大的 ## 标题作为分割点
      4. 兜底：返回完整报告作为前 7 章
    """
    # ── 策略 1：隐式标记 ──
    if SECTION_8_MARKER in report_content:
        parts = report_content.split(SECTION_8_MARKER, 1)
        return parts[0].strip(), parts[1].strip()

    # ── 策略 2：正则匹配章节标题 ──
    patterns = [
        r'\n(?=##\s*八[、.．\s])',        # ## 八、 / ## 八. / ## 八
        r'\n(?=##\s*8[、.．\s])',          # ## 8、 / ## 8. / ## 8
        r'\n(?=##\s*第八部分)',             # ## 第八部分
        r'\n(?=##\s*第\s*八\s*章)',         # ## 第八章
        r'\n(?=##\s*Chapter\s*8\b)',        # ## Chapter 8
        r'\n(?=##\s*VIII\b)',               # ## VIII
        r'\n(?=##\s*8[.．]\s)',             # ## 8. （英文句点）
    ]
    for pattern in patterns:
        parts = re.split(pattern, report_content, maxsplit=1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()

    # ── 策略 3：降级 — 在末尾 1/3 处找最大的 ## 标题 ──
    lines = report_content.split('\n')
    h2_positions = [i for i, line in enumerate(lines)
                    if re.match(r'^##\s+\S', line)]
    if len(h2_positions) >= 2:
        # 取后 1/3 区域的第一个 ## 标题
        cutoff = len(lines) * 2 // 3
        for pos in h2_positions:
            if pos >= cutoff:
                ch1_7 = '\n'.join(lines[:pos]).strip()
                ch8 = '\n'.join(lines[pos:]).strip()
                logger.debug(f"_split_t1_report: 降级分割于行 {pos} (标题: {lines[pos][:40]})")
                return ch1_7, ch8

    # ── 策略 4：兜底 ──
    logger.debug("_split_t1_report: 未找到章节 8 分割点，返回完整报告作为前 7 章")
    return report_content.strip(), ""


def _strip_ch8_header(ch8_content: str) -> str:
    """移除 LLM 可能重复输出的「## 八、...」标题行，避免报告中出现重复标题。"""
    # LLM 输出时可能已经带了 ## 八、... 标题，先检查
    ch8 = ch8_content.strip()
    # 如果 LLM 输出以 ## 八 开头，保留它；否则需要拼接
    return ch8


# ============================================================
# 盘中报告生成
# ============================================================

def generate_intraday_report(
    t1_report_content: str,
    snapshot_text: str,
    stock_info: dict,
) -> str:
    """
    生成盘中分析报告。

    结构：
      ⚡ 盘中实时快照（纯计算，已格式化）
      → T-1 日报告第 1-7 章（复用）
      → 第八章：盘中操作参考（LLM 重新生成）

    Args:
        t1_report_content: T-1 日完整报告的 Markdown 全文
        snapshot_text:     compute_intraday_snapshot() 返回的 Markdown 文本
        stock_info:        股票基本信息字典

    Returns:
        完整的盘中分析报告 Markdown 文本
    """
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 分割 T-1 报告
    t1_body, _ = _split_t1_report(t1_report_content)

    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")

    # 尝试 LLM 生成第八章
    chapter_8 = None
    if api_key or "localhost" in base_url or "127.0.0.1" in base_url:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
            user_prompt = build_intraday_user_prompt(t1_report_content, snapshot_text, stock_info)
            logger.info(f"调用 LLM 生成盘中操作参考: model={model}")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": INTRADAY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=8000,
            )
            choice = response.choices[0]
            finish = choice.finish_reason
            if finish and finish != "stop":
                logger.warning(f"LLM 盘中报告输出提前结束，finish_reason={finish}")
            chapter_8 = _clean_llm_output(choice.message.content)
            if chapter_8:
                logger.info(f"LLM 盘中第 8 章: {len(chapter_8)} chars")
        except Exception as e:
            logger.error(f"LLM 盘中报告生成失败: {e}")

    if not chapter_8:
        logger.warning("LLM 盘中第 8 章为空，使用回退模板")
        chapter_8 = _build_intraday_fallback_ch8(stock_info, snapshot_text)

    # 拼接完整报告
    report_title = f"# {name}（{code}）盘中分析报告"
    header = (
        f"{report_title}\n\n"
        f"> ⏰ 盘中实时 | 更新时间：{now_str}\n"
        f"> 📊 分析基底：T-1 日收盘后完整分析\n\n"
    )

    report = (
        f"{header}"
        f"---\n\n"
        f"{snapshot_text}\n\n"
        f"---\n\n"
        f"{t1_body}\n\n"
        f"---\n\n"
        f"{chapter_8}\n\n"
        f"---\n\n"
        f"> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。\n"
        f"> ⏰ 本报告基于 T-1 日收盘后的完整分析 + 盘中实时数据叠加生成。\n"
        f"> 盘中价格和盘口数据实时变化，报告中的操作参考价位仅反映生成时刻（{now_str}）的状态。\n"
    )

    logger.info(f"盘中报告生成完成: {len(report)} chars")
    return report


def _build_intraday_fallback_ch8(
    stock_info: dict,
    snapshot_text: str,
) -> str:
    """盘中报告第八章的本地回退模板（无 LLM 时使用）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""## 八、盘中操作参考（AI实时分析）

> ⚠️ 当前未配置 LLM API Key，请配置后获取 AI 综合解读。以下为盘中原始数据汇总。

### 8.1 数据说明

上方「盘中实时快照」表格包含：
- **实时价格位置**：最新价 vs MA5/MA10/MA20/MA60 的偏离百分比、布林带位置、VWAP 偏离、日内动量
- **盘口买卖比**：买卖挂单量对比 + 盘口因子得分
- **盘中走势数据**：开盘/最高/最低/最新价位表
- **T-1 日关键信号**：Alpha Final_Score、MACD、RSI、KDJ、ADX、ATR

### 8.2 操作建议

请配置 LLM API Key 以获取 AI 综合解读与操作建议。以上为原始数据汇总。

> ⏰ 快照时间：{now_str}"""


# ============================================================
# 盘前报告生成
# ============================================================

def generate_premarket_report(
    t1_report_content: str,
    snapshot_text: str,
    stock_info: dict,
) -> str:
    """
    生成盘前分析报告。

    结构：
      ⚡ 盘前快照（期货 + 盘前价格 + 隔夜新闻）
      → T-1 日报告第 1-7 章（复用）
      → 第八章：盘前策略参考（LLM 重新生成）

    Args:
        t1_report_content: T-1 日完整报告的 Markdown 全文
        snapshot_text:     compute_premarket_snapshot() 返回的 Markdown 文本
        stock_info:        股票基本信息字典

    Returns:
        完整的盘前分析报告 Markdown 文本
    """
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 分割 T-1 报告
    t1_body, _ = _split_t1_report(t1_report_content)

    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")

    # 尝试 LLM 生成第八章
    chapter_8 = None
    if api_key or "localhost" in base_url or "127.0.0.1" in base_url:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
            user_prompt = build_premarket_user_prompt(t1_report_content, snapshot_text, stock_info)
            logger.info(f"调用 LLM 生成盘前策略参考: model={model}")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": PREMARKET_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=10000,
            )
            choice = response.choices[0]
            finish = choice.finish_reason
            if finish and finish != "stop":
                logger.warning(f"LLM 盘前报告输出提前结束，finish_reason={finish}")
            chapter_8 = _clean_llm_output(choice.message.content)
            if chapter_8:
                logger.info(f"LLM 盘前第 8 章: {len(chapter_8)} chars")
        except Exception as e:
            logger.error(f"LLM 盘前报告生成失败: {e}")

    if not chapter_8:
        logger.warning("LLM 盘前第 8 章为空，使用回退模板")
        chapter_8 = _build_premarket_fallback_ch8(stock_info, snapshot_text)

    # 拼接完整报告
    report_title = f"# {name}（{code}）盘前分析报告"
    header = (
        f"{report_title}\n\n"
        f"> 🌅 盘前分析 | 生成时间：{now_str}\n"
        f"> 📊 分析基底：T-1 日收盘后完整分析\n\n"
    )

    report = (
        f"{header}"
        f"---\n\n"
        f"{snapshot_text}\n\n"
        f"---\n\n"
        f"{t1_body}\n\n"
        f"---\n\n"
        f"{chapter_8}\n\n"
        f"---\n\n"
        f"> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。\n"
        f"> 🌅 本报告基于 T-1 日收盘后的完整分析 + 盘前数据叠加生成。\n"
        f"> 盘前流动性较低，开盘后可能因流动性改善而出现价格跳变。报告中的策略参考价位基于生成时刻（{now_str}）的数据。\n"
    )

    logger.info(f"盘前报告生成完成: {len(report)} chars")
    return report


def _build_premarket_fallback_ch8(
    stock_info: dict,
    snapshot_text: str,
) -> str:
    """盘前报告第八章的本地回退模板（无 LLM 时使用）。"""
    name = stock_info.get("name", "")
    code = stock_info.get("code", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""## 八、盘前策略参考（AI分析）

> ⚠️ 当前未配置 LLM API Key，请配置后获取 AI 综合解读。以下为盘前原始数据汇总。

### 8.1 数据说明

上方「盘前快照」表格包含：
- **期货风向标**：NQ/ES 期货涨跌幅、成交量、5分钟K线数据（阳线/总根数）
- **期货宏观情绪得分**：量化期货对开盘方向的影响程度
- **个股盘前**：盘前价格、与期货相对差值、成交量、距 MA5 跳空幅度

### 8.2 今日操作策略

请配置 LLM API Key 以获取 AI 情景推演与策略分析。以上为原始数据汇总。

> 🌅 快照时间：{now_str}"""
