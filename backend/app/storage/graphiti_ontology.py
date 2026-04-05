"""
Map MiroFish JSON ontology (``entity_types`` / ``relation_types``) to Graphiti prescribed models.

Graphiti expects ``entity_types: dict[str, type[pydantic.BaseModel]]`` on ``add_episode``.
Subclass models must **not** declare fields that collide with ``EntityNode`` (see
``graphiti_core.utils.ontology_utils.entity_types_utils.validate_entity_types``).

We generate **empty** Pydantic models per entity type and put guidance in ``model.__doc__``
so extraction prompts can use prescribed types without custom attribute fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, create_model

logger = logging.getLogger("mirofish.graphiti_ontology")

# Graphiti forbids subclass fields whose names match EntityNode’s reserved fields.
try:
    from graphiti_core.nodes import EntityNode

    _ENTITY_NODE_FIELD_NAMES = frozenset(EntityNode.model_fields.keys())
except ImportError:  # pragma: no cover
    _ENTITY_NODE_FIELD_NAMES = frozenset()


_SAFE_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def mirofish_ontology_to_entity_types(
    ontology: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Type[BaseModel]]]:
    """
    Build Graphiti ``entity_types`` from MiroFish ontology JSON.

    Returns ``None`` when there is nothing to prescribe (learned / default Entity behavior).
    """
    if not ontology:
        return None
    raw = ontology.get("entity_types")
    if not raw or not isinstance(raw, list):
        return None

    out: Dict[str, Type[BaseModel]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if not _SAFE_TYPE_NAME.match(name):
            logger.warning("Skipping entity type with invalid identifier: %r", name)
            continue
        if name in out:
            continue
        desc = (item.get("description") or "").strip()
        doc = desc if desc else f"Entity type: {name}"

        model = create_model(  # type: ignore[call-overload]
            name,
            __base__=BaseModel,
            __doc__=doc,
        )
        for fname in model.model_fields.keys():
            if fname in _ENTITY_NODE_FIELD_NAMES:
                raise ValueError(
                    f"Generated entity type {name!r} unexpectedly reserves field {fname!r}"
                )
        out[name] = model

    return out if out else None
