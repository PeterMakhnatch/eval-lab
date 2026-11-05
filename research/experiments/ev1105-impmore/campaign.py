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
    if args.stage == "canary":
        module = load("canary")
        module.NOFALLBACK = CONFIG
        module.subprocess = process_proxy()
    elif args.stage == "batch":
        module = load("batch")
        module.NOFALLBACK = CONFIG
        module.MODELS = MODELS
        module.subprocess = process_proxy()
        plan = Path(remaining[remaining.index("--plan") + 1])
        selected = {row["model"] for row in json.loads(plan.read_text())}
        if not selected <= MODELS:
            raise ValueError("Unsupported model route")
        module.quota = lambda omp, output: quota_probe(omp, output, selected)
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
    sys.argv = [str(Path(module.__file__)), *remaining]
    result = module.main()
    return result or 0


if __name__ == "__main__":
    raise SystemExit(main())
