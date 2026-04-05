"""
Graph ingest KPIs and structured logging (Phase 8 — TASK-020, TASK-021).

Field names below are the contract for log parsing and golden-set regression.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("mirofish.ingest_metrics")

# --- TASK-020: documented KPI keys (single-chunk ingest) ---
INGEST_METRIC_KEYS = (
    "event",  # always "ingest_chunk"
    "graph_id",
    "episode_id",
    "chunk_chars",
    "entity_count",
    "relation_count",
    "entities_per_chunk",
    "relations_per_chunk",
    "edge_to_node_ratio",
    "ner_success",
    "ner_error",
    "duration_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "finish_reason",
    "ner_max_output_tokens",
    "ollama_num_ctx",
    "suspected_output_truncation",
    "relations_skipped_missing_endpoint",
    "ner_two_pass",
)


def edge_to_node_ratio(entity_count: int, relation_count: int) -> float:
    """Relations per entity for this chunk (0.0 if no entities)."""
    if entity_count <= 0:
        return 0.0
    return relation_count / entity_count


def compute_suspected_truncation(
    finish_reason: Optional[str],
    completion_tokens: Optional[int],
    max_output_tokens: int,
) -> bool:
    """Heuristic: model hit output budget or reported length stop."""
    if (finish_reason or "").lower() == "length":
        return True
    if completion_tokens is not None and max_output_tokens > 0:
        if completion_tokens >= max(1, int(max_output_tokens * 0.92)):
            return True
    return False


def build_ingest_metrics(
    *,
    graph_id: str,
    episode_id: str,
    chunk_chars: int,
    entity_count: int,
    relation_count: int,
    ner_success: bool,
    ner_error: Optional[str],
    duration_ms: float,
    ner_max_output_tokens: int,
    ollama_num_ctx: Optional[int],
    usage: Optional[Dict[str, Any]],
    finish_reason: Optional[str],
    relations_skipped_missing_endpoint: int = 0,
    ner_two_pass: bool = False,
) -> Dict[str, Any]:
    """Assemble one JSON-serializable ingest record."""
    pt = None
    ct = None
    tt = None
    if usage:
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        tt = usage.get("total_tokens")

    trunc = compute_suspected_truncation(finish_reason, ct, ner_max_output_tokens)

    return {
        "event": "ingest_chunk",
        "graph_id": graph_id,
        "episode_id": episode_id,
        "chunk_chars": chunk_chars,
        "entity_count": entity_count,
        "relation_count": relation_count,
        "entities_per_chunk": entity_count,
        "relations_per_chunk": relation_count,
        "edge_to_node_ratio": round(edge_to_node_ratio(entity_count, relation_count), 4),
        "ner_success": ner_success,
        "ner_error": ner_error,
        "duration_ms": round(duration_ms, 2),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "finish_reason": finish_reason,
        "ner_max_output_tokens": ner_max_output_tokens,
        "ollama_num_ctx": ollama_num_ctx,
        "suspected_output_truncation": trunc,
        "relations_skipped_missing_endpoint": relations_skipped_missing_endpoint,
        "ner_two_pass": ner_two_pass,
    }


def log_ingest_metrics(metrics: Dict[str, Any], log: Optional[logging.Logger] = None) -> None:
    """Emit one line: JSON after prefix for grep (`ingest_metrics`)."""
    lg = log or logger
    lg.info("ingest_metrics %s", json.dumps(metrics, ensure_ascii=False))


def warn_ingest_guardrails(
    metrics: Dict[str, Any], log: Optional[logging.Logger] = None
) -> None:
    """TASK-021: warnings near max_tokens / truncation / sparse extraction."""
    lg = log or logger
    if metrics.get("suspected_output_truncation"):
        lg.warning(
            "ingest_guardrail suspected_output_truncation graph_id=%s episode=%s "
            "finish_reason=%s completion_tokens=%s ner_max_output_tokens=%s",
            metrics.get("graph_id"),
            metrics.get("episode_id"),
            metrics.get("finish_reason"),
            metrics.get("completion_tokens"),
            metrics.get("ner_max_output_tokens"),
        )
    chunk_chars = int(metrics.get("chunk_chars") or 0)
    ec = int(metrics.get("entity_count") or 0)
    rc = int(metrics.get("relation_count") or 0)
    if chunk_chars >= 1200 and ec >= 4 and rc == 0:
        lg.warning(
            "ingest_guardrail sparse_relations graph_id=%s episode=%s chunk_chars=%s entity_count=%s",
            metrics.get("graph_id"),
            metrics.get("episode_id"),
            chunk_chars,
            ec,
        )
    if not metrics.get("ner_success"):
        lg.warning(
            "ingest_guardrail ner_failed graph_id=%s episode=%s error=%s",
            metrics.get("graph_id"),
            metrics.get("episode_id"),
            metrics.get("ner_error"),
        )
