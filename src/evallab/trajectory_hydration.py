"""CAS/raw-ATIF-backed redacted hydration API for cited trajectory content.

Key invariants:
- Raw evidence files and CAS archives remain strictly immutable.
- Canonical CitationHandle binds source document digest, CAS URI, typed locator, and content digest.
- Redaction (secret masking, truncation) occurs only on read / presentation.
- Redaction policy computes a deterministic digest; changing policy mints a new pack digest.
- Strict path jailing: rejects absolute paths and traversal escaping the trial directory.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.evidence_store import restore_evidence
from evallab.traj import TrajectoryOutline

_DEFAULT_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"(?:api_key|token|password|secret|authorization)\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})[\"']?", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----"),
)


class CitationPathJailError(ValueError):
    """Raised when a citation source_path is absolute or escapes the trial jail directory."""


def _sha256_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()}"

@dataclass(frozen=True)
class CitationHandle:
    """Canonical citation handle pointing to an exact element in raw/CAS trajectory evidence."""

    citation_id: str = ""
    trial_id: str | None = None
    source_document_id: str = "main"
    source_path: str = "agent/trajectory.json"
    source_sha256: str = ""
    raw_cas_uri: str | None = None
    cas_uri: str | None = None
    ir_event_id: str | None = None
    step_id: int | str | None = None
    step_index: int | str | None = None
    tool_call_id: str | None = None
    call_index: int | None = None
    source_call_id: str | None = None
    observation_index: int | None = None
    target_type: str = "step"  # "step" | "tool_call" | "observation" | "stderr" | "stdout" | "arguments" | "file"
    content_sha256: str | None = None
    redaction_profile_digest: str | None = None
    availability: str = "available"

    def __post_init__(self) -> None:
        if self.step_id is None and self.step_index is not None:
            object.__setattr__(self, "step_id", self.step_index)
        elif self.step_index is None and self.step_id is not None:
            object.__setattr__(self, "step_index", self.step_id)
        if self.raw_cas_uri is None and self.cas_uri is not None:
            object.__setattr__(self, "raw_cas_uri", self.cas_uri)
        elif self.cas_uri is None and self.raw_cas_uri is not None:
            object.__setattr__(self, "cas_uri", self.raw_cas_uri)

    def format_citation(self) -> str:
        parts = [f"{self.source_path}"]
        coords: list[str] = []
        if self.step_id is not None:
            coords.append(f"step={self.step_id}")
        if self.tool_call_id is not None:
            coords.append(f"call={self.tool_call_id}")
        elif self.source_call_id is not None:
            coords.append(f"call={self.source_call_id}")
        if self.observation_index is not None:
            coords.append(f"obs={self.observation_index}")
        if coords:
            parts.append(f"#{':'.join(coords)}")
        if self.content_sha256:
            parts.append(f" ({self.content_sha256[:16]}...)")
        elif self.source_sha256:
            parts.append(f" (file {self.source_sha256[:16]}...)")
        return "".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Backward compatibility alias
CitationTarget = CitationHandle


def create_citation_handle(
    *,
    trial_id: str | None = None,
    source_path: str,
    source_sha256: str = "",
    raw_cas_uri: str | None = None,
    cas_uri: str | None = None,
    ir_event_id: str | None = None,
    step_id: int | str | None = None,
    step_index: int | str | None = None,
    tool_call_id: str | None = None,
    call_index: int | None = None,
    source_call_id: str | None = None,
    observation_index: int | None = None,
    target_type: str = "step",
    content_sha256: str | None = None,
    redaction_profile_digest: str | None = None,
) -> CitationHandle:
    """Deterministic factory for canonical CitationHandle with content hashing."""
    actual_cas = raw_cas_uri or cas_uri
    effective_step = step_id if step_id is not None else step_index
    loc_parts = [
        str(trial_id or ""),
        str(source_path),
        str(source_sha256),
        str(actual_cas or ""),
        str(effective_step if effective_step is not None else ""),
        str(tool_call_id or ""),
        str(call_index if call_index is not None else ""),
        str(source_call_id or ""),
        str(observation_index if observation_index is not None else ""),
        str(target_type),
    ]
    loc_str = ":".join(loc_parts)
    cit_id = f"cit_{hashlib.sha256(loc_str.encode('utf-8')).hexdigest()[:16]}"

    return CitationHandle(
        citation_id=cit_id,
        trial_id=trial_id,
        source_document_id="main",
        source_path=source_path,
        source_sha256=source_sha256,
        raw_cas_uri=actual_cas,
        cas_uri=actual_cas,
        ir_event_id=ir_event_id,
        step_id=effective_step,
        step_index=effective_step,
        tool_call_id=tool_call_id,
        call_index=call_index,
        source_call_id=source_call_id,
        observation_index=observation_index,
        target_type=target_type,
        content_sha256=content_sha256,
        redaction_profile_digest=redaction_profile_digest,
        availability="available",
    )


@dataclass(frozen=True)
class RedactionPolicy:
    """Policy for on-read redaction and presentation formatting."""

    redact_secrets: bool = True
    max_display_bytes: int | None = None
    secret_patterns: tuple[re.Pattern[str], ...] = _DEFAULT_SECRET_PATTERNS

    def compute_digest(self) -> str:
        """Deterministic digest of the redaction policy configuration."""
        raw_patterns = [p.pattern for p in self.secret_patterns]
        cfg = {
            "redact_secrets": self.redact_secrets,
            "max_display_bytes": self.max_display_bytes,
            "secret_patterns": sorted(raw_patterns),
        }
        serialized = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class HydratedEvidence:
    """Hydrated content for a cited trajectory element with exact provenance."""

    citation: CitationHandle
    raw_content: str
    redacted_content: str
    content_bytes: int
    content_sha256: str
    is_redacted: bool
    redaction_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": self.citation.to_dict(),
            "raw_content": self.raw_content,
            "redacted_content": self.redacted_content,
            "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256,
            "is_redacted": self.is_redacted,
            "redaction_metadata": self.redaction_metadata,
        }


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
    citation: CitationHandle,
) -> str | None:
    """Extract cited string content from a raw ATIF/Harbor payload."""
    target_type = citation.target_type

    if target_type == "file":
        return json.dumps(payload, indent=2)

    # Navigate steps
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return None

    target_step: dict[str, Any] | None = None
    step_id_target = citation.step_id

    if step_id_target is not None:
        for s in steps:
            if isinstance(s, dict) and (
                s.get("step_id") == step_id_target
                or str(s.get("step_id")) == str(step_id_target)
            ):
                target_step = s
                break

    if target_step is None:
        return None

    if target_type == "step":
        return json.dumps(target_step, indent=2)

    # Handle tool calls
    tool_calls = target_step.get("tool_calls") or []
    observations = target_step.get("observations") or []
    if target_type in ("tool_call", "arguments"):
        target_tc: dict[str, Any] | None = None
        if citation.tool_call_id:
            for tc in tool_calls:
                if isinstance(tc, dict) and (tc.get("tool_call_id") == citation.tool_call_id or tc.get("id") == citation.tool_call_id):
                    target_tc = tc
                    break
        elif citation.call_index is not None and 0 <= citation.call_index < len(tool_calls) and isinstance(tool_calls[citation.call_index], dict):
            target_tc = tool_calls[citation.call_index]

        if target_tc:
            if target_type == "arguments":
                args = target_tc.get("arguments") or target_tc.get("parameters") or target_tc.get("input")
                return json.dumps(args, indent=2) if not isinstance(args, str) else args

            # Match sibling observation without fake positional fallback
            tc_id = target_tc.get("tool_call_id") or target_tc.get("id")
            matching_obs: dict[str, Any] | None = None
            if tc_id is not None:
                for obs in observations:
                    if isinstance(obs, dict) and (obs.get("source_call_id") == tc_id or obs.get("tool_call_id") == tc_id):
                        matching_obs = obs
                        break

            payload_out = {"tool_call": target_tc}
            if matching_obs is not None:
                payload_out["observation"] = matching_obs
            return json.dumps(payload_out, indent=2)
        return None

    # Handle observations
    if target_type in ("observation", "stderr", "stdout"):
        target_obs: dict[str, Any] | None = None
        if citation.source_call_id is not None:
            for obs in observations:
                if isinstance(obs, dict) and obs.get("source_call_id") == citation.source_call_id:
                    target_obs = obs
                    break
        elif citation.observation_index is not None and 0 <= citation.observation_index < len(observations) and isinstance(observations[citation.observation_index], dict):
            target_obs = observations[citation.observation_index]

        if target_obs:
            content = target_obs.get("content") or target_obs.get("output") or target_obs.get("result")
            if target_type == "stderr":
                extra = target_obs.get("extra")
                if isinstance(extra, dict) and extra.get("stderr"):
                    return str(extra["stderr"])
                if isinstance(content, dict) and "stderr" in content:
                    return str(content["stderr"])
            elif target_type == "stdout":
                extra = target_obs.get("extra")
                if isinstance(extra, dict) and extra.get("stdout"):
                    return str(extra["stdout"])
                if isinstance(content, dict) and "stdout" in content:
                    return str(content["stdout"])

            if isinstance(content, str):
                return content
            elif content is not None:
                return json.dumps(content, indent=2)
        return None

    return json.dumps(target_step, indent=2)


def hydrate_citation(
    citation: CitationHandle,
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

    # 2. If raw_cas_uri / cas_uri is present, restore the archive to temporary root and extract cited member
    cas_target = citation.raw_cas_uri or citation.cas_uri
    temp_cas_dir: tempfile.TemporaryDirectory[str] | None = None
    if cas_target:
        if repo_root is not None and (repo_root / "blobs").exists():
            store_root = repo_root
        else:
            store_root = (repo_root / "derived" / "evidence-cas") if repo_root else Path("derived/evidence-cas")
            if not store_root.exists() and repo_root:
                alt_cas = repo_root / "evidence" / "cas"
                if alt_cas.exists():
                    store_root = alt_cas
        try:
            temp_cas_dir = tempfile.TemporaryDirectory()
            extracted_path = Path(temp_cas_dir.name)
            restore_evidence(store_root, cas_target, extracted_path)
            if citation.source_path:
                raw_src = citation.source_path.strip()
                if Path(raw_src).is_absolute() or raw_src.startswith("/") or raw_src.startswith("\\"):
                    raise CitationPathJailError(
                        f"Citation source_path must be relative, got absolute path: {citation.source_path!r}"
                    )
                extracted_resolved = extracted_path.resolve()
                cand_member_resolved = (extracted_path / raw_src).resolve()
                if not (cand_member_resolved == extracted_resolved or cand_member_resolved.is_relative_to(extracted_resolved)):
                    raise CitationPathJailError(
                        f"Citation source_path {citation.source_path!r} escapes CAS archive root {extracted_path}"
                    )
                cand_member = cand_member_resolved
            else:
                cand_member = extracted_path / "agent" / "trajectory.json"

            if not cand_member.is_file():
                alt_traj = extracted_path / "agent" / "trajectory.json"
                if alt_traj.is_file():
                    cand_member = alt_traj
                else:
                    alt_traj2 = extracted_path / "trajectory.json"
                    if alt_traj2.is_file():
                        cand_member = alt_traj2
            if cand_member.is_file():
                payload = _load_raw_json(cand_member)
                if payload is not None:
                    raw_text = _extract_content_from_payload(payload, citation)
                if raw_text is None:
                    raw_text = cand_member.read_text(encoding="utf-8", errors="replace")
            else:
                limitation_metadata["limitation_reason"] = "cas_member_not_found"
                limitation_metadata["source_path"] = citation.source_path
                raw_text = f"[EvidenceLimitation: cas_member_not_found path={citation.source_path} uri={cas_target}]"
        except CitationPathJailError:
            raise
        except FileNotFoundError as fnf:
            limitation_metadata["limitation_reason"] = "cas_archive_not_found"
            limitation_metadata["error_detail"] = str(fnf)
            raw_text = f"[EvidenceLimitation: cas_archive_not_found uri={cas_target}]"
        except Exception as exc:
            limitation_metadata["limitation_reason"] = "cas_load_error"
            limitation_metadata["error_detail"] = f"{type(exc).__name__}: {exc}"
            raw_text = f"[EvidenceLimitation: cas_load_error {type(exc).__name__}: {exc}]"
        finally:
            if temp_cas_dir is not None:
                with contextlib.suppress(Exception):
                    temp_cas_dir.cleanup()
    # 3. Resolve from trial directory if not loaded from CAS
    if raw_text is None and trial_dir is not None:
        candidate_file = trial_dir / citation.source_path
        if not candidate_file.is_file():
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

        citation = create_citation_handle(
            source_path=outline.source_path,
            source_sha256=outline.source_sha256,
            step_id=step.step_id,
            target_type="observation",
        )

        raw_obs_text = ""
        matched_raw_step: dict[str, Any] | None = None
        for s in steps_payload:
            if isinstance(s, dict) and (s.get("step_id") == step.step_id or str(s.get("step_id")) == str(step.step_id)):
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
