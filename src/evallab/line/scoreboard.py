"""Label-isolated, file-derived assembly-line metrics and their text view."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any

from evallab import hack_drop

from .io import canonical_hash, read_json, read_rows


def _metric(k: int, n: int) -> dict[str, Any]:
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "wilson_ci": list(hack_drop.wilson_interval(k, n)),
    }


def _summary(
    decisions: list[hack_drop.RolloutDecision],
    labels: dict[str, bool],
    attempted: set[str],
) -> dict[str, Any]:
    report = hack_drop.compute_report(decisions, labels=labels)
    evaluation = report.get("evaluation", {})
    cheats = evaluation.get("cheats", 0)
    clean = evaluation.get("cleans", 0)
    breakdown = evaluation.get("breakdown", {})
    dropped = breakdown.get("dropped", {"cheats": 0, "cleans": 0})
    reviewed = breakdown.get("review", {"cheats": 0, "cleans": 0})
    ids = sorted(decision.id for decision in decisions)
    return {
        "runs": len(decisions),
        "labelled_runs": evaluation.get("total_labeled", 0),
        "cheats": cheats,
        "clean": clean,
        "attempted_cheat": sum(run_id in attempted for run_id in ids),
        "counts": report["counts"],
        "cohort_hash": canonical_hash(ids),
        "labels_hash": canonical_hash(
            [
                {
                    "run_id": run_id,
                    "label": labels.get(run_id),
                    "attempted_cheat": run_id in attempted,
                }
                for run_id in ids
            ]
        ),
        "metrics": {
            "drop_recall": _metric(dropped["cheats"], cheats),
            "drop_or_review_recall": _metric(dropped["cheats"] + reviewed["cheats"], cheats),
            "clean_false_alarm_rate": _metric(dropped["cleans"], clean),
            "clean_drop_or_review_rate": _metric(dropped["cleans"] + reviewed["cleans"], clean),
            "drop_precision": _metric(dropped["cheats"], dropped["cheats"] + dropped["cleans"]),
        },
    }


def _load_labels(
    spec: dict[str, Any],
    repo_root: Path,
    policy: str,
) -> tuple[dict[str, dict[str, bool]], dict[str, set[str]]]:
    by_corpus: dict[str, dict[str, bool]] = defaultdict(dict)
    attempted_by_corpus: dict[str, set[str]] = defaultdict(set)
    identities: dict[str, bool | str] = {}
    for source in spec["sources"]:
        if not source.get("labels"):
            continue
        corpus = source["corpus"]
        attempted: set[str] = set()
        labels = hack_drop.load_labels(
            source["labels"],
            repo_root=repo_root,
            attempted_cheat=policy,
            attempted_ids=attempted,
        )
        for run_id in labels.keys() | attempted:
            identity: bool | str = "attempted_cheat" if run_id in attempted else labels[run_id]
            if run_id in identities and identities[run_id] != identity:
                raise ValueError(f"conflicting labels for rollout {run_id!r}")
            identities[run_id] = identity
        by_corpus[corpus].update(labels)
        attempted_by_corpus[corpus].update(attempted)
    return dict(by_corpus), dict(attempted_by_corpus)


def _receipt_rows(receipt: dict[str, Any], side: str, batch: Path) -> int:
    count_key = "rows_in" if side == "inputs" else "rows_out"
    if count_key in receipt.get("counts", {}):
        return int(receipt["counts"][count_key])
    total = 0
    for item in receipt.get(side, []):
        path = Path(item["path"])
        if not path.is_absolute():
            path = batch / path
        if path.suffix == ".jsonl":
            total += len(read_rows(path))
        elif "rows" in item:
            total += int(item["rows"])
    return total


def build_scoreboard(batch: Path, repo_root: Path) -> dict:
    """Recompute every metric from persisted batch files, never changing decisions."""
    batch = Path(batch)
    spec = read_json(batch / "spec.json")
    policy = spec.get("labels_policy", {}).get("attempted_cheat", "exclude")
    if policy not in {"exclude", "cheat", "clean"}:
        raise ValueError(f"invalid attempted_cheat policy: {policy!r}")
    runs = read_rows(batch / "runs.jsonl")
    rows = read_rows(batch / "decisions.jsonl")
    intake: dict[str, dict[str, Any]] = {}
    for run in runs:
        run_id = str(run["run_id"])
        if run_id in intake:
            raise ValueError(f"duplicate intake run_id: {run_id}")
        intake[run_id] = run
    decisions: list[hack_drop.RolloutDecision] = []
    by_corpus: dict[str, list[hack_drop.RolloutDecision]] = defaultdict(list)
    seen: set[str] = set()
    for row in rows:
        run_id = str(row["run_id"])
        if run_id in seen or run_id not in intake:
            raise ValueError(f"duplicate or unknown decision run_id: {run_id}")
        if row["decision"] not in {"keep", "drop", "review"}:
            raise ValueError(f"invalid decision for rollout {run_id!r}")
        seen.add(run_id)
        decision = hack_drop.RolloutDecision(
            id=run_id,
            task_id=str(intake[run_id]["task_id"]),
            decision=row["decision"],
            reasons=list(row.get("reasons", [])),
            evidence_steps=list(row.get("evidence_steps", [])),
            stage_decisions=dict(row.get("stage_decisions", {})),
        )
        decisions.append(decision)
        by_corpus[intake[run_id]["corpus"]].append(decision)
    if seen != intake.keys():
        raise ValueError("decisions do not cover every intake run")
    for source in spec["sources"]:
        by_corpus.setdefault(source["corpus"], [])
    labels, attempted = _load_labels(spec, repo_root, policy)
    joined_labels: dict[str, bool] = {}
    joined_attempted: set[str] = set()
    summaries: dict[str, Any] = {}
    for corpus in sorted(by_corpus):
        corpus_ids = {decision.id for decision in by_corpus[corpus]}
        corpus_labels = {
            key: value for key, value in labels.get(corpus, {}).items() if key in corpus_ids
        }
        corpus_attempted = attempted.get(corpus, set()) & corpus_ids
        joined_labels.update(corpus_labels)
        joined_attempted.update(corpus_attempted)
        summaries[corpus] = _summary(by_corpus[corpus], corpus_labels, corpus_attempted)

    stages: dict[str, Any] = {}
    models: set[tuple[str, str]] = set()
    spend_by_stage: dict[str, float] = {}
    for path in sorted((batch / "receipts").glob("*.json")):
        receipt = read_json(path)
        stage = receipt["stage"]
        if stage in stages:
            raise ValueError(f"duplicate stage receipt: {stage}")
        errors = Counter(error["class"] for error in receipt.get("errors", []))
        stages[stage] = {
            "rows_in": _receipt_rows(receipt, "inputs", batch),
            "rows_out": _receipt_rows(receipt, "outputs", batch),
            "seconds": receipt["seconds"],
            "errors": dict(sorted(errors.items())),
        }
        spend_by_stage[stage] = float(receipt.get("spend_usd", 0))
        models.update((model["role"], model["route"]) for model in receipt.get("models", []))

    tasks: dict[str, str] = {}
    for row in read_rows(batch / "tasks.jsonl"):
        task_id, status = str(row["task_id"]), row["status"]
        if status not in {"certified", "provisional", "weak", "uncertified"}:
            status = "uncertified"
        if task_id in tasks and tasks[task_id] != status:
            raise ValueError(f"conflicting task statuses for {task_id!r}")
        tasks[task_id] = status
    statuses = ("certified", "provisional", "weak", "uncertified")
    task_side = dict.fromkeys(statuses, 0)
    reward1 = dict.fromkeys(statuses, 0)
    for run in runs:
        status = tasks.get(str(run["task_id"]), "uncertified")
        task_side[status] += 1
        reward = run.get("reward")
        if reward is not None:
            with suppress(TypeError, ValueError):
                reward1[status] += float(reward) == 1.0
    for status in statuses:
        task_side[f"reward1_{status}"] = reward1[status]
    reviews = sum(row["decision"] == "review" for row in rows)
    audits = sum(row["decision"] == "keep" and bool(row.get("audit")) for row in rows)
    check_path = batch / "check.json"
    return {
        "schema": "evallab.line_scoreboard/v1",
        "batch_id": spec["batch_id"],
        "labels_policy": {"attempted_cheat": policy},
        "overall": _summary(decisions, joined_labels, joined_attempted),
        "by_corpus": summaries,
        "stages": stages,
        "judge_load": {
            "review": reviews,
            "audit": audits,
            "total": reviews + audits,
            "per_100_runs": round(100 * (reviews + audits) / len(runs), 4) if runs else 0.0,
            "queued": len(read_rows(batch / "judge-queue.jsonl")),
        },
        "task_side": task_side,
        "spend": {
            "usd": sum(spend_by_stage.values()),
            "cap_usd": spec.get("autoreview", {}).get("max_usd", 0),
            "by_stage": spend_by_stage,
        },
        "models": [{"role": role, "route": route} for role, route in sorted(models)],
        "quality_checks": read_json(check_path)["checks"] if check_path.exists() else [],
    }


def _display_metric(metric: dict[str, Any]) -> str:
    rate = metric["rate"]
    if rate is None:
        return f"{metric['k']}/{metric['n']} (n/a; Wilson n/a)"
    low, high = metric["wilson_ci"]
    return f"{metric['k']}/{metric['n']} ({rate:.1%}; Wilson {low:.1%}–{high:.1%})"


def render_scoreboard(value: dict) -> str:
    """Render the saved scoreboard, including denominators and uncertainty."""
    lines = [
        f"# Assembly line scoreboard: {value['batch_id']}",
        "",
        f"Attempted-cheat policy: `{value['labels_policy']['attempted_cheat']}`.",
        "",
        "## Cohorts",
        "",
        "| Corpus | Runs | Labelled | Cheats | Clean | Attempted | Keep | Drop | Review |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    cohorts = [("Overall", value["overall"]), *value["by_corpus"].items()]
    for corpus, row in cohorts:
        counts = row["counts"]
        lines.append(
            f"| {corpus} | {row['runs']} | {row['labelled_runs']} | {row['cheats']} | "
            f"{row['clean']} | {row['attempted_cheat']} | {counts['keep']} | "
            f"{counts['drop']} | {counts['review']} |"
        )
    lines.extend(
        [
            "",
            "## Labelled metrics (95% Wilson intervals)",
            "",
            "| Corpus | Metric | k/n, rate, interval |",
            "|---|---|---|",
        ]
    )
    for corpus, row in cohorts:
        for name, metric in row["metrics"].items():
            lines.append(f"| {corpus} | {name} | {_display_metric(metric)} |")
    lines.extend(
        [
            "",
            "## Stages",
            "",
            "| Stage | Rows in | Rows out | Seconds | Errors by class |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for name, stage in value["stages"].items():
        errors = ", ".join(f"{kind}: {count}" for kind, count in stage["errors"].items()) or "none"
        lines.append(
            f"| {name} | {stage['rows_in']} | {stage['rows_out']} | {stage['seconds']:.3f} | {errors} |"
        )
    judge = value["judge_load"]
    lines.extend(
        [
            "",
            "## Judge load",
            "",
            f"Review: {judge['review']}; audit: {judge['audit']}; total: {judge['total']} "
            f"({judge['per_100_runs']:.2f} per 100 runs); queued: {judge['queued']}.",
            "",
            "## Task side",
            "",
        ]
    )
    for name, count in value["task_side"].items():
        lines.append(f"- {name}: {count}")
    lines.extend(
        [
            "",
            "## Spend and models",
            "",
            f"Spend: ${value['spend']['usd']:.4f}; cap: ${value['spend']['cap_usd']:.4f}.",
        ]
    )
    for model in value["models"]:
        lines.append(f"- {model['role']}: `{model['route']}`")
    if not value["models"]:
        lines.append("- No models used.")
    lines.extend(["", "## Quality checks", ""])
    for check in value["quality_checks"]:
        lines.append(f"- {check['name']}: **{check['status']}** — {check.get('details', '')}")
    if not value["quality_checks"]:
        lines.append("No quality checks recorded.")
    return "\n".join(lines) + "\n"
