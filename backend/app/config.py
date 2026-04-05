"""
Configuration Management
Loads configuration from .env file in project root directory
"""

import os
from dotenv import load_dotenv

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
    NEO4J_MAX_CONNECTION_POOL_SIZE = int(os.environ.get('NEO4J_MAX_POOL_SIZE', '50'))
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT = int(os.environ.get('NEO4J_ACQUISITION_TIMEOUT', '60'))

    # Embedding configuration
    EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'nomic-embed-text')
    EMBEDDING_BASE_URL = os.environ.get('EMBEDDING_BASE_URL', 'http://localhost:11434')

    # File upload configuration
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}

    # Text processing configuration
    DEFAULT_CHUNK_SIZE = 500  # Default chunk size
    DEFAULT_CHUNK_OVERLAP = 50  # Default overlap size
    NER_MAX_OUTPUT_TOKENS = int(os.environ.get('NER_MAX_OUTPUT_TOKENS', '8192'))

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
    def validate(cls):
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
        if cls.EMBEDDING_BASE_URL and not (
            cls.EMBEDDING_BASE_URL.startswith('http://') or cls.EMBEDDING_BASE_URL.startswith('https://')
        ):
            errors.append(
                f"EMBEDDING_BASE_URL must start with http:// or https://, got: '{cls.EMBEDDING_BASE_URL}'"
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

        return errors, warnings
