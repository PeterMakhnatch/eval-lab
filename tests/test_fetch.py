from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab import cli
from evallab.fetch import (
    PROOFJUDGE_ATIF_SAMPLE,
    ControlCall,
    DatasetListing,
    FetchError,
    FetchService,
    PublicAtifSource,
    fetch_public_atif,
    parse_pin,
)

ATIF_FIXTURE = (
    Path(__file__).parents[1]
    / "research/explorations/harbor-021/fixtures/trajectory.json"
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


def test_audit_skips_ruff_cache_and_dirs_without_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harbor = FakeHarbor()
    service = FetchService(root=tmp_path, harbor=harbor)
    service.fetch("hello-world@1.0")
    cache = tmp_path / "library/benchmarks/.ruff_cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    stray = tmp_path / "library/benchmarks/not-an-ingest"
    stray.mkdir()
    (stray / "notes.txt").write_text("no MANIFEST.md\n")
    rows = service.audit()
    assert [row.name for row in rows] == ["hello-world"]
    assert rows[0].status == "pass"
    code = cli.run_cli(["fetch", "--audit"], workspace=tmp_path, harbor=harbor)
    out = capsys.readouterr().out
    assert code == 0
    assert "hello-world" in out
    assert ".ruff_cache" not in out
    assert "not-an-ingest" not in out
    assert "MANIFEST.md missing" not in out


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


def _public_source(payload: bytes, *, revision: str = "a" * 40) -> PublicAtifSource:
    import hashlib

    return PublicAtifSource(
        item_id="public-atif@test",
        repo_id="example/public-atif",
        revision=revision,
        filename="data/sample.jsonl",
        sha256=hashlib.sha256(payload).hexdigest(),
        license="apache-2.0",
    )


def _atif_payload() -> bytes:
    trajectory = json.loads(ATIF_FIXTURE.read_text())
    return (json.dumps(trajectory, separators=(",", ":")) + "\n").encode()


def test_public_atif_fetch_checksums_validates_and_projects(tmp_path: Path) -> None:
    payload = _atif_payload()
    source = _public_source(payload)
    seen: list[str] = []

    def download(url: str) -> bytes:
        seen.append(url)
        return payload

    result = fetch_public_atif(
        source,
        output_root=tmp_path / "derived/parquet",
        downloader=download,
        fetched_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    provenance = json.loads(result.provenance_path.read_text())
    assert result.status == "fetched"
    assert result.records == 1
    assert result.valid == 1
    assert result.invalid == 0
    assert result.row_counts["trajectories"] == 1
    assert result.row_counts["steps"] > 0
    assert provenance["zone"] == "01-external"
    assert provenance["revision"] == "a" * 40
    assert provenance["material_digest"] == f"sha256:{source.sha256}"
    assert seen == [source.url]

    second = fetch_public_atif(
        source,
        output_root=tmp_path / "derived/parquet",
        downloader=lambda _url: pytest.fail("no-op fetch must not download"),
    )
    assert second.status == "noop"
    assert second.row_counts == result.row_counts


def test_public_atif_fetch_refuses_digest_drift(tmp_path: Path) -> None:
    payload = _atif_payload()
    source = _public_source(payload)
    with pytest.raises(FetchError, match="digest mismatch"):
        fetch_public_atif(
            source,
            output_root=tmp_path / "derived/parquet",
            downloader=lambda _url: payload + b"drift\n",
        )
    assert not (tmp_path / "derived/parquet/external/public-atif@test").exists()


def test_public_atif_fetch_requires_commit_revision(tmp_path: Path) -> None:
    payload = _atif_payload()
    source = _public_source(payload, revision="main")
    with pytest.raises(FetchError, match="40-hex commit"):
        fetch_public_atif(
            source,
            output_root=tmp_path / "derived/parquet",
            downloader=lambda _url: payload,
        )


def test_public_atif_fetch_rejects_unsafe_item_id(tmp_path: Path) -> None:
    payload = _atif_payload()
    source = _public_source(payload)
    source = replace(source, item_id="../escape")
    with pytest.raises(FetchError, match="item id"):
        fetch_public_atif(
            source,
            output_root=tmp_path / "derived/parquet",
            downloader=lambda _url: payload,
        )


def test_public_atif_fetch_audits_invalid_jsonl(tmp_path: Path) -> None:
    payload = b"not-json\n"
    result = fetch_public_atif(
        _public_source(payload),
        output_root=tmp_path / "derived/parquet",
        downloader=lambda _url: payload,
    )
    assert result.records == 1
    assert result.valid == 0
    assert result.invalid == 1
    assert result.row_counts["trajectories"] == 1


def test_proofjudge_sample_is_commit_and_checksum_pinned() -> None:
    assert len(PROOFJUDGE_ATIF_SAMPLE.revision) == 40
    assert len(PROOFJUDGE_ATIF_SAMPLE.sha256) == 64
    assert "resolve/main" not in PROOFJUDGE_ATIF_SAMPLE.url
