"""Canonical digests and source labels for retained read-only review packets."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from evallab.failure_library.records import RunRecord

HACK_LABELS = frozenset({"reward_hacked", "hacked", "exploit-pass", "reward_hacking"})
CLEAN_LABELS = frozenset({"clean", "actually_solved", "not_hacked"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def run_label(run: RunRecord) -> str | None:
    """Reward or a judge suspicion alone is never a hacked/clean label."""
    labels = {label.label.casefold() for label in run.labels if label.kind in {"executed", "human"}}
    positive, negative = bool(labels & HACK_LABELS), bool(labels & CLEAN_LABELS)
    return "hacked" if positive and not negative else "clean" if negative and not positive else None


