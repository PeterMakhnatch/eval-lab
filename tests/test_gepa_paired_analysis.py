"""Behavioral tests for paired candidate-vs-stock receipt analysis.

Receipts are built from real ``EvaluationRecord(...).to_dict()`` rows written
into temporary campaign directories, mirroring the evaluator's on-disk layout.
Every external seam is injected (tmp dirs, fixed seeds); no queue, model, or
network is touched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from evallab.gepa_optimizer.__main__ import main
from evallab.gepa_optimizer.evaluator import EvaluationRecord
from evallab.gepa_optimizer.paired_analysis import analyze_campaign

STOCK_TEXT = "stock seed instruction"
CANDIDATE_TEXT = "candidate instruction"


def _cid(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(task_id: str) -> str:
    return "sha256:" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def write_receipt(
    campaign_dir: Path,
    *,
    task_id: str,
    candidate_text: str = STOCK_TEXT,
    candidate_id: str | None = None,
    task_package_digest: str | None = None,
    status: str = "completed",
    score: float | None = None,
    agent: str = "oracle",
    model: str | None = None,
    usage: dict | None = None,
    filename_suffix: str = "",
) -> Path:
    """Write one real EvaluationRecord receipt using the producer's naming."""
    cid = candidate_id or _cid(candidate_text)
    if usage is None:
        usage = (
            {"estimated_cost_usd": 0.01}
            if status != "completed"
            else {"cost_usd": 0.01, "n_input_tokens": 100, "n_output_tokens": 50}
        )
    record = EvaluationRecord(
        candidate_id=cid,
        candidate_sha256=cid,
        candidate_path=f"research/out/candidates/{cid.split(':', 1)[1]}.txt",
        task_id=task_id,
        task_path=f"library/tasks/{task_id}",
        task_package_digest=task_package_digest or _digest(task_id),
        agent=agent,
        model=model,
        split="development",
        job_path=None if status != "completed" else f"runs/{task_id}",
        receipt_paths={},
        score=score,
        rewards={} if score is None else {"reward": score},
        usage=usage,
        status=status,
        error=None if status != "error" else "simulated infra failure",
        trial_id="trial-1",
        trial_name="trial-1",
        evaluated_at="2026-09-11T00:00:00+00:00",
    )
    evaluations = campaign_dir / "evaluations"
    evaluations.mkdir(parents=True, exist_ok=True)
    task_tag = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
    path = evaluations / f"{cid.split(':', 1)[1]}_{task_tag}{filename_suffix}.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, allow_nan=False) + "\n")
    return path


def test_pairs_by_immutable_task_identity(tmp_path: Path) -> None:
    """Pairing uses (task_id, task_package_digest); digest drift does not pair."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    write_receipt(campaign, task_id="t2", score=0.0)
    write_receipt(campaign, task_id="t4", score=1.0)
    write_receipt(campaign, task_id="t1", candidate_text=CANDIDATE_TEXT, score=0.5)
    write_receipt(campaign, task_id="t2", candidate_text=CANDIDATE_TEXT, score=0.5)
    write_receipt(campaign, task_id="t3", candidate_text=CANDIDATE_TEXT, score=1.0)
    # Same task_id under a different package digest is a different task.
    write_receipt(
        campaign,
        task_id="t2",
        candidate_text=CANDIDATE_TEXT,
        task_package_digest=_digest("t2-mutated"),
        score=1.0,
        filename_suffix="-mutated",
    )

    report = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=11)

    entry = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]
    assert entry["paired_task_count"] == 2
    assert {row["task_id"] for row in entry["paired_tasks"]} == {"t1", "t2"}
    assert entry["stock_mean_success"] == pytest.approx(0.5)
    assert entry["candidate_mean_success"] == pytest.approx(0.5)
    assert entry["paired_mean_delta"] == pytest.approx(0.0)
    only_candidate = {
        (row["task_id"], row["task_package_digest"]) for row in entry["only_candidate_tasks"]
    }
    assert ("t2", _digest("t2-mutated")) in only_candidate
    assert ("t3", _digest("t3")) in only_candidate
    assert {row["task_id"] for row in entry["only_stock_tasks"]} == {"t4"}


def test_refuses_mixed_treatment_binding(tmp_path: Path) -> None:
    """A comparison across different agents or models is refused, not reported."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0, agent="oracle")
    write_receipt(campaign, task_id="t1", candidate_text=CANDIDATE_TEXT, score=1.0, agent="nop")
    with pytest.raises(ValueError, match="not a harness comparison"):
        analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=3)

    model_campaign = tmp_path / "model-campaign"
    write_receipt(model_campaign, task_id="t1", score=1.0, model=None)
    write_receipt(
        model_campaign, task_id="t1", candidate_text=CANDIDATE_TEXT, score=1.0, model="m/x"
    )
    with pytest.raises(ValueError, match="not a harness comparison"):
        analyze_campaign(model_campaign, stock_candidate=_cid(STOCK_TEXT), seed=3)


def test_regressions_listed_with_delta(tmp_path: Path) -> None:
    """Tasks where the candidate scored below stock are listed as regressions."""
    campaign = tmp_path / "campaign"
    for task_id, stock_score, candidate_score in (
        ("t1", 1.0, 0.25),
        ("t2", 0.0, 1.0),
        ("t3", 0.5, 0.5),
    ):
        write_receipt(campaign, task_id=task_id, score=stock_score)
        write_receipt(
            campaign, task_id=task_id, candidate_text=CANDIDATE_TEXT, score=candidate_score
        )

    report = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=5)
    entry = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]
    assert [row["task_id"] for row in entry["regressions"]] == ["t1"]
    assert entry["regressions"][0]["delta"] == pytest.approx(-0.75)
    assert entry["paired_mean_delta"] == pytest.approx((0.25 - 1.0 + 1.0 - 0.0 + 0.0) / 3)


def test_pending_and_error_rows_are_missingness_never_scored(tmp_path: Path) -> None:
    """pending/error (and unscored completed) rows are counted, never scored."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    write_receipt(campaign, task_id="t2", score=0.0)
    write_receipt(campaign, task_id="t5", status="pending")
    write_receipt(campaign, task_id="t6", status="error")
    write_receipt(campaign, task_id="t1", candidate_text=CANDIDATE_TEXT, score=0.0)
    write_receipt(campaign, task_id="t2", candidate_text=CANDIDATE_TEXT, status="pending")
    write_receipt(campaign, task_id="t3", candidate_text=CANDIDATE_TEXT, status="error")
    write_receipt(
        campaign, task_id="t7", candidate_text=CANDIDATE_TEXT, status="completed", score=1.0
    )
    # A completed row without a finite score is missingness too, never zero.
    write_receipt(
        campaign,
        task_id="t8",
        candidate_text=CANDIDATE_TEXT,
        status="completed",
        score=None,
    )

    report = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=7)
    entry = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]
    assert entry["paired_task_count"] == 1
    assert entry["paired_tasks"][0]["task_id"] == "t1"
    assert entry["candidate_mean_success"] == pytest.approx(0.0)
    assert entry["stock_mean_success"] == pytest.approx(1.0)
    assert entry["missingness"] == {
        "completed": 2,
        "completed_unscored": 1,
        "error": 1,
        "pending": 1,
    }
    assert {row["task_id"] for row in entry["only_stock_tasks"]} == {"t2"}
    assert report["campaign"]["stock"]["missingness"] == {
        "completed": 2,
        "error": 1,
        "pending": 1,
    }
    assert report["campaign"]["receipts"]["completed"] == 4
    assert report["campaign"]["receipts"]["pending"] == 2
    assert report["campaign"]["receipts"]["error"] == 2
    assert report["campaign"]["receipts"]["completed_unscored"] == 1


def test_unknown_cost_is_counted_not_zeroed(tmp_path: Path) -> None:
    """Missing usage values stay None plus an unknown count, never zero."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0, usage={"cost_usd": 0.5})
    write_receipt(campaign, task_id="t2", score=1.0, usage={"cost_usd": 0.25})
    write_receipt(
        campaign,
        task_id="t1",
        candidate_text=CANDIDATE_TEXT,
        score=1.0,
        usage={"cost_usd": None},
    )
    write_receipt(
        campaign,
        task_id="t2",
        candidate_text=CANDIDATE_TEXT,
        score=1.0,
        usage={"cost_usd": 0.75},
    )

    report = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=9)
    totals = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["usage_totals"]["paired_tasks"]
    assert totals["candidate"]["cost_usd"] == {
        "total": 0.75,
        "known_rows": 1,
        "unknown_rows": 1,
    }
    assert totals["stock"]["cost_usd"] == {
        "total": 0.75,
        "known_rows": 2,
        "unknown_rows": 0,
    }
    # A latency the producer never wrote is unknown, not a zero duration.
    assert totals["candidate"]["latency_seconds"] == {
        "total": None,
        "known_rows": 0,
        "unknown_rows": 2,
    }


def test_bootstrap_deterministic_and_unavailable_with_one_cluster(tmp_path: Path) -> None:
    """Same seed reproduces the interval; a single cluster yields no interval."""
    campaign = tmp_path / "campaign"
    for index, (stock_score, candidate_score) in enumerate(
        [(1.0, 0.6), (0.2, 0.9), (0.4, 0.1), (0.8, 0.3), (0.0, 0.5), (0.6, 0.95)]
    ):
        write_receipt(campaign, task_id=f"t{index}", score=stock_score)
        write_receipt(
            campaign,
            task_id=f"t{index}",
            candidate_text=CANDIDATE_TEXT,
            score=candidate_score,
        )

    first = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=42, resamples=64)
    second = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=42, resamples=64)
    other_seed = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=43, resamples=64)
    uncertainty = first["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["uncertainty"]
    assert uncertainty == second["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["uncertainty"]
    assert uncertainty["available"] is True
    assert uncertainty["seed"] == 42
    assert uncertainty["resamples"] == 64
    assert uncertainty["clusters"] == 6
    assert uncertainty["low"] < uncertainty["high"]
    other = other_seed["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["uncertainty"]
    assert (other["low"], other["high"]) != (uncertainty["low"], uncertainty["high"])

    single_cluster = analyze_campaign(
        campaign,
        stock_candidate=_cid(STOCK_TEXT),
        source_groups={f"t{index}": "solo" for index in range(6)},
        seed=42,
        resamples=64,
    )
    clustered = single_cluster["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["uncertainty"]
    assert clustered["low"] is None
    assert clustered["high"] is None
    assert "at least two clusters" in clustered["unavailable_reason"]
    assert clustered["clusters"] == 1


def test_budget_mismatch_refuses_budget_matched_label(tmp_path: Path) -> None:
    """A control with a different completed-evaluation count is not budget-matched."""
    campaign = tmp_path / "campaign"
    control = tmp_path / "control"
    for task_id, stock_score, candidate_score in (("t1", 1.0, 0.5), ("t2", 0.0, 0.5)):
        write_receipt(campaign, task_id=task_id, score=stock_score)
        write_receipt(
            campaign, task_id=task_id, candidate_text=CANDIDATE_TEXT, score=candidate_score
        )
        write_receipt(control, task_id=task_id, score=stock_score)
    write_receipt(control, task_id="t1", candidate_text=CANDIDATE_TEXT, score=1.0)

    report = analyze_campaign(
        campaign,
        stock_candidate=_cid(STOCK_TEXT),
        control_dir=control,
        seed=13,
    )
    assert report["method_control"]["status"] == "budget_mismatch"
    reasons = " ".join(report["method_control"]["budget_check"]["mismatch_reasons"])
    assert "completed evaluations differ" in reasons
    assert report["method_control"]["budget_check"]["completed_evaluations"] == {
        "campaign": 4,
        "control": 3,
    }


def test_evaluation_count_matched_control_reports_best_deltas(tmp_path: Path) -> None:
    """Equal evaluation counts allow a descriptive delta, not a spend-match claim."""
    campaign = tmp_path / "campaign"
    control = tmp_path / "control"
    for task_id, stock_score, candidate_score in (("t1", 1.0, 0.5), ("t2", 0.0, 0.75)):
        write_receipt(campaign, task_id=task_id, score=stock_score)
        write_receipt(
            campaign, task_id=task_id, candidate_text=CANDIDATE_TEXT, score=candidate_score
        )
    for task_id, stock_score, candidate_score in (("t1", 1.0, 0.75), ("t2", 0.0, 0.25)):
        write_receipt(control, task_id=task_id, score=stock_score)
        write_receipt(
            control, task_id=task_id, candidate_text=CANDIDATE_TEXT, score=candidate_score
        )

    report = analyze_campaign(
        campaign,
        stock_candidate=_cid(STOCK_TEXT),
        control_dir=control,
        seed=13,
    )
    control_report = analyze_campaign(control, stock_candidate=_cid(STOCK_TEXT), seed=13)
    assert report["method_control"]["status"] == "evaluation_count_matched"
    assert report["method_control"]["budget_check"]["mismatch_reasons"] == []
    campaign_delta = report["campaign"]["best_by_search_score"]["paired_mean_delta_vs_stock"]
    control_delta = control_report["campaign"]["best_by_search_score"]["paired_mean_delta_vs_stock"]
    assert campaign_delta == pytest.approx(0.125)
    # Control candidate merely ties the stock search mean: the incumbent stock
    # arm wins the tie, so its paired delta against itself is exactly zero.
    assert control_delta == pytest.approx(0.0)
    assert report["method_control"]["control_best"]["candidate_id"] == _cid(STOCK_TEXT)
    assert report["method_control"]["paired_delta_difference"] == pytest.approx(
        campaign_delta - control_delta
    )
    assert report["method_control"]["campaign_best"]["candidate_id"] == _cid(CANDIDATE_TEXT)


def test_missing_control_reported_as_missing(tmp_path: Path) -> None:
    """Without control_dir the method control is explicitly missing."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    report = analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=1)
    assert report["method_control"] == {"status": "missing"}


def test_symlinked_receipt_rejected(tmp_path: Path) -> None:
    """A symlinked receipt is refused rather than followed."""
    real = tmp_path / "real.json"
    campaign = tmp_path / "campaign"
    receipt = write_receipt(campaign, task_id="t1", score=1.0)
    real.write_text(receipt.read_text())
    receipt.unlink()
    (campaign / "evaluations" / "linked.json").symlink_to(real)
    with pytest.raises(ValueError, match="symlinked receipt rejected"):
        analyze_campaign(campaign, stock_candidate=_cid(STOCK_TEXT), seed=1)


def test_stock_candidate_must_be_exact_and_present(tmp_path: Path) -> None:
    """Ambiguous prefixes and absent stock arms are refused."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    with pytest.raises(ValueError, match="prefix ambiguity"):
        analyze_campaign(campaign, stock_candidate="sha256:abc123", seed=1)
    with pytest.raises(ValueError, match="no receipts"):
        analyze_campaign(campaign, stock_candidate="sha256:" + "e" * 64, seed=1)
    pending_only = tmp_path / "pending-only"
    write_receipt(pending_only, task_id="t1", status="pending")
    with pytest.raises(ValueError, match="no completed scored evaluations"):
        analyze_campaign(pending_only, stock_candidate=_cid(STOCK_TEXT), seed=1)


def test_unpaired_candidates_preserve_unknown_delta_and_empty_paired_usage(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    control = tmp_path / "control"
    for root in (campaign, control):
        write_receipt(root, task_id="stock-only", score=0.0)
        write_receipt(root, task_id="candidate-only", candidate_text=CANDIDATE_TEXT, score=1.0)

    report = analyze_campaign(
        campaign, stock_candidate=_cid(STOCK_TEXT), control_dir=control, seed=21
    )

    candidate = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]
    assert candidate["paired_mean_delta"] is None
    assert candidate["usage_totals"]["paired_tasks"]["candidate"]["cost_usd"] == {
        "total": None,
        "known_rows": 0,
        "unknown_rows": 0,
    }
    assert report["campaign"]["usage_totals"]["cost_usd"]["total"] == pytest.approx(0.02)
    assert report["method_control"]["paired_delta_difference"] is None


def test_duplicate_scored_receipts_are_refused(tmp_path: Path) -> None:
    write_receipt(tmp_path, task_id="t1", score=1.0)
    write_receipt(tmp_path, task_id="t1", score=1.0, filename_suffix="-copy")
    with pytest.raises(ValueError, match="duplicate receipts"):
        analyze_campaign(tmp_path, stock_candidate=_cid(STOCK_TEXT), seed=1)


def test_sealed_receipts_cannot_enter_search_analysis(tmp_path: Path) -> None:
    receipt = write_receipt(tmp_path, task_id="t1", score=1.0)
    row = json.loads(receipt.read_text())
    row["split"] = "test"
    receipt.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="not a development evaluation"):
        analyze_campaign(tmp_path, stock_candidate=_cid(STOCK_TEXT), seed=1)


def test_finite_large_scores_do_not_overflow_mean_aggregation(tmp_path: Path) -> None:
    for task_id in ("t1", "t2"):
        write_receipt(tmp_path, task_id=task_id, score=1e308)
        write_receipt(tmp_path, task_id=task_id, candidate_text=CANDIDATE_TEXT, score=1e308)
    report = analyze_campaign(tmp_path, stock_candidate=_cid(STOCK_TEXT), seed=1)
    candidate = report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]
    assert candidate["stock_mean_success"] == 1e308
    assert candidate["candidate_mean_success"] == 1e308
    assert candidate["paired_mean_delta"] == 0.0


def test_cli_analyze_writes_report_and_refusal_exits_two(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """analyze exits 0 with --out written, and 2 with the refusal on stderr."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    write_receipt(campaign, task_id="t1", candidate_text=CANDIDATE_TEXT, score=0.5)
    out = tmp_path / "report.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "analyze",
            "--campaign-dir",
            str(campaign),
            "--stock-candidate",
            _cid(STOCK_TEXT),
            "--seed",
            "31",
            "--resamples",
            "32",
            "--out",
            str(out),
        ],
    )
    assert main() == 0
    report = json.loads(out.read_text())
    assert set(report) == {
        "schema_version",
        "caveat",
        "source_groups",
        "campaign",
        "method_control",
    }
    assert report["campaign"]["candidates"][_cid(CANDIDATE_TEXT)]["paired_task_count"] == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "analyze",
            "--campaign-dir",
            str(campaign),
            "--stock-candidate",
            "sha256:short",
            "--seed",
            "31",
        ],
    )
    assert main() == 2
    assert "analyze refused" in capsys.readouterr().err


def test_cli_seed_has_no_default_and_run_still_requires_campaign(
    tmp_path: Path, monkeypatch
) -> None:
    """analyze without --seed is a usage error; run still demands its positional."""
    campaign = tmp_path / "campaign"
    write_receipt(campaign, task_id="t1", score=1.0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "analyze",
            "--campaign-dir",
            str(campaign),
            "--stock-candidate",
            _cid(STOCK_TEXT),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2

    monkeypatch.setattr(sys, "argv", ["prog", "run"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
