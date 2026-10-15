"""V5 verify fill: compute manifest `verify` + verify-detail.csv from
v5 results.csv, v5 probe summary, v4 verify-detail, guard results,
and the overrides file.

Usage: uv run --extra laminar python3 research/experiments/mimo-clean-v5-census/fill_verify.py
Re-apply after every `mimo-census report` (report writes coarse grades;
overrides survive automatically, this fill restores coverage/carry labels).
"""

import csv
import json
from collections import Counter
from pathlib import Path

WT = Path(__file__).resolve().parents[3]
RCPT = WT / "research/experiments/mimo-clean-v5-census"
RESROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-11")
V4D = WT / "research/experiments/mimo-clean-v4-census/verify-detail.csv"

FOLD_REAL = ["format-code-task-000919", "format-code-task-000435", "format-code-task-000170"]
PASS3 = FOLD_REAL  # probe-0 + (1,0) + ladder-clean on @5 digests
NODE_FOLD_AT_RISK = {
    f"format-code-task-{t}"
    for t in [
        "000271",
        "000776",
        "000867",
        "001550",
        "001576",
        "001672",
        "001901",
        "000794",
        "000876",
        "000934",
        "001437",
        "001808",
        "001890",
        "001931",
        "001932",
        "001933",
        "001934",
    ]
}
NODE_BUILD_LEAK = {"format-code-task-000632", "format-code-task-001763"}


def read_rows(path):
    with Path(path).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_jsonl(path):
    out = {}
    if Path(path).is_file():
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["task_id"]] = r
    return out


def main():
    man = read_rows(WT / "research/experiments/mimo-clean-v5/manifest.csv")
    man_by_id = {r["task_id"]: r for r in man}
    reslists = {}
    for r in read_rows(RCPT / "results.csv"):
        reslists.setdefault(r["task_id"], []).append(r)
    res = {}
    for t, rows in reslists.items():
        m = man_by_id.get(t, {})
        match = [r for r in rows if r.get("final_digest") == m.get("final_digest")]
        res[t] = match[0] if match else rows[-1]
    v4 = {r["task_id"]: r for r in read_rows(V4D)}
    ovr = {r["task_id"]: r for r in read_rows(RCPT / "verify-overrides.csv")}
    refresh_path = RCPT / "digest-refresh.csv"
    refreshed = {r["task_id"]: r for r in read_rows(refresh_path)} if refresh_path.is_file() else {}
    probe = load_jsonl(RESROOT / "mimo-clean-v5-probe" / "rows.jsonl")
    guard = load_jsonl(RESROOT / "mimo-clean-v5-guard" / "results.jsonl")
    detail = []
    for row in man:
        t = row["task_id"]
        has_fix = row["reference_fix"] != "none"
        r = res.get(t)
        fresh = r is not None and r.get("final_digest") == row["final_digest"]
        v, ev = None, ""
        if row["status"] != "built":
            v, ev = "unverified", f"skipped:{row.get('reason', '')[:80]}"
        elif t in ovr and fresh and ovr[t]["final_digest"] in ("", row["final_digest"]):
            v, ev = ovr[t]["verify"], "override:" + ovr[t]["reason"][:160]
        elif fresh:
            nop, ora = r.get("nop", ""), r.get("oracle", "")
            lad, crack = r.get("ladder_verdict", ""), r.get("ladder_cracking_attacks", "")
            if crack:
                v, ev = "fail:grader-hole", f"ladder cracked: {crack}"
            elif nop == "setup-fail" or ora == "setup-fail":
                v, ev = "env-broken", f"fresh setup-fail nop={nop} oracle={ora}"
            elif lad == "cracked":
                v, ev = "fail:grader-hole", "ladder cracked"
            elif lad in ("unscored", "partial"):
                v, ev = f"unverified:ladder-{lad}", "ladder did not score"
            elif has_fix and ora not in ("1", "1-noexec", "n/a", "") and ora.startswith("fail:"):
                v, ev = "unverified:fail-untriaged", f"fresh {ora} without triage"
            elif has_fix and ora in ("", "missing"):
                v, ev = "unverified:oracle-missing", "no fresh oracle cell"
            elif nop in ("", "missing"):
                v, ev = "unverified:nop-missing", "no fresh nop cell"
            elif t in PASS3 and (nop, ora, lad) == ("0", "1", "clean"):
                v, ev = "pass", "probe-0 + (1,0) + ladder-1.4.0-clean on final digest"
            elif has_fix and t not in probe:
                v, ev = "unverified:probe-pending", f"({nop},{ora})" + (f"+{lad}" if lad else "")
            elif nop.endswith("noexec") or ora.endswith("noexec"):
                v, ev = "unverified:noexec", f"({nop},{ora})" + (" + probe-0" if t in probe else "")
            elif not lad or lad in ("missing", "unscored", "partial"):
                tag = lad or "missing"
                v = f"unverified:ladder-{tag}" if tag != "missing" else "unverified:ladder-missing"
                ev = f"({nop},{ora}) ladder={tag}" + (" + probe-0" if t in probe else "")
            elif (nop, ora, lad) == ("0", "1", "clean") or (
                not has_fix and nop == "0" and lad == "clean"
            ):
                v, ev = (
                    "pass",
                    f"({nop},{ora}) + ladder clean" + (" + probe-0" if t in probe else ""),
                )
            else:
                v, ev = "unverified", f"({nop},{ora},{lad})"
        else:
            v4row = v4.get(t, {})
            vv = v4row.get("verify", "unverified")
            if vv == "pass":
                v, ev = (
                    "unverified:ladder-missing",
                    "v4 pass components carried C1+C2; no v5 ladder",
                )
            elif vv == "fail:open-leak":
                v, ev = "unverified:v4-leak-ungraded", f"v4 {vv}; never carried; no v5 cells"
            elif vv.startswith("fail:"):
                v, ev = "unverified:v4-fail-ungraded", f"v4 {vv}; never carried"
            else:
                v, ev = vv, "v4 carry C1+C2: " + v4row.get("evidence", "")[:160]
            g = guard.get(t, {})
            if (
                not v4row.get("fresh_nop")
                and not v4row.get("fresh_oracle")
                and not v4row.get("fresh_ladder")
                and g.get("setup_rc") not in (0, None, "")
            ):
                v, ev = (
                    "env-broken",
                    (
                        "guard setup-only rc="
                        f"{g.get('setup_rc')} ready={g.get('ready')}; no cells either version"
                    ),
                )
        # node-build marks (fill-side; need no aggregates)
        if t == "format-code-task-000928":
            v, ev = (
                "fail:open-leak:node-build-pnpm",
                "excluded-from-training; pnpm leg blocks node handling",
            )
        if t in NODE_FOLD_AT_RISK:
            g = guard.get(t, {})
            v, ev = (
                "unverified:node-build-fold",
                (f"@5 fold; guard setup rc={g.get('setup_rc', '?')} ready={g.get('ready', '?')}"),
            )
        if t in NODE_BUILD_LEAK:
            v, ev = "unverified:node-build-leak", "kept @4; @5 setup fails closed (guard)"
        # #909 changed package bytes without rerunning the old census.
        # Neither task-id carries nor re-pinned overrides certify new bytes.
        refresh = refreshed.get(t)
        if refresh and row["final_digest"] == refresh["new_digest"]:
            if not fresh:
                v, ev = (
                    "unverified:package-changed",
                    f"no census on {refresh['new_digest']}; historical "
                    f"{refresh['old_verify']} belongs to {refresh['old_digest']}",
                )
            elif (
                has_fix
                and v == "pass"
                and ovr.get(t, {}).get("final_digest") != row["final_digest"]
            ):
                v, ev = (
                    "unverified:probe-pending",
                    "fresh controls/ladder do not carry the historical fix-content probe",
                )
            if refresh["old_verify"].startswith("fail:") or refresh["old_verify"] == "env-broken":
                hold = (
                    "excluded-from-training pending revalidation: historical "
                    f"{refresh['old_verify']} on {refresh['old_digest']}"
                )
                if hold not in row["reason"]:
                    row["reason"] += "; " + hold
        detail.append(
            {
                "task_id": t,
                "verify": v,
                "evidence": ev,
                "has_fix": str(has_fix),
                "chain": row["chain"],
            }
        )
        row["verify"] = v
    with open(RCPT / "verify-detail.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["task_id", "verify", "evidence", "has_fix", "chain"])
        w.writeheader()
        w.writerows(detail)
    with open(WT / "research/experiments/mimo-clean-v5/manifest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(man[0].keys()))
        w.writeheader()
        w.writerows(man)
    print(Counter(r["verify"] for r in detail).most_common(40))


if __name__ == "__main__":
    main()
