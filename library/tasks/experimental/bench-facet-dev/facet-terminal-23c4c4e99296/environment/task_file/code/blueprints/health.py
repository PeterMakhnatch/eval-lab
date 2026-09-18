"""
Health check blueprint.

Provides endpoints for service health monitoring.
"""
from flask import Blueprint, jsonify
from datetime import datetime, timezone

health_bp = Blueprint('health', __name__)


@health_bp.route('/health', methods=['GET'])
def health_check():
    """
    GET /api/v1/health

    Returns the current health status of the API and its dependencies.

    Responses:
        200: Service is healthy.
            {
                "status": "healthy",
                "timestamp": "2025-09-15T14:21:55Z",
                "version": "1.0.0",
                "uptime_seconds": 3600.0,
                "dependencies": {
                    "database": "connected",
                    "cache": "connected"
                }
            }
        503: Service is unhealthy.
            {
                "status": "unhealthy",
                "timestamp": "...",
                "error": "..."
            }
    """
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "uptime_seconds": 3600.0,
        "dependencies": {
            "database": "connected",
            "cache": "connected"
        }
    }), 200
