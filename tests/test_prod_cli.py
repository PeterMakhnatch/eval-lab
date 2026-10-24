from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import evallab.production.cli as production_cli
from evallab import cli
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry

DATASET = "tb4-audit-v1"
DEV = "public-dev"
SEALED = "sealed-test-never-export"
JOB_TYPES = {
    "task_quality": "task_quality.audit_task",
    "reward_seeker": "reward_seeker.seek_task",
    "misalignment": "misalignment.replay_task",
}


@pytest.fixture
def corpus_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    for dataset in (DATASET, "cheatbench-actions-v1", "hacktrace-act-v1"):
        path = keys / dataset / "items.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"item_id": DEV, "split": "dev", "subject": {"task_id": "unmaterialized-dev"}},
            {"item_id": SEALED, "split": "test", "subject": {"task_id": "sealed-task"}},
        ] if dataset == DATASET else []
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (keys / DATASET / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [],
        "tasks": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.delenv("EVALLAB_OPERATOR_TOKEN", raising=False)
    return root


def _path(root: Path) -> Path:
    return root / "derived/production/prod-queue.sqlite"


def _seed(root: Path, *systems: str) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuousWorkQueue(path, registry=CorpusRegistry(root)) as queue:
        for system in systems:
            queue.append_event({
                "event_id": "cli-job:" + system,
                "run_id": "cli-regression",
                "system": system,
                "event_type": JOB_TYPES[system],
                "status": "confirmed",
                "payload": {"dataset_id": DATASET, "item_id": DEV},
            })


def _state(root: Path) -> dict[str, Any]:
    with ContinuousWorkQueue(_path(root), registry=CorpusRegistry(root)) as queue:
        return queue.queue_snapshot()


def _events(root: Path) -> list[dict[str, Any]]:
    with ContinuousWorkQueue(_path(root), registry=CorpusRegistry(root)) as queue:
        return queue.read_events(limit=1000)


def _cache_state(cache: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        path.relative_to(cache).as_posix(): (
            path.stat().st_mode, path.read_bytes() if path.is_file() else None,
        )
        for path in cache.rglob("*")
    }


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail("A read-only command reached dispatch or a broker")


def _runtime_seam(monkeypatch: pytest.MonkeyPatch, runtime_type: type) -> None:
    from evallab.production import runtime

    monkeypatch.setattr(runtime, "ProductionRuntime", runtime_type)


def _relay_seam(monkeypatch: pytest.MonkeyPatch, relay_type: type) -> None:
    from evallab.production import relay

    monkeypatch.setattr(relay, "ProductionOutboxRelay", relay_type)


def _server_seam(monkeypatch: pytest.MonkeyPatch, server_type: type) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", server_type)


def test_prod_help_does_not_open_runtime_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(production_cli, "_open_queue", _forbidden)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.run_cli(["prod", "--help"], workspace=corpus_root)
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert all(command in help_text for command in ("ingest", "status", "run-once", "serve"))
    assert "TEST/heldout" in help_text
    assert not _path(corpus_root).exists()


def test_ingest_persists_admission_idempotently_and_reports_sealed_exclusions(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["prod", "ingest", "--dataset", DATASET]
    assert cli.run_cli(argv, workspace=corpus_root) == 0
    first_text = capsys.readouterr().out
    first = json.loads(first_text)
    assert first["ingest"]["totals"]["admitted"] == 1
    assert first["ingest"]["totals"]["excluded"] == 1
    assert first["ingest"]["totals"]["blocked"] == 1
    assert SEALED not in first_text
    before = _state(corpus_root)
    assert before["depth"] == 0
    assert cli.run_cli(argv, workspace=corpus_root) == 0
    second_text = capsys.readouterr().out
    assert SEALED not in second_text
    assert _state(corpus_root) == before
    with ContinuousWorkQueue(_path(corpus_root), registry=CorpusRegistry(corpus_root)) as queue:
        events = queue.read_events(limit=100)
    assert any(event["event_type"] == "queue.corpus_admitted" for event in events)
    assert SEALED not in json.dumps(events)


def test_refresh_preflight_only_observes_existing_admissions_and_never_seeds_jobs(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    refresh = ["prod", "ingest", "--refresh-preflight", "--dataset", DATASET]
    assert cli.run_cli(refresh, workspace=corpus_root) == 0
    empty = json.loads(capsys.readouterr().out)
    assert "ingest" not in empty
    assert empty["preflight_refresh"]["totals"] == {
        "inspected": 0, "recorded": 0, "unchanged": 0,
    }
    assert _events(corpus_root) == []

    assert cli.run_cli(["prod", "ingest", "--dataset", DATASET], workspace=corpus_root) == 0
    capsys.readouterr()
    admitted = _events(corpus_root)
    assert len(admitted) == 1
    source = corpus_root / "research/answer-keys" / DATASET / "items.jsonl"
    with source.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "item_id": "unadmitted-dev", "split": "dev",
            "subject": {"task_id": "unadmitted-task"},
        }) + "\n")

    assert cli.run_cli(refresh + ["--run-id", "observation-a"], workspace=corpus_root) == 0
    first_text = capsys.readouterr().out
    first = json.loads(first_text)
    report = first["preflight_refresh"]
    assert report["schema"] == "production.corpus-preflight-refresh/v1"
    assert report["run_id"] == "observation-a"
    assert report["totals"] == {"inspected": 1, "recorded": 1, "unchanged": 0}
    assert set(report["datasets"]) == {DATASET}
    assert [item["item_id"] for item in report["datasets"][DATASET]["items"]] == [DEV]
    assert first["queue"]["depth"] == 0
    observed = _events(corpus_root)
    assert observed[:1] == admitted
    assert [event["event_type"] for event in observed[1:]] == ["queue.corpus_preflight_updated"]
    assert SEALED not in first_text
    assert "unadmitted-dev" not in json.dumps(observed)

    assert cli.run_cli(refresh + ["--run-id", "observation-b"], workspace=corpus_root) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["preflight_refresh"]["totals"] == {
        "inspected": 1, "recorded": 0, "unchanged": 1,
    }
    assert _events(corpus_root) == observed
    assert _state(corpus_root)["depth"] == 0


def test_refresh_uses_explicit_cache_relative_to_repo_root_for_real_offline_port(
    corpus_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from evallab.candidate_port import PM_FIXED_PIN
    from evallab.registry import task_directory_digest
    from evallab.upstream_grader import _tree_digest

    original = corpus_root / "fixtures/upstream/native-task"
    fixed = corpus_root / "fixtures/port/native-task"
    cache = corpus_root / "immutable-assets"
    cache.mkdir()
    asset = b"pinned public fixture bytes\n"
    digest = hashlib.sha256(asset).hexdigest()
    (cache / digest).write_bytes(asset)
    files = {
        original / "task.toml": (
            'schema_version = "1.0"\nartifacts = ["/app/result.txt"]\n'
            '[task]\nname = "terminal-bench/native-task"\n'
            '[agent]\ntimeout_sec = 60.0\n'
            '[verifier]\ntimeout_sec = 60.0\nenvironment_mode = "shared"\n'
            '[environment]\ncpus = 2\nmemory_mb = 4096\nstorage_mb = 10240\n'
            'gpus = 0\nbuild_timeout_sec = 600.0\n'
        ),
        original / "instruction.md": "Write result.txt.\n",
        original / "environment/Dockerfile": "FROM scratch\n",
        fixed / "Containerfile": (
            "FROM scratch\nCOPY shared_data/ /shared_data/\nCOPY root_data/ /root_data/\n"
        ),
        fixed / "pyproject.toml": '[project]\nname="environment"\nversion="0.1.0"\n',
        fixed / "uv.lock": "version = 1\n",
        fixed / ".manifest.json": "{}\n",
        fixed / "shared_data/.gitkeep": "",
        fixed / "root_data/.gitkeep": "",
        fixed / "setup_data.py": (
            "def setup(ctx):\n"
            f"    ctx.fetch('https://example.invalid/pinned', ctx.root_data / 'blob.bin', {digest!r})\n"
        ),
        fixed / "src/environment/tasks/native_task/__init__.py": (
            "class NativeTask(Task):\n"
            "    id = 'native-task'\n"
            "    def required_hardware(self):\n"
            "        return 'cpus=2,memory_mb=4096,storage_mb=10240'\n"
            "    def judge(self):\n"
            "        return ExecutableJudge(['python', 'scoring_script.py'])\n"
        ),
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    source = corpus_root / "research/answer-keys" / DATASET / "items.jsonl"
    source.write_text(json.dumps({
        "item_id": DEV, "split": "dev",
        "subject": {
            "task_id": "terminal-bench/native-task",
            "task_digest": task_directory_digest(original),
        },
        "package": {"path": original.relative_to(corpus_root).as_posix()},
    }) + "\n", encoding="utf-8")
    inventory = (
        corpus_root / "research/experiments/candidate-execution-20261021/static-inventory.json"
    )
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "schema": "evallab.candidate_execution_inventory/v1",
        "source_roots": {"upstream": str(original.parent), "port": str(fixed.parent)},
        "tasks": [{
            "id": "native-task", "upstream_task": str(original), "port_task": str(fixed),
            "upstream_exact_tree_digest": _tree_digest(original),
            "port_exact_tree_digest": _tree_digest(fixed),
            "source_pins": {"port_required": PM_FIXED_PIN},
        }],
    }), encoding="utf-8")
    before_cache = _cache_state(cache)
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    options = ["--repo-root", str(corpus_root)]
    assert cli.run_cli(
        ["prod", *options, "ingest", "--dataset", DATASET], workspace=workspace,
    ) == 0
    capsys.readouterr()
    admission = _events(corpus_root)[0]
    refresh = ["prod", "ingest", *options, "--refresh-preflight", "--dataset", DATASET]
    assert cli.run_cli(refresh, workspace=workspace) == 0
    without_cache = json.loads(capsys.readouterr().out)["preflight_refresh"]
    without_port = without_cache["datasets"][DATASET]["items"][0]["preflight"]["native_port"]
    assert without_port["state"] == "blocked"

    assert cli.run_cli(refresh + ["--asset-cache", "immutable-assets"], workspace=workspace) == 0
    receipt = json.loads(capsys.readouterr().out)
    report = receipt["preflight_refresh"]
    assert report["totals"] == {"inspected": 1, "recorded": 1, "unchanged": 0}
    native_port = report["datasets"][DATASET]["items"][0]["preflight"]["native_port"]
    assert native_port["state"] == "described"
    assert receipt["queue"]["depth"] == 0
    assert _events(corpus_root)[0] == admission
    assert _cache_state(cache) == before_cache
    assert not (workspace / "derived").exists()
    assert not (workspace / "immutable-assets").exists()
    observed = _events(corpus_root)
    # Source changes after observation do not turn status into a fresh probe.
    (fixed / "setup_data.py").write_text("unsupported_new_recipe()\n", encoding="utf-8")
    assert cli.run_cli(["prod", "status", *options], workspace=workspace) == 0
    summary = json.loads(capsys.readouterr().out)["preflight"]
    assert summary["totals"]["native_port"]["described"] == 1
    assert summary["last_observed_at"]
    assert summary["execution_proof"] is False
    assert summary["runtime_qualified"] is False
    assert _events(corpus_root) == observed


def test_status_reports_last_observed_static_preflight_read_only_beyond_first_page(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.run_cli(["prod", "ingest", "--dataset", DATASET], workspace=corpus_root) == 0
    capsys.readouterr()
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
    unobserved = json.loads(capsys.readouterr().out)["preflight"]
    assert unobserved["totals"]["admitted"] == 1
    assert unobserved["totals"]["observed"] == 0
    assert unobserved["totals"]["native_port"]["not_observed"] == 1
    assert unobserved["last_observed_at"] is None
    with ContinuousWorkQueue(_path(corpus_root), registry=CorpusRegistry(corpus_root)) as queue:
        for number in range(105):
            queue.append_event({
                "event_id": f"cli-padding:{number}", "run_id": "cli-padding",
                "system": "queue", "event_type": "queue.cli_fixture",
                "status": "applied", "payload": {},
            })
    assert cli.run_cli(
        ["prod", "ingest", "--refresh-preflight", "--dataset", DATASET],
        workspace=corpus_root,
    ) == 0
    capsys.readouterr()
    before_events = _events(corpus_root)
    before_state = _state(corpus_root)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
    status_text = capsys.readouterr().out
    status = json.loads(status_text)
    summary = status["preflight"]
    assert summary["schema"] == "production.corpus-preflight-summary/v1"
    assert summary["totals"]["admitted"] == summary["totals"]["observed"] == 1
    assert summary["totals"]["native_port"] == {
        "described": 0, "blocked": 0, "unavailable": 1, "not_observed": 0,
    }
    assert summary["last_observed_at"]
    assert summary["execution_proof"] is False
    assert summary["runtime_qualified"] is False
    assert SEALED not in status_text
    assert "items" not in summary["datasets"][DATASET]
    assert status["queue"] == before_state
    assert _events(corpus_root) == before_events


def test_asset_cache_requires_explicit_refresh_before_creating_a_queue(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cache = corpus_root / "cache"
    cache.mkdir()
    (cache / "pinned").write_bytes(b"immutable input")
    before = _cache_state(cache)
    assert cli.run_cli(
        ["prod", "ingest", "--asset-cache", "cache", "--database", "cache/queue.sqlite"],
        workspace=corpus_root,
    ) == 2
    error = json.loads(capsys.readouterr().err)
    assert "--asset-cache requires --refresh-preflight" in error["error"]["message"]
    assert _cache_state(cache) == before
    assert not _path(corpus_root).exists()


@pytest.mark.parametrize("output", [
    "database", "sqlite-uri", "postgres-fallback", "postgres-default-fallback",
    "queue-alias", "poison", "postgres-poison", "sqlite-sidecar",
])
def test_refresh_rejects_cache_output_overlap_before_opening_any_database(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], output: str,
) -> None:
    cache = corpus_root / ("derived" if output == "postgres-default-fallback" else "cache")
    cache.mkdir()
    pinned = cache / "pinned"
    pinned.write_bytes(b"immutable input")
    database = corpus_root / "queues/work.sqlite"
    options = ["--database", "queues/work.sqlite"]
    postgres = "postgresql://127.0.0.1:1/cli-guard-must-not-connect"
    if output == "database":
        options = ["--database", "cache/queue.sqlite"]
    elif output == "sqlite-uri":
        options = ["--database", "sqlite:///cache/queue.sqlite"]
    elif output == "postgres-fallback":
        options = ["--database", postgres, "--sqlite-fallback", "cache/fallback.sqlite"]
    elif output == "postgres-default-fallback":
        options = ["--database", postgres]
    elif output == "queue-alias":
        (corpus_root / "cache-alias").symlink_to(cache, target_is_directory=True)
        options = ["--database", "cache-alias/queue.sqlite"]
    else:
        database.parent.mkdir()
        companion = (
            Path(str(database) + "-wal") if output == "sqlite-sidecar"
            else database.with_suffix(".poison.sqlite")
        )
        companion.symlink_to(pinned)
        if output == "postgres-poison":
            options = ["--database", postgres, "--sqlite-fallback", "queues/work.sqlite"]
    before = _cache_state(cache)
    assert cli.run_cli([
        "prod", "ingest", "--refresh-preflight", "--asset-cache", cache.name, *options,
    ], workspace=corpus_root) == 2
    error = json.loads(capsys.readouterr().err)
    assert "outside immutable asset cache" in error["error"]["message"]
    assert _cache_state(cache) == before
    assert not database.exists()
    assert not _path(corpus_root).exists()


@pytest.mark.parametrize("database", [":memory:", "sqlite:///:memory:"])
def test_refresh_preserves_sqlite_memory_semantics_instead_of_resolving_a_file(
    corpus_root: Path, capsys: pytest.CaptureFixture[str], database: str,
) -> None:
    cache = corpus_root / ":memory:"
    cache.mkdir()
    (cache / "pinned").write_bytes(b"immutable input")
    before = _cache_state(cache)
    assert cli.run_cli([
        "prod", "ingest", "--refresh-preflight", "--asset-cache", ":memory:",
        "--database", database, "--dataset", DATASET,
    ], workspace=corpus_root) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["backend"] == "sqlite"
    assert receipt["preflight_refresh"]["totals"] == {
        "inspected": 0, "recorded": 0, "unchanged": 0,
    }
    assert _cache_state(cache) == before
    assert not (corpus_root / "derived").exists()


def test_status_reads_persistent_jobs_without_claiming_or_publishing(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment")
    before = _state(corpus_root)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    assert cli.run_cli(["prod", "status"], workspace=corpus_root) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["backend"] == "sqlite"
    assert receipt["queue"]["depth"] == 1
    assert receipt["queue"]["jobs"] == {"queued": 1, "leased": 0, "completed": 0}
    assert receipt["queue"]["active_leases"] == []
    assert _state(corpus_root) == before


def test_explicit_root_and_relative_database_work_on_either_side_of_subcommand(
    corpus_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    options = ["--repo-root", str(corpus_root), "--database", "queues/explicit.sqlite"]
    assert cli.run_cli(["prod", *options, "ingest", "--dataset", DATASET], workspace=workspace) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["queue"]["last_seq"] > 0
    assert cli.run_cli(["prod", "status", *options], workspace=workspace) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["queue"] == first["queue"]
    assert (corpus_root / "queues/explicit.sqlite").is_file()
    assert not (workspace / "queues/explicit.sqlite").exists()




@pytest.mark.parametrize("driver", ["gptoss", "ollama", "mlx", "local", "arbitrary/model"])
def test_non_subscription_drivers_are_rejected_before_opening_queue(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, driver: str,
) -> None:
    monkeypatch.setattr(production_cli, "_open_queue", _forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.run_cli(["prod", "run-once", "--seeker-driver", driver], workspace=corpus_root)
    assert stopped.value.code == 2


def test_worker_failure_is_nonzero_but_relay_still_runs_and_resources_close(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []

    class FailedRuntime:
        def run_once(self) -> dict[str, Any]:
            return {"ok": False, "failures": {"misalignment": {"error": "bad trace"}}}

        def close(self) -> None:
            closed.append("runtime")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            closed.append("relayed")
            return {"delivered": 0}

        async def close(self) -> None:
            closed.append("relay")

    monkeypatch.setattr(production_cli, "_runtime", lambda *_: FailedRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    assert cli.run_cli(["prod", "run-once"], workspace=corpus_root) == 1
    assert json.loads(capsys.readouterr().out)["runtime"]["ok"] is False
    assert closed == ["relayed", "relay", "runtime"]


def test_dashboard_default_serves_real_asgi_without_dispatch_or_broker_connection(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(corpus_root, "misalignment")
    before = _state(corpus_root)
    observed: dict[str, Any] = {}

    class DashboardServer:
        def __init__(self, config: Any):
            self.config = config
            self.started, self.should_exit = False, False
            observed["host"] = config.host

        async def serve(self) -> None:
            self.started = True
            with TestClient(self.config.app) as client:
                assert client.get("/").status_code == 200
                observed["state"] = client.get("/api/state").json()
                response = client.post("/api/intervene", json={
                    "action": "pause", "run_id": "cli-regression", "reason": "not authorized",
                })
                observed["intervention_status"] = response.status_code

    _server_seam(monkeypatch, DashboardServer)
    monkeypatch.setattr(production_cli, "_runtime", _forbidden)
    monkeypatch.setattr(production_cli, "_relay", _forbidden)
    monkeypatch.setenv("EVALLAB_SEEKER_LIVE_MODEL", "1")
    assert cli.run_cli(
        ["prod", "serve", "--nats-url", "nats://must-not-connect.invalid:4222"],
        workspace=corpus_root,
    ) == 0
    receipts = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert receipts[0]["workers"] is False
    assert receipts[-1]["event"] == "stopped"
    assert observed["host"] == "127.0.0.1"
    assert observed["state"]["queue"]["depth"] == 1
    assert observed["intervention_status"] == 503
    assert _state(corpus_root) == before


def test_background_worker_crash_stops_http_and_closes_resources_without_secrets(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []
    observed = SimpleNamespace(server=None)
    secret = "operator-secret-never-print"
    password = "database-secret-never-print"

    class CrashRuntime:
        def run_once(self) -> dict[str, Any]:
            raise RuntimeError(f"worker crashed {secret} postgresql://user:{password}@host/db")

        def close(self) -> None:
            closed.append("runtime")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            return {"delivered": 0}

        async def close(self) -> None:
            closed.append("relay")

    class RunningServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False
            observed.server = self

        async def serve(self) -> None:
            self.started = True
            try:
                while not self.should_exit:
                    await asyncio.sleep(0.001)
            finally:
                closed.append("http")

    _server_seam(monkeypatch, RunningServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: CrashRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    monkeypatch.setenv("EVALLAB_OPERATOR_TOKEN", secret)
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error"]["type"] == "RuntimeError"
    assert "worker crashed" in error["error"]["message"]
    assert secret not in captured.out + captured.err
    assert password not in captured.out + captured.err
    assert observed.server.should_exit is True
    assert closed == ["http", "relay", "runtime"]


def test_http_startup_failure_never_dispatches_and_closes_worker_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    closed: list[str] = []

    class NeverRuntime:
        run_once = staticmethod(_forbidden)

        def close(self) -> None:
            closed.append("runtime")

    class NeverRelay:
        run_once = staticmethod(_forbidden)

        async def close(self) -> None:
            closed.append("relay")

    class BindFailureServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        async def serve(self) -> None:
            raise SystemExit(1)

    _server_seam(monkeypatch, BindFailureServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: NeverRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: NeverRelay())
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 2
    assert "failed to start" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert closed == ["relay", "runtime"]


def test_connection_error_receipt_redacts_encoded_credentials_and_environment_token(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    token = "sensitive-operator-token"
    database = "postgresql://user:password%2Fwith%20spaces@localhost/lab"
    broker = "nats://broker-token@localhost:4222"
    monkeypatch.setenv("EVALLAB_OPERATOR_TOKEN", token)

    def broken_open(*_: Any) -> Any:
        raise OSError(f"{database} password/with spaces {broker} broker-token {token}")

    monkeypatch.setattr(production_cli, "_open_queue", broken_open)
    assert cli.run_cli([
        "prod", "run-once", "--database", database, "--nats-url", broker,
    ], workspace=corpus_root) == 2
    captured = capsys.readouterr()
    receipt = json.loads(captured.err)
    assert receipt["error"]["type"] == "OSError"
    for secret in (token, database, broker, "password/with spaces", "broker-token"):
        assert secret not in captured.out + captured.err


def test_graceful_signal_restores_handlers_without_replaying_before_cleanup(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    class SignalServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        def capture_signals(self) -> Any:
            pytest.fail("The CLI must own the graceful signal lifecycle")

        def handle_exit(self, sig: int, frame: Any) -> None:
            observed["signal"] = sig
            self.should_exit = True

        async def serve(self) -> None:
            with self.capture_signals():
                self.started = True
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
                assert self.should_exit

    _server_seam(monkeypatch, SignalServer)
    monkeypatch.setattr(signal, "raise_signal", _forbidden)
    assert cli.run_cli(["prod", "serve"], workspace=corpus_root) == 0
    assert observed["signal"] == signal.SIGTERM
    assert all(signal.getsignal(sig) == handler for sig, handler in originals.items())
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["event"] == "stopped"


def test_shutdown_waits_for_inflight_consumer_before_closing_resources(
    corpus_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class FinishingRuntime:
        def run_once(self) -> dict[str, Any]:
            started.set()
            assert release.wait(timeout=2)
            calls.append("completed")
            return {"ok": True}

        def close(self) -> None:
            assert "completed" in calls
            calls.append("runtime-closed")

    class Relay:
        async def run_once(self) -> dict[str, Any]:
            calls.append("relay")
            return {"delivered": 0}

        async def close(self) -> None:
            calls.append("relay-closed")

    class StoppingServer:
        def __init__(self, config: Any):
            self.started, self.should_exit = False, False

        async def serve(self) -> None:
            self.started = True
            while not started.is_set():
                await asyncio.sleep(0.001)
            self.should_exit = True
            release.set()
            calls.append("http-stopped")

    _server_seam(monkeypatch, StoppingServer)
    monkeypatch.setattr(production_cli, "_runtime", lambda *_: FinishingRuntime())
    monkeypatch.setattr(production_cli, "_relay", lambda *_: Relay())
    assert cli.run_cli(["prod", "serve", "--workers"], workspace=corpus_root) == 0
    assert calls == ["http-stopped", "completed", "relay", "relay-closed", "runtime-closed"]


def test_real_async_seeker_cycle_returns_success_and_closes_from_cli(
    corpus_root: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    entries = corpus_root / "research/hackbook/entries.jsonl"
    entries.parent.mkdir(parents=True)
    entries.write_text("", encoding="utf-8")
    _path(corpus_root).parent.mkdir(parents=True, exist_ok=True)
    before = _state(corpus_root)
    assert cli.run_cli(
        ["prod", "run-once", "--systems", "reward_seeker"], workspace=corpus_root,
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["runtime"]["ok"] is True
    assert receipt["runtime"]["workers"]["reward_seeker"]["status"] == "idle"
    assert _state(corpus_root) == before
