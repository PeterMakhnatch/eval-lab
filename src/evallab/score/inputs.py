"""Joins of explicitly named DEV evidence and cached outputs; no cache discovery."""
from __future__ import annotations

import hashlib
import json
import math
import zipfile
from pathlib import Path
from typing import Any

from evallab import hack_drop
from evallab.line.core import _autoreview_stage
from evallab.line.io import read_rows
from evallab.line.scoreboard import _summary

from .metrics import retained


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source(ref: dict, root: Path, *, optional: bool = False) -> Path:
    path = Path(ref["path"]).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file() and optional:
        return path
    if digest(path) != ref["sha256"]:
        raise ValueError(f"frozen input hash differs: {path}")
    return path


def _unique(rows: list[dict], field: str) -> dict[str, dict]:
    output = {}
    for row in rows:
        key = str(row[field])
        if key in output:
            raise ValueError(f"duplicate {field}: {key}")
        output[key] = row
    return output


def _prediction(score: Any, citations: list | None = None, **extra: Any) -> dict:
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)):
        raise ValueError("invalid cached prediction")
    return {"score": score, "citations": citations or [], **extra}


def _lengths(path: Path) -> tuple[int | None, int | None]:
    """Use DetBench's one view/length definition, restricted to the actor view."""
    doc = json.loads(path.read_text())
    library = retained("detbench_lib")
    steps = doc.get("steps")
    if not isinstance(steps, list):
        return None, None  # Constructed file bundles are not agent trajectories.
    view = doc if steps and "role" in steps[0] else library.build_view("length", steps)
    view = {**view, "steps": [s for s in view["steps"] if s["role"] in {"agent", "tool"}]}
    n_steps, n_chars = library.view_size(view)
    return n_chars, n_steps


def replay(config: dict, root: Path) -> tuple[dict[str, dict], dict[str, dict], dict]:
    """Read stored decisions and raw stage scores, never invoking a line stage."""
    intake = _unique(read_rows(source(config["runs"], root)), "run_id")
    decisions = _unique(cached_rows(config["decisions"], root), "run_id")
    traces = _unique(cached_rows(config["trace"], root), "run_id")
    labels: dict[str, bool] = {}
    for ref in config["labels"]:
        labels.update(hack_drop.load_labels(source(ref, root), repo_root=root, attempted_cheat="exclude"))
    runs, predictions = {}, {name: {} for name in ("L0", "AutoReview", "full_line")}
    for key, row in intake.items():
        if key not in labels:
            raise ValueError(f"publisher label missing: {key}")
        trajectory = config["trajectories"][key]
        path = source(trajectory, root, optional=True) if not trajectory.get("unavailable") else None
        chars, steps = _lengths(path) if path is not None and path.is_file() else (None, None)
        runs[key] = {"run_key": key, "set": row["corpus"], "task": row["task_id"],
                     "label": int(labels[key]), "n_chars": chars, "n_steps": steps,
                     "split": "dev", "model": row.get("model"), "variant": None}
        trace = traces.get(key, {})
        l0 = trace.get("l0")
        if l0:
            scan = l0.get("details", {}).get("scan_l0", {})
            if "has_gate_card" in scan:
                predictions["L0"][key] = _prediction(int(l0["decision"] == "drop"), l0.get("evidence_steps"),
                                                       coordinate="line_step_id")
        ar = trace.get("autoreview", {})
        score = ar.get("score")
        predictions["AutoReview"][key] = _prediction(score, ar.get("evidence_steps"),
            coordinate="line_step_id", cost_usd=ar.get("details", {}).get("cost_usd"))
        if key in decisions:
            decision = decisions[key]
            predictions["full_line"][key] = _prediction({"keep": 0, "review": 1, "drop": 2}[decision["decision"]],
                                                         decision.get("evidence_steps"), coordinate="line_step_id")
    for ref in config.get("autoreview_cached", []):
        for row in cached_rows(ref, root):
            key = row["id"]
            if key not in runs:
                raise ValueError("AutoReview cache contains an unregistered run")
            stage = row["stage_decisions"]["autoreview"]
            parsed = _autoreview_stage(stage, f"score:{key}", row.get("evidence_steps", []))
            if parsed.details.get("suspicion") is None:
                continue
            if parsed.details.get("model") != config["autoreview_model"]:
                raise ValueError("AutoReview model differs from freeze")
            valid = parsed.details.get("attested") is True and "AUTOREVIEW_UNPARSED" not in parsed.reasons
            predictions["AutoReview"][key] = _prediction(parsed.details["suspicion"] if valid else None,
                stage.get("evidence_steps"), coordinate="line_step_id",
                citations_available="evidence_steps" in stage, cost_usd=parsed.details.get("cost_usd"))
    native = [hack_drop.RolloutDecision(id=k, task_id=intake[k]["task_id"], decision=r["decision"],
                reasons=r.get("reasons", []), evidence_steps=r.get("evidence_steps", []),
                stage_decisions=r.get("stage_decisions", {})) for k, r in decisions.items() if k in runs]
    policy = _summary(native, labels, set())
    policy["cached_decision_coverage"] = {"eligible": len(runs), "scored": len(native), "missing": len(runs) - len(native)}
    return runs, predictions, policy


def j3(config: dict, root: Path, eligible: set[str]) -> dict[str, dict]:
    path = source(config, root, optional=True)
    if not path.is_file():
        return {}
    output = {}
    if config.get("member"):
        with zipfile.ZipFile(path) as archive:
            data = archive.read(config["member"])
        if hashlib.sha256(data).hexdigest() != config["member_sha256"]:
            raise ValueError("frozen DEV archive member differs")
        rows = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
    else:
        rows = read_rows(path)
    for row in rows:
        key = row["run_id"]
        if key not in eligible:
            raise ValueError("J3 receipt contains a non-DEV or unregistered run")
        reading = row.get("reading") or row.get("autoreview", {}).get("details", {}).get("j3")
        if not reading:
            continue
        if key in output or reading["route"] != config["model"] or reading["rubric_sha256"] != config["rubric_sha256"] or reading["prompt_template_sha256"] != config["prompt_template_sha256"]:
            raise ValueError("duplicate J3 reading or identity differs from freeze")
        windows = reading.get("windows", [])
        complete = windows and not reading.get("error") and all(
            not w.get("error") and w.get("suspicion") is not None and w.get("served_route") == reading["route"]
            for w in windows)
        output[key] = _prediction(max(w["suspicion"] for w in windows) if complete else None,
            [step for w in windows for step in w.get("evidence_steps", [])], coordinate="line_step_id",
            model=reading["route"], tokens_in=_recorded_sum(windows, "tokens_in"),
            tokens_out=_recorded_sum(windows, "tokens_out"),
            historical_recorded_usd=_recorded_sum(windows, "cost_usd"),
            latency_s=_recorded_sum(windows, "latency_s"))
    return output


def signals(config: dict, root: Path, eligible: set[str]) -> dict[str, dict]:
    path = source(config, root, optional=True)
    if not path.is_file():
        return {}
    output = {}
    for row in read_rows(path):
        key = row["run_key"]
        if key not in eligible:
            raise ValueError("signal contains an unregistered run")
        if row.get("version") != config["version"] or row.get("model") != config.get("model"):
            raise ValueError("cached signal identity differs from freeze")
        output[key] = _prediction(None if row.get("abstain") else row.get("score"),
            row.get("evidence_steps"), coordinate="prepared_step_id", version=row.get("version"),
            model=row.get("model"))
        if key in config.get("readings", {}):
            raw_path = source(config["readings"][key], root, optional=True)
            if raw_path.is_file():
                reading = json.loads(raw_path.read_text())
                if reading["run_key"] != key or reading["model"] != config.get("model"):
                    raise ValueError("cached reading identity differs")
                windows = reading.get("windows", [])
                output[key].update(citations=reading.get("evidence_steps", []),
                    tokens_in=_recorded_sum([w.get("usage", {}) for w in windows], "prompt_tokens"),
                    tokens_out=_recorded_sum([w.get("usage", {}) for w in windows], "completion_tokens"),
                    latency_s=_recorded_sum(windows, "latency_s"))
    return output


def gold_rows(root: Path, state_root: Path | None, *, selected_cohorts: set[str] | None = None,
              unlocked_cohorts: set[str] | None = None) -> tuple[dict[str, dict], dict[str, Any]]:
    """Use Gold's registry and coordinate authority, never a second label registry."""
    from evallab.gold import load_rows
    from evallab.gold.cohorts import LABELLED_ONLY, check_scoring, registrations

    selected, unlocked = selected_cohorts or set(), unlocked_cohorts or set()
    registry = registrations(root)
    check_scoring(registry, selected, unlocked)

    output, originals = {}, {}
    for row in load_rows(root, state_root=state_root):
        scoring = row.metadata.get("scoring", row.metadata.get("use_state"))
        if scoring == LABELLED_ONLY:
            check_scoring({row.source: {"scoring": scoring}}, selected, unlocked)
            if row.source not in selected and f"action-v2/{row.source}" not in selected:
                continue
        if row.split.upper() != "DEV" or row.declared_split.upper() != "DEV":
            raise ValueError("score step 1 permits DEV rows only")
        key = f"{row.source}:{row.subject_id}" if row.protocol == "action-v2" else row.subject_id
        if key in output:
            raise ValueError(f"duplicate Gold run: {key}")
        label = {"executed-positive": 1, "reviewed-control": 0}.get(row.label_state)
        output[key] = {"run_key": key, "set": row.source, "task": row.task_id,
                       "label": label, "label_state": row.label_state, "split": "dev",
                       "model": row.metadata.get("model"), "variant": row.metadata.get("variant"),
                       "exposure": row.exposure, "n_chars": None, "n_steps": None,
                       "gold_label_sha256": identity(row.model_dump(mode="json"))}
        if row.protocol == "action-v2":
            output[key].update(protocol=row.protocol, subject_id=row.subject_id)
        originals[key] = row
    return output, originals


def load(config: dict, root: Path, state_root: Path | None, *, selected_cohorts: set[str] | None = None,
         unlocked_cohorts: set[str] | None = None) -> tuple[dict, dict, dict]:
    from evallab.gold.cohorts import check_scoring, registrations

    check_scoring(registrations(root), selected_cohorts or set(), unlocked_cohorts or set())
    publisher, pred, policy = replay(config["replay"], root)
    pred["J3"] = j3(config["j3"], root, set(publisher))
    gold, originals = gold_rows(root, state_root, selected_cohorts=selected_cohorts,
                               unlocked_cohorts=unlocked_cohorts)
    goodfire, impossible, action_v2 = {}, {}, {}
    for key, row in gold.items():
        if row.get("protocol") == "action-v2":
            subject = row["subject_id"]
            original = originals[key]
            if subject in publisher:
                chars, steps = publisher[subject]["n_chars"], publisher[subject]["n_steps"]
                task = publisher[subject]["task"]
            else:
                chars, steps = _lengths(Path(original.trajectory_ref)) if original.trajectory_ref else (None, None)
                task = row["task"]
            action_v2.setdefault(f"action-v2/{row['set']}", {})[key] = {
                **row, "task": task, "n_chars": chars, "n_steps": steps}
        elif row["set"] == "goodfire-action-v1":
            if key not in publisher:
                raise ValueError(f"Gold Goodfire run does not join replay: {key}")
            goodfire[key] = {**publisher[key], **row, "task": publisher[key]["task"],
                             "n_chars": publisher[key]["n_chars"], "n_steps": publisher[key]["n_steps"]}
        else:
            path = source(config["impossible_trajectories"][key], root, optional=True)
            chars, steps = _lengths(path) if path.is_file() else (None, None)
            impossible[key] = {**row, "n_chars": chars, "n_steps": steps}
    impossible_pred = {name: signals(ref, root, set(impossible)) for name, ref in config["impossible_signals"].items()}
    cohorts = {"A": goodfire, "B": impossible, "C": publisher}
    predictions = {"A": {name: {k: v for k, v in rows.items() if k in goodfire} for name, rows in pred.items()},
                   "B": impossible_pred, "C": pred}
    for source_name in registrations(root):
        panel = f"action-v2/{source_name}"
        if source_name in (selected_cohorts or ()) or panel in (selected_cohorts or ()):
            action_v2.setdefault(panel, {})
    for cohort, runs in action_v2.items():
        cohorts[cohort] = runs
        predictions[cohort] = {
            name: {key: values[row["subject_id"]] for key, row in runs.items()
                   if row["subject_id"] in values}
            for name, values in pred.items()}
    return cohorts, predictions, {"policy_counts": policy, "gold_rows": originals}


def _recorded_sum(rows: list[dict], field: str) -> float | None:
    if not rows:
        return None
    total = 0.0
    for row in rows:
        value = row.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        total += value
    return total


def cached_rows(ref: dict, root: Path) -> list[dict]:
    path = source(ref, root, optional=True)
    return read_rows(path) if path.is_file() else []


def cohort_signature(runs: dict[str, dict]) -> str:
    # Feature availability changes coverage, never cohort membership or ground truth.
    return identity([{k: v for k, v in row.items() if k not in {"n_chars", "n_steps"}}
                     for _, row in sorted(runs.items())])
