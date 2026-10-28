"""Behaviour of the launchd, Modal and process sources of the Live Supervisor.

Every probe is injected: plist/state/log files live under ``tmp_path``, ``launchctl``, ``modal`` and
``ps`` are fakes, and the clock is the ``now`` argument. Log samples are small redacted copies of
real lines from ~/Library/Logs/evallab and the run-atlas / docent state files.
"""

from __future__ import annotations

import json
import os
import plistlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evallab.production.live_supervisor import guard, propose
from evallab.production.live_supervisor_services import (
    LaunchdSource,
    ModalSource,
    ProcessSource,
    modal_owner,
    parse_etime,
    parse_launchctl_list,
    parse_status_line,
)

NOW = datetime(2026, 10, 28, 20, 40, tzinfo=UTC)
PREFIX = "com.petermakhnatch.evallab."

SYNC_DIRTY = "2026-10-28T20:12:02Z Skipped: working tree dirty (2 files)."
SYNC_FETCH = ("2026-10-28T20:29:25Z Skipped: fetch from origin failed (error: cannot lock ref "
              "'refs/remotes/origin/main': is at 9698 but expected 1768 (unable to update local ref)).")
SYNC_OK = "2026-10-08T19:48:38Z Up to date with origin/main."
ATLAS_BLOCKED = json.dumps({
    "blocked_providers": ["google-antigravity", "openai-codex"], "budget_exhausted": True,
    "budget_skipped": 25, "dry_run": False, "errors": {"DailyTokenBudgetExceeded": 25}, "failed": 0,
    "pending": 1990, "selected": 25, "tokens_today": 960012, "written": 0}, sort_keys=True)
DOCENT_BLOCKED = json.dumps({
    "at": "2026-10-28T20:28:05.729754+00:00", "completed": 0, "model_approvals": 0, "pending": 778,
    "reason": "hosted_free_allowance_exhausted", "result_lines": 0, "status": "blocked"}, sort_keys=True)
DOCENT_OK = json.dumps({"at": "2026-10-28T19:36:30+00:00", "status": "idle", "pending": 0})


def touch(path: Path, text: str, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    moment = (NOW - timedelta(seconds=age)).timestamp()
    os.utime(path, (moment, moment))
    return path


def write_plist(agents: Path, suffix: str, *, out: Path | None = None, err: Path | None = None,
                interval: int | None = None, calendar: bool = False, name: str | None = None,
                label: str | None = None) -> None:
    body: dict[str, object] = {"Label": label or PREFIX + suffix}
    if out:
        body["StandardOutPath"] = str(out)
    if err:
        body["StandardErrorPath"] = str(err)
    if interval:
        body["StartInterval"] = interval
    if calendar:
        body["StartCalendarInterval"] = {"Hour": 3}
    agents.mkdir(parents=True, exist_ok=True)
    (agents / (name or f"{PREFIX}{suffix}.plist")).write_bytes(plistlib.dumps(body))


def launchctl(*suffixes: str):
    rows = "PID\tStatus\tLabel\n" + "".join(f"-\t0\t{PREFIX}{s}\n" for s in suffixes)

    def run(argv: Sequence[str], timeout: float) -> str:
        assert list(argv) == ["launchctl", "list"]
        return rows

    return run


def service(tmp_path: Path, *, suffix: str = "svc", loaded: bool = True, runner=None) -> LaunchdSource:
    return LaunchdSource(launch_agents_dir=tmp_path / "agents", state_dir=tmp_path / "state",
                         runner=runner or launchctl(*([suffix] if loaded else [])))


def one(source, now=NOW):
    observations = list(source.observe(now))
    assert len(observations) == 1
    return observations[0]


# --------------------------------------------------------------------------- launchd: repetition


def lines(*rows: str) -> str:
    return "\n".join(rows) + "\n"


@pytest.mark.parametrize(("cycles", "fires", "score"), [(5, False, None), (6, True, 1.0), (9, True, 1.5)])
def test_repetition_boundary_on_json_status(tmp_path: Path, cycles: int, fires: bool, score: float | None):
    out = touch(tmp_path / "svc.out", lines(*[DOCENT_BLOCKED] * cycles))
    write_plist(tmp_path / "agents", "svc", out=out, interval=600)
    source = service(tmp_path)
    observation = one(source)
    hits = source.triggers(observation)
    assert bool(hits) is fires
    if fires:
        (hit,) = hits
        assert (hit.trigger, hit.kind, hit.score) == ("repetition", "infra", score)
        assert "hosted_free_allowance_exhausted" in hit.detail
        assert hit.evidence_steps and all(0 <= s < len(observation.events) for s in hit.evidence_steps)


def test_ok_cycle_resets_the_run(tmp_path: Path):
    out = touch(tmp_path / "svc.out", lines(*[DOCENT_BLOCKED] * 8, DOCENT_OK, *[DOCENT_BLOCKED] * 5))
    write_plist(tmp_path / "agents", "svc", out=out, interval=600)
    source = service(tmp_path)
    assert source.triggers(one(source)) == []


def test_changed_status_class_breaks_the_run(tmp_path: Path):
    mixed = [json.dumps({"status": "blocked"})] * 4 + [json.dumps({"status": "error"})] * 5
    out = touch(tmp_path / "svc.out", lines(*mixed))
    write_plist(tmp_path / "agents", "svc", out=out, interval=600)
    source = service(tmp_path)
    observation = one(source)
    # only the five trailing 'error' cycles count: the 'blocked' cycles are a different status
    assert observation.metrics["status_run"] == 5
    assert source.triggers(observation) == []


def test_run_atlas_budget_exhaustion_with_nothing_written_is_non_ok(tmp_path: Path):
    out = touch(tmp_path / "atlas.out", lines(*[ATLAS_BLOCKED] * 6))
    write_plist(tmp_path / "agents", "run-atlas-watch", out=out, interval=300)
    source = service(tmp_path, suffix="run-atlas-watch")
    (hit,) = source.triggers(one(source))
    assert hit.trigger == "repetition"
    assert "DailyTokenBudgetExceeded" in hit.detail and "budget_exhausted" in hit.detail


def test_budget_exhausted_that_still_wrote_is_ok_and_empty_errors_are_ok():
    assert parse_status_line(json.dumps({"budget_exhausted": True, "written": 4})).signature is None
    assert parse_status_line(json.dumps({"errors": {}, "written": 0})).signature is None
    assert parse_status_line(json.dumps({"errors": {"X": 1}})).signature == "errors=X"
    assert parse_status_line(json.dumps({"ok": False})).signature == "ok=false"
    assert parse_status_line(json.dumps({"status": "idle", "ok": True})).signature is None


def test_sync_primary_text_status_lives_in_stderr_and_fetch_failure_extends_the_dirty_run(tmp_path: Path):
    rows = [SYNC_OK] + [SYNC_DIRTY] * 5 + [SYNC_FETCH]
    err = touch(tmp_path / "sync.err", lines(*rows))
    out = touch(tmp_path / "sync.out", "")
    write_plist(tmp_path / "agents", "sync-primary", out=out, err=err, interval=300)
    source = service(tmp_path, suffix="sync-primary")
    observation = one(source)
    assert observation.metrics["status_source"] == "sync.err"
    assert observation.metrics["status_run"] == 6
    (hit,) = source.triggers(observation)
    assert hit.trigger == "repetition" and "Skipped" in hit.detail
    assert [e.text.split()[1] for e in observation.events[-2:]] == ["Skipped:", "Skipped:"]
    assert observation.events[-1].at == datetime(2026, 10, 28, 20, 29, 25, tzinfo=UTC)


def test_events_are_the_last_twelve_status_lines(tmp_path: Path):
    rows = [json.dumps({"status": "idle", "n": n}) for n in range(30)]
    out = touch(tmp_path / "svc.out", lines(*rows))
    write_plist(tmp_path / "agents", "svc", out=out, interval=600)
    observation = one(service(tmp_path))
    assert len(observation.events) == 12
    assert '"n": 29' in observation.events[-1].text and '"n": 18' in observation.events[0].text
    assert [e.index for e in observation.events] == list(range(12))


def test_known_status_file_extends_empty_history_and_trace_store_pause_is_non_ok(tmp_path: Path):
    state = tmp_path / "state"
    touch(state / "trace-tools" / "docent-watch-last.json", json.dumps(json.loads(DOCENT_BLOCKED), indent=2))
    write_plist(tmp_path / "agents", "docent-watch", out=tmp_path / "missing.log", interval=600)
    source = service(tmp_path, suffix="docent-watch")
    observation = one(source)
    assert observation.metrics["status_signature"] == "status=blocked reason=hosted_free_allowance_exhausted"
    assert observation.metrics["status_run"] == 1 and source.triggers(observation) == []

    paused = {"targets": {"docent": {"at": "2026-10-28T14:57:01+00:00", "error_type": None, "paused": True,
                                     "reason": "quota"}}}
    assert parse_status_line(json.dumps(paused)).signature == "docent:paused reason=quota"


# --------------------------------------------------------------------------- launchd: silence


@pytest.mark.parametrize(("gap", "fires", "score"), [(899, False, None), (900, True, 1.0), (1800, True, 2.0)])
def test_silence_boundary_is_three_intervals(tmp_path: Path, gap: float, fires: bool, score: float | None):
    out = touch(tmp_path / "svc.out", lines(DOCENT_OK), age=gap)
    write_plist(tmp_path / "agents", "svc", out=out, interval=300)
    source = service(tmp_path)
    hits = source.triggers(one(source))
    assert bool(hits) is fires
    if fires:
        (hit,) = hits
        assert (hit.trigger, hit.kind, hit.score) == ("silence", "latency", score)


def test_silence_detail_names_the_running_job_pid(tmp_path: Path):
    out = touch(tmp_path / "svc.out", lines(DOCENT_OK), age=1088)
    write_plist(tmp_path / "agents", "svc", out=out, interval=300)
    source = service(tmp_path, runner=lambda argv, timeout: f"73351\t0\t{PREFIX}svc\n")
    observation = one(source)
    assert observation.metrics["pid"] == 73351
    (hit,) = source.triggers(observation)
    assert "job running pid 73351" in hit.detail and hit.score > 1.2


def test_silence_uses_the_freshest_of_stdout_stderr_and_status_files(tmp_path: Path):
    out = touch(tmp_path / "svc.out", lines(DOCENT_OK), age=5000)
    err = touch(tmp_path / "svc.err", "INFO tick\n", age=10)
    write_plist(tmp_path / "agents", "svc", out=out, err=err, interval=300)
    source = service(tmp_path)
    assert source.triggers(one(source)) == []


def test_silence_needs_a_loaded_interval_service_with_known_write_times(tmp_path: Path):
    agents = tmp_path / "agents"
    old = touch(tmp_path / "old.out", "x\n", age=10**6)
    write_plist(agents, "unloaded", out=old, interval=300)
    write_plist(agents, "calendar", out=old, calendar=True)
    write_plist(agents, "nologs", interval=300)
    source = LaunchdSource(launch_agents_dir=agents, state_dir=tmp_path / "state",
                           runner=launchctl("calendar", "nologs"))
    by_label = {o.subject.label: o for o in source.observe(NOW)}
    assert by_label["unloaded"].terminal and source.triggers(by_label["unloaded"]) == []
    assert source.triggers(by_label["calendar"]) == []
    assert source.triggers(by_label["nologs"]) == []
    assert by_label["nologs"].metrics["silent_seconds"] is None


def test_unreadable_launchctl_leaves_loaded_unknown_not_false(tmp_path: Path):
    def broken(argv: Sequence[str], timeout: float) -> str:
        raise RuntimeError("launchctl unavailable")

    out = touch(tmp_path / "svc.out", lines(*[DOCENT_BLOCKED] * 6))
    write_plist(tmp_path / "agents", "svc", out=out, interval=600)
    source = service(tmp_path, runner=broken)
    observation = one(source)
    assert observation.metrics["loaded"] is None and not observation.terminal
    assert [h.trigger for h in source.triggers(observation)] == ["repetition"]


# --------------------------------------------------------------------------- launchd: stderr


TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/x/watch.py", line 12, in main',
    "docent.errors.AuthenticationError: bad key",
]


def test_recent_stderr_traceback_fires_error_signature_infra(tmp_path: Path):
    err = touch(tmp_path / "svc.err", lines("16:30:12 [INFO] docent.sdk._base: Logged in", *TRACEBACK), age=60)
    write_plist(tmp_path / "agents", "svc", err=err, interval=300)
    source = service(tmp_path)
    observation = one(source)
    (hit,) = source.triggers(observation)
    assert (hit.trigger, hit.kind) == ("error_signature", "infra")
    assert observation.events[-1].source == "stderr"
    assert all(observation.events[s].source == "stderr" for s in hit.evidence_steps)


def test_stale_stderr_and_lowercase_error_prose_do_not_fire(tmp_path: Path):
    stale = touch(tmp_path / "stale.err", lines(*TRACEBACK), age=601)
    prose = touch(tmp_path / "prose.err", lines(SYNC_FETCH, "16:30:12 [INFO] Logged in"), age=10)
    write_plist(tmp_path / "agents", "stale", err=stale, interval=300)
    write_plist(tmp_path / "agents", "prose", err=prose, interval=300)
    source = LaunchdSource(launch_agents_dir=tmp_path / "agents", state_dir=tmp_path / "state",
                           runner=launchctl("stale", "prose"))
    for observation in source.observe(NOW):
        assert source.triggers(observation) == [], observation.subject.label


# --------------------------------------------------------------------------- launchd: discovery


def test_discovery_skips_backups_foreign_labels_and_broken_plists_and_never_kills(tmp_path: Path):
    agents = tmp_path / "agents"
    write_plist(agents, "keep", out=touch(tmp_path / "k.out", "x\n"))
    write_plist(agents, "nightly", name=f"{PREFIX}nightly.plist.bak-har199")
    write_plist(agents, "x", name=f"{PREFIX}old.bak-1.plist")
    write_plist(agents, "other", name=f"{PREFIX}foreign.plist", label="com.other.thing")
    (agents / f"{PREFIX}broken.plist").write_text("not a plist")
    write_plist(agents, "x", name="unrelated.plist")
    source = LaunchdSource(launch_agents_dir=agents, state_dir=tmp_path / "state", runner=launchctl("keep"))
    (observation,) = list(source.observe(NOW))
    subject = observation.subject
    assert (subject.kind, subject.id, subject.owner, subject.killable) == (
        "launchd_service", PREFIX + "keep", "lab", False)
    assert subject.label == "keep"


def test_service_findings_are_alert_research_never_a_kill(tmp_path: Path):
    err = touch(tmp_path / "svc.err", lines(*TRACEBACK), age=5)
    write_plist(tmp_path / "agents", "svc", err=err, interval=300)
    source = service(tmp_path)
    observation = one(source)
    decision = guard(observation.subject, propose(observation.subject, source.triggers(observation)))
    assert decision.action == "alert_research"


def test_parse_launchctl_list_rows():
    text = "PID\tStatus\tLabel\n73351\t0\tcom.x.a\n-\t78\tcom.x.b\n-\t-9\tcom.x.c\nbad line\n"
    assert parse_launchctl_list(text) == {"com.x.a": (73351, 0), "com.x.b": (None, 78), "com.x.c": (None, -9)}


# --------------------------------------------------------------------------- modal


def app(app_id="ap-1", description="ts14-native-repairs-x", tasks="0", state="deployed",
        created="2026-10-28 18:33:58-04:00", stopped=None) -> dict[str, object]:
    return {"app_id": app_id, "description": description, "state": state, "tasks": tasks,
            "created_at": created, "stopped_at": stopped}


def created_ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def modal(apps: list[dict[str, object]], logs=None, **kwargs) -> ModalSource:
    def lister(argv: Sequence[str], timeout: float) -> str:
        assert list(argv) == ["keys", "run", "--", "modal", "app", "list", "--json"]
        return json.dumps(apps)

    return ModalSource(runner=lister, logs_runner=logs, **kwargs)


@pytest.mark.parametrize(("description", "owner", "killable"), [
    ("evallab-cloud-production", "production", False),
    ("evallab-envcheck-repair", "lab", True),
    ("ts14-native-repairs-20261028-p1351-original", "lab", True),
    ("sb1-seeker-b-pilot", "lab", True),
    ("fl-verify-20261027-native", "lab", True),
    ("mimo-clean-census", "lab", True),
    ("harbor-anything", "lab", True),
    ("eval-rollout-probe", "lab", True),
    ("envcheck-repair-v6", "lab", True),
    ("cache-v4-repro", "lab", True),
    ("mtime-normalize-v2-repro", "lab", True),
    ("2026-10-21-reward-seeker", "lab", True),
    ("qa-sft-pilot", "lab", True),
    ("kev-rh-1", "lab", True),
    ("__harbor__", "lab", True),
    ("peters-personal-app", "other", False),
    ("", "other", False),
])
def test_allow_list_ownership_and_production_is_never_killable(description, owner, killable):
    assert modal_owner(description) == owner
    (observation,) = list(modal([app(description=description)]).observe(NOW))
    assert (observation.subject.owner, observation.subject.killable) == (owner, killable)
    assert observation.subject.kind == "modal_app" and observation.subject.label == description


@pytest.mark.parametrize(("age", "fires", "score"), [(7199, False, None), (7200, True, 1.0), (14400, True, 2.0)])
def test_wall_time_boundary_for_apps_with_running_tasks(age, fires, score):
    source = modal([app(tasks="2", created=created_ago(age))])
    hits = source.triggers(next(iter(source.observe(NOW))))
    assert bool(hits) is fires
    if fires:
        (hit,) = hits
        assert (hit.trigger, hit.kind, hit.score) == ("wall_time", "runaway", score)


def test_deployed_apps_without_tasks_never_trigger_but_are_counted():
    source = modal([app("ap-a", tasks="0", created=created_ago(10**6)), app("ap-b", tasks="0"),
                    app("ap-c", tasks="1", created=created_ago(60))])
    observations = list(source.observe(NOW))
    assert len(observations) == 3
    assert [source.triggers(o) for o in observations] == [[], [], []]
    assert {o.metrics["deployed_idle_apps"] for o in observations} == {2}
    assert {o.metrics["apps_running"] for o in observations} == {1}
    assert {o.metrics["apps_total"] for o in observations} == {3}


def test_production_deployed_service_is_exempt_from_wall_time_but_not_from_log_errors():
    logs = lambda argv, timeout: "2026 INFO ok\nserved_route_mismatch: expected a got b\n"  # noqa: E731
    source = modal([app(description="evallab-cloud-production", tasks="3", created=created_ago(10**6))], logs=logs)
    observation = next(iter(source.observe(NOW)))
    (hit,) = source.triggers(observation)
    assert hit.trigger == "error_signature"
    decision = guard(observation.subject, propose(observation.subject, [hit]))
    assert decision.action == "alert_research"  # production is never killed


def test_lab_app_log_error_proposes_kill_and_other_app_alerts_research():
    logs = lambda argv, timeout: "ImageBuildError: build failed\n"  # noqa: E731
    source = modal([app("ap-lab", "sb1-x", tasks="1"), app("ap-other", "personal", tasks="1")], logs=logs)
    decisions = {}
    for observation in source.observe(NOW):
        hits = source.triggers(observation)
        decisions[observation.subject.id] = guard(observation.subject, propose(observation.subject, hits)).action
    assert decisions == {"ap-lab": "kill_and_mark_infra", "ap-other": "alert_research"}


@pytest.mark.parametrize(("line", "name"), [
    ("ImageBuildError: failed", "ImageBuildError"),
    ("served_route_mismatch route=a", "served_route_mismatch"),
    ("Sandbox sb-123abc not found", "sandbox_gone"),
    ("sandbox is already shut down", "sandbox_gone"),
])
def test_modal_log_signatures(line, name):
    source = modal([app(tasks="1")], logs=lambda argv, timeout: f"noise\n{line}\nmore noise\n")
    observation = next(iter(source.observe(NOW)))
    assert observation.metrics["log_signatures"] == [name]
    (hit,) = source.triggers(observation)
    assert (hit.trigger, hit.kind) == ("error_signature", "infra")
    assert name in observation.events[hit.evidence_steps[0]].text


def test_logs_are_read_only_for_running_apps_bounded_and_failures_are_contained():
    calls: list[tuple[Sequence[str], float]] = []

    def logs(argv: Sequence[str], timeout: float) -> str:
        calls.append((argv, timeout))
        if argv[-1] == "ap-2":
            raise TimeoutError("too slow")
        return "all good\n"

    apps = [app("ap-0", tasks="0"), app("ap-1", tasks="1"), app("ap-2", tasks="1"), app("ap-3", tasks="1")]
    source = modal(apps, logs=logs, max_log_scans=2, logs_timeout=3.0)
    observations = {o.subject.id: o for o in source.observe(NOW)}
    assert [c[0][-1] for c in calls] == ["ap-1", "ap-2"]
    assert all(c[1] == 3.0 and c[0][:5] == ("keys", "run", "--", "modal", "app") for c in calls)
    assert "TimeoutError" in observations["ap-2"].metrics["logs_error"]
    assert source.triggers(observations["ap-2"]) == [] and source.triggers(observations["ap-3"]) == []


def test_stopped_apps_are_terminal_and_a_bad_listing_raises():
    stopped = modal([app("ap-s", tasks="0", state="stopped", stopped="2026-10-28 19:00:00-04:00")])
    assert next(iter(stopped.observe(NOW))).terminal
    broken = ModalSource(runner=lambda argv, timeout: '{"error": "nope"}')
    with pytest.raises(TypeError):
        list(broken.observe(NOW))
    garbage = ModalSource(runner=lambda argv, timeout: "not json")
    with pytest.raises(ValueError):
        list(garbage.observe(NOW))


# --------------------------------------------------------------------------- processes

EVAL_LAB = "/Users/p/Developer/eval-lab/.worktrees/recalc-task-20261028"
PS_HEADER = "  PID  PPID     ELAPSED COMMAND\n"
PS_LIVE = PS_HEADER + "\n".join([
    # harbor run (python wrapper) -> docker compose -> compose plugin: one local trial
    f" 75826 57335       05:38 {EVAL_LAB}/.venv/bin/python3 {EVAL_LAB}/.venv/bin/harbor run --path "
    f"{EVAL_LAB}/runs/.exec-stage/hack-1 --agent oracle --env evallab.harbor_host_isolation:HostIsolatedDockerEnvironment",
    f" 77563 75826       01:20 docker compose --project-name hack-1__verifier__trial --project-directory "
    f"{EVAL_LAB}/runs/.exec-stage/hack-1 up -d",
    f" 77565 77563       01:20 /Users/p/.docker/cli-plugins/docker-compose compose --project-name hack-1 "
    f"--project-directory {EVAL_LAB}/runs/.exec-stage/hack-1",
    # a non-lab docker compose and a remote harbor run: not local lab docker
    "  5000     1       10:00 docker compose --project-directory /Users/p/Other/app up",
    f"  5001  4000       10:00 {EVAL_LAB}/.venv/bin/harbor run --path {EVAL_LAB}/x --env modal",
    # controller launched with uv, python child: one subject
    " 57330  2164       22:17 uv run evallab hack run library/tasks/sheet-recalc --execute",
    f" 57335 57330       22:17 {EVAL_LAB}/.venv/bin/python {EVAL_LAB}/.venv/bin/evallab hack run library/tasks/sheet-recalc",
    # launchd server, omp fleet and unrelated processes
    " 1068     1  3-14:22:21 /Users/p/Library/Application Support/evallab/results-viewer/venv/bin/python "
    "/Users/p/Library/Application Support/evallab/results-viewer/venv/bin/evallab results-viewer --root /x",
    " 2150  1840  3-14:24:26 omp --resume=/Users/p/.omp/agent/sessions/x.jsonl",
    " 2235     1  3-14:24:13 /Users/p/.local/bin/omp __omp_worker_daemon_broker",
    "  412     1  3-14:24:45 /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow console",
]) + "\n"


def processes(output: str) -> ProcessSource:
    def ps(argv: Sequence[str], timeout: float) -> str:
        assert list(argv) == ["ps", "-axo", "pid,ppid,etime,command"]
        return output

    return ProcessSource(runner=ps)


def test_local_docker_trial_is_one_subject_with_the_contract_detail():
    source = processes(PS_LIVE)
    observations = {o.subject.id: o for o in source.observe(NOW)}
    assert set(observations) == {"75826", "57330"}  # outermost only; non-lab, remote, omp, services ignored
    (hit,) = source.triggers(observations["75826"])
    assert (hit.trigger, hit.kind, hit.detail) == (
        "error_signature", "other", "local docker trial: contract sets EVALLAB_DISABLE_LOCAL_DOCKER=1")
    assert source.triggers(observations["57330"]) == []  # a young controller is quiet
    for observation in observations.values():
        assert (observation.subject.kind, observation.subject.owner, observation.subject.killable) == (
            "local_process", "lab", False)


def test_docker_compose_child_alone_is_reported_when_its_parent_is_not_a_lab_process():
    ps = PS_HEADER + f" 9 1 00:30 docker compose --project-directory {EVAL_LAB}/runs/x up\n"
    (observation,) = list(processes(ps).observe(NOW))
    assert observation.subject.id == "9" and processes(ps).triggers(observation)


@pytest.mark.parametrize(("etime", "fires", "score"), [
    ("05:59:59", False, None), ("06:00:00", True, 1.0), ("12:00:00", True, 2.0), ("1-00:00:00", True, 4.0)])
def test_controller_wall_time_boundary_is_six_hours(etime, fires, score):
    ps = PS_HEADER + f" 8 1 {etime} {EVAL_LAB}/.venv/bin/python -B {EVAL_LAB}/controller.py --registration r\n"
    source = processes(ps)
    (observation,) = list(source.observe(NOW))
    hits = source.triggers(observation)
    assert bool(hits) is fires
    if fires:
        (hit,) = hits
        assert (hit.trigger, hit.kind, hit.score) == ("wall_time", "runaway", score)


def test_long_running_launchd_servers_and_omp_processes_are_never_candidates():
    ps = PS_HEADER + "\n".join([
        " 1068 1 3-14:22:21 /Users/p/Library/Application Support/evallab/results-viewer/venv/bin/evallab results-viewer",
        " 1057 1 3-14:22:21 /x/.venv/bin/python -m streamlit run evallab dashboard",
        f" 2150 1 3-14:22:21 omp -p run evallab hack --project-directory {EVAL_LAB} docker compose",
        f" 2151 1 3-14:22:21 /Users/p/.local/bin/omp {EVAL_LAB}/controller.py",
    ]) + "\n"
    assert list(processes(ps).observe(NOW)) == []


def test_local_docker_trial_older_than_six_hours_fires_both_triggers_only_if_a_controller():
    ps = PS_HEADER + f" 7 1 07:00:00 docker compose --project-directory {EVAL_LAB}/x up\n"
    source = processes(ps)
    (observation,) = list(source.observe(NOW))
    assert [h.trigger for h in source.triggers(observation)] == ["error_signature"]


@pytest.mark.parametrize(("raw", "seconds"), [
    ("00:05", 5), ("05:38", 338), ("1:02:03", 3723), ("3-14:22:21", 3 * 86400 + 14 * 3600 + 22 * 60 + 21),
    ("nonsense", None)])
def test_parse_etime(raw, seconds):
    assert parse_etime(raw) == seconds


def test_unparseable_ps_rows_are_skipped():
    ps = PS_HEADER + "garbage\n  abc 1 00:01 foo\n" + f" 8 1 00:01 docker compose --project-directory {EVAL_LAB}/x\n"
    assert [o.subject.id for o in processes(ps).observe(NOW)] == ["8"]
