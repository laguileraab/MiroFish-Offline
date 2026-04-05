"""
SearchService — hybrid search (vector + keyword) over Neo4j graph data.

Phase 13: env weights (SEARCH_VECTOR_WEIGHT / SEARCH_KEYWORD_WEIGHT), optional
BFS neighborhood expansion, token-overlap rerank on top-M, TTL embedding cache
(see EmbeddingService + Config).
"""

import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from neo4j import Session as Neo4jSession

from ..config import Config
from .embedding_service import EmbeddingService
from .relation_temporal_filters import relation_temporal_where

logger = logging.getLogger('mirofish.search')


def _edge_passes_temporal(
    edge: Dict[str, Any],
    as_of: Optional[str],
    include_invalid_relations: bool,
) -> bool:
    if not Config.GRAPH_TEMPORAL_ENABLED or include_invalid_relations:
        return True
    if not Config.GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY:
        return True
    inv = edge.get("invalid_at")
    val = edge.get("valid_at")
    if as_of:
        if val and val > as_of:
            return False
        if inv and inv <= as_of:
            return False
        return True
    return not inv


def _tokens(text: str) -> Set[str]:
    return {t for t in re.findall(r"[\w']+", (text or "").lower()) if len(t) > 1}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rel_to_edge_hit(rel: Any, su: str, tu: str, score: float) -> Dict[str, Any]:
    props = dict(rel)
    attrs_json = props.pop("attributes_json", "{}")
    try:
        attributes = json.loads(attrs_json) if attrs_json else {}
    except (json.JSONDecodeError, TypeError):
        attributes = {}
    props.pop("fact_embedding", None)
    episode_ids = props.get("episode_ids", [])
    if episode_ids and not isinstance(episode_ids, list):
        episode_ids = [str(episode_ids)]
    return {
        "uuid": props.get("uuid", ""),
        "name": props.get("name", ""),
        "fact": props.get("fact", ""),
        "source_node_uuid": su,
        "target_node_uuid": tu,
        "attributes": attributes,
        "created_at": props.get("created_at"),
        "valid_at": props.get("valid_at"),
        "invalid_at": props.get("invalid_at"),
        "expired_at": props.get("expired_at"),
        "episode_ids": episode_ids,
        "score": score,
    }


# Cypher for vector search on edges (facts)
_VECTOR_SEARCH_EDGES = """
CALL db.index.vector.queryRelationships('fact_embedding', $limit, $query_vector)
YIELD relationship, score
WHERE relationship.graph_id = $graph_id
RETURN relationship AS r, score
ORDER BY score DESC
LIMIT $limit
"""

# Cypher for vector search on nodes (entities)
_VECTOR_SEARCH_NODES = """
CALL db.index.vector.queryNodes('entity_embedding', $limit, $query_vector)
YIELD node, score
WHERE node.graph_id = $graph_id
RETURN node AS n, score
ORDER BY score DESC
LIMIT $limit
"""

# Cypher for fulltext (BM25) search on edges
_FULLTEXT_SEARCH_EDGES = """
CALL db.index.fulltext.queryRelationships('fact_fulltext', $query_text)
YIELD relationship, score
WHERE relationship.graph_id = $graph_id
RETURN relationship AS r, score
ORDER BY score DESC
LIMIT $limit
"""

# Cypher for fulltext search on nodes
_FULLTEXT_SEARCH_NODES = """
CALL db.index.fulltext.queryNodes('entity_fulltext', $query_text)
YIELD node, score
WHERE node.graph_id = $graph_id
RETURN node AS n, score
ORDER BY score DESC
LIMIT $limit
"""


class SearchService:
    """Hybrid search combining vector similarity and keyword matching."""

    def __init__(self, embedding_service: EmbeddingService):
        self.embedding = embedding_service

    def search_edges(
        self,
        session: Neo4jSession,
        graph_id: str,
        query: str,
        limit: int = 10,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
        extra_seed_node_uuids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search edges (facts/relations) using hybrid scoring.

        Returns list of dicts with edge properties + 'score'.
        """
        query_vector = self.embedding.embed(query)

        mult = 4 if (
            Config.GRAPH_TEMPORAL_ENABLED
            and Config.GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY
            and not include_invalid_relations
        ) else 2
        inner = limit * mult

        # Vector search
        vector_results = self._run_edge_vector_search(
            session, graph_id, query_vector, inner, as_of, include_invalid_relations
        )

        # Keyword search
        keyword_results = self._run_edge_keyword_search(
            session, graph_id, query, inner, as_of, include_invalid_relations
        )

        # Merge and rank
        merged = self._merge_results(
            vector_results, keyword_results, key="uuid", limit=limit
        )

        seen_e = {x["uuid"] for x in merged if x.get("uuid")}
        seeds: List[str] = []
        for x in merged:
            su, tu = x.get("source_node_uuid"), x.get("target_node_uuid")
            if su:
                seeds.append(su)
            if tu:
                seeds.append(tu)
        if extra_seed_node_uuids:
            seeds.extend(extra_seed_node_uuids)

        expanded = self._expand_neighbor_edges(
            session, graph_id, seeds, seen_e, as_of, include_invalid_relations, merged
        )
        combined = merged + expanded
        combined = self._apply_rerank(combined, query)

        out_cap = limit
        if Config.GRAPH_SEARCH_EXPAND_HOPS > 0 and Config.GRAPH_SEARCH_EXPAND_EXTRA > 0:
            out_cap = limit + Config.GRAPH_SEARCH_EXPAND_EXTRA

        combined.sort(key=lambda z: z.get("score", 0), reverse=True)
        return combined[:out_cap]

    def search_nodes(
        self,
        session: Neo4jSession,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Search nodes (entities) using hybrid scoring.

        Returns list of dicts with node properties + 'score'.
        """
        query_vector = self.embedding.embed(query)

        vector_results = self._run_node_vector_search(
            session, graph_id, query_vector, limit * 2
        )

        keyword_results = self._run_node_keyword_search(
            session, graph_id, query, limit * 2
        )

        merged = self._merge_results(
            vector_results, keyword_results, key="uuid", limit=limit
        )
        return self._apply_rerank(merged, query)

    def _expand_neighbor_edges(
        self,
        session: Neo4jSession,
        graph_id: str,
        seed_node_uuids: List[str],
        seen_edge_uuids: Set[str],
        as_of: Optional[str],
        include_invalid_relations: bool,
        hybrid_hits: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        hops = Config.GRAPH_SEARCH_EXPAND_HOPS
        max_extra = Config.GRAPH_SEARCH_EXPAND_EXTRA
        if hops <= 0 or max_extra <= 0:
            return []

        tw, tp = relation_temporal_where(as_of, include_invalid_relations)
        no_lbl = not Config.GRAPH_SEARCH_EXPAND_ENTITY_TYPES
        allowed = Config.GRAPH_SEARCH_EXPAND_ENTITY_TYPES or []
        max_per = Config.GRAPH_SEARCH_EXPAND_MAX_PER_SEED
        base = 0.25
        if hybrid_hits:
            base = max(0.08, float(hybrid_hits[0].get("score", 0.25)) * 0.45)

        out: List[Dict[str, Any]] = []
        seen_e = set(seen_edge_uuids)
        frontier = list(dict.fromkeys(u for u in seed_node_uuids if u))

        for hop in range(hops):
            if len(out) >= max_extra or not frontier:
                break
            hop_mult = 0.88**hop
            q = f"""
            UNWIND $frontier AS seed_uid
            MATCH (n:Entity {{uuid: seed_uid, graph_id: $gid}})-[r:RELATION]-(b:Entity {{graph_id: $gid}})
            WHERE 1=1 {tw}
              AND ($no_lbl OR any(lbl IN labels(b) WHERE lbl IN $allowed))
            RETURN seed_uid AS seed_uid, r, startNode(r).uuid AS su, endNode(r).uuid AS tu
            """
            try:
                rows = list(
                    session.run(
                        q,
                        frontier=frontier,
                        gid=graph_id,
                        no_lbl=no_lbl,
                        allowed=allowed,
                        **tp,
                    )
                )
            except Exception as e:
                logger.warning("Neighbor expansion query failed: %s", e)
                break

            per_seed: Dict[str, int] = defaultdict(int)
            next_frontier: Set[str] = set()
            rows.sort(key=lambda rec: (rec["seed_uid"], str(rec["r"].get("uuid", ""))))

            for row in rows:
                if len(out) >= max_extra:
                    break
                suid = row["seed_uid"]
                if per_seed[suid] >= max_per:
                    continue
                rel = row["r"]
                rd = dict(rel)
                euuid = str(rd.get("uuid") or "")
                if not euuid or euuid in seen_e:
                    continue
                per_seed[suid] += 1
                seen_e.add(euuid)
                su, tu = row["su"], row["tu"]
                sc = max(0.02, base * 0.55 * hop_mult)
                out.append(_rel_to_edge_hit(rel, su, tu, sc))
                other = tu if su == suid else su
                if other and other != suid:
                    next_frontier.add(other)

            frontier = list(next_frontier)

        return out[:max_extra]

    def _apply_rerank(self, items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        m = Config.GRAPH_SEARCH_RERANK_TOP_M
        if m <= 0 or not items:
            return items
        boost = Config.GRAPH_SEARCH_RERANK_BOOST
        qtok = _tokens(query)
        ordered = sorted(items, key=lambda x: x.get("score", 0), reverse=True)
        rescored: List[Dict[str, Any]] = []
        for i, it in enumerate(ordered):
            s0 = float(it.get("score", 0))
            if i < m:
                blob = f"{it.get('fact', '')} {it.get('name', '')} {it.get('summary', '')}"
                j = _jaccard(qtok, _tokens(blob))
                it = {**it, "score": s0 * (1 + boost * j)}
            rescored.append(it)
        rescored.sort(key=lambda x: x.get("score", 0), reverse=True)
        return rescored

    def search_nodes_by_vector(
        self,
        session: Neo4jSession,
        graph_id: str,
        query_vector: List[float],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Vector-only entity search (Phase 11) — cosine scores from Neo4j index.
        """
        return self._run_node_vector_search(session, graph_id, query_vector, limit)

    def _run_edge_vector_search(
        self,
        session: Neo4jSession,
        graph_id: str,
        query_vector: List[float],
        limit: int,
        as_of: Optional[str],
        include_invalid_relations: bool,
    ) -> List[Dict[str, Any]]:
        """Run vector similarity search on edge fact_embedding."""
        try:
            result = session.run(
                _VECTOR_SEARCH_EDGES,
                graph_id=graph_id,
                query_vector=query_vector,
                limit=limit,
            )
            out = [
                {**dict(record["r"]), "uuid": record["r"]["uuid"], "_score": record["score"]}
                for record in result
            ]
            return [
                row
                for row in out
                if _edge_passes_temporal(row, as_of, include_invalid_relations)
            ]
        except Exception as e:
            logger.warning(f"Vector edge search failed (index may not exist yet): {e}")
            return []

    def _run_edge_keyword_search(
        self,
        session: Neo4jSession,
        graph_id: str,
        query: str,
        limit: int,
        as_of: Optional[str],
        include_invalid_relations: bool,
    ) -> List[Dict[str, Any]]:
        """Run fulltext (BM25) search on edge fact + name."""
        try:
            # Escape special Lucene characters in query
            safe_query = self._escape_lucene(query)
            result = session.run(
                _FULLTEXT_SEARCH_EDGES,
                graph_id=graph_id,
                query_text=safe_query,
                limit=limit,
            )
            out = [
                {**dict(record["r"]), "uuid": record["r"]["uuid"], "_score": record["score"]}
                for record in result
            ]
            return [
                row
                for row in out
                if _edge_passes_temporal(row, as_of, include_invalid_relations)
            ]
        except Exception as e:
            logger.warning(f"Keyword edge search failed: {e}")
            return []

    def _run_node_vector_search(
        self, session: Neo4jSession, graph_id: str, query_vector: List[float], limit: int
    ) -> List[Dict[str, Any]]:
        """Run vector similarity search on entity embedding."""
        try:
            result = session.run(
                _VECTOR_SEARCH_NODES,
                graph_id=graph_id,
                query_vector=query_vector,
                limit=limit,
            )
            return [
                {**dict(record["n"]), "uuid": record["n"]["uuid"], "_score": record["score"]}
                for record in result
            ]
        except Exception as e:
            logger.warning(f"Vector node search failed: {e}")
            return []

    def _run_node_keyword_search(
        self, session: Neo4jSession, graph_id: str, query: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Run fulltext search on entity name + summary."""
        try:
            safe_query = self._escape_lucene(query)
            result = session.run(
                _FULLTEXT_SEARCH_NODES,
                graph_id=graph_id,
                query_text=safe_query,
                limit=limit,
            )
            return [
                {**dict(record["n"]), "uuid": record["n"]["uuid"], "_score": record["score"]}
                for record in result
            ]
        except Exception as e:
            logger.warning(f"Keyword node search failed: {e}")
            return []

    def _merge_results(
        self,
        vector_results: List[Dict[str, Any]],
        keyword_results: List[Dict[str, Any]],
        key: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """
        Merge vector and keyword results with weighted scoring.

        Normalizes scores to [0, 1] range before combining.
        """
        # Normalize vector scores
        v_max = max((r["_score"] for r in vector_results), default=1.0) or 1.0
        v_scores = {r[key]: r["_score"] / v_max for r in vector_results}

        # Normalize keyword scores
        k_max = max((r["_score"] for r in keyword_results), default=1.0) or 1.0
        k_scores = {r[key]: r["_score"] / k_max for r in keyword_results}

        # Build combined result map
        all_items: Dict[str, Dict[str, Any]] = {}
        for r in vector_results:
            all_items[r[key]] = {k: v for k, v in r.items() if k != "_score"}
        for r in keyword_results:
            if r[key] not in all_items:
                all_items[r[key]] = {k: v for k, v in r.items() if k != "_score"}

        # Calculate hybrid scores
        scored = []
        vw = Config.SEARCH_VECTOR_WEIGHT
        kw = Config.SEARCH_KEYWORD_WEIGHT
        for uid, item in all_items.items():
            v = v_scores.get(uid, 0.0)
            k = k_scores.get(uid, 0.0)
            combined = vw * v + kw * k
            item["score"] = combined
            scored.append(item)

        # Sort by combined score descending
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _escape_lucene(query: str) -> str:
        """Escape special Lucene query characters."""
        special = r'+-&|!(){}[]^"~*?:\/'
        result = []
        for ch in query:
            if ch in special:
                result.append('\\')
            result.append(ch)
        return ''.join(result)
