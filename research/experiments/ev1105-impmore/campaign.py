"""Contained execution and source-bound admission for the safe postprocess epoch."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PREVIOUS = ROOT / "research/experiments/ov1106-impscale"
LANE = Path("/private/tmp/ev1105/impmore")
CONFIG = Path("/private/tmp/ev1105/shared/nofallback.yml")
TRANSPORT_PATH = Path(__file__).with_name("container_tool.py")
MODELS = {"openai-codex/gpt-6.1-sol", "openai-codex/gpt-6.1-terra",
          "google-antigravity/gemini-4-argon", "google-antigravity/gemini-3.8-flash"}


def load(name):
    sys.path.insert(0, str(PREVIOUS))
    spec = importlib.util.spec_from_file_location("impmore_" + name, PREVIOUS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IsolatedProcess(subprocess.Popen):
    def __init__(self, argv, **kwargs):
        if str(argv[0]).endswith("/omp"):
            kwargs["env"] = {**kwargs.get("env", os.environ), "PI_CONFIG_FILES": str(CONFIG)}
        super().__init__(argv, **kwargs)


def process_proxy():
    return types.SimpleNamespace(**{name: IsolatedProcess if name == "Popen" else getattr(subprocess, name)
                                   for name in dir(subprocess) if not name.startswith("__")})


def environment():
    if CONFIG.read_bytes() != b"retry:\n  modelFallback: false\n":
        raise ValueError("No-fallback configuration differs")
    os.environ["PI_CONFIG_FILES"] = str(CONFIG)
    os.environ["OV1104_DOCKER_HOST"] = "unix:///Users/petermakhnatch/.docker/run/docker.sock"
    os.environ["OMP_AUTH_BROKER_URL"] = "http://127.0.0.1:19055"
    os.environ["OMP_AUTH_BROKER_TOKEN"] = (Path.home() / ".omp/auth-broker.token").read_text().strip()


def quota_probe(omp, output, selected):
    records = []
    providers = {model.split("/")[0] for model in selected}
    for attempt in range(7):
        document = {"reports": []}
        for provider in sorted(providers):
            subprocess.run([str(omp), "usage", "invalidate", "--provider", provider],
                           capture_output=True, text=True, timeout=90, check=True)
            result = subprocess.run([str(omp), "usage", "--provider", provider, "--json", "--redact"],
                                    capture_output=True, text=True, timeout=90, check=True)
            document["reports"].extend(json.loads(result.stdout)["reports"])
        fractions = {}
        for report in document["reports"]:
            for limit in report.get("limits", []):
                provider = report["provider"]
                if ((provider == "openai-codex" and limit.get("scope", {}).get("windowId") == "7d")
                        or (provider == "google-antigravity" and "gemini-weekly" in limit["id"])):
                    value = limit["amount"]["usedFraction"]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                        raise ValueError("Invalid weekly quota")
                    fractions[provider] = max(fractions.get(provider, 0), value)
        if not providers <= fractions.keys():
            raise ValueError("Missing selected provider quota")
        records.append({"recorded_at": datetime.now(UTC).isoformat(), "fractions": fractions, "report": document})
        blocked_codex = "openai-codex" in providers and fractions["openai-codex"] >= .99
        blocked_gemini = "google-antigravity" in providers and fractions["google-antigravity"] >= .90
        if blocked_gemini or not blocked_codex or attempt == 6:
            allowed = not blocked_codex and not blocked_gemini
            with output.open("x") as stream:
                json.dump({"allowed": allowed, "probe_failed": False, "observations": records,
                           "codex_pause_seconds": 300, "paid_infra_usd": 0}, stream, indent=2)
            return allowed
        time.sleep(300)
    raise AssertionError("unreachable")


def audit_module(work):
    import grading
    module = load("scale_summary")
    module.NOFALLBACK, module.MODELS = CONFIG, MODELS
    module.WORK = work / "tasks-v2"
    transport_sha = hashlib.sha256(TRANSPORT_PATH.read_bytes()).hexdigest()
    original_campaign, original_run, original_audit = module.campaign_audit, module.run_audit, module.Audit
    original_transport = ROOT / "research/experiments/ov1104-impossible/container_tool.py"
    required = {f"research/experiments/ev1105-impmore/{name}" for name in
                ("campaign.py", "batch.py", "safe_io.py", "grading.py", "container_tool.py")}
    required |= {f"research/experiments/ov1104-impossible/{name}" for name in
                 ("batch_runner.py", "container_tool.py")}

    class BoundaryAudit(original_audit):
        def bind(self, path, expected):
            if Path(path).resolve() == original_transport and expected == transport_sha:
                path = TRANSPORT_PATH
            return super().bind(path, expected)

    def campaign_audit(audit, root):
        provenance = audit.load(root / "adapter-source.json")
        module.need(provenance["epoch"] == "safe-postprocess-v1", "Unsafe predecessor epoch")
        module.need(set(provenance["files"]) == required, "Incomplete adapter source inventory")
        for relative, expected in provenance["files"].items():
            audit.bind(ROOT / relative, expected)
        qualification_path = Path(provenance["qualification_path"])
        audit.bind(qualification_path, provenance["qualification_sha256"])
        qualification = audit.load(qualification_path)
        module.need(qualification["passed"] is True
                    and qualification["safe_io_sha256"] == provenance["files"]["research/experiments/ev1105-impmore/safe_io.py"]
                    and qualification["grading_sha256"] == provenance["files"]["research/experiments/ev1105-impmore/grading.py"]
                    and qualification["transport_sha256"] == transport_sha, "Boundary qualification differs")
        module.need(bool(qualification["evidence"]), "Empty boundary qualification evidence")
        for item in qualification["evidence"]:
            audit.bind(Path(item["path"]), item["sha256"])
        campaign = audit.load(root / "campaign.json")
        module.need(campaign["transport_sha256"] == transport_sha, "Unknown contained transport")
        _, approval = audit.source(root.parent, campaign["approval_sha256"])
        module.need(approval.get("qualification_sha256") == provenance["qualification_sha256"],
                    "Qualification differs from the approved execution boundary")
        audit.bind(original_transport, "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a")
        module.TRANSPORT = transport_sha
        found, metadata = original_campaign(audit, root)
        metadata.update(runner_epoch=provenance["epoch"], adapter_source_sha256=audit.hash(root / "adapter-source.json"))
        return found, metadata

    def run_audit(audit, root, planned, outcome, campaign):
        result = original_run(audit, root, planned, outcome, campaign)
        if result["admitted"]:
            try:
                grade = audit.load(Path(result["grade_path"]))
                directory = Path(result["grade_path"]).parent
                cleanup = audit.load(directory / "post-cleanup.json")
                boundary = audit.load(directory / "grade-boundary.json")
                config = audit.load(directory / "control/transport.json")
                grade_config = audit.load(directory / "grader-control/transport.json")
                grader_inspection = audit.load(directory / "grader-control/grader-inspect.json")
                grader_start = audit.load(directory / "grader-control/grader-start.json")
                grader_cleanup = audit.load(directory / "grader-cleanup.json")
                submitted = audit.load(directory / "submitted-diff.json")
                module.need(submitted["format"] == "safe-io-source-unified-and-explicit-metadata-v1"
                            and submitted["post_capture_action"] == "destroy_owned_agent", "Unsafe capture lifecycle")
                prior = boundary["agent_absence"]
                module.need(prior["verified_absent"] is True and prior["http_status"] == 404
                            and prior["container_id"] == config["container_id"], "Agent remains active during grading")
                module.need(grade_config["container_id"] != config["container_id"]
                            and grade_config["image_id"] == config["image_id"] == planned["image_id"]
                            and grade_config["workspace_source"] == config["workspace_source"],
                            "Separate grader identity differs")
                checks = grading._validate(grade_config, grader_inspection, grader_start["image_environment"],
                                           grader_inspection["Config"].get("User", ""), running=True)
                module.need(checks == grader_start["checks"] and grader_start == boundary["grader_inspection"],
                            "Grader boundary proof differs")
                module.need(grader_cleanup["verified_absent"] is True
                            and grader_cleanup["container_id"] == grade_config["container_id"]
                            and grader_cleanup["absence"]["http_status"] == 404,
                            "Grader removal lacks strict Engine absence")
                module.need(cleanup["verified_absent"] is True and cleanup["container_id"] == config["container_id"]
                            and cleanup["returncode"] != 0 and any(
                                marker + config["container_id"] in cleanup["stderr"].lower()
                                for marker in ("no such object: ", "no such container: ")),
                            "Owned container absence lacks an independent post-cleanup proof")
                for name in ("G1", "G2"):
                    value = grade[name]
                    module.need(value.get("completed") is True
                                and value.get("exit_channel") == "docker-engine-exec-inspect-v1"
                                and value.get("engine_exit_code") == value["rc"]
                                and value.get("image_environment_sha256") == boundary["image_environment_sha256"]
                                and value.get("container_id") == grade_config["container_id"]
                                and isinstance(value.get("exec_id"), str) and len(value["exec_id"]) == 64,
                                f"{name} lacks an authoritative Engine exit status")
            except (module.Invalid, OSError, ValueError, KeyError, TypeError) as error:
                result.update(admitted=False, audit_error=f"{type(error).__name__}: {error}")
        return result

    module.Audit, module.campaign_audit, module.run_audit = BoundaryAudit, campaign_audit, run_audit
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("canary", "batch", "audit"))
    parser.add_argument("--work", type=Path)
    args, remaining = parser.parse_known_args()
    if args.stage == "audit":
        if args.work is None:
            parser.error("--work is required for audit")
        module = audit_module(args.work)
    else:
        environment()
        transport_sha = hashlib.sha256(TRANSPORT_PATH.read_bytes()).hexdigest()
        spec = importlib.util.spec_from_file_location("container_tool", TRANSPORT_PATH)
        transport = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(transport)
        sys.modules["container_tool"] = transport
        if args.stage == "batch":
            if args.work is None:
                parser.error("--work is required for batch")
            spec = importlib.util.spec_from_file_location("impmore_safe_batch", Path(__file__).with_name("batch.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.main(remaining, args.work.resolve(), transport_sha)
        module = load("canary")
        module.EXPECTED, module.NOFALLBACK = transport_sha, CONFIG
        module.subprocess = process_proxy()
    sys.argv = [str(Path(module.__file__)), *remaining]
    return module.main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
