"""Read-only per-task trial dossier rows (HAR-186).

Membership comes only from the ``trials`` census view (queried with
``task_names`` + ``read_only=True``). Copy facts always come from the
original source trial first, so they survive a missing or ambiguous
publication; the publication alias only resolves reader verdicts the
original job name cannot reach in the store. Every source keeps its own
separately named verdict under ``copy_verdicts``. Missing evidence stays
null; conflicts stay explicit. Nothing here writes, probes, or collects
anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from evallab import results_home, task_pages
from evallab.storage.trials import connect_trials

#: Viewer route for a task page (served by ``evallab view`` / harbor view).
PAGE_BASE = "http://127.0.0.1:8100/jobs/"

#: Namespace/source variants of one MIMO leaf task id.
MIMO_NAMESPACE = "mimo-v2.6-rl"


def canonical_aliases(task_id: str) -> list[str]:
    """Exact native task names selected for one leaf task id."""
    return [task_id, f"{MIMO_NAMESPACE}/{task_id}", f"{MIMO_NAMESPACE}__{task_id}"]


def page_url_for(task_id: str) -> str:
    """Viewer URL for the task page (route only; not a liveness claim)."""
    return PAGE_BASE + task_pages.page_name(task_id)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_id(value: Any) -> str | None:
    """Native id text safe to compare (mirrors the census identity rule)."""
    if not isinstance(value, str) or not value:
        return None
    if value in (".", ".."):
        return None
    if "/" in value or "\\" in value or "\x00" in value:
        return None
    if Path(value).name != value:
        return None
    return value


def _native_pair(
    trial_result: dict[str, Any], job_result: dict[str, Any]
) -> tuple[str | None, str | None]:
    """``(job_id, trial_id)`` for one trial: config precedes the parent id."""
    trial_id = _safe_id(trial_result.get("id"))
    config = trial_result.get("config")
    config_job = _safe_id(config.get("job_id")) if isinstance(config, dict) else None
    parent_job = _safe_id(job_result.get("id"))
    return config_job or parent_job, trial_id


def _is_trial_result(payload: dict[str, Any]) -> bool:
    return "task_name" in payload and "trial_name" in payload


def build_alias_index(home: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Native pair -> published ``(job_dir, trial_dir)`` candidates.

    Read-only: reuses the results-home publication listing and binds trials
    by recorded native identity, never by directory-name similarity.
    """
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for job_dir, provenance in results_home._published_jobs(home):
        job_result = _read_json_object(job_dir / "result.json") or {}
        try:
            trials = sorted(
                p for p in job_dir.iterdir() if p.is_dir() and not p.is_symlink()
            )
        except OSError:
            continue
        for trial_dir in trials:
            trial_result = _read_json_object(trial_dir / "result.json")
            if not isinstance(trial_result, dict) or not _is_trial_result(trial_result):
                continue
            job_id, trial_id = _native_pair(trial_result, job_result)
            if job_id is None or trial_id is None:
                continue
            index.setdefault((job_id, trial_id), []).append(
                {
                    "job_dir": job_dir,
                    "trial_dir": trial_dir,
                    "trial_result": trial_result,
                    "provenance_path": job_dir / "provenance.json",
                    "source_path": provenance.get("source_path"),
                }
            )
    return index


def resolve_alias(
    row: dict[str, Any],
    index: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Bind one census row to its published alias (or an explicit miss).

    Statuses: ``ok`` | ``missing`` (no published copy with this native pair)
    | ``ambiguous`` (several distinct copies share the pair) | ``conflict``
    (the single candidate's trial name disagrees with the census row).
    """
    key = (row.get("job_id"), row.get("trial_id"))
    candidates = index.get(key, []) if key[0] is not None and key[1] is not None else []
    if not candidates:
        return {"status": "missing", "detail": "no published copy with this native pair"}
    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "detail": f"{len(candidates)} published copies share this native pair",
            "candidates": [str(c["trial_dir"]) for c in candidates],
        }
    candidate = candidates[0]
    trial_dir = candidate["trial_dir"]
    if trial_dir.name != row.get("trial"):
        return {
            "status": "conflict",
            "detail": (
                f"published trial {trial_dir.name!r} disagrees "
                f"with census trial {row.get('trial')!r}"
            ),
            "published_job": candidate["job_dir"].name,
            "published_trial": trial_dir.name,
            "provenance_path": str(candidate["provenance_path"]),
            "source_path": candidate["source_path"],
        }
    return {
        "status": "ok",
        "published_job": candidate["job_dir"].name,
        "published_trial": trial_dir.name,
        "provenance_path": str(candidate["provenance_path"]),
        "source_path": candidate["source_path"],
    }


def _copy_criterion_values(trial_dir: Path) -> list[Any]:
    """Recorded ``copy_check*`` criterion values, or ``[]`` without a record."""
    payload = _read_json_object(trial_dir / "verifier" / "reward-details.json")
    if payload is None:
        return []
    integrity = payload.get("integrity")
    components = (
        integrity.get("components") if isinstance(integrity, dict) else None
    )
    if not isinstance(components, list):
        return []
    values: list[Any] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        detail = component.get("detail")
        criteria = detail.get("criteria") if isinstance(detail, dict) else None
        if not isinstance(criteria, list):
            continue
        for criterion in criteria:
            if not isinstance(criterion, dict):
                continue
            name = criterion.get("name")
            if isinstance(name, str) and name.startswith("copy_check"):
                values.append(criterion.get("value"))
    return values


def _rewardkit_copy(trial_dir: Path) -> bool | None:
    """Binary RewardKit copy verdict; None without a decided rule record.

    A bare integrity score never proves the copy rule ran: only a recorded
    ``copy_check*`` criterion with a decided ``0``/``1`` value decides --
    ``0`` (fired) is true, ``1`` (clean) is false. An absent, malformed, or
    undecided record stays null, never a clean result.
    """
    values = _copy_criterion_values(trial_dir)
    decided = [value for value in values if value in (0, 1)]
    if any(value == 0 for value in decided):
        return True
    if values and len(decided) == len(values):
        return False
    return None


def _identity_bound_verdicts(
    merged: dict[str, Any], accepted: set[tuple[Any, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Stored reader verdicts whose recorded job/trial match a bound alias."""
    bound: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for reader in task_pages.READERS:
        verdict = merged.get(reader)
        if not isinstance(verdict, dict):
            continue
        if (verdict.get("job"), verdict.get("trial")) in accepted:
            bound[reader] = verdict
        else:
            conflicts.append(
                f"{reader} verdict binds {verdict.get('job')}/{verdict.get('trial')}, "
                "not a bound alias of this trial"
            )
    return bound, conflicts


def _laminar_url(
    row: dict[str, Any], bound: dict[str, dict[str, Any]]
) -> tuple[str | None, list[str], bool]:
    """Return the recorded link, diagnostics, and whether the trace is disputed."""
    verdict = bound.get("laminar_signals")
    if not verdict:
        return None, [], False
    recorded = verdict.get("trace_id")
    census = row.get("laminar_trace_id")
    if recorded is not None and census is not None and recorded != census:
        return None, [
            f"laminar trace {recorded} disagrees with census {census}"
        ], True
    url = verdict.get("trace_url")
    if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        return None, [], False
    if recorded is None or url.rstrip("/").rsplit("/", 1)[-1] != recorded:
        return None, [f"laminar url {url!r} does not identify recorded trace {recorded!r}"], False
    return url, [], False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _row_dict(columns: list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    return {name: _jsonable(value) for name, value in zip(columns, values, strict=True)}


def _null_verdicts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "native_copy_verdict": row.get("copy_verdict"),
        "copy_check": None,
        "copy_check_note": None,
        "rewardkit_copy": None,
        "laminar_copied": None,
        "harbor_analyze_reward_hacking": None,
    }


def _enrich_row(
    row: dict[str, Any],
    *,
    page_url: str,
    store: Path,
    index: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    enriched: dict[str, Any] = dict(row)
    enriched["page_url"] = page_url
    conflicts: list[str] = []

    # Native evidence first: copy facts survive any publication outcome.
    native: dict[str, Any] | None = None
    native_trial: Path | None = None
    if row.get("source_job_dir") and row.get("source_trial_dir"):
        job_dir, trial_dir = Path(row["source_job_dir"]), Path(row["source_trial_dir"])
        if trial_dir.is_dir():
            native = task_pages.trial_evidence(store, job_dir, trial_dir)
            native_trial = trial_dir

    # The publication only resolves reader verdicts the original job
    # name cannot reach in the store.
    alias = resolve_alias(row, index)
    pub: dict[str, Any] | None = None
    published_trial: Path | None = None
    if alias["status"] == "ok":
        published_job = Path(alias["provenance_path"]).parent
        published_trial = published_job / alias["published_trial"]
        if published_job.name != alias["published_job"] or not published_trial.is_dir():
            alias = {
                "status": "missing",
                "detail": "published copy listed in the alias index is not on disk",
            }
        else:
            pub = task_pages.trial_evidence(store, published_job, published_trial)
    if alias["status"] in ("ambiguous", "conflict"):
        conflicts.append(str(alias.get("detail")))
    enriched["alias"] = alias

    base = native if native is not None else pub
    backing = native_trial if native is not None else published_trial
    if base is None or backing is None:
        enriched["evidence"] = None
        enriched["copy_verdicts"] = _null_verdicts(row)
        enriched["laminar_url"] = None
        enriched["binding_conflicts"] = conflicts
        return enriched

    merged = dict((pub.get("verdicts") or {}) if pub else {})
    merged.update((native.get("verdicts") or {}) if native else {})
    accepted = {(row.get("job"), row.get("trial"))}
    if alias["status"] == "ok":
        accepted.add((alias["published_job"], alias["published_trial"]))
    bound, binding_conflicts = _identity_bound_verdicts(merged, accepted)
    conflicts.extend(binding_conflicts)

    laminar_url, laminar_conflicts, trace_disputed = _laminar_url(row, bound)
    conflicts.extend(laminar_conflicts)
    laminar = (bound.get("laminar_signals") or {}).get("checks") or {}
    analyze = (bound.get("harbor_analyze") or {}).get("checks") or {}

    evidence = dict(base)
    evidence["verdicts"] = merged
    enriched["evidence"] = evidence
    enriched["copy_verdicts"] = {
        "native_copy_verdict": row.get("copy_verdict"),
        "copy_check": base.get("copy_check"),
        "copy_check_note": base.get("copy_check_note"),
        "rewardkit_copy": _rewardkit_copy(backing),
        "laminar_copied": None if trace_disputed else laminar.get("copied"),
        "harbor_analyze_reward_hacking": analyze.get("reward_hacking"),
    }
    enriched["laminar_url"] = laminar_url
    enriched["binding_conflicts"] = conflicts
    return enriched


def task_trials(
    task_id: str,
    *,
    repo_root: Path,
    roots: Sequence[Path] | None = None,
    derived_root: Path | None = None,
    reader_store: Path | None = None,
) -> dict[str, Any]:
    """Every census trial for one leaf task id, enriched read-only.

    Returns ``{'trials': [...], 'coverage': {...}}``; both JSON-serializable.
    Unknown tasks yield an empty trial list with null evidence -- never
    fabricated verdicts. Raw evidence and stored reader verdicts pass
    through immutably; per-source copy verdicts are separately named with
    no consensus applied.
    """
    aliases = canonical_aliases(task_id)
    store = Path(reader_store) if reader_store is not None else task_pages.default_store()
    page_url = page_url_for(task_id)

    con, info = connect_trials(
        repo_root=Path(repo_root),
        roots=roots,
        derived_root=derived_root,
        task_names=aliases,
        read_only=True,
    )
    try:
        cursor = con.execute("SELECT * FROM trials")
        columns = [desc[0] for desc in cursor.description]
        rows = [_row_dict(columns, values) for values in cursor.fetchall()]
    finally:
        con.close()

    home = results_home.results_root()
    index = build_alias_index(home) if rows else {}
    trials = [
        _enrich_row(row, page_url=page_url, store=store, index=index) for row in rows
    ]
    status_counts: dict[str, int] = {}
    for trial in trials:
        status = trial["alias"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    coverage = {
        "task_id": task_id,
        "aliases": aliases,
        "n_trials": len(trials),
        "page_url": page_url,
        "reader_store": str(store),
        "results_home": str(home),
        "alias_status": status_counts,
        "n_projection_errors": sum(1 for row in rows if row.get("projection_error")),
        "read_only": True,
        "in_memory_projections": info.get("in_memory_projections", 0),
    }
    return {"trials": trials, "coverage": coverage}
