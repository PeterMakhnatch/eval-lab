#!/usr/bin/env python3
"""Grader-trust scorecard over DEV MiMo tasks, assembled from existing receipts only.

Stdlib only. Re-runnable: ``python3 build_scorecard.py`` from this folder writes
``scorecard.json``, ``scorecard.md`` and ``headline.json`` next to the script.

Status rule and columns follow ``../PREREG.md`` (fixed before assembly). Every
judgement call about receipt fields is recorded in the worker notes
(``/private/tmp/ov1104/grader/workers/scorecard/NOTES.md``) and summarised in
``NOTES`` below. Nothing here executes a grader; a value absent from every receipt
is ``null``, never a pass.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = Path("/private/tmp/eval-lab-study/research/experiments")
RESULTS = Path.home() / "Developer/eval-lab-results"

# ---------------------------------------------------------------- receipts
UNIVERSE_SUMMARY = EXP / "on-gold-universe-20261102/summary.json"
UNIVERSE_ROWS = RESULTS / "2026-11-02/A1-INVENTORY/gold-universe.json"  # strictly_dev per task

STING_SUMMARY = EXP / "ov-sting-mutants-20261031/results/audit-summary.json"
STING_DIFFS = EXP / "ov-sting-mutants-20261031/results/survivor-diffs"
CENSUS_SUMMARY = EXP / "on-weak-grader-census-20261102/summary.json"
CENSUS_ROWS = RESULTS / "2026-11-02/A2/census.json"  # row-level file named in receipts-manifest.json
R3_PAIRED = EXP / "on-repair-vs-gold-20261102/paired_comparison.json"
R3_ROWS = RESULTS / "2026-11-02/R3-REPAIR-VS-GOLD/repair_vs_gold.json"
VFR3_CELLS = RESULTS / "2026-11-02/VF-R3/results.jsonl"  # verifier reruns of R3 cells
KILLER_SUMMARY = EXP / "ov-mutant-killer-loop-20261031/results-r/summary.json"
CERT8_SUMMARY = EXP / "ov-gold-certifier-8-20261101/results/summary.json"
A4B_CELLS = RESULTS / "2026-11-02/A4B/cells"  # per-cell result.json; 'base/eval-*' variants ran on the original package
CERT8_CELLS = EXP / "ov-gold-certifier-8-20261101/results/cells.csv"
A4B_MANIFEST = EXP / "on-gold-v1b-20261102/a4b-manifest.json"
A4B_CERTIFIED = RESULTS / "2026-11-02/A4B/receipts/gold-v1.1-certified.json"
A4B_TASKS = RESULTS / "2026-11-02/A4B/tasks"
GOLD_V1_MANIFEST = EXP / "on-gold-v1-20261102/gold-v1-manifest.json"  # harness artefact; not used for gates
MIMO_CENSUS = EXP / "mimo-clean-census/results.csv"
HAR169 = EXP / "har169-separate-verifier/results.md"
H2_CELLS = EXP / "h2-separate-verifier-pth-20261030/results-cells.json"
SEPVER_V2 = EXP / "separate-verifier-v2/README.md"
H1_CELLS = EXP / "h1-grader-hardening-20261030/results/cells.csv"
AHA_ALL = EXP / "a-harden-all-20261030/results/all/cells.csv"
AHA_L1 = EXP / "a-harden-all-20261030/results/l1/cells.csv"
T2_SUMMARY = EXP / "t2-unified-harden-20261031/results/summary.json"
K4_ATTACKER = EXP / "ov-k4-redteam-harden-20261031/attacker.json"
K4_SLICE1 = EXP / "ov-k4-redteam-harden-20261031/k4-slice1.json"
A5B_ROWS = RESULTS / "2026-11-02/A5B/results.jsonl"  # on-gold-attack-b-20261102 row-level file
A5_ROWS = RESULTS / "2026-11-02/A5/results.jsonl"
K7_ROWS = RESULTS / "2026-11-02/K7/results.jsonl"  # K7/K7B submissions reused by on-l0-aware-attacker
L0AWARE_README = EXP / "on-l0-aware-attacker-20261102/README.md"
FALSE_REJECT = EXP / "on-gold-false-reject-20261102/false_reject_receipt.json"
REPLAY = EXP / "on-real-submission-replay-20261102/replay_receipt.json"
A0_INVENTORY = RESULTS / "2026-11-02/A0/submissions-inventory.json"
# Tonight's certify-gold runs, validated generated alternatives, and audit extension.
CERTIFY_GOLD_RUNS_DIR = HERE.parent / "cert/runs"
CERT_SUMMARY_JSON = HERE.parent / "cert/summary.json"
GENERATED_ALTERNATIVES = HERE.parent / "cert/validation.json"
OV1104_AUDIT_DIR = HERE.parent / "audit"

# DEV cohort outside the 50-candidate universe: the ov-sting-mutants cohort
# (README: strictly DEV by CorpusRegistry; re-checked 2026-11-03 with
# CorpusRegistry.assigned_split/sealed_identity/assigned_test_anywhere).
STING_EXTRA_DEV = {
    "format-code-task-000484", "format-code-task-000581", "format-code-task-001793",
    "format-code-task-001954", "format-code-task-002212", "format-code-task-003005",
}
STATUSES = [
    "certified", "near_certifiable", "hardened_but_leaky", "original_weak",
    "original_no_weakness_found", "no_executed_evidence",
]
GATES = ["gate1_reference", "gate2_nop", "gate3_witness", "gate4_exit0_pth",
         "gate5_train_mutants", "gate6_heldout_mutants", "gate7_alternatives"]
LADDER_130_ATTACKS = 12  # docs/mimo/verification.md: a 1.3.0-clean ladder carries 12 attacks


def wilson_interval(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials; (nan, nan) when n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + (z * z) / (4.0 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_kn(k: int, n: int) -> str:
    if n == 0:
        return "0/0"
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"


def load_json(path: Path):
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tid(short: str) -> str:
    return short if short.startswith("format-code-task-") else f"format-code-task-{short}"


def rel(path: Path) -> str:
    return str(path)


class Row:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.receipts: list[str] = []
        self.nop: list[tuple[int, float | None, str]] = []  # (priority, value, receipt)
        self.ref: list[tuple[int, float | None, str]] = []
        # original-grader mutant cells: key -> list of (reward, receipt)
        self.orig_mutants: dict[tuple, list[tuple[float | None, str]]] = defaultdict(list)
        self.orig_per_receipt: list[dict] = []
        self.a4b_mutants: dict[tuple, float] = {}  # A4B held-out mutants executed on the original package
        self.hardened: list[dict] = []  # candidate mutants_hardened blocks, with stage rank
        self.attack_orig: list[dict] = []  # {success, attempts, receipt, label}
        self.attack_sep: list[dict] = []
        self.attack_hard: list[dict] = []
        self.gold_runs: list[dict] = []
        self.alt: dict | None = None
        self.notes: list[str] = []

    def receipt(self, path: Path | str) -> str:
        p = str(path)
        if p not in self.receipts:
            self.receipts.append(p)
        return p


rows: dict[str, Row] = {}
ALLOWED: set[str] = set()


def row(task_id: str) -> Row | None:
    if task_id not in ALLOWED:
        return None
    return rows.setdefault(task_id, Row(task_id))


def add_mutant(r: Row, key: tuple, reward, receipt: str) -> None:
    """Pool executed original-grader mutant cells. Identity = (operator, line): some receipts
    (VF-R3 reruns, certifier-8 evalbase, killer-loop r2base) name cells without the file path."""
    op, path, line = key
    r.orig_mutants[(op, line)].append((reward, receipt))


# ---------------------------------------------------------------- universe
def load_universe() -> None:
    u = load_json(UNIVERSE_ROWS)
    for t in u["tasks"]:
        if t["strictly_dev"]:
            ALLOWED.add(t["task_id"])
    ALLOWED.update(STING_EXTRA_DEV)


# ---------------------------------------------------------------- mutation audits
def ingest_sting() -> None:
    d = load_json(STING_SUMMARY)
    for t in d["tasks"]:
        r = row(t["task_id"])
        if r is None:
            continue
        rc = r.receipt(STING_SUMMARY)
        if t["probe"]["status"] == "executed":
            r.ref.append((10, t["probe"]["reward"], rc))
        killed = total = 0
        for cell in t["original"]:
            if cell["status"] != "executed" or not cell["applied"]:
                continue
            m = cell["mutant"]
            add_mutant(r, (m["operator"], m["path"], m["line"]), cell["reward"], rc)
            total += 1
            killed += cell["reward"] == 0.0
        r.orig_per_receipt.append({"receipt": rc, "grader": "original", "killed": killed, "total": total,
                                   "control_ok": t["control_original_ok"]})
        rep = [c for c in t.get("repaired") or [] if c["status"] == "executed" and c["applied"]]
        if rep:
            rk = sum(c["reward"] == 0.0 for c in rep)
            r.hardened.append({"stage": 1, "grader": "task-quality repaired snapshot (ov-sting)",
                               "killed": rk, "total": len(rep), "receipt": rc})


def ingest_census() -> None:
    d = load_json(CENSUS_ROWS)
    for t in d["tasks"]:
        r = row(t["task_id"])
        if r is None:
            continue
        rc = r.receipt(CENSUS_ROWS)
        r.receipt(CENSUS_SUMMARY)
        if t.get("control_status") in ("passed", "passed_no_valid_mutants"):
            r.ref.append((9, 1.0, rc))
        elif t.get("control_status") == "failed":
            r.ref.append((9, 0.0, rc))
        if t.get("nop_verified") and t.get("nop_control_score") is not None:
            r.nop.append((9, float(t["nop_control_score"]), rc))
        killed = total = 0
        for m in t.get("mutants", []):
            if not m.get("applied") or m.get("reward") is None:
                continue
            add_mutant(r, (m["operator"], m["path"], m["line"]), m["reward"], rc)
            total += 1
            killed += m["reward"] == 0.0
        if total or t.get("control_status") == "passed_no_valid_mutants":
            r.orig_per_receipt.append({"receipt": rc, "grader": "original", "killed": killed, "total": total,
                                       "control_ok": t.get("control_original_ok")})


def ingest_r3() -> None:
    d = load_json(R3_ROWS)
    for t in d["tasks"]:
        r = row(t["task_id"])
        if r is None or not t.get("cells"):
            continue
        rc = r.receipt(R3_ROWS)
        r.receipt(R3_PAIRED)
        rep = [c for c in t["cells"] if c["status"] == "executed" and c["applied"]]
        # The 'orig_reward' on each R3 cell is copied from A2 (cache_hit); the repaired side is executed here.
        if rep:
            r.hardened.append({"stage": 1, "grader": "task-quality repaired snapshot (R3)",
                               "killed": sum(c["reward"] == 0.0 for c in rep), "total": len(rep), "receipt": rc})


def ingest_vfr3() -> None:
    pat = re.compile(r"^(?P<kind>pair-[a-z-]+?|extra-orig-rep\d)-(?P<task>format-code-task-\d{6})-(?P<op>[a-z_]+)-L(?P<line>\d+)-(?P<grader>original|repaired)$")
    for cell in load_jsonl(VFR3_CELLS):
        m = pat.match(cell["name"])
        if not m or cell.get("status") != "executed":
            continue
        r = row(m["task"])
        if r is None:
            continue
        rc = r.receipt(VFR3_CELLS)
        if m["grader"] == "original":
            add_mutant(r, (m["op"], None, int(m["line"])), cell["reward"], rc)
    # per-receipt summary for the original-grader reruns
    for r in rows.values():
        cells = [(k, v) for k, lst in r.orig_mutants.items() for v in lst if v[1] == str(VFR3_CELLS)]
        if cells:
            r.orig_per_receipt.append({"receipt": str(VFR3_CELLS), "grader": "original (VF-R3 fresh reruns)",
                                       "killed": sum(v[0] == 0.0 for _, v in cells), "total": len(cells)})


def ingest_killer_loop() -> None:
    d = load_json(KILLER_SUMMARY)
    for short, t in d["tasks"].items():
        r = row(tid(short))
        if r is None:
            continue
        rc = r.receipt(KILLER_SUMMARY)
        r1 = t.get("r1", {})
        r2 = t.get("r2", [])
        killed = sum(v == 0.0 for v in r1.values()) + sum(1 for x in r2 if x.get("killed_upgraded"))
        total = len(r1) + sum(1 for x in r2 if x.get("applied"))
        r.hardened.append({"stage": 2, "grader": "mutation-guided behavioral repair + full-hardening (killer loop)",
                           "killed": killed, "total": total, "receipt": rc})
        if t["kind"] == "original":
            # r2base cells ran the hold-out mutants on the original package
            for x in r2:
                if x.get("applied") and x.get("base_reward") is not None:
                    op, line = x["mutant"].rsplit("-L", 1)
                    add_mutant(r, (op, None, int(line)), x["base_reward"], rc)
            base = [x for x in r2 if x.get("applied") and x.get("base_reward") is not None]
            if base:
                r.orig_per_receipt.append({"receipt": rc, "grader": "original (r2base hold-out cells)",
                                           "killed": sum(x["base_reward"] == 0.0 for x in base), "total": len(base)})
        witnesses = list((t.get("witness_rewards") or {}).values())
        r.gold_runs.append({
            "run": "ov-mutant-killer-loop (hardened package gates)", "receipt": rc,
            "gate1_reference": t.get("reference_reward") == 1.0,
            "gate2_nop": t.get("nop_reward") == 0.0,
            "gate3_witness": (all(v == 0.0 for v in witnesses) if witnesses else None),
            "gate4_exit0_pth": None,
            "gate5_train_mutants": (len(r1) > 0 and all(v == 0.0 for v in r1.values())),
            "gate6_heldout_mutants": (bool(r2) and all(x.get("killed_upgraded") for x in r2 if x.get("applied"))) if r2 else None,
            "gate7_alternatives": None,
            "detail": {"r1": f"{sum(v == 0.0 for v in r1.values())}/{len(r1)}",
                       "r2": f"{sum(1 for x in r2 if x.get('killed_upgraded'))}/{len(r2)}"},
            "hardened_test_exists": bool(t.get("valid")),
        })


def ingest_cert8() -> None:
    d = load_json(CERT8_SUMMARY)
    cells = list(csv.DictReader(CERT8_CELLS.open()))
    for t in d["tasks"]:
        r = row(tid(t["task"]))
        if r is None:
            continue
        rc = r.receipt(CERT8_SUMMARY)
        r.receipt(CERT8_CELLS)
        killed = t["train_killed"] + t["eval_killed"]
        total = t["train_total"] + t["eval_total"]
        r.hardened.append({"stage": 3, "grader": "gold-certifier-8 upgraded package (train+eval)",
                           "killed": killed, "total": total, "receipt": rc})
        if t["kind"] == "original":
            base = [c for c in cells if c["task"] == t["task"] and c["arm_kind"] == "eval_base_mutant"
                    and c["status"] == "executed" and c["applied"] == "True"]
            for c in base:
                _, op, line = c["cell"].split("-", 2)
                add_mutant(r, (op, None, int(line[1:])), float(c["reward"]), str(CERT8_CELLS))
            if base:
                r.orig_per_receipt.append({"receipt": str(CERT8_CELLS), "grader": "original (evalbase cells)",
                                           "killed": sum(float(c["reward"]) == 0.0 for c in base), "total": len(base)})
        r.gold_runs.append({
            "run": "ov-gold-certifier-8", "receipt": rc,
            "gate1_reference": bool(t["gate1_ref"]), "gate2_nop": bool(t["gate2_nop"]),
            "gate3_witness": bool(t["gate3_wit"]), "gate4_exit0_pth": bool(t["gate4_exit0"]),
            "gate5_train_mutants": t["train_total"] > 0 and t["train_killed"] == t["train_total"],
            "gate6_heldout_mutants": t["eval_total"] > 0 and t["eval_killed"] == t["eval_total"],
            "gate7_alternatives": None,
            "detail": {"train": f"{t['train_killed']}/{t['train_total']}", "eval": f"{t['eval_killed']}/{t['eval_total']}"},
            "hardened_test_exists": bool(t["gate1_ref"]),
        })


_A4B_BASE: dict[str, list[tuple[str, str, int, float]]] | None = None


def a4b_base_cells() -> dict[str, list[tuple[str, str, int, float]]]:
    """task_id -> [(operator, path, line, reward)] for executed, applied base/eval-* cells."""
    global _A4B_BASE
    if _A4B_BASE is not None:
        return _A4B_BASE
    out: dict[str, dict[tuple, float]] = defaultdict(dict)
    marker = re.compile(r"MUTANT_APPLIED (\S+) (\S+?):(\d+) ")
    for res in A4B_CELLS.glob("*/result.json"):
        try:
            c = load_json(res)
        except Exception:
            continue
        if not str(c.get("variant", "")).startswith("base/eval-") or c.get("status") != "executed":
            continue
        m = marker.search(c.get("agent_log", ""))
        if not m or c.get("reward") is None:
            continue
        key = (m.group(1), m.group(2), int(m.group(3)))
        out[c["task"]][key] = max(out[c["task"]].get(key, 0.0), float(c["reward"]))
    _A4B_BASE = {t: [(*k, v) for k, v in d.items()] for t, d in out.items()}
    return _A4B_BASE


def ingest_a4b() -> None:
    manifest = {t["task_id"]: t for t in load_json(A4B_MANIFEST)}
    certified = {t["task_id"]: t for t in load_json(A4B_CERTIFIED)["tasks"]}
    for task_id, m in manifest.items():
        r = row(task_id)
        if r is None:
            continue
        if not m.get("accepted_test") or m.get("shipped_round") is None:
            r.receipt(A4B_MANIFEST)
            r.notes.append(f"A4B: status={m.get('status')} reason={m.get('reason')} (no accepted hardened test)")
            continue
        per = A4B_TASKS / task_id / f"a4b-r{m['shipped_round']}.json"
        if not per.exists():
            continue
        t = load_json(per)
        rc = r.receipt(per)
        r.receipt(A4B_MANIFEST)
        r.receipt(A4B_CERTIFIED)
        if "counts" not in t or "gates" not in t:
            r.notes.append(f"A4B: accepted test but shipped round not graded: status={t.get('status')} reason={t.get('reason')}")
            continue
        c, g = t["counts"], t["gates"]
        pairs = t.get("paired_eval_base_accepted", [])
        a2 = t.get("a2_survivors", [])
        killed = c["train_killed"] + c["probe_killed"] + sum(1 for p in pairs if p.get("arm2_killed") is True) \
            + sum(1 for s in a2 if s.get("killed") is True)
        total = c["train_total"] + c["probe_total"] + len(pairs) + len(a2)
        r.hardened.append({"stage": 4, "grader": "A4B Gold v1.1 shipped package (train+probe+held-out+A2 survivors)",
                           "killed": killed, "total": total, "receipt": rc})
        # held-out mutants were also graded on the original package (base/eval-* cells); per-cell rewards
        # live in A4B/cells/<key>/result.json (variant 'base/eval-<sha8>', MUTANT_APPLIED marker in agent_log).
        base_cells = a4b_base_cells().get(task_id, [])
        for op, path, line, reward in base_cells:
            r.a4b_mutants[(op, path, line)] = max(reward, r.a4b_mutants.get((op, path, line), 0.0))
        if base_cells:
            r.receipt(A4B_CELLS)
        status = certified.get(task_id, {}).get("status", m.get("status"))
        heldout_all = (len(pairs) > 0 and all(p.get("arm2_killed") is True for p in pairs))
        r.gold_runs.append({
            "run": f"A4B Gold v1.1 ({status}, {m.get('reason')})", "receipt": rc,
            "gate1_reference": g.get("reference_passed") is True, "gate2_nop": g.get("nop_passed") is True,
            "gate3_witness": g.get("witness_passed") is True, "gate4_exit0_pth": g.get("exit0_passed") is True,
            "gate5_train_mutants": c["train_total"] > 0 and c["train_killed"] == c["train_total"]
            and c["probe_killed"] == c["probe_total"],
            "gate6_heldout_mutants": heldout_all if pairs else None,
            "gate7_alternatives": None,
            "a4b_status": status,
            "detail": {"train": f"{c['train_killed']}/{c['train_total']}", "probe": f"{c['probe_killed']}/{c['probe_total']}",
                       "heldout_base_accepted_killed": f"{sum(1 for p in pairs if p.get('arm2_killed'))}/{len(pairs)}",
                       "eval_total": c["eval_total"], "eval_base_accepted": c["eval_base_accepted"],
                       "a2_survivors_killed": f"{sum(1 for s in a2 if s.get('killed'))}/{len(a2)}"},
            "hardened_test_exists": True,
        })


# ---------------------------------------------------------------- controls, attackers, separate verifier
def ingest_mimo_census() -> None:
    best: dict[str, dict] = {}
    for c in csv.DictReader(MIMO_CENSUS.open()):
        if c["manifest_version"] != "mimo-clean-v3" or c["task_id"] not in ALLOWED:
            continue
        cur = best.setdefault(c["task_id"], {})
        if c["nop"] in ("0", "1"):
            cur["nop"] = float(c["nop"])
        if c["oracle"] in ("0", "1") or c["oracle"].startswith("fail:"):
            cur["oracle"] = 1.0 if c["oracle"] == "1" else 0.0
        if c["ladder_verdict"] in ("clean", "cracked") and c["ladder_version"] == "1.3.0":
            cracked = [a for a in c["ladder_cracking_attacks"].split(",") if a]
            cur["ladder"] = len(cracked)
    for task_id, cur in best.items():
        r = row(task_id)
        rc = r.receipt(MIMO_CENSUS)
        if "nop" in cur:
            r.nop.append((1, cur["nop"], rc))
        if "oracle" in cur:
            r.ref.append((1, cur["oracle"], rc))
        if "ladder" in cur:
            r.attack_sep.append({"success": cur["ladder"], "attempts": LADDER_130_ATTACKS, "receipt": rc,
                                 "label": "mimo-clean-v3 package (separate-verifier@4 chain), 12-attack ladder 1.3.0"})


def ingest_har169() -> None:
    text = HAR169.read_text()
    for task_short, mode, agent, reward in re.findall(r"^\| (\d{6}) \| (original \(shared\)|separate) \| (nop|oracle) \| (\d) \|", text, re.M):
        r = row(tid(task_short))
        if r is None:
            continue
        rc = r.receipt(HAR169)
        if mode.startswith("original"):
            (r.nop if agent == "nop" else r.ref).append((5, float(reward), rc))


def ingest_h2() -> None:
    by: dict[str, dict] = defaultdict(dict)
    for c in load_json(H2_CELLS):
        by[c["task_id"]][c["cell"]] = c["reward"]
    for task_id, cells in by.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(H2_CELLS)
        if "cheat" in cells:
            r.attack_sep.append({"success": int(cells["cheat"] == 1.0), "attempts": 1, "receipt": rc,
                                 "label": "mimo-clean-v5 separate verifier: 3 .pth start-up probes in one cheat cell (oracle 1, nop 0)"})


def ingest_sepver_v2() -> None:
    text = SEPVER_V2.read_text()
    for line in text.splitlines():
        m = re.match(r"^\| (\d{6}) \| (\d) / (\d) \| (\d) / (\d) \|(.*)\|\s*$", line)
        if not m:
            continue
        r = row(tid(m.group(1)))
        if r is None:
            continue
        rc = r.receipt(SEPVER_V2)
        r.ref.append((4, float(m.group(3)), rc))
        r.nop.append((4, float(m.group(5)), rc))
        v2_s = cur_s = n = 0
        for cell in m.group(6).split("|"):
            cell = cell.strip()
            mm = re.match(r"^(\d) / (\d)", cell)
            if mm:
                n += 1
                v2_s += mm.group(1) == "1"
                cur_s += mm.group(2) == "1"
        if n:
            r.attack_orig.append({"success": cur_s, "attempts": n, "receipt": rc, "label": "separate-verifier-v2 attacks A1-A6 on current (shared) grading"})
            r.attack_sep.append({"success": v2_s, "attempts": n, "receipt": rc, "label": "separate-verifier@2 patch-only verifier, attacks A1-A6"})
    m = re.search(r"^\| A7 002552 v2 / cur \| (\d) / (\d) \|", text, re.M)
    if m and (r := row(tid("002552"))):
        rc = r.receipt(SEPVER_V2)
        r.attack_orig.append({"success": int(m.group(2)), "attempts": 1, "receipt": rc, "label": "sepver-v2 A7 import-time obfuscated exit, current grading"})
        r.attack_sep.append({"success": int(m.group(1)), "attempts": 1, "receipt": rc, "label": "sepver-v2 A7, separate-verifier@2"})
    m = re.search(r"^\| unittest \| 000803, 000838 \| 1, 1 \| 0, 0 \| 0, 0 \| 1, 1 \|", text, re.M)
    if m and (r := row(tid("000838"))):
        rc = r.receipt(SEPVER_V2)
        r.ref.append((4, 1.0, rc))
        r.nop.append((4, 0.0, rc))
        r.attack_orig.append({"success": 1, "attempts": 1, "receipt": rc, "label": "separate-verifier@3 port validation: unittest tamper on current grading"})
        r.attack_sep.append({"success": 0, "attempts": 1, "receipt": rc, "label": "separate-verifier@3 port validation: unittest tamper"})


def ingest_level_matrix(path: Path, label: str) -> None:
    by: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
    for c in csv.DictReader(path.open()):
        by[c["task"]][c["level"]].append((c["arm"], float(c["reward"])))
    for task_id, levels in by.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(path)
        for level, cells in levels.items():
            attacks = [(a, v) for a, v in cells if a not in ("correct", "empty")]
            ctrl = dict((a, v) for a, v in cells if a in ("correct", "empty"))
            if level == "L0":
                if "correct" in ctrl:
                    r.ref.append((8, ctrl["correct"], rc))
                if "empty" in ctrl:
                    r.nop.append((8, ctrl["empty"], rc))
                if attacks:
                    r.attack_orig.append({"success": sum(v == 1.0 for _, v in attacks), "attempts": len(attacks),
                                          "receipt": rc, "label": f"{label}: TQ-2 attack families {','.join(a for a, _ in attacks)} on the original grader"})
            elif level == "H" and attacks:
                r.attack_hard.append({"success": sum(v == 1.0 for _, v in attacks), "attempts": len(attacks),
                                      "receipt": rc, "label": f"{label}: grader-hardening@1 (L1+L3)"})


def ingest_t2() -> None:
    d = load_json(T2_SUMMARY)
    by: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for c in d["cells"]:
        if c["status"] != "executed":
            continue
        task, cell = c["cell"].split("/", 1)
        if not task.isdigit():
            continue
        level, _, arm = cell.partition("-")
        by[tid(task)].append((level, arm, c["reward"]))
    for task_id, cells in by.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(T2_SUMMARY)
        for level, store in (("L0", r.attack_orig), ("HS", r.attack_sep), ("H1", r.attack_hard)):
            attacks = [(a, v) for lv, a, v in cells if lv == level and a not in ("reference", "empty")]
            if level == "L0":
                for lv, a, v in cells:
                    if lv == "L0" and a == "reference":
                        r.ref.append((8, v, rc))
                    if lv == "L0" and a == "empty":
                        r.nop.append((8, v, rc))
            if attacks:
                store.append({"success": sum(v == 1.0 for _, v in attacks), "attempts": len(attacks), "receipt": rc,
                              "label": f"t2-unified-harden {level}: {','.join(a for a, _ in attacks)}"})


def ingest_k4() -> None:
    by: dict[str, list[dict]] = defaultdict(list)
    for c in load_json(K4_SLICE1):
        if c["status"] == "executed":
            by[c["task"]].append(c)
    for task_id, cells in by.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(K4_SLICE1)
        for level, store in (("L0", r.attack_orig), ("HS", r.attack_sep), ("H1", r.attack_hard)):
            attacks = [c for c in cells if c["layer"] == level and c["arm"] not in ("reference", "empty")]
            if level == "L0":
                for c in cells:
                    if c["layer"] == "L0" and c["arm"] == "reference":
                        r.ref.append((8, c["reward"], rc))
                    if c["layer"] == "L0" and c["arm"] == "empty":
                        r.nop.append((8, c["reward"], rc))
            if attacks:
                store.append({"success": sum(c["reward"] == 1.0 for c in attacks), "attempts": len(attacks), "receipt": rc,
                              "label": f"k4-slice1 {level}: {','.join(c['arm'] for c in attacks)}"})
    by2: dict[str, list[dict]] = defaultdict(list)
    for c in load_json(K4_ATTACKER):
        if c["status"] == "executed" and not c.get("refused"):
            by2[c["task"]].append(c)
    for task_id, cells in by2.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(K4_ATTACKER)
        for level, store in (("L0", r.attack_orig), ("HS", r.attack_sep), ("H1", r.attack_hard)):
            sub = [c for c in cells if c["layer"] == level]
            if sub:
                store.append({"success": sum(c["reward"] == 1.0 for c in sub), "attempts": len(sub), "receipt": rc,
                              "label": f"k4 adaptive attacker (claude-sonnet) vs {level}"})


def ingest_adaptive_attackers() -> None:
    for path, label in ((A5B_ROWS, "A5B adaptive attacker"), (A5_ROWS, "A5 adaptive attacker")):
        for c in load_jsonl(path):
            if not c.get("complete"):
                continue
            r = row(c["task_id"])
            if r is None:
                continue
            rc = r.receipt(path)
            if c.get("L0", {}).get("status") == "executed":
                r.attack_orig.append({"success": int(c["L0"]["reward"] == 1.0), "attempts": 1, "receipt": rc, "label": f"{label} vs original (L0)"})
            if c.get("Gold", {}).get("status") == "executed":
                r.attack_hard.append({"success": int(c["Gold"]["reward"] == 1.0), "attempts": 1, "receipt": rc, "label": f"{label} vs A4B Gold package"})
    best: dict[str, dict] = {}
    for c in load_jsonl(K7_ROWS):  # complete records with executed cells win (resumable sweep)
        if c.get("complete") and c.get("L0", {}).get("status") == "executed":
            best[c["task_id"]] = c
    for task_id, c in best.items():
        r = row(task_id)
        if r is None:
            continue
        rc = r.receipt(K7_ROWS)
        r.receipt(L0AWARE_README)
        r.attack_orig.append({"success": int(c["L0"]["reward"] == 1.0), "attempts": 1, "receipt": rc, "label": "K7/K7B naive adaptive attacker vs original (L0)"})
        if c.get("HS", {}).get("status") == "executed":
            r.attack_sep.append({"success": int(c["HS"]["reward"] == 1.0), "attempts": 1, "receipt": rc, "label": "K7/K7B same submission vs evallab harden separate verifier (HS)"})


# ---------------------------------------------------------------- alternatives
def ingest_alternatives() -> None:
    inv = load_json(A0_INVENTORY)
    accepted_original: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in inv["submissions"]:
        if s["task_id"] in ALLOWED and s["original_reward"] == 1.0:
            accepted_original[s["task_id"]][s["patch_sha256"][:8]] = s
    hardened: dict[str, dict[str, float]] = defaultdict(dict)  # task -> sha8 -> reward on A4B shipped grader
    fr = load_json(FALSE_REJECT)
    for s in fr["submissions_evaluated"]:
        hardened[s["task_id"]][s["patch_sha8"]] = s["s0_reward"]
    rp = load_json(REPLAY)
    for s in rp["real_submissions"]:
        hardened[s["task_id"]].setdefault(s["patch_sha256"][:8], s["gold_reward"])
    generated: dict[str, list[dict]] = defaultdict(list)
    if GENERATED_ALTERNATIVES.exists():
        for g in load_json(GENERATED_ALTERNATIVES):
            generated[g["task_id"]].append(g)
    for task_id in set(accepted_original) | set(hardened) | set(generated):
        r = row(task_id)
        if r is None:
            continue
        subs = accepted_original.get(task_id, {})
        graded = {k: v for k, v in hardened.get(task_id, {}).items()}
        total = len(set(subs) | set(graded))
        if graded:
            rc = r.receipt(FALSE_REJECT)
            r.receipt(REPLAY)
            r.receipt(A0_INVENTORY)
            r.alt = {"accepted": sum(v == 1.0 for v in graded.values()), "total": total,
                     "grader": "A4B Gold v1.1 shipped test suite (S0)", "receipt": rc,
                     "original_grader_accepted": len(subs)}
        elif subs:
            rc = r.receipt(A0_INVENTORY)
            r.alt = {"accepted": len(subs), "total": len(subs), "grader": "original", "receipt": rc}
        gens = generated.get(task_id, [])
        if gens:
            rc = r.receipt(GENERATED_ALTERNATIVES)
            ok = sum(g.get("original_reward") == 1.0 and not g.get("test_aware") for g in gens)
            if r.alt is None:
                r.alt = {"accepted": ok, "total": len(gens), "grader": "original (generated, validated on Modal)", "receipt": rc}
            else:
                r.alt["generated"] = {"accepted": ok, "total": len(gens), "receipt": rc}


def ingest_ov1104_audits() -> None:
    if not OV1104_AUDIT_DIR.exists():
        return
    for path in sorted(OV1104_AUDIT_DIR.glob("*/audit.json")):
        t = load_json(path)
        r = row(t["task_id"])
        if r is None:
            continue
        rc = r.receipt(path)
        if t["probe"]["status"] == "executed":
            r.ref.append((10, t["probe"]["reward"], rc))
        killed = total = 0
        for cell in t["original"]:
            if cell["status"] != "executed" or not cell["applied"]:
                continue
            m = cell["mutant"]
            add_mutant(r, (m["operator"], m["path"], m["line"]), cell["reward"], rc)
            total += 1
            killed += cell["reward"] == 0.0
        r.orig_per_receipt.append({
            "receipt": rc, "grader": "original", "killed": killed, "total": total,
            "control_ok": t.get("control_original_ok"),
        })


def ingest_certify_gold() -> None:
    if not CERTIFY_GOLD_RUNS_DIR.exists():
        return
    for path in sorted(CERTIFY_GOLD_RUNS_DIR.glob("*/certification.json")):
        run_name = path.parent.name
        is_h5 = any(run_name.endswith(sfx) for sfx in ("-r2", "-r3", "-r2-honest"))
        d = load_json(path)
        task_id = d.get("task_id") or d.get("task")
        if not task_id:
            continue
        r = row(tid(task_id))
        if r is None:
            continue
        rc = r.receipt(path)
        alt_total = d.get("alternative_solutions_total")
        alt_acc = d.get("alternative_solutions_accepted")
        label = f"certify-gold H5 ({run_name}: {d.get('status')})" if is_h5 else f"certify-gold ({d.get('status')})"
        r.gold_runs.append({
            "run": label, "receipt": rc, "certification_status": d.get("status"),
            "protocol": "H5" if is_h5 else "H3_H4",
            "gate1_reference": d.get("gate1_reference_passed"), "gate2_nop": d.get("gate2_nop_passed"),
            "gate3_witness": d.get("gate3_witness_passed"), "gate4_exit0_pth": d.get("gate4_exit0_pth_passed"),
            "gate5_train_mutants": (d.get("train_mutants_total", 0) > 0 and d.get("train_mutants_killed") == d.get("train_mutants_total")),
            "gate6_heldout_mutants": (d.get("eval_mutants_total", 0) > 0 and d.get("eval_mutants_killed") == d.get("eval_mutants_total")),
            "gate7_alternatives": (alt_acc is not None and alt_acc >= 2),
            "detail": {"train": f"{d.get('train_mutants_killed')}/{d.get('train_mutants_total')}",
                       "eval": f"{d.get('eval_mutants_killed')}/{d.get('eval_mutants_total')}",
                       "alternatives": f"{alt_acc}/{alt_total}"},
            "hardened_test_exists": d.get("gate1_reference_passed") is True,
        })
        if not is_h5 and alt_total is not None:
            r.alt = {"accepted": alt_acc or 0, "total": alt_total, "grader": "certify-gold upgraded package", "receipt": rc}

# ---------------------------------------------------------------- assembly
def pick(values: list[tuple[int, float | None, str]]) -> dict | None:
    if not values:
        return None
    values = sorted(values, key=lambda x: -x[0])
    top = values[0]
    conflicts = sorted({(v, rc) for _, v, rc in values if v != top[1]})
    out = {"value": top[1], "receipt": top[2]}
    if conflicts:
        out["conflicts"] = [{"value": v, "receipt": rc} for v, rc in conflicts]
    return out


def mutants_block(r: Row) -> tuple[dict | None, bool]:
    pooled = r.orig_mutants
    if not pooled and not r.orig_per_receipt:
        return None, False
    total = len(pooled)
    accepted = sum(1 for lst in pooled.values() if any(v == 1.0 for v, _ in lst))
    killed = total - accepted
    lo, hi = wilson_interval(killed, total) if total else (None, None)
    primary = max(r.orig_per_receipt, key=lambda x: x.get("total", 0))["receipt"] if r.orig_per_receipt else None
    block = {"killed": killed, "total": total,
             "wilson_lo": None if total == 0 else round(lo, 4), "wilson_hi": None if total == 0 else round(hi, 4),
             "receipt": primary,
             "per_receipt": r.orig_per_receipt,
             "note": "pooled over distinct executed mutants (operator,path,line) across audits; a mutant counts as accepted if any execution scored 1.0"}
    weak = accepted > 0
    return block, weak


def a4b_block(r: Row) -> dict | None:
    """A4B held-out mutants executed on the original package (gold_certifier operator family, wider than
    the 8 STING/census operators). Kept out of the PREREG k/n column; reported for sensitivity."""
    if not r.a4b_mutants:
        return None
    total = len(r.a4b_mutants)
    killed = sum(1 for v in r.a4b_mutants.values() if v == 0.0)
    lo, hi = wilson_interval(killed, total)
    return {"killed": killed, "total": total, "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4), "receipt": str(A4B_CELLS)}


def hardened_block(r: Row) -> dict | None:
    if not r.hardened:
        return None
    best = max(r.hardened, key=lambda h: (h["stage"], h["total"]))
    lo, hi = wilson_interval(best["killed"], best["total"]) if best["total"] else (None, None)
    return {"killed": best["killed"], "total": best["total"],
            "wilson_lo": None if not best["total"] else round(lo, 4), "wilson_hi": None if not best["total"] else round(hi, 4),
            "receipt": best["receipt"], "grader": best["grader"],
            "others": [{k: h[k] for k in ("grader", "killed", "total", "receipt")} for h in r.hardened if h is not best]}


def attack_block(items: list[dict]) -> dict | None:
    if not items:
        return None
    success = sum(i["success"] for i in items)
    attempts = sum(i["attempts"] for i in items)
    primary = max(items, key=lambda i: i["attempts"])["receipt"]
    return {"success": success, "attempts": attempts, "receipt": primary,
            "per_receipt": [{k: i[k] for k in ("success", "attempts", "receipt", "label")} for i in items]}


def gold_block(r: Row) -> tuple[dict, dict | None]:
    if not r.gold_runs:
        return {"passed": 0, "of": 7, "failed": GATES, "receipt": None, "run": None,
                "note": "no gold run executed; all 7 gates unmeasured"}, None
    def score(run: dict) -> tuple:
        passed = sum(1 for g in GATES if run.get(g) is True)
        is_h3_h4 = run.get("protocol") != "H5"
        return (is_h3_h4, passed, run.get("a4b_status") in ("gold_structural", "gold_heldout"), run["run"].startswith("certify-gold"))
    best = max(r.gold_runs, key=score)
    failed = [g for g in GATES if best.get(g) is not True]
    return {"passed": sum(1 for g in GATES if best.get(g) is True), "of": 7, "failed": failed,
            "receipt": best["receipt"], "run": best["run"], "detail": best.get("detail"),
            "unmeasured": [g for g in GATES if best.get(g) is None],
            "all_runs": [{"run": g["run"], "receipt": g["receipt"], "passed": sum(1 for x in GATES if g.get(x) is True),
                          "failed": [x for x in GATES if g.get(x) is not True]} for g in r.gold_runs]}, best


def gates_1_6(run: dict) -> bool:
    if run.get("a4b_status") in ("gold_structural", "gold_heldout"):
        return True
    return all(run.get(g) is True for g in GATES[:6])


def assemble(r: Row) -> dict:
    mo, weak = mutants_block(r)
    mh = hardened_block(r)
    gg, best_run = gold_block(r)
    alt = r.alt
    alt_accepted = alt["accepted"] if alt else None
    certified = any(g.get("protocol") != "H5" and g.get("certification_status") == "certified" and g.get("gate7_alternatives") for g in r.gold_runs)
    gates16_runs = [g for g in r.gold_runs if g.get("protocol") != "H5" and gates_1_6(g)]
    hardened_exists = any(g.get("hardened_test_exists") for g in r.gold_runs)
    leaky = False
    for g in r.gold_runs:
        if not g.get("hardened_test_exists"):
            continue
        if any(g.get(x) is False for x in GATES[1:4]) or g.get("gate5_train_mutants") is False or g.get("gate6_heldout_mutants") is False:
            leaky = True
    if certified:
        status = "certified"
    elif gates16_runs:
        status = "near_certifiable"  # see NOTES: gate 7 counts as unmeasured until a certify-gold run grades alternatives
    elif hardened_exists and leaky:
        status = "hardened_but_leaky"
    elif weak:
        status = "original_weak"
    elif mo and mo["total"] > 0:
        status = "original_no_weakness_found"
    else:
        status = "no_executed_evidence"

    missing: list[str] = []
    if status != "certified":
        if not r.gold_runs:
            missing.append("gold run: gates 1-7 unmeasured")
        else:
            ref_run = best_run
            for g in GATES[:6]:
                if ref_run.get(g) is True:
                    continue
                det = ref_run.get("detail") or {}
                if g == "gate5_train_mutants":
                    missing.append(f"gate5: train mutants killed {det.get('train') or det.get('r1')}" + (f", probe {det['probe']}" if det.get("probe") else ""))
                elif g == "gate6_heldout_mutants":
                    hv = det.get("heldout_base_accepted_killed") or det.get("eval") or det.get("r2")
                    if ref_run.get(g) is None:
                        if det.get("eval_total") is not None:
                            if det["eval_total"] == 0:
                                missing.append("gate6: no held-out mutant generated")
                            else:
                                missing.append(f"gate6: 0 of {det['eval_total']} held-out mutants accepted by the original grader, none left to test the hardened package")
                        else:
                            missing.append("gate6: held-out mutants not executed on this package" + (f" ({hv})" if hv and hv != "0/0" else ""))
                    else:
                        k, n = hv.split("/")
                        missing.append(f"gate6: {int(n) - int(k)} held-out mutant(s) survive ({hv})")
                elif ref_run.get(g) is None:
                    missing.append(f"{g.split('_')[0]}: unmeasured")
                else:
                    missing.append(f"{g.split('_')[0]}: failed")
            if ref_run.get("certification_status") is None:
                missing.append("certify-gold run (certification.json) not executed")
        if alt is None:
            missing.append("2 alternative solutions (none measured)")
        elif alt_accepted < 2:
            missing.append(f"{2 - alt_accepted} alternative solution(s) ({alt_accepted}/{alt['total']} accepted by {alt['grader']})")
        if status in ("original_weak", "no_executed_evidence") and not hardened_exists and mo and mo["total"] - mo["killed"] > 0:
            missing.append(f"hardened package: original grader accepts {mo['total'] - mo['killed']} executed mutant(s)")
        if status == "no_executed_evidence" and not mo:
            missing.append("executed mutant audit of the original grader")
    ref = pick(r.ref)
    if ref and ref["value"] == 0.0:
        r.notes.append("reference control scored 0.0 on the original package: mutant kills on it are uninformative")
        missing.append("reference control scored 0.0 on the original package")
    row_out = {
        "task_id": r.task_id,
        "nop_reward": pick(r.nop),
        "reference_reward": pick(r.ref),
        "mutants_original": mo,
        "mutants_original_a4b_heldout": a4b_block(r),
        "mutants_hardened": mh,
        "attacker_vs_original": attack_block(r.attack_orig),
        "attacker_vs_separate_verifier": attack_block(r.attack_sep),
        "attacker_vs_hardened": attack_block(r.attack_hard),
        "alt_solutions": alt,
        "gold_gates": gg,
        "status": status,
        "missing_for_certification": missing,
        "receipts": r.receipts,
        "notes": r.notes,
    }
    return row_out


def main() -> None:
    load_universe()
    for fn in (ingest_sting, ingest_census, ingest_r3, ingest_vfr3, ingest_killer_loop, ingest_cert8, ingest_a4b,
               ingest_mimo_census, ingest_har169, ingest_h2, ingest_sepver_v2,
               lambda: ingest_level_matrix(H1_CELLS, "h1-grader-hardening"),
               lambda: ingest_level_matrix(AHA_ALL, "a-harden-all (full)"),
               lambda: ingest_level_matrix(AHA_L1, "a-harden-all (l1-only)"),
               ingest_t2, ingest_k4, ingest_adaptive_attackers, ingest_alternatives):
        fn()
    # Capture $0 existing-receipts H1 before ingesting tonight's audit-mutants and certify-gold runs
    pre_rows = [assemble(r) for r in rows.values()]
    pre_mut = [o for o in pre_rows if o["mutants_original"] and o["mutants_original"]["total"] > 0]
    pre_weak = [o for o in pre_mut if o["mutants_original"]["total"] - o["mutants_original"]["killed"] > 0]
    pre_k, pre_n = len(pre_weak), len(pre_mut)
    pre_lo, pre_hi = wilson_interval(pre_k, pre_n)
    ingest_ov1104_audits()
    ingest_certify_gold()
    out_rows = [assemble(r) for r in rows.values()]
    for o in out_rows:
        existing = [p for p in o["receipts"] if Path(p).exists()]
        assert existing, f"{o['task_id']}: no receipt path exists on disk"
        assert o["status"] in STATUSES
    order = {s: i for i, s in enumerate(STATUSES)}
    out_rows.sort(key=lambda o: (order[o["status"]], len(o["missing_for_certification"]), o["task_id"]))
    (HERE / "scorecard.json").write_text(json.dumps(out_rows, indent=1) + "\n")

    # headline
    with_mutants = [o for o in out_rows if o["mutants_original"] and o["mutants_original"]["total"] > 0]
    weak = [o for o in with_mutants if o["mutants_original"]["total"] - o["mutants_original"]["killed"] > 0]
    k, n = len(weak), len(with_mutants)
    lo, hi = wilson_interval(k, n)
    def accepted_any(o):
        a = o["mutants_original"]
        b = o["mutants_original_a4b_heldout"]
        return (a and a["total"] - a["killed"] > 0) or (b and b["total"] - b["killed"] > 0)
    with_any = [o for o in out_rows if (o["mutants_original"] and o["mutants_original"]["total"] > 0) or o["mutants_original_a4b_heldout"]]
    weak_any = [o for o in with_any if accepted_any(o)]
    k2, n2 = len(weak_any), len(with_any)
    lo2, hi2 = wilson_interval(k2, n2)
    dist = {s: sum(1 for o in out_rows if o["status"] == s) for s in STATUSES}
    h2 = [o for o in out_rows if order[o["status"]] <= order["near_certifiable"]]
    sources = {
        str(STING_SUMMARY): "18-task STING pilot: reference probe, executed mutants on original and repaired graders",
        str(CENSUS_ROWS): "A2 census rows (28 audited tasks): nop control, reference control, executed mutants on original grader",
        str(CENSUS_SUMMARY): "A2 census aggregates",
        str(R3_ROWS): "R3 repaired-grader mutant cells (original side copied from A2)",
        str(R3_PAIRED): "R3 paired comparison aggregates",
        str(VFR3_CELLS): "VF-R3 fresh reruns of R3 pairs (original and repaired cells)",
        str(KILLER_SUMMARY): "mutation-guided repair loop: hardened-package gates, r1/r2 mutants, r2base on original-kind tasks",
        str(CERT8_SUMMARY): "gold-certifier-8: gates 1-6 per task on upgraded packages",
        str(CERT8_CELLS): "gold-certifier-8 cells: evalbase mutants on the base (original) package for original-kind tasks",
        str(A4B_MANIFEST): "A4B Gold v1.1 manifest (status, reason, accepted_test)",
        str(A4B_CERTIFIED): "A4B per-task status/reason",
        str(A4B_TASKS): "A4B per-task shipped-round receipts (gates, counts, paired held-out, A2 survivors)",
        str(A4B_CELLS): "A4B per-cell results; base/eval-* cells = held-out mutants executed on the original package (sensitivity only)",
        str(MIMO_CENSUS): "mimo-clean-v3 census: nop/oracle fallback and 12-attack ladder on the separate-verifier chain",
        str(HAR169): "HAR-169 separate verifier: oracle/nop on original and separate modes",
        str(H2_CELLS): "H2: .pth start-up probes against mimo-clean-v5 separate verifier",
        str(SEPVER_V2): "separate-verifier@2/@3 validation: attacks on current vs patch-only verifier",
        str(H1_CELLS): "H1 grader-hardening matrix: L0/L1/H x TQ-2 attacks, correct, empty",
        str(AHA_ALL): "a-harden-all full-class tasks: L0/L1/H x TQ-2 attacks, correct, empty",
        str(AHA_L1): "a-harden-all l1-only tasks: L0/L1 x attacks A-D, correct, empty",
        str(T2_SUMMARY): "t2-unified-harden: L0/H1/HS cells (reference, empty, exit-0 and hook attacks, F3)",
        str(K4_SLICE1): "K4 slice 1: reference/empty/attacks on L0, H1, HS",
        str(K4_ATTACKER): "K4 adaptive attacker vs L0/H1/HS",
        str(A5B_ROWS): "A5B adaptive attacker vs original (L0) and A4B Gold package",
        str(A5_ROWS): "A5 adaptive attacker vs original and Gold package",
        str(K7_ROWS): "K7/K7B adaptive attacker vs original (L0) and harden separate verifier (HS)",
        str(L0AWARE_README): "K7C: reuse of K7B submissions (tripwire scan); no new grader cells",
        str(FALSE_REJECT): "real passing submissions graded by the A4B shipped test suite (s0_reward)",
        str(REPLAY): "real passing submissions replayed on the A4B Gold package (gold_reward)",
        str(A0_INVENTORY): "inventory of real submissions with original-grader reward",
        str(UNIVERSE_ROWS): "task universe with strictly_dev flag",
    }
    exec_audit_ids = set(load_json(UNIVERSE_SUMMARY)["task_ids"]["executable_audit"])
    ea_rows = [o for o in out_rows if o["task_id"] in exec_audit_ids and o["mutants_original"] and o["mutants_original"]["total"] > 0]
    ea_weak = [o for o in ea_rows if o["mutants_original"]["total"] - o["mutants_original"]["killed"] > 0]
    ea_k, ea_n = len(ea_weak), len(ea_rows)
    ea_lo, ea_hi = wilson_interval(ea_k, ea_n)
    headline = {
        "H1": {"question": "share of tasks with executed mutant evidence whose original grader accepted >=1 broken mutant ($0 existing receipts)",
               "k": pre_k, "n": pre_n, "wilson95": [round(pre_lo, 4), round(pre_hi, 4)], "fmt": fmt_kn(pre_k, pre_n),
               "tasks_weak": [o["task_id"] for o in pre_weak],
               "tasks_not_weak": [o["task_id"] for o in pre_mut if o not in pre_weak]},
        "H1_with_ov1104_audit_extension": {
            "question": "same as H1 after executing audit-mutants on the 3 remaining on-gold-universe executable_audit tasks (002680, 002976, 003007)",
            "k": k, "n": n, "wilson95": [round(lo, 4), round(hi, 4)], "fmt": fmt_kn(k, n),
            "tasks_weak": [o["task_id"] for o in weak],
            "tasks_not_weak": [o["task_id"] for o in with_mutants if o not in weak]},
        "H1_gold_universe_executable_audit_census": {
            "question": "complete census over all 31 mutatable executable_audit tasks of on-gold-universe (28 from A2 + 3 audited in ov1104-grader)",
            "k_pooled_with_vfr3": ea_k, "n": ea_n, "wilson95_pooled": [round(ea_lo, 4), round(ea_hi, 4)], "fmt_pooled": fmt_kn(ea_k, ea_n),
            "k_a2_plus_ov1104_only": ea_k - 1, "fmt_a2_plus_ov1104_only": fmt_kn(ea_k - 1, ea_n)},
        "H1_sensitivity_including_a4b_heldout": {
            "question": "same as H1_with_ov1104_audit_extension but also counting the A4B held-out mutants executed on the original package (wider operator family)",
            "k": k2, "n": n2, "wilson95": [round(lo2, 4), round(hi2, 4)], "fmt": fmt_kn(k2, n2),
            "tasks_weak_only_via_a4b": [o["task_id"] for o in weak_any if o not in weak]},
        "status_distribution": dist,
        "H2": {"count_near_certifiable_or_better": len(h2),
               "count_pre_ov1104_runs": 11,
               "tasks": [{"task_id": o["task_id"], "status": o["status"], "missing": o["missing_for_certification"]} for o in h2]},
        "H3_H4_H5_certification": load_json(CERT_SUMMARY_JSON) if CERT_SUMMARY_JSON.exists() else None,
        "rows": len(out_rows),
        "sources": sources,
    }
    (HERE / "headline.json").write_text(json.dumps(headline, indent=1) + "\n")
    write_markdown(out_rows, headline)
    print(f"rows={len(out_rows)} H1={fmt_kn(pre_k, pre_n)} ext={fmt_kn(k, n)} status={dist}")


def cell_kn(block: dict | None, k: str = "killed", n: str = "total") -> str:
    if not block:
        return "null"
    return f"{block[k]}/{block[n]}"


def write_markdown(out_rows: list[dict], headline: dict) -> None:
    lines = ["# Grader-trust scorecard (DEV tasks, receipts only)", "",
             "Built by `build_scorecard.py` from existing receipts under `/private/tmp/eval-lab-study/research/experiments/` and `~/Developer/eval-lab-results/2026-11-02/`, plus `ov1104-grader` audit/certification receipts. Rule and columns: `../PREREG.md`. `null` = not measured.",
             "", f"- H1 ($0 existing receipts, original grader accepted >=1 executed broken STING/census-family mutant): **{headline['H1']['fmt']}**",
             f"- H1 with `ov1104-grader` audit extension (+`002680`, `002976`, `003007`): **{headline['H1_with_ov1104_audit_extension']['fmt']}** (`on-gold-universe` `executable_audit` 31/31 census: **{headline['H1_gold_universe_executable_audit_census']['fmt_pooled']}** pooled, **{headline['H1_gold_universe_executable_audit_census']['fmt_a2_plus_ov1104_only']}** A2+ov1104 only); sensitivity incl. A4B held-out mutants on the original: {headline['H1_sensitivity_including_a4b_heldout']['fmt']}",
             "- Status distribution (strict H3/H4): " + ", ".join(f"{s} {c}" for s, c in headline["status_distribution"].items()),
             f"- H2 (near_certifiable or better): **{headline['H2']['count_near_certifiable_or_better']}** (11 pre-run; `002139` upgraded from `hardened_but_leaky` after passing Gates 1-6 in `ov1104-grader`)",
             "- H3/H4/H5 certification: **0/5** under strict blind synthesis (`H3/H4`); **2** tasks reach `certification.json.status == \"certified\"` (7/7 gates, 2/2 held-out alternatives accepted) under `H5` alternative-informed synthesis: `000682-r3` (`provisional: test-aware alternatives`, 4/4 total accepted) and `002391-r2-honest` (4/4 honest alternatives accepted, non-test-aware; `1/2` held-out when including the `HAR-202` cheat copy `real-1` that drops `published=None`)", "",
             "| task | status | nop | ref | mutants killed by original k/n (Wilson 95%) | A4B held-out mutants killed by original k/n | mutants killed by hardened k/n | attacker vs original | attacker vs separate verifier | attacker vs hardened | alt solutions accepted | gold gates | missing for certification |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for o in out_rows:
        mo = o["mutants_original"]
        mo_s = "null" if not mo else (f"{mo['killed']}/{mo['total']} ({100 * mo['wilson_lo']:.0f}-{100 * mo['wilson_hi']:.0f}%)" if mo["total"] else "0/0")
        mh = o["mutants_hardened"]
        mh_s = "null" if not mh else f"{mh['killed']}/{mh['total']}"
        ma = o["mutants_original_a4b_heldout"]
        ma_s = "null" if not ma else f"{ma['killed']}/{ma['total']}"
        alt = o["alt_solutions"]
        alt_s = "null" if not alt else f"{alt['accepted']}/{alt['total']} ({alt['grader'].split(' (')[0]})"
        gg = o["gold_gates"]
        gg_s = f"{gg['passed']}/7" + (f" ({gg['run'].split(' (')[0]})" if gg.get("run") else "")
        def kn(b):
            return "null" if not b else f"{b['success']}/{b['attempts']}"
        def val(b):
            return "null" if not b else (f"{b['value']:g}" if b["value"] is not None else "null")
        lines.append(f"| {o['task_id'].replace('format-code-task-', '')} | {o['status']} | {val(o['nop_reward'])} | {val(o['reference_reward'])} | {mo_s} | {ma_s} | {mh_s} | {kn(o['attacker_vs_original'])} | {kn(o['attacker_vs_separate_verifier'])} | {kn(o['attacker_vs_hardened'])} | {alt_s} | {gg_s} | {'; '.join(o['missing_for_certification']) or '-'} |")
    lines += ["", "Attacker columns pool every executed attack cell across receipts (success/attempts); per-receipt counts are in `scorecard.json`.", ""]
    lines += ["## Closest to certifiable", ""]
    order = {s: i for i, s in enumerate(STATUSES)}
    closest = sorted(out_rows, key=lambda o: (order[o["status"]], len(o["missing_for_certification"]), o["task_id"]))[:10]
    for i, o in enumerate(closest, 1):
        lines.append(f"{i}. `{o['task_id']}` ({o['status']}, gold gates {o['gold_gates']['passed']}/7): " + "; ".join(o["missing_for_certification"]))
    lines += ["", "## Worked example: `format-code-task-000682` original grader accepts a broken mutant", ""]
    lines += worked_example()
    (HERE / "scorecard.md").write_text("\n".join(lines) + "\n")


def worked_example() -> list[str]:
    task = "format-code-task-000682"
    diff = STING_DIFFS / f"{task}-call_drop-L316.diff"
    sting = next(t for t in load_json(STING_SUMMARY)["tasks"] if t["task_id"] == task)
    cell = next(c for c in sting["original"] if c["cell"] == "original-m0-call_drop-L316")
    rep = next((c for c in sting.get("repaired") or [] if c["cell"].endswith("call_drop-L316")), None)
    killer = load_json(KILLER_SUMMARY)["tasks"]["000682"]
    r3 = next(t for t in load_json(R3_ROWS)["tasks"] if t["task_id"] == task)
    r3cell = next(c for c in r3["cells"] if c["cell"].endswith("call_drop-L316"))
    a4b = load_json(A4B_TASKS / task / "a4b-r1.json")
    lines = [f"Mutant `call_drop` at `c7n/resources/sns.py:316` (STING operator: drop the call the reference patch added). Receipt: `{STING_SUMMARY}` cell `{cell['cell']}`; diff file `{diff}`.", "",
             "```diff"] + diff.read_text().rstrip("\n").splitlines()[:38] + ["```", "",
             f"- original grader reward: **{cell['reward']}** (STING, `{cell['evidence_sha256'][:12]}`); A2 census re-execution also 1.0 (`{CENSUS_ROWS}`).",
             f"- task-quality repaired snapshot reward: {rep['reward'] if rep else 'null'} (STING repaired cell); R3 repaired cell: {r3cell['reward']} (`{R3_ROWS}`).",
             f"- mutation-guided repair + full-hardening reward: {killer['r1'].get('r1-call_drop-L316')} (`{KILLER_SUMMARY}`, r1 cell; reference 1.0, nop 0.0).",
             f"- A4B Gold v1.1 shipped package: A2 survivors killed {sum(1 for s in a4b['a2_survivors'] if s['killed'])}/{len(a4b['a2_survivors'])}, but gate 4 (exit-0 `.pth`) = {a4b['gates']['exit0_passed']} (`{A4B_TASKS / task / 'a4b-r1.json'}`)."]
    return lines


if __name__ == "__main__":
    main()
