# ov1104-grader: Systematic Grader Trust Scorecard, Gold Certification, and Complete DEV Mutation Census

## Question
1. **Q1 (Scorecard & Mutation Census):** Across every strictly-DEV task with executed grader evidence, how is original-grader weakness distributed, and which tasks are closest to certifiable (`7/7` Gold gates)?
2. **Q2 (Gold Certification):** Can `evallab certify-gold` certify task `format-code-task-000682` and up to 4 more of the closest tasks (`002552`, `002391`, `002139`, `000838`) across all 7 gates (including Gate 7: $\ge 2$ accepted alternative correct solutions) within the $6.00 lane cap?

## Answer (Headline Numbers with 95% Wilson Intervals)
- **H1 ($0 existing receipts, original grader accepts $\ge 1$ broken STING/census mutant):** **23/35 = 65.7% (95% Wilson CI: 49.2–79.2%)**.
- **H1 extended with `ov1104-grader` Modal mutation audits (`+002680, 002976, 003007`):** **24/37 = 64.9% (95% Wilson CI: 48.8–78.2%)** across all 37 mutatable strictly-DEV Python tasks in the 41-task scorecard universe (**30/37 = 81.1% (65.8–90.5%)** when also counting A4B held-out mutants executed on the original grader).
- **Complete `on-gold-universe` `executable_audit` census (31/31 = 100% of mutatable tasks, up from 28/31 in A2):** **19/31 = 61.3% (43.8–76.3%)** weak in A2 + `ov1104-grader` (`002680` killed 3/3, `002976` killed 3/3, `003007` killed 0/3); **20/31 = 64.5% (46.9–78.9%)** pooling the `000322` VF-R3 rerun.
- **H2 (Status distribution across 41 strictly-DEV tasks with executed evidence under strict H3/H4 rule):** `certified`: **0**, `near_certifiable`: **12** (`11` pre-run; `002139` upgraded from `hardened_but_leaky` after passing Gates 1–6 tonight), `hardened_but_leaky`: **15**, `original_weak`: **5**, `original_no_weakness_found`: **5**, `no_executed_evidence`: **4**.
- **H3/H4 (Blind contract-test synthesis on 5 closest tasks):** **0/5 = 0.0% (0.0–43.4%)** certified under blind synthesis:
  - `002391` and `002139` passed **Gates 1–6 (6/7)** (`reference=1.0, nop=0.0, witness=0.0, exit0_pth=0.0`, `train=4/4` killed, `eval=3/3` and `4/4` killed) but failed **Gate 7** (`0/4` and `0/3` alternatives accepted) because the synthesizer pinned reference-only internals (`VulnerabilityResult.merge_aliases` in `002391`; literal exception message `'The GCP API threw a NotFound exception'` in `002139`).
  - `000682` failed Gate 1 (`reference=0.0`) because the synthesized test added `/testbed/tests` to `sys.path` and ran `from common import BaseTest`, triggering a relative-import `ImportError` in `/testbed/tests/common.py`. Moreover, **3/4 instruction-only alternatives failed the original grader** (`1/4` passed) because the original placebo flight-data fixture only recorded `sns.set_topic_attributes` and lacked recordings for `kms.describe_key`.
  - `000838` failed Gate 1 (`reference=0.0`) because `separate-verifier@10` prepends `/var/lib/mimo-grade/v10shim/sitecustomize.py` to `PYTHONPATH` and sets `PYTHONPYCACHEPREFIX`, breaking `from ruamel import yaml` (`ModuleNotFoundError: No module named 'ruamel'`) in Python 3.8 even on the pristine baseline.
  - `002552` was refused before synthesis (`insufficient valid mutants for train and held-out evaluation`: its 4-line Python reference patch yields only 1 AST mutant).
- **H5 (PREREG Amendment 1: Alternative-informed synthesis with 50/50 sha256 split into shown vs strictly held-out alternatives):**
  - `000682-r3` (with Gate-1 self-repair): **`certification.json.status == "certified"` (7/7 gates: `ref=1.0, nop=0.0, witness=0.0, exit0_pth=0.0, train=1/1, eval=1/1`, `2/2` held-out alternatives accepted, `4/4` total alternatives accepted)**. Labeled `certified (alt-informed, 2 held out; provisional: test-aware alternatives)` per PREREG because 3 of 4 validated alternatives (`b-argon`, `b-fable`, `b-flash`) were generated knowing the placebo fixture constraint.
  - `002391-r2` / `002391-r2-honest`: Passes Gates 1–6 (`train=4/4`, `eval=3/3` killed) and accepts **2/2 shown** (`real-2`, `real-3`) and **2/2 held-out honest alternatives** (`real-4` from `HAR-202-chrr1-l-honest-002391` and `a-flash` generated instruction-only tonight) -> **`certification.json.status == "certified"` (7/7 gates, 4/4 honest solutions accepted, non-test-aware)** in `cert/runs/002391-r2-honest/certification.json`. In `cert/runs/002391-r2/certification.json` (which included all 4 `A0` inventory rows), held-out acceptance is **1/2** because `real-1` (`65e5213c`, from the **cheat** arm `HAR-202-chrr1-l-cheat-002391-k1`) copied an older upstream commit whose `merge_aliases` omits `self.published`, resetting `published` from `datetime(2024, 1, 1)` to `None`! (Notably, A4B's over-strict test accepted only that buggy cheat copy `real-1` because it defined `merge_aliases`, while rejecting all 3 honest Union-Find solutions `real-2, real-3, real-4`.)
  - `002139-r2`: Passes Gates 1–6 (`train=4/4`, `eval=4/4` killed) and accepts **1/1 shown** (`a-argon`) and **1/2 held-out** (`a-fable=1.0`, `a-flash=0.0`; `2/3` total instruction-only alternatives accepted). `a-flash` failed only because `FakeStorageClient.__init__(self, project="UNSET")` in the synthesized test did not default `project=None` when `a-flash` called `storage.Client()`.

## How to Read This Directory
- `PREREG.md`: Pre-registration (committed at `011d7fbb2` before the first Modal cell) and Amendment 1 (`b262a8781`, pre-registering `H5` alternative-informed synthesis after `002391` run 1).
- `scorecard/build_scorecard.py`: Deterministic, stdlib-only builder (`python3 research/experiments/ov1104-grader/scorecard/build_scorecard.py`) that assembles `scorecard.json` (41 task rows with per-cell receipts), `headline.json`, and `scorecard.md`.
- `audit/{002680,002976,003007}/audit.json`: Executed `evallab audit-mutants` receipts on the 3 `on-gold-universe` `executable_audit` tasks that were stopped by the A2 spend cap.
- `cert/cells.py`: Modal single-cell probe and patch grader with `ReserveSettleSpendGate` ledger accounting.
- `cert/alt_informed_synth.py`: Alternative-informed contract-test synthesizer (`H5`, splitting validated alternatives in half by `sha256` ascending, showing the first half and holding out the second half for Gate 7).
- `cert/resume_certify.py`: Idempotent resume runner for `certify-gold` plans.
- `cert/validation.json`: Original-grader Modal validation receipts for all 14 alternative solutions generated tonight across `000682`, `002139`, `000838`, and `002391`.
- `cert/summary.json` and `cert/runs/*/certification.json`: Gate-by-gate certification receipts and served-model attestation across all 9 `certify-gold` runs.

## Worked Example: `format-code-task-000682` Original Grader Accepts a Broken Mutant (`call_drop`)
In `format-code-task-000682` (Cloud Custodian SNS KMS filter and `set-encryption` action), STING operator `call_drop` at `c7n/resources/sns.py:316` replaces the `sns.set_topic_attributes(...)` call inside `SetEncryption.process` with `pass`:

```diff
--- a/c7n/resources/sns.py
+++ b/c7n/resources/sns.py
@@ -314,8 +314,4 @@
 
         for r in resources:
-            sns.set_topic_attributes(
-                TopicArn=r['TopicArn'],
-                AttributeName='KmsMasterKeyId',
-                AttributeValue=key
-            )
+            pass
         return resources
```

- **Original grader reward:** **1.0** (`ov-sting-mutants-20261031` cell `original-m0-call_drop-L316`, evidence `aa393fc69dea`; reproduced at **1.0** in A2 `census.json`). Why: `tests/test_sns.py::test_set_sns_topic_encryption` replays a static placebo flight recording (`get_topic_attributes`) that returns `'alias/aws/sns'` regardless of whether `set_topic_attributes` was ever called, and asserts `self.assertTrue(attributes['Attributes']['KmsMasterKeyId'], 'alias/aws/sns')` (2-arg `assertTrue(expr, msg)` bug!).
- **Hardened (`000682-r3`) grader reward:** **0.0** (killed by `test_gold_contract.py`, while `reference=1.0`, `nop=0.0`, `witness=0.0`, `exit0_pth=0.0`, and `4/4` alternative solutions score `1.0`).

## Spend Actuals
- **Total Modal executions:** 136 cells (`25` standalone probe/grade cells + `99` non-cached `certify-gold` cells + `12` `audit-mutants` cells).
- **Total Modal spend (`est_usd`):** **$0.6251** (10.4% of the $6.00 lane cap; logged in `~/.local/state/evallab-ov1104/grader-spend.json` and `~/.local/state/evallab-ov1104/spend.jsonl`).
- **Paid LLM / external API spend:** **$0.00** (all synthesis and solution generation ran on $0 OMP subscription routes with `resolvedModelIsFallback_count = 0`).
