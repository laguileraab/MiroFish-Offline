"""
GraphStorage implementation backed entirely by **graphiti-core**.

**Why Neo4j is still configured:** Graphiti is not a database. For the Neo4j provider,
``graphiti-core`` persists the temporal context graph *into* Neo4j (``Entity``, ``Episodic``,
``RELATES_TO``, etc.). ``NEO4J_URI`` / ``NEO4J_DATABASE`` are therefore the **Graphiti backend**,
not a parallel custom graph stack. To drop Neo4j you would switch Graphiti to another supported
backend (e.g. FalkorDB, Kuzu) and change driver construction upstream — out of scope here.

**Async vs Flask:** Graphiti’s API is asyncio-based. The sync ``GraphStorage`` contract is
bridged by scheduling coroutines on a **single background event loop** (daemon thread) with
``run_coroutine_threadsafe`` + a re-entrant lock — the async Neo4j driver must stay on one loop.
You do **not** need async Flask routes unless you migrate to ASGI and can ``await`` Graphiti.

**MiroFish-only additions (Graphiti does not provide these):**
- ``:Graph`` registry nodes for human-readable name/description and JSON ontology used by the
  rest of the app (same contract as ``Neo4jStorage``).

All graph *content* reads/writes for partitions go through Graphiti’s driver and models.
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

from ..config import Config
from ..utils.ingest_metrics import build_ingest_metrics, log_ingest_metrics
from .graph_storage import GraphStorage

logger = logging.getLogger("mirofish.graphiti_storage")

try:
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.edges import EntityEdge
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.errors import GroupsEdgesNotFoundError, NodeNotFoundError
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
    from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
    from graphiti_core.search.search_filters import SearchFilters
    from graphiti_core.utils.maintenance import clear_data
except ImportError as e:  # pragma: no cover
    Graphiti = None  # type: ignore[misc, assignment]
    Neo4jDriver = None  # type: ignore[misc, assignment]
    EntityEdge = None  # type: ignore[misc, assignment]
    EntityNode = None  # type: ignore[misc, assignment]
    EpisodeType = None  # type: ignore[misc, assignment]
    EpisodicNode = None  # type: ignore[misc, assignment]
    COMBINED_HYBRID_SEARCH_RRF = None  # type: ignore[misc, assignment]
    SearchFilters = None  # type: ignore[misc, assignment]
    clear_data = None  # type: ignore[misc, assignment]
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
    """Delegates partition data to Graphiti; keeps minimal ``:Graph`` registry for the app UI."""

    _bg_loop: Optional[asyncio.AbstractEventLoop] = None
    _bg_thread: Optional[threading.Thread] = None
    _bg_lock = threading.Lock()

    @classmethod
    def _ensure_background_loop(cls) -> asyncio.AbstractEventLoop:
        with cls._bg_lock:
            if cls._bg_loop is not None:
                return cls._bg_loop
            ready = threading.Event()
            holder: list[Optional[asyncio.AbstractEventLoop]] = [None]

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder[0] = loop
                ready.set()
                loop.run_forever()

            t = threading.Thread(target=_runner, daemon=True, name="mirofish-graphiti-async")
            t.start()
            ready.wait(timeout=30.0)
            cls._bg_loop = holder[0]
            cls._bg_thread = t
            if cls._bg_loop is None:
                raise RuntimeError("Graphiti background event loop failed to start")
            return cls._bg_loop

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

        self._op_lock = threading.RLock()

        api_key = Config.LLM_API_KEY or "ollama"
        llm_cfg = LLMConfig(
            api_key=api_key,
            model=Config.LLM_MODEL_NAME,
            base_url=Config.LLM_BASE_URL,
            max_tokens=int(Config.NER_MAX_OUTPUT_TOKENS),
        )
        llm_client = OpenAIGenericClient(config=llm_cfg, max_tokens=int(Config.NER_MAX_OUTPUT_TOKENS))

        emb_cfg = OpenAIEmbedderConfig(
            api_key=api_key,
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
            "GraphitiStorage initialized (Graphiti → Neo4j %s, database=%s)",
            self._uri,
            self._database,
        )

    @property
    def driver(self):
        """
        No separate sync Neo4j driver: Graphiti owns the async driver.

        Admin routes use ``build_graph_admin_ingest_payload`` when present.
        """
        return None

    def close(self) -> None:
        try:
            self._run_async(self._graphiti.close())
        except Exception as e:  # noqa: BLE001
            logger.warning("Graphiti close: %s", e)

    def _async_timeout_sec(self) -> float:
        return max(60.0, float(Config.LLM_HTTP_TIMEOUT_SEC))

    def _run_async(self, coro):
        loop = self._ensure_background_loop()
        with self._op_lock:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=self._async_timeout_sec())

    @staticmethod
    def _entity_model_to_dict(node: Any) -> Dict[str, Any]:
        d = node.model_dump(mode="python")
        labels = d.get("labels") or []
        return {
            "uuid": d.get("uuid", ""),
            "name": d.get("name", ""),
            "labels": [x for x in labels if x != "Entity"],
            "summary": d.get("summary") or "",
            "attributes": d.get("attributes") or {},
            "created_at": d.get("created_at"),
        }

    @staticmethod
    def _edge_model_to_dict(edge: Any) -> Dict[str, Any]:
        d = edge.model_dump(mode="python")
        eps = d.get("episodes") or []
        if eps and not isinstance(eps, list):
            eps = [str(eps)]
        return {
            "uuid": d.get("uuid", ""),
            "name": d.get("name", ""),
            "fact": d.get("fact", ""),
            "source_node_uuid": d.get("source_node_uuid", ""),
            "target_node_uuid": d.get("target_node_uuid", ""),
            "attributes": {},
            "created_at": d.get("created_at"),
            "valid_at": d.get("valid_at"),
            "invalid_at": d.get("invalid_at"),
            "expired_at": d.get("expired_at"),
            "episode_ids": [str(x) for x in eps],
        }

    # ------------------------------------------------------------------
    # Registry (MiroFish-only; not part of Graphiti’s model)
    # ------------------------------------------------------------------

    async def _registry_create(self, graph_id: str, name: str, description: str, created_at: str) -> None:
        await self._graphiti.driver.execute_query(
            """
            CREATE (g:Graph {
                graph_id: $graph_id,
                name: $name,
                description: $description,
                ontology_json: $ontology_json,
                created_at: $created_at
            })
            """,
            graph_id=graph_id,
            name=name,
            description=description,
            ontology_json="{}",
            created_at=created_at,
        )

    async def _registry_delete(self, graph_id: str) -> None:
        await self._graphiti.driver.execute_query(
            "MATCH (g:Graph {graph_id: $gid}) DELETE g",
            gid=graph_id,
        )

    async def _registry_set_ontology(self, graph_id: str, ontology_json: str) -> None:
        await self._graphiti.driver.execute_query(
            """
            MATCH (g:Graph {graph_id: $gid})
            SET g.ontology_json = $ontology_json
            """,
            gid=graph_id,
            ontology_json=ontology_json,
        )

    async def _registry_get_ontology(self, graph_id: str) -> Dict[str, Any]:
        records, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj LIMIT 1",
            gid=graph_id,
        )
        if records and records[0].get("oj"):
            return json.loads(records[0]["oj"])
        return {}

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    def create_graph(self, name: str, description: str = "") -> str:
        graph_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._run_async(self._registry_create(graph_id, name, description, now))
        logger.info("Created graph '%s' with id %s (Graphiti group_id)", name, graph_id)
        return graph_id

    def delete_graph(self, graph_id: str) -> None:
        async def _purge():
            await clear_data(self._graphiti.driver, group_ids=[graph_id])
            await self._registry_delete(graph_id)

        self._run_async(_purge())
        logger.info("Deleted graph %s via Graphiti clear_data + registry", graph_id)

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        oj = json.dumps(ontology, ensure_ascii=False)
        self._run_async(self._registry_set_ontology(graph_id, oj))

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        return self._run_async(self._registry_get_ontology(graph_id))

    # ------------------------------------------------------------------
    # Ingest — Graphiti episode pipeline; prior context from DB (not RAM)
    # ------------------------------------------------------------------

    def add_text(self, graph_id: str, text: str) -> str:
        from ..utils.llm_ingest_concurrency import acquire_ingest_slot

        with acquire_ingest_slot():
            return self._add_text_impl(graph_id, text)

    def _add_text_impl(self, graph_id: str, text: str) -> str:
        t0 = time.perf_counter()
        ref = datetime.now(timezone.utc)
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
                previous_episode_uuids=None,
            )

        try:
            res = self._run_async(_ingest())
        except Exception as e:
            logger.exception("Graphiti add_episode failed: %s", e)
            raise

        ep_uuid = res.episode.uuid
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
    # Reads — Graphiti EntityNode / EntityEdge APIs
    # ------------------------------------------------------------------

    def get_all_nodes(self, graph_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        async def _read():
            nodes = await EntityNode.get_by_group_ids(
                self._graphiti.driver, [graph_id], limit=limit, uuid_cursor=None
            )
            return [self._entity_model_to_dict(n) for n in nodes]

        return self._run_async(_read())

    def get_node(self, uuid: str) -> Optional[Dict[str, Any]]:
        async def _read():
            try:
                n = await EntityNode.get_by_uuid(self._graphiti.driver, uuid)
            except NodeNotFoundError:
                return None
            return self._entity_model_to_dict(n)

        return self._run_async(_read())

    def get_node_edges(
        self,
        node_uuid: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        async def _read():
            edges = await EntityEdge.get_by_node_uuid(self._graphiti.driver, node_uuid)
            out = [self._edge_model_to_dict(e) for e in edges]
            if not include_invalid_relations:
                out = [e for e in out if not e.get("invalid_at")]
            return out

        edges = self._run_async(_read())
        if as_of:
            pass
        return edges

    def get_nodes_by_label(self, graph_id: str, label: str) -> List[Dict[str, Any]]:
        async def _read():
            nodes = await EntityNode.get_by_group_ids(self._graphiti.driver, [graph_id])
            return [
                self._entity_model_to_dict(n)
                for n in nodes
                if label in (n.labels or [])
            ]

        return self._run_async(_read())

    def get_all_edges(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        async def _read():
            try:
                edges = await EntityEdge.get_by_group_ids(self._graphiti.driver, [graph_id])
            except GroupsEdgesNotFoundError:
                edges = []
            out = [self._edge_model_to_dict(e) for e in edges]
            if not include_invalid_relations:
                out = [e for e in out if not e.get("invalid_at")]
            return out

        return self._run_async(_read())

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
            edges_out.append(self._edge_model_to_dict(e))

        for n in sr.nodes or []:
            d = n.model_dump(mode="python")
            labels = d.get("labels") or []
            nodes_out.append(
                {
                    "uuid": d.get("uuid", ""),
                    "name": d.get("name", ""),
                    "labels": [x for x in labels if x != "Entity"],
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
        async def _read():
            nodes = await EntityNode.get_by_group_ids(self._graphiti.driver, [graph_id])
            try:
                edges = await EntityEdge.get_by_group_ids(self._graphiti.driver, [graph_id])
            except GroupsEdgesNotFoundError:
                edges = []
            active_edges = [e for e in edges if not e.invalid_at]
            labels: set[str] = set()
            for n in nodes:
                for lb in n.labels or []:
                    if lb != "Entity":
                        labels.add(lb)
            return {
                "graph_id": graph_id,
                "node_count": len(nodes),
                "edge_count": len(active_edges),
                "entity_types": sorted(labels),
            }

        return self._run_async(_read())

    def get_graph_data(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> Dict[str, Any]:
        async def _read():
            nodes_m = await EntityNode.get_by_group_ids(self._graphiti.driver, [graph_id])
            try:
                edges_m = await EntityEdge.get_by_group_ids(self._graphiti.driver, [graph_id])
            except GroupsEdgesNotFoundError:
                edges_m = []

            nodes = [self._entity_model_to_dict(n) for n in nodes_m]
            name_by_uuid = {n["uuid"]: n["name"] for n in nodes}

            edges: List[Dict[str, Any]] = []
            for e in edges_m:
                ed = self._edge_model_to_dict(e)
                if not include_invalid_relations and ed.get("invalid_at"):
                    continue
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

        return self._run_async(_read())

    # ------------------------------------------------------------------
    # Admin — EpisodicNode counts (Graphiti-native)
    # ------------------------------------------------------------------

    def build_graph_admin_ingest_payload(
        self,
        process_counters: Dict[str, Any],
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        neo: Dict[str, Any] = {}
        try:

            async def _rollup():
                if graph_id:
                    eps = await EpisodicNode.get_by_group_ids(self._graphiti.driver, [graph_id])
                    return {"episodes_total": len(eps), "by_graph": []}
                records, _, _ = await self._graphiti.driver.execute_query(
                    """
                    MATCH (n:Episodic)
                    RETURN n.group_id AS graph_id, count(n) AS episodes
                    ORDER BY episodes DESC
                    LIMIT 50
                    """
                )
                by_graph = [
                    {"graph_id": r["graph_id"], "episodes": int(r["episodes"])} for r in records
                ]
                total = sum(x["episodes"] for x in by_graph)
                return {"episodes_total": total, "by_graph": by_graph}

            neo = self._run_async(_rollup())
        except Exception as e:  # noqa: BLE001
            neo = {"error": str(e)}
        return {"process_counters": process_counters, "neo4j": neo}
