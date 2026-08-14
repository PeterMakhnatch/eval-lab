"""ATIF → OpenTelemetry conversion and Phoenix shipping.

Uses `harbor-atif2otel` the same way RECON's harbor-021 demo did: validate,
convert, then (optionally) POST OTLP protobuf to Phoenix `/v1/traces`.
Missing or invalid trajectories raise `TraceError` with a one-line message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTROL_AGENTS = frozenset({"oracle", "nop"})
DEFAULT_ENDPOINT = "http://127.0.0.1:6006"
DEFAULT_SERVICE_NAME = "harbor-lab"
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
            f"no ATIF trajectory at {path} "
            "(oracle/nop controls write agent/oracle.txt instead)"
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
    convert_trajectory, resource_spans_to_otlp_json, validate_trajectory = (
        _require_atif2otel()
    )
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
            f"converted {path} but no root AGENT span was present "
            f"(kinds={summary.span_kinds})"
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
