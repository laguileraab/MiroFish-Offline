"""LLM adjudication for ambiguous entity merge pairs (Phase 11 TASK-036)."""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("mirofish.merge_adjudicator")

_PROMPT = """Are these two names referring to the same real-world entity (same company, person, or organization)?

Name A: {name_a}
Context A: {ctx_a}

Name B: {name_b}
Context B: {ctx_b}

Reply with JSON only: {{"same": true}} or {{"same": false}}."""


def llm_same_real_world_entity(
    llm: Any,
    name_a: str,
    ctx_a: str,
    name_b: str,
    ctx_b: str,
) -> bool:
    try:
        messages = [
            {"role": "system", "content": "You only output valid JSON."},
            {
                "role": "user",
                "content": _PROMPT.format(
                    name_a=name_a[:200],
                    ctx_a=(ctx_a or "")[:400],
                    name_b=name_b[:200],
                    ctx_b=(ctx_b or "")[:400],
                ),
            },
        ]
        out: Dict[str, Any] = llm.chat_json(messages, temperature=0.0, max_tokens=64)
        return bool(out.get("same"))
    except Exception as e:
        logger.warning("Merge adjudication LLM failed: %s", e)
        return False
