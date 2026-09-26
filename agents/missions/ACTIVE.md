# Mission board

The live backlog and pull protocol is **`research/inbox/board.md`**; active
claim files live in `research/inbox/claims/`. This page is navigation, not a
second roster, dispatch authority, or current worktree inventory.

Permanent path lanes: `agents/OWNERS.md`. Worktree and handoff protocol:
`agents/WORKFLOW.md`. Verification: `agents/CHECKS.md`. Placement:
`agents/STRUCTURE.md`.

## Now

Read the board and pickup counter, then inspect current Git worktrees and PRs.
`scripts/fleet-status.sh` reports those sources without granting cleanup permission.
Historical branch inventories do not prove current ownership, activity, or closure.

## Missions

- `research/inbox/board.md` — work intake, backlog, and the claim protocol.
- `research/inbox/QUEUE.md` — the HARVEST source-intake checklist, not a competing
  work-assignment board.
- `research/archive/2026-W36/automated-trajectory-overnight-ledger.md` — dated program
  decisions and settlements; consult exact refs before reusing availability claims.
- `docs/archive/git-estate-inventory.md` — historical estate snapshot, not a prune list.

## HAR-73 continuation handoff — 2026-09-25

**Current authority (September 26):** Peter directly confirmed in the existing
RE - Eval Lab tab, “of course it should go ahead,” activating HAR-73 here.
This supersedes the September 25 stop/new-tab requirement below, not the
execution, spending, ownership or per-spec approval limits. HAR-73 is In Progress;
branch `feat/har73-evallab-gate` starts at `a320f0a9` (through PR #470).
Linear owns the queue and acceptance. HAR-74 already has draft
[PR #462](https://github.com/PeterMakhnatch/eval-lab/pull/462); do not duplicate it.

**Absolute worktree:** `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har72-reef-gate-20260925`

```bash
omp --cwd /Users/petermakhnatch/Developer/eval-lab/.worktrees/har72-reef-gate-20260925
```

The existing tab owns HAR-73 implementation following the full card. The original
HAR-72 author stopped as instructed; activation was a subsequent explicit decision.
The committed split is `research/experiments/reef-loop-pool-20260925/har73-split.json`:
two already registered dev tasks, with four exclusion-only held-out identities.
No evaluation or Reef traffic preceded that split commit.

### Verified delivery and evidence

- HAR-70: [PR #453](https://github.com/PeterMakhnatch/eval-lab/pull/453),
  merge `7bacbff73fe2102b5538e84efbca73a58d384404`.
- HAR-71: [PR #454](https://github.com/PeterMakhnatch/eval-lab/pull/454),
  merge `dde797ac5377cecf24e4e1e2216207de61ce9787`. Pinned Terminus trees,
  retained-spec replay, and comparison validity checks are available.
- HAR-72: [PR #455](https://github.com/PeterMakhnatch/eval-lab/pull/455),
  merge `0a6a219d114edb5995c965b7af46a13dca420a46`. Eleven exact-head CI checks
  passed. Merged proof: 97 focused tests passed / 16 intentional external-Reef
  skips, plus 19 actual-Reef integration/safety tests. Both retained campaign
  summaries recomputed byte-identically on the merged revision without new
  model calls.

Evidence below is relative to the absolute worktree, not a new global ledger:
`derived/har72/receipt.json` contains hashes, limits and verification;
`api-aa-acceptance.json`, `api-control-acceptance.json`, `api-final-usage.json`,
and `merged-runtime-proof.json` sit beside it. Raw source runs remain under
`runs/har72-aa-deepseek-api` and `runs/har72-known-effect-deepseek-api`.

| DeepSeek API condition | Publications | Wilson 95% interval | Scored / missing episodes | Median trial / evaluation / decision computation |
|---|---:|---|---:|---|
| A/A, 30 trials, 5 repeats/task | 0/30 | [0, 0.1135133948] | 898 / 2 | 31.3 s / 28.9483 s / 0.06394 ms |
| Fixed known effect, 5 trials, 5 repeats/task | 5/5 | [0.5655175313, 1] | 150 / 0 | 52.7 s / 49.7391 s / 0.08204 ms |

The A/A interval includes both the current measured-rate sign-only prediction
(`2.9402378936e-9`, near-ceiling task performance) and the historical approximately
0.019 prediction. This is **not** a controlled before/after comparison with the
historical Qwen/default gate, nor evidence that false publication is below 5%.
Control power applies to the large answer-format restoration effect; every
trial's degraded starting skill was verified in actual model inputs. Decision
computation excludes persistence, and missing measurements are never zeros.

The proxy-accounted conservative upper bound was **$0.763430** under the $2
campaign cap: 2,018 requests, 2,016 reconciled and two unresolved reservations.
Invoice-level billed cost is unknown. Both failed API episodes remain `None`.
All owned model/calibration/proxy services are stopped; the temporary provider
key file was removed. Partial local-Qwen campaigns remain separate.

### Decisions, constraints and open risks

- **Do not restart Ollama.** Peter directs GLM Flash, Qwen or DeepSeek via API,
  or Gemini via subscription. HAR-72's native-Reef API qualification does not
  establish support for that same model route in Eval Lab Terminus. Inspect
  existing profiles; standard `ZAI_OPENAPI_API_KEY` and OMP Z.ai Coding Plan
  credentials are not interchangeable.
- The completed HAR-72 cap/capability is not an authorization for future specs.
  HAR-73 still requires recorded per-spec approvals; no autoapproval, policy
  changes, registration, cloud sandbox, download or upstream Reef PR without
  the applicable separate authorization.
- Keep `~/Developer/reef` and its `.venv` read-only. On HAR-73 intake its clean
  HEAD is `2a1864d4158de8a24e00ae777e9ff0501f49a97f`, superseding the historical
  HAR-72 runtime pin `818997d76412f0eead7d0b4b343da701d6ce2c20`.
  Bind the actual clean revision explicitly; do not reset another checkout.
  `reef-client==0.2.1` is allowed. Native Reef is not an OS sandbox, and an
  owner-only same-UID provider-key file is not credential containment. Reef can
  retain its proxy capability in resolved runtime configuration.
- API weight digests are unavailable. Keep requested/returned model provenance
  and do not pool different model cohorts. DeepSeek pricing changed: this run
  used conservative peak cache-miss rates of $0.30/M input and $1.20/M output
  from the [current pricing documentation](https://api-docs.deepseek.com/quick_start/pricing),
  not the repository's stale $0.28/$0.42 defaults.
- [PR #456](https://github.com/PeterMakhnatch/eval-lab/pull/456) introduced the
  evidence reader; [PR #458](https://github.com/PeterMakhnatch/eval-lab/pull/458)
  renamed it to **`evallab.evidence.reef_intake`** and added captured-traffic
  intake. These are data readers, not HAR-73/HAR-74 execution integration.
  The original single-scenario intake consumed all 900 A/A episodes with
  score/pair agreement. The historical multi-scenario overwrite at `03893a42`
  was fixed by [PR #464](https://github.com/PeterMakhnatch/eval-lab/pull/464)
  (`36c0b58c`), then hardened by PR #465. Paths and trial joins include scenario,
  with duplicate-output refusal. Source review confirms the fix; PR #464's
  receipt reports 150/150 retained control trajectories. Do not redo it.
  The old `control-atif-intake-diagnostic` and `-03893` outputs remain invalid;
  fixing the importer does not repair those historical artifacts.

### HAR-73 implementation steps — existing tab activated

1. Read the full HAR-73 card. Commit dev/held-out task IDs and digests **before**
   candidate evaluation or Reef traffic. Refuse both split-held-out tasks and
   registry entries whose `allowed_uses` contains `heldout`.
   First read the incoming
   `research/experiments/reef-loop-pool-20260925/README.md` and split proposal
   ([PR #461](https://github.com/PeterMakhnatch/eval-lab/pull/461)): they provide
   an inventory, controls, exact gate-planning utilities and parked specs, but
   explicitly are **not** registration, a committed split or execution approval.
2. Materialize current/candidate files as valid HAR-71 pinned Terminus trees.
   The native tutorial graph is not automatically a Terminus tree; opaque Reef
   content IDs are not cryptographic tree digests. Keep model binding outside
   candidate files and run fresh, paired/interleaved repeats on 2–3 dev tasks
   through the Eval Lab CLI in its own environment.
3. Return Reef's exact evaluation/settlement shape, including positional
   `None`, per-side failures/residue/score/agents/paths, `episode_failures` and
   `episode_repeats`. Link real specs/trials from the gate record.
4. Deduplicate by `candidate_id`, reusing submitted specs/results. Surface
   `evallab approve` commands and wait. Missing/withdrawn approval, timeout or
   budget stop means insufficient evidence.
5. Exercise one real candidate, held-out refusal, infrastructure/unscored
   outcomes, missing approval/timeout and retry-without-duplicate submission.
   Deliver through CI, protected merge and merged proof. HAR-74 traffic comes
   later. Preserve the primary checkout on `main` and all user changes; do not
   page persistent peers or remap roles/models without Peter's authorization.

## Archive

Historical completed mission logs and narrative records:
- `agents/archive/2026-08-15-missions-m001-m004-a001.md` (Wave 1: M001–M004, A001)
- `agents/archive/2026-08-15-missions-m005-m008.md` (Wave 1: M005–M008)
- `agents/archive/2026-08-27-mission-board-pre-hygiene.md` (Pre-hygiene verbatim board snapshot)
