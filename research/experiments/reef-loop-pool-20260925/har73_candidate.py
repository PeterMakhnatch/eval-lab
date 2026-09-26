"""Prepare or settle HAR-73's manual AGENTS.md candidate in the real Reef process.

Run with the read-only Reef interpreter and the gate package on PYTHONPATH.
--prepare-only submits ordinary Lab specs, but never approves or executes them.
The default mode evaluates the same candidate and calls real Reef settlement.
No native Reef inference route is enabled: all measurement belongs to Lab specs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

BASE_RULES = (
    "Follow the task instructions. Inspect available files before changing them. "
    "Preserve input data and verify required outputs before reporting completion."
)
CANDIDATE_RULES = BASE_RULES + (
    "\nBefore editing, list the required output files and constraints. "
    "After writing outputs, validate their format, counts, and stated constraints "
    "with a short terminal check. Fix discrepancies before completing the task."
)


def keep_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"refusing to replace retained campaign evidence: {path}") from None


def refuse_native_score(task: str, result: object) -> float:
    raise RuntimeError("HAR-73 measurement must use Lab specs, not native Reef episodes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    # These imports belong only to this separate Reef-process experiment.
    from evallab_reef_gate.plugin import CONFIG_ENV, Factory
    from reef.harness.adapters import get_adapter
    from reef.harness.episodes.model_binding import ModelBinding
    from reef.train.cordis_backend import CordisBackend, Mutation
    from reef.train.cordis_backend.strategies import resolve_episode_scorer, resolve_proposer
    from reef.train.types import TrainingBatch

    os.environ[CONFIG_ENV] = str(args.gate_config.resolve(strict=True))
    factory = Factory()
    if factory.lab is None:
        raise ValueError("this experiment requires an explicit lab gate configuration")
    output = args.output.resolve()
    if output.is_relative_to(factory.reef_root):
        raise ValueError("experiment outputs must remain outside the Reef checkout")
    output.mkdir(parents=True, exist_ok=True)
    if not args.prepare_only and (output / "settlement.json").exists():
        raise ValueError("candidate already settled; inspect its retained evidence")
    split = json.loads(factory.lab.split_path.read_text(encoding="utf-8"))
    tasks = tuple(row["task_id"] for row in split["dev"])
    mutation = Mutation("update", "rules", {
        "name": "rules", "config": {"text": CANDIDATE_RULES},
    })

    def propose(nodes, samples, models):
        return mutation

    seed = (
        {"id": "settings", "name": "config", "config": {"data": {
            "enable_summarize": False, "max_turns": 6, "temperature": 0.0,
            "llm_call_kwargs": {"max_tokens": 8192},
        }}},
        {"id": "rules", "name": "rules", "config": {"text": BASE_RULES}},
    )
    backend = CordisBackend(
        descriptor=get_adapter("terminus"), propose=resolve_proposer(propose),
        score_episode=resolve_episode_scorer(refuse_native_score), tasks=tasks,
        # This unavailable native endpoint is never a fallback. The Lab plugin
        # exclusively owns inference, with its configured model and credentials.
        models=ModelBinding(base_url="http://127.0.0.1:1", model=factory.lab.model),
        binary=str(factory.reef_root / ".venv/bin/reef-terminus"),
        episode_repeats=factory.lab.episode_repeats, episode_workers=1, seed=seed,
    )
    prepared = backend.prepare_step(
        TrainingBatch("har73-agents-validation"), backend.initial_state(), 0,
    )
    candidate = prepared.candidate
    if candidate is None:
        raise RuntimeError("Reef refused to prepare the manual rules mutation")
    keep_json(output / "candidate.json", {
        "candidate_id": candidate.candidate_id,
        "reef_commit": factory.reef_commit,
        "current_files": dict(candidate.current_files),
        "candidate_files": dict(candidate.candidate_files),
        "evaluation_tasks": list(candidate.evaluation_tasks),
        "current_entries": list(candidate.current_entries),
        "candidate_entries": list(candidate.candidate_entries),
    })
    plugin = factory.build(backend)
    if args.prepare_only:
        manifest = plugin.lab_evaluator.prepare(
            candidate.candidate_id, candidate.current_files,
            candidate.candidate_files, candidate.evaluation_tasks,
        )
        keep_json(output / "prepared-specs.json", manifest)
        print(json.dumps(manifest, indent=2, allow_nan=False))
        backend.abort_step(prepared)
        return

    try:
        evaluation = plugin.evaluate(candidate)
        decision = plugin.decide(candidate, evaluation)
        settled = backend.settle_step(prepared, decision)
    except BaseException:
        backend.abort_step(prepared)
        raise
    artifact_path = None
    if settled.artifact is not None:
        source = settled.artifact.local_path
        if source is None:
            raise RuntimeError("expected a local harness artifact from Reef settlement")
        destination = output / "selected-harness"
        shutil.copytree(source, destination)
        artifact_path = str(destination)
        settled.artifact.discard()
    receipt = {
        "candidate_id": candidate.candidate_id,
        "reef_commit": factory.reef_commit,
        "evaluation": evaluation.to_dict(),
        "selection": decision.to_dict(),
        "settled_state": dict(settled.state),
        "settled_metrics": dict(settled.metrics),
        "selected_harness_path": artifact_path,
        "scope": "real CordisBackend prepare/evaluate/decide/settle; no shared service deployment",
    }
    keep_json(output / "settlement.json", receipt)
    print(json.dumps({
        "selected": decision.selected, "reason": decision.reason,
        "receipt": str(output / "settlement.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
