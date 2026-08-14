from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.fetch import (
    ControlCall,
    DatasetListing,
    FetchError,
    FetchService,
    parse_pin,
)


class FakeHarbor:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, Path]] = []
        self.controls: list[ControlCall] = []
        self.hub = [
            DatasetListing(name="hello-world", version="1.0", task_count=1, source="hub"),
            DatasetListing(name="aime", version="1.0", task_count=60, source="hub"),
        ]

    def list_hub_datasets(self) -> list[DatasetListing]:
        return list(self.hub)

    def download(self, pin: str, dest: Path) -> None:
        self.downloads.append((pin, dest))
        name = pin.split("@", 1)[0]
        task = dest / name / "task-0"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n')
        (task / "instruction.md").write_text("say hello\n")
        (task / "solution").mkdir()
        (task / "solution" / "solve.sh").write_text("#!/bin/sh\necho ok\n")

    def run_control(self, call: ControlCall) -> float:
        self.controls.append(call)
        if call.n_concurrent > 2:
            raise AssertionError("n-concurrent exceeds 2")
        return 1.0 if call.agent == "oracle" else 0.0


def test_parse_pin_refuses_latest_and_bare_name() -> None:
    with pytest.raises(FetchError, match="@latest"):
        parse_pin("aime@latest")
    with pytest.raises(FetchError, match="unpinned"):
        parse_pin("aime")
    with pytest.raises(FetchError, match="unpinned"):
        parse_pin("ds-1000@head")
    pin = parse_pin("hello-world@1.0")
    assert pin.ref == "hello-world@1.0"


def test_cli_refuses_unpinned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    harbor = FakeHarbor()
    latest = cli.run_cli(
        ["fetch", "demo@latest"], workspace=tmp_path, harbor=harbor
    )
    bare = cli.run_cli(["fetch", "demo"], workspace=tmp_path, harbor=harbor)
    err = capsys.readouterr().err
    assert latest == 2
    assert bare == 2
    assert "@latest" in err or "unpinned" in err
    assert "unpinned" in err


def test_fetch_writes_ingest_style_manifest(tmp_path: Path) -> None:
    harbor = FakeHarbor()
    result = FetchService(root=tmp_path, harbor=harbor).fetch("hello-world@1.0")
    manifest = (tmp_path / "library/benchmarks/hello-world/MANIFEST.md").read_text()
    assert result.status == "fetched"
    assert "## Source and pin" in manifest
    assert "## License" in manifest
    assert "## Counts / subset" in manifest
    assert "## Lane / resources" in manifest
    assert "## Sample verification" in manifest
    assert "hello-world@1.0" in manifest
    assert "@latest" in manifest  # the Never line
    assert "Never" in manifest
    assert "sha256:" in manifest
    assert harbor.downloads == [
        ("hello-world@1.0", tmp_path / "library/benchmarks/hello-world")
    ]


def test_refetch_is_noop_and_does_not_rewrite_tasks(tmp_path: Path) -> None:
    harbor = FakeHarbor()
    service = FetchService(root=tmp_path, harbor=harbor)
    service.fetch("hello-world@1.0")
    task = tmp_path / "library/benchmarks/hello-world/hello-world/task-0/task.toml"
    original = task.read_text()
    task.write_text(original)  # keep bytes stable
    marker = tmp_path / "library/benchmarks/hello-world/hello-world/task-0/instruction.md"
    before = marker.read_bytes()
    harbor.downloads.clear()
    second = service.fetch("hello-world@1.0")
    assert second.status == "noop"
    assert harbor.downloads == []
    assert marker.read_bytes() == before
    assert task.read_text() == original


def test_audit_reports_flipped_digest(tmp_path: Path) -> None:
    harbor = FakeHarbor()
    service = FetchService(root=tmp_path, harbor=harbor)
    service.fetch("hello-world@1.0")
    dest = tmp_path / "library/benchmarks/hello-world"
    state_path = dest / ".fetch.json"
    payload = json.loads(state_path.read_text())
    payload["tree_digest"] = "sha256:" + ("ab" * 32)
    state_path.write_text(json.dumps(payload))
    rows = service.audit()
    hello = next(row for row in rows if row.name == "hello-world")
    assert hello.status == "fail"
    assert "digest drift" in hello.detail


def test_verify_sample_caps_n_and_records_rewards(tmp_path: Path) -> None:
    harbor = FakeHarbor()
    service = FetchService(root=tmp_path, harbor=harbor)
    service.fetch("hello-world@1.0", verify_sample=1, jobs_dir=tmp_path / "runs")
    assert len(harbor.controls) == 2
    assert {call.agent for call in harbor.controls} == {"oracle", "nop"}
    assert all(call.n_concurrent <= 2 for call in harbor.controls)
    assert all(call.n_attempts == 1 for call in harbor.controls)
    manifest = (tmp_path / "library/benchmarks/hello-world/MANIFEST.md").read_text()
    assert "oracle-fetch-hello-world-task-0" in manifest
    assert "**1.0**" in manifest
    assert "**0.0**" in manifest


def test_cli_list_excludes_latest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harbor = FakeHarbor()
    code = cli.run_cli(["fetch", "--list"], workspace=tmp_path, harbor=harbor)
    out = capsys.readouterr().out
    assert code == 0
    assert "hello-world@1.0" in out
    assert "aime@1.0" in out
    assert "harbor/adapters/aime" in out
    assert "@latest" not in out.split("Hub:")[1].split("Adapter")[0]
    assert "refused" in out.lower() or "never" in out.lower() or "Pinned" in out


def test_refuses_to_mutate_existing_different_pin(tmp_path: Path) -> None:
    harbor = FakeHarbor()
    service = FetchService(root=tmp_path, harbor=harbor)
    service.fetch("hello-world@1.0")
    with pytest.raises(FetchError, match="refusing to mutate"):
        service.fetch("hello-world@2.0")
