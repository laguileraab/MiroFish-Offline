"""
NER/RE Extractor — entity and relation extraction via local LLM

Replaces Zep Cloud's built-in NER/RE pipeline.
Uses LLMClient.chat_json() with a structured prompt to extract
entities and relations from text chunks, guided by the graph's ontology.

Phase 10: optional two-pass extraction (entities then relations) and
graph-aware linking hints (candidate entity names injected before pass 2).
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set

from ..config import Config
from ..utils.llm_client import LLMClient

logger = logging.getLogger("mirofish.ner_extractor")


def _default_llm_for_extraction() -> LLMClient:
    """
    Phase 9 TASK-024 — optional LLM_EXTRACT_* / OLLAMA_EXTRACT_NUM_CTX for NER/RE only.
    """
    api_key = Config.LLM_EXTRACT_API_KEY or Config.LLM_API_KEY
    base_url = Config.LLM_EXTRACT_BASE_URL or Config.LLM_BASE_URL
    model = Config.LLM_EXTRACT_MODEL_NAME or Config.LLM_MODEL_NAME
    timeout = Config.LLM_EXTRACT_HTTP_TIMEOUT_SEC or Config.LLM_HTTP_TIMEOUT_SEC
    extract_ctx = os.environ.get("OLLAMA_EXTRACT_NUM_CTX", "").strip()
    num_ctx = int(extract_ctx) if extract_ctx else None
    return LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
        num_ctx=num_ctx,
    )


# Single-pass: entities + relations
_SYSTEM_PROMPT = """You are a Named Entity Recognition and Relation Extraction system.
Given a text and an ontology (entity types + relation types), extract all entities and relations.

ONTOLOGY:
{ontology_description}

RULES:
1. Only extract entity types and relation types defined in the ontology.
2. Normalize entity names: strip whitespace, use canonical form (e.g., "Jack Ma" not "ma jack").
3. Each entity must have: name, type (from ontology), and optional attributes.
4. Each relation must have: source entity name, target entity name, type (from ontology), and a fact sentence describing the relationship.
5. If no entities or relations are found, return empty lists.
6. Be precise — only extract what is explicitly stated or strongly implied in the text.

Return ONLY valid JSON in this exact format:
{{
  "entities": [
    {{"name": "...", "type": "...", "attributes": {{"key": "value"}}}}
  ],
  "relations": [
    {{"source": "...", "target": "...", "type": "...", "fact": "..."}}
  ]
}}"""

_USER_PROMPT = """Extract entities and relations from the following text:

{text}"""

# Phase 10 — Pass 1: entities only
_PASS1_SYSTEM = """You are a Named Entity Recognition system.
Given a text and an ontology of entity types, extract ALL entities (no relations).

ONTOLOGY:
{ontology_description}

RULES:
1. Only use entity types from the ontology (or "Entity" if ontology is open).
2. Normalize names: canonical form, trimmed whitespace.
3. Each entity: name, type, optional attributes object.
4. Return empty list if none.

Return ONLY valid JSON:
{{"entities": [{{"name": "...", "type": "...", "attributes": {{}}}}]}}"""

_PASS1_USER = """Extract entities from the following text:

{text}"""

# Phase 10 — Pass 2: relations only (endpoints must be from provided lists)
_PASS2_SYSTEM = """You are a Relation Extraction system.
Given the same text, ontology relation types, and a FROZEN list of entity names from pass 1 (plus optional graph candidates),
output ONLY relations. Every source and target MUST match one of the allowed names exactly (case-sensitive as given).

ONTOLOGY RELATION TYPES:
{relation_lines}

ALLOWED ENTITY NAMES (use only these as source/target string values):
{allowed_names_block}

RULES:
1. Relation type must be from ontology (or RELATED_TO only if ontology has no types).
2. Each relation: source, target, type, fact (short sentence).
3. If no valid relations, return an empty list.

Return ONLY valid JSON:
{{"relations": [{{"source": "...", "target": "...", "type": "...", "fact": "..."}}]}}"""

_PASS2_USER = """Text:

{text}"""


class NERExtractor:
    """Extract entities and relations from text using local LLM."""

    def __init__(self, llm_client: Optional[LLMClient] = None, max_retries: int = 2):
        self.llm = llm_client or _default_llm_for_extraction()
        self.max_retries = max_retries

    def extract(
        self,
        text: str,
        ontology: Dict[str, Any],
        *,
        graph_link_candidates: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Extract entities and relations from text, guided by ontology.

        Args:
            text: Input text chunk
            ontology: Dict with 'entity_types' and 'relation_types' from graph
            graph_link_candidates: Optional rows with 'name' and optional 'summary' for graph-aware linking (Phase 10).

        Returns:
            Dict with entities, relations, success, error, usage (last LLM call), ner_two_pass flag.
        """
        if not text or not text.strip():
            return {
                "entities": [],
                "relations": [],
                "success": True,
                "error": None,
                "usage": None,
                "finish_reason": None,
                "ner_two_pass": bool(Config.NER_TWO_PASS),
            }

        if Config.NER_TWO_PASS:
            return self._extract_two_pass(text.strip(), ontology, graph_link_candidates)

        return self._extract_single_pass(text.strip(), ontology, graph_link_candidates)

    def _extract_single_pass(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_link_candidates: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        ontology_desc = self._format_ontology(ontology)
        system_msg = _SYSTEM_PROMPT.format(ontology_description=ontology_desc)
        user_msg = _USER_PROMPT.format(text=text)
        if graph_link_candidates:
            g_lines = []
            for row in graph_link_candidates:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                summ = (row.get("summary") or "")[:120]
                g_lines.append(f"- {name}" + (f" — {summ}" if summ else ""))
            if g_lines:
                user_msg += (
                    "\n\nExisting graph entities that may appear in the text "
                    "(prefer these exact names when extracting):\n"
                    + "\n".join(g_lines)
                )

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                result = self.llm.chat_json(
                    messages=messages,
                    temperature=0.1,
                    max_tokens=Config.NER_MAX_OUTPUT_TOKENS,
                )
                cleaned = self._validate_and_clean(result, ontology)
                cleaned["success"] = True
                cleaned["error"] = None
                cleaned["usage"] = getattr(self.llm, "last_usage", None)
                cleaned["finish_reason"] = getattr(self.llm, "last_finish_reason", None)
                cleaned["ner_two_pass"] = False
                return cleaned

            except ValueError as e:
                last_error = e
                logger.warning(
                    "NER extraction failed (attempt %s): invalid JSON — %s", attempt + 1, e
                )
            except Exception as e:
                last_error = e
                logger.error("NER extraction error: %s", e)
                if attempt >= self.max_retries:
                    break

        logger.error(
            "NER extraction failed after %s attempts: %s", self.max_retries + 1, last_error
        )
        err_msg = str(last_error) if last_error else "NER extraction failed"
        return {
            "entities": [],
            "relations": [],
            "success": False,
            "error": err_msg,
            "usage": getattr(self.llm, "last_usage", None),
            "finish_reason": getattr(self.llm, "last_finish_reason", None),
            "ner_two_pass": False,
        }

    def _extract_two_pass(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_link_candidates: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """Phase 10 TASK-028/029 — entities first, then relations with frozen entity list."""
        entity_ont = self._format_entity_types_only(ontology)
        pass1_messages = [
            {"role": "system", "content": _PASS1_SYSTEM.format(ontology_description=entity_ont)},
            {"role": "user", "content": _PASS1_USER.format(text=text)},
        ]

        entities: List[Dict[str, Any]] = []
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.llm.chat_json(
                    messages=pass1_messages,
                    temperature=0.1,
                    max_tokens=Config.NER_MAX_OUTPUT_TOKENS,
                )
                ent_only = {"entities": raw.get("entities", []), "relations": []}
                cleaned_e = self._validate_and_clean(ent_only, ontology)
                entities = cleaned_e["entities"]
                break
            except (ValueError, Exception) as e:
                last_error = e
                logger.warning("NER pass-1 failed (attempt %s): %s", attempt + 1, e)
                if attempt >= self.max_retries:
                    return {
                        "entities": [],
                        "relations": [],
                        "success": False,
                        "error": f"pass1: {last_error}",
                        "usage": getattr(self.llm, "last_usage", None),
                        "finish_reason": getattr(self.llm, "last_finish_reason", None),
                        "ner_two_pass": True,
                    }

        allowed_lines: List[str] = []
        allowed_lower: Set[str] = set()
        for e in entities:
            line = f"- {e['name']} ({e['type']})"
            allowed_lines.append(line)
            allowed_lower.add(e["name"].lower())

        graph_block = ""
        if graph_link_candidates:
            g_lines = []
            for row in graph_link_candidates:
                name = (row.get("name") or "").strip()
                if not name or name.lower() in allowed_lower:
                    continue
                summ = (row.get("summary") or "")[:120]
                g_lines.append(f"- {name}" + (f" — {summ}" if summ else ""))
                allowed_lower.add(name.lower())
            if g_lines:
                graph_block = "\n\nGRAPH CANDIDATES (existing graph entities; use exact names if they appear in the text):\n" + "\n".join(
                    g_lines
                )

        relation_lines = self._format_relation_types_only(ontology)
        allowed_names_block = "\n".join(allowed_lines) + graph_block

        pass2_messages = [
            {
                "role": "system",
                "content": _PASS2_SYSTEM.format(
                    relation_lines=relation_lines,
                    allowed_names_block=allowed_names_block,
                ),
            },
            {"role": "user", "content": _PASS2_USER.format(text=text)},
        ]

        for attempt in range(self.max_retries + 1):
            try:
                raw2 = self.llm.chat_json(
                    messages=pass2_messages,
                    temperature=0.1,
                    max_tokens=Config.NER_MAX_OUTPUT_TOKENS,
                )
                combined = {"entities": entities, "relations": raw2.get("relations", [])}
                cleaned = self._validate_and_clean(combined, ontology)
                cleaned["success"] = True
                cleaned["error"] = None
                cleaned["usage"] = getattr(self.llm, "last_usage", None)
                cleaned["finish_reason"] = getattr(self.llm, "last_finish_reason", None)
                cleaned["ner_two_pass"] = True
                return cleaned
            except (ValueError, Exception) as e:
                last_error = e
                logger.warning("NER pass-2 failed (attempt %s): %s", attempt + 1, e)
                if attempt >= self.max_retries:
                    return {
                        "entities": entities,
                        "relations": [],
                        "success": False,
                        "error": f"pass2: {last_error}",
                        "usage": getattr(self.llm, "last_usage", None),
                        "finish_reason": getattr(self.llm, "last_finish_reason", None),
                        "ner_two_pass": True,
                    }

        raise RuntimeError("NER pass-2 exhausted retries without return")

    def _format_entity_types_only(self, ontology: Dict[str, Any]) -> str:
        """Entity types section only (smaller prompt for pass 1)."""
        parts: List[str] = []
        entity_types = ontology.get("entity_types", [])
        if entity_types:
            parts.append("Entity Types:")
            for et in entity_types:
                if isinstance(et, dict):
                    name = et.get("name", str(et))
                    desc = et.get("description", "")
                    attrs = et.get("attributes", [])
                    line = f"  - {name}"
                    if desc:
                        line += f": {desc}"
                    if attrs:
                        attr_names = [
                            a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in attrs
                        ]
                        line += f" (attributes: {', '.join(attr_names)})"
                    parts.append(line)
                else:
                    parts.append(f"  - {et}")
        if not parts:
            parts.append("No entity ontology; use type Entity for all.")
        return "\n".join(parts)

    def _format_relation_types_only(self, ontology: Dict[str, Any]) -> str:
        lines: List[str] = []
        relation_types = ontology.get("relation_types", ontology.get("edge_types", []))
        if not relation_types:
            return "  - RELATED_TO (generic; use only if nothing else fits)"
        for rt in relation_types:
            if isinstance(rt, dict):
                name = rt.get("name", str(rt))
                desc = rt.get("description", "")
                lines.append(f"  - {name}" + (f": {desc}" if desc else ""))
            else:
                lines.append(f"  - {rt}")
        return "\n".join(lines)

    def _format_ontology(self, ontology: Dict[str, Any]) -> str:
        """Format ontology dict into readable text for the LLM prompt."""
        parts: List[str] = []

        entity_types = ontology.get("entity_types", [])
        if entity_types:
            parts.append("Entity Types:")
            for et in entity_types:
                if isinstance(et, dict):
                    name = et.get("name", str(et))
                    desc = et.get("description", "")
                    attrs = et.get("attributes", [])
                    line = f"  - {name}"
                    if desc:
                        line += f": {desc}"
                    if attrs:
                        attr_names = [
                            a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in attrs
                        ]
                        line += f" (attributes: {', '.join(attr_names)})"
                    parts.append(line)
                else:
                    parts.append(f"  - {et}")

        relation_types = ontology.get("relation_types", ontology.get("edge_types", []))
        if relation_types:
            parts.append("\nRelation Types:")
            for rt in relation_types:
                if isinstance(rt, dict):
                    name = rt.get("name", str(rt))
                    desc = rt.get("description", "")
                    source_targets = rt.get("source_targets", [])
                    line = f"  - {name}"
                    if desc:
                        line += f": {desc}"
                    if source_targets:
                        st_strs = [
                            f"{st.get('source', '?')} → {st.get('target', '?')}"
                            for st in source_targets
                        ]
                        line += f" ({', '.join(st_strs)})"
                    parts.append(line)
                else:
                    parts.append(f"  - {rt}")

        if not parts:
            parts.append("No specific ontology defined. Extract all entities and relations you find.")

        return "\n".join(parts)

    def _validate_and_clean(
        self, result: Dict[str, Any], ontology: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate and normalize LLM output."""
        entities = result.get("entities", [])
        relations = result.get("relations", [])

        valid_entity_types = set()
        for et in ontology.get("entity_types", []):
            if isinstance(et, dict):
                valid_entity_types.add(et.get("name", "").strip())
            else:
                valid_entity_types.add(str(et).strip())

        valid_relation_types = set()
        for rt in ontology.get("relation_types", ontology.get("edge_types", [])):
            if isinstance(rt, dict):
                valid_relation_types.add(rt.get("name", "").strip())
            else:
                valid_relation_types.add(str(rt).strip())

        cleaned_entities: List[Dict[str, Any]] = []
        seen_names: Set[str] = set()
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name", "")).strip()
            etype = str(entity.get("type", "Entity")).strip()
            if not name:
                continue

            name_lower = name.lower()
            if name_lower in seen_names:
                continue
            seen_names.add(name_lower)

            if valid_entity_types and etype not in valid_entity_types:
                logger.debug("Entity '%s' has type '%s' not in ontology, keeping anyway", name, etype)

            cleaned_entities.append(
                {
                    "name": name,
                    "type": etype,
                    "attributes": entity.get("attributes", {}),
                }
            )

        cleaned_relations: List[Dict[str, Any]] = []
        entity_names_lower = {e["name"].lower() for e in cleaned_entities}
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            source = str(relation.get("source", "")).strip()
            target = str(relation.get("target", "")).strip()
            rtype = str(relation.get("type", "RELATED_TO")).strip()
            fact = str(relation.get("fact", "")).strip()

            if not source or not target:
                continue

            if source.lower() not in entity_names_lower:
                cleaned_entities.append(
                    {"name": source, "type": "Entity", "attributes": {}}
                )
                entity_names_lower.add(source.lower())

            if target.lower() not in entity_names_lower:
                cleaned_entities.append(
                    {"name": target, "type": "Entity", "attributes": {}}
                )
                entity_names_lower.add(target.lower())

            rel_out: Dict[str, Any] = {
                "source": source,
                "target": target,
                "type": rtype,
                "fact": fact or f"{source} {rtype} {target}",
            }
            # Phase 12 — optional hints if the model emits them (ingest uses when GRAPH_TEMPORAL_ENABLED)
            vf = relation.get("valid_from")
            if isinstance(vf, str) and vf.strip():
                rel_out["valid_from"] = vf.strip()
            sup = relation.get("supersedes_relation_uuid")
            if isinstance(sup, str) and sup.strip():
                rel_out["supersedes_relation_uuid"] = sup.strip()

            cleaned_relations.append(rel_out)

        return {
            "entities": cleaned_entities,
            "relations": cleaned_relations,
        }
