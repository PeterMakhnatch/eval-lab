"""Compare two Reef harness versions' episodes by failure mode.

Reads imported Reef gate corpora (see ``evallab.evidence.reef_intake``) and
labels every failed episode with deterministic failure-mode heuristics that
mirror ``04_gate_aa.py`` ``episode_flags`` over the imported ATIF documents
instead of the native session log. Reports agreement and disagreement with
the exp04 reference counts; it never re-runs Reef, calls models, or applies
gate decision rules.

Run with ``python -m evallab.evidence.reef_shift --seed <dir> --check <dir>
[--answers PATH] [--json-out PATH]``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def _number_pattern(expected: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])")


def _agent_steps(payload: JsonObject) -> list[JsonObject]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict) and step.get("source") == "agent"]


def _step_messages(step: JsonObject) -> str:
    message = step.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(
            part.get("text", "") for part in message if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _step_results(step: JsonObject) -> list[JsonObject]:
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return []
    results = observation.get("results")
    if not isinstance(results, list):
        return []
    return [result for result in results if isinstance(result, dict)]


def episode_flags(payload: JsonObject, answers: dict[str, str]) -> set[str]:
    """Failure-mode flags for one imported ATIF document.

    Mirrors ``04_gate_aa.py`` ``episode_flags`` flag for flag: a recorded
    pass (``extra.reef.episode_score >= 1.0``) is ``{"pass"}``; otherwise the
    expected number for the episode's task label is located in the recorded
    agent replies and tool outputs. ``answers`` maps task label to expected
    number; a task label absent from it skips the number-placement flags.
    Multi-label: several flags may apply to one episode.
    """
    reef_meta = payload.get("extra", {}).get("reef", {}) if isinstance(payload.get("extra"), dict) else {}
    score = reef_meta.get("episode_score") if isinstance(reef_meta, dict) else None
    if isinstance(score, bool) or not isinstance(score, int | float):
        passed = False
    else:
        passed = score >= 1.0
    if passed:
        return {"pass"}
    steps = _agent_steps(payload)
    task = reef_meta.get("task") if isinstance(reef_meta, dict) else None
    label = task.split(maxsplit=1)[0] if isinstance(task, str) and task.split(maxsplit=1) else ""
    expected = answers.get(label)
    number = _number_pattern(expected) if expected is not None else None
    replies = [text for step in steps if (text := _step_messages(step).strip())]
    outputs: list[str] = []
    calls = 0
    bad_args = False
    for step in steps:
        calls += len(step.get("tool_calls", []) if isinstance(step.get("tool_calls"), list) else [])
        for result in _step_results(step):
            content = result.get("content")
            text = content if isinstance(content, str) else ""
            outputs.append(text)
            extra = result.get("extra")
            is_error = isinstance(extra, dict) and extra.get("is_error") is True
            bad_args = bad_args or (is_error and "argument" in text)
    final = replies[-1] if replies else None
    flags: set[str] = set()
    if final is None:
        flags.add("turn ended on an empty reply")
    elif number is not None and number.search(final):
        flags.add("right number, not alone on the last line")
    if (
        number is not None
        and any(number.search(output) for output in outputs)
        and not (final and number.search(final))
    ):
        flags.add("had the right number in tool output, never reported it")
    if any(output.strip() == "exit 0" for output in outputs):
        flags.add("ran code that printed nothing")
    if bad_args:
        flags.add("called a tool with wrong arguments")
    if any("NameError" in output for output in outputs):
        flags.add("assumed state persisted between tool calls")
    if calls == 0:
        flags.add("never used a tool")
    return flags


def failure_flag_counts(
    documents: list[JsonObject], answers: dict[str, str]
) -> dict[str, int]:
    """Multi-label flag counts over every failed document, mirroring ``failure_flags``."""
    counts: dict[str, int] = {}
    for payload in documents:
        flags = episode_flags(payload, answers)
        if "pass" in flags:
            continue
        counts["failed episodes"] = counts.get("failed episodes", 0) + 1
        for flag in flags or {"no flag matched"}:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def load_imported_trajectories(corpus_dir: Path) -> list[JsonObject]:
    """Load every ATIF document named by an intake ``manifest.json``."""
    corpus_dir = Path(corpus_dir)
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    trajectories = manifest.get("trajectories")
    if not isinstance(trajectories, list):
        raise ValueError(f"manifest under {corpus_dir} names no trajectories list")
    documents = []
    for relative in trajectories:
        if not isinstance(relative, str):
            raise ValueError(f"manifest under {corpus_dir} names a non-string trajectory")
        payload = json.loads((corpus_dir / relative).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"trajectory {relative} under {corpus_dir} is not an object")
        documents.append(payload)
    return documents


def compare_failure_shift(
    seed_documents: list[JsonObject],
    check_documents: list[JsonObject],
    answers: dict[str, str],
    reference: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Count failure modes per corpus, optionally checking them against a reference.

    Returns observed counts and, when ``reference`` is given, per-flag
    agreement (observed == reference) and disagreement lists. Without a
    reference the report carries observed counts only. No significance or
    calibration claims: HAR-72 owns gate decisions; this is descriptive
    data over recorded evidence.
    """
    observed = {
        "seed": failure_flag_counts(seed_documents, answers),
        "check": failure_flag_counts(check_documents, answers),
    }
    if reference is None:
        return {
            "observed": observed,
            "reference": {},
            "agreement": {},
            "disagreements": {},
            "all_agree": None,
        }
    agreement: dict[str, dict[str, bool]] = {}
    disagreements: dict[str, list[dict[str, Any]]] = {}
    for arm in ("seed", "check"):
        agreement[arm] = {}
        disagreements[arm] = []
        expected = reference.get(arm, {})
        for flag, expected_count in expected.items():
            matched = observed[arm].get(flag, 0) == expected_count
            agreement[arm][flag] = matched
            if not matched:
                disagreements[arm].append(
                    {
                        "flag": flag,
                        "reference": expected_count,
                        "observed": observed[arm].get(flag, 0),
                    }
                )
        for flag in observed[arm]:
            if flag not in expected:
                agreement[arm][flag] = False
                disagreements[arm].append(
                    {"flag": flag, "reference": 0, "observed": observed[arm][flag]}
                )
    return {
        "observed": observed,
        "reference": {arm: dict(counts) for arm, counts in reference.items()},
        "agreement": agreement,
        "disagreements": disagreements,
        "all_agree": all(disagreement == [] for disagreement in disagreements.values()),
    }


def _load_string_map(path: Path, kind: str) -> dict[str, str]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{kind} file must hold a JSON object: {path}")
    return {str(key): str(value) for key, value in loaded.items()}


def _load_reference(path: Path) -> dict[str, dict[str, int]]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"reference file must hold a JSON object: {path}")
    reference: dict[str, dict[str, int]] = {}
    for arm, counts in loaded.items():
        if not isinstance(counts, dict) or not all(
            isinstance(count, int) and not isinstance(count, bool) for count in counts.values()
        ):
            raise ValueError(f"reference arm {arm!r} must map flags to integer counts: {path}")
        reference[str(arm)] = {str(flag): count for flag, count in counts.items()}
    return reference


def main(argv: list[str] | None = None) -> int:
    """Module CLI: ``python -m evallab.evidence.reef_shift ...``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--seed", type=Path, required=True, help="Imported seed corpus directory")
    parser.add_argument("--check", type=Path, required=True, help="Imported check corpus directory")
    parser.add_argument(
        "--answers",
        type=Path,
        required=True,
        help="JSON {task label: expected number} map; answer-dependent flags need it",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="JSON {arm: {flag: count}} reference; without it the report carries observed counts only",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="Write the report to a new file")
    args = parser.parse_args(argv)
    try:
        report = compare_failure_shift(
            load_imported_trajectories(args.seed),
            load_imported_trajectories(args.check),
            _load_string_map(args.answers, "answers"),
            _load_reference(args.reference) if args.reference is not None else None,
        )
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.json_out is not None:
            with args.json_out.open("x", encoding="utf-8") as handle:
                handle.write(output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
