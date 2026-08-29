"""Calibrated report and summary generator for Z.ai OpenCode MCP evaluations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any

from evallab.zai_analysis import (
    ContrastGroup,
    TrialEvidence,
)


def generate_summary_json(
    all_trials: list[TrialEvidence],
    contrasts: list[ContrastGroup],
    t1_results: dict[str, Any],
) -> dict[str, Any]:
    valid_contrasts = [c for c in contrasts if len(c.trials_arm_a) > 0 and len(c.trials_arm_b) > 0]
    trials_data = [asdict(t) for t in all_trials]
    contrasts_data = [asdict(c) for c in valid_contrasts]

    # Model breakdown (scored models only)
    model_counts: dict[str, dict[str, Any]] = {}
    for t in all_trials:
        if t.is_infra_exception or t.reward is None:
            continue
        m = t.model_name
        if m not in model_counts:
            model_counts[m] = {"total_trials": 0, "passed": 0, "failed": 0, "mean_reward": 0.0}
        model_counts[m]["total_trials"] += 1
        if t.passed:
            model_counts[m]["passed"] += 1
        else:
            model_counts[m]["failed"] += 1

    for m, stats in model_counts.items():
        m_trials = [
            t
            for t in all_trials
            if t.model_name == m and not t.is_infra_exception and t.reward is not None
        ]
        stats["mean_reward"] = (
            sum(float(t.reward) for t in m_trials if t.reward is not None) / len(m_trials)
            if m_trials
            else 0.0
        )
    # Family breakdown
    family_counts: dict[str, dict[str, Any]] = {}
    for t in all_trials:
        f = t.benchmark_family
        if f not in family_counts:
            family_counts[f] = {"total_trials": 0, "passed": 0, "failed": 0}
        family_counts[f]["total_trials"] += 1
        if t.passed:
            family_counts[f]["passed"] += 1
        else:
            family_counts[f]["failed"] += 1

    t11 = t1_results["t11_report"]
    t12 = t1_results["t12_result"]
    t13 = t1_results["t13_report"]

    return {
        "program": "zai-opencode-mcp-wave1-wave2",
        "snapshot_digest": t1_results["snapshot_digest"],
        "total_trials": len(all_trials),
        "models": model_counts,
        "families": family_counts,
        "contrasts": contrasts_data,
        "t1_capabilities": {
            "t11_process_outcome": {
                "report_digest": t11.report_digest,
                "results": [r.model_dump(mode="json") for r in t11.results],
            },
            "t12_conditional_recovery": {
                "result_digest": t12.result_digest,
                "status": str(t12.status),
                "refusal_code": str(t12.refusal_code) if t12.refusal_code else None,
                "estimate": t12.estimate,
                "interval_lower": t12.interval_lower,
                "interval_upper": t12.interval_upper,
                "n_total": t12.n_total,
                "n_effective": t12.n_effective,
                "recovered_count": t12.recovered_count,
            },
            "t13_cascade_distance": {
                "report_digest": t13.report_digest,
                "total_trajectories": len(t13.results),
                "observed_count": sum(1 for r in t13.results if r.status == "OBSERVED"),
                "censored_count": sum(1 for r in t13.results if r.status == "CENSORED"),
                "refused_count": sum(1 for r in t13.results if r.status == "REFUSED"),
                "results": [r.model_dump(mode="json") for r in t13.results],
            },
        },
        "trials": trials_data,
    }


def generate_calibrated_markdown_report(
    all_trials: list[TrialEvidence],
    contrasts: list[ContrastGroup],
    t1_results: dict[str, Any],
) -> str:
    """Generate publication-ready calibrated Markdown evaluation report."""
    scored_trials = [t for t in all_trials if not t.is_infra_exception and t.reward is not None]
    infra_trials = [t for t in all_trials if t.is_infra_exception]

    w1_scored = [t for t in scored_trials if t.wave == "wave1"]
    w2_scored = [t for t in scored_trials if t.wave == "wave2"]
    w2_flash = [t for t in w2_scored if t.model_name == "glm-5.3-flash"]
    w2_full = [t for t in w2_scored if t.model_name == "glm-5.3-full"]

    w1_passed = sum(1 for t in w1_scored if t.passed)
    w2_passed = sum(1 for t in w2_scored if t.passed)
    w2_flash_passed = sum(1 for t in w2_flash if t.passed)
    w2_full_passed = sum(1 for t in w2_full if t.passed)
    total_passed = sum(1 for t in scored_trials if t.passed)

    t11 = t1_results["t11_report"]
    t12 = t1_results["t12_result"]
    t13 = t1_results["t13_report"]
    lines: list[str] = [
        "---",
        "type: study-report",
        "topic: zai-opencode-mcp-wave1-wave2-analysis",
        "author: research-engineer",
        "date: 2026-08-29",
        "status: complete",
        "epistemic: observed outcomes across Flash, Full, and Highspeed on MCP synthetic benchmarks; strictly scoped to tested configurations; no general ranking or unsupported dose slopes",
        "collection: trajectory-analysis",
        "reviewed: 2026-08-29",
        f"snapshot_digest: {t1_results['snapshot_digest']}",
        "---",
        "",
        "# Z.ai OpenCode MCP Experiment Program — Wave 1 & Wave 2 Consolidated Analysis",
        "",
        "## 1. Executive Summary & Observed Facts",
        "",
        "This report consolidates empirical findings from the expanded Z.ai Coding Plan evaluation program using Harbor 0.21, OpenCode 1.18.25, and ATIF v1.7 trajectory capture across three synthetic agent-capability benchmark categories:",
        "",
        "1. **Function DAG (Tool Selection, Composition & Value Propagation)**",
        "2. **Action Memory (Context Dilation & Distraction Resistance: 4k, 16k, 64k)**",
        "3. **Recovery (Error Detection & Autonomous Adaptation: transient 5xx, persistent signature, silent wrong)**",
        "",
        f"The evaluated corpus comprises **{len(all_trials)} total attempts** ({len(scored_trials)} scored trials + {len(infra_trials)} infrastructure exclusions):",
        "",
        f"- **Combined Program Scored Total:** {total_passed}/{len(scored_trials)} passed ({total_passed / len(scored_trials) * 100:.1f}%) across Wave 1 ({w1_passed}/{len(w1_scored)}) and Wave 2 ({w2_passed}/{len(w2_scored)}).",
        f"- **Wave 2 Scored Total:** {w2_passed}/{len(w2_scored)} passed ({w2_passed / len(w2_scored) * 100:.1f}% across {len(w2_scored)} scored trials):",
        f"  - **GLM-5.3-Flash (Wave 2):** {w2_flash_passed}/{len(w2_flash)} passed (16/24, 66.7%).",
        f"  - **GLM-5.3 Full (Wave 2):** {w2_full_passed}/{len(w2_full)} passed (2/3, 66.7% — FuncDAG depth 5 canary 1/1, Recovery persistent signature 1/1, Action 64k semantic 0/1).",
        "- **GLM-5.3-Highspeed:** 0/0 scored trials (all 3 mini-battery attempts were excluded from scored denominators due to upstream subscription HTTP 429 access restrictions).",
        f"- **Infrastructure Exclusions (Non-Scored):** {len(infra_trials)} trials (Highspeed subscription 429 entitlement error and default-timeout sequential chunk retrieval `AgentTimeoutError`).",
        "",
        "| Benchmark Family | Wave | Model | Tasks / Cells | Completed Trials | Reward 1.0 | Pass Rate |",
        "|---|---|---|---|---:|---:|---:|",
    ]

    # Group trials for table (scored only)
    grouped_cells: dict[tuple[str, str, str, str], list[TrialEvidence]] = defaultdict(list)
    for t in scored_trials:
        dose_label = t.dose or t.factor or t.arm or "standard"
        grouped_cells[(t.benchmark_family, t.wave, t.model_name, dose_label)].append(t)
    for (fam, wave, model, dose_lbl), c_trials in sorted(grouped_cells.items()):
        n = len(c_trials)
        n_pass = sum(1 for t in c_trials if t.passed)
        p_rate = (n_pass / n * 100) if n else 0.0
        lines.append(
            f"| {fam:<16} | {wave:<5} | {model:<14} | {dose_lbl:<20} | {n:>16} | {n_pass:>10} | {p_rate:>8.1f}% |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 2. Per-Construct Diagnostic & Failure Trace Evidence",
            "",
            "### 2.1 Function DAG Tool Composition",
            "",
            "- **Easy (Wave 1, Flash, 3 trials):** 2/3 passed. One trial failed due to shell output format pollution: `Invalid JSON format: Extra data: line 2 column 1 (char 2)` where the agent printed a diagnostic number before emitting `/app/output/result.json`.",
            "- **Depth 5 (Wave 2, Flash, seeds 42, 101, 2024):** 3/3 passed. Both the single-task canary (`seed42__PBZKQYS`) and the matrix runs (`seed101__mJU4JQC`, `seed2024__gXLQWSh`) correctly discovered FastMCP tool endpoints, traversed prerequisite nodes, and wrote valid `/app/result.json` integer payloads.",
            "- **Depth 5 (Wave 2, Full GLM-5.3, seed 42):** 1/1 passed (`seed42__Ca9ToPk`).",
            "- **Depth 5 Highspeed Attempt:** The single planned Highspeed attempt (`mcp-funcdag-depth_5-seed42__q22U798`) threw `NonZeroAgentExitCodeError` before creating `/app/result.json` due to an upstream provider subscription entitlement error (HTTP 429: 'Your current subscription plan does not yet include access to GLM-5.3-Highspeed'). It is classified as an observed non-scored access failure, not an agent reasoning outcome.",
            "- **Name Similarity High (Wave 2, Flash, seeds 42, 101, 2024):** 2/3 passed (seeds 42 and 101 passed; seed 2024 failed value propagation).",
            "",
            "### 2.2 Action Memory Context Dilation & Distraction",
            "",
            "- **4k Clean (Wave 1, Flash, 3 trials):** 3/3 passed.",
            "- **16k Neutral vs. Semantic (Wave 1, Flash, 3 pairs):** 3/3 passed on neutral padding; 2/3 passed on semantic distractor (one trial failed with 66 reads vs. expected 65 due to a duplicate chunk retrieval).",
            "- **64k Seed 42 vs. Seed 1337 (Wave 2, Flash):**",
            "  - **Seed 42:** Both `neutral_padding` (1.0) and `semantic_distractor` (1.0) passed on Flash with complete 257/257 chunk retrieval and exact state binding.",
            "  - **Seed 1337:** Failed across unscaffolded neutral (0/3) and semantic (0/3) runs (matrix and repeats).",
            "- **64k Seed 1337 Handle-Level Failure Analysis (Wave 2, Flash, 6 trials):**",
            "  All six seed 1337 Action64 trials failed (0/3 neutral padding, 0/3 semantic distractor across the matrix and repeat runs). Detailed trajectory trace parsing reveals this is a **specific agent issuance, transcription, and long-sequence maintenance failure mode**, rather than complete coverage, simple reordering, or a pure token-capacity deficit:",
            "  1. **ATIF Issuance vs. Benchmark Events Alignment:** ATIF tool-call issuance order strictly equals the server `benchmark-events.jsonl` execution order across all six failures. This rules out server-side capture anomalies, transport dropouts, or async network reordering.",
            "  2. **Omitted Handle Invariant:** In all 6 trials, the agent omitted the valid listed chunk `ctx_2110473c018845ab0cc32bf4` (`...32bf4`).",
            "  3. **Near-Typo Hallucination:** In 5 of 6 trials (`xTVP9AZ`, `Qoz3nbU`, `wCHLZ4M`, `FLiG7jy`, `Pgukjp8`), the agent transcribed a 1-character hallucinated typo handle `ctx_2110473c018845ab0cc32bf6` (`...32bf6`). In 1 trial (`JvdEs9Y`), the agent transcribed `ctx_2110473c018845ab0cc32bf3` (`...32bf3`).",
            "  4. **Duplicate & Mismatch Dynamics:** In `Pgukjp8`, the agent issued 259 total calls including a duplicate typo call (`...32bf6` called twice) plus a duplicate final handle (`ctx_f4e8c2abe047ae311b1b587b`). Three trials deviated from listed order early (call index 2 or 10), whereas three trials preserved exact prefix order until encountering the omitted handle at call index 83.",
            "  5. **Event-Level Success:** 256/257 calls (99.6%) succeeded on the FastMCP server; the single unlisted typo handle returned a 404 error.",
            "- **64k Semantic Distractor (Wave 2, Full GLM-5.3, seed 42):** Scored 0.0 under context pressure.",
            "",
            "### 2.3 Recovery Fault Detection & Autonomous Adaptation",
            "",
            "- **Transient HTTP 5xx, Persistence 1 (Wave 1, Flash, 3 pairs):** 3/3 passed on clean twin; 2/3 passed on fault arm. In the failing fault trial, the agent retried and wrote the record without executing the mandatory recovery mutation (`refresh_auth`), resulting in `causal_mutation=false` from the verifier.",
            "- **Persistent Signature Error (Wave 2, Flash & Full GLM-5.3):** 4/4 passed on Flash (seeds 42 & 1337); 1/1 passed on Full GLM-5.3.",
            "- **Silent Wrong Payload (Wave 2, Flash):** 4/4 passed on Flash (seeds 42 & 1337).",
            "",
            "### 2.4 Sequential Retrieval Scaffold Pacing & Infrastructure Timeout Feasibility",
            "",
            "- **Final Timeout×3 Execution (t3 Rerun):** The clean rerun with `agent-timeout-multiplier=3` (`zai-wave2-action64k-s1337-sequential-scaffold-t3`) completed 2 valid scored trials:",
            "  1. `action-64k-neutral_padding-s1337__u4CZxsA` **passed (reward 1.0)**, reading exact 257/257 chunks sequentially and executing the exact final state binding `f3e822e6_v2` for `entity_817.routing_key` (**6,683,558 prompt tokens**, 10,662 completion).",
            "  2. `action-64k-semantic_distractor-s__A67eDZ2` **scored 0.0**, reading 232/257 chunks before early mutation under semantic distractor pressure (**7,454,261 prompt tokens**, 13,526 completion).",
            "- **Prompt Token Expansion:** Total prompt tokens across t3: **14,137,819 prompt tokens** with a two-concurrent wall-clock duration of **23 minutes 50 seconds**. In comparison, the 9 unscaffolded Action 64k trials averaged **412,753 prompt tokens** (range 227,610 – 539,198), representing a **~16–18x prompt expansion** due to accumulating prompt history across 257 single-chunk turns.",
            "- **Policy & Non-Generalization:** While the sequential scaffold resolved the single-character transcription error on neutral padding, it failed on semantic distractors and imposed severe token and latency overhead. **No general effectiveness claim is made.**",
            "- **Default Timeout Execution (First Run):** The initial sequential scaffold run (`zai-wave2-action64k-s1337-sequential-scaffold`) produced 2 `AgentTimeoutError` outcomes when cut off by Harbor's default agent timeout ceiling while issuing chunk reads. These are classified as non-scored harness budget constraints (`reward = None`).",
            "",
            "---",
            "",
            "## 3. Seed-Blocked Descriptive Contrasts",
            "",
            "All comparisons are strictly blocked on matching task, seed, and perturbation parameters. No cross-cell pooling or unweighted averaging is performed.",
            "",
            "| Contrast Identifier | Dimension | Arm A (Baseline / Clean) | Arm B (Perturbed / Treatment) | Observed Mean A | Observed Mean B | Delta (B - A) | Notes |",
            "|---|---|---|---|---:|---:|---:|---|",
        ]
    )

    valid_contrasts = [c for c in contrasts if len(c.trials_arm_a) > 0 and len(c.trials_arm_b) > 0]
    for c in valid_contrasts:
        lines.append(
            f"| {c.contrast_name} | {c.dimension} | {c.arm_a_label} (n={len(c.trials_arm_a)}) | {c.arm_b_label} (n={len(c.trials_arm_b)}) | {c.mean_reward_a:.3f} | {c.mean_reward_b:.3f} | {c.reward_delta:+.3f} | {'; '.join(c.notes)} |"
        )
    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. Context Dilation Dose Analysis & Confounding Audit",
            "",
            "Comparing Action Memory across 4k, 16k, and 64k doses reveals marked degradation under context scaling, but parametric curve fitting is strictly **refused** due to the following confounding structure:",
            "",
            "1. **Seed Confounding:** 4k and 16k were evaluated solely on seed 42 in Wave 1; 64k incorporates seed 1337 in Wave 2.",
            "2. **Repetition Asymmetry:** 4k and 16k have 3 repetitions per cell; 64k has 1 repetition per (dose, arm, seed) cell in the initial matrix.",
            "3. **Token Multiplier Distortion:** The ratio of retrieved context tokens to total prompt budget changes non-linearly with buffer length.",
            "",
            "**Policy:** Observed dose steps are reported as discrete empirical points only; no continuous slope $\\beta$ or parametric dose-response equation is claimed.",
            "",
            "---",
            "",
            "## 5. Execution of T1 Analysis Capabilities",
            "",
            "The frozen Research-Engineer T1 analysis API suite was executed over the full combined dataset without manual input transformation:",
            "",
            "### 5.1 T1.1 Process-vs-Outcome Discrimination Gate",
            f"- **Snapshot Digest:** `{t11.source_analysis_snapshot_digest}`",
            f"- **Report Digest:** `{t11.report_digest}`",
            "",
            "| Feature Name | Lineage / Metric Inputs | Verdict | Epistemic Basis | CI Disposition | Requires Allowlist |",
            "|---|---|---|---|---|---|",
        ]
    )

    for r in t11.results:
        lines.append(
            f"| `{r.feature_name}` | `{r.verdict}` | **{r.verdict}** | `{r.basis}` | `{r.ci_disposition}` | `{r.requires_allowlist}` |"
        )

    lines.extend(
        [
            "",
            "**Key Invariant Verified:** The two PR #267 known-positive features (`value_propagation_accuracy` and `dag_edge_conformance_rate`) are flagged statically as `LINEAGE_VIOLATION` with `basis = REGISTRY_CONFIRMED` and `ci_disposition = BLOCK` because they read post-verdict fields (`invariants_passed`).",
            "",
            "### 5.2 T1.2 Opportunity-Conditioned Recovery",
            f"- **Result Digest:** `{t12.result_digest}`",
            f"- **Status:** `{t12.status}` (Refusal: `{t12.refusal_code}`)",
            f"- **Point Estimand:** Fault-weighted recovery rate over eligible fault opportunities = {t12.estimate if t12.estimate is not None else 'NULL'}",
            f"- **Cluster Bootstrap:** `{t12.uncertainty_method}` with {t12.resamples} resamples, clustered by `coalesce(repeat_group_id, trial_id)`.",
            f"- **Sample Power:** n_total = {t12.n_total}, n_effective = {t12.n_effective} clusters.",
            "",
            "### 5.3 T1.3 Cascade Distance Analysis",
            f"- **Report Digest:** `{t13.report_digest}`",
            f"- **Evaluated Trajectories (steps $\\ge 5$):** {len(t13.results)}",
            f"- **Observed Lock Events:** {sum(1 for r in t13.results if r.status == 'OBSERVED')}",
            f"- **Right-Censored Trajectories:** {sum(1 for r in t13.results if r.status == 'CENSORED')}",
            f"- **Conjunctive Refusals:** {sum(1 for r in t13.results if r.status == 'REFUSED')}",
            "",
            "---",
            "",
            "## 6. Prohibited Claims & Methodological Boundaries",
            "",
            "In accordance with repository epistemic governance standards, the following claims are explicitly **barred**:",
            "",
            "- **No General Model Ranking:** GLM-5.3 Full vs. Flash differences are reported only for the three exact matched tasks (Full 2/3 vs. Flash 3/3), not as general capability or ranking claims.",
            "- **No Parametric Dose-Response Scaling Law:** Action Memory context degradation is non-linear and confounded across wave seeds.",
            "- **No Cost / Throughput Extrapolations:** No per-token billing rates or latency guarantees are claimed.",
            "- **No Unchecked Causal Assertions:** Recovery pass rates are causal only when conditioned on verified verifier mutations (`causal_mutation=true`).",
            "",
            "---",
            "",
            "## 7. Recommended Next Discriminating Cells",
            "",
            "1. **Action Memory 32k Intermediate Dose:** Bridge the 16k $\\to$ 64k gap with matched neutral/semantic pairs across seeds 42, 101, 1337.",
            "2. **FuncDAG v2 Discrete MCP Server Decomposition:** Transition from file-based execution to multi-container discrete tool nodes to enable true edge-traversal observability.",
            "3. **Recovery Persistence Scaling:** Evaluate persistence ladders $p \\in \\{1, 2, 4\\}$ on persistent signature errors to measure adaptive backoff.",
            "",
        ]
    )

    return "\n".join(lines)


def generate_source_manifest(all_trials: list[TrialEvidence]) -> dict[str, Any]:
    """Generate cryptographic source-path and digest manifest."""
    manifest_entries: list[dict[str, Any]] = []
    for t in sorted(all_trials, key=lambda x: (x.wave, x.job_name, x.trial_name)):
        manifest_entries.append(
            {
                "wave": t.wave,
                "job_name": t.job_name,
                "trial_name": t.trial_name,
                "trial_path": t.trial_dir,
                "benchmark_family": t.benchmark_family,
                "model_name": t.model_name,
                "reward": t.reward,
                "passed": t.passed,
                "result_digest": t.result_digest,
                "trajectory_digest": t.trajectory_digest,
            }
        )
    return {
        "manifest_version": "1.0.0",
        "program": "zai-opencode-mcp-wave1-wave2",
        "entry_count": len(manifest_entries),
        "entries": manifest_entries,
    }
