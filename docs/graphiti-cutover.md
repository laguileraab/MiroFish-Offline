# Graphiti cutover — MiroFish-Offline

This document is the **integration plan** and **runbook** for the Graphiti-backed memory path: **[Graphiti](https://github.com/getzep/graphiti)** (`graphiti-core`) as the **single** writer and query engine for episodic ingest → temporal graph → hybrid retrieval (when `GRAPH_BACKEND=graphiti`).

**Related:** Phase 15 in [`ZEP_STYLE_MEMORY_ROADMAP.md`](ZEP_STYLE_MEMORY_ROADMAP.md). **Baseline before cutover:** Git branch `checkpoint/pre-graphiti`. **Implementation branch:** `feature/graphiti-integration`.

---

## Implementation status (living checklist)

| Area | Status | Notes |
|------|--------|--------|
| `GraphitiStorage` implements `GraphStorage` | **Done** | `backend/app/storage/graphiti_storage.py` |
| Flask `create_app` selects backend | **Done** | `GRAPH_BACKEND=neo4j` vs `graphiti` |
| Ingest via `add_episode` | **Done** | Text chunks; prior context from DB |
| Hybrid search via `search_` | **Done** | `COMBINED_HYBRID_SEARCH_RRF` |
| Reads via Graphiti models | **Done** | `EntityNode`, `EntityEdge`, `EpisodicNode`; partition delete via `clear_data` |
| `:Graph` registry (name, description, ontology JSON) | **Done** | MiroFish-only; not Graphiti schema |
| JSON ontology → `entity_types` on ingest | **Done** | `graphiti_ontology.py` → empty Pydantic models + `__doc__`; cache invalidated on `set_ontology` |
| JSON `relation_types` → Graphiti `edge_types` | **TODO** | Needs `edge_types` / `edge_type_map` mapping; verify prompts |
| `add_episode_bulk` for job throughput | **TODO** | Optional vs sequential `add_text` |
| Golden-set / snapshot parity for Graphiti | **TODO** | Extend `scripts/snapshot_golden_counts.py` or add Graphiti-specific counts |
| Alternate Graphiti backend (FalkorDB / Kuzu) | **TODO** | New driver wiring + env; not started |

---

## Principle: one memory engine, no dual-write

- **Do not** run Graphiti and the custom `Neo4jStorage` pipeline on the **same** logical graph (no dual-write of two merge/temporal models).
- **Do** keep MiroFish orchestration: Flask, projects, chunking, job queue, simulations, reports, golden-set scripts, admin metrics — all calling **`GraphStorage`**.
- **Expect** **re-ingest** on a clean store: legacy `RELATION` / `:Episode` layout ≠ Graphiti’s `RELATES_TO` / `:Episodic` layout.

### Graphiti vs “why Neo4j is still in .env”

**Graphiti is not a database.** It is the temporal graph *engine*. With the default stack, **`graphiti-core` uses Neo4j as its persistence backend** (or FalkorDB, Kuzu, Neptune per Graphiti docs). So `NEO4J_URI` / `NEO4J_DATABASE` mean **where Graphiti stores data**, not a second competing implementation. To drop Neo4j as a product, swap in another Graphiti driver — you still need *some* graph store.

**Async:** Graphiti’s API is `async`. Flask stays sync; `GraphitiStorage` uses a **background event loop** + `run_coroutine_threadsafe` + a lock so the async Neo4j driver stays on one loop. ASGI (e.g. FastAPI) could `await` Graphiti directly later.

**MiroFish-only data:** Graphiti does not ship project registry rows or the ontology JSON blob the UI expects. The adapter keeps **`:Graph`** nodes; everything else goes through Graphiti APIs.

---

## Prescribed ontology (MiroFish JSON → Graphiti)

MiroFish stores ontology as JSON (`entity_types[]`, `relation_types[]`) on `:Graph`, compatible with [`docs/golden-set/ontology-min.json`](golden-set/ontology-min.json).

**Entity types:** For each `entity_types[].name` / `description`, we build an **empty** Pydantic `BaseModel` subclass whose **`__doc__`** carries the description. That becomes Graphiti’s `entity_types=` map on `add_episode`. Graphiti forbids subclass fields that collide with `EntityNode`’s reserved names — empty models satisfy that.

**Relation types:** Not yet passed as Graphiti `edge_types` / `edge_type_map`; extraction still uses Graphiti defaults for edges until we map `relation_types` safely.

**Cache:** Prescribed models are cached per `graph_id` in memory; **`set_ontology`** and **`delete_graph`** invalidate the cache.

---

## Operational runbook

### Required / common environment variables

| Variable | Role |
|----------|------|
| `GRAPH_BACKEND` | `neo4j` (custom pipeline) or `graphiti` |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | Graphiti’s Neo4j backend (when using Neo4j driver) |
| `NEO4J_DATABASE` | Logical DB name (default `neo4j`) |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME` | OpenAI-compatible **chat** API for Graphiti extraction (Ollama/LM Studio often need `LLM_API_KEY=ollama`) |
| `NER_MAX_OUTPUT_TOKENS` | Passed into Graphiti’s `OpenAIGenericClient` as generation budget |
| `EMBEDDING_MODEL`, `EMBEDDING_BASE_URL` | OpenAI-compatible **embeddings** (`/v1` appended automatically when missing) |
| `LLM_INGEST_MAX_CONCURRENT` | When `>0`, feeds `Graphiti(..., max_coroutines=…)` unless `GRAPHITI_MAX_COROUTINES` is set |
| `GRAPHITI_MAX_COROUTINES` | Optional override for Graphiti internal concurrency |
| `SEMAPHORE_LIMIT` | Graphiti’s own env (see upstream README); coordinate with ingest concurrency |

See [`.env.example`](../.env.example) for the full list shared with the `neo4j` backend.

### Cutover procedure (staging → prod)

1. **New Neo4j DB or wipe** data that used the custom schema if you previously used `GRAPH_BACKEND=neo4j` on the same database (mixed schemas are unsupported).
2. Set `GRAPH_BACKEND=graphiti`, restart backend, run **`build_indices`** once (happens on `GraphitiStorage` init).
3. **Re-create graphs** and **re-ingest** documents (no automatic migration from `RELATION` to `RELATES_TO`).
4. Smoke: create graph → set ontology → upload / build → `search_graph` from UI or API.
5. Watch logs for JSON / schema failures from small local LLMs; prefer models that support **structured JSON** for extraction.

### Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Ingest errors, malformed JSON in logs | Local LLM too small or no `json_schema` / `json_object` support |
| Empty search results | Wrong `group_id` / `graph_id`, or no ingest yet |
| `Graph storage initialization failed` | Neo4j down, wrong URI, or `graphiti-core` not installed |
| Slow ingest | Lower `GRAPHITI_MAX_COROUTINES` / `SEMAPHORE_LIMIT` if rate-limited; or raise if the provider allows |

---

## Upstream Zep Cloud SDK → Graphiti (conceptual)

| Zep Cloud–style concern | Graphiti direction |
|-------------------------|-------------------|
| Create graph / project scope | `group_id` = MiroFish `graph_id`; Neo4j = one DB + property partition |
| Set ontology | `:Graph` JSON + prescribed `entity_types` on `add_episode` (relations TODO) |
| Add episodes | `add_episode` (text / JSON); optional bulk API |
| Hybrid search | `search_` + search config recipes |
| Temporal / invalidation | Built into Graphiti edges (`valid_at` / `invalid_at`) |

---

## MiroFish `GraphStorage` → Graphiti adapter (reference)

1. **Maps `graph_id`** → Graphiti **`group_id`**.
2. **Registry** → `set_ontology` / `get_ontology` on **`:Graph`**.
3. **Ingest** → `add_text` / `add_text_batch` → `add_episode` + optional `entity_types` from ontology.
4. **Search** → `search_` → normalized `edges` / `nodes` for `GraphToolsService`.
5. **Reads** → `EntityNode` / `EntityEdge` / `EpisodicNode`; **delete partition** → `clear_data`.

**Injection:** `create_app` — `GRAPH_BACKEND=graphiti` → `GraphitiStorage`.

---

## Environment and dependencies

- **Package:** `graphiti-core` pinned in `backend/requirements.txt` / `pyproject.toml`.
- **Neo4j:** Graphiti README cites **Neo4j 5.26**; validate against your server before production.
- **Concurrency:** Align Graphiti `SEMAPHORE_LIMIT` with `LLM_INGEST_MAX_CONCURRENT` / `GRAPHITI_MAX_COROUTINES`.

---

## What stays in MiroFish (complementary)

| Area | Role |
|------|------|
| `docs/golden-set/`, `scripts/snapshot_golden_counts.py` | Regression signals (extend for Graphiti as needed) |
| `ingest_metrics`, `ingest_counters`, admin ingest-stats | Ops visibility (`Episodic` counts when on Graphiti) |
| `TaskManager`, chunk jobs | Backpressure / UX around ingest |
| Simulations / `graph_memory_updater` | Call `GraphStorage` only |

---

## Rollout sequence

1. **Vertical slice:** one graph, ingest + search + golden smoke.
2. **Staging:** full API parity; fix `edge_types` / bulk if needed.
3. **Production flip:** re-ingest; monitor extraction quality.
4. **Deprecate** custom NER/search **for graph memory** when parity is proven.

---

## Phase 15 task alignment

| Task | Status / intent |
|------|----------------|
| **TASK-049** | **In progress** — `GraphitiStorage` + prescribed entity types; edge types + golden automation remain. |
| **TASK-050** | **In progress** — this document + `.env.example`; expand with on-call notes as you learn. |

---

*Last updated: extended runbook, implementation checklist, prescribed ontology wiring (`graphiti_ontology.py`).*
