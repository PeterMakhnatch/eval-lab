"""
DeFi Dashboard API Application

A Flask-based API for managing DeFi dashboard features including
health monitoring and roadmap generation.
"""
from flask import Flask, jsonify, request
from code.blueprints.health import health_bp
from code.blueprints.roadmap import roadmap_bp
from code.utils.validators import validate_roadmap_request

app = Flask(__name__)
app.register_blueprint(health_bp, url_prefix='/api/v1')
app.register_blueprint(roadmap_bp, url_prefix='/api/v1')


@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request", "message": str(error)}), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found", "message": str(error)}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error", "message": "An unexpected error occurred"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
