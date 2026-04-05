"""
Configuration Management
Loads configuration from .env file in project root directory
"""

import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from dotenv import load_dotenv


def parse_iso8601_utc(value: Optional[str]) -> Optional[str]:
    """Normalize an ISO-8601 string to UTC ``isoformat()`` or None if empty/invalid."""
    if not value or not str(value).strip():
        return None
    s = str(value).strip()
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None

# Load .env file from project root
# Path: MiroFish/.env (relative to backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    # If no .env in root, try to load environment variables (for production)
    load_dotenv(override=True)


class Config:
    """Flask configuration class"""

    # Flask configuration
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'

    # JSON configuration - disable ASCII escaping to display Chinese directly (not as \uXXXX)
    JSON_AS_ASCII = False

    # LLM configuration (unified OpenAI format)
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://localhost:11434/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'qwen2.5:32b')
    # HTTP timeout for each LLM request (seconds). Local LM Studio / large prompts often need 30+ min.
    LLM_HTTP_TIMEOUT_SEC = float(os.environ.get('LLM_HTTP_TIMEOUT_SEC', '1800'))

    # Neo4j configuration
    NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
    NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD', 'mirofish')
    # Logical Neo4j database name (Graphiti + sync driver). Default matches Neo4j Desktop.
    NEO4J_DATABASE = os.environ.get('NEO4J_DATABASE', 'neo4j').strip() or 'neo4j'

    # Graph backend: ``neo4j`` = custom NER/RE pipeline; ``graphiti`` = graphiti-core engine
    GRAPH_BACKEND = os.environ.get('GRAPH_BACKEND', 'neo4j').strip().lower() or 'neo4j'
    _gmc = os.environ.get('GRAPHITI_MAX_COROUTINES', '').strip()
    GRAPHITI_MAX_COROUTINES = int(_gmc) if _gmc else None
    NEO4J_MAX_CONNECTION_POOL_SIZE = int(os.environ.get('NEO4J_MAX_POOL_SIZE', '50'))
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT = int(os.environ.get('NEO4J_ACQUISITION_TIMEOUT', '60'))

    # Embedding configuration
    EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'nomic-embed-text')
    EMBEDDING_BASE_URL = os.environ.get('EMBEDDING_BASE_URL', 'http://localhost:11434')

    # File upload configuration
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}

    # Text processing configuration (env: GRAPH_CHUNK_SIZE / GRAPH_CHUNK_OVERLAP — Phase 8 TASK-023)
    # Phase 10 TASK-031: optional GRAPH_CHUNK_PROFILE=small|medium|large when GRAPH_CHUNK_SIZE unset
    _explicit_cs = os.environ.get('GRAPH_CHUNK_SIZE')
    _chunk_prof = os.environ.get('GRAPH_CHUNK_PROFILE', '').strip().lower()
    _CHUNK_PRESETS = {'small': (400, 40), 'medium': (500, 50), 'large': (800, 80)}
    if _explicit_cs:
        DEFAULT_CHUNK_SIZE = int(_explicit_cs)
        DEFAULT_CHUNK_OVERLAP = int(
            os.environ.get('GRAPH_CHUNK_OVERLAP', os.environ.get('DEFAULT_CHUNK_OVERLAP', '50'))
        )
    elif _chunk_prof in _CHUNK_PRESETS:
        DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP = _CHUNK_PRESETS[_chunk_prof]
    else:
        DEFAULT_CHUNK_SIZE = int(os.environ.get('DEFAULT_CHUNK_SIZE', '500'))
        DEFAULT_CHUNK_OVERLAP = int(os.environ.get('GRAPH_CHUNK_OVERLAP', os.environ.get('DEFAULT_CHUNK_OVERLAP', '50')))

    NER_MAX_OUTPUT_TOKENS = int(os.environ.get('NER_MAX_OUTPUT_TOKENS', '8192'))
    # Phase 10: two-pass NER/RE + graph-aware linking hints
    NER_TWO_PASS = os.environ.get('NER_TWO_PASS', '').strip().lower() in ('1', 'true', 'yes')
    NER_LINK_TOP_K = int(os.environ.get('NER_LINK_TOP_K', '12'))
    # Prefer breaking chunks at paragraph boundaries (\n\n) when possible (TASK-032)
    GRAPH_CHUNK_PREFER_PARAGRAPH = os.environ.get('GRAPH_CHUNK_PREFER_PARAGRAPH', '').strip().lower() in (
        '1',
        'true',
        'yes',
    )

    # Phase 11 — merge graph / dedupe
    ENTITY_ALIAS_JSON_PATH = os.environ.get('ENTITY_ALIAS_JSON_PATH', '').strip()
    GRAPH_MERGE_VECTOR_ENABLED = os.environ.get('GRAPH_MERGE_VECTOR_ENABLED', '').strip().lower() in (
        '1',
        'true',
        'yes',
    )
    GRAPH_MERGE_VECTOR_THRESHOLD = float(os.environ.get('GRAPH_MERGE_VECTOR_THRESHOLD', '0.88'))
    GRAPH_MERGE_VECTOR_AMBIG_LOW = float(os.environ.get('GRAPH_MERGE_VECTOR_AMBIG_LOW', '0.72'))
    GRAPH_MERGE_LLM_ADJUDICATE = os.environ.get('GRAPH_MERGE_LLM_ADJUDICATE', '').strip().lower() in (
        '1',
        'true',
        'yes',
    )
    RELATION_DEDUPE_ENABLED = os.environ.get('RELATION_DEDUPE_ENABLED', 'true').strip().lower() not in (
        '0',
        'false',
        'no',
    )
    ENTITY_SUMMARY_MAX_PER_CHUNK = int(os.environ.get('ENTITY_SUMMARY_MAX_PER_CHUNK', '0'))

    # Phase 12 — temporal memory (RELATION valid_at / invalid_at)
    GRAPH_TEMPORAL_ENABLED = os.environ.get('GRAPH_TEMPORAL_ENABLED', '').strip().lower() in (
        '1',
        'true',
        'yes',
    )
    GRAPH_TEMPORAL_SUPERSEDE_SAME_TRIPLE = os.environ.get(
        'GRAPH_TEMPORAL_SUPERSEDE_SAME_TRIPLE', 'true'
    ).strip().lower() not in ('0', 'false', 'no')
    GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY = os.environ.get(
        'GRAPH_TEMPORAL_QUERY_ACTIVE_ONLY', 'true'
    ).strip().lower() not in ('0', 'false', 'no')
    GRAPH_QUERY_AS_OF_ISO = os.environ.get('GRAPH_QUERY_AS_OF_ISO', '').strip() or None

    # Phase 13 — retrieval tuning (TASK-042–045)
    SEARCH_VECTOR_WEIGHT = float(os.environ.get('SEARCH_VECTOR_WEIGHT', '0.7'))
    SEARCH_KEYWORD_WEIGHT = float(os.environ.get('SEARCH_KEYWORD_WEIGHT', '0.3'))
    GRAPH_SEARCH_EXPAND_HOPS = int(os.environ.get('GRAPH_SEARCH_EXPAND_HOPS', '0'))
    GRAPH_SEARCH_EXPAND_EXTRA = int(os.environ.get('GRAPH_SEARCH_EXPAND_EXTRA', '24'))
    GRAPH_SEARCH_EXPAND_MAX_PER_SEED = int(os.environ.get('GRAPH_SEARCH_EXPAND_MAX_PER_SEED', '8'))
    _expand_types_raw = os.environ.get('GRAPH_SEARCH_EXPAND_ENTITY_TYPES', '').strip()
    GRAPH_SEARCH_EXPAND_ENTITY_TYPES = [
        x.strip() for x in _expand_types_raw.split(',') if x.strip()
    ]
    GRAPH_SEARCH_RERANK_TOP_M = int(os.environ.get('GRAPH_SEARCH_RERANK_TOP_M', '0'))
    GRAPH_SEARCH_RERANK_BOOST = float(os.environ.get('GRAPH_SEARCH_RERANK_BOOST', '0.15'))
    _emb_ttl = os.environ.get('EMBEDDING_CACHE_TTL_SEC', '0').strip()
    EMBEDDING_CACHE_TTL_SEC = float(_emb_ttl) if _emb_ttl else 0.0
    _src_ttl = os.environ.get('GRAPH_SEARCH_RESULT_CACHE_TTL_SEC', '0').strip()
    GRAPH_SEARCH_RESULT_CACHE_TTL_SEC = float(_src_ttl) if _src_ttl else 0.0

    # Phase 14 — throughput / jobs (TASK-046–048)
    GRAPH_JOB_PERSIST_DIR = os.environ.get('GRAPH_JOB_PERSIST_DIR', '').strip()
    LLM_INGEST_MAX_CONCURRENT = int(os.environ.get('LLM_INGEST_MAX_CONCURRENT', '2'))
    GRAPH_INGEST_BATCH_SIZE = int(os.environ.get('GRAPH_INGEST_BATCH_SIZE', '3'))

    # Optional NER/RE-only LLM (Phase 9 TASK-024) — unset keys fall back to main LLM_*
    LLM_EXTRACT_BASE_URL = os.environ.get('LLM_EXTRACT_BASE_URL')
    LLM_EXTRACT_MODEL_NAME = os.environ.get('LLM_EXTRACT_MODEL_NAME')
    LLM_EXTRACT_API_KEY = os.environ.get('LLM_EXTRACT_API_KEY')
    _ext_to = os.environ.get('LLM_EXTRACT_HTTP_TIMEOUT_SEC')
    LLM_EXTRACT_HTTP_TIMEOUT_SEC = float(_ext_to) if _ext_to else None

    # OASIS simulation configuration
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')

    # OASIS platform available actions configuration
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]

    # Report Agent configuration
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))

    @classmethod
    def ingest_context_warnings(cls) -> List[str]:
        """
        Phase 9 TASK-026 — heuristic checks: NER prompt + max output vs Ollama num_ctx.
        Ontology size is not measured here; large JSON ontologies need extra headroom.
        """
        out: List[str] = []
        try:
            num_ctx = int(os.environ.get('OLLAMA_NUM_CTX', '8192'))
        except ValueError:
            num_ctx = 8192

        overhead_tokens = 2600
        chunk_tokens_est = max(1, cls.DEFAULT_CHUNK_SIZE // 4)
        prompt_est = overhead_tokens + chunk_tokens_est

        if prompt_est > int(num_ctx * 0.88):
            out.append(
                f"NER base prompt budget (rough est. {prompt_est} tok, excl. ontology) "
                f"is high vs OLLAMA_NUM_CTX={num_ctx}. Increase num_ctx or reduce GRAPH_CHUNK_SIZE."
            )

        if cls.NER_MAX_OUTPUT_TOKENS + prompt_est > num_ctx:
            out.append(
                f"NER_MAX_OUTPUT_TOKENS ({cls.NER_MAX_OUTPUT_TOKENS}) + est. prompt ({prompt_est}) "
                f"> OLLAMA_NUM_CTX ({num_ctx}) — risk of truncated prompt or completion."
            )

        return out

    @classmethod
    def validate(cls) -> Tuple[List[str], List[str]]:
        """Validate required configuration. Returns (errors, warnings)."""
        errors = []
        warnings = []

        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY not configured (set to any non-empty value, e.g. 'ollama')")
        if not cls.NEO4J_URI:
            errors.append("NEO4J_URI not configured")
        if not cls.NEO4J_PASSWORD:
            errors.append("NEO4J_PASSWORD not configured")

        if cls.LLM_BASE_URL and not (
            cls.LLM_BASE_URL.startswith('http://') or cls.LLM_BASE_URL.startswith('https://')
        ):
            errors.append(f"LLM_BASE_URL must start with http:// or https://, got: '{cls.LLM_BASE_URL}'")
        if cls.LLM_EXTRACT_BASE_URL and not (
            cls.LLM_EXTRACT_BASE_URL.startswith('http://')
            or cls.LLM_EXTRACT_BASE_URL.startswith('https://')
        ):
            errors.append(
                f"LLM_EXTRACT_BASE_URL must start with http:// or https://, got: '{cls.LLM_EXTRACT_BASE_URL}'"
            )
        if cls.EMBEDDING_BASE_URL and not (
            cls.EMBEDDING_BASE_URL.startswith('http://') or cls.EMBEDDING_BASE_URL.startswith('https://')
        ):
            errors.append(
                f"EMBEDDING_BASE_URL must start with http:// or https://, got: '{cls.EMBEDDING_BASE_URL}'"
            )

        if cls.GRAPH_BACKEND not in ('neo4j', 'graphiti'):
            errors.append(
                f"GRAPH_BACKEND must be 'neo4j' or 'graphiti', got: {cls.GRAPH_BACKEND!r}"
            )
        if cls.GRAPH_BACKEND == 'graphiti':
            warnings.append(
                "GRAPH_BACKEND=graphiti: Graphiti ingestion uses structured LLM output; "
                "small or non–JSON-schema-capable models often fail extraction."
            )

        if cls.OASIS_DEFAULT_MAX_ROUNDS <= 0:
            errors.append(f"OASIS_DEFAULT_MAX_ROUNDS must be positive, got: {cls.OASIS_DEFAULT_MAX_ROUNDS}")
        if cls.REPORT_AGENT_MAX_TOOL_CALLS <= 0:
            errors.append(
                f"REPORT_AGENT_MAX_TOOL_CALLS must be positive, got: {cls.REPORT_AGENT_MAX_TOOL_CALLS}"
            )

        upload_dir = os.path.abspath(cls.UPLOAD_FOLDER)
        if os.path.exists(upload_dir) and not os.access(upload_dir, os.W_OK):
            errors.append(f"UPLOAD_FOLDER is not writable: {upload_dir}")

        if cls.SECRET_KEY == 'mirofish-secret-key':
            if not cls.DEBUG:
                errors.append("SECRET_KEY is set to the default value. Set a strong secret key for production.")
            else:
                warnings.append("SECRET_KEY is using the default value. Consider setting a unique key.")

        if cls.LLM_EXTRACT_BASE_URL and cls.LLM_EXTRACT_MODEL_NAME:
            warnings.append(
                "Using dedicated extract LLM (LLM_EXTRACT_*). Ensure that server is reachable and model is loaded."
            )
        elif cls.LLM_EXTRACT_BASE_URL or cls.LLM_EXTRACT_MODEL_NAME:
            warnings.append(
                "Only one of LLM_EXTRACT_BASE_URL / LLM_EXTRACT_MODEL_NAME is set; "
                "the other falls back to main LLM_BASE_URL / LLM_MODEL_NAME."
            )

        if cls.NER_LINK_TOP_K < 0:
            errors.append(f"NER_LINK_TOP_K must be >= 0, got: {cls.NER_LINK_TOP_K}")

        warnings.extend(cls.ingest_context_warnings())

        if cls.NER_TWO_PASS:
            warnings.append(
                "NER_TWO_PASS enabled — each chunk uses two LLM calls (entities, then relations); expect ~2× NER latency."
            )

        if not (0.0 <= cls.GRAPH_MERGE_VECTOR_AMBIG_LOW <= 1.0 and 0.0 <= cls.GRAPH_MERGE_VECTOR_THRESHOLD <= 1.0):
            errors.append("GRAPH_MERGE_VECTOR_* thresholds must be between 0 and 1.")
        elif cls.GRAPH_MERGE_VECTOR_AMBIG_LOW > cls.GRAPH_MERGE_VECTOR_THRESHOLD:
            errors.append(
                "GRAPH_MERGE_VECTOR_AMBIG_LOW must be <= GRAPH_MERGE_VECTOR_THRESHOLD "
                f"({cls.GRAPH_MERGE_VECTOR_AMBIG_LOW} > {cls.GRAPH_MERGE_VECTOR_THRESHOLD})."
            )

        if cls.GRAPH_MERGE_LLM_ADJUDICATE and not cls.GRAPH_MERGE_VECTOR_ENABLED:
            warnings.append(
                "GRAPH_MERGE_LLM_ADJUDICATE has no effect unless GRAPH_MERGE_VECTOR_ENABLED=true."
            )

        if cls.ENTITY_SUMMARY_MAX_PER_CHUNK < 0:
            errors.append(f"ENTITY_SUMMARY_MAX_PER_CHUNK must be >= 0, got {cls.ENTITY_SUMMARY_MAX_PER_CHUNK}")

        if cls.GRAPH_QUERY_AS_OF_ISO and parse_iso8601_utc(cls.GRAPH_QUERY_AS_OF_ISO) is None:
            errors.append(
                "GRAPH_QUERY_AS_OF_ISO must be a valid ISO-8601 datetime (e.g. 2025-01-15T12:00:00+00:00)"
            )
        if cls.GRAPH_QUERY_AS_OF_ISO and not cls.GRAPH_TEMPORAL_ENABLED:
            warnings.append(
                "GRAPH_QUERY_AS_OF_ISO is set but GRAPH_TEMPORAL_ENABLED is false — as-of filtering is not applied."
            )

        wsum = cls.SEARCH_VECTOR_WEIGHT + cls.SEARCH_KEYWORD_WEIGHT
        if abs(wsum - 1.0) > 0.02:
            errors.append(
                f"SEARCH_VECTOR_WEIGHT + SEARCH_KEYWORD_WEIGHT must sum to 1.0 (±0.02), got {wsum:.4f}"
            )
        if not (0.0 <= cls.SEARCH_VECTOR_WEIGHT <= 1.0 and 0.0 <= cls.SEARCH_KEYWORD_WEIGHT <= 1.0):
            errors.append("SEARCH_VECTOR_WEIGHT and SEARCH_KEYWORD_WEIGHT must be between 0 and 1.")

        if cls.GRAPH_SEARCH_EXPAND_HOPS < 0 or cls.GRAPH_SEARCH_EXPAND_HOPS > 2:
            errors.append(f"GRAPH_SEARCH_EXPAND_HOPS must be 0, 1, or 2, got {cls.GRAPH_SEARCH_EXPAND_HOPS}")
        if cls.GRAPH_SEARCH_EXPAND_EXTRA < 0:
            errors.append(f"GRAPH_SEARCH_EXPAND_EXTRA must be >= 0, got {cls.GRAPH_SEARCH_EXPAND_EXTRA}")
        if cls.GRAPH_SEARCH_EXPAND_MAX_PER_SEED < 1:
            errors.append(
                f"GRAPH_SEARCH_EXPAND_MAX_PER_SEED must be >= 1, got {cls.GRAPH_SEARCH_EXPAND_MAX_PER_SEED}"
            )
        if cls.GRAPH_SEARCH_RERANK_TOP_M < 0:
            errors.append(f"GRAPH_SEARCH_RERANK_TOP_M must be >= 0, got {cls.GRAPH_SEARCH_RERANK_TOP_M}")
        if cls.GRAPH_SEARCH_RERANK_BOOST < 0:
            errors.append(f"GRAPH_SEARCH_RERANK_BOOST must be >= 0, got {cls.GRAPH_SEARCH_RERANK_BOOST}")
        if cls.EMBEDDING_CACHE_TTL_SEC < 0:
            errors.append(f"EMBEDDING_CACHE_TTL_SEC must be >= 0, got {cls.EMBEDDING_CACHE_TTL_SEC}")
        if cls.GRAPH_SEARCH_RESULT_CACHE_TTL_SEC < 0:
            errors.append(
                f"GRAPH_SEARCH_RESULT_CACHE_TTL_SEC must be >= 0, got {cls.GRAPH_SEARCH_RESULT_CACHE_TTL_SEC}"
            )

        if cls.LLM_INGEST_MAX_CONCURRENT < 0:
            errors.append(
                f"LLM_INGEST_MAX_CONCURRENT must be >= 0 (0 = unlimited), got {cls.LLM_INGEST_MAX_CONCURRENT}"
            )
        if cls.GRAPH_INGEST_BATCH_SIZE < 1:
            errors.append(
                f"GRAPH_INGEST_BATCH_SIZE must be >= 1, got {cls.GRAPH_INGEST_BATCH_SIZE}"
            )
        if cls.GRAPH_JOB_PERSIST_DIR:
            try:
                os.makedirs(cls.GRAPH_JOB_PERSIST_DIR, exist_ok=True)
                if not os.access(cls.GRAPH_JOB_PERSIST_DIR, os.W_OK):
                    errors.append(
                        f"GRAPH_JOB_PERSIST_DIR is not writable: {cls.GRAPH_JOB_PERSIST_DIR}"
                    )
            except OSError as e:
                errors.append(f"GRAPH_JOB_PERSIST_DIR cannot be created: {e}")

        return errors, warnings

    @classmethod
    def effective_graph_query_as_of(cls) -> Optional[str]:
        """Default as-of instant for retrieval when ``GRAPH_QUERY_AS_OF_ISO`` is set."""
        if not cls.GRAPH_QUERY_AS_OF_ISO:
            return None
        return parse_iso8601_utc(cls.GRAPH_QUERY_AS_OF_ISO)
