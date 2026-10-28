# fixmine-harbor-framework-terminal-bench-science

Unverified upstream-diff candidates, not canonical broken-task findings.
A commit or agreement of models is not independent verification. Proposed
changes and model labels remain candidate metadata; no Finding, tier,
fixed status or upstream_fix/executed evidence is emitted automatically.
Raw model rationale stays private; no executable recipes are published.

- Repository: https://github.com/harbor-framework/terminal-bench-science
- History snapshot: `48f90831c07105d62fad0cb2aa1ee44a8125443e`
- Task glob: `tasks/*`; since: `all history`
- Non-merge commits inspected: 299
- Candidate task-version changes: 176
- Candidate splits: `{"DEV": 102, "heldout": 74}`
- Classified: 0; not classified: 176
- Yes/yes candidate labels: 0; rejected labels: 0
- Model disagreement flags: 0
- Agreed labels by class: `{}`
- Canonical defect findings: 0
- Candidate digest: `sha256:8eb75e4188ed08ab6140f32d5828876657f51ec0d4d76ed2ab2981918b64bab6`
- Label digest: `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- Candidate state: `state://fixmine/harbor-framework-terminal-bench-science/985229ae66404f80/candidates.jsonl`
- Label state: `state://fixmine/harbor-framework-terminal-bench-science/985229ae66404f80/candidate-labels.jsonl`

## Provenance and reproducibility

The blobless clone lives under `raw/git/<repo-slug>/`. Git objects pin
the parent and proposed revision. The query cache retains read-only
candidates, a manifest and content-addressed full diffs. Classifier caches
retain prompts, raw outputs, session JSONL, served-model counts and hashes.
Identity attestation verifies the served model, not a semantic task defect.
Raw sessions, diffs and free-form rationale are not committed.

```bash
evallab failures mine --rebuild --cache "$HOME/.local/state/evallab-failure-library/fixmine/harbor-framework-terminal-bench-science/985229ae66404f80"
```

Rebuild mode cannot fetch Git or call a model. Collection is the default:

```bash
evallab failures mine --repo https://github.com/harbor-framework/terminal-bench-science --task-glob 'tasks/*'
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
