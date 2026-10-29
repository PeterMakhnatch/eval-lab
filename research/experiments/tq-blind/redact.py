"""Identity redaction for TQ-BLIND (pure, deterministic; pinned by SHA-256 in the preregistration).

The TQ-UNVERIFIED adjudicator prompts showed the AutomationBench package as the gate displayed it, so the
benchmark name, its source URL and its copyright holder reached the adjudicators. This module replaces exactly
those identity strings with neutral placeholders and changes nothing else; ``verify`` proves it.

Token rules (case-insensitive match, applied in one left-to-right pass, first alternative wins):
  url        ``https://github.com/zapier/AutomationBench``  ->  ``https://example.invalid/org/TaskPkg``
  holder     ``Zapier, Inc.``                               ->  ``Example Org, Inc.``
  name       ``automationbench`` in any position (import path, directory, env-var prefix, prose), cased as
             ``automationbench`` -> ``taskpkg``, ``AutomationBench`` -> ``TaskPkg``, ``AUTOMATIONBENCH`` ->
             ``TASKPKG``; any other casing raises.

Deliberately not redacted: ``zapier_tools`` and other task-domain vocabulary (integration names, the rubric and
world-schema structure). They are task content, not the benchmark's name; the residual recognizability is a
stated limit of the experiment.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

URL = "https://github.com/zapier/AutomationBench"
URL_PLACEHOLDER = "https://example.invalid/org/TaskPkg"
HOLDER = "Zapier, Inc."
HOLDER_PLACEHOLDER = "Example Org, Inc."
NAME_PLACEHOLDERS = {
    "automationbench": "taskpkg",
    "AutomationBench": "TaskPkg",
    "AUTOMATIONBENCH": "TASKPKG",
}

TOKEN = re.compile(
    "|".join(
        (
            f"(?P<url>{re.escape(URL)})",
            f"(?P<holder>{re.escape(HOLDER)})",
            "(?P<name>automationbench)",
        )
    ),
    re.IGNORECASE,
)
PLACEHOLDER = re.compile(
    "|".join(
        (
            f"(?P<url>{re.escape(URL_PLACEHOLDER)})",
            f"(?P<holder>{re.escape(HOLDER_PLACEHOLDER)})",
            "(?P<name>" + "|".join(map(re.escape, NAME_PLACEHOLDERS.values())) + ")",
        )
    )
)


def _replace(match: re.Match[str]) -> str:
    kind = match.lastgroup
    if kind == "url":
        return URL_PLACEHOLDER
    if kind == "holder":
        return HOLDER_PLACEHOLDER
    try:
        return NAME_PLACEHOLDERS[match.group(0)]
    except KeyError:
        raise ValueError(f"unexpected casing of the benchmark name: {match.group(0)!r}") from None


def redact(text: str) -> tuple[str, Counter[str]]:
    """Return the redacted text and the number of replacements per rule."""
    if PLACEHOLDER.search(text):
        raise ValueError(
            "input already contains a placeholder string; redaction would be ambiguous"
        )
    counts: Counter[str] = Counter(m.lastgroup for m in TOKEN.finditer(text))
    return TOKEN.sub(_replace, text), counts


def verify(original: str, redacted: str) -> Mapping[str, object]:
    """Prove only the identity tokens changed: equal non-token segments, equal token counts, no residue."""
    kept_original = TOKEN.split(original)[0 :: TOKEN.groups + 1]
    kept_redacted = PLACEHOLDER.split(redacted)[0 :: PLACEHOLDER.groups + 1]
    token_counts = Counter(m.lastgroup for m in TOKEN.finditer(original))
    placeholder_counts = Counter(m.lastgroup for m in PLACEHOLDER.finditer(redacted))
    return {
        "tokens": sum(token_counts.values()),
        "per_rule": dict(token_counts),
        "segments_identical": kept_original == kept_redacted,
        "placeholder_counts_equal": token_counts == placeholder_counts,
        "residual_tokens": len(TOKEN.findall(redacted)),
        "residual_zapier_words": len(re.findall(r"zapier", redacted, re.IGNORECASE)),
        "bytes_original": len(original.encode()),
        "bytes_redacted": len(redacted.encode()),
    }
