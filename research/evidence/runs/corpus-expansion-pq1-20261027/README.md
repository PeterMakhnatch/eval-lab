# pQ-SELF-1: staging independent labeled cheat corpora for cross-testing

`tools/xt_stage.py` stages one Hugging Face dataset as a **local, cross-test-only bundle**: a pinned revision
under `~/.local/state/evallab-cross-test/<repo>/<rev12>/` (outside every git checkout, the production database
and the content store), every file verified against the Hub's SHA-256 (LFS) or Git blob SHA-1, a
`STAGE-MANIFEST.json` and a `CROSS_TEST_ONLY.md` notice, then `lock` (read-only). The HackBench bundle (LB-6) was
staged with its own tool; this one generalises it.

```
keys run -- python tools/xt_stage.py plan  --repo OWNER/NAME [--revision SHA] [--include GLOB] [--exclude GLOB]
keys run -- python tools/xt_stage.py fetch --repo OWNER/NAME --revision SHA [--include GLOB]
python tools/xt_stage.py verify --root DIR
python tools/xt_stage.py lock   --root DIR
```

The token comes from the environment and is never printed. `fetch` **refuses a dataset whose licence tag is
missing, `other` or not on the clean list** (MIT, Apache-2.0, BSD, CC0, CC-BY, CC-BY-SA, CC-BY-NC, ODC-By) unless
`--licence-ok REASON` records why; it never accepts a gate for anyone (HTTP 401 or 403 stops with "needs the gate
accepted by the account owner").

## Per-corpus rules

1. Eligibility: explicit clean licence; labels about cheating, reward hacking, exploits or wrong passes in agent
   runs (from execution, human review or a documented process; pass or fail alone is not a cheat label); independent
   of Goodfire, CheatBench, HackTrace, Terminal Wrench, SimGlot and HackBench.
2. Cross-test only: freeze the detector design first (hash rules, prompts, judge model), run once, never tune,
   train, retrieve examples or distil playbook entries from it. A change made after a result is seen makes the set
   tuning data for that change.
3. Receipts in this repository and in the library hold counts, indices and digests only: no corpus text, no model
   replies that quote it, no record identifiers beyond a digest of the sorted ids.
4. Each corpus gets its own directory `corpora/<slug>/` (README with the label schema and the scoring recipe,
   `results/profile.json` aggregates). One writer per directory.
