# ov-tb4-anatomy-union-qa-20261031 — W2-TB4-UNION-QA / W3-TB4-FINISH-SCORE

Wave-2 / Wave-3 Task-QA experiment: does a **package-complete audit view** (PackageAnatomy card from `evallab.package_anatomy`
+ `instruction.md` + `task.toml` + `environment/**` incl. `Dockerfile`/`docker-compose.yaml` + `tests/**`), a lifted
finding cap (**k=8**) and a **two-family union** (`openrouter/~anthropic/claude-sonnet-latest`, `deepseek/deepseek-v4-pro`) raise
causal-key recall on the 14 TB4 DEV tasks (45 keys) over OQ-6 Sonnet-B (view = `instruction.md|task.toml|tests/**`,
k=3, one model), when evaluated via a **blinded pooled judge** (`google-antigravity/gemini-3.8-flash`) running OQ-6's immutable `scorer.py` rubric?

## Cohort & Guardrails
- Cohort: the 14 `tb4-audit-v1` rows of `T2B-OQ6-DIAGNOSIS/receipts/oq6_cohort_manifest.json`. Keys are read only from
  the OQ-6 DEV `scorer-only-keys/D####.json` paths pinned there; `research/answer-keys/` and non-DEV tasks were never opened.
- All auditor runs (`claude-sonnet-latest` 14/14, `deepseek-v4-pro` 14/14) and 14 pooled judge calls are 100% session-attested.

## Primary Results (NOFLOOR-k8, All Citation-Verified Findings)
- **Baseline (`oq6_sonnet_B` re-judged):** Recall **7/45 = 15.6%** (95% Wilson: 7.7–28.8%), 7/14 tasks, strict precision 7/29 = 24.1%, Eval³ precision 7/29 = 24.1%, 0/10 `not_in_view` keys recovered.
- **Family 1 (`sonnet` k=8):** Recall **10/45 = 22.2%** (12.5–36.3%), 8/14 tasks, strict precision 10/48 = 20.8%, Eval³ precision 10/48 = 20.8%, 2/10 `not_in_view` keys recovered.
- **Family 2 (`deepseek` k=8):** Recall **9/45 = 20.0%** (10.9–33.8%), 7/14 tasks, strict precision 9/63 = 14.3%, Eval³ precision 9/63 = 14.3%, 1/10 `not_in_view` keys recovered.
- **`union_top8`:** Recall **12/45 = 26.7%** (16.0–41.0%), 9/14 tasks, strict precision 12/94 = 12.8%, Eval³ precision 12/88 = 13.6%, 2/10 `not_in_view` keys recovered. Paired McNemar vs baseline: $b=1, c=6, p = 0.125$.
- **`union_all`:** Recall **12/45 = 26.7%** (16.0–41.0%), 9/14 tasks, strict precision 12/111 = 10.8%, Eval³ precision 12/104 = 11.5%, 2/10 `not_in_view` keys recovered.

## Gate Verdict
- **FAIL:** `union_top8` matched 12/45 keys (gate required $\ge 15/45$) and McNemar $p = 0.125$ (gate required $p < 0.01$).
- Although the gate threshold was missed, the package-complete view increased recall by +11.1 percentage points (from 15.6% to 26.7%), added 2 tasks with causal hits (9/14 vs 7/14), and successfully recovered 2 previously `not_in_view` keys (`D0017:67bef2cbca` and `D0021:20a4f05f95`).
