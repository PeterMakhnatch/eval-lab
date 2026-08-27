from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from evallab.cli import run_cli
from evallab.evidence.facts import AnalyzerCallResult, run_trial_analysis
from evallab.results import load_job
from evallab.status import (
    SECTION_KEYS,
    build_status_snapshot,
    iter_labeled_items,
    resolve_status_layout,
    snapshot_as_dict,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/operability"
PROMPT = ROOT / "research/analysis/stage5-prompt.md"
RUBRIC = ROOT / "research/analysis/stage5-rubric.json"
STUB = ROOT / "research/analysis/stub-oracle-analysis.json"
FIXED = datetime(2026, 8, 14, tzinfo=UTC)


def _copy(name: str, tmp_path: Path) -> Path:
    destination = tmp_path / name
    shutil.copytree(FIXTURES / name, destination)
    return destination


def _tree_signature(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _offline_snapshot(root: Path):
    return build_status_snapshot(
        root,
        postgres_probe=lambda: False,
        phoenix_probe=lambda: False,
        generated_at=FIXED,
    )


def _attach_saved_analysis(scratch: Path) -> Path:
    job = load_job(scratch / "jobs/operability-join")

    def analyzer(_prompt: str, _schema: dict[str, object]) -> AnalyzerCallResult:
        return AnalyzerCallResult(raw_output=STUB.read_text(), cost_usd=0.0)

    path, sidecar = run_trial_analysis(
        job,
        job.trials[0],
        analyzer=analyzer,
        repo_root=scratch,
        destination_root=scratch / "analyses",
        prompt_path=PROMPT,
        rubric_path=RUBRIC,
        agent="stub",
        agent_version="1",
        model="saved-response",
        created_at=FIXED,
    )
    assert sidecar.validation_status == "valid"
    return path


def test_join_experiment_job_trial_trajectory_analysis(tmp_path: Path) -> None:
    scratch = _copy("complete", tmp_path)
    sidecar_path = _attach_saved_analysis(scratch)
    snapshot = _offline_snapshot(scratch)
    recent = next(item for item in snapshot.Recent.items if item.kind == "trial")
    analysis = next(item for item in snapshot.Analysis.items if item.kind == "analysis")

    assert recent.experiment_id == "exp-operability-join"
    assert recent.job_id == "11111111-1111-4111-8111-111111111111"
    assert recent.trial_id == "22222222-2222-4222-8222-222222222222"
    assert recent.trajectory_present is True
    assert recent.analysis_id == analysis.analysis_id
    assert analysis.provenance is not None
    assert analysis.provenance["agent"] == "stub"
    assert analysis.provenance["model"] == "saved-response"
    assert analysis.provenance["prompt_digest"].startswith("sha256:")
    assert sidecar_path.is_file()
    assert analysis.availability == "draft"


def test_harness_exception_is_not_a_model_failure(tmp_path: Path) -> None:
    scratch = _copy("harness-exception", tmp_path)
    snapshot = _offline_snapshot(scratch)
    trial = next(item for item in snapshot.Recent.items if item.kind == "trial")
    assert trial.exception_class == "RuntimeError"
    assert trial.scored_as_model_failure is False
    assert "harness exception" in (trial.detail or "")


def test_malformed_and_missing_stores_are_labeled_not_crashes(tmp_path: Path) -> None:
    malformed = _offline_snapshot(_copy("malformed", tmp_path))
    missing = _offline_snapshot(_copy("missing-stores", tmp_path))

    assert malformed.Now.availability == "review-needed"
    assert any(item.kind == "malformed-spec" for item in malformed.Now.items)
    assert any(item.kind == "malformed-job" for item in malformed.Recent.items)
    assert any(item.kind == "malformed-analysis" for item in malformed.Analysis.items)

    health_labels = {item.label: item.availability for item in missing.Health.items}
    assert health_labels["postgres"] == "unavailable"
    assert health_labels["phoenix"] == "unavailable"
    assert missing.Recent.availability == "unavailable"
    allowed = {"observed", "unavailable", "draft", "review-needed"}
    assert all(item.availability in allowed for item in iter_labeled_items(missing))


def test_empty_approved_and_running_queues_are_represented(tmp_path: Path) -> None:
    snapshot = _offline_snapshot(_copy("empty-queue", tmp_path))
    assert snapshot.Now.availability == "observed"
    assert snapshot.Now.items[0].label == "no approved or running work"
    assert snapshot.Next.items[0].label == "no waiting work"


def test_status_writes_nothing(tmp_path: Path) -> None:
    scratch = _copy("complete", tmp_path)
    before = _tree_signature(scratch)
    _offline_snapshot(scratch)
    after = _tree_signature(scratch)
    assert before == after
    assert not (scratch / "status.json").exists()


def test_cli_and_dashboard_share_the_same_projection(tmp_path: Path, capsys) -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from dashboard.projection import dashboard_view, load_operator_snapshot

    scratch = _copy("complete", tmp_path)
    _attach_saved_analysis(scratch)
    snapshot = build_status_snapshot(scratch, generated_at=FIXED)
    dashboard = load_operator_snapshot(scratch, generated_at=FIXED)
    assert snapshot_as_dict(snapshot) == dashboard_view(dashboard)

    code = run_cli(["status", "--json", "--from", str(scratch)], workspace=scratch)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert tuple(key for key in payload if key in SECTION_KEYS) == SECTION_KEYS
    for key in SECTION_KEYS:
        assert payload[key]["items"] == snapshot_as_dict(snapshot)[key]["items"]
        for item in payload[key]["items"]:
            assert item["availability"] in {
                "observed",
                "unavailable",
                "draft",
                "review-needed",
            }

    human = run_cli(["status", "--from", str(scratch)], workspace=scratch)
    assert human == 0
    text = capsys.readouterr().out
    for key in SECTION_KEYS:
        assert key in text


def test_cold_status_is_readable_without_traceback(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "cold"
    empty.mkdir()
    snapshot = _offline_snapshot(empty)
    health = {item.label: item.availability for item in snapshot.Health.items}
    assert health["postgres"] == "unavailable"
    assert health["phoenix"] == "unavailable"

    code = run_cli(["status", "--json", "--from", str(empty)], workspace=empty)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "Traceback" not in json.dumps(payload)
    assert tuple(key for key in payload if key in SECTION_KEYS) == SECTION_KEYS
    assert payload["Health"]["items"]


def test_real_checkout_uses_the_shared_parquet_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "checkout"
    (root / "src/evallab").mkdir(parents=True)
    monkeypatch.delenv("EVALLAB_DERIVED_ROOT", raising=False)
    assert resolve_status_layout(root).parquet_root == root / "derived/parquet"


def test_dashboard_preserves_research_panes() -> None:
    source = (ROOT / "dashboard/app.py").read_text()
    for heading in (
        "Leaderboard by cohort",
        "Canary trend vs 7-day baseline",
        "Spend vs daily ceiling",
        "Queue funnel",
        "Calibration history",
        "ATIF-derived activity",
        "DISCOVERIES",
    ):
        assert heading in source
