"""Pure export secret gate, without optional Docent integration imports.

The patterns and dict-value/list traversal reuse
research/explorations/trace-lab/docent/export_harbor.py (2026-10-27). Provider
prefixes additionally require a token boundary so canonical task-<hash> IDs
cannot be mistaken for sk-prefixed credentials. Assignment/private-key rules
are unchanged. JSON string wrappers are inspected as decoded values too, while
preserving every raw-string hit. Results contain only generic, safe markers.
"""

from __future__ import annotations

import json
import re

SECRET_RE = re.compile(
    r"((?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_\-]{16,}|dk_[A-Za-z0-9]{10,}"
    r"|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|gh[pous]_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|[A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD)[A-Z0-9_]*\s*=\s*[^\s\"'$]{8,}"
    # TOKEN only as a full underscore-delimited segment: TOKENIZER_INFINITY
    # (a public lm_eval constant) must not trip the scan.
    r"|(?:[A-Z0-9_]*_)?TOKENS?(?:_[A-Z0-9_]+)?\s*=\s*[^\s\"'$]{8,})"
)


class SecretScanRejected(ValueError):
    """A known trace failed a local scan before any payload was transmitted."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        super().__init__("Secret scan rejected the encoded trace payload")


def secret_hits(payload: object) -> list[str]:
    """Return safe match markers from raw values and nested JSON string wrappers."""
    hits: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            before = len(hits)
            hits.extend("secret-pattern" for _ in SECRET_RE.finditer(value))
            if len(hits) != before:
                return
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            if isinstance(decoded, (dict, list, str)) and decoded != value:
                walk(decoded)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return hits
