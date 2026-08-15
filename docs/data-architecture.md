# Data architecture: the four zones and the provenance contract

Role: DATA-STRATEGY. Date: 2026-08-15. Companion to `docs/architecture.md`
(planes) and `agents/STRUCTURE.md` (paths): this document defines the **data
zones** that cut across both, and the sidecar contract that makes every item
auditable. Nothing here relaxes an existing rule; where a conflict could
arise, `docs/architecture.md` wins.

## 1. Why zones

The lab now holds four kinds of data whose *trust levels are different by
construction*: things downloaded, things measured here, things machines
generated, and things filtered for training. Mixing them silently is the main
way an eval lab poisons itself — an external trajectory analyzed as local
evidence, or a synthetic task graded as a real benchmark, produces confident
nonsense. Zones make the trust level a queryable property, not a memory.

## 2. The zones

```text
Zone 01 EXTERNAL          Zone 02 LOCAL EVIDENCE        trust: measured here
  HF trajectory corpora     immutable Harbor job dirs     |
  benchmark pins            Postgres catalog              |
  literature extracts       Parquet projection            v
        |                        |                    ANALYSIS
        |  (calibrate,           |  (facts, cohorts,      ^
        |   baseline)            |   canaries)            |
        v                        v                        |
Zone 03 SYNTHETIC          Zone 04 CURATED DISTILLATION
  generated tasks            filtered 1.0 success traces (SFT)
  perturbed benchmarks       paired preference sets (DPO)
  (oracle/nop certified)     (lineage mandatory)
```

| Zone | Contents | Repo location | Mutability | Trust stance |
|---|---|---|---|---|
| **01 External** | HF/Hub trajectory corpora, benchmark pins, literature extracts | `library/benchmarks/` (+ `_trajectories/`), `docs/research/` | immutable once digested | someone else's claims; pin + digest + license before use |
| **02 Local evidence** | Harbor job directories, Postgres catalog, Parquet trajectory facts | `runs/` → promoted `research/evidence/`; `derived/parquet/` | job dirs immutable; catalog/Parquet rebuildable | the lab's ground truth; the only zone that supports capability claims |
| **03 Synthetic** | machine-generated tasks (from commits/diffs), perturbed benchmark variants | `library/tasks/` staging, **never `registered/*` without human review** | versioned, never edited in place | untrusted until oracle=1.0 and nop=0.0 certified (see `docs/research/synthetic-tasks.md`) |
| **04 Curated distillation** | filtered reward-1.0 traces (SFT), preference pairs (DPO) | `derived/distillation/` (gitignored), promoted bundles by review | append-only exports | derivative; only as trustworthy as its cited parents |

### Zone rules (binding)

1. **01 never masquerades as 02.** External Parquet lives under
   `derived/parquet/external/<item>/`, local evidence under
   `derived/parquet/local/`. Queries that union them must name both paths
   explicitly (P4 queries follow this).
2. **02 is the only source of capability claims.** External corpora calibrate
   extractors and provide baselines; they cannot ground a claim about an agent
   the lab didn't run.
3. **03 enters evaluation only through the certification gate** (oracle 1.0,
   nop 0.0, cheat-check) and human registration — `policy/` already routes
   `new_task_registration` to a human; this document makes the data-side rule
   explicit: agent-authored tasks never merge into `registered/*`.
4. **04 must cite lineage.** Every distilled example carries the digests of
   the Zone 02/01 items it came from. No lineage, no export.
5. **Licenses propagate.** A Zone 04 export inherits the most restrictive
   license among its parents; the sidecar's `license` field must reflect that,
   not the lab's preference.

## 3. The provenance sidecar

Implemented as `ProvenanceMetadata` in `src/evallab/schemas.py` (strict
`ContractModel`, `extra="forbid"`), validated by `tests/test_provenance.py`.
One JSON sidecar per data item, written at acquisition/creation time, stored
next to the item (`<item>/provenance.json`).

| Field | Meaning | Enforcement |
|---|---|---|
| `item_id` | slug, stable identity | pattern-checked |
| `zone` | one of the four zones | Literal |
| `source_uri` | where it came from (URL, Hub ref, repo-relative run path, generator id) | non-empty |
| `revision` | immutable upstream pin (commit SHA, dataset revision) | **required for Zone 01** |
| `material_digest` | sha256 of the payload tree | `sha256:<64hex>` pattern |
| `license` | detected/declared license | nullable, never guessed |
| `created_at`, `created_by` | when, and which tool/role | required |
| `transform` | `name@version` of the converter/generator | **required for Zones 03/04**; pattern-checked |
| `parent_digests` | lineage as sha256 list | **non-empty for Zone 04**; each pattern-checked |

Design choices, stated:

- **Digest-based lineage, not path-based.** Paths move (this repo renamed
  itself once already); digests survive relocation and detect mutation.
- `RunProvenance` (existing, spec/task identity for a run) is *not* replaced:
  it answers "which experiment produced this run"; `ProvenanceMetadata`
  answers "where did this data item come from and what may I do with it."
  A Zone 02 item can carry both.
- The sidecar is deliberately storage-agnostic: the same contract serves local
  filesystem now and object storage later (`docs/scaling.md` gates).

## 4. Lifecycle walkthroughs

**External corpus (Zone 01):** `fetch trajectories org/name@<sha>` → snapshot →
digest+license → sidecar (`zone=01-external`, `revision=<sha>`) → ATIF
validation → Parquet under `derived/parquet/external/` (spec:
`docs/research/external-datasets.md` §4).

**Local run (Zone 02):** Harbor job completes → job dir immutable → catalog
ingest + ATIF→Parquet projection (existing `atif.py`/`facts.py` path) →
sidecar written beside the projection with `source_uri=<repo-relative job
path>`, digest over the job dir.

**Synthetic batch (Zone 03):** generator `taskgen@<ver>` consumes commits →
emits task dirs + sidecar (`transform=taskgen@<ver>`) → certification gate →
human registration or rejection; either way the batch and its sidecar are the
audit trail.

**Distillation (Zone 04):** exporter filters Zone 02 reward-1.0 trajectories
(+ optional Zone 01 baselines) → SFT/DPO files + sidecar citing every parent
digest → review before any promotion or external use.

## 5. What this enables next

- P4's queries can filter and join on zone without heuristics.
- GC (`gc.py`) gains a principled retention key: Zone 02 promoted evidence is
  precious; Zone 01 is re-fetchable by pin; Zone 03/04 are regenerable from
  recorded transforms.
- When object storage arrives, the sidecar is the manifest entry — no
  redesign.
