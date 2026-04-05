"""
Phase 14 TASK-048 — Neo4j-backed ingest overview for admin/dashboard.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from neo4j import Driver

from .ingest_counters import get_ingest_counters


def neo4j_ingest_overview(driver: Driver, graph_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Episode counts and per-graph rollup (optional graph_id filter).
    """
    out: Dict[str, Any] = {"episodes_total": 0, "by_graph": []}
    with driver.session() as session:
        if graph_id:
            rec = session.run(
                "MATCH (e:Episode) WHERE e.graph_id = $gid RETURN count(e) AS episodes",
                gid=graph_id,
            ).single()
            out["episodes_total"] = int(rec["episodes"]) if rec else 0
        else:
            rec = session.run("MATCH (e:Episode) RETURN count(e) AS episodes").single()
            out["episodes_total"] = int(rec["episodes"]) if rec else 0
            rows = session.run(
                """
                MATCH (e:Episode)
                RETURN e.graph_id AS graph_id, count(e) AS episodes
                ORDER BY episodes DESC
                LIMIT 50
                """
            )
            out["by_graph"] = [
                {"graph_id": r["graph_id"], "episodes": int(r["episodes"])} for r in rows
            ]
    return out


def build_admin_ingest_payload(driver: Optional[Driver], graph_id: Optional[str] = None) -> Dict[str, Any]:
    counters = get_ingest_counters().snapshot()
    neo: Dict[str, Any] = {}
    if driver is not None:
        try:
            neo = neo4j_ingest_overview(driver, graph_id=graph_id)
        except Exception as e:  # noqa: BLE001
            neo = {"error": str(e)}
    return {"process_counters": counters, "neo4j": neo}
