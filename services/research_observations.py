"""
研究员观察候选池。

LLM 可以提出“值得观察的机会/风险”，但不能直接下交易指令。本模块把
LLM 或系统规则产生的观察候选转成结构化记录，再用已有事实数据进行确认、
降级或驳回，最后输出给用户可见的“研究员观察 vs 系统确认”章节。
"""

from dataclasses import dataclass
import re

from data.models import ResearchObservationLog


@dataclass
class ResearchObservation:
    code: str
    name: str
    observation: str
    source: str = "LLM"
    system_status: str = "待验证"
    execution_level: str = "C"
    next_step: str = "等待系统条件确认"
    reason: str = ""
    pattern_type: str = "general"
    trigger_price: float = 0.0
    stop_loss: float = 0.0
    expected_direction: str = "neutral"
    llm_proposed: int = 0


def parse_llm_observations(llm_report: str) -> list[ResearchObservation]:
    """从 LLM 报告中解析“研究员观察候选”表格。"""
    if not llm_report:
        return []

    lines = llm_report.splitlines()
    start = -1
    for i, line in enumerate(lines):
        if "研究员观察" in line and ("候选" in line or "系统确认" in line):
            start = i
            break
    if start < 0:
        return []

    observations: list[ResearchObservation] = []
    for raw in lines[start + 1:]:
        line = raw.strip()
        if line.startswith("##") and observations:
            break
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if any(h in cells[0] for h in ("股票", "代码")):
            continue
        code, name = _split_symbol(cells[0])
        observation = cells[1]
        if not code or not observation:
            continue
        observations.append(ResearchObservation(
            code=code,
            name=name or code,
            observation=observation,
            source="LLM",
            pattern_type=_observation_type(observation),
            llm_proposed=1,
        ))
    return observations


def build_research_confirmation_section(
    holdings_data: list[dict],
    watchlist_data: list[dict],
    llm_report: str = "",
) -> str:
    """构建“研究员观察 vs 系统确认”章节。"""
    rows = confirm_research_observations(holdings_data, watchlist_data, llm_report)
    return build_research_confirmation_markdown(rows)


def confirm_research_observations(
    holdings_data: list[dict],
    watchlist_data: list[dict],
    llm_report: str = "",
) -> list[ResearchObservation]:
    """生成并确认研究员观察候选，返回结构化结果。"""
    all_data = holdings_data + watchlist_data
    if not all_data:
        return []

    parsed = parse_llm_observations(llm_report)
    rule_based = _rule_based_observations(all_data)
    candidates = _dedupe_observations(parsed + rule_based)
    if not candidates:
        return []

    return [_validate_observation(obs, all_data) for obs in candidates]


def build_research_confirmation_markdown(rows: list[ResearchObservation]) -> str:
    """把已确认/降级的观察候选渲染成 Markdown。"""
    if not rows:
        return ""

    lines = [
        "\n---\n",
        "## 🧪 研究员观察 vs 系统确认\n",
        "> LLM/研究员可以提出观察候选，但不能直接生成交易指令；以下执行等级由代码系统按事实、风险和历史/策略状态确认。\n",
        "| 股票 | 来源 | 研究员观察 | 系统状态 | 执行级别 | 下一步 | 系统理由 |",
        "|------|------|------|------|:---:|------|------|",
    ]
    for obs in rows:
        lines.append(
            f"| {obs.name}（{obs.code}） | {obs.source} | {_clip(obs.observation, 56)} | "
            f"{obs.system_status} | {obs.execution_level} | {_clip(obs.next_step, 62)} | "
            f"{_clip(obs.reason, 72)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_research_history_markdown(
    observations: list[ResearchObservation],
    get_stats,
) -> str:
    """构建观察形态历史表现摘要。"""
    seen = set()
    rows = []
    for obs in observations:
        key = (obs.code, obs.pattern_type)
        if key in seen or not obs.code or not obs.pattern_type:
            continue
        seen.add(key)
        stats = get_stats(obs.code, obs.pattern_type)
        if not stats or int(stats.get("count", 0) or 0) <= 0:
            continue
        rows.append((obs, stats))

    if not rows:
        return ""

    lines = [
        "### 📚 观察形态历史表现\n",
        "| 股票 | 形态 | 样本 | 5日胜率 | 5日均值 | 10日均值 | 最大不利波动 | 正期望状态 |",
        "|------|------|------:|------:|------:|------:|------:|------|",
    ]
    label_map = {
        "profit_lock": "冲高回落锁利",
        "ma120_support": "MA120支撑",
        "momentum": "动量观察",
        "strategy_signal": "策略信号",
    }
    expectancy_map = {
        "positive": "正期望",
        "negative": "负期望",
        "insufficient": "样本不足",
    }
    for obs, stats in rows:
        lines.append(
            f"| {obs.name}（{obs.code}） | {label_map.get(obs.pattern_type, obs.pattern_type)} | "
            f"{int(stats.get('count', 0) or 0)} | "
            f"{float(stats.get('win_rate_5d', 0) or 0):.0%} | "
            f"{float(stats.get('avg_return_5d', 0) or 0):+.2%} | "
            f"{float(stats.get('avg_return_10d', 0) or 0):+.2%} | "
            f"{float(stats.get('avg_adverse', 0) or 0):+.2%} | "
            f"{expectancy_map.get(stats.get('expectancy'), stats.get('expectancy', '—'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def apply_history_feedback(
    observations: list[ResearchObservation],
    get_stats,
) -> list[ResearchObservation]:
    """用历史表现修正风控官执行等级。"""
    for obs in observations:
        if not obs.code or not obs.pattern_type:
            continue
        stats = get_stats(obs.code, obs.pattern_type)
        if not stats:
            continue
        count = int(stats.get("count", 0) or 0)
        expectancy = stats.get("expectancy", "insufficient")
        win_rate = float(stats.get("win_rate_5d", 0) or 0)
        avg_5d = float(stats.get("avg_return_5d", 0) or 0)
        if count < 3:
            continue

        if expectancy == "negative":
            old = obs.execution_level
            obs.execution_level = "D" if old == "C" else "C"
            obs.system_status = "历史负期望降级"
            obs.next_step = "不执行，等待该形态历史表现改善或出现更强系统信号"
            obs.reason = (
                f"{obs.reason}；历史样本{count}次，5日胜率{win_rate:.0%}，"
                f"5日均值{avg_5d:+.2%}，风控官降级"
            )
        elif expectancy == "positive" and obs.execution_level == "C":
            obs.execution_level = "B"
            obs.system_status = "历史正期望增强"
            obs.reason = (
                f"{obs.reason}；历史样本{count}次，5日胜率{win_rate:.0%}，"
                f"5日均值{avg_5d:+.2%}，允许小仓验证"
            )
        elif expectancy == "positive" and obs.execution_level == "B":
            obs.reason = (
                f"{obs.reason}；历史样本{count}次，5日胜率{win_rate:.0%}，"
                f"5日均值{avg_5d:+.2%}"
            )
    return observations


def observations_to_logs(
    observations: list[ResearchObservation],
    market: str,
    mode: str,
    report_id: int | None,
    observed_at: str,
) -> list[ResearchObservationLog]:
    """转换为数据库日志模型。"""
    logs: list[ResearchObservationLog] = []
    for obs in observations:
        if obs.execution_level == "D" and obs.system_status == "数据冲突":
            continue
        logs.append(ResearchObservationLog(
            code=obs.code,
            name=obs.name,
            market=market,
            mode=mode,
            report_id=report_id,
            observed_at=observed_at,
            pattern_type=obs.pattern_type or _observation_type(obs.observation),
            observation=obs.observation,
            source=obs.source,
            system_status=obs.system_status,
            execution_level=obs.execution_level,
            trigger_price=obs.trigger_price,
            stop_loss=obs.stop_loss,
            expected_direction=obs.expected_direction,
            llm_proposed=obs.llm_proposed,
        ))
    return logs


def _rule_based_observations(all_data: list[dict]) -> list[ResearchObservation]:
    observations: list[ResearchObservation] = []
    for item in all_data:
        obj = item.get("holding") or item.get("watch_item")
        if not obj:
            continue
        code = getattr(obj, "code", "")
        name = getattr(obj, "name", "") or code
        marker = item.get("technical_marker") or {}
        price = float(item.get("current_price") or marker.get("close") or 0)
        alpha = float(item.get("alpha_score") or 0)

        high = float(marker.get("high") or 0)
        close = float(marker.get("close") or price or 0)
        high_120 = float(marker.get("high_120") or 0)
        holding = item.get("holding")
        cost = float(getattr(holding, "cost_price", 0) or 0) if holding else 0
        if holding and high > 0 and close > 0 and cost > 0:
            profit_pct = (close - cost) / cost
            pullback = (close - high) / high
            if profit_pct >= 0.10 and high_120 > 0 and high >= high_120 * 0.995 and pullback <= -0.035:
                observations.append(ResearchObservation(
                    code=code,
                    name=name,
                    observation="冲高后明显回落，适合进入锁利观察",
                    source="系统研究员",
                    pattern_type="profit_lock",
                ))

        low = float(marker.get("low") or 0)
        ma120 = float(marker.get("ma_120") or 0)
        if low > 0 and ma120 > 0 and low <= ma120 * 1.01 and low >= ma120 * 0.97:
            observations.append(ResearchObservation(
                code=code,
                name=name,
                observation="价格触碰 MA120 附近，可能存在半年线支撑反弹机会",
                source="系统研究员",
                pattern_type="ma120_support",
            ))

        if price > 0 and alpha < -0.20 and _has_positive_momentum(marker, price):
            observations.append(ResearchObservation(
                code=code,
                name=name,
                observation="短线动量较强但 Alpha 偏空，追高点子需要系统反驳或降级",
                source="系统研究员",
                pattern_type="momentum",
            ))
    return observations


def _validate_observation(
    obs: ResearchObservation,
    all_data: list[dict],
) -> ResearchObservation:
    item = _find_item(obs, all_data)
    if not item:
        obs.system_status = "数据冲突"
        obs.execution_level = "D"
        obs.next_step = "不执行，等待数据匹配"
        obs.reason = "报告数据中找不到该股票"
        obs.pattern_type = obs.pattern_type or _observation_type(obs.observation)
        return obs
    obj = item.get("holding") or item.get("watch_item")
    if obj:
        obs.code = getattr(obj, "code", obs.code) or obs.code
        obs.name = getattr(obj, "name", obs.name) or obs.name

    marker = item.get("technical_marker") or {}
    price = float(item.get("current_price") or marker.get("close") or 0)
    alpha = float(item.get("alpha_score") or 0)
    text = obs.observation.lower()

    if _mentions_profit_lock(text):
        return _validate_profit_lock(obs, item, marker)
    if _mentions_ma120_support(text):
        return _validate_ma120(obs, item, marker)
    if _mentions_momentum(text):
        return _validate_momentum(obs, marker, price, alpha)

    signals = item.get("signal_check") or []
    executable = next((s for s in signals if s.get("signal") in ("buy", "sell")), None)
    if executable:
        obs.system_status = "已确认"
        obs.execution_level = executable.get("execution_level") or "B"
        obs.next_step = executable.get("reason") or "按系统策略触发条件执行"
        obs.reason = f"存在系统策略信号：{executable.get('name') or executable.get('strategy_name')}"
        obs.pattern_type = obs.pattern_type or "strategy_signal"
        obs.trigger_price = float(executable.get("entry_price") or executable.get("trigger_price") or price or 0)
        obs.stop_loss = float(executable.get("stop_loss") or 0)
        obs.expected_direction = "bearish" if executable.get("signal") == "sell" else "bullish"
    else:
        obs.system_status = "待验证"
        obs.execution_level = "C"
        obs.next_step = "保留观察，等待系统策略或关键价位确认"
        obs.reason = "当前没有可执行策略信号，不能把观察直接升级为交易指令"
        obs.pattern_type = obs.pattern_type or "general"
        obs.trigger_price = price
        obs.expected_direction = "neutral"
    return obs


def _validate_profit_lock(
    obs: ResearchObservation,
    item: dict,
    marker: dict,
) -> ResearchObservation:
    holding = item.get("holding")
    high = float(marker.get("high") or 0)
    close = float(marker.get("close") or item.get("current_price") or 0)
    high_120 = float(marker.get("high_120") or 0)
    cost = float(getattr(holding, "cost_price", 0) or 0) if holding else 0
    if not holding or high <= 0 or close <= 0 or cost <= 0:
        obs.system_status = "数据冲突"
        obs.execution_level = "D"
        obs.next_step = "不执行，等待持仓成本和日内高低点数据"
        obs.reason = "锁利观察必须有真实持仓、成本价和日内高点"
        return obs

    profit_pct = (close - cost) / cost
    pullback = (close - high) / high
    near_high = high_120 > 0 and high >= high_120 * 0.995
    if profit_pct >= 0.10 and pullback <= -0.035 and near_high:
        lock_line = high * 0.965
        obs.system_status = "已确认"
        obs.execution_level = "A"
        obs.next_step = f"跌破锁利线 {lock_line:.2f} 后部分止盈或上移止损"
        obs.reason = f"浮盈{profit_pct:.1%}，当日高点{high:.2f}接近120日高点，回落{pullback:.1%}"
        obs.pattern_type = "profit_lock"
        obs.trigger_price = close
        obs.stop_loss = lock_line
        obs.expected_direction = "bearish"
    else:
        obs.system_status = "待验证"
        obs.execution_level = "C"
        obs.next_step = "继续观察是否接近阶段高点且从高点回落≥3.5%"
        obs.reason = f"浮盈{profit_pct:.1%}，高点回落{pullback:.1%}，尚未满足锁利规则"
        obs.pattern_type = "profit_lock"
        obs.trigger_price = close
        obs.stop_loss = high * 0.965 if high > 0 else 0.0
        obs.expected_direction = "bearish"
    return obs


def _validate_ma120(
    obs: ResearchObservation,
    item: dict,
    marker: dict,
) -> ResearchObservation:
    price = float(item.get("current_price") or marker.get("close") or 0)
    close = float(marker.get("close") or price or 0)
    low = float(marker.get("low") or 0)
    ma120 = float(marker.get("ma_120") or 0)
    alpha = float(item.get("alpha_score") or 0)
    if price <= 0 or low <= 0 or ma120 <= 0:
        obs.system_status = "数据冲突"
        obs.execution_level = "D"
        obs.next_step = "不执行，等待 MA120/低点/现价数据"
        obs.reason = "MA120 支撑观察缺少必要价格数据"
        return obs

    touched = low <= ma120 * 1.01 and low >= ma120 * 0.97
    reclaimed = close >= ma120 or price >= ma120
    if touched and reclaimed and alpha >= -0.35:
        obs.system_status = "已确认"
        obs.execution_level = "B"
        obs.next_step = f"站回 MA120={ma120:.2f} 后仅小仓验证，跌破 {ma120*0.98:.2f} 失效"
        obs.reason = f"低点{low:.2f}触碰 MA120={ma120:.2f}，现价/收盘已重新站回"
        obs.pattern_type = "ma120_support"
        obs.trigger_price = price or close
        obs.stop_loss = ma120 * 0.98
        obs.expected_direction = "bullish"
    elif touched:
        obs.system_status = "待验证"
        obs.execution_level = "C"
        obs.next_step = f"重新站回 MA120={ma120:.2f} 后再评估小仓验证"
        obs.reason = f"低点{low:.2f}触碰 MA120，但现价/收盘仍未重新站回"
        obs.pattern_type = "ma120_support"
        obs.trigger_price = ma120
        obs.stop_loss = ma120 * 0.98
        obs.expected_direction = "bullish"
    else:
        obs.system_status = "系统反驳"
        obs.execution_level = "D"
        obs.next_step = "不执行，等待真实触碰 MA120"
        obs.reason = f"低点{low:.2f}并未触碰 MA120={ma120:.2f} 附近"
        obs.pattern_type = "ma120_support"
        obs.trigger_price = ma120
        obs.stop_loss = ma120 * 0.98
        obs.expected_direction = "bullish"
    return obs


def _validate_momentum(
    obs: ResearchObservation,
    marker: dict,
    price: float,
    alpha: float,
) -> ResearchObservation:
    if price <= 0:
        obs.system_status = "数据冲突"
        obs.execution_level = "D"
        obs.next_step = "不执行，等待现价数据"
        obs.reason = "缺少当前价格"
        obs.pattern_type = "momentum"
        return obs
    if alpha < -0.20:
        obs.system_status = "系统反驳"
        obs.execution_level = "D"
        obs.next_step = "不追高；只有 Alpha 转正且回撤后重新确认，才重新评估"
        obs.reason = f"Alpha={alpha:+.3f} 偏空，动量观察不能升级为交易指令"
        obs.pattern_type = "momentum"
        obs.trigger_price = price
        obs.expected_direction = "bullish"
        return obs
    if _has_positive_momentum(marker, price):
        obs.system_status = "待验证"
        obs.execution_level = "C"
        obs.next_step = "等待系统趋势策略给出买入触发和明确止损"
        obs.reason = "存在短线动量，但缺少系统策略确认和风险金额"
        obs.pattern_type = "momentum"
        obs.trigger_price = price
        obs.expected_direction = "bullish"
        return obs
    obs.system_status = "系统反驳"
    obs.execution_level = "D"
    obs.next_step = "不执行"
    obs.reason = "动量事实不成立"
    obs.pattern_type = "momentum"
    obs.trigger_price = price
    obs.expected_direction = "bullish"
    return obs


def _find_item(obs: ResearchObservation, all_data: list[dict]) -> dict | None:
    target = (obs.code or "").upper()
    target_name = obs.name or ""
    for item in all_data:
        obj = item.get("holding") or item.get("watch_item")
        if not obj:
            continue
        code = getattr(obj, "code", "")
        name = getattr(obj, "name", "")
        if code.upper() == target or name == target_name or name == obs.code:
            return item
    return None


def _dedupe_observations(observations: list[ResearchObservation]) -> list[ResearchObservation]:
    result: list[ResearchObservation] = []
    seen = set()
    for obs in observations:
        key = (obs.code.upper(), _observation_type(obs.observation))
        if key in seen:
            continue
        seen.add(key)
        result.append(obs)
    return result


def _split_symbol(value: str) -> tuple[str, str]:
    value = value.strip()
    match = re.match(r"(.+?)（([A-Za-z0-9.\-]+)）", value)
    if match:
        return match.group(2).strip(), match.group(1).strip()
    match = re.match(r"([A-Za-z0-9.\-]+)\s*[-/]\s*(.+)", value)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return value.strip(), value.strip()


def _observation_type(text: str) -> str:
    lower = text.lower()
    if _mentions_profit_lock(lower):
        return "profit_lock"
    if _mentions_ma120_support(lower):
        return "ma120"
    if _mentions_momentum(lower):
        return "momentum"
    return re.sub(r"\s+", "", lower)[:24]


def _mentions_profit_lock(text: str) -> bool:
    return any(k in text for k in ("锁利", "止盈", "冲高", "回落", "profit"))


def _mentions_ma120_support(text: str) -> bool:
    return any(k in text for k in ("ma120", "半年线", "支撑"))


def _mentions_momentum(text: str) -> bool:
    return any(k in text for k in ("动量", "追", "突破", "momentum"))


def _has_positive_momentum(marker: dict, price: float) -> bool:
    ma20 = float(marker.get("ma_20") or 0)
    ma60 = float(marker.get("ma_60") or 0)
    high = float(marker.get("high") or 0)
    return (ma20 > 0 and price > ma20) or (ma60 > 0 and price > ma60) or (high > 0 and price >= high * 0.98)


def _clip(text: str, length: int) -> str:
    text = str(text or "").replace("|", "/").strip()
    return text if len(text) <= length else text[:length - 3] + "..."
