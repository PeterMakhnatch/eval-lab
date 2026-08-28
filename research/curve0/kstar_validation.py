"""Curve 0 - read-only method validation of derailment-point (k*) predicates.

PURPOSE
    Validate whether *preregistered, deterministic* divergence predicates locate a
    real irreversibility boundary in agent trajectories. The deliverable is a
    statement about the METHOD, not about any model's capability.

WHAT THIS IS NOT
    Not a capability measurement. Not a benchmark comparison. No pooling of
    effects across corpora. No model calls. No re-runs. No LLM-judged labels.

PREREGISTRATION (transcribed from
research/inbox/CAPABILITY-CURVE-ENGINE-SPEC-2026-08-27.md, Curve 0)
    Predicates and thresholds were fixed before any outcome was computed. This is
    transcribed preregistration, not third-party timestamped: the honest claim is
    "declared in the spec and not altered after seeing results". The artifact
    records a predicate digest so any later change is detectable.

    P1 no_progress_lock     earliest action index k after which the trajectory
                            acquires no NEW observation content.
    P2 blind_retry_lock     earliest k after which every later action repeats an
                            already-seen (name, arguments) pair and contributes no
                            new observation content.
    P3 error_cascade_to_end earliest k beginning a maximal run of non-zero exit
                            codes extending to the final action. Requires
                            STRUCTURED exit codes. Corpora lacking them report
                            PREDICATE_UNAVAILABLE - we never parse exit codes out
                            of observation text and call it a fact.
    P4 aci_state_stall      earliest k after which the per-step interface state
                            snapshot never changes again. Requires a structured
                            per-step state field.

    TAIL_MIN = 3   a predicate firing on the final steps is trivial
    MIN_N    = 5   below this an arm's rate is null with a stated reason

ELIGIBILITY (opportunity conditioning)
    A trial is ELIGIBLE for a predicate only if it has at least TAIL_MIN + 1
    actions and carries the predicate's required fields. Rates are conditioned on
    ELIGIBLE trials, never on all trials. Ineligible trials are counted and
    reported, never silently absorbed into a denominator.

KEY VALIDATION METRIC
    false_positive_on_later_success - how often a predicate fires on runs that
    nevertheless SUCCEEDED. A predicate that fires before recoverable dips is
    measuring noise, not irreversibility. This number decides whether the method
    survives.

VALIDITY DIAGNOSTIC
    observation_digest_stability - among repeated actions, how often the
    observation digest differs. Digest-equality is the information-intake proxy
    used by P1/P2; if repeated identical actions yield differing digests
    (timestamps, wall-time, PIDs in output), that proxy is INVALID for the corpus
    and P1/P2 results must be read as inconclusive rather than negative.

USAGE
    python research/curve0/kstar_validation.py \
        --local-runs-root <path> \
        --swebench-dir research/curve0/.cache/swebench \
        --taubench-file research/curve0/.cache/taubench/gpt-4o-airline.json \
        --out research/curve0/results
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TAIL_MIN = 3
MIN_N = 5
PREREGISTRATION_TEXT = (
    "P1 no_progress_lock; P2 blind_retry_lock; P3 error_cascade_to_end; "
    "P4 aci_state_stall; "
    f"TAIL_MIN={TAIL_MIN}; MIN_N={MIN_N}; "
    "rates conditioned on ELIGIBLE trials (>=TAIL_MIN+1 actions and required "
    "fields present); primary metric=false_positive_on_later_success; "
    "no pooling of effects across corpora"
)

# --------------------------------------------------------------------------- #
# ARM 2 PREREGISTRATION - normalized-observation proxy
#
# Arm 1 found the information-intake proxy (raw observation-digest equality) is
# defeated by nondeterministic content: instability ranged 0.045 -> 1.0 across
# corpora, making arm-1 nulls on unstable corpora INCONCLUSIVE rather than
# negative. Arm 2 tests whether digesting NORMALIZED observations repairs the
# proxy without inflating false positives.
#
# Normalization is itself a hypothesis with a specific failure mode:
# OVER-normalization manufactures false equality, which inflates predicate
# firing and therefore false positives. So the arm is only accepted if it fixes
# instability AND holds precision. The decision rule is fixed here, before any
# arm-2 outcome is computed.
#
# Rule set is deliberately CONSERVATIVE and general-purpose. Ambiguous
# single-letter duration units (s/m/h) are excluded on purpose: under-
# normalizing is the safe error direction, since over-normalizing corrupts
# precision. Line numbers are NEVER stripped - they carry real information in
# file-view observations (SWE-agent).
# --------------------------------------------------------------------------- #
_NORM_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "iso_timestamp",
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<TS>",
    ),
    ("clock_time", re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b"), "<TIME>"),
    ("wall_time_line", re.compile(r"(?i)\bwall time\b[^\n]*"), "<WALL>"),
    (
        "duration_unambiguous",
        re.compile(
            r"(?i)\b\d+(?:\.\d+)?\s*"
            r"(?:ns|us|\u00b5s|ms|sec|secs|seconds|min|mins|minutes|hr|hrs|hours)\b"
        ),
        "<DUR>",
    ),
    ("hex_address", re.compile(r"\b0x[0-9a-fA-F]+\b"), "<ADDR>"),
    (
        "uuid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    ("long_hex", re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HEX>"),
    ("pid", re.compile(r"(?i)\bpid[=: ]\s*\d+"), "<PID>"),
    (
        "temp_path",
        re.compile(r"(?:/tmp|/var/folders|/private/var/folders)/[\w./-]+"),
        "<TMP>",
    ),
    ("trailing_ws", re.compile(r"[ \t]+$", re.MULTILINE), ""),
)

ARM2_PREREGISTRATION_TEXT = (
    "ARM2 normalized-observation proxy; "
    "P1n no_progress_lock_normalized; P2n blind_retry_lock_normalized; "
    "normalization rules=" + ",".join(name for name, _, _ in _NORM_RULES) + "; "
    "line numbers never stripped; ambiguous single-letter duration units excluded; "
    "DECISION RULE: ACCEPT iff on a corpus whose raw instability > 0.5 the "
    "normalized instability < 0.2 AND normalized FPR <= 0.05; "
    "REJECT_OVER_NORMALIZED iff normalized FPR > 0.05 on any corpus whose raw "
    "FPR was 0.0; else INCONCLUSIVE; "
    "instability rate reported for BOTH raw and normalized on every corpus; "
    "no pooling of effects across corpora"
)
ARM2_ACCEPT_INSTABILITY_MAX = 0.2
ARM2_RAW_INSTABILITY_TRIGGER = 0.5
ARM2_ACCEPT_FPR_MAX = 0.05


def normalize_observation(text: str) -> tuple[str, dict[str, int]]:
    """Apply the preregistered rule set. Returns normalized text and per-rule hits."""
    hits: dict[str, int] = {}
    out = text
    for name, pattern, repl in _NORM_RULES:
        out, n = pattern.subn(repl, out)
        if n:
            hits[name] = hits.get(name, 0) + n
    return out, hits


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest_any(value: Any) -> str:
    return _sha256_text(value if isinstance(value, str) else json.dumps(value, sort_keys=True))


def _digest_norm(value: Any, hits: dict[str, int]) -> str:
    """Digest of the NORMALIZED observation. Accumulates per-rule hit counts so
    over-normalization is auditable rather than invisible."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    normalized, rule_hits = normalize_observation(text)
    for name, n in rule_hits.items():
        hits[name] = hits.get(name, 0) + n
    hits["_chars_before"] = hits.get("_chars_before", 0) + len(text)
    hits["_chars_after"] = hits.get("_chars_after", 0) + len(normalized)
    return _sha256_text(normalized)


@dataclass
class CorpusDescriptor:
    corpus_id: str
    disposition: str  # available | unavailable
    source: str
    harness: str
    is_public: bool
    scaffold: str | None = None
    models: list[str] = field(default_factory=list)
    pin: str | None = None
    artifact_digests: dict[str, str] = field(default_factory=dict)
    license: str | None = None
    n_trials: int = 0
    n_duplicates_dropped: int = 0
    fields_present: list[str] = field(default_factory=list)
    fields_absent: list[str] = field(default_factory=list)
    predicates_available: list[str] = field(default_factory=list)
    predicates_unavailable: dict[str, str] = field(default_factory=dict)
    unavailable_reason: str | None = None
    normalization_hits: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Trial:
    trial_uid: str
    corpus_id: str
    task_name: str
    model_name: str | None
    succeeded: bool | None
    actions: list[dict[str, Any]]
    source_ref: str
    has_structured_exit: bool = False
    has_state: bool = False


# --------------------------------------------------------------------------- #
# predicates
# --------------------------------------------------------------------------- #
def _cumulative_obs(actions: list[dict[str, Any]], obs_key: str = "obs_digests") -> list[set[str]]:
    seen: set[str] = set()
    out = []
    for a in actions:
        seen = seen | set(a[obs_key])
        out.append(set(seen))
    return out


def p_no_progress_lock(t: Trial, obs_key: str = "obs_digests") -> int | None:
    acts = t.actions
    n = len(acts)
    cum = _cumulative_obs(acts, obs_key)
    total = cum[-1]
    for k in range(n - TAIL_MIN):
        if cum[k] == total:
            return k
    return None


def p_blind_retry_lock(t: Trial, obs_key: str = "obs_digests") -> int | None:
    acts = t.actions
    n = len(acts)
    for k in range(n - TAIL_MIN):
        prior_keys = {a["key"] for a in acts[: k + 1]}
        prior_obs: set[str] = set()
        for a in acts[: k + 1]:
            prior_obs |= set(a[obs_key])
        if all(a["key"] in prior_keys and not (set(a[obs_key]) - prior_obs) for a in acts[k + 1 :]):
            return k
    return None


def p_no_progress_lock_normalized(t: Trial) -> int | None:
    return p_no_progress_lock(t, obs_key="obs_digests_norm")


def p_blind_retry_lock_normalized(t: Trial) -> int | None:
    return p_blind_retry_lock(t, obs_key="obs_digests_norm")


def p_error_cascade_to_end(t: Trial) -> int | None:
    acts = t.actions
    n = len(acts)
    codes = [a.get("exit_code") for a in acts]
    if any(c is None for c in codes):
        return None
    k = n
    while k - 1 >= 0 and codes[k - 1] != 0:
        k -= 1
    return k if k < n - TAIL_MIN else None


def p_aci_state_stall(t: Trial) -> int | None:
    acts = t.actions
    n = len(acts)
    states = [a.get("state_digest") for a in acts]
    if any(s is None for s in states):
        return None
    for k in range(n - TAIL_MIN):
        if all(states[j] == states[k] for j in range(k, n)):
            return k
    return None


PREDICATES: dict[str, Callable[[Trial], int | None]] = {
    "no_progress_lock": p_no_progress_lock,
    "blind_retry_lock": p_blind_retry_lock,
    "error_cascade_to_end": p_error_cascade_to_end,
    "aci_state_stall": p_aci_state_stall,
}
PREDICATE_REQUIRES = {
    "no_progress_lock": "observations",
    "blind_retry_lock": "observations+action_keys",
    "error_cascade_to_end": "structured_exit_codes",
    "aci_state_stall": "per_step_state",
}
PREDICATE_IDS = tuple(PREDICATES)

# arm 2: same predicates, normalized information-intake proxy
PREDICATES_NORMALIZED: dict[str, Callable[[Trial], int | None]] = {
    "no_progress_lock_normalized": p_no_progress_lock_normalized,
    "blind_retry_lock_normalized": p_blind_retry_lock_normalized,
}
NORMALIZED_BASE = {
    "no_progress_lock_normalized": "no_progress_lock",
    "blind_retry_lock_normalized": "blind_retry_lock",
}


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def load_local_atif(runs_root: Path) -> tuple[CorpusDescriptor, list[Trial]]:
    paths = sorted(runs_root.glob("**/agent/trajectory.json"))
    trials: list[Trial] = []
    norm_hits: dict[str, int] = {}
    seen: set[str] = set()
    dup = 0
    models: set[str] = set()
    any_exit = False
    any_reasoning_tokens = False

    for p in paths:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session = doc.get("session_id") or str(p)
        if session in seen:
            dup += 1
            continue
        seen.add(session)

        trial_dir = p.parent.parent
        reward = None
        task_name = trial_dir.parent.name
        res_p = trial_dir / "result.json"
        if res_p.exists():
            try:
                res = json.loads(res_p.read_text(encoding="utf-8"))
                rewards = (res.get("verifier_result") or {}).get("rewards") or {}
                if rewards:
                    reward = rewards.get("reward", next(iter(rewards.values()), None))
                task_name = res.get("task_name") or task_name
            except (OSError, json.JSONDecodeError):
                pass

        actions: list[dict[str, Any]] = []
        for s in doc.get("steps") or []:
            if s.get("model_name"):
                models.add(s["model_name"])
            if ((s.get("metrics") or {}).get("extra") or {}).get(
                "reasoning_output_tokens"
            ) is not None:
                any_reasoning_tokens = True
            obs_contents = [
                r["content"]
                for r in ((s.get("observation") or {}).get("results") or [])
                if r.get("content") is not None
            ]
            obs_digests = [_digest_any(c) for c in obs_contents]
            obs_digests_norm = [_digest_norm(c, norm_hits) for c in obs_contents]
            for tc in s.get("tool_calls") or []:
                actions.append(
                    {
                        "key": _digest_any(f"{tc.get('function_name')}::{tc.get('arguments')}"),
                        "obs_digests": obs_digests,
                        "obs_digests_norm": obs_digests_norm,
                        "exit_code": None,
                        "state_digest": None,
                    }
                )
        trials.append(
            Trial(
                trial_uid=session,
                corpus_id="local-atif-harbor",
                task_name=task_name,
                model_name=next(iter(sorted(models)), None) if models else None,
                succeeded=(reward == 1.0) if isinstance(reward, (int, float)) else None,
                actions=actions,
                source_ref=str(p.relative_to(runs_root.parent)),
            )
        )

    desc = CorpusDescriptor(
        corpus_id="local-atif-harbor",
        disposition="available",
        source="LOCAL working tree runs/ (NOT a public corpus)",
        harness="Harbor / ATIF-v1.7",
        is_public=False,
        scaffold="codex / antigravity-cli (mixed)",
        models=sorted(models),
        pin="local-working-tree (no upstream pin possible)",
        license="internal",
        n_trials=len(trials),
        n_duplicates_dropped=dup,
        fields_present=[
            "step_id",
            "source",
            "timestamp",
            "model_name",
            "message",
            "tool_calls{function_name,arguments}",
            "observation.results{content,source_call_id}",
            "metrics{prompt,completion,cached,cost}",
            "metrics.extra.reasoning_output_tokens",
        ],
        fields_absent=[
            "structured exit_code",
            "reasoning_content",
            "logprobs",
            "per-step environment state",
            "finish_reason",
            "sampling params",
        ],
        predicates_unavailable={
            "error_cascade_to_end": "no structured exit codes; ATIF observation results carry only {content, source_call_id}",
            "aci_state_stall": "no per-step state field in ATIF steps",
        },
        notes=[
            "Short-horizon only: cannot validate any long-horizon claim.",
            "reasoning_output_tokens present under metrics.extra but dropped by the "
            "current ingest path (evidence/atif.py) - block J data loss confirmed."
            if any_reasoning_tokens
            else "no reasoning token fields observed",
        ],
    )
    desc.predicates_available = [p for p in PREDICATE_IDS if p not in desc.predicates_unavailable]
    _ = any_exit
    desc.normalization_hits = norm_hits
    return desc, trials


def load_swebench_sweagent(cache_dir: Path) -> tuple[CorpusDescriptor, list[Trial]]:
    results_p = cache_dir / "results.json"
    trajs_dir = cache_dir / "trajs"
    results = json.loads(results_p.read_text(encoding="utf-8"))
    resolved = set(results.get("resolved") or [])
    generated = set(results.get("generated") or [])

    trials: list[Trial] = []
    norm_hits: dict[str, int] = {}
    digests: dict[str, str] = {"results.json": _sha256_file(results_p)}
    for p in sorted(trajs_dir.glob("*.traj")):
        instance = p.stem
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = doc.get("trajectory") or []
        actions = []
        for s in steps:
            action = s.get("action")
            obs = s.get("observation")
            state = s.get("state")
            actions.append(
                {
                    "key": _digest_any(action if action is not None else ""),
                    "obs_digests": [_digest_any(obs)] if obs is not None else [],
                    "obs_digests_norm": ([_digest_norm(obs, norm_hits)] if obs is not None else []),
                    "exit_code": None,
                    "state_digest": _digest_any(state) if state is not None else None,
                }
            )
        trials.append(
            Trial(
                trial_uid=instance,
                corpus_id="swebench-verified-sweagent-gpt4",
                task_name=instance.split("-")[0],
                model_name="gpt-4 (SWE-agent 20240402)",
                succeeded=(instance in resolved) if instance in generated else None,
                actions=actions,
                source_ref=f"trajs/{instance}.traj",
                has_state=all(a["state_digest"] is not None for a in actions) if actions else False,
            )
        )
        digests[f"trajs/{instance}.traj"] = _sha256_file(p)

    desc = CorpusDescriptor(
        corpus_id="swebench-verified-sweagent-gpt4",
        disposition="available",
        source="SWE-bench/experiments + swe-bench-submissions S3 (verified/20240402_sweagent_gpt4)",
        harness="SWE-agent ACI",
        is_public=True,
        scaffold="SWE-agent",
        models=["gpt-4 (20240402 submission)"],
        pin="SWE-bench/experiments@1faa91cade0562ba62b66c1c99e71f7b72d96f13; "
        "trajs from s3://swe-bench-submissions/verified/20240402_sweagent_gpt4/trajs",
        artifact_digests=digests,
        license="MIT",
        n_trials=len(trials),
        fields_present=[
            "action",
            "observation",
            "response",
            "thought",
            "state{open_file,working_dir}",
            "info.exit_status",
        ],
        fields_absent=[
            "structured exit_code",
            "per-step token usage",
            "per-step timestamp",
            "full environment state snapshot",
        ],
        predicates_unavailable={
            "error_cascade_to_end": "no structured exit codes; observations are terminal text",
        },
        notes=[
            "Sampling is deterministic: sorted(resolved)[:30] + sorted(generated-resolved)[:30].",
            "state is INTERFACE state (open_file, working_dir), NOT full environment state; "
            "aci_state_stall therefore measures navigation stall, not environment divergence.",
        ],
    )
    desc.predicates_available = [p for p in PREDICATE_IDS if p not in desc.predicates_unavailable]
    desc.normalization_hits = norm_hits
    return desc, trials


def load_taubench(path: Path) -> tuple[CorpusDescriptor, list[Trial]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    trials: list[Trial] = []
    norm_hits: dict[str, int] = {}
    for i, trial in enumerate(doc):
        traj = trial.get("traj") or []
        actions = []
        pending: list[str] = []
        for turn in traj:
            role = turn.get("role")
            if role == "assistant" and turn.get("tool_calls"):
                for tc in turn["tool_calls"]:
                    fn = tc.get("function") or {}
                    pending.append(_digest_any(f"{fn.get('name')}::{fn.get('arguments')}"))
            elif role == "tool":
                key = pending.pop(0) if pending else _digest_any("unpaired")
                actions.append(
                    {
                        "key": key,
                        "obs_digests": [_digest_any(turn.get("content") or "")],
                        "obs_digests_norm": [_digest_norm(turn.get("content") or "", norm_hits)],
                        "exit_code": None,
                        "state_digest": None,
                    }
                )
        reward = trial.get("reward")
        trials.append(
            Trial(
                trial_uid=f"{trial.get('task_id')}#trial{trial.get('trial', i)}",
                corpus_id="taubench-airline-gpt4o",
                task_name=f"airline/task_{trial.get('task_id')}",
                model_name="gpt-4o",
                succeeded=(reward == 1.0) if isinstance(reward, (int, float)) else None,
                actions=actions,
                source_ref=f"historical_trajectories/gpt-4o-airline.json#{i}",
            )
        )

    desc = CorpusDescriptor(
        corpus_id="taubench-airline-gpt4o",
        disposition="available",
        source="sierra-research/tau-bench historical_trajectories/gpt-4o-airline.json",
        harness="tau-bench",
        is_public=True,
        scaffold="tau-bench tool-calling agent",
        models=["gpt-4o"],
        pin="sierra-research/tau-bench@59a200c6d575d595120f1cb70fea53cef0632f6b",
        artifact_digests={"gpt-4o-airline.json": _sha256_file(path)},
        license="MIT",
        n_trials=len(trials),
        fields_present=[
            "role",
            "content",
            "tool_calls{function{name,arguments}}",
            "tool_call_id",
            "reward",
            "info",
        ],
        fields_absent=[
            "structured exit_code",
            "per-step state snapshot",
            "per-step token usage",
            "per-step timestamp",
            "logprobs",
            "finish_reason",
        ],
        predicates_unavailable={
            "error_cascade_to_end": "no structured exit codes; tool results are JSON/text strings",
            "aci_state_stall": "no per-step state field; DB mutations appear only inside tool output",
        },
        notes=[
            "Upstream tau-bench drops usage/logprobs/finish_reason at the agent loop "
            "(tool_calling_agent.py keeps only choices[0].message), so per-step token "
            "telemetry is absent by construction, not by our parsing.",
        ],
    )
    desc.predicates_available = [p for p in PREDICATE_IDS if p not in desc.predicates_unavailable]
    desc.normalization_hits = norm_hits
    return desc, trials


def unavailable(corpus_id: str, source: str, reason: str) -> CorpusDescriptor:
    return CorpusDescriptor(
        corpus_id=corpus_id,
        disposition="unavailable",
        source=source,
        harness="n/a",
        is_public=True,
        unavailable_reason=reason,
        notes=["No rows emitted. Absence recorded, never simulated."],
    )


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def _rate(numer: int, denom: int) -> float | None:
    if denom == 0 or denom < MIN_N:
        return None
    return round(numer / denom, 4)


def _eligible(t: Trial, predicate: str) -> bool:
    if len(t.actions) < TAIL_MIN + 1:
        return False
    req = PREDICATE_REQUIRES[predicate]
    if req == "structured_exit_codes":
        return all(a.get("exit_code") is not None for a in t.actions)
    if req == "per_step_state":
        return all(a.get("state_digest") is not None for a in t.actions)
    return any(a["obs_digests"] for a in t.actions)


def observation_digest_stability(
    trials: list[Trial], obs_key: str = "obs_digests"
) -> dict[str, Any]:
    """Validity check on the information-intake proxy used by P1/P2.

    Reported for BOTH the raw and normalized proxy on every corpus, per the arm-2
    preregistration: a null result on an unstable proxy is inconclusive, never
    negative, and that distinction must be visible in the artifact.
    """
    same = diff = 0
    for t in trials:
        bykey: dict[str, list[tuple[str, ...]]] = {}
        for a in t.actions:
            bykey.setdefault(a["key"], []).append(tuple(sorted(a[obs_key])))
        for obs_list in bykey.values():
            if len(obs_list) < 2:
                continue
            if len(set(obs_list)) == 1:
                same += 1
            else:
                diff += 1
    total = same + diff
    return {
        "proxy": obs_key,
        "repeated_action_groups": total,
        "identical_observations": same,
        "differing_observations": diff,
        "instability_rate": (round(diff / total, 4) if total else None),
        "verdict": (
            "NO_REPEATS: corpus contains too few repeated actions to assess the proxy"
            if total == 0
            else "PROXY_INVALID: repeated actions yield differing observation digests "
            "(nondeterministic content); nulls are INCONCLUSIVE, not negative"
            if diff / total > ARM2_RAW_INSTABILITY_TRIGGER
            else "PROXY_USABLE: repeated actions mostly yield identical digests"
        ),
    }


def evaluate_corpus(desc: CorpusDescriptor, trials: list[Trial]) -> dict[str, Any]:
    scored = [t for t in trials if t.succeeded is not None]
    succ = [t for t in scored if t.succeeded]
    fail = [t for t in scored if not t.succeeded]

    out: dict[str, Any] = {
        "corpus_id": desc.corpus_id,
        "is_public": desc.is_public,
        "n_trials_total": len(trials),
        "n_with_outcome": len(scored),
        "n_success": len(succ),
        "n_fail": len(fail),
        "action_count_distribution": dict(
            Counter(
                "0"
                if not t.actions
                else "1-3"
                if len(t.actions) <= 3
                else "4-10"
                if len(t.actions) <= 10
                else "11-30"
                if len(t.actions) <= 30
                else "31-100"
                if len(t.actions) <= 100
                else ">100"
                for t in trials
            )
        ),
        "observation_digest_stability": observation_digest_stability(trials),
        "observation_digest_stability_normalized": observation_digest_stability(
            trials, "obs_digests_norm"
        ),
        "normalization_audit": _normalization_audit(desc.normalization_hits),
        "predicates": {},
        "predicates_normalized": {},
    }

    if succ and fail:
        ms = sum(len(t.actions) for t in succ) / len(succ)
        mf = sum(len(t.actions) for t in fail) / len(fail)
        out["length_confound"] = {
            "mean_actions_success": round(ms, 2),
            "mean_actions_fail": round(mf, 2),
            "warning": (
                "success and failure sets differ in mean action count; fire-rate "
                "differences are confounded with trajectory length in this corpus"
                if abs(ms - mf) >= 3
                else "action counts comparable"
            ),
        }

    for pid in PREDICATE_IDS:
        if pid in desc.predicates_unavailable:
            out["predicates"][pid] = {
                "disposition": "PREDICATE_UNAVAILABLE",
                "reason": desc.predicates_unavailable[pid],
            }
            continue
        out["predicates"][pid] = _score_predicate(PREDICATES[pid], pid, succ, fail, len(scored))

    # ---- arm 2: same predicates over the normalized information-intake proxy --
    for pid, fn in PREDICATES_NORMALIZED.items():
        base = NORMALIZED_BASE[pid]
        if base in desc.predicates_unavailable:
            out["predicates_normalized"][pid] = {
                "disposition": "PREDICATE_UNAVAILABLE",
                "reason": desc.predicates_unavailable[base],
            }
            continue
        out["predicates_normalized"][pid] = _score_predicate(fn, base, succ, fail, len(scored))

    out["arm2_decision"] = _arm2_decision(out)
    return out


def _normalization_audit(hits: dict[str, int]) -> dict[str, Any]:
    """Expose how aggressively normalization fired, so over-normalization is
    visible rather than hidden behind an improved instability number."""
    before = hits.get("_chars_before", 0)
    after = hits.get("_chars_after", 0)
    rules = {k: v for k, v in sorted(hits.items()) if not k.startswith("_")}
    return {
        "chars_before": before,
        "chars_after": after,
        "chars_removed_fraction": (round((before - after) / before, 6) if before else None),
        "rule_hits": rules,
        "rules_fired": len(rules),
    }


def _score_predicate(
    fn: Callable[[Trial], int | None],
    eligibility_id: str,
    succ: list[Trial],
    fail: list[Trial],
    n_scored: int,
) -> dict[str, Any]:
    el_succ = [t for t in succ if _eligible(t, eligibility_id)]
    el_fail = [t for t in fail if _eligible(t, eligibility_id)]
    # evaluate each predicate exactly once per trial
    fired_fail = [(t, fn(t)) for t in el_fail]
    fired_succ = [(t, fn(t)) for t in el_succ]
    nf = sum(1 for _, k in fired_fail if k is not None)
    ns = sum(1 for _, k in fired_succ if k is not None)
    pos = sorted(round(k / len(t.actions), 3) for t, k in fired_fail if k is not None)
    reasons = []
    if len(el_fail) < MIN_N:
        reasons.append(f"eligible failed runs {len(el_fail)} < MIN_N={MIN_N}")
    if len(el_succ) < MIN_N:
        reasons.append(f"eligible success runs {len(el_succ)} < MIN_N={MIN_N}")
    return {
        "disposition": "COMPUTED",
        "n_eligible_fail": len(el_fail),
        "n_eligible_success": len(el_succ),
        "n_ineligible_excluded": n_scored - len(el_fail) - len(el_succ),
        "fire_count_fail": nf,
        "fire_rate_on_failed_runs": _rate(nf, len(el_fail)),
        "false_positive_count": ns,
        "false_positive_on_later_success": _rate(ns, len(el_succ)),
        "kstar_normalized_position_on_failures": {
            "n": len(pos),
            "min": pos[0] if pos else None,
            "median": pos[len(pos) // 2] if pos else None,
            "max": pos[-1] if pos else None,
        },
        "rate_null_reason": "; ".join(reasons) or None,
    }


def _arm2_decision(out: dict[str, Any]) -> dict[str, Any]:
    """Apply the PREREGISTERED arm-2 decision rule. Fixed before any arm-2
    outcome was computed; see ARM2_PREREGISTRATION_TEXT."""
    raw = out["observation_digest_stability"]["instability_rate"]
    norm = out["observation_digest_stability_normalized"]["instability_rate"]
    findings: list[str] = []
    verdict = "INCONCLUSIVE"

    over_normalized = False
    for pid, p in out["predicates_normalized"].items():
        if p.get("disposition") != "COMPUTED":
            continue
        base = out["predicates"].get(NORMALIZED_BASE[pid], {})
        raw_fpr = base.get("false_positive_on_later_success")
        norm_fpr = p.get("false_positive_on_later_success")
        if raw_fpr == 0.0 and norm_fpr is not None and norm_fpr > ARM2_ACCEPT_FPR_MAX:
            over_normalized = True
            findings.append(
                f"{pid}: FPR rose {raw_fpr} -> {norm_fpr} above "
                f"{ARM2_ACCEPT_FPR_MAX}; normalization is over-aggressive"
            )

    fixed_proxy = (
        raw is not None
        and norm is not None
        and raw > ARM2_RAW_INSTABILITY_TRIGGER
        and norm < ARM2_ACCEPT_INSTABILITY_MAX
    )
    precision_held = all(
        (p.get("false_positive_on_later_success") or 0.0) <= ARM2_ACCEPT_FPR_MAX
        for p in out["predicates_normalized"].values()
        if p.get("disposition") == "COMPUTED"
    )

    if over_normalized:
        verdict = "REJECT_OVER_NORMALIZED"
    elif fixed_proxy and precision_held:
        verdict = "ACCEPT"
        findings.append(
            f"instability {raw} -> {norm} (below {ARM2_ACCEPT_INSTABILITY_MAX}) "
            f"with all normalized FPR <= {ARM2_ACCEPT_FPR_MAX}"
        )
    else:
        if raw is not None and raw <= ARM2_RAW_INSTABILITY_TRIGGER:
            findings.append(
                f"raw instability {raw} did not exceed the "
                f"{ARM2_RAW_INSTABILITY_TRIGGER} trigger; this corpus cannot "
                "accept or reject the normalization"
            )
        elif norm is not None and norm >= ARM2_ACCEPT_INSTABILITY_MAX:
            findings.append(
                f"normalization reduced instability {raw} -> {norm} but not below "
                f"{ARM2_ACCEPT_INSTABILITY_MAX}; proxy still unusable"
            )
    return {
        "verdict": verdict,
        "instability_raw": raw,
        "instability_normalized": norm,
        "findings": findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Curve 0 k* method validation (read-only)")
    ap.add_argument("--local-runs-root", type=Path, default=None)
    ap.add_argument("--swebench-dir", type=Path, default=None)
    ap.add_argument("--taubench-file", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    descriptors: list[CorpusDescriptor] = []
    results: list[dict[str, Any]] = []

    if args.swebench_dir and (args.swebench_dir / "results.json").exists():
        d, t = load_swebench_sweagent(args.swebench_dir)
        descriptors.append(d)
        results.append(evaluate_corpus(d, t))
    else:
        descriptors.append(
            unavailable(
                "swebench-verified-sweagent-gpt4",
                "swe-bench-submissions S3",
                "local cache absent; re-fetch with the pinned SHA in the report",
            )
        )

    if args.taubench_file and args.taubench_file.exists():
        d, t = load_taubench(args.taubench_file)
        descriptors.append(d)
        results.append(evaluate_corpus(d, t))
    else:
        descriptors.append(
            unavailable(
                "taubench-airline-gpt4o",
                "sierra-research/tau-bench",
                "local cache absent; re-fetch with the pinned SHA in the report",
            )
        )

    if args.local_runs_root and args.local_runs_root.exists():
        d, t = load_local_atif(args.local_runs_root)
        descriptors.append(d)
        results.append(evaluate_corpus(d, t))

    # source-specific unavailable dispositions, verified this session
    descriptors += [
        unavailable(
            "vending-bench-1-2",
            "Andon Labs (arXiv:2502.15840)",
            "CLOSED/UNRELEASED: no public per-step trace dump on GitHub or HuggingFace. "
            "This falsifies the Curve 0 spec's assumption that Vending-Bench was the "
            "ideal substrate; the spec is corrected by this artifact.",
        ),
        unavailable(
            "agentlab-browsergym-tmlr",
            "HF agentlabtraces/agentlabtraces",
            "impractical size: multi-part splits ~42.9 GB each (~207 GB total), no "
            "single-file/granular fetch API. Deferred, not fabricated.",
        ),
        unavailable(
            "osworld-2.0-trajectory",
            "HF xlangai/osworld2.0-trajectory",
            "GATED: HTTP 401 without an authenticated HF token.",
        ),
        unavailable(
            "swe-rebench-openhands",
            "HF nebius/SWE-rebench-openhands-trajectories",
            "single parquet is ~2.08 GB; deferred to a later pass rather than fetched now.",
        ),
    ]

    artifact = {
        "artifact": "curve0-kstar-method-validation",
        "generated_at": datetime.now(UTC).isoformat(),
        "preregistration": {
            "arm1_raw_proxy": {
                "text": PREREGISTRATION_TEXT,
                "digest": _sha256_text(PREREGISTRATION_TEXT),
            },
            "arm2_normalized_proxy": {
                "text": ARM2_PREREGISTRATION_TEXT,
                "digest": _sha256_text(ARM2_PREREGISTRATION_TEXT),
                "decision_thresholds": {
                    "raw_instability_trigger": ARM2_RAW_INSTABILITY_TRIGGER,
                    "accept_instability_max": ARM2_ACCEPT_INSTABILITY_MAX,
                    "accept_fpr_max": ARM2_ACCEPT_FPR_MAX,
                },
            },
            "note": "transcribed from the Curve 0 spec; not third-party timestamped. "
            "Arm 2 predicates, normalization rule set, and decision rule were fixed "
            "before any arm-2 outcome was computed.",
        },
        "scope": {
            "claim_type": "METHOD VALIDATION ONLY",
            "prohibited": [
                "capability claims about any model",
                "pooling effects across corpora",
                "LLM-judged labels",
                "re-running or model calls",
            ],
        },
        "corpora": [asdict(d) for d in descriptors],
        "results": results,
    }
    (args.out / "kstar_validation.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "available": [d.corpus_id for d in descriptors if d.disposition == "available"],
                "unavailable": [d.corpus_id for d in descriptors if d.disposition == "unavailable"],
                "results": len(results),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
