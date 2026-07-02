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
from strategies import get_execution_strategy
from report.prompts import (
    SYSTEM_PROMPT, build_user_prompt,
    INTRADAY_SYSTEM_PROMPT, build_intraday_user_prompt,
    PREMARKET_SYSTEM_PROMPT, build_premarket_user_prompt,
    PORTFOLIO_SYSTEM_PROMPT, build_portfolio_user_prompt,
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


_RESEARCH_HEADING_RE = re.compile(
    r"^#{2,4}\s*(?:\d+\s*[.、]\s*)?\**研究员观察候选\**\s*$",
    flags=re.MULTILINE,
)
_RISK_REWARD_TERM_RE = re.compile(r"风险(?:收益|回报)比")
_RISK_REWARD_PRAISE_RE = re.compile(
    r"风险(?:收益|回报)比\s*(?:极佳|优秀|很好|良好|较好|理想|可观|很高|较高|(?:非常)?有吸引力)"
)
_RISK_REWARD_RATIO_RE = re.compile(r"1\s*[:：]\s*(\d+(?:\.\d+)?)")


def _normalize_research_candidate_heading(text: str) -> tuple[str, int]:
    """统一研究员候选标题并删除模型生成的重复标题。"""
    seen = False
    removed = 0

    def replace(_match: re.Match) -> str:
        nonlocal seen, removed
        if seen:
            removed += 1
            return ""
        seen = True
        return "### 研究员观察候选"

    normalized = _RESEARCH_HEADING_RE.sub(replace, text or "")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip(), removed


def _enforce_llm_compliance(text: str, operation_plan: str = "") -> str:
    """对 LLM 交易表述做确定性后验校验。"""
    normalized, duplicate_headings = _normalize_research_candidate_heading(text)
    code_has_fixed_ratio = bool(_RISK_REWARD_RATIO_RE.search(operation_plan or ""))
    correction_count = duplicate_headings
    output_lines = []

    for line in normalized.splitlines():
        if _RISK_REWARD_TERM_RE.search(line):
            line, praise_count = _RISK_REWARD_PRAISE_RE.subn(
                "固定风险收益比须以代码方案中的固定止盈目标和止损计算",
                line,
            )
            correction_count += praise_count

            def validate_ratio(match: re.Match) -> str:
                nonlocal correction_count
                correction_count += 1
                return (
                    "见代码方案的确定性计算"
                    if code_has_fixed_ratio else
                    "固定风险收益比不可量化"
                )

            line = _RISK_REWARD_RATIO_RE.sub(validate_ratio, line)
        output_lines.append(line)

    if correction_count:
        logger.warning(f"LLM 报告合规校验修正 {correction_count} 处")
    return "\n".join(output_lines).strip()


def _build_backtest_summary(
    bt_results: dict,
    benchmark_return: float = 0.0,
    market_regime: str = "unknown",
    strategy_meta: dict | None = None,
) -> str:
    """将多策略回测结果格式化为可读文本，包含策略逻辑说明供 LLM 交叉分析。

    Args:
        bt_results: {策略名: BacktestResult}
        benchmark_return: 买入持有基准收益
        market_regime: 当前行情类型
        strategy_meta: {策略key: {"name":..., "description":..., "suitable_regimes":[...]}}
    """
    if not bt_results:
        return "回测数据不可用。"

    # 行情中文映射
    regime_cn = {
        "trending_volatile": "强趋势+高波动",
        "trending_steady": "慢涨/弱趋势",
        "trending": "趋势市",
        "ranging": "震荡市",
        "transitional": "过渡期",
    }.get(market_regime, market_regime)

    lines = [
        f"**回测环境**：当前行情 = {regime_cn}（{market_regime}），基准收益（买入持有）= {benchmark_return*100:+.2f}%",
        "",
        "**重要提示**：以下每个策略都附带了其核心操作逻辑说明。你必须基于这些逻辑，在操作建议中告诉用户「如果要复制此策略的做法，现在应该怎么做」——把策略规则翻译成当前可执行的具体操作。",
        "",
    ]

    # 分组
    human_keys = set("IJKLMN")
    quant_entries = []
    human_entries = []
    for name, r in bt_results.items():
        key = name[0] if name and name[0].isalpha() else ""
        meta = (strategy_meta or {}).get(name, {})
        desc = meta.get("description", "")
        regimes = meta.get("suitable_regimes", [])
        regime_note = ""
        if regimes:
            regime_note = f"（适配行情：{', '.join(regimes)}）"
        elif key in human_keys:
            regime_note = "（全行情通用—人类交易策略）"

        # 构建交易记录（供 LLM 参考真实买卖时机和价位）
        trade_log = ""
        if r.trades:
            trade_lines = ["", "  **实际交易记录**（回测期内的真实买卖）："]
            # LLM 只需要最近一笔作为行为示例；完整交易明细留在代码报告/回测对象中。
            for i, t in enumerate(r.trades[-1:], 1):
                entry_d = t.get("entry_date", "?")
                entry_p = t.get("entry_price", 0)
                exit_d = t.get("exit_date", "?")
                exit_p = t.get("exit_price", 0)
                shares = t.get("shares", 0)
                pnl = t.get("pnl", 0)
                ret_pct = t.get("return_pct", 0)
                reason_in = t.get("reason", "")
                reason_out = t.get("exit_reason", "")
                trade_lines.append(
                    f"  · 第{i}笔：{entry_d} 买入 {shares}股 @ ${entry_p:.2f}（{reason_in}）"
                    f" → {exit_d} 卖出 @ ${exit_p:.2f}（{reason_out}）"
                    f" 盈亏 {pnl:+.0f}（{ret_pct:+.1f}%）"
                )
            trade_log = "\n".join(trade_lines) + "\n"

        entry = (
            f"### {name}\n"
            f"- **核心逻辑**：{desc}\n"
            f"- **适配行情说明**：{regime_note}\n"
            f"- **回测绩效**：总收益 {r.total_return*100:+.2f}%，年化 {r.annual_return*100:+.2f}%，"
            f"最大回撤 {r.max_drawdown*100:.2f}%，夏普 {r.sharpe_ratio:.2f}，"
            f"Calmar {r.calmar_ratio:.2f}，胜率 {r.win_rate*100:.0f}%，交易 {r.total_trades} 次"
            f"{trade_log}\n"
        )
        if key in human_keys:
            human_entries.append(entry)
        else:
            quant_entries.append(entry)

    if quant_entries:
        lines.append("## 量化策略（A-H，O）")
        lines.extend(quant_entries)
    if human_entries:
        lines.append("## 人类策略（I-N）")
        lines.append("（I/J/K = 新手，L/M/N = 老手）")
        lines.extend(human_entries)

    return "\n".join(lines)


def _build_backtest_markdown_table(bt_results: dict) -> str:
    """生成 Markdown 格式的策略对比表，量化策略和人类策略分两组。"""
    if not bt_results:
        return ""

    HUMAN_KEYS = set("IJKLMN")

    header = "| 策略 | 总收益 | 年化收益 | 最大回撤 | 夏普比率 | Calmar | 胜率 | 交易次数 |"
    sep = "|------|--------|----------|----------|----------|--------|------|----------|"

    quant_rows = []
    human_rows = []
    for name, r in bt_results.items():
        key = name[0] if name and name[0].isalpha() else ""
        row = (
            f"| {name} | {r.total_return*100:+.2f}% | {r.annual_return*100:+.2f}% | "
            f"{r.max_drawdown*100:.2f}% | {r.sharpe_ratio:.2f} | {r.calmar_ratio:.2f} | "
            f"{r.win_rate*100:.0f}% | {r.total_trades} |"
        )
        if key in HUMAN_KEYS:
            human_rows.append(row)
        else:
            quant_rows.append(row)

    parts = []
    if quant_rows:
        parts.append("### 量化策略\n")
        parts.extend([header, sep] + quant_rows)
    if human_rows:
        if parts:
            parts.append("")
        parts.append("### 人类策略对比\n")
        parts.extend([header, sep] + human_rows)
    return "\n".join(parts)


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
    market_regime: str = "unknown",
    active_strategies: list | None = None,
    skipped_strategies: list | None = None,
    param_tuning: dict | None = None,
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
    operation_plan: str | None = None,
    use_llm: bool = True,
) -> str:
    """
    生成完整分析报告。market_regime 等回测元信息会体现在报告中。

    operation_plan: 代码生成的操作方案 Markdown（Phase 5），注入 LLM prompt 供解读。

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
    enable_thinking = settings.get("llm_enable_thinking", False)

    if not use_llm or (
        not api_key
        and "localhost" not in base_url
        and "127.0.0.1" not in base_url
    ):
        return _generate_fallback_report(
            stock_info, technical_summary, news_aggregation,
            backtest_results, alpha_stats, data_range, depth_factor,
            validation, fundamental_data, rank_ic, benchmark_return,
            realtime_quote=realtime_quote,
            operation_plan=operation_plan,
        )

    # 构建策略元数据（供 LLM 理解每个策略的操作逻辑）
    from strategies import get_execution_strategy
    strategy_meta = {}
    for key in (active_strategies or []) + (skipped_strategies or []):
        try:
            s = get_execution_strategy(key)
            strategy_meta[key] = {
                "name": s.name,
                "description": s.description,
                "suitable_regimes": s.suitable_regimes,
            }
        except Exception:
            pass
    # 回测结果中可能有关键名不是简单 key 的情况，补齐
    for bt_name in backtest_results:
        if bt_name not in strategy_meta:
            # 尝试从策略名前缀匹配
            for key in list(strategy_meta.keys()):
                if bt_name.startswith(key):
                    strategy_meta[bt_name] = strategy_meta[key]
                    break

    # 构建 LLM 提示词
    news_text = news_aggregation.get("summary", "")
    top_news = news_aggregation.get("top_news", "")
    bt_summary = _build_backtest_summary(
        backtest_results,
        benchmark_return=benchmark_return,
        market_regime=market_regime,
        strategy_meta=strategy_meta,
    )
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
    analysis_context = ""
    if validation:
        from alpha.validation import factor_validation_coverage

        rows = []
        for col, v in validation.items():
            grade = v.get("grade", "?")
            mult = v.get("multiplier", 1.0)
            status = (
                "未验证（原权重）" if grade == "?"
                else ("全权" if mult >= 1.0 else ("半权" if mult >= 0.5 else "剔除"))
            )
            rows.append(f"| {col} | {v.get('samples', 0)} | {v.get('IC', 0):+.4f} | {v.get('IR', 0):+.2f} | {grade} | {status} |")
        if rows:
            coverage = factor_validation_coverage(validation)
            analysis_context += (
                f"\n## 因子有效性检验\n\n验证覆盖率：{coverage:.0%}。"
                "未验证因子保留研究先验权重，但不会被视为已通过历史检验。\n\n"
                "| 因子 | 样本数 | IC | IR | 评级 | 处置 |\n"
                "|------|--------|-----|------|------|------|\n"
                + "\n".join(rows) + "\n"
            )
    if fundamental_data and fundamental_data.get("style_factors"):
        sf = fundamental_data["style_factors"]
        ff = fundamental_data["fundamental_factors"]
        analysis_context += f"\n## 基本面与估值因子\n- PE(TTM)历史分位: {sf['pe_percentile']:.1%}\n- PB历史分位: {sf['pb_percentile']:.1%}\n- ROE: {ff['roe']:.1%}\n- 毛利率: {ff['gross_margin']:.1%}\n- 资产负债率: {ff['debt_ratio']:.1%}\n- 净利润同比: {ff['net_profit_yoy']:+.1%}\n- 营收同比: {ff['revenue_yoy']:+.1%}\n"
    if depth_factor and depth_factor.get("available"):
        d = depth_factor
        analysis_context += f"\n## 实时盘口数据\n- 买盘总量: {d['bid_volume']:,.0f}\n- 卖盘总量: {d['ask_volume']:,.0f}\n- 买卖比: {d['imbalance']:.2f}\n- 盘口信号得分: {d['depth_score']:+.3f}\n"
    if rank_ic:
        analysis_context += f"\n## 因子模型整体有效性（Rank IC — 多周期）\n" \
                 f"| 周期 | Rank IC 均值 | IC_IR | 解读 |\n" \
                 f"|------|-------------|-------|------|\n" \
                 f"| 1 日 | {rank_ic.get('rank_ic_mean', 0):+.4f} | {rank_ic.get('ic_ir', 0):+.2f} | "
        ic1 = rank_ic.get('rank_ic_mean', 0)
        analysis_context += ("短期预测力偏多" if ic1 > 0.05 else ("短期预测力偏空" if ic1 < -0.05 else "短期预测力中性")) + " |\n"
        if rank_ic_5d:
            analysis_context += f"| 5 日 | {rank_ic_5d.get('rank_ic_mean', 0):+.4f} | {rank_ic_5d.get('ic_ir', 0):+.2f} | "
            ic5 = rank_ic_5d.get('rank_ic_mean', 0)
            analysis_context += ("中期预测力偏多" if ic5 > 0.05 else ("中期预测力偏空" if ic5 < -0.05 else "中期预测力中性")) + " |\n"
        if rank_ic_10d:
            analysis_context += f"| 10 日 | {rank_ic_10d.get('rank_ic_mean', 0):+.4f} | {rank_ic_10d.get('ic_ir', 0):+.2f} | "
            ic10 = rank_ic_10d.get('rank_ic_mean', 0)
            analysis_context += ("中长期预测力偏多" if ic10 > 0.05 else ("中长期预测力偏空" if ic10 < -0.05 else "中长期预测力中性")) + " |\n"
        analysis_context += (
            f"\n注：Rank IC > 0.05 表示因子在该周期有正向预测力；"
            f"< -0.05 表示有反向预测力（短期可能为均值回归）；"
            f"IC_IR > 0.5 表示预测能力稳定。\n"
            f"不同周期的 IC 符号可能不同——短期均值回归（IC 为负）与中长期趋势跟随（IC 为正）可同时存在。\n"
        )
    if benchmark_return:
        analysis_context += f"\n## 基准收益\n" \
                 f"- 买入持有收益（同期）: {benchmark_return*100:+.2f}%\n" \
                 f"注：用于对比策略是否跑赢被动持有。\n"
    # ── 策略参数调优结果 ──
    if param_tuning:
        analysis_context += "\n## 策略参数优化\n\n"
        analysis_context += "| 策略 | 参数 | 默认值 | 最优值 |\n"
        analysis_context += "|------|------|--------|--------|\n"
        for key, info in param_tuning.items():
            s = get_execution_strategy(key)
            analysis_context += (f"| {key} {s.name} | {info['param']} "
                     f"| {info['default']:.2f} | {info['best_value']:.2f} |\n")
        analysis_context += ("\n注：以上为基于回测期内数据的参数扫描结果，最优值可能受过拟合影响。"
                  "实际使用时建议综合考虑。\n")

    # ── 市场状态 + 策略适配 ──
    if market_regime and market_regime != "unknown":
        regime_labels = {
            "trending_volatile": "强趋势+高波动",
            "trending_steady": "慢涨/弱趋势",
            "ranging": "震荡",
            "transitional": "趋势形成中",
        }
        regime_label = regime_labels.get(market_regime, market_regime)
        analysis_context += f"\n## 市场状态检测\n\n当前行情：**{regime_label}**（{market_regime}）\n\n"
        if active_strategies or skipped_strategies:
            analysis_context += "| 策略 | 状态 | 说明 |\n|------|------|------|\n"
            for name in (active_strategies or []):
                s = get_execution_strategy(name)
                analysis_context += f"| {name} {s.name} | ▶ 运行 | 适配当前 {regime_label} 行情 |\n"
            for name in (skipped_strategies or []):
                s = get_execution_strategy(name)
                regime_desc = ", ".join(s.suitable_regimes) if s.suitable_regimes else "全部行情"
                analysis_context += f"| {name} {s.name} | ⏭ 跳过 | 适配 {regime_desc}；当前为 {regime_label} |\n"
            analysis_context += "\n"

    if realtime_quote:
        status_map = {0: "正常交易", 1: "停牌", 2: "退市", 3: "熔断"}
        ts = realtime_quote.get("timestamp", 0)
        time_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else "未知"
        analysis_context += f"\n## 实时报价\n" \
                 f"- 最新价: {realtime_quote['latest']:.2f}（{realtime_quote['change_pct']:+.2%}）\n" \
                 f"- 开盘: {realtime_quote['open']:.2f} | 最高: {realtime_quote['high']:.2f} | 最低: {realtime_quote['low']:.2f}\n" \
                 f"- 前收盘: {realtime_quote['prev_close']:.2f} | 成交量: {realtime_quote['volume']:,.0f}\n" \
                 f"- 状态: {status_map.get(realtime_quote.get('status', 0), '未知')} | 更新时间: {time_str}\n"
    user_prompt = build_user_prompt(
        stock_info, technical_summary, news_aggregation,
        bt_summary, bt_table, alpha_text, data_info, analysis_context,
        swot_data=swot_data,
        peer_data=peer_data,
    )

    # 注入代码生成的操作方案（如有），LLM 只需解读
    if operation_plan:
        user_prompt = operation_plan + "\n\n" + user_prompt

    # 调用 LLM API（OpenAI 兼容格式，Ollama 通过 /v1 端点同样支持）
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
        logger.info(f"调用 LLM: model={model}, thinking={enable_thinking}")
        request_options = {}
        if enable_thinking:
            # DeepSeek extended thinking: 让模型在输出前先深度推理
            # 兼容 deepseek-chat (V3/V3.1) 和 deepseek-reasoner (R1)
            request_options["extra_body"] = {"thinking": {"type": "enabled"}}
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=9000,
            **request_options,
        )
        choice = response.choices[0]
        finish = choice.finish_reason
        if finish and finish != "stop":
            logger.warning(f"LLM 输出提前结束，finish_reason={finish}，报告可能不完整")
        else:
            logger.info(f"LLM 输出完成，finish_reason={finish}")
        report = _enforce_llm_compliance(
            _clean_llm_output(choice.message.content), operation_plan or ""
        )
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
                operation_plan=operation_plan,
            )
        # 确保 LLM 输出包含隐式分隔标记
        report = _ensure_section_marker(report)
        # 在第 7 章和第 8 章之间插入结构化数据（不经过 LLM，确保格式完整）
        structured = _build_structured_sections(
            market_regime, active_strategies, skipped_strategies, param_tuning)
        if structured and SECTION_8_MARKER in report:
            parts = report.split(SECTION_8_MARKER, 1)
            report = parts[0].strip() + "\n\n" + structured + "\n\n" + SECTION_8_MARKER + "\n\n" + parts[1].strip()
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
            operation_plan=operation_plan,
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
    operation_plan: str | None = None,
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
        from alpha.validation import factor_validation_coverage

        rows = []
        for col, v in validation.items():
            grade = v.get("grade", "?")
            mult = v.get("multiplier", 1.0)
            status = (
                "? 未验证（原权重）" if grade == "?"
                else ("✓ 全权" if mult >= 1.0 else ("△ 半权" if mult >= 0.5 else "✗ 剔除"))
            )
            rows.append(
                f"| {col} | {v.get('samples', 0)} | {v.get('IC', 0):+.4f} | "
                f"{v.get('IR', 0):+.2f} | {grade} | {status} |"
            )
        if rows:
            coverage = factor_validation_coverage(validation)
            validation_str = (
                "\n### 因子有效性检验\n\n"
                f"验证覆盖率：**{coverage:.0%}**。未验证因子保留原始研究权重，"
                "但执行可信度会降级。\n\n"
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
        s_score = score_style_factor(
            sf["pe_percentile"], sf["pb_percentile"], ff.get("ev_ebitda", 0))
        f_score = score_fundamental_factor(**{k: ff.get(k, 0) for k in
            ("roe", "gross_margin", "debt_ratio", "net_profit_yoy", "revenue_yoy")})

        ev_ebitda_str = f"| EV/EBITDA | {ff.get('ev_ebitda', 0):.1f} |" if ff.get("ev_ebitda", 0) > 0 else ""
        fund_str = (
            f"\n### 基本面与估值因子\n\n"
            f"**风格因子**（估值分位，高=偏空，低=偏多）\n"
            f"| PE(TTM) 分位 | PB 分位 | 风格得分 |\n"
            f"|-------------|---------|----------|\n"
            f"| {sf['pe_percentile']:.1%} | {sf['pb_percentile']:.1%} | {s_score:+.3f} |\n"
            f"{ev_ebitda_str}\n\n"
            f"**基本面因子**（最新一期）\n"
            f"| ROE | 毛利率 | 资产负债率 | 净利同比 | 营收同比 | 基本面得分 |\n"
            f"|-----|--------|-----------|----------|----------|----------|\n"
            f"| {ff['roe']:.1%} | {ff['gross_margin']:.1%} | {ff['debt_ratio']:.1%} | "
            f"{ff['net_profit_yoy']:+.1%} | {ff['revenue_yoy']:+.1%} | {f_score:+.3f} |\n"
        )

    # 策略对比表
    bt_table = _build_backtest_markdown_table(backtest_results)
    bt_summary = _build_backtest_summary(
        backtest_results, benchmark_return=benchmark_return,
        market_regime="unknown", strategy_meta=None,
    )

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
    if operation_plan:
        report = operation_plan + "\n\n" + report
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


def _build_structured_sections(
    market_regime: str = "unknown",
    active_strategies: list | None = None,
    skipped_strategies: list | None = None,
    param_tuning: dict | None = None,
) -> str:
    """生成结构化数据段（直接写入报告，不经过 LLM）。"""
    parts = []

    # 市场状态 + 策略适配
    if market_regime and market_regime != "unknown":
        regime_labels = {
            "trending_volatile": "强趋势+高波动", "trending_steady": "慢涨/弱趋势",
            "ranging": "震荡", "transitional": "趋势形成中",
        }
        label = regime_labels.get(market_regime, market_regime)
        parts.append(f"## 市场状态检测\n\n当前行情：**{label}**（{market_regime}）\n")
        if active_strategies or skipped_strategies:
            parts.append("| 策略 | 状态 | 说明 |\n|------|------|------|")
            for key in (active_strategies or []):
                s = get_execution_strategy(key)
                parts.append(f"| {key} {s.name} | ▶ 运行 | 适配当前行情 |")
            for key in (skipped_strategies or []):
                s = get_execution_strategy(key)
                regime_desc = ", ".join(s.suitable_regimes) if s.suitable_regimes else "全部行情"
                parts.append(f"| {key} {s.name} | ⏭ 跳过 | 适配 {regime_desc} |")
            parts.append("")

    # 策略参数优化
    if param_tuning:
        parts.append("## 策略参数优化\n")
        parts.append("| 策略 | 参数 | 默认值 | 最优值 |\n|------|------|--------|--------|")
        for key, info in param_tuning.items():
            s = get_execution_strategy(key)
            parts.append(f"| {key} {s.name} | {info['param']} | {info['default']:.2f} | {info['best_value']:.2f} |")
        parts.append("\n> 注：基于回测期内数据的参数扫描结果，最优值可能受过拟合影响。\n")

    return "\n".join(parts)


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


# ============================================================
# 盘中报告生成
# ============================================================

def generate_intraday_report(
    t1_report_content: str,
    snapshot_text: str,
    stock_info: dict,
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
    pre_report_content: str | None = None,
    operation_plan: str | None = None,
) -> str:
    """
    生成盘中分析报告。

    Args:
        t1_report_content: T-1 EOD 报告全文
        snapshot_text: 盘中实时快照
        stock_info: 股票基本信息
        swot_data: 实时 SWOT 素材
        peer_data: 同板块快速评分
        pre_report_content: 盘前报告全文（用于承上启下）
        operation_plan: 代码生成的操作方案（来自 T-1 EOD 分析）

    结构：
      ⚡ 盘中实时快照（纯计算，已格式化）
      → T-1 日报告第 1-7 章（复用）
      → 第八章：盘中操作参考（LLM 重新生成）
      → 第九章：SWOT 竞争分析（可选，如有补充数据）
      → 第十章：同板块关注（可选，如有补充数据）

    Args:
        t1_report_content: T-1 日完整报告的 Markdown 全文
        snapshot_text:     compute_intraday_snapshot() 返回的 Markdown 文本
        stock_info:        股票基本信息字典
        swot_data:         实时 SWOT 素材（可选）
        peer_data:         同板块快速评分（可选）
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
    enable_thinking = settings.get("llm_enable_thinking", False)

    # 尝试 LLM 生成第八章
    chapter_8 = None
    if api_key or "localhost" in base_url or "127.0.0.1" in base_url:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
            user_prompt = build_intraday_user_prompt(
                t1_report_content, snapshot_text, stock_info,
                swot_data=swot_data, peer_data=peer_data,
                pre_report_content=pre_report_content,
            )
            if operation_plan:
                user_prompt = operation_plan + "\n\n" + user_prompt
            logger.info(f"调用 LLM 生成盘中操作参考: model={model}, thinking={enable_thinking}")
            extra = {}
            if enable_thinking:
                extra["extra_body"] = {"thinking": {"type": "enabled"}}
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": INTRADAY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=6000,
                **extra,
            )
            choice = response.choices[0]
            finish = choice.finish_reason
            if finish and finish != "stop":
                logger.warning(f"LLM 盘中报告输出提前结束，finish_reason={finish}")
            chapter_8 = _enforce_llm_compliance(
                _clean_llm_output(choice.message.content), operation_plan or ""
            )
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
    swot_data: dict | None = None,
    peer_data: list[dict] | None = None,
    operation_plan: str | None = None,
) -> str:
    """
    生成盘前分析报告。

    结构：
      ⚡ 盘前快照（期货 + 盘前价格 + 隔夜新闻）
      → T-1 日报告第 1-7 章（复用）
      → 第八章：盘前策略参考（LLM 重新生成）
      → 第九章：SWOT 竞争分析（可选）
      → 第十章：同板块关注（可选）

    Args:
        t1_report_content: T-1 日完整报告的 Markdown 全文
        snapshot_text:     compute_premarket_snapshot() 返回的 Markdown 文本
        stock_info:        股票基本信息字典
        swot_data:         实时 SWOT 素材（可选）
        peer_data:         同板块快速评分（可选）
        operation_plan:    代码生成的操作方案（来自 T-1 EOD 分析）
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
    enable_thinking = settings.get("llm_enable_thinking", False)

    # 尝试 LLM 生成第八章
    chapter_8 = None
    if api_key or "localhost" in base_url or "127.0.0.1" in base_url:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
            user_prompt = build_premarket_user_prompt(
                t1_report_content, snapshot_text, stock_info,
                swot_data=swot_data, peer_data=peer_data,
            )
            if operation_plan:
                user_prompt = operation_plan + "\n\n" + user_prompt
            logger.info(f"调用 LLM 生成盘前策略参考: model={model}, thinking={enable_thinking}")
            extra = {}
            if enable_thinking:
                extra["extra_body"] = {"thinking": {"type": "enabled"}}
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": PREMARKET_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=7000,
                **extra,
            )
            choice = response.choices[0]
            finish = choice.finish_reason
            if finish and finish != "stop":
                logger.warning(f"LLM 盘前报告输出提前结束，finish_reason={finish}")
            chapter_8 = _enforce_llm_compliance(
                _clean_llm_output(choice.message.content), operation_plan or ""
            )
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


# ============================================================
#  持仓综合分析报告生成
# ============================================================

def generate_portfolio_report(
    balance: dict,
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    period: str,
    mode: str = "eod",
    portfolio_operation_plan: str = "",
) -> str:
    """生成持仓综合分析报告。

    Args:
        balance: {"us_balance": float, "a_balance": float}
        holdings_data: 每只持仓的完整分析数据列表
        watchlist_data: 每只关注股的完整分析数据列表
        market: "US" | "A"
        period: 回测周期
        mode: 分析模式
        portfolio_operation_plan: 组合级操作方案 Markdown（代码生成）

    Returns:
        完整的 Markdown 报告字符串
    """
    settings = Settings()
    api_key = settings.get("llm_api_key", "")
    base_url = settings.get("llm_base_url", "https://api.openai.com/v1")
    model = settings.get("llm_model", "gpt-4o")
    enable_thinking = settings.get("llm_enable_thinking", False)

    if not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
        return _generate_fallback_portfolio_report(
            balance, holdings_data, watchlist_data, market, period, mode,
            portfolio_operation_plan=portfolio_operation_plan,
        )

    user_prompt = build_portfolio_user_prompt(
        balance, holdings_data, watchlist_data, market, period, mode
    )

    # 注入代码生成的组合操作方案（如有），LLM 只需解读
    if portfolio_operation_plan:
        user_prompt = portfolio_operation_plan + "\n\n" + user_prompt

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
        logger.info(f"调用 LLM (持仓分析): model={model}, thinking={enable_thinking}")
        extra = {}
        if enable_thinking:
            extra["extra_body"] = {"thinking": {"type": "enabled"}}
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": PORTFOLIO_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=9000,
            **extra,
        )
        choice = response.choices[0]
        finish = choice.finish_reason
        if finish and finish != "stop":
            logger.warning(f"LLM 输出提前结束，finish_reason={finish}，报告可能不完整")
        else:
            logger.info(f"LLM 输出完成，finish_reason={finish}")
        report = _enforce_llm_compliance(
            _clean_llm_output(choice.message.content), portfolio_operation_plan or ""
        )
        if not report:
            logger.warning("LLM returned empty portfolio report, falling back")
            return _generate_fallback_portfolio_report(
                balance, holdings_data, watchlist_data, market, period, mode
            )
        logger.info(f"Portfolio report generated by LLM: {len(report)} chars")
        return report

    except Exception as e:
        logger.error(f"LLM portfolio report generation failed: {e}")
        return _generate_fallback_portfolio_report(
            balance, holdings_data, watchlist_data, market, period, mode
        )


def _generate_fallback_portfolio_report(
    balance: dict,
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    period: str,
    mode: str = "eod",
    portfolio_operation_plan: str = "",
) -> str:
    """本地模板兜底的持仓综合分析报告。"""
    market_label = "美股" if market == "US" else "A股"
    currency = "$" if market == "US" else "¥"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cash = balance.get("us_balance" if market == "US" else "a_balance", 0)

    lines = [
        f"# {market_label}持仓综合分析报告",
        f"",
        f"> 生成时间：{now_str}",
        f"> 回测周期：{period} | 分析模式：{mode}",
        f"> ⚠️ 当前未配置 LLM API Key，以下是基于回测数据的自动汇总。",
        f"",
        f"## 一、账户概览",
        f"",
        f"- {market_label}可用资金：**{currency}{cash:,.2f}**",
        f"",
        f"## 二、持仓个股",
    ]

    for hd in holdings_data:
        h = hd["holding"]
        cp = hd.get("current_price")
        pnl_str = ""
        if cp and cp > 0:
            pnl = (cp - h.cost_price) / h.cost_price
            pnl_str = f"（浮盈/亏 {pnl:+.2%}）"
        lines.append(f"- **{h.code} {h.name}**：{h.shares:,.0f} 股，成本 {currency}{h.cost_price:.2f} {pnl_str}")

    lines.append("")
    lines.append("## 三、关注股票")
    if watchlist_data:
        for wd in watchlist_data:
            w = wd["watch_item"]
            cp = wd.get("current_price")
            price_str = f"当前价 {currency}{cp:.2f}" if cp else "价格未获取"
            lines.append(f"- **{w.code} {w.name}**：{price_str}")
    else:
        lines.append("暂无关注股票。")

    lines.append("")
    lines.append("## 四、历史策略适配对照（非资产质量排名）")
    lines.append(
        "> 仅表示历史回测中风险调整后表现较好的策略，不代表当前资产质量或买卖顺序。"
    )
    all_stocks = []
    for hd in holdings_data:
        h = hd["holding"]
        best_name, best = max(
            (hd.get("backtest") or {}).items(),
            key=lambda pair: (pair[1].sharpe_ratio, pair[1].total_return),
            default=("—", None),
        )
        all_stocks.append((h.code, h.name, "持仓", best_name, best))
    for wd in watchlist_data:
        w = wd["watch_item"]
        best_name, best = max(
            (wd.get("backtest") or {}).items(),
            key=lambda pair: (pair[1].sharpe_ratio, pair[1].total_return),
            default=("—", None),
        )
        all_stocks.append((w.code, w.name, "关注", best_name, best))

    lines.append("| 类型 | 代码 | 名称 | 历史适配策略 | 收益 | 夏普 | 最大回撤 |")
    lines.append("|------|------|------|------|------:|------:|------:|")
    for code, name, stype, strategy_name, result in all_stocks:
        ret = result.total_return if result else 0.0
        sharpe = result.sharpe_ratio if result else 0.0
        drawdown = result.max_drawdown if result else 0.0
        lines.append(
            f"| {stype} | {code} | {name} | {strategy_name} | "
            f"{ret*100:+.2f}% | {sharpe:.2f} | {drawdown*100:.2f}% |"
        )

    lines.append("")
    lines.append("---")
    lines.append("> ⚠️ **免责声明**：以上分析仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。")
    lines.append(f"> 请配置 LLM API Key 以获取 AI 综合调仓方案。")

    result = "\n".join(lines)
    if portfolio_operation_plan:
        result = portfolio_operation_plan + "\n\n" + result
    return result
