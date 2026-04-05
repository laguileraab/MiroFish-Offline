"""
API Routes Module
"""

import traceback
from flask import Blueprint, jsonify

from ..config import Config


def safe_error_response(error: Exception, status_code: int = 500):
    """Create a safe JSON error response.

    In DEBUG mode, includes the full traceback for development convenience.
    In production, returns only error message to avoid leaking internal paths.
    """
    if Config.DEBUG:
        return jsonify({
            "success": False,
            "error": str(error),
            "traceback": traceback.format_exc()
        }), status_code
    return jsonify({
        "success": False,
        "error": str(error)
    }), status_code


graph_bp = Blueprint('graph', __name__)
simulation_bp = Blueprint('simulation', __name__)
report_bp = Blueprint('report', __name__)

from . import graph  # noqa: E402, F401
from . import simulation  # noqa: E402, F401
from . import report  # noqa: E402, F401
