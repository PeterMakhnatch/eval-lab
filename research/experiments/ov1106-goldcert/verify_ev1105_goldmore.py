"""Verify retained ev1105-goldmore native measurements offline using Python standard library only.

Run with:
  python3 -I -S research/experiments/ov1106-goldcert/verify_ev1105_goldmore.py
"""

import argparse
import hashlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OLD_LANE = Path("/private/tmp/ov1106/goldcert")
TASKS = ("000081", "000682", "002139", "003007")
FILES = (
    "agent-script.sh",
    "result.json",
    "trial/qa-execution.json",
    "trial/egress-lock.json",
    "trial/agent/oracle.txt",
    "trial/verifier/test_output.log",
)


def need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_checked(path: Path, expected: str | None = None) -> bytes:
    need(path.is_file() and not path.is_symlink(), f"missing/nonregular file: {path}")
    data = path.read_bytes()
    if expected is not None:
        need(digest(data) == expected, f"hash mismatch: {path}")
    return data


def load_checked(path: Path, expected: str | None = None):
    return json.loads(read_checked(path, expected))


def wilson95(k: int, n: int):
    if n <= 0:
        return None
    z = 1.959964
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + (z * z) / (4.0 * n * n))) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def counts(rows, wanted: float):
    measured = [r for r in rows if r.get("reward") is not None]
    n = len(measured)
    k = sum(float(r["reward"]) == wanted for r in measured)
    return {
        "planned": len(rows),
        "measured": n,
        "accepted": sum(float(r["reward"]) == 1.0 for r in measured),
        "rejected": sum(float(r["reward"]) == 0.0 for r in measured),
        "unmeasured": len(rows) - n,
        "successes": k,
        "wilson95": wilson95(k, n),
    }


def compare_counts(actual, recorded, label: str) -> None:
    need(
        {k: v for k, v in actual.items() if k != "wilson95"}
        == {k: v for k, v in recorded.items() if k != "wilson95"},
        f"count mismatch for {label}",
    )
    if actual["wilson95"] is None:
        need(recorded["wilson95"] is None, f"unexpected interval for {label}")
    else:
        need(
            len(recorded["wilson95"]) == 2
            and all(
                abs(x - y) < 1e-6
                for x, y in zip(actual["wilson95"], recorded["wilson95"], strict=True)
            ),
            f"interval mismatch for {label}",
        )


def verify_native_cell(row, job, checked_hashes: dict[str, str], *, expect_cloud=None) -> float:
    cell = Path(row["receipt"]).parent
    need(Path(row["receipt"]).name == "result.json", f"bad receipt filename: {row['receipt']}")
    hashes = row["hashes"]
    need(set(FILES) <= set(hashes), f"missing file hashes in {row['name']}")
    data = {}
    for name, sha in hashes.items():
        part = Path(name)
        need(not part.is_absolute() and ".." not in part.parts, f"unsafe relative path: {name}")
        target = cell / part
        data[name] = read_checked(target, sha)
        checked_hashes[str(target)] = sha
    need(
        digest(data["agent-script.sh"]) == job["script_sha256"] == row["script_sha256"],
        f"script sha mismatch: {row['name']}",
    )
    if "script" in job:
        need(
            data["agent-script.sh"] == job["script"].encode("utf-8"),
            f"script bytes mismatch: {row['name']}",
        )
    raw, native, egress = (json.loads(data[name]) for name in FILES[1:4])
    reward = raw.get("reward")
    need(
        all(type(x) in (int, float) and x in (0, 1) for x in (reward, row["reward"], native["reward"]))
        and float(reward) == float(row["reward"]) == float(native["reward"]),
        f"reward mismatch: {row['name']}",
    )
    need(
        raw.get("status") == row["status"] == "executed"
        and raw.get("cache_hit") is False
        and raw.get("reason") is None
        and native.get("exception") is None
        and native.get("environment") == "modal"
        and native.get("mode") == "shortcut"
        and native.get("package_digest") == job["package_digest"]
        and Path(native["job_path"]).resolve() == (cell / "trial").resolve(),
        f"native execution identity mismatch: {row['name']}",
    )
    if expect_cloud is not None:
        cloud = native.get("cloud_spend") or {}
        need(
            all(cloud.get(k) == v for k, v in expect_cloud.items()),
            f"cloud_spend identity mismatch: {row['name']}",
        )
    log = data["trial/agent/oracle.txt"].decode("utf-8", errors="replace")
    output = data["trial/verifier/test_output.log"].decode("utf-8", errors="replace")
    need(
        raw.get("agent_log") == log
        and raw.get("test_output_tail") == output[-600:]
        and raw.get("evidence_sha256") == digest((log + output).encode("utf-8")),
        f"log/evidence digest mismatch: {row['name']}",
    )
    if job.get("marker"):
        marker = job["marker"]
        count = job["script"].count(marker)
        need(
            count > 0
            and log.count(marker) >= count
            and native.get("agent_stdout", "").count(marker) >= count,
            f"marker missing in {row['name']}",
        )
    need(
        egress.get("schema_version") == 2
        and egress.get("requested") is True
        and egress.get("error") is None
        and any(
            r.get("source") == "creation"
            and r.get("network_mode") in ("public", "allowlist", "no-network")
            and isinstance(r.get("sandbox_id"), str)
            and r["sandbox_id"].startswith("sb-")
            for r in egress.get("applications", [])
        ),
        f"missing sandbox creation receipt in {row['name']}",
    )
    return float(reward)


def verify(repo: Path, aggregate_path: Path):
    checked_hashes: dict[str, str] = {}
    base = repo / "research/experiments/ov1106-goldcert"
    agg_bytes = read_checked(aggregate_path)
    agg = json.loads(agg_bytes)
    freeze_path = base / "ev1105-goldmore-freeze.json"
    freeze = load_checked(freeze_path, agg["freeze_sha256"])
    checked_hashes[str(freeze_path)] = agg["freeze_sha256"]

    need(agg["prepared"] == freeze["prepared"], "prepared binding mismatch")
    prepared = load_checked(Path(agg["prepared"]["path"]), agg["prepared"]["sha256"])
    checked_hashes[agg["prepared"]["path"]] = agg["prepared"]["sha256"]

    execution = load_checked(Path(agg["execution"]["path"]), agg["execution"]["sha256"])
    checked_hashes[agg["execution"]["path"]] = agg["execution"]["sha256"]

    need(
        prepared["policy"] == agg["policy"] == execution["policy"] == freeze["policy"]
        and execution["publication_commit"] == agg["publication_commit"]
        and execution["freeze_sha256"] == agg["freeze_sha256"],
        "policy/publication binding mismatch",
    )

    wt_prefix = "/Users/petermakhnatch/Developer/eval-lab/.worktrees/ev1105-goldmore/"
    for item in prepared["inputs"] + prepared["source_files"]:
        target_path = Path(item["path"])
        if not target_path.is_file() and item["path"].startswith(wt_prefix):
            target_path = repo / item["path"][len(wt_prefix) :]
        read_checked(target_path, item["sha256"])
        checked_hashes[item["path"]] = item["sha256"]

    # Verify all 32 reused native rows
    reused_rows = agg["reused_rows"]
    need(len(reused_rows) == 32, "expected 32 reused native rows")
    for row in reused_rows:
        verify_native_cell(
            row,
            {
                "script_sha256": row["script_sha256"],
                "package_digest": row["package_digest"],
            },
            checked_hashes,
        )

    # Verify all 28 newly executed native rows
    policy = agg["policy"]
    expected_cloud = {
        "approval": policy["approval"],
        "billing_tag": policy["billing_tag"],
        "cap_usd": policy["cap_usd"],
        "modal_app_name": policy["app"],
        "spend_ledger": policy["ledger"],
        "usd_per_execution": policy["unit_usd"],
    }
    jobs_by_name = {j["name"]: j for j in prepared["jobs"]}
    exec_rows = agg["executed_rows"]
    need(
        len(jobs_by_name) == len(exec_rows) == 28
        and {r["name"] for r in exec_rows} == set(jobs_by_name),
        "executed row cohort mismatch",
    )
    for row in exec_rows:
        job = jobs_by_name[row["name"]]
        verify_native_cell(row, job, checked_hashes, expect_cloud=expected_cloud)
        meas_path = Path(row["receipt"]).with_name("measurement.json")
        saved_meas = load_checked(meas_path)
        need(
            saved_meas["reward"] == row["reward"] and saved_meas["status"] == "executed",
            f"measurement.json mismatch: {row['name']}",
        )
        fixed = row["fixed_reservations"]
        need(
            fixed["executions"] == policy["units_per_hardened_cell"]
            and Decimal(str(fixed["this_gate_reserved_usd"]))
            == Decimal(str(policy["unit_usd"])) * policy["units_per_hardened_cell"],
            f"per-cell reservation mismatch: {row['name']}",
        )

    # Verify ledger and spend.jsonl
    ledger = execution["ledger_snapshot"]
    need(ledger is not None and len(ledger["reservations"]) == 168, "expected 168 reservations")
    total_reserved = sum(
        (Decimal(str(r["reserved_usd"])) for r in ledger["reservations"]), Decimal(0)
    )
    need(
        total_reserved
        == Decimal(str(ledger["reserved_usd"]))
        == Decimal(str(agg["spend"]["reserved_usd"]))
        <= Decimal(str(policy["cap_usd"])),
        "ledger reserved_usd mismatch or cap exceeded",
    )

    spend_log_path = Path(policy["spend_log"])
    need(spend_log_path.is_file(), "spend.jsonl missing")
    spend_lines = [
        json.loads(line)
        for line in spend_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    goldmore_lines = [r for r in spend_lines if r.get("lane") == "goldmore"]
    before_rows = [r for r in goldmore_lines if r.get("phase") == "before"]
    after_rows = [r for r in goldmore_lines if r.get("phase") == "after"]
    need(
        len(before_rows) == 28
        and len(after_rows) == 28
        and all(r.get("actual_usd") is None for r in before_rows)
        and all(
            isinstance(r.get("actual_usd"), int | float) and r["actual_usd"] > 0
            for r in after_rows
        ),
        "spend.jsonl before/after contract violated for goldmore",
    )

    # Recompute per-task counts and gates
    all_rows = list(reused_rows) + list(exec_rows)
    for tid in TASKS:
        trows = [r for r in all_rows if r["task"] == tid]
        recorded = agg["tasks"][tid]
        for key, subset, wanted in [
            ("reference", [r for r in trows if r["arm"] == "reference"], 1.0),
            ("nop", [r for r in trows if r["arm"] == "nop"], 0.0),
            ("witness", [r for r in trows if r["arm"] == "witness"], 0.0),
            ("startup", [r for r in trows if r["arm"] == "startup"], 0.0),
            (
                "train_defect",
                [r for r in trows if r["arm"] == "train" and r.get("static_category") == "DEFECT"],
                0.0,
            ),
            (
                "train_possibly_valid",
                [
                    r
                    for r in trows
                    if r["arm"] == "train" and r.get("static_category") == "POSSIBLY_VALID"
                ],
                0.0,
            ),
            ("train_all", [r for r in trows if r["arm"] == "train"], 0.0),
            (
                "eval_defect",
                [r for r in trows if r["arm"] == "eval" and r.get("static_category") == "DEFECT"],
                0.0,
            ),
            (
                "eval_possibly_valid",
                [
                    r
                    for r in trows
                    if r["arm"] == "eval" and r.get("static_category") == "POSSIBLY_VALID"
                ],
                0.0,
            ),
            ("eval_all", [r for r in trows if r["arm"] == "eval"], 0.0),
            (
                "defect_all",
                [
                    r
                    for r in trows
                    if r["arm"] in ("train", "eval") and r.get("static_category") == "DEFECT"
                ],
                0.0,
            ),
            (
                "possibly_valid_all",
                [
                    r
                    for r in trows
                    if r["arm"] in ("train", "eval")
                    and r.get("static_category") == "POSSIBLY_VALID"
                ],
                0.0,
            ),
            ("alternative", [r for r in trows if r["arm"] == "alternative"], 1.0),
            (
                "alternative_held_out",
                [
                    r
                    for r in trows
                    if r["arm"] == "alternative" and r.get("split") == "held_out"
                ],
                1.0,
            ),
            (
                "historical_informed_alternative",
                [r for r in trows if r["arm"] == "historical_informed_alternative"],
                1.0,
            ),
        ]:
            compare_counts(counts(subset, wanted), recorded["counts"][key], f"{tid}.{key}")

    need(
        execution.get("remaining_owned_apps") == [],
        "owned Modal apps were not stopped cleanly",
    )

    return {
        "verified": True,
        "aggregate": str(aggregate_path),
        "aggregate_sha256": digest(agg_bytes),
        "checked_file_hashes": len(checked_hashes),
        "verified_native_cells": 60,
        "new_native_cells": 28,
        "reused_native_cells": 32,
        "reserved_usd": float(total_reserved),
        "measured_meter_usd": agg["spend"]["measured_meter_usd"],
        "tasks": {
            tid: {
                "gates": agg["tasks"][tid]["gates"],
                "certified_strict": agg["tasks"][tid]["certified_strict"],
                "certified_strict_original_pool_only": agg["tasks"][tid][
                    "certified_strict_original_pool_only"
                ],
            }
            for tid in TASKS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--aggregate",
        type=Path,
        default=HERE / "ev1105-goldmore-aggregate.json",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        report = verify(args.repo.resolve(), args.aggregate.resolve())
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as handle:
                handle.write(text)
        print(text, end="")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"verified": False, "error": str(exc)}), file=sys.stdout)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
