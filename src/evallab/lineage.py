"""Lineage walker for generated artifacts (E14).

Recursively traces the lineage of generated artifacts back to Zone 1
(immutable evidence), validating recorded content digests and detecting
cycles, missing inputs, and unrecorded derivations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml

from evallab.attach import attach
from evallab.contextpack import parse_front_matter


@dataclass(frozen=True)
class LineageNode:
    """A single node in an artifact lineage graph."""

    target: str
    path: str | None
    zone: str
    digest: str | None
    status: str
    resolved: bool
    reason: str | None = None
    expected_digest: str | None = None
    actual_digest: str | None = None
    inputs: tuple[LineageNode, ...] = ()


def classify_zone(path_or_str: str | Path) -> str:
    """Classify the storage/provenance zone of a path or target.

    Zones:
    - z1: Immutable evidence (runs/, research/evidence/, library/benchmarks/_trajectories/)
    - z2: PostgreSQL catalog
    - z3: Parquet analytics (derived/parquet/, derived/, *.parquet)
    - z4: Knowledge docs (docs/, research/ (non-evidence), *.md)
    - z5: Coordination (board/, agents/)
    - unknown: Fallback
    """
    p_str = path_or_str.as_posix() if isinstance(path_or_str, Path) else str(path_or_str)
    p_str = p_str.replace("\\", "/")

    # Check Z1 first (immutable evidence)
    if (
        p_str.startswith("runs/")
        or "/runs/" in p_str
        or p_str.startswith("research/evidence/")
        or "/research/evidence/" in p_str
        or p_str.startswith("library/benchmarks/_trajectories/")
        or "/library/benchmarks/_trajectories/" in p_str
    ):
        return "z1"

    # Check Z3 (Parquet / derived)
    if p_str.startswith("derived/") or "/derived/" in p_str or p_str.endswith(".parquet"):
        return "z3"

    # Check Z4 (Knowledge / docs)
    if (
        p_str.startswith("docs/")
        or "/docs/" in p_str
        or p_str.startswith("research/")
        or "/research/" in p_str
        or p_str.endswith(".md")
    ):
        return "z4"

    # Check Z5 (Coordination)
    if (
        p_str.startswith("board/")
        or "/board/" in p_str
        or p_str.startswith("agents/")
        or "/agents/" in p_str
    ):
        return "z5"

    if p_str.startswith("sql/") or "/sql/" in p_str or p_str.startswith("catalog/"):
        return "z2"

    return "unknown"


def normalize_digest(digest: str | None) -> str | None:
    """Normalize a content digest string to sha256:<64 hex> format."""
    if digest is None:
        return None
    d = digest.strip()
    if d.startswith("sha256:"):
        return d.lower()
    if len(d) == 64 and all(c in "0123456789abcdefABCDEF" for c in d):
        return f"sha256:{d.lower()}"
    return d


def compute_file_digest(path: Path) -> str:
    """Compute sha256:<hex> content digest for a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _rel_path(path: Path, repo_root: Path) -> str:
    """Return repo-relative path if inside repo_root, else posix path."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, RuntimeError):
        return path.as_posix()


def resolve_target(
    target: str,
    repo_root: Path,
    explicit_derived: Path | None = None,
    duckdb_conn: duckdb.DuckDBPyConnection | None = None,
) -> tuple[Path | None, str, str | None]:
    """Resolve a target path or ID to (resolved_path, zone, entity_id).

    1. Direct filesystem check relative to repo_root or explicit_derived or absolute.
    2. DuckDB attach query for knowledge docs (z4) or derived tables (z3/z2).
    3. Direct check under runs/ or research/evidence/runs/ for job/trial directories.
    """
    # 1. Direct path check
    candidates = [
        repo_root / target,
        Path(target),
    ]
    if explicit_derived is not None:
        candidates.append(explicit_derived / target)

    for c in candidates:
        if c.is_file():
            zone = classify_zone(_rel_path(c, repo_root))
            return c, zone, None
        if c.is_dir():
            # If directory in Z1 (e.g. trial directory or job directory)
            zone = classify_zone(_rel_path(c, repo_root))
            if zone == "z1":
                # Check for canonical manifest or result.json
                for sub in ("manifest.json", "result.json", "rollout.json", "PROMOTION.json"):
                    if (c / sub).is_file():
                        return c / sub, "z1", None
                return c, "z1", None

    # 2. Query DuckDB attached surface if available
    if duckdb_conn is not None:
        try:
            # Query z4.front_matter by path or title
            rows = duckdb_conn.execute(
                "SELECT path FROM z4.front_matter WHERE path = ? OR title = ? LIMIT 1",
                (target, target),
            ).fetchall()
            if rows and rows[0][0]:
                doc_path = repo_root / rows[0][0]
                if doc_path.is_file():
                    return doc_path, "z4", None
        except Exception:
            pass

    # 3. Z1 directory scan by job/trial ID
    z1_dirs = [
        repo_root / "runs" / target,
        repo_root / "research" / "evidence" / "runs" / target,
    ]
    for zd in z1_dirs:
        if zd.is_file():
            return zd, "z1", target
        if zd.is_dir():
            for sub in ("manifest.json", "result.json", "rollout.json", "PROMOTION.json"):
                if (zd / sub).is_file():
                    return zd / sub, "z1", target
            return zd, "z1", target

    return None, classify_zone(target), None


def _parse_input_items(raw_inputs: Any) -> list[dict[str, Any]]:
    """Normalize raw inputs from front-matter or JSON into standard dicts."""
    results: list[dict[str, Any]] = []
    if not isinstance(raw_inputs, (list, tuple)):
        return results

    for item in raw_inputs:
        if isinstance(item, dict):
            path_val = item.get("path") or item.get("source_uri") or item.get("target")
            id_val = item.get("id") or item.get("item_id") or item.get("trial_id")
            digest_val = (
                item.get("digest")
                or item.get("sha256")
                or item.get("material_digest")
                or item.get("evidence_sha256")
            )
            results.append(
                {
                    "path": str(path_val) if path_val is not None else None,
                    "id": str(id_val) if id_val is not None else None,
                    "digest": str(digest_val) if digest_val is not None else None,
                }
            )
        elif isinstance(item, str):
            results.append({"path": item, "id": None, "digest": None})
    return results


def read_artifact_inputs(
    file_path: Path,
    zone: str,
    repo_root: Path,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Read the `inputs: [{path|id, digest}]` contract from an artifact.

    Returns (status, inputs_list, reason):
    - ("ok", inputs, None)
    - ("unrecorded", [], reason)
    """
    if not file_path.is_file():
        return "unrecorded", [], f"artifact is not a regular file: {file_path}"

    ext = file_path.suffix.lower()

    # Markdown artifacts (Z4 front-matter)
    if ext == ".md":
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return "unrecorded", [], f"unable to read markdown file: {exc}"

        fm, _ = parse_front_matter(content)
        if fm is None:
            return "unrecorded", [], "no front-matter in markdown artifact"

        if "inputs" in fm:
            raw_inputs = fm["inputs"]
            if not isinstance(raw_inputs, list):
                return "unrecorded", [], "inputs field in front-matter is not a list"
            return "ok", _parse_input_items(raw_inputs), None

        return "unrecorded", [], "no inputs field declared in front-matter"

    # JSON artifacts (Z3 / sidecars)
    if ext == ".json":
        try:
            content = file_path.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            return "unrecorded", [], f"unable to parse JSON artifact: {exc}"

        if isinstance(data, dict):
            if "inputs" in data:
                raw_inputs = data["inputs"]
                if not isinstance(raw_inputs, list):
                    return "unrecorded", [], "inputs field in JSON artifact is not a list"
                return "ok", _parse_input_items(raw_inputs), None

            # Check ProvenanceMetadata sidecar format
            if isinstance(data.get("parent_digests"), list) and data["parent_digests"]:
                items = [
                    {"path": None, "id": None, "digest": d} for d in data["parent_digests"]
                ]
                return "ok", items, None

        # Check for sidecar file beside it
        sidecar_path = file_path.with_suffix(".provenance.json")
        if sidecar_path.is_file():
            try:
                sc_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
                if isinstance(sc_data, dict) and "inputs" in sc_data:
                    return "ok", _parse_input_items(sc_data["inputs"]), None
            except Exception:
                pass

        return "unrecorded", [], "no inputs field in JSON artifact"

    # Parquet artifacts
    if ext == ".parquet":
        # Check Parquet sidecar JSON files
        manifest_path = file_path.parent / "_MANIFEST.json"
        if manifest_path.is_file():
            try:
                m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(m_data, dict) and "inputs" in m_data:
                    return "ok", _parse_input_items(m_data["inputs"]), None
                if isinstance(m_data, dict) and "source_files" in m_data:
                    items = [
                        {"path": f, "id": None, "digest": None} for f in m_data["source_files"]
                    ]
                    return "ok", items, None
            except Exception:
                pass

        sidecar_json = file_path.with_suffix(".json")
        if sidecar_json.is_file():
            try:
                sc_data = json.loads(sidecar_json.read_text(encoding="utf-8"))
                if isinstance(sc_data, dict) and "inputs" in sc_data:
                    return "ok", _parse_input_items(sc_data["inputs"]), None
            except Exception:
                pass

        try:
            import pyarrow.parquet as pq

            schema = pq.read_schema(file_path)
            metadata = schema.metadata or {}
            if b"inputs" in metadata:
                inputs_raw = json.loads(metadata[b"inputs"].decode("utf-8"))
                return "ok", _parse_input_items(inputs_raw), None
        except Exception:
            pass

        return "unrecorded", [], "no inputs metadata found for parquet artifact"

    # YAML artifacts
    if ext in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "inputs" in data and isinstance(data["inputs"], list):
                return "ok", _parse_input_items(data["inputs"]), None
        except Exception:
            pass
        return "unrecorded", [], "no inputs field in YAML artifact"

    return "unrecorded", [], f"artifact type {ext} does not declare inputs"


def _walk_lineage(
    target: str,
    *,
    repo_root: Path,
    explicit_derived: Path | None = None,
    expected_digest: str | None = None,
    ancestors: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int = 32,
    duckdb_conn: duckdb.DuckDBPyConnection | None = None,
) -> LineageNode:
    """Recursively walk lineage of target, stopping at Zone 1 (immutable evidence)."""
    # 1. Target resolution
    resolved_path, zone, _ = resolve_target(target, repo_root, explicit_derived, duckdb_conn)

    rel_path = _rel_path(resolved_path, repo_root) if resolved_path is not None else None
    canonical_id = rel_path if rel_path is not None else target

    # 2. Cycle detection
    if canonical_id in ancestors:
        return LineageNode(
            target=target,
            path=rel_path,
            zone=zone,
            digest=None,
            status="cycle",
            resolved=False,
            reason=f"cycle detected: {canonical_id} is in ancestor chain",
            expected_digest=expected_digest,
        )

    # 3. Depth bound
    if depth >= max_depth:
        return LineageNode(
            target=target,
            path=rel_path,
            zone=zone,
            digest=None,
            status="depth_exceeded",
            resolved=False,
            reason=f"max recursion depth ({max_depth}) exceeded",
            expected_digest=expected_digest,
        )

    # 4. Nonexistent target
    if resolved_path is None:
        return LineageNode(
            target=target,
            path=None,
            zone="unknown",
            digest=None,
            status="not_found",
            resolved=False,
            reason=f"target not found: {target}",
            expected_digest=expected_digest,
        )

    # 5. Digest computation and verification
    actual_digest = compute_file_digest(resolved_path)

    if expected_digest is not None:
        norm_expected = normalize_digest(expected_digest)
        norm_actual = normalize_digest(actual_digest)
        if norm_expected != norm_actual:
            return LineageNode(
                target=target,
                path=rel_path,
                zone=zone,
                digest=actual_digest,
                status="digest_mismatch",
                resolved=False,
                reason=f"digest mismatch: expected {expected_digest}, actual {actual_digest}",
                expected_digest=expected_digest,
                actual_digest=actual_digest,
            )

    # 6. Terminal base case: Zone 1 (immutable evidence)
    if zone == "z1":
        return LineageNode(
            target=target,
            path=rel_path,
            zone="z1",
            digest=actual_digest,
            status="terminal",
            resolved=True,
            expected_digest=expected_digest,
            actual_digest=actual_digest,
            inputs=(),
        )

    # 7. Read inputs contract from derived artifact
    inputs_status, inputs_list, reason = read_artifact_inputs(resolved_path, zone, repo_root)
    if inputs_status != "ok":
        return LineageNode(
            target=target,
            path=rel_path,
            zone=zone,
            digest=actual_digest,
            status="unrecorded",
            resolved=False,
            reason=reason,
            expected_digest=expected_digest,
            actual_digest=actual_digest,
            inputs=(),
        )

    # 8. Recurse over recorded inputs
    next_ancestors = (*ancestors, canonical_id)
    child_nodes: list[LineageNode] = []

    for item in inputs_list:
        child_target = item.get("path") or item.get("id") or ""
        if not child_target and item.get("digest"):
            child_target = str(item["digest"])
        child_digest = item.get("digest")

        child_node = _walk_lineage(
            child_target,
            repo_root=repo_root,
            explicit_derived=explicit_derived,
            expected_digest=child_digest,
            ancestors=next_ancestors,
            depth=depth + 1,
            max_depth=max_depth,
            duckdb_conn=duckdb_conn,
        )
        child_nodes.append(child_node)

    # Deterministic child ordering: by (path or target, digest)
    child_nodes.sort(key=lambda c: (c.path or c.target, c.digest or ""))

    all_resolved = all(c.resolved for c in child_nodes)
    node_status = "resolved"

    return LineageNode(
        target=target,
        path=rel_path,
        zone=zone,
        digest=actual_digest,
        status=node_status,
        resolved=all_resolved,
        expected_digest=expected_digest,
        actual_digest=actual_digest,
        inputs=tuple(child_nodes),
    )


def resolve_lineage(
    target: str,
    *,
    repo_root: Path,
    explicit_derived: Path | None = None,
    max_depth: int = 32,
) -> LineageNode:
    """Resolve the recursive lineage of an artifact down to Zone 1.

    Returns the root LineageNode.
    """
    # Open DuckDB attach surface for query resolution (Z2/Z3/Z4)
    attach_res = attach(repo_root=repo_root, explicit_derived=explicit_derived)
    try:
        return _walk_lineage(
            target,
            repo_root=repo_root,
            explicit_derived=explicit_derived,
            expected_digest=None,
            ancestors=(),
            depth=0,
            max_depth=max_depth,
            duckdb_conn=attach_res.connection,
        )
    finally:
        with contextlib.suppress(Exception):
            attach_res.connection.close()


def lineage_to_dict(node: LineageNode) -> dict[str, Any]:
    """Convert a LineageNode tree into a deterministic JSON-serializable dict."""
    return {
        "target": node.target,
        "path": node.path,
        "zone": node.zone,
        "digest": node.digest,
        "status": node.status,
        "resolved": node.resolved,
        "reason": node.reason,
        "inputs": [lineage_to_dict(c) for c in node.inputs],
    }


def render_lineage_tree(
    node: LineageNode,
    prefix: str = "",
    is_last: bool = True,
    is_root: bool = True,
) -> str:
    """Render a LineageNode tree into a human-readable formatted string."""
    lines: list[str] = []

    # Format status text
    if node.status == "terminal":
        status_str = "[terminal]"
    elif node.status == "resolved":
        status_str = "[resolved]"
    elif node.reason:
        status_str = f"[{node.status}: {node.reason}]"
    else:
        status_str = f"[{node.status}]"

    digest_str = f"({node.digest})" if node.digest else ""
    target_str = node.path if node.path else node.target
    zone_str = f"[{node.zone}]"

    parts = [p for p in [target_str, zone_str, digest_str, status_str] if p]
    header = " ".join(parts)

    if is_root:
        lines.append(header)
    else:
        branch = "└── " if is_last else "├── "
        lines.append(f"{prefix}{branch}{header}")

    child_prefix = prefix + ("    " if is_last else "│   ") if not is_root else ""
    for idx, child in enumerate(node.inputs):
        child_is_last = idx == len(node.inputs) - 1
        lines.append(
            render_lineage_tree(
                child,
                prefix=child_prefix,
                is_last=child_is_last,
                is_root=False,
            )
        )

    return "\n".join(lines)
