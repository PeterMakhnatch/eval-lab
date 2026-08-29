"""Run Wave 1 + Wave 2 Z.ai OpenCode MCP analysis and write report artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.zai_analysis import (
    build_seed_blocked_contrasts,
    collect_wave1_trials,
    collect_wave2_trials,
    run_t1_analysis,
)
from evallab.zai_report import (
    generate_calibrated_markdown_report,
    generate_source_manifest,
    generate_summary_json,
)

WAVE1_RUNS_DIR = Path("research/evidence/runs")
WAVE2_RUNS_DIR = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/zai-opencode-experiments/runs/zai-opencode-wave2"
)
OUTPUT_DIR = Path("research/analysis")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Scanning Wave 1 from: {WAVE1_RUNS_DIR}")
    w1_trials = collect_wave1_trials(WAVE1_RUNS_DIR)
    print(f"Collected {len(w1_trials)} Wave 1 trials.")

    print(f"Scanning Wave 2 from: {WAVE2_RUNS_DIR}")
    w2_trials = collect_wave2_trials(WAVE2_RUNS_DIR)
    print(f"Collected {len(w2_trials)} Wave 2 trials.")

    all_trials = w1_trials + w2_trials
    print(f"Total combined trials: {len(all_trials)}")

    contrasts = build_seed_blocked_contrasts(all_trials)
    t1_results = run_t1_analysis(all_trials)

    # 1. Summary JSON
    summary_data = generate_summary_json(all_trials, contrasts, t1_results)
    summary_path = OUTPUT_DIR / "zai-opencode-mcp-wave1-wave2-summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2, sort_keys=True)
    print(f"Wrote summary JSON: {summary_path}")

    # 2. Calibrated Markdown Report
    report_md = generate_calibrated_markdown_report(all_trials, contrasts, t1_results)
    report_path = OUTPUT_DIR / "zai-opencode-mcp-wave1-wave2-analysis-2026-08-29.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"Wrote report Markdown: {report_path}")

    # 3. Source Manifest
    manifest_data = generate_source_manifest(all_trials)
    manifest_path = OUTPUT_DIR / "zai-opencode-mcp-manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, sort_keys=True)
    print(f"Wrote source manifest: {manifest_path}")

    print("\nDone generating all Wave 1 + Wave 2 analysis artifacts.")


if __name__ == "__main__":
    main()
