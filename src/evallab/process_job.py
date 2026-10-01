"""Automatic processing for every landed Harbor job (HAR-107 slice A).

One command -- ``evallab process-job <job_dir>`` -- runs the full
post-finalize pipeline over a landed job directory and writes one run
report per trial plus one job report into ``<job>/processed/`` (JSON plus
a short markdown each)::

    <job>/processed/trial-<trial_name>.json
    <job>/processed/trial-<trial_name>.md
    <job>/processed/job.json
    <job>/processed/job.md

The pipeline reuses existing code; this module adds no new detectors
beyond wiring:

* catalog ingest with cost from the proxy ledger: :func:`evallab.ledger.
  build_cost_block` (source ``proxy_ledger_x_pinned_price``) for the job
  block, split evenly across trials exactly like
  :func:`evallab.database.trial_cost_columns`. The self-hosted route is
  zero-priced per token, so ``cost_usd`` stays ``None`` with the ledger's
  reason and a time-based estimate from
  :func:`evallab.execution_contracts.mimo_selfhosted_trial_cost_usd`
  (trial wall time x server rate / concurrency, sandbox excluded) rides
  along as ``cost_estimate_usd``. Missing data is ``None`` with a reason,
  never 0.
* stitching: :mod:`evallab.step_layers` is the one shared stitching
  library (``discover_trajectory_parts`` + ``stitch_steps`` +
  ``coverage_record``). Token sums and step counts here come from its
  stitched unique steps.
* proxy usage: ``tokens_proxy`` comes only from the validated, settled
  ledger, never Harbor's native totals. A job-level ledger is attributable
  to a trial only when the job has exactly one trial; otherwise trial
  usage stays unknown. ``tokens_native`` preserves Harbor's independent
  counters, and ``tokens_steps`` remains the stitched-step sum.
* detectors: :func:`evallab.trial_diagnosis.diagnose_trial` (failure-mode
  taxonomy), :func:`evallab.traj.outline_trajectory` (loop suspicion,
  error counts), and :mod:`evallab.probe03` (Traces probe-03 first-failure
  and attribution tags, identical-command loops, completion handshake and
  confirmation loops, wedged terminal, token-ceiling naming,
  submit-contract and suspect-grader evidence).
* taint candidates: a process-job flag (not a probe-03 rule) combining
  the verifier's ``anti_hack_guard: REJECT`` line (pattern reused from
  probe-03) with remote-content fetches in executed model commands via
  the canonical :mod:`evallab.upstream_fetch` guard (shared with GEPA
  scoring: the task images are offline, so a trial that downloads the
  upstream package or curls the upstream file may have graded something
  other than the agent's own work).

The trial markdown opens with a decision page
(:mod:`evallab.trial_decision`). That page invents no label. It quotes
the probe-03 outcome, the existing grader-gap checks, the taint flags,
and token-flow's last useful edit, and says whose problem the rule is.
``R-ENV-02`` reads as the task. A pass with a fetch or a guard reject is
a taint candidate, not a coordinator ruling.

The runner calls :func:`process_job` automatically when a job finalizes
"""

import datetime as _datetime
import json
from pathlib import Path
from typing import Any

PROCESS_JOB_SCHEMA = "process_job/v1"

#: Remote-fetch detection lives in :mod:`evallab.upstream_fetch` (the
#: canonical answer-leak guard, shared with GEPA scoring); process-job
#: reuses it instead of a second detector. Only the verifier-side guard
#: reject stays here.


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _iter_trial_dirs(job_dir: Path) -> list[Path]:
    """Trial directories directly under a Harbor job directory, by name."""
    from evallab.model_capture import iter_trial_dirs

    return iter_trial_dirs(job_dir)


def _trial_wall_hours(result: dict[str, Any]) -> tuple[float | None, str | None]:
    """Trial wall time in hours from result.json, or (None, reason)."""
    started, finished = result.get("started_at"), result.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None, "result.json carries no started_at/finished_at pair"
    try:
        start = _datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = _datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None, "result.json timestamps do not parse as ISO-8601"
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None, "result.json finished_at precedes started_at"
    return seconds / 3600.0, None


def _trial_concurrency(result: dict[str, Any]) -> int:
    config = result.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    concurrent = agent.get("n_concurrent") if isinstance(agent, dict) else None
    if isinstance(concurrent, int) and not isinstance(concurrent, bool) and concurrent >= 1:
        return concurrent
    return 1


def _step_token_sums(steps: list[Any]) -> dict[str, Any]:
    """Used prompt/completion sums over stitched unique steps.

    Unmetered agent steps are counted, never zero-filled.
    """
    prompt = completion = metered = unmetered = agent_steps = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("source", "")).lower() not in ("agent", "assistant"):
            continue
        agent_steps += 1
        metrics = step.get("metrics")
        prompt_tokens = completion_tokens = None
        if isinstance(metrics, dict):
            raw_prompt, raw_completion = (
                metrics.get("prompt_tokens"),
                metrics.get("completion_tokens"),
            )
            if isinstance(raw_prompt, (int, float)) and not isinstance(raw_prompt, bool):
                prompt_tokens = int(raw_prompt)
            if isinstance(raw_completion, (int, float)) and not isinstance(raw_completion, bool):
                completion_tokens = int(raw_completion)
        if prompt_tokens is None or completion_tokens is None:
            unmetered += 1
            continue
        metered += 1
        prompt += prompt_tokens
        completion += completion_tokens
    total = prompt + completion if metered else None
    return {
        "prompt_tokens": prompt if metered else None,
        "completion_tokens": completion if metered else None,
        "total_tokens": total,
        "metered_steps": metered,
        "unmetered_steps": unmetered,
        "agent_steps": agent_steps,
        "reason": None if metered else "no stitched agent step carries token metrics",
    }


def _shell_commands(agent_seq: list[tuple[str, dict]], info: dict) -> list[tuple[str, Any, str]]:
    """``(doc, step_id, shell text)`` per agent step with proposed commands.

    Shell text is the harness-recorded executed keystrokes when present,
    else the normalizer's replay of the proposed Terminus commands -- never
    the raw message, so model prose about fetching cannot misfire the
    remote-fetch guard.
    """
    from evallab import probe03

    layer_of = info if isinstance(info, dict) else {}
    commands: list[tuple[str, Any, str]] = []
    for doc, step in agent_seq:
        keystrokes: list[str] = []
        layer = (layer_of.get((doc, step.get("step_id"))) or {}).get("layer")
        if isinstance(layer, dict):
            sent = layer.get("executed_keystrokes") or layer.get("keystrokes_sent")
            if isinstance(sent, str) and sent.strip():
                keystrokes = [sent]
            elif isinstance(sent, list):
                keystrokes = [part for part in sent if isinstance(part, str) and part.strip()]
        if not keystrokes:
            keystrokes = [
                part
                for part in probe03._replay_keystrokes(None, str(step.get("message") or ""))
                if part.strip()
            ]
        if keystrokes:
            commands.append((doc, step.get("step_id"), "\n".join(keystrokes)))
    return commands


def _taint_flags(
    agent_seq: list[tuple[str, dict]], info: dict, trial_dir: Path
) -> list[dict[str, Any]]:
    """Taint candidates: guard rejects plus upstream-fetch findings.

    Remote-fetch detection is the canonical :mod:`evallab.upstream_fetch`
    guard (shared with GEPA scoring); only the verifier-side guard reject
    stays here.
    """
    from evallab import probe03
    from evallab.upstream_fetch import detect_upstream_fetch

    flags: list[dict[str, Any]] = []
    try:
        stdout = (trial_dir / "verifier" / "test-stdout.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        stdout = ""
    guard = probe03.GUARD_REJECT_RE.search(stdout)
    if guard:
        writes = probe03.protected_file_writes(agent_seq, guard.group(1))
        flags.append(
            {
                "kind": "guard_reject",
                "rule": "probe03.GUARD_REJECT_RE",
                "evidence": guard.group(0)[:160],
                "guard_mutation_steps": writes,
            }
        )
    commands = _shell_commands(agent_seq, info)
    doc_of = {step_id: doc for doc, step_id, _ in commands}
    findings = detect_upstream_fetch(
        [(step_id if isinstance(step_id, int) else -1, text) for _, step_id, text in commands]
    )
    for finding in findings:
        step_id = finding.step_index if finding.step_index >= 0 else None
        flags.append(
            {
                "kind": "upstream_fetch",
                "rule": f"upstream_fetch:{finding.kind}",
                "evidence": probe03._ref(doc_of.get(step_id, "head"), step_id),
                "command": finding.excerpt[:160],
                "names_task_repo": finding.names_task_repo,
            }
        )
    return flags


def _process_trial(
    trial_dir: Path, job_dir: Path, *, nop_runs_dir: str | None = None
) -> dict[str, Any]:
    """One trial's run report record (JSON-serializable)."""
    from evallab import probe03
    from evallab.step_layers import (
        coverage_record,
        discover_trajectory_parts,
        stitch_steps,
    )

    trial_name = trial_dir.name
    result = _read_json(trial_dir / "result.json") or {}
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_metadata = agent_result.get("metadata")
    agent_metadata = agent_metadata if isinstance(agent_metadata, dict) else {}

    # Shared stitching library: parts -> unique steps -> coverage.
    agent_dir = trial_dir / "agent"
    parts = discover_trajectory_parts(agent_dir) if agent_dir.is_dir() else []
    docs: list[dict[str, Any]] = []
    doc_names: list[str] = []
    for part in parts:
        if not part.readable:
            continue
        try:
            payload = json.loads(part.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            docs.append(payload)
            doc_names.append(part.name)
    unique_steps, stitch_stats = stitch_steps(docs)
    step_lists = {name: doc.get("steps") for name, doc in zip(doc_names, docs, strict=True)}
    coverage = coverage_record(
        parts,
        stitch_stats,
        summarization_count=agent_metadata.get("summarization_count"),
        step_lists=step_lists,
    )
    tokens_steps = _step_token_sums(unique_steps)

    # Reward (probe-02 semantics via the probe03 port).
    reward, scored, reward_source = probe03.read_reward(trial_dir)

    # First-failure + attribution tags (ported Traces probe-03 rules).
    try:
        analysis = probe03.analyze_trial_core(trial_dir, job_dir, nop_runs_dir=nop_runs_dir)
        analysis_error = None
    except Exception as exc:  # noqa: BLE001 -- one bad trial must not kill the job
        analysis = None
        analysis_error = f"{type(exc).__name__}: {exc}"

    # Loop suspicion + outline counts (shared traj outline).
    try:
        from evallab.traj import outline_trajectory

        outline = outline_trajectory(trial_dir)
        loop = outline.loop_suspicion
        loop_record = {
            "score": loop.score,
            "detected": loop.detected,
            "reasons": list(loop.reasons),
            "repeated_command_count": loop.repeated_command_count,
            "repeated_error_count": loop.repeated_error_count,
            "cyclic_patterns_count": loop.cyclic_patterns_count,
        }
        outline_record: dict[str, Any] | None = {
            "total_steps": outline.total_steps,
            "agent_steps": outline.agent_steps,
            "status": outline.status,
            "primary_reward": outline.primary_reward,
            "exception_class": outline.exception_class,
        }
        outline_error = None
    except Exception as exc:  # noqa: BLE001
        loop_record = None
        outline_record = None
        outline_error = f"{type(exc).__name__}: {exc}"

    # Failure-mode taxonomy (existing deterministic diagnosis).
    try:
        from evallab.trial_diagnosis import diagnose_trial

        diagnosis = diagnose_trial(trial_dir)
        diagnosis_record: dict[str, Any] | None = {
            "outcome": diagnosis.outcome,
            "reward": diagnosis.reward,
            "exception_class": diagnosis.exception_class,
            "heuristic_label": diagnosis.heuristic_label,
            "modes": [
                {
                    "mode": str(mode.mode),
                    "step_ids": [str(ref) for ref in mode.step_ids],
                    "excerpt": str(mode.excerpt)[:300],
                }
                for mode in diagnosis.modes
            ],
            "notices": list(diagnosis.notices),
        }
        diagnosis_error = None
    except Exception as exc:  # noqa: BLE001
        diagnosis_record = None
        diagnosis_error = f"{type(exc).__name__}: {exc}"

    # Token-flow analysis (HAR-114): where the input-token budget goes.
    # Best-effort like every other per-trial detector; never fails the job.
    try:
        from evallab.token_flow import analyze_token_flow

        token_flow = analyze_token_flow(trial_dir, job_dir)
        token_flow_error = None
    except Exception as exc:  # noqa: BLE001
        token_flow = None
        token_flow_error = f"{type(exc).__name__}: {exc}"

    # Taint candidates (process-job flag, not a probe-03 rule).
    if analysis is not None:
        taint = _taint_flags(analysis["agent_seq"], analysis["info"], trial_dir)
    else:
        taint = []

    # Stop reason and behavioral findings from the probe-03 analysis.
    if analysis is not None:
        stop_reason = analysis["stop_reason"]
        first_failure = analysis["first_failure"]
        outcome_failure = _jsonable(analysis["outcome_failure"])
        handshake = analysis["handshake"]
        wedge = {
            "keystroke_source": analysis["wedge"]["keystroke_source"],
            "executed_turns": analysis["wedge"]["executed_turns"],
            "states": analysis["wedge"]["states"],
            "stretches": analysis["wedge"]["stretches"],
            "short_stretches": analysis["wedge"]["short_stretches"],
        }
        identical = [
            {
                "start": probe03._ref(run["start_doc"], run["start"]),
                "end": probe03._ref(run["end_doc"], run["end"]),
                "length": run["length"],
            }
            for run in analysis["runs"]
        ]
        secondaries = list(analysis["secondaries"])
        shape_counts = dict(analysis["shape_counts"])
        acceptance = {
            "counts": dict(analysis["acc_counts"]),
            "provenance": dict(analysis["acceptance_provenance"]),
            "agreement": dict(analysis["acceptance_agreement"]),
        }
        rejection_causes = {
            doc: dict(causes) for doc, causes in analysis["rejection_causes"].items()
        }
        agent_steps = analysis["agent_steps"]
        model_steps = analysis["model_steps"]
        assembly_pattern = analysis["coverage"]["assembly_pattern"]
        confirmation = analysis["confirmation_loop"]
        claim_regime = analysis["claim_regime"]
        completion_refs = list(analysis["completion_refs"])
        loop_cost = analysis["loop_cost"]
        task_name = result.get("task_name") or "unknown"
        model_name = ((result.get("config") or {}).get("agent") or {}).get(
            "model_name"
        ) or "unknown"
    else:
        stop_reason = None
        first_failure = None
        outcome_failure = None
        handshake = None
        wedge = None
        identical = []
        secondaries = []
        shape_counts = {}
        acceptance = {}
        rejection_causes = {}
        agent_steps = None
        model_steps = None
        assembly_pattern = None
        confirmation = None
        claim_regime = None
        completion_refs = []
        loop_cost = None
        task_name = result.get("task_name") or "unknown"
        model_name = "unknown"

    # Cost: job ledger block split across trials happens at the job level;
    # the per-trial record carries the proxy totals for reference.
    wall_hours, wall_reason = _trial_wall_hours(result)
    record: dict[str, Any] = {
        "schema": PROCESS_JOB_SCHEMA,
        "trial_name": trial_name,
        "task_name": task_name,
        "model_name": model_name,
        "reward": reward,
        "scored": scored,
        "reward_source": reward_source,
        "stop_reason": stop_reason,
        "tokens_steps": tokens_steps,
        "tokens_native": {
            "input_tokens": agent_result.get("n_input_tokens"),
            "output_tokens": agent_result.get("n_output_tokens"),
            "source": "result.json#agent_result",
        },
        "tokens_proxy": None,  # attached once the job ledger's scope is known
        "tokens_attempted_proxy": None,  # attributable ledger ceiling, never a split
        "cost_usd": None,  # filled at job level (ledger split)
        "cost_attempted_usd": None,  # filled at job level (ledger split)
        "cost_source": "proxy_ledger_x_pinned_price",
        "cost_reason": None,  # filled at job level (ledger reason)
        "cost_estimate_usd": None,  # self-hosted time-based estimate
        "cost_estimate_reason": None,
        "trial_wall_hours": wall_hours,
        "trial_wall_reason": wall_reason,
        "trial_concurrency": _trial_concurrency(result),
        "stitched_steps": len(unique_steps),
        "agent_steps": agent_steps,
        "model_steps": model_steps,
        "assembly_pattern": assembly_pattern,
        "coverage": coverage,
        "first_failure": first_failure,
        "outcome_failure": outcome_failure,
        "secondaries": secondaries,
        "identical_runs": identical,
        "completion_refs": completion_refs,
        "claim_regime": claim_regime,
        "confirmation_loop": confirmation,
        "handshake": handshake,
        "wedge": wedge,
        "loop_cost": loop_cost,
        "loop_suspicion": loop_record,
        "diagnosis": diagnosis_record,
        "shape_counts": shape_counts,
        "acceptance": acceptance,
        "rejection_causes": rejection_causes,
        "taint": taint,
        "outline": outline_record,
        "token_flow": token_flow,
        "errors": {
            "analysis": analysis_error,
            "outline": outline_error,
            "diagnosis": diagnosis_error,
            "token_flow": token_flow_error,
        },
    }

    # Flags: one short string per fired detector for the summary table.
    flags: list[str] = []
    if stop_reason is not None and stop_reason.startswith("ceiling:"):
        flags.append(f"token_ceiling:{stop_reason.partition(':')[2]}")
    if stop_reason == "agent_timeout":
        flags.append("timeout")
    if identical:
        longest = max(identical, key=lambda run: run["length"])
        flags.append(f"identical_loop:{longest['length']}x:{longest['start']}-{longest['end']}")
    if loop_record is not None and loop_record["detected"]:
        flags.append(f"loop_suspicion:{loop_record['score']:.2f}")
    if handshake is not None:
        flags.append(
            f"completion_handshake:{handshake['first_prompt_ref']}"
            f":{handshake['echo_task_complete_turns']}x-echo"
        )
    if confirmation is not None:
        flags.append(f"confirmation_loop:{confirmation['rule_id']}:{confirmation['detector']}")
    if claim_regime is not None and outcome_failure is not None:
        flags.append(f"claim_regime:{claim_regime['steps']}-steps")
    if wedge is not None and wedge["stretches"]:
        flags.append(f"wedged_terminal:{len(wedge['stretches'])}-stretches")
    if analysis is not None and analysis["livelock"] is not None:
        flags.append("context_livelock")
    if outcome_failure is not None:
        flags.append(f"outcome:{outcome_failure['rule_id']}:{outcome_failure['attribution']}")
    if first_failure is not None:
        flags.append(f"first:{first_failure['rule_id']}:{first_failure['attribution']}")
    if shape_counts.get("unparseable"):
        flags.append(f"parse_error_shapes:{shape_counts['unparseable']}")
    if taint:
        kinds = sorted({flag["kind"] for flag in taint})
        flags.append(f"taint_candidate:{'+'.join(kinds)}")
    if diagnosis_record is not None and diagnosis_record["modes"]:
        modes = ",".join(str(mode["mode"]) for mode in diagnosis_record["modes"])
        flags.append(f"diagnosis:{modes}")
    if completion_refs and stop_reason != "task_complete_confirmed":
        flags.append("claimed_unconfirmed")
    if stop_reason == "task_complete_confirmed":
        flags.append("completed")
    if token_flow is not None:
        from evallab.token_flow import trial_flags as _token_flow_flags

        flags.extend(_token_flow_flags(token_flow))
    record["flags"] = flags
    # The decision page is attached in process_job after counts land, via
    # _attach_decision: one clean path with the real counts field, never a
    # provisional page built without it. Direct _process_trial callers get
    # decision None until attached.
    record["grader_evidence"] = (analysis or {}).get("grader_evidence") if analysis else None
    record["decision"] = None
    return record


def _job_task_identity(job_dir: Path) -> dict[str, Any | None]:
    """Canonical task identity for counts, read once per job.

    ``experiment-spec.json`` carries the optional ``task_id`` (explicit
    canonical identity; avoids variant display-name aliases) and
    ``task_package_digest``. Either may be absent on older jobs: counts
    then keeps its legacy policy and reports the ledger unmatched.
    """
    spec = _read_json(job_dir / "experiment-spec.json") or {}
    digest = spec.get("task_package_digest")
    task_id = spec.get("task_id")
    return {
        "task_id": task_id.strip() if isinstance(task_id, str) and task_id.strip() else None,
        "task_package_digest": digest.strip()
        if isinstance(digest, str) and digest.strip()
        else None,
    }


def _attach_trial_counts(
    record: dict[str, Any],
    result: dict[str, Any],
    *,
    label_root: Path | None,
    task_identity: dict[str, Any | None],
) -> dict[str, Any]:
    """Attach counts with the canonical task identity (HAR-131 cutover)."""
    from evallab.counts import attach_counts

    return attach_counts(
        record,
        result,
        label_root=label_root,
        package_digest=task_identity.get("task_package_digest"),
        task_id=task_identity.get("task_id"),
    )


def _attach_decision(record: dict[str, Any], trial_dir: Path) -> None:
    """Build the trial-decision page in place, after counts are attached."""
    from evallab.trial_decision import build_decision

    try:
        record["decision"] = build_decision(
            trial_dir,
            reward=record.get("reward"),
            scored=bool(record.get("scored")),
            outcome=record.get("outcome_failure")
            if isinstance(record.get("outcome_failure"), dict)
            else None,
            first_failure=record.get("first_failure")
            if isinstance(record.get("first_failure"), dict)
            else None,
            grader_evidence=record.get("grader_evidence")
            if isinstance(record.get("grader_evidence"), dict)
            else None,
            taint=record.get("taint") if isinstance(record.get("taint"), list) else [],
            token_flow=record.get("token_flow")
            if isinstance(record.get("token_flow"), dict)
            else None,
            stop_reason=record.get("stop_reason"),
            calls=record.get("agent_steps"),
            tokens=record.get("tokens_proxy")
            if isinstance(record.get("tokens_proxy"), dict)
            else None,
            counts=record.get("counts") if isinstance(record.get("counts"), dict) else None,
        )
        decision_error = None
    except Exception as exc:  # noqa: BLE001 -- one bad trial must not kill the job
        record["decision"] = None
        decision_error = f"{type(exc).__name__}: {exc}"
    record["errors"]["decision"] = decision_error


def _jsonable(value: Any) -> Any:
    """Convert info-keyed structures to JSON-serializable values."""
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _render_trial_markdown(record: dict[str, Any]) -> str:
    """Short markdown run report for one trial, decision page first."""
    from evallab.trial_decision import render_decision_markdown

    outcome = record.get("outcome_failure") or {}
    first = record.get("first_failure") or {}
    lines = [
        f"# Run report: `{record['trial_name']}`",
        "",
    ]
    lines.extend(render_decision_markdown(record.get("decision")))
    lines.extend(
        [
            "## Record",
            "",
            f"- task: `{record['task_name']}`; model: `{record['model_name']}`",
            f"- reward: `{record['reward']}` (scored={record['scored']}, {record['reward_source']})",
            _counts_line(record),
            f"- stop reason: `{record['stop_reason']}`",
            _tokens_line(record),
            _cost_line(record),
            f"- steps: stitched {record['stitched_steps']}"
            + (
                f" (agent {record['agent_steps']}, model {record['model_steps']}"
                f", {record['assembly_pattern']})"
                if record["agent_steps"] is not None
                else ""
            ),
            f"- first failure: `{first.get('rule_id', 'none')}`"
            + (
                f" ({first.get('attribution')}) at `{first.get('step_ref')}`"
                if first
                else " (clean execution)"
            ),
            f"- outcome: `{outcome.get('rule_id', 'none')}`"
            + (
                f" ({outcome.get('attribution')}): {outcome.get('note', '')[:220]}"
                if outcome
                else ""
            ),
            f"- flags: {', '.join(f'`{flag}`' for flag in record['flags']) or 'none'}",
        ]
    )
    if record.get("token_flow") is not None:
        from evallab.token_flow import markdown_lines as _token_flow_lines

        lines.extend(_token_flow_lines(record["token_flow"]))
    errors = {key: val for key, val in (record.get("errors") or {}).items() if val}
    if errors:
        lines.append(
            "- processing gaps: " + "; ".join(f"{key}: {val}" for key, val in errors.items())
        )
    lines.append("")
    return "\n".join(lines)


def _counts_line(record: dict[str, Any]) -> str:
    """Counts verdict. The reward line above it is not rewritten."""
    counts = record.get("counts") or {}
    reasons = ", ".join(counts.get("reasons") or []) or "none"
    return f"- counts: `{counts.get('verdict')}` ({reasons})"


def _tokens_line(record: dict[str, Any]) -> str:
    steps = record.get("tokens_steps") or {}
    native = record.get("tokens_native") or {}
    proxy = record.get("tokens_proxy") or {}
    return (
        f"- tokens: step sum `{steps.get('total_tokens')}` "
        f"({steps.get('prompt_tokens')}/{steps.get('completion_tokens')}); "
        f"Harbor native `{native.get('input_tokens')}/{native.get('output_tokens')}`; "
        f"proxy-settled `{proxy.get('input_tokens')}/{proxy.get('output_tokens')}` "
        f"({proxy.get('attribution')}; {proxy.get('reason') or proxy.get('path')}); "
        f"attempted ceiling `{record.get('tokens_attempted_proxy')}` "
        "(settled usage plus unresolved reservations; same attribution)"
    )


def _cost_line(record: dict[str, Any]) -> str:
    cost, reason = record.get("cost_usd"), record.get("cost_reason")
    estimate, estimate_reason = record.get("cost_estimate_usd"), record.get("cost_estimate_reason")
    line = f"- cost: `{cost}` ({record.get('cost_source')}"
    line += f"; {reason}" if reason else ""
    line += ")"
    if estimate is not None:
        line += f"; self-hosted time estimate `${estimate:.4f}`"
    elif estimate_reason:
        line += f"; estimate unavailable: {estimate_reason}"
    return line


def _job_ledger_block(job_dir: Path) -> dict[str, Any]:
    """Job-level cost block from the proxy ledger (cost-at-finalize shape)."""
    from evallab.ledger import build_cost_block

    lab = _read_json(job_dir / "lab-metadata.json") or {}
    provider_usage = lab.get("provider_usage")
    block = build_cost_block(provider_usage if isinstance(provider_usage, dict) else None)
    totals: dict[str, Any] = {}
    if isinstance(provider_usage, dict):
        try:
            from evallab.ledger import split_usage

            split = split_usage(provider_usage)
            totals = {
                "used": split["used"],
                "attempted": split["attempted"],
                "unresolved_requests": split["unresolved_requests"],
            }
        except ValueError as exc:
            totals = {"error": f"proxy ledger unreadable: {exc}"}
    return {"block": block, "totals": totals}


def _trial_proxy_tokens(totals: dict[str, Any], n_trials: int) -> dict[str, Any]:
    """Attribute settled job usage without pretending it was metered per trial."""
    tokens: dict[str, Any] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "source": "proxy_settled_ledger",
        "path": "lab-metadata.json#provider_usage",
        "scope": "job",
        "attribution": "unavailable",
        "reason": None,
    }
    used = totals.get("used")
    if not isinstance(used, dict):
        tokens["reason"] = totals.get("error") or "proxy ledger missing"
    elif n_trials != 1:
        tokens["reason"] = (
            f"job ledger covers {n_trials} trials without per-trial attribution"
        )
    else:
        tokens.update(
            input_tokens=used["input_tokens"],
            output_tokens=used["output_tokens"],
            total_tokens=used["total_tokens"],
            attribution="single_trial",
        )
    return tokens


def _selfhosted_estimate(
    record: dict[str, Any], result: dict[str, Any]
) -> tuple[float | None, str | None]:
    """Time-based cost estimate for zero-priced self-hosted trials.

    Returns (estimate, None), or (None, reason) when the trial is not a
    self-hosted trial or the wall time is unknown. The sandbox input is
    unavailable at finalize, so the estimate excludes it (like the
    ledger's own zero-priced reason states).
    """
    from evallab.execution_contracts import (
        is_mimo_selfhosted_model,
        mimo_selfhosted_trial_cost_usd,
    )

    config = result.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    model = agent.get("model_name") if isinstance(agent, dict) else None
    if not is_mimo_selfhosted_model(model):
        return None, "not a self-hosted route trial"
    wall_hours = record.get("trial_wall_hours")
    if not isinstance(wall_hours, (int, float)):
        return None, record.get("trial_wall_reason") or "trial wall time unknown"
    try:
        estimate = mimo_selfhosted_trial_cost_usd(
            float(wall_hours), record.get("trial_concurrency") or 1, 0.0
        )
    except ValueError as exc:
        return None, str(exc)
    return estimate, None


def _render_job_markdown(report: dict[str, Any]) -> str:
    """Short markdown job report with the per-trial summary table."""
    summary = report.get("summary") or {}
    lines = [
        f"# Job report: `{report['job_name']}`",
        "",
        f"- trials: {summary.get('n_trials')} "
        f"(pass {summary.get('n_pass')}, fail {summary.get('n_fail')}, "
        f"unscored {summary.get('n_unscored')})",
        f"- counts: pass {summary.get('n_counted_pass')}, "
        f"fail {summary.get('n_counted_fail')}, "
        f"excluded {summary.get('n_excluded')} ({summary.get('excluded_reasons') or {}})",
        f"- stop reasons: {summary.get('stop_reasons') or 'none'}",
        f"- tokens: step-sum used `{summary.get('tokens_used')}` "
        f"vs attributable attempted ceiling `{summary.get('tokens_attempted')}`",
        f"- cost: `{summary.get('cost_usd')}` ({summary.get('cost_source')})"
        + (
            f"; self-hosted time estimate `${summary.get('cost_estimate_usd', 0):.4f}`"
            if summary.get("cost_estimate_usd") is not None
            else ""
        ),
        f"- ingest: {summary.get('ingest')}",
        "",
        "| trial | reward | verdict | stop reason | tokens used/attempted | cost | flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report.get("trials") or []:
        tokens = f"{row.get('tokens_used')}/{row.get('tokens_attempted')}"
        lines.append(
            f"| `{row['trial_name']}` | {row.get('reward')} | `{row.get('verdict')}` "
            f"| `{row.get('stop_reason')}` | {tokens} | {row.get('cost')} "
            f"| {', '.join(f'`{flag}`' for flag in row.get('flags') or [])} |"
        )
    lines.append("")
    return "\n".join(lines)


def process_job(
    job_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    root: str | Path | None = None,
    database_url: str | None = None,
    ingest: bool = True,
    nop_runs_dir: str | None = None,
    publish: bool = True,
    results_home: str | Path | None = None,
    pr_lookup: Any = None,
) -> dict[str, Any]:
    """Process a landed Harbor job directory.

    Writes per-trial and job reports under ``output_dir`` (default
    ``<job>/processed/``) and ingests the job into the catalog unless
    ``ingest`` is False. Returns the JSON-serializable job report. A
    missing catalog raises nothing: the ingest outcome (or skip) is
    recorded in the report.
    """
    job_path = Path(job_dir).resolve()
    if not job_path.is_dir():
        raise ValueError(f"Not a job directory: {job_dir}")
    out_dir = Path(output_dir).resolve() if output_dir is not None else job_path / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(root).resolve() if root is not None else job_path.parent
    from evallab.counts import find_label_root, summarize_counts

    label_root = find_label_root(repo_root) or find_label_root(job_path)

    trials = _iter_trial_dirs(job_path)
    ledger = _job_ledger_block(job_path)
    block = ledger["block"]
    totals = ledger["totals"]
    n_trials = max(1, len(trials))

    job_cost = block.get("cost_usd")
    job_attempted = block.get("attempted_cost_usd")
    attempted_input = (totals.get("attempted") or {}).get("input_tokens")
    attempted_output = (totals.get("attempted") or {}).get("output_tokens")

    trial_reports: list[dict[str, Any]] = []
    task_identity = _job_task_identity(job_path)
    for trial_path in trials:
        record = _process_trial(trial_path, job_path, nop_runs_dir=nop_runs_dir)
        trial_result = _read_json(trial_path / "result.json") or {}
        record["tokens_proxy"] = _trial_proxy_tokens(totals, len(trials))
        # Job ledger split across trials (same convention as
        # database.trial_cost_columns: daily sums still equal the ledger).
        record["cost_usd"] = job_cost / n_trials if isinstance(job_cost, (int, float)) else None
        record["cost_attempted_usd"] = (
            job_attempted / n_trials if isinstance(job_attempted, (int, float)) else None
        )
        record["cost_reason"] = block.get("reason")
        # Attempted ceiling includes settled usage and unresolved reservations.
        # Like settled tokens, a job ledger is attributable only for one trial.
        used_input = (totals.get("used") or {}).get("input_tokens")
        used_output = (totals.get("used") or {}).get("output_tokens")
        if (
            n_trials == 1
            and isinstance(used_input, int)
            and isinstance(used_output, int)
            and isinstance(attempted_input, int)
            and isinstance(attempted_output, int)
        ):
            record["tokens_attempted_proxy"] = (
                used_input + used_output + attempted_input + attempted_output
            )
        estimate, estimate_reason = _selfhosted_estimate(record, trial_result)
        record["cost_estimate_usd"] = estimate
        record["cost_estimate_reason"] = (
            "excludes sandbox and warm periods; time-based only"
            if estimate is not None
            else estimate_reason
        )
        record["task_package_digest"] = task_identity.get("task_package_digest")
        record["counts"] = _attach_trial_counts(
            record, trial_result, label_root=label_root, task_identity=task_identity
        )
        _attach_decision(record, trial_path)
        trial_reports.append(record)
        trial_file = out_dir / f"trial-{trial_path.name}.json"
        trial_file.write_text(
            json.dumps(_jsonable(record), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / f"trial-{trial_path.name}.md").write_text(
            _render_trial_markdown(record), encoding="utf-8"
        )

    # Job aggregates. Unknown stays unknown: sums cover only trials that
    # measured a value, with an explicit measured count.
    scored = [record for record in trial_reports if record["scored"]]
    measured_cost = [
        record["cost_usd"]
        for record in trial_reports
        if isinstance(record["cost_usd"], (int, float))
    ]
    measured_attempted = [
        record["tokens_attempted_proxy"]
        for record in trial_reports
        if isinstance(record["tokens_attempted_proxy"], int)
    ]
    measured_used = [
        record["tokens_steps"]["total_tokens"]
        for record in trial_reports
        if isinstance((record["tokens_steps"] or {}).get("total_tokens"), int)
    ]
    measured_estimate = [
        record["cost_estimate_usd"]
        for record in trial_reports
        if isinstance(record["cost_estimate_usd"], (int, float))
    ]
    stop_histogram: dict[str, int] = {}
    for record in trial_reports:
        stop_histogram[str(record["stop_reason"])] = (
            stop_histogram.get(str(record["stop_reason"]), 0) + 1
        )

    ingest_note: str
    if ingest:
        try:
            from evallab import database
            from evallab.results import load_job
            from evallab.runner import database_url_from_environment

            url = database_url_from_environment(database_url)
            job_record = load_job(job_path)
            database.initialize(url)
            database.ingest(url, [job_record], root=repo_root)
            ingest_note = f"ingested into {database.identity(url)}"
        except Exception as exc:  # noqa: BLE001 -- ingest gaps are recorded, not raised
            ingest_note = f"ingest skipped: {type(exc).__name__}: {exc}"
    else:
        ingest_note = "ingest disabled by caller"

    rows = [
        {
            "trial_name": record["trial_name"],
            "reward": record["reward"],
            "verdict": (record.get("counts") or {}).get("verdict"),
            "reasons": (record.get("counts") or {}).get("reasons") or [],
            "stop_reason": record["stop_reason"],
            "tokens_used": (record["tokens_steps"] or {}).get("total_tokens"),
            "tokens_attempted": record["tokens_attempted_proxy"],
            "cost": record["cost_usd"],
            "flags": record["flags"],
        }
        for record in trial_reports
    ]
    counts_summary = summarize_counts(trial_reports)
    report: dict[str, Any] = {
        "schema": PROCESS_JOB_SCHEMA,
        "job_name": job_path.name,
        "job_dir": str(job_path),
        "ledger": ledger,
        "trials": rows,
        "summary": {
            "n_trials": len(trial_reports),
            "n_pass": sum(1 for record in scored if (record["reward"] or 0) >= 1.0),
            "n_fail": sum(1 for record in scored if (record["reward"] or 0) < 1.0),
            "n_unscored": sum(1 for record in trial_reports if not record["scored"]),
            "stop_reasons": stop_histogram,
            "tokens_used": sum(measured_used) if measured_used else None,
            "tokens_used_measured": len(measured_used),
            "tokens_attempted": sum(measured_attempted) if measured_attempted else None,
            "tokens_attempted_measured": len(measured_attempted),
            "cost_usd": sum(measured_cost) if measured_cost else None,
            "cost_measured": len(measured_cost),
            "cost_source": block.get("source"),
            "cost_reason": block.get("reason"),
            "cost_estimate_usd": sum(measured_estimate) if measured_estimate else None,
            "cost_estimate_measured": len(measured_estimate),
            "ingest": ingest_note,
            "n_counted_pass": counts_summary["n_counted_pass"],
            "n_counted_fail": counts_summary["n_counted_fail"],
            "n_excluded": counts_summary["n_excluded"],
            "excluded_reasons": counts_summary["excluded_reasons"],
        },
    }
    # The job report lands in processed/ BEFORE the publish copies the tree:
    # publish_job snapshots the source, so publishing first would copy a
    # processed/ without job.json and the INDEX row would read "unprocessed".
    report["results_home"] = None
    (out_dir / "job.json").write_text(
        json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "job.md").write_text(_render_job_markdown(report), encoding="utf-8")
    if publish:
        from evallab.results_home import publish_job

        published = publish_job(
            job_path,
            root=results_home,
            repo_root=Path(root).resolve() if root is not None else None,
            pr_lookup=pr_lookup,
        )
        report["results_home"] = published["published"]
        (out_dir / "job.json").write_text(
            json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report
