"""
Shared RELATION temporal predicates for Cypher (prefix ``r``).

Used by Neo4jStorage reads and SearchService neighborhood expansion.
"""

from typing import Any, Dict, Optional, Tuple

from ..config import Config


def relation_temporal_where(
    as_of: Optional[str], include_invalid: bool
) -> Tuple[str, Dict[str, Any]]:
    """Returns (WHERE fragment starting with AND, or empty, extra params)."""
    if not Config.GRAPH_TEMPORAL_ENABLED or include_invalid:
        return "", {}
    if not Config.GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY:
        return "", {}
    if as_of:
        return (
            " AND (r.valid_at IS NULL OR r.valid_at <= $as_of) "
            "AND (r.invalid_at IS NULL OR r.invalid_at > $as_of)",
            {"as_of": as_of},
        )
    return " AND r.invalid_at IS NULL", {}
