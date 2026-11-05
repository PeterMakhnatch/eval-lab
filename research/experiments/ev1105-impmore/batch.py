"""Finite contained trials with safe post-agent I/O and Engine-authoritative grades."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import types
from datetime import UTC, datetime
from pathlib import Path

import grading
import safe_io
from campaign import CONFIG, MODELS, ROOT, IsolatedProcess, quota_probe

HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "ov1104-impossible"
EPOCH = "safe-postprocess-v1"
SOURCES = [HERE / name for name in ("batch.py", "campaign.py", "safe_io.py", "grading.py", "container_tool.py")]
SOURCES += [BASE / name for name in ("batch_runner.py", "container_tool.py")]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def main(argv, work, transport_sha):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("plan", "output", "canary", "approval", "qualification"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--workers", type=int, choices=(1,), default=1)
    parser.add_argument("--batch-size", type=int, choices=(1, 2), default=2)
    args = parser.parse_args(argv)
    if not 1 <= args.max_runs <= 100:
        raise ValueError("Run limit must be 1..100")
    args.output = args.output.resolve()
    spec = importlib.util.spec_from_file_location("impmore_safe_base_runner", BASE / "batch_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    ct = runner.ct
    canary = json.loads(args.canary.read_text())
    qualification = json.loads(args.qualification.read_text())
    source_hashes = {str(p.relative_to(ROOT)): digest(p) for p in SOURCES}
    canary_sha = digest(args.canary)
    plan = json.loads(args.plan.read_text())
    approval = json.loads(args.approval.read_text())
    if not (approval.get("approved") is True and approval["plan_sha256"] == digest(args.plan)
            and approval["canary_sha256"] == canary_sha and args.max_runs <= approval["max_runs"] <= 100
            and approval["qualification_sha256"] == digest(args.qualification)):
        raise ValueError("Approval must bind plan, canary, qualification, and limit")
    rows = plan[:args.max_runs]
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Unique nonempty plan required")
    for row in rows:
        if (row["model"] not in MODELS or row["split"] != "dev" or row["layout"] != "v2"
                or row["variant"] not in ("original", "conflicting_certified_bool")
                or row["required_transport_sha256"] != transport_sha or row["runner_epoch"] != EPOCH):
            raise ValueError("Unsupported execution identity")
        task = work / "tasks-v2" / row["task_id"]
        if not (json.loads((task / "controls.json").read_text())["control_eligible"] is True
                and digest(task / "controls.json") == row["controls_sha256"]
                and digest(task / "generated.json") == row["generated_sha256"]):
            raise ValueError("Task control bindings differ")

    def canary_gate():
        if not (digest(args.canary) == canary_sha
                and digest(Path(ct.__file__)) == transport_sha == canary["runtime"]["transport_sha256"]
                and digest(runner.OMP) == canary["runtime"]["binary_sha256"]
                and all(canary.get(k) is True for k in ("passed", "cleanup_ok", "audit_complete"))
                and canary.get("outside_workspace_access") is False and canary.get("secrets_forwarded") is False
                and ct.registry_ok(canary["registry"], canary["model"])
                and qualification.get("passed") is True
                and not args.qualification.with_name("SAFETY_STOP.json").exists()
                and qualification["safe_io_sha256"] == digest(HERE / "safe_io.py")
                and qualification["grading_sha256"] == digest(HERE / "grading.py")
                and digest(args.qualification) == approval["qualification_sha256"]
                and qualification["runtime_control_count"] == qualification["runtime_controls_passed"] == 20
                and {row["task_id"] for row in rows} <= set(qualification["task_ids"])
                and qualification["transport_sha256"] == transport_sha
                and source_hashes == {str(p.relative_to(ROOT)): digest(p) for p in SOURCES}
                and CONFIG.read_bytes() == b"retry:\n  modelFallback: false\n"):
            raise ValueError("Exact boundary qualification failed")
        driver = json.loads(args.canary.with_name("driver-receipt.json").read_text())
        if driver.get("passed") is not True or driver["receipt_sha256"] != canary_sha:
            raise ValueError("Canary driver binding differs")
        for item in qualification["evidence"]:
            if digest(item["path"]) != item["sha256"]:
                raise ValueError("Boundary qualification evidence changed")
        return canary

    canary_gate()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot = args.output.parent / "source"
    snapshot.mkdir(exist_ok=False)
    shutil.copyfile(__file__, snapshot / "batch.py")
    save(args.output / "adapter-source.json", {"epoch": EPOCH, "files": source_hashes,
         "qualification_path": str(args.qualification.resolve()), "qualification_sha256": digest(args.qualification),
         "created_at": datetime.now(UTC).isoformat()})
    runner.WORK, runner.RUNS, runner.JAIL = work, args.output / "runs", args.output / "jails"
    runner.CANARY, runner.canary_gate = args.canary.resolve(), canary_gate
    runner.PROMPT += "\nFor every bash tool call, set cwd explicitly to {workdir}.\n"
    base_prompt = runner.PROMPT
    original_prepare, original_parts = runner.prepare, runner.task_parts
    prepared = {}
    graders = {}

    def task_parts(task_id, layout="v2"):
        task, meta, generated = original_parts(task_id, layout)
        generated["variant_sha"] = {name: files[generated["mutated_file"]]
                                    for name, files in generated["variant_test_sha256"].items()}
        return task, meta, generated

    def prepare(row):
        rdir, workspace = original_prepare(row)
        canonical = dict(entry.split("=", 1) for entry in safe_io.image_environment({
            "docker_env": {"DOCKER_HOST": os.environ["OV1104_DOCKER_HOST"]}, "image_id": row["image_id"]}))
        note = f"\nUse HOME={canonical['HOME']} when a task command needs the image's installed environment.\n"
        runner.PROMPT = base_prompt + note.replace("{", "{{").replace("}", "}}")
        baseline = rdir / "input-tree"
        shutil.copytree(workspace, baseline, symlinks=True)
        prepared[row["id"]] = (rdir, workspace, baseline)
        return rdir, workspace

    def run_tests(config, command):
        run_id = Path(config["control"]).parent.name
        rdir, workspace, baseline = prepared[run_id]
        if run_id not in graders:
            safe_io.capture_submission(config, baseline, workspace, rdir, destroy_agent=True)
            agent_absence = grading.absence(config)
            grade_config, watchdog, inspection = grading.start(config, rdir / "grader-control")
            graders[run_id] = grade_config, watchdog
            environment = safe_io.image_environment(grade_config)
            save(rdir / "grade-boundary.json", {"agent_absence": agent_absence,
                 "grader_inspection": inspection, "image_environment_sha256": hashlib.sha256(json.dumps(environment).encode()).hexdigest()})
        return safe_io.run_tests(graders[run_id][0], command)

    def restore(config, row, workspace):
        task, _, generated = task_parts(row["task_id"], row["layout"])
        safe_io.restore_tests(graders[row["id"]][0],
                              {rel: (task / "variants" / row["variant"] / rel).resolve(strict=True)
                               for rel in generated["test_files"]}, workspace)

    def captured(workspace, rdir, base):
        metadata = json.loads((rdir / "submitted-diff.json").read_text())
        if digest(rdir / "final.diff") != metadata["diff_sha256"]:
            raise ValueError("Submitted diff changed")
        return {key: metadata[key] for key in ("diff_sha256", "diff_chars", "changed_files")}

    class AgentProcess(IsolatedProcess):
        def communicate(self, input=None, timeout=None):
            try:
                result = super().communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill()
                super().communicate()
                raise
            if self.returncode:
                raise RuntimeError(f"Contained agent failed: {self.returncode}")
            return result

    def popen(argv, **kwargs):
        if str(argv[0]) == str(runner.OMP):
            canary_gate()
            argv = [*argv, "--config", str(CONFIG)]
            control = Path(argv[argv.index("--cwd") + 1])
            save(control.parent / "actual-launch.json", {"argv": argv, "nofallback_sha256": digest(CONFIG)})
            return AgentProcess(argv, **kwargs)
        return subprocess.Popen(argv, **kwargs)

    runner.subprocess = types.SimpleNamespace(**{name: popen if name == "Popen" else getattr(subprocess, name)
                                                for name in dir(subprocess) if not name.startswith("__")})
    runner.task_parts, runner.prepare, runner.run_tests = task_parts, prepare, run_tests
    runner.restore_tests, runner.capture = restore, captured
    save(args.output / "campaign.json", {"plan_sha256": digest(args.plan), "driver_sha256": digest(__file__),
         "runner_sha256": digest(BASE / "batch_runner.py"), "approval_sha256": digest(args.approval),
         "transport_sha256": transport_sha, "canary_sha256": canary_sha,
         "max_runs": args.max_runs, "planned": len(rows), "workers": args.workers,
         "nofallback_sha256": digest(CONFIG), "paid_infra_usd": 0, "started_at": datetime.now(UTC).isoformat()})
    outcomes = []
    (args.output / "outcomes.jsonl").touch(exist_ok=False)
    for start in range(0, len(rows), args.batch_size):
        quota_path = args.output / f"quota-{start:03d}.json"
        try:
            allowed = quota_probe(runner.OMP, quota_path, {r["model"] for r in rows})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            save(quota_path, {"allowed": False, "probe_failed": True, "error_type": type(error).__name__,
                 "error": "Quota probe fails; no further model requests are authorized.", "stop_reason": "quota_probe_failed"})
            outcomes.append({"status": "quota_probe_failed", "next_plan_index": start,
                             "quota_receipt": quota_path.name, "quota_receipt_sha256": digest(quota_path)})
            break
        if not allowed:
            outcomes.append({"status": "quota_stop", "next_plan_index": start,
                             "quota_receipt": quota_path.name, "quota_receipt_sha256": digest(quota_path)})
            break
        for row in rows[start:start + args.batch_size]:
            try:
                runner.execute(row, canary_gate())
                receipt_path = runner.RUNS / row["id"] / "receipt.json"
                receipt = json.loads(receipt_path.read_text())
                if not receipt["passed"] or receipt["canary"]["sha256"] != canary_sha:
                    raise ValueError("Run receipt fails canary binding")
                outcome = {"id": row["id"], "status": "complete", "receipt_sha256": digest(receipt_path)}
            except Exception as error:
                outcome = {"id": row["id"], "status": "error", "error": f"{type(error).__name__}: {error}"}
            outcomes.append(outcome)
            with (args.output / "outcomes.jsonl").open("a") as stream:
                stream.write(json.dumps(outcome) + "\n")
            print(json.dumps(outcome), flush=True)
            directory = runner.RUNS / row["id"]
            grader_cleanup = {"verified_absent": True, "not_started": True}
            try:
                context = graders.pop(row["id"], None)
                if context is not None:
                    grader_cleanup = grading.close(*context)
                elif (directory / "grader-control/transport.json").exists():
                    grader_cleanup = grading.absence(json.loads((directory / "grader-control/transport.json").read_text()))
                elif (directory / "grader-control").exists():
                    grader_cleanup = {"verified_absent": False, "error": "Grader creation has no usable configuration"}
            except Exception as error:
                grader_cleanup = {"verified_absent": False, "error_type": type(error).__name__}
            save(directory / "grader-cleanup.json", grader_cleanup)
            verified_absent = False
            cleanup = {"verified_absent": False, "recorded_at": datetime.now(UTC).isoformat()}
            try:
                config = json.loads((directory / "control/transport.json").read_text())
                probe = ct.docker(config, "inspect", config["container_id"])
                error_text = (probe.stderr or "").lower()
                verified_absent = grader_cleanup["verified_absent"] is True and probe.returncode != 0 and any(
                    marker + config["container_id"] in error_text
                    for marker in ("no such object: ", "no such container: "))
                cleanup.update(verified_absent=verified_absent, container_id=config["container_id"],
                               returncode=probe.returncode, stderr=(probe.stderr or "")[-1000:])
            except Exception as error:
                cleanup["error_type"] = type(error).__name__
            save(directory / "post-cleanup.json", cleanup)
            if not verified_absent:
                flag = args.qualification.with_name("SAFETY_STOP.json")
                if not flag.exists():
                    save(flag, {"run_id": row["id"], "reason": "Owned container absence is not verified"})
                raise RuntimeError("Safety stop: no later trial starts before verified container removal")
    save(args.output / "summary.json", {"completed_at": datetime.now(UTC).isoformat(), "outcomes": outcomes,
         "attempted": sum("id" in r for r in outcomes), "complete": sum(r["status"] == "complete" for r in outcomes),
         "errors": sum(r["status"] == "error" for r in outcomes),
         "quota_stopped": any(r["status"] == "quota_stop" for r in outcomes),
         "quota_probe_failed": any(r["status"] == "quota_probe_failed" for r in outcomes),
         "stop_reason": next((r["status"] for r in outcomes if "id" not in r), "completed"), "paid_infra_usd": 0})
    return 0
