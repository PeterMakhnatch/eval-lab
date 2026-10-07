"""Fixture-backed tests for evallab.task_dossier_trials (HAR-186).

Real files end to end: tiny raw job trees feed the real ``connect_trials``
(``task_names`` + ``read_only``), tiny published copies feed the alias index,
and tiny reader verdicts feed enrichment. No providers, no network, no writes
(the suite snapshots every fixture byte before/after each read-only call).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evallab import task_dossier_trials as dossier

TASK = "format-code-task-000792"
NAMESPACED = f"mimo-v2.6-rl/{TASK}"
TRACE_A1 = "2ef52689-4435-ffcf-b89a-e7b5e55eb60a"
TRACE_A2 = "36776e3d-21ac-d5fe-3e3d-79334760b1a5"
LAMINAR_PROJECT = "a5586b6d-55e8-466f-9718-c38255c2ae94"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _trial_result(
    trial_id: str, job_id: str, task_name: str, trial_name: str
) -> dict:
    return {
        "id": trial_id,
        "config": {"job_id": job_id},
        "task_name": task_name,
        "trial_name": trial_name,
        "verifier_result": {"rewards": {"reward": 1.0}},
    }


def _reward_details_fired() -> dict:
    return {
        "integrity": {
            "components": [
                {
                    "detail": {
                        "criteria": [
                            {
                                "name": "copy_check/upstream",
                                "value": 0,
                                "reasoning": "matched outside code",
                            }
                        ]
                    }
                }
            ]
        }
    }


def _laminar_verdict(job: str, trial: str, copied: bool, trace: str) -> dict:
    return {
        "schema": "evallab.reader_verdict/v1",
        "reader": "laminar_signals",
        "trial": trial,
        "job": job,
        "checks": {"copied": copied, "false_completion": False, "stuck_loop": False},
        "explanations": {},
        "model": "glm-5.3-flash",
        "tokens": {"input": 10, "output": 2},
        "cost_usd": None,
        "at": "2026-10-06T18:00:44.809798+00:00",
        "raw": None,
        "trace_id": trace,
        "trace_url": f"https://lmnr.ai/project/{LAMINAR_PROJECT}/traces/{trace}",
    }


def _analyze_verdict(job: str, trial: str, reward_hacking: bool) -> dict:
    return {
        "schema": "evallab.reader_verdict/v1",
        "reader": "harbor_analyze",
        "trial": trial,
        "job": job,
        "checks": {"reward_hacking": reward_hacking, "task_specification": False},
        "explanations": {"reward_hacking": "recorded explanation"},
        "model": "openai/glm-5.3-flash",
        "tokens": {"input": 10, "output": 2},
        "cost_usd": 0.01,
        "at": "2026-10-06T19:28:54.282768+00:00",
        "raw": "raw/harbor_analyze/x/analysis.json",
        "input_policy": "evallab.reader_input/blind-v2",
    }


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Raw jobs, published copies, reader verdicts, and env overrides."""
    runs = tmp_path / "runs"
    home = tmp_path / "home"
    store = tmp_path / "store"
    derived = tmp_path / "derived"
    derived.mkdir()

    # -- raw census jobs (original har168- names) --------------------------
    raw_a1 = runs / "har168-20261006-000792-a1"
    raw_a2 = runs / "har168-20261006-000792-a2"
    raw_other = runs / "har168-20261006-000788-a1"
    t1 = "har168-20261006-000792-a1__XavEDNw"
    t2 = "har168-20261006-000792-a2__iNkmiTm"
    _write(raw_a1 / "result.json", {"id": "job-a1"})
    _write(raw_a1 / t1 / "result.json", _trial_result("trial-a1", "job-a1", TASK, t1))
    _write(
        raw_a1 / t1 / "laminar-trace.json",
        {"trial_name": t1, "trace_id": TRACE_A1, "root_span_id": "0123456789abcdef"},
    )
    _write(
        raw_a1 / "processed" / f"trial-{t1}.json",
        {"taint": [{"kind": "copied_code", "matched_lines": 7, "added_lines": 9}]},
    )
    _write(raw_a1 / t1 / "verifier" / "reward-details.json", _reward_details_fired())
    _write(raw_a2 / "result.json", {"id": "job-a2"})
    _write(
        raw_a2 / t2 / "result.json", _trial_result("trial-a2", "job-a2", NAMESPACED, t2)
    )
    _write(raw_a2 / "processed" / f"trial-{t2}.json", {"taint": []})
    # Unrelated same trial dir name, distinct native identity + task: excluded.
    _write(raw_other / "result.json", {"id": "job-other"})
    _write(
        raw_other / t1 / "result.json",
        _trial_result("trial-other", "job-other", "format-code-task-000788", t1),
    )

    # -- published copies (HAR-168- names) ---------------------------------
    day = home / "2026-10-06"
    pub_a1 = day / "HAR-168-har168-20261006-000792-a1"
    pub_a2 = day / "HAR-168-har168-20261006-000792-a2"
    for pub, raw, job_id, pairs in (
        (pub_a1, raw_a1, "job-a1", [("trial-a1", TASK, t1)]),
        (pub_a2, raw_a2, "job-a2", [("trial-a2", NAMESPACED, t2)]),
    ):
        _write(pub / "result.json", {"id": job_id})
        _write(
            pub / "provenance.json",
            {"schema": "results_home/v1", "source_path": str(raw.resolve())},
        )
        for trial_id, task_name, trial_name in pairs:
            _write(
                pub / trial_name / "result.json",
                _trial_result(trial_id, job_id, task_name, trial_name),
            )
    # a1: stored copy_check taint + RewardKit copy rule fired.
    _write(
        pub_a1 / "processed" / f"trial-{t1}.json",
        {"taint": [{"kind": "copied_code", "matched_lines": 7, "added_lines": 9}]},
    )
    _write(pub_a1 / t1 / "verifier" / "reward-details.json", _reward_details_fired())
    # a2: no processed taint, no reward-details (both must stay null, not clean).
    _write(pub_a2 / "processed" / f"trial-{t2}.json", {"taint": []})

    # -- reader verdicts ----------------------------------------------------
    _write(
        store / pub_a1.name / t1 / "laminar_signals.json",
        _laminar_verdict(pub_a1.name, t1, True, TRACE_A1),
    )
    _write(
        store / pub_a1.name / t1 / "harbor_analyze.json",
        _analyze_verdict(pub_a1.name, t1, False),
    )
    _write(
        store / pub_a2.name / t2 / "laminar_signals.json",
        _laminar_verdict(pub_a2.name, t2, False, TRACE_A2),
    )

    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(home))
    monkeypatch.setenv("EVALLAB_READERS_STORE", str(store))
    return {
        "runs": runs,
        "home": home,
        "store": store,
        "derived": derived,
        "root": tmp_path,
    }


def _snapshot(root: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


def _call(task: str, world: dict[str, Path]) -> dict:
    return dossier.task_trials(
        task,
        repo_root=world["root"],
        roots=[world["runs"]],
        derived_root=world["derived"],
        reader_store=world["store"],
    )


def test_membership_two_attempts_unrelated_excluded(world: dict[str, Path]) -> None:
    before = _snapshot(world["root"])
    dossier_out = _call(TASK, world)
    assert _snapshot(world["root"]) == before  # read-only: not one byte changed
    assert list(world["derived"].rglob("*")) == []

    by_job = {row["job"]: row for row in dossier_out["trials"]}
    assert set(by_job) == {"har168-20261006-000792-a1", "har168-20261006-000792-a2"}
    assert by_job["har168-20261006-000792-a1"]["task"] == TASK
    assert by_job["har168-20261006-000792-a2"]["task"] == NAMESPACED
    # Same dir name, distinct native identity and task: never merged in.
    assert all(row["job_id"] in ("job-a1", "job-a2") for row in dossier_out["trials"])
    assert "legit" in by_job["har168-20261006-000792-a1"]
    assert "projection_error" in by_job["har168-20261006-000792-a1"]
    assert by_job["har168-20261006-000792-a1"]["laminar_trace_id"] == TRACE_A1
    coverage = dossier_out["coverage"]
    assert coverage["n_trials"] == 2
    assert coverage["aliases"] == [TASK, NAMESPACED, f"mimo-v2.6-rl__{TASK}"]
    assert coverage["read_only"] is True
    json.dumps(dossier_out)  # fully JSON-serializable


def test_unknown_task_yields_empty_without_fabrication(
    world: dict[str, Path],
) -> None:
    dossier_out = _call("format-code-task-999999", world)
    assert dossier_out["trials"] == []
    assert dossier_out["coverage"]["n_trials"] == 0


def test_source_binding_original_to_published(world: dict[str, Path]) -> None:
    dossier_out = _call(TASK, world)
    by_job = {row["job"]: row for row in dossier_out["trials"]}
    a1 = by_job["har168-20261006-000792-a1"]
    assert a1["alias"]["status"] == "ok"
    assert a1["alias"]["published_job"] == "HAR-168-har168-20261006-000792-a1"
    assert (
        a1["alias"]["published_trial"] == "har168-20261006-000792-a1__XavEDNw"
    )
    assert a1["alias"]["source_path"] == str(
        (world["runs"] / "har168-20261006-000792-a1").resolve()
    )
    # Copy facts come from the original source trial, not the publication.
    assert a1["evidence"]["job"] == "har168-20261006-000792-a1"
    # Raw evidence passes through immutably: exact source/check labels kept.
    verdicts = a1["evidence"]["verdicts"]
    assert verdicts["laminar_signals"]["checks"]["copied"] is True
    assert verdicts["harbor_analyze"]["checks"]["reward_hacking"] is False
    assert (
        verdicts["harbor_analyze"]["input_policy"]
        == "evallab.reader_input/blind-v2"
    )


def test_per_source_disagreement_preserved_no_consensus(
    world: dict[str, Path],
) -> None:
    dossier_out = _call(TASK, world)
    by_job = {row["job"]: row for row in dossier_out["trials"]}
    a1 = by_job["har168-20261006-000792-a1"]
    assert a1["copy_verdicts"]["copy_check"] is True
    assert a1["copy_verdicts"]["rewardkit_copy"] is True
    assert a1["copy_verdicts"]["laminar_copied"] is True
    # Genuine disagreement across sources is preserved, never averaged away.
    assert a1["copy_verdicts"]["harbor_analyze_reward_hacking"] is False
    assert "consensus" not in a1

    a2 = by_job["har168-20261006-000792-a2"]
    # Missing evidence stays null: never promoted to a clean result.
    assert a2["copy_verdicts"]["copy_check"] is False
    assert a2["copy_verdicts"]["rewardkit_copy"] is None
    assert a2["copy_verdicts"]["laminar_copied"] is False
    assert a2["copy_verdicts"]["harbor_analyze_reward_hacking"] is None
    assert a2["laminar_url"] is not None  # recorded link, census trace unrecorded


def test_rewardkit_requires_decided_rule_record(tmp_path: Path) -> None:
    def trial_with(details: object) -> Path:
        trial = tmp_path / f"t{len(list(tmp_path.iterdir()))}"
        verifier = trial / "verifier"
        verifier.mkdir(parents=True)
        if details is not None:
            (verifier / "reward-details.json").write_text(
                "not json {" if details == "malformed" else json.dumps(details),
                encoding="utf-8",
            )
        return trial

    fired = {
        "integrity": {
            "components": [
                {"detail": {"criteria": [{"name": "copy_check/x", "value": 0}]}}
            ]
        }
    }
    clean = {
        "integrity": {
            "components": [
                {"detail": {"criteria": [{"name": "copy_check/x", "value": 1}]}}
            ]
        }
    }
    assert dossier._rewardkit_copy(trial_with(fired)) is True
    assert dossier._rewardkit_copy(trial_with(clean)) is False
    # No record, malformed record, or no decided copy_check criterion: unknown.
    assert dossier._rewardkit_copy(trial_with(None)) is None
    assert dossier._rewardkit_copy(trial_with("malformed")) is None
    assert dossier._rewardkit_copy(trial_with({})) is None
    assert dossier._rewardkit_copy(trial_with({"integrity": {"score": 0}})) is None
    unrelated = {
        "integrity": {
            "components": [
                {"detail": {"criteria": [{"name": "other_rule", "value": 0}]}}
            ]
        }
    }
    assert dossier._rewardkit_copy(trial_with(unrelated)) is None
    undecided = {
        "integrity": {
            "components": [
                {"detail": {"criteria": [{"name": "copy_check/x", "value": None}]}}
            ]
        }
    }
    assert dossier._rewardkit_copy(trial_with(undecided)) is None


def test_page_and_laminar_urls(world: dict[str, Path]) -> None:
    dossier_out = _call(TASK, world)
    assert dossier_out["coverage"]["page_url"] == "http://127.0.0.1:8100/jobs/task-000792"
    by_job = {row["job"]: row for row in dossier_out["trials"]}
    a1 = by_job["har168-20261006-000792-a1"]
    assert a1["page_url"] == "http://127.0.0.1:8100/jobs/task-000792"
    assert (
        a1["laminar_url"]
        == f"https://lmnr.ai/project/{LAMINAR_PROJECT}/traces/{TRACE_A1}"
    )


def test_laminar_trace_mismatch_nulls_verdict_but_keeps_metadata(
    world: dict[str, Path],
) -> None:
    t1 = "har168-20261006-000792-a1__XavEDNw"
    verdict_path = (
        world["store"]
        / "HAR-168-har168-20261006-000792-a1"
        / t1
        / "laminar_signals.json"
    )
    original = verdict_path.read_text(encoding="utf-8")
    wrong = _laminar_verdict(
        "HAR-168-har168-20261006-000792-a1", t1, True, TRACE_A2
    )
    _write(verdict_path, wrong)
    try:
        dossier_out = _call(TASK, world)
        by_job = {row["job"]: row for row in dossier_out["trials"]}
        a1 = by_job["har168-20261006-000792-a1"]
        # Wrong-trace verdict is ineligible: URL and copied both null ...
        assert a1["laminar_url"] is None
        assert a1["copy_verdicts"]["laminar_copied"] is None
        # ... but the stored record is retained for audit, with the conflict.
        assert a1["evidence"]["verdicts"]["laminar_signals"]["checks"]["copied"] is True
        # Unaffected sources keep their verdicts.
        assert a1["copy_verdicts"]["harbor_analyze_reward_hacking"] is False
    finally:
        verdict_path.write_text(original, encoding="utf-8")


def test_laminar_url_path_must_identify_recorded_trace() -> None:
    row = {"laminar_trace_id": TRACE_A1}
    bound = {
        "laminar_signals": {
            "trace_id": TRACE_A1,
            "trace_url": f"https://lmnr.ai/project/{LAMINAR_PROJECT}/traces/{TRACE_A2}",
        }
    }
    url, _, trace_disputed = dossier._laminar_url(row, bound)
    assert url is None
    assert trace_disputed is False


def test_ambiguous_alias_keeps_native_facts(world: dict[str, Path]) -> None:
    # Duplicate the published copy: same native pair, second directory.
    dup = world["home"] / "2026-10-06" / "HAR-168-har168-20261006-000792-a1-dup"
    src = world["home"] / "2026-10-06" / "HAR-168-har168-20261006-000792-a1"
    dup.mkdir(parents=True)
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dup / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    try:
        dossier_out = _call(TASK, world)
        by_job = {row["job"]: row for row in dossier_out["trials"]}
        a1 = by_job["har168-20261006-000792-a1"]
        assert a1["alias"]["status"] == "ambiguous"
        # Native copy facts survive; only the published reader alias is unknown.
        assert a1["evidence"] is not None
        assert a1["copy_verdicts"]["copy_check"] is True
        assert a1["copy_verdicts"]["rewardkit_copy"] is True
        assert a1["copy_verdicts"]["laminar_copied"] is None
        assert a1["binding_conflicts"]
    finally:
        shutil.rmtree(dup, ignore_errors=True)


def test_missing_publication_keeps_native_facts(world: dict[str, Path]) -> None:
    shutil.rmtree(world["home"] / "2026-10-06" / "HAR-168-har168-20261006-000792-a2")
    dossier_out = _call(TASK, world)
    by_job = {row["job"]: row for row in dossier_out["trials"]}
    a2 = by_job["har168-20261006-000792-a2"]
    assert a2["alias"]["status"] == "missing"
    assert a2["evidence"] is not None
    assert a2["copy_verdicts"]["copy_check"] is False
    assert a2["copy_verdicts"]["rewardkit_copy"] is None
    assert a2["copy_verdicts"]["laminar_copied"] is None
    assert a2["laminar_url"] is None


def test_non_blind_analyzer_verdict_is_excluded(world: dict[str, Path]) -> None:
    store = world["store"]
    verdict_path = (
        store
        / "HAR-168-har168-20261006-000792-a2"
        / "har168-20261006-000792-a2__iNkmiTm"
        / "harbor_analyze.json"
    )
    verdict = _analyze_verdict(
        "HAR-168-har168-20261006-000792-a2",
        "har168-20261006-000792-a2__iNkmiTm",
        True,
    )
    verdict["input_policy"] = "something-else"
    _write(verdict_path, verdict)
    try:
        dossier_out = _call(TASK, world)
        by_job = {row["job"]: row for row in dossier_out["trials"]}
        a2 = by_job["har168-20261006-000792-a2"]
        assert a2["copy_verdicts"]["harbor_analyze_reward_hacking"] is None
    finally:
        verdict_path.unlink()


@pytest.mark.parametrize(
    ("values", "expected"), [([1, None], None), ([0, None], True), ([1, 1], False)],
)
def test_partial_copy_rules_do_not_imply_a_clean_verdict(tmp_path, values, expected):
    trial = tmp_path / "trial"
    _write(trial / "verifier/reward-details.json", {
        "integrity": {"components": [{"detail": {"criteria": [
            {"name": f"copy_check_{index}", "value": value}
            for index, value in enumerate(values)
        ]}}]},
    })

    assert dossier._rewardkit_copy(trial) is expected
