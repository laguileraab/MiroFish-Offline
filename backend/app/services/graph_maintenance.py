"""
Graph maintenance helpers (Phase 11 TASK-038).

Optional entity summary refresh from connected relation facts — callable from ingest or admin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..config import Config

logger = logging.getLogger("mirofish.graph_maintenance")

_MAX_FACTS = 12
_SUMMARY_PROMPT = """You are compressing knowledge-graph evidence into a short entity summary.

Entity name: {name}
Entity type: {etype}

Facts (from graph edges):
{facts}

Write ONE concise English sentence (max 40 words) describing what this entity is or does per the facts.
Do not invent facts not supported above. If facts conflict, mention uncertainty briefly.

Return ONLY valid JSON: {{"summary": "..."}}"""


def fetch_entity_fact_bullets(driver, graph_id: str, entity_uuid: str, limit: int = _MAX_FACTS) -> List[str]:
    """Load fact strings from relations touching this entity."""
    tw = ""
    if Config.GRAPH_TEMPORAL_ENABLED and Config.GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY:
        tw = " AND r.invalid_at IS NULL"
    q = f"""
    MATCH (n:Entity {{uuid: $uid, graph_id: $gid}})
    MATCH (n)-[r:RELATION]-(m:Entity {{graph_id: $gid}})
    WHERE r.fact IS NOT NULL AND trim(r.fact) <> ''{tw}
    RETURN DISTINCT r.fact AS fact
    LIMIT $lim
    """
    bullets: List[str] = []
    with driver.session() as session:
        result = session.run(q, uid=entity_uuid, gid=graph_id, lim=limit)
        for record in result:
            f = (record["fact"] or "").strip()
            if f and f not in bullets:
                bullets.append(f)
    return bullets


def refresh_entity_summary_llm(
    driver,
    graph_id: str,
    entity_uuid: str,
    entity_name: str,
    entity_type: str,
    llm_client: Any,
) -> Optional[str]:
    """
    TASK-038 — Rebuild `summary` on an Entity from connected facts via LLM.

    Returns new summary text or None if skipped/failed.
    """
    facts = fetch_entity_fact_bullets(driver, graph_id, entity_uuid)
    if len(facts) < 2:
        return None

    fact_block = "\n".join(f"- {f[:500]}" for f in facts)
    messages = [
        {
            "role": "system",
            "content": "You output only JSON.",
        },
        {
            "role": "user",
            "content": _SUMMARY_PROMPT.format(
                name=entity_name,
                etype=entity_type or "Entity",
                facts=fact_block,
            ),
        },
    ]
    try:
        out = llm_client.chat_json(messages, temperature=0.2, max_tokens=256)
        summary = (out.get("summary") or "").strip()
        if not summary:
            return None
    except Exception as e:
        logger.warning("Entity summary LLM failed for %s: %s", entity_uuid, e)
        return None

    def _set_summary(tx):
        tx.run(
            """
            MATCH (n:Entity {uuid: $uid, graph_id: $gid})
            SET n.summary = $summary
            """,
            uid=entity_uuid,
            gid=graph_id,
            summary=summary,
        )

    try:
        with driver.session() as session:
            session.execute_write(_set_summary)
    except Exception as e:
        logger.warning("Failed to write summary for %s: %s", entity_uuid, e)
        return None

    return summary
