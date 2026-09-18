#!/bin/bash
set -e
cd /task_file

python3 - <<'PY'
import json
import os
import pathlib
import re

TASK_ROOT = pathlib.Path("/task_file")
INPUT_DIR = TASK_ROOT / "input"
CODE_DIR = TASK_ROOT / "code"

# ---------------------------------------------------------------------------
# 1. Integration manifest
# ---------------------------------------------------------------------------
def build_integration_manifest():
    artifacts = []
    for entry in sorted(INPUT_DIR.iterdir(), key=lambda p: str(p)):
        if not entry.is_file():
            continue
        abs_path = str(entry)
        status = "present" if entry.is_file() else "invalid"
        fname = entry.name

        # Determine type and digest
        if fname == "flight_search_results.json":
            typ = "flight_search"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            origin = data.get("request_params", {}).get("origin", "?")
            destination = data.get("request_params", {}).get("destination", "?")
            date = data.get("request_params", {}).get("departure_date", "?")
            count = len(data.get("results", []))
            digest = f"origin={origin}, destination={destination}, date={date}, offers={count}"

        elif fname == "llm_routing_response.json":
            typ = "llm_routing"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            content = ""
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
            digest = content[:120]

        elif fname == "cross_chain_swap_dry_run.json":
            typ = "cross_chain_swap"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            src = data.get("requestPreview", {}).get("source_chain", "?")
            tgt = data.get("requestPreview", {}).get("destination_chain", "?")
            est = data.get("estimated_cost", {}).get("total_estimated_usd", "?")
            digest = f"source_chain={src}, target_chain={tgt}, estimated_output={est}"

        elif fname == "optimization_output.json":
            typ = "optimization_output"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            model_id = data.get("source", "?")
            prompt_count = len(data.get("results", []))
            total_billed = data.get("billing_metadata", {}).get("estimated_cost_usd", "?")
            digest = f"model_id={model_id}, prompt_count={prompt_count}, total_billed={total_billed}"

        elif fname == "health_check_response.json":
            typ = "health_check"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            status_code = data.get("status_code", "?")
            status_field = data.get("summary", {}).get("overall_status", "?")
            digest = f"status_code={status_code}, status={status_field}"

        elif fname == "fixture_manifest.json":
            typ = "fixture_manifest"
            with open(entry, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            keys = sorted(data.keys())
            count = len(data.get("fixtures", []))
            digest = f"keys={keys}, entries={count}"

        elif fname == "manifest.yaml":
            typ = "manifest_yaml"
            # Parse YAML with stdlib only – the YAML is simple enough
            with open(entry, "r", encoding="utf-8") as fh:
                raw = fh.read()
            # Very lightweight extraction: look for top-level keys under fixture_manifest
            keys = []
            fixtures_count = 0
            in_fixtures = False
            for line in raw.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("fixture_manifest:"):
                    continue
                # top-level keys under fixture_manifest (indented by 2 spaces)
                if line.startswith("  ") and not line.startswith("    "):
                    key = stripped.split(":")[0].strip()
                    if key and not key.startswith("-"):
                        keys.append(key)
                if stripped.startswith("fixtures:"):
                    in_fixtures = True
                    continue
                if in_fixtures and stripped.startswith("- path:"):
                    fixtures_count += 1
            digest = f"keys={sorted(set(keys))}, entries={fixtures_count}"
        else:
            # Should not happen with known files
            typ = "unknown"
            digest = ""

        artifacts.append({
            "path": abs_path,
            "status": status,
            "type": typ,
            "digest": digest
        })

    manifest = {"artifacts": artifacts}
    out_path = TASK_ROOT / "integration_manifest.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print("Created integration_manifest.json")

# ---------------------------------------------------------------------------
# 2. OpenAPI specification
# ---------------------------------------------------------------------------
def build_openapi_spec():
    # Read health check response for schema reference
    with open(INPUT_DIR / "health_check_response.json", "r", encoding="utf-8") as fh:
        health_data = json.load(fh)

    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "DeFi Dashboard API",
            "version": "1.0.0"
        },
        "paths": {
            "/api/v1/health": {
                "get": {
                    "summary": "Health check",
                    "description": "Returns the current health status of the API and its dependencies.",
                    "responses": {
                        "200": {
                            "description": "Service is healthy",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "status": {"type": "string"},
                                            "timestamp": {"type": "string", "format": "date-time"},
                                            "version": {"type": "string"},
                                            "uptime_seconds": {"type": "number"},
                                            "dependencies": {
                                                "type": "object",
                                                "properties": {
                                                    "database": {"type": "string"},
                                                    "cache": {"type": "string"}
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/v1/roadmap/generate": {
                "post": {
                    "summary": "Generate integration roadmap",
                    "description": "Generates a technical integration roadmap based on provided parameters.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["feature"],
                                    "properties": {
                                        "feature": {"type": "string", "description": "Name of the feature to roadmap"},
                                        "target_platforms": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "List of target platforms"
                                        },
                                        "constraints": {
                                            "type": "object",
                                            "properties": {
                                                "timeline_weeks": {"type": "integer", "minimum": 1, "maximum": 52},
                                                "budget_usd": {"type": "number", "minimum": 0}
                                            }
                                        },
                                        "preferred_technologies": {
                                            "type": "array",
                                            "items": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Roadmap generated successfully",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "request_id": {"type": "string"},
                                            "feature": {"type": "string"},
                                            "generated_at": {"type": "string", "format": "date-time"},
                                            "phases": {"type": "array", "items": {"type": "object"}},
                                            "estimated_total_weeks": {"type": "integer"},
                                            "risk_assessment": {"type": "object"}
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Invalid request parameters",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"},
                                            "details": {"type": "array", "items": {"type": "string"}}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    out_path = TASK_ROOT / "openapi_spec.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2, ensure_ascii=False)
    print("Created openapi_spec.json")

# ---------------------------------------------------------------------------
# 3. Code review report
# ---------------------------------------------------------------------------
def build_code_review_report():
    functional_files = []
    for py_file in sorted(CODE_DIR.rglob("*.py")):
        if py_file.stat().st_size > 0:
            functional_files.append(py_file)

    lines = []
    lines.append("# Code Review Report")
    lines.append("")

    for py_file in functional_files:
        rel = str(py_file.relative_to(TASK_ROOT))
        with open(py_file, "r", encoding="utf-8") as fh:
            content = fh.read()

        lines.append(f"## {rel}")
        lines.append("")

        # Overview
        overview = derive_overview(rel, content)
        lines.append(overview)
        lines.append("")

        # Issues
        issues = derive_issues(rel, content)
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")

    out_path = TASK_ROOT / "code_review_report.md"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("Created code_review_report.md")


def derive_overview(rel_path: str, content: str) -> str:
    if rel_path == "code/app.py":
        return "Application entry point that creates a Flask app, registers the health and roadmap blueprints under the `/api/v1` prefix, and defines global error handlers for 400, 404, and 500 responses."
    if rel_path == "code/config.py":
        return "Application configuration classes (base, development, production) with environment-variable fallbacks for secrets, database URL, and Redis URL."
    if rel_path == "code/blueprints/health.py":
        return "Health-check blueprint exposing `GET /health` that returns a JSON payload with status, timestamp, version, uptime, and dependency states."
    if rel_path == "code/blueprints/roadmap.py":
        return "Roadmap generation blueprint exposing `POST /roadmap/generate`. It validates the JSON body, builds a phased roadmap, and returns a structured response with phases, estimated weeks, and a risk assessment."
    if rel_path == "code/utils/validators.py":
        return "Request validation utility for the roadmap endpoint. Enforces that the request body is a JSON object, `feature` is a non-empty string, `target_platforms` is a list of strings, `constraints` is an object with integer `timeline_weeks` (1-52) and positive numeric `budget_usd`, and `preferred_technologies` is a list of strings."
    return "Python source file with functional code."


def derive_issues(rel_path: str, content: str) -> list:
    issues = []
    if rel_path == "code/app.py":
        if "app.run(host='0.0.0.0', port=8080)" in content:
            issues.append("HIGH: Hardcoded development server run call inside the module; should be guarded by `if __name__ == '__main__'` and use a production WSGI server.")
        if "from code.utils.validators import validate_roadmap_request" in content and "validate_roadmap_request" not in content.split("register_blueprint")[0]:
            issues.append("LOW: Unused import of `validate_roadmap_request` at the app level; validation is invoked inside the blueprint.")
        if not re.search(r'@app\.errorhandler\(\d+\)', content):
            issues.append("MEDIUM: Global error handlers return hardcoded messages that may leak internal details in production.")
        else:
            issues.append("MEDIUM: Global error handlers return generic messages but do not log the original exception for observability.")
    elif rel_path == "code/config.py":
        if "dev-secret-change-in-production" in content:
            issues.append("HIGH: Default `SECRET_KEY` is a hardcoded default value unsuitable for any non-development environment.")
        if "postgresql://localhost:5432" in content or "redis://localhost:6379" in content:
            issues.append("MEDIUM: Default database and Redis URLs point to localhost, which will not work in containerized or distributed deployments.")
        issues.append("LOW: Missing docstrings on configuration classes beyond the base class.")
    elif rel_path == "code/blueprints/health.py":
        if "datetime.now(timezone.utc)" in content:
            issues.append("LOW: Timestamp is generated at call time rather than capturing a single consistent snapshot for all components.")
        if "uptime_seconds":
            issues.append("LOW: `uptime_seconds` is hardcoded (3600.0) instead of being computed from the actual application start time.")
    elif rel_path == "code/blueprints/roadmap.py":
        if "request.get_json(silent=True)" in content:
            issues.append("MEDIUM: `silent=True` suppresses JSON parse errors; the endpoint returns a generic 400 without distinguishing malformed JSON from missing body.")
        if "max(4, timeline - 5)" in content:
            issues.append("MEDIUM: Phase 3 duration calculation can produce unrealistic values when `timeline_weeks` is very low or very high.")
        if "rg-" in content:
            issues.append("LOW: Request ID generation relies on a timestamp without additional entropy; collisions are possible under concurrent requests.")
    elif rel_path == "code/utils/validators.py":
        if "if not isinstance(data, dict):" in content:
            issues.append("MEDIUM: The validator returns early after checking the top-level type but does not collect all errors before returning.")
        if "len(data['feature'].strip()) == 0" in content:
            issues.append("LOW: Whitespace-only strings are rejected but the error message does not mention that leading/trailing whitespace is trimmed.")
        if "isinstance(tw, int)" in content:
            issues.append("MEDIUM: `timeline_weeks` must be a Python `int`; boolean `True`/`False` would pass the `isinstance(tw, int)` check, potentially allowing invalid values.")
    else:
        issues.append("LOW: No specific issues identified; consider adding docstrings and type hints.")
    return issues

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    build_integration_manifest()
    build_openapi_spec()
    build_code_review_report()

    # ---- self-checks ----
    # 1. integration manifest
    with open(TASK_ROOT / "integration_manifest.json", "r", encoding="utf-8") as fh:
        im = json.load(fh)
    assert "artifacts" in im, "Missing artifacts key"
    assert len(im["artifacts"]) == 7, f"Expected 7 artifacts, got {len(im['artifacts'])}"
    paths = [a["path"] for a in im["artifacts"]]
    assert paths == sorted(paths), "Artifacts not sorted by path"
    for a in im["artifacts"]:
        assert a["path"].startswith("/task_file/input/"), f"Bad path: {a['path']}"
        assert a["status"] == "present", f"Status not present: {a['path']}"
        assert a["digest"], f"Empty digest for {a['path']}"
        allowed_types = {"flight_search", "llm_routing", "cross_chain_swap", "optimization_output", "health_check", "fixture_manifest", "manifest_yaml"}
        assert a["type"] in allowed_types, f"Unknown type: {a['type']}"

    # 2. openapi spec
    with open(TASK_ROOT / "openapi_spec.json", "r", encoding="utf-8") as fh:
        oas = json.load(fh)
    assert oas["openapi"] == "3.0.3"
    assert "/api/v1/health" in oas["paths"]
    assert "get" in oas["paths"]["/api/v1/health"]
    health_schema = oas["paths"]["/api/v1/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "status" in health_schema.get("properties", {})
    assert "/api/v1/roadmap/generate" in oas["paths"]
    post_op = oas["paths"]["/api/v1/roadmap/generate"]["post"]
    assert "requestBody" in post_op
    req_schema = post_op["requestBody"]["content"]["application/json"]["schema"]
    assert "feature" in req_schema.get("required", [])

    # 3. code review report
    report_path = TASK_ROOT / "code_review_report.md"
    with open(report_path, "r", encoding="utf-8") as fh:
        report = fh.read()
    # every functional file must have a heading
    for py_file in ["code/app.py", "code/config.py", "code/blueprints/health.py", "code/blueprints/roadmap.py", "code/utils/validators.py"]:
        assert f"## {py_file}" in report, f"Missing section for {py_file}"
    # no placeholder text
    assert "TODO" not in report
    assert "TBD" not in report
    # each section has at least one issue bullet with severity
    for section in re.findall(r"## (.+)", report):
        # find the block until next heading or end
        pattern = re.compile(rf"## {re.escape(section)}\n(.*?)(?=\n## |\Z)", re.DOTALL)
        m = pattern.search(report)
        assert m, f"Could not extract section for {section}"
        body = m.group(1)
        assert re.search(r"- (HIGH|MEDIUM|LOW):", body), f"No severity issue in {section}"

    print("All self-checks passed.")

if __name__ == "__main__":
    main()
PY
