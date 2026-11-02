"""Receipt-backed DEV replay through the existing hack-drop stages."""

from __future__ import annotations

import hashlib
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab import hack_drop as hd
from evallab.line.io import canonical_hash, digest, read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec

CHECK_NAMES = (
    "conservation",
    "dev_only",
    "provenance",
    "determinism",
    "spend_and_models",
    "judge_routing",
    "label_isolation",
    "scoreboard_recompute",
)
STATUSES = {"certified", "provisional", "weak", "uncertified"}


def audit_sample(seed: str, run_id: str, rate: float) -> bool:
    return int(hashlib.sha256(f"{seed}:{run_id}".encode()).hexdigest()[:8], 16) / 2**32 < rate


def _code(root: Path) -> tuple[str, bool]:
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True
        ).stdout.strip()
    )
    return rev, dirty


def _cache(spec: dict[str, Any]) -> tuple[dict[str, hd.StageVerdict], list[Path]]:
    if spec["autoreview"]["mode"] != "cached":
        return {}, []
    path = hd.check_dev_path_allowed(spec["autoreview"]["cache"])
    files = (
        [path]
        if path.is_file()
        else sorted(
            p
            for p in path.rglob("*.jsonl")
            if p.name in {"kept.jsonl", "dropped.jsonl", "review.jsonl"}
            and (p.parent.name.endswith("_l0_autoreview") or p.parent == path)
        )
    )
    if not files:
        raise ValueError(f"no hack-drop output manifests found: {path}")
    cache: dict[str, hd.StageVerdict] = {}
    for file in files:
        hd.check_dev_path_allowed(file)
        for row in read_rows(file):
            hd._check_row_dev(row)
            value = row.get("stage_decisions", {}).get("autoreview")
            if not isinstance(value, dict):
                continue
            key = str(row.get("id") or row.get("run_id") or "")
            if not key or value.get("decision") not in {"pass", "review", "drop"}:
                raise ValueError(f"invalid cached stage verdict: {file}")
            verdict = hd.StageVerdict(
                "autoreview",
                value["decision"],
                value.get("reasons", []),
                value.get("evidence_steps", row.get("evidence_steps", [])),
                value.get("details", {}),
            )
            if key in cache and asdict(cache[key]) != asdict(verdict):
                raise ValueError(f"conflicting AutoReview cache for {key}")
            cache[key] = verdict
    return cache, files


def _intake(spec: dict[str, Any], root: Path) -> tuple[list[hd.RolloutItem], list[dict[str, Any]]]:
    items: list[hd.RolloutItem] = []
    rows = []
    seen: set[str] = set()
    for source in spec["sources"]:
        for item in hd.load_rollouts([source["rollouts"]], repo_root=root):
            if item.item_id in seen:
                raise ValueError(f"duplicate intake run_id: {item.item_id}")
            seen.add(item.item_id)
            hd._check_row_dev(item.metadata)
            hd.check_dev_path_allowed(item.task_id)
            path = hd.check_dev_path_allowed(item.source_path or source["rollouts"])
            doc = item.run.doc if item.run else {}
            metadata = [item.metadata, doc]
            for key in ("metadata", "result"):
                if isinstance(doc.get(key), dict):
                    metadata.append(doc[key])
            if isinstance(item.metadata.get("run_meta"), dict):
                metadata.append(item.metadata["run_meta"])
            if path.is_dir():
                for filename in ("result.json", "config.json", "run.json"):
                    sidecar = path / filename
                    if sidecar.is_file():
                        metadata.append(read_json(sidecar))
            reward = next((m["reward"] for m in metadata if m.get("reward") is not None), None)
            if reward is None:
                for document in metadata:
                    verifier_result = document.get("verifier_result")
                    if not isinstance(verifier_result, dict):
                        continue
                    rewards = verifier_result.get("rewards")
                    if isinstance(rewards, dict) and rewards.get("reward") is not None:
                        reward = rewards["reward"]
                        break
            model = next((m["model"] for m in metadata if m.get("model") is not None), None)
            rows.append(
                {
                    "run_id": item.item_id,
                    "corpus": source["corpus"],
                    "task_id": item.task_id,
                    "model": model,
                    "reward": reward,
                    "rollout_path": str(path),
                    "sha256": digest(path),
                }
            )
            items.append(item)
    return items, rows


def _tasks(spec: dict[str, Any], items: list[hd.RolloutItem]) -> dict[str, dict[str, str]]:
    source = hd.load_certified_tasks(spec["task_status"]) if spec["task_status"] else {}
    result = {}
    for task in sorted({item.task_id for item in items}):
        hd.check_dev_path_allowed(task)
        info = dict(
            source.get(
                task,
                source.get(task.split("/")[-1], {"status": "uncertified", "reason": "NO_EVIDENCE"}),
            )
        )
        if info.get("status") not in STATUSES:
            info = {"status": "uncertified", "reason": info.get("reason") or "NO_EVIDENCE"}
        result[task] = info
    return result


def _evaluate(
    spec: dict[str, Any],
    items: list[hd.RolloutItem],
    intake: list[dict[str, Any]],
    tasks: dict[str, dict[str, str]],
    cache: dict[str, hd.StageVerdict],
    stored_ar: dict[str, hd.StageVerdict] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    decisions, traces = [], []
    state: dict[str, Any] = {"calls": 0, "spent_usd": 0.0}
    mode = spec["autoreview"]["mode"]
    for item, run in zip(items, intake, strict=True):
        task = hd.evaluate_task_stage(item.task_id, tasks, default_action=spec["task_action"])
        l0 = hd.evaluate_l0_stage(item)
        if stored_ar is not None:
            ar = stored_ar[item.item_id]
        elif mode == "cached":
            ar = cache.get(item.item_id)
            if ar is None:
                raise ValueError(f"missing cached AutoReview verdict: {item.item_id}")
        else:
            ar = hd.evaluate_autoreview_stage(
                item,
                l0,
                enabled=mode == "live",
                model_route=spec["autoreview"].get("route") or "",
                max_model_usd=spec["autoreview"]["max_usd"],
                max_calls=len(items),
                state=state,
            )
        decision = hd.consolidate_decisions(item, task, l0, ar).to_manifest_row()
        audit = decision["decision"] == "keep" and audit_sample(
            spec["audit"]["seed"], item.item_id, spec["audit"]["rate"]
        )
        if audit:
            decision["reasons"] = sorted(set(decision["reasons"]) | {"AUDIT_SAMPLE"})
        decision.update(run_id=item.item_id, corpus=run["corpus"], audit=audit)
        decisions.append(decision)
        traces.append(
            {
                "run_id": item.item_id,
                "l0": asdict(l0),
                "autoreview": {
                    **asdict(ar),
                    "mode": mode,
                    "score": ar.details.get("suspicion"),
                    "cited_steps": ar.evidence_steps,
                },
            }
        )
    return decisions, traces, state


def _queue(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": row["run_id"],
            "reason": ",".join(row["reasons"]),
            "priority": 1 if row["decision"] == "review" else 0,
            "source": "line",
            "reveal": {"decision": row["decision"], "audit": row["audit"]},
        }
        for row in decisions
        if row["decision"] == "review" or row["audit"]
    ]


def run_line(spec_path: Path, repo_root: Path) -> Path:
    from evallab.line.scoreboard import build_scoreboard, render_scoreboard
    from evallab.production.factory_autoreview import append_judge_queue_entry
    from evallab.production.judge_report import effective_records, load_rows
    from evallab.production.judge_station import STATE_DIR, QueueEntry

    spec = load_spec(spec_path)
    intake_started = time.time()
    batch = Path(spec["out_root"]) / spec["batch_id"]
    if batch.exists():
        raise ValueError(f"batch already exists; choose a new batch_id: {batch}")
    # Admit every source before producing a batch or touching a queue.
    items, intake = _intake(spec, repo_root)
    task_started = time.time()
    tasks = _tasks(spec, items)
    cache, cache_files = _cache(spec)
    if spec["autoreview"]["mode"] == "cached":
        missing = sorted({item.item_id for item in items} - cache.keys())
        if missing:
            raise ValueError(f"missing cached AutoReview verdict: {missing[0]}")
    rev, dirty = _code(repo_root)
    batch.mkdir(parents=True)
    write_json(batch / "spec.json", spec)

    def receipt(
        stage: str,
        started: float,
        inputs: list[Path],
        outputs: list[Path],
        rows_in: int,
        rows_out: int,
        *,
        spend: float = 0,
        models: list[dict[str, str]] | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        finished = time.time()
        write_json(
            batch / "receipts" / f"{stage}.json",
            {
                "schema": "evallab.line_receipt/v1",
                "batch_id": spec["batch_id"],
                "stage": stage,
                "code_rev": rev,
                "code_dirty": dirty,
                "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
                "finished_at": datetime.fromtimestamp(finished, UTC).isoformat(),
                "seconds": finished - started,
                "inputs": [{"path": str(p), "sha256": digest(p)} for p in dict.fromkeys(inputs)],
                "outputs": [
                    {"path": str(p), "sha256": digest(p), "rows": len(read_rows(p))}
                    for p in outputs
                ],
                "counts": {"rows_in": rows_in, "rows_out": rows_out},
                "models": models or [],
                "spend_usd": spend,
                "errors": errors or [],
            },
        )

    start = intake_started
    write_rows(batch / "runs.jsonl", intake)
    source_inputs = [Path(s["rollouts"]) for s in spec["sources"]] + [
        Path(r["rollout_path"]) for r in intake
    ]
    receipt(
        "intake",
        start,
        [batch / "spec.json", *source_inputs],
        [batch / "runs.jsonl"],
        len(items),
        len(items),
    )
    start = task_started
    write_rows(batch / "tasks.jsonl", [{"task_id": key, **value} for key, value in tasks.items()])
    receipt(
        "task",
        start,
        [batch / "runs.jsonl", *([Path(spec["task_status"])] if spec["task_status"] else [])],
        [batch / "tasks.jsonl"],
        len(items),
        len(tasks),
    )
    start = time.time()
    decisions, traces, state = _evaluate(spec, items, intake, tasks, cache)
    frozen_ar = {
        row["run_id"]: hd.StageVerdict(
            **{
                k: v
                for k, v in row["autoreview"].items()
                if k in {"stage", "decision", "reasons", "evidence_steps", "details"}
            }
        )
        for row in traces
    }
    second, _, _ = _evaluate(
        spec,
        items,
        intake,
        tasks,
        cache,
        frozen_ar if spec["autoreview"]["mode"] == "live" else None,
    )
    write_rows(batch / "trace.jsonl", traces)
    errors = [
        {"class": reason.split(":", 1)[-1], "message": reason, "run_id": row["run_id"]}
        for row in traces
        for reason in row["autoreview"]["reasons"]
        if reason.startswith("AUTOREVIEW_ERROR:")
    ]
    models = (
        [{"role": "autoreview", "route": spec["autoreview"]["route"]}]
        if spec["autoreview"]["mode"] == "live" and state["calls"]
        else []
    )
    receipt(
        "trace",
        start,
        [batch / "runs.jsonl", *cache_files],
        [batch / "trace.jsonl"],
        len(items),
        len(traces),
        spend=state["spent_usd"],
        models=models,
        errors=errors,
    )
    start = time.time()
    write_rows(batch / "decisions.jsonl", decisions)
    receipt(
        "decisions",
        start,
        [batch / "tasks.jsonl", batch / "trace.jsonl"],
        [batch / "decisions.jsonl"],
        len(items),
        len(decisions),
    )
    start = time.time()
    queue = _queue(decisions)
    write_rows(batch / "judge-queue.jsonl", queue)
    if spec["judge"]["write"]:
        for row in queue:
            append_judge_queue_entry(Path(spec["judge"]["queue"]), QueueEntry(**row))
    verdict_source = Path(spec["judge"].get("verdicts") or STATE_DIR / "verdicts.jsonl")
    verdicts = (
        [
            row
            for row in effective_records(load_rows(verdict_source))
            if row.get("run_id") in {r["run_id"] for r in intake}
        ]
        if verdict_source.is_file()
        else []
    )
    write_rows(batch / "verdicts.jsonl", verdicts)
    receipt(
        "judge",
        start,
        [batch / "decisions.jsonl", *([verdict_source] if verdict_source.is_file() else [])],
        [batch / "judge-queue.jsonl", batch / "verdicts.jsonl"],
        len(decisions),
        len(queue),
    )
    start = time.time()
    exports = []
    for decision, name in (("keep", "kept"), ("drop", "dropped"), ("review", "review")):
        path = batch / "export" / f"{name}.jsonl"
        write_rows(path, [row for row in decisions if row["decision"] == decision])
        exports.append(path)
    receipt("export", start, [batch / "decisions.jsonl"], exports, len(decisions), len(decisions))
    start = time.time()
    receipt(
        "scoreboard",
        start,
        [batch / "decisions.jsonl", *[Path(s["labels"]) for s in spec["sources"] if s["labels"]]],
        [],
        len(decisions),
        len(decisions),
    )
    write_json(
        batch / "check.json",
        {
            "checks": [{"name": name, "status": "PENDING", "details": ""} for name in CHECK_NAMES],
            "passed": False,
            "decision_hash": canonical_hash(decisions),
            "second_decision_hash": canonical_hash(second),
        },
    )
    score = build_scoreboard(batch, repo_root)
    write_json(batch / "scoreboard.json", score)
    write_json(batch / "check.json", check_batch(batch, repo_root))
    score = build_scoreboard(batch, repo_root)
    write_json(batch / "scoreboard.json", score)
    (batch / "SCOREBOARD.md").write_text(render_scoreboard(score), encoding="utf-8")
    return batch


def check_batch(batch: Path, repo_root: Path) -> dict[str, Any]:
    """Re-derive all eight gates; any missing/corrupt evidence fails closed."""
    from evallab.line.scoreboard import build_scoreboard

    checks = []
    previous = read_json(batch / "check.json") if (batch / "check.json").is_file() else {}

    def gate(name: str, function: Any) -> None:
        try:
            function()
            checks.append({"name": name, "status": "PASS", "details": ""})
        except (ValueError, OSError, KeyError, TypeError, AssertionError) as error:
            checks.append(
                {"name": name, "status": "FAIL", "details": str(error) or type(error).__name__}
            )

    spec = load_spec(batch / "spec.json")
    runs = read_rows(batch / "runs.jsonl")
    decisions = read_rows(batch / "decisions.jsonl")
    receipts = [read_json(p) for p in sorted((batch / "receipts").glob("*.json"))]

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    def conservation() -> None:
        ids = [r["run_id"] for r in runs]
        require(len(ids) == len(set(ids)), "duplicate intake IDs")
        require(Counter(ids) == Counter(d["run_id"] for d in decisions), "intake/decision mismatch")
        require(
            all(
                d["decision"] in {"keep", "drop", "review"}
                and isinstance(d["audit"], bool)
                and (not d["audit"] or d["decision"] == "keep")
                for d in decisions
            ),
            "invalid decisions or audits",
        )
        for d in decisions:
            require(
                d["audit"]
                == (
                    d["decision"] == "keep"
                    and audit_sample(spec["audit"]["seed"], d["run_id"], spec["audit"]["rate"])
                ),
                "audit sample changed",
            )

    gate("conservation", conservation)

    def dev() -> None:
        items, actual = _intake(spec, repo_root)
        require(actual == runs, "intake source identity changed")
        require(
            _tasks(spec, items)
            == {
                r["task_id"]: {"status": r["status"], "reason": r["reason"]}
                for r in read_rows(batch / "tasks.jsonl")
            },
            "task source changed",
        )
        _cache(spec)

    gate("dev_only", dev)

    def provenance() -> None:
        require(
            {r["stage"] for r in receipts}
            == {"intake", "task", "trace", "decisions", "judge", "export", "scoreboard"},
            "missing stage receipts",
        )
        for receipt in receipts:
            require(
                receipt.get("schema") == "evallab.line_receipt/v1"
                and bool(receipt.get("code_rev")),
                "receipt missing revision/schema",
            )
            for ref in receipt["inputs"] + receipt["outputs"]:
                path = Path(ref["path"])
                require(digest(path) == ref["sha256"], f"hash mismatch: {path}")
                if "rows" in ref:
                    require(len(read_rows(path)) == ref["rows"], f"row mismatch: {path}")

    gate("provenance", provenance)

    def determinism() -> None:
        items, intake = _intake(spec, repo_root)
        cache, _ = _cache(spec)
        stored_ar = None
        if spec["autoreview"]["mode"] == "live":
            stored_ar = {
                r["run_id"]: hd.StageVerdict(
                    **{
                        k: v
                        for k, v in r["autoreview"].items()
                        if k in {"stage", "decision", "reasons", "evidence_steps", "details"}
                    }
                )
                for r in read_rows(batch / "trace.jsonl")
            }
        a, _, _ = _evaluate(spec, items, intake, _tasks(spec, items), cache, stored_ar)
        b, _, _ = _evaluate(spec, items, intake, _tasks(spec, items), cache, stored_ar)
        require(
            canonical_hash(a)
            == canonical_hash(b)
            == canonical_hash(decisions)
            == previous.get("decision_hash")
            == previous.get("second_decision_hash"),
            "decision hashes differ",
        )

    gate("determinism", determinism)

    def spending() -> None:
        spend = sum(r["spend_usd"] for r in receipts)
        require(spend >= 0 and spend <= spec["autoreview"]["max_usd"], "spend exceeds cap")
        require(
            all(m["route"] in spec["models"]["allowed"] for r in receipts for m in r["models"]),
            "model not allowed",
        )
        require(spec["autoreview"]["mode"] == "live" or spend == 0, "non-live stage spent money")

    gate("spend_and_models", spending)

    def routing() -> None:
        require(
            read_rows(batch / "judge-queue.jsonl") == _queue(decisions),
            "Judge queue differs from reviews + audits",
        )
        if spec["judge"]["write"]:
            external = (
                Counter(r["run_id"] for r in read_rows(Path(spec["judge"]["queue"])))
                if Path(spec["judge"]["queue"]).is_file()
                else Counter()
            )
            require(
                all(external[r["run_id"]] == 1 for r in _queue(decisions)),
                "external queue missing/duplicate run",
            )

    gate("judge_routing", routing)

    def isolation() -> None:
        forbidden = {
            "label",
            "labels",
            "is_cheat",
            "is_exploit",
            "ground_truth",
            "expected_verdict",
        }

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                require(not forbidden.intersection(value), "label field in decisions")
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(decisions)
        label_paths = {str(Path(s["labels"])) for s in spec["sources"] if s["labels"]}
        require(
            all(
                not label_paths.intersection(ref["path"] for ref in r["inputs"])
                for r in receipts
                if r["stage"] != "scoreboard"
            ),
            "labels entered a decision stage",
        )

    gate("label_isolation", isolation)

    def scoreboard() -> None:
        require(
            read_json(batch / "scoreboard.json") == build_scoreboard(batch, repo_root),
            "scoreboard differs from recomputation",
        )

    gate("scoreboard_recompute", scoreboard)
    return {
        "checks": checks,
        "passed": all(c["status"] == "PASS" for c in checks),
        "decision_hash": previous.get("decision_hash"),
        "second_decision_hash": previous.get("second_decision_hash"),
    }
