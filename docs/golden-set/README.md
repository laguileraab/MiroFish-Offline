# Golden set — graph ingest regression (Phase 8, TASK-022)

Fixed documents and a repeatable procedure to snapshot **entity / relation / episode** counts after roadmap changes.

## Contents

| File | Purpose |
|------|---------|
| `ontology-min.json` | Small ontology (entity + relation types) for comparable NER/RE |
| `sample-01-org.txt` | Short org / product text |
| `sample-02-policy.txt` | Short policy-style paragraph |
| `sample-03-tiny.txt` | Edge case: minimal length |

## Canonical graph id

Use a dedicated id so you never mix golden data with real projects, e.g. `golden-regression-v1`.

## Procedure

1. **Environment:** Neo4j + LLM running; `.env` matches [`../../.env.example`](../../.env.example) checklist (chunk size, `OLLAMA_NUM_CTX`, `NER_MAX_OUTPUT_TOKENS`).
2. **Create graph** (API or UI) with your chosen `graph_id`.
3. **Attach ontology:** set the graph’s ontology JSON to the contents of `ontology-min.json` (same shape as production: `entity_types`, `relation_types`).
4. **Ingest** each `sample-*.txt` (upload or call graph build API) with **fixed** `chunk_size` / `chunk_overlap` (record them in your run notes).
5. **Snapshot counts** from repo root:

   ```bash
   python scripts/snapshot_golden_counts.py golden-regression-v1
   ```

6. **Record** the printed line plus git SHA and model name in a text file or ticket. Re-run after Phases 9–11 to compare.

## Entity alias table (optional)

For acronym → canonical names on ingest, set **`ENTITY_ALIAS_JSON_PATH`** to a JSON file like [`entity-aliases.example.json`](entity-aliases.example.json) (Phase 11).

## NER output token budget

See [`NER_MAX_OUTPUT_TOKENS.md`](NER_MAX_OUTPUT_TOKENS.md) (Phase 9 TASK-025).

## Log analysis

Structured ingest lines are prefixed with `ingest_metrics` (JSON payload). Example:

```bash
grep ingest_metrics /path/to/backend.log
```

Optional guardrail warnings: `ingest_guardrail`.

## Neo4j Browser (manual alternative)

```cypher
MATCH (e:Entity {graph_id: 'golden-regression-v1'}) RETURN count(e) AS entities;
MATCH ()-[r:RELATION {graph_id: 'golden-regression-v1'}]->() RETURN count(r) AS relations;
MATCH (e:Episode {graph_id: 'golden-regression-v1'}) RETURN count(e) AS episodes;
```
