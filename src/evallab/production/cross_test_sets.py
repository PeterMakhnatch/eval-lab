"""Read-only view of the staged cross-test sets, from the registry file only.

The registry (``cross-test-registry/v1``, kept in research-context and checked by
``validate_cross_test_registry.py``) is metadata: where each bundle is, its licence, label type
and counts. This module reads that one file. It never opens a bundle, a manifest or a record, so
no record text can reach the page. Counts are integers copied as published.

Each entry is checked on its own, so one malformed entry is shown as an error without hiding the
rest. A set is "uploadable: no" only when the registry says so; an entry that says otherwise, or
that is not eval-only, local-only and no-training, is refused as malformed (the validator's rule).
The four HOLD-access sets are shown as "needs Peter, not available", never as usable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from evallab.production.atlas_view import json_reason, read_regular, strict_loads

SCHEMA = "evallab.cross_test_sets/v1"
REGISTRY_SCHEMA = "cross-test-registry/v1"
DEFAULT_FILE = "cross-test/registry.json"
MAX_BYTES = 2 * 1024 * 1024
#: The validator reads and writes research-context paths, so the command runs from there.
VALIDATOR_DIRECTORY = "~/Developer/research-context"
_SCRIPT = re.compile(r"^[\w./-]+\.py$")
_Id = Annotated[str, Field(min_length=1, max_length=200)]
_Prose = Annotated[str, Field(max_length=2000)]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Flags(_Loose):
    eval_only: StrictBool
    local_only: StrictBool
    no_training: StrictBool
    no_rehosting: StrictBool
    uploadable_to_vendor_projects: StrictBool
    hosted_model_api_for_evaluation: str


class _Licence(_Loose):
    text: _Id
    redistributable: StrictBool


class _Labels(_Loose):
    type: _Prose
    counts: dict[str, StrictInt]


class _Scoring(_Loose):
    command_level_localization_scorable: StrictBool
    command_level_reason: _Prose = ""


class _Adapter(_Loose):
    merge_state: Literal["merged", "open-pr", "not-started"]
    pr: StrictInt | str | None = None


class _Entry(_Loose):
    id: _Id
    name: _Id
    source_url: str
    unit: _Id
    unit_note: _Prose = ""
    status: _Id
    flags: _Flags
    licence: _Licence
    labels: _Labels
    scoring: _Scoring
    adapter: _Adapter


class _Hold(_Loose):
    id: _Id
    name: _Id
    source_url: str | None = None
    status: Literal["needs_peter"]
    available: Literal[False]
    staged: Literal[False]
    bundle_path: None = None
    why_held: _Prose = ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _safe_script(script: str) -> bool:
    """A relative ``.py`` path with no traversal, so the displayed command stays inside the repository."""
    return bool(_SCRIPT.match(script)) and not script.startswith("/") and ".." not in script.split("/")


def _https(url: str) -> str | None:
    return url if url.startswith("https://") else None


def _pr(value: int | str | None) -> tuple[int | None, str | None]:
    number = int(value) if isinstance(value, int) or (isinstance(value, str) and value.isdigit()) else None
    if number is None or number < 1:
        return None, None
    return number, f"https://github.com/PeterMakhnatch/eval-lab/pull/{number}"


def _entry(raw: Any) -> dict[str, Any]:
    item = _Entry.model_validate(raw)
    flags = item.flags
    if flags.uploadable_to_vendor_projects:
        raise ValueError("the registry marks this set uploadable; no cross-test set may be")
    for name in ("eval_only", "local_only", "no_training"):
        if not getattr(flags, name):
            raise ValueError(f"flag {name} is not true")
    if not item.licence.redistributable and not flags.no_rehosting:
        raise ValueError("a set that cannot be redistributed must say no-rehosting")
    number, url = _pr(item.adapter.pr)
    return {
        "id": item.id,
        "name": item.name,
        "source_url": _https(item.source_url),
        "unit": item.unit,
        "unit_note": _clip(item.unit_note, 400),
        "label_type": _clip(item.labels.type, 200),
        "counts": dict(item.labels.counts),
        "command_level": {
            "scorable": item.scoring.command_level_localization_scorable,
            "reason": _clip(item.scoring.command_level_reason, 300),
        },
        "licence": {"text": item.licence.text, "redistributable": item.licence.redistributable},
        "flags": {
            "eval_only": flags.eval_only,
            "local_only": flags.local_only,
            "no_training": flags.no_training,
            "no_rehosting": flags.no_rehosting,
        },
        "uploadable": False,
        "hosted_model_api": flags.hosted_model_api_for_evaluation,
        "adapter": {"merge_state": item.adapter.merge_state, "pr": number, "pr_url": url},
        "status": item.status,
    }


def _hold(raw: Any) -> dict[str, Any]:
    item = _Hold.model_validate(raw)
    return {
        "id": item.id,
        "name": item.name,
        "source_url": _https(item.source_url) if item.source_url else None,
        "state": "needs Peter, not available",
        "why_held": _clip(item.why_held, 300),
    }


def _problem(where: str, raw: Any, error: Exception) -> dict[str, str]:
    identity = raw.get("id") if isinstance(raw, dict) else None
    return {
        "where": where,
        "id": identity if isinstance(identity, str) else "",
        "reason": json_reason(error),
    }


def _checked(items: Any, build: Any, where: str, seen: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for index, raw in enumerate(items if isinstance(items, list) else []):
        try:
            row = build(raw)
            if row["id"] in seen:
                raise ValueError(f"id {row['id']!r} appears more than once")
            seen.add(row["id"])
            rows.append(row)
        except (ValueError, ValidationError) as error:
            problems.append(_problem(f"{where}[{index}]", raw, error))
    return rows, problems


def parse_registry(raw: bytes) -> dict[str, Any]:
    """The served board from registry bytes; a bad entry is reported, not fatal."""
    document = strict_loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != REGISTRY_SCHEMA:
        raise ValueError(f"schema_version must be {REGISTRY_SCHEMA}")
    for key in ("entries", "hold_access"):
        if not isinstance(document.get(key), list):
            raise ValueError(f"{key} must be a list")
    seen: set[str] = set()
    entries, problems = _checked(document["entries"], _entry, "entries", seen)
    held, hold_problems = _checked(document["hold_access"], _hold, "hold_access", seen)
    not_staged = document.get("hold_licence_not_staged")
    script = (document.get("validator") or {}).get("script") if isinstance(document.get("validator"), dict) else None
    script = script if isinstance(script, str) and _safe_script(script) else None
    command = f"cd {VALIDATOR_DIRECTORY} && python3 {script}" if script else None
    return {
        "created": document.get("created") if isinstance(document.get("created"), str) else None,
        "entries": entries,
        "hold_access": held,
        "malformed": problems + hold_problems,
        "hold_licence_not_staged_n": len(not_staged) if isinstance(not_staged, list) else 0,
        "validator": None
        if command is None
        else {"command": command, "quick_command": command + " --quick --offline"},
    }


def load_registry(path: Path) -> dict[str, Any]:
    """The served state for ``path``: parsed, absent, or unreadable with the reason."""
    base: dict[str, Any] = {"schema": SCHEMA, "present": False, "error": None}
    try:
        raw = read_regular(path.parent, path.name, MAX_BYTES)
        if raw is None:
            return base | {"error": "no cross-test registry is retained at the configured path"}
        board = parse_registry(raw)
    except (OSError, ValueError, ValidationError, UnicodeDecodeError) as error:
        return base | {"error": f"unreadable: {json_reason(error)}"}
    return base | board | {
        "present": True,
        "notice": (
            "Metadata from the registry file only: no bundle or record is opened and no record text "
            "is shown. Every set is eval-only, local-only and no-training and none is uploadable. "
            "Sets that need Peter are not available to any lane."
        ),
    }
