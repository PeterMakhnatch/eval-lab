# fixmine-laude-institute-terminal-bench

Unverified upstream-diff candidates, not canonical broken-task findings.
A commit or agreement of models is not independent verification. Proposed
changes and model labels remain candidate metadata; no Finding, tier,
fixed status or upstream_fix/executed evidence is emitted automatically.
Raw model rationale stays private; no executable recipes are published.

- Repository: https://github.com/laude-institute/terminal-bench
- History snapshot: `d28711d0da2675d0bb1d56de45ae5df6082438a3`
- Task glob: `tasks/*`; since: `2025-08-01`
- Non-merge commits inspected: 360
- Candidate task-version changes: 666
- Candidate splits: `{"DEV": 266, "cross_test": 312, "heldout": 88}`
- Classified: 32; not classified: 634
- Yes/yes candidate labels: 0; rejected labels: 32
- Model disagreement flags: 2
- Agreed labels by class: `{}`
- Canonical defect findings: 0
- Candidate digest: `sha256:2cccd32554e01f475eca40bc1898d04bca6870116422fdffe795045be4f15600`
- Label digest: `sha256:99c4ff52067e5d5fe986be31541e0ef04c61228d950c563d8608af3428590b61`
- Candidate state: `state://fixmine/laude-institute-terminal-bench/523167b4ba1752d4/candidates.jsonl`
- Label state: `state://fixmine/laude-institute-terminal-bench/523167b4ba1752d4/candidate-labels.jsonl`

## Provenance and reproducibility

The blobless clone lives under `raw/git/<repo-slug>/`. Git objects pin
the parent and proposed revision. The query cache retains read-only
candidates, a manifest and content-addressed full diffs. Classifier caches
retain prompts, raw outputs, session JSONL, served-model counts and hashes.
Identity attestation verifies the served model, not a semantic task defect.
Raw sessions, diffs and free-form rationale are not committed.

```bash
evallab failures mine --rebuild --cache "$HOME/.local/state/evallab-failure-library/fixmine/laude-institute-terminal-bench/523167b4ba1752d4"
```

Rebuild mode cannot fetch Git or call a model. Collection is the default:

```bash
evallab failures mine --repo https://github.com/laude-institute/terminal-bench --task-glob 'tasks/*' --since 2025-08-01
```

New classification requires explicit `--classify --limit N`, with positive
N. This bounds label work without discarding collected candidates. There
is no execution, exploit/re-break campaign or automatic canonical promotion.

## Splits and validation

New families use `int(sha256('fixmine/v1\x1f' + repo_slug + '\x1f' + family),16) % 5`:
bucket 0 is heldout, otherwise DEV. Directory variants stay grouped; shared
task-table ids group by domain/template; tau Python tasks group by domain
and user id. Existing non-DEV assignments override to cross_test; existing
DEV-only assignments are preserved. Only DEV may feed defensive knowledge,
with uncertainty intact. Classifiers do not receive splits or old labels.

A proposed defect needs independent, task-version-specific evidence and
benign validation before entering canonical broken-task findings. A source
label, model agreement or a reproducer path alone does not provide this.

## Audit

No positive third-family audit is recorded; no agreement rate is claimed.
