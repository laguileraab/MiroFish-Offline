"""
Deterministic entity / fact normalization (Phase 11 TASK-034).

Optional alias table: JSON file path from Config.ENTITY_ALIAS_JSON_PATH
mapping surface form -> canonical display name for merge keys.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from functools import lru_cache
from typing import Any, Dict, List

logger = logging.getLogger("mirofish.entity_normalize")


def normalize_entity_name(name: str) -> str:
    """NFKC, strip, collapse internal whitespace."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_fact_key(fact: str) -> str:
    """Stable key for relation dedupe (TASK-037)."""
    if not fact:
        return ""
    s = unicodedata.normalize("NFKC", str(fact)).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s[:2048]


@lru_cache(maxsize=1)
def _load_alias_map_from_config() -> Dict[str, str]:
    from ..config import Config

    path = getattr(Config, "ENTITY_ALIAS_JSON_PATH", None) or ""
    path = path.strip()
    if not path:
        return {}
    if not os.path.isfile(path):
        logger.warning("ENTITY_ALIAS_JSON_PATH not found: %s", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Failed to load entity aliases: %s", e)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in data.items():
        if k is None or v is None:
            continue
        ks = str(k).strip()
        vs = str(v).strip()
        if ks and vs:
            out[ks] = vs
            out[ks.lower()] = vs
    return out


def apply_entity_aliases(name: str) -> str:
    """Replace with canonical name if alias table defines it."""
    n = normalize_entity_name(name)
    if not n:
        return n
    aliases = _load_alias_map_from_config()
    if not aliases:
        return n
    if n in aliases:
        return normalize_entity_name(aliases[n])
    low = n.lower()
    if low in aliases:
        return normalize_entity_name(aliases[low])
    return n


def normalize_entities_in_place(entities: List[Dict[str, Any]]) -> None:
    """Mutate entity dicts: normalize + alias names."""
    for e in entities:
        if not isinstance(e, dict):
            continue
        raw = e.get("name", "")
        e["name"] = apply_entity_aliases(raw)


def normalize_relation_endpoints(relations: List[Dict[str, Any]]) -> None:
    """Normalize source/target strings on relation dicts."""
    for r in relations:
        if not isinstance(r, dict):
            continue
        if r.get("source"):
            r["source"] = apply_entity_aliases(r["source"])
        if r.get("target"):
            r["target"] = apply_entity_aliases(r["target"])
        if r.get("fact"):
            r["fact"] = normalize_entity_name(r["fact"])  # light cleanup only
