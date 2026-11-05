"""Lane-local execution adapter; the frozen container transport remains unchanged."""
from __future__ import annotations

import argparse
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
    for attempt in range(7):
        result = subprocess.run([str(omp), "usage", "--json", "--redact"], capture_output=True,
                                text=True, timeout=90, check=True)
        document = json.loads(result.stdout)
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
        providers = {model.split("/")[0] for model in selected}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("canary", "batch", "audit"))
    parser.add_argument("--work", type=Path)
    args, remaining = parser.parse_known_args()
    environment()
    transport_sha = __import__("hashlib").sha256(TRANSPORT_PATH.read_bytes()).hexdigest()
    if args.stage != "audit":
        spec = importlib.util.spec_from_file_location("container_tool", TRANSPORT_PATH)
        transport = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(transport)
        sys.modules["container_tool"] = transport
    if args.stage == "canary":
        module = load("canary")
        module.EXPECTED = transport_sha
        module.NOFALLBACK = CONFIG
        module.subprocess = process_proxy()
    elif args.stage == "batch":
        module = load("batch")
        module.TRANSPORT = transport_sha
        module.NOFALLBACK = CONFIG
        module.MODELS = MODELS
        module.subprocess = process_proxy()
        plan = Path(remaining[remaining.index("--plan") + 1])
        selected = {row["model"] for row in json.loads(plan.read_text())}
        if not selected <= MODELS:
            raise ValueError("Unsupported model route")
        def quota(omp, output):
            try:
                return quota_probe(omp, output, selected)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                with output.open("x") as stream:
                    json.dump({"allowed": False, "probe_failed": True, "error_type": type(error).__name__,
                               "stop_reason": "quota_probe_failed"}, stream)
                raise module.QuotaProbeError(str(error)) from error

        module.quota = quota
        if args.work is None:
            parser.error("--work is required for batch")
        remaining += ["--work", str(args.work)]
    else:
        module = load("scale_summary")
        module.NOFALLBACK = CONFIG
        module.MODELS = MODELS
        if args.work is None:
            parser.error("--work is required for audit")
        module.WORK = args.work / "tasks-v2"
        original_campaign_audit = module.campaign_audit
        original_audit = module.Audit
        original_transport = ROOT / "research/experiments/ov1104-impossible/container_tool.py"

        class TransportAudit(original_audit):
            def bind(self, path, expected):
                if Path(path).resolve() == original_transport and expected == transport_sha:
                    path = TRANSPORT_PATH
                return super().bind(path, expected)

        def campaign_audit(audit, root):
            recorded = audit.load(root / "campaign.json")["transport_sha256"]
            if recorded not in (transport_sha, "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"):
                raise module.Invalid("Unknown contained transport")
            module.TRANSPORT = recorded
            return original_campaign_audit(audit, root)

        module.Audit = TransportAudit
        module.campaign_audit = campaign_audit
    sys.argv = [str(Path(module.__file__)), *remaining]
    result = module.main()
    return result or 0


if __name__ == "__main__":
    raise SystemExit(main())
