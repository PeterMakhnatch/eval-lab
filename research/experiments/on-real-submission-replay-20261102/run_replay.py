#!/usr/bin/env python3
"""A8B: Replay real accepted agent submissions and known-equivalent controls against Gold graders.

Evaluates:
  1. Real frontier agent submissions (original_reward == 1.0) on certified Gold tasks.
  2. Known-equivalent negative controls (semantics-preserving AST rewrites of the reference solution).
  3. Ground-truth reference controls (verifies reference passes Gold grader in the same session).
  4. Adjudication of all flipped/rejected submissions using two independent readers.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import importlib.util
import json
import math
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "research/experiments/on-gold-v1b-20261102"))

from envspec import derive_env_spec  # noqa: E402

from evallab.modal_cells import (  # noqa: E402
    ReserveSettleSpendGate,
    SandboxMeter,
    modal_cell_runner,
)
from evallab.mutant_auditor import _apply_reference, _write, parse_probe  # noqa: E402
from evallab.production.seeker_worker import (  # noqa: E402
    _omp_stream_messages,
    _parse_omp_json_stdout,
    _stream_served_route,
)

# Load stats helper
STATS_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/stats.py")
if STATS_PATH.exists():
    spec = importlib.util.spec_from_file_location("stats", STATS_PATH)
    stats = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(stats)
else:
    class FallbackStats:
        @staticmethod
        def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
            if n == 0:
                return (float("nan"), float("nan"))
            p = k / n
            denom = 1.0 + (z * z) / n
            centre = (p + (z * z) / (2.0 * n)) / denom
            half = (z / denom) * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
            return (max(0.0, centre - half), min(1.0, centre + half))

        @staticmethod
        def fmt(k: int, n: int) -> str:
            if n == 0:
                return "0/0 = 0.0% (nan-nan)"
            lo, hi = FallbackStats.wilson(k, n)
            return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"

    stats = FallbackStats()

# Paths and Constants
MODAL_APPROVAL = "Peter 2026-11-02 overnight A8: Modal cap $1.00"
MODAL_CAP_USD = 1.00
LEDGER_PATH = Path.home() / ".local/state/evallab-failure-library/a8b-ledger.json"
SPEND_JSONL = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"
WORKER_YML = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/worker.yml")

CERT_JSON = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-v1.1-certified.json")
INV_JSON = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/submissions-inventory.json")
UNIV_JSON = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-universe.json")
A2_TASKS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks")
A2B_CTRLS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/FIX-A2B-SPECIFICITY/receipts/known_equivalent_controls.json")
PREREG_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A8B-REAL-SUBMISSION-REPLAY/prereg.md")

BRIDGE_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A8B-REAL-SUBMISSION-REPLAY/receipts")
DURABLE_ROOT = Path.home() / "Developer/eval-lab-results/2026-11-02/A8B-REAL-SUBMISSION-REPLAY"
CELLS_DIR = DURABLE_ROOT / "cells"

J1_MODEL = "google-antigravity/gemini-3.8-flash"
J2_MODEL = "openrouter/~anthropic/claude-sonnet-latest"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def _log_spend(cost_usd: float, job: str, route: str, note: str = "") -> None:
    rec = {
        "cost_usd": round(cost_usd, 6),
        "job": job,
        "route": route,
        "note": note,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    SPEND_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEND_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def build_control_rewrites(task_sources: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Build exactly 3 AST-preserving rewrites per task for the 8 certified tasks (24 total)."""
    controls: list[dict[str, Any]] = []

    # 1. format-code-task-000081 (xdoctest)
    tid = "format-code-task-000081"
    src_81 = task_sources[tid]["xdoctest/runner.py"]
    # CTRL-41A: rename_var (hard)
    s_41a = (
        src_81.replace("n_total = len(enabled_examples)", "_total_examples = len(enabled_examples)")
        .replace("print('running %d test(s)' % n_total)", "print('running %d test(s)' % _total_examples)")
        .replace("print('%d / %d passed'  % (n_passed, n_total))", "print('%d / %d passed'  % (n_passed, _total_examples))")
        .replace("'n_total': n_total", "'n_total': _total_examples")
    )
    controls.append({
        "control_id": "CTRL-81A", "task_id": tid, "path": "xdoctest/runner.py",
        "operator": "rename_var", "category": "hard", "description": "Local variable rename n_total -> _total_examples",
        "new_source": s_41a, "verified_original": True,
    })
    # CTRL-41B: symmetric_comparison (easy)
    s_41b = src_81.replace("if len(enabled_examples) == 1:", "if 1 == len(enabled_examples):")
    controls.append({
        "control_id": "CTRL-81B", "task_id": tid, "path": "xdoctest/runner.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric equality 1 == len(enabled_examples)",
        "new_source": s_41b, "verified_original": True,
    })
    # CTRL-81C: dead_branch (easy)
    s_81c = src_81.replace("def _run_examples(enabled_examples, verbose):", "def _run_examples(enabled_examples, verbose):\n    if False:\n        pass")
    controls.append({
        "control_id": "CTRL-81C", "task_id": tid, "path": "xdoctest/runner.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch if False: pass in _run_examples",
        "new_source": s_81c, "verified_original": False,
    })

    # 2. format-code-task-000085 (gymnasium taxi)
    tid = "format-code-task-000085"
    src_85 = task_sources[tid]["gymnasium/envs/toy_text/taxi.py"]
    # CTRL-85A: symmetric_comparison
    s_85a = src_85.replace("taxi_loc == self.locs[pass_idx]", "self.locs[pass_idx] == taxi_loc")
    controls.append({
        "control_id": "CTRL-85A", "task_id": tid, "path": "gymnasium/envs/toy_text/taxi.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric comparison self.locs[pass_idx] == taxi_loc",
        "new_source": s_85a, "verified_original": False,
    })
    # CTRL-85B: dead_branch
    s_85b = src_85.replace("def _pickup(self, taxi_loc, pass_idx, reward):", "def _pickup(self, taxi_loc, pass_idx, reward):\n        if False:\n            pass")
    controls.append({
        "control_id": "CTRL-85B", "task_id": tid, "path": "gymnasium/envs/toy_text/taxi.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch in _pickup",
        "new_source": s_85b, "verified_original": False,
    })
    # CTRL-85C: redundant_assert (easy)
    s_85c = src_85.replace("new_reward = -10", "new_reward = -10\n            assert True")
    controls.append({
        "control_id": "CTRL-85C", "task_id": tid, "path": "gymnasium/envs/toy_text/taxi.py",
        "operator": "redundant_assert", "category": "easy", "description": "Insert redundant assert True in _pickup",
        "new_source": s_85c, "verified_original": False,
    })

    # 3. format-code-task-000232 (quantecon)
    tid = "format-code-task-000232"
    src_232 = task_sources[tid]["quantecon/gridtools.py"]
    # CTRL-50A: symmetric_comparison (easy)
    s_232a = src_232.replace("if X[t, i] <= nodes[i][0]:", "if nodes[i][0] >= X[t, i]:")
    controls.append({
        "control_id": "CTRL-232A", "task_id": tid, "path": "quantecon/gridtools.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric boundary comparison nodes[i][0] >= X[t, i]",
        "new_source": s_232a, "verified_original": True,
    })
    # CTRL-50B: symmetric_comparison (easy)
    s_232b = src_232.replace("elif X[t, i] >= nodes[i][-1]:", "elif nodes[i][-1] <= X[t, i]:")
    controls.append({
        "control_id": "CTRL-232B", "task_id": tid, "path": "quantecon/gridtools.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric boundary comparison nodes[i][-1] <= X[t, i]",
        "new_source": s_232b, "verified_original": True,
    })
    # CTRL-232C: dead_branch (easy)
    s_232c = src_232.replace("for t in range(m):", "if False:\n        pass\n    for t in range(m):")
    controls.append({
        "control_id": "CTRL-232C", "task_id": tid, "path": "quantecon/gridtools.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch before loop in gridtools",
        "new_source": s_232c, "verified_original": False,
    })

    # 4. format-code-task-000812 (django-oauth-toolkit)
    tid = "format-code-task-000812"
    src_812 = task_sources[tid]["oauth2_provider/oauth2_validators.py"]
    # CTRL-812A: dead_branch
    s_812a = src_812.replace("def get_oidc_claims(self, token, token_handler, request):", "def get_oidc_claims(self, token, token_handler, request):\n        if False:\n            pass")
    controls.append({
        "control_id": "CTRL-812A", "task_id": tid, "path": "oauth2_provider/oauth2_validators.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch in get_oidc_claims",
        "new_source": s_812a, "verified_original": False,
    })
    # CTRL-812B: redundant_assert
    s_812b = src_812.replace("claims = {", "assert True\n        claims = {")
    controls.append({
        "control_id": "CTRL-812B", "task_id": tid, "path": "oauth2_provider/oauth2_validators.py",
        "operator": "redundant_assert", "category": "easy", "description": "Add redundant assert True in get_oidc_claims",
        "new_source": s_812b, "verified_original": False,
    })
    # CTRL-812C: rename_var (hard)
    s_812c = src_812.replace(
        'claims = {\n            "sub": str(request.user.id),\n        }\n\n        # https://openid.net/specs/openid-connect-core-1_0.html#StandardClaims\n        claims.update(**self.get_additional_claims(request))\n\n        return claims',
        '_oidc_claims = {\n            "sub": str(request.user.id),\n        }\n\n        # https://openid.net/specs/openid-connect-core-1_0.html#StandardClaims\n        _oidc_claims.update(**self.get_additional_claims(request))\n\n        return _oidc_claims'
    )
    controls.append({
        "control_id": "CTRL-812C", "task_id": tid, "path": "oauth2_provider/oauth2_validators.py",
        "operator": "rename_var", "category": "hard", "description": "Local variable rename claims -> _oidc_claims in get_oidc_claims",
        "new_source": s_812c, "verified_original": False,
    })

    # 5. format-code-task-000838 (ntfy)
    tid = "format-code-task-000838"
    src_838 = task_sources[tid]["ntfy/cli.py"]
    # CTRL-64A: commute_and (easy)
    s_838a = src_838.replace("if getattr(args, 'func', None) == run_cmd and 'hide_command' in config:",
                             "if 'hide_command' in config and getattr(args, 'func', None) == run_cmd:")
    controls.append({
        "control_id": "CTRL-838A", "task_id": tid, "path": "ntfy/cli.py",
        "operator": "commute_and", "category": "easy", "description": "Commute and operands on config check",
        "new_source": s_838a, "verified_original": True,
    })
    # CTRL-64B: symmetric_comparison (easy)
    s_838b = src_838.replace("if getattr(args, 'func', None) == run_cmd and 'hide_command' in config:",
                             "if run_cmd == getattr(args, 'func', None) and 'hide_command' in config:")
    controls.append({
        "control_id": "CTRL-838B", "task_id": tid, "path": "ntfy/cli.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric comparison run_cmd == getattr(...)",
        "new_source": s_838b, "verified_original": True,
    })
    # CTRL-838C: dead_branch (easy)
    s_838c = src_838.replace("def run_cmd(args):", "def run_cmd(args):\n    if False:\n        pass")
    controls.append({
        "control_id": "CTRL-838C", "task_id": tid, "path": "ntfy/cli.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch in run_cmd",
        "new_source": s_838c, "verified_original": False,
    })

    # 6. format-code-task-002391 (pip-audit)
    tid = "format-code-task-002391"
    src_2391 = task_sources[tid]["pip_audit/_audit.py"]
    # CTRL-2391A: dead_branch
    s_2391a = src_2391.replace("specs = source.collect()", "if False:\n            pass\n        specs = source.collect()")
    controls.append({
        "control_id": "CTRL-2391A", "task_id": tid, "path": "pip_audit/_audit.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch before specs collection in audit()",
        "new_source": s_2391a, "verified_original": False,
    })
    # CTRL-2391B: redundant_assert
    s_2391b = src_2391.replace('if not v.id.startswith("PYSEC"):', 'assert True\n                    if not v.id.startswith("PYSEC"):')
    controls.append({
        "control_id": "CTRL-2391B", "task_id": tid, "path": "pip_audit/_audit.py",
        "operator": "redundant_assert", "category": "easy", "description": "Add redundant assert True in audit()",
        "new_source": s_2391b, "verified_original": False,
    })
    # CTRL-2391C: rename_var (hard)
    s_2391c = src_2391.replace("seen_aliases: set[str] = set()", "_seen_aliases: set[str] = set()").replace("seen_aliases.", "_seen_aliases.")
    controls.append({
        "control_id": "CTRL-2391C", "task_id": tid, "path": "pip_audit/_audit.py",
        "operator": "rename_var", "category": "hard", "description": "Local variable rename seen_aliases -> _seen_aliases",
        "new_source": s_2391c, "verified_original": False,
    })

    # 7. format-code-task-002552 (python-miio)
    tid = "format-code-task-002552"
    src_2552 = task_sources[tid]["miio/miot_models.py"]
    # CTRL-2552A: symmetric_comparison
    s_2552a = src_2552.replace('if values["in"] is None:', 'if None is values["in"]:')
    controls.append({
        "control_id": "CTRL-2552A", "task_id": tid, "path": "miio/miot_models.py",
        "operator": "symmetric_comparison", "category": "easy", "description": "Symmetric identity comparison None is values['in']",
        "new_source": s_2552a, "verified_original": False,
    })
    # CTRL-2552B: dead_branch
    s_2552b = src_2552.replace('if values["out"] is None:', 'if False:\n            pass\n        if values["out"] is None:')
    controls.append({
        "control_id": "CTRL-2552B", "task_id": tid, "path": "miio/miot_models.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch in default_null_to_empty",
        "new_source": s_2552b, "verified_original": False,
    })
    # CTRL-2552C: redundant_assert
    s_2552c = src_2552.replace('values["in"] = []', 'assert True\n            values["in"] = []')
    controls.append({
        "control_id": "CTRL-2552C", "task_id": tid, "path": "miio/miot_models.py",
        "operator": "redundant_assert", "category": "easy", "description": "Add redundant assert True in default_null_to_empty",
        "new_source": s_2552c, "verified_original": False,
    })

    # 8. format-code-task-002609 (cartopy)
    tid = "format-code-task-002609"
    src_2609 = task_sources[tid]["lib/cartopy/io/shapereader.py"]
    # CTRL-2609A: dead_branch
    s_2609a = src_2609.replace("def natural_earth(resolution='110m', category='physical', name='coastline'):",
                               "def natural_earth(resolution='110m', category='physical', name='coastline'):\n    if False:\n        pass")
    controls.append({
        "control_id": "CTRL-2609A", "task_id": tid, "path": "lib/cartopy/io/shapereader.py",
        "operator": "dead_branch", "category": "easy", "description": "Add dead branch in natural_earth",
        "new_source": s_2609a, "verified_original": False,
    })
    # CTRL-2609B: redundant_assert
    s_2609b = src_2609.replace("ne_downloader = Downloader.from_config", "assert True\n    ne_downloader = Downloader.from_config")
    controls.append({
        "control_id": "CTRL-2609B", "task_id": tid, "path": "lib/cartopy/io/shapereader.py",
        "operator": "redundant_assert", "category": "easy", "description": "Add redundant assert True in natural_earth",
        "new_source": s_2609b, "verified_original": False,
    })
    # CTRL-2609C: try_finally
    s_2609c = src_2609.replace("return ne_downloader.path(format_dict)", "try:\n        return ne_downloader.path(format_dict)\n    finally:\n        pass")
    controls.append({
        "control_id": "CTRL-2609C", "task_id": tid, "path": "lib/cartopy/io/shapereader.py",
        "operator": "try_finally", "category": "easy", "description": "Wrap return in try/finally: pass in natural_earth",
        "new_source": s_2609c, "verified_original": False,
    })

    # Compute diff_text for each control
    for c in controls:
        orig = task_sources[c["task_id"]][c["path"]]
        diff = difflib.unified_diff(
            orig.splitlines(keepends=True),
            c["new_source"].splitlines(keepends=True),
            fromfile=f"a/{c['path']}",
            tofile=f"b/{c['path']}",
        )
        c["diff_text"] = "".join(diff)

    return controls


def run_adjudication_model(
    prompt: str,
    route: str,
    session_dir: Path,
    spend_tag: str = "adjudication",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    argv = [
        "omp", "-p", "--mode", "json",
        "--config", str(WORKER_YML),
        "--no-skills", "--no-tools", "--no-rules", "--no-extensions",
        "--no-session", "--no-title", "--no-lsp",
        "--thinking", "low",
        "--model", route,
        prompt,
    ]
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    wall_s = round(time.monotonic() - started, 2)
    stdout = proc.stdout or ""
    (session_dir / "response.jsonl").write_text(stdout, encoding="utf-8")

    text, _, _ = _parse_omp_json_stdout(stdout)
    provider, model = _stream_served_route(_omp_stream_messages(stdout), route)
    attested = bool(provider and model and (f"{provider}/{model}" == route or model in route))

    record = {
        "requested_route": route,
        "served_provider": provider,
        "served_model": model,
        "attested": attested,
        "wall_s": wall_s,
        "raw_text": text,
    }

    # Estimate cost
    # Gemini subscription: $0.00
    # OpenRouter Claude Sonnet: ~$0.003 - $0.008 per call
    est_cost = 0.0
    if "openrouter" in route:
        est_cost = 0.005
        _log_spend(est_cost, f"a8b-judge:{session_dir.name}", route, f"Claude adjudication wall_s={wall_s}")

    record["cost_usd"] = est_cost

    if not text:
        return None, record

    # Parse JSON verdict
    try:
        # Look for code block or first json
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group(1))
        else:
            match = re.search(r"(\{.*\})", text, re.DOTALL)
            parsed = json.loads(match.group(1)) if match else None

        if parsed and "verdict" in parsed:
            verdict = parsed["verdict"].strip().lower()
            if verdict not in ("wrong", "correct-but-test-too-strict", "unclear"):
                if "wrong" in verdict:
                    verdict = "wrong"
                elif "too-strict" in verdict or "strict" in verdict:
                    verdict = "correct-but-test-too-strict"
                else:
                    verdict = "unclear"
            parsed["verdict"] = verdict
            return parsed, record
    except Exception as exc:
        record["json_parse_error"] = str(exc)

    return None, record


def main() -> None:
    started_at = datetime.now(UTC).isoformat()
    _ = started_at
    print("=== A8B: REAL SUBMISSION REPLAY AGAINST GOLD GRADERS ===")

    # 1. Verify Prerequisites & Pre-Registration
    assert PREREG_PATH.exists(), f"Missing prereg at {PREREG_PATH}"
    prereg_bytes = PREREG_PATH.read_bytes()
    prereg_sha256 = hashlib.sha256(prereg_bytes).hexdigest()
    print(f"Pre-registration SHA256: {prereg_sha256} (sha256[:12]: {prereg_sha256[:12]})")

    with open(CERT_JSON, encoding="utf-8") as f:
        cert_data = json.load(f)
    cert_tasks = {t["task_id"]: t for t in cert_data["tasks"] if t.get("gold_structural") or t.get("gold_heldout")}
    print(f"Certified Gold tasks: {len(cert_tasks)} tasks")
    if len(cert_tasks) < 4:
        print(f"STOPPING: Insufficient Gold tasks ({len(cert_tasks)} < 4)")
        sys.exit(0)

    with open(UNIV_JSON, encoding="utf-8") as f:
        univ_data = json.load(f)
    univ_tasks = {t["task_id"]: t for t in univ_data["tasks"]}

    with open(INV_JSON, encoding="utf-8") as f:
        inv_data = json.load(f)
    real_accepted = [
        s for s in inv_data["submissions"]
        if s.get("task_id") in cert_tasks and s.get("original_reward") == 1.0 and s.get("patch_path")
    ]
    real_accepted.sort(key=lambda s: (s["task_id"], s["run_id"]))
    print(f"Accepted real agent submissions on certified tasks: {len(real_accepted)}")

    # Load sources for the 8 tasks
    task_sources: dict[str, dict[str, str]] = {}
    env_specs: dict[str, Any] = {}
    ref_patches: dict[str, str] = {}
    for tid in cert_tasks:
        probe_p = A2_TASKS_DIR / tid / "probe/result.json"
        res = json.loads(probe_p.read_text())
        task_sources[tid] = parse_probe(res.get("agent_log", ""))

        pkg = Path(cert_tasks[tid]["package_dir"])
        patch_file = pkg / "tests/test.patch"
        env_specs[tid] = derive_env_spec(patch_file.read_text(errors="replace"))

        ref_path = REPO_ROOT / univ_tasks[tid]["reference_patch_path"]
        ref_patches[tid] = ref_path.read_text(encoding="utf-8")

    # Build 24 controls
    controls = build_control_rewrites(task_sources)
    print(f"Constructed {len(controls)} known-equivalent negative controls (3 per certified task).")

    # Setup Modal runner
    DURABLE_ROOT.mkdir(parents=True, exist_ok=True)
    BRIDGE_RECEIPTS.mkdir(parents=True, exist_ok=True)
    CELLS_DIR.mkdir(parents=True, exist_ok=True)

    gate = ReserveSettleSpendGate(
        approval=MODAL_APPROVAL,
        cap_usd=MODAL_CAP_USD,
        usd_per_execution=0.008,
        spend_ledger=LEDGER_PATH,
        modal_app_name="a8b-real-replay",
        billing_tag="a8b-real-replay",
    )
    meter = SandboxMeter(MODAL_CAP_USD)
    runner = modal_cell_runner(
        repo_root=REPO_ROOT,
        spend_gate=gate,
        meter=meter,
        timeout_seconds=300,
        results_dir=CELLS_DIR,
    )

    # -------------------------------------------------------------------------
    # Step A: Verify new controls on Original Grader (must score reward 1.0)
    # -------------------------------------------------------------------------
    print("\n--- Step A: Verifying Controls on Original Graders ---")
    uncached_controls = [c for c in controls if not c.get("verified_original")]
    print(f"Controls needing Modal verification on original grader: {len(uncached_controls)} (6 already verified in A2B)")

    def verify_control_orig(c: dict[str, Any]) -> dict[str, Any]:
        tid = c["task_id"]
        orig_pkg = Path(univ_tasks[tid]["package_path"])
        ref_patch = ref_patches[tid]
        workdir = env_specs[tid].workdir
        script = (
            f"set -u\ncd {workdir} || exit 1\n"
            + _apply_reference(ref_patch)
            + _write(c["path"], c["new_source"])
            + f"echo EQUIV_CONTROL_APPLIED {c['control_id']}\n"
        )
        cell_dir = CELLS_DIR / f"orig_{c['control_id']}"
        res = runner(cell_dir, orig_pkg, script)
        c["orig_reward"] = res.get("reward")
        c["orig_status"] = res.get("status")
        c["orig_wall_s"] = res.get("wall_s")
        return res

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(verify_control_orig, c): c for c in uncached_controls}
        for fut in as_completed(futures):
            c = futures[fut]
            res = fut.result()
            print(f"  [ORIG] {c['control_id']} ({c['task_id']}): reward={res.get('reward')} ({res.get('status')})")

    failed_orig = [c for c in uncached_controls if c.get("orig_reward") != 1.0]
    if failed_orig:
        print(f"WARNING: {len(failed_orig)} controls failed original grader: {[c['control_id'] for c in failed_orig]}")
    else:
        print(f"ALL {len(uncached_controls)} new controls VERIFIED (reward == 1.0) on original graders!")

    # -------------------------------------------------------------------------
    # Step B: Mandatory Control Invariant: Reference Patch on Gold Grader
    # -------------------------------------------------------------------------
    print("\n--- Step B: Evaluating Reference Patches on Gold Graders ---")
    ref_results: dict[str, dict[str, Any]] = {}

    def run_ref_control(tid: str) -> dict[str, Any]:
        gold_pkg = Path(cert_tasks[tid]["package_dir"])
        ref_patch = ref_patches[tid]
        workdir = env_specs[tid].workdir

        script = (
            f"set -u\ncd {workdir} || exit 1\n"
            + f"echo {_b64(ref_patch)} | base64 -d > /tmp/reference.patch\n"
            + "git apply --whitespace=nowarn /tmp/reference.patch || patch -p1 < /tmp/reference.patch || exit 1\n"
            + "echo REFERENCE_APPLIED\n"
        )
        cell_dir = CELLS_DIR / f"gold_ref_{tid}"
        res = runner(cell_dir, gold_pkg, script)
        return res

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_ref_control, tid): tid for tid in sorted(cert_tasks)}
        for fut in as_completed(futures):
            tid = futures[fut]
            res = fut.result()
            ref_results[tid] = res
            print(f"  [GOLD REF] {tid}: reward={res.get('reward')} ({res.get('status')})")

    broken_gold = [tid for tid, r in ref_results.items() if r.get("reward") != 1.0]
    if broken_gold:
        print(f"CRITICAL: Gold grader broken on reference for: {broken_gold}")
        assert not broken_gold, f"Reference failed on Gold packages: {broken_gold}"
    print(f"ALL {len(cert_tasks)} Reference solutions passed Gold graders (Mandatory Control Invariant MET)!")

    # -------------------------------------------------------------------------
    # Step C: Replay Real Accepted Submissions on Gold Grader
    # -------------------------------------------------------------------------
    print("\n--- Step C: Replaying Real Accepted Submissions on Gold Grader ---")
    real_results: list[dict[str, Any]] = []

    def run_real_submission(sub: dict[str, Any]) -> dict[str, Any]:
        tid = sub["task_id"]
        gold_pkg = Path(cert_tasks[tid]["package_dir"])
        workdir = env_specs[tid].workdir
        patch_path = Path(sub["patch_path"])
        patch_text = patch_path.read_text(errors="replace")

        # Exclude pycache diff lines that break git apply
        script = (
            f"set -u\ncd {workdir} || exit 1\n"
            + f"echo {_b64(patch_text)} | base64 -d > /tmp/agent.patch\n"
            + "git apply --whitespace=nowarn --exclude='*.pyc' /tmp/agent.patch || "
            + "git apply --whitespace=nowarn /tmp/agent.patch || "
            + "patch -p1 < /tmp/agent.patch || exit 1\n"
            + "echo AGENT_PATCH_APPLIED\n"
        )
        cell_dir = CELLS_DIR / f"gold_real_{tid}_{sub['run_id'][:8]}"
        res = runner(cell_dir, gold_pkg, script)
        assert res.get("reward") in (0.0, 1.0), f"Real submission cell failed: {cell_dir.name} -> {res}"
        # Read failing assertion if reward 0
        test_out = ""
        trial_log = cell_dir / "trial/verifier/test_output.log"
        if trial_log.is_file():
            test_out = trial_log.read_text(errors="replace")

        row = {
            "item_id": f"REAL-{tid}-{sub['run_id'][:8]}",
            "cohort_type": "real_submission",
            "task_id": tid,
            "run_id": sub["run_id"],
            "agent_model": sub.get("agent_model"),
            "patch_path": str(patch_path),
            "patch_text": patch_text,
            "original_reward": 1.0,
            "gold_reward": res.get("reward"),
            "status": res.get("status"),
            "flipped": (res.get("reward") == 0.0),
            "wall_s": res.get("wall_s"),
            "est_usd": res.get("est_usd"),
            "test_output": test_out,
            "cell_dir": str(cell_dir),
        }
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_real_submission, s) for s in real_accepted]
        for fut in as_completed(futures):
            row = fut.result()
            real_results.append(row)
            status_str = "FLIPPED (0.0)" if row["flipped"] else "PASSED (1.0)"
            print(f"  [REAL SUBMISSION] {row['item_id']}: {status_str}")

    real_results.sort(key=lambda r: (r["task_id"], r["run_id"]))

    # -------------------------------------------------------------------------
    # Step D: Replay Known-Equivalent Controls on Gold Grader
    # -------------------------------------------------------------------------
    print("\n--- Step D: Replaying Known-Equivalent Controls on Gold Grader ---")
    ctrl_results: list[dict[str, Any]] = []

    def run_control_gold(c: dict[str, Any]) -> dict[str, Any]:
        tid = c["task_id"]
        gold_pkg = Path(cert_tasks[tid]["package_dir"])
        ref_patch = ref_patches[tid]
        workdir = env_specs[tid].workdir

        script = (
            f"set -u\ncd {workdir} || exit 1\n"
            + _apply_reference(ref_patch)
            + _write(c["path"], c["new_source"])
            + f"echo EQUIV_CONTROL_APPLIED {c['control_id']}\n"
        )
        cell_dir = CELLS_DIR / f"gold_ctrl_{c['control_id']}"
        res = runner(cell_dir, gold_pkg, script)
        assert res.get("reward") in (0.0, 1.0), f"Control cell failed: {cell_dir.name} -> {res}"
        test_out = ""
        trial_log = cell_dir / "trial/verifier/test_output.log"
        if trial_log.is_file():
            test_out = trial_log.read_text(errors="replace")

        row = {
            "item_id": c["control_id"],
            "cohort_type": "equivalent_control",
            "task_id": tid,
            "path": c["path"],
            "operator": c["operator"],
            "category": c["category"],
            "description": c["description"],
            "diff_text": c["diff_text"],
            "original_reward": 1.0,
            "gold_reward": res.get("reward"),
            "status": res.get("status"),
            "flipped": (res.get("reward") == 0.0),
            "wall_s": res.get("wall_s"),
            "est_usd": res.get("est_usd"),
            "test_output": test_out,
            "cell_dir": str(cell_dir),
        }
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_control_gold, c) for c in controls]
        for fut in as_completed(futures):
            row = fut.result()
            ctrl_results.append(row)
            status_str = "REJECTED (0.0)" if row["flipped"] else "PASSED (1.0)"
            print(f"  [CONTROL] {row['item_id']} ({row['task_id']}): {status_str}")

    ctrl_results.sort(key=lambda r: r["item_id"])

    # -------------------------------------------------------------------------
    # Step E: Adjudication of Flipped Submissions & Controls
    # -------------------------------------------------------------------------
    print("\n--- Step E: Model Adjudication of Flipped/Rejected Items ---")
    flipped_items = [r for r in real_results if r["flipped"]] + [r for r in ctrl_results if r["flipped"]]
    print(f"Total flipped items needing adjudication: {len(flipped_items)}")
    print(f"  - Real submissions flipped: {sum(1 for r in real_results if r['flipped'])} / {len(real_results)}")
    print(f"  - Controls rejected: {sum(1 for r in ctrl_results if r['flipped'])} / {len(ctrl_results)}")

    adjudications: list[dict[str, Any]] = []

    # Deterministic sample of at most 12 for Reader 2 (Claude)
    random.seed(20261102)
    j2_sample_indices = set(random.sample(range(len(flipped_items)), min(12, len(flipped_items)))) if flipped_items else set()

    for idx, item in enumerate(flipped_items):
        tid = item["task_id"]
        pkg = Path(cert_tasks[tid]["package_dir"])
        instruction = (pkg / "instruction.md").read_text(errors="replace")
        ref_patch = ref_patches[tid]
        diff_text = item.get("diff_text") or item.get("patch_text") or ""
        test_out = item.get("test_output", "")

        # Extract failing assertion line
        failing_lines = [line for line in test_out.splitlines() if "FAILED" in line or "AssertionError" in line or "assert" in line or "Error:" in line]
        failing_summary = "\n".join(failing_lines[-10:]) if failing_lines else test_out[-500:]

        prompt = f"""You are an expert software verification auditor adjudicating a test rejection.

A test grader ("Gold Grader") evaluated a proposed bug fix and REJECTED it (reward 0.0).
We need to know whether the submission was TRULY WRONG / DEFECTIVE, or whether the submission was CORRECT but rejected because the Gold test suite made overly strict or reference-specific assumptions.

### Task Instruction:
{instruction[:4000]}

### Human Ground-Truth Reference Solution:
```diff
{ref_patch[:3000]}
```

### Evaluated Submission Diff:
```diff
{diff_text[:3000]}
```

### Grader Failure Output:
```
{failing_summary[:2000]}
```

Evaluate the code and failure objectively.
Did the submission violate the task requirements, introduce a defect, or fail to fix the bug?
Or is the submission a completely valid alternative solution that fails only because the test asserts private/incidental behavior of the reference solution?

Output exactly ONE JSON object:
```json
{{
  "verdict": "wrong" | "correct-but-test-too-strict" | "unclear",
  "reason": "<quoted explanation referencing specific code and requirements>",
  "confidence": 1 to 5
}}
```
"""
        adj_record = {
            "item_id": item["item_id"],
            "task_id": tid,
            "cohort_type": item["cohort_type"],
            "j1_gemini": None,
            "j2_claude": None,
        }

        # Reader 1: Gemini 3.8 Flash (all flipped items)
        sess_j1 = DURABLE_ROOT / "adjudication" / f"j1_{item['item_id']}"
        j1_verdict, j1_rec = run_adjudication_model(prompt, J1_MODEL, sess_j1, "j1_gemini")
        adj_record["j1_gemini"] = {
            "verdict": j1_verdict.get("verdict") if j1_verdict else "unclear",
            "reason": j1_verdict.get("reason") if j1_verdict else "",
            "confidence": j1_verdict.get("confidence") if j1_verdict else 1,
            "record": j1_rec,
        }
        print(f"  [JUDGE 1 - Gemini] {item['item_id']}: {adj_record['j1_gemini']['verdict']}")

        # Reader 2: Claude Sonnet (sample of up to 12)
        if idx in j2_sample_indices:
            sess_j2 = DURABLE_ROOT / "adjudication" / f"j2_{item['item_id']}"
            j2_verdict, j2_rec = run_adjudication_model(prompt, J2_MODEL, sess_j2, "j2_claude")
            adj_record["j2_claude"] = {
                "verdict": j2_verdict.get("verdict") if j2_verdict else "unclear",
                "reason": j2_verdict.get("reason") if j2_verdict else "",
                "confidence": j2_verdict.get("confidence") if j2_verdict else 1,
                "record": j2_rec,
            }
            print(f"  [JUDGE 2 - Claude] {item['item_id']}: {adj_record['j2_claude']['verdict']}")

        adjudications.append(adj_record)

    # -------------------------------------------------------------------------
    # Step F: Statistical Aggregation & Receipts
    # -------------------------------------------------------------------------
    print("\n--- Step F: Computing Metrics & Generating Receipts ---")

    n_real = len(real_results)
    k_real_flipped = sum(1 for r in real_results if r["flipped"])

    n_ctrl = len(ctrl_results)
    k_ctrl_rejected = sum(1 for r in ctrl_results if r["flipped"])

    n_total = n_real + n_ctrl
    k_total_rejected = k_real_flipped + k_ctrl_rejected

    # Per task breakdown
    per_task_stats: dict[str, dict[str, Any]] = {}
    for tid in sorted(cert_tasks):
        t_reals = [r for r in real_results if r["task_id"] == tid]
        t_ctrls = [r for r in ctrl_results if r["task_id"] == tid]
        per_task_stats[tid] = {
            "real_n": len(t_reals),
            "real_flipped": sum(1 for r in t_reals if r["flipped"]),
            "real_flip_rate": (sum(1 for r in t_reals if r["flipped"]) / len(t_reals)) if t_reals else None,
            "ctrl_n": len(t_ctrls),
            "ctrl_rejected": sum(1 for r in t_ctrls if r["flipped"]),
            "ctrl_reject_rate": (sum(1 for r in t_ctrls if r["flipped"]) / len(t_ctrls)) if t_ctrls else None,
            "reference_passed": ref_results.get(tid, {}).get("reward") == 1.0,
        }

    # Adjudication agreement
    paired_adjudications = [a for a in adjudications if a.get("j2_claude") is not None]
    n_paired = len(paired_adjudications)
    k_agree = sum(
        1 for a in paired_adjudications
        if a["j1_gemini"]["verdict"] == a["j2_claude"]["verdict"]
    )
    raw_agreement = (k_agree / n_paired) if n_paired > 0 else 1.0

    # Adjudicated precision (fraction judged truly wrong by Reader 1)
    k_wrong = sum(1 for a in adjudications if a["j1_gemini"]["verdict"] == "wrong")
    k_strict = sum(1 for a in adjudications if a["j1_gemini"]["verdict"] == "correct-but-test-too-strict")
    k_unclear = sum(1 for a in adjudications if a["j1_gemini"]["verdict"] == "unclear")
    precision = (k_wrong / len(adjudications)) if adjudications else 0.0

    summary = {
        "schema_version": "a8b.real_submission_replay/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "prereg_sha256": prereg_sha256,
        "prereg_sha256_12": prereg_sha256[:12],
        "spend": {
            "modal_settled_usd": round(meter.spent, 4),
            "modal_cap_usd": MODAL_CAP_USD,
            "modal_cells": meter.cells,
            "modal_seconds": round(meter.seconds, 1),
            "openrouter_usd": round(sum(a.get("j2_claude", {}).get("record", {}).get("cost_usd", 0.0) for a in adjudications), 4),
        },
        "controls": {
            "reference_control_pass_rate": f"{sum(1 for r in ref_results.values() if r.get('reward') == 1.0)}/{len(ref_results)} = 100.0%",
            "all_references_passed": all(r.get("reward") == 1.0 for r in ref_results.values()),
        },
        "cohort": {
            "certified_tasks_count": len(cert_tasks),
            "real_submissions_count": n_real,
            "known_equivalent_controls_count": n_ctrl,
            "total_cohort_count": n_total,
        },
        "metrics": {
            "real_flip_rate": {
                "k": k_real_flipped,
                "n": n_real,
                "fmt": stats.fmt(k_real_flipped, n_real),
                "rate": round(k_real_flipped / n_real, 4) if n_real else 0.0,
                "ci": list(stats.wilson(k_real_flipped, n_real)) if n_real else [0.0, 0.0],
            },
            "control_false_reject_rate": {
                "k": k_ctrl_rejected,
                "n": n_ctrl,
                "fmt": stats.fmt(k_ctrl_rejected, n_ctrl),
                "rate": round(k_ctrl_rejected / n_ctrl, 4) if n_ctrl else 0.0,
                "ci": list(stats.wilson(k_ctrl_rejected, n_ctrl)) if n_ctrl else [0.0, 0.0],
            },
            "combined_reject_rate": {
                "k": k_total_rejected,
                "n": n_total,
                "fmt": stats.fmt(k_total_rejected, n_total),
                "rate": round(k_total_rejected / n_total, 4) if n_total else 0.0,
                "ci": list(stats.wilson(k_total_rejected, n_total)) if n_total else [0.0, 0.0],
            },
            "adjudication": {
                "total_adjudicated": len(adjudications),
                "paired_sample_size": n_paired,
                "raw_agreement_rate": round(raw_agreement, 4),
                "verdicts_gemini": {
                    "wrong": k_wrong,
                    "correct_too_strict": k_strict,
                    "unclear": k_unclear,
                },
                "adjudicated_precision": {
                    "k": k_wrong,
                    "n": len(adjudications),
                    "fmt": stats.fmt(k_wrong, len(adjudications)),
                    "rate": round(precision, 4) if adjudications else 0.0,
                    "ci": list(stats.wilson(k_wrong, len(adjudications))) if adjudications else [0.0, 0.0],
                },
            },
            "per_task": per_task_stats,
        },
    }

    # Save clean receipt files (strip large source code bodies to comply with data policy)
    receipt_data = {
        "summary": summary,
        "real_submissions": [
            {
                "item_id": r["item_id"], "task_id": r["task_id"], "run_id": r["run_id"],
                "agent_model": r["agent_model"], "patch_sha256": hashlib.sha256(r["patch_text"].encode()).hexdigest(),
                "original_reward": r["original_reward"], "gold_reward": r["gold_reward"],
                "flipped": r["flipped"], "wall_s": r["wall_s"],
            }
            for r in real_results
        ],
        "controls": [
            {
                "item_id": c["item_id"], "task_id": c["task_id"], "path": c["path"],
                "operator": c["operator"], "category": c["category"], "description": c["description"],
                "original_reward": c["original_reward"], "gold_reward": c["gold_reward"],
                "flipped": c["flipped"], "wall_s": c["wall_s"],
            }
            for c in ctrl_results
        ],
        "adjudications": adjudications,
    }

    receipt_json = json.dumps(receipt_data, indent=2)
    (BRIDGE_RECEIPTS / "replay_receipt.json").write_text(receipt_json, encoding="utf-8")
    (DURABLE_ROOT / "receipts" / "replay_receipt.json").parent.mkdir(parents=True, exist_ok=True)
    (DURABLE_ROOT / "receipts" / "replay_receipt.json").write_text(receipt_json, encoding="utf-8")
    (REPO_ROOT / "research/experiments/on-real-submission-replay-20261102" / "replay_receipt.json").write_text(receipt_json, encoding="utf-8")

    print("\n=== SUMMARY OF RESULTS ===")
    print(f"Real Submission Flip Rate:        {summary['metrics']['real_flip_rate']['fmt']}")
    print(f"Control False-Reject Rate:        {summary['metrics']['control_false_reject_rate']['fmt']}")
    print(f"Combined Valid Rejection Rate:    {summary['metrics']['combined_reject_rate']['fmt']}")
    print(f"Adjudication Agreement:           {k_agree}/{n_paired} = {100*raw_agreement:.1f}%")
    print(f"Adjudicated Precision (Wrong):    {summary['metrics']['adjudication']['adjudicated_precision']['fmt']}")
    print(f"Modal Spend:                      ${meter.spent:.4f} (Cap: ${MODAL_CAP_USD})")
    print("Receipts written successfully!")


if __name__ == "__main__":
    main()
