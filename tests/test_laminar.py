"""Behavioral tests for evallab.laminar: live trial export, alert spans, Signals as code."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from evallab.laminar import SIGNALS, LaminarExporter, apply_signals, trial_trace_id
from evallab.live_watch import run_watch

TRIAL = "task-1__abc"
COPY = "pip download pkg==2.0 --no-deps -d /tmp/p && cd /tmp/p && unzip -o pkg-2.0-py3-none-any.whl\n"


def _step(step_id: int, command: str, output: str) -> dict[str, Any]:
    return {
        "step_id": str(step_id),
        "timestamp": f"2026-10-05T10:00:{step_id:02d}+00:00",
        "source": "agent",
        "model_name": "m",
        "message": f"turn {step_id}",
        "tool_calls": [
            {"tool_call_id": f"c{step_id}", "function_name": "bash_command", "arguments": {"keystrokes": command}}
        ],
        "observation": {"results": [{"source_call_id": f"c{step_id}", "content": output}]},
        "metrics": {"prompt_tokens": 100, "completion_tokens": 10},
    }


STEPS = [
    {"step_id": "1", "timestamp": "2026-10-05T10:00:00+00:00", "source": "user", "message": "Fix it"},
    _step(2, "ls\n", "a.py"),
    _step(3, COPY, "Successfully downloaded pkg\n  inflating: /tmp/p/pkg/core.py\n"),
    _step(4, "cat /tmp/p/pkg/core.py\n", "def fixed(): ...\n"),
]


class Recorder:
    def __init__(self) -> None:
        self.posts: list[list[Any]] = []
        self.fail = False

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        assert headers["authorization"] == "Bearer k-123456789"
        if self.fail:
            raise OSError("unreachable")
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        self.posts.append([s for r in request.resource_spans for ss in r.scope_spans for s in ss.spans])
        return 200


def _pass(runs: Path, out: Path, recorder: Recorder, secrets: list[str] | None = None) -> dict:
    exporter = LaminarExporter(
        api_key="k-123456789", out_dir=out, endpoint="http://x", transport=recorder, secrets=secrets or []
    )
    return run_watch(runs_dirs=[runs], out_dir=out, laminar=exporter)


def _write(runs: Path, steps: list[dict], result: dict | None = None) -> Path:
    trial = runs / "job" / TRIAL
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    if result is not None:
        (trial / "result.json").write_text(json.dumps(result))
    return trial


def test_live_trial_exports_finished_steps_then_root_once(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    _write(runs, STEPS[:3])
    _pass(runs, out, rec)
    first = {s.name for s in rec.posts[-1]}
    # The last step's observation may still change, and the root waits for result.json.
    assert "turn 2" in first and "turn 3" not in first and "harbor.trial" not in first

    _write(runs, STEPS, {"finished_at": "2026-10-05T10:01:00+00:00", "verifier_result": {"rewards": {"reward": 1.0}}})
    _pass(runs, out, rec)
    second = [s for s in rec.posts[-1]]
    names = {s.name for s in second}
    assert {"turn 3", "turn 4", "harbor.trial"} <= names and "turn 2" not in names
    assert all(s.trace_id.hex() == trial_trace_id(TRIAL) for s in second)

    posted = len(rec.posts)
    _pass(runs, out, rec)
    assert len(rec.posts) == posted, "an unchanged finished trial must not be re-exported"


def test_alert_becomes_span_event_under_cited_step(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    _write(runs, STEPS)
    summary = _pass(runs, out, rec)
    fetch = next(a for a in summary["statuses"][0]["open_alerts"] if a["rule"] == "fetch_attempt")
    spans = {s.span_id.hex(): s for post in rec.posts for s in post}
    alert = next(s for s in spans.values() if s.name == "evallab.alert.fetch_attempt")
    parent = spans[alert.parent_span_id.hex()]
    step = {a.key: a.value.string_value for a in parent.attributes}["evallab.step_id"]
    assert f"head#{step}" == fetch["step_ref"] == "head#3"
    assert [e.name for e in alert.events] == ["fetch_attempt"]


def test_failed_export_is_retried_and_secrets_never_leave(tmp_path: Path) -> None:
    runs, out, rec = tmp_path / "runs", tmp_path / "out", Recorder()
    secret = "sk-live-0123456789abcdef"
    steps = [*STEPS[:2], _step(3, f"echo {secret}\n", secret), STEPS[3]]
    _write(runs, steps, {"finished_at": "2026-10-05T10:01:00+00:00"})
    rec.fail = True
    failed = _pass(runs, out, rec, secrets=[secret])
    assert failed["laminar"]["ok"] is False and not rec.posts
    rec.fail = False
    _pass(runs, out, rec, secrets=[secret])
    assert {"turn 2", "turn 3", "harbor.trial"} <= {s.name for s in rec.posts[-1]}
    assert secret.encode() not in b"".join(s.SerializeToString() for s in rec.posts[-1])


def test_apply_signals_creates_missing_and_patches_existing() -> None:
    calls: list[tuple[str, str, Any]] = []
    existing = {"signals": [{"id": "s-1", "name": "stuck_loop"}, {"id": "s-9", "name": "other"}]}

    def opener(request: Any, timeout: float) -> Any:
        body = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), request.full_url.split("api.lmnr.ai")[1], body))
        reply = existing if request.get_method() == "GET" else {**(body or {}), "id": "new", "version": 1}
        return io.BytesIO(json.dumps(reply).encode())

    apply_signals(api_key="k", opener=opener)
    writes = {(method, path, (body or {}).get("name")) for method, path, body in calls[1:]}
    assert ("PATCH", "/v1/signals/s-1", "stuck_loop") in writes
    created = {name for method, _, name in writes if method == "POST"}
    assert created == {d["name"] for d in SIGNALS} - {"stuck_loop"}
    assert all(body["filters"] == [] for _, _, body in calls[1:]), "every trace must be evaluated"
    for _, _, body in calls[1:]:
        schema = body["structuredOutput"]
        assert schema["required"] == list(schema["properties"]), "the API rejects schemas without required"


def test_byok_route_is_sent_with_every_signal() -> None:
    bodies: list[Any] = []

    def opener(request: Any, timeout: float) -> Any:
        if request.get_method() == "GET":
            return io.BytesIO(b'{"signals": []}')
        bodies.append(json.loads(request.data))
        return io.BytesIO(json.dumps({**bodies[-1], "id": "x", "version": 1}).encode())

    apply_signals(api_key="k", opener=opener, llm_profile_id="p-1", model="glm-5.3-flash")
    assert len(bodies) == len(SIGNALS)
    assert all((b["llmProfileId"], b["model"]) == ("p-1", "glm-5.3-flash") for b in bodies)
