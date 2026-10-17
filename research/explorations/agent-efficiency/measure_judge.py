"""Exercise frozen-window judging and count its transient reference footprint.

This is a deterministic application/API smoke, not a model-quality experiment.
Only step coordinates and error flags are projected from a frozen trace cohort;
all commands, observations, free-form messages and reasoning are excluded.
OMP tokenizers are offline encoders, never local generative inference.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from evallab import analyst
from evallab.lance import TrajectoryWindowV1, build_trajectory_windows
from evallab.schemas import ConfidenceClaim, EvidenceCitation
from evallab.traj import outline_trajectory

POLICY = "step-coordinate-and-error-flag-projection/v1:exclude-all-free-form-text"
RUBRIC = "Mechanical frozen-source citation binding only; do not infer a causal diagnosis."


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def private_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def baseline_module(root: Path, output: Path) -> tuple[ModuleType, str]:
    study = json.loads((Path(__file__).parent / "study.json").read_bytes())
    revision = study["baseline_revision"]
    source = subprocess.check_output(
        ["git", "show", f"{revision}:src/evallab/analyst.py"],
        cwd=root,
        timeout=60,
    )
    path = output / "baseline-analyst.py"
    private_bytes(path, source)
    name = "_agent_efficiency_baseline_analyst"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen analyst source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, sha256(source)


class BindingControl:
    """Return fixed, explicitly non-semantic judgments through the real API seam."""

    model = "deterministic-source-binding-control-not-a-frontier-model"

    def __init__(
        self,
        module: ModuleType,
        windows: list[TrajectoryWindowV1],
        *,
        compact: bool,
    ) -> None:
        self.module = module
        self.windows = windows
        self.compact = compact
        self.requests: list[tuple[str, str, Any]] = []

    def analyze(self, prompt: str, context: str) -> Any:
        final = "Stage: FINAL" in prompt
        result = self.module.AnalystResult(
            category="uncertain",
            summary="Mechanical binding control; no semantic diagnosis is being scored.",
            evidence=[
                EvidenceCitation(path="W9" if self.compact else self.windows[8].window_digest)
            ],
            contradicting_evidence=(
                [EvidenceCitation(path="W2" if self.compact else self.windows[1].window_digest)]
                if final
                else []
            ),
            alternative_explanations=["The projection intentionally excludes causal evidence."]
            if final
            else [],
            confidence=ConfidenceClaim(
                level="low", n=1, provenance_digest=self.windows[8].source_digest
            ),
        )
        self.requests.append((prompt, context, result))
        return result


def offline_count(path: Path) -> list[dict[str, Any]]:
    content = path.read_bytes()
    result = json.loads(subprocess.check_output(["omp", "toks", str(path), "--json"], timeout=60))
    source = result.get("source", {})
    if (
        source.get("kind") != "file"
        or Path(source.get("path", "")).resolve() != path.resolve()
        or result.get("bytes") != len(content)
        or result.get("chars") != len(content.decode())
    ):
        raise RuntimeError("offline tokenizer counted something other than the intended file")
    encodings = result.get("encodings", [])
    if not encodings or any(
        type(row.get("tokens")) is not int or row["tokens"] < 0 for row in encodings
    ):
        raise RuntimeError("offline tokenizer returned missing or invalid counts")
    return encodings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output_dir.resolve()
    if not output.is_relative_to(Path("/tmp/agent-efficiency").resolve()):
        parser.error("private measurement output must remain under /tmp/agent-efficiency")
    manifest_bytes = args.cohort.read_bytes()
    manifest = json.loads(manifest_bytes)
    snapshot = "sha256:" + sha256(manifest_bytes)
    policy = "sha256:" + sha256(POLICY.encode())
    runs_root = Path(manifest["cohort"]["results_root"]) / manifest["cohort"]["date"]
    pinned: dict[Path, str] = {}
    windows: list[TrajectoryWindowV1] = []
    for index, task in enumerate(manifest["tasks"], 1):
        for record in task["source_files"]:
            path = (Path(task["source_root"]) / record["path"]).resolve()
            expected = record["sha256"]
            if sha256(path.read_bytes()) != expected:
                raise ValueError("frozen source changed before the binding smoke")
            pinned[path] = expected
        outline = outline_trajectory(
            task["trial_dir"], repo_root=root, explicit_runs_root=runs_root
        )
        projection = [
            {
                "step_id": step.step_id,
                "source_step_id": step.source_step_id,
                "is_error": step.is_error,
            }
            for step in outline.steps
        ]
        windows.extend(
            build_trajectory_windows(
                projection,
                snapshot_digest=snapshot,
                source_digest="sha256:" + task["source_digest"],
                redaction_policy_digest=policy,
                source_is_redacted=True,
                job_id="binding-footprint-smoke",
                trial_id=f"S{index}",
                window_steps=8,
                stride_steps=8,
            )
        )
    if len(windows) < 9:
        parser.error("the fixed late-window binding control requires at least nine windows")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    old, baseline_sha = baseline_module(root, output)
    controls = {
        "baseline": BindingControl(old, windows, compact=False),
        "candidate": BindingControl(analyst, windows, compact=True),
    }
    results: dict[str, Any] = {}
    rows = []
    facts = {
        "projection": POLICY,
        "source_count": len(manifest["tasks"]),
        "semantic_quality_measured": False,
    }
    for arm, control in controls.items():
        module = control.module
        context = module.create_judge_deterministic_context(
            snapshot_digest=snapshot,
            source_digest=snapshot,
            facts=facts,
        )
        runs, disagreement = module.run_trajectory_judge(
            control,
            windows,
            rubric=RUBRIC,
            deterministic_context=context,
            repeats=1,
        )
        if len(control.requests) != 3:
            raise RuntimeError("valid citation controls unexpectedly required repair")
        results[arm] = {
            "runs": [run.model_dump(mode="json") for run in runs],
            "disagreement": disagreement.model_dump(mode="json"),
        }
        private_bytes(output / f"{arm}-canonical-records.json", canonical(results[arm]))
        for stage, (prompt, text, reply) in zip(
            ("triage", "inspect", "final"), control.requests, strict=True
        ):
            pieces = {
                "input_prompt_and_context": (prompt + "\n\n" + text).encode(),
                "transient_reply_control": json.dumps(
                    dataclasses.asdict(reply),
                    sort_keys=True,
                    default=lambda item: item.model_dump(mode="json"),
                ).encode(),
            }
            for kind, content in pieces.items():
                path = output / f"{arm}-{stage}-{kind}.txt"
                private_bytes(path, content)
                rows.append(
                    {
                        "arm": arm,
                        "stage": stage,
                        "kind": kind,
                        "file": path.name,
                        "sha256": sha256(content),
                        "bytes": len(content),
                        "encodings": offline_count(path),
                    }
                )
    if canonical(results["baseline"]) != canonical(results["candidate"]):
        raise RuntimeError("compact transient references changed canonical persisted results")
    if any(sha256(path.read_bytes()) != expected for path, expected in pinned.items()):
        raise RuntimeError("frozen source changed during the binding smoke")
    report = {
        "schema": "agent-efficiency-judge-binding-footprint/v1",
        "cohort_manifest_sha256": sha256(manifest_bytes),
        "measurement_script_sha256": sha256(Path(__file__).read_bytes()),
        "baseline_source_sha256": baseline_sha,
        "candidate_source_sha256": sha256((root / "src/evallab/analyst.py").read_bytes()),
        "window_count": len(windows),
        "source_count": len(manifest["tasks"]),
        "projection_policy": POLICY,
        "model_calls": 0,
        "canonical_records_identical": True,
        "quality_scope": "Real API and exact canonical citations/hashes on coordinate-only projections; deterministic replies are not model judgments or semantic accuracy.",
        "token_scope": "Embedded offline encoding compatibility labels, not served-model/provider tokens. Input counts omit transport wrappers; transient reply control counts are not generated model output.",
        "omp_version": subprocess.check_output(["omp", "--version"], timeout=30).decode().strip(),
        "rows": rows,
    }
    private_bytes(
        output / "results.json", json.dumps(report, indent=2, allow_nan=False).encode() + b"\n"
    )
    print(
        json.dumps(
            {
                "output": str(output / "results.json"),
                "windows": len(windows),
                "canonical_records_identical": True,
                "model_calls": 0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
