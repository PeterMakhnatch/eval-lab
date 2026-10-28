"""One real scoped quota verification; resume fixed pending IDs only if admitted.

No sealed report/receipt is overwritten. Unknown telemetry means zero model
calls and one small receipt, not another copied metrics campaign. No labels
are read by this command. Re-running the same attempt namespace is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import campaign
import common
import run_omp

OUT = Path(__file__).resolve().parent
NAME = "scoped-forward-20261028"
PUBLIC = OUT / NAME
PRIVATE = common.STATE / "tt7-scoped-forward-20261028"
RECEIPT = PUBLIC / "attempt.json"
ROUTES = ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def byte_count(value: Any) -> int:
    return len(value if isinstance(value, bytes) else str(value or "").encode())


def pending_counts() -> dict[str, int]:
    allowed = campaign.eligible_pending()
    rows = run_omp.read_jsonl(common.RESULTS)
    counts = {}
    for route in ROUTES:
        done = {row["run_id"] for row in rows if row.get("tool") == "omp"
                and row.get("detector") == f"tt7_form:{route}"}
        counts[route] = len(allowed[route] - done)
    return counts


def verify_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Observe, never replace, the corrected helper's bounded real CLI calls."""
    original = run_omp.subprocess

    def observed(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        started = time.perf_counter()
        event: dict[str, Any] = {"argv": argv, "at": common.now()}
        try:
            proc = original.run(argv, **kwargs)
        except subprocess.SubprocessError as exc:
            event.update(returncode=getattr(exc, "returncode", None),
                         failure_type=type(exc).__name__,
                         stdout_bytes=byte_count(getattr(exc, "stdout", None)),
                         stderr_bytes=byte_count(getattr(exc, "stderr", None)))
            raise
        else:
            event.update(returncode=proc.returncode, stdout_bytes=byte_count(proc.stdout),
                         stderr_bytes=byte_count(proc.stderr))
            if "invalidate" not in argv:
                raw = json.loads(proc.stdout)
                reports = [row for row in raw.get("reports", []) if row.get("provider") == "anthropic"]
                event.update(usage_reports=len(reports),
                             accounts_without_usage=len(raw.get("accountsWithoutUsage", [])),
                             disabled_credentials=len(raw.get("disabledCredentials", [])),
                             fetched_at_ms=[row.get("fetchedAt") for row in reports])
            else:
                event.update(invalidation_provider_flag="--provider" in argv,
                             stdout_mentions_anthropic="anthropic" in proc.stdout.lower())
            return proc
        finally:
            event["elapsed_s"] = round(time.perf_counter() - started, 6)
            events.append(event)

    run_omp.subprocess = SimpleNamespace(run=observed)
    try:
        return run_omp.refresh_usage("anthropic")
    finally:
        run_omp.subprocess = original


def privacy_check() -> dict[str, Any]:
    old = json.loads((common.STATE / "tt7-continuation-20261028/privacy-audit.json").read_text())
    expected = {row["opaque_id"]: row for row in old["rows"]}
    allowed = campaign.eligible_pending()
    eligible_names = {campaign.opaque(run_id) for route in ROUTES for run_id in allowed[route]}
    flags = []
    runs = size = steps = 0
    for item in common.load_sample():
        run = common.load_run(item)
        name = campaign.opaque(item["run_id"])
        if digest(run["text"].encode()) != expected[name]["text_sha256"]:
            raise ValueError("frozen blind input changed; no transmission admitted")
        hits = common.run_secret_hits(run)
        if hits:
            flags.append(name)
        runs += 1
        size += len(run["text"].encode())
        steps += len(run["steps"])
    if runs != 200 or len(flags) != 4 or set(flags) & eligible_names:
        raise ValueError("uniform canonical privacy policy changed; no transmission admitted")
    return {"input_runs": runs, "input_text_bytes": size, "input_steps": steps,
            "same_policy_flags": len(flags), "eligible_policy_flags": 0,
            "gate_sha256": common.SECRET_GATE_SHA256, "all_input_hashes_match_prior_audit": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="Run only predeclared pending Claude cells if all guards pass")
    args = parser.parse_args()
    PUBLIC.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    if RECEIPT.exists() or (PRIVATE / "started.json").exists():
        raise ValueError("scoped attempt already exists; no repeated quota probe or inference")
    initial = pending_counts()
    if set(initial.values()) != {125}:
        raise ValueError("authorized original pending partition changed")
    run_omp.OUT = PUBLIC
    run_omp.PRIVATE = PRIVATE / "omp"
    run_omp.PRIVATE.mkdir(parents=True, exist_ok=True)
    run_omp.SCAN = str(PUBLIC / "omp-receipts.json")
    started = common.now()
    clock = time.perf_counter()
    (PRIVATE / "started.json").write_text(json.dumps({"at": started, "labels_read": False}) + "\n")
    events: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema": "tt7.scoped_forward_attempt/v1", "started_at": started,
        "sample_sha256": common.SAMPLE_SHA256, "baseline_study_commit": "25f63a3c28a3e02474eeb3014715158ba2b20493",
        "sealed_continuation_commit": "727486eb54ab7892e3c50c59900b371840329fab",
        "baseline_score_sha256": digest((OUT / "score.json").read_bytes()),
        "source_sha256": {name: digest((OUT / name).read_bytes()) for name in ("run_omp.py", "scoped_attempt.py")},
        "admission_fraction": 0.65, "hard_stop_fraction": 0.70, "quota_ttl_s": 120,
        "concurrency": 2, "initial_unattempted_per_route": initial,
        "quota_events": events, "new_model_calls": 0, "new_terminal_records": 0,
        "labels_read": False, "old_terminal_retries": 0, "old_results_rewritten": False,
        "account_config_or_reset_changes": False, "paid_fallback": False,
    }
    stage = "quota_verification"
    try:
        usage = verify_usage(events)
        result["verified_usage"] = usage
        if usage["maximum_fraction"] >= 0.65:
            result.update(status="stopped_threshold", reason="applicable fresh quota window at or above65%; no model call")
        elif not args.resume:
            result.update(status="verified_only", reason="fresh headroom proven; resume flag not supplied")
        else:
            stage = "privacy_preparation"
            result["privacy"] = privacy_check()
            previous = sys.argv
            try:
                sys.argv = [str(OUT / "run_omp.py"), "run", "--routes", *ROUTES,
                            "--max-runs", "200", "--stop-threshold", "0.65", "--concurrency", "2", "--batch-size", "2"]
                stage = "reading"
                run_omp.main()
            finally:
                sys.argv = previous
            new = [row for row in run_omp.read_jsonl(common.RESULTS) if row.get("scan") == run_omp.SCAN]
            result.update(status="readings_closed", new_terminal_records=len(new),
                          new_model_calls=sum(row["value"].get("calls", 0) for row in new),
                          remaining_unattempted_per_route=pending_counts())
            if new:
                payload = campaign.filtered_results()
                campaign.write_once(PRIVATE / "results-snapshot.jsonl", payload)
                result["results_snapshot"] = {"path": str(PRIVATE / "results-snapshot.jsonl"),
                                               "sha256": digest(payload), "bytes": len(payload)}
    except (run_omp.RouteError, OSError, subprocess.SubprocessError, ValueError) as exc:
        result.update(status="stopped_unknown", failure_stage=stage,
                      reason=str(exc) if stage == "quota_verification" else
                      f"{type(exc).__name__}: bounded input/reading admission unavailable; no inferred headroom")
    # Count retained attempts even if a later transport/closure operation failed.
    new = [row for row in run_omp.read_jsonl(common.RESULTS) if row.get("scan") == run_omp.SCAN]
    identities = list(run_omp.PRIVATE.glob("*/*/*-identity.json"))
    result.update(new_terminal_records=len(new),
                  new_model_calls=max(len(identities), sum(row["value"].get("calls", 0) for row in new)),
                  remaining_unattempted_per_route=pending_counts())
    result.update(finished_at=common.now(), elapsed_s=round(time.perf_counter() - clock, 6))
    if not result["new_model_calls"]:
        common.log_spend({"tool": "omp", "route": "anthropic/claude-sonnet-5-5 + anthropic/claude-opus-5-5",
                          "purpose": "TT7 corrected scoped quota verification; zero inference", "batch_id": "tt7-scoped-forward-20261028",
                          "calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0,
                          "basis": "bounded real quota reads/scoped cache invalidation only; original whole-study Luna billing remains unknown",
                          "scan": str(RECEIPT)})
    with RECEIPT.open("x") as handle:
        handle.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "new_model_calls": result["new_model_calls"],
                      "new_terminal_records": result["new_terminal_records"], "quota_cli_calls": len(events),
                      "elapsed_s": result["elapsed_s"], "receipt": str(RECEIPT)}), flush=True)


if __name__ == "__main__":
    main()
