"""CAS/raw-ATIF-backed redacted hydration API for cited trajectory content.

Key invariants:
- Raw evidence files and CAS archives remain strictly immutable.
- Redaction (secret masking, truncation) occurs only on read / presentation.
- Every piece of hydrated evidence retains exact source provenance:
  digest (sha256), source_path, document_id, step_index, call_index, observation_index.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.evidence_store import load_archive
from evallab.traj import TrajectoryOutline

_DEFAULT_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"(?:api_key|token|password|secret|authorization)\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})[\"']?", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----"),
)

_REDACTED_MARKER_PATTERN = re.compile(
    r"<<evallab-redacted: (?P<bytes>\d+) bytes, (?P<digest>sha256:[0-9a-f]{64})>>"
)


class CitationPathJailError(ValueError):
    """Raised when a citation source_path is absolute or escapes the trial jail directory."""


@dataclass(frozen=True)
class CitationTarget:
    """Provenance citation to a specific element within trajectory evidence."""

    trial_id: str
    source_path: str
    source_sha256: str = ""
    document_id: str = "main"
    step_index: int | None = None
    call_index: int | None = None
    observation_index: int | None = None
    target_type: str = "step"  # "step" | "tool_call" | "observation" | "stderr" | "stdout" | "arguments" | "file"
    content_sha256: str | None = None
    cas_uri: str | None = None

    def format_citation(self) -> str:
        parts = [f"{self.source_path}"]
        coords: list[str] = []
        if self.step_index is not None:
            coords.append(f"step={self.step_index}")
        if self.call_index is not None:
            coords.append(f"call={self.call_index}")
        if self.observation_index is not None:
            coords.append(f"obs={self.observation_index}")
        if coords:
            parts.append(f"#{':'.join(coords)}")
        if self.content_sha256:
            parts.append(f" ({self.content_sha256})")
        elif self.source_sha256:
            parts.append(f" (file {self.source_sha256[:16]}...)")
        return "".join(parts)


@dataclass(frozen=True)
class RedactionPolicy:
    """Policy for on-read redaction and presentation formatting."""

    redact_secrets: bool = True
    max_display_bytes: int | None = None
    secret_patterns: tuple[re.Pattern[str], ...] = _DEFAULT_SECRET_PATTERNS


@dataclass(frozen=True)
class HydratedEvidence:
    """Hydrated content for a cited trajectory element with exact provenance."""

    citation: CitationTarget
    raw_content: str
    redacted_content: str
    content_bytes: int
    content_sha256: str
    is_redacted: bool
    redaction_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": asdict(self.citation),
            "raw_content": self.raw_content,
            "redacted_content": self.redacted_content,
            "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256,
            "is_redacted": self.is_redacted,
            "redaction_metadata": self.redaction_metadata,
        }


def _sha256_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()}"


def apply_redaction(
    text: str,
    policy: RedactionPolicy | None = None,
) -> tuple[str, bool, dict[str, Any]]:
    """Apply on-read redaction without mutating any source files.
    
    Returns (redacted_text, is_redacted, metadata).
    """
    if policy is None:
        policy = RedactionPolicy()

    is_redacted = False
    metadata: dict[str, Any] = {"secrets_masked": 0, "truncated_bytes": 0}
    redacted = text

    # 1. Mask secrets if policy enables it
    if policy.redact_secrets:
        for pattern in policy.secret_patterns:
            matches = list(pattern.finditer(redacted))
            if matches:
                is_redacted = True
                metadata["secrets_masked"] += len(matches)

                def _mask_match(m: re.Match[str]) -> str:
                    matched_str = m.group(0)
                    raw_len = len(matched_str.encode("utf-8"))
                    digest = _sha256_text(matched_str)
                    return f"<<evallab-redacted: {raw_len} bytes, {digest}>>"

                redacted = pattern.sub(_mask_match, redacted)

    # 2. Display truncation if max_display_bytes is exceeded
    if policy.max_display_bytes is not None and len(redacted.encode("utf-8")) > policy.max_display_bytes:
        raw_bytes = redacted.encode("utf-8")
        kept = raw_bytes[: policy.max_display_bytes].decode("utf-8", errors="ignore")
        truncated_len = len(raw_bytes) - policy.max_display_bytes
        digest = _sha256_text(text)
        redacted = f"{kept}\n<<evallab-truncated: {truncated_len} bytes omitted, full {digest}>>"
        is_redacted = True
        metadata["truncated_bytes"] = truncated_len

    return redacted, is_redacted, metadata


def _load_raw_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_content_from_payload(
    payload: dict[str, Any],
    citation: CitationTarget,
) -> str | None:
    """Extract cited string content from a raw ATIF/Harbor payload."""
    target_type = citation.target_type

    if target_type == "file":
        return json.dumps(payload, indent=2)

    # Navigate steps
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return None

    step_idx = citation.step_index
    target_step: dict[str, Any] | None = None

    if step_idx is not None:
        if 0 <= step_idx < len(steps) and isinstance(steps[step_idx], dict):
            target_step = steps[step_idx]
        else:
            # Try 1-based index or step_id match
            for s in steps:
                if isinstance(s, dict) and (s.get("step_id") == step_idx or s.get("step_id") == str(step_idx)):
                    target_step = s
                    break

    if target_step is None and steps and step_idx is None:
        target_step = steps[0] if isinstance(steps[0], dict) else None

    if target_step is None:
        return None

    if target_type == "step":
        return json.dumps(target_step, indent=2)

    # Handle tool calls
    tool_calls = target_step.get("tool_calls") or []
    if target_type in ("tool_call", "arguments"):
        call_idx = citation.call_index or 0
        if 0 <= call_idx < len(tool_calls) and isinstance(tool_calls[call_idx], dict):
            tc = tool_calls[call_idx]
            if target_type == "arguments":
                args = tc.get("arguments") or tc.get("parameters") or tc.get("input")
                return json.dumps(args, indent=2) if not isinstance(args, str) else args
            return json.dumps(tc, indent=2)
        return None

    # Handle observations
    observations = target_step.get("observations") or []
    if target_type in ("observation", "stderr", "stdout"):
        obs_idx = citation.observation_index or 0
        if 0 <= obs_idx < len(observations) and isinstance(observations[obs_idx], dict):
            obs = observations[obs_idx]
            content = obs.get("content") or obs.get("output") or obs.get("result")
            if target_type == "stderr":
                extra = obs.get("extra")
                if isinstance(extra, dict) and extra.get("stderr"):
                    return str(extra["stderr"])
                # Check if content has stderr structure
                if isinstance(content, dict) and "stderr" in content:
                    return str(content["stderr"])
            elif target_type == "stdout":
                extra = obs.get("extra")
                if isinstance(extra, dict) and extra.get("stdout"):
                    return str(extra["stdout"])
                if isinstance(content, dict) and "stdout" in content:
                    return str(content["stdout"])
            
            if isinstance(content, str):
                return content
            elif content is not None:
                return json.dumps(content, indent=2)
        return None

    # Fallback to entire step
    return json.dumps(target_step, indent=2)


def hydrate_citation(
    citation: CitationTarget,
    *,
    trial_dir: Path | None = None,
    repo_root: Path | None = None,
    policy: RedactionPolicy | None = None,
) -> HydratedEvidence:
    """Hydrate content for a cited element from raw trajectory evidence or CAS store."""
    if policy is None:
        policy = RedactionPolicy()

    # 1. Path jailing enforcement: reject absolute paths and traversal escaping trial_dir
    if citation.source_path:
        raw_src = citation.source_path.strip()
        if Path(raw_src).is_absolute() or raw_src.startswith("/") or raw_src.startswith("\\"):
            raise CitationPathJailError(
                f"Citation source_path must be relative, got absolute path: {citation.source_path!r}"
            )
        if trial_dir is not None:
            trial_resolved = trial_dir.resolve()
            candidate_file = (trial_dir / raw_src).resolve()
            if not (candidate_file == trial_resolved or candidate_file.is_relative_to(trial_resolved)):
                raise CitationPathJailError(
                    f"Citation source_path {citation.source_path!r} escapes trial directory {trial_dir}"
                )

    raw_text: str | None = None
    limitation_metadata: dict[str, Any] = {}

    # 2. If cas_uri is present, resolve from CAS store and verify digest
    if citation.cas_uri:
        store_root = (repo_root / "evidence" / "cas") if repo_root else Path("evidence/cas")
        try:
            archive_path = load_archive(store_root, citation.cas_uri)
            if not archive_path.is_file():
                limitation_metadata["limitation_reason"] = "cas_archive_not_found"
                limitation_metadata["cas_uri"] = citation.cas_uri
                raw_text = f"[EvidenceLimitation: cas_archive_not_found uri={citation.cas_uri}]"
            else:
                raw_text = archive_path.read_text(encoding="utf-8", errors="replace")
                expected_digest = citation.cas_uri.removeprefix("cas://sha256/").removeprefix("sha256:")
                actual_digest = _sha256_text(raw_text).removeprefix("sha256:")
                if expected_digest and actual_digest != expected_digest:
                    limitation_metadata["limitation_reason"] = "cas_digest_mismatch"
                    limitation_metadata["expected_digest"] = expected_digest
                    limitation_metadata["actual_digest"] = actual_digest
                    raw_text = f"[EvidenceLimitation: cas_digest_mismatch expected={expected_digest} actual={actual_digest}]"
        except FileNotFoundError as fnf:
            limitation_metadata["limitation_reason"] = "cas_archive_not_found"
            limitation_metadata["error_detail"] = str(fnf)
            raw_text = f"[EvidenceLimitation: cas_archive_not_found uri={citation.cas_uri}]"
        except Exception as exc:
            limitation_metadata["limitation_reason"] = "cas_load_error"
            limitation_metadata["error_detail"] = f"{type(exc).__name__}: {exc}"
            raw_text = f"[EvidenceLimitation: cas_load_error {type(exc).__name__}: {exc}]"

    # 3. Resolve from trial directory if not loaded from CAS
    if raw_text is None and trial_dir is not None:
        candidate_file = trial_dir / citation.source_path
        if not candidate_file.is_file():
            # Try searching agent/ or sessions/
            alt1 = trial_dir / "agent" / "trajectory.json"
            if alt1.is_file():
                candidate_file = alt1

        if candidate_file.is_file():
            payload = _load_raw_json(candidate_file)
            if payload is not None:
                raw_text = _extract_content_from_payload(payload, citation)
            if raw_text is None:
                raw_text = candidate_file.read_text(encoding="utf-8", errors="replace")
        else:
            limitation_metadata["limitation_reason"] = "file_not_found"
            limitation_metadata["source_path"] = citation.source_path

    if raw_text is None:
        raw_text = f"[EvidenceLimitation: evidence_unavailable citation={citation.format_citation()}]"

    content_bytes = len(raw_text.encode("utf-8"))
    content_sha256 = _sha256_text(raw_text)

    # Verify content digest if specified in citation
    if citation.content_sha256 and content_sha256 != citation.content_sha256:
        limitation_metadata["content_digest_mismatch"] = True
        limitation_metadata["expected_content_sha256"] = citation.content_sha256
        limitation_metadata["actual_content_sha256"] = content_sha256

    # Apply on-read redactions
    redacted_text, is_redacted, meta = apply_redaction(raw_text, policy)
    meta.update(limitation_metadata)

    return HydratedEvidence(
        citation=citation,
        raw_content=raw_text,
        redacted_content=redacted_text,
        content_bytes=content_bytes,
        content_sha256=content_sha256,
        is_redacted=is_redacted,
        redaction_metadata=meta,
    )

def hydrate_error_observations(
    trial_dir: Path,
    outline: TrajectoryOutline,
    *,
    policy: RedactionPolicy | None = None,
) -> list[HydratedEvidence]:
    """Hydrate untruncated error observations and stderr for all failing steps in a trial."""
    if policy is None:
        policy = RedactionPolicy()

    error_evidences: list[HydratedEvidence] = []
    
    # Load primary trajectory JSON
    traj_path = trial_dir / outline.source_path
    if not traj_path.is_file():
        traj_path = trial_dir / "agent" / "trajectory.json"

    payload = _load_raw_json(traj_path) if traj_path.is_file() else None
    raw_steps_obj = payload.get("steps") if payload else None
    steps_payload: list[Any] = raw_steps_obj if isinstance(raw_steps_obj, list) else []
    for step in outline.steps:
        is_failing = (step.exit_code is not None and step.exit_code != 0) or step.is_error
        if not is_failing:
            continue

        citation = CitationTarget(
            trial_id=outline.trial_id,
            source_path=outline.source_path,
            source_sha256=outline.source_sha256,
            step_index=step.step_id,
            target_type="observation",
        )

        raw_obs_text = ""
        # Find raw step in payload
        matched_raw_step: dict[str, Any] | None = None
        for s in steps_payload:
            if isinstance(s, dict) and (s.get("step_id") == step.step_id or s.get("step_id") == str(step.step_id)):
                matched_raw_step = s
                break

        if matched_raw_step:
            observations = matched_raw_step.get("observations") or []
            if observations:
                first_obs = observations[0]
                if isinstance(first_obs, dict):
                    content = first_obs.get("content") or first_obs.get("output") or first_obs.get("result")
                    extra = first_obs.get("extra")
                    if isinstance(extra, dict) and extra.get("stderr"):
                        raw_obs_text = str(extra["stderr"])
                    elif isinstance(content, str):
                        raw_obs_text = content
                    elif content is not None:
                        raw_obs_text = json.dumps(content, indent=2)

        if not raw_obs_text and step.error_message:
            raw_obs_text = step.error_message
        elif not raw_obs_text and step.thought_snippet:
            raw_obs_text = step.thought_snippet

        if not raw_obs_text:
            raw_obs_text = f"Command failed with exit code {step.exit_code or 'non-zero'}"

        content_bytes = len(raw_obs_text.encode("utf-8"))
        content_sha256 = _sha256_text(raw_obs_text)
        redacted_text, is_redacted, meta = apply_redaction(raw_obs_text, policy)

        error_evidences.append(
            HydratedEvidence(
                citation=citation,
                raw_content=raw_obs_text,
                redacted_content=redacted_text,
                content_bytes=content_bytes,
                content_sha256=content_sha256,
                is_redacted=is_redacted,
                redaction_metadata=meta,
            )
        )

    return error_evidences
