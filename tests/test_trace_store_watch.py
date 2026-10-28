from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from evallab.trace_store import watch


def make_report(**over: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "target": "laminar", "selected": 2, "synced": 2, "unchanged": 0, "rejected": [],
        "sources": {"own": 2}, "payload_bytes": 10, "sent_bytes": 10, "requests": 2,
        "wall_seconds": 0.1, "stopped": None, "cursor": None,
    }
    report.update(over)
    return report


class Guard:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.safe = False
        self.checks = 0

    def check_signals(self) -> None:
        self.checks += 1
        if self.error:
            raise self.error
        self.safe = True


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.db = tmp_path / "db.sqlite"
        self.db.write_bytes(b"one")
        self.root = tmp_path / "new" / "trace-store"
        self.sync_calls: list[tuple[str, dict[str, Any]]] = []
        self.spend: list[tuple[str, dict[str, Any]]] = []
        self.guards: list[Guard] = []
        self.guard_error: Exception | None = None
        self.factory_error: Exception | None = None
        self.replies: dict[str, Any] = {}
        monkeypatch.setattr(watch, "run_sync", self._sync)
        monkeypatch.setattr(watch, "new_laminar_target", self._factory)
        monkeypatch.setattr(watch, "record_spend", lambda route, **d: self.spend.append((route, d)))

    def _sync(self, target: str, **kwargs: Any) -> dict[str, Any]:
        self.sync_calls.append((target, kwargs))
        reply = self.replies.get(target)
        return reply(kwargs) if reply else make_report(target=target)

    def _factory(self) -> Guard:
        if self.factory_error:
            raise self.factory_error
        guard = Guard(self.guard_error)
        self.guards.append(guard)
        return guard

    def tick(self, **kwargs: Any) -> dict[str, Any]:
        return watch.watch_once(repo_root=self.repo, db_path=self.db, root=self.root, **kwargs)

    def hosted(self) -> int:
        return len(self.sync_calls) + len(self.guards) + len(self.spend)

    def targets_called(self) -> list[str]:
        return [target for target, _ in self.sync_calls]


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_new_db_syncs_both_then_unchanged_makes_zero_hosted_calls(h: Harness) -> None:
    first = h.tick()
    assert first["ok"] is True
    assert h.targets_called() == ["laminar", "docent"]
    assert [s["status"] for s in first["targets"].values()] == ["synced", "synced"]
    before = h.hosted()
    second = h.tick()
    assert second["ok"] is True
    assert h.hosted() == before
    assert {s["status"] for s in second["targets"].values()} == {"unchanged"}


def test_wal_change_triggers_but_shm_does_not(h: Harness) -> None:
    h.tick()
    shm = Path(str(h.db) + "-shm")
    shm.write_bytes(b"x" * 64)
    h.tick()
    assert len(h.sync_calls) == 2
    Path(str(h.db) + "-wal").write_bytes(b"wal")
    h.tick()
    assert h.targets_called() == ["laminar", "docent"] * 2
    shm.write_bytes(b"y" * 4096)
    h.tick()
    assert len(h.sync_calls) == 4


def test_db_change_triggers_again(h: Harness) -> None:
    h.tick()
    h.db.write_bytes(b"one-but-longer")
    h.tick()
    assert len(h.sync_calls) == 4


def test_runtime_source_change_reconsiders_earlier_pass(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.tick()
    monkeypatch.setattr(watch, "RUNTIME_SOURCE", "/opt/runtimes/v2/watch.py")
    h.tick()
    assert len(h.sync_calls) == 4


def test_success_records_pre_pass_signature_so_mid_pass_write_retriggers(h: Harness) -> None:
    def writes_during_pass(kwargs: dict[str, Any]) -> dict[str, Any]:
        h.db.write_bytes(b"one-plus-a-mid-pass-write")
        return make_report()

    h.replies["laminar"] = writes_during_pass
    h.tick()
    h.replies.clear()
    out = h.tick()
    assert h.targets_called() == ["laminar", "docent", "laminar"]
    assert out["targets"]["laminar"]["status"] == "synced"
    assert out["targets"]["docent"]["status"] == "unchanged"


def test_sync_is_the_only_uploader_and_gates_are_not_overridden(h: Harness) -> None:
    h.tick()
    laminar_kwargs = h.sync_calls[0][1]
    docent_kwargs = h.sync_calls[1][1]
    assert set(laminar_kwargs) == {"repo_root", "db_path", "root", "uploader"}
    assert set(docent_kwargs) == set(laminar_kwargs)
    assert laminar_kwargs["repo_root"] == h.repo
    assert laminar_kwargs["db_path"] == h.db
    assert laminar_kwargs["root"] == h.root
    assert laminar_kwargs["uploader"] is h.guards[0]
    assert h.guards[0].safe is True
    assert docent_kwargs["uploader"] is None
    assert len(h.guards) == 1


def test_guard_runs_before_any_sync_and_is_logged_as_zero_dollar_get(h: Harness) -> None:
    h.tick()
    assert h.spend == [(
        "laminar-signals-check",
        {"requests": 1, "ok": True, "error_type": None,
         "basis": "read-only GET /v1/signals guard; no model or paid Signal calls"},
    )]


def test_active_signals_block_both_targets_and_latch_without_leaking(h: Harness) -> None:
    h.guard_error = ValueError("Pause Laminar Signal before sync: sk-secret-name")
    out = h.tick()
    assert h.sync_calls == []
    assert out["ok"] is False
    assert {s["status"] for s in out["targets"].values()} == {"refused"}
    assert all(s["error_type"] == "ValueError" for s in out["targets"].values())
    assert "sk-secret-name" not in json.dumps(out)
    assert "sk-secret-name" not in (h.root / "watch-state.json").read_text()
    assert h.spend[0][1]["requests"] == 1 and h.spend[0][1]["ok"] is False
    # Later ticks repeat nothing, even after the DB changed.
    h.db.write_bytes(b"changed-db")
    before = h.hosted()
    again = h.tick()
    assert h.hosted() == before
    assert {s["status"] for s in again["targets"].values()} == {"paused"}
    assert again["ok"] is False


def test_guard_without_credentials_logs_no_invented_request(h: Harness) -> None:
    h.factory_error = ValueError("LMNR_PROJECT_API_KEY is required")
    out = h.tick()
    assert h.sync_calls == []
    assert h.spend[0][1]["requests"] == 0 and h.spend[0][1]["ok"] is False
    assert {s["status"] for s in out["targets"].values()} == {"refused"}


def test_unverified_guard_is_refusal(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    class Lax(Guard):
        def check_signals(self) -> None:
            self.checks += 1

    monkeypatch.setattr(watch, "new_laminar_target", lambda: Lax(None))
    out = h.tick()
    assert h.sync_calls == []
    assert out["ok"] is False


def test_quota_stop_latches_only_that_target_and_other_proceeds(h: Harness) -> None:
    h.replies["docent"] = lambda kw: make_report(
        stopped="HTTP 429: quota/access/payload limit; upload paused", cursor="run-9", synced=1,
    )
    out = h.tick()
    assert out["ok"] is False
    assert out["targets"]["laminar"]["status"] == "synced"
    assert out["targets"]["docent"]["status"] == "stopped"
    assert out["targets"]["docent"]["cursor"] == "run-9"
    h.replies.clear()
    h.db.write_bytes(b"changed-db")
    again = h.tick()
    assert h.targets_called() == ["laminar", "docent", "laminar"]
    assert again["targets"]["laminar"]["status"] == "synced"
    assert again["targets"]["docent"]["status"] == "paused"
    assert again["targets"]["docent"]["cursor"] == "run-9"
    assert again["ok"] is False


def test_laminar_stop_does_not_prevent_docent_attempt(h: Harness) -> None:
    h.replies["laminar"] = lambda kw: make_report(stopped="x", cursor="r1")
    out = h.tick()
    assert h.targets_called() == ["laminar", "docent"]
    assert out["targets"]["docent"]["status"] == "synced"


def test_resume_reruns_guard_and_sync_and_relatches_on_cap(h: Harness) -> None:
    h.replies["docent"] = lambda kw: make_report(stopped="2.5 GB monthly payload-byte allowance reached")
    h.tick()
    calls = len(h.sync_calls)
    out = h.tick(resume=["docent"])
    assert h.targets_called()[calls:] == ["docent"]
    assert len(h.guards) == 2
    assert out["resumed"] == ["docent"]
    assert out["targets"]["docent"]["status"] == "stopped"
    assert out["targets"]["laminar"]["status"] == "unchanged"
    final = h.tick()
    assert final["targets"]["docent"]["status"] == "paused"
    assert len(h.sync_calls) == calls + 1


def test_resume_cannot_bypass_signals_guard(h: Harness) -> None:
    h.replies["docent"] = lambda kw: make_report(stopped="paused")
    h.tick()
    calls = len(h.sync_calls)
    h.guard_error = RuntimeError("signals enabled")
    out = h.tick(resume=["all"])
    assert len(h.sync_calls) == calls
    assert {s["status"] for s in out["targets"].values()} == {"refused"}
    assert watch._load_state(h.root / "watch-state.json")["docent"]["paused"] is True


def test_resume_success_clears_pause(h: Harness) -> None:
    h.replies["docent"] = lambda kw: make_report(stopped="paused")
    h.tick()
    h.replies.clear()
    out = h.tick(resume=["docent"])
    assert out["ok"] is True
    assert h.tick()["targets"]["docent"]["status"] == "unchanged"


def test_initial_manual_stop_is_adopted_and_not_repeated(h: Harness) -> None:
    h.root.mkdir(parents=True)
    rows = [
        {"target": "laminar", "stopped": None, "at": "2026-10-27T00:00:00+00:00"},
        {"target": "docent", "stopped": "2.5 GB monthly payload-byte allowance reached",
         "cursor": "run-7", "at": "2026-10-27T01:00:00+00:00"},
    ]
    (h.root / "sync-runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = h.tick()
    assert h.targets_called() == ["laminar"]
    assert out["targets"]["docent"]["status"] == "paused"
    assert out["targets"]["docent"]["reason"] == "adopted_manual_stop"
    assert out["targets"]["docent"]["cursor"] == "run-7"
    assert out["ok"] is False


def test_initial_seeding_uses_file_order_and_later_success_cancels_older_stop(h: Harness) -> None:
    h.root.mkdir(parents=True)
    rows = [
        # File order wins even though this later row carries an earlier timestamp.
        {"target": "docent", "stopped": "cap", "at": "2026-10-27T05:00:00+00:00"},
        {"target": "docent", "stopped": None, "at": "2026-10-27T01:00:00+00:00"},
        {"target": "laminar", "stopped": None, "at": "2026-10-27T05:00:00+00:00"},
        {"target": "laminar", "stopped": "cap", "at": "2026-10-27T01:00:00+00:00"},
    ]
    (h.root / "sync-runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = h.tick()
    assert h.targets_called() == ["docent"]
    assert out["targets"]["laminar"]["status"] == "paused"


def test_seeding_happens_only_once(h: Harness) -> None:
    h.tick()
    h.root.joinpath("sync-runs.jsonl").write_text(json.dumps({"target": "docent", "stopped": "late"}) + "\n")
    h.db.write_bytes(b"changed-db")
    out = h.tick()
    assert out["targets"]["docent"]["status"] == "synced"


@pytest.mark.parametrize("content", ["{bad", "[]", json.dumps({"version": 1, "targets": {"laminar": {"paused": "no"}}}),
                                     json.dumps({"version": 2, "targets": {}}),
                                     json.dumps({"version": 1, "targets": {"other": {}}})])
def test_malformed_state_fails_closed_before_network(h: Harness, content: str) -> None:
    h.root.mkdir(parents=True)
    (h.root / "watch-state.json").write_text(content)
    with pytest.raises(watch.WatchError, match="state_malformed"):
        h.tick()
    assert h.hosted() == 0


def test_malformed_sync_log_fails_closed_before_network(h: Harness) -> None:
    h.root.mkdir(parents=True)
    (h.root / "sync-runs.jsonl").write_text('{"target": "laminar", "stopped": null}\nnot json\n')
    with pytest.raises(watch.WatchError, match="sync_log_malformed"):
        h.tick()
    assert h.hosted() == 0
    assert not (h.root / "watch-state.json").exists()


def test_missing_db_fails_closed_before_any_hosted_call(h: Harness) -> None:
    h.db.unlink()
    with pytest.raises(watch.WatchError, match="db_unreadable"):
        h.tick()
    assert h.hosted() == 0


def test_invalid_options(h: Harness, tmp_path: Path) -> None:
    with pytest.raises(watch.WatchError, match="target_invalid"):
        h.tick(targets=["nope"])
    with pytest.raises(watch.WatchError, match="target_invalid"):
        h.tick(targets=[])
    with pytest.raises(watch.WatchError, match="resume_invalid"):
        h.tick(resume=["nope"])
    with pytest.raises(watch.WatchError, match="repo_root_invalid"):
        watch.watch_once(repo_root=Path("relative"), db_path=h.db, root=h.root)
    with pytest.raises(watch.WatchError, match="repo_root_invalid"):
        watch.watch_once(repo_root=tmp_path / "missing", db_path=h.db, root=h.root)
    assert h.hosted() == 0


def test_targets_are_deduplicated_and_selectable(h: Harness) -> None:
    out = h.tick(targets=["docent", "docent"])
    assert h.targets_called() == ["docent"]
    assert list(out["targets"]) == ["docent"]


def test_compact_output_has_counts_not_rejection_arrays_or_exception_text(h: Harness) -> None:
    rejected = [{"trace_id": f"run-{i}", "kind": "secret_scan"} for i in range(300)]
    h.replies["laminar"] = lambda kw: make_report(rejected=rejected, synced=0)

    def leaks(kw: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("payload sk-live-TOPSECRET")

    h.replies["docent"] = leaks
    out = h.tick()
    text = json.dumps(out)
    assert out["targets"]["laminar"]["status"] == "synced"  # rejections still complete the pass
    assert out["targets"]["laminar"]["rejected"] == 300
    assert "run-5" not in text and "TOPSECRET" not in text
    assert out["targets"]["docent"]["status"] == "error"
    assert out["targets"]["docent"]["error_type"] == "RuntimeError"
    assert "TOPSECRET" not in (h.root / "watch-state.json").read_text()
    assert len(text) < 4000


def test_state_and_lock_are_owner_only_and_empty_root_works(h: Harness) -> None:
    assert not h.root.exists()
    h.tick()
    for name in ("watch-state.json", "watch.lock"):
        assert stat.S_IMODE((h.root / name).stat().st_mode) == 0o600
    assert not (h.root / "watch-state.json.tmp").exists()


def test_cli_exit_codes_and_single_compact_line(h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--repo-root", str(h.repo), "--db-path", str(h.db), "--state-root", str(h.root),
            "--target", "laminar", "--target", "laminar"]
    assert watch.main(argv) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and " " not in lines[0]
    assert list(json.loads(lines[0])["targets"]) == ["laminar"]
    assert h.targets_called() == ["laminar"]
    h.replies["docent"] = lambda kw: make_report(stopped="paused")
    assert watch.main(argv[:-4]) == 2
    assert json.loads(capsys.readouterr().out)["targets"]["docent"]["status"] == "stopped"


def test_cli_fail_closed_exit_and_argparse_rejection(h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    h.db.unlink()
    assert watch.main(["--repo-root", str(h.repo), "--db-path", str(h.db), "--state-root", str(h.root)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"] == "db_unreadable"
    with pytest.raises(SystemExit) as exc:
        watch.main(["--repo-root", str(h.repo), "--target", "other"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        watch.main(["--repo-root", str(h.repo), "--resume", "other"])
    assert h.hosted() == 0