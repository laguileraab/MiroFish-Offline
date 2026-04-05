# MiroFish-Offline — Zep / Graphiti–Style Memory Roadmap

## Overview

This document is the **post-migration memory and quality roadmap**. It builds on the work tracked in [`docs/progress.md`](progress.md).

**Scope:** Strengthen **knowledge-graph ingestion**, **entity/relation quality**, **retrieval**, **temporal memory**, and **operations** using patterns from **Zep** and **Graphiti**, while **keeping Neo4j** (or another Graphiti-supported backend) as the graph store. **Phase 15** adopts **`graphiti-core`** as the single memory engine for ingest and retrieval; see [`docs/graphiti-cutover.md`](graphiti-cutover.md). Earlier phases (8–14) implemented a custom Neo4j pipeline preserved on branch **`checkpoint/pre-graphiti`**.

**Canonical migration tracker:** [`docs/progress.md`](progress.md) — Phases **0–6** (TASK-001–018) are **COMPLETE**; **Phase 7** (TASK-019 publish) remains **TODO** there.

---

## Phase index (single source of truth)

| Phase | Focus | Task range | Status |
|------:|-------|------------|--------|
| **0** | Scaffolding — `LLMClient`, `NERExtractor`, `EmbeddingService` | TASK-001–003 | **Complete** — see `progress.md` |
| **1** | Storage — `GraphStorage`, `Neo4jStorage`, hybrid search | TASK-004–007 | **Complete** |
| **2** | Service rewrite — `graph_builder`, `entity_reader`, `graph_tools`, `report_agent`, `graph_memory_updater`, Zep → GraphStorage | TASK-008–014b | **Complete** |
| **3** | Flask DI — `Neo4jStorage` in `create_app`, API wiring | TASK-015 | **Complete** |
| **4** | End-to-end import / app factory smoke | TASK-016 | **Complete** |
| **5** | CAMEL-AI + Ollama compatibility | TASK-017 | **Complete** |
| **6** | Cleanup — delete `zep_*.py`, docstring fixes | TASK-018 | **Complete** |
| **7** | Publish — rename branding, AGPL-3.0, GitHub | TASK-019 | **TODO** (`progress.md`) |
| **8** | Baseline metrics, ingest logging, golden set, config runbook | TASK-020–023 | **Done** (v3.1 — see § Phase 8) |
| **9** | Inference stack — dedicated extract model, tokens, context, JSON repair | TASK-024–027 | **Done** (v3.2 — see § Phase 9) |
| **10** | Extraction pipeline — two-pass, linking, chunking, provenance | TASK-028–033 | **Done** (v3.3 — see § Phase 10) |
| **11** | Graph maintenance — merge, dedupe, summarization | TASK-034–038 | **Done** (v3.4 — see § Phase 11) |
| **12** | Temporal memory — validity, as-of retrieval | TASK-039–041 | **Done** (v3.5 — see § Phase 12) |
| **13** | Retrieval — hybrid tuning, expansion, optional rerank/cache | TASK-042–045 | **Done** (v3.6 — see § Phase 13) |
| **14** | Throughput — job queue, backpressure, admin metrics | TASK-046–048 | **Done** (v3.7 — see § Phase 14) |
| **15** | Graphiti integration — `graphiti-core` as `GraphStorage` | TASK-049–050 | **In progress** — [`graphiti-cutover.md`](graphiti-cutover.md) |

**Memory execution order (summary):** Phase 8 → 9 → (10A+10C) → measure → 10B → 11 → 13 → 12 → 14 → optional 15.

**Prerequisites for memory work:** Migration **Phases 0–6** done (per `progress.md`). **Phase 7** (publish) is independent of Phases 8+; complete it when ready for public repo hygiene, not blocking local memory improvements.

---

## PHASE 7 — Publish (TODO — tracked in `progress.md`)

**Goal:** Repo and licensing ready for public GitHub.

- **TASK-019**: Rename to MiroFish-Offline, add AGPL-3.0 license, publish to GitHub

*Detail, file lists, and completed migration tasks (Phases 0–6) remain in [`docs/progress.md`](progress.md). Do not duplicate TASK-001–018 here; update that file when migration-adjacent work lands.*

---

## PHASE 8 — Baseline, metrics, and guardrails (COMPLETE)

**Goal:** Measure whether each change improves density, reliability, or latency.

- **TASK-020**: **Done** — KPI field contract in `backend/app/utils/ingest_metrics.py` (`INGEST_METRIC_KEYS`, `edge_to_node_ratio`, `build_ingest_metrics`). Token usage and `finish_reason` captured on `LLMClient` after each completion (`last_usage`, `last_finish_reason`).
- **TASK-021**: **Done** — `Neo4jStorage.add_text` emits one JSON line per chunk: `ingest_metrics {...}`; guardrail warnings `ingest_guardrail` for truncation, sparse relations, NER failure. Skipped relations (missing endpoints) counted as `relations_skipped_missing_endpoint`.
- **TASK-022**: **Done** — `docs/golden-set/` (3 sample texts + `ontology-min.json` + `README.md`); `scripts/snapshot_golden_counts.py` for entity/relation/episode counts.
- **TASK-023**: **Done** — `.env.example` expanded (chunk env, `OLLAMA_NUM_CTX`, `LLM_PROVIDER`); `Config.DEFAULT_CHUNK_SIZE` / `DEFAULT_CHUNK_OVERLAP` read `GRAPH_CHUNK_SIZE` / `GRAPH_CHUNK_OVERLAP`.

---

## PHASE 9 — Inference stack (COMPLETE)

**Goal:** Remove the main ceiling on extraction quality and JSON completeness.

- **TASK-024**: **Done** — `LLM_EXTRACT_BASE_URL`, `LLM_EXTRACT_MODEL_NAME`, `LLM_EXTRACT_API_KEY`, `LLM_EXTRACT_HTTP_TIMEOUT_SEC`, optional `OLLAMA_EXTRACT_NUM_CTX` on `LLMClient(..., num_ctx=…)`; `NERExtractor` uses `_default_llm_for_extraction()` when no client is injected.
- **TASK-025**: **Done** — tuning guide [`docs/golden-set/NER_MAX_OUTPUT_TOKENS.md`](golden-set/NER_MAX_OUTPUT_TOKENS.md) (ranges + procedure with golden set + `ingest_metrics`).
- **TASK-026**: **Done** — `Config.ingest_context_warnings()`; merged into `Config.validate()`; logged at app startup (`create_app`). LM Studio: same heuristic applies to whatever context you set server-side (documented in `.env.example`).
- **TASK-027**: **Done** — `LLMClient.chat_json` → `_parse_json_with_repair`: trailing-comma fix + string-aware `{…}` extraction before failing. Still uses `json_object` when the server accepts it (existing `chat()` behavior).

---

## PHASE 10 — Extraction pipeline (COMPLETE)

**Goal:** Split work so relations are not starved; improve cross-chunk linking and provenance.

### 10A — Two-pass extraction

- **TASK-028**: **Done** — Pass 1 prompts in `ner_extractor.py` (`_PASS1_*`); JSON `{"entities":[...]}` only; cleaned via `_validate_and_clean`.
- **TASK-029**: **Done** — Pass 2 `_PASS2_*` with frozen entity lines + optional graph candidates; relations merged with pass-1 entities. Enable with **`NER_TWO_PASS=true`**. Ingest logs `ner_two_pass` in `ingest_metrics`.

### 10B — Graph-aware linking

- **TASK-030**: **Done** — `Neo4jStorage._graph_link_candidates()` uses `SearchService.search_nodes` on chunk text (truncated); **`NER_LINK_TOP_K`** (default 12, `0` = off). Candidates go to pass-2 prompt when **`NER_TWO_PASS`**; otherwise appended to the single-pass user message.

### 10C — Chunking

- **TASK-031**: **Done** — **`GRAPH_CHUNK_PROFILE`** = `small` (400/40), `medium` (500/50), `large` (800/80) when **`GRAPH_CHUNK_SIZE`** is unset (`config.py`).
- **TASK-032**: **Done** — **`GRAPH_CHUNK_PREFER_PARAGRAPH=true`** → `split_text_into_chunks(..., prefer_paragraph_boundary=True)` via `TextProcessor.split_text`.

### 10D — Episode provenance

- **TASK-033**: **Done** — After entity upserts, `MERGE (ep:Episode)-[:MENTIONS]->(n:Entity)` for all entity UUIDs written in the chunk (`neo4j_storage.py`); schema note in `neo4j_schema.py`.

---

## PHASE 11 — Graph maintenance (merge graph) (COMPLETE)

**Goal:** Fewer duplicates, canonical entities, controlled edge growth.

- **TASK-034**: **Done** — `storage/entity_normalize.py`: NFKC, whitespace collapse, optional **`ENTITY_ALIAS_JSON_PATH`** (see `docs/golden-set/entity-aliases.example.json`). Applied on ingest before embedding.
- **TASK-035**: **Done** — **`GRAPH_MERGE_VECTOR_ENABLED`**: `SearchService.search_nodes_by_vector` + reuse canonical **`uuid`** when cosine score ≥ **`GRAPH_MERGE_VECTOR_THRESHOLD`** (no separate `SAME_AS` node).
- **TASK-036**: **Done** — **`GRAPH_MERGE_LLM_ADJUDICATE`**: for scores between **`GRAPH_MERGE_VECTOR_AMBIG_LOW`** and threshold, `merge_adjudicator.llm_same_real_world_entity` (NER LLM client).
- **TASK-037**: **Done** — **`RELATION_DEDUPE_ENABLED`** (default on): `fact_normalized` + match existing `RELATION`; append **`episode_ids`**; legacy edges matched by raw `fact` if `fact_normalized` is null.
- **TASK-038**: **Done** — `services/graph_maintenance.py`: **`refresh_entity_summary_llm`**; ingest runs up to **`ENTITY_SUMMARY_MAX_PER_CHUNK`** refreshes per chunk (default **0**).

---

## PHASE 12 — Temporal memory (COMPLETE)

**Goal:** Facts can update without deleting history.

- **TASK-039**: **Done** — On ingest, when **`GRAPH_TEMPORAL_ENABLED`** and **`GRAPH_TEMPORAL_SUPERSEDE_SAME_TRIPLE`**, creating a new `RELATION` with the same `(src,tgt,type)` but a different `fact_normalized` sets **`invalid_at`** on previously active edges; new edge gets **`valid_at`** (episode time or optional `valid_from`).
- **TASK-040**: **Done** — `ner_extractor` passes through optional relation keys **`valid_from`** (ISO-8601) and **`supersedes_relation_uuid`** (sets that edge’s **`invalid_at`** on ingest). Episode **`created_at`** remains provenance on `Episode`.
- **TASK-041**: **Done** — When temporal mode is on and **`GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY`**, hybrid edge search (`search_service`), **`get_all_edges`**, **`get_node_edges`**, **`get_graph_data`**, **`get_graph_info`** edge counts, and **`fetch_entity_fact_bullets`** filter active edges; optional **`GRAPH_QUERY_AS_OF_ISO`** and per-call **`as_of`** / **`include_invalid_relations`** on `Neo4jStorage.search` and `GraphToolsService` (`search_graph`, `get_all_edges`, `get_node_edges`).

---

## PHASE 13 — Retrieval and agent memory (COMPLETE)

**Goal:** Richer context for `GraphToolsService`, `ReportAgent`, agent chat.

- **TASK-042**: **Done** — `SEARCH_VECTOR_WEIGHT` / `SEARCH_KEYWORD_WEIGHT` in `config.py` (defaults 0.7 / 0.3); `SearchService._merge_results` uses them; `Config.validate()` requires sum 1.0 ±0.02.
- **TASK-043**: **Done** — `GRAPH_SEARCH_EXPAND_HOPS` (0–2), `GRAPH_SEARCH_EXPAND_EXTRA`, `GRAPH_SEARCH_EXPAND_MAX_PER_SEED`, optional `GRAPH_SEARCH_EXPAND_ENTITY_TYPES`; BFS neighbor `RELATION`s in `search_service.py` with shared `relation_temporal_filters.py`. `scope=both` runs node search first, then edge search with `extra_seed_node_uuids` from node hits.
- **TASK-044** (optional): **Done (lightweight)** — `GRAPH_SEARCH_RERANK_TOP_M` + `GRAPH_SEARCH_RERANK_BOOST`: token Jaccard overlap on query vs fact/name/summary for the top-M by fused score (no cross-encoder).
- **TASK-045** (optional): **Done** — `EMBEDDING_CACHE_TTL_SEC` on `EmbeddingService` cache entries; `GRAPH_SEARCH_RESULT_CACHE_TTL_SEC` on full `Neo4jStorage.search` payloads (keyed by graph/query/scope/limit + relevant retrieval flags).

---

## PHASE 14 — Throughput, async jobs, and UX (COMPLETE)

**Goal:** Resilient builds, visibility, controlled concurrency.

- **TASK-046**: **Done** — `TaskManager` optional JSON persistence when **`GRAPH_JOB_PERSIST_DIR`** is set (survives poll after process restart for finished tasks). New **`POST /api/graph/jobs/chunks`** enqueues `graph_chunk_ingest` with `task_id`; poll existing **`GET /api/graph/task/<task_id>`**. Project graph build fixes **`create_task("graph_build", metadata=...)`** and uses **`GRAPH_INGEST_BATCH_SIZE`**; chunk progress in **`progress_detail`** (`chunks_done`, `chunks_total`, batches).
- **TASK-047**: **Done** — **`LLM_INGEST_MAX_CONCURRENT`** (default **2**, **0** = unlimited) via `threading.BoundedSemaphore` in **`utils/llm_ingest_concurrency.py`**, applied around **`Neo4jStorage.add_text`**. **`GRAPH_INGEST_BATCH_SIZE`** (default **3**) for ingest batching.
- **TASK-048**: **Done** — **`GET /api/graph/admin/ingest-stats`** returns **`process_counters`** (`chunks_ok` / `chunks_failed` from **`utils/ingest_counters.py`**) and Neo4j **`episodes_total`** plus per-graph episode counts (optional **`?graph_id=`**). Merge-vector stats remain future work if needed.

---

## PHASE 15 — Graphiti integration (in progress)

**Goal:** Replace the custom Neo4j ingest/search stack with **[Graphiti](https://github.com/getzep/graphiti)** as the **only** writer and query path for graph memory (no dual-write). Baseline snapshot: branch **`checkpoint/pre-graphiti`**.

**Canonical plan:** [`docs/graphiti-cutover.md`](graphiti-cutover.md) — Zep SDK → Graphiti mapping, `GraphitiStorage` adapter outline, Neo4j version notes, rollout steps.

- **TASK-049**: Implement `GraphStorage` backed by `graphiti-core`; vertical slice ingest + hybrid search (golden set / smoke).
- **TASK-050**: Env/runbook in [`docs/graphiti-cutover.md`](graphiti-cutover.md) (expanded); `GRAPH_BACKEND`; JSON → Graphiti `entity_types` wired (`graphiti_ontology.py`); `edge_types` + golden parity still open.

---

## Execution flow (reference)

```mermaid
flowchart LR
  M[Phases 0-6 Migration done]
  P7[Phase 7 Publish]
  P8[Phase 8 Metrics]
  P9[Phase 9 Inference]
  P10[Phase 10 Extract]
  P11[Phase 11 Maintain]
  P12[Phase 12 Temporal]
  P13[Phase 13 Retrieve]
  P14[Phase 14 Async UX]
  P15[Phase 15 Graphiti]
  M --> P7
  M --> P8
  P8 --> P9 --> P10 --> P11
  P10 --> P13
  P11 --> P12
  P13 --> P14
  P10 -.-> P15
```

---

## Success criteria (acceptance examples)

- **Density:** Golden doc edge-to-node ratio improves materially without junk-edge explosion
- **Stability:** Lower JSON failure rate; no silent context truncation
- **Retrieval:** Better hit rate on manual/scripted eval for graph tools + reports
- **Operations:** Predictable build time; failed chunks visible and retryable

---

## Clarifications

- **Neo4j vs Graphiti:** Neo4j is the **database**. Graphiti names **patterns and optional software** for ingest/merge/time—not “switch DB to win.”
- **Code anchors:** `backend/app/storage/ner_extractor.py`, `backend/app/utils/llm_client.py`, `backend/app/utils/ingest_metrics.py`, `neo4j_storage.py`, `neo4j_schema.py`, `graph_storage.py`, `search_service.py`, `config.py`, `file_parser.py`, `graph_memory_updater.py`, `graph_builder.py`, `graph.py`, `services/graph_tools.py`, `services/entity_reader.py` (see `progress.md` for what already exists).

---

## Planned new / touched files (tracking)

Paths below extend the **already landed** layout from `progress.md` (`storage/`, `services/`, `utils/`).

| File / area | Purpose | Status |
|-------------|---------|--------|
| `backend/app/utils/ingest_metrics.py` | KPI schema + structured ingest log line | **Done** (Phase 8) |
| `backend/app/storage/entity_normalize.py` | Name/fact normalization + aliases | **Done** (Phase 11) |
| `backend/app/storage/merge_adjudicator.py` | LLM merge adjudication | **Done** (Phase 11) |
| `backend/app/services/graph_maintenance.py` | Entity summary refresh from facts | **Done** (Phase 11) |
| `backend/app/storage/graph_maintenance.py` (new) | Optional future batch jobs | Optional |
| `ner_extractor.py` + `neo4j_storage.py` | Two-pass NER, linking hints, `MENTIONS` | **Done** (Phase 10) |
| `backend/app/services/graph_ingestion_pipeline.py` (new, optional) | Extra orchestration if split from storage | Optional |
| `backend/app/storage/ner_extractor.py` | Two-pass, optional JSON repair (Phase 9–10) | TODO |
| `backend/app/utils/llm_client.py` | Optional extract-only base URL, `chat_json` repair path | TODO |
| `backend/app/storage/neo4j_storage.py` | Provenance edges, dedupe hooks | TODO |
| `backend/app/storage/neo4j_schema.py` | Episode–entity relations, indexes if needed | TODO |
| `backend/app/jobs/` or `services/graph_build_queue.py` (new) | Async build jobs | TODO |
| `docs/golden-set/` + `scripts/snapshot_golden_counts.py` | Golden set + count snapshot | **Done** (Phase 8) |

---

## Dependencies for prioritization

- GPU VRAM and whether ingest must stay on a small local model
- Primary corpus type (reports vs chat vs code)—chunking and eval differ
- Offline-only vs cloud OK for extraction-only endpoint
- Location of golden documents and who signs off on quality bars

---

# Addendum A — Improving analysis (quality of insights, not just graph size)

“Analysis” here means **trustworthy conclusions** from the graph + LLM stack: simulations, `ReportAgent`, `GraphToolsService` (insight_forge, panorama, interviews), and any post-build QA.

## A.1 Evaluation and ground truth

- **TASK-A01**: Maintain **golden questions** with expected entities/relations or “must cite” facts—automate precision/recall on graph answers where feasible
- **TASK-A02**: **Human rubric** (1–5) for report sections—groundedness, completeness, actionability; run before/after retrieval changes
- **TASK-A03**: Log **which edges/nodes** were retrieved for each tool call; store in debug mode for failure analysis

## A.2 Graph quality → analysis quality

- Denser, deduped graphs (Phases 10–11) directly improve tool hits; **temporal** correctness (Phase 12) stops contradictory evidence in one answer
- **Provenance** (Phase 10D) enables “show sources” in reports—reduces hallucinated glue between facts

## A.3 Report and tool prompts

- **TASK-A04**: Explicit **citation format** in prompts—require fact sentences or node names from retrieved context only
- **TASK-A05**: **Confidence gating**—if retrieval score below threshold, answer “insufficient graph evidence” instead of inventing bridges
- **TASK-A06**: Optional **second-pass critic** on draft report (small model or same model)—check unsupported claims vs retrieved bundle (cost/latency tradeoff)

## A.4 Simulation ↔ graph alignment

- **TASK-A07**: Compare `GraphMemoryUpdater` episode text to ontology—ensure action descriptions mention entities the NER can extract
- **TASK-A08**: Periodic **consistency check**—sample agents vs graph entities they should “know” from simulation memory

## A.5 Product analytics

- **TASK-A09**: Dashboards—tool call success rate, empty-search rate, avg evidence size per report section
- **TASK-A10**: A/B or version tags on prompt/pipeline versions to correlate with rubric scores

*Include or exclude Addendum A tasks in your backlog as you prefer; they are independent of Phases 8–15 but compound with better graphs and retrieval.*

---

# Addendum B — Async / concurrent LLM calls: will it be better?

**Short answer:** Async or **limited parallel** calls usually **improve wall-clock throughput** for graph builds and batch tools; they do **not** by themselves improve **per-chunk extraction quality**. Quality still comes from model, prompts, chunking, and multi-pass logic (Phases 9–10). Misconfigured parallelism can **hurt** stability (OOM, timeouts, nondeterministic ordering).

## B.1 Where concurrency helps

| Scenario | Benefit |
|----------|---------|
| **Many independent chunks** in one graph build | Overlap I/O + GPU wait; shorter total build time |
| **Embedding batches** | Often already batched; parallel **requests** can complement batch APIs |
| **Multiple graphs / users** | Throughput across tenants |
| **ReportAgent / multi-tool** | Parallel retrieval + LLM steps where dependencies allow |

## B.2 Where concurrency does not replace other work

- **Single long chunk**—one completion is still one completion; no gain from duplicate calls
- **Ordering-sensitive** writes—if merge logic assumes strict episode order, parallel ingest may need **per-graph serialization** or transactional merge (Phase 11)
- **VRAM-bound local GPU**—too many concurrent generations **queue or OOM**; optimal is often **small pool** (e.g. 1–2) not “N = chunk count”

## B.3 Risks and mitigations

- **Rate limits / server queue** (LM Studio, Ollama): many clients → timeouts; use **semaphore** + retries + jitter
- **Non-determinism**: parallel chunk completion order ≠ document order—**OK** if merges are commutative; **not OK** if you rely on “first wins” without merge rules
- **Debugging**: failures harder to reproduce—**structured job ids** and per-chunk logs (Phase 8) become essential

## B.4 Recommended direction for this fork

1. **Phase 8 metrics first**—measure p50/p95 chunk latency and failure rate **serially**
2. **Phase 9–10**—quality baseline before scaling concurrency
3. **Phase 14**—introduce **bounded worker pool** (env `GRAPH_LLM_MAX_CONCURRENT` or similar), optional **async job API** for UX; avoid unbounded `asyncio.gather` on all chunks
4. **Optional**: parallel **CPU** work (parsing, embedding prep) while GPU runs LLM—cheap win with same GPU concurrency cap

## B.5 Verdict

| Question | Answer |
|----------|--------|
| Will async always be “better”? | **No**—only better for **throughput** when hardware and merge semantics allow it |
| Should we plan it? | **Yes** in Phase 14 (queue + backpressure), **after** quality baselines |
| Default for local single-GPU? | **Low concurrency (1–2)** often optimal; measure before raising |

---

# Addendum C — Evidence base: is this roadmap direction sound?

This addendum maps the roadmap to **published research**, **public benchmarks**, and **industry practice**, and states **limits of correlation** (your stack is local LLMs + Neo4j; many papers use cloud APIs or different datasets).

**For challenges to these claims, precision–recall tradeoffs, and classical methods that *revise* the roadmap, see Addendum D (below).**

## C.1 Overall verdict

| Claim in roadmap | Support in literature / practice |
|------------------|--------------------------------|
| **Temporal KG + episodic memory for agents** (Zep-style) | Zep’s system paper is an **arXiv preprint** ([2501.13956](https://arxiv.org/abs/2501.13956)); treat benchmark claims as **internal/author-reported** until independently replicated. The **research question** (structured, time-aware memory vs flat context) is mainstream; the **exact numbers** are not general scientific law. |
| **LLMs for KG construction + ontology/schema guidance** | Surveys frame **LLM–KG interplay**: ontology generation, validation, QA, consistency—aligned with ontology-guided extraction (Phases 9–11). See Springer *Discover Artificial Intelligence* ([10.1007/s44163-024-00175-8](https://doi.org/10.1007/s44163-024-00175-8)) and the arXiv survey *Research Trends for the Interplay between Large Language Models and Knowledge Graphs* ([arXiv:2406.08223](https://arxiv.org/abs/2406.08223)). |
| **Splitting “structure” from “content” (two-pass / staged extraction)** | **Empirical support**: work on improving structured IE output finds **decoupling** structuring from raw generation helps NER/RE-style tasks; see [“A Simple but Effective Approach to Improve Structured Language Model Output for Information Extraction”](https://aclanthology.org/2024.findings-emnlp.295) (Findings of EMNLP 2024, [arXiv:2402.13364](https://arxiv.org/abs/2402.13364)). This **supports Phase 10A** conceptually (entities then relations), independent of Zep. |
| **Hybrid retrieval (dense + lexical/BM25)** | **Strong engineering consensus** and benchmark evidence: sparse and dense capture **complementary** errors; hybrid fusion often beats either alone on retrieval benchmarks such as **BEIR** ([BEIR paper](https://arxiv.org/abs/2104.08663)); see also ecosystem summaries on RRF / weighted fusion. **Supports Phase 13** (your 0.7/0.3 style blend is a standard starting point; tuning beats fixed weights). |
| **Graph structure for “global” QA / summaries** | **GraphRAG** (Microsoft Research) builds an entity graph, community structure, and summaries for query-focused summarization—showing **graphs + retrieval** help holistic questions where vector-only RAG is weak. See [From Local to Global: A Graph RAG Approach to Query-Focused Summarization](https://www.microsoft.com/en-us/research/publication/from-local-to-global-a-graph-rag-approach-to-query-focused-summarization/) and [GraphRAG project](https://microsoft.github.io/graphrag/). **Supports** richer graph density + community/summary ideas (related to Phase 11 entity summarization and Phase 13 expansion). |
| **Long-horizon memory evaluation** | **LongMemEval** ([arXiv:2410.10813](https://arxiv.org/abs/2410.10813), ICLR 2025) stresses **temporal reasoning**, **knowledge updates**, and **abstention**—directly relevant to **Phase 12** and **Addendum A** (analysis groundedness). Zep’s blog/paper claims improvements on this axis; treat **vendor-reported numbers** as **hypothesis-generating**, not independent proof of your fork’s gains. |
| **Entity resolution / deduplication (blocking, matching, canonicalization)** | **Established field**: end-to-end **entity resolution** surveys ([arXiv:1905.06397](https://arxiv.org/abs/1905.06397)) and **neural entity linking** surveys (e.g. [Semantic Web journal survey](https://doi.org/10.3233/SW-222986)) justify **staged** candidate generation + ranking + optional LLM adjudication (**Phase 11**). Graphiti’s MinHash/LSH-style ideas are **plausible engineering** in that tradition; exact choice should still be **measured on your data**. |
| **Structured outputs / JSON for extraction** | Provider APIs and recent IE papers push **schema-constrained** decoding; quality varies by model and task size. Treat **Phase 9** structured output as **best practice**, not a guarantee—keep **repair/retry** and golden-set checks. |
| **Async / parallel LLM calls** | **No universal paper** “async = smarter.” Throughput gains follow **queueing theory** and **GPU scheduling**; risks (OOM, ordering) are **systems** concerns. **Addendum B** remains the correct framing: measure after quality baselines. |

## C.2 What is *not* proven by citations here

- **Your** edge-to-node ratio target on **your** corpus with **your** local model is an **empirical** question—Phase 8 golden set is the right scientific move **on your fork**.
- **Zep vs MemGPT** numbers come from Zep’s paper/blog; replication on **Ollama + Neo4j** is **not** automatic.
- **Graphiti** as a library: open source and aligned with Zep’s story ([getzep/graphiti](https://github.com/getzep/graphiti)); adopting it is an **integration** decision, not a requirement for validity of the *concepts*.

## C.3 Correlation vs causation (how to use this doc)

1. **Literature** supports **which layers exist** (extract → store → retrieve → time → evaluate).  
2. **Your roadmap** orders **engineering work**; correlation with “better UX” requires **before/after metrics** (Phase 8, Addendum A rubrics).  
3. **Strongest causal levers** you can test locally: **model capacity**, **context/output limits**, **chunk boundaries**, **two-pass extraction**, **hybrid retrieval weights**, **dedupe**—each can be **ablated** on the golden set.

## C.4 Suggested reading order (for implementers)

1. [arXiv:2501.13956](https://arxiv.org/abs/2501.13956) — Zep / temporal KG for agent memory (big picture).  
2. [arXiv:2410.10813](https://arxiv.org/abs/2410.10813) — LongMemEval (what “good memory” benchmarks measure).  
3. [arXiv:2104.08663](https://arxiv.org/abs/2104.08663) — BEIR (why hybrid retrieval).  
4. [ACL 2024 Findings EMNLP structured IE paper](https://aclanthology.org/2024.findings-emnlp.295) — staged structuring for extraction.  
5. Microsoft **GraphRAG** publication + docs — graph communities and global retrieval.  
6. Entity resolution survey [arXiv:1905.06397](https://arxiv.org/abs/1905.06397) — dedupe pipeline vocabulary.

---

# Addendum D — Critical review: evidence that *challenges* the roadmap, and methodological upgrades

Addendum C asked what **supports** the plan. This addendum does the opposite: **contradictions, risks, and established methods** that should **tighten or revise** Phases 8–15. Preference is given to **mature venues and methods** (statistics, IR, ACL-affiliated proceedings, surveys); **arXiv-only** or very new workshop items are cited **with caveats** where they supply needed empirical warnings.

## D.1 Epistemic stance

The roadmap is a **set of engineering hypotheses**. Scientific backing requires:

1. **Pre-specified** metrics and stop rules (what would falsify an approach on your golden set).  
2. **Human adjudication** for relation and entity gold—generative RE evaluation is **not** reliably reduced to exact string match; [Wadhwa et al., ACL 2023 — *Revisiting Relation Extraction in the era of Large Language Models*](https://aclanthology.org/2023.acl-long.868/) argue for **human evaluation** alongside automated metrics.  
3. **Inter-annotator agreement** when more than one annotator exists; disagreement signals **ambiguous guidelines**, not only noise ([Kulick et al., EVENTS workshop 2014](https://aclanthology.org/W14-2904/) — *Inter-annotator Agreement for ERE annotation*).

**Implication:** Phase 8 is necessary but **insufficient** without a written **annotation guideline** and, where possible, **two annotators + κ / α** on a slice of the corpus.

---

## D.2 Challenge: “Two-pass extraction will fix sparse relations”

**Supporting evidence (decoupling helps):** [Wang et al., Findings EMNLP 2024](https://aclanthology.org/2024.findings-emnlp.295) — separating structuring from raw generation improves structured IE.

**Counter-evidence and limits:**

- **Robustness across data regimes:** [Swarup et al., COLING 2025 — *LLM4RE: A Data-centric Feasibility Study for Relation Extraction*](https://aclanthology.org/2025.coling-main.447/) report that **frontier LLMs are not uniformly robust** across relation-extraction data characteristics; **2100+** controlled experiments show **failure modes** persist. Two-pass reduces **coupling** in the *prompt*; it does not erase **domain shift**, **long-tail relations**, or **contextual ambiguity**.  
- **Benchmark quality:** [Zhou et al., Findings ACL 2024 — *The State of Relation Extraction Data Quality*](https://aclanthology.org/2024.findings-acl.470/) show **label errors and weak distant supervision** are widespread; **larger datasets ≠ better evaluation**. Improving extraction on **noisy gold** optimizes the wrong target.

**Roadmap revision:** Treat Phase 10A as **necessary for pipeline ergonomics**, not sufficient for **factual** KG quality. Add an explicit **verification** path (sample-based human review or a **secondary model-as-judge** with known false-positive rates—see D.3). Prefer **small, clean expert slices** of gold over large noisy corpora for acceptance tests.

---

## D.3 Challenge: “Denser graphs are always better for analysis”

**Bidirectional fact:** External KGs can **reduce** LLM hallucination in retrieval-augmented use, but **erroneous triples** become **trusted evidence**. [Agrawal et al., NAACL 2024 — *Can Knowledge Graphs Reduce Hallucinations in LLMs? A Survey*](https://aclanthology.org/2024.naacl-long.219/) (DOI [10.18653/v1/2024.naacl-long.219](https://doi.org/10.18653/v1/2024.naacl-long.219)) catalogs methods and **limits** of KG grounding.

**Recent IE direction (use cautiously—EMNLP main):** [Wang et al., EMNLP 2025 — *Can LLMs be Good Graph Judge for Knowledge Graph Construction?*](https://aclanthology.org/2025.emnlp-main.554/) propose **LLM-as-judge** for constructed graphs; useful as a **filter**, not a proof of truth (judge models **share** some failure modes with extractors).

**Classical tradeoff:** Higher **recall** on relations (prompting for “all edges”) **raises false triple rate** unless precision mechanisms exist—standard **precision–recall** logic, not LLM-specific.

**Roadmap revision:** Phase 11 should **separate** (a) **high-precision** merge rules for canonical entities from (b) **optional recall-oriented** relation expansion, with **downstream retrieval** defaulting to **confidence / source filters**. Do not treat edge count as a **monotonic** quality metric.

---

## D.4 Challenge: fixed weighted hybrid search (0.7 / 0.3)

**Established alternative:** **Reciprocal Rank Fusion (RRF)** is an **unsupervised** fusion rule with **TREC-backed** evaluation: [Cormack, Clarke, Büttcher, SIGIR 2009](https://dl.acm.org/doi/10.1145/1571941.1572114) (*Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods*). RRF needs **no training labels** and is a **standard baseline** when combining rankers.

**Dense vs sparse complementarity:** [Thakur et al., BEIR](https://arxiv.org/abs/2104.08663) (NeurIPS 2021 Datasets & Benchmarks track) motivates **hybrid** retrieval under **distribution shift**; it does **not** mandate a single convex weight.

**Roadmap revision:** Phase 13 should (1) implement or evaluate **RRF** against fixed weights on a **held-out query set**, (2) treat **tuned** linear weights as a **second step** that requires **labeled** or **explicitly judged** query–document relevance, not default constants.

---

## D.5 Strengthen entity resolution (Phase 11) with classical statistics

**Foundational model:** [Fellegi & Sunter, JASA 1969](https://doi.org/10.1080/01621459.1969.10501049) — *A Theory for Record Linkage*. Core idea: linkage is **probabilistic**; optimal decisions partition pairs into **link**, **non-link**, and **possible link** (clerical review). **m- and u-probabilities** make **false merge** rates discussable.

**Modern synthesis:** Christen’s **entity resolution** survey ([arXiv:1905.06397](https://arxiv.org/abs/1905.06397)) covers blocking, scalability, and **quality–efficiency** tradeoffs—still the standard vocabulary for engineering dedupe pipelines.

**Challenge to current practice:** `MERGE` on `name_lower` is a **deterministic** rule, not a **three-decision** FS pipeline. It **cannot** express “uncertain—send to review” and **collapses** homonyms (same string, different referents).

**Roadmap revision:** Introduce a **similarity score + threshold band**: auto-merge above τ_high, reject below τ_low, **queue** between. Calibrate τ using **labeled** duplicate pairs or **Fellegi–Sunter-style** weighting where feasible; avoid **LLM merge** without **budget** for errors.

---

## D.6 Temporal memory (Phase 12): harder than “set invalid_at”

**Survey grounding:** Temporal KG surveys (e.g. representation learning and applications: [arXiv:2403.04782](https://arxiv.org/abs/2403.04782) — *A Survey on Temporal Knowledge Graph: Representation Learning and Applications*) stress **time granularity** (interval vs point), **multiple calendars**, and **forecasting vs reasoning** tasks.

**Evaluation reality:** [LongMemEval](https://arxiv.org/abs/2410.10813) (ICLR 2025) shows **knowledge updates** and **temporal reasoning** remain **weak spots** for long-horizon assistants; LLM-based contradiction detection inherits those limits.

**Roadmap revision:** Prefer **document- or episode-level timestamps** and **explicit provenance** over LLM-only “this contradicts that.” Combine **rules** (same subject–predicate pair, new object with newer source time) with **optional** LLM summarization of change. Phase 12 should specify **time model** (point vs interval) **before** coding.

---

## D.7 GraphRAG-style global retrieval: benefit and cost

Microsoft **GraphRAG** ([From Local to Global…](https://www.microsoft.com/en-us/research/publication/from-local-to-global-a-graph-rag-approach-to-query-focused-summarization/)) demonstrates **community summaries** for **global** questions. **Challenge:** pipeline cost is **multi-stage** (extract → cluster → summarize); benefit is tied to **question type** (global vs local).

**Roadmap revision:** Treat community summarization as **Phase 11/13 optional** branch gated by **query log analysis**—if users rarely ask global questions, **defer** GraphRAG-like stages to avoid **fixed overhead** on every graph.

---

## D.8 Async / parallel LLM (Addendum B) — systems, not cognition

There is **no** seminal paper stating “parallelism improves extraction F1.” Throughput gains are predicted by **queueing models** (e.g. M/M/c and variants) and **GPU memory** constraints—**operations research** and **systems engineering**, not NLP benchmarks.

**Roadmap revision:** Keep Addendum B’s ordering: **quality baselines serially**, then **bounded** parallelism with **measured** p95 latency and **OOM** monitoring. Present concurrency as **capacity planning**, not **accuracy**.

---

## D.9 Consolidated science-backed amendments to the roadmap

| Area | Amendment |
|------|-----------|
| **Metrics (Phase 8)** | Add **human gold** protocol, **IAA** where feasible, **generative-RE-aware** eval (not exact-match-only). |
| **Extraction (10A)** | Add **verification** or **judge** pass on a **stratified sample**; track **precision** of new edges, not only count. |
| **Dedupe (11)** | Replace naive single-key `MERGE` over time with **thresholds + possible-link queue**; study **Fellegi–Sunter** / Christen pipeline. |
| **Retrieval (13)** | Add **RRF** baseline; tune weights only with **held-out queries**. |
| **Temporal (12)** | Define **time model** and **non-LLM** invalidation rules first. |
| **GraphRAG-like** | **Conditional** on use case; avoid universal community summarization. |
| **Evidence hierarchy** | Prefer **ACL/NAACL/EMNLP/COLING**, **SIGIR**, **JASA**, **IEEE/Springer surveys**; treat **arXiv** and **vendor blogs** as **hypothesis-generating**. |

---

## D.10 Reading list — critical / methods-first

1. [Fellegi & Sunter, JASA 1969](https://doi.org/10.1080/01621459.1969.10501049) — probabilistic linkage.  
2. [Cormack et al., SIGIR 2009](https://dl.acm.org/doi/10.1145/1571941.1572114) — RRF.  
3. [Thakur et al., BEIR, NeurIPS 2021 D&B](https://arxiv.org/abs/2104.08663) — hybrid retrieval motivation.  
4. [Agrawal et al., NAACL 2024](https://aclanthology.org/2024.naacl-long.219/) — KGs and hallucination (**limits**).  
5. [Zhou et al., Findings ACL 2024](https://aclanthology.org/2024.findings-acl.470/) — RE dataset quality.  
6. [Swarup et al., COLING 2025](https://aclanthology.org/2025.coling-main.447/) — LLM RE robustness (LLM4RE).  
7. [Wadhwa et al., ACL 2023](https://aclanthology.org/2023.acl-long.868/) — generative RE + human eval.  
8. [Wang et al., EMNLP 2025](https://aclanthology.org/2025.emnlp-main.554/) — graph judge (use with care).  
9. Christen, [Entity resolution survey](https://arxiv.org/abs/1905.06397).  
10. [LongMemEval, ICLR 2025](https://arxiv.org/abs/2410.10813) — long-horizon memory tasks.  

---

*Document version: 3.8 — Phase 15 reframed as Graphiti integration; see `graphiti-cutover.md`. Phases 8–14 complete on `checkpoint/pre-graphiti`. Addenda A–D optional for backlog triage.*

