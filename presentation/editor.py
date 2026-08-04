"""Strict report-editor contract for explaining frozen decisions in plain Chinese."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from contracts import ContractViolation, ReportDocument, ReportBlockKind, canonical_json, stable_hash


EDITOR_PROMPT_VERSION = "report_editor_v1"
_FORBIDDEN_CLAIMS = (
    "保证盈利", "稳赚", "必涨", "必跌", "没有风险", "肯定赚钱", "确定上涨", "确定下跌",
)
_ACTION_WORDS = frozenset(("买入", "加仓", "卖出", "减仓", "持有", "观察"))


@dataclass(frozen=True, slots=True)
class EditorialItem:
    instrument: str
    action: str
    headline: str
    reasons: tuple[str, ...]
    risk_note: str

    def __post_init__(self):
        reasons = tuple(item.strip() for item in self.reasons if item.strip())
        if (
            not self.instrument or self.action not in _ACTION_WORDS
            or not self.headline.strip() or not self.risk_note.strip()
            or not 1 <= len(reasons) <= 3
            or len(self.headline) > 90 or len(self.risk_note) > 100
            or any(len(item) > 100 for item in reasons)
        ):
            raise ContractViolation("invalid report editorial item")
        text = " ".join((self.headline, *reasons, self.risk_note))
        if any(char.isdigit() for char in text) or any(claim in text for claim in _FORBIDDEN_CLAIMS):
            raise ContractViolation("report editorial text may not invent numbers or guarantees")
        conflicting = _ACTION_WORDS.intersection(word for word in _ACTION_WORDS if word in text)
        if any(word != self.action for word in conflicting):
            raise ContractViolation("report editorial action conflicts with frozen decision")
        object.__setattr__(self, "headline", self.headline.strip())
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "risk_note", self.risk_note.strip())


@dataclass(frozen=True, slots=True)
class EditorialNarrative:
    overview: str
    items: tuple[EditorialItem, ...]
    source: str = "LLM 报告编辑员"

    def __post_init__(self):
        items = tuple(sorted(self.items, key=lambda item: item.instrument))
        if not self.overview.strip() or len(self.overview) > 180 or any(char.isdigit() for char in self.overview):
            raise ContractViolation("invalid report editorial overview")
        if any(claim in self.overview for claim in _FORBIDDEN_CLAIMS):
            raise ContractViolation("report editorial overview contains a prohibited claim")
        if len({item.instrument for item in items}) != len(items):
            raise ContractViolation("duplicate report editorial instrument")
        object.__setattr__(self, "overview", self.overview.strip())
        object.__setattr__(self, "items", items)


def _action_rows(document: ReportDocument) -> tuple[dict[str, str], ...]:
    # The detailed editorial table contains every portfolio member while the
    # first-screen quick-action table is intentionally capped at five items.
    for section in document.sections:
        for block in section.blocks:
            if block.kind is not ReportBlockKind.TABLE or block.payload.table_id != "operation_editorial":
                continue
            return tuple({
                "instrument": row.cells[0],
                "identity": row.cells[1],
                "action": row.cells[2],
                "system_judgment": row.cells[3],
                "next_step": f"{row.cells[4]}\n{row.cells[5]}",
                "profiles": "具体价格、数量和风险以冻结操作卡为准",
            } for row in block.payload.rows)
    table_ids = {"portfolio_quick_actions", "single_quick_action"}
    for section in document.sections:
        for block in section.blocks:
            if block.kind is not ReportBlockKind.TABLE or block.payload.table_id not in table_ids:
                continue
            rows = []
            for row in block.payload.rows:
                stock, identity, action_text, next_step, profiles = row.cells
                action = action_text.split("；", 1)[0].strip()
                rows.append({
                    "instrument": stock,
                    "identity": identity,
                    "action": action,
                    "system_judgment": action_text.split("；", 1)[-1].strip(),
                    "next_step": next_step,
                    "profiles": profiles,
                })
            return tuple(rows)
    return ()


def build_editor_prompt(document: ReportDocument) -> tuple[str, str]:
    actions = _action_rows(document)
    if not actions:
        raise ContractViolation("report has no frozen action rows for editorial explanation")
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["overview", "items"],
        "properties": {
            "overview": {"type": "string", "maxLength": 180},
            "items": {
                "type": "array", "minItems": len(actions), "maxItems": len(actions),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["instrument", "action", "headline", "reasons", "risk_note"],
                    "properties": {
                        "instrument": {"type": "string"},
                        "action": {"enum": sorted(_ACTION_WORDS)},
                        "headline": {"type": "string", "maxLength": 90},
                        "reasons": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 100}},
                        "risk_note": {"type": "string", "maxLength": 100},
                    },
                },
            },
        },
    }
    payload = {
        "task": (
            "你是报告编辑员，只负责把冻结的交易结论讲清楚。不得改变动作，不得新增价格、股数、收益率、"
            "止损、止盈或交易条件；输出中不得出现任何阿拉伯数字，不得承诺收益。headline 要给完整结论，"
            "reasons 解释预测、策略与风控的逻辑，risk_note 强调等待、复核或风险边界。只返回 JSON。"
        ),
        "report": {"kind": document.report_kind.value, "market": document.market.value, "mode": document.analysis_mode.value},
        "frozen_actions": actions,
        "output_schema": schema,
    }
    prompt = canonical_json(payload)
    return prompt, stable_hash({"version": EDITOR_PROMPT_VERSION, "prompt": prompt})


def parse_editor_response(content: str, document: ReportDocument) -> EditorialNarrative:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractViolation("report editor did not return strict JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"overview", "items"} or not isinstance(raw["items"], list):
        raise ContractViolation("invalid report editor response shape")
    expected = {item["instrument"]: item["action"] for item in _action_rows(document)}
    items = []
    for raw_item in raw["items"]:
        if not isinstance(raw_item, dict) or set(raw_item) != {"instrument", "action", "headline", "reasons", "risk_note"}:
            raise ContractViolation("invalid report editor item shape")
        instrument = str(raw_item["instrument"])
        action = str(raw_item["action"])
        if expected.get(instrument) != action or not isinstance(raw_item["reasons"], list):
            raise ContractViolation("report editor changed a frozen instrument or action")
        items.append(EditorialItem(
            instrument, action, str(raw_item["headline"]), tuple(str(item) for item in raw_item["reasons"]), str(raw_item["risk_note"]),
        ))
    if {item.instrument for item in items} != set(expected):
        raise ContractViolation("report editor omitted a frozen action")
    return EditorialNarrative(str(raw["overview"]), tuple(items))
