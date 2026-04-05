"""
Neo4jStorage — Neo4j Community Edition implementation of GraphStorage.

Replaces all Zep Cloud API calls with local Neo4j Cypher queries.
Includes: CRUD, NER/RE-based text ingestion, hybrid search, retry logic.
"""

import copy
import hashlib
import json
import os
import re
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable, Tuple

from neo4j import GraphDatabase, Session as Neo4jSession
from neo4j.exceptions import (
    TransientError,
    ServiceUnavailable,
    SessionExpired,
)

from ..config import Config, parse_iso8601_utc
from ..utils.ingest_metrics import (
    build_ingest_metrics,
    log_ingest_metrics,
    warn_ingest_guardrails,
)
from .graph_storage import GraphStorage
from .embedding_service import EmbeddingService
from .ner_extractor import NERExtractor
from .search_service import SearchService
from . import neo4j_schema
from .entity_normalize import (
    normalize_entities_in_place,
    normalize_fact_key,
    normalize_relation_endpoints,
)
from .merge_adjudicator import llm_same_real_world_entity
from .relation_temporal_filters import relation_temporal_where

logger = logging.getLogger('mirofish.neo4j_storage')


def _sanitize_neo4j_label(label: str) -> str:
    """Restrict dynamic entity-type labels to safe Neo4j identifier syntax."""
    s = (label or "").strip()
    if not s or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", s):
        raise ValueError(f"Invalid entity type label for Neo4j: {label!r}")
    return s


class Neo4jStorage(GraphStorage):
    """Neo4j CE implementation of the GraphStorage interface."""

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1  # seconds

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        embedding_service: Optional[EmbeddingService] = None,
        ner_extractor: Optional[NERExtractor] = None,
    ):
        self._uri = uri or Config.NEO4J_URI
        self._user = user or Config.NEO4J_USER
        self._password = password or Config.NEO4J_PASSWORD

        self._driver = GraphDatabase.driver(
            self._uri,
            auth=(self._user, self._password),
            max_connection_pool_size=Config.NEO4J_MAX_CONNECTION_POOL_SIZE,
            connection_acquisition_timeout=Config.NEO4J_CONNECTION_ACQUISITION_TIMEOUT,
        )
        self._embedding = embedding_service or EmbeddingService()
        self._ner = ner_extractor or NERExtractor()
        self._search = SearchService(self._embedding)
        self._search_result_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # Initialize schema (indexes, constraints)
        self._ensure_schema()

    @property
    def driver(self):
        """Neo4j driver (for admin metrics / advanced callers)."""
        return self._driver

    def close(self):
        """Close the Neo4j driver connection."""
        self._driver.close()

    def _ensure_schema(self):
        """Create indexes and constraints if they don't exist."""
        with self._driver.session() as session:
            for query in neo4j_schema.ALL_SCHEMA_QUERIES:
                try:
                    session.run(query)
                except Exception as e:
                    logger.warning(f"Schema query warning (may already exist): {e}")

    # ----------------------------------------------------------------
    # Retry wrapper
    # ----------------------------------------------------------------

    def _graph_link_candidates(self, graph_id: str, text: str) -> List[Dict[str, str]]:
        """Phase 10 TASK-030 — top-K graph entities similar to chunk text for relation linking."""
        k = Config.NER_LINK_TOP_K
        if k <= 0:
            return []
        q = (text or "")[:2000].strip()
        if not q:
            return []
        try:
            with self._driver.session() as session:
                hits = self._search.search_nodes(session, graph_id, q, limit=k)
        except Exception as e:
            logger.warning("Graph link candidate search failed: %s", e)
            return []
        out: List[Dict[str, str]] = []
        seen: set[str] = set()
        for h in hits:
            name = (h.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            summ = (h.get("summary") or "")[:200]
            out.append({"name": name, "summary": summ})
        return out

    def _vector_resolve_canonical_uuid(
        self,
        session: Neo4jSession,
        graph_id: str,
        surface_name: str,
        name_lower: str,
        embedding: list,
        summary_text: str,
    ) -> Optional[str]:
        """
        Phase 11 TASK-035/036 — if another entity is very similar in embedding space, reuse its uuid.
        """
        if not Config.GRAPH_MERGE_VECTOR_ENABLED or not embedding:
            return None
        try:
            hits = self._search.search_nodes_by_vector(
                session, graph_id, embedding, limit=10
            )
        except Exception as e:
            logger.warning("Vector merge search failed: %s", e)
            return None

        for h in hits:
            uid = h.get("uuid")
            hn_raw = (h.get("name") or "").strip()
            hl = (h.get("name_lower") or hn_raw.lower()).strip()
            if not uid or hl == name_lower:
                continue
            score = float(h.get("_score", 0.0))
            if score < Config.GRAPH_MERGE_VECTOR_AMBIG_LOW:
                break
            if score >= Config.GRAPH_MERGE_VECTOR_THRESHOLD:
                logger.info(
                    "Vector merge: %r -> canonical %r (score=%.3f)",
                    surface_name,
                    hn_raw,
                    score,
                )
                return str(uid)
            if Config.GRAPH_MERGE_LLM_ADJUDICATE:
                summ_b = (h.get("summary") or "")[:400]
                if llm_same_real_world_entity(
                    self._ner.llm,
                    surface_name,
                    summary_text,
                    hn_raw,
                    summ_b,
                ):
                    logger.info("LLM merge adjudication: %r -> %r", surface_name, hn_raw)
                    return str(uid)
        return None

    def _call_with_retry(self, func, *args, **kwargs):
        """
        Execute a function with retry on Neo4j transient errors.
        Replaces 3 different retry patterns from the Zep codebase.
        """
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except (TransientError, ServiceUnavailable, SessionExpired) as e:
                last_error = e
                wait = self.RETRY_DELAY_BASE * (2 ** attempt)
                logger.warning(
                    f"Neo4j transient error (attempt {attempt + 1}/{self.MAX_RETRIES}), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
            except Exception:
                raise

        raise last_error  # type: ignore

    # ----------------------------------------------------------------
    # Graph lifecycle
    # ----------------------------------------------------------------

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

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _create)

        logger.info(f"Created graph '{name}' with id {graph_id}")
        return graph_id

    def delete_graph(self, graph_id: str) -> None:
        def _delete(tx):
            # Delete all entities and their relationships
            tx.run(
                "MATCH (n {graph_id: $gid}) DETACH DELETE n",
                gid=graph_id,
            )
            # Delete graph node
            tx.run(
                "MATCH (g:Graph {graph_id: $gid}) DELETE g",
                gid=graph_id,
            )

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _delete)
        logger.info(f"Deleted graph {graph_id}")

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

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _set)

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj",
                gid=graph_id,
            )
            record = result.single()
            if record and record["oj"]:
                return json.loads(record["oj"])
            return {}

    # ----------------------------------------------------------------
    # Add data (NER → nodes/edges)
    # ----------------------------------------------------------------

    def add_text(self, graph_id: str, text: str) -> str:
        """Process text: NER/RE → batch embed → create nodes/edges → return episode_id."""
        from ..utils.llm_ingest_concurrency import acquire_ingest_slot

        with acquire_ingest_slot():
            return self._add_text_impl(graph_id, text)

    def _add_text_impl(self, graph_id: str, text: str) -> str:
        episode_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        # Get ontology for NER guidance
        ontology = self.get_ontology(graph_id)

        # Extract entities and relations
        link_cand: List[Dict[str, str]] = []
        if Config.NER_LINK_TOP_K > 0:
            link_cand = self._graph_link_candidates(graph_id, text)
        if Config.NER_TWO_PASS:
            logger.info(
                "[add_text] NER two-pass + link candidates=%s for chunk (%s chars)...",
                len(link_cand),
                len(text),
            )
        else:
            logger.info("[add_text] Starting NER extraction for chunk (%s chars)...", len(text))
        extraction = self._ner.extract(
            text, ontology, graph_link_candidates=link_cand if link_cand else None
        )
        entities = extraction.get("entities", [])
        relations = extraction.get("relations", [])

        normalize_entities_in_place(entities)
        normalize_relation_endpoints(relations)

        logger.info(
            f"[add_text] NER done: {len(entities)} entities, {len(relations)} relations"
        )

        # --- Batch embed all texts at once ---
        entity_summaries = [f"{e['name']} ({e['type']})" for e in entities]
        fact_texts = [r.get("fact", f"{r['source']} {r['type']} {r['target']}") for r in relations]
        all_texts_to_embed = entity_summaries + fact_texts

        all_embeddings: list = []
        if all_texts_to_embed:
            logger.info(f"[add_text] Batch-embedding {len(all_texts_to_embed)} texts...")
            try:
                all_embeddings = self._embedding.embed_batch(all_texts_to_embed)
            except Exception as e:
                logger.warning(f"[add_text] Batch embedding failed, falling back to empty: {e}")
                all_embeddings = [[] for _ in all_texts_to_embed]

        entity_embeddings = all_embeddings[:len(entities)]
        relation_embeddings = all_embeddings[len(entities):]
        logger.info(f"[add_text] Embedding done, writing to Neo4j...")

        relations_skipped_missing = 0

        with self._driver.session() as session:
            # Create episode node
            def _create_episode(tx):
                tx.run(
                    """
                    CREATE (ep:Episode {
                        uuid: $uuid,
                        graph_id: $graph_id,
                        data: $data,
                        processed: true,
                        created_at: $created_at
                    })
                    """,
                    uuid=episode_id,
                    graph_id=graph_id,
                    data=text,
                    created_at=now,
                )

            self._call_with_retry(session.execute_write, _create_episode)

            # MERGE entities (upsert by graph_id + name + primary label)
            entity_uuid_map: Dict[str, str] = {}  # name_lower -> uuid
            for idx, entity in enumerate(entities):
                ename = entity["name"]
                etype = entity["type"]
                attrs = entity.get("attributes", {})
                summary_text = entity_summaries[idx]
                embedding = entity_embeddings[idx] if idx < len(entity_embeddings) else []

                canon = self._vector_resolve_canonical_uuid(
                    session,
                    graph_id,
                    ename,
                    ename.lower(),
                    embedding,
                    summary_text,
                )
                if canon:
                    entity_uuid_map[ename.lower()] = canon
                    continue

                e_uuid = str(uuid.uuid4())
                entity_uuid_map[ename.lower()] = e_uuid

                def _merge_entity(tx, _uuid=e_uuid, _name=ename, _type=etype,
                                  _attrs=attrs, _embedding=embedding,
                                  _summary=summary_text, _now=now):
                    # MERGE by graph_id + lowercase name to deduplicate
                    result = tx.run(
                        """
                        MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower})
                        ON CREATE SET
                            n.uuid = $uuid,
                            n.name = $name,
                            n.summary = $summary,
                            n.attributes_json = $attrs_json,
                            n.embedding = $embedding,
                            n.created_at = $now
                        ON MATCH SET
                            n.summary = CASE WHEN n.summary = '' OR n.summary IS NULL
                                THEN $summary ELSE n.summary END,
                            n.attributes_json = $attrs_json,
                            n.embedding = $embedding
                        RETURN n.uuid AS uuid
                        """,
                        gid=graph_id,
                        name_lower=_name.lower(),
                        uuid=_uuid,
                        name=_name,
                        summary=_summary,
                        attrs_json=json.dumps(_attrs, ensure_ascii=False),
                        embedding=_embedding,
                        now=_now,
                    )
                    record = result.single()
                    return record["uuid"] if record else _uuid

                actual_uuid = self._call_with_retry(session.execute_write, _merge_entity)
                entity_uuid_map[ename.lower()] = actual_uuid

                # Add entity type label (sanitized — LLM output must not inject Cypher)
                if etype and etype != "Entity":
                    try:
                        safe_label = _sanitize_neo4j_label(etype)

                        def _add_label(tx, _name_lower=ename.lower(), _label=safe_label):
                            tx.run(
                                f"MATCH (n:Entity {{graph_id: $gid, name_lower: $nl}}) SET n:`{_label}`",
                                gid=graph_id,
                                nl=_name_lower,
                            )

                        self._call_with_retry(session.execute_write, _add_label)
                    except ValueError as e:
                        logger.warning("Skipping dynamic label for '%s': %s", ename, e)
                    except Exception as e:
                        logger.warning(f"Failed to add label '{etype}' to '{ename}': {e}")

            # Episode → entity provenance (Phase 10 TASK-033)
            uuids_for_mentions = list(dict.fromkeys(entity_uuid_map.values()))
            if uuids_for_mentions:

                def _link_mentions(tx):
                    tx.run(
                        """
                        MATCH (ep:Episode {uuid: $eid, graph_id: $gid})
                        UNWIND $uuids AS uid
                        MATCH (n:Entity {uuid: uid, graph_id: $gid})
                        MERGE (ep)-[:MENTIONS]->(n)
                        """,
                        eid=episode_id,
                        gid=graph_id,
                        uuids=uuids_for_mentions,
                    )

                self._call_with_retry(session.execute_write, _link_mentions)

            # Create relations
            for idx, relation in enumerate(relations):
                source_name = relation["source"]
                target_name = relation["target"]
                rtype = relation["type"]
                fact = relation["fact"]

                source_uuid = entity_uuid_map.get(source_name.lower())
                target_uuid = entity_uuid_map.get(target_name.lower())

                if not source_uuid or not target_uuid:
                    relations_skipped_missing += 1
                    logger.warning(
                        f"Skipping relation {source_name}->{target_name}: "
                        f"entity not found in extraction results"
                    )
                    continue

                fact_embedding = relation_embeddings[idx] if idx < len(relation_embeddings) else []
                r_uuid = str(uuid.uuid4())
                fact_norm = normalize_fact_key(fact)
                rel_valid_at = None
                supersedes_uuid = None
                if Config.GRAPH_TEMPORAL_ENABLED:
                    rel_valid_at = parse_iso8601_utc(relation.get("valid_from")) or now
                    su = (relation.get("supersedes_relation_uuid") or "").strip()
                    if su:
                        try:
                            uuid.UUID(su)
                            supersedes_uuid = su
                        except ValueError:
                            pass

                def _upsert_relation(
                    tx,
                    _r_uuid=r_uuid,
                    _source_uuid=source_uuid,
                    _target_uuid=target_uuid,
                    _rtype=rtype,
                    _fact=fact,
                    _fn=fact_norm,
                    _fact_emb=fact_embedding,
                    _episode_id=episode_id,
                    _now=now,
                    _valid_at=rel_valid_at,
                    _sup=supersedes_uuid,
                ):
                    if _sup and Config.GRAPH_TEMPORAL_ENABLED:
                        tx.run(
                            """
                            MATCH ()-[r:RELATION {uuid: $u, graph_id: $gid}]->()
                            SET r.invalid_at = $now
                            """,
                            u=_sup,
                            gid=graph_id,
                            now=_now,
                        )

                    def _invalidate_conflicting_same_triple():
                        if not (
                            Config.GRAPH_TEMPORAL_ENABLED
                            and Config.GRAPH_TEMPORAL_SUPERSEDE_SAME_TRIPLE
                        ):
                            return
                        tx.run(
                            """
                            MATCH (src:Entity {uuid: $su, graph_id: $gid})
                                  -[r:RELATION]->(tgt:Entity {uuid: $tu, graph_id: $gid})
                            WHERE r.graph_id = $gid AND r.name = $rtype
                              AND r.invalid_at IS NULL
                              AND NOT (
                                (r.fact_normalized IS NOT NULL AND r.fact_normalized = $fn) OR
                                (r.fact_normalized IS NULL AND r.fact = $fact)
                              )
                            SET r.invalid_at = $now
                            """,
                            su=_source_uuid,
                            tu=_target_uuid,
                            gid=graph_id,
                            rtype=_rtype,
                            fn=_fn,
                            fact=_fact,
                            now=_now,
                        )

                    _va = _valid_at if Config.GRAPH_TEMPORAL_ENABLED else None

                    if not Config.RELATION_DEDUPE_ENABLED:
                        _invalidate_conflicting_same_triple()
                        tx.run(
                            """
                            MATCH (src:Entity {uuid: $src_uuid, graph_id: $gid})
                            MATCH (tgt:Entity {uuid: $tgt_uuid, graph_id: $gid})
                            CREATE (src)-[r:RELATION {
                                uuid: $uuid,
                                graph_id: $gid,
                                name: $name,
                                fact: $fact,
                                fact_normalized: $fn,
                                fact_embedding: $fact_embedding,
                                attributes_json: '{}',
                                episode_ids: [$episode_id],
                                created_at: $now,
                                valid_at: $valid_at,
                                invalid_at: null,
                                expired_at: null
                            }]->(tgt)
                            """,
                            src_uuid=_source_uuid,
                            tgt_uuid=_target_uuid,
                            uuid=_r_uuid,
                            gid=graph_id,
                            name=_rtype,
                            fact=_fact,
                            fn=_fn,
                            fact_embedding=_fact_emb,
                            episode_id=_episode_id,
                            now=_now,
                            valid_at=_va,
                        )
                        return
                    row = tx.run(
                        """
                        MATCH (src:Entity {uuid: $src_uuid, graph_id: $gid})
                        MATCH (tgt:Entity {uuid: $tgt_uuid, graph_id: $gid})
                        MATCH (src)-[r:RELATION]->(tgt)
                        WHERE r.graph_id = $gid AND r.name = $rtype AND (
                            (r.fact_normalized IS NOT NULL AND r.fact_normalized = $fn) OR
                            (r.fact_normalized IS NULL AND r.fact = $fact)
                        )
                        RETURN r.uuid AS ru, r.episode_ids AS eps
                        LIMIT 1
                        """,
                        src_uuid=_source_uuid,
                        tgt_uuid=_target_uuid,
                        gid=graph_id,
                        rtype=_rtype,
                        fn=_fn,
                        fact=_fact,
                    ).single()
                    if row:
                        ru = row["ru"]
                        eps = list(row["eps"] or [])
                        if _episode_id not in eps:
                            eps.append(_episode_id)
                        tx.run(
                            """
                            MATCH ()-[r:RELATION {uuid: $ru}]->()
                            SET r.episode_ids = $eps,
                                r.fact_normalized = coalesce(r.fact_normalized, $fn),
                                r.fact_embedding = CASE WHEN $emb IS NULL OR size($emb) = 0
                                    THEN r.fact_embedding ELSE $emb END,
                                r.invalid_at = CASE WHEN $clear_invalid THEN null ELSE r.invalid_at END
                            """,
                            ru=ru,
                            eps=eps,
                            fn=_fn,
                            emb=_fact_emb,
                            clear_invalid=Config.GRAPH_TEMPORAL_ENABLED,
                        )
                    else:
                        _invalidate_conflicting_same_triple()
                        tx.run(
                            """
                            MATCH (src:Entity {uuid: $src_uuid, graph_id: $gid})
                            MATCH (tgt:Entity {uuid: $tgt_uuid, graph_id: $gid})
                            CREATE (src)-[r:RELATION {
                                uuid: $uuid,
                                graph_id: $gid,
                                name: $name,
                                fact: $fact,
                                fact_normalized: $fn,
                                fact_embedding: $fact_embedding,
                                attributes_json: '{}',
                                episode_ids: [$episode_id],
                                created_at: $now,
                                valid_at: $valid_at,
                                invalid_at: null,
                                expired_at: null
                            }]->(tgt)
                            """,
                            src_uuid=_source_uuid,
                            tgt_uuid=_target_uuid,
                            uuid=_r_uuid,
                            gid=graph_id,
                            name=_rtype,
                            fact=_fact,
                            fn=_fn,
                            fact_embedding=_fact_emb,
                            episode_id=_episode_id,
                            now=_now,
                            valid_at=_va,
                        )

                self._call_with_retry(session.execute_write, _upsert_relation)

            # Optional entity summary refresh (TASK-038)
            if Config.ENTITY_SUMMARY_MAX_PER_CHUNK > 0:
                from ..services.graph_maintenance import refresh_entity_summary_llm

                n_refresh = 0
                for ent in entities:
                    if n_refresh >= Config.ENTITY_SUMMARY_MAX_PER_CHUNK:
                        break
                    uid = entity_uuid_map.get(ent.get("name", "").lower())
                    if not uid:
                        continue
                    if refresh_entity_summary_llm(
                        self._driver,
                        graph_id,
                        uid,
                        ent.get("name", ""),
                        str(ent.get("type") or "Entity"),
                        self._ner.llm,
                    ):
                        n_refresh += 1

        duration_ms = (time.perf_counter() - t0) * 1000.0
        try:
            num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        except ValueError:
            num_ctx = 8192
        metrics = build_ingest_metrics(
            graph_id=graph_id,
            episode_id=episode_id,
            chunk_chars=len(text or ""),
            entity_count=len(entities),
            relation_count=len(relations),
            ner_success=bool(extraction.get("success")),
            ner_error=extraction.get("error"),
            duration_ms=duration_ms,
            ner_max_output_tokens=Config.NER_MAX_OUTPUT_TOKENS,
            ollama_num_ctx=num_ctx,
            usage=extraction.get("usage"),
            finish_reason=extraction.get("finish_reason"),
            relations_skipped_missing_endpoint=relations_skipped_missing,
            ner_two_pass=bool(extraction.get("ner_two_pass")),
        )
        log_ingest_metrics(metrics, log=logger)
        warn_ingest_guardrails(metrics, log=logger)

        logger.info(f"[add_text] Chunk done: episode={episode_id}")
        return episode_id

    def add_text_batch(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> List[str]:
        """Batch-add text chunks with progress reporting."""
        episode_ids = []
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            if not chunk or not chunk.strip():
                continue
            episode_id = self.add_text(graph_id, chunk)
            episode_ids.append(episode_id)

            if progress_callback:
                progress = (i + 1) / total
                progress_callback(progress)

            logger.info(f"Processed chunk {i + 1}/{total}")

        return episode_ids

    def wait_for_processing(
        self,
        episode_ids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600,
    ) -> None:
        """No-op — processing is synchronous in Neo4j."""
        if progress_callback:
            progress_callback(1.0)

    # ----------------------------------------------------------------
    # Read nodes
    # ----------------------------------------------------------------

    def get_all_nodes(self, graph_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n, labels(n) AS labels
                ORDER BY n.created_at DESC
                LIMIT $limit
                """,
                gid=graph_id,
                limit=limit,
            )
            return [self._node_to_dict(record["n"], record["labels"]) for record in result]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_node(self, uuid: str) -> Optional[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                "MATCH (n:Entity {uuid: $uuid}) RETURN n, labels(n) AS labels",
                uuid=uuid,
            )
            record = result.single()
            if record:
                return self._node_to_dict(record["n"], record["labels"])
            return None

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_node_edges(
        self,
        node_uuid: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        """O(1) Cypher — NOT full scan + filter like the old Zep code."""
        tw, tparams = relation_temporal_where(as_of, include_invalid_relations)

        def _read(tx):
            result = tx.run(
                f"""
                MATCH (n:Entity {{uuid: $uuid}})-[r:RELATION]-(m:Entity)
                WHERE 1=1 {tw}
                RETURN r, startNode(r).uuid AS src_uuid, endNode(r).uuid AS tgt_uuid
                """,
                uuid=node_uuid,
                **tparams,
            )
            return [
                self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                for record in result
            ]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_nodes_by_label(self, graph_id: str, label: str) -> List[Dict[str, Any]]:
        def _read(tx):
            # Dynamic label in query (safe — label comes from ontology, not user input)
            query = f"""
                MATCH (n:Entity:`{label}` {{graph_id: $gid}})
                RETURN n, labels(n) AS labels
            """
            result = tx.run(query, gid=graph_id)
            return [self._node_to_dict(record["n"], record["labels"]) for record in result]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Read edges
    # ----------------------------------------------------------------

    def get_all_edges(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> List[Dict[str, Any]]:
        tw, tparams = relation_temporal_where(as_of, include_invalid_relations)

        def _read(tx):
            result = tx.run(
                f"""
                MATCH (src:Entity)-[r:RELATION {{graph_id: $gid}}]->(tgt:Entity)
                WHERE 1=1 {tw}
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid
                ORDER BY r.created_at DESC
                """,
                gid=graph_id,
                **tparams,
            )
            return [
                self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                for record in result
            ]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Search
    # ----------------------------------------------------------------

    def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ):
        """
        Hybrid search — returns results matching the scope.

        Returns a dict with 'edges' and/or 'nodes' lists
        (callers like zep_tools will wrap into SearchResult).
        """
        eff_as_of = as_of
        if eff_as_of is None and Config.GRAPH_TEMPORAL_ENABLED:
            eff_as_of = Config.effective_graph_query_as_of()

        ttl = Config.GRAPH_SEARCH_RESULT_CACHE_TTL_SEC
        cache_key = None
        if ttl > 0:
            sig = (
                f"{graph_id}\x1f{query}\x1f{limit}\x1f{scope}\x1f{eff_as_of}\x1f"
                f"{include_invalid_relations}\x1f{Config.SEARCH_VECTOR_WEIGHT}\x1f"
                f"{Config.SEARCH_KEYWORD_WEIGHT}\x1f{Config.GRAPH_SEARCH_EXPAND_HOPS}\x1f"
                f"{Config.GRAPH_SEARCH_EXPAND_EXTRA}\x1f{Config.GRAPH_SEARCH_RERANK_TOP_M}\x1f"
                f"{','.join(Config.GRAPH_SEARCH_EXPAND_ENTITY_TYPES)}\x1f"
                f"{Config.GRAPH_TEMPORAL_ENABLED}\x1f{Config.GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY}"
            ).encode()
            cache_key = hashlib.sha256(sig).hexdigest()
            now = time.time()
            ent = self._search_result_cache.get(cache_key)
            if ent and now - ent[0] < ttl:
                return copy.deepcopy(ent[1])

        result = {"edges": [], "nodes": [], "query": query}

        with self._driver.session() as session:
            if scope == "nodes":
                result["nodes"] = self._search.search_nodes(
                    session, graph_id, query, limit
                )
            elif scope == "edges":
                result["edges"] = self._search.search_edges(
                    session,
                    graph_id,
                    query,
                    limit,
                    as_of=eff_as_of,
                    include_invalid_relations=include_invalid_relations,
                )
            else:
                result["nodes"] = self._search.search_nodes(
                    session, graph_id, query, limit
                )
                node_seeds = [n["uuid"] for n in result["nodes"] if n.get("uuid")]
                result["edges"] = self._search.search_edges(
                    session,
                    graph_id,
                    query,
                    limit,
                    as_of=eff_as_of,
                    include_invalid_relations=include_invalid_relations,
                    extra_seed_node_uuids=node_seeds,
                )

        if ttl > 0 and cache_key is not None:
            now = time.time()
            self._search_result_cache[cache_key] = (now, copy.deepcopy(result))
            if len(self._search_result_cache) > 160:
                for k, (ts, _) in sorted(
                    self._search_result_cache.items(), key=lambda kv: kv[1][0]
                )[:80]:
                    self._search_result_cache.pop(k, None)

        return result

    # ----------------------------------------------------------------
    # Graph info
    # ----------------------------------------------------------------

    def get_graph_info(self, graph_id: str) -> Dict[str, Any]:
        tw, tparams = relation_temporal_where(None, include_invalid=False)

        def _read(tx):
            # Count nodes
            node_result = tx.run(
                "MATCH (n:Entity {graph_id: $gid}) RETURN count(n) AS cnt",
                gid=graph_id,
            )
            node_count = node_result.single()["cnt"]

            # Count edges (respect temporal active-only defaults)
            edge_result = tx.run(
                f"""
                MATCH ()-[r:RELATION {{graph_id: $gid}}]->()
                WHERE 1=1 {tw}
                RETURN count(r) AS cnt
                """,
                gid=graph_id,
                **tparams,
            )
            edge_count = edge_result.single()["cnt"]

            # Distinct entity types
            label_result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                UNWIND labels(n) AS lbl
                WITH lbl WHERE lbl <> 'Entity'
                RETURN DISTINCT lbl
                """,
                gid=graph_id,
            )
            entity_types = [record["lbl"] for record in label_result]

            return {
                "graph_id": graph_id,
                "node_count": node_count,
                "edge_count": edge_count,
                "entity_types": entity_types,
            }

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_graph_data(
        self,
        graph_id: str,
        as_of: Optional[str] = None,
        include_invalid_relations: bool = False,
    ) -> Dict[str, Any]:
        """
        Full graph dump with enriched edge format (for frontend).
        Includes derived fields: fact_type, source_node_name, target_node_name.
        """
        eff_as_of = as_of
        if eff_as_of is None and Config.GRAPH_TEMPORAL_ENABLED:
            eff_as_of = Config.effective_graph_query_as_of()
        tw, tparams = relation_temporal_where(eff_as_of, include_invalid_relations)

        def _read(tx):
            # Get all nodes
            node_result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n, labels(n) AS labels
                """,
                gid=graph_id,
            )
            nodes = []
            node_map: Dict[str, str] = {}  # uuid -> name
            for record in node_result:
                nd = self._node_to_dict(record["n"], record["labels"])
                nodes.append(nd)
                node_map[nd["uuid"]] = nd["name"]

            # Get all edges with source/target node names (JOIN)
            edge_result = tx.run(
                f"""
                MATCH (src:Entity)-[r:RELATION {{graph_id: $gid}}]->(tgt:Entity)
                WHERE 1=1 {tw}
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """,
                gid=graph_id,
                **tparams,
            )
            edges = []
            for record in edge_result:
                ed = self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                # Enriched fields for frontend
                ed["fact_type"] = ed["name"]
                ed["source_node_name"] = record["src_name"] or ""
                ed["target_node_name"] = record["tgt_name"] or ""
                # Legacy alias
                ed["episodes"] = ed.get("episode_ids", [])
                edges.append(ed)

            return {
                "graph_id": graph_id,
                "nodes": nodes,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Dict conversion helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _node_to_dict(node, labels: List[str]) -> Dict[str, Any]:
        """Convert Neo4j node to the standard node dict format."""
        props = dict(node)
        attrs_json = props.pop("attributes_json", "{}")
        try:
            attributes = json.loads(attrs_json) if attrs_json else {}
        except (json.JSONDecodeError, TypeError):
            attributes = {}

        # Remove internal fields from dict
        props.pop("embedding", None)
        props.pop("name_lower", None)

        return {
            "uuid": props.get("uuid", ""),
            "name": props.get("name", ""),
            "labels": [l for l in labels if l != "Entity"] if labels else [],
            "summary": props.get("summary", ""),
            "attributes": attributes,
            "created_at": props.get("created_at"),
        }

    @staticmethod
    def _edge_to_dict(rel, source_uuid: str, target_uuid: str) -> Dict[str, Any]:
        """Convert Neo4j relationship to the standard edge dict format."""
        props = dict(rel)
        attrs_json = props.pop("attributes_json", "{}")
        try:
            attributes = json.loads(attrs_json) if attrs_json else {}
        except (json.JSONDecodeError, TypeError):
            attributes = {}

        # Remove internal fields
        props.pop("fact_embedding", None)

        episode_ids = props.get("episode_ids", [])
        if episode_ids and not isinstance(episode_ids, list):
            episode_ids = [str(episode_ids)]

        return {
            "uuid": props.get("uuid", ""),
            "name": props.get("name", ""),
            "fact": props.get("fact", ""),
            "source_node_uuid": source_uuid,
            "target_node_uuid": target_uuid,
            "attributes": attributes,
            "created_at": props.get("created_at"),
            "valid_at": props.get("valid_at"),
            "invalid_at": props.get("invalid_at"),
            "expired_at": props.get("expired_at"),
            "episode_ids": episode_ids,
        }
