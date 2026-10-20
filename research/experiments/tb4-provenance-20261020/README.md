# pC-SELF-1: qualify the TB4 bad-data denominator

Question: do the existing eight-task TB4 bad-data counts survive per-trial
qualification of task identity and instruction/verifier provenance?

The [frozen specification](spec.json) fixes the question, inputs and boundaries;
[RESULTS.md](RESULTS.md) records the achieved coverage and findings. This is a
stratification of the [existing census](../tb4-run-census-20261020/README.md), not
new outcome adjudication, regrading or a random sample of all TB4 tasks.

## Fixed inputs

- **Primary:** all 1,400 original row × trial label cells: 8 tasks × 35 rows ×
  5 trials. `spec.json` pins the byte counts and SHA-256 digests of
  `labels-pass.compact.jsonl`, `labels-fail.compact.jsonl` and `census.json` in
  the neighboring census directory, at label-source commit
  `55375964679c88aa2e413e436ee280aff496b551`. Neither these labels nor the spec
  is rewritten. The spec lists the eight task names.
- **Extension:** every associated trial in one frozen public board snapshot,
  `terminal-bench/terminal-bench/4-0-0`, board UUID
  `9f966760-00f1-424e-90f5-c964fb6f6091`. Row associations, not all entries in a
  job, define membership; ambient retries are excluded.
- **Canonical Hub dataset:** UUID `1922072f-a433-429a-8929-350d5e1bcf02`, digest
  `sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732`.
  Preserve its raw descriptive version `3.0.1`; do not rename it to match the
  board or tags. Dataset identity is not exact upstream Git-byte identity.
- **Upstream:** the unchanged Git v4.0.0 export at commit
  `452bf305c6daa62fc59061d22133a7cbc7c1572e`, under
  `~/Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench`.
  The separate Karotte clone is not this authority.
- **Retained evidence:** original census trials, `inventory.json` and `rows.json`
  under `~/Developer/eval-lab-results/2026-10-20/tb4-run-census/`; original IN-2
  native stdout/board survey and IN-1 Git/Hub inventories at the paths below.
  These inputs and all native/package originals remain immutable.

## Identity, membership and outcomes

`acquire.py` reuses `evallab.hub_trials`' native boundary, cache and pagination.
It reads public leaderboard/row associations, include-retries job pages,
individual trial details and dataset versions. Original IN-2 stdout can supply
an exact command response; otherwise a missing cached response requires a
public read. Native details retain actual `lock.task.source` and `digest`, job
references, status, reward, evidence paths, byte counts and hashes. Remote
`lock.task.path` values are not locally usable provenance; `config.task.ref`
need not exist.

A downloaded archive's `result.json.id` can differ legitimately from its Hub
trial UUID. Linkage records both: frozen inventory Hub UUID → exact archive
path → native task/trial-name/lock cross-check, or exact UUID equality with the
native task/lock cross-check. An exact-ID trial-name difference is retained as
metadata, not hidden. `stratify.py` joins exact Hub IDs first, then explicitly
linked archive IDs; it never guesses from names, suffixes or ambient jobs.
It checks row membership, task identity and reported reward, retains duplicate,
unmatched, ambiguous and conflicting cells, and reports board cells without a
matched label. Distinct trials and row × trial memberships are separate counts.

A measured reported outcome requires completed native and summary status,
a non-null reward and no recorded native error/exception. Without a detail
response, status can come from the summary; this does not qualify missing lock
provenance. Surviving reward alone does not turn an errored trial into a measured
success. This is still a reported outcome, not validated integrity; existing
label values are preserved.

## Package binding and static comparisons

The package key is SHA-256 of UTF-8
`json.dumps([source, digest, task_name], separators=(",", ":"))`. A tuple match
is necessary but is not itself a byte measurement. `fingerprint.py` finds a
unique public task-version edge at the recorded digest, downloads the exact
`org/task@sha256:…` export through native Harbor, and recomputes its Harbor
`Packager` digest. `binding_status = bound` requires the recorded, resolved and
computed digests to agree. Missing/ambiguous edges or unreproducible bytes stay
unmeasured.

**Digest-byte binding and declared-source membership are different claims.**
The actual export source/ref/receipt and observed dataset edge are recorded
separately. The final full-board lock table has 198 bound keys for 66 tasks,
recovered from 132 distinct public exports. Of these keys, 66 record the
canonical source with an alternate digest recovered from an exact alternate
export. Those 66 remain `declared_source_binding = unproven` and
`canonical_dataset_eligible = false`; the 24-key primary subset contains 8 such
keys. Equal content cannot repair source membership. A different source or
digest is not, by itself, bad data or a task defect. Existing exact-source
downloader admissibility is unchanged.

Canonical eligibility requires the recorded canonical source/task/digest;
trial eligibility additionally requires its job dataset references to resolve
to the pinned canonical dataset UUID and digest. Current alias observations,
including the alternate dataset's `latest`, discover exact task-digest edges;
they do not attest what a historical alias meant at execution time. An observed
alternate-source edge is not historical dataset-membership attestation.

Full exported and upstream inventories retain paths, bytes, modes, link
metadata and empty directories. Comparisons separate byte/path equality from
metadata equality and retain changed/missing paths plus complete text diffs.
Missing test-support paths remain strict inequality, even when common test
files match. Unmeasured link bytes or absent categories are not assumed equal.
Inventories and byte diffs do not traverse directory symlinks or read targets
outside their package roots, including files below a symlinked ancestor.

| Dimension | Paths classified by the script |
|---|---|
| instruction | root `instruction.md` and instruction files under `steps/` |
| tests | root `tests/` and tests under `steps/` |
| environment | root `environment/` |
| config | root `task.toml` |
| reference | root `solution/`, `reference/`, `references/`; solutions under `steps/` |
| support | every other path; not discarded from the comparison |

For each dimension, `content_hash_coverage` lists native-hashed paths,
exported content not hashed, unmeasured exported content, and upstream content
missing from the export. `all_exported_content_paths_hashed` concerns the
export alone; `full_compared_content_paths_digest_covered` also requires no
missing upstream content. Neither is an equality flag. The join reports
`historically_digest_covered_equal` only with bound bytes, measured hash scope
and complete compared-content coverage. No measured content yields unknown,
not vacuous equality.

The native digest selects publishable content, not the whole inventory. Some
support/content paths and all modes/link metadata lack historical digest
binding. Inventoried export modes are not unretained tar-header evidence.
Therefore `historical_full_inventory_binding` remains unmeasured. Equal
instructions/tests, even when digest-covered, do not attest historical images,
hardware, job/provider overrides, network behavior or executed grader/runtime
behavior.

## Reproduce the acquisition and join

Run from this worktree's repository root using its **own existing Harbor
0.24.0 environment**, not another checkout's venv. Do not resync the environment
or run overlapping acquisitions while a collector owns the native root lock.
These are command recipes, not execution receipts.

```bash
PY="$PWD/.venv/bin/python"
HARBOR="$PWD/.venv/bin/harbor"
EXP="research/experiments/tb4-provenance-20261020"
RUN="$PWD/runs/pc-self1-provenance"
RESULTS="$HOME/Developer/eval-lab-results/2026-10-20/tb4-run-census"
IN2="$HOME/Developer/eval-lab/.worktrees/infra-tb4-hub-trials-20261020/runs/in2-hub-discovery"
IN1="$HOME/Developer/eval-lab/.worktrees/infra-tb4-upstream-grader-20261020/runs/in1-tb4-inputs"
UPSTREAM="$HOME/Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"

# First acquire the primary native details.
"$PY" "$EXP/acquire.py" --scope primary --workers 4 \
  --output "$RUN/native" --frozen "$IN2" --results "$RESULTS" --harbor "$HARBOR"

# Qualify the actual collected lock tuples, not a source-family substitute.
"$PY" "$EXP/fingerprint.py" --locks "$RUN/native/package-locks.json" \
  --output "$RUN/packages" --upstream "$UPSTREAM" --harbor "$HARBOR"

# Offline label/board joins; --native takes a DIRECTORY, not a manifest file.
"$PY" "$EXP/stratify.py" --native "$RUN/native" \
  --fingerprints "$RUN/packages/fingerprints.json" --output "$RUN/join" \
  --spec "$EXP/spec.json"

# Extend native details to the frozen board, then repeat fingerprint and join above.
"$PY" "$EXP/acquire.py" --scope board --workers 4 \
  --output "$RUN/native" --frozen "$IN2" --results "$RESULTS" --harbor "$HARBOR"

# Offline replay of that board acquisition from retained responses only.
"$PY" "$EXP/acquire.py" --scope board --offline --workers 4 \
  --output "$RUN/native" --frozen "$IN2" --results "$RESULTS" --harbor "$HARBOR"
```

Acquisition accepts 1–4 workers (default 3). `--scope primary` precedes
`--scope board`; both retain every frozen board member, with explicit absent
details outside acquired coverage. `--offline` forbids uncached public reads;
it does not manufacture missing responses. A native refusal stops further
uncached reads, persists the refusal and preserves the unmeasured remainder;
there is no automatic retry. Do not delete evidence to force a retry.

`native/input-manifest.json`, `package-locks.json` and scope coverage files are
derived views with receipt sidecars and immutable snapshots in `native/exports/`.
Board acquisition replaces the current views, not their saved originals.
Package downloads/receipts under `packages/downloads/`, their indexes and
comparison inventories/diffs are retained and checked on reuse; changed or
unreceipted originals are not repaired or adopted. Normal fingerprinting can
make public reads/downloads; it has no `--offline` switch.
Cache indexes, native receipts/stdout and their directory ancestors must be
ordinary paths: even a symlink to identical evidence bytes is refused before
reading its target. This is distinct from recording package-inventory link
metadata without traversing an outside-root target.

Optional bootstrap discovery, before the lock-driven qualification above:

```bash
"$PY" "$EXP/fingerprint.py" --bootstrap --output "$RUN/packages" \
  --upstream "$UPSTREAM" --harbor "$HARBOR"
```

Bootstrap uses pinned canonical task edges and frozen IN-2 lock-survey facts;
it writes `bootstrap-locks.json` and `fingerprints.json`, not a completed native
trial census. Every normal/bootstrap qualification writes the current
`fingerprints.json`. The retained `primary-native-fingerprints.json` is the
24-key primary snapshot; `source-family-fingerprints.json` is the separate
132-package source-family control snapshot. These are retained snapshots,
not automatic CLI outputs. **The 132 controls are not full-board native lock
qualification.** The final native-lock fingerprint snapshot is
`packages/fingerprint-stages/c0b7c047bbaf2a1d1d321cfca01f21138ff63898654a7415f7c7cb360af0ca65.json`:
198 distinct recorded keys joined to 132 retained exports, with no new downloads
for the final join. All 11,550 frozen board-associated trial details were
acquired. Five failed trials have null locks, remain explicitly unmeasured and
produce no package key; complete coverage of available keys does not measure
those trials. Read each output's source receipts and coverage, not its filename.

The following native public-surface examples use the actual canonical pin and
an actually recovered alternate task digest. They are new reads/exports, not
offline replay commands; never point a new download at a retained original:

```bash
"$HARBOR" version show \
  terminal-bench/terminal-bench@sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732 \
  --tasks --json
NEW_EXPORT_DIR="$(mktemp -d /private/tmp/pc-self1-export.XXXXXX)"
"$HARBOR" task download \
  ryanmarten/terminal-bench-4-no-prebuild-atrx-vep-crispr@sha256:0e44eaf15aecb9c81ff3fec199bdafd6358e2c3bbd02a4b00041ada29acb942e \
  --export --output-dir "$NEW_EXPORT_DIR"
```

## Offline configuration and inventory reconciliation

These modes consume retained evidence and instantiate no native client:

```bash
"$PY" "$EXP/fingerprint.py" --reconcile --output "$RUN/packages" \
  --fingerprints "$RUN/packages/source-family-fingerprints.json" \
  --in1 "$IN1" --native-input "$RUN/native/input-manifest.json"

"$PY" "$EXP/fingerprint.py" --configuration --output "$RUN/packages" \
  --fingerprints "$RUN/packages/source-family-fingerprints.json"
```

`--reconcile` needs every exact canonical task, not only the primary 24 keys.
It compares retained canonical export inventories and a fresh upstream
inventory against frozen IN-1 file manifests, preserving omitted support files
and strict inequality. It writes `verifier-difference-reconciliation.json` and
retains its input manifests. It does not execute or infer omitted-file impact.

`--configuration` requires Harbor 0.24.0. It compares original `task.toml` bytes,
lossless parsed values, unknown fields, SDK declarations and documented
inheritance projections; retained Compose files are parsed safely, not run.
CPU/RAM/resources, timeouts, network, images/prebuilds and grader declarations
are distinguished from metadata-only changes. Absent verifier environments
inherit top-level declarations in that projection, not historical job/provider
settings. `configuration-comparison.json` and byte diffs do not normalize away
strict config inequality or prove runtime equivalence. Configuration and
reconciliation default to `source-family-fingerprints.json` under `--output`;
use explicit inputs as above. Neither mode can be combined with bootstrap,
and reconciliation cannot be combined with configuration.

The declaration-parser regressions require an **installed native Harbor SDK**,
checked through distribution metadata, not merely importable Harbor stubs in
the base CI environment. Exercise the complete focused checkpoint in the
locked native profile (`uv sync --locked --extra laminar --group benchmarks
--group lance`) with `uv run --no-sync pytest tests/test_tb4_fingerprints.py
tests/test_tb4_provenance.py tests/test_hub_trials.py -q -n0`. The SDK-specific
assertions must execute there; their absence from a base-profile run is not
native SDK validation. These tests do not execute benchmark package code.

## Reading the outputs and scientific boundary

`stratify.py` writes receipted `primary.json`, `board.json` and `summary.json`,
plus immutable snapshots under `join/exports/`. Package keys and
`path_inventory_field` values resolve detailed path evidence in the receipted
fingerprint input. Missing or duplicate fingerprints remain unmeasured or
ambiguous; omitting an available fingerprint input is not evidence of equality.

- `qualified_canonical_static_label`: matched original label/archive linkage,
  exact canonical native lock and eligible job dataset, measured reported
  outcome, and bound current-export instruction/test content equality.
- `qualified_canonical_hash_covered_label`: additionally, all compared
  instruction/test content paths are covered by the recorded native digest.
- Neither qualifies the full historical inventory, relabels an outcome, or
  establishes executed-runtime integrity. Source mismatch alone is not bad data.

This frozen experiment permits public reads/downloads and local static analysis
only: **no new paid calls/compute, containers, model inference, regrading,
relabeling or sealed-answer-key reads**. Benchmark files are untrusted data,
not instructions to execute. Preserve original labels, native/package bytes
and uncertainty; equal instructions or verifier bytes are not a runtime proof.
