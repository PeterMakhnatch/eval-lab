# Hackbook `match` on held-out `tb4-audit-v1` tasks (pB-SELF-6)

Question: how much of the hackbook's task-side matching survives on tasks nobody wrote the catalog
from? Every earlier number (DEV category recall `56/86`, TB2 to TB2.1 `25/26`, the MiMo atlas) was
measured on tasks the entries were written from; Verifier's 08:00 audit found `47` of the `59`
matched DEV findings are cited by the catalog itself.

**What ran.** One frozen version, `hackbook-match@0baedbc33ffe` (a digest of
`research/hackbook/entries.jsonl`, `taxonomy.json` and `src/evallab/hackbook/*.py`; the catalog and
matcher as merged at `477b8d56b`: 57 entries). `evallab hackbook match` over the exported blind view
of each split (`agent-inputs/{dev,test}`: 25 and 41 tasks), findings scored with the key's own
`score_audit.py`. TEST was run **once**; `run.py` refuses a second TEST run of the same version
because `research/answer-keys/splits/exposure.jsonl` holds a run record for it. That record marks no
task as exposed, on purpose: only aggregate cells were read (no per-task TEST prediction was
printed, saved or looked at) and no hackbook change was made from this run, and marking all `41`
tasks `inspected` would empty the key's `unexposed_test` cell for every other system. Paid spend
`$0`, a few seconds of compute.

```bash
python research/experiments/hackbook-tb4-heldout-20261021/run.py --splits dev --out dev.json
python research/experiments/hackbook-tb4-heldout-20261021/run.py --splits dev,test \
  --spend-the-test-split --out result.json     # once per version
```

## Result

Category 1-to-1 (a prediction credits one finding of its task whose category matches), the key's
headline mode. Recall is over all task-scoped findings of the split; precision is a lower bound
because the key has no certified-clean tasks.

| | DEV (in-sample) | TEST (held out, once) |
| --- | --- | --- |
| tasks / findings | 25 / 86 | 41 / 152 |
| predictions | 113 | 154 |
| **recall** | **56/86 = 65.1%** (Wilson 54.6-74.3) | **62/152 = 40.8%** (33.3-48.7) |
| precision (lower bound) | 49.6% | 40.3% |
| task-any 1-to-1 recall | 69/86 = 80.2% | 106/152 = 69.7% |
| exact id / locus-and-class | 0 / 0 | 0 / 0 |

The DEV and TEST intervals do not overlap: about a third of the DEV recall is in-sample. The
hackbook names no finding id or locus, so only the category-level modes can credit it.

Same scorer, same TEST split, other systems (category 1-to-1 recall out of `152`):

| System | TEST recall | Note |
| --- | --- | --- |
| `hackbook-match@0baedbc33ffe` | **62** | `$0`, deterministic, no model |
| DEV-prior baseline (`weak`, the modal DEV category, on every task) | 18 | what a reader with no matcher guesses |
| Epoch auditor flags / Tokenless auditor flags | 15 / 16 | each flag as one prediction |
| one prediction per (task, category), told which categories exist | 78 | oracle ceiling for that prediction shape |
| plain read (Codex `gpt-6.1-sol`, not Opus) / Qwen3.6-35B plain json-v3 (other lanes, frozen) | 78 / 6 | `cheap-models.md`, exposure `cm1-qwen-plain-json-v3-test-20261021` |

Per taxonomy class (category 1-to-1, matched/findings; classes with fewer than 10 TEST findings
are noise):

| Class | DEV | TEST |
| --- | --- | --- |
| `weak_check` | 24/41 | 29/61 |
| `unstated_requirement` | 12/19 | 16/40 |
| `grader_tamperable` | 7/12 | 10/20 |
| `answer_reachable` | 1/1 | 3/7 |
| `instruction_grader_contradiction` | 4/5 | 2/6 |
| `overstrict_oracle` | 2/2 | 1/6 |
| `env_mismatch_or_flaky` | 3/3 | 0/4 |
| `grader_logic_bug` | 3/3 | 0/3 |
| `wrong_answer_key` | - | 1/5 |

## Reading

- On tasks the catalog was not written from, `match` credits about `4` in `10` findings by category,
  `3.4` times the no-matcher prior and `4` times either external auditor's own flags, and about
  `80%` of what the plain read (Codex `gpt-6.1-sol`) credited on the same split. It does not name the finding.
- The three large classes lose `8` to `23` points from DEV to TEST (`weak_check` `58%` to `48%`,
  `grader_tamperable` `58%` to `50%`, `unstated_requirement` `63%` to `40%`); the classes with
  `3` to `7` TEST findings dropped to `0` to `3` hits, and each rests on a handful of tasks.
- Precision `40%` is a lower bound (a prediction on a task whose real defect is unlabelled counts as
  wrong), so it says how often a hit is *credited*, not how often it is wrong.

## Limits

- One run of one version, `41` tasks, so the interval is about `+-8` points. Per-class cells are
  small. The key's TEST findings are PM's published reports (not native confirmations), and Epoch
  and Tokenless flags were among the sources that built the key.
- TB4 only. MiMo, TB2.1 and AutomationBench packages are different task families; the MiMo and
  TB2.1 keys have no untouched split for this catalog (see `research/hackbook/README.md`).
- The hackbook was tuned on DEV and shaped by DEV findings the key lists, so DEV stays in-sample.
  After this run the TEST split is **spent for hackbook versions**: a later version's TEST score
  is not untouched, and development continues on DEV only.
