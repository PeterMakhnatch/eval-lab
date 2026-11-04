"""Run at most 100 strict tasks through the unchanged contained transport.

The existing runner owns task execution, grading, watchdogs, and cleanup.
This driver adds exact-canary admission, quota gates, and a submitted-state diff.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

TRANSPORT = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
MODELS = {"openai-codex/gpt-6.1-sol", "openai-codex/gpt-6.1-terra"}
NOFALLBACK = Path("/private/tmp/ov1106/shared/nofallback.yml")
NOFALLBACK_BYTES = b"retry:\n  modelFallback: false\n"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)


def quota(omp: Path, output: Path) -> bool:
    result = subprocess.run([str(omp), "usage", "--json", "--redact"],
                            capture_output=True, text=True, check=True, timeout=90)
    document = json.loads(result.stdout)
    codex, gemini = [], []
    for report in document["reports"]:
        for limit in report.get("limits", []):
            if report["provider"] == "openai-codex" and limit.get("scope", {}).get("windowId") == "7d":
                codex.append(limit["amount"]["usedFraction"])
            if (report["provider"] == "google-antigravity" and "gemini-weekly" in limit["id"]):
                gemini.append(limit["amount"]["usedFraction"])
    allowed = bool(codex and gemini and max(codex) < 0.82 and max(gemini) < 0.78)
    save(output, {"recorded_at": datetime.now(UTC).isoformat(), "allowed": allowed,
                  "codex_weekly": codex, "gemini_weekly": gemini, "report": document})
    return allowed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--canary", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--max-runs", type=int, default=100)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.max_runs <= 100 or not 1 <= args.batch_size <= 10:
        raise ValueError("Run limit 1..100 and batch size 1..10 required")
    args.work = args.work.resolve()
    args.output = args.output.resolve()
    here = Path(__file__).resolve().parent
    original = here.parent / "ov1104-impossible"
    sys.path.insert(0, str(original))
    spec = importlib.util.spec_from_file_location("impscale_original_runner", original / "batch_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    ct = runner.ct
    canary = json.loads(args.canary.read_text())
    expected_canary = digest(args.canary)

    def canary_gate() -> dict:
        if (digest(args.canary) != expected_canary or digest(Path(ct.__file__)) != TRANSPORT
                or canary["runtime"]["transport_sha256"] != TRANSPORT
                or digest(runner.OMP) != canary["runtime"]["binary_sha256"]
                or not all(canary.get(key) is True for key in ("passed", "cleanup_ok", "audit_complete"))
                or canary.get("outside_workspace_access") is not False
                or canary.get("secrets_forwarded") is not False
                or not ct.registry_ok(canary["registry"], canary["model"])):
            raise RuntimeError("Exact-transport canary admission failed")
        driver = json.loads(args.canary.with_name("driver-receipt.json").read_text())
        if not driver["passed"] or driver["receipt_sha256"] != expected_canary:
            raise RuntimeError("Canary driver binding failed")
        if NOFALLBACK.read_bytes() != NOFALLBACK_BYTES:
            raise RuntimeError("Required no-fallback overlay changed")
        return canary

    canary_gate()
    plan = json.loads(args.plan.read_text())
    approval = json.loads(args.approval.read_text())
    if (approval.get("approved") is not True
            or approval.get("plan_sha256") != digest(args.plan)
            or approval.get("canary_sha256") != expected_canary
            or not 1 <= approval.get("max_runs", 0) <= 100
            or args.max_runs > approval["max_runs"]):
        raise ValueError("Parent approval must bind this plan, canary, and run cap")
    rows = plan[:args.max_runs]
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("A nonempty unique run plan is required")
    for row in rows:
        if row["model"] not in MODELS or row["layout"] != "v2":
            raise ValueError("Only named Codex routes and certified v2 tasks are allowed")
        if row["variant"] != "original" and not row["variant"].startswith("conflicting_certified"):
            raise ValueError("Uncertified variant")
        if row["split"] != "dev" or row["required_transport_sha256"] != TRANSPORT:
            raise ValueError("Only exact-transport DEV rows are admitted")
        task = args.work / "tasks-v2" / row["task_id"]
        controls = json.loads((task / "controls.json").read_text())
        if controls.get("control_eligible") is not True:
            raise ValueError("Task lacks executed eligible controls")
        if (digest(task / "controls.json") != row["controls_sha256"]
                or digest(task / "generated.json") != row["generated_sha256"]):
            raise ValueError("Task control or generated bytes changed")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runner.WORK = args.work
    runner.RUNS = args.output / "runs"
    runner.JAIL = args.output / "jails"
    runner.CANARY = args.canary.resolve()
    runner.canary_gate = canary_gate
    runner.PROMPT += "\nFor every bash tool call, set cwd explicitly to {workdir}.\n"
    original_prepare, original_tests = runner.prepare, runner.run_tests
    prepared = {}
    original_task_parts = runner.task_parts

    def task_parts(task_id: str, layout: str = "v2"):
        directory, metadata, generated = original_task_parts(task_id, layout)
        generated["variant_sha"] = {
            name: files[generated["mutated_file"]]
            for name, files in generated["variant_test_sha256"].items()
        }
        return directory, metadata, generated

    runner.task_parts = task_parts

    def prepare(row: dict):
        rdir, workspace = original_prepare(row)
        baseline = rdir / "input-tree"
        shutil.copytree(workspace, baseline, symlinks=True)
        prepared[row["id"]] = (rdir, workspace, baseline, row)
        return rdir, workspace

    def capture_submission(config: dict, command: str) -> dict:
        run_id = Path(config["control"]).parent.name
        rdir, workspace, baseline, row = prepared[run_id]
        if not (rdir / "submitted-diff.json").exists():
            result = subprocess.run(["diff", "-ruN", "--exclude=.git", "--exclude=__pycache__",
                                     "--exclude=.pytest_cache", str(baseline), str(workspace)],
                                    capture_output=True, text=True, check=False)
            if result.returncode not in (0, 1):
                raise RuntimeError("Could not capture submitted workspace")
            body = result.stdout.replace(str(baseline) + "/", "a/").replace(str(workspace) + "/", "b/")
            (rdir / "final.diff").write_text(body)
            files = [line[4:].split("\t")[0] for line in body.splitlines() if line.startswith("+++ ")]
            save(rdir / "submitted-diff.json", {"diff_sha256": digest(rdir / "final.diff"),
                                                "diff_chars": len(body), "changed_files": files,
                                                "capture_phase": "before_G1_before_test_restoration",
                                                "baseline": "agent_input_tree_including_constructed_tests"})
        return original_tests(config, command)

    def captured_diff(workspace: Path, rdir: Path, base: Path) -> dict:
        metadata = json.loads((rdir / "submitted-diff.json").read_text())
        if digest(rdir / "final.diff") != metadata["diff_sha256"]:
            raise RuntimeError("Submitted diff changed after capture")
        return {key: metadata[key] for key in ("diff_sha256", "diff_chars", "changed_files")}

    class AgentProcess(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            try:
                result = super().communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill()
                super().communicate()
                raise
            if self.returncode:
                raise RuntimeError(f"Contained agent failed with return code {self.returncode}")
            return result

    def popen(argv, **kwargs):
        if argv[0] == str(runner.OMP):
            canary_gate()
            argv = [*argv, "--config", str(NOFALLBACK)]
            control = Path(argv[argv.index("--cwd") + 1])
            save(control.parent / "actual-launch.json", {
                "argv": argv, "nofallback_sha256": digest(NOFALLBACK)})
            return AgentProcess(argv, **kwargs)
        return subprocess.Popen(argv, **kwargs)

    runner.subprocess = types.SimpleNamespace(**{
        name: popen if name == "Popen" else getattr(subprocess, name)
        for name in dir(subprocess) if not name.startswith("__")
    })
    runner.prepare, runner.run_tests, runner.capture = prepare, capture_submission, captured_diff
    save(args.output / "campaign.json", {"plan_sha256": digest(args.plan), "driver_sha256": digest(Path(__file__)),
                                         "runner_sha256": digest(original / "batch_runner.py"),
                                         "approval_sha256": digest(args.approval),
                                         "transport_sha256": TRANSPORT, "canary_sha256": expected_canary,
                                         "max_runs": args.max_runs, "planned": len(rows), "workers": args.workers,
                                         "nofallback_sha256": digest(NOFALLBACK),
                                         "paid_infra_usd": 0, "started_at": datetime.now(UTC).isoformat()})
    outcomes = []
    for start in range(0, len(rows), args.batch_size):
        if not quota(runner.OMP, args.output / f"quota-{start:03d}.json"):
            outcomes.append({"status": "quota_stop", "next_plan_index": start})
            break
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(runner.execute, row, canary_gate()): row for row in rows[start:start + args.batch_size]}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    future.result()
                    receipt_path = runner.RUNS / row["id"] / "receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    if (not receipt["passed"] or receipt["canary"]["sha256"] != expected_canary
                            or receipt["runtime"]["transport_sha256"] != TRANSPORT):
                        raise RuntimeError("Run receipt does not bind exact passing canary")
                    outcome = {"id": row["id"], "status": "complete", "receipt_sha256": digest(receipt_path)}
                except Exception as error:
                    outcome = {"id": row["id"], "status": "error", "error": f"{type(error).__name__}: {error}"}
                outcomes.append(outcome)
                with (args.output / "outcomes.jsonl").open("a") as stream:
                    stream.write(json.dumps(outcome) + "\n")
                print(json.dumps(outcome), flush=True)
    save(args.output / "summary.json", {"completed_at": datetime.now(UTC).isoformat(), "outcomes": outcomes,
                                        "attempted": sum("id" in row for row in outcomes),
                                        "complete": sum(row["status"] == "complete" for row in outcomes),
                                        "errors": sum(row["status"] == "error" for row in outcomes),
                                        "quota_stopped": any(row["status"] == "quota_stop" for row in outcomes),
                                        "paid_infra_usd": 0})


if __name__ == "__main__":
    main()
