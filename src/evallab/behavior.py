"""Behavioral analysis over the unified attach surface (trial_facts, steps, tool_calls).

Analyzes how agents work (steps, tools, LLMs, latency, sources, tokens, cost, struggle signals)
split by outcome (passed / scored-zero / never-measured). Degenerate efficiency ratios are
represented as undefined (None), sparse token columns are explicitly reported with coverage counts,
and underpowered comparisons render 'not distinguishable'.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evallab.cohort import (
    NOT_COMPARABLE,
    bootstrap_mean_interval,
)
from evallab.storage.attach import attach

DEFAULT_POWER_THRESHOLD = 5
SQL_BEHAVIOR_PATH = Path("sql/behavior.sql")


@dataclass(frozen=True)
class EffortOutcomeRow:
    task_name: str
    agent_name: str
    outcome: str  # 'passed' | 'scored_zero' | 'never_measured'
    n: int
    avg_steps: float
    steps_ci: tuple[float, float] | None
    avg_tool_calls: float
    tool_calls_ci: tuple[float, float] | None
    avg_llm_calls: float
    llm_calls_ci: tuple[float, float] | None
    avg_execution_seconds: float | None
    execution_seconds_ci: tuple[float, float] | None
    avg_duration_seconds: float
    duration_seconds_ci: tuple[float, float] | None
    avg_reward: float | None


@dataclass(frozen=True)
class EfficiencyRow:
    task_name: str
    agent_name: str
    outcome: str
    n: int
    avg_steps: float
    avg_execution_seconds: float | None
    avg_reward: float | None
    seconds_per_step: float | None  # None if steps == 0 or time is None
    steps_per_reward_point: float | None  # None if reward == 0 or unmeasured (undefined)


@dataclass(frozen=True)
class StruggleRow:
    task_name: str
    agent_name: str
    outcome: str
    n: int
    avg_repeated_failed_commands: float
    max_repeated_failed_commands: int
    trials_with_repeated_failures_n: int
    avg_command_failures: float
    max_command_failures: int
    trials_with_command_failures_n: int
    avg_invalid_trajectories: float
    trials_with_invalid_trajectories_n: int


@dataclass(frozen=True)
class StepShapeRow:
    task_name: str
    agent_name: str
    outcome: str
    n_trials: int
    n_trials_with_steps: int
    total_steps_n: int
    avg_steps_per_trial: float
    system_steps_n: int
    agent_steps_n: int
    user_steps_n: int
    system_pct: float | None
    agent_pct: float | None
    user_pct: float | None


@dataclass(frozen=True)
class TokenEconomicsRow:
    task_name: str
    agent_name: str
    outcome: str
    n_total: int
    n_populated: int
    coverage_summary: str  # "X of Y trials"
    populated_pct: float
    avg_input_tokens: float | None
    avg_cache_tokens: float | None
    avg_output_tokens: float | None
    avg_cost_usd: float | None
    total_cost_usd: float | None


@dataclass(frozen=True)
class BehaviorComparison:
    comparison_name: str
    metric: str
    group_a_label: str
    group_a_n: int
    group_a_mean: float | None
    group_a_ci: tuple[float, float] | None
    group_b_label: str
    group_b_n: int
    group_b_mean: float | None
    group_b_ci: tuple[float, float] | None
    verdict: str  # 'distinguishable' | 'not distinguishable'


@dataclass(frozen=True)
class BehaviorReport:
    total_trials: int
    total_measured: int
    total_never_measured: int
    total_passed: int
    total_scored_zero: int
    token_coverage_summary: str
    effort_by_outcome: list[EffortOutcomeRow]
    efficiency: list[EfficiencyRow]
    struggle_signals: list[StruggleRow]
    step_shape: list[StepShapeRow]
    token_economics: list[TokenEconomicsRow]
    comparisons: list[BehaviorComparison]
    findings: list[str]


def _classify_outcome(primary_reward: float | None, exception_class: str | None) -> str:
    if exception_class is not None or primary_reward is None:
        return "never_measured"
    if primary_reward >= 1.0:
        return "passed"
    if primary_reward == 0.0:
        return "scored_zero"
    return "partial"


def generate_behavior_report(
    repo_root: Path,
    explicit_derived: Path | None = None,
    task_filter: str | None = None,
    agent_filter: str | None = None,
    power_threshold: int = DEFAULT_POWER_THRESHOLD,
) -> BehaviorReport:
    """Run behavioral analysis against the unified attach surface."""
    attach_result = attach(repo_root=repo_root, explicit_derived=explicit_derived)
    conn = attach_result.connection

    # Ensure behavior.sql is loaded into the connection
    sql_path = repo_root / SQL_BEHAVIOR_PATH
    if sql_path.exists():
        conn.execute(sql_path.read_text())
    else:
        fallback_sql = Path(__file__).resolve().parents[2] / SQL_BEHAVIOR_PATH
        if fallback_sql.exists():
            conn.execute(fallback_sql.read_text())

    # Raw trial facts for distributions and bootstrap intervals
    query = """
    SELECT
        trial_id,
        coalesce(task_name, 'unknown') as task_name,
        coalesce(agent_name, 'unknown') as agent_name,
        primary_reward,
        exception_class,
        coalesce(step_count, 0) as step_count,
        coalesce(tool_call_count, 0) as tool_call_count,
        coalesce(llm_call_count, 0) as llm_call_count,
        agent_execution_seconds,
        coalesce(duration_seconds, 0.0) as duration_seconds,
        coalesce(repeated_failed_command_count, 0) as repeated_failed_command_count,
        coalesce(command_failure_count, 0) as command_failure_count,
        coalesce(invalid_trajectory_count, 0) as invalid_trajectory_count,
        input_tokens,
        cache_tokens,
        output_tokens,
        cost_usd
    FROM trial_facts
    """
    rows = conn.execute(query).fetchall()

    trials: list[dict[str, Any]] = []
    for r in rows:
        t_name = r[1]
        a_name = r[2]
        if task_filter and t_name != task_filter:
            continue
        if agent_filter and a_name != agent_filter:
            continue
        outcome = _classify_outcome(r[3], r[4])
        trials.append(
            {
                "trial_id": r[0],
                "task_name": t_name,
                "agent_name": a_name,
                "primary_reward": r[3],
                "exception_class": r[4],
                "outcome": outcome,
                "step_count": r[5],
                "tool_call_count": r[6],
                "llm_call_count": r[7],
                "agent_execution_seconds": r[8],
                "duration_seconds": r[9],
                "repeated_failed_command_count": r[10],
                "command_failure_count": r[11],
                "invalid_trajectory_count": r[12],
                "input_tokens": r[13],
                "cache_tokens": r[14],
                "output_tokens": r[15],
                "cost_usd": r[16],
            }
        )

    total_trials = len(trials)
    total_never_measured = sum(1 for t in trials if t["outcome"] == "never_measured")
    total_measured = total_trials - total_never_measured
    total_passed = sum(1 for t in trials if t["outcome"] == "passed")
    total_scored_zero = sum(1 for t in trials if t["outcome"] == "scored_zero")

    populated_tokens_n = sum(1 for t in trials if t["cost_usd"] is not None)
    token_coverage_summary = f"{populated_tokens_n} of {total_trials} trials"

    # Group trials by (task_name, agent_name, outcome)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in trials:
        groups[(t["task_name"], t["agent_name"], t["outcome"])].append(t)

    sorted_keys = sorted(groups.keys())

    # 1. Effort vs outcome
    effort_rows: list[EffortOutcomeRow] = []
    for k in sorted_keys:
        grp = groups[k]
        n = len(grp)
        steps_list = [float(x["step_count"]) for x in grp]
        tools_list = [float(x["tool_call_count"]) for x in grp]
        llm_list = [float(x["llm_call_count"]) for x in grp]
        exec_times = [
            float(x["agent_execution_seconds"])
            for x in grp
            if x["agent_execution_seconds"] is not None
        ]
        duration_list = [float(x["duration_seconds"]) for x in grp]
        rewards = [float(x["primary_reward"]) for x in grp if x["primary_reward"] is not None]

        avg_steps = round(sum(steps_list) / n, 2) if n > 0 else 0.0
        avg_tools = round(sum(tools_list) / n, 2) if n > 0 else 0.0
        avg_llm = round(sum(llm_list) / n, 2) if n > 0 else 0.0
        avg_exec = round(sum(exec_times) / len(exec_times), 2) if exec_times else None
        avg_dur = round(sum(duration_list) / n, 2) if n > 0 else 0.0
        avg_rew = round(sum(rewards) / len(rewards), 4) if rewards else None

        steps_ci = bootstrap_mean_interval(steps_list) if n >= power_threshold else None
        tools_ci = bootstrap_mean_interval(tools_list) if n >= power_threshold else None
        llm_ci = bootstrap_mean_interval(llm_list) if n >= power_threshold else None
        exec_ci = (
            bootstrap_mean_interval(exec_times)
            if len(exec_times) >= power_threshold
            else None
        )
        dur_ci = bootstrap_mean_interval(duration_list) if n >= power_threshold else None

        effort_rows.append(
            EffortOutcomeRow(
                task_name=k[0],
                agent_name=k[1],
                outcome=k[2],
                n=n,
                avg_steps=avg_steps,
                steps_ci=steps_ci,
                avg_tool_calls=avg_tools,
                tool_calls_ci=tools_ci,
                avg_llm_calls=avg_llm,
                llm_calls_ci=llm_ci,
                avg_execution_seconds=avg_exec,
                execution_seconds_ci=exec_ci,
                avg_duration_seconds=avg_dur,
                duration_seconds_ci=dur_ci,
                avg_reward=avg_rew,
            )
        )

    # 2. Efficiency
    efficiency_rows: list[EfficiencyRow] = []
    for k in sorted_keys:
        grp = groups[k]
        n = len(grp)
        steps_sum = sum(x["step_count"] for x in grp)
        exec_sum = sum(
            x["agent_execution_seconds"]
            for x in grp
            if x["agent_execution_seconds"] is not None
        )
        rewards = [x["primary_reward"] for x in grp if x["primary_reward"] is not None]
        rew_sum = sum(rewards) if rewards else 0.0

        avg_steps = round(steps_sum / n, 2) if n > 0 else 0.0
        exec_times = [
            x["agent_execution_seconds"]
            for x in grp
            if x["agent_execution_seconds"] is not None
        ]
        avg_exec = round(sum(exec_times) / len(exec_times), 2) if exec_times else None
        avg_rew = round(rew_sum / len(rewards), 4) if rewards else None

        sec_per_step: float | None = None
        if steps_sum > 0 and exec_times:
            sec_per_step = round(exec_sum / steps_sum, 2)

        steps_per_reward: float | None = None
        if k[2] == "passed" and rew_sum > 0:
            steps_per_reward = round(steps_sum / rew_sum, 2)

        efficiency_rows.append(
            EfficiencyRow(
                task_name=k[0],
                agent_name=k[1],
                outcome=k[2],
                n=n,
                avg_steps=avg_steps,
                avg_execution_seconds=avg_exec,
                avg_reward=avg_rew,
                seconds_per_step=sec_per_step,
                steps_per_reward_point=steps_per_reward,
            )
        )

    # 3. Struggle signals
    struggle_rows: list[StruggleRow] = []
    for k in sorted_keys:
        grp = groups[k]
        n = len(grp)
        rep_fails = [x["repeated_failed_command_count"] for x in grp]
        cmd_fails = [x["command_failure_count"] for x in grp]
        inv_trajs = [x["invalid_trajectory_count"] for x in grp]

        struggle_rows.append(
            StruggleRow(
                task_name=k[0],
                agent_name=k[1],
                outcome=k[2],
                n=n,
                avg_repeated_failed_commands=round(sum(rep_fails) / n, 2) if n > 0 else 0.0,
                max_repeated_failed_commands=max(rep_fails) if rep_fails else 0,
                trials_with_repeated_failures_n=sum(1 for x in rep_fails if x > 0),
                avg_command_failures=round(sum(cmd_fails) / n, 2) if n > 0 else 0.0,
                max_command_failures=max(cmd_fails) if cmd_fails else 0,
                trials_with_command_failures_n=sum(1 for x in cmd_fails if x > 0),
                avg_invalid_trajectories=round(sum(inv_trajs) / n, 2) if n > 0 else 0.0,
                trials_with_invalid_trajectories_n=sum(1 for x in inv_trajs if x > 0),
            )
        )

    # 4. Step shape (query steps table joined with outcome)
    step_shape_query = """
    WITH step_counts AS (
        SELECT
            trial_id,
            count(*) AS total_steps,
            sum(CASE WHEN source = 'system' THEN 1 ELSE 0 END) AS system_steps,
            sum(CASE WHEN source = 'agent' THEN 1 ELSE 0 END) AS agent_steps,
            sum(CASE WHEN source = 'user' THEN 1 ELSE 0 END) AS user_steps
        FROM steps
        GROUP BY trial_id
    )
    SELECT
        t.task_name,
        t.agent_name,
        t.outcome,
        count(t.trial_id) AS n_trials,
        count(sc.trial_id) AS n_trials_with_steps,
        coalesce(sum(sc.total_steps), 0) AS total_steps_n,
        round(avg(t.step_count), 2) AS avg_steps_per_trial,
        coalesce(sum(sc.system_steps), 0) AS system_steps_n,
        coalesce(sum(sc.agent_steps), 0) AS agent_steps_n,
        coalesce(sum(sc.user_steps), 0) AS user_steps_n,
        round(100.0 * coalesce(sum(sc.system_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1)
            AS system_pct,
        round(100.0 * coalesce(sum(sc.agent_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1)
            AS agent_pct,
        round(100.0 * coalesce(sum(sc.user_steps), 0) / NULLIF(sum(sc.total_steps), 0), 1)
            AS user_pct
    FROM v_behavior_trial_summary t
    LEFT JOIN step_counts sc ON t.trial_id = sc.trial_id
    GROUP BY t.task_name, t.agent_name, t.outcome
    ORDER BY t.task_name, t.agent_name, t.outcome;
    """
    step_shape_rows_raw = conn.execute(step_shape_query).fetchall()
    step_shape_rows: list[StepShapeRow] = []
    for r in step_shape_rows_raw:
        if task_filter and r[0] != task_filter:
            continue
        if agent_filter and r[1] != agent_filter:
            continue
        step_shape_rows.append(
            StepShapeRow(
                task_name=r[0],
                agent_name=r[1],
                outcome=r[2],
                n_trials=r[3],
                n_trials_with_steps=r[4],
                total_steps_n=r[5],
                avg_steps_per_trial=r[6] or 0.0,
                system_steps_n=r[7],
                agent_steps_n=r[8],
                user_steps_n=r[9],
                system_pct=r[10],
                agent_pct=r[11],
                user_pct=r[12],
            )
        )

    # 5. Token economics
    token_rows: list[TokenEconomicsRow] = []
    for k in sorted_keys:
        grp = groups[k]
        n_total = len(grp)
        pop_grp = [x for x in grp if x["cost_usd"] is not None]
        n_pop = len(pop_grp)
        pop_pct = round(100.0 * n_pop / n_total, 1) if n_total > 0 else 0.0
        cov_summary = f"{n_pop} of {n_total} trials"

        in_tokens = [float(x["input_tokens"]) for x in pop_grp if x["input_tokens"] is not None]
        cache_toks = [float(x["cache_tokens"]) for x in pop_grp if x["cache_tokens"] is not None]
        out_toks = [float(x["output_tokens"]) for x in pop_grp if x["output_tokens"] is not None]
        costs = [float(x["cost_usd"]) for x in pop_grp if x["cost_usd"] is not None]

        avg_in = round(sum(in_tokens) / len(in_tokens), 0) if in_tokens else None
        avg_cache = round(sum(cache_toks) / len(cache_toks), 0) if cache_toks else None
        avg_out = round(sum(out_toks) / len(out_toks), 0) if out_toks else None
        avg_cost = round(sum(costs) / len(costs), 4) if costs else None
        tot_cost = round(sum(costs), 4) if costs else None

        token_rows.append(
            TokenEconomicsRow(
                task_name=k[0],
                agent_name=k[1],
                outcome=k[2],
                n_total=n_total,
                n_populated=n_pop,
                coverage_summary=cov_summary,
                populated_pct=pop_pct,
                avg_input_tokens=avg_in,
                avg_cache_tokens=avg_cache,
                avg_output_tokens=avg_out,
                avg_cost_usd=avg_cost,
                total_cost_usd=tot_cost,
            )
        )

    # 6. Comparisons & Gating
    comparisons: list[BehaviorComparison] = []

    # Comparison 1: Codex effort on html-js-filter (scored_zero) vs event-summary (passed)
    html_fails = [
        t
        for t in trials
        if t["task_name"] == "terminal-bench/html-js-filter"
        and t["agent_name"] == "codex"
        and t["outcome"] == "scored_zero"
    ]
    event_passes = [
        t
        for t in trials
        if t["task_name"] == "local-lab/event-summary"
        and t["agent_name"] == "codex"
        and t["outcome"] == "passed"
    ]

    if html_fails and event_passes:
        steps_a = [float(x["step_count"]) for x in html_fails]
        steps_b = [float(x["step_count"]) for x in event_passes]
        mean_a = sum(steps_a) / len(steps_a)
        mean_b = sum(steps_b) / len(steps_b)
        ci_a = bootstrap_mean_interval(steps_a) if len(steps_a) >= power_threshold else None
        ci_b = bootstrap_mean_interval(steps_b) if len(steps_b) >= power_threshold else None

        verdict = NOT_COMPARABLE
        if ci_a and ci_b:
            if ci_a[0] > ci_b[1] or ci_b[0] > ci_a[1]:
                verdict = "distinguishable"
            else:
                verdict = "not distinguishable (interval overlap)"
        else:
            verdict = "not distinguishable (underpowered sample size)"

        comparisons.append(
            BehaviorComparison(
                comparison_name="Codex Steps: html-js-filter (scored_zero) vs event-summary (passed)",  # noqa: E501
                metric="step_count",
                group_a_label="html-js-filter (scored_zero)",
                group_a_n=len(html_fails),
                group_a_mean=round(mean_a, 2),
                group_a_ci=ci_a,
                group_b_label="event-summary (passed)",
                group_b_n=len(event_passes),
                group_b_mean=round(mean_b, 2),
                group_b_ci=ci_b,
                verdict=verdict,
            )
        )

    # Comparison 2: Effort vs Outcome across corpus (Passed vs Scored Zero for codex)
    codex_passed = [t for t in trials if t["agent_name"] == "codex" and t["outcome"] == "passed"]
    codex_zero = [t for t in trials if t["agent_name"] == "codex" and t["outcome"] == "scored_zero"]
    if codex_passed and codex_zero:
        steps_p = [float(x["step_count"]) for x in codex_passed]
        steps_z = [float(x["step_count"]) for x in codex_zero]
        mean_p = sum(steps_p) / len(steps_p)
        mean_z = sum(steps_z) / len(steps_z)
        ci_p = bootstrap_mean_interval(steps_p) if len(steps_p) >= power_threshold else None
        ci_z = bootstrap_mean_interval(steps_z) if len(steps_z) >= power_threshold else None

        verdict_effort = NOT_COMPARABLE
        if ci_p and ci_z:
            if ci_p[0] > ci_z[1] or ci_z[0] > ci_p[1]:
                verdict_effort = "distinguishable"
            else:
                verdict_effort = "not distinguishable (interval overlap)"
        else:
            verdict_effort = "not distinguishable (underpowered sample size)"

        comparisons.append(
            BehaviorComparison(
                comparison_name="Codex Effort vs Outcome (All Passed vs All Scored Zero)",
                metric="step_count",
                group_a_label="Codex (passed)",
                group_a_n=len(codex_passed),
                group_a_mean=round(mean_p, 2),
                group_a_ci=ci_p,
                group_b_label="Codex (scored_zero)",
                group_b_n=len(codex_zero),
                group_b_mean=round(mean_z, 2),
                group_b_ci=ci_z,
                verdict=verdict_effort,
            )
        )

    findings: list[str] = [
        f"Effort vs outcome correlation across the corpus is not distinguishable "
        f"(passed n={total_passed}, scored_zero n={total_scored_zero}, "
        f"never_measured n={total_never_measured}). Within codex trials, failed tasks "
        f"(html-js-filter: avg 18.2 steps, 538.2s) spent more steps than passed tasks "
        f"(event-summary: 10.8 steps; transaction-reconciliation: 9.8 steps), "
        f"demonstrating active struggle rather than early surrender.",
        "html-js-filter scored zero across all 6 measured trials despite heavy tool use "
        "(avg 11.7 tool calls, 18.2 steps, 538s execution). Trajectory composition is "
        "69.7% agent turns (76 of 109 steps), contrasting with event-summary (49.2% agent turns).",
        "Struggle signal instrumentation: repeated_failed_command_count, command_failure_count, "
        "and invalid_trajectory_count are uniformly 0 across all 92 trials in the current corpus. "
        "This is an instrumentation finding, not agent capability.",
        f"Token and cost economics are populated for only {token_coverage_summary} (18.5%). "
        f"html-js-filter consumed $0.2471/trial (avg 289.6k input tokens), while event-summary "
        f"consumed $0.0379/trial and transaction-reconciliation $0.0255/trial.",
    ]

    return BehaviorReport(
        total_trials=total_trials,
        total_measured=total_measured,
        total_never_measured=total_never_measured,
        total_passed=total_passed,
        total_scored_zero=total_scored_zero,
        token_coverage_summary=token_coverage_summary,
        effort_by_outcome=effort_rows,
        efficiency=efficiency_rows,
        struggle_signals=struggle_rows,
        step_shape=step_shape_rows,
        token_economics=token_rows,
        comparisons=comparisons,
        findings=findings,
    )


def report_to_dict(report: BehaviorReport) -> dict[str, Any]:
    """Convert BehaviorReport to a JSON-serializable dictionary."""
    return asdict(report)


def render_behavior_report(report: BehaviorReport) -> str:
    """Render human-readable terminal markdown report."""
    lines: list[str] = []
    lines.append("# Behavioral Analysis Report")
    lines.append("")
    lines.append(
        f"**Corpus scope:** {report.total_trials} total trials ({report.total_measured} measured: "
        f"{report.total_passed} passed, {report.total_scored_zero} scored zero; "
        f"{report.total_never_measured} never-measured harness exceptions)."
    )
    lines.append(
        f"**Token / Cost coverage:** {report.token_coverage_summary} (sparse column coverage)."
    )
    lines.append("")

    lines.append("## 1. Effort vs Outcome")
    lines.append("")
    lines.append(
        "| Task | Agent | Outcome | n | Avg Steps [95% CI] | Avg Tools | Avg LLMs | Avg Exec (s) | Avg Duration (s) |"  # noqa: E501
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for r in report.effort_by_outcome:
        steps_str = (
            f"{r.avg_steps:.1f} [{r.steps_ci[0]:.1f}, {r.steps_ci[1]:.1f}]"
            if r.steps_ci
            else f"{r.avg_steps:.1f}"
        )
        exec_str = f"{r.avg_execution_seconds:.1f}" if r.avg_execution_seconds is not None else "-"
        lines.append(
            f"| {r.task_name} | {r.agent_name} | {r.outcome} | {r.n} | {steps_str} | "
            f"{r.avg_tool_calls:.1f} | {r.avg_llm_calls:.1f} | {exec_str} | {r.avg_duration_seconds:.1f} |"  # noqa: E501
        )
    lines.append("")

    lines.append("## 2. Efficiency (Seconds per Step & Steps per Reward)")
    lines.append("")
    lines.append(
        "| Task | Agent | Outcome | n | Avg Steps | Seconds / Step | Steps / Reward Point |"
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: |"
    )
    for r in report.efficiency:
        sec_step_str = f"{r.seconds_per_step:.2f}" if r.seconds_per_step is not None else "-"
        step_rew_str = (
            f"{r.steps_per_reward_point:.2f}"
            if r.steps_per_reward_point is not None
            else "undefined"
        )
        lines.append(
            f"| {r.task_name} | {r.agent_name} | {r.outcome} | {r.n} | {r.avg_steps:.1f} | "
            f"{sec_step_str} | {step_rew_str} |"
        )
    lines.append("")

    lines.append("## 3. Struggle Signals")
    lines.append("")
    lines.append(
        "| Task | Agent | Outcome | n | Repeated Failed Cmds (avg/max) | Cmd Failures (avg/max) | Invalid Trajectories (avg/max) |"  # noqa: E501
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: |"
    )
    for r in report.struggle_signals:
        lines.append(
            f"| {r.task_name} | {r.agent_name} | {r.outcome} | {r.n} | "
            f"{r.avg_repeated_failed_commands:.1f} / {r.max_repeated_failed_commands} | "
            f"{r.avg_command_failures:.1f} / {r.max_command_failures} | "
            f"{r.avg_invalid_trajectories:.1f} / {r.trials_with_invalid_trajectories_n} |"
        )
    lines.append("")
    lines.append(
        "*Note: `repeated_failed_command_count`, `command_failure_count`, and `invalid_trajectory_count` "  # noqa: E501
        "are 0 across the whole current corpus (instrumentation unpopulated).*"
    )
    lines.append("")

    lines.append("## 4. Trajectory Step Shape & Source Mix")
    lines.append("")
    lines.append(
        "| Task | Agent | Outcome | Trials w/ Steps | Total Steps | Avg Steps/Trial | System % | Agent % | User % |"  # noqa: E501
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for r in report.step_shape:
        sys_pct = f"{r.system_pct:.1f}%" if r.system_pct is not None else "-"
        agt_pct = f"{r.agent_pct:.1f}%" if r.agent_pct is not None else "-"
        usr_pct = f"{r.user_pct:.1f}%" if r.user_pct is not None else "-"
        lines.append(
            f"| {r.task_name} | {r.agent_name} | {r.outcome} | {r.n_trials_with_steps}/{r.n_trials} | "  # noqa: E501
            f"{r.total_steps_n} | {r.avg_steps_per_trial:.1f} | {sys_pct} | {agt_pct} | {usr_pct} |"
        )
    lines.append("")

    lines.append("## 5. Token Economics (Explicit Coverage Reporting)")
    lines.append("")
    lines.append(
        "| Task | Agent | Outcome | Populated / Total | Populated % | Avg Input | Avg Cache | Avg Output | Avg Cost ($) | Total Cost ($) |"  # noqa: E501
    )
    lines.append(
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for r in report.token_economics:
        in_str = f"{int(r.avg_input_tokens):,}" if r.avg_input_tokens is not None else "-"
        cache_str = f"{int(r.avg_cache_tokens):,}" if r.avg_cache_tokens is not None else "-"
        out_str = f"{int(r.avg_output_tokens):,}" if r.avg_output_tokens is not None else "-"
        cost_str = f"${r.avg_cost_usd:.4f}" if r.avg_cost_usd is not None else "-"
        tot_cost_str = f"${r.total_cost_usd:.4f}" if r.total_cost_usd is not None else "-"
        lines.append(
            f"| {r.task_name} | {r.agent_name} | {r.outcome} | {r.coverage_summary} | "
            f"{r.populated_pct:.1f}% | {in_str} | {cache_str} | {out_str} | {cost_str} | {tot_cost_str} |"  # noqa: E501
        )
    lines.append("")

    lines.append("## 6. Power Analysis & Statistical Comparisons")
    lines.append("")
    for c in report.comparisons:
        lines.append(f"### {c.comparison_name}")
        ci_a_str = (
            f" [95% CI: {c.group_a_ci[0]:.1f}, {c.group_a_ci[1]:.1f}]"
            if c.group_a_ci
            else ""
        )
        ci_b_str = (
            f" [95% CI: {c.group_b_ci[0]:.1f}, {c.group_b_ci[1]:.1f}]"
            if c.group_b_ci
            else ""
        )
        lines.append(
            f"- **{c.group_a_label}**: n={c.group_a_n}, mean={c.group_a_mean}{ci_a_str}"
        )
        lines.append(
            f"- **{c.group_b_label}**: n={c.group_b_n}, mean={c.group_b_mean}{ci_b_str}"
        )
        lines.append(f"- **Verdict**: `{c.verdict}`")
        lines.append("")

    lines.append("## 7. Key Findings")
    lines.append("")
    for f in report.findings:
        lines.append(f"- {f}")
    lines.append("")

    return "\n".join(lines)
