# fixmine-xlang-ai-osworld

Unverified upstream-diff candidates, not canonical broken-task findings.
A commit or agreement of models is not independent verification. Proposed
changes and model labels remain candidate metadata; no Finding, tier,
fixed status or upstream_fix/executed evidence is emitted automatically.
Raw model rationale stays private; no executable recipes are published.

- Repository: https://github.com/xlang-ai/OSWorld
- History snapshot: `044bc33901cb893808e3f91ebc9a17ed8e54b44c`
- Task glob: `evaluation_examples/examples/*/*.json`; since: `2026-01-01`
- Non-merge commits inspected: 68
- Candidate task-version changes: 158
- Candidate splits: `{"DEV": 129, "heldout": 29}`
- Classified: 0; not classified: 158
- Yes/yes candidate labels: 0; rejected labels: 0
- Model disagreement flags: 0
- Agreed labels by class: `{}`
- Canonical defect findings: 0
- Candidate digest: `sha256:4a3069554b6d36c99724dcac7213c902560577d495a6a487444223f1f8288621`
- Label digest: `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- Candidate state: `state://fixmine/xlang-ai-osworld/14b24e3312ea949a/candidates.jsonl`
- Label state: `state://fixmine/xlang-ai-osworld/14b24e3312ea949a/candidate-labels.jsonl`

## Provenance and reproducibility

The blobless clone lives under `raw/git/<repo-slug>/`. Git objects pin
the parent and proposed revision. The query cache retains read-only
candidates, a manifest and content-addressed full diffs. Classifier caches
retain prompts, raw outputs, session JSONL, served-model counts and hashes.
Identity attestation verifies the served model, not a semantic task defect.
Raw sessions, diffs and free-form rationale are not committed.

```bash
evallab failures mine --rebuild --cache "$HOME/.local/state/evallab-failure-library/fixmine/xlang-ai-osworld/14b24e3312ea949a"
```

Rebuild mode cannot fetch Git or call a model. Collection is the default:

```bash
evallab failures mine --repo https://github.com/xlang-ai/OSWorld --task-glob 'evaluation_examples/examples/*/*.json' --since 2026-01-01
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
