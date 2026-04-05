# Tuning `NER_MAX_OUTPUT_TOKENS` (Phase 9, TASK-025)

`NER_MAX_OUTPUT_TOKENS` caps the **completion** size for each NER/RE JSON response. If it is too low, the model may **truncate** inside the `relations` array (invalid JSON or partial lists).

## Constraint

For Ollama, **`OLLAMA_NUM_CTX` must fit prompt + completion** (roughly):

```text
num_ctx ≥ (tokens for system + ontology + user chunk) + NER_MAX_OUTPUT_TOKENS
```

The backend logs a **Config** warning on startup when `estimated_prompt + NER_MAX_OUTPUT_TOKENS > OLLAMA_NUM_CTX`.

## Practical ranges (starting points)

| Setup | `OLLAMA_NUM_CTX` | `NER_MAX_OUTPUT_TOKENS` | Notes |
|-------|------------------|-------------------------|--------|
| Safe default | **16384** or **32768** | **4096** | Room for medium ontology + long relation lists |
| Tight 8k GPU | 8192 | **2048**–**3072** | Reduce chunk size (`GRAPH_CHUNK_SIZE`) if prompts are large |
| Maximum recall | 32768+ | **8192** | Watch VRAM and latency |

## How to tune

1. Ingest `docs/golden-set/sample-01-org.txt` with `ontology-min.json` on a throwaway `graph_id`.
2. Watch logs for `ingest_metrics`: `finish_reason: length`, `suspected_output_truncation: true`, or `relations_per_chunk` oddly low vs. text.
3. Raise `NER_MAX_OUTPUT_TOKENS` in steps (e.g. 2048 → 4096) until relations stabilize; **always** raise `OLLAMA_NUM_CTX` if the startup warning appears.
4. Re-run `scripts/snapshot_golden_counts.py` and compare relation counts.

## JSON repair

Phase 9 also applies **lightweight JSON repair** in `LLMClient.chat_json` (trailing commas, extract first top-level `{...}`). It does not fix a **truncated** stream; fixing truncation still requires higher `NER_MAX_OUTPUT_TOKENS` or a smaller extraction pass (see roadmap Phase 10 two-pass).
