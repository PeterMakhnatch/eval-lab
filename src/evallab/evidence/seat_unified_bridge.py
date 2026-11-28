from __future__ import annotations

import json
import math
import subprocess
import sys
from collections import Counter, defaultdict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from worldwatch.detectors import run_detectors
from worldwatch.validity import classify_trial_validity

from ._multiagent_io import safe_finite_float, sanitize_json_value, sanitize_utf8_text
from ._seat_bridge_html import build_bridge_html_document
from .seat_cda import JobCdaReport, TrialCdaReport, analyze_cda
from .seat_decompose import decompose_coordination
from .seat_forensics import JobForensicsReport, TrialForensicsReport, analyze_forensics

BRIDGE_SCHEMA = "seat-unified-bridge/v1"
_MAX_FINITE_NODES = 2_000_000
_SUBPROCESS_TIMEOUT_SEC = 120.0


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BridgeForensicsRollup(_StrictModel):
    messages_posted: int = 0
    messages_fetched: int = 0
    messages_in_context: int = 0
    messages_acted_on: int = 0
    unread_at_action: int = 0
    never_fetched: int = 0
    duplicate_target_actions: int = 0
    stale_read_write_races: int = 0
    post_end_of_play_events: int = 0
    failure_mode_counts: dict[str, int] = Field(default_factory=dict)
    logs_complete: int = 0
    cost_complete: int = 0
    event_log_present: int = 0


class BridgeCdaRollup(_StrictModel):
    messages_posted: int = 0
    messages_fetched: int = 0
    avoided_duplicate_targets: int = 0
    triggered_actions: int = 0
    ignored_messages: int = 0
    sender_causal_credit: dict[str, float] = Field(default_factory=dict)
    claim_locks_granted: int = 0
    claim_rejections_saved_dups: int = 0
    unread_blocks_count: int = 0
    post_block_fetch_compliance_rate: float | None = None
    hash_chain_checked_trials: int = 0
    hash_chain_valid_trials: int = 0
    hash_chain_tampered_trials: int = 0


class BridgeDetectorStat(_StrictModel):
    trials_flagged: int
    total_hits: int
    rate: float


class BridgeDetectorsRollup(_StrictModel):
    trials_scanned: int
    detector_names: list[str]
    summary: dict[str, BridgeDetectorStat]


class BridgeValidityRollup(_StrictModel):
    total_trials: int
    measured: int
    unmeasured: int
    unmeasured_rate: float
    by_reason: dict[str, int]


class BridgeTidegaugeAudit(_StrictModel):
    available: bool
    error: str | None = None
    stall_seconds: float
    arms: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    worst: str | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)


class BridgeMusterRegrade(_StrictModel):
    available: bool
    reason: str | None = None
    trials: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    trials_with_late_actions: int = 0
    chain_checked: int = 0
    chain_failures: int = 0


class BridgeDecomposePair(_StrictModel):
    label: str
    seat_decompose: dict[str, Any]
    tidegauge_decompose: dict[str, Any] | None = None


class UnifiedBridgeTrial(_StrictModel):
    trial_key: str
    trial_name: str
    trial_id: str | None = None
    trial_dir: str
    task_name: str | None = None
    ecosystem: str
    ww_ecosystem: str | None = None
    seats: list[str] = Field(default_factory=list)
    rewards: dict[str, float] = Field(default_factory=dict)
    reward: float | None = None
    reward_valid: bool | None = None
    total_cost_usd: float | None = None
    known_cost_usd: float | None = None
    forensics_funnel: dict[str, Any] = Field(default_factory=dict)
    forensics_collisions: dict[str, Any] = Field(default_factory=dict)
    failure_mode_counts: dict[str, int] = Field(default_factory=dict)
    integrity: dict[str, Any] = Field(default_factory=dict)
    cda: dict[str, Any] = Field(default_factory=dict)
    detectors_triggered: list[str] = Field(default_factory=list)
    detectors_counts: dict[str, int] = Field(default_factory=dict)
    ww_chain_ok: bool | None = None
    ww_chain_error: str | None = None
    validity_measured: bool | None = None
    validity_reason: str | None = None
    muster_regrade: dict[str, Any] | None = None


class UnifiedBridgeReport(_StrictModel):
    schema_version: str = BRIDGE_SCHEMA
    target_path: str
    job_id: str | None = None
    n_trials: int
    ecosystems: dict[str, int]
    discovery_issues: list[str]
    forensics: BridgeForensicsRollup
    cda: BridgeCdaRollup
    detectors: BridgeDetectorsRollup
    validity: BridgeValidityRollup
    tidegauge_audit: BridgeTidegaugeAudit
    tidegauge_split: BridgeTidegaugeAudit | None = None
    muster_regrade: BridgeMusterRegrade
    decompositions: list[BridgeDecomposePair] = Field(default_factory=list)
    trials: list[UnifiedBridgeTrial]


def _assert_all_finite(value: Any) -> None:
    stack: list[Any] = [value]
    visited = 0
    while stack:
        visited += 1
        assert visited <= _MAX_FINITE_NODES, "bridge report exceeds finite scan bound"
        cur = stack.pop()
        if isinstance(cur, float):
            assert math.isfinite(cur), f"non-finite float in bridge report: {cur!r}"
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


def _run_json_tool(
    module: str, args: list[str], *, ok_codes: tuple[int, ...] = (0,)
) -> tuple[dict[str, Any] | None, str | None]:
    cmd = [sys.executable, "-m", module, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{module}: {type(exc).__name__}: {exc}"
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if proc.returncode not in ok_codes:
        return None, stderr or stdout or f"{module} exited {proc.returncode}"
    if not stdout:
        return None, stderr or f"{module} produced no JSON output"
    try:
        parsed = json.loads(stdout)
    except ValueError as exc:
        return None, f"{module} returned invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, f"{module} JSON root is not an object"
    return parsed, None


def _run_tidegauge_audit(
    target: Path, stall_seconds: float, *, split: bool = False
) -> BridgeTidegaugeAudit:
    args = ["audit", str(target), "--stall-seconds", str(stall_seconds), "--json"]
    if split:
        args.insert(1, "--split")
    doc, err = _run_json_tool("tidegauge", args, ok_codes=(0, 1))
    if doc is None:
        return BridgeTidegaugeAudit(available=False, error=err, stall_seconds=stall_seconds)
    return BridgeTidegaugeAudit(
        available=True,
        stall_seconds=float(doc.get("stall_seconds", stall_seconds)),
        arms=list(doc.get("arms") or []),
        counts={str(k): int(v) for k, v in (doc.get("counts") or {}).items()},
        worst=doc.get("worst"),
        findings=list(doc.get("findings") or []),
    )


def _run_muster_regrade(
    target: Path, task_root: Path | None
) -> tuple[BridgeMusterRegrade, dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    args = ["regrade", str(target), "--json"]
    if task_root is not None:
        args.extend(["--task-root", str(task_root)])
    doc, err = _run_json_tool("muster", args, ok_codes=(0, 1, 3))
    if doc is None or not isinstance(doc.get("rows"), list):
        summary = BridgeMusterRegrade(available=False, reason=err or "no regrade rows")
        return summary, {}, {}
    rows = [r for r in doc["rows"] if isinstance(r, dict)]
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tname = str(row.get("trial") or "")
        ttask = str(row.get("task") or "").lower()
        by_name[tname].append(row)
        by_pair[(tname, ttask)].append(row)
    uniq_name = {k: v[0] for k, v in by_name.items() if len(v) == 1}
    uniq_pair = {k: v[0] for k, v in by_pair.items() if len(v) == 1}
    summary = BridgeMusterRegrade(
        available=True,
        trials=int(doc.get("trials", len(rows))),
        counts={str(k): int(v) for k, v in (doc.get("counts") or {}).items()},
        trials_with_late_actions=int(doc.get("trials_with_late_actions", 0)),
        chain_checked=int(doc.get("chain_checked", 0)),
        chain_failures=int(doc.get("chain_failures", 0)),
    )
    return summary, uniq_name, uniq_pair


def _build_forensics_rollup(report: TrialForensicsReport | JobForensicsReport) -> BridgeForensicsRollup:
    if isinstance(report, TrialForensicsReport):
        i = report.integrity
        return BridgeForensicsRollup(
            messages_posted=report.funnel.messages_posted,
            messages_fetched=report.funnel.messages_fetched,
            messages_in_context=report.funnel.messages_in_context,
            messages_acted_on=report.funnel.messages_acted_on,
            unread_at_action=report.funnel.unread_at_action,
            never_fetched=report.funnel.never_fetched,
            duplicate_target_actions=report.collisions.duplicate_target_actions,
            stale_read_write_races=report.collisions.stale_read_write_races,
            post_end_of_play_events=report.collisions.post_end_of_play_events,
            failure_mode_counts=dict(sorted(report.failure_mode_counts.items())),
            logs_complete=int(i.logs_complete),
            cost_complete=int(i.cost_complete),
            event_log_present=int(i.event_log_present),
        )
    s = report.integrity_summary
    return BridgeForensicsRollup(
        messages_posted=report.funnel.messages_posted,
        messages_fetched=report.funnel.messages_fetched,
        messages_in_context=report.funnel.messages_in_context,
        messages_acted_on=report.funnel.messages_acted_on,
        unread_at_action=report.funnel.unread_at_action,
        never_fetched=report.funnel.never_fetched,
        duplicate_target_actions=report.collisions.duplicate_target_actions,
        stale_read_write_races=report.collisions.stale_read_write_races,
        post_end_of_play_events=report.collisions.post_end_of_play_events,
        failure_mode_counts=dict(sorted(report.failure_mode_counts.items())),
        logs_complete=int(s.get("logs_complete", 0)),
        cost_complete=int(s.get("cost_complete", 0)),
        event_log_present=int(s.get("event_log_present", 0)),
    )


def _build_cda_rollup(report: TrialCdaReport | JobCdaReport) -> BridgeCdaRollup:
    g = report.guard_impact
    return BridgeCdaRollup(
        messages_posted=report.messages_posted,
        messages_fetched=report.messages_fetched,
        avoided_duplicate_targets=report.avoided_duplicate_targets,
        triggered_actions=report.triggered_actions,
        ignored_messages=report.ignored_messages,
        sender_causal_credit={str(k): float(v) for k, v in report.sender_causal_credit.items()},
        claim_locks_granted=g.claim_locks_granted,
        claim_rejections_saved_dups=g.claim_rejections_saved_dups,
        unread_blocks_count=g.unread_blocks_count,
        post_block_fetch_compliance_rate=g.post_block_fetch_compliance_rate,
        hash_chain_checked_trials=g.hash_chain_checked_trials,
        hash_chain_valid_trials=g.hash_chain_valid_trials,
        hash_chain_tampered_trials=g.hash_chain_tampered_trials,
    )


def _primary_reward(rewards: dict[str, float] | None, ww_reward: Any) -> float | None:
    if rewards:
        if "reward" in rewards:
            return safe_finite_float(rewards["reward"])
        if len(rewards) == 1:
            return safe_finite_float(next(iter(rewards.values())))
    return safe_finite_float(ww_reward)


def _match_forensics_trials(
    f_trials: list[TrialForensicsReport],
    c_trials: list[TrialCdaReport],
) -> list[tuple[TrialForensicsReport, TrialCdaReport]]:
    assert len(f_trials) == len(c_trials), f"trial count mismatch: {len(f_trials)} vs {len(c_trials)}"
    if all(
        (f.trial_name, f.trial_id or "") == (c.trial_name, c.trial_id or "")
        for f, c in zip(f_trials, c_trials, strict=True)
    ):
        return list(zip(f_trials, c_trials, strict=True))
    buckets: dict[tuple[str, str], deque[TrialForensicsReport]] = defaultdict(deque)
    for f in f_trials:
        buckets[(f.trial_name, f.trial_id or "")].append(f)
    paired: list[tuple[TrialForensicsReport, TrialCdaReport]] = []
    for c in c_trials:
        key = (c.trial_name, c.trial_id or "")
        assert buckets[key], f"missing forensics trial for {key!r}"
        paired.append((buckets[key].popleft(), c))
    return paired


def _match_regrade_row(
    trial: TrialForensicsReport,
    uniq_name: dict[str, dict[str, Any]],
    uniq_pair: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    raw = uniq_name.get(trial.trial_name)
    if raw is None and trial.task_name:
        short_task = trial.task_name.rsplit("/", 1)[-1].lower()
        raw = uniq_pair.get((trial.trial_name, short_task))
    if raw is None:
        return None
    return {
        "status": raw.get("status"),
        "log": raw.get("log"),
        "reason": raw.get("reason"),
        "chain_ok": raw.get("chain_ok"),
        "chain_problem": raw.get("chain_problem"),
        "late_actions_count": len(raw.get("late_actions") or []),
        "differences": raw.get("differences") or {},
    }


def _join_trial_row(
    f_rep: TrialForensicsReport,
    c_rep: TrialCdaReport,
    ww_tr: dict[str, Any] | None,
    val_tr: dict[str, Any] | None,
    regrade_info: dict[str, Any] | None,
) -> UnifiedBridgeTrial:
    t_key = Path(c_rep.trial_dir).resolve().as_posix()
    det_counts = (
        {
            name: int(info.get("count", 0))
            for name, info in (ww_tr.get("detectors") or {}).items()
            if info.get("fired")
        }
        if ww_tr is not None
        else {}
    )
    rewards = dict(f_rep.rewards or {})
    return UnifiedBridgeTrial(
        trial_key=t_key,
        trial_name=f_rep.trial_name,
        trial_id=f_rep.trial_id,
        trial_dir=t_key,
        task_name=f_rep.task_name,
        ecosystem=f_rep.ecosystem,
        ww_ecosystem=str(ww_tr["ecosystem"]) if ww_tr is not None else None,
        seats=list(c_rep.roster),
        rewards=rewards,
        reward=_primary_reward(rewards, ww_tr.get("reward") if ww_tr else None),
        reward_valid=f_rep.reward_valid,
        total_cost_usd=safe_finite_float(f_rep.total_cost_usd),
        known_cost_usd=safe_finite_float(f_rep.known_cost_usd),
        forensics_funnel=f_rep.funnel.model_dump(mode="json"),
        forensics_collisions=f_rep.collisions.model_dump(mode="json"),
        failure_mode_counts=dict(sorted(f_rep.failure_mode_counts.items())),
        integrity=f_rep.integrity.model_dump(mode="json"),
        cda={
            "messages_posted": c_rep.messages_posted,
            "messages_fetched": c_rep.messages_fetched,
            "avoided_duplicate_targets": c_rep.avoided_duplicate_targets,
            "triggered_actions": c_rep.triggered_actions,
            "ignored_messages": c_rep.ignored_messages,
            "sender_causal_credit": dict(c_rep.sender_causal_credit),
            "guard_impact": c_rep.guard_impact.model_dump(mode="json"),
        },
        detectors_triggered=list(ww_tr.get("triggered") or []) if ww_tr is not None else [],
        detectors_counts=det_counts,
        ww_chain_ok=ww_tr.get("chain_ok") if ww_tr is not None else None,
        ww_chain_error=ww_tr.get("chain_error") if ww_tr is not None else None,
        validity_measured=val_tr.get("measured") if val_tr is not None else None,
        validity_reason=val_tr.get("reason") if val_tr is not None else None,
        muster_regrade=regrade_info,
    )


def _run_decompositions(
    specs: Sequence[tuple[str, Path, Path, Path]],
) -> list[BridgeDecomposePair]:
    out: list[BridgeDecomposePair] = []
    for label, solo, silent, team in specs:
        seat_dec = decompose_coordination(solo, silent, team).model_dump(mode="json")
        tg_doc, _ = _run_json_tool(
            "tidegauge",
            ["decompose", str(solo), str(silent), str(team), "--json"],
            ok_codes=(0,),
        )
        out.append(
            BridgeDecomposePair(
                label=sanitize_utf8_text(label),
                seat_decompose=seat_dec,
                tidegauge_decompose=tg_doc,
            )
        )
    return out


def _collect_validity(
    ww_trials: list[dict[str, Any]], stall_seconds: float
) -> tuple[dict[str, dict[str, Any]], BridgeValidityRollup]:
    val_by_path: dict[str, dict[str, Any]] = {}
    measured = 0
    reasons: Counter[str] = Counter()
    for tr in ww_trials:
        tpath = Path(str(tr["path"])).resolve()
        res = classify_trial_validity(tpath, str(tr["ecosystem"]), stall_seconds=stall_seconds)
        val_by_path[tpath.as_posix()] = res
        if res.get("measured"):
            measured += 1
        else:
            reasons[str(res.get("reason") or "unknown")] += 1
    total = len(ww_trials)
    unmeasured = total - measured
    rate = round(unmeasured / total, 6) if total else 0.0
    rollup = BridgeValidityRollup(
        total_trials=total,
        measured=measured,
        unmeasured=unmeasured,
        unmeasured_rate=rate,
        by_reason=dict(sorted(reasons.items())),
    )
    return val_by_path, rollup


def _join_all_trials(
    forensics_rep: TrialForensicsReport | JobForensicsReport,
    cda_rep: TrialCdaReport | JobCdaReport,
    ww_by_path: dict[str, dict[str, Any]],
    val_by_path: dict[str, dict[str, Any]],
    uniq_name: dict[str, dict[str, Any]],
    uniq_pair: dict[tuple[str, str], dict[str, Any]],
) -> list[UnifiedBridgeTrial]:
    f_trials = (
        [forensics_rep] if isinstance(forensics_rep, TrialForensicsReport) else list(forensics_rep.trials)
    )
    c_trials = [cda_rep] if isinstance(cda_rep, TrialCdaReport) else list(cda_rep.trials)
    paired = _match_forensics_trials(f_trials, c_trials)
    joined: list[UnifiedBridgeTrial] = []
    for f_rep, c_rep in paired:
        key = Path(c_rep.trial_dir).resolve().as_posix()
        joined.append(
            _join_trial_row(
                f_rep,
                c_rep,
                ww_by_path.get(key),
                val_by_path.get(key),
                _match_regrade_row(f_rep, uniq_name, uniq_pair),
            )
        )
    joined.sort(key=lambda t: (t.trial_name, t.trial_id or "", t.trial_dir))
    assert len(joined) == len(f_trials)
    return joined


def analyze_unified_bridge(
    target: Path,
    *,
    task_root: Path | None = None,
    stall_seconds: float = 300.0,
    split_arms: bool = False,
    include_regrade: bool = True,
    decompositions: Sequence[tuple[str, Path, Path, Path]] = (),
) -> UnifiedBridgeReport:
    assert isinstance(target, Path)
    assert math.isfinite(stall_seconds) and stall_seconds > 0
    resolved = target.expanduser().resolve()
    forensics_rep = analyze_forensics(resolved)
    cda_rep = analyze_cda(resolved)
    ww_rep = run_detectors(resolved)
    ww_trials = list(ww_rep.get("trials") or [])
    ww_by_path = {Path(str(tr["path"])).resolve().as_posix(): tr for tr in ww_trials}
    val_by_path, val_rollup = _collect_validity(ww_trials, stall_seconds)
    tg_audit = _run_tidegauge_audit(resolved, stall_seconds, split=False)
    tg_split = _run_tidegauge_audit(resolved, stall_seconds, split=True) if split_arms else None
    if include_regrade:
        reg_summary, uniq_name, uniq_pair = _run_muster_regrade(resolved, task_root)
    else:
        reg_summary = BridgeMusterRegrade(available=False, reason="disabled")
        uniq_name, uniq_pair = {}, {}
    joined = _join_all_trials(forensics_rep, cda_rep, ww_by_path, val_by_path, uniq_name, uniq_pair)
    det_rollup = BridgeDetectorsRollup(
        trials_scanned=int(ww_rep["trials_scanned"]),
        detector_names=list(ww_rep["detector_names"]),
        summary={
            str(k): BridgeDetectorStat(**v) for k, v in (ww_rep.get("summary") or {}).items()
        },
    )
    is_single = isinstance(forensics_rep, TrialForensicsReport)
    ecosystems = {forensics_rep.ecosystem: 1} if is_single else dict(sorted(forensics_rep.ecosystems.items()))
    issues = [] if is_single else list(forensics_rep.discovery_issues)
    job_id = None if is_single else forensics_rep.job_id
    raw_report = UnifiedBridgeReport(
        schema_version=BRIDGE_SCHEMA,
        target_path=resolved.as_posix(),
        job_id=job_id,
        n_trials=len(joined),
        ecosystems=ecosystems,
        discovery_issues=issues,
        forensics=_build_forensics_rollup(forensics_rep),
        cda=_build_cda_rollup(cda_rep),
        detectors=det_rollup,
        validity=val_rollup,
        tidegauge_audit=tg_audit,
        tidegauge_split=tg_split,
        muster_regrade=reg_summary,
        decompositions=_run_decompositions(decompositions),
        trials=joined,
    )
    clean_dict = sanitize_json_value(raw_report.model_dump(mode="json"))
    _assert_all_finite(clean_dict)
    return UnifiedBridgeReport.model_validate(clean_dict)


def format_bridge_json(report: UnifiedBridgeReport) -> str:
    dumped = report.model_dump(mode="json")
    _assert_all_finite(dumped)
    return json.dumps(dumped, allow_nan=False, sort_keys=True, indent=2)


def render_bridge_text(report: UnifiedBridgeReport) -> str:
    f, c, v = report.forensics, report.cda, report.validity
    fired = sorted(
        f"{k}={s.trials_flagged}" for k, s in report.detectors.summary.items() if s.trials_flagged > 0
    )
    tg = report.tidegauge_audit
    mr = report.muster_regrade
    lines = [
        f"Unified Forensics Bridge ({report.schema_version}): {report.target_path}",
        f"Trials: {report.n_trials} | Ecosystems: {report.ecosystems} | Validity: {v.measured}/{v.total_trials} measured ({v.unmeasured} unmeasured)",
        f"Forensics funnel: posted={f.messages_posted} fetched={f.messages_fetched} in_ctx={f.messages_in_context} acted={f.messages_acted_on} unread={f.unread_at_action}",
        f"CDA: avoided_dups={c.avoided_duplicate_targets} triggered={c.triggered_actions} ignored={c.ignored_messages} locks={c.claim_locks_granted} saved={c.claim_rejections_saved_dups}",
        f"WorldWatch detectors: {', '.join(fired) if fired else 'none'}",
        f"Tidegauge audit: available={tg.available} worst={tg.worst} counts={tg.counts}",
        f"Muster regrade: available={mr.available} trials={mr.trials} counts={mr.counts} chain={mr.chain_checked} fail={mr.chain_failures}",
    ]
    return "\n".join(lines)


def render_bridge_html(report: UnifiedBridgeReport, *, title: str | None = None) -> str:
    dumped = sanitize_json_value(report.model_dump(mode="json"))
    _assert_all_finite(dumped)
    clean_title = sanitize_utf8_text(title or f"Unified Forensics Bridge: {Path(report.target_path).name}")
    dumped["title"] = clean_title
    payload_json = json.dumps(dumped, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return sanitize_utf8_text(build_bridge_html_document(clean_title, payload_json))


def write_bridge_html(report: UnifiedBridgeReport, out_html: Path, *, title: str | None = None) -> Path:
    html = render_bridge_html(report, title=title)
    dest = out_html.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return dest
