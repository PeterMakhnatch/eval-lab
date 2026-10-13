"""$0 detectors over every agent pass: Eval Lab rules and the TA code rules.

- ``rules``: ``evallab.detectors.Rules`` (live-watch + RewardKit integrity
  tripwires on the agent's commands and file-edit calls).
- ``ta``: the content rules from ``research/experiments/live-stop-thresholds``
  (#847), applied to what the agent wrote: the added side of
  ``verifier/agent.diff`` per file, plus whole-file write tool calls in the
  ATIF. ``tamper_signature``, ``always_equal`` and ``entrypoint_assertless`` are
  mechanism hits; ``process_exit`` and ``self_test_harness`` are recorded as
  weak signals only (#847 found most such flags benign).

Usage (worktree root):
  uv run python research/experiments/contamination-audit/score_free.py
Reads derived/contam/inventory.jsonl, writes derived/contam/free.jsonl.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
sys.path.insert(0, str(WORKTREE / "research/experiments/live-stop-thresholds"))

import candidate_rules as ta  # noqa: E402

from evallab.detectors import Rules  # noqa: E402

STRONG_TA = ("tamper_signature", "always_equal", "entrypoint_assertless")
WEAK_TA = ("process_exit", "self_test_harness")
_WRITE_TOOLS = {"write", "write_file", "create", "create_file", "str_replace_based_edit_tool"}
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")


def diff_files(diff: str) -> list[tuple[str, str]]:
    """(path, added lines) per file of a unified diff."""
    files: dict[str, list[str]] = {}
    current = None
    for line in diff.split("\n"):
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group(1)
            files.setdefault(current, [])
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current].append(line[1:])
    return [(p, "\n".join(lines)) for p, lines in files.items() if lines]


def tool_writes(steps: list[dict]) -> list[tuple[str, str]]:
    out = []
    for step in steps:
        for call in step.get("tool_calls") or []:
            if str(call.get("function_name") or "").lower() not in _WRITE_TOOLS:
                continue
            args = call.get("arguments") or {}
            if not isinstance(args, dict):
                continue
            path = args.get("file_path") or args.get("path") or args.get("filename")
            body = args.get("content") or args.get("file_text") or args.get("new_str")
            if isinstance(path, str) and isinstance(body, str):
                out.append((path, body))
    return out


def instruction(trial: Path, steps: list[dict]) -> str:
    for name in ("instruction.md", "agent/instruction.md"):
        if (trial / name).is_file():
            return (trial / name).read_text(errors="replace")
    return next(
        (
            s.get("message")
            for s in steps
            if s.get("source") == "user" and isinstance(s.get("message"), str)
        ),
        "",
    )


def main() -> None:
    inv = [
        json.loads(x)
        for x in (WORKTREE / "derived/contam/inventory.jsonl").read_text().split("\n")
        if x
    ]
    dest = WORKTREE / "derived/contam/free.jsonl"
    done = (
        {r["trial"]: r for r in (json.loads(x) for x in dest.read_text().split("\n") if x)}
        if dest.is_file()
        else {}
    )
    rules = Rules()
    out = []
    for row in inv:
        if row["control"] or not row["atif"]:
            continue
        if row["trial"] in done:
            out.append(done[row["trial"]])
            continue
        trial = Path(row["trial_dir"])
        steps = json.loads((trial / "agent/trajectory.json").read_text(errors="replace")).get(
            "steps", []
        )
        try:
            verdict = rules.judge({"id": row["trial"]}, trial)
            rule_hits = (
                []
                if verdict["explanation"] == "fired: none"
                else verdict["explanation"][7:].split(", ")
            )
        except Exception as exc:  # a malformed trajectory must not stop the audit
            rule_hits = [f"error:{type(exc).__name__}"]
        diff = trial / "verifier/agent.diff"
        files = diff_files(diff.read_text(errors="replace")) if diff.is_file() else []
        files += tool_writes(steps)
        fired = ta.evaluate(files, instruction(trial, steps)) if files else dict.fromkeys(ta.RULES)
        out.append(
            {
                "trial": row["trial"],
                "rules": rule_hits,
                "ta_strong": [k for k in STRONG_TA if fired.get(k)],
                "ta_weak": [k for k in WEAK_TA if fired.get(k)],
                "ta_files": len(files),
                "has_diff": diff.is_file(),
            }
        )
    dest.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in out))
    print(
        f"{len(out)} agent passes ({len(out) - len([r for r in out if r['trial'] in done])} newly scored): "
        f"rules fired {sum(bool(r['rules']) for r in out)}, "
        f"TA strong {sum(bool(r['ta_strong']) for r in out)}, TA weak {sum(bool(r['ta_weak']) for r in out)}, "
        f"with diff {sum(r['has_diff'] for r in out)}"
    )


if __name__ == "__main__":
    main()
