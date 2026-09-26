"""Unit tests for HAR-74 Reef traffic: spec validation, pinning, proxy, reports.

All Reef and transport peers are in-process fakes on ephemeral loopback ports:
no network, no host state, no wall clock. Behavior, not source text.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from evallab.evidence_store import evidence_tree_digest
from evallab.execution_contracts import ReefTrafficBinding, RunRequest
from evallab.reef_traffic import (
    HeldoutCheckError,
    HeldoutRefusal,
    ReefCaptureProxy,
    ReefDriftError,
    ReefTrafficError,
    build_report_payload,
    canonical_files_digest,
    check_drift,
    check_task_allowed,
    heldout_uses_for_task,
    post_report,
    pull_and_pin,
    pull_manifest,
    reef_evidence_block,
    refuse_if_heldout,
    report_job_trials,
    served_files_digest,
    trial_report_decision,
    write_served_tree,
)
from evallab.schemas import ExperimentSpec, ReefTrafficSpec
from evallab.terminus_harness import load_harness_tree

TOKEN = "test-token-reef-traffic"
SCENARIO = "har74-unit"
OTHER_SCENARIO = "har74-other"


class FakeReef(http.server.BaseHTTPRequestHandler):
    """Deterministic Reef service double: harness, inference, reports."""

    server_version = "FakeReef"

    def log_message(self, *args: object) -> None:
        pass

    @property
    def state(self) -> dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self.state["requests"].append(("GET", self.path, dict(self.headers)))
        if self.path == "/healthz" or self.path.startswith("/reef/harness"):
            release = None
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
                release = params.get("release_id")
            manifest = self.state["manifest"](release)
            self._send(200, manifest, {"x-reef-release-id": manifest["release_id"]})
            return
        self._send(404, {"error": "no such route"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        self.state["requests"].append(("POST", self.path, dict(self.headers), body))
        scenario = self.headers.get("x-reef-scenario")
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "bad token"})
            return
        if self.path == "/v1/chat/completions":
            self.state["calls"] += 1
            receipt = f"inference-{self.state['calls']:04d}"
            self.state["inference"][receipt] = {"scenario": scenario, "headers": dict(self.headers)}
            self._send(
                200,
                {
                    "id": f"chatcmpl-{receipt}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                },
                {"x-reef-agent-record-id": receipt, "x-reef-release-id": "rel-test"},
            )
            return
        if self.path == "/reef/report":
            report_id = body.get("agent_record_id")
            references = body.get("references", [])
            if not isinstance(report_id, str) or not report_id:
                self._send(400, {"error": "agent_record_id required"})
                return
            known = self.state["inference"]
            if any(ref not in known or known[ref]["scenario"] != scenario for ref in references):
                self._send(400, {"error": "unknown or foreign reference"})
                return
            stored = self.state["reports"].get(report_id)
            canonical = json.dumps(body, sort_keys=True)
            if stored is not None:
                if stored != canonical:
                    self._send(409, {"error": "conflicting id"})
                    return
                self._send(200, {"agent_record_id": report_id, "scenario": scenario, "request_type": "report"})
                return
            self.state["reports"][report_id] = canonical
            self._send(200, {"agent_record_id": report_id, "scenario": scenario, "request_type": "report"})
            return
        self._send(404, {"error": "no such route"})


@pytest.fixture()
def reef() -> Any:
    files = {
        "terminus/config.json": json.dumps({"max_turns": 4}),
        "terminus/AGENTS.md": "Be concise.\n",
    }
    state: dict[str, Any] = {
        "requests": [],
        "calls": 0,
        "inference": {},
        "reports": {},
        "files": dict(files),
        "release_id": "rel-001",
        "content_id": "content:aaa",
    }

    def manifest(release: str | None) -> dict[str, Any]:
        rid = release or state["release_id"]
        return {
            "release_id": rid,
            "content_id": state["content_id"],
            "parent_release_id": None,
            "files": dict(state["files"]),
            "evaluation": {},
            "requires": [],
        }

    state["manifest"] = manifest
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeReef)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(reef: Any) -> str:
    return f"http://127.0.0.1:{reef.server_address[1]}"


def _valid_reef_kwargs(url: str) -> dict[str, Any]:
    return {
        "url": url,
        "scenario": SCENARIO,
        "token_env": "HAR74_TEST_TOKEN",
        "release_id": "rel-001",
        "content_id": "content:aaa",
    }


def _base_spec(url: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "har74-unit-spec",
        "hypothesis": "reef traffic wiring",
        "purpose": "baseline",
        "task": "library/tasks/event-summary",
        "task_path": "library/tasks/event-summary",
        "agent": "terminus-2",
        "model": "ollama_chat/qwen2.5:7b",
        "submitted_by": "tester",
        "harness_tree_path": "runs/.prepared-harnesses/abc",
        "harness_tree_sha256": "sha256:" + "0" * 64,
    }
    params.update(overrides)
    params["reef"] = ReefTrafficSpec(**_valid_reef_kwargs(url))
    return params


# -- spec validation -------------------------------------------------------- #


def test_reef_spec_valid_and_pinned_together() -> None:
    spec = ReefTrafficSpec(**_valid_reef_kwargs("http://127.0.0.1:18941/"))
    assert spec.url == "http://127.0.0.1:18941"
    with pytest.raises(ValueError):
        ReefTrafficSpec(
            url="http://127.0.0.1:18941",
            scenario=SCENARIO,
            token_env="HAR74_TEST_TOKEN",
            release_id="rel-001",
        )


def test_reef_spec_rejects_bad_url_scenario_token_env() -> None:
    good = _valid_reef_kwargs("http://127.0.0.1:18941")
    for bad_url in ("https://example.com:8900", "http://192.168.1.2:8900", "not-a-url", ""):
        with pytest.raises(ValueError):
            ReefTrafficSpec(**{**good, "url": bad_url})
    for bad in ("", "has space", "UPPER!", "a" * 65):
        with pytest.raises(ValueError):
            ReefTrafficSpec(**{**good, "scenario": bad})
    for bad in ("lowercase", "has space", "DASH-NAME", ""):
        with pytest.raises(ValueError):
            ReefTrafficSpec(**{**good, "token_env": bad})


def test_experiment_spec_reef_rules() -> None:
    url = "http://127.0.0.1:18941"
    ExperimentSpec(**_base_spec(url))
    with pytest.raises(ValueError, match="only by terminus-2"):
        ExperimentSpec(**_base_spec(url, agent="mini-swe-agent"))
    with pytest.raises(ValueError, match="local terminus route"):
        ExperimentSpec(**_base_spec(url, model="zai/glm-5.3-flash"))
    unpinned = _valid_reef_kwargs(url)
    del unpinned["release_id"]
    del unpinned["content_id"]
    with pytest.raises(ValueError, match="release_id and content_id"):
        ExperimentSpec(**{**_base_spec(url), "reef": ReefTrafficSpec(**unpinned)})
    with pytest.raises(ValueError, match="harness_tree"):
        ExperimentSpec(**{**_base_spec(url), "harness_tree_path": None, "harness_tree_sha256": None})


def test_run_request_reef_requires_pin(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[task]\nname = 't'\n", encoding="utf-8")
    binding = ReefTrafficBinding(
        url="http://127.0.0.1:18941",
        scenario=SCENARIO,
        token_env="HAR74_TEST_TOKEN",
        release_id="rel-001",
        content_id="content:aaa",
    )
    from evallab.execution_contracts import validate_request

    with pytest.raises(ValueError, match="pinned harness tree"):
        validate_request(
            RunRequest(
                task=task,
                agent="terminus-2",
                name="har74-x",
                jobs_dir=tmp_path,
                model="ollama_chat/qwen2.5:7b",
                allow_billable=True,
                reef=binding,
            )
        )
# -- pinning and drift ------------------------------------------------------ #


def test_pin_matches_har71_and_writes_no_sidecars(reef: Any, tmp_path: Path) -> None:
    pinned = pull_and_pin(_url(reef), SCENARIO, TOKEN, tmp_path / "pull")
    assert load_harness_tree(tmp_path / "pull").sha256 == pinned.digest
    assert pinned.release_id == "rel-001"
    assert pinned.content_id == "content:aaa"
    names = sorted(path.relative_to(tmp_path / "pull").as_posix() for path in (tmp_path / "pull").rglob("*") if path.is_file())
    assert names == ["terminus/AGENTS.md", "terminus/config.json"]


def test_same_files_different_release_pin_identically(reef: Any, tmp_path: Path) -> None:
    first = pull_and_pin(_url(reef), SCENARIO, TOKEN, tmp_path / "first")
    reef.state["release_id"] = "rel-002"  # noqa: SLF001 - fake state
    reef.state["content_id"] = "content:bbb"  # noqa: SLF001 - fake state
    second = pull_and_pin(_url(reef), SCENARIO, TOKEN, tmp_path / "second")
    assert first.digest == second.digest
    assert first.release_id != second.release_id


def test_framing_parity_with_evidence_digest(tmp_path: Path) -> None:
    files = {"terminus/config.json": '{"max_turns": 4}', "terminus/AGENTS.md": "Be concise.\n"}
    assert served_files_digest(files) == canonical_files_digest(
        {name: text.encode() for name, text in files.items()}
    )
    target = tmp_path / "tree"
    write_served_tree(files, target)
    assert evidence_tree_digest(target) == served_files_digest(files)


def test_unsafe_served_paths_refused_atomically(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(ReefTrafficError):
        write_served_tree({"ok/file.txt": "x", "../escape": "y"}, dest)
    with pytest.raises(ReefTrafficError):
        write_served_tree({"/abs/path": "x"}, dest)
    with pytest.raises(ReefTrafficError):
        write_served_tree({".reef-harness-version": "{}"}, dest)
    assert not dest.exists() or list(dest.rglob("*")) == []


def test_drift_release_change_is_provenance_not_drift(reef: Any, tmp_path: Path) -> None:
    pinned = pull_and_pin(_url(reef), SCENARIO, TOKEN, tmp_path / "pull")
    reef.state["release_id"] = "rel-002"  # noqa: SLF001 - fake state
    record = check_drift(
        _url(reef), SCENARIO, TOKEN, pinned_digest=pinned.digest, pinned_release_id=pinned.release_id
    )
    assert record.digest_matches and record.release_changed
    assert record.served_release_id == "rel-002"


def test_drift_changed_files_refuse(reef: Any, tmp_path: Path) -> None:
    pinned = pull_and_pin(_url(reef), SCENARIO, TOKEN, tmp_path / "pull")
    reef.state["files"]["terminus/config.json"] = json.dumps({"max_turns": 9})  # noqa: SLF001
    with pytest.raises(ReefDriftError):
        check_drift(
            _url(reef), SCENARIO, TOKEN, pinned_digest=pinned.digest, pinned_release_id=pinned.release_id
        )


def test_manifest_pin_by_release_id(reef: Any) -> None:
    manifest = pull_manifest(_url(reef), SCENARIO, TOKEN, release_id="rel-000")
    assert manifest.release_id == "rel-000"
    assert any("?release_id=" in request[1] for request in reef.state["requests"])  # noqa: SLF001


# -- capture proxy ---------------------------------------------------------- #


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}", **headers},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read()), {key.lower(): value for key, value in response.getheaders()}


def test_proxy_injects_headers_overrides_auth_and_captures_receipts(reef: Any) -> None:
    proxy = ReefCaptureProxy(
        _url(reef), SCENARIO, TOKEN, tags={"task": "smoke-search", "episode": "ep-001"}
    )
    proxy.start()
    try:
        body, headers = _post(
            proxy.url + "/v1/chat/completions",
            {"model": "smoke-model", "messages": [{"role": "user", "content": "hi"}]},
            {"Authorization": "Bearer wrong"},
        )
        assert body["choices"][0]["message"]["content"] == "ok"
        assert headers["x-reef-agent-record-id"].startswith("inference-")
    finally:
        proxy.stop()
    (turn,) = proxy.drain()
    assert turn.receipt is not None and turn.status == 200
    assert turn.tags == {"task": "smoke-search", "episode": "ep-001"}
    seen = reef.state["inference"][turn.receipt]["headers"]  # noqa: SLF001
    lowered = {key.lower(): value for key, value in seen.items()}
    assert lowered["x-reef-scenario"] == SCENARIO
    assert lowered["x-reef-tag-task"] == "smoke-search"
    assert lowered["x-reef-tag-episode"] == "ep-001"
    assert lowered["authorization"] == f"Bearer {TOKEN}"


def test_proxy_drain_returns_tagged_turns_and_empties(reef: Any) -> None:
    proxy = ReefCaptureProxy(_url(reef), SCENARIO, TOKEN, tags={"task": "t", "episode": "e"})
    proxy.start()
    try:
        _post(
            proxy.url + "/v1/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            {},
        )
    finally:
        proxy.stop()
    (turn,) = proxy.drain()
    assert turn.status == 200 and turn.receipt is not None
    assert turn.tags == {"task": "t", "episode": "e"}
    assert proxy.drain() == []


# -- reports ---------------------------------------------------------------- #


def test_report_payload_shape_has_no_feedback() -> None:
    payload = build_report_payload(
        trial_id="trial-1",
        score=1.0,
        references=["inference-0001", "inference-0001"],
        task_name="smoke-search",
        task_path="smoke/smoke-search",
        task_digest="sha256:" + "0" * 63 + "1",
    )
    assert set(payload) == {"agent_record_id", "score", "references", "metadata"}
    assert "feedback" not in payload
    assert payload["references"] == ["inference-0001"]
    assert payload["metadata"] == {
        "task": {"name": "smoke-search", "path": "smoke/smoke-search", "digest": "sha256:" + "0" * 63 + "1"}
    }
    with pytest.raises(ValueError):
        build_report_payload(
            trial_id="trial-1", score=1.0, references=[],
            task_name="t", task_path="p", task_digest="sha256:" + "0" * 64,
        )


def test_report_accepted_resent_and_conflicts(reef: Any) -> None:
    _post(_url(reef) + "/v1/chat/completions", {"model": "m", "messages": []}, {"x-reef-scenario": SCENARIO})
    receipt = next(iter(reef.state["inference"]))  # noqa: SLF001
    payload = build_report_payload(
        trial_id="trial-1", score=1.0, references=[receipt],
        task_name="t", task_path="p", task_digest="sha256:" + "0" * 64,
    )
    first = post_report(_url(reef), SCENARIO, TOKEN, payload)
    assert first == "trial-1"
    assert post_report(_url(reef), SCENARIO, TOKEN, payload) == "trial-1"
    clash = dict(payload, score=0.0)
    with pytest.raises(ReefTrafficError) as excinfo:
        post_report(_url(reef), SCENARIO, TOKEN, clash)
    assert excinfo.value.status == 409


def test_report_rejects_missing_and_foreign_references(reef: Any) -> None:
    payload = build_report_payload(
        trial_id="trial-x", score=1.0, references=["no-such-receipt"],
        task_name="t", task_path="p", task_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(ReefTrafficError) as excinfo:
        post_report(_url(reef), SCENARIO, TOKEN, payload)
    assert excinfo.value.status == 400
    _post(_url(reef) + "/v1/chat/completions", {"model": "m", "messages": []}, {"x-reef-scenario": OTHER_SCENARIO})
    foreign = next(rid for rid, row in reef.state["inference"].items() if row["scenario"] == OTHER_SCENARIO)  # noqa: SLF001
    payload = build_report_payload(
        trial_id="trial-y", score=1.0, references=[foreign],
        task_name="t", task_path="p", task_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(ReefTrafficError) as excinfo:
        post_report(_url(reef), SCENARIO, TOKEN, payload)
    assert excinfo.value.status == 400


def _trial(result: dict[str, Any], rewards: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return ("trial-1", result, rewards)


def test_trial_decision_matrix() -> None:
    ok, _ = trial_report_decision(
        result={"finished_at": "2026-09-25T00:00:00Z"}, rewards={"reward": 1.0}
    )
    assert ok == 1.0
    cases = [
        ({"exception_info": {"class": "Boom"}, "finished_at": "x"}, {"reward": 1.0}, "infra-failed"),
        ({}, {"reward": 1.0}, "infra-failed"),
        ({"finished_at": "x"}, {}, "unscored"),
        ({"finished_at": "x"}, {"reward": True}, "unscored"),
        ({"finished_at": "x"}, {"reward": float("nan")}, "unscored"),
        ({"finished_at": "x"}, {"reward": float("inf")}, "unscored"),
        ({"finished_at": "x"}, {"reward": 0.0}, None),
    ]
    for result, rewards, expect in cases:
        score, reason = trial_report_decision(result=result, rewards=rewards)
        if expect is None:
            assert score == 0.0, reason
        else:
            assert score is None and reason.startswith(expect), (result, rewards, reason)


def test_report_job_trials_reports_single_scored_trial(reef: Any) -> None:
    _post(_url(reef) + "/v1/chat/completions", {"model": "m", "messages": []}, {"x-reef-scenario": SCENARIO})
    receipt = next(iter(reef.state["inference"]))  # noqa: SLF001
    digest = "sha256:" + "0" * 64
    (report,) = report_job_trials(
        url=_url(reef), scenario=SCENARIO, token=TOKEN,
        task_name="t", task_path="p", task_digest=digest,
        trials=[_trial({"finished_at": "x"}, {"reward": 1.0})],
        receipts=[receipt],
    )
    assert (report.trial_id, report.status, report.report_id) == ("trial-1", "reported", "trial-1")
    assert "feedback" not in json.dumps(reef.state["requests"])  # noqa: SLF001


def test_report_job_trials_skips_single_unscored_trial(reef: Any) -> None:
    digest = "sha256:" + "0" * 64
    (report,) = report_job_trials(
        url=_url(reef), scenario=SCENARIO, token=TOKEN,
        task_name="t", task_path="p", task_digest=digest,
        trials=[_trial({"finished_at": "x"}, {})],
        receipts=["inference-9999"],
    )
    assert report.status == "skipped"
    assert reef.state["reports"] == {}  # noqa: SLF001


def test_report_job_trials_refuses_multi_trial_job(reef: Any) -> None:
    digest = "sha256:" + "0" * 64
    with pytest.raises(ReefTrafficError, match="exactly one trial"):
        report_job_trials(
            url=_url(reef), scenario=SCENARIO, token=TOKEN,
            task_name="t", task_path="p", task_digest=digest,
            trials=[
                ("trial-a", {"finished_at": "x"}, {"reward": 1.0}),
                ("trial-b", {"finished_at": "x"}, {"reward": 0.0}),
            ],
            receipts=["rc-A1", "rc-B1"],
        )
    posted = [entry for entry in reef.state["requests"] if entry[1] == "/reef/report"]  # noqa: SLF001
    assert posted == []


def test_report_job_trials_needs_receipts(reef: Any) -> None:
    digest = "sha256:" + "0" * 64
    (report,) = report_job_trials(
        url=_url(reef), scenario=SCENARIO, token=TOKEN,
        task_name="t", task_path="p", task_digest=digest,
        trials=[_trial({"finished_at": "x"}, {"reward": 1.0})],
        receipts=[],
    )
    assert report.status == "skipped" and report.reason == "no_receipts"
    assert reef.state["reports"] == {}  # noqa: SLF001


# -- held-out refusal ------------------------------------------------------- #


def _write_registry_record(
    root: Path,
    *,
    allowed_uses: list[str],
    task_id: str = "heldout-demo",
    task_path: str = "library/tasks/heldout-demo",
    package_digest: str = "sha256:" + "1" * 64,
) -> Path:
    record = {
        "schema_version": 2,
        "task_id": task_id,
        "task_family": "demo",
        "version": "1.0.0",
        "task_path": task_path,
        "digests": {
            "task_toml": "sha256:" + "0" * 64,
            "package": package_digest,
            "instruction": "sha256:" + "2" * 64,
            "environment": "sha256:" + "3" * 64,
            "verifier": "sha256:" + "4" * 64,
        },
        "source_uri": "local",
        "provenance_zone": "02-local-evidence",
        "is_synthetic": False,
        "state": "candidate",
        "state_reason": "awaiting_review",
        "allowed_uses": allowed_uses,
    }
    registry = root / "library" / "registry"
    registry.mkdir(parents=True)
    path = registry / f"{task_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    task_dir = root / "library" / "tasks" / "heldout-demo"
    task_dir.mkdir(parents=True)
    return task_dir


def test_heldout_refusal_before_any_reef_call(reef: Any, tmp_path: Path) -> None:
    task_dir = _write_registry_record(tmp_path, allowed_uses=["measurement", "heldout"])
    calls_before = len(reef.state["requests"])  # noqa: SLF001
    with pytest.raises(HeldoutRefusal):
        refuse_if_heldout(["measurement", "heldout"], task_label="heldout-demo")
    with pytest.raises(HeldoutRefusal):
        check_task_allowed(tmp_path, task_label="heldout-demo", task_id="heldout-demo")
    with pytest.raises(HeldoutRefusal):
        check_task_allowed(tmp_path, task_label="heldout-demo", task_dir=task_dir)
    assert len(reef.state["requests"]) == calls_before  # noqa: SLF001 - no Reef call happened


def test_non_heldout_and_unregistered_tasks_pass(tmp_path: Path) -> None:
    task_dir = _write_registry_record(tmp_path, allowed_uses=["measurement"])
    check_task_allowed(tmp_path, task_label="heldout-demo", task_id="heldout-demo")
    check_task_allowed(tmp_path, task_label="heldout-demo", task_dir=task_dir)
    check_task_allowed(tmp_path, task_label="local-only", task_dir=tmp_path / "elsewhere")


# -- no secret in evidence -------------------------------------------------- #


def test_no_secret_in_evidence_blocks() -> None:
    token = "super-secret-reef-token-value"
    payload = build_report_payload(
        trial_id="t", score=1.0, references=["r"],
        task_name="t", task_path="p", task_digest="sha256:" + "0" * 64,
    )
    assert token not in json.dumps(payload)
    block = reef_evidence_block(
        url="http://127.0.0.1:18941",
        scenario=SCENARIO,
        release_id="rel-001",
        content_id="content:aaa",
        digest="sha256:" + "0" * 64,
        drift=None,
        receipts=["inference-0001"],
        reports=[],
    )
    dumped = json.dumps(block)
    assert token not in dumped
    assert "super-secret" not in dumped
    assert block["scenario"] == SCENARIO


# -- single-trial contract ---------------------------------------------------- #


def test_reef_spec_rejects_multi_attempt() -> None:
    url = "http://127.0.0.1:18941"
    with pytest.raises(ValueError, match="exactly one trial"):
        ExperimentSpec(**_base_spec(url, attempts=2))


def test_run_request_reef_rejects_multi_attempt(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[task]\nname = 't'\n", encoding="utf-8")
    binding = ReefTrafficBinding(
        url="http://127.0.0.1:18941",
        scenario=SCENARIO,
        token_env="HAR74_TEST_TOKEN",
        release_id="rel-001",
        content_id="content:aaa",
    )
    from evallab.execution_contracts import validate_request

    with pytest.raises(ValueError, match="exactly one trial"):
        validate_request(
            RunRequest(
                task=task,
                agent="terminus-2",
                name="har74-x",
                jobs_dir=tmp_path,
                model="ollama_chat/qwen2.5:7b",
                allow_billable=True,
                attempts=2,
                reef=binding,
            )
        )


# -- url userinfo ------------------------------------------------------------- #


def test_reef_url_rejects_userinfo() -> None:
    from evallab.reef_traffic import check_url

    with pytest.raises(ValueError, match="must not carry credentials"):
        check_url("http://user:pass@127.0.0.1:18941")
    with pytest.raises(ValueError, match="must not carry credentials"):
        check_url("http://token-only@127.0.0.1:18941/")
    with pytest.raises(ValueError, match="loopback URL|not carry credentials"):
        ReefTrafficSpec(**{**_valid_reef_kwargs("http://127.0.0.1:18941"), "url": "http://u@127.0.0.1:18941"})


def test_reef_url_refusal_never_echoes_userinfo() -> None:
    from evallab.reef_traffic import check_url

    for bad in (
        "http://user:s3cret@127.0.0.1:18941",
        "http://user:s3cret@127.0.0.1:18941/v1",
        "http://user:s3cret@example.com:8900",
    ):
        with pytest.raises(ValueError) as excinfo:
            check_url(bad)
        assert "s3cret" not in str(excinfo.value)
        assert "user" not in str(excinfo.value)


# -- registry matching and corruption ----------------------------------------- #


def test_heldout_renamed_copy_matches_by_digest(reef: Any, tmp_path: Path) -> None:
    import shutil

    from evallab.registry import compute_task_digests

    original = _demo_task(tmp_path / "original")
    digest = compute_task_digests(original).package
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_registry_record(
        repo, allowed_uses=["measurement", "heldout"], package_digest=digest
    )
    renamed = tmp_path / "renamed-copy"
    shutil.copytree(original, renamed)
    calls_before = len(reef.state["requests"])  # noqa: SLF001 - fake state
    with pytest.raises(HeldoutRefusal):
        check_task_allowed(repo, task_label="renamed", task_dir=renamed, package_digest=digest)
    with pytest.raises(HeldoutRefusal):
        check_task_allowed(repo, task_label="by-path", task_path="library/tasks/heldout-demo")
    assert len(reef.state["requests"]) == calls_before  # noqa: SLF001 - no Reef call happened


def test_corrupt_registry_refuses_loudly(tmp_path: Path) -> None:
    registry = tmp_path / "library" / "registry"
    registry.mkdir(parents=True)
    (registry / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(HeldoutCheckError):
        heldout_uses_for_task(tmp_path, task_id="anything")
    with pytest.raises(HeldoutCheckError):
        check_task_allowed(tmp_path, task_label="anything", task_dir=tmp_path)


def test_missing_registry_dir_stays_silent(tmp_path: Path) -> None:
    assert heldout_uses_for_task(tmp_path, task_id="anything") is None
    check_task_allowed(tmp_path, task_label="anything", task_dir=tmp_path)


# -- prepare staging ---------------------------------------------------------- #


def _demo_task(root: Path) -> Path:
    package = root / "demo-task"
    package.mkdir(parents=True)
    (package / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "lab/demo-task"\nversion = "1.2.0"\n'
        "[agent]\ntimeout_sec = 120.0\n[verifier]\ntimeout_sec = 60.0\n"
        "[environment]\ncpus = 1\nmemory_mb = 512\n",
        encoding="utf-8",
    )
    (package / "instruction.md").write_text("Do the demo thing.\n", encoding="utf-8")
    environment = package / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    tests = package / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
    return package


def test_prepare_stages_into_repo_and_cleans_tempdir(
    reef: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    from evallab.task_prepare import prepare_task

    repo = tmp_path / "repo"
    repo.mkdir()
    source = _demo_task(tmp_path / "external")
    monkeypatch.setenv("HAR74_TEST_TOKEN", TOKEN)
    pulled_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        pulled_dirs.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    prepared = prepare_task(
        repo,
        source,
        name="har74-staged",
        agent="terminus-2",
        model="ollama_chat/qwen2.5:7b",
        environment="docker",
        reef_url=_url(reef),
        reef_scenario=SCENARIO,
        reef_token_env="HAR74_TEST_TOKEN",
    )
    assert prepared.spec.harness_tree_path is not None
    assert prepared.spec.harness_tree_path.startswith("runs/.prepared-harnesses/")
    assert (repo / prepared.spec.harness_tree_path).is_dir()
    assert prepared.spec.reef is not None and prepared.spec.reef.release_id == "rel-001"
    assert pulled_dirs, "expected the pull to stage through a tempdir"
    assert all(not Path(path).exists() for path in pulled_dirs)


def test_prepare_heldout_by_digest_before_any_reef_call(
    reef: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.registry import compute_task_digests
    from evallab.task_prepare import prepare_task

    repo = tmp_path / "repo"
    repo.mkdir()
    source = _demo_task(tmp_path / "external")
    digest = compute_task_digests(source).package
    _write_registry_record(repo, allowed_uses=["measurement", "heldout"], package_digest=digest)
    monkeypatch.setenv("HAR74_TEST_TOKEN", TOKEN)
    calls_before = len(reef.state["requests"])  # noqa: SLF001 - fake state
    with pytest.raises(HeldoutRefusal):
        prepare_task(
            repo,
            source,
            name="har74-heldout",
            agent="terminus-2",
            model="ollama_chat/qwen2.5:7b",
            environment="docker",
            reef_url=_url(reef),
            reef_scenario=SCENARIO,
            reef_token_env="HAR74_TEST_TOKEN",
        )
    assert len(reef.state["requests"]) == calls_before  # noqa: SLF001 - no Reef call happened


def test_prepare_corrupt_registry_before_any_reef_call(
    reef: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.task_prepare import prepare_task

    repo = tmp_path / "repo"
    repo.mkdir()
    source = _demo_task(tmp_path / "external")
    registry = repo / "library" / "registry"
    registry.mkdir(parents=True)
    (registry / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HAR74_TEST_TOKEN", TOKEN)
    calls_before = len(reef.state["requests"])  # noqa: SLF001 - fake state
    with pytest.raises(HeldoutCheckError):
        prepare_task(
            repo,
            source,
            name="har74-broken-registry",
            agent="terminus-2",
            model="ollama_chat/qwen2.5:7b",
            environment="docker",
            reef_url=_url(reef),
            reef_scenario=SCENARIO,
            reef_token_env="HAR74_TEST_TOKEN",
        )
    assert len(reef.state["requests"]) == calls_before  # noqa: SLF001 - no Reef call happened
