"""Optional LLM report editor; failures always fall back to deterministic prose."""
from __future__ import annotations

import logging
from time import monotonic
from urllib.parse import urlparse

from presentation.editor import build_editor_prompt, parse_editor_response
from research.client import capabilities_for_endpoint, openai_compatible_transport, openai_response_content


logger = logging.getLogger(__name__)


def edit_report(document, settings, *, transport=openai_compatible_transport):
    endpoint = getattr(settings, "llm_base_url", None)
    api_key = getattr(settings, "llm_api_key", None)
    model = getattr(settings, "llm_model", None)
    if not endpoint or not api_key or not model:
        return None
    if (urlparse(endpoint).hostname or "").endswith(".invalid"):
        return None
    prompt, prompt_hash = build_editor_prompt(document)
    capabilities = capabilities_for_endpoint(endpoint)
    thinking = bool(getattr(settings, "llm_enable_thinking", False) and capabilities.supports_thinking)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a report editor. Return only strict JSON. Never change frozen trading facts."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 6000 if thinking else 3000,
    }
    if capabilities.supports_thinking:
        body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if capabilities.supports_temperature and not thinking:
        body["temperature"] = 0.0
    started = monotonic()
    try:
        raw = transport(endpoint, body, api_key, 90)
        finish = raw.get("finish_reason")
        if finish not in {None, "stop", "end_turn", "completed"}:
            logger.warning("Report editor truncated report_id=%s finish=%s prompt_hash=%s", document.report_id, finish, prompt_hash)
            return None
        narrative = parse_editor_response(openai_response_content(raw), document)
    except Exception as exc:
        logger.warning(
            "Report editor fallback report_id=%s error_type=%s error=%s duration_seconds=%.3f",
            document.report_id, type(exc).__name__, exc, monotonic() - started,
        )
        return None
    logger.info(
        "Report editor completed report_id=%s instruments=%d prompt_hash=%s duration_seconds=%.3f",
        document.report_id, len(narrative.items), prompt_hash, monotonic() - started,
    )
    return narrative
