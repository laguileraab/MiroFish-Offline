"""
GraphitiStorage — GraphStorage backed by graphiti-core (Zep's open-source graph engine).

Uses one Neo4j database name (``NEO4J_DATABASE``, default ``neo4j``). Each MiroFish ``graph_id``
is Graphiti’s ``group_id`` on ``Entity`` / ``Episodic`` / ``RELATES_TO`` data. Registry rows use ``:Graph``
label as Neo4jStorage for ontology and listing.

Graphiti's public API is async; this adapter runs coroutines via ``asyncio.run`` under a
process lock so Flask worker threads do not interleave Graphiti calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from neo4j import GraphDatabase

from ..config import Config
from ..utils.ingest_metrics import build_ingest_metrics, log_ingest_metrics
from .graph_storage import GraphStorage

logger = logging.getLogger("mirofish.graphiti_storage")

# graphiti-core (optional until GRAPH_BACKEND=graphiti)
try:
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
    from graphiti_core.search.search_filters import SearchFilters
except ImportError as e:  # pragma: no cover
    Graphiti = None  # type: ignore[misc, assignment]
    Neo4jDriver = None  # type: ignore[misc, assignment]
    EpisodeType = None  # type: ignore[misc, assignment]
    COMBINED_HYBRID_SEARCH_RRF = None  # type: ignore[misc, assignment]
    SearchFilters = None  # type: ignore[misc, assignment]
    _GRAPHITI_IMPORT_ERROR = e
else:
    _GRAPHITI_IMPORT_ERROR = None


def _embedding_api_base_url() -> str:
    base = (Config.EMBEDDING_BASE_URL or "").rstrip("/")
    if not base:
        return "http://localhost:11434/v1"
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


class GraphitiStorage(GraphStorage):
    """GraphStorage implementation delegating ingest and search to Graphiti."""

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
        if Graphiti is None or Neo4jDriver is None:
            raise RuntimeError(
                "graphiti-core is not installed. Install with `pip install graphiti-core` "
                f"(import error: {_GRAPHITI_IMPORT_ERROR})"
            )

        self._uri = uri or Config.NEO4J_URI
        self._user = user or Config.NEO4J_USER
        self._password = password or Config.NEO4J_PASSWORD
        self._database = database or getattr(Config, "NEO4J_DATABASE", None) or "neo4j"

        self._sync = GraphDatabase.driver(
            self._uri,
            auth=(self._user, self._password or ""),
            max_connection_pool_size=Config.NEO4J_MAX_CONNECTION_POOL_SIZE,
            connection_acquisition_timeout=Config.NEO4J_CONNECTION_ACQUISITION_TIMEOUT,
        )
        self._async_lock = threading.RLock()
        self._last_episode_by_graph: Dict[str, str] = {}

        api_key = Config.LLM_API_KEY or "ollama"
        llm_cfg = LLMConfig(
            api_key=api_key,
            model=Config.LLM_MODEL_NAME,
            base_url=Config.LLM_BASE_URL,
            max_tokens=int(Config.NER_MAX_OUTPUT_TOKENS),
        )
        llm_client = OpenAIGenericClient(config=llm_cfg, max_tokens=int(Config.NER_MAX_OUTPUT_TOKENS))

        emb_key = api_key
        emb_cfg = OpenAIEmbedderConfig(
            api_key=emb_key,
            base_url=_embedding_api_base_url(),
            embedding_model=Config.EMBEDDING_MODEL,
        )
        embedder = OpenAIEmbedder(config=emb_cfg)

        max_coro = getattr(Config, "GRAPHITI_MAX_COROUTINES", None)
        if max_coro is None:
            max_coro = Config.LLM_INGEST_MAX_CONCURRENT if Config.LLM_INGEST_MAX_CONCURRENT > 0 else None

        graph_driver = Neo4jDriver(self._uri, self._user, self._password, database=self._database)
        self._graphiti = Graphiti(
            graph_driver=graph_driver,
            llm_client=llm_client,
            embedder=embedder,
            max_coroutines=max_coro,
        )

        self._run_async(self._graphiti.build_indices_and_constraints())
        logger.info(
            "GraphitiStorage initialized (Neo4j %s, database=%s)",
            self._uri,
            self._database,
        )

    @property
    def driver(self):
        """Sync Neo4j driver (admin metrics, Cypher helpers). Same as Neo4jStorage."""
        return self._sync

    def close(self) -> None:
        try:
            self._run_async(self._graphiti.close())
        except Exception as e:  # noqa: BLE001
            logger.warning("Graphiti close: %s", e)
        self._sync.close()

    def _session(self):
        return self._sync.session(database=self._database)

    def _run_async(self, coro):
        with self._async_lock:
            return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    def create_graph(self, name: str, description: str = "") -> str:
        graph_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        def _create(tx):
            tx.run(
                """
                CREATE (g:Graph {
                    graph_id: $graph_id,
                    name: $name,
                    description: $description,
                    ontology_json: '{}',
                    created_at: $created_at
                })
                """,
                graph_id=graph_id,
                name=name,
                description=description,
                created_at=now,
            )

        with self._session() as session:
            session.execute_write(_create)
        logger.info("Created graph '%s' with id %s (Graphiti group_id)", name, graph_id)
        return graph_id

    def delete_graph(self, graph_id: str) -> None:
        def _delete(tx):
            tx.run(
                "MATCH (n) WHERE n.group_id = $gid DETACH DELETE n",
                gid=graph_id,
            )
            tx.run(
                "MATCH (g:Graph {graph_id: $gid}) DELETE g",
                gid=graph_id,
            )

        with self._session() as session:
            session.execute_write(_delete)
        self._last_episode_by_graph.pop(graph_id, None)
        logger.info("Deleted graph %s (Graphiti group_id)", graph_id)

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        def _set(tx):
            tx.run(
                """
                MATCH (g:Graph {graph_id: $gid})
                SET g.ontology_json = $ontology_json
                """,
                gid=graph_id,
                ontology_json=json.dumps(ontology, ensure_ascii=False),
            )

        with self._session() as session:
            session.execute_write(_set)

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        with self._session() as session:
            result = session.run(
                "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj",
                gid=graph_id,
            )
            record = result.single()
            if record and record["oj"]:
                return json.loads(record["oj"])
            return {}

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def add_text(self, graph_id: str, text: str) -> str:
        from ..utils.llm_ingest_concurrency import acquire_ingest_slot

        with acquire_ingest_slot():
            return self._add_text_impl(graph_id, text)

    def _add_text_impl(self, graph_id: str, text: str) -> str:
        t0 = time.perf_counter()
        ref = datetime.now(timezone.utc)
        prev = self._last_episode_by_graph.get(graph_id)
        episode_uuid = str(uuid.uuid4())
        name = f"chunk-{episode_uuid[:8]}"

        async def _ingest():
            return await self._graphiti.add_episode(
                name=name,
                episode_body=text,
                source_description="mirofish_graph_ingest",
                reference_time=ref,
                source=EpisodeType.text,
                group_id=graph_id,
                uuid=episode_uuid,
                previous_episode_uuids=[prev] if prev else None,
            )

        try:
            res = self._run_async(_ingest())
        except Exception as e:
            logger.exception("Graphiti add_episode failed: %s", e)
            raise

        ep_uuid = res.episode.uuid
        self._last_episode_by_graph[graph_id] = ep_uuid
        n_nodes = len(res.nodes or [])
        n_edges = len(res.edges or [])
        duration_ms = (time.perf_counter() - t0) * 1000.0
        try:
            num_ctx = int(__import__("os").environ.get("OLLAMA_NUM_CTX", "8192"))
        except ValueError:
            num_ctx = 8192
        metrics = build_ingest_metrics(
            graph_id=graph_id,
            episode_id=ep_uuid,
            chunk_chars=len(text or ""),
            entity_count=n_nodes,
            relation_count=n_edges,
            ner_success=True,
            ner_error=None,
            duration_ms=duration_ms,
            ner_max_output_tokens=Config.NER_MAX_OUTPUT_TOKENS,
            ollama_num_ctx=num_ctx,
            usage=None,
            finish_reason=None,
            relations_skipped_missing_endpoint=0,
            ner_two_pass=False,
        )
        log_ingest_metrics(metrics, log=logger)
        logger.info("[add_text] Graphiti chunk done: episode=%s", ep_uuid)
        return ep_uuid

    def add_text_batch(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> List[str]:
        episode_ids: List[str] = []
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            if not chunk or not str(chunk).strip():
                continue
            episode_ids.append(self.add_text(graph_id, chunk))
            if progress_callback:
                progress_callback((i + 1) / max(total, 1))
        return episode_ids

    def wait_for_processing(
        self,
        episode_ids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600,
    ) -> None:
        if progress_callback:
            progress_callback(1.0)

    # ------------------------------------------------------------------
    # Read — Graphiti schema: Entity, RELATES_TO, Episodic, group_id
    # ------------------------------------------------------------------

    @staticmethod
    def _entity_node_to_dict(props: dict, labels: List[str]) -> Dict[str, Any]:
        summary = props.get("summary") or ""
        return {
            "uuid": props.get("uuid", ""),
            "name": props.get("name", ""),
            "labels": [x for x in labels if x != "Entity"],
            "summary": summary,
            "attributes": {},
            "created_at": props.get("created_at"),
        }

    @staticmethod
    def _entity_edge_to_dict(
        props: dict,
        source_uuid: str,
        target_uuid: str,
    ) -> Dict[str, Any]:
        eps = props.get("episodes") or []
        if eps and not isinstance(eps, list):
            eps = [str(eps)]
        return {
            "uuid": props.get("uuid", ""),
            "name": props.get("name", ""),
            "fact": props.get("fact", ""),
            "source_node_uuid": source_uuid,
            "target_node_uuid": target_uuid,
            "attributes": {},
            "created_at": props.get("created_at"),
            "valid_at": props.get("valid_at"),
            "invalid_at": props.get("invalid_at"),
            "expired_at": props.get("expired_at"),
            "episode_ids": [str(x) for x in eps],
        }

    def get_all_nodes(self, graph_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity {group_id: $gid})
                WITH n ORDER BY n.created_at DESC LIMIT $limit
                RETURN properties(n) AS props, labels(n) AS labels
                """,
                gid=graph_id,
                limit=limit,
            )
            out = []
            for record in result:
                out.append(
                    self._entity_node_to_dict(dict(record["props"]), list(record["labels"] or []))
                )
            return out

        with self._session() as session:
            return session.execute_read(_read)

    def get_node(self, uuid: str) -> Optional[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                "MATCH (n:Entity {uuid: $uuid}) RETURN properties(n) AS props, labels(n) AS labels",
                uuid=uuid,
            )
            record = result.single()
            if record:
                return self._entity_node_to_dict(
                    dict(record["props"]), list(record["labels"] or [])
                )
            return None

        with self._session() as session:
            return session.execute_read(_read)

    def get_node_edges(
        self,
        node_uuid: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        tw = ""
        if not include_invalid_relations:
            tw = "AND r.invalid_at IS NULL"

        def _read(tx):
            result = tx.run(
                f"""
                MATCH (n:Entity {{uuid: $uuid}})-[r:RELATES_TO]-(m:Entity)
                WHERE 1=1 {tw}
                RETURN properties(r) AS rp,
                       startNode(r).uuid AS su,
                       endNode(r).uuid AS eu
                """,
                uuid=node_uuid,
            )
            rows = []
            for record in result:
                su, eu = record["su"], record["eu"]
                rows.append(self._entity_edge_to_dict(dict(record["rp"]), su, eu))
            return rows

        with self._session() as session:
            edges = session.execute_read(_read)
        # as_of filtering is optional; Graphiti stores datetimes on edges
        if as_of:
            # Best-effort: skip strict temporal parsing for first cut
            pass
        return edges

    def get_nodes_by_label(self, graph_id: str, label: str) -> List[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity {group_id: $gid})
                WHERE $lbl IN n.labels
                RETURN properties(n) AS props, labels(n) AS labels
                """,
                gid=graph_id,
                lbl=label,
            )
            return [
                self._entity_node_to_dict(dict(r["props"]), list(r["labels"] or []))
                for r in result
            ]

        with self._session() as session:
            return session.execute_read(_read)

    def get_all_edges(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        tw = ""
        if not include_invalid_relations:
            tw = "AND r.invalid_at IS NULL"

        def _read(tx):
            result = tx.run(
                f"""
                MATCH (src:Entity)-[r:RELATES_TO {{group_id: $gid}}]->(tgt:Entity)
                WHERE 1=1 {tw}
                RETURN properties(r) AS rp, src.uuid AS su, tgt.uuid AS tu
                ORDER BY r.created_at DESC
                """,
                gid=graph_id,
            )
            return [self._entity_edge_to_dict(dict(rec["rp"]), rec["su"], rec["tu"]) for rec in result]

        with self._session() as session:
            return session.execute_read(_read)

    def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ):
        cfg = COMBINED_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit})

        async def _do():
            return await self._graphiti.search_(
                query,
                config=cfg,
                group_ids=[graph_id],
                search_filter=SearchFilters(),
            )

        sr = self._run_async(_do())
        edges_out: List[Dict[str, Any]] = []
        nodes_out: List[Dict[str, Any]] = []

        for e in sr.edges or []:
            d = e.model_dump(mode="python")
            edges_out.append(
                {
                    "uuid": d.get("uuid", ""),
                    "name": d.get("name", ""),
                    "fact": d.get("fact", ""),
                    "source_node_uuid": d.get("source_node_uuid", ""),
                    "target_node_uuid": d.get("target_node_uuid", ""),
                    "valid_at": d.get("valid_at"),
                    "invalid_at": d.get("invalid_at"),
                    "episode_ids": [str(x) for x in (d.get("episodes") or [])],
                }
            )

        for n in sr.nodes or []:
            d = n.model_dump(mode="python")
            nodes_out.append(
                {
                    "uuid": d.get("uuid", ""),
                    "name": d.get("name", ""),
                    "labels": [x for x in (d.get("labels") or []) if x != "Entity"],
                    "summary": d.get("summary") or "",
                }
            )

        if not include_invalid_relations:
            edges_out = [x for x in edges_out if not x.get("invalid_at")]

        if scope == "edges":
            return {"edges": edges_out}
        if scope == "nodes":
            return {"nodes": nodes_out}
        return {"edges": edges_out, "nodes": nodes_out}

    def get_graph_info(self, graph_id: str) -> Dict[str, Any]:
        def _read(tx):
            node_count = tx.run(
                "MATCH (n:Entity {group_id: $gid}) RETURN count(n) AS c",
                gid=graph_id,
            ).single()["c"]
            edge_count = tx.run(
                """
                MATCH ()-[r:RELATES_TO {group_id: $gid}]->()
                WHERE r.invalid_at IS NULL
                RETURN count(r) AS c
                """,
                gid=graph_id,
            ).single()["c"]
            rows = tx.run(
                """
                MATCH (n:Entity {group_id: $gid})
                UNWIND n.labels AS lbl
                WITH lbl WHERE lbl <> 'Entity'
                RETURN DISTINCT lbl
                """,
                gid=graph_id,
            )
            entity_types = [r["lbl"] for r in rows]
            return {
                "graph_id": graph_id,
                "node_count": node_count,
                "edge_count": edge_count,
                "entity_types": entity_types,
            }

        with self._session() as session:
            return session.execute_read(_read)

    def get_graph_data(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> Dict[str, Any]:
        tw = ""
        if not include_invalid_relations:
            tw = "AND r.invalid_at IS NULL"

        def _read(tx):
            node_result = tx.run(
                "MATCH (n:Entity {group_id: $gid}) RETURN properties(n) AS props, labels(n) AS labels",
                gid=graph_id,
            )
            nodes = []
            name_by_uuid: Dict[str, str] = {}
            for record in node_result:
                nd = self._entity_node_to_dict(dict(record["props"]), list(record["labels"] or []))
                nodes.append(nd)
                name_by_uuid[nd["uuid"]] = nd["name"]

            edge_result = tx.run(
                f"""
                MATCH (src:Entity)-[r:RELATES_TO {{group_id: $gid}}]->(tgt:Entity)
                WHERE 1=1 {tw}
                RETURN properties(r) AS rp, src.uuid AS su, tgt.uuid AS tu
                """,
                gid=graph_id,
            )
            edges = []
            for record in edge_result:
                ed = self._entity_edge_to_dict(dict(record["rp"]), record["su"], record["tu"])
                ed["fact_type"] = ed["name"]
                ed["source_node_name"] = name_by_uuid.get(ed["source_node_uuid"], "")
                ed["target_node_name"] = name_by_uuid.get(ed["target_node_uuid"], "")
                ed["episodes"] = ed.get("episode_ids", [])
                edges.append(ed)

            return {
                "graph_id": graph_id,
                "nodes": nodes,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }

        with self._session() as session:
            return session.execute_read(_read)

    # ------------------------------------------------------------------
    # Admin (Phase 14) — Episodic episodes, not legacy :Episode
    # ------------------------------------------------------------------

    def build_graph_admin_ingest_payload(
        self,
        process_counters: Dict[str, Any],
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        neo: Dict[str, Any] = {}
        try:
            with self._session() as session:
                if graph_id:
                    rec = session.run(
                        "MATCH (n:Episodic {group_id: $gid}) RETURN count(n) AS c",
                        gid=graph_id,
                    ).single()
                    neo = {"episodes_total": int(rec["c"]) if rec else 0, "by_graph": []}
                else:
                    rows = session.run(
                        """
                        MATCH (n:Episodic)
                        RETURN n.group_id AS graph_id, count(n) AS episodes
                        ORDER BY episodes DESC
                        LIMIT 50
                        """
                    )
                    by_graph = [
                        {"graph_id": r["graph_id"], "episodes": int(r["episodes"])} for r in rows
                    ]
                    total = sum(x["episodes"] for x in by_graph)
                    neo = {"episodes_total": total, "by_graph": by_graph}
        except Exception as e:  # noqa: BLE001
            neo = {"error": str(e)}
        return {"process_counters": process_counters, "neo4j": neo}
