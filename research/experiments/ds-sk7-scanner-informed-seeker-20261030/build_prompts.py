#!/usr/bin/env python3
"""SK-7: build R1C (SK-5 parity) and R1CS (R1C + TQ-1 static grader-surface summary) prompts.

R1C text is taken byte-for-byte from SK-5's builder (imported, not retyped) and its sha256 must
match SK-5/receipts/prompts.json. The summary block uses ONLY scan-all.json scalar features and
flagged class names; list features become item counts, and no task/dataset identifiers are used.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

BASE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
SK2 = BASE / "SK-2" / "receipts"
SK5 = BASE / "SK-5" / "receipts"
TQ1 = BASE / "TQ-1" / "receipts"
OUT = BASE / "SK-7" / "receipts"
STAGE = Path("/tmp/sk2-stage")
DEST = Path("/tmp/sk7/prompts")


def load_sk5():
    spec = importlib.util.spec_from_file_location("sk5_build", SK5 / "code" / "build_prompts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def summary_block(scan: dict) -> str:
    flags = sorted(k for k, v in scan["class_flags"].items() if v)
    lines = []
    for name in sorted(scan["features"]):
        value = scan["features"][name]
        if isinstance(value, list):
            lines.append(f"- {name}: {len(value)} items")
        else:
            lines.append(f"- {name}: {json.dumps(value)}")
    return (
        "\n\n## Static grader-surface summary\n\n"
        "Output of a static scanner on this task package. It reports the grader surface the package "
        "exposes and which weakness classes it flags; it does not report whether any weakness was "
        "ever demonstrated. Use or ignore it as background.\n\n"
        f"Flagged classes: {', '.join(flags) if flags else 'none'}\n\n"
        "Grader-surface features:\n" + "\n".join(lines) + "\n"
    )


def main() -> int:
    sk5 = load_sk5()
    prompts = json.loads((SK5 / "prompts.json").read_text())
    tasks = [json.loads(line) for line in (SK2 / "tasks.jsonl").read_text().splitlines() if line.strip()]
    scans = {s["task_id"]: s for s in json.loads((TQ1 / "scan-all.json").read_text())}
    DEST.mkdir(parents=True, exist_ok=True)
    manifest, bad = {}, []
    cards = sk5.cards_block()
    for t in tasks:
        tid = t["task_id"]
        src = STAGE / tid / "r1" / "headless_prompt.md"
        base = src.read_text(encoding="utf-8")
        ref = prompts[f"{tid}|R1C"]
        if sha(base) != ref["base_sha256"]:
            bad.append(f"{tid}: base sha mismatch")
            continue
        r1c = base + cards + sk5.OVERRIDE
        if sha(r1c) != ref["sha256"]:
            bad.append(f"{tid}: R1C sha mismatch")
            continue
        if tid not in scans:
            bad.append(f"{tid}: no scan")
            continue
        r1cs = base + cards + summary_block(scans[tid]) + sk5.OVERRIDE
        for arm, body in (("R1C", r1c), ("R1CS", r1cs)):
            path = DEST / f"{tid}__{arm}.md"
            path.write_text(body, encoding="utf-8")
            manifest[f"{tid}|{arm}"] = {"path": str(path), "chars": len(body), "sha256": sha(body),
                                        "base_sha256": ref["base_sha256"], "half": scans[tid]["half"],
                                        "flags": sorted(k for k, v in scans[tid]["class_flags"].items() if v),
                                        "has_cards": True, "has_summary": arm == "R1CS"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "prompts.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print("prompts", len(manifest), "bad", len(bad))
    for b in bad[:10]:
        print(" ", b)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
