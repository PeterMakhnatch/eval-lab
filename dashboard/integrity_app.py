"""Integrity page rendering: pool-scoped tabs over ``dashboard.integrity`` rows."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from dashboard import integrity as iq
from dashboard.integrity import SourceUnavailable
from dashboard.snapshot_cache import RefreshingCache

Row = dict[str, Any]
SnapshotKey = tuple[str, str | None]
SNAPSHOT_MAX_AGE_S = 300

REPO_ROOT = Path(
    os.environ.get("EVALLAB_DASHBOARD_ROOT", Path(__file__).resolve().parents[1])
).resolve()


def _derived() -> Path | None:
    return iq.resolve_derived(REPO_ROOT)


def load_snapshot(
    repo_root_value: str, explicit_derived_value: str | None
) -> dict[str, Any]:
    """One snapshot: ``{"rows": {...}, "errors": {...}}`` (about 30 s to build)."""
    repo_root = Path(repo_root_value)
    derived = Path(explicit_derived_value) if explicit_derived_value else None
    rows: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def load(label: str, function: Any, fallback: Any = None) -> Any:
        try:
            value = function()
            rows[label] = value
            return value
        except (SourceUnavailable, Exception) as exc:
            errors[label] = str(exc)
            rows[label] = fallback
            return fallback

    source = None
    try:
        source = iq.open_source(repo_root, derived)
    except SourceUnavailable as exc:
        errors["attach"] = str(exc)

    rows["zones"] = iq.zone_notes(source) if source is not None else []
    if source is not None:
        try:
            load("audits", lambda: iq.task_audit_rows(source), [])
            load("findings", lambda: iq.findings_rows(source), [])
            load("table", lambda: iq.task_table(source), [])
            load("nop_tasks", lambda: iq.nop_per_task(source), [])
            load("nop_positive", lambda: iq.nop_positive_rows(source), [])
            load("stability", lambda: iq.stability_rows(source), [])
            load("exploits", lambda: iq.exploits_rows(source), [])
            load("cracks", lambda: iq.exploit_cracks(source), [])
            load("versions", lambda: iq.versions_rows(source), [])
            load("quality", lambda: iq.traj_quality_rows(source), [])
            load("loops", lambda: iq.loop_suspicion_rows(source), [])
            load("trial_facts", lambda: iq.trial_facts_rows(source), [])
            load("identities", lambda: iq.trial_identity_rows(source), [])
            load("gated", lambda: iq.integrity_gated_rows(source), [])
            load("actions", lambda: iq.actions_rows(source), [])
        finally:
            source.close()

    load("ledger", lambda: iq.ledger_counts(repo_root), [])
    load("ledger_rows", lambda: iq.ledger_rows(repo_root), [])
    load("processed_scan", lambda: iq.processed_run_scan(None), ([], {}))
    processed, _meta = rows.get("processed_scan") or ([], {})
    rows["processed"] = processed
    load("oracle", lambda: iq.oracle_labels(None), [])
    load("regrade", lambda: iq.heldout_regrade_rows(None), [])
    load("detectors", lambda: iq.detector_score_rows(None), [])
    load(
        "census",
        lambda: iq.trials_census_rows(repo_root=repo_root, derived_root=derived),
        [],
    )
    rows["errors"] = errors
    return rows


@st.cache_resource(show_spinner=False)
def _snapshots() -> RefreshingCache[SnapshotKey, dict[str, Any]]:
    return RefreshingCache(lambda key: load_snapshot(*key), SNAPSHOT_MAX_AGE_S)


def _err(rows: dict[str, Any], label: str) -> str | None:
    return (rows.get("errors") or {}).get(label)


def render_integrity_page() -> None:
    """Default page: pool-scoped Integrity tabs (zero-arg for ``st.Page``)."""
    st.title("Integrity")
    key = (str(REPO_ROOT), str(_derived()) if _derived() else None)
    snapshot = _snapshots().get(key)
    if snapshot is None:
        with st.spinner("Loading projections; the first view after a restart takes ~30 s."):
            snapshot = _snapshots().build(key)
    if _err(snapshot, "attach"):
        st.warning(_err(snapshot, "attach"))
        return
    ledger_ids = {iq.task_key(r.get("task_id")) for r in (snapshot.get("ledger_rows") or [])}
    audit_ids = {str(r.get("task_id")) for r in (snapshot.get("audits") or [])}
    pool = st.radio(
        "pool",
        [f"MiMo Python pool ({len(ledger_ids):,})", f"All MiMo tasks ({len(audit_ids):,})"],
        horizontal=True,
    )
    pool_ids = ledger_ids if pool.startswith("MiMo Python") else None
    scoped = _scope(snapshot, pool_ids)
    domains = ["(all)"] + sorted(scoped["domains"])
    domain = st.selectbox("domain", domains)
    if domain != "(all)":
        scoped = _scope(snapshot, pool_ids, domain)
    overview_tab, tasks_tab, verifier_tab, runs_tab, new_tab = st.tabs(
        ["Overview", "Tasks", "Verifier", "Runs", "New"]
    )
    with overview_tab:
        _render_overview(snapshot, scoped)
    with tasks_tab:
        _render_tasks(snapshot, scoped)
    with verifier_tab:
        _render_verifier(snapshot, scoped)
    with runs_tab:
        _render_runs(snapshot, scoped)
    with new_tab:
        _render_new(snapshot, scoped)


def _scope(
    snap: dict[str, Any], pool_ids: set[str] | None, domain: str = "(all)"
) -> dict[str, Any]:
    """Filter every task-linkable row set to the selected pool + domain."""
    task_domains: dict[str, str] = {}
    for row in snap.get("audits") or []:
        task_domains.setdefault(str(row.get("task_id")), str(row.get("domain") or "unknown"))

    def in_pool(task_ref: Any) -> bool:
        return pool_ids is None or iq.task_key(task_ref) in pool_ids

    def in_domain(row: Row) -> bool:
        return domain == "(all)" or str(row.get("domain") or "unknown") == domain

    def task_in_domain(task_ref: Any) -> bool:
        return domain == "(all)" or task_domains.get(iq.task_key(task_ref)) == domain

    audits = [
        r for r in (snap.get("audits") or []) if in_pool(r.get("task_id")) and in_domain(r)
    ]
    pool_tasks = {str(r.get("task_id")) for r in audits}
    digests = {r.get("task_version_digest") for r in audits if r.get("task_version_digest")}
    trial_task: dict[str, str] = {}
    for t in snap.get("trial_facts") or []:
        if t.get("trial_name"):
            trial_task[str(t["trial_name"])] = iq.task_key(t.get("task_name"))
    name_agent: dict[str, str] = {
        str(t.get("trial_name")): str(t.get("agent_name") or "")
        for t in (snap.get("trial_facts") or [])
        if t.get("trial_name")
    }
    id_task: dict[tuple[str, str], str] = {}
    for t in snap.get("identities") or []:
        if t.get("job_id") is not None and t.get("trial_name"):
            task = trial_task.get(str(t["trial_name"]))
            if task:
                id_task[(str(t["job_id"]), str(t["trial_id"]))] = task

    def proc_in(row: Row) -> bool:
        return in_pool(iq.task_key(row.get("task"))) and task_in_domain(row.get("task"))

    actions = [
        r
        for r in (snap.get("actions") or [])
        if id_task.get((str(r.get("job_id")), str(r.get("trial_id"))), "") in pool_tasks
        and task_in_domain(id_task.get((str(r.get("job_id")), str(r.get("trial_id"))), ""))
    ]
    out: dict[str, Any] = {
        "audits": audits,
        "pool_tasks": pool_tasks,
        "domains": sorted({str(r.get("domain") or "unknown") for r in (snap.get("audits") or []) if in_pool(r.get("task_id"))}),
        "findings": [
            r for r in (snap.get("findings") or []) if in_pool(r.get("task_id")) and in_domain(r)
        ],
        "table": [
            r for r in (snap.get("table") or []) if in_pool(r.get("task_id")) and in_domain(r)
        ],
        "processed": [r for r in (snap.get("processed") or []) if proc_in(r)],
        "trial_facts": [
            r for r in (snap.get("trial_facts") or []) if in_pool(iq.task_key(r.get("task_name"))) and task_in_domain(r.get("task_name"))
        ],
        "gated": [
            r for r in (snap.get("gated") or []) if in_pool(iq.task_key(r.get("task_name"))) and task_in_domain(r.get("task_name"))
        ],
        "census": [
            r for r in (snap.get("census") or []) if in_pool(iq.task_key(r.get("task"))) and task_in_domain(r.get("task"))
        ],
        "oracle": [
            r for r in (snap.get("oracle") or [])
            if in_pool(r.get("task_id")) and task_in_domain(r.get("task_id"))
        ],
        "versions": [
            r for r in (snap.get("versions") or []) if in_pool(r.get("task_id")) and in_domain(r)
        ],
        "nop_tasks": [
            r for r in (snap.get("nop_tasks") or [])
            if in_pool(r.get("task_id")) and task_in_domain(r.get("task_id"))
        ],
        "nop_positive": [
            r for r in (snap.get("nop_positive") or [])
            if in_pool(r.get("task_id")) and task_in_domain(r.get("task_id"))
        ],
        "stability": [r for r in (snap.get("stability") or []) if r.get("task_version_digest") in digests],
        "exploits": [r for r in (snap.get("exploits") or []) if r.get("task_version_digest") in digests],
        "cracks": [r for r in (snap.get("cracks") or []) if r.get("task_version_digest") in digests],
        "actions": actions,
        "loops": [
            r for r in (snap.get("loops") or []) if in_pool(iq.task_key(r.get("task_name"))) and task_in_domain(r.get("task_name"))
        ],
        "name_agent": name_agent,
    }
    quality_all = snap.get("quality") or []
    quality = [
        r
        for r in quality_all
        if id_task.get((str(r.get("job_id")), str(r.get("trial_id"))), "") in pool_tasks
        and task_in_domain(id_task.get((str(r.get("job_id")), str(r.get("trial_id"))), ""))
    ]
    out["quality"] = quality
    out["n_quality_unmapped"] = len(quality_all) - len(quality)
    out["quality_by_code"] = _quality_by_code(quality)
    try:
        out["action_mix"] = iq.build_action_mix(
            actions, snap.get("identities") or [], out["processed"]
        )
        out["action_mix_error"] = None
    except SourceUnavailable as exc:
        out["action_mix"] = []
        out["action_mix_error"] = str(exc)
    home = _results_home_or_none()
    resolved = []
    for row in out["processed"]:
        item = dict(row)
        agent, via = iq.classify_run_agent(item, name_agent, home=home)
        item["agent"] = agent
        item["agent_via"] = via
        resolved.append(item)
    out["processed"] = resolved
    out["run_buckets"] = iq.partition_runs(resolved)
    return out


def _quality_by_code(quality: list[Row]) -> list[Row]:
    counts: dict[str, int] = {}
    for row in quality:
        code = str(row.get("code") or "unknown")
        counts[code] = counts.get(code, 0) + 1
    return [
        {"code": code, "n": counts[code]}
        for code in sorted(counts, key=lambda code: (-counts[code], code))
    ]


def _results_home_or_none() -> Path | None:
    try:
        return iq.results_home()
    except Exception:
        return None


def _cheat_rate(processed: list[Row]) -> tuple[int, str]:
    """(cheat passes, formatted rate): excluded passes with cheat reasons ÷ passes."""
    passes = sum(1 for r in processed if r.get("passed"))
    cheats = sum(
        1
        for r in processed
        if r.get("passed")
        and r.get("verdict") == "excluded"
        and (set(r.get("reasons") or []) & set(iq.CHEAT_REASONS))
    )
    rate = f"{100.0 * cheats / passes:.1f}%" if passes else "—"
    return cheats, rate


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _render_overview(snap: dict[str, Any], scoped: dict[str, Any]) -> None:
    ledger = {r["verdict"]: r["n"] for r in (snap.get("ledger") or [])}
    if _err(snap, "ledger"):
        st.warning(_err(snap, "ledger"))
    oracle = scoped["oracle"]
    proven_tasks = {
        iq.task_key(r.get("task_id")) for r in oracle if r.get("label") == "oracle:pass+nop:fail"
    }
    broken = {
        str(r.get("task_id")) for r in scoped["audits"] if r.get("qual_broken") == 1
    }
    st.subheader("Tasks")
    task_cards = st.columns(5)
    task_cards[0].metric("usable (keep)", ledger.get("keep", 0))
    task_cards[1].metric("fix", ledger.get("fix", 0))
    task_cards[2].metric("discard", ledger.get("discard", 0))
    task_cards[3].metric("oracle-proven solvable", len(proven_tasks))
    task_cards[4].metric("broken grader", len(broken))
    st.caption("keep/fix/discard: python-pool ledger curation. proven/broken: selected pool.")
    if _err(snap, "oracle"):
        st.warning(_err(snap, "oracle"))

    buckets = scoped.get("run_buckets") or {"agent": [], "control": [], "unknown": []}
    agent = buckets["agent"]
    n_runs = len(agent)
    n_passed = sum(1 for r in agent if r.get("passed"))
    n_counted = sum(1 for r in agent if r.get("verdict") == "counted_pass")
    n_excluded = sum(1 for r in agent if r.get("verdict") == "excluded" and r.get("passed"))
    _cheats, rate = _cheat_rate(agent)
    legacy = sum(1 for r in agent if r.get("schema") == "legacy")
    st.subheader("Agent runs")
    run_cards = st.columns(5)
    run_cards[0].metric("runs", n_runs)
    run_cards[1].metric("passes", n_passed)
    run_cards[2].metric("counted passes", n_counted)
    run_cards[3].metric("excluded passes", n_excluded)
    run_cards[4].metric("cheat rate", rate)
    st.caption(
        "known-agent runs only. cheat rate = excluded passes with "
        "copied_fix/pass_tainted ÷ passes. "
        f"{legacy} legacy reports carry no verdict. "
        f"controls: {len(buckets['control'])}, unknown agent: {len(buckets['unknown'])}."
    )
    if _err(snap, "processed_scan"):
        st.warning(_err(snap, "processed_scan"))

    disagreements = iq.verdict_disagreements(scoped["audits"])
    if disagreements:
        with st.expander(f"verdict disagreement across cohorts ({len(disagreements)})"):
            st.dataframe(pd.DataFrame(disagreements), hide_index=True)

    st.subheader("Exclusion reasons among passes")
    if _err(snap, "processed_scan"):
        pass
    else:
        cheats_frame = pd.DataFrame(iq.reason_histogram(agent, only_passed=True))
        if not cheats_frame.empty:
            st.bar_chart(cheats_frame, x="reason", y="n", horizontal=True)
            st.dataframe(cheats_frame, hide_index=True)
        else:
            st.write("no reasons on passes recorded")
    _render_rule_summary(scoped)


def _render_rule_summary(scoped: dict[str, Any]) -> None:
    """100%-of-pool rules as one caption line; the rest as % bars."""
    st.subheader("Finding rules")
    coverage = iq.rule_coverage(scoped["findings"], pool_tasks=len(scoped["pool_tasks"]))
    if not coverage:
        st.write("no task findings recorded")
        return
    universal = [c["rule"] for c in coverage if c["pct"] >= 100.0]
    rest = [c for c in coverage if c["pct"] < 100.0]
    if universal:
        st.caption(f"applies to every task: {', '.join(universal)}")
    if rest:
        frame = pd.DataFrame(rest[:12])
        st.bar_chart(frame, x="rule", y="pct", horizontal=True)
        st.dataframe(frame, hide_index=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _render_tasks(snap: dict[str, Any], scoped: dict[str, Any]) -> None:
    _render_rule_summary(scoped)
    counts: dict[tuple[str, str], int] = {}
    for row in scoped["findings"]:
        key = (str(row.get("domain") or "unknown"), str(row.get("rule") or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    st.subheader("Domain x rule")
    if counts:
        frame = pd.DataFrame(
            [{"domain": d, "rule": r, "n": n} for (d, r), n in sorted(counts.items())]
        )
        st.dataframe(
            frame.pivot_table(index="domain", columns="rule", values="n", fill_value=0)
        )
    else:
        st.write("no domain x rule hits recorded")
    st.subheader("Tasks")
    table = scoped["table"]
    if _err(snap, "table"):
        st.warning(_err(snap, "table"))
        return
    if not table:
        st.write("no audited tasks indexed")
        return
    frame = pd.DataFrame(table)
    verdicts = ["(all)"] + sorted(str(v) for v in frame["verdict"].dropna().unique())
    verdict = st.selectbox("verdict", verdicts)
    only_broken = st.checkbox("only qual_broken", value=False)
    only_nop = st.checkbox("only nop>0", value=False)
    view = frame
    if verdict != "(all)":
        view = view[view["verdict"] == verdict]
    if only_broken:
        view = view[view["qual_broken"] == 1]
    if only_nop:
        view = view[view["nop_positive"]]
    st.write(f"tasks shown: {view['task_id'].nunique()} distinct ({len(view)} cohorts)")
    st.dataframe(view, hide_index=True)
    options = view["task_id"].tolist() if len(view) else []
    if not options:
        return
    selected = st.selectbox("task detail", options)
    _render_task_detail(snap, scoped, str(selected))


def _render_task_detail(snap: dict[str, Any], scoped: dict[str, Any], task_id: str) -> None:
    cohorts = [r for r in scoped["table"] if r.get("task_id") == task_id]
    if cohorts:
        st.write(f"cohorts: {len(cohorts)}")
        st.dataframe(pd.DataFrame(cohorts), hide_index=True)
    audits = [r for r in scoped["audits"] if str(r.get("task_id")) == task_id]
    versions = [r for r in scoped["versions"] if str(r.get("task_id")) == task_id]
    if versions:
        st.dataframe(pd.DataFrame(versions), hide_index=True)
    oracle = [r for r in scoped["oracle"] if iq.task_key(r.get("task_id")) == task_id]
    if oracle:
        st.dataframe(pd.DataFrame(oracle), hide_index=True)
    elif _err(snap, "oracle"):
        st.warning(_err(snap, "oracle"))
    digests = {r.get("task_version_digest") for r in audits if r.get("task_version_digest")}
    if digests:
        stability = [r for r in scoped["stability"] if r.get("task_version_digest") in digests]
        exploits = [r for r in scoped["exploits"] if r.get("task_version_digest") in digests]
        if stability:
            st.dataframe(pd.DataFrame(stability), hide_index=True)
        if exploits:
            st.dataframe(pd.DataFrame(exploits), hide_index=True)
    processed = [r for r in scoped["processed"] if iq.task_key(r.get("task")) == task_id]
    facts = [r for r in scoped["trial_facts"] if iq.task_key(r.get("task_name")) == task_id]
    st.subheader("Runs")
    run_rows: list[Row] = []
    for row in processed:
        run_rows.append(
            {
                "viewer": row.get("viewer_url"),
                "job": row.get("job"),
                "trial": row.get("trial"),
                "verdict": row.get("verdict"),
                "reasons": row.get("reason_str"),
                "raw_reward": row.get("raw_reward"),
            }
        )
    for row in facts:
        run_rows.append(
            {
                "viewer": iq.trial_url(str(row.get("job_name")), str(row.get("trial_name"))),
                "job": row.get("job_name"),
                "trial": row.get("trial_name"),
                "verdict": None,
                "reasons": "",
                "raw_reward": row.get("primary_reward"),
            }
        )
    if run_rows:
        st.dataframe(
            pd.DataFrame(run_rows),
            hide_index=True,
            column_config={"viewer": st.column_config.LinkColumn("viewer")},
        )
    else:
        st.write("no runs recorded for this task")
    if st.button("Load full dossier", key=f"dossier-{task_id}"):
        try:
            dossier = iq.task_dossier_summary(
                task_id, repo_root=REPO_ROOT, derived_root=_derived()
            )
        except SourceUnavailable as exc:
            st.warning(str(exc))
            return
        st.write(f"dossier status: {(dossier.get('audit') or {}).get('status')}")
        st.write(f"dossier verdict: {(dossier.get('audit') or {}).get('verdict')}")
        coverage = dossier.get("coverage") or {}
        st.write(f"dossier trials: {coverage.get('n_trials', 0)}")
        st.json({key: dossier.get(key) for key in ("health", "verdict", "leak", "coverage")})


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def _render_verifier(snap: dict[str, Any], scoped: dict[str, Any]) -> None:
    oracle = scoped["oracle"]
    pool_tasks = scoped["pool_tasks"]
    swept = {iq.task_key(r.get("task_id")) for r in oracle}
    groups: dict[str, set[str]] = {"proven": set(), "fail": set(), "conflict": set(), "none": set()}
    for row in oracle:
        label = str(row.get("label") or "")
        task = iq.task_key(row.get("task_id"))
        if label == "oracle:pass+nop:fail":
            groups["proven"].add(task)
        elif label in ("oracle:fail", "oracle:fail-network"):
            groups["fail"].add(task)
        elif label == "oracle:patch-conflict":
            groups["conflict"].add(task)
        else:
            groups["none"].add(task)
    st.subheader("Oracle evidence")
    if _err(snap, "oracle"):
        st.warning(_err(snap, "oracle"))
    else:
        cards = st.columns(4)
        cards[0].metric("oracle-proven", len(groups["proven"]))
        cards[1].metric("oracle fail", len(groups["fail"]))
        cards[2].metric("patch conflict", len(groups["conflict"]))
        cards[3].metric("not attempted", len(pool_tasks - swept))
        st.caption("proven = oracle passes where nop fails.")
        label_counts: dict[str, int] = {}
        for row in oracle:
            label_counts[str(row.get("label"))] = label_counts.get(str(row.get("label")), 0) + 1
        if label_counts:
            frame = pd.DataFrame(
                [{"label": label, "n": label_counts[label]} for label in sorted(label_counts)]
            )
            st.bar_chart(frame, x="label", y="n", horizontal=True)

    nop = scoped["nop_positive"]
    st.subheader("Grader passes with no work")
    if _err(snap, "nop_positive"):
        st.warning(_err(snap, "nop_positive"))
    else:
        st.write(f"nop>0 runs: {len(nop)}")
        if nop:
            st.dataframe(pd.DataFrame(nop), hide_index=True)
        else:
            st.write("no nop run earned reward")

    controls = iq.control_rows(
        [
            {"agent_name": t.get("agent_name"), "job_name": t.get("job_name"),
             "trial_name": t.get("trial_name"), "primary_reward": t.get("primary_reward")}
            for t in scoped["trial_facts"]
        ]
    )
    st.subheader("Control runs (nop / oracle agents)")
    st.write(f"control runs: {len(controls)}")
    if controls:
        control_passes = sum(1 for r in controls if (r.get("primary_reward") or 0) > 0)
        st.write(f"control passes: {control_passes}")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "viewer": iq.trial_url(str(r.get("job_name")), str(r.get("trial_name"))),
                        "job": r.get("job_name"),
                        "trial": r.get("trial_name"),
                        "agent": r.get("agent_name"),
                        "reward": r.get("primary_reward"),
                    }
                    for r in controls[:500]
                ]
            ),
            hide_index=True,
            column_config={"viewer": st.column_config.LinkColumn("viewer")},
        )

    stability = scoped["stability"]
    st.subheader("Stability probes")
    if _err(snap, "stability"):
        st.warning(_err(snap, "stability"))
    else:
        verdicts: dict[str, int] = {}
        for row in stability:
            verdicts[str(row.get("verdict"))] = verdicts.get(str(row.get("verdict")), 0) + 1
        st.write(f"probed tasks: {len(stability)}")
        for verdict in sorted(verdicts):
            st.write(f"{verdict}: {verdicts[verdict]}")
        if stability:
            st.dataframe(pd.DataFrame(stability), hide_index=True)

    cracks = scoped["cracks"]
    st.subheader("Exploit-probe cracks")
    if _err(snap, "cracks"):
        st.warning(_err(snap, "cracks"))
    else:
        st.write(f"exploit cracks: {len(cracks)}")
        if cracks:
            st.dataframe(pd.DataFrame(cracks), hide_index=True)
        else:
            st.write("no probe cracked its task")

    versions = scoped["versions"]
    st.subheader("LLM-judge graders")
    if _err(snap, "versions"):
        st.warning(_err(snap, "versions"))
    else:
        by_kind: dict[str, int] = {}
        for row in versions:
            if str(row.get("grader_kind") or "") in ("llm_judge", "vlm_judge"):
                by_kind[str(row["grader_kind"])] = by_kind.get(str(row["grader_kind"]), 0) + 1
        st.write(f"judge-graded tasks: {sum(by_kind.values())}")
        for kind in sorted(by_kind):
            st.write(f"{kind}: {by_kind[kind]}")

    st.subheader("Verifier isolation")
    coverage = iq.rule_coverage(scoped["findings"], pool_tasks=len(pool_tasks))
    needles = ("conftest", "isolat", "plantable", "network", "hook", "leak", "git-history")
    hits = [c for c in coverage if any(n in c["rule"] for n in needles)]
    if _err(snap, "findings"):
        st.warning(_err(snap, "findings"))
    else:
        st.write(f"isolation finding rules: {len(hits)}")
        if hits:
            st.bar_chart(pd.DataFrame(hits), x="rule", y="pct", horizontal=True)
            st.dataframe(pd.DataFrame(hits), hide_index=True)

    st.subheader("Held-out regrade")
    if _err(snap, "regrade"):
        st.warning(_err(snap, "regrade"))
    else:
        regrade = snap.get("regrade") or []
        st.write(f"regrade rows: {len(regrade)}")
        if regrade:
            st.dataframe(pd.DataFrame(regrade), hide_index=True)
    st.subheader("Detector scores")
    if _err(snap, "detectors"):
        st.warning(_err(snap, "detectors"))
    else:
        detectors = snap.get("detectors") or []
        st.write(f"detector score rows: {len(detectors)}")
        if detectors:
            st.dataframe(pd.DataFrame(detectors), hide_index=True)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def _render_runs(snap: dict[str, Any], scoped: dict[str, Any]) -> None:
    buckets = scoped.get("run_buckets") or {"agent": [], "control": [], "unknown": []}
    agent = buckets["agent"]
    over_time = iq.passes_over_time(agent)
    st.subheader("Daily agent passes")
    if over_time:
        frame = pd.DataFrame(over_time)
        st.bar_chart(frame, x="date", y=["counted", "excluded"])
        st.dataframe(frame, hide_index=True)
    else:
        st.write("no published agent passes indexed")

    st.subheader("Passes by model")
    if agent:
        by_model: dict[str, dict[str, int]] = {}
        for row in agent:
            model = str(row.get("model") or "unknown")
            entry = by_model.setdefault(
                model, {"runs": 0, "passes": 0, "counted": 0, "excluded_cheat": 0, "excluded_pass": 0}
            )
            entry["runs"] += 1
            if row.get("passed"):
                entry["passes"] += 1
                if row.get("verdict") == "counted_pass":
                    entry["counted"] += 1
                if (
                    row.get("verdict") == "excluded"
                    and (set(row.get("reasons") or []) & set(iq.CHEAT_REASONS))
                ):
                    entry["excluded_cheat"] += 1
                if row.get("verdict") == "excluded":
                    entry["excluded_pass"] += 1
        model_rows = [
            {
                "model": model,
                **counts,
                "cheat_rate": f"{100.0 * counts['excluded_cheat'] / counts['passes']:.1f}%"
                if counts["passes"]
                else "—",
            }
            for model, counts in sorted(by_model.items())
        ]
        st.dataframe(pd.DataFrame(model_rows), hide_index=True)
    else:
        st.write("no agent runs indexed")

    st.subheader("Excluded or suspicious passes")
    excluded = iq.excluded_or_suspicious(agent)
    if excluded:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "viewer": r.get("viewer_url"),
                        "date": r.get("date"),
                        "model": r.get("model"),
                        "task": r.get("task"),
                        "reason": r.get("reason_str"),
                        "evidence": (r.get("evidence_command") or "")[:80],
                    }
                    for r in excluded
                ]
            ),
            hide_index=True,
            column_config={"viewer": st.column_config.LinkColumn("viewer")},
        )
    else:
        st.write("no excluded or suspicious passes recorded")

    gated = [r for r in scoped["gated"] if r.get("suspicious_pass")]
    st.subheader("Integrity-gate failures")
    st.write(f"suspicious passes: {len(gated)}")
    if gated:
        st.dataframe(pd.DataFrame(gated), hide_index=True)
    if _err(snap, "gated"):
        st.warning(_err(snap, "gated"))

    mix = scoped.get("action_mix") or []
    mix_error = scoped.get("action_mix_error")
    scoped_trials = {iq.task_key(t.get("task_name")) for t in scoped["trial_facts"]}
    st.subheader("Action mix")
    if mix_error:
        st.warning(mix_error)
    elif mix:
        frame = pd.DataFrame(mix)
        st.dataframe(
            frame.pivot_table(index="action_family", columns="trial_class", values="n", fill_value=0)
        )
        st.caption(
            "pool-scoped: published verdicts joined to attach trials by trial name; "
            f"pool tasks with attach trials: {len(scoped_trials)}."
        )
    else:
        st.write("no action mix recorded")

    census = scoped["census"]
    st.subheader("Local trials census")
    if _err(snap, "census"):
        st.warning(_err(snap, "census"))
    else:
        st.write(f"census trials: {len(census)}")
        if census:
            st.dataframe(pd.DataFrame(census), hide_index=True)
        else:
            st.write("no local trials indexed")

    _meta = (snap.get("processed_scan") or ([], {}))[1]
    if _meta.get("n_unreadable"):
        st.caption(f"unreadable processed reports: {_meta['n_unreadable']}")
    if _meta.get("n_legacy"):
        st.caption(
            f"{_meta['n_legacy']} legacy-schema reports carry no counted/excluded verdict."
        )


# ---------------------------------------------------------------------------
# New
# ---------------------------------------------------------------------------


def _recent_iso(items: list[Row], key: str, *, days: int) -> list[Row]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    recent: list[Row] = []
    for row in items:
        stamp = row.get(key)
        if not isinstance(stamp, str) or not stamp:
            continue
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if moment >= cutoff:
            recent.append(row)
    return recent


def _render_new(snap: dict[str, Any], scoped: dict[str, Any]) -> None:
    window = st.selectbox("window", ["24h", "7d"])
    days = 1 if window == "24h" else 7
    buckets = scoped.get("run_buckets") or {"agent": [], "control": [], "unknown": []}
    processed = buckets["agent"] + buckets["unknown"]
    fresh_passes = [
        r
        for r in _recent_iso(processed, "mtime", days=days)
        if r.get("verdict") == "excluded" and r.get("passed")
    ]
    st.subheader("New excluded passes")
    st.write(f"new excluded passes ({window}): {len(fresh_passes)}")
    if fresh_passes:
        st.dataframe(
            pd.DataFrame(fresh_passes),
            hide_index=True,
            column_config={"viewer_url": st.column_config.LinkColumn("viewer")},
        )
    if _err(snap, "processed_scan"):
        st.warning(_err(snap, "processed_scan"))
    cracks = scoped["cracks"]
    fresh_cracks = _recent_iso(cracks, "produced_at", days=days)
    st.subheader("New exploit cracks")
    st.write(f"new exploit cracks ({window}): {len(fresh_cracks)}")
    if fresh_cracks:
        st.dataframe(pd.DataFrame(fresh_cracks), hide_index=True)
    if _err(snap, "cracks"):
        st.warning(_err(snap, "cracks"))
    quality = scoped.get("quality") or []
    fresh_quality = _recent_iso(quality, "evaluated_at", days=days)
    st.subheader("New trajectory findings")
    if _err(snap, "quality"):
        st.warning(_err(snap, "quality"))
    else:
        st.write(f"new trajectory findings ({window}): {len(fresh_quality)}")
        if fresh_quality:
            counts: dict[str, int] = {}
            for row in fresh_quality:
                code = str(row.get("code") or "unknown")
                counts[code] = counts.get(code, 0) + 1
            st.dataframe(
                pd.DataFrame([{"code": code, "n": counts[code]} for code in sorted(counts)]),
                hide_index=True,
            )
    if scoped.get("n_quality_unmapped"):
        st.caption(
            f"{scoped['n_quality_unmapped']} findings without a pool task link excluded."
        )
    st.subheader("New findings / broken tasks")
    st.caption("task findings carry no timestamp: current totals.")
    coverage = iq.rule_coverage(scoped["findings"], pool_tasks=len(scoped["pool_tasks"]))
    st.write(f"finding rules active: {len(coverage)}")
    broken = {str(r.get("task_id")) for r in scoped["audits"] if r.get("qual_broken") == 1}
    st.write(f"broken tasks now: {len(broken)}")
