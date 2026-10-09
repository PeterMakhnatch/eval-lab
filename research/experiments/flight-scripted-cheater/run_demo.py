"""Drive the scripted-cheater demo: stub -> capture tap -> real NativeMimoAgent trial.

Chain: the mimoagent worker (host loop; only shell commands run in-sandbox)
talks DIRECTLY to ``evallab capture serve`` (host loopback, full-body tap),
which forwards to this directory's ``stub_server`` (host loopback, scripted
cheating replies with ``reasoning_content``).

Direct ``harbor run`` (not ``evallab flight record``) is used because the
evallab direct path is control-agents-only by standing policy and the queue
path needs a human spend approval; this run provably spends $0 (the only
upstream is the local stub). Everything else mirrors ``flight record``: the
same locked environment, the same flight plugin + ``.flight`` config, and
``--capture-dir`` so the timeline joins model + trajectory + kernel planes.
$0: local Docker, stdlib stub, no vendor calls.

Usage: ``uv run --extra laminar python run_demo.py [--name NAME]``
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent  # worktree root
STUB_PORT = 18081
STUB_URL = f"http://127.0.0.1:{STUB_PORT}"
MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
AGENT = "mimoagent"
TASK = "library/tasks/event-summary"

def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            return
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {path}")


def main() -> int:
    name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else "flight-cheater-demo"
    demo_dir = ROOT / "runs" / name
    capdir = demo_dir / "capture"
    capdir.mkdir(parents=True, exist_ok=True)

    stub_log = demo_dir / "stub-requests.jsonl"
    stub = subprocess.Popen(
        [sys.executable, str(HERE / "stub_server.py"), str(STUB_PORT), str(stub_log)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        capture = subprocess.Popen(
            ["uv", "run", "--extra", "laminar", "evallab", "capture", "serve",
             "--upstream", STUB_URL, "--out", str(capdir)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for(capdir / "capture.json")
            endpoint = json.loads((capdir / "capture.json").read_text())["endpoint"]
            print(f"tap: {STUB_URL} <- {endpoint}", flush=True)
            from evallab.execution_contracts import RunRequest
            from evallab.execution_contracts import build_command as harbor_command
            from evallab.flight.plugin import configuration_path

            jobs_dir = ROOT / "runs"
            request = RunRequest(
                task=ROOT / TASK, agent=AGENT, name=name, jobs_dir=jobs_dir,
                environment="docker", egress_lock=True, model=MODEL,
                concurrency=1, attempts=1, timeout_seconds=1800,
                allow_billable=True,  # $0: the only upstream is the local stub
            )
            config_path = configuration_path(jobs_dir / name)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with config_path.open("x", encoding="utf-8") as config:
                    json.dump({"schema": "evallab.flight.config/v1", "egress": "locked"}, config)
            except FileExistsError:
                print(f"error: flight config already exists: {config_path}", flush=True)
                return 1
            trial_env = dict(os.environ)
            trial_env["EVALLAB_TERMINUS_PROXY_URL"] = endpoint
            trial_env["EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY"] = "demo-trial-capability"
            trial_env["EVALLAB_MODEL_CAPTURE"] = "1"
            trial_env["EVALLAB_MODEL_CAPTURE_DIR"] = str(capdir)
            trial = subprocess.run(
                harbor_command(request), cwd=ROOT, env=trial_env,
                capture_output=True, text=True, timeout=1800)
            (demo_dir / "harbor.stdout.log").write_text(trial.stdout)
            (demo_dir / "harbor.stderr.log").write_text(trial.stderr)
            print(f"harbor exit: {trial.returncode}", flush=True)
            config_path.unlink(missing_ok=True)
            if trial.returncode != 0:
                print(trial.stderr[-3000:], flush=True)
                return 1
            show = subprocess.run(
                ["uv", "run", "--extra", "laminar", "evallab", "flight", "show",
                 str(jobs_dir / name), "--capture-dir", str(capdir), "--json"],
                cwd=ROOT, capture_output=True, text=True, timeout=300)
            (demo_dir / "show.stdout.json").write_text(show.stdout)
            if show.returncode != 0:
                print(show.stderr[-3000:], flush=True)
                return 1
            summary = json.loads(show.stdout)["summary"]
        finally:
            capture.terminate()
    finally:
        stub.terminate()
    timeline = Path(summary["timeline"])
    rows = [json.loads(line) for line in timeline.read_text().splitlines() if line.strip()]
    print(f"timeline: {timeline} ({len(rows)} rows)", flush=True)
    print("planes:", dict(Counter((row["plane"], row["kind"]) for row in rows)), flush=True)
    # Excerpt: the blocked-egress story across planes, newest evidence first.
    def story_text(row: dict) -> str | None:
        detail = row.get("detail", {}) if isinstance(row.get("detail"), dict) else {}
        kind = row.get("kind")
        if kind == "model_call":
            messages = (detail.get("request_body") or {}).get("messages", [])
            return f"request {len(messages)} msgs model={detail.get('model')}"
        if kind == "model_reasoning":
            text = str(detail.get("text", ""))
            if "package index" not in text and "pypi" not in text.lower():
                return None
            return f"reasoning: {text[:200]}"
        if kind == "tool_call":
            raw = detail.get("raw_call", {}) if isinstance(detail, dict) else {}
            command = (raw.get("arguments", {}) or {}).get("command", "")
            if "curl" not in command and "socket" not in command:
                return None
            return f"agent tool {raw.get('function_name')}: {command[:160]}"
        if kind == "exec":
            argv = detail.get("args", []) or []
            blob = json.dumps(argv)
            if "curl" not in blob and "socket" not in blob:
                return None
            return f"exec pid={row.get('pid')} argv={argv[:4]}"
        if kind in ("connect", "udp_send"):
            if detail.get("dest") is None:
                return None
            return (f"{kind} {detail.get('dest')}:{detail.get('port')} "
                    f"{detail.get('outcome')}/{detail.get('errno')}")
        if kind == "exit" and detail.get("comm") in ("curl", "python3", "python"):
            return f"exit comm={detail.get('comm')} code={detail.get('exit_code')}"
        return None

    print("--- blocked-egress story ---", flush=True)
    shown = 0
    for row in rows:
        text = story_text(row)
        if text is None:
            continue
        print(f"{row['ts']} [{row['plane']}/{row['kind']}] {text}", flush=True)
        shown += 1
        if shown >= 25:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
