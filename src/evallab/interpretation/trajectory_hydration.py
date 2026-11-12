"""Canonical citation handles, secret detection, and pack-bound redaction policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

_DEFAULT_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(
        r"(?:api_key|token|password|secret|authorization)\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})[\"']?",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----"),
)

# AddressSanitizer and UBSan print ``DEDUP_TOKEN: frame--frame--frame`` to
# deduplicate crash reports. The generic ``token:`` rule above matches inside it,
# so every trajectory that shows a sanitizer report would read as secret-bearing.
# Only that exact shape is exempt: the whole uppercase word ``DEDUP_TOKEN``
# followed by a colon. The word may also follow a literal ``\n``/``\r``/``\t``
# escape, as when a report is printed inside a JSON or repr string.
# ``DEDUP_TOKEN=``, ``MY_DEDUP_TOKEN:`` and any other match in the same text
# still count.
_SANITIZER_DEDUP_TOKEN_RE = re.compile(r"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))DEDUP_TOKEN\s*:")


def secret_pattern_hits(text: str) -> int:
    """Number of secret patterns with at least one match in ``text``, sanitizer dedup lines aside."""
    exempt = {match.start() + len("DEDUP_") for match in _SANITIZER_DEDUP_TOKEN_RE.finditer(text)}
    return sum(
        1
        for pattern in _DEFAULT_SECRET_PATTERNS
        if any(match.start() not in exempt for match in pattern.finditer(text))
    )


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
    target_type: str = (
        "step"  # "step" | "tool_call" | "observation" | "stderr" | "stdout" | "arguments" | "file"
    )
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

    @staticmethod
    def _digest_config(value: dict[str, Any]) -> str:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    def compute_digest(self) -> str:
        """Digest every setting that can change redacted output."""
        return self._digest_config(self.to_config())

    def _legacy_digest(self) -> str:
        """Digest used by pre-config packs, which were always default-policy artifacts."""
        return self._digest_config(
            {
                "redact_secrets": self.redact_secrets,
                "max_display_bytes": self.max_display_bytes,
                "secret_patterns": sorted(pattern.pattern for pattern in self.secret_patterns),
            }
        )

    def to_config(self) -> dict[str, Any]:
        """Serialize the exact policy needed to reproduce redaction."""
        return {
            "redact_secrets": self.redact_secrets,
            "max_display_bytes": self.max_display_bytes,
            "secret_patterns": [
                {"pattern": pattern.pattern, "flags": pattern.flags}
                for pattern in self.secret_patterns
            ],
        }

    @classmethod
    def from_config(cls, value: Any) -> RedactionPolicy:
        """Reconstruct a policy from a pack-bound configuration."""
        expected_keys = {"redact_secrets", "max_display_bytes", "secret_patterns"}
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ValueError("invalid redaction policy configuration")
        redact_secrets = value["redact_secrets"]
        max_display_bytes = value["max_display_bytes"]
        raw_patterns = value["secret_patterns"]
        if not isinstance(redact_secrets, bool):
            raise ValueError("invalid redact_secrets setting")
        if max_display_bytes is not None and (
            not isinstance(max_display_bytes, int)
            or isinstance(max_display_bytes, bool)
            or max_display_bytes < 0
        ):
            raise ValueError("invalid max_display_bytes setting")
        if not isinstance(raw_patterns, list):
            raise ValueError("invalid secret_patterns setting")
        patterns: list[re.Pattern[str]] = []
        for raw_pattern in raw_patterns:
            if not isinstance(raw_pattern, dict) or set(raw_pattern) != {"pattern", "flags"}:
                raise ValueError("invalid secret pattern configuration")
            pattern = raw_pattern["pattern"]
            flags = raw_pattern["flags"]
            if (
                not isinstance(pattern, str)
                or not isinstance(flags, int)
                or isinstance(flags, bool)
            ):
                raise ValueError("invalid secret pattern configuration")
            try:
                patterns.append(re.compile(pattern, flags))
            except (OverflowError, re.error) as exc:
                raise ValueError("invalid secret pattern configuration") from exc
        return cls(
            redact_secrets=redact_secrets,
            max_display_bytes=max_display_bytes,
            secret_patterns=tuple(patterns),
        )

    @classmethod
    def from_pack_config(cls, value: Any, digest: Any) -> RedactionPolicy:
        """Reconstruct the exact policy bound to a current or legacy pack."""
        if not isinstance(digest, str):
            raise ValueError("invalid redaction policy digest")
        if value is None:
            policy = cls()
            if policy._legacy_digest() != digest:
                raise ValueError("legacy pack redaction policy is not reproducible")
            return policy
        policy = cls.from_config(value)
        if policy.compute_digest() != digest:
            raise ValueError("redaction policy configuration digest mismatch")
        return policy
