"""Zero-spend replay of frozen assembly-line inputs and quality baselines."""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .io import canonical_hash, digest, read_json, read_rows, write_json

BASELINE_SCHEMA = "evallab.line_baseline/v1"


def _decisions(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("replay decisions must be a list of objects")
    result = {}
    for row in rows:
        corpus, run_id, decision = row.get("corpus"), row.get("run_id"), row.get("decision")
        if not isinstance(corpus, str) or not corpus or not isinstance(run_id, str) or not run_id:
            raise ValueError("replay decisions need nonempty corpus and run_id strings")
        if not isinstance(decision, str) or decision not in {"keep", "drop", "review"}:
            raise ValueError(f"invalid replay decision for {corpus}/{run_id}")
        key = (corpus, run_id)
        if key in result:
            raise ValueError(f"duplicate replay decision for {corpus}/{run_id}")
        result[key] = decision
    return result


def _changed_runs(
    before: dict[tuple[str, str], str], after: dict[tuple[str, str], str]
) -> list[dict[str, Any]]:
    return [
        {
            "corpus": corpus,
            "run_id": run_id,
            "before": before.get((corpus, run_id)),
            "after": after.get((corpus, run_id)),
        }
        for corpus, run_id in sorted(before.keys() | after.keys())
        if before.get((corpus, run_id)) != after.get((corpus, run_id))
    ]


def _count(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid replay count: {field}")
    return value


def _metric(group: dict[str, Any], name: str, denominator: str) -> tuple[int, int]:
    metrics = group.get("metrics")
    metric = metrics.get(name) if isinstance(metrics, dict) else None
    if not isinstance(metric, dict):
        raise ValueError(f"missing replay metric: {name}")
    k = _count(metric.get("k"), f"{name}.k")
    n = _count(metric.get("n"), f"{name}.n")
    if n != group[denominator] or k > n:
        raise ValueError(f"replay metric denominator mismatch: {name}")
    return k, n


def _quality_failures(scoreboard: dict[str, Any]) -> list[dict[str, Any]]:
    checks = scoreboard.get("quality_checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(check, dict) for check in checks)
    ):
        raise ValueError("replay scoreboard needs quality checks")
    return [check for check in checks if str(check.get("status", "")).lower() != "pass"]


def compare_baseline(
    baseline: dict[str, Any],
    current_scoreboard: dict[str, Any],
    current_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare matched labelled cohorts; invalid comparisons raise ``ValueError``.

    A change of exactly one run or exactly the percentage-point allowance passes.
    Quality failures are regressions even if all measured rates improve.
    """
    if baseline.get("schema") != BASELINE_SCHEMA:
        raise ValueError("unsupported replay baseline schema")
    previous = baseline.get("scoreboard")
    rows = baseline.get("decisions")
    if not isinstance(previous, dict) or not isinstance(rows, list):
        raise ValueError("baseline needs scoreboard and decisions")
    before, after = _decisions(rows), _decisions(current_decisions)
    old_groups, new_groups = previous.get("by_corpus"), current_scoreboard.get("by_corpus")
    if (
        not isinstance(old_groups, dict)
        or not isinstance(new_groups, dict)
        or old_groups.keys() != new_groups.keys()
    ):
        raise ValueError("replay corpus cohort mismatch")
    if before.keys() != after.keys():
        raise ValueError("replay decision cohort mismatch")
    if set(corpus for corpus, _ in before) != set(old_groups):
        raise ValueError("replay decisions and scoreboard cohorts disagree")
    if previous.get("labels_policy") != current_scoreboard.get("labels_policy"):
        raise ValueError("replay labels policy mismatch")
    regressions = []
    for corpus, old in old_groups.items():
        new = new_groups[corpus]
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ValueError(f"invalid replay corpus scoreboard: {corpus}")
        for field in ("runs", "labelled_runs", "cheats", "clean", "attempted_cheat"):
            if _count(old.get(field), field) != _count(new.get(field), field):
                raise ValueError(f"replay denominator mismatch: {corpus}.{field}")
        for field in ("cohort_hash", "labels_hash"):
            if (
                not isinstance(old.get(field), str)
                or not old[field]
                or old[field] != new.get(field)
            ):
                raise ValueError(f"replay {field} mismatch: {corpus}")
        if old["runs"] != sum(key[0] == corpus for key in before):
            raise ValueError(f"replay decision denominator mismatch: {corpus}")
        for metric, denominator, allowance, direction in (
            ("drop_or_review_recall", "cheats", 50, "fall"),
            ("clean_false_alarm_rate", "clean", 100, "rise"),
        ):
            old_k, n = _metric(old, metric, denominator)
            new_k, _ = _metric(new, metric, denominator)
            if not n:
                continue
            change = old_k - new_k if direction == "fall" else new_k - old_k
            tolerance = max(Fraction(1), Fraction(n, allowance))
            if change > tolerance:
                regressions.append(
                    {
                        "corpus": corpus,
                        "metric": metric,
                        "before": old_k / n,
                        "after": new_k / n,
                        "tolerance": float(tolerance / n),
                        "changed_count": change,
                        "denominator": n,
                    }
                )
    failures = _quality_failures(current_scoreboard)
    regressions.extend(
        {"check": check.get("name"), "details": check.get("details"), "kind": "quality_check"}
        for check in failures
    )
    return {
        "passed": not regressions,
        "regressions": regressions,
        "changed_runs": _changed_runs(before, after),
    }


def _frozen_hash(spec: dict[str, Any]) -> str:
    frozen = deepcopy(spec)
    frozen.pop("batch_id", None)
    frozen.pop("out_root", None)
    judge = frozen.get("judge")
    if isinstance(judge, dict):
        judge.pop("queue", None)
        judge.pop("write", None)
    return canonical_hash(frozen)


def _input_hashes(spec: dict[str, Any]) -> dict[str, str]:
    paths = {
        str(source[field])
        for source in spec["sources"]
        for field in ("rollouts", "labels")
        if source.get(field) is not None
    }
    if spec.get("task_status") is not None:
        paths.add(str(spec["task_status"]))
    if spec["autoreview"].get("cache") is not None:
        paths.add(str(spec["autoreview"]["cache"]))
    return {path: digest(Path(path)) for path in sorted(paths)}


def replay(
    spec_path: Path,
    baseline: Path,
    repo_root: Path,
    write_baseline: bool = False,
) -> tuple[dict[str, Any], int]:
    """Rerun off/cached inputs without editing the spec or external judge queue."""
    result: dict[str, Any] = {
        "schema": "evallab.line_replay/v1",
        "passed": False,
        "changed_runs": [],
        "regressions": [],
    }
    try:
        from .spec import load_spec

        spec = load_spec(spec_path)
        autoreview = spec.get("autoreview", {})
        if autoreview.get("mode") not in {"off", "cached"} or autoreview.get("max_usd") != 0:
            raise ValueError("replay requires autoreview off or cached and a zero spend cap")
        frozen_hash = _frozen_hash(spec)
        input_hashes = _input_hashes(spec)
        stored = None if write_baseline else read_json(baseline)
        if stored is not None:
            if stored.get("schema") != BASELINE_SCHEMA:
                raise ValueError("unsupported replay baseline schema")
            if stored.get("frozen_spec_hash") != frozen_hash:
                raise ValueError("replay frozen spec mismatch; refresh the baseline explicitly")
            if stored.get("input_hashes") != input_hashes:
                raise ValueError("replay frozen input bytes mismatch")
        # Source paths were resolved against the original spec, not this scratch file.
        replay_spec = deepcopy(spec)
        replay_spec["batch_id"] = f"{spec['batch_id']}-replay-{uuid4().hex}"
        replay_spec["judge"]["write"] = False
        from .core import check_batch, run_line

        with TemporaryDirectory(prefix="evallab-line-replay-") as scratch:
            scratch_path = Path(scratch) / "spec.json"
            write_json(scratch_path, replay_spec)
            batch = run_line(scratch_path, repo_root)
        result["batch"] = str(batch)
        scoreboard = read_json(batch / "scoreboard.json")
        decisions = [
            {"run_id": row["run_id"], "corpus": row["corpus"], "decision": row["decision"]}
            for row in read_rows(batch / "decisions.jsonl")
        ]
        if stored is not None:
            result["changed_runs"] = _changed_runs(
                _decisions(stored.get("decisions", [])), _decisions(decisions)
            )
        intake_hash = canonical_hash(
            sorted(read_rows(batch / "runs.jsonl"), key=lambda row: (row["corpus"], row["run_id"]))
        )
        if stored is not None and stored.get("intake_hash") != intake_hash:
            raise ValueError("replay frozen rollout cohort or bytes mismatch")
        checks = check_batch(batch, repo_root)
        scoreboard["quality_checks"] = checks["checks"]
        result["frozen_spec_hash"] = frozen_hash
        if write_baseline:
            # A failed batch must never bless a new baseline, even on explicit refresh.
            failures = _quality_failures(scoreboard)
            if failures or not checks.get("passed", False):
                result["regressions"] = [
                    {
                        "kind": "quality_check",
                        "check": check.get("name"),
                        "details": check.get("details"),
                    }
                    for check in failures
                ]
                return result, 1
            _decisions(decisions)
            candidate = {
                "schema": BASELINE_SCHEMA,
                "frozen_spec_hash": frozen_hash,
                "input_hashes": input_hashes,
                "intake_hash": intake_hash,
                "scoreboard": scoreboard,
                "decisions": decisions,
            }
            compare_baseline(candidate, scoreboard, decisions)
            write_json(baseline, candidate)
            result.update(passed=True, baseline_written=True, baseline=str(baseline))
            return result, 0
        if stored is None:
            raise ValueError("missing replay baseline")
        result.update(compare_baseline(stored, scoreboard, decisions))
        if not checks.get("passed", False) and result["passed"]:
            result["passed"] = False
            result["regressions"].append(
                {
                    "kind": "quality_check",
                    "check": "batch",
                    "details": "batch quality checks failed",
                }
            )
        return result, 0 if result["passed"] else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
        return result, 2
