"""Frozen board memberships of the 22 DEV tasks, from TB4Acquire's immutable ``trajectory-index.jsonl``.

One line per (board row x task x attempt). Only DEV tasks are ever returned (the filter runs on the raw
line before JSON parsing). Strata:

* ``canonical``  provenance_admission == admitted_exact_source AND acquisition_state == complete
* ``quarantine`` the public archive failed #1113's source/identity gate (different-source lock, drift);
                 retained and labelled separately, NEVER mixed into the canonical denominator
* ``unavailable`` / ``incomplete``  no archive or integrity/identity problem: reward may be known from
                 the Hub metadata but there is no evidence to classify (label ``unmeasured``)
``--bootstrap`` builds the same schema from archives already on disk (development only, never final).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from census_core import DEV_TASKS, HUB, LEGACY, LEGACY_DEV_TASKS, read_jsonl_dev, sha256_file

INDEX = HUB / "trajectory-index.jsonl"
SUMMARY = HUB / "acquisition-summary.json"


def stratum(m: dict[str, Any]) -> str:
    state, adm = m.get("acquisition_state"), m.get("provenance_admission")
    if adm == "admitted_exact_source" and state == "complete":
        return "canonical"
    if str(state).startswith("quarantine") or m.get("archive_class") == "quarantine" or \
            str(adm).startswith("quarantined"):
        return "quarantine"
    if state in {"unavailable_no_public_archive"}:
        return "unavailable"
    return "incomplete"


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    task = str(raw["task"]).removeprefix("terminal-bench/")
    reward = raw.get("reward")
    m = {
        "row_id": raw["row_id"], "board_rank": raw.get("board_rank"),
        "model": raw.get("model_label", raw.get("model")),
        "effort": raw.get("reasoning_effort", raw.get("effort")),
        "task": task, "trial_id": raw["hub_trial_id"], "archive_trial_id": raw.get("archive_trial_id"),
        "job_id": raw.get("job_id"), "trial_name": raw.get("trial_name"), "attempt": raw.get("attempt"),
        "reward": None if reward is None else float(reward),
        "acquisition_state": raw.get("acquisition_state"),
        "provenance_admission": raw.get("provenance_admission"),
        "archive_class": raw.get("archive_class"), "evidence_class": raw.get("evidence_class"),
        "archive_path": raw.get("archive_path"), "tree_sha256": raw.get("tree_sha256"),
        "provenance_1120": (raw.get("provenance_1120") or {}).get("native_lock_stratum")
        if isinstance(raw.get("provenance_1120"), dict) else raw.get("provenance_1120"),
    }
    m["stratum"] = stratum(m)
    return m


def load_index(path: Path = INDEX) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"{path} does not exist yet (TB4Acquire has not built the index)")
    rows = [normalize(r) for r in read_jsonl_dev(path, prefix="terminal-bench/")]
    dev = [r for r in rows if r["task"] in DEV_TASKS]
    if len(dev) != len(rows):
        raise SystemExit("non-DEV row leaked through the DEV filter")
    seen: set[tuple[str, str, str]] = set()
    for r in dev:
        key = (r["row_id"], r["task"], r["trial_id"])
        if key in seen:
            raise SystemExit(f"duplicate membership {key}")
        seen.add(key)
    return sorted(dev, key=lambda r: (r["row_id"], r["task"], str(r["attempt"]), r["trial_id"]))


def board_rows() -> dict[str, dict[str, Any]]:
    """Public leaderboard row metadata (agent, overall accuracy) of RC-1's inventory; contextual only."""
    path = LEGACY / "rows.json"
    if not path.exists():
        return {}
    return {r["row_id"]: r for r in json.loads(path.read_text())}


def bootstrap_index() -> list[dict[str, Any]]:
    """Development fallback: memberships of trials already on disk, built from their result.json only."""
    meta = board_rows()
    out: list[dict[str, Any]] = []
    for row in sorted((HUB / "rows").glob("*")):
        for trial in row.glob("dev22/trials/*/*"):
            res = trial / "result.json"
            if not res.is_file():
                continue
            r = json.loads(res.read_text())
            task = r["task_name"].split("/")[-1]
            if task not in DEV_TASKS:
                continue
            rew = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            b = meta.get(row.name, {})
            out.append(normalize({
                "row_id": row.name, "board_rank": b.get("rank"), "model_label": b.get("model", "?"),
                "reasoning_effort": b.get("effort", "?"), "task": task, "hub_trial_id": trial.name,
                "archive_trial_id": r["id"], "job_id": trial.parent.name, "trial_name": r["trial_name"],
                "reward": rew, "acquisition_state": "complete",
                "provenance_admission": "admitted_exact_source", "archive_class": "canonical",
                "evidence_class": "full_trajectory", "archive_path": str(trial)}))
    return out


def index_receipt(path: Path = INDEX) -> dict[str, Any]:
    return {"index": str(path), "index_sha256": sha256_file(path) if path.exists() else None,
            "summary": str(SUMMARY), "summary_sha256": sha256_file(SUMMARY) if SUMMARY.exists() else None,
            "legacy_dev_tasks": sorted(LEGACY_DEV_TASKS)}
