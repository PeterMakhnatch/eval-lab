"""Normalize Harbor trials into one stock-shaped ATIF file per trial.

Why: analysis tools (Inspect Scout, Docent, Phoenix, ``harbor view``) read
Harbor's ATIF natively, but two things in our runs break them:

* **Continuations.** With Terminus-2 ``linear_history``, a run that hits
  context summarization is split into ``trajectory.json`` +
  ``trajectory.cont-N.json`` (+ ``trajectory.summarization-N-*.json``
  subagent files). Docent's converter rejects such trials; Scout imports
  each file as a separate transcript; Phoenix drops tails whose
  ``continued_trajectory_ref`` chain is broken (0758-c cont-31).
* **raw_content mode.** Terminus-2 ``trajectory_config.raw_content=True``
  stores the raw model reply as the step message and writes NO
  ``tool_calls``, so every tool sees zero tool calls.

This script stitches each trial exactly the way probe-03 does
(``capabilities.assemble_trial``: duplicate / cumulative_superset /
new_session / head_missing) and restores ``tool_calls`` in stock Terminus-2
shape (``bash_command`` {keystrokes, duration}, ``mark_task_complete``,
ids ``call_<episode>_<n>``, observation ``source_call_id`` when one command):

* ``recorded`` -- the harness's own record of what it executed
  (``extra.step_layers`` accepted calls, HAR-81 onward);
* ``replay``   -- no record (HAR-90): the Eval Lab MiMo normalizer that the
  harness ran (``--evallab-src``);
* ``native``   -- the step already had ``tool_calls`` (non-raw runs): kept;
* no calls: ``rejected`` (recorded parse error, or the observation shows
  the harness rejected the turn), ``harness_standin`` (Harbor's
  "Technical difficulties" placeholder, not model output), ``none``
  (accepted turn that proposed nothing).

The raw model message is kept verbatim. Each step's
``extra.trace_lab`` records the probe-03 ref (``head#12``,
``trajectory.cont-1.json#40``), source file and original step id, so
probe-03 labels map onto normalized steps. Raw trial files are never
modified; outputs mirror ``<out>/<job>/<trial>/`` with config/result/
verifier copies and a single ``agent/trajectory.json``, validated against
Harbor's ``Trajectory`` model.

    uv run --no-project --python 3.12 --with harbor==0.21.0 python harbor_normalize.py \\
        <job_or_runs_dir>... --out DIR [--evallab-src PATH] [--check capabilities.jsonl]

$0: reads local files only; no model calls, no uploads.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "probe-03-capabilities"))
import capabilities as cap  # noqa: E402  (probe-03 assembly + acceptance rules)

probe02 = cap.probe02
JOB_FILES = ("config.json", "result.json")
TRIAL_FILES = ("config.json", "result.json", "exception.txt")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strip_subagent_refs(step: dict) -> list[dict]:
    """Remove ``subagent_trajectory_ref`` from observation results (the
    summarization content already opens the next linear document)."""
    removed: list[dict] = []
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return removed
    for result in observation.get("results") or []:
        if isinstance(result, dict) and result.get("subagent_trajectory_ref") is not None:
            removed.append(result.pop("subagent_trajectory_ref"))
    return removed


def _replay_calls(message: str, normalize_fn) -> tuple[list[dict], bool]:
    """(commands [{keystrokes, duration}], task_complete) the harness parser
    derives from a raw message."""
    try:
        normalized = normalize_fn(message) if normalize_fn else None
    except Exception:  # noqa: BLE001 -- one bad message must not kill the trial
        normalized = None
    for text in (normalized, message):
        if not text:
            continue
        try:
            value = json.loads(text.strip())
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        commands = [
            {"keystrokes": str(c.get("keystrokes") or ""), "duration": c.get("duration")}
            for c in value.get("commands") or [] if isinstance(c, dict)
        ]
        return commands, bool(value.get("task_complete"))
    return [], False


def _tool_calls(step: dict, episode: int, normalize_fn) -> tuple[list[dict] | None, str]:
    """(tool_calls or None, source) for one agent step."""
    if step.get("tool_calls"):
        return step["tool_calls"], "native"
    if str(step.get("message") or "").strip() == cap.HARNESS_STANDIN:
        return None, "harness_standin"
    layer = cap.layer_status(step)
    if layer is not None:
        if cap.recorded_acceptance(layer) == "false":
            return None, "rejected"
        commands = [
            {"keystrokes": str(c.get("keystrokes") or c.get("command") or ""),
             "duration": c.get("duration_sec", c.get("duration"))}
            for c in layer["calls"]
            if isinstance(c, dict) and ("keystrokes" in c or "command" in c)
        ]
        complete = bool(layer.get("task_complete"))
        source = "recorded"
    else:
        accepted, _rule = cap.harness_accepted(cap.obs_content(step))
        if accepted == "false":
            return None, "rejected"
        commands, complete = _replay_calls(str(step.get("message") or ""), normalize_fn)
        source = "replay"
    calls = [
        {"tool_call_id": f"call_{episode}_{n}", "function_name": "bash_command",
         "arguments": {"keystrokes": c["keystrokes"], "duration": c["duration"]}}
        for n, c in enumerate(commands, start=1)
    ]
    if complete:
        calls.append({"tool_call_id": f"call_{episode}_task_complete",
                      "function_name": "mark_task_complete", "arguments": {}})
    return (calls or None), (source if calls else "none")


def normalize_trial(trial: Path, normalize_fn) -> tuple[dict, dict]:
    """Return (trajectory, manifest row)."""
    coverage, assembled = cap.assemble_trial(trial)
    agent = trial / "agent"
    docs = sorted(agent.glob("trajectory*.json"))
    head_doc = None
    for name in ["trajectory.json"] + [f"trajectory.cont-{n}.json" for n in coverage["cont_files"]]:
        path = agent / name
        if path.is_file():
            head_doc = json.loads(path.read_text(encoding="utf-8"))
            break
    if head_doc is None:
        raise ValueError(f"{trial}: no readable trajectory document")

    steps: list[dict] = []
    sources: dict[str, int] = {}
    stripped_refs: list[dict] = []
    episode = 0
    for index, (docname, raw) in enumerate(assembled, start=1):
        step = copy.deepcopy(raw)
        ref = cap.step_ref(docname, raw)
        refs = _strip_subagent_refs(step)
        stripped_refs.extend({"ref": ref, "subagent_trajectory_ref": r} for r in refs)
        trace_lab = {"ref": ref, "source_file": "trajectory.json" if docname == "head" else docname,
                     "orig_step_id": raw.get("step_id")}
        if str(step.get("source", "")).lower() in probe02.AGENT_SOURCES:
            calls, source = _tool_calls(step, episode, normalize_fn)
            episode += 1
            if calls:
                step["tool_calls"] = calls
            else:  # stock files omit the key; Phoenix's importer rejects null
                step.pop("tool_calls", None)
            trace_lab["tool_calls_source"] = source
            sources[source] = sources.get(source, 0) + 1
            results = (step.get("observation") or {}).get("results") or []
            bash = [c for c in calls or [] if c["function_name"] == "bash_command"]
            if source != "native" and len(bash) == 1 and len(results) == 1 and isinstance(results[0], dict):
                results[0]["source_call_id"] = bash[0]["tool_call_id"]
        step["step_id"] = index
        extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
        step["extra"] = {**extra, "trace_lab": trace_lab}
        steps.append(step)

    trajectory = {k: copy.deepcopy(v) for k, v in head_doc.items()
                  if k not in ("steps", "continued_trajectory_ref", "final_metrics")}
    trajectory["steps"] = steps
    if coverage.get("last_doc_final_metrics"):
        trajectory["final_metrics"] = coverage["last_doc_final_metrics"]
    inputs = [{"file": p.name, "sha256": _sha256(p)} for p in docs]
    top_extra = trajectory.get("extra") if isinstance(trajectory.get("extra"), dict) else {}
    trajectory["extra"] = {**top_extra, "trace_lab": {
        "normalizer": "trace-lab/normalize/harbor_normalize.py",
        "assembly_pattern": coverage["assembly_pattern"],
        "assembly_notes": coverage["notes"],
        "inputs": inputs,
        "stripped_subagent_refs": stripped_refs,
        "tool_calls_sources": sources,
    }}
    row = {
        "trial": trial.name,
        "job": trial.parent.name,
        "assembly_pattern": coverage["assembly_pattern"],
        "input_files": [i["file"] for i in inputs],
        "steps": len(steps),
        "agent_steps": sum(sources.values()),
        "tool_calls": sum(len(s.get("tool_calls") or []) for s in steps),
        "bash_calls": sum(1 for s in steps for c in s.get("tool_calls") or []
                          if c.get("function_name") == "bash_command"),
        "tool_calls_sources": sources,
        "probe03_assembled_steps": coverage["assembled_steps"],
        "probe03_assembled_agent_steps": coverage["assembled_agent_steps"],
    }
    return trajectory, row


def validate(trajectory: dict) -> str | None:
    try:
        from harbor.models.trajectories import Trajectory
    except ImportError:
        return "skipped: harbor not importable (run with --with harbor==0.21.0)"
    try:
        Trajectory.model_validate(trajectory)
    except Exception as exc:  # noqa: BLE001 -- reported per trial
        return f"invalid: {str(exc)[:500]}"
    return None


def write_trial(trial: Path, out_root: Path, trajectory: dict) -> Path:
    job_out = out_root / trial.parent.name
    trial_out = job_out / trial.name
    (trial_out / "agent").mkdir(parents=True, exist_ok=True)
    for name in JOB_FILES:
        if (trial.parent / name).is_file():
            shutil.copy2(trial.parent / name, job_out / name)
    for name in TRIAL_FILES:
        if (trial / name).is_file():
            shutil.copy2(trial / name, trial_out / name)
    if (trial / "verifier").is_dir():
        shutil.copytree(trial / "verifier", trial_out / "verifier", dirs_exist_ok=True)
    path = trial_out / "agent" / "trajectory.json"
    path.write_text(json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+", help="trial, job, or runs dirs")
    parser.add_argument("--out", required=True, help="output root (mirrors <job>/<trial>)")
    parser.add_argument("--evallab-src", help="Eval Lab src/ providing normalize_mimo_tool_calls (replay)")
    parser.add_argument("--check", help="probe-03 capabilities.jsonl: step counts must match")
    args = parser.parse_args(argv)

    provenance, normalize_fn = cap.load_normalizer(args.evallab_src)
    expected: dict[str, dict] = {}
    if args.check:
        for line in Path(args.check).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                expected[row["trial"]] = row.get("coverage") or {}

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    rows, failures = [], 0
    for root in map(Path, args.roots):
        for trial in probe02.find_trials(root):
            try:
                trajectory, row = normalize_trial(trial, normalize_fn)
            except (OSError, ValueError) as exc:
                print(f"error {trial.name}: {exc}", file=sys.stderr)
                failures += 1
                continue
            row["validation"] = validate(trajectory) or "ok"
            if args.check:
                cov = expected.get(trial.name)
                row["probe03_match"] = (
                    "missing" if cov is None else
                    "ok" if (cov.get("assembled_steps"), cov.get("assembled_agent_steps"))
                    == (row["steps"], row["agent_steps"]) else
                    f"mismatch: probe-03 {cov.get('assembled_steps')}/{cov.get('assembled_agent_steps')}"
                )
            if row["validation"].startswith("invalid"):
                failures += 1
            row["output"] = str(write_trial(trial, out_root, trajectory).relative_to(out_root))
            rows.append(row)
    manifest = {"normalizer_provenance": provenance, "trials": rows}
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    calls = sum(r["bash_calls"] for r in rows)
    mismatches = sum(1 for r in rows if r.get("probe03_match", "ok") != "ok")
    print(f"{len(rows)} trials -> {out_root} | {calls} bash calls | "
          f"{failures} invalid/errors | {mismatches} probe-03 mismatches | normalizer {provenance['mode']}")
    return 1 if failures or mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
