"""Behavioural contracts for the flight recorder (fixture-only, no Docker)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from evallab.flight.gateway import MetadataGateway, client_hello_sni, dns_metadata
from evallab.flight.invisibility import canonicalize, diff_probes
from evallab.flight.timeline import build_timeline

TRIAL_ID = "00000000-0000-0000-0000-000000000042"
JOB_ID = "00000000-0000-0000-0000-000000000007"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _result(*, agent: str = "test-agent") -> dict[str, object]:
    return {
        "id": TRIAL_ID,
        "trial_name": "job__attempt",
        "task_name": "local-lab/fixture-task",
        "agent_info": {"name": agent, "version": "1.0.0", "model_info": None},
        "agent_execution": {
            "started_at": "2026-10-08T21:30:49.000000Z",
            "finished_at": "2026-10-08T21:30:59.000000Z",
        },
        "verifier": {
            "started_at": "2026-10-08T21:31:05.000000Z",
            "finished_at": "2026-10-08T21:31:10.000000Z",
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
        "config": {},
    }


def _trial_with_planes(root: Path, *, agent: str = "test-agent") -> tuple[Path, Path]:
    job = root / "job"
    trial = job / "job__attempt"
    _write_json(job / "result.json", {"id": JOB_ID})
    _write_json(
        job / "lab-metadata.json",
        {
            "provider_usage": {"attempt_id": "tok123"},
            "model_capture": {"capture_dir": str(root / "cap")},
        },
    )
    (root / "cap").mkdir(parents=True, exist_ok=True)
    (root / "cap" / "calls.jsonl").write_text(
        json.dumps(
            {
                "schema": "evallab.model_call/v1",
                "seq": 1,
                "started_at": "2026-10-08T21:30:50.000000Z",
                "ended_at": "2026-10-08T21:30:51.000000Z",
                "method": "POST",
                "path": "/t/tok123/v1/chat/completions",
                "route_token": "tok123",
                "request_body": {"messages": [{"role": "user", "content": "do it"}]},
                "response_body": {"choices": [{"message": {"content": "done"}}]},
                "assistant_texts": ["done"],
                "tool_calls": [{"name": "bash", "arguments": "{}"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                "model": "fixture-model",
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(trial / "result.json", _result(agent=agent))
    _write_json(
        trial / "agent" / "trajectory.json",
        {
            "steps": [
                {
                    "timestamp": "2026-10-08T21:30:50.500000Z",
                    "role": "assistant",
                    "content": "running tests",
                    "tool_calls": [{"name": "bash", "arguments": "pytest -q"}],
                }
            ]
        },
    )
    flight = trial / "flight"
    (flight / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (flight / "events.jsonl").write_text(
        "\n".join(
            json.dumps(row, sort_keys=True)
            for row in [
                {
                    "schema_version": 1,
                    "kind": "exec",
                    "ts": "2026-10-08T21:30:49.500000Z",
                    "plane": "kernel",
                    "pid": 7,
                    "comm": "pytest",
                    "path": "/usr/bin/pytest",
                },
                {
                    "schema_version": 1,
                    "kind": "dns",
                    "ts": "2026-10-08T21:30:52.000000Z",
                    "plane": "egress",
                    "port": 53,
                    "dest": "8.8.8.8",
                },
                {
                    "schema_version": 1,
                    "kind": "packet",
                    "ts": "2026-10-08T21:31:06.000000Z",
                    "plane": "egress",
                    "src": "a",
                    "dst": "b",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        flight / "filediff.agent.json",
        {
            "schema_version": 1,
            "/app": {
                "added": [],
                "removed": [],
                "modified": ["output/summary.json"],
                "added_count": 0,
                "removed_count": 0,
                "modified_count": 1,
                "truncated": False,
            },
        },
    )
    _write_json(trial / "verifier" / "reward.json", {"reward": 1.0})
    return trial, job


def test_timeline_joins_all_planes_with_ids_on_every_row(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    summary = build_timeline(trial, job)

    assert summary["model_calls"] == 1
    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows, "timeline must not be empty"
    for row in rows:
        assert row["trial_id"] == TRIAL_ID
        assert row["job_id"] == JOB_ID
        assert row["task"] == "local-lab/fixture-task"
    planes = {row["plane"] for row in rows}
    assert {"phase", "model", "trajectory", "tool", "kernel", "egress", "file", "verifier"} <= planes

    stamped = [row["ts"] for row in rows if row["ts"] is not None]
    assert stamped == sorted(stamped), "timeline must be timestamp ordered"

    calls = [row for row in rows if row["plane"] == "model" and row["kind"] == "model_call"]
    assert len(calls) == 1
    detail = calls[0]["detail"]
    assert detail["request_body"]["messages"][0]["content"] == "do it"
    assert detail["response_body"]["choices"][0]["message"]["content"] == "done"
    assert detail["assistant_texts"] == ["done"]
    assert detail["usage"] == {"prompt_tokens": 3, "completion_tokens": 1}


def test_control_agent_empty_model_plane_is_expected_not_missing(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "job__attempt"
    _write_json(job / "result.json", {"id": JOB_ID})
    _write_json(trial / "result.json", _result(agent="oracle"))

    summary = build_timeline(trial, job)

    assert summary["model_calls"] == 0
    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    verdicts = [row for row in rows if row["kind"] == "model_verdict"]
    assert verdicts and all(
        v["detail"]["verdict"] == "control-agent-expected-empty" for v in verdicts
    )


def test_model_agent_without_capture_reports_missing(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "job__attempt"
    _write_json(job / "result.json", {"id": JOB_ID})
    _write_json(trial / "result.json", _result(agent="terminus"))

    build_timeline(trial, job)

    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    verdicts = [row for row in rows if row["kind"] == "model_verdict"]
    assert verdicts and verdicts[0]["detail"]["verdict"] == "capture-missing"


def test_flight_plugin_wiring_is_opt_in(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from evallab.flight.plugin import FlightRecorderPlugin, configuration_path

    queue = SimpleNamespace(_setup_hooks=lambda _trial: None)
    callbacks: list[object] = []
    job = SimpleNamespace(job_dir=tmp_path / "runs" / "job", _trial_queue=queue,
                          on_agent_ended=callbacks.append, on_trial_ended=callbacks.append,
                          on_trial_cancelled=callbacks.append, on_verification_started=callbacks.append)
    plain = FlightRecorderPlugin()
    asyncio.run(plain.on_job_start(job))
    assert not callbacks
    assert plain.image is None

    async def image() -> str:
        return "fixture-image"

    monkeypatch.setattr("evallab.flight.observer.ensure_image", image)
    path = configuration_path(job.job_dir)
    _write_json(path, {"schema": "evallab.flight.config/v1", "egress": "locked"})
    job.job_dir.mkdir()
    recorded = FlightRecorderPlugin()
    original = queue._setup_hooks
    asyncio.run(recorded.on_job_start(job))
    assert recorded.image == "fixture-image"
    assert len(callbacks) == 4
    assert queue._setup_hooks is not original
    asyncio.run(recorded.on_job_end(None))
    assert queue._setup_hooks is original


def _probe_dump(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ps": [["sleep", "sleep 300"], ["python3", "python3 /tmp/invisibility-probe.py"]],
        "env": ["HOME=/root", "HOSTNAME=abc123", "PATH=/usr/bin"],
        "mounts": ["overlay / fuse-overlayfs rw 0 0"],
        "mountinfo": ["1 2 0:1 / / rw - overlay overlay rw"],
        "inotify": {"inotify_fds": 0, "other_fds": 3},
        "dns": {"resolv.conf": "nameserver 8.8.8.8\n", "connect_8_8_8_8_53": "NetworkUnreachable"},
        "timing": {"open_read_close_ns": 1_000_000},
    }
    base.update(overrides)
    return base


def test_invisibility_diff_clean_on_noise_only() -> None:
    plain = _probe_dump()
    monitored = _probe_dump(
        ps=[["python3", "python3 /tmp/invisibility-probe.py"], ["sleep", "sleep 300"]],
        env=["PATH=/usr/bin", "HOME=/root", "HOSTNAME=abc123"],
        timing={"open_read_close_ns": 2_500_000},
    )
    report = diff_probes(plain, monitored)
    assert report["pass"] is True
    assert all(check["pass"] for check in report["checks"].values())


def test_invisibility_diff_flags_environment_change() -> None:
    plain = _probe_dump()
    monitored = _probe_dump(env=["HOME=/root", "PATH=/usr/bin", "EVALLAB_SNOOP=1"])
    report = diff_probes(plain, monitored)
    assert report["pass"] is False
    assert report["checks"]["env"]["pass"] is False
    assert report["checks"]["env"]["only_monitored"] == ['"EVALLAB_SNOOP=1"']
    assert report["checks"]["ps"]["pass"] is True


def test_invisibility_diff_flags_slowdown() -> None:
    plain = _probe_dump()
    monitored = _probe_dump(timing={"open_read_close_ns": 50_000_000})
    report = diff_probes(plain, monitored)
    assert report["checks"]["timing"]["pass"] is False


def test_canonicalize_drops_volatile_mount_prefixes() -> None:
    left = canonicalize("mountinfo", ["11 22 0:5 / /proc rw - proc proc rw"])
    right = canonicalize("mountinfo", ["99 88 0:5 / /proc rw - proc proc rw"])
    assert left == right


def _packet(payload: bytes, *, src: str = "172.19.0.3", dst: str = "1.1.1.1",
            sport: int = 12345, dport: int = 53, flags: int | None = None) -> bytes:
    if flags is None:
        transport = struct.pack("!4H", sport, dport, len(payload) + 8, 0)
        protocol = 17
    else:
        transport = struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 0x50, flags, 0, 0, 0)
        protocol = 6
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(transport) + len(payload),
                     0, 0, 64, protocol, 0, socket.inet_aton(src), socket.inet_aton(dst))
    return b"\0" * 12 + b"\x08\x00" + ip + transport + payload


def _dns_query() -> bytes:
    return struct.pack("!6H", 42, 0x100, 1, 0, 0, 0) + b"\x07example\x03com\0" + struct.pack("!HH", 1, 1)


def _hello() -> bytes:
    name = b"example.com"
    extension = struct.pack("!HHHBH", 0, len(name) + 5, len(name) + 3, 0, len(name)) + name
    body = b"\x03\x03" + b"\0" * 32 + b"\0" + b"\0\x02\x13\x01" + b"\x01\0"
    body += struct.pack("!H", len(extension)) + extension
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def test_gateway_metadata_only_and_decoy_container_excluded() -> None:
    observer = MetadataGateway({"172.19.0.3"})
    assert observer.observe(_packet(_dns_query(), src="172.19.0.99"), 1.0) == []
    assert observer.flows == {}
    assert observer.dropped_other == 1
    dns = observer.observe(_packet(_dns_query()), 2.0)
    assert dns[0]["questions"] == [{"name": "example.com", "type": 1}]
    observer.observe(_packet(b"", dport=443, flags=2), 2.1)
    observer.observe(_packet(b"", src="1.1.1.1", dst="172.19.0.3",
                             sport=443, dport=12345, flags=0x12), 2.2)
    observer.observe(_packet(_hello(), dport=443, flags=0x18), 2.3)
    rows = dns + observer.finish(3.0)
    tcp = next(row for row in rows if row.get("protocol") == "tcp")
    assert tcp["outcome"] == "established"
    assert tcp["tls_sni"] == "example.com"
    assert tcp["packets"] == 3 and tcp["bytes"] > 120
    assert all(row["enforcement"] == "none" and row["mode"] == "passive" for row in rows)
    exported = json.dumps(rows)
    assert "payload" not in exported and "body" not in exported and "hello" not in exported


def test_dns_parser_answers_and_client_hello() -> None:
    answer = bytearray(_dns_query())
    struct.pack_into("!H", answer, 2, 0x8180)
    struct.pack_into("!H", answer, 6, 1)
    answer.extend(b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("93.184.216.34"))
    metadata = dns_metadata(bytes(answer))
    assert metadata is not None
    assert metadata["answers"][0]["answer"] == "93.184.216.34"
    assert client_hello_sni(_hello()) == "example.com"
    assert client_hello_sni(b"not TLS") is None


def test_kernel_decoder_never_reads_decoy_process(monkeypatch: MonkeyPatch) -> None:
    from evallab.flight.runtime import recorder

    def forbidden_proc_read(_pid: int, _cgid: int) -> int | None:
        raise AssertionError("decoy /proc must never be read")

    monkeypatch.setattr(recorder, "_process_fd", forbidden_proc_read)
    decode_probe = recorder.decode_probe

    # A wrong cgroup is excluded before /proc args or fd enrichment.
    assert decode_probe(["exec", "1", "999", "123", "123", "0", "decoy", "1", "/bin/sh"],
                        42, str) is None
    event = decode_probe(["connect_result", "1", "42", "123", "123", "0", "agent", "-101"],
                         42, str)
    assert event is not None and event["errno"] == "ENETUNREACH"


def test_continuation_and_large_tool_bodies_retained(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    long = "a" * 12000
    _write_json(trial / "agent" / "trajectory.cont-1.json",
                {"steps": [{"timestamp": "2026-10-08T21:30:53Z", "content": long,
                            "tool_calls": [{"name": "write", "arguments": long}]}]})
    build_timeline(trial, job)
    rows = [json.loads(line) for line in (trial / "flight" / "timeline.jsonl").read_text().splitlines()]
    assert any(row["detail"].get("raw_step", {}).get("content") == long for row in rows)
    assert any(row["detail"].get("raw_call", {}).get("arguments") == long for row in rows)


def test_local_full_body_tap_chains_reasoning_to_trial(tmp_path: Path) -> None:
    import threading
    import urllib.request
    from datetime import UTC, datetime, timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from evallab.model_capture import serve_capture

    secret = "flight-unit-secret"

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"model": "local-fixture", "choices": [{"message": {
                "content": "done", "reasoning_content": "thinking " + secret}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    trial, job = _trial_with_planes(tmp_path)
    now = datetime.now(UTC)
    result = _result()
    result["agent_execution"] = {"started_at": (now - timedelta(seconds=10)).isoformat(),
                                 "finished_at": (now + timedelta(seconds=10)).isoformat()}
    _write_json(trial / "result.json", result)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    capture_dir = tmp_path / "loopback"
    tap, recorder, manifest = serve_capture(
        upstream=f"http://127.0.0.1:{upstream.server_address[1]}", out_dir=capture_dir, upstream_key=secret)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (upstream, tap)]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(
            manifest["endpoint"] + "/t/tok123/v1/chat/completions",
            data=json.dumps({"model": "local-fixture", "messages": [{"role": "user",
                            "content": "prompt " + secret}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        for server in (tap, upstream):
            server.shutdown()
            server.server_close()
        recorder.close()
    summary = build_timeline(trial, job, capture_dir=capture_dir)
    assert summary["model_calls"] == 1
    serialized = (trial / "flight" / "timeline.jsonl").read_text()
    assert secret not in serialized
    rows = [json.loads(line) for line in serialized.splitlines()]
    call = next(row["detail"] for row in rows if row["kind"] == "model_call")
    assert call["response_body"]["choices"][0]["message"]["reasoning_content"].startswith("thinking ")
    assert call["request_body"]["messages"][0]["content"].startswith("prompt ")


def test_observer_missing_phase_is_explicit_incomplete_coverage(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    assert build_timeline(trial, job)["coverage"] == "incomplete"
    for phase in ("agent", "verifier"):
        _write_json(trial / "flight" / f"status.{phase}.json", {"status": "available"})
    assert build_timeline(trial, job)["coverage"] == "prototype_available"
    _write_json(trial / "flight" / "status.verifier.json", {"status": "partial", "truncated": True})
    summary = build_timeline(trial, job)
    assert summary["coverage"] == "incomplete"
    assert summary["observer_status"]["status.verifier.json"]["truncated"] is True


def test_pcap_stream_preserves_packets_across_partial_pipe_reads() -> None:
    from evallab.flight.gateway import PcapStream

    frame = _packet(_dns_query())
    header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    packet = struct.pack("<IIII", 123, 500000, len(frame), len(frame)) + frame
    stream = PcapStream()
    assert stream.feed(header[:9]) == []
    assert stream.feed(header[9:] + packet[:11]) == []
    assert stream.feed(packet[11:]) == [(123.5, frame)]


def test_kernel_dns_answers_and_async_connect_are_metadata_only() -> None:
    from evallab.flight.runtime.recorder import decode_probe

    answer = bytearray(_dns_query())
    struct.pack_into("!H", answer, 2, 0x8180)
    struct.pack_into("!H", answer, 6, 1)
    answer.extend(b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("1.2.3.4"))
    escaped = "".join(f"\\x{byte:02x}" for byte in answer)
    dns = decode_probe(["dns_receive", "1", "42", "123", "123", "0", "agent",
                        "3", str(len(answer)), escaped], 42, str)
    assert dns is not None and dns["dns"]["answers"][0]["answer"] == "1.2.3.4"
    assert "payload" not in dns and "body" not in dns
    pending = decode_probe(["connect_result", "1", "42", "123", "123", "0", "agent", "-115"],
                           42, str)
    assert pending is not None and pending["outcome"] == "in_progress"


def test_cli_show_emits_full_timeline_bodies(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from evallab.cli import _flight_show_command

    trial, _job = _trial_with_planes(tmp_path)
    args = SimpleNamespace(path=trial, capture_dir=None, json=True)
    assert _flight_show_command(args, tmp_path) == 0
    shown = json.loads(capsys.readouterr().out)
    assert len(shown["events"]) == shown["summary"]["rows"]
    call = next(row for row in shown["events"] if row["kind"] == "model_call")
    assert call["detail"]["request_body"]["messages"][0]["content"] == "do it"


def test_invisibility_diff_rejects_changed_connect_reachability() -> None:
    plain = _probe_dump(timing={"open_read_close_ns": 1, "connect_outcome": "connected"})
    monitored = _probe_dump(timing={"open_read_close_ns": 1, "connect_outcome": "OSError"})
    assert diff_probes(plain, monitored)["checks"]["timing"]["pass"] is False


def test_full_body_timeline_is_private_on_disk(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    timeline = Path(build_timeline(trial, job)["timeline"])
    assert timeline.stat().st_mode & 0o777 == 0o600
    assert timeline.parent.stat().st_mode & 0o777 == 0o700


def test_external_snapshots_never_follow_target_symlinks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from evallab.flight.runtime import recorder

    sandbox = tmp_path / "sandbox"
    peer = tmp_path / "peer"
    (sandbox / "app").mkdir(parents=True)
    (sandbox / "app" / "local").write_text("own trial")
    (peer / "verifier").mkdir(parents=True)
    (peer / "secret").write_text("another trial")
    (peer / "verifier" / "secret").write_text("another verifier")
    (sandbox / "app" / "file-link").symlink_to(peer / "secret")
    (sandbox / "app" / "directory-link").symlink_to(peer, target_is_directory=True)
    (sandbox / "logs").symlink_to(peer, target_is_directory=True)
    monkeypatch.setattr(recorder, "LINUX_O_NOATIME", 0)
    root_fd = os.open(sandbox, os.O_RDONLY | os.O_DIRECTORY)
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert path not in ("secret", "verifier"), "observer escaped into another trial"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(recorder.os, "open", guarded_open)
    try:
        assert set(recorder.snapshot(root_fd, "/app")) == {"local"}
        assert recorder.snapshot(root_fd, "/logs/verifier") == {}
    finally:
        os.close(root_fd)


def test_pid_reuse_excludes_foreign_argv_and_fd_enrichment(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from evallab.flight.runtime import recorder

    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    opened: list[int] = []

    def reused_process(_path, _flags):
        fd = os.dup(root_fd)
        opened.append(fd)
        return fd

    def forbidden_link(*_args, **_kwargs):
        raise AssertionError("foreign fd contents must not be inspected")

    monkeypatch.setattr(recorder.os, "open", reused_process)
    monkeypatch.setattr(recorder, "_cgroup_at", lambda _fd: ("/docker/other-trial", 999))
    monkeypatch.setattr(recorder.os, "readlink", forbidden_link)
    try:
        assert recorder.proc_path(123, 3, 42, "relative") == "relative"
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(root_fd)

# --- Fail-open lifecycle hardening (fixture-only, no Docker) ---


def _journal_job(job_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        job_dir=job_dir,
        on_agent_started=lambda _cb: None,
        on_agent_ended=lambda _cb: None,
        on_trial_ended=lambda _cb: None,
        on_trial_cancelled=lambda _cb: None,
    )


def test_journal_unaffected_path_never_imports_flight(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import sys

    from evallab.harbor_state_journal import StateJournalPlugin

    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    for module in [m for m in sys.modules if m == "evallab.flight.plugin"]:
        del sys.modules[module]
    job_dir = tmp_path / "runs" / "job"
    job_dir.mkdir(parents=True)
    asyncio.run(StateJournalPlugin().on_job_start(_journal_job(job_dir)))
    assert "evallab.flight.plugin" not in sys.modules


def test_journal_flight_failure_is_swallowed_and_recorded(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    import evallab.flight.plugin as flight_plugin
    from evallab.harbor_state_journal import StateJournalPlugin

    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    job_dir = tmp_path / "runs" / "job"
    job_dir.mkdir(parents=True)
    _write_json(job_dir.parent / ".flight" / "job.json", {})

    async def boom(self: object, _job: object) -> None:
        raise RuntimeError("fixture flight failure")

    monkeypatch.setattr(flight_plugin.FlightRecorderPlugin, "on_job_start", boom)
    plugin = StateJournalPlugin()
    asyncio.run(plugin.on_job_start(_journal_job(job_dir)))
    recorded = json.loads((job_dir / "flight-unavailable.json").read_text(encoding="utf-8"))
    assert recorded["status"] == "unavailable"
    assert any("job_start" in error for error in plugin._flight_errors)


def test_journal_flight_cancellation_propagates(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import evallab.flight.plugin as flight_plugin
    from evallab.harbor_state_journal import StateJournalPlugin

    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    job_dir = tmp_path / "runs" / "job"
    job_dir.mkdir(parents=True)
    _write_json(job_dir.parent / ".flight" / "job.json", {})

    async def cancelled(self: object, _job: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(flight_plugin.FlightRecorderPlugin, "on_job_start", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(StateJournalPlugin().on_job_start(_journal_job(job_dir)))


def test_journal_flight_job_end_failure_is_swallowed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from evallab.harbor_state_journal import StateJournalPlugin

    monkeypatch.setenv("EVALLAB_STATE_JOURNAL", "off")
    job_dir = tmp_path / "runs" / "job"
    job_dir.mkdir(parents=True)
    plugin = StateJournalPlugin()
    plugin._flight_job_dir = job_dir

    async def boom(_result: object) -> None:
        raise RuntimeError("fixture job-end failure")

    plugin._flight = SimpleNamespace(on_job_end=boom)
    asyncio.run(plugin.on_job_end(None))
    recorded = json.loads((job_dir / "flight-unavailable.json").read_text(encoding="utf-8"))
    assert recorded["status"] == "unavailable"
    assert any("job_end" in error for error in plugin._flight_errors)


def _flight_event(trials_dir: Path, **overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "config": SimpleNamespace(trials_dir=str(trials_dir), job_id=JOB_ID),
        "trial_name": "job__attempt",
        "trial_id": TRIAL_ID,
        "task_name": "task",
        "result": SimpleNamespace(task_id="task-1"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_plugin_start_failure_writes_per_phase_status(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from evallab.flight import observer
    from evallab.flight.plugin import FlightRecorderPlugin

    trials_dir = tmp_path / "trials"
    job_dir = tmp_path / "job"
    plugin = FlightRecorderPlugin(image="fixture-image", job_dir=job_dir)

    async def no_container(session_id: str) -> str:
        raise RuntimeError(f"fixture resolve failure: {session_id}")

    monkeypatch.setattr(observer, "resolve_session_container", no_container)
    asyncio.run(plugin._start(_flight_event(trials_dir), "agent", "session-1"))
    status = json.loads(
        (trials_dir / "job__attempt" / "flight" / "status.agent.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "unavailable"
    assert status["phase"] == "agent"
    assert (TRIAL_ID, "agent") not in plugin.active
    recorded = json.loads((job_dir / "flight-unavailable.json").read_text(encoding="utf-8"))
    assert recorded["status"] == "unavailable"


def _verifier_trial(tmp_path: Path) -> tuple[SimpleNamespace, SimpleNamespace, dict, dict]:
    import contextlib

    async def start(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="agent-session")

    environment = SimpleNamespace(session_id="agent-session", start=start)
    entered = {"count": 0}
    exited = {"count": 0}
    verifier = SimpleNamespace(session_id="verifier-session")

    @contextlib.asynccontextmanager
    async def separate_verifier(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        entered["count"] += 1
        try:
            yield verifier
        finally:
            exited["count"] += 1

    trial = SimpleNamespace(
        id=TRIAL_ID,
        config=SimpleNamespace(trial_name="job__attempt", trials_dir=str(tmp_path), job_id=JOB_ID),
        task=SimpleNamespace(name="task"),
        result=SimpleNamespace(task_id="task-1"),
        agent_environment=environment,
        _separate_verifier_env=separate_verifier,
    )
    return trial, verifier, entered, exited


def _failing_resolve(monkeypatch: MonkeyPatch) -> None:
    from evallab.flight import observer

    async def no_container(session_id: str) -> str:
        raise RuntimeError(f"fixture resolve failure: {session_id}")

    monkeypatch.setattr(observer, "resolve_session_container", no_container)


def test_plugin_verifier_wrapper_survives_observer_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from evallab.flight.plugin import FlightRecorderPlugin

    _failing_resolve(monkeypatch)
    plugin = FlightRecorderPlugin(image="fixture-image", job_dir=tmp_path / "job")
    trial, verifier, entered, exited = _verifier_trial(tmp_path)
    plugin._instrument_trial(trial)

    async def main() -> str:
        async with trial._separate_verifier_env() as seen:
            assert seen is verifier
            return "body-ok"

    assert asyncio.run(main()) == "body-ok"
    assert entered["count"] == 1 and exited["count"] == 1
    status = json.loads(
        (tmp_path / "job__attempt" / "flight" / "status.verifier.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "unavailable"


def test_plugin_verifier_wrapper_never_suppresses_body_errors(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from evallab.flight.plugin import FlightRecorderPlugin

    _failing_resolve(monkeypatch)
    plugin = FlightRecorderPlugin(image="fixture-image", job_dir=tmp_path / "job")
    trial, _verifier, _entered, exited = _verifier_trial(tmp_path)
    plugin._instrument_trial(trial)

    async def main() -> None:
        async with trial._separate_verifier_env():
            raise ValueError("fixture body failure")

    with pytest.raises(ValueError, match="fixture body failure"):
        asyncio.run(main())
    assert exited["count"] == 1


def test_write_status_unavailable_never_raises(tmp_path: Path) -> None:
    from evallab.flight import observer

    assert observer.write_status_unavailable(tmp_path / "flight", "boom", None) is True
    payload = json.loads((tmp_path / "flight" / "status.json").read_text(encoding="utf-8"))
    assert payload["status"] == "unavailable"

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert observer.write_status_unavailable(
        blocker / "flight", "boom", "img", phase="agent") is False


def test_plugin_job_end_stops_every_observer_and_restores_hooks(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from evallab.flight import observer
    from evallab.flight.observer import Observer
    from evallab.flight.plugin import FlightRecorderPlugin

    stopped: list[str] = []

    async def stop(name: str) -> None:
        stopped.append(name)
        if name == "observer-one":
            raise RuntimeError("fixture stop failure")

    monkeypatch.setattr(observer, "stop_observer", stop)

    def original_hooks(_trial: object) -> None:
        return None

    queue = SimpleNamespace(_setup_hooks=original_hooks)
    plugin = FlightRecorderPlugin(
        job_dir=tmp_path / "job", queue=queue, original_setup_hooks=original_hooks)
    plugin.active[("trial-1", "agent")] = Observer(
        "observer-one", tmp_path, "container", 11, "agent")
    plugin.active[("trial-2", "agent")] = Observer(
        "observer-two", tmp_path, "container", 12, "agent")
    asyncio.run(plugin.on_job_end(None))
    assert sorted(stopped) == ["observer-one", "observer-two"]
    assert plugin.active == {}
    assert queue._setup_hooks is original_hooks
    assert any(error["phase"] == "stop" for error in plugin.errors)


def test_plugin_bad_config_is_unavailable_not_value_error(tmp_path: Path) -> None:
    from evallab.flight.plugin import FlightRecorderPlugin, configuration_path

    job_dir = tmp_path / "runs" / "job"
    job_dir.mkdir(parents=True)
    _write_json(configuration_path(job_dir), {"schema": "evallab.flight.config/v999"})
    plugin = FlightRecorderPlugin()
    asyncio.run(plugin.on_job_start(SimpleNamespace(job_dir=job_dir)))
    assert plugin.image is None
    recorded = json.loads((job_dir / "flight-unavailable.json").read_text(encoding="utf-8"))
    assert recorded["status"] == "unavailable"
    assert any(error["phase"] == "configuration" for error in plugin.errors)


def test_plugin_shared_verifier_without_session_is_unobserved(tmp_path: Path) -> None:
    from evallab.flight.plugin import FlightRecorderPlugin

    trials_dir = tmp_path / "trials"
    plugin = FlightRecorderPlugin(job_dir=tmp_path / "job")
    event = _flight_event(
        trials_dir,
        result=SimpleNamespace(task_id="task-1", verifier_environment_mode=None),
    )
    asyncio.run(plugin._on_verification_started(event))
    status = json.loads(
        (trials_dir / "job__attempt" / "flight" / "status.verifier.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "unavailable"
    assert status["code"] == "verifier_unobserved"


# --- Forged-evidence framing + snapshot robustness (flight-recorder hardening) ---


def test_probe_template_renders_nonce_and_cgid() -> None:
    from evallab.flight.runtime import recorder

    template = Path(recorder.__file__).with_name("probes.bt").read_text(encoding="utf-8")
    assert "@@CGID@@" in template and "@@NONCE@@" in template
    nonce = recorder.fresh_nonce()
    assert recorder.NONCE_RE.fullmatch(nonce)
    rendered = recorder.render_probe_template(template, 12345, nonce)
    assert "@@" not in rendered and nonce in rendered and "12345" in rendered
    assert all(nonce in line for line in rendered.splitlines() if "printf(" in line)
    with pytest.raises(ValueError):
        recorder.render_probe_template(template, 12345, "short")
    with pytest.raises(ValueError):
        recorder.render_probe_template(template.replace("@@CGID@@", "1") + "@@EXTRA@@", 1, nonce)


def test_forged_kernel_row_without_nonce_rejected_and_counted() -> None:
    from evallab.flight.runtime import recorder

    nonce = recorder.fresh_nonce()
    legit = f"{nonce}\tE\texit\t1\t42\t123\t123\t0\tagent\t0"
    disposition, row, _note = recorder.parse_trace_line(legit, nonce=nonce, cgid=42, wall=str)
    assert disposition == "event" and row is not None and row["kind"] == "exit"
    # Sandbox plants a filename containing a forged row; the byte stream splits
    # on the embedded newline. The continuation lacks the nonce and must die.
    forged = "E\texit\t1\t42\t1\t1\t0\troot\t99"
    disposition, row, _note = recorder.parse_trace_line(forged, nonce=nonce, cgid=42, wall=str)
    assert disposition == "tamper" and row is None


def test_adversarial_filename_splits_are_counted_never_forged() -> None:
    from collections import Counter

    from evallab.flight.runtime import recorder

    nonce = recorder.fresh_nonce()
    # Raw probe bytes for open("/tmp/x\nE\texit\t1\t42\t1\t1\t0\troot\t99")
    # and open("/tmp/a\tb"): tab breaks arity, newline breaks the line.
    raw_lines = [
        f"{nonce}\tE\tfile\t1\t42\t7\t7\t0\tsh\t3\t0\t/tmp/x",
        "E\texit\t1\t42\t1\t1\t0\troot\t99",
        f"{nonce}\tE\tfile\t2\t42\t7\t7\t0\tsh\t3\t0\t/tmp/a\tb",
        f"{nonce}\tREADY",
        f"{nonce}\tHEARTBEAT",
    ]
    framing: Counter[str] = Counter()
    rows = []
    for text in raw_lines:
        disposition, row, _note = recorder.parse_trace_line(text, nonce=nonce, cgid=42, wall=str)
        if disposition == "event":
            assert row is not None
            rows.append(row)
        elif disposition == "malformed":
            framing["malformed_events"] += 1
        elif disposition == "tamper":
            framing["tamper_suspect"] += 1
    # The forged exit (pid 1, code 99) never materializes; the tab-split line
    # is counted malformed rather than decoded with a reshaped path.
    assert not any(row.get("pid") == 1 for row in rows)
    assert framing["tamper_suspect"] == 1 and framing["malformed_events"] == 1
    assert len(rows) == 1 and rows[0]["path"] == "/tmp/x"


def test_wrong_arity_and_unknown_kind_rejected() -> None:
    from evallab.flight.runtime import recorder

    nonce = recorder.fresh_nonce()
    short_exec = f"{nonce}\tE\texec\t1\t42\t123\t123\t0\tagent\t7"
    disposition, row, note = recorder.parse_trace_line(short_exec, nonce=nonce, cgid=42, wall=str)
    assert disposition == "malformed" and row is None and note is not None
    evil = f"{nonce}\tE\tgrant_root\t1\t42\t123\t123\t0\tagent"
    assert recorder.parse_trace_line(evil, nonce=nonce, cgid=42, wall=str)[0] == "malformed"
    with pytest.raises(ValueError):
        recorder.decode_probe(["exec", "1", "42", "1", "1", "0", "a", "7"], 42, str)


def test_3000_deep_chain_is_partial_not_recursion_error(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import sys

    from evallab.flight.runtime import recorder

    monkeypatch.setattr(recorder, "LINUX_O_NOATIME", 0)
    tmp_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.mkdir("chain", dir_fd=tmp_fd)
        parent = os.open("chain", os.O_RDONLY | os.O_DIRECTORY, dir_fd=tmp_fd)
        try:
            for index in range(3000):
                name = f"d{index}"
                os.mkdir(name, dir_fd=parent)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            leaf = os.open("leaf", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=parent)
            os.close(leaf)
        finally:
            os.close(parent)
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(150)
        try:
            _entries, meta = recorder.snapshot_full(tmp_fd, "/chain")
        finally:
            sys.setrecursionlimit(old_limit)
    finally:
        os.close(tmp_fd)
    assert meta["status"] == "partial" and "depth" in str(meta["reason"])


def test_growing_file_hash_is_bounded_and_counted(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import hashlib

    from evallab.flight.runtime import recorder

    monkeypatch.setattr(recorder, "LINUX_O_NOATIME", 0)
    # A pipe reports st_size 0 yet yields unbounded bytes: the hash must stop
    # after max_bytes+1 and report truncation instead of reading forever.
    reader, writer = os.pipe()
    try:
        os.write(writer, b"y" * 5000)
        os.close(writer)
        digest, truncated = recorder.hash_file_bounded(reader, 16)
    finally:
        os.close(reader)
    assert digest is None and truncated is True
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "small").write_bytes(b"abc")
    (snap / "big").write_bytes(b"z" * 100)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entries, meta = recorder.snapshot_full(root_fd, "/snap", max_bytes=16)
    finally:
        os.close(root_fd)
    assert entries["small"][2] == hashlib.sha256(b"abc").hexdigest()
    assert entries["big"][2] is None and meta["hash_truncated"] == 1


def test_cmdline_over_64k_truncated_with_flag(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from evallab.flight.runtime import recorder

    proc = tmp_path / "proc123"
    proc.mkdir()
    (proc / "cmdline").write_bytes(b"a" * 70000 + b"\0--flag\0")
    real_open = os.open

    def fake_process_fd(pid: int, cgid: int) -> int | None:
        assert (pid, cgid) == (123, 42)
        return real_open(proc, os.O_RDONLY | os.O_DIRECTORY)

    monkeypatch.setattr(recorder, "_process_fd", fake_process_fd)
    fields = ["exec", "1", "42", "123", "123", "0", "agent", "7", "/bin/x"]
    row = recorder.decode_probe(fields, 42, str)
    assert row is not None and row["args_truncated"] is True
    assert sum(len(arg) for arg in row["args"]) <= recorder.CMDLINE_LIMIT
    (proc / "cmdline").write_bytes(b"python\0-x\0")
    row = recorder.decode_probe(fields, 42, str)
    assert row is not None and row["args"] == ["python", "-x"] and row["args_truncated"] is False


def test_error_buffer_caps_at_30_with_dropped_counter() -> None:
    from collections import Counter

    from evallab.flight.runtime import recorder

    errors: list[str] = []
    tally: Counter[str] = Counter()
    for index in range(45):
        recorder._record_error(errors, tally, f"boom-{index}")
    assert len(errors) == recorder.MAX_ERRORS == 30
    assert tally["errors_dropped"] == 15
    assert errors[0] == "boom-15" and errors[-1] == "boom-44"


def test_gateway_caps_oversized_tls_hello() -> None:
    from evallab.flight.gateway import MAX_FLOWS, TLS_HELLO_CAP

    assert TLS_HELLO_CAP <= 4096
    assert MAX_FLOWS <= 2048
    observer = MetadataGateway({"172.19.0.3"})
    assert observer.max_flows <= 2048
    huge = b"\x16\x03\x01" + b"A" * 49_997
    for _ in range(3):
        observer.observe(_packet(huge, dport=443, flags=0x18), 1.0)
    flow = next(iter(observer.flows.values()))
    assert len(flow.hello) <= TLS_HELLO_CAP
    assert flow.sni is None


def test_gateway_evicts_oldest_flow_past_cap() -> None:
    observer = MetadataGateway({"172.19.0.3"}, max_flows=4)
    for sport in range(1000, 1006):
        observer.observe(_packet(b"", dport=443, flags=2, sport=sport), 1.0)
    assert len(observer.flows) <= 4
    assert observer.dropped_capacity >= 1


def test_gateway_drops_hello_buffer_on_non_tls_payload() -> None:
    observer = MetadataGateway({"172.19.0.3"})
    observer.observe(_packet(b"GET / HTTP/1.0\r\n\r\n", dport=443, flags=0x18), 1.0)
    flow = next(iter(observer.flows.values()))
    assert bytes(flow.hello) == b""
    assert flow.next_sequence is None
    observer.observe(_packet(_hello(), dport=443, flags=0x18), 2.0)
    finished = observer.finish(3.0)
    assert finished[0]["tls_sni"] == "example.com"


def test_timeline_drops_bad_seq_without_aborting(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    with (tmp_path / "cap" / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"seq": "not-a-number"}) + "\n")
        handle.write(json.dumps({"seq": {"nested": 1}}) + "\n")
    summary = build_timeline(trial, job)
    assert summary["dropped_bad_seq"] == 2
    assert summary["model_calls"] == 1


def test_status_available_without_events_is_missing_not_available(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "job__attempt"
    _write_json(job / "result.json", {"id": JOB_ID})
    _write_json(trial / "result.json", _result(agent="oracle"))
    for phase in ("agent", "verifier"):
        _write_json(trial / "flight" / f"status.{phase}.json", {"status": "available"})
    summary = build_timeline(trial, job)
    assert summary["coverage"] == "incomplete"
    assert summary["presence"]["kernel"] == "missing"
    assert summary["coverage_reasons"]
    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    presence = [row for row in rows if row["plane"] == "kernel" and row["kind"] == "presence"]
    assert len(presence) == 1
    assert presence[0]["detail"]["status"] == "missing"
    assert "flight/events.jsonl" in presence[0]["detail"]["missing"]


def test_timeline_counts_corrupt_jsonl_lines(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    with (trial / "flight" / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write("{truncated\n")
        handle.write("[1, 2]\n")
    summary = build_timeline(trial, job)
    assert summary["corrupt_lines"].get("events.jsonl") == 3
    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["plane"] == "kernel" and row["kind"] == "exec" for row in rows)


def test_timeline_caps_embedded_paths_and_text(tmp_path: Path) -> None:
    from evallab.flight.timeline import MAX_PATHS_PER_LIST, MAX_TEXT_CHARS

    trial, job = _trial_with_planes(tmp_path)
    big = [f"output/file-{index:05d}.log" for index in range(1500)]
    _write_json(
        trial / "flight" / "filediff.big.json",
        {
            "schema_version": 1,
            "/app": {
                "added": big,
                "removed": [],
                "modified": [],
                "added_count": 1500,
                "removed_count": 0,
                "modified_count": 0,
                "truncated": False,
            },
        },
    )
    (trial / "verifier" / "test-stdout.txt").write_text("x" * 100_000, encoding="utf-8")
    build_timeline(trial, job)
    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    added = [
        row for row in rows
        if row["plane"] == "file" and row["kind"] == "file_added"
        and row["detail"]["source"] == "filediff.big.json"
    ]
    assert added and len(added[0]["detail"]["paths"]) <= MAX_PATHS_PER_LIST
    assert added[0]["detail"]["paths_truncated"] is True
    assert added[0]["detail"]["count"] == 1500
    stdout = [
        row for row in rows
        if row["plane"] == "verifier" and row["detail"].get("file") == "test-stdout.txt"
    ]
    assert stdout and len(stdout[0]["detail"]["text"]) <= MAX_TEXT_CHARS
    assert stdout[0]["detail"]["text_truncated"] is True
    assert stdout[0]["detail"]["text_chars"] == 100_000


def test_record_leaves_preexisting_config_alone(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    from evallab.cli import _flight_record_command
    from evallab.flight.plugin import configuration_path

    args = SimpleNamespace(
        environment="docker", flight_egress="locked", egress_lock=None,
        task=Path("library/tasks/event-summary"), agent="oracle", model=None,
        name="flight-x", jobs_dir=Path("runs"), concurrency=1, attempts=1,
        timeout_seconds=1800, allow_billable=False, capture_dir=None, json=True,
    )
    sentinel = {"schema": "evallab.flight.config/v1", "egress": "sentinel"}
    target = configuration_path(tmp_path / "runs" / "flight-x")
    _write_json(target, sentinel)
    assert _flight_record_command(args, tmp_path) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == sentinel
    assert "already exists" in capsys.readouterr().err


def test_flight_capture_dir_resolves_like_record(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    from evallab.cli import _flight_show_command, _resolve_flight_capture_dir

    assert _resolve_flight_capture_dir(tmp_path, SimpleNamespace(capture_dir=None)) is None
    assert _resolve_flight_capture_dir(
        tmp_path, SimpleNamespace(capture_dir=Path("cap"))
    ) == (tmp_path / "cap").resolve()
    trial, _job = _trial_with_planes(tmp_path)
    args = SimpleNamespace(path=trial, capture_dir=Path("cap"), json=True)
    assert _flight_show_command(args, tmp_path) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["summary"]["model_calls"] == 1


def test_observer_command_tiers_nonce_and_caps() -> None:
    from evallab.flight.observer import observer_command

    locked = observer_command(
        image="img", observer_name="o", target_pid=1, target_container_id="c",
        output_dir=Path("/out"), phase="agent", nonce="ab" * 16)
    assert "--privileged" not in locked
    assert "--network=none" in locked
    assert not any("NET_RAW" in part for part in locked)
    # A compromised observer must start from no capabilities, never gain new ones, and stay
    # bounded: it shares the host with other trials' containers.
    assert locked.index("--cap-drop=ALL") < min(
        i for i, part in enumerate(locked) if part.startswith("--cap-add="))
    assert "--security-opt=no-new-privileges=true" in locked
    assert any(part.startswith("--memory=") for part in locked)
    assert any(part.startswith("--pids-limit=") for part in locked)
    assert not any(cap in part for part in locked for cap in ("SETUID", "SETGID", "CHOWN"))
    assert any(part.startswith("FLIGHT_NONCE=") and len(part) == 13 + 32 for part in locked)
    assert "BPFTRACE_STRLEN=160" in locked
    assert "BPFTRACE_MAP_KEYS_MAX=1024" in locked

    open_cmd = observer_command(
        image="img", observer_name="o", target_pid=1, target_container_id="c",
        output_dir=Path("/out"), phase="agent", sniff="br-1,10.0.0.2", nonce="cd" * 16)
    assert "--privileged" not in open_cmd
    assert "--cap-drop=ALL" in open_cmd
    assert "--network=host" in open_cmd
    assert any("NET_RAW" in part for part in open_cmd)
    # tcpdump's own privilege drop needs these; they must exist only where tcpdump runs.
    assert all(f"--cap-add={cap}" in open_cmd for cap in ("SETUID", "SETGID", "CHOWN"))


def test_decoder_flags_clipped_paths_and_payload_windows() -> None:
    from evallab.flight.runtime import recorder

    wall = lambda ns: "2026-10-08T00:00:00+00:00"  # noqa: E731
    long_path = "/x/" + "y" * 300
    file_row = recorder.decode_probe(
        ["file", "1", "99", "7", "8", "0", "cat", "-1", "0", long_path], 99, wall)
    assert file_row is not None and file_row["path_truncated"] is True
    short_row = recorder.decode_probe(
        ["file", "1", "99", "7", "8", "0", "cat", "-1", "0", "/etc/hosts"], 99, wall)
    assert short_row is not None and short_row["path_truncated"] is False
    big_dns = recorder.decode_probe(
        ["udp_send", "1", "99", "7", "8", "0", "py", "3", "2", "13568", "1", "512", "AA=="],
        99, wall)
    assert big_dns is not None and big_dns["payload_truncated"] is True
    small_dns = recorder.decode_probe(
        ["udp_send", "1", "99", "7", "8", "0", "py", "3", "2", "13568", "1", "29", "AA=="],
        99, wall)
    assert small_dns is not None and small_dns["payload_truncated"] is False


def test_descendant_pids_reports_child_cgroup_members(tmp_path: Path) -> None:
    from evallab.flight.runtime import recorder

    fake = tmp_path / "cgroup"
    (fake / "docker" / "abc").mkdir(parents=True)
    (fake / "docker" / "abc" / "cgroup.procs").write_text("11\n")
    (fake / "docker" / "abc" / "child").mkdir()
    (fake / "docker" / "abc" / "child" / "cgroup.procs").write_text("42\nnotapid\n")
    assert recorder.descendant_pids("/docker/abc", base=str(fake)) == {42}
    assert recorder.descendant_pids("/docker/missing", base=str(fake)) == set()


def test_bpftrace_startup_chatter_is_ignored_uncounted() -> None:
    from evallab.flight.runtime import recorder

    wall = lambda ns: "2026-10-08T00:00:00+00:00"  # noqa: E731
    disposition, row, _note = recorder.parse_trace_line(
        "Attaching 17 probes...", nonce="ab" * 16, cgid=99, wall=wall)
    assert (disposition, row) == ("info", None)


def test_model_reasoning_rows_join_request_response_order(tmp_path: Path) -> None:
    trial, job = _trial_with_planes(tmp_path)
    cap_calls = tmp_path / "cap" / "calls.jsonl"
    call = json.loads(cap_calls.read_text(encoding="utf-8").splitlines()[0])
    call["response_body"] = {
        "id": "chatcmpl-stub",
        "model": "fixture-model",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "checking git history first",
                "reasoning_content": "The tests may hide answers; git log could reveal them.",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "bash", "arguments": "{\"cmd\": \"git log\"}"},
                }],
            },
        }],
    }
    cap_calls.write_text(json.dumps(call) + "\n", encoding="utf-8")

    summary = build_timeline(trial, job)

    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    calls = [row for row in rows if row["kind"] == "model_call"]
    reasoning = [row for row in rows if row["kind"] == "model_reasoning"]
    assert len(calls) == 1 and len(reasoning) == 1
    assert reasoning[0]["plane"] == "model"
    assert reasoning[0]["detail"]["text"] == "The tests may hide answers; git log could reveal them."
    assert reasoning[0]["detail"]["source"] == "chat.reasoning_content"
    assert reasoning[0]["trial_id"] == TRIAL_ID and reasoning[0]["job_id"] == JOB_ID
    order = [rows.index(calls[0]), rows.index(reasoning[0])]
    assert order == sorted(order), "reasoning must follow its model call"
    assert summary["model_calls"] == 1


def test_stub_scripted_cheat_joins_model_reasoning_trajectory(tmp_path: Path) -> None:
    import sys

    from evallab.flight.timeline import build_timeline

    stub_dir = Path(__file__).resolve().parent.parent / "research" / "experiments" / "flight-scripted-cheater"
    sys.path.insert(0, str(stub_dir))
    try:
        from stub_server import build_chat_response
    finally:
        sys.path.remove(str(stub_dir))

    trial, job = _trial_with_planes(tmp_path)
    history: list[dict[str, object]] = [{"role": "user", "content": "do the task"}]
    calls = []
    for turn in range(3):
        response = build_chat_response("fixture-model", history)  # same stub as the demo
        message = response["choices"][0]["message"]
        started = f"2026-10-08T21:30:{50 + turn:02d}.000000Z"
        calls.append({
            "schema": "evallab.model_call/v1", "seq": turn + 1,
            "started_at": started, "ended_at": started,
            "method": "POST", "path": "/t/tok123/v1/chat/completions",
            "route_token": "tok123",
            "request_body": {"model": "fixture-model", "messages": list(history)},
            "response_body": response, "assistant_texts": [],
            "tool_calls": [], "usage": response["usage"], "model": "fixture-model",
            "error": None,
        })
        history.append({"role": "assistant", "content": message["content"]})
        history.append({"role": "user", "content": f"terminal output {turn}"})
    (tmp_path / "cap" / "calls.jsonl").write_text(
        "\n".join(json.dumps(call) for call in calls) + "\n", encoding="utf-8")
    _write_json(trial / "agent" / "trajectory.json", {"steps": [
        {"timestamp": "2026-10-08T21:30:50.500000Z", "role": "assistant",
         "content": history[1]["content"],
         "tool_calls": [{"name": "bash_command",
                         "arguments": json.dumps({"keystrokes": "git log --all --oneline"})}]},
    ]})

    build_timeline(trial, job)

    rows = [
        json.loads(line)
        for line in (trial / "flight" / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [(row["plane"], row["kind"]) for row in rows
             if row["plane"] in ("model", "trajectory", "tool")]
    call_at = kinds.index(("model", "model_call"))
    reasoning_at = kinds.index(("model", "model_reasoning"))
    step_at = kinds.index(("trajectory", "step"))
    assert call_at < reasoning_at < step_at, kinds
    reasoning = [row for row in rows if row["kind"] == "model_reasoning"]
    assert len(reasoning) == 3
    assert "git history" in reasoning[0]["detail"]["text"]
    assert all(row["trial_id"] == TRIAL_ID and row["job_id"] == JOB_ID for row in reasoning)
