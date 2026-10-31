"""Hack-drop filter: frozen tripwires on a submission file or directory.

Rules are the B4 prereg (``RULES_VERSION`` 3). This module does not tune them
and does not read labels. ``notes.json`` is skipped because, on the executed
exploit events, it carries ``exploit_found``.

A path is a drop when ``evaluate_content_rules`` sets a stop-eligible key,
``tamper_signature_hit`` matches a scored file, or (only if agent steps are
present) ``scan_trace`` fires a stop-eligible content hit or a hackbook
verdict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from evallab.tripwires import (
    CONTENT_STOP_ELIGIBLE,
    evaluate_content_rules,
    tamper_signature_hit,
)

_SKIP_NAMES = frozenset({"notes.json"})
_TEXT_SUFFIXES = frozenset(
    {".sh", ".py", ".patch", ".diff", ".json", ".txt", ".md", ".toml", ".cfg", ".ini"}
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _bundle_files(path: Path) -> list[tuple[str, str]]:
    """Executable text files under ``path``. ``notes.json`` is never included."""
    if path.is_file():
        if path.name in _SKIP_NAMES:
            return []
        return [(path.name, _read_text(path))]
    if not path.is_dir():
        return []
    found: list[tuple[str, str]] = []
    for child in sorted(path.rglob("*")):
        if not child.is_file() or child.name in _SKIP_NAMES:
            continue
        if child.suffix.lower() not in _TEXT_SUFFIXES and child.suffix != "":
            continue
        rel = child.relative_to(path).as_posix()
        found.append((rel, _read_text(child)))
    return found


def _agent_steps(path: Path) -> list[dict[str, Any]]:
    """Steps only when the located bytes already contain them. Never synthesized."""
    candidates = [path] if path.is_file() else sorted(path.rglob("*.json"))
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() != ".json":
            continue
        if candidate.name in _SKIP_NAMES:
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        steps = data.get("agent_steps") or data.get("steps")
        if isinstance(steps, list) and steps and all(isinstance(item, dict) for item in steps):
            return steps
    return []


def score_path(path: str | Path) -> dict[str, Any]:
    """Frozen-rule report for one file or directory. Does not read labels."""
    target = Path(path)
    files = _bundle_files(target)
    content = evaluate_content_rules(files, prompt="") if files else {}
    stop_hits = [
        {"rule": rule, "evidence": content.get(rule)}
        for rule in CONTENT_STOP_ELIGIBLE
        if content.get(rule)
    ]
    other_content = [
        {"rule": rule, "evidence": evidence}
        for rule, evidence in content.items()
        if evidence and rule not in CONTENT_STOP_ELIGIBLE
    ]
    tamper_hits: list[dict[str, str]] = []
    for name, text in files:
        hit = tamper_signature_hit(text)
        if hit:
            tamper_hits.append({"file": name, "signature": hit})
    steps = _agent_steps(target)
    verdict_hits: list[dict[str, Any]] = []
    trace_stop: list[str] = []
    if steps:
        from evallab.tripwires import scan_trace

        report = scan_trace(steps)
        for rule in CONTENT_STOP_ELIGIBLE:
            if report.get(rule):
                trace_stop.append(rule)
        for hit in report.get("hackbook") or []:
            if isinstance(hit, dict) and hit.get("eligibility") == "verdict":
                verdict_hits.append(
                    {
                        "rule": hit.get("rule"),
                        "mechanism": hit.get("mechanism"),
                    }
                )
    signatures = [item["evidence"] for item in stop_hits if item["rule"] == "tamper_signature"]
    signatures.extend(item["signature"] for item in tamper_hits)
    signatures.extend(item["rule"] for item in stop_hits if item["rule"] != "tamper_signature")
    signatures.extend(f"trace:{rule}" for rule in trace_stop)
    signatures.extend(f"verdict:{item['rule']}" for item in verdict_hits)
    flagged = bool(stop_hits or tamper_hits or trace_stop or verdict_hits)
    scored_bytes = sum(len(text.encode("utf-8")) for _, text in files)
    return {
        "flagged": flagged,
        "n_files": len(files),
        "file_names": [name for name, _ in files],
        "scored_bytes": scored_bytes,
        "steps_present": bool(steps),
        "n_steps": len(steps),
        "stop_hits": stop_hits,
        "tamper_hits": tamper_hits,
        "other_content": other_content,
        "trace_stop": trace_stop,
        "verdict_hits": verdict_hits,
        "signatures": list(dict.fromkeys(signatures)),
    }


def is_hack_drop(path: str | Path) -> bool:
    """True when the frozen rules say this path must not be exported."""
    return bool(score_path(path)["flagged"])


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if not args:
        print("usage: hack_drop.py <file-or-dir> ...", file=sys.stderr)
        return 2
    for arg in args:
        report = score_path(arg)
        print(f"{arg}\t{'DROP' if report['flagged'] else 'KEEP'}\t{','.join(report['signatures'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
