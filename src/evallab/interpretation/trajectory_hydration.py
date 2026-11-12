"""Canonical citation handles, secret detection, and pack-bound redaction policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
