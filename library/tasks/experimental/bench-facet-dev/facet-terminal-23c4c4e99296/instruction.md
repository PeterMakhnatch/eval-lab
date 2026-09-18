# Objective

Produce three self-contained deliverables by consolidating, cross-validating, and analyzing the local fixture files and source code under `/task_file`. All work must rely exclusively on the provided local snapshots; no network calls or external services are permitted.

# Inputs

Every file listed below is present under `/task_file` and must be treated as an authoritative local snapshot. The file roles are assigned as follows:

## Service response and artifact snapshots (`/task_file/input/`)

- `/task_file/input/flight_search_results.json` — primary source: flight-search API response snapshot listing available flights between an origin and a beach destination for a specific date.
- `/task_file/input/llm_routing_response.json` — primary source: smart-routing LLM service response containing a generated content field.
- `/task_file/input/cross_chain_swap_dry_run.json` — primary source: cross-chain swap dry-run detail snapshot.
- `/task_file/input/optimization_output.json` — primary source: optimization output containing platform-specific prompt strings and billing metadata.
- `/task_file/input/health_check_response.json` — primary source: health-check response confirming a 200 OK status.
- `/task_file/input/fixture_manifest.json` — supporting evidence: pre-existing manifest enumerating some of the input artifacts.
- `/task_file/input/manifest.yaml` — supporting evidence: YAML-formatted manifest providing provenance or metadata for the input artifacts.

## Project source code (`/task_file/code/`)

- `/task_file/code/app.py` — primary source: application entry point and route registration.
- `/task_file/code/config.py` — configuration and provenance metadata: application configuration values.
- `/task_file/code/blueprints/health.py` — primary source: health endpoint implementation.
- `/task_file/code/blueprints/roadmap.py` — primary source: roadmap generation POST endpoint implementation.
- `/task_file/code/utils/validators.py` — primary source: request validation utilities used by the endpoints.
- `/task_file/code/__init__.py` — package marker (empty, no functional role).
- `/task_file/code/blueprints/__init__.py` — package marker (empty, no functional role).
- `/task_file/code/utils/__init__.py` — package marker (empty, no functional role).

# Required outputs

## 1. Integration manifest

- **Path:** `/task_file/integration_manifest.json`
- **Format:** Valid JSON object.

### Required content

The manifest must contain a top-level key `"artifacts"` whose value is an array of objects. Every artifact discovered across the entire `/task_file/input/` directory must be represented by exactly one entry. Each entry must include the following keys:

- `"path"`: the absolute path of the artifact file as a string.
- `"status"`: a string set to `"present"` if the file exists and is readable, or `"invalid"` if the file is unreadable or missing (though all listed inputs are expected to be present).
- `"type"`: a string describing the artifact category. Allowed values are `"flight_search"`, `"llm_routing"`, `"cross_chain_swap"`, `"optimization_output"`, `"health_check"`, `"fixture_manifest"`, or `"manifest_yaml"`.
- `"digest"`: a string containing a brief content excerpt or summary. The excerpt must be derived from the actual file content:
  - For `/task_file/input/flight_search_results.json`, include the `"origin"`, `"destination"`, `"date"`, and the count of flight offers found.
  - For `/task_file/input/llm_routing_response.json`, include the first 120 characters of the generated content field.
  - For `/task_file/input/cross_chain_swap_dry_run.json`, include the `"source_chain"`, `"target_chain"`, and `"estimated_output"` values.
  - For `/task_file/input/optimization_output.json`, include the `"model_id"` or equivalent identifier, the count of prompt strings, and the `"total_billed"` amount.
  - For `/task_file/input/health_check_response.json`, include the HTTP status code and the `"status"` field from the response body.
  - For `/task_file/input/fixture_manifest.json` and `/task_file/input/manifest.yaml`, include the top-level keys present (as a list) and the number of entries they describe.

### Ordering and completeness

- The entries in the `"artifacts"` array must be sorted alphabetically by the `"path"` field.
- The array must contain exactly seven entries, one per file in `/task_file/input/`.
- No artifact entry may be omitted.

### Verification constraints

- The output file must be parseable as JSON.
- The `"artifacts"` array must be present and contain exactly seven objects.
- Every `"path"` must be an absolute path starting with `/task_file/input/`.
- Every `"status"` must equal `"present"`.
- The `"type"` field must match one of the allowed values listed above.
- The `"digest"` field must not be empty and must contain the required excerpt components for the corresponding file type.

---

## 2. OpenAPI specification

- **Path:** `/task_file/openapi_spec.json`
- **Format:** Valid JSON document conforming to the OpenAPI 3.0.3 schema.

### Required content

The specification must be derived exclusively from the source code under `/task_file/code/`. It must include the following:

- `openapi`: set to `"3.0.3"`.
- `info`: an object containing at least `title` (derived from the application name found in the code or a sensible default) and `version` (set to `"1.0.0"`).
- `paths`: an object documenting every route exposed by the application as implemented in the code, with particular attention to:
  - The **health endpoint** (inferred from `/task_file/code/blueprints/health.py`). The specification must include the HTTP method, path, summary, and the expected `200` response structure. The response schema must reflect the structure found in `/task_file/input/health_check_response.json`.
  - The **roadmap generation POST endpoint** (inferred from `/task_file/code/blueprints/roadmap.py`). The specification must include the HTTP method, path, summary, request body schema (derived from validators in `/task_file/code/utils/validators.py`), and the expected success and error response schemas.
- Any additional endpoints discovered in the code must also be documented with the same level of detail.

### Source-to-output mapping

- Route definitions and docstrings in `/task_file/code/app.py`, `/task_file/code/blueprints/health.py`, and `/task_file/code/blueprints/roadmap.py` provide the endpoints, methods, and descriptions.
- `/task_file/code/utils/validators.py` defines the request body schemas and constraints for the roadmap POST endpoint.
- `/task_file/input/health_check_response.json` provides the concrete response shape for the health endpoint.

### Verification constraints

- The output file must be parseable as JSON.
- The top-level `openapi` field must be `"3.0.3"`.
- The `paths` object must contain at least the `/health` GET endpoint and the roadmap generation POST endpoint.
- The health endpoint response schema must include a `status` property of type string.
- The roadmap POST endpoint must include a `requestBody` with an `application/json` content type and a schema that requires at least one input field (as enforced by the validators).

---

## 3. Code review report

- **Path:** `/task_file/code_review_report.md`
- **Format:** Valid Markdown.

### Required content

The report must contain a per-file review of every Python file under `/task_file/code/` that contains functional code (files with `size_bytes > 0`). The empty `__init__.py` files may be omitted. Each reviewed file must have its own section with a heading matching the relative path from `/task_file/` (e.g., `## code/app.py`).

For each file section, include:

- A brief **overview** paragraph summarizing the file’s purpose based on its contents.
- A bullet list of **issues**, where each issue is assigned a severity level of `HIGH`, `MEDIUM`, or `LOW`. Issues must be concrete and grounded in observable patterns in the code, such as:
  - Missing input validation.
  - Hardcoded secrets or URLs.
  - Unhandled exceptions.
  - Missing docstrings on public functions.
  - Inconsistent error response formats.
  - Use of mutable default arguments.
  - Any deviation from typical Flask/Blueprint best practices visible in the code.

### Ordering

- File sections must appear in alphabetical order by the relative path.

### Verification constraints

- The report must be a valid Markdown file.
- Every functional Python file under `/task_file/code/` must have a dedicated section with a heading that contains its relative path.
- Each file section must contain at least one issue bullet.
- Every issue bullet must start with a severity label in uppercase (`HIGH`, `MEDIUM`, or `LOW`) followed by a colon.
- The report must not contain placeholder text or generic comments; all observations must reference specific code constructs found in the files.

---

# Global constraints

- All output paths are absolute and must be created under `/task_file/`.
- Do not modify any existing input file unless explicitly required (this task does not require in-place modification of inputs).
- Do not include embedded images, external links, or references to resources outside `/task_file/`.
- All deliverables must be self-contained and derived solely from the provided local snapshots and source code.
