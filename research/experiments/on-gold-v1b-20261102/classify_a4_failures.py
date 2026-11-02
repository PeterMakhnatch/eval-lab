#!/usr/bin/env python3
"""Phase 0 diagnosis: classify A4's 28 reference failures from stored cells, before and after the environment fix.

Reads (local only, third-party repo output): A4 cells ``variant == r1/reference`` and the A4B control cells
``control/a4stored-fixedhook`` (the same stored A4 test, re-run through A4B's staging with the grader's own
interpreter and workdir). Writes a per-task receipt and an aggregate with counts per class.

Classes (pre-stated in the brief): (a) test calls code with inputs outside the contract (the reference raises),
(b) test asserts exact output the reference does not produce, (c) harness error, (d) timeout/other.
Operational rules, applied to the log of the contract test:
  c: still-failing log shows an import/collection/syntax error, or the run is flagged by the fix (the test passes
     once the grader's environment is used);
  d: timeout, or the grader never reached the contract test (the hidden tests failed first);
  a: unittest ERROR entries (the reference raised an exception inside the test);
  b: unittest FAIL entries only (assertion mismatch).
Mixed logs go to a when any ERROR exists, else b.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

RES = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02")
A4 = RES / "gold-v1"
B = RES / "A4B"


def cells(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "cells").glob("*/result.json")):
        r = json.loads(f.read_text())
        r["_dir"] = f.parent
        out.append(r)
    return out


def log_of(r: dict) -> str:
    p = r["_dir"] / "trial/verifier/test_output.log"
    return p.read_text(errors="replace") if p.is_file() else ""


def contract_section(log: str) -> str:
    """Text from the contract test's own output (unittest) onwards, if it ran."""
    idx = [m.start() for m in re.finditer(r"(?m)^(?:Traceback|test\w* \(__main__|Ran \d+ tests?|ERROR: |FAIL: )", log)]
    return log[idx[0] :] if idx else log


def classify(log: str, reward: float | None) -> tuple[str, str]:
    if reward == 1.0:
        return "pass", ""
    if re.search(r"timed out|TimeoutExpired|Timeout", log):
        return "d", "timeout"
    if "/tests/test_gold_contract.py" not in log and "__main__" not in log:
        return "d", "hidden tests failed before the contract test ran"
    section = contract_section(log)
    if re.search(r"(?m)^(ModuleNotFoundError|ImportError|SyntaxError|IndentationError)\b", section) and not re.search(
        r"(?m)^(ERROR|FAIL): ", section
    ):
        line = next(ln for ln in section.splitlines() if re.match(r"(ModuleNotFoundError|ImportError|SyntaxError|IndentationError)", ln))
        return "c", line[:140]
    errs = re.findall(r"(?m)^ERROR: (\w+) ", section)
    fails = re.findall(r"(?m)^FAIL: (\w+) ", section)
    blocks = re.split(r"\n={60,}\n", section)
    evidence = ""
    for blk in blocks:
        if blk.startswith(("ERROR: ", "FAIL: ")):
            lines = [ln for ln in blk.strip().splitlines() if ln.strip()]
            evidence = next((ln for ln in reversed(lines) if re.match(r"\w*(Error|Exception|Exit)\b", ln.strip())), lines[-1])
            break
    if errs:
        return "a", evidence[:140]
    if fails:
        return "b", evidence[:140]
    tb = re.findall(r'(?m)^  File "/tests/test_gold_contract\.py", line \d+, in <module>', section)
    if tb:  # the contract file died while importing, before any test ran
        last = [ln for ln in section.splitlines() if re.match(r"\w*(Error|Exception)\b", ln)]
        return "c", (last[-1] if last else "module-level traceback")[:140]
    return "d", "no verdict lines parsed"


def main() -> None:
    a4 = {c["task"]: c for c in cells(A4) if c["variant"] == "r1/reference"}
    fixed = {c["task"]: c for c in cells(B) if c["variant"] == "control/a4stored-fixedhook"}
    rows = []
    for tid, c in sorted(a4.items()):
        before_cls, before_ev = classify(log_of(c), c.get("reward"))
        f = fixed.get(tid)
        after_cls, after_ev = classify(log_of(f), f.get("reward")) if f else ("NOT_RERUN", "")
        final = "c" if after_cls == "pass" else after_cls
        evidence = "flips to reward 1.0 with the grader's own interpreter/workdir" if after_cls == "pass" else after_ev
        rows.append({"task": tid, "a4_class_by_log": before_cls, "a4_evidence": before_ev, "after_fix": after_cls,
                     "after_fix_evidence": after_ev, "final_class": final, "final_evidence": evidence})
    counts = Counter(r["final_class"] for r in rows)
    flips = sum(1 for r in rows if r["after_fix"] == "pass")
    summary = {
        "n_reference_failures": len(rows),
        "final_class_counts": dict(counts),
        "flipped_to_pass_after_environment_fix": flips,
        "by_log_before_fix": dict(Counter(r["a4_class_by_log"] for r in rows)),
        "still_failing_after_fix": dict(Counter(r["after_fix"] for r in rows if r["after_fix"] != "pass")),
    }
    out = B / "work/classification.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    ex: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        ex.setdefault(r["final_class"], []).append((r["task"][-6:], r["final_evidence"]))
    for k, v in sorted(ex.items()):
        print(k, v[:2])
    sys.exit(0)


if __name__ == "__main__":
    main()
