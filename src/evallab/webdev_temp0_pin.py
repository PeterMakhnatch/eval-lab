"""Temperature-zero webdev judging with a fail-closed artifact revision pin.

The Hub commit identifies model bytes, not what a floating HF router serves.
This variant binds the request model/provider, requires a version-aware
deployment's configured revision, and rejects responses without the matching
model_revision field. HF router has no documented revision selection, so
judge-dependent validation is staged, not falsely claimed version-pinned.
Temperature zero reduces sampling variance; it does not guarantee agreement.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "webdev-temp0-pin@1"

#: Marker comment identifying the inserted change (also the idempotence guard).
MARKER = "webdev-temp0-pin@1"

#: Package-relative paths this transform rewrites.
GRADE_REL = "tests/grade.py"
GRADE_JSON_REL = "tests/grade.json"

#: Pinned judge identity (resolved from the Hub model page, 2026-10-09).
PINNED_MODEL = "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
PINNED_MODEL_SHA = "94125d2bd83076b21eed33119525e29eaf3894f4"
PINNED_MODEL_DATE = "2026-10-09"
RUBRIC_ID = "rva1:mean(visual,query,asset)"

#: Exact parent snippet (byte-identical across the pinned webdev snapshot).
_TEMP_OLD = '"temperature": 1.0,'
_TEMP_NEW = '"temperature": 0, "max_tokens": 1024,  # webdev-temp0-pin@1'


def build_grade_py(parent_grade_py: str) -> str:
    """Bind request configuration and refuse unversioned judge responses."""
    if MARKER in parent_grade_py:
        return parent_grade_py
    anchors = (
        _TEMP_OLD,
        'os.environ["WEBDEV_JUDGE_MODEL"]',
        'body = json.dumps(',
        '            msg = json.loads(r.read())["choices"][0]["message"]',
    )
    if any(parent_grade_py.count(anchor) != 1 for anchor in anchors):
        raise VariantInvalid("grade.py has no unique temperature anchor/request shape")
    result = parent_grade_py.replace(_TEMP_OLD, _TEMP_NEW, 1)
    result = result.replace('os.environ["WEBDEV_JUDGE_MODEL"]', 'cfg["judge"]["request_model"]', 1)
    guard = '''_judge_pin = cfg["judge"]
if os.environ.get("WEBDEV_JUDGE_REVISION") != _judge_pin["model_sha"]:
    done(None, "Judge deployment revision is unbound; not scored.")
(V / "judge_config.json").write_text(json.dumps(_judge_pin, indent=1))
'''
    result = result.replace('body = json.dumps(', guard + 'body = json.dumps(', 1)
    reply = '''            _judge_reply = json.loads(r.read())
            if _judge_reply.get("model_revision") != _judge_pin["model_sha"]:
                done(None, "Judge response revision differs from the task pin; not scored.")
            msg = _judge_reply["choices"][0]["message"]'''
    return result.replace(anchors[-1], reply, 1)


def build_grade_json(parent_grade_json: str) -> str:
    """Parent ``grade.json`` plus the pinned judge record. Idempotent."""
    try:
        cfg = json.loads(parent_grade_json)
    except Exception as exc:
        raise VariantInvalid(f"parent grade.json is not JSON: {exc}") from exc
    if not isinstance(cfg, dict) or "cwd" not in cfg or "query" not in cfg:
        raise VariantInvalid("parent grade.json lacks cwd/query")
    judge = cfg.get("judge") or {}
    pin = {"model": PINNED_MODEL, "model_sha": PINNED_MODEL_SHA,
           "request_model": PINNED_MODEL + ":novita", "provider": "novita",
           "pinned_on": PINNED_MODEL_DATE, "temperature": 0, "max_tokens": 1024, "rubric_id": RUBRIC_ID}
    if judge == pin:
        return parent_grade_json
    cfg["judge"] = pin
    return json.dumps(cfg, indent=1, ensure_ascii=False) + "\n"


def _read_parent(parent_dir: Path) -> tuple[str, str, str]:
    try:
        task_name = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))["task"]["name"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        grade_py = (parent_dir / GRADE_REL).read_text(encoding="utf-8")
        grade_json = (parent_dir / GRADE_JSON_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent grader file is missing: {exc}") from exc
    if not task_name:
        raise VariantInvalid("parent task.toml names no task")
    return task_name, grade_py, grade_json


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``tests/grade.py`` and ``tests/grade.json`` change; rendering and
    the rubric are byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, grade_py, grade_json = _read_parent(parent)
    new_py = build_grade_py(grade_py)
    new_json = build_grade_json(grade_json)
    if new_py == grade_py and new_json == grade_json:
        raise VariantInvalid("parent already carries webdev-temp0-pin@1")
    changes: dict[str, bytes | None] = {}
    if new_py != grade_py:
        changes[GRADE_REL] = new_py.encode("utf-8")
    if new_json != grade_json:
        changes[GRADE_JSON_REL] = new_json.encode("utf-8")
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "judge_model": PINNED_MODEL,
        "judge_model_sha": PINNED_MODEL_SHA,
        "judge_temperature": 0,
        "rubric_id": RUBRIC_ID,
        "grade_before_sha256": f"sha256:{hashlib.sha256(grade_py.encode()).hexdigest()}",
        "grade_after_sha256": f"sha256:{hashlib.sha256(new_py.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_webdev_temp0_pin(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Use temperature zero and bind the model/provider request and artifact "
        "revision; mask absent or mismatched deployment/response revision. "
        "Version-aware judge endpoint validation is staged."
    ),
    created_by: str = "webdev-temp0-pin",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``webdev-temp0-pin@1`` variant of a webdev task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "GRADE_JSON_REL",
    "GRADE_REL",
    "MARKER",
    "PINNED_MODEL",
    "PINNED_MODEL_SHA",
    "RUBRIC_ID",
    "TRANSFORM_ID",
    "build_changes",
    "build_grade_json",
    "build_grade_py",
    "derive_webdev_temp0_pin",
]
