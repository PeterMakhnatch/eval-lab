"""Read-only generic defect lookup, without case labels or executable payloads.

The first supported source is the shared taxonomy YAML. Case examples, source
links, exploit/probe programs and per-task findings never enter reviewer output.
The frozen catalog bytes are checked again before every lookup.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evallab.qa.env_playbooks import defect_classes

_PUBLIC_FIELDS = (
    "id", "key", "name", "definition", "direction", "fix_location", "subtypes",
    "evidence_standard", "detection_methods",
)


def _catalog_bytes(path: Path) -> bytes:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("hackbook source must not follow symlinks")
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("hackbook source exceeds the two-megabyte catalog limit")
    return raw


class DefectCatalog:
    """Pinned taxonomy capability, deliberately narrower than its source file."""

    def __init__(self, path: Path) -> None:
        requested = path.expanduser().absolute()
        raw = _catalog_bytes(requested)
        self.path = requested.resolve(strict=True)
        self.digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if self.path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            import yaml

            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise ValueError("hackbook source is not valid YAML") from exc
        if not isinstance(data, dict) or not isinstance(data.get("task_side"), list):
            raise ValueError("hackbook source must expose the shared taxonomy task_side schema")
        canonical = set(defect_classes())
        entries: dict[str, dict[str, Any]] = {}
        for row in data["task_side"]:
            if not isinstance(row, dict) or row.get("key") not in canonical:
                raise ValueError("hackbook task-side entry has no canonical defect class")
            key = row["key"]
            if key in entries:
                raise ValueError("hackbook repeats a task-side defect class")
            public = {name: row[name] for name in _PUBLIC_FIELDS if name in row}
            for name, value in public.items():
                if name in {"subtypes", "detection_methods"}:
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        raise ValueError(f"hackbook {name} must be a list of generic strings")
                elif not isinstance(value, str):
                    raise ValueError(f"hackbook {name} must be a generic string")
            if not isinstance(public.get("definition"), str) or not public["definition"].strip():
                raise ValueError("hackbook class needs a nonempty definition")
            entries[key] = public
        if set(entries) != canonical:
            raise ValueError("hackbook must cover every canonical task-side class")
        self.entries = entries
        if isinstance(data.get("version"), bool) or not isinstance(data.get("version"), int | str):
            raise ValueError("hackbook version must be a scalar version identifier")
        self.version = data.get("version")

    def lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) - {"defect_class", "query", "limit"}:
            raise ValueError("hackbook accepts defect_class, query and limit only")
        if "sha256:" + hashlib.sha256(_catalog_bytes(self.path)).hexdigest() != self.digest:
            raise ValueError("hackbook source changed after its identity was pinned")
        key, query, limit = arguments.get("defect_class"), arguments.get("query", ""), arguments.get("limit", 11)
        if key is not None and (not isinstance(key, str) or key not in self.entries):
            raise ValueError("hackbook defect_class must be a canonical task-side class")
        if not isinstance(query, str) or len(query) > 1_000:
            raise ValueError("hackbook query must be a string of at most 1000 characters")
        if type(limit) is not int or not 1 <= limit <= 11:
            raise ValueError("hackbook limit must be in [1, 11]")
        candidates = [self.entries[key]] if key is not None else list(self.entries.values())
        words = query.casefold().split()
        matches = [row for row in candidates if all(
            word in json.dumps(row, ensure_ascii=False).casefold() for word in words
        )]
        return {
            "evidence_basis": "generic_taxonomy", "catalog_digest": self.digest,
            "catalog_version": self.version, "scope": "generic_task_side_classes_only",
            "case_examples_included": False, "executable_payloads_included": False,
            "matches": matches[:limit], "total_matches": len(matches),
            "note": "A catalog match is a detection aid, not a task finding or executed witness.",
        }
