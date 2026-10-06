"""Behavioral tests for the transient trials census (HAR-178).

Real DuckDB plus tiny parquet fixtures throughout. Posture enters
through the real module with per-trial fixture overrides (the cut-short
mapping always runs for real); ``process_job`` runs for real on the
backfill paths, with a call-through counter proving per-job caching and
a call-forbidding spy proving warm reuse. No SQL-text, column-order, or
parser-wiring assertions: those are covered by behavior below.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.storage.trials import command, connect_trials

# Contract-pinned legit inputs (the view DDL in sql/trials.sql owns the
# predicate; these are fixture values for the rows under test).
LEGIT_MODEL = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
LEGIT_HARNESS = "evallab.harbor_mimoagent:NativeMimoAgent"

LEGIT_OK = {
    "campaign": "HAR-178-probe",
    "model": LEGIT_MODEL,
    "harness": LEGIT_HARNESS,
    "egress_lock": True,
    "reference_profile": "mimo-64k",
    "reference_profile_match": True,
    "infra": False,
}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _trial_body(trial_id, job_id, trial_name, task_name, trial_uri=None):
    body = {
        "id": trial_id,
        "trial_name": trial_name,
        "task_name": task_name,
        "config": {"job_id": job_id, "agent": {"name": "terminus-2"}},
        "agent_info": {"name": "terminus-2", "model_name": LEGIT_MODEL},
        "started_at": "2026-09-22T11:00:00+00:00",
        "finished_at": "2026-09-22T11:20:00+00:00",
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    if trial_uri is not None:
        body["trial_uri"] = trial_uri
    return body


def _make_trial(job_dir, dirname, *, trial_id, job_id, trial_name, task_name,
                trial_uri=None):
    trial_dir = job_dir / dirname
    _write_json(
        trial_dir / "result.json",
        _trial_body(trial_id, job_id, trial_name, task_name, trial_uri),
    )
    _write_json(trial_dir / "config.json", {"job_id": job_id, "trial_name": trial_name})
    return trial_dir


def _make_job(runs_dir, name, *, job_id, n_total, finished=True):
    job_dir = runs_dir / name
    if finished:
        _write_json(
            job_dir / "result.json",
            {
                "id": job_id,
                "n_total_trials": n_total,
                "stats": {"n_completed_trials": n_total},
                "finished_at": "2026-09-22T11:30:00+00:00",
            },
        )
    _write_json(job_dir / "config.json", {"job_id": job_id})
    return job_dir


def _write_hot(
    derived,
    job_id,
    trial_id,
    *,
    primary="absent",
    rewards=None,
    stop="task_complete",
    verdict="counted_pass",
):
    """One per-pair projection trio. Sources are written first by callers."""
    part = derived / f"job_id={job_id}" / f"trial_id={trial_id}"
    part.mkdir(parents=True, exist_ok=True)
    if primary != "absent":
        row = {"job_id": job_id, "trial_id": trial_id}
        if primary is not None:
            row["primary_reward"] = float(primary)
        pq.write_table(pa.Table.from_pylist([row]), part / "trial_facts.parquet")
    if rewards is not None:
        schema = pa.schema(
            [
                ("job_id", pa.string()),
                ("trial_id", pa.string()),
                ("reward_name", pa.string()),
                ("reward_value", pa.float64()),
            ]
        )
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "job_id": job_id,
                        "trial_id": trial_id,
                        "reward_name": name,
                        "reward_value": float(value),
                    }
                    for name, value in rewards.items()
                ],
                schema=schema,
            ),
            part / "reward_facts.parquet",
        )
    pq.write_table(
        pa.Table.from_pylist(
            [{"job": "job", "trial": "trial", "copy_verdict": verdict, "stop_reason": stop}]
        ),
        part / "features.parquet",
    )


def _install_posture(monkeypatch, overrides):
    """Real posture first, per-trial fixture overrides second.

    Patches the function on the real module, so unknown keys keep their
    genuine closed-world values and the real cut-short mapping always runs.
    """
    from evallab.interpretation import trial_posture as posture_module

    real = posture_module.trial_posture

    def patched(*, repo_root, job_dir, trial_dir, result):
        row = dict(
            real(repo_root=repo_root, job_dir=job_dir, trial_dir=trial_dir, result=result)
        )
        row.update(overrides.get(Path(trial_dir).name, {}))
        return row

    monkeypatch.setattr("evallab.storage.trials.trial_posture", patched)


def _fetch(con, sql):
    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description]
    return columns, cursor.fetchall()


def _open(repo_root, roots, derived):
    return connect_trials(repo_root=repo_root, roots=roots, derived_root=derived)


def test_census_mixed_jobs_and_nested_roots(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    _install_posture(
        monkeypatch,
        {
            "canary-task__i1": {
                "infra": True,
                "infra_exception": "DaytonaNotFoundError",
                **{k: v for k, v in LEGIT_OK.items() if k != "infra"},
            }
        },
    )
    finished = _make_job(runs, "job-finished", job_id="job-finished", n_total=2)
    _make_trial(finished, "event-summary__s1", trial_id="tf-1", job_id="job-finished",
                trial_name="trial-one", task_name="event-summary")
    _make_trial(finished, "terminus-probe__s2", trial_id="tf-2", job_id="job-finished",
                trial_name="trial-two", task_name="terminus-probe")
    infra = _make_job(runs, "job-infra", job_id="job-infra", n_total=1)
    _make_trial(infra, "canary-task__i1", trial_id="ti-1", job_id="job-infra",
                trial_name="trial-infra", task_name="canary")
    partial = _make_job(runs, "job-partial", job_id="job-partial", n_total=0, finished=False)
    _make_trial(partial, "task-a__p1", trial_id="tp-1", job_id="job-partial",
                trial_name="trial-partial", task_name="task-a")
    _write_json(partial / "probe-task__u9" / "config.json", {"trial": "unfinished"})
    (partial / "hook-task__h1").mkdir(parents=True)
    (partial / "watch").mkdir(parents=True, exist_ok=True)
    (partial / "watch" / "hooks.jsonl").write_text(
        json.dumps({"trial": "hook-task__h1", "kind": "hook", "event": "start",
                    "at": "2026-09-22T11:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    nested = runs / "nested"
    deep = _make_job(nested, "job-deep", job_id="job-deep", n_total=1)
    _make_trial(deep, "deep-task__d1", trial_id="td-1", job_id="job-deep",
                trial_name="trial-deep", task_name="deep")
    running = runs / "2026-10-06__12-00-00"
    _write_json(
        running / "result.json",
        {"id": "job-running", "n_total_trials": 1, "stats": {"n_completed_trials": 0}},
    )
    _write_json(running / "config.json", {"job_id": "job-running"})
    _make_trial(running, "live-task__r1", trial_id="tr-1", job_id="job-running",
                trial_name="trial-running", task_name="live")
    sneaky = runs / ".executor" / "sneaky"
    _write_json(sneaky / "result.json", _trial_body("t-sneak", "j-sneak", "sneaky", "sneak"))
    link = runs / "job-link"
    try:
        link.symlink_to(finished, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    stray = finished / "stray"
    stray.mkdir()
    (stray / "result.json").write_text("{not json", encoding="utf-8")
    for job_id, trial_id, primary in (
        ("job-finished", "tf-1", 1.0),
        ("job-finished", "tf-2", 0.0),
        ("job-infra", "ti-1", None),
        ("job-deep", "td-1", 1.0),
        ("job-running", "tr-1", 1.0),
    ):
        _write_hot(
            derived, job_id, trial_id, primary=primary,
            rewards={"reward": primary if primary is not None else 0.0,
                     "integrity": 1.0, "reward_gated": primary if primary is not None else 0.0},
        )

    con, info = _open(tmp_path, [runs, nested], derived)
    try:
        assert len(info["unreadable_paths"]) == 1
        assert info["unreadable_paths"][0].endswith("stray/result.json")
        assert info["n_inventory"] == 8
        _, count_rows = _fetch(con, 'SELECT COUNT(*) FROM "trials"')
        assert count_rows == [(8,)]
        names = {row[0] for row in _fetch(con, 'SELECT "trial" FROM "trials"')[1]}
        assert names == {
            "trial-one", "trial-two", "trial-infra", "trial-partial",
            "probe-task__u9", "hook-task__h1", "trial-deep", "trial-running",
        }
        assert "2026-10-06__12-00-00" not in names
        unknown = _fetch(
            con,
            'SELECT "trial", "job_id", "trial_id", "projection_error" FROM "trials"'
            ' WHERE "trial_id" IS NULL ORDER BY "trial"',
        )[1]
        assert [(r[0], r[1], r[2]) for r in unknown] == [
            ("hook-task__h1", None, None),
            ("probe-task__u9", None, None),
        ]
        assert all("unknown native identity" in r[3] for r in unknown)
        infra_rows = _fetch(con, 'SELECT "infra" FROM "trials" WHERE "trial" = \'trial-infra\'')[1]
        assert infra_rows == [(True,)]
        finished_rows = _fetch(
            con,
            'SELECT COUNT(*) FROM "trials" WHERE "source_job_dir" = '
            f"'{finished.resolve()}'",
        )[1]
        assert finished_rows == [(2,)]
        sneak = _fetch(con, 'SELECT COUNT(*) FROM "trials" WHERE "trial" = \'sneaky\'')[1]
        assert sneak == [(0,)]
        partial_err = _fetch(
            con, 'SELECT "projection_error" FROM "trials" WHERE "trial" = \'trial-partial\''
        )[1]
        assert "missing projections" in partial_err[0][0]
        assert "backfill failed:" in partial_err[0][0]
    finally:
        con.close()


def test_native_pair_dedup_prefers_recorded_original(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    early = tmp_path / "a-runs"
    derived = tmp_path / "derived"
    _install_posture(monkeypatch, {})
    real = _make_job(runs, "job-real", job_id="job-real", n_total=1)
    real_trial = _make_trial(real, "t__a", trial_id="trial-1", job_id="job-real",
                             trial_name="t", task_name="k",
                             trial_uri=(real / "t__a").as_uri())
    for copy_parent, copy_job_id in (
        (runs / "job-agg", "job-agg-view"),
        (early / "job-real", "job-real"),
    ):
        copy_parent.mkdir(parents=True, exist_ok=True)
        _write_json(copy_parent / "result.json", {"id": copy_job_id})
        _write_json(copy_parent / "config.json", {"job_id": copy_job_id})
        dest = copy_parent / "t__a"
        dest.mkdir(parents=True, exist_ok=True)
        _write_json(dest / "result.json", json.loads((real_trial / "result.json").read_text()))
        _write_json(dest / "config.json", {"job_id": "job-real", "trial_name": "t"})
    _write_json(runs / "job-agg" / ".evallab-source.json", {"source": str(real)})
    other = _make_job(runs, "job-other", job_id="job-other", n_total=1)
    _make_trial(other, "t__a", trial_id="trial-9", job_id="job-other",
                trial_name="t", task_name="k")
    for job_id, trial_id in (("job-real", "trial-1"), ("job-other", "trial-9")):
        _write_hot(derived, job_id, trial_id, primary=1.0,
                   rewards={"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0})

    con, _ = _open(tmp_path, [runs, early], derived)
    try:
        rows = _fetch(
            con,
            'SELECT "job_id", "trial_id", "trial", "source_job_dir" FROM "trials"'
            ' ORDER BY "trial_id"',
        )[1]
        assert {(r[0], r[1], r[2]) for r in rows} == {
            ("job-other", "trial-9", "t"),
            ("job-real", "trial-1", "t"),
        }
        # The recorded trial_uri original wins over the aggregate overlay and
        # over the lexically-earlier cross-root copy alike.
        assert {row[0]: row[3] for row in rows}["job-real"] == str(real.resolve())
    finally:
        con.close()


def test_missing_projection_error_row_and_source_untouched(tmp_path):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    job = _make_job(runs, "job-no-result", job_id="job-no-result", n_total=0, finished=False)
    _make_trial(job, "task-a__u1", trial_id="tu-1", job_id="job-no-result",
                trial_name="trial-unfinished", task_name="task-a")

    con, _ = _open(tmp_path, [runs], derived)
    try:
        rows = _fetch(
            con,
            'SELECT "trial", "reward", "integrity", "projection_error" FROM "trials"',
        )[1]
        assert len(rows) == 1
        name, reward, integrity, error = rows[0]
        assert name == "trial-unfinished"
        assert reward is None and integrity is None
        assert not (job / "processed").exists()
    finally:
        con.close()


def test_existing_projections_reused(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    _install_posture(monkeypatch, {})

    def _forbid(*args, **kwargs):
        raise AssertionError("backfill must not run when projections are fresh")

    monkeypatch.setattr("evallab.storage.trials.process_job", _forbid)
    job = _make_job(runs, "job-calm", job_id="job-calm", n_total=1)
    _make_trial(job, "task__c1", trial_id="tc-1", job_id="job-calm",
                trial_name="trial-calm", task_name="task")
    _write_hot(derived, "job-calm", "tc-1", primary=0.0,
               rewards={"reward": 1.0, "integrity": 0.0, "reward_gated": 0.0},
               stop="task_complete", verdict="copied_fail")

    con, info = _open(tmp_path, [runs], derived)
    try:
        assert info["projection_errors"] == {}
        rows = _fetch(
            con,
            'SELECT "reward", "integrity", "reward_gated", "stop_reason",'
            ' "copy_verdict", "projection_error" FROM "trials"',
        )[1]
        assert rows == [(1.0, 0.0, 0.0, "task_complete", "copied_fail", None)]
    finally:
        con.close()


def test_reward_dimensions_exact_and_unmultiplied(tmp_path):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    job = _make_job(runs, "job-reward", job_id="job-reward", n_total=2)
    _make_trial(job, "a__1", trial_id="tr-1", job_id="job-reward",
                trial_name="trial-a", task_name="t")
    _make_trial(job, "b__2", trial_id="tr-2", job_id="job-reward",
                trial_name="trial-b", task_name="t")
    _write_hot(derived, "job-reward", "tr-1", primary=0.0,
               rewards={"reward": 1.0, "integrity": 0.0, "reward_gated": 0.0, "style": 0.5})
    _write_hot(derived, "job-reward", "tr-2", primary=0.5,
               rewards={"integrity": 1.0})

    con, _ = _open(tmp_path, [runs], derived)
    try:
        rows = _fetch(
            con,
            'SELECT "trial", "reward", "integrity", "reward_gated" FROM "trials"'
            ' ORDER BY "trial"',
        )[1]
        assert rows == [
            ("trial-a", 1.0, 0.0, 0.0),
            ("trial-b", 0.5, 1.0, None),
        ]
    finally:
        con.close()


def test_legit_clauses_unknown_false_independent(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    cases = [
        ("legit-ok__a", "ok", {}, "task_complete", "counted_pass",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, True),
        ("bad-model__a", "bad-model", {"model": "other/model"}, "task_complete",
         "counted_pass", {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("bad-harness__a", "bad-harness", {"harness": "evallab.other:Harness"},
         "task_complete", "counted_pass",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("egress-open__a", "egress-open", {"egress_lock": False}, "task_complete",
         "counted_pass", {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("ref-mismatch__a", "ref-mismatch", {"reference_profile_match": False},
         "task_complete", "counted_pass",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("infra-yes__a", "infra-yes",
         {"infra": True, "infra_exception": "DaytonaNotFoundError"}, "task_complete",
         "counted_pass", {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("cut-short__a", "cut-short", {}, "trial_budget_exhausted", "counted_pass",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("unknown-model__a", "unknown-model", {"model": None, "date": "not-a-date"},
         "task_complete", "counted_pass",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, False),
        ("integrity-zero__a", "integrity-zero", {}, "task_complete", "counted_pass",
         {"reward": 1.0, "integrity": 0.0, "reward_gated": 0.0}, True),
        ("copy-bad__a", "copy-bad", {}, "task_complete", "copied_fail",
         {"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0}, True),
        ("reward-zero__a", "reward-zero", {}, "task_complete", "counted_pass",
         {"reward": 0.0, "integrity": 1.0, "reward_gated": 0.0}, True),
    ]
    overrides = {}
    for dirname, _, fault, *_ in cases:
        row = dict(LEGIT_OK)
        row.update(fault)
        overrides[dirname] = row
    _install_posture(monkeypatch, overrides)
    job = _make_job(runs, "job-legit", job_id="job-legit", n_total=len(cases))
    for index, (dirname, trial_name, _, stop, verdict, rewards, _) in enumerate(cases):
        _make_trial(job, dirname, trial_id=f"trial-legit-{index}", job_id="job-legit",
                    trial_name=trial_name, task_name="t")
        _write_hot(derived, "job-legit", f"trial-legit-{index}", primary=rewards["reward"],
                   rewards=rewards, stop=stop, verdict=verdict)

    con, _ = _open(tmp_path, [runs], derived)
    try:
        rows = dict(
            (r[0], r[1])
            for r in _fetch(con, 'SELECT "trial", "legit" FROM "trials"')[1]
        )
        for _, trial_name, _, _, _, _, expected in cases:
            assert rows[trial_name] is expected, trial_name
        dates = dict(
            (r[0], r[1])
            for r in _fetch(con, 'SELECT "trial", "date" FROM "trials"')[1]
        )
        assert dates["ok"] is not None
        assert dates["unknown-model"] is None
    finally:
        con.close()


def test_backfill_success_then_warm_reuse(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    fixture = (
        Path(__file__).resolve().parent / "fixtures" / "terminus2" / "job-terminus2"
    )
    assert fixture.is_dir()
    shutil.copytree(fixture, runs / "job-terminus2")
    decoy = derived / "job_id=job-terminus2" / "trial_id=trial-terminus-continued"
    decoy.mkdir(parents=True, exist_ok=True)
    for table, payload in (
        ("trial_facts", [{"job_id": "job-terminus2", "trial_id": "trial-terminus-continued",
                          "primary_reward": 7.5}]),
        ("reward_facts", [{"job_id": "job-terminus2", "trial_id": "trial-terminus-continued",
                           "reward_name": "reward", "reward_value": 7.5}]),
        ("features", [{"job": "job", "trial": "trial",
                       "copy_verdict": "stale", "stop_reason": "stale"}]),
    ):
        path = decoy / f"{table}.parquet"
        pq.write_table(pa.Table.from_pylist(payload), path)
        old = 946684800.0
        os.utime(path, (old, old))

    import evallab.process_job as process_job_module

    calls: list[str] = []
    real_process_job = process_job_module.process_job

    def counting(job_dir, **kwargs):
        calls.append(str(job_dir))
        return real_process_job(job_dir, **kwargs)

    monkeypatch.setattr("evallab.storage.trials.process_job", counting)

    con, info = _open(tmp_path, [runs], derived)
    try:
        assert len(calls) == 1
        assert info["projection_errors"] == {}
        rewards = dict(
            (r[0], r[1])
            for r in _fetch(con, 'SELECT "trial", "reward" FROM "trials"')[1]
        )
        assert rewards["trial-continued"] == 1.0
        assert rewards["trial-summarized"] == 1.0
        assert rewards["trial-failed"] is None
        first = sorted(_fetch(con, 'SELECT * FROM "trials" ORDER BY "trial"')[1])
    finally:
        con.close()

    def _forbid(*args, **kwargs):
        raise AssertionError("warm query must reuse projections without backfill")

    monkeypatch.setattr("evallab.storage.trials.process_job", _forbid)
    con, info = _open(tmp_path, [runs], derived)
    try:
        assert info["n_backfilled"] == 0
        assert info["projection_errors"] == {}
        second = sorted(_fetch(con, 'SELECT * FROM "trials" ORDER BY "trial"')[1])
        assert second == first
    finally:
        con.close()


def test_revision_and_cold_projections_join_natively(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    _install_posture(monkeypatch, {})

    def _forbid(*args, **kwargs):
        raise AssertionError("lake projections must be reused without backfill")

    monkeypatch.setattr("evallab.storage.trials.process_job", _forbid)
    job = _make_job(runs, "job-lake", job_id="job-lake", n_total=2)
    _make_trial(job, "rev__a", trial_id="t-rev", job_id="job-lake",
                trial_name="trial-rev", task_name="t")
    _make_trial(job, "cold__b", trial_id="t-cold", job_id="job-lake",
                trial_name="trial-cold", task_name="t")
    revision = derived / "job_id=job-lake" / "revision_id=r1"
    revision.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"job_id": "job-lake", "trial_id": "t-rev", "primary_reward": 1.0}]
        ),
        revision / "trial_facts.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"job_id": "job-lake", "trial_id": "t-rev",
                 "reward_name": "reward", "reward_value": 1.0},
                {"job_id": "job-lake", "trial_id": "t-rev",
                 "reward_name": "integrity", "reward_value": 1.0},
                {"job_id": "job-lake", "trial_id": "t-rev",
                 "reward_name": "reward_gated", "reward_value": 1.0},
            ]
        ),
        revision / "reward_facts.parquet",
    )
    cold_day = derived / "compact" / "trial_facts" / "dt=2026-09-22"
    cold_day.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"job_id": "job-lake", "trial_id": "t-cold", "primary_reward": 0.0}]
        ),
        cold_day / "part-0.parquet",
    )
    cold_rewards = derived / "compact" / "reward_facts" / "dt=2026-09-22"
    cold_rewards.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"job_id": "job-lake", "trial_id": "t-cold",
                 "reward_name": "reward", "reward_value": 0.0},
                {"job_id": "job-lake", "trial_id": "t-cold",
                 "reward_name": "integrity", "reward_value": 1.0},
                {"job_id": "job-lake", "trial_id": "t-cold",
                 "reward_name": "reward_gated", "reward_value": 0.0},
            ]
        ),
        cold_rewards / "part-0.parquet",
    )
    _write_hot(derived, "job-lake", "t-rev", primary="absent", rewards=None,
               stop="task_complete", verdict="counted_pass")
    (derived / "job_id=job-lake" / "trial_id=t-rev" / "trial_facts.parquet").unlink(
        missing_ok=True
    )
    (derived / "job_id=job-lake" / "trial_id=t-rev" / "reward_facts.parquet").unlink(
        missing_ok=True
    )
    _write_hot(derived, "job-lake", "t-cold", primary="absent", rewards=None,
               stop="task_complete", verdict="counted_pass")
    (derived / "job_id=job-lake" / "trial_id=t-cold" / "trial_facts.parquet").unlink(
        missing_ok=True
    )
    (derived / "job_id=job-lake" / "trial_id=t-cold" / "reward_facts.parquet").unlink(
        missing_ok=True
    )

    con, info = _open(tmp_path, [runs], derived)
    try:
        assert info["projection_errors"] == {}
        rows = _fetch(
            con,
            'SELECT "trial", "reward", "integrity", "reward_gated", "stop_reason"'
            ' FROM "trials" ORDER BY "trial"',
        )[1]
        assert rows == [
            ("trial-cold", 0.0, 1.0, 0.0, "task_complete"),
            ("trial-rev", 1.0, 1.0, 1.0, "task_complete"),
        ]
    finally:
        con.close()


def test_empty_roots_selection_scans_nothing(tmp_path):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    job = _make_job(runs, "job-quiet", job_id="job-quiet", n_total=1)
    _make_trial(job, "t__q", trial_id="tq-1", job_id="job-quiet",
                trial_name="trial-quiet", task_name="t")

    con, info = connect_trials(repo_root=tmp_path, roots=[], derived_root=derived)
    try:
        assert info["n_rows"] == 0
        assert _fetch(con, 'SELECT COUNT(*) FROM "trials"')[1] == [(0,)]
    finally:
        con.close()


def test_command_summary_sql_safety_and_stderr(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"
    derived = tmp_path / "derived"
    _install_posture(
        monkeypatch,
        {
            "task__s1": dict(LEGIT_OK),
            "task__s2": {**LEGIT_OK, "model": "other/model"},
        },
    )
    job = _make_job(runs, "job-cli", job_id="job-cli", n_total=2)
    _make_trial(job, "task__s1", trial_id="tk-1", job_id="job-cli",
                trial_name="trial-1", task_name="t")
    _make_trial(job, "task__s2", trial_id="tk-2", job_id="job-cli",
                trial_name="trial-2", task_name="t")
    for trial_id in ("tk-1", "tk-2"):
        _write_hot(derived, "job-cli", trial_id, primary=1.0,
                   rewards={"reward": 1.0, "integrity": 1.0, "reward_gated": 1.0})
    stray = runs / "job-cli" / "stray"
    stray.mkdir()
    (stray / "result.json").write_text("{not json", encoding="utf-8")

    args = argparse.Namespace(sql=None, runs_dir=[str(runs)], derived_root=str(derived))
    assert command(args, tmp_path, harbor=None) == 0
    captured = capsys.readouterr()
    assert "total: 2" in captured.out
    assert "legit: 1" in captured.out
    assert "campaign HAR-178-probe: 2 (legit 1)" in captured.out
    assert "rows carry projection errors; 1 unreadable result.json files" in captured.err
    assert "stray/result.json" in captured.err

    args = argparse.Namespace(
        sql='SELECT "trial", "legit" FROM "trials" ORDER BY "trial"',
        runs_dir=[str(runs)],
        derived_root=str(derived),
    )
    assert command(args, tmp_path, harbor=None) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "trial\tlegit"
    assert out[1:] == ["trial-1\ttrue", "trial-2\tfalse"]

    for bad in ('DELETE FROM "trials"', 'COPY "trials" TO \'/tmp/trials-x.csv\'', ""):
        args = argparse.Namespace(sql=bad, runs_dir=[str(runs)], derived_root=str(derived))
        assert command(args, tmp_path, harbor=None) == 1
