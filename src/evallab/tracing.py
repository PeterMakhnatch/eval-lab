"""ATIF → OpenTelemetry conversion and Phoenix shipping.

Uses `harbor-atif2otel` the same way RECON's harbor-021 demo did: validate,
convert, then (optionally) POST OTLP protobuf to Phoenix `/v1/traces`.
Missing or invalid trajectories raise `TraceError` with a one-line message.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTROL_AGENTS = frozenset({"oracle", "nop"})
DEFAULT_ENDPOINT = "http://127.0.0.1:6006"
DEFAULT_SERVICE_NAME = "evallab"
TRAJECTORY_RELATIVE = Path("agent") / "trajectory.json"


class TraceError(ValueError):
    """User-facing convert/ship failure. The CLI prints this and exits 2."""


@dataclass(frozen=True)
class SpanSummary:
    n_spans: int
    n_root_spans: int
    root_kinds: tuple[str, ...]
    root_names: tuple[str, ...]
    span_kinds: tuple[str, ...]

    @property
    def has_root_agent(self) -> bool:
        return "AGENT" in self.root_kinds


@dataclass
class TrialTraceResult:
    trial_dir: Path
    trajectory_path: Path | None = None
    status: str = "ok"
    message: str = ""
    summary: SpanSummary | None = None
    otel_json: dict[str, Any] | None = None


@dataclass
class TraceBatch:
    results: list[TrialTraceResult] = field(default_factory=list)

    @property
    def shipped(self) -> int:
        return sum(1 for item in self.results if item.status == "ok")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")


def default_endpoint() -> str:
    return os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", DEFAULT_ENDPOINT)


def trajectory_path_for(trial_dir: Path) -> Path:
    return trial_dir / TRAJECTORY_RELATIVE


def is_trial_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (path / "trial.log").is_file() or (path / "agent").is_dir()


def is_job_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "job.log").is_file():
        return True
    return (path / "config.json").is_file() and any(
        is_trial_dir(child) for child in path.iterdir() if child.is_dir()
    )


def is_trajectory_file(path: Path) -> bool:
    return path.is_file() and path.name == "trajectory.json"


def iter_trial_dirs(path: Path) -> list[Path]:
    resolved = path.resolve()
    if not resolved.exists():
        raise TraceError(f"path does not exist: {resolved}")
    if is_trajectory_file(resolved):
        return [resolved.parent.parent if resolved.parent.name == "agent" else resolved.parent]
    if is_trial_dir(resolved) and not is_job_dir(resolved):
        return [resolved]
    if is_job_dir(resolved):
        trials = [child for child in sorted(resolved.iterdir()) if is_trial_dir(child)]
        if not trials:
            raise TraceError(f"job directory contains no trials: {resolved}")
        return trials
    if is_trial_dir(resolved):
        return [resolved]
    raise TraceError(f"not a Harbor trial or job directory: {resolved}")


def load_trajectory(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TraceError(
            f"no ATIF trajectory at {path} (oracle/nop controls write agent/oracle.txt instead)"
        )
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise TraceError(f"trajectory is not valid JSON: {path} ({exc})") from None
    if not isinstance(payload, dict):
        raise TraceError(f"trajectory must be a JSON object: {path}")
    return payload


def _require_atif2otel():
    try:
        from harbor_atif2otel import (  # type: ignore[import-not-found]
            convert_trajectory,
            resource_spans_to_otlp_json,
            validate_trajectory,
        )
    except ImportError as exc:
        raise TraceError(
            "harbor-atif2otel is not installed; "
            "uv sync --group observability (or uv sync, which includes that group)"
        ) from exc
    return convert_trajectory, resource_spans_to_otlp_json, validate_trajectory


def validate_atif(trajectory: dict[str, Any]) -> list[str]:
    _, _, validate_trajectory = _require_atif2otel()
    return list(validate_trajectory(trajectory))


def convert_atif(
    trajectory: dict[str, Any],
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> tuple[Any, dict[str, Any]]:
    convert_trajectory, resource_spans_to_otlp_json, validate_trajectory = _require_atif2otel()
    issues = list(validate_trajectory(trajectory))
    if issues:
        rendered = "\n".join(f"  - {issue}" for issue in issues)
        raise TraceError(f"invalid ATIF trajectory:\n{rendered}")
    resource_spans = convert_trajectory(trajectory, service_name=service_name)
    payload = resource_spans_to_otlp_json(resource_spans)
    return resource_spans, payload


def _span_kind(attributes: list[dict[str, Any]]) -> str | None:
    for attr in attributes:
        if attr.get("key") == "openinference.span.kind":
            value = attr.get("value") or {}
            if isinstance(value, dict):
                return value.get("stringValue")
    return None


def summarize_otel(payload: dict[str, Any]) -> SpanSummary:
    spans: list[dict[str, Any]] = []
    for resource in payload.get("resourceSpans") or []:
        for scope in resource.get("scopeSpans") or []:
            spans.extend(scope.get("spans") or [])
    kinds = [_span_kind(span.get("attributes") or []) for span in spans]
    roots = [span for span in spans if not span.get("parentSpanId")]
    root_kinds = tuple(k for k in (_span_kind(s.get("attributes") or []) for s in roots) if k)
    root_names = tuple(str(span.get("name") or "") for span in roots)
    present = tuple(sorted({kind for kind in kinds if kind}))
    return SpanSummary(
        n_spans=len(spans),
        n_root_spans=len(roots),
        root_kinds=root_kinds,
        root_names=root_names,
        span_kinds=present,
    )


def traces_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def ship_resource_spans(resource_spans: Any, *, endpoint: str) -> None:
    try:
        from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
    except ImportError as exc:
        raise TraceError(
            "opentelemetry-proto is not installed; uv sync --group observability"
        ) from exc
    url = traces_endpoint(endpoint)
    body = TracesData(resource_spans=[resource_spans]).SerializeToString()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-protobuf"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise TraceError(f"OTLP export to {url} returned HTTP {status}")
    except HTTPError as exc:
        raise TraceError(f"OTLP export to {url} returned HTTP {exc.code}") from None
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise TraceError(
            f"could not reach Phoenix OTLP at {url} ({reason}). "
            "Integrator starts Phoenix from the main checkout: "
            "docker compose up -d phoenix"
        ) from None


def trial_agent_name(trial_dir: Path) -> str:
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
        except (json.JSONDecodeError, OSError):
            result = {}
        if isinstance(result, dict):
            agent_info = result.get("agent_info") or {}
            if isinstance(agent_info, dict) and agent_info.get("name"):
                return str(agent_info["name"])
    config_path = trial_dir / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            config = {}
        if isinstance(config, dict):
            agent = config.get("agent") or {}
            if isinstance(agent, dict) and agent.get("name"):
                return str(agent["name"])
    return ""


def is_control_trial(trial_dir: Path) -> bool:
    return trial_agent_name(trial_dir) in CONTROL_AGENTS


def convert_source(
    path: Path,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> tuple[Any, dict[str, Any], SpanSummary, Path]:
    trajectory = load_trajectory(path)
    resource_spans, payload = convert_atif(trajectory, service_name=service_name)
    summary = summarize_otel(payload)
    if not summary.has_root_agent:
        raise TraceError(
            f"converted {path} but no root AGENT span was present (kinds={summary.span_kinds})"
        )
    return resource_spans, payload, summary, path


def convert_trial(
    trial_dir: Path,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> tuple[Any, dict[str, Any], SpanSummary, Path]:
    return convert_source(trajectory_path_for(trial_dir), service_name=service_name)


def _append_converted(
    batch: TraceBatch,
    *,
    trial_dir: Path,
    source: Path,
    endpoint: str,
    dry_run: bool,
    service_name: str,
) -> None:
    try:
        resource_spans, payload, summary, traj_path = convert_source(
            source, service_name=service_name
        )
        if not dry_run:
            ship_resource_spans(resource_spans, endpoint=endpoint)
        batch.results.append(
            TrialTraceResult(
                trial_dir=trial_dir,
                trajectory_path=traj_path,
                status="ok",
                message="dry-run" if dry_run else f"shipped to {traces_endpoint(endpoint)}",
                summary=summary,
                otel_json=payload,
            )
        )
    except TraceError as exc:
        status = "skipped" if "no ATIF trajectory" in str(exc) else "failed"
        batch.results.append(
            TrialTraceResult(
                trial_dir=trial_dir,
                trajectory_path=source,
                status=status,
                message=str(exc),
            )
        )


def trace_path(
    path: Path,
    *,
    endpoint: str | None = None,
    dry_run: bool = False,
    include_controls: bool = False,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> TraceBatch:
    batch = TraceBatch()
    target = endpoint or default_endpoint()
    resolved = path.resolve()
    if is_trajectory_file(resolved):
        _append_converted(
            batch,
            trial_dir=resolved.parent,
            source=resolved,
            endpoint=target,
            dry_run=dry_run,
            service_name=service_name,
        )
        return batch
    for trial_dir in iter_trial_dirs(path):
        if not include_controls and is_control_trial(trial_dir):
            batch.results.append(
                TrialTraceResult(
                    trial_dir=trial_dir,
                    trajectory_path=trajectory_path_for(trial_dir),
                    status="skipped",
                    message="control agent (oracle/nop); pass include_controls to trace",
                )
            )
            continue
        _append_converted(
            batch,
            trial_dir=trial_dir,
            source=trajectory_path_for(trial_dir),
            endpoint=target,
            dry_run=dry_run,
            service_name=service_name,
        )
    return batch


def trace_completed_jobs(
    runs_dir: Path,
    *,
    endpoint: str | None = None,
    include_controls: bool = False,
    dry_run: bool = False,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> TraceBatch:
    """Trace every job under `runs_dir`. Nightly uses include_controls=False."""
    combined = TraceBatch()
    if not runs_dir.is_dir():
        return combined
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir() or not is_job_dir(child):
            continue
        batch = trace_path(
            child,
            endpoint=endpoint,
            dry_run=dry_run,
            include_controls=include_controls,
            service_name=service_name,
        )
        combined.results.extend(batch.results)
    return combined


def instrument_openinference(*, enabled: bool = True) -> dict[str, bool]:
    """Wire OpenInference on LiteLLM and DSPy when those packages are present.

    Dormant until a researcher/DSPy call actually imports those libraries.
    Returns which instrumentors attached. Never raises.
    """
    wired = {"litellm": False, "dspy": False}
    if not enabled:
        return wired
    try:
        import litellm  # noqa: F401
        from openinference.instrumentation.litellm import LiteLLMInstrumentor

        LiteLLMInstrumentor().instrument()
        wired["litellm"] = True
    except Exception:
        pass
    try:
        import dspy  # noqa: F401
        from openinference.instrumentation.dspy import DSPyInstrumentor

        DSPyInstrumentor().instrument()
        wired["dspy"] = True
    except Exception:
        pass
    return wired


def format_batch(batch: TraceBatch) -> str:
    lines = [
        f"traced {batch.shipped}  skipped {batch.skipped}  failed {batch.failed}",
    ]
    for item in batch.results:
        name = item.trial_dir.name
        if item.summary is not None:
            lines.append(
                f"  {item.status:7} {name}  spans={item.summary.n_spans} "
                f"root={','.join(item.summary.root_names) or '-'} "
                f"kinds={','.join(item.summary.span_kinds)}"
            )
        else:
            lines.append(f"  {item.status:7} {name}  {item.message}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The session bridge: Phoenix trace <-> research graph.
#
# Converted spans carry no `spec_id`, `job_id` or `trial_id` — `atif2otel`
# converts the ATIF document alone. The one identifier that does cross is
# `session.id` on the root span, which is the ATIF `session_id` verbatim
# (harbor_atif2otel/convert.py:212) and is stored as
# `trajectory_documents.session_id`, from which `trial_id -> job_id ->
# experiment_id` follow. Everything below is derived from that single hop.
#
# Phoenix stays disposable: nothing here reads Phoenix, and no resolution
# result is evidence. The Harbor job directory remains canonical.
# --------------------------------------------------------------------------

# `scripts/promote_codex_bundle.py` writes this marker in place of any withheld
# text. The exact shape is pinned so a truncated marker cannot pass as a whole
# one; see `truncated_redaction_markers`.
REDACTION_MARKER_PREFIX = "<<evallab-redacted:"
REDACTION_MARKER_RE = re.compile(r"<<evallab-redacted: \d+ bytes, sha256:[0-9a-f]{64}>>")
SESSION_ATTRIBUTE = "session.id"


def _require_atif2otel_ids():
    try:
        from harbor_atif2otel.ids import (  # type: ignore[import-not-found]
            base_session_id,
            sha256_span_id,
            sha256_trace_id,
            trajectory_span_seed,
            trajectory_trace_seed,
        )
    except ImportError as exc:
        raise TraceError(
            "harbor-atif2otel is not installed; "
            "uv sync --group observability (or uv sync, which includes that group)"
        ) from exc
    return (
        base_session_id,
        sha256_span_id,
        sha256_trace_id,
        trajectory_span_seed,
        trajectory_trace_seed,
    )


@dataclass(frozen=True)
class TraceIdentity:
    """What a trial is called inside Phoenix.

    `session_id` is the raw ATIF value and is what lands on the span. Harbor
    writes continued sessions as `<base>-cont-N`, and the converter seeds the
    trace from the stripped base, so several `session_id` values can share one
    `trace_id`. `base_session_id` is therefore the trace key and `session_id`
    is the span key; they are not interchangeable.
    """

    session_id: str | None
    base_session_id: str | None
    trajectory_id: str | None
    trace_id: str
    root_span_id: str

    @property
    def joinable(self) -> bool:
        """True when a span from this trace can be resolved back to a trial."""
        return bool(self.session_id)


def trace_identity(trajectory: dict[str, Any]) -> TraceIdentity:
    """Resolve the Phoenix identity of an ATIF document without converting it.

    Uses `atif2otel`'s own seed functions, so the ids cannot drift from the
    ids the converter actually emits.
    """
    (
        base_session_id,
        sha256_span_id,
        sha256_trace_id,
        trajectory_span_seed,
        trajectory_trace_seed,
    ) = _require_atif2otel_ids()
    session_id = trajectory.get("session_id")
    session = str(session_id) if session_id else None
    trajectory_id = trajectory.get("trajectory_id")
    trace_id = sha256_trace_id(trajectory_trace_seed(trajectory)).hex()
    span_seed = trajectory_span_seed(trajectory, trace_id)
    return TraceIdentity(
        session_id=session,
        base_session_id=base_session_id(session) if session else None,
        trajectory_id=str(trajectory_id) if trajectory_id else None,
        trace_id=trace_id,
        root_span_id=sha256_span_id(f"{span_seed}:root").hex(),
    )


def trace_identity_for_trial(trial_dir: Path) -> TraceIdentity:
    """Trial -> trace. Reads the trial's ATIF document; never contacts Phoenix."""
    return trace_identity(load_trajectory(trajectory_path_for(trial_dir)))


def iter_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for resource in payload.get("resourceSpans") or []:
        for scope in resource.get("scopeSpans") or []:
            spans.extend(scope.get("spans") or [])
    return spans


def span_ids_by_step(payload: dict[str, Any]) -> dict[int, str]:
    """Map unambiguous ATIF step attributes to OTLP span IDs.

    A step may legitimately have several child spans.  Since an episode must be
    attached to one exact evidence span, such a mapping is rejected rather than
    selecting a span by traversal order.
    """
    step_keys = {
        "step_id",
        "step.id",
        "atif.step_id",
        "evallab.step_id",
        "harbor.step_id",
    }
    result: dict[int, str] = {}
    for span in iter_spans(payload):
        span_id = span.get("spanId") or span.get("span_id") or span.get("spanID")
        if not isinstance(span_id, str) or not span_id:
            continue
        step_id: int | None = None
        for attribute in span.get("attributes") or ():
            if not isinstance(attribute, dict) or attribute.get("key") not in step_keys:
                continue
            value = attribute.get("value")
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                continue
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if step_id is not None and step_id != candidate:
                raise TraceError(f"ambiguous step identity on span {span_id!r}")
            step_id = candidate
        if step_id is None:
            continue
        previous = result.get(step_id)
        if previous is not None and previous != span_id:
            raise TraceError(f"step {step_id} maps to multiple OTLP spans")
        result[step_id] = span_id
    return result


def span_attribute(span: dict[str, Any], key: str) -> str | None:
    for attr in span.get("attributes") or []:
        if attr.get("key") != key:
            continue
        value = attr.get("value")
        if isinstance(value, dict):
            for candidate in value.values():
                if isinstance(candidate, str):
                    return candidate
    return None


def root_session_id(payload: dict[str, Any]) -> str | None:
    """Span -> session. The `session.id` carried by the converted root span."""
    for span in iter_spans(payload):
        if span.get("parentSpanId"):
            continue
        session = span_attribute(span, SESSION_ATTRIBUTE)
        if session:
            return session
    return None


def payload_strings(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Every string in an OTLP payload as `(json path, value)`.

    Used to assert that nothing promotion withheld reaches a span, including
    attributes, span names, and events — not just the ones we thought of.
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            found.append((path, node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload, "$")
    return found


def redaction_markers(document: Any) -> tuple[str, ...]:
    """Every complete `<<evallab-redacted: ...>>` marker inside a JSON document.

    Markers are matched as substrings, not whole values: the converter folds a
    step message into a JSON-encoded message list on LLM spans, so a marker
    reaches the payload embedded in a larger string.
    """
    markers: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            markers.extend(REDACTION_MARKER_RE.findall(node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    return tuple(dict.fromkeys(markers))


def truncated_redaction_markers(document: Any) -> tuple[str, ...]:
    """Locations holding a marker prefix that no complete marker accounts for.

    `atif2otel` truncates long attribute values. A marker cut in half would
    still look redacted while no longer carrying the digest, so this is checked
    separately rather than folded into `redaction_markers`.
    """
    broken: list[str] = []
    for path, value in payload_strings(document):
        prefixes = value.count(REDACTION_MARKER_PREFIX)
        if prefixes and prefixes != len(REDACTION_MARKER_RE.findall(value)):
            broken.append(path)
    return tuple(broken)


def leaked_values(payload: dict[str, Any], secrets: Iterable[str]) -> tuple[str, ...]:
    """Payload locations whose text contains any of `secrets`.

    Empty means the OTLP payload is free of that text. This is the check that
    makes shipping a promoted trajectory to Phoenix safe: promotion's redaction
    is only useful if it survives conversion.
    """
    wanted = [secret for secret in secrets if secret]
    if not wanted:
        return ()
    return tuple(
        path
        for path, value in payload_strings(payload)
        if any(secret in value for secret in wanted)
    )


@dataclass(frozen=True)
class SessionMatch:
    document_id: str
    session_id: str
    trajectory_id: str | None
    embedded_path: str | None
    trial_id: str
    trial_name: str
    agent_name: str | None
    job_id: str
    job_name: str
    evidence_path: str
    experiment_id: str | None

    @property
    def is_root_document(self) -> bool:
        return self.embedded_path is None


@dataclass(frozen=True)
class SessionResolution:
    """The research-graph rows a span's `session.id` reaches.

    Deliberately a set, not a row: `trajectory_documents.session_id` is not
    unique and cannot be made unique (see `sql/schema.sql`). Ambiguity is
    refused here rather than hidden by picking the first row.
    """

    session_id: str
    matches: tuple[SessionMatch, ...]

    @property
    def trial_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(match.trial_id for match in self.matches))

    @property
    def trial(self) -> SessionMatch:
        """The one trial this session belongs to, or `TraceError`.

        Several documents for one trial (a multi-agent trajectory) is a normal
        answer; several *trials* is a fan-out and never has a right answer.
        """
        if not self.matches:
            raise TraceError(
                f"no catalog trajectory document has session.id {self.session_id!r}; "
                "Phoenix is derived — reindex from the Harbor evidence"
            )
        trials = self.trial_ids
        if len(trials) > 1:
            raise TraceError(
                f"session.id {self.session_id!r} fans out to {len(trials)} trials "
                f"({', '.join(trials)}); the trace cannot identify a trial"
            )
        for match in self.matches:
            if match.is_root_document:
                return match
        return self.matches[0]


def session_lookup_sql(placeholder: str = "%s") -> str:
    """The span -> research graph join.

    `placeholder` is the parameter marker of the driver in use (`%s` for
    psycopg, `?` for DuckDB and sqlite3), so the same join text can be run
    against the catalog and against a fixture.
    """
    return f"""
SELECT
    td.id,
    td.session_id,
    td.trajectory_id,
    td.embedded_path,
    t.id,
    t.trial_name,
    t.agent_name,
    j.id,
    j.job_name,
    j.evidence_path,
    j.experiment_id
FROM trajectory_documents td
JOIN trials t ON t.id = td.trial_id
JOIN jobs j ON j.id = t.job_id
WHERE td.session_id = {placeholder}
ORDER BY td.embedded_path NULLS FIRST, td.id
""".strip()


def _session_match(row: Sequence[Any]) -> SessionMatch:
    if len(row) != 11:
        raise TraceError(f"session lookup returned {len(row)} columns, expected 11")
    return SessionMatch(
        document_id=str(row[0]),
        session_id=str(row[1]),
        trajectory_id=str(row[2]) if row[2] is not None else None,
        embedded_path=str(row[3]) if row[3] is not None else None,
        trial_id=str(row[4]),
        trial_name=str(row[5]),
        agent_name=str(row[6]) if row[6] is not None else None,
        job_id=str(row[7]),
        job_name=str(row[8]),
        evidence_path=str(row[9]),
        experiment_id=str(row[10]) if row[10] is not None else None,
    )


def resolve_session(
    session_id: str | None,
    *,
    fetch: Callable[[str, tuple[Any, ...]], Iterable[Sequence[Any]]],
    placeholder: str = "%s",
) -> SessionResolution:
    """Resolve a span's `session.id` to experiment / job / trial.

    `fetch` runs `(sql, params)` and returns rows; the caller owns the
    connection, which keeps this deterministic and driver-agnostic.
    """
    if not session_id:
        raise TraceError("a session.id is required to resolve a trace to a trial")
    rows = fetch(session_lookup_sql(placeholder), (session_id,))
    return SessionResolution(
        session_id=session_id,
        matches=tuple(_session_match(row) for row in rows),
    )
