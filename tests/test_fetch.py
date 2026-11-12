from __future__ import annotations

from pathlib import Path

import pytest

from evallab.fetch import (
    PROOFJUDGE_ATIF_SAMPLE,
    ControlCall,
    DatasetListing,
    FetchError,
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


def test_proofjudge_sample_is_commit_and_checksum_pinned() -> None:
    assert len(PROOFJUDGE_ATIF_SAMPLE.revision) == 40
    assert len(PROOFJUDGE_ATIF_SAMPLE.sha256) == 64
    assert "resolve/main" not in PROOFJUDGE_ATIF_SAMPLE.url


