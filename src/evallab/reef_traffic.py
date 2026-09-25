"""Eval Lab side of HAR-74: run the harness Reef serves, route Terminus 2's model
calls through Reef's capture proxy, and post each verifier score back to Reef.

Eval Lab stays the runner and the evidence of record; Reef's harness proposers
learn from this traffic. No model training, no paid sweeps, no held-out runs.

Reef wire-format mirrors (Reef checkout at ``818997d7``; ``reef-client`` 0.2.0
from PyPI — Reef's own locked dependency — read-only at
``~/Developer/reef/.venv/lib/python3.12/site-packages/reef_client/``):

- ``docs/reference/http-api.rst:94-117`` — ``x-reef-scenario`` names the
  workload on inference, report, harness manifest, releases and proposals;
  ``Authorization: Bearer`` on every route but ``GET /healthz``;
  ``x-reef-tag-<name>`` rides inference and is stored under
  ``payload.metadata.tags``.
- ``docs/reference/http-api.rst:499-517`` — a non-streaming inference answer
  carries its receipt in the ``x-reef-agent-record-id`` response header; a
  file-serving scenario also sends ``x-reef-release-id``.
- ``docs/reference/http-api.rst:542-605`` — ``POST /reef/report`` stores
  ``score``/``feedback``/``references``/``metadata``; ``agent_record_id`` makes
  posting retry-safe (identical resend returns the stored record, same id with
  different content is HTTP 409); every reference must already be stored in the
  same scenario (HTTP 400 otherwise).
- ``docs/reference/http-api.rst:639-648`` — ``GET /reef/harness`` answers
  ``{release_id, content_id, parent_release_id, files, evaluation, requires}``
  plus an ``x-reef-release-id`` header; a pin reads ``?release_id=``.
- ``third_party/reef-client`` ``reef_client/serve.py`` — ``ServeConfig``,
  ``CaptureStore``/``CapturedTurn`` and ``build_handler``: the stdlib-only
  forward-and-capture proxy this module mirrors (Eval Lab never imports
  ``reef`` itself, and takes no new dependency).
- ``reef/harness/client/wrapper.py:592-654`` — ``CaptureProxy`` tags every
  forwarded call and keeps the receipts; ``reef/harness/client/tasks.py:297``
  tags each trial ``{task, episode}``, ``tasks.py:311`` keeps receipts with
  HTTP 200 only, ``tasks.py:316`` skips unscored trials, and
  ``tasks.py:336-366`` posts one report per episode with ``metadata.task``.

Two verified gotchas shape this module (experiment 06, ``## 06``):

- the installed ``reef-client`` 0.2.0 ``harness_pull`` is stale against this
  server (``client.py:193-249`` reads ``manifest["artifact_version"]`` and
  pulls with ``?version=``, but the server answers ``release_id`` under
  ``?release_id=`` per ``reef/service/request_service.py:596-605``), so the
  pull below is a raw ``GET /reef/harness`` with no new dependency;
- the records list endpoint returns summaries only (no tags); tags ride
  ``payload.metadata.tags`` on the per-record detail endpoint.

Pinning (parent finding, verified by diffing two pulls of the same harness):
only the served harness *files* enter the HAR-71 digest. Reef bookkeeping
(``.reef-harness-version``, ``.reef-harness-release``) is excluded, and
``content_id`` is recorded as provenance, not identity: two releases with
identical files pin to the same digest, and the execution-time drift check
compares file digests, never ``release_id``/``content_id``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

#: Header naming the workload a record belongs to (http-api.rst:94-117).
SCENARIO_HEADER = "x-reef-scenario"
#: Receipt header on every non-streaming inference answer (http-api.rst:499-517).
RECEIPT_HEADER = "x-reef-agent-record-id"
#: Release header on inference answers of file-serving scenarios.
RELEASE_HEADER = "x-reef-release-id"
#: Prefix for per-call opaque tags (http-api.rst:114-117).
TAG_PREFIX = "x-reef-tag-"
#: Inference paths the proxy captures (wrapper.py CAPTURE_PATHS minus the
#: Anthropic beta-query form, which Terminus 2 never sends).
CAPTURE_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
#: Reef bookkeeping sidecars, never part of the pinned harness bytes.
BOOKKEEPING_NAMES = frozenset({".reef-harness-version", ".reef-harness-release"})

_SCENARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOKEN_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_TAG_VALUE_RE = re.compile(r"^[\x20-\x7e]+$")


class ReefTrafficError(RuntimeError):
    """A Reef HTTP call failed; ``status`` is the HTTP status when known."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HeldoutRefusal(ValueError):
    """A held-out task was refused before any call reached Reef."""


class ReefDriftError(ValueError):
    """Reef serves different harness files than the approved pin."""


def check_scenario(value: str) -> str:
    """Validate a scenario name (letters, digits, ``_``/``.``/``-``)."""
    if not isinstance(value, str) or _SCENARIO_RE.fullmatch(value) is None or len(value) > 64:
        raise ValueError(f"reef scenario must match {_SCENARIO_RE.pattern}, got {value!r}")
    return value


def check_token_env(value: str) -> str:
    """Validate a token *variable name* (never a token value)."""
    if not isinstance(value, str) or _TOKEN_ENV_RE.fullmatch(value) is None or len(value) > 64:
        raise ValueError(f"reef token_env must be an env-var name, got {value!r}")
    return value


def check_url(value: str) -> str:
    """Fail closed to http(s) loopback URLs; the bearer token never leaves host."""
    if not isinstance(value, str):
        raise ValueError(f"reef url must be a string, got {value!r}")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"reef url is invalid: {value!r}") from exc
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"reef url must be an http(s) loopback URL, got {value!r}")
    return value.rstrip("/")


def checked_tags(tags: Mapping[str, str]) -> dict[str, str]:
    """Tag values as the service stores them: nonempty printable ASCII."""
    checked: dict[str, str] = {}
    for name, value in tags.items():
        text = str(value)
        if not text or _TAG_VALUE_RE.fullmatch(text) is None:
            raise ValueError(f"reef tag {name!r} needs a nonempty printable ASCII value")
        checked[str(name)] = text
    return checked


# --------------------------------------------------------------------------- #
# Stdlib HTTP client (mirrors reef_client/client.py without the dependency).
# --------------------------------------------------------------------------- #


def _http_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None,
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], Mapping[str, str]]:
    """Send one JSON request; return (parsed body, lowercased response headers)."""
    body = None
    outgoing = dict(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        outgoing["Content-Type"] = "application/json"
        outgoing["Content-Length"] = str(len(body))
    request = urllib.request.Request(url, data=body, headers=outgoing, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            response_headers = {key.lower(): value for key, value in response.getheaders()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReefTrafficError(
            f"Reef service returned {exc.code}: {detail[:300]}",
            status=exc.code,
            body=detail,
        ) from exc
    except OSError as exc:
        raise ReefTrafficError(f"Reef service unreachable at {url}: {exc}") from exc
    try:
        parsed: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise ReefTrafficError(f"Reef service returned non-JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise ReefTrafficError("Reef service returned a non-object JSON body")
    return parsed, response_headers


def _auth_headers(scenario: str, token: str) -> dict[str, str]:
    check_scenario(scenario)
    if not token:
        raise ValueError("reef token is empty; set the env var named by token_env")
    return {SCENARIO_HEADER: scenario, "Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class ServedHarness:
    """One ``GET /reef/harness`` answer (http-api.rst:639-648)."""

    release_id: str
    content_id: str
    parent_release_id: str | None
    files: dict[str, str]


def pull_manifest(
    url: str,
    scenario: str,
    token: str,
    *,
    release_id: str | None = None,
    timeout_s: float = 60.0,
) -> ServedHarness:
    """Read the served harness manifest (raw GET; bundled harness_pull is stale)."""
    base = check_url(url)
    path = "/reef/harness"
    if release_id is not None:
        path += f"?release_id={urllib.parse.quote(release_id, safe='')}"
    body, _ = _http_json("GET", base + path, _auth_headers(scenario, token), None, timeout_s=timeout_s)
    try:
        release = body["release_id"]
        content = body["content_id"]
        files = body["files"]
    except KeyError as exc:
        raise ReefTrafficError(f"harness manifest is missing {exc}") from exc
    if not isinstance(release, str) or not release:
        raise ReefTrafficError("harness manifest carries no release_id")
    if not isinstance(content, str) or not content:
        raise ReefTrafficError("harness manifest carries no content_id")
    if not isinstance(files, dict) or not files:
        raise ReefTrafficError("harness manifest carries no files")
    for relative, text in files.items():
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).is_absolute() or ".." in parts or not isinstance(text, str):
            raise ReefTrafficError(f"served path {relative!r} is unsafe or not text")
    parent = body.get("parent_release_id")
    return ServedHarness(
        release_id=release,
        content_id=content,
        parent_release_id=parent if isinstance(parent, str) and parent else None,
        files=dict(files),
    )


def canonical_files_digest(files: Mapping[str, bytes]) -> str:
    """Digest served-file bytes with the evidence-tree framing.

    Mirrors ``evallab.evidence_store._content_digest`` (sorted relative paths,
    length-prefixed names and contents), so the in-memory digest of a manifest
    equals ``evidence_tree_digest`` of the directory those bytes are written
    to. Covered by a framing-parity test.
    """
    digest = hashlib.sha256()
    for relative in sorted(files):
        content = files[relative]
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def served_files_digest(files: Mapping[str, str]) -> str:
    """Digest a manifest's served text files as written to disk (UTF-8, exact)."""
    return canonical_files_digest({name: text.encode("utf-8") for name, text in files.items()})


def write_served_tree(files: Mapping[str, str], destination: Path) -> Path:
    """Write served files only: no sidecars, no rendering, no validation.

    Served paths must stay inside ``destination``; an absolute or
    parent-escaping path refuses the pull (mirrors the client's own guard).
    """
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).is_absolute() or ".." in parts:
            raise ValueError(f"served path {relative!r} escapes the destination")
        if PurePosixPath(relative).name in BOOKKEEPING_NAMES:
            raise ValueError(f"served path {relative!r} collides with Reef bookkeeping")
        target = root / PurePosixPath(relative).as_posix()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return root


@dataclass(frozen=True)
class PinnedHarness:
    """A pulled tree frozen with HAR-71 pinning plus its Reef provenance."""

    path: Path
    digest: str
    release_id: str
    content_id: str
    parent_release_id: str | None


def pull_and_pin(
    url: str,
    scenario: str,
    token: str,
    destination: Path,
    *,
    release_id: str | None = None,
    timeout_s: float = 60.0,
) -> PinnedHarness:
    """Pull the served tree and pin it with HAR-71 validation.

    Only served files enter the digest: the pull writes no sidecars, so the
    HAR-71 pin covers the harness bytes and nothing else. ``release_id`` and
    ``content_id`` travel beside the digest as provenance.
    """
    from evallab.terminus_harness import load_harness_tree

    manifest = pull_manifest(url, scenario, token, release_id=release_id, timeout_s=timeout_s)
    write_served_tree(manifest.files, destination)
    tree = load_harness_tree(destination)
    expected = served_files_digest(manifest.files)
    if tree.sha256 != expected:
        raise ReefTrafficError(
            f"pinned digest {tree.sha256} does not match served bytes {expected}"
        )
    return PinnedHarness(
        path=Path(destination),
        digest=tree.sha256,
        release_id=manifest.release_id,
        content_id=manifest.content_id,
        parent_release_id=manifest.parent_release_id,
    )


@dataclass(frozen=True)
class DriftRecord:
    """What Reef serves now, against the approved pin."""

    served_release_id: str
    served_content_id: str
    digest_matches: bool
    release_changed: bool


def check_drift(
    url: str,
    scenario: str,
    token: str,
    *,
    pinned_digest: str,
    pinned_release_id: str,
    timeout_s: float = 60.0,
) -> DriftRecord:
    """Re-read the served manifest and compare file digests, never bare ids.

    A changed ``release_id`` with identical files is provenance, not drift.
    Different files refuse: approval bound one exact tree.
    """
    manifest = pull_manifest(url, scenario, token, timeout_s=timeout_s)
    digest = served_files_digest(manifest.files)
    record = DriftRecord(
        served_release_id=manifest.release_id,
        served_content_id=manifest.content_id,
        digest_matches=digest == pinned_digest,
        release_changed=manifest.release_id != pinned_release_id,
    )
    if not record.digest_matches:
        raise ReefDriftError(
            f"Reef serves different harness files than the approved pin {pinned_digest}: "
            f"release {manifest.release_id} digests to {digest}"
        )
    return record


# --------------------------------------------------------------------------- #
# Held-out refusal: before any Reef call. Same rule as training_pool.py:120
# (``heldout`` in ``allowed_uses`` refuses). HAR-73 will add a committed split
# as a second source beside this call; nothing stubbed here.
# --------------------------------------------------------------------------- #


def refuse_if_heldout(allowed_uses: object, *, task_label: str) -> None:
    """Refuse when a registry ``allowed_uses`` value contains ``heldout``."""
    uses = list(allowed_uses) if isinstance(allowed_uses, (list, tuple)) else []
    if "heldout" in [str(use) for use in uses]:
        raise HeldoutRefusal(
            f"task {task_label!r} is held-out (allowed_uses {uses!r}); "
            "refusing before any call reaches Reef"
        )


def heldout_uses_for_task(
    repo_root: Path,
    *,
    task_id: str | None = None,
    task_path: str | None = None,
    task_dir: Path | None = None,
    package_digest: str | None = None,
) -> tuple[str, list[str]] | None:
    """Find a registry record's ``(task_id, allowed_uses)`` for a task, if any.

    Matches by explicit ``task_id`` (``registered/<id>`` specs), by resolved
    task directory, or by package digest. Returns ``None`` when no record
    matches: local-package tasks carry no registry signal. Never raises for a
    missing or unreadable registry; registry *errors* still raise.
    """
    from evallab.registry import TaskRegistry

    try:
        registry = TaskRegistry.from_repo(Path(repo_root))
    except (OSError, ValueError):
        return None
    if task_id is not None:
        record = registry.get(task_id)
        if record is not None:
            return record.task_id, list(record.allowed_uses)
    resolved_dir: Path | None = None
    if task_dir is not None:
        try:
            resolved_dir = Path(task_dir).resolve()
        except OSError:
            resolved_dir = None
    for record in registry.list_records():
        if task_path is not None and record.task_path == task_path:
            return record.task_id, list(record.allowed_uses)
        if resolved_dir is not None:
            try:
                candidate = (Path(repo_root) / record.task_path).resolve()
            except OSError:
                continue
            if candidate == resolved_dir:
                return record.task_id, list(record.allowed_uses)
        if package_digest is not None and record.digests.package == package_digest:
            return record.task_id, list(record.allowed_uses)
    return None


def check_task_allowed(
    repo_root: Path,
    *,
    task_label: str,
    task_id: str | None = None,
    task_path: str | None = None,
    task_dir: Path | None = None,
    package_digest: str | None = None,
) -> None:
    """Refuse held-out tasks before any Reef call; pass silently otherwise."""
    found = heldout_uses_for_task(
        repo_root,
        task_id=task_id,
        task_path=task_path,
        task_dir=task_dir,
        package_digest=package_digest,
    )
    if found is not None:
        _, allowed_uses = found
        refuse_if_heldout(allowed_uses, task_label=task_label)


# --------------------------------------------------------------------------- #
# Capture proxy: one model path. Mirrors reef_client/serve.py's forwarding
# store and wrapper.py's CaptureProxy tags, stdlib-only.
# --------------------------------------------------------------------------- #


@dataclass
class CapturedTurn:
    """One forwarded inference call: its receipt, status, tags and release."""

    receipt: str | None
    status: int
    tags: dict[str, str]
    release_id: str | None = None


_HOP_BY_HOP = frozenset(
    {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
     "te", "trailer", "transfer-encoding", "upgrade", "content-length"}
)


def _make_handler(
    proxy: ReefCaptureProxy,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # quiet
            pass

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else None
            body_json: dict[str, Any] | None = None
            if raw:
                try:
                    body_json = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body_json = None
            split = urllib.parse.urlsplit(self.path)
            forward_path = split.path + (f"?{split.query}" if split.query else "")
            capture = (
                self.command == "POST"
                and split.path in CAPTURE_PATHS
                and body_json is not None
            )
            headers = {
                key: value for key, value in self.headers.items() if key.lower() not in _HOP_BY_HOP
            }
            headers[SCENARIO_HEADER] = proxy.scenario
            headers["Authorization"] = f"Bearer {proxy._token}"
            for name, value in proxy._tags.items():
                headers[f"{TAG_PREFIX}{name}"] = value
            if raw is not None:
                headers["Content-Length"] = str(len(raw))
            connection: http.client.HTTPConnection = (
                http.client.HTTPSConnection(proxy._upstream_host, proxy._upstream_port, timeout=proxy.timeout_s)
                if proxy._upstream_scheme == "https"
                else http.client.HTTPConnection(proxy._upstream_host, proxy._upstream_port, timeout=proxy.timeout_s)
            )
            try:
                connection.request(self.command, forward_path, body=raw, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                status = response.status
                response_headers = {key.lower(): value for key, value in response.getheaders()}
            except Exception as exc:
                self.send_error(502, f"reef capture proxy: {exc}")
                return
            finally:
                connection.close()
            receipt = response_headers.get(RECEIPT_HEADER)
            if capture:
                with proxy._lock:
                    proxy._turns.append(
                        CapturedTurn(
                            receipt=receipt,
                            status=status,
                            tags=dict(proxy._tags),
                            release_id=response_headers.get(RELEASE_HEADER),
                        )
                    )
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            content_type = response_headers.get("content-type")
            if content_type:
                self.send_header("Content-Type", content_type)
            if receipt:
                self.send_header(RECEIPT_HEADER, receipt)
            release = response_headers.get(RELEASE_HEADER)
            if release:
                self.send_header(RELEASE_HEADER, release)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def do_POST(self) -> None:
            self._forward()

        def do_GET(self) -> None:
            self._forward()

    return Handler


class ReefCaptureProxy:
    """Forward Terminus model calls to Reef with scenario/tag headers.

    Mirrors ``reef/harness/client/wrapper.py:592`` ``CaptureProxy``: every
    forwarded call carries ``x-reef-scenario`` and ``x-reef-tag-<name>``; the
    token lives in this (runner-side) process only, overriding whatever the
    client sent; receipts accumulate per turn. Composes with HAR-70's
    host-loopback proxy path: the runner binds this proxy on 127.0.0.1 and
    hands the Harbor child only its URL.
    """

    def __init__(
        self,
        upstream: str,
        scenario: str,
        token: str,
        *,
        tags: Mapping[str, str] | None = None,
        timeout_s: float = 7200.0,
    ) -> None:
        self.upstream = check_url(upstream)
        split = urllib.parse.urlsplit(self.upstream)
        if split.hostname is None:
            raise ValueError(f"reef url has no host: {upstream!r}")
        self._upstream_host = split.hostname
        self._upstream_port = split.port or (443 if split.scheme == "https" else 80)
        self._upstream_scheme = split.scheme
        self.scenario = check_scenario(scenario)
        if not token:
            raise ValueError("reef capture proxy requires a bearer token")
        self._token = token
        self._tags = checked_tags(tags or {})
        self.timeout_s = timeout_s
        self._turns: list[CapturedTurn] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def tags(self) -> dict[str, str]:
        """Live tag channel: each name rides every forwarded call."""
        return self._tags

    @property
    def url(self) -> str:
        """Loopback URL for the Harbor child; only valid while running."""
        if self._server is None:
            raise ReefTrafficError("the reef capture proxy is not running")
        return f"http://127.0.0.1:{int(self._server.server_address[1])}"

    def start(self) -> None:
        """Bind 127.0.0.1 on an ephemeral port and serve in a daemon thread."""
        if self._server is not None:
            raise ReefTrafficError("the reef capture proxy is already running")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self._server = server

    def stop(self) -> None:
        """Quiesce the proxy; safe to call repeatedly."""
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def drain(self) -> list[CapturedTurn]:
        """Captured turns since the last drain, oldest first."""
        with self._lock:
            turns, self._turns = list(self._turns), []
        return turns

    def receipts(self) -> list[str]:
        """Successful (HTTP 200) inference receipts (tasks.py:311)."""
        return [turn.receipt for turn in self.drain() if turn.status == 200 and turn.receipt]

# --------------------------------------------------------------------------- #
# Reports: one per trial, score = verifier reward (http-api.rst:542-605,
# tasks.py:336-366). No feedback: TrialDiagnosis is unmerged, and hidden
# verifier inputs must never enter agent-visible feedback.
# --------------------------------------------------------------------------- #


def build_report_payload(
    *,
    trial_id: str,
    score: float,
    references: list[str],
    task_name: str,
    task_path: str,
    task_digest: str,
) -> dict[str, Any]:
    """Build the exact card-step-4 payload: score, references, task, trial id."""
    if not trial_id:
        raise ValueError("report agent_record_id (trial id) must be nonempty")
    if not math_is_finite(score):
        raise ValueError(f"report score must be finite, got {score!r}")
    unique = list(dict.fromkeys(references))
    if not unique:
        raise ValueError("report requires at least one receipt reference")
    if not task_name or not task_path or not task_digest:
        raise ValueError("report metadata.task needs name, path and digest")
    return {
        "agent_record_id": trial_id,
        "score": float(score),
        "references": unique,
        "metadata": {"task": {"name": task_name, "path": task_path, "digest": task_digest}},
    }


def math_is_finite(value: object) -> bool:
    """Finite-number check that rejects bools (mirrors episode_reward)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def post_report(
    url: str,
    scenario: str,
    token: str,
    payload: Mapping[str, Any],
    *,
    timeout_s: float = 60.0,
) -> str:
    """POST one report; return its ``agent_record_id``.

    Identical resends return the stored record (retry-safe). A 409 (same id,
    different content) or 400 (missing/foreign references) raises without
    inventing a new id.
    """
    body, _ = _http_json(
        "POST", check_url(url) + "/reef/report", _auth_headers(scenario, token),
        dict(payload), timeout_s=timeout_s,
    )
    record_id = body.get("agent_record_id")
    if not isinstance(record_id, str) or not record_id:
        raise ReefTrafficError("report answer carries no agent_record_id")
    return record_id


def post_report_retrying(
    url: str,
    scenario: str,
    token: str,
    payload: Mapping[str, Any],
    *,
    timeout_s: float = 60.0,
    max_attempts: int = 3,
) -> str:
    """POST one report, retrying connection loss only, with the identical id.

    HTTP answers (including 409/400) are final: only a lost reply may have
    committed server-side, where the identical resend deduplicates.
    """
    last: ReefTrafficError | None = None
    for _ in range(max_attempts):
        try:
            return post_report(url, scenario, token, payload, timeout_s=timeout_s)
        except ReefTrafficError as exc:
            if exc.status is not None:
                raise
            last = exc
    raise last or ReefTrafficError("report failed without an attempt")


def trial_report_decision(
    *,
    trial_id: str,
    result: Mapping[str, Any],
    rewards: Mapping[str, Any],
) -> tuple[float | None, str]:
    """Score a trial for reporting, or skip with a reason.

    Mirrors ``matrix_run_outcome``'s per-trial rule and Reef's own player
    (``tasks.py:316``): harness failures and absent/non-finite rewards are
    never reported, and never as 0.
    """
    if not isinstance(result, Mapping):
        return None, "unscored: unreadable result"
    if result.get("exception_info"):
        return None, "infra-failed: trial carries exception_info"
    if not result.get("finished_at"):
        return None, "infra-failed: trial never finished"
    raw = rewards.get("reward")
    if raw is None:
        return None, "unscored: verifier wrote no reward"
    if not math_is_finite(raw):
        return None, "unscored: reward is not a finite number"
    _ = trial_id
    return float(raw), "ok"


@dataclass
class TrialReport:
    """Outcome of reporting one trial: the report id or the skip reason."""

    trial_id: str
    status: str
    report_id: str | None = None
    reason: str = ""


def report_job_trials(
    *,
    url: str,
    scenario: str,
    token: str,
    task_name: str,
    task_path: str,
    task_digest: str,
    receipts: list[str],
    trials: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    timeout_s: float = 60.0,
    max_attempts: int = 3,
) -> list[TrialReport]:
    """Report every scored trial of one job; skip unscored/infra-failed ones.

    ``trials`` holds ``(trial_id, result, rewards)`` per trial. With no
    receipts nothing is reported: the server needs at least one reference.
    Report errors are recorded per trial, never raised: the receipts stay in
    evidence and the retry-safe id allows a later repost.
    """
    reports: list[TrialReport] = []
    for trial_id, result, rewards in trials:
        score, reason = trial_report_decision(trial_id=trial_id, result=result, rewards=rewards)
        if score is None:
            reports.append(TrialReport(trial_id=trial_id, status="skipped", reason=reason))
            continue
        if not receipts:
            reports.append(TrialReport(trial_id=trial_id, status="skipped", reason="no_receipts"))
            continue
        try:
            payload = build_report_payload(
                trial_id=trial_id, score=score, references=list(receipts),
                task_name=task_name, task_path=task_path, task_digest=task_digest,
            )
            record_id = post_report_retrying(
                url, scenario, token, payload, timeout_s=timeout_s, max_attempts=max_attempts
            )
        except ReefTrafficError as exc:
            reports.append(TrialReport(trial_id=trial_id, status="error", reason=str(exc)[:300]))
            continue
        reports.append(TrialReport(trial_id=trial_id, status="reported", report_id=record_id, reason="ok"))
    return reports


def reef_evidence_block(
    *,
    url: str,
    scenario: str,
    release_id: str,
    content_id: str,
    digest: str,
    drift: DriftRecord | None,
    receipts: list[str],
    reports: list[TrialReport],
) -> dict[str, Any]:
    """Job-level Reef evidence: ids, drift record, receipts and report ids.

    Takes no token parameter by construction: no credential value can enter
    retained evidence through this block.
    """
    return {
        "schema_version": 1,
        "url": url,
        "scenario": scenario,
        "release_id": release_id,
        "content_id": content_id,
        "digest": digest,
        "drift": (
            {
                "served_release_id": drift.served_release_id,
                "served_content_id": drift.served_content_id,
                "release_changed": drift.release_changed,
            }
            if drift is not None
            else None
        ),
        "receipts": list(receipts),
        "reports": [
            {
                "trial_id": item.trial_id,
                "status": item.status,
                "report_id": item.report_id,
                "reason": item.reason,
            }
            for item in reports
        ],
    }
