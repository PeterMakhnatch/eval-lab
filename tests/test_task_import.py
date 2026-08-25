from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evallab import cli
from evallab.task_import import import_task_batch, ledger_rows


def _task(root: Path, name: str, content: str = "echo ok\n") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "task.toml").write_text(
        f'schema_version = "1.0"\n[task]\nname = "{name}"\n'
    )
    (package / "instruction.md").write_text(f"Do {name}\n")
    (package / "solution.sh").write_text(content)
    return package


def test_batch_import_is_restartable_and_failure_isolated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    first = _task(source, "first")
    _task(source, "second")
    broken = source / "broken"
    broken.mkdir(parents=True)
    (broken / "task.toml").write_text("not = [valid")
    destination = tmp_path / "imported"
    ledger = tmp_path / "ledger/tasks.sqlite3"

    initial = import_task_batch(source, destination, ledger)

    assert initial.discovered == 3
    assert initial.imported == 2
    assert initial.failed == 1
    assert len(list(destination.iterdir())) == 2
    assert len(ledger_rows(ledger)) == 3

    resumed = import_task_batch(source, destination, ledger)
    assert resumed.imported == 0
    assert resumed.skipped == 2
    assert resumed.failed == 1

    # A durable importing marker left by a crash is safe to retry.
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "UPDATE imports SET status = 'importing' WHERE source_path = ?",
            (str(first.resolve()),),
        )
    recovered = import_task_batch(source, destination, ledger)
    assert recovered.imported == 1
    assert recovered.skipped == 1


def test_crash_restart_reclaims_staging_and_completes_ledger(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = _task(source, "crash-restart")
    destination_root = tmp_path / "imported"
    ledger = tmp_path / "ledger.sqlite3"
    initial = import_task_batch(source, destination_root, ledger)
    destination = initial.items[0].destination
    assert destination is not None

    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "UPDATE imports SET status = 'importing' WHERE source_path = ?",
            (str(package.resolve()),),
        )
    stale = destination.with_name(f".{destination.name}.tmp-12345")
    stale.mkdir()
    (stale / "partial").write_text("incomplete")
    staging_symlink = destination.with_name(f".{destination.name}.tmp-54321")
    staging_symlink.symlink_to(destination, target_is_directory=True)

    recovered = import_task_batch(source, destination_root, ledger)

    assert recovered.imported == 1
    assert recovered.skipped == 0
    assert recovered.failed == 0
    assert list(destination_root.iterdir()) == [destination]
    assert destination.is_dir()
    rows = ledger_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["status"] == "imported"


def test_changed_task_bytes_create_a_new_content_addressed_import(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = _task(source, "mutable")
    destination = tmp_path / "imported"
    ledger = tmp_path / "ledger.sqlite3"
    first = import_task_batch(source, destination, ledger)
    first_path = first.items[0].destination

    (package / "solution.sh").write_text("echo changed\n")
    second = import_task_batch(source, destination, ledger)

    assert second.imported == 1
    assert second.items[0].destination != first_path
    assert len(ledger_rows(ledger)) == 2


def test_tasks_import_cli_reports_machine_readable_ledger_result(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source"
    _task(source, "cli-task")
    destination = tmp_path / "destination"
    ledger = tmp_path / "tasks.sqlite3"

    rc = cli.run_cli([
        "tasks", "import", str(source),
        "--destination", str(destination),
        "--ledger", str(ledger),
        "--json",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 1
    assert payload["items"][0]["status"] == "imported"
