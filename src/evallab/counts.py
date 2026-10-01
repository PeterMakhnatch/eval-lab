"""One deterministic counts verdict per finished trial (HAR-78).

``reward`` is never rewritten. ``counts`` says whether that reward may be
quoted as a model result. Only deterministic facts exclude a trial:

- ``copied_fix``: a pass with confirmed upstream acquisition, or whose added
  lines match code read from outside the base checkout
  (:mod:`evallab.copy_check`, ``copied_code``)
- ``pass_tainted``: HAR-100, a pass with confirmed acquisition or a guard reject
- ``task_not_usable``: census or hand label says the task cannot be read.
  The census labels a task's original package; a trial that ran a
  ``validated`` task variant (``library/task-variants``: its own nop was
  sound, e.g. a HAR-113/HAR-115 environment repair) is not excluded by the
  original's census label. Hand labels still apply.
- ``infra``: no verifier reward, or a proxy/gateway error and no score

A fetch on a failure stays ``counted_fail`` and is flagged. First failure,
blame, and loop kind are attached for display. They never change the verdict.
GEPA should read ``counts.verdict`` instead of applying its own fetch-to-zero
rule; this module does not change GEPA.
Failed and unknown fetch attempts are non-deciding flags. The legacy opt-in
GEPA ``UPSTREAM_FETCH_ZERO`` rule still zeros any attempt, regardless of outcome.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from evallab.upstream_fetch import confirmed_fetch

COUNTS_SCHEMA = "evallab.counts/v1"
VERDICTS = ("counted_pass", "counted_fail", "excluded")
EXCLUSION_REASONS = ("copied_fix", "pass_tainted", "task_not_usable", "infra")
ACCURACY_NOTE = "display only; HAR-119 pending; never decides"

HAND_NOT_USABLE = frozenset({"broken", "suspect", "discarded", "review", "unchecked"})
CENSUS_NOT_USABLE = frozenset({"broken_environment", "grader_suspect", "unknown"})
_CENSUS_RELATIVE = Path("research/experiments/har108-python-census/task_health.parquet")
_LEDGER_RELATIVE = Path("research/experiments/python-task-ledger/ledger.csv")
_LEDGER_STATUSES = frozenset({"usable", "review", "discarded", "unchecked"})
_HAND_GLOB = "research/explorations/trace-lab/**/hand/*.json"
_VARIANT_GLOB = "library/task-variants/*/*.json"


def classify_counts(
    *,
    reward: float | None,
    scored: bool,
    taint: list[dict[str, Any]] | None = None,
    usability: dict[str, Any] | None = None,
    exception: dict[str, Any] | None = None,
    first_failure: dict[str, Any] | None = None,
    outcome_failure: dict[str, Any] | None = None,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    """Return ``counts`` for one trial. Judgments are attached and ignored."""
    flags = [flag for flag in (flags or []) if isinstance(flag, str)]
    taint_flags = [flag for flag in (taint or []) if isinstance(flag, dict)]
    attempts = [flag for flag in taint_flags if flag.get("kind") == "upstream_fetch"]
    fetches = [flag for flag in attempts if confirmed_fetch(flag)]
    guards = [flag for flag in taint_flags if flag.get("kind") == "guard_reject"]
    copies = [flag for flag in taint_flags if flag.get("kind") == "copied_code"]
    passed = _passed(reward, scored)
    failed = scored and isinstance(reward, (int, float)) and not isinstance(reward, bool) and float(reward) < 1.0

    reasons: list[str] = []
    evidence: list[dict[str, Any]] = []
    if passed and (fetches or copies):
        reasons.append("copied_fix")
        evidence.extend(_fetch_evidence("copied_fix", fetches))
        evidence.extend(_copy_evidence(copies))
    if passed and (fetches or guards):
        reasons.append("pass_tainted")
        evidence.extend(_taint_evidence(fetches, guards))
    if usability:
        reasons.append("task_not_usable")
        evidence.append(
            _evidence(
                "task_not_usable",
                detector=str(usability.get("detector") or "task_label"),
                excerpt=usability.get("excerpt"),
                path=usability.get("path"),
            )
        )
    if not scored or reward is None:
        reasons.append("infra")
        evidence.append(_infra_evidence(exception))

    display_flags: list[dict[str, Any]] = [
        {
            "code": "upstream_fetch_attempt",
            "decisive": False,
            "outcome": flag.get("outcome", "unknown"),
            "step": flag.get("evidence"),
            "target": flag.get("target"),
            "command": flag.get("command"),
            "outcome_evidence": flag.get("outcome_evidence") or [],
            "document": flag.get("document"),
            "call_id": flag.get("call_id"),
            "outcome_reason": flag.get("outcome_reason"),
            "note": "fetch attempt is not confirmed acquisition; never decides counts",
        }
        for flag in attempts
        if not confirmed_fetch(flag)
    ]
    if fetches and not passed:
        display_flags.append(
            {
                "code": "upstream_fetch",
                "decisive": False,
                "note": "fetched upstream and still failed; verdict stays counted_fail"
                if failed and not reasons
                else "fetched upstream; exclusion came from another fact",
            }
        )

    if reasons:
        verdict = "excluded"
    elif passed:
        verdict = "counted_pass"
    elif failed:
        verdict = "counted_fail"
    else:
        verdict = "excluded"
        if "infra" not in reasons:
            reasons.append("infra")
            evidence.append(_infra_evidence(exception))

    raw_reward = float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else None
    return {
        "schema": COUNTS_SCHEMA,
        "raw_reward": raw_reward,
        "scored": bool(scored) and raw_reward is not None,
        "verdict": verdict,
        "reasons": [reason for reason in EXCLUSION_REASONS if reason in reasons],
        "evidence": evidence,
        "flags": display_flags,
        "judgments": _judgments(first_failure, outcome_failure, flags),
    }


def summarize_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Job rollup. A trial with two exclusion reasons increments both keys."""
    excluded_reasons: dict[str, int] = {}
    for row in rows:
        counts = row.get("counts") if isinstance(row, dict) else None
        if not isinstance(counts, dict) or counts.get("verdict") != "excluded":
            continue
        for reason in counts.get("reasons") or []:
            excluded_reasons[str(reason)] = excluded_reasons.get(str(reason), 0) + 1
    verdicts = [
        (row.get("counts") or {}).get("verdict")
        for row in rows
        if isinstance(row, dict)
    ]
    return {
        "n_counted_pass": sum(1 for verdict in verdicts if verdict == "counted_pass"),
        "n_counted_fail": sum(1 for verdict in verdicts if verdict == "counted_fail"),
        "n_excluded": sum(1 for verdict in verdicts if verdict == "excluded"),
        "excluded_reasons": excluded_reasons,
    }


def attach_counts(
    record: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    label_root: Path | None,
    package_digest: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Build ``counts`` from a process-job record and its ``result.json``.

    ``package_digest`` is the task package the job ran (its ExperimentSpec's
    ``task_package_digest``); a validated variant's digest lifts the original
    task's census exclusion.
    """
    result = result if isinstance(result, dict) else {}
    task_name = task_id or record.get("task_name") or result.get("task_name")
    trial_name = record.get("trial_name") or result.get("trial_name")
    exception = result.get("exception_info")
    exception = exception if isinstance(exception, dict) else None
    index = task_index_for(label_root)
    counts = classify_counts(
        reward=record.get("reward"),
        scored=bool(record.get("scored")),
        taint=record.get("taint") if isinstance(record.get("taint"), list) else [],
        usability=usability(
            index,
            trial_name=trial_name,
            task_name=task_name,
            package_digest=package_digest,
        ),
        exception=exception,
        first_failure=record.get("first_failure") if isinstance(record.get("first_failure"), dict) else None,
        outcome_failure=record.get("outcome_failure") if isinstance(record.get("outcome_failure"), dict) else None,
        flags=record.get("flags") if isinstance(record.get("flags"), list) else [],
    )
    counts["task_status"] = index.status_for(task_name, package_digest)
    return counts


def find_label_root(start: Path) -> Path | None:
    """Walk parents for the committed task ledger or legacy census."""
    for parent in (start, *start.parents):
        if (parent / _LEDGER_RELATIVE).is_file() or (parent / _CENSUS_RELATIVE).is_file():
            return parent
    return None


def usability(
    index: TaskIndex,
    *,
    trial_name: str | None,
    task_name: str | None,
    package_digest: str | None = None,
) -> dict[str, Any] | None:
    """Keep hand exclusions; bind ledger/validated-variant rules to the package."""
    hand = index.hand_for(trial_name, task_name)
    if hand is not None and hand["status"] in HAND_NOT_USABLE:
        return hand
    ledger = index.ledger.get(_task_id(task_name) or "")
    if (
        ledger
        and package_digest
        and ledger["run_digest"] == package_digest
        and ledger["status"] != "usable"
    ):
        return ledger
    census = index.census_for(task_name)
    if census is not None and census["status"] in CENSUS_NOT_USABLE:
        if index.validated_variant(task_name, package_digest):
            return None
        return census
    return None


class TaskIndex:
    """Version-bound canonical task ledger with legacy hand/census fallback."""

    def __init__(
        self,
        hands: dict[str, dict[str, Any]],
        census: dict[str, dict[str, Any]],
        validated: dict[str, str],
        ledger: dict[str, dict[str, Any]],
    ) -> None:
        self.hands = hands
        self.census = census
        self.ledger = ledger
        self.validated = validated

    def status_for(
        self, task_name: str | None, task_package_digest: str | None
    ) -> dict[str, Any]:
        """Display the exact ledger binding, not a guessed usable status."""
        task_id = _task_id(task_name)
        row = self.ledger.get(task_id or "")
        matched = (
            row["run_digest"] == task_package_digest
            if row is not None and task_package_digest
            else None
        )
        if row is None:
            reason = "No canonical task ledger row; verdict uses legacy labels when present."
        elif matched is None:
            reason = "Trial task-package digest is absent; ledger status is not applied."
        elif not matched:
            reason = "Ledger describes a different task package; verdict uses legacy labels."
        else:
            reason = row["excerpt"]
        return {
            "status": row["status"] if row is not None and matched else None,
            "ledger_status": row["status"] if row is not None else None,
            "source": "python_task_ledger" if row is not None else None,
            "path": row["path"] if row is not None else None,
            "source_sha256": row["source_sha256"] if row is not None else None,
            "task_id": task_id,
            "run_digest": row["run_digest"] if row is not None else None,
            "trial_digest": task_package_digest,
            "digest_match": matched,
            "reason": reason,
            "evidence": row["evidence"] if row is not None else [],
        }

    def hand_for(self, trial_name: str | None, task_name: str | None) -> dict[str, Any] | None:
        if trial_name and trial_name in self.hands:
            return self.hands[trial_name]
        task_id = _task_id(task_name)
        if task_id and task_id in self.hands:
            return self.hands[task_id]
        return None

    def census_for(self, task_name: str | None) -> dict[str, Any] | None:
        task_id = _task_id(task_name)
        if task_id and task_id in self.census:
            return self.census[task_id]
        return None

    def validated_variant(self, task_name: str | None, package_digest: str | None) -> bool:
        """The trial ran a validated variant of this very task."""
        task_id = _task_id(task_name)
        return bool(package_digest) and task_id is not None and (
            self.validated.get(package_digest or "") == task_id
        )


def task_index_for(root: Path | None) -> TaskIndex:
    if root is None:
        return TaskIndex({}, {}, {}, {})
    return _load_task_index(str(root.resolve()))


@lru_cache(maxsize=4)
def _load_task_index(root: str) -> TaskIndex:
    base = Path(root)
    hands: dict[str, dict[str, Any]] = {}
    for path in sorted(base.glob(_HAND_GLOB)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("task_verdict") or "").strip().lower()
        if not status:
            continue
        record = {
            "status": status,
            "detector": "hand_label",
            "path": str(path.relative_to(base)),
            "excerpt": f"task_verdict={status}",
        }
        trial_id = payload.get("trial_id")
        task_id = _task_id(payload.get("task_id"))
        if isinstance(trial_id, str) and trial_id:
            hands[trial_id] = record
        if task_id:
            hands.setdefault(task_id, record)
    census: dict[str, dict[str, Any]] = {}
    parquet = base / _CENSUS_RELATIVE
    if parquet.is_file():
        import pyarrow.parquet as pq

        for row in pq.read_table(parquet, columns=["task_id", "label", "evidence"]).to_pylist():
            task_id = _task_id(row.get("task_id"))
            if not task_id:
                continue
            label = str(row.get("label") or "")
            census[task_id] = {
                "status": label,
                "detector": "task_health",
                "path": str(_CENSUS_RELATIVE),
                "excerpt": f"label={label}; {row.get('evidence') or ''}"[:160],
            }
    validated: dict[str, str] = {}
    for path in sorted(base.glob(_VARIANT_GLOB)):
        try:
            variant = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(variant, dict) or variant.get("status") != "validated":
            continue
        digest = variant.get("variant_digest")
        task_id = _task_id(variant.get("task_name"))
        if isinstance(digest, str) and task_id:
            validated[digest] = task_id
    ledger: dict[str, dict[str, Any]] = {}
    ledger_path = base / _LEDGER_RELATIVE
    if ledger_path.is_file():
        raw = ledger_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
        required = {"task_id", "status", "run_digest", "reason", "evidence"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f"Task ledger missing columns: {ledger_path}")
        for row in reader:
            task_id = _task_id(row["task_id"])
            if not task_id or task_id in ledger or row["status"] not in _LEDGER_STATUSES:
                raise ValueError(f"Invalid or duplicate task ledger row: {row['task_id']}")
            ledger[task_id] = {
                "status": row["status"],
                "detector": "python_task_ledger",
                "path": f"{_LEDGER_RELATIVE}#{task_id}",
                "source_sha256": digest,
                "run_digest": row["run_digest"],
                "excerpt": f"status={row['status']}; {row['reason']}",
                "evidence": row["evidence"].split(),
            }
    return TaskIndex(hands, census, validated, ledger)


def _passed(reward: float | None, scored: bool) -> bool:
    return (
        scored
        and isinstance(reward, (int, float))
        and not isinstance(reward, bool)
        and float(reward) >= 1.0
    )


def _task_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().rsplit("/", 1)[-1]


def _evidence(
    reason: str,
    *,
    detector: str,
    step: int | str | None = None,
    command: str | None = None,
    excerpt: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"reason": reason, "detector": detector}
    if isinstance(step, int) or (isinstance(step, str) and step.isdigit()):
        item["step"] = int(step)
    if command:
        item["command"] = command[:160]
    if excerpt:
        item["excerpt"] = str(excerpt)[:160]
    if path:
        item["path"] = path
    if not any(item.get(key) for key in ("step", "command", "excerpt", "path")):
        item["excerpt"] = "no excerpt recorded"
    return item


def _fetch_evidence(
    reason: str,
    fetches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **_evidence(
                reason,
                detector="upstream_fetch",
                command=flag.get("command"),
                excerpt=flag.get("evidence"),
                path="agent/trajectory.json"
                if flag.get("document") == "head"
                else f"agent/trajectory.{flag.get('document')}.json",
            ),
            "outcome": "succeeded",
            "target": flag.get("target"),
            "document": flag.get("document"),
            "call_id": flag.get("call_id"),
            "observations": flag["outcome_evidence"],
        }
        for flag in fetches
    ]


def _copy_evidence(copies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **_evidence(
                "copied_fix",
                detector="copy_check",
                step=(flag.get("source_steps") or [{}])[0].get("step"),
                command=(flag.get("source_steps") or [{}])[0].get("command"),
                excerpt=f"{flag.get('matched_lines')} of {flag.get('added_lines')} added lines "
                "match code read from outside the base checkout",
                path="verifier/agent.diff",
            ),
            "rule": flag.get("rule"),
            "source_steps": flag.get("source_steps") or [],
            "examples": flag.get("examples") or [],
        }
        for flag in copies
    ]


def _taint_evidence(fetches: list[dict[str, Any]], guards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = _fetch_evidence("pass_tainted", fetches)
    items.extend(
        _evidence(
            "pass_tainted",
            detector="guard_reject",
            excerpt=flag.get("evidence") or flag.get("rule"),
            path="verifier/test-stdout.txt",
        )
        for flag in guards
    )
    return items


def _infra_evidence(exception: dict[str, Any] | None) -> dict[str, Any]:
    if not exception:
        return _evidence("infra", detector="verifier_result", excerpt="no verifier reward", path="result.json")
    message = exception.get("exception_message") or exception.get("exception_type") or "no verifier reward"
    return _evidence(
        "infra",
        detector="result.exception_info",
        excerpt=str(message),
        path="result.json",
    )


def _judgments(
    first_failure: dict[str, Any] | None,
    outcome_failure: dict[str, Any] | None,
    flags: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if first_failure and first_failure.get("rule_id"):
        items.append(_judgment("first_failure", first_failure.get("rule_id"), first_failure.get("attribution")))
    if outcome_failure and (outcome_failure.get("attribution") or outcome_failure.get("rule_id")):
        items.append(_judgment("blame", outcome_failure.get("rule_id"), outcome_failure.get("attribution")))
    loop = next(
        (flag for flag in flags if flag.startswith(("identical_loop:", "loop_suspicion:", "confirmation_loop:"))),
        None,
    )
    if loop:
        items.append(_judgment("loop_kind", loop.split(":", 1)[0], None))
    return items


def _judgment(label: str, value: Any, attribution: Any) -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "attribution": attribution,
        "accuracy": None,
        "accuracy_note": ACCURACY_NOTE,
    }
