"""Focused tests for evallab.readers.laminar_import (no network)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.request import Request

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from evallab.laminar import laminar_trace_uuid
from evallab.readers import laminar_import as li


def _trial(root: Path, name: str) -> Path:
    trial = root / name
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.6",
                "steps": [
                    {
                        "step_id": "1",
                        "timestamp": "2026-10-06T10:00:00+00:00",
                        "source": "user",
                        "message": "Fix it",
                    },
                    {
                        "step_id": "2",
                        "timestamp": "2026-10-06T10:00:01+00:00",
                        "source": "agent",
                        "message": "checking",
                        "tool_calls": [
                            {
                                "tool_call_id": "c1",
                                "function_name": "bash_command",
                                "arguments": {"keystrokes": "ls\n"},
                            }
                        ],
                        "observation": {"results": [{"source_call_id": "c1", "content": "a.py"}]},
                    },
                ],
            }
        )
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "t",
                "trial_name": name,
                "agent_info": {"agent": "test"},
                "verifier_result": {"rewards": {"reward": 1.0}},
                "finished_at": "2026-10-06T10:00:02+00:00",
            }
        )
    )
    return trial


class Recorder:
    def __init__(self) -> None:
        self.posts: list[list[Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        assert url.endswith("/v1/traces")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        self.posts.append(
            [s for r in request.resource_spans for ss in r.scope_spans for s in ss.spans]
        )
        return 200


def test_import_posts_steps_and_root_with_stable_trace_id(tmp_path: Path) -> None:
    trial = _trial(tmp_path / "trials", "tw-0001")
    rec = Recorder()
    out = li.import_trial(
        trial, api_key="k", endpoint="http://x", state_dir=tmp_path / "state", transport=rec
    )
    assert out["trace_id"] == laminar_trace_uuid("tw-0001")
    names = {s.name for post in rec.posts for s in post}
    assert {"user 1", "turn 2", "bash_command", "harbor.trial"} <= names
    assert all(
        s.trace_id.hex() == out["trace_id"].replace("-", "") for post in rec.posts for s in post
    )
    # Idempotent: a second import posts nothing new.
    out2 = li.import_trial(
        trial, api_key="k", endpoint="http://x", state_dir=tmp_path / "state", transport=rec
    )
    assert out2["trace_id"] == out["trace_id"]
    assert out2["spans"] == 0


class FakeOpener:
    """Canned /v1/sql/query pages; records request bodies."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.seen: list[dict] = []

    def __call__(self, request: Request, timeout: float = 30) -> Any:
        body = json.loads(request.data.decode())
        self.seen.append(body)
        payload = self.pages[min(len(self.seen) - 1, len(self.pages) - 1)]
        return _FakeResponse(payload)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._buf.read()


RUNS = {
    "data": [
        {
            "trace_id": "t1",
            "signal_id": "s-copied",
            "status": "COMPLETED",
            "input_tokens": 30000,
            "cache_read_tokens": 5000,
            "output_tokens": 300,
        }
    ]
}
EVENTS = {"data": [{"trace_id": "t1", "signal_id": "s-copied", "payload": {"source": "git"}}]}


def test_await_and_verdict_flagged() -> None:
    opener = FakeOpener([RUNS, EVENTS, RUNS, EVENTS])
    sigmap = {"copied_upstream_fix": "s-copied"}
    state = li.await_signal_runs(
        ["t1"], sigmap, api_key="k", opener=opener, interval_s=0, sleep=lambda _: None
    )
    verdict = li.verdict_for(
        "tw-0001",
        "laminar_copied",
        trace_id="t1",
        project_id="p",
        model="m",
        entry=state.get("t1\x00copied_upstream_fix"),
    )
    assert verdict["flagged"] is True
    assert verdict["score"] is None
    assert verdict["tokens"] == {"input": 30000, "cached": 5000, "output": 300}
    assert verdict["cost_usd"] > 0
    assert verdict["raw"] is None


def test_verdict_unfired_and_missing() -> None:
    done = li.verdict_for(
        "tw-0002",
        "laminar_false_completion",
        trace_id="t2",
        project_id="p",
        model="m",
        entry={"run": {"status": "COMPLETED", "input_tokens": 10}, "fired": None},
    )
    assert done["flagged"] is False
    missing = li.verdict_for(
        "tw-0003", "laminar_copied", trace_id="t3", project_id="p", model="m", entry=None
    )
    assert missing["flagged"] is None


def test_convert_reused_costs_nothing() -> None:
    stored = {
        "checks": {"copied": False, "false_completion": None},
        "explanations": {},
        "model": "glm-5.3-flash",
        "tokens": {"input": 82549, "output": 968},
        "trace_id": "tid",
        "trace_url": "url",
    }
    verdict = li.convert_reused(stored, "our-0001", "laminar_copied")
    assert verdict["flagged"] is False
    assert verdict["cost_usd"] == 0.0
    assert verdict["reused"] is True


def test_write_and_spend(tmp_path: Path) -> None:
    verdicts = [
        li.verdict_for(
            "tw-0001", "laminar_copied", trace_id="t1", project_id="p", model="m", entry=None
        )
    ]
    paths = li.write_verdicts(verdicts, {"laminar_copied\x00tw-0001": {"run": None}}, tmp_path)
    assert paths[0].name == "tw-0001.json"
    assert json.loads(paths[0].read_text())["raw"].endswith(".json")
    assert li.estimate_cost(40, 0.01) == 0.40
    line = li.append_spend(
        tmp_path / "spend.jsonl",
        reader="laminar_import",
        ids=["tw-0001"],
        signal_state={
            "k": {
                "run": {"input_tokens": 100, "cache_read_tokens": 10, "output_tokens": 5},
                "fired": None,
            }
        },
        signals={"copied_upstream_fix": "s"},
    )
    assert line["cost_usd"] > 0 and len(line["ids"]) == 1
