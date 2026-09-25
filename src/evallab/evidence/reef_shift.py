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

#: The tutorial grader's answer table (``04_gate_aa.py`` ``ANSWERS``, citing
#: ``tutorials/evolve-your-harness/harness/evolution.py:12-15``). Used only
#: to locate the expected number in recorded text, never as hidden grading
#: logic: pass/fail still comes from the recorded episode score.
EXP04_ANSWERS = {"[sieve]": "9592", "[fib]": "2880067194370816120", "[csv]": "30"}

#: Reference failure-flag counts from the exp04 ``summary.json`` files, kept
#: beside the comparison so agreement is checked, never assumed.
EXP04_REFERENCE = {
    "seed": {
        "failed episodes": 169,
        "ran code that printed nothing": 71,
        "turn ended on an empty reply": 68,
        "never used a tool": 46,
        "right number, not alone on the last line": 34,
        "assumed state persisted between tool calls": 25,
        "called a tool with wrong arguments": 23,
        "no flag matched": 8,
        "had the right number in tool output, never reported it": 7,
    },
    "check": {
        "failed episodes": 75,
        "ran code that printed nothing": 61,
        "called a tool with wrong arguments": 19,
        "assumed state persisted between tool calls": 16,
        "no flag matched": 7,
        "had the right number in tool output, never reported it": 5,
        "right number, not alone on the last line": 2,
        "never used a tool": 1,
    },
}

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


def episode_flags(payload: JsonObject, answers: dict[str, str] = EXP04_ANSWERS) -> set[str]:
    """Failure-mode flags for one imported ATIF document.

    Mirrors ``04_gate_aa.py`` ``episode_flags`` flag for flag: a recorded
    pass (``extra.reef.episode_score >= 1.0``) is ``{"pass"}``; otherwise the
    expected number is located in the recorded agent replies and tool
    outputs. Multi-label: several flags may apply to one episode.
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
    documents: list[JsonObject], answers: dict[str, str] = EXP04_ANSWERS
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
    answers: dict[str, str] = EXP04_ANSWERS,
    reference: dict[str, dict[str, int]] = EXP04_REFERENCE,
) -> dict[str, Any]:
    """Count failure modes per corpus and check them against the reference.

    Returns observed counts plus per-flag agreement (observed == reference)
    and disagreement lists. No significance or calibration claims: HAR-72
    owns gate decisions; this is descriptive data over recorded evidence.
    """
    observed = {
        "seed": failure_flag_counts(seed_documents, answers),
        "check": failure_flag_counts(check_documents, answers),
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


def main(argv: list[str] | None = None) -> int:
    """Module CLI: ``python -m evallab.evidence.reef_shift ...``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--seed", type=Path, required=True, help="Imported seed corpus directory")
    parser.add_argument("--check", type=Path, required=True, help="Imported check corpus directory")
    parser.add_argument("--answers", type=Path, default=None, help="JSON {task label: number} map")
    parser.add_argument("--json-out", type=Path, default=None, help="Write the report to a new file")
    args = parser.parse_args(argv)
    try:
        answers = dict(EXP04_ANSWERS)
        if args.answers is not None:
            loaded = json.loads(args.answers.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("answers file must hold a JSON object")
            answers = {str(key): str(value) for key, value in loaded.items()}
        report = compare_failure_shift(
            load_imported_trajectories(args.seed),
            load_imported_trajectories(args.check),
            answers,
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
