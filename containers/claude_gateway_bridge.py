"""Host-loopback bridge from a task sandbox to OMP's Claude subscription.

Claude Code runs inside the sandbox and speaks the Anthropic Messages API to
``ANTHROPIC_BASE_URL``. This bridge binds 127.0.0.1 on the controller host
(the sandbox reaches it as ``host.docker.internal``) and forwards each
``POST /v1/messages`` call to one ``omp auth-gateway stdio`` child, which
answers from OMP's local Claude login. The sandbox sees only a per-trial
capability bearer; no gateway secret exists anywhere in this design (stdio
mode needs none), and the capability never reaches logs, errors, or the
usage file.

Fail-closed rules, mirroring ``containers/zai_openapi_secret_proxy.py``:
- only the exact ``POST /v1/messages`` target and its ``?beta=true`` SDK
  variant are admitted; all other methods, paths, and query strings fail
  before authentication, body reads, or upstream calls;
- bearer checked before the body is read (401 + close otherwise);
- requested model must be in ``--allowed-model`` exactly (400 otherwise);
- the served model must match the requested one (prefix-tolerant, see
  ``_models_match``) or the call fails 502 and is recorded;
- the stdio child must print its ready line before this bridge binds;
- a dead child fails later calls 502 rather than queuing them;
- 429/5xx from the gateway are forwarded verbatim, never retried: a 429 is
  admission feedback the queue must back off on, not a capacity signal.

Only shape-only facts are logged (method, path, status, model names, token
counts). Request/response bodies, headers, and the capability never are.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import itertools
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
UPSTREAM_TIMEOUT_SECONDS = 600.0
READY_TIMEOUT_SECONDS = 120.0
STDIO_LINE_LIMIT = MAX_RESPONSE_BYTES + 1024
MAX_CONCURRENT_UPSTREAM = 4
MAX_CONCURRENT_CONNECTIONS = 16
SHUTDOWN_DRAIN_SECONDS = 120.0

MESSAGES_PATH = "/v1/messages"
# Claude Code's beta SDK uses this one query variant. OMP's stdio gateway
# supports messages, but not count_tokens; do not broaden this to a prefix
# match or forward arbitrary request targets to the privileged gateway.
MESSAGES_TARGETS = frozenset({MESSAGES_PATH, MESSAGES_PATH + "?beta=true"})

CAPABILITY_ENV = "EVALLAB_CLAUDE_GATEWAY_CAPABILITY"
ATTEMPT_ENV = "EVALLAB_CLAUDE_GATEWAY_ATTEMPT_ID"
USAGE_FILE_ENV = "EVALLAB_CLAUDE_GATEWAY_USAGE_FILE"

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})

_SHUTDOWN_REQUESTED = threading.Event()


def _log(fields: dict[str, Any]) -> None:
    """One shape-only JSON line on stderr; never bodies, headers, or secrets."""
    try:
        sys.stderr.write(json.dumps(fields, sort_keys=True) + "\n")
        sys.stderr.flush()
    except OSError:
        pass


class GatewayError(RuntimeError):
    """The stdio child is unusable; every pending and future call fails."""


class StdioGateway:
    """One ``omp auth-gateway stdio`` child, shared by id across threads."""

    def __init__(
        self,
        command: list[str],
        *,
        max_concurrency: int,
        upstream_timeout: float,
    ) -> None:
        self._command = command
        self._upstream_timeout = upstream_timeout
        self._slots = threading.Semaphore(max_concurrency)
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._dead: GatewayError | None = None
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise GatewayError(f"cannot start stdio gateway {command[0]!r}: {exc}") from exc
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        ready = self._read_line(self._process.stdout, READY_TIMEOUT_SECONDS)
        try:
            payload = json.loads(ready)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._process.kill()
            raise GatewayError(f"stdio gateway first line is not JSON: {ready[:120]!r}") from exc
        if not isinstance(payload, dict) or not payload.get("ready"):
            self._process.kill()
            raise GatewayError(f"stdio gateway did not become ready: {ready[:200]!r}")
        self._reader = threading.Thread(target=self._read_loop, name="stdio-reader", daemon=True)
        self._reader.start()
        assert self._process.stderr is not None
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name="stdio-stderr", daemon=True
        )
        self._stderr_reader.start()

    @staticmethod
    def _read_line(pipe: Any, timeout: float) -> str:
        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("stdio gateway read timed out")
            ready, _, _ = select.select([pipe], [], [], remaining)
            if not ready:
                raise GatewayError("stdio gateway read timed out")
            chunk = os.read(pipe.fileno(), 65536)
            if not chunk:
                raise GatewayError("stdio gateway closed the pipe")
            chunks.append(chunk)
            total += len(chunk)
            if total > STDIO_LINE_LIMIT:
                raise GatewayError("stdio gateway line exceeded the response bound")
            if chunk.endswith(b"\n"):
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr_tail.append(line.decode(errors="replace").strip()[-300:])
        except OSError:
            pass

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                try:
                    line = self._read_line(self._process.stdout, UPSTREAM_TIMEOUT_SECONDS + 60.0)
                except GatewayError as exc:
                    self._fail_all(exc)
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = str(message.get("id"))
                with self._pending_lock:
                    slot = self._pending.pop(request_id, None)
                if slot is not None:
                    event, cell = slot
                    cell["status"] = message.get("status")
                    cell["body"] = message.get("body")
                    event.set()
        finally:
            self._fail_all(GatewayError("stdio gateway reader stopped"))

    def _fail_all(self, error: GatewayError) -> None:
        self._dead = error
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for event, cell in slots:
            cell["error"] = error
            event.set()

    def request(self, path: str, body: dict[str, Any]) -> tuple[Any, Any]:
        """Send one request; return (status, body). Raises GatewayError when dead."""
        if self._dead is not None:
            raise self._dead
        if not self._slots.acquire(blocking=False):
            raise GatewayError("gateway concurrency exhausted")
        try:
            request_id = str(next(self._ids))
            event = threading.Event()
            cell: dict[str, Any] = {}
            with self._pending_lock:
                if self._dead is not None:
                    raise self._dead
                self._pending[request_id] = (event, cell)
            line = (json.dumps({"id": request_id, "path": path, "body": body}) + "\n").encode()
            if len(line) > MAX_REQUEST_BYTES:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise GatewayError("request exceeds the request bound")
            try:
                with self._write_lock:
                    assert self._process.stdin is not None
                    self._process.stdin.write(line)
                    self._process.stdin.flush()
            except OSError as exc:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                self._fail_all(GatewayError(f"stdio gateway write failed: {exc}"))
                raise self._dead from exc
            if not event.wait(timeout=self._upstream_timeout):
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise GatewayError("upstream call timed out")
            if "error" in cell:
                raise cell["error"]
            return cell.get("status"), cell.get("body")
        finally:
            self._slots.release()

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self._process.terminate()


def _served_model(requested: str, status: Any, body: Any) -> str | None:
    """The model that answered, or None when the response carries none."""
    if isinstance(body, dict):
        model = body.get("model")
        return model if isinstance(model, str) and model else None
    if isinstance(body, str):
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: ") :])
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "message_start":
                continue
            message = event.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model:
                    return model
    return None


def _models_match(requested: str, served: str) -> bool:
    """Exact match, or the same bare name served unqualified or as anthropic/.

    Any other namespace fails closed: a silent substitution must never pass
    as the requested model.
    """
    if served == requested:
        return True

    def bare(name: str) -> str:
        return name.split("/", 1)[-1] if "/" in name else name

    if bare(served) != bare(requested) or not bare(served):
        return False
    return served.startswith("anthropic/") or "/" not in served


def _sse_usage(body: str) -> tuple[int, int, int, int]:
    """(input, cache_read, cache_creation, output) merged across SSE events."""
    total_in = total_read = total_create = total_out = 0
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[len("data: ") :])
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage: Any = None
        if event.get("type") == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        elif event.get("type") == "message_delta":
            usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        if isinstance(usage.get("input_tokens"), int):
            total_in = max(total_in, usage["input_tokens"])
        if isinstance(usage.get("cache_read_input_tokens"), int):
            total_read = max(total_read, usage["cache_read_input_tokens"])
        if isinstance(usage.get("cache_creation_input_tokens"), int):
            total_create = max(total_create, usage["cache_creation_input_tokens"])
        if isinstance(usage.get("output_tokens"), int):
            # message_start carries the running count, message_delta the final
            # total: the last value wins, never the sum.
            total_out = max(total_out, usage["output_tokens"])
    return total_in, total_read, total_create, total_out


def _response_usage(status: Any, body: Any) -> tuple[int, int, int, int]:
    def as_int(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    if isinstance(body, dict):
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return (
            as_int(usage.get("input_tokens")),
            as_int(usage.get("cache_read_input_tokens")),
            as_int(usage.get("cache_creation_input_tokens")),
            as_int(usage.get("output_tokens")),
        )
    if isinstance(body, str):
        return _sse_usage(body)
    return 0, 0, 0, 0


class Handler(BaseHTTPRequestHandler):
    """One trial capability, one model allowlist, one stdio child upstream."""

    server_version = "ClaudeGatewayBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # silence BaseHTTPRequestHandler
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        # Rejected requests may leave unread bodies. Never parse those bytes
        # as another request on this capability-bearing connection.
        self.close_connection = True
        body = bytes(payload)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (OSError, ValueError):
            pass

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, (json.dumps(value) + "\n").encode(), "application/json")

    def _authorized(self) -> bool:
        server = self.server
        assert isinstance(server, BridgeServer)
        presented = self.headers.get("Authorization", "")
        if presented.startswith("Bearer "):
            candidate = presented[len("Bearer ") :].strip()
        else:
            candidate = (self.headers.get("x-api-key") or "").strip()
        return bool(candidate) and hmac.compare_digest(candidate, server.capability)

    def _admitted_request_target(self) -> bool:
        # BaseHTTPRequestHandler normalizes a leading // in self.path.
        # Check the original request-line target too: normalization, URL
        # decoding, absolute URLs, and extra query parameters are not grants.
        parts = self.requestline.split()
        return (
            self.command == "POST"
            and len(parts) == 3
            and parts[1] == self.path
            and self.path in MESSAGES_TARGETS
        )

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if not self._admitted_request_target():
            self._json(404, {"error": {"type": "not_found", "message": "unknown route"}})
            return False
        return True

    def handle_expect_100(self) -> bool:
        # The standard parser sends 100 Continue before parse_request returns.
        # Do not invite a body for an unadmitted route or a missing capability.
        if not self._admitted_request_target():
            self._json(404, {"error": {"type": "not_found", "message": "unknown route"}})
            return False
        if not self._authorized():
            self._json(
                401, {"error": {"type": "authentication_error", "message": "bad capability"}}
            )
            return False
        return super().handle_expect_100()

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, BridgeServer)
        if not self._admitted_request_target():
            self._json(404, {"error": {"type": "not_found", "message": "unknown route"}})
            return
        if not self._authorized():
            self._json(
                401, {"error": {"type": "authentication_error", "message": "bad capability"}}
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"error": {"type": "invalid_request", "message": "bad length"}})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._json(
                400, {"error": {"type": "invalid_request", "message": "bounded body required"}}
            )
            return
        try:
            raw = self.rfile.read(length)
        except OSError:
            return
        try:
            request_body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": {"type": "invalid_request", "message": "JSON body required"}})
            return
        if not isinstance(request_body, dict):
            self._json(
                400, {"error": {"type": "invalid_request", "message": "object body required"}}
            )
            return
        model = request_body.get("model")
        if not isinstance(model, str) or model not in server.allowed_models:
            self._json(400, {"error": {"type": "invalid_request", "message": "model not admitted"}})
            return
        stream = request_body.get("stream") is True
        try:
            status, gateway_body = server.gateway.request(MESSAGES_PATH, request_body)
        except GatewayError as exc:
            server.record(model, None, "gateway_error", False, (0, 0, 0, 0), error=str(exc)[:120])
            self._json(502, {"error": {"type": "gateway_error", "message": "upstream unavailable"}})
            return
        if not isinstance(status, int) or status < 100 or status > 599:
            server.record(model, None, "bad_status", False, (0, 0, 0, 0))
            self._json(502, {"error": {"type": "gateway_error", "message": "bad upstream status"}})
            return
        served = _served_model(model, status, gateway_body)
        usage = _response_usage(status, gateway_body)
        if status < 400 and (served is None or not _models_match(model, served)):
            server.record(model, served, "model_mismatch", stream, usage, status=status)
            self._json(
                502,
                {"error": {"type": "gateway_error", "message": "served model did not match"}},
            )
            return
        server.record(model, served, "ok", stream, usage, status=status)
        if status >= 400:
            # Error bodies (including 429 rate limits) pass through verbatim
            # so the client backs off on the provider's own signal.
            if isinstance(gateway_body, dict):
                self._json(status, gateway_body)
            elif isinstance(gateway_body, str):
                self._send(status, gateway_body.encode(), "text/event-stream")
            else:
                self._json(
                    status, {"error": {"type": "gateway_error", "message": "upstream error"}}
                )
            return
        if isinstance(gateway_body, dict):
            self._json(status, gateway_body)
            return
        if isinstance(gateway_body, str):
            self._send_stream(status, gateway_body)
            return
        self._json(502, {"error": {"type": "gateway_error", "message": "bad upstream body"}})

    def _send_stream(self, status: int, body: str) -> None:
        self.close_connection = True
        encoded = body.encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            self._json(502, {"error": {"type": "gateway_error", "message": "response too large"}})
            return
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # Re-chunk on event boundaries so the client parses incrementally.
            start = 0
            while start < len(encoded):
                end = encoded.find(b"\n\n", start)
                chunk = encoded[start:] if end == -1 else encoded[start : end + 2]
                self.wfile.write(chunk)
                self.wfile.flush()
                if end == -1:
                    break
                start = end + 2
        except (OSError, ValueError):
            pass


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        capability: str,
        attempt_id: str,
        usage_path: Path,
        allowed_models: frozenset[str],
        gateway: StdioGateway,
    ) -> None:
        self.capability = capability
        self.attempt_id = attempt_id
        self.usage_path = usage_path
        self.allowed_models = allowed_models
        self.gateway = gateway
        self._connection_slots = threading.Semaphore(MAX_CONCURRENT_CONNECTIONS)
        self._usage_lock = threading.Lock()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        super().__init__(address, Handler)

    @property
    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def get_request(self) -> Any:
        if not self._connection_slots.acquire(blocking=False):
            raise OSError("connection bound exceeded")
        try:
            request = super().get_request()
        except BaseException:
            self._connection_slots.release()
            raise
        with self._in_flight_lock:
            self._in_flight += 1
        return request

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            self._connection_slots.release()
            with self._in_flight_lock:
                self._in_flight = max(0, self._in_flight - 1)

    def record(
        self,
        model_requested: str,
        model_served: str | None,
        outcome: str,
        stream: bool,
        usage: tuple[int, int, int, int],
        *,
        status: int | None = None,
        error: str | None = None,
    ) -> None:
        total_in, cache_read, cache_create, total_out = usage
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "attempt_id": self.attempt_id,
            "model_requested": model_requested,
            "model_served": model_served,
            "outcome": outcome,
            "status": status,
            "stream": stream,
            "input_tokens": total_in,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
            "output_tokens": total_out,
            "rate_limited": status == 429,
        }
        if error is not None:
            record["error"] = error
        line = (json.dumps(record, sort_keys=True) + "\n").encode()
        _log(
            {
                "bridge": "request",
                "attempt": self.attempt_id,
                "model": model_requested,
                "served": model_served,
                "outcome": outcome,
                "status": status,
                "stream": stream,
                "in": total_in,
                "out": total_out,
            }
        )
        with self._usage_lock:
            try:
                with open(self.usage_path, "ab") as handle:
                    handle.write(line)
            except OSError as exc:
                _log({"bridge": "usage_write_failed", "error": str(exc)[:120]})


def _write_ready_file(path: Path, host: str, port: int) -> None:
    payload = (json.dumps({"host": host, "port": port}, sort_keys=True) + "\n").encode("ascii")
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with open(descriptor, "wb", closefd=True) as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    os.replace(temporary, target)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claude subscription gateway bridge (host loopback)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", default=None)
    parser.add_argument("--attempt-id", default=os.environ.get(ATTEMPT_ENV, ""))
    parser.add_argument("--usage-file", default=os.environ.get(USAGE_FILE_ENV, ""))
    parser.add_argument("--allowed-model", dest="allowed_models", action="append", default=[])
    parser.add_argument("--max-concurrency", type=int, default=MAX_CONCURRENT_UPSTREAM)
    parser.add_argument(
        "--stdio-command",
        # Tests point this at a stub via argv or env; the runner never
        # overrides it, so production always spawns the real gateway. (Env
        # and PATH already decide what `omp` resolves to; this changes
        # nothing about that trust.)
        default=os.environ.get(
            "EVALLAB_CLAUDE_GATEWAY_STDIO_COMMAND", "omp auth-gateway stdio"
        ),
        help="Gateway child argv (tests point this at a stub; the runner never overrides it)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.host not in ALLOWED_HOSTS:
        print(f"refusing non-loopback bind: {args.host}", file=sys.stderr)
        return 2
    capability = os.environ.get(CAPABILITY_ENV, "")
    if not capability:
        print(f"{CAPABILITY_ENV} is required", file=sys.stderr)
        return 2
    if not args.attempt_id:
        print(f"--attempt-id (or {ATTEMPT_ENV}) is required", file=sys.stderr)
        return 2
    if not args.usage_file:
        print(f"--usage-file (or {USAGE_FILE_ENV}) is required", file=sys.stderr)
        return 2
    usage_path = Path(args.usage_file)
    if not usage_path.parent.is_dir():
        print(f"usage parent is not a directory: {usage_path.parent}", file=sys.stderr)
        return 2
    allowed = frozenset(m for m in args.allowed_models if isinstance(m, str) and m)
    if not allowed:
        print("at least one --allowed-model is required", file=sys.stderr)
        return 2
    concurrency = max(1, min(int(args.max_concurrency or 0), MAX_CONCURRENT_UPSTREAM))
    try:
        gateway = StdioGateway(
            shlex.split(args.stdio_command),
            max_concurrency=concurrency,
            upstream_timeout=UPSTREAM_TIMEOUT_SECONDS,
        )
    except GatewayError as exc:
        print(f"stdio gateway unavailable: {exc}", file=sys.stderr)
        return 1
    with contextlib.suppress(OSError):
        os.chmod(usage_path, 0o600) if usage_path.exists() else None
    server = BridgeServer(
        (args.host, int(args.port or 0)),
        capability=capability,
        attempt_id=args.attempt_id,
        usage_path=usage_path,
        allowed_models=allowed,
        gateway=gateway,
    )
    if args.ready_file is not None:
        bound_host, bound_port = server.server_address[:2]
        _write_ready_file(Path(args.ready_file), str(bound_host), int(bound_port))
    _log(
        {
            "bridge": "ready",
            "attempt": args.attempt_id,
            "models": sorted(allowed),
            "concurrency": concurrency,
        }
    )

    def _on_sigterm(_signum: object, _frame: object) -> None:
        _SHUTDOWN_REQUESTED.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)  # type: ignore[arg-type]
    server.serve_forever()
    # Drain in-flight provider calls so their usage settles; exit as soon as
    # nothing is in flight rather than burning the whole deadline idle.
    deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    while server.in_flight > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    gateway.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
