#!/usr/bin/env python3
"""Alternative-informed contract-test synthesis (PREREG amendment 1, H5).

Takes the synthesis prompt that `evallab certify-gold` wrote for a task (`<run1>/prompt.txt`), appends the
first half (sha256-ascending) of the task's validated alternative solutions as "independent correct solutions
the test must also pass", calls the same OMP subscription route the certifier uses, validates the returned
unittest with the certifier's own validator, and writes it for `evallab certify-gold --contract-test`.
The second half of the alternatives is never shown; the caller passes only those as `--alt-solution`.

Usage:
  uv run --no-sync python research/experiments/ov1104-grader/cert/alt_informed_synth.py \
      --run1 DIR --alts a.patch b.patch c.patch d.patch --out DIR2 --model google-antigravity/gemini-4-argon
Prints JSON: shown/held-out patch sha256s, served model, test path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.gold_certifier import extract_code_block, validate_contract_test  # noqa: E402


def split_alternatives(patches: list[Path]) -> tuple[list[Path], list[Path]]:
    ordered = sorted(patches, key=lambda p: hashlib.sha256(p.read_bytes()).hexdigest())
    half = len(ordered) // 2
    return ordered[:half], ordered[half:]


def build_prompt(base_prompt: str, shown: list[Path]) -> str:
    section = [
        "\n\n## Independent correct solutions (the test MUST also pass on every one of these)\n",
        "The hidden tests accepted each of the following independently written patches. They are correct solutions "
        "that differ from the reference in structure, helper names and internal precedence. Your test must exercise "
        "only the behaviour the task instruction and hidden tests require, never helper names, private attributes or "
        "incidental choices that only the reference makes. A test that fails any of these patches is wrong.\n",
    ]
    for i, p in enumerate(shown, 1):
        section.append(f"\n### Alternative solution {i} (sha256 {hashlib.sha256(p.read_bytes()).hexdigest()[:12]})\n")
        section.append("```diff\n" + p.read_text(errors="replace")[:40_000] + "\n```\n")
    marker = "\n\n## Requirements for your output\n"
    if marker in base_prompt:
        head, tail = base_prompt.split(marker, 1)
        return head + "".join(section) + marker + tail
    return base_prompt + "".join(section)


def served_model(session_dir: Path) -> dict:
    counts: dict[str, int] = {}
    fallback = 0
    for f in session_dir.glob("*.jsonl"):
        for line in f.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
            if msg.get("role") != "assistant":
                continue
            key = f"{msg.get('provider')}/{msg.get('model')}"
            counts[key] = counts.get(key, 0) + 1
            fallback += 1 if msg.get("resolvedModelIsFallback") else 0
    return {"assistant_messages_by_model": counts, "fallback_count": fallback}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run1", type=Path, required=True)
    ap.add_argument("--alts", type=Path, nargs="*", default=[],
                    help="validated alternatives; first half (sha256 order) is shown, second half held out; "
                         "omit for a plain retry of the certifier's own prompt (lock-safe)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="google-antigravity/gemini-4-argon")
    args = ap.parse_args()
    shown, held = split_alternatives(args.alts)
    args.out.mkdir(parents=True, exist_ok=True)
    base_prompt = (args.run1 / "prompt.txt").read_text()
    ref_res_path = args.run1 / "cells" / "reference" / "result.json"
    if ref_res_path.exists():
        ref_res = json.loads(ref_res_path.read_text())
        if ref_res.get("reward") == 0.0 and ref_res.get("test_output_tail"):
            base_prompt += (
                "\n\n## Feedback from previous execution attempt (Self-repair revision)\n"
                "The previous version of your test file failed on the reference implementation with:\n\n"
                + ref_res["test_output_tail"].strip()
                + "\n\nFix the import/test error so it passes on the reference and all correct solutions, and fails on all mutants.\n"
            )
    prompt = build_prompt(base_prompt, shown) if shown else base_prompt
    prompt_path = args.out / "prompt.txt"
    prompt_path.write_text(prompt)
    session_dir = args.out / "sessions"
    for attempt in range(6):
        proc = subprocess.run(
            ["omp", "-p", "--model", args.model, "--thinking", "medium", "--no-tools", "--no-skills",
             "--session-dir", str(session_dir), f"@{prompt_path}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900, check=False,
            env={**os.environ, "OMP_NO_COLOR": "1"},
        )
        # The OMP agent store is a shared SQLite file; under many concurrent headless sessions start-up can hit
        # SQLITE_BUSY. That is not a model result, so retry after a pause.
        if proc.returncode == 0 or "database is locked" not in proc.stderr:
            break
        time.sleep(20 * (attempt + 1))
    (args.out / "synthesis.stdout").write_text(proc.stdout)
    (args.out / "synthesis.stderr").write_text(proc.stderr)
    code = extract_code_block(proc.stdout) if proc.returncode == 0 else None
    valid, reason = validate_contract_test(code or "")
    report = {
        "shown": [str(p) for p in shown], "held_out": [str(p) for p in held],
        "shown_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in shown],
        "held_out_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in held],
        "omp_exit": proc.returncode, "attempts": attempt + 1, "valid": valid, "reason": reason, "model": args.model,
        "served": served_model(session_dir),
    }
    if valid and code:
        test_path = args.out / "test_gold_contract.py"
        test_path.write_text(code)
        report["test_path"] = str(test_path)
        report["test_lines"] = len(code.splitlines())
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
