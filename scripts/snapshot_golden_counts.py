#!/usr/bin/env python3
"""
Print Entity, RELATION, and Episode counts for a graph_id (Phase 8 TASK-022).

Usage (from repository root):
    python scripts/snapshot_golden_counts.py golden-regression-v1
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load Flask app config (.env via backend/app/config.py)
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from neo4j import GraphDatabase  # noqa: E402

from app.config import Config  # noqa: E402


def main() -> None:
    gid = sys.argv[1] if len(sys.argv) > 1 else "golden-regression-v1"
    driver = GraphDatabase.driver(
        Config.NEO4J_URI,
        auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
    )
    try:
        with driver.session() as session:
            ent = session.run(
                "MATCH (e:Entity {graph_id: $gid}) RETURN count(e) AS c", gid=gid
            ).single()
            rel = session.run(
                "MATCH ()-[r:RELATION {graph_id: $gid}]->() RETURN count(r) AS c",
                gid=gid,
            ).single()
            ep = session.run(
                "MATCH (e:Episode {graph_id: $gid}) RETURN count(e) AS c", gid=gid
            ).single()
        n_ent = int(ent["c"]) if ent else 0
        n_rel = int(rel["c"]) if rel else 0
        n_ep = int(ep["c"]) if ep else 0
        print(f"graph_id={gid} entities={n_ent} relations={n_rel} episodes={n_ep}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
