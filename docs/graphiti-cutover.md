# Graphiti cutover — MiroFish-Offline

This document is the **integration plan** for replacing the custom **`Neo4jStorage` / `NERExtractor` / `SearchService`** pipeline with **[Graphiti](https://github.com/getzep/graphiti)** (`graphiti-core`) as the **single** writer and query engine for episodic ingest → temporal graph → hybrid retrieval.

**Related:** Phase 15 in [`ZEP_STYLE_MEMORY_ROADMAP.md`](ZEP_STYLE_MEMORY_ROADMAP.md). **Baseline before cutover:** Git branch `checkpoint/pre-graphiti`.

---

## Principle: one memory engine, no dual-write

- **Do not** run Graphiti and the current custom ingest/search in parallel on the same logical graph (no dual-write “hybrid” of two merge/temporal models).
- **Do** keep MiroFish orchestration: Flask, projects, chunking policy, job queue, simulations, reports, golden-set scripts, admin metrics — **calling** Graphiti for graph lifecycle, episode ingest, and search.
- **Expect** **re-ingest** on a clean store: custom `Entity` / `Episode` / `RELATION` data is not Graphiti’s schema (`Entity` / `Episodic` / `RELATES_TO`, etc.). With Neo4j, Graphiti 0.28 partitions graphs by **`group_id`** on nodes/edges inside the configured **`NEO4J_DATABASE`** (default `neo4j`); MiroFish `graph_id` is passed as that `group_id`. Optional separate Neo4j databases are a deployment choice, not required by the current adapter.

### Graphiti vs “why Neo4j is still in .env”

**Graphiti is not a database.** It is the temporal graph *engine* (extract, merge, validity, hybrid search). With the default stack, **`graphiti-core` uses Neo4j as its persistence backend** — the same way it can target FalkorDB, Kuzu, or Neptune. So `NEO4J_URI` / `NEO4J_DATABASE` mean “where Graphiti stores its graph,” not “a second competing graph implementation.” To eliminate Neo4j entirely you would run Graphiti on another supported backend and swap the driver in app wiring (not done in this repo yet).

**Async:** Graphiti’s public API is `async`. The Flask app stays synchronous; `GraphitiStorage` runs coroutines on one **background event loop** (thread + `run_coroutine_threadsafe`) so the async Neo4j driver stays on a stable loop. You do **not** need async routes unless you migrate to ASGI and can `await` Graphiti directly.

**MiroFish-only data:** Graphiti does not define project registry rows or the JSON ontology blob the UI/builder expect. The adapter keeps minimal **`:Graph`** nodes for name, description, and `ontology_json` — everything else goes through Graphiti (`EntityNode`, `EntityEdge`, `EpisodicNode`, `clear_data`, `search_`, etc.).

---

## Upstream Zep Cloud SDK → Graphiti (conceptual)

MiroFish-Offline already replaced Zep with `GraphStorage`. For teams comparing **Zep Cloud SDK** calls to **Graphiti**, the mapping is:

| Zep Cloud–style concern | Graphiti direction |
|-------------------------|-------------------|
| Create graph / project scope | Graphiti driver + `Graphiti` instance; use **group_id** (or equivalent) to isolate graphs per MiroFish project |
| Set ontology (entity/relation types) | **Prescribed ontology** via Pydantic models (Graphiti docs / examples) |
| Add episodes (async batch) | **`add_episode`** (text or structured JSON); concurrency via **`SEMAPHORE_LIMIT`** env (Graphiti README) |
| Poll processing | Graphiti ingest is **pipeline-oriented**; surface completion via your job layer (existing `TaskManager` / chunk jobs) |
| Hybrid graph search | Graphiti **hybrid search** (semantic + keyword + graph traversal); reranking options in quickstart |
| Temporal / invalidation | Graphiti **validity windows** and fact supersession (core product design) |
| Entity summaries | Graphiti **entity summaries** that evolve over time |

Exact method names and constructors follow the installed **`graphiti-core`** version; pin a version in `requirements.txt` and link to that release’s quickstart.

---

## MiroFish `GraphStorage` → Graphiti adapter

Today, all graph I/O goes through **`GraphStorage`** (`backend/app/storage/graph_storage.py`). Implement **`GraphitiStorage(GraphStorage)`** (name TBD) that:

1. **Maps `graph_id`** to Graphiti **`group_id`** (Neo4j provider: same logical database, property-based partition).
2. **Implements** `set_ontology` / `get_ontology` on **`:Graph`** registry nodes. Mapping JSON ontology → Graphiti **prescribed Pydantic** types at ingest is a follow-up (Graphiti supports `entity_types=` on `add_episode`; not yet wired).
3. **Implements** `add_text` / `add_text_batch` by enqueueing **episodes** (chunk text + provenance metadata). `wait_for_processing` can remain a compatibility no-op if ingest is synchronous from the caller’s perspective, or tie to task completion.
4. **Implements** `search` by delegating to Graphiti hybrid search and **normalizing** results into the shapes `GraphToolsService` / `oasis_profile_generator` expect (`edges` / `nodes` lists, fields used in prompts).
5. **Implements** read APIs (`get_all_nodes`, `get_node_edges`, `get_graph_data`, …) either via **Graphiti query helpers** or **narrow Cypher** against Graphiti’s schema — avoid reintroducing a second extraction path.

**Injection:** `create_app` selects `Neo4jStorage` vs `GraphitiStorage` from config (e.g. `GRAPH_BACKEND=graphiti|neo4j_custom`).

---

## Environment and dependencies

- **Package:** `graphiti-core` ([PyPI](https://pypi.org/project/graphiti-core/)), version **pinned** after the first green vertical slice.
- **Neo4j:** Graphiti README currently cites **Neo4j 5.26** (and other backends). MiroFish lists `neo4j>=5.15.0`; **validate** driver and server versions against Graphiti’s supported matrix before production cutover.
- **LLM / embeddings:** Graphiti defaults to OpenAI; supports other providers and **OpenAI-compatible** endpoints. Small local models may fail **structured output** requirements — align with Phase 9 guidance (extract model quality).
- **Concurrency:** Graphiti uses **`SEMAPHORE_LIMIT`** for ingest; coordinate with MiroFish **`LLM_INGEST_MAX_CONCURRENT`** so total parallel LLM calls stay within provider limits.

---

## What stays in MiroFish (complementary, not duplicated)

| Area | Role |
|------|------|
| **`docs/golden-set/`**, **`scripts/snapshot_golden_counts.py`** | Regression signals after cutover |
| **`ingest_metrics`**, **`ingest_counters`**, **`GET .../admin/ingest-stats`** | Ops visibility (may wrap Graphiti stages) |
| **`TaskManager`**, chunk ingest jobs | UX and backpressure around Graphiti ingest |
| **`LLMClient`**, Ollama / LM Studio | Shared HTTP client patterns; Graphiti may use its own client config |
| **Simulations / `graph_memory_updater`** | Still call **`GraphStorage`** only; implementation becomes Graphiti-backed |

**Avoid** reimplementing merge adjudication, temporal invalidation, or hybrid ranking **in parallel** with Graphiti’s built-in behavior — that was the main cost of the pre-Graphiti stack.

---

## Rollout sequence (suggested)

1. **Vertical slice:** one project, `add_text` → Graphiti episode → hybrid `search` → assert golden-set or smoke queries.
2. **Normalize API:** finish `GraphStorage` methods used by `graph_builder`, `graph_tools`, `entity_reader`, `graph_memory_updater`, `oasis_profile_generator`.
3. **Flip config** in staging; burn down adapter gaps; **re-ingest** from documents.
4. **Deprecate** direct use of `NERExtractor` / `SearchService` / custom `neo4j_schema` **for graph memory** (keep or delete after parity tests).

---

## Phase 15 task alignment

| Task | Updated intent |
|------|----------------|
| **TASK-049** | Implement Graphiti-backed `GraphStorage` and prove ingest + search on golden/smoke data (not only a throwaway spike). |
| **TASK-050** | Document env vars, Neo4j version, ontology mapping, and operational runbook; remove dual-write options from consideration. |

---

*Last updated: cutover planning doc created alongside branch `feature/graphiti-integration`.*
