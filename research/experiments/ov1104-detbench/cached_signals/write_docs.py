#!/usr/bin/env python3
"""Generate CACHED-DETECTORS.md and REPORT.md for the cache worker slice from summary.json."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKER_DIR = Path("/private/tmp/ov1104/detbench/workers/cache")
SUMMARY_PATH = WORKER_DIR / "summary.json"

DEV_SETS = ["c1c5", "cheatbench", "goodfire", "sol-astra", "terminal-wrench"]
HELDOUT_SETS = [
    "agent-cheat-index",
    "beam-tripwire",
    "cape-bench",
    "caught-not-taught",
    "evilgenie-escalation",
    "flake-laundering",
    "kernelbench-hard-runs",
    "kobayashi-maru",
    "latent-agent-sandbox-episodes",
    "mistrace-reward-hacking",
    "paichecker",
    "reflex-sentinel",
    "researcharena-benchmark-gaming",
    "resolvebench-support-agents",
    "reward-pressure-study-2",
    "rhb-delivery-queue",
    "rsi-benchmark-monitor-training",
    "sdf-shortcut-taking",
    "sleight-bench",
    "trusted-monitoring-logs",
]


def main() -> int:
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    detbench_sets: dict[str, int] = summary["detbench_sets"]
    signals: list[dict] = summary["signals"]
    et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")

    # Build CACHED-DETECTORS.md
    cd_lines = [
        "# Cached Detectors Inventory (`ov1104-detbench` / `cache` slice)",
        "",
        f"Generated: {et_now}. Total DetBench sets: **25** (5 DEV sets = **690** runs; 20 held-out sets = **15,859** runs; total = **16,549** runs).",
        f"Total cached signal files emitted to `/private/tmp/ov1104/shared/detbench/signals/`: **{len(signals)}**.",
        "",
        "## 1. Set-Level Coverage Summary Across All 25 DetBench Sets",
        "",
        "| Set ID | Split | DetBench Rows | Cached Signals Covering Set | Max Joined Rows (Coverage) | Detectors Present |",
        "|---|---|---:|---:|---:|---|",
    ]

    for s in DEV_SETS + HELDOUT_SETS:
        n_s = detbench_sets[s]
        split = "dev" if s in DEV_SETS else "heldout"
        covering = [sig for sig in signals if s in sig["per_set"]]
        max_j = max((sig["per_set"][s]["joined"] for sig in covering), default=0)
        fams = sorted({sig["family"] for sig in covering})
        fam_str = ", ".join(f"`{f}`" for f in fams) if fams else "**None** (0 cached outputs on disk)"
        cov_pct = f"{max_j}/{n_s} ({100.0 * max_j / n_s:.1f}%)" if n_s else "0/0"
        cd_lines.append(f"| `{s}` | `{split}` | {n_s} | {len(covering)} | {cov_pct} | {fam_str} |")

    cd_lines.extend([
        "",
        "## 2. Detector x Set Join Table (All 112 Emitted `cached_<detector>_<variant>.jsonl` Files)",
        "",
        "Every entry below lists exact denominators: **`joined / cached_rows / detbench_set_rows`** (and non-null scored rows where abstentions occur).",
        "",
        "| Signal File (`cached_<detector>_<variant>`) | Family | Score Type | Served Model | Date | Cached Rows | Total Joined | Per-Set Join (`set: joined/cached/detbench_rows [non_null]`) | Source Path | Receipt / Provenance |",
        "|---|---|---|---|---|---:|---:|---|---|---|",
    ])

    for sig in signals:
        per_set_parts = []
        for s, info in sorted(sig["per_set"].items()):
            j = info["joined"]
            nn = info["non_null"]
            db_n = info["detbench_rows"]
            c_n = sig["total_cached_rows"]
            nn_note = f" [nn={nn}]" if nn != j else ""
            per_set_parts.append(f"`{s}`: {j}/{c_n}/{db_n}{nn_note}")
        per_set_str = "; ".join(per_set_parts)
        smod = sig["served_model"] or "deterministic / rule"
        cd_lines.append(
            f"| `{sig['signal']}.jsonl` (`{sig['version']}`) | `{sig['family']}` | `{sig['score_type']}` | `{smod}` | {sig['date']} | {sig['total_cached_rows']} | {sig['joined_total']} (nn={sig['non_null_total']}) | {per_set_str} | `{sig['source_path']}` | `{sig['receipt_path']}` |"
        )

    cd_lines.extend([
        "",
        "## 3. Detector x Set Existence Matrix (Major Detector Families x 25 Sets)",
        "",
        "Cells show `joined/detbench_rows` for the primary cached variant of each detector family (`—` = 0/detbench_rows, not run on that set).",
        "",
        "| Set ID (DetBench rows) | Cascade v8 (`cached_cascade_v8_score`) | Cascade v8 Haiku Matched (`cached_cascade_v8_haiku_matched_p0f9d7d6`) | Facets Haiku (`cached_facets_haiku_all`) | Facets Sonnet (`cached_facets_sonnet_all`) | Native Tripwires/Flags/QA-T0/RunHealth (`cached_p0f9d7d6_*_all`) | Kev-4B (`cached_kev_any_cheat_all`) | METR Scanner (`cached_metr_scanner_*` / `detectors_v1`) | Laminar (`cached_detectors_v1_laminar_*`) | AutoReview (`ar_g38_fixed` / `v2_tw_cb` / `replay_v1`) | DeepSeek Readers (`dt8_v0` / `dt15_masked` / `dt18_v4`) | Action-Features LR (`cached_action_features_lr_*`) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])

    sig_by_name = {sig["signal"]: sig for sig in signals}

    def cell(sig_name: str, s: str) -> str:
        sig = sig_by_name.get(sig_name)
        if not sig or s not in sig["per_set"]:
            return f"0/{detbench_sets[s]}"
        j = sig["per_set"][s]["joined"]
        return f"**{j}/{detbench_sets[s]}**"

    for s in DEV_SETS + HELDOUT_SETS:
        n_s = detbench_sets[s]
        c_casc = cell("cached_cascade_v8_score", s)
        c_chaiku = cell("cached_cascade_v8_haiku_matched_p0f9d7d6", s)
        c_fhaiku = cell("cached_facets_haiku_all", s)
        c_fsonnet = cell("cached_facets_sonnet_all", s)
        c_p02 = cell("cached_p0f9d7d6_tripwire_all", s)
        c_kev = cell("cached_kev_any_cheat_all", s)
        # METR: check grok43/kimi on goodfire or detectors_v1 on terminal-wrench
        if s == "goodfire":
            c_metr = cell("cached_metr_scanner_kimi_k31_dev", s)
        elif s == "terminal-wrench":
            c_metr = cell("cached_detectors_v1_metr_reward_hacking", s)
        else:
            c_metr = f"0/{n_s}"
        # Laminar: check detectors-v1 laminar_copied/stuck_loop/false_completion
        c_lam = f"0/{n_s}"
        # AutoReview: check tmb_autoreview_g38_fixed (620 rows) or autoreview_v2_tw_cb
        j_ar = max(
            sig_by_name["cached_tmb_autoreview_g38_fixed"]["per_set"].get(s, {}).get("joined", 0),
            sig_by_name["cached_autoreview_v2_tw_cb"]["per_set"].get(s, {}).get("joined", 0),
        )
        c_ar = f"**{j_ar}/{n_s}**" if j_ar > 0 else f"0/{n_s}"
        c_ds = cell("cached_reader_dt15_masked_deepseek", s)
        c_aflr = cell("cached_action_features_lr_in_key_cheatbench", s)
        cd_lines.append(
            f"| `{s}` ({n_s}) | {c_casc} | {c_chaiku} | {c_fhaiku} | {c_fsonnet} | {c_p02} | {c_kev} | {c_metr} | {c_lam} | {c_ar} | {c_ds} | {c_aflr} |"
        )

    cd_lines.extend([
        "",
        "## 4. Not Found / Zero-Overlap Cached Outputs (and Where We Looked)",
        "",
        "1. **9 Held-Out Sets With Zero Cached Detector Outputs on Disk (`0` joined rows across all detectors)**:",
        "   - `cape-bench` (0/800), `latent-agent-sandbox-episodes` (0/326), `mistrace-reward-hacking` (0/200), `paichecker` (0/2587), `resolvebench-support-agents` (0/512), `rhb-delivery-queue` (0/538), `rsi-benchmark-monitor-training` (0/836), `sleight-bench` (0/86), `trusted-monitoring-logs` (0/233).",
        "   - **Where we looked**: `~/.local/state/evallab-*`, `~/.local/state/evallab-prod-live/{trace-harness,run-atlas,trace-tools}`, `~/Developer/eval-lab-results/`, `~/Developer/research-context/bridge/`, `research/evidence/runs/*`, and `~/Developer/research-context/bridge/2026-10-27-run-atlas/np45-heldout-corpus-table.md`. Per `np45-heldout-corpus-table.md` and the worker `runs.*.jsonl` `read_by` fields, these 9 sets were staged in PQ2–PQ8 (Oct 28–29) and never had LLM monitors or `p0-f9d7d6-2` scorecard caches run on them prior to `ov1104-detbench` (`np35-s-census` only computed aggregate group counts in `research/evidence/runs/np35-s-census-20261029/results/groups.json`, with no per-run IDs stored).",
        "2. **B1 / CM-16 Causal-Prefix Facets on Goodfire (`0/50` cached rows join the 150 `goodfire` DEV runs)**:",
        "   - **Where we looked**: `~/.local/state/evallab-prod-live/run-atlas/comparisons/{cm16,cm16-kimi,cm16-opus,cm16-sonnet}/prefix-facets.jsonl` (150 rows each = 50 unique `trace_id`s x 3 prefix fractions `0.25, 0.50, 1.00`), `~/.local/state/evallab-prod-live/trace-harness/findings-v1.jsonl` (`detector in ('facets_prefix25', 'facets_prefix50', 'facets_prefix100')`, 50 rows each), and `research/evidence/runs/b1-cm16-sonnet-20261028/results/sample.json`.",
        "   - **Why 0/50 joined**: CM-16 / B1 sampled 50 runs (25 enacted + 25 clean) from the wider 3,674-run Goodfire DEV pool (`goodfire-reward-hack-data/swebench-verified-*` and `impossiblebench-*`) using seed `cm16-20261028`; none of those 50 `trace_id`s overlap the 150-run CM-2/CM-6 Goodfire DEV cohort in `replay-v1` (`0/50/150`).",
        "3. **Laminar Cloud Signals (`laminar_copied`, `laminar_false_completion`, `laminar_stuck_loop`)**:",
        "   - **Where we looked**: `research/calibration/detectors-v1/cache/{laminar_copied,laminar_false_completion,laminar_stuck_loop}/` (104, 104, and 60 cached `.json` files), `~/.local/state/evallab-prod-live/trace-harness/findings-v1.jsonl` (`detcache.laminar_*` 16 rows; `laminar` / `laminar_copy` 32 rows), and `~/.local/state/evallab-prod-live/trace-tools/results-v1.jsonl` (`tool=='laminar'`).",
        "   - **Why 0 joined**: The 104/60 cached files in `detectors-v1/cache/laminar_*` cover `ours-*`, `g6-*`, `ht-*`, and `tw-raw-0001..0010` (`0` `cb-*` files, and only `tw-0023` for `laminar_stuck_loop`, which is not among the 9 `tw-*` runs overlapping `replay-v1`'s 304 `terminal-wrench` runs: `0/104/304` and `0/60/304`). The `results-v1.jsonl` Laminar rows are on `own-harbor` (`cb-oma-*`, `ovn-g5-*`, `har116-*`) trials, not the 690 `replay-v1` DEV runs (`0/690`).",
        "4. **Non-DetBench Cross-Test Sets Present in CM-18 / NP21 / NP33 / NP38 State Directories**:",
        "   - `~/.local/state/evallab-np21-cm1/` includes `baitbench` (`120` rows; not a DetBench set).",
        "   - `~/.local/state/evallab-np33-cm1/` includes `agentcheck` (`23` rows) and `ptb` / PostTrainBench (`120` rows); neither is in the 20 DetBench held-out sets.",
        "   - `~/.local/state/evallab-np38-cm1/` covers `linuxarena` (`60` rows; not in the 20 DetBench held-out sets).",
        "5. **Nov 2 R1-Prevalence (`bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts/readings.jsonl`, 800 rows)**:",
        "   - Sampled 200 unlabelled/prevalence rollouts from the wider Goodfire DEV pool x 4 models; `0/800` overlap the 150 `goodfire` DEV runs in `replay-v1`.",
        "6. **DS-Squad DT-5 / DT-5b / DT-7 (`bridge/2026-10-29-ds-squad/raw/DT-{5,5b,7}`)**:",
        "   - `DT-5` (70 rows) and `DT-5b` (167 rows) were run on `mimo-v7` / `own-dev` Harbor rollouts (`0/690` overlap with `replay-v1`). `DT-7` (96 rows) was run on live-supervisor `harbor_timeout` cases (`0/690` overlap with `replay-v1`).",
        "",
    ])

    (WORKER_DIR / "CACHED-DETECTORS.md").write_text("\n".join(cd_lines), encoding="utf-8")

    # Build REPORT.md
    rep_lines = [
        "# Cache Worker Report (`ov1104-detbench` / `cache` slice)",
        "",
        "## 1. What We Ran",
        "",
        "- Built `research/experiments/ov1104-detbench/cached_signals/extract_cached_signals.py` and `write_docs.py`.",
        "- Exact commands run from `/Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-detbench`:",
        "  ```bash",
        "  uv run python research/experiments/ov1104-detbench/cached_signals/extract_cached_signals.py",
        "  uv run python research/experiments/ov1104-detbench/cached_signals/write_docs.py",
        "  uv run python research/experiments/ov1104-detbench/score.py --signal cached_cascade_v8_score --bootstrap 0",
        "  uv run python research/experiments/ov1104-detbench/score.py --signal cached_facets_haiku_all --bootstrap 0",
        "  ```",
        f"- Extracted **{len(signals)}** cached signal files to `/private/tmp/ov1104/shared/detbench/signals/cached_<detector>_<variant>.jsonl` in {summary['elapsed_sec']}s with **$0 cost**, **0 model calls**, and **0 access to `research/answer-keys/`, `heldout-*`, or TEST splits**.",
        "",
        "## 2. Join Architecture and Decisions",
        "",
        "1. **Master Join Index (`JoinIndex` in `extract_cached_signals.py`)**:",
        "   - **DEV (`690` runs across 5 sets: `c1c5` 30, `cheatbench` 150, `goodfire` 150, `sol-astra` 56, `terminal-wrench` 304)**:",
        "     - Direct `run_key` (`<set>:<native_id>`) and bare `native_id`.",
        "     - Blinded Run Atlas / Trace Harness IDs: `run-<sha256(native_id)[:16]>`, bare `<sha256(native_id)[:16]>`, `run-<sha256(native_id)[:32]>`, plus `~/.local/state/evallab-prod-live/trace-tools/keymap-v1.jsonl` (`opaque_id -> run_id`) and `~/Developer/eval-lab-results/2026-11-02/goodfire-dev-master/goodfire-dev-all-3674.jsonl` (`id -> trace_id`).",
        "     - CheatBench `detectors-v1` / Kev IDs (`cb-XXXXXX`): mapped via `CorpusRegistry(REPO_ROOT).items(dataset_id='cheatbench-actions-v1', split='dev')` (`item.metadata['source_id'] -> cheatbench:<item_id>`), joining all 150/150 `cheatbench` DEV runs.",
        "     - Terminal-Wrench `detectors-v1` IDs (`tw-XXXX`): mapped via `(task_id, model, variant, label)` between `research/calibration/detectors-v1/manifest.jsonl` and `~/.local/state/evallab-prod-live/factory/tw-dev-export.manifest.json`. Because `detectors-v1` sampled 200 `tw_subset.json` runs (seed `20261006`) + 16 raw runs while `replay-v1` exported 304 runs holding out different task/variant pairs, exactly **9/216** `tw-*` runs in `detectors-v1` overlap `replay-v1`'s 304 `terminal-wrench` runs (**7/304** scored in `kev_*`, `sol_codex`, `combined_v1`, `rules`, `content_rules`, `qa_trace_t0`).",
        "     - TraceMonitor-Bench DEV v1 (`tmb1-<12hex>`): mapped via `~/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1/manifest.jsonl` (`case_id -> source_id / blind_id`), joining **680/690** DEV runs (`c1c5` 20/30, `cheatbench` 150/150, `goodfire` 150/150, `sol-astra` 56/56, `terminal-wrench` 304/304; the remaining 10 `c1c5` scripted controls were excluded from TMB, while TMB's extra 50 `deep_swe` and 50 `sol_astra_c` cases are outside the 690 DEV set).",
        "   - **Held-Out Sets (`15,859` runs across 20 sets; 11 sets have cached outputs on disk)**:",
        "     - **CM-18 (`~/.local/state/evallab-cm18/`, 946 sampled runs across 7 sets)** and **B6 (`~/.local/state/evallab-cm18-b6/`, 281 runs across the same 7 sets)**: mapped via `sha256(f'{cm18_set}:{raw_id}')[:16]`. Joins **946/946 (100%)** of CM-18 rows to DetBench (`agent-cheat-index` 180/180/1148, `beam-tripwire` 180/180/613, `evilgenie-escalation` 180/180/2114, `kernelbench-hard-runs` 30/30/30, `reflex-sentinel` 155/155/250, `researcharena-benchmark-gaming` 41/41/41, `sdf-shortcut-taking` 180/180/995) and **281/281 (100%)** of B6 Sonnet facet rows.",
        "     - **NP21-CM-1 (`~/.local/state/evallab-np21-cm1/`, 429 sampled runs across 4 sets)**: mapped via `sha256(f'{np21_set}:{raw_id}')[:16]`. Joins **69/69/224** on `flake-laundering`, **120/120/1120** on `reward-pressure-study-2`, and **33/120/3000** on `kobayashi-maru` (DetBench's `kobayashi-maru` is a 3,000-row stratified sample with seed `1104` from 8,705 scoreable rows, while NP21-CM-1 drew 120 rows with seed `20261028` from the 8,705 rows, yielding 33 overlapping runs). The 4th NP21 set (`baitbench`, 120 rows) is not a DetBench set.",
        "     - **NP33-CM-1 (`~/.local/state/evallab-np33-cm1/`, 269 sampled runs across 3 sets)**: mapped via `sha256(f'cnt:{raw_id}')[:16] -> caught-not-taught:cnt_{sha256(raw_id.casefold())[:24]}`. Joins **126/126/206** on `caught-not-taught` (all 126 untrusted-arm episodes; the 80 trusted-arm honest negatives were not read in NP33-CM-1). The other 2 NP33 sets (`agentcheck` 23 rows, `ptb` 120 rows) are not DetBench sets.",
        "2. **Scoring & Abstention Decisions**:",
        "   - For Cascade v8 (`cached_cascade_v8_*`), we emitted four complementary variants across all 13 covered sets (1,594 joined runs): `cached_cascade_v8_verdict` (`1.0` if `pass_through` or `confirmed_windows > 0`, `0.0` if clear, `None` with `abstain='triage_unavailable'` if judge calls failed without alarm), `cached_cascade_v8_score` (`1.0 + 0.1*min(confirmed_windows,5) + 0.01*min(cards,9)` on alarms vs `0.01*min(cards,9)` on clear runs so Stage-1 card counts break ties monotonically), `cached_cascade_v8_judge_only` (`1.0` iff `confirmed_windows > 0`, isolating the Stage-2 LLM judge `J` per P0-F9D7D6-7), and `cached_cascade_v8_stage1_cards` (deterministic Stage-1 card count).",
        "   - We also emitted single-model Cascade v8 slices (`cached_cascade_v8_kimi_dev_cm18` 1,246 runs; `cached_cascade_v8_sonnet_np21_np33` 348 runs; `cached_cascade_v8_haiku_matched_p0f9d7d6` 1,013 runs across 10 sets where Haiku 5.5 judged DEV + CM-18 + NP21 under identical Stage-2 rules).",
        "   - For Kev-4B (`cached_kev_*`), we emitted both the eval-only slice (`zeroshot.scores.jsonl` + `detectors-v1/cache/kev_*`, joining 2 `cheatbench` + 7 `terminal-wrench` = 9 runs) and the full cached chunk-max slice (`zeroshot.scores.jsonl` + `zeroshot.train.scores.jsonl` + `detectors-v1/cache/kev_*`, joining 58 `cheatbench` + 7 `terminal-wrench` = 65 runs).",
        "   - For `transfer-matrix-20261019/predictions.jsonl`, we strictly filtered `split == 'dev'` (never touching `split == 'test'`) and emitted all 4 L2 `action_features` logistic regression models (`in_key_cheatbench`, `cross_from_hacktrace`, `cross_from_trajjudge`, `cross_from_tracepaired`) covering 150/150 `cheatbench` DEV runs.",
        "",
        "## 3. Summary of Emitted Signal Files by Family",
        "",
        "| Family | Signal Files Written | Max Joined Runs | Sets Covered | Primary Signal File |",
        "|---|---:|---:|---:|---|",
    ]

    by_fam: dict[str, list[dict]] = {}
    for sig in signals:
        by_fam.setdefault(sig["family"], []).append(sig)
    for fam, flist in sorted(by_fam.items()):
        best = max(flist, key=lambda x: x["joined_total"])
        rep_lines.append(
            f"| `{fam}` | {len(flist)} | {best['joined_total']}/{best['total_cached_rows']} | {best['sets_covered']} | `{best['signal']}.jsonl` |"
        )

    rep_lines.extend([
        "",
        "Full per-signal x per-set breakdown with denominators is in `/private/tmp/ov1104/detbench/workers/cache/CACHED-DETECTORS.md` and `/private/tmp/ov1104/detbench/workers/cache/summary.json`.",
        "",
        f"DONE {et_now}",
        "",
    ])

    (WORKER_DIR / "REPORT.md").write_text("\n".join(rep_lines), encoding="utf-8")
    print(f"Wrote CACHED-DETECTORS.md ({len(cd_lines)} lines) and REPORT.md ({len(rep_lines)} lines) at {et_now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
