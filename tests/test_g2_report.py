"""G2 report builder: consumer-visible behaviour on synthetic fixtures."""

import json
import sys
from pathlib import Path

import pytest

_BATCH_DIR = Path(__file__).resolve().parents[1] / "research" / "experiments" / "har120-data-batch"
if str(_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BATCH_DIR))

import build_report as b  # noqa: E402


def _job(
    root: Path,
    name: str,
    *,
    verdict="counted_fail",
    raw=0.0,
    reasons=(),
    exc="LoopBreakStop",
    stop="unknown",
    tf_stop="loop_break",
    in_tok=100,
    out_tok=10,
    requests=5,
    job_id="jid-1",
    loop_break=None,
    trial="t1",
):
    jd = root / "runs" / name
    tdir = jd / f"{name}__{trial}"
    tdir.mkdir(parents=True)
    (jd / "result.json").write_text(json.dumps({"id": job_id}))
    (jd / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-01T08:01:00Z",
                "finished_at": "2026-10-01T08:05:00Z",
                "provider_usage": {
                    "attempt_id": "a1",
                    "totals": {
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "requests": requests,
                        "total_tokens": in_tok + out_tok,
                    },
                },
            }
        )
    )
    (jd / "processed").mkdir(exist_ok=True)
    (jd / "processed" / "job.json").write_text(
        json.dumps(
            {
                "results_home": str(root / "rh" / name),
                "ledger": {
                    "totals": {
                        "used": {
                            "input_tokens": in_tok,
                            "output_tokens": out_tok,
                            "requests": requests,
                        }
                    }
                },
            }
        )
    )
    (jd / "processed" / f"trial-{name}__{trial}.json").write_text(
        json.dumps(
            {
                "trial_name": f"{name}__{trial}",
                "task_name": "mimo-v2.6-rl/format-code-task-000001",
                "task_package_digest": "sha256:abc",
                "agent_steps": requests,
                "stop_reason": stop,
                "diagnosis": {"exception_class": exc},
                "token_flow": {
                    "stop": {"stop_reason": tf_stop},
                    "loop_onset": {"call_index": 3},
                    "last_useful_edit": {"call_index": 1},
                },
                "tokens_proxy": {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "source": "proxy_settled_ledger",
                },
                "tokens_native": {"input_tokens": in_tok + 7, "output_tokens": out_tok},
                "handshake": {"confirmed": False},
                "counts": {
                    "schema": "evallab.counts/v1",
                    "verdict": verdict,
                    "raw_reward": raw,
                    "reasons": list(reasons),
                    "task_status": {"status": "usable"},
                },
            }
        )
    )
    (tdir / "result.json").write_text(
        json.dumps(
            {
                "verifier_result": {"rewards": {"reward": raw}},
                "agent_result": {"metadata": {"loop_break": loop_break or {}}},
                "exception_info": {},
            }
        )
    )
    return jd


def test_scan_bounds_records_lines_and_bytes(tmp_path):
    p = tmp_path / "calls.jsonl"
    recs = [
        {"route_token": "aaa", "seq": 1},
        {"route_token": "bbb", "seq": 2},
        {"route_token": "aaa", "seq": 3},
    ]
    with open(p, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    got = b.scan_bounds(p)
    assert got["lines"] == 3
    assert got["tokens"]["aaa"]["first_line"] == 1
    assert got["tokens"]["aaa"]["last_line"] == 3
    assert got["tokens"]["bbb"]["n"] == 1
    assert got["tokens"]["aaa"]["last_byte"] == got["size_bytes"]


def test_refused_outcome_vocabulary():
    assert b.counted_outcome({"finished": False, "status": "refused"}).startswith("not_run:")
    assert b.counted_outcome({"finished": False, "status": "absent"}) == "pending"


def _spec(specdir: Path, name: str):
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / f"{name}.json").write_text(
        json.dumps(
            {
                "harness_tree_sha256": "sha256:lf2",
                "max_requests": 120,
                "max_input_tokens": 2500000,
                "max_output_tokens": 131072,
                "model": "m",
                "environment": "daytona",
                "timeout_seconds": 3600,
                "concurrency": 1,
            }
        )
    )


def test_stop_prefers_exception_over_unknown_field():
    row = {
        "finished": True,
        "stop_reason_raw": "unknown",
        "exception_class": "LoopBreakStop",
        "token_flow_stop": "loop_break",
        "reasons": [],
        "exception_message": None,
    }
    assert b.canonical_stop(row) == "LoopBreakStop"


def test_stop_ceiling_request_and_infra_labels():
    assert (
        b.canonical_stop(
            {
                "finished": True,
                "stop_reason_raw": "ceiling:requests",
                "exception_class": "TrialBudgetExhaustedError",
                "token_flow_stop": None,
                "reasons": [],
                "exception_message": None,
            }
        )
        == "TrialBudgetExhausted (request ceiling)"
    )
    assert (
        b.canonical_stop(
            {
                "finished": True,
                "stop_reason_raw": "unknown",
                "exception_class": "ServiceUnavailableError",
                "token_flow_stop": None,
                "reasons": ["infra"],
                "exception_message": "503",
            }
        )
        == "infra (upstream 503)"
    )
    assert (
        b.counted_outcome({"finished": True, "verdict": "excluded", "reasons": ["infra"]})
        == "infra"
    )
    assert (
        b.counted_outcome({"finished": True, "verdict": "excluded", "reasons": ["copied_fix"]})
        == "excluded:copied_fix"
    )


def test_stale_processed_detection():
    assert b.needs_process({"processed": False}) == "processed/ missing"
    assert b.needs_process({"processed": True, "n_processed_trials": 0}) is not None
    ok = {
        "processed": True,
        "n_processed_trials": 1,
        "counts_schema": "evallab.counts/v1",
        "tokens_proxy": {"source": "proxy_settled_ledger"},
    }
    assert b.needs_process(ok) is None
    bad = dict(ok, tokens_proxy={"source": "analysis.tokens_result"})
    assert "proxy-settled" in b.needs_process(bad)


def test_link_target_prefers_dir_with_bytes(tmp_path):
    g2 = tmp_path / "g2"
    g2c = tmp_path / "g2c"
    g2.mkdir()
    g2c.mkdir()
    (g2 / "calls.jsonl").write_text("{}\n")
    (g2c / "calls.jsonl").write_text("")
    caps = {"g2": g2, "g2c": g2c}
    assert b.link_target({"job": "har120-000001-a1"}, caps)[0] == "g2"
    # r2 traffic still lands in g2 while the g2c file is empty.
    assert b.link_target({"job": "har120-000001-a1-r2"}, caps)[0] == "g2"
    (g2c / "calls.jsonl").write_text("{}\n")
    assert b.link_target({"job": "har120-000001-a1-r2"}, caps)[0] == "g2c"


def test_full_build_and_rerun_idempotent(tmp_path):
    live = tmp_path / "live"
    _job(
        live,
        "har120-000001-a1",
        verdict="counted_pass",
        raw=1.0,
        exc="LoopBreakStop",
        in_tok=485421,
        out_tok=4929,
        requests=51,
        loop_break={
            "fired": True,
            "nudge_call": 18,
            "stop_call": 52,
            "detector": "identical_message_run",
        },
    )
    _job(
        live,
        "har120-000002-a1",
        verdict="excluded",
        raw=None,
        reasons=["infra"],
        exc="ServiceUnavailableError",
        stop="unknown",
        tf_stop=None,
        in_tok=0,
        out_tok=0,
        requests=27,
        job_id="jid-2",
    )
    _spec(live / "research" / "experiments" / "har120-data-batch" / "specs", "har120-000001-a1")
    _spec(live / "research" / "experiments" / "har120-data-batch" / "specs", "har120-000002-a1")
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "calls.jsonl").write_text("")
    out = tmp_path / "out"
    rc = b.main(
        [
            "--live-root",
            str(live),
            "--spec-dir",
            str(live / "research" / "experiments" / "har120-data-batch" / "specs"),
            "--capture",
            f"g2={cap}",
            "--out",
            str(out),
            "--no-maintenance",
        ]
    )
    assert rc == 0
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    by_job = {r["job"]: r for r in rows}
    assert by_job["har120-000001-a1"]["outcome"] == "counted_pass"
    assert by_job["har120-000001-a1"]["stop"] == "LoopBreakStop"
    assert by_job["har120-000001-a1"]["stop_category"] == "our_limit"
    assert by_job["har120-000001-a1"]["ledger_tokens"]["input"] == 485421
    assert by_job["har120-000002-a1"]["outcome"] == "infra"
    assert by_job["har120-000002-a1"]["stop_category"] == "error"
    passes = (out / "counted_pass.jsonl").read_text().strip().splitlines()
    assert len(passes) == 1
    p = json.loads(passes[0])
    assert (p["task"], p["attempt"]) == ("000001", 1)
    assert p["trial_path"].endswith("har120-000001-a1__t1")
    md = (out / "RESULTS.md").read_text()
    assert "18 -> 52" in md and "counted_pass" in md
    assert "stop category" in md
    assert "Limit-hit share over all finished trials: 1/2 (50.0%)" in md
    assert "setup-limited: `true`" in md
    before = (out / "results.jsonl").read_bytes()
    assert (
        b.main(
            [
                "--live-root",
                str(live),
                "--spec-dir",
                str(live / "research" / "experiments" / "har120-data-batch" / "specs"),
                "--capture",
                f"g2={cap}",
                "--out",
                str(out),
                "--no-maintenance",
            ]
        )
        == 0
    )
    assert (out / "results.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    ("stop", "exception", "category"),
    [
        ("ceiling:requests", "TrialBudgetExhaustedError", "our_limit"),
        ("unknown", "LoopBreakStop", "our_limit"),
        ("harness_step_limit", None, "harness_step_limit"),
        ("unknown", "AgentTimeoutError", "task_timeout"),
        ("task_complete_confirmed", None, "model_end"),
        ("unknown", None, "unknown"),
    ],
)
def test_results_category_uses_trial_evidence_without_changing_counts(
    tmp_path, stop, exception, category
):
    job = _job(
        tmp_path,
        "har120-000001-a1",
        stop=stop,
        exc=exception,
        tf_stop=None,
        verdict="counted_pass",
        raw=1.0,
    )

    row = b.read_job(job, tmp_path / "derived")

    assert row["stop_category"] == category
    assert row["verdict"] == "counted_pass"
    assert row["raw_reward"] == 1.0


@pytest.mark.parametrize(("trials", "flagged"), [(20, False), (19, True)])
def test_results_limit_hit_share_excludes_unrun_slots_at_strict_boundary(tmp_path, trials, flagged):
    live = tmp_path / "live"
    specs = live / "research" / "experiments" / "har120-data-batch" / "specs"
    for index in range(trials):
        name = f"har120-{index:06}-a1"
        _job(
            live,
            name,
            exc="LoopBreakStop" if index == 0 else None,
            stop="unknown" if index == 0 else "harness_step_limit",
            tf_stop=None,
        )
        _spec(specs, name)
    # This cell was never run; it must not dilute the trial denominator.
    _spec(specs, "har120-999999-a1")
    cap = tmp_path / "capture"
    cap.mkdir()
    (cap / "calls.jsonl").write_text("")
    out = tmp_path / "out"

    assert (
        b.main(
            [
                "--live-root",
                str(live),
                "--spec-dir",
                str(specs),
                "--capture",
                f"g2={cap}",
                "--out",
                str(out),
                "--no-maintenance",
            ]
        )
        == 0
    )
    markdown = (out / "RESULTS.md").read_text()
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]

    assert len(rows) == trials + 1
    assert sum(row["outcome"] == "counted_fail" for row in rows) == trials
    assert f"Limit-hit share over all finished trials: 1/{trials}" in markdown
    assert f"setup-limited: `{str(flagged).lower()}`" in markdown
    assert ("Flagged: over 5%" in markdown) is flagged
