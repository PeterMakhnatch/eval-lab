# Experiment: on-repair-vs-gold-20261102

## R3-REPAIR-VS-GOLD: Do Task Repairs Produce Stronger Graders?

Paired comparison of original vs. container-proved repaired task graders on identical deterministic AST semantic mutants.

### Findings
- **Task-Level Weakness:** Dropped from 17 / 24 (70.8%) to 8 / 24 (33.3%). Exact McNemar test: $b = 10, c = 1, p = 0.011719$.
- **Mutant-Level Survival:** Dropped from 21 / 64 (32.8%) to 8 / 64 (12.5%). Exact McNemar test: $b = 14, c = 1, p = 0.000977$.
- **Grader Degradation:** 1 / 24 tasks (`format-code-task-000322`) degraded ($c = 1$), accepting mutant `boundary_flip` at `aiohttp/web_fileresponse.py:176`.
- **Repair-Qualified vs. Gold Status (2x2):** 27 / 27 (100.0%) of overlapping tasks are Repair-Qualified, but 0 / 27 (0.0%) are Gold certified in `gold-v1-manifest.json` (`ref_failed_no_repair`).
- **Conclusion:** Repaired graders are significantly stronger against mutants than originals, but one-third remain weak, demonstrating that repaired tasks require an explicit hardening gate before production certification.

### Files
- `run_audit.py`: Audit script executed against Modal sandboxes.
- `repair_vs_gold.json` / `summary.json`: Complete audit data and per-cell results.
- `paired_comparison.json`: Task-level and mutant-level paired contingency data.
- `manifest_overlap.json`: 2x2 overlap table with Gold v1 manifest.
