# OVN G5 paired eval: spec generator + operator runbook (prep, $0)

Part of HAR-126 (G5). This prep generates the paired-eval specs and
documents the endpoint and operator steps. **Nothing here spends
anything: no trial launches, no deploys, no model calls, no submits.**
The paid eval (20 tasks x up to 3 arms) is launched by the parent after
Infra posts the G4 adapter digest + parity record and HAR-135's
candidate admission/family-size decision is frozen before the first G5
trial, per PREREG.md.

- `make_g5_specs.py` — generates 2–3 ExperimentSpecs per frozen eval
  task. `--check` revalidates without writing.
- `tasks/` (gitignored) — staged task bytes, reproducible from the eval
  CSV + variant records.
- Specs output (default `g5-specs/`, gitignored) — generated specs;
  submit parks them in `queue/waiting/`.

## 1. Frozen inputs

| input | value |
|---|---|
| Eval list | `research/experiments/ovn-sft-v0/eval_tasks.csv`, v2, sha256 `3b997fdcff048061fd8a05d446948d4d6425bf670eb0d6710989c50dcd0a9219` (20 tasks; the generator refuses any other bytes) |
| Harness | `research/experiments/har126-lf2/harness-lf2`, sha256 `f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be` (G0, #608) |
| Route | retained HAR-110 base spec (`har110-python-gepa/base-specs/student-terminus2-selfhosted-python.json`): agent `terminus-2`, env `daytona`, 120 calls / 2.5M input tokens, `override_storage_mb` 10240 |
| Adapter route | `:har129` admitted by #604 (`selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129`); same forced generation_config, priced (0,0) |
| GEPA candidate | HAR-135 C1 handoff exists on `origin/feat/har135-seven-task-gate-20261001` (commit `cb1465fd`): addendum sha256 `b55a90cdf5e07719150c5642043bebfa54ce7ff68f28aff2009d4f738e7f9470`, 7 gated specs — **not on main, not admitted**. The pre-G5 admission decision happens at launch: admitted third arm or two-arm fallback (sole primary test at 0.05) |

## 2. Arms

| arm | model | extra instruction |
|---|---|---|
| `stock` | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | none |
| `tuned` | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129` | none |
| `gepa` (optional) | stock model name, adapter disabled | `--gepa-candidate <path> --gepa-sha256 sha256:<hex>` |

Spec names are deterministic: `ovn-g5-<short>-<arm>`
(e.g. `ovn-g5-002302-stock`). `--check` enforces that arms of one task
differ ONLY in `model`, `extra_instruction_path`/`sha256` and
`name`/`hypothesis` — a limit, tree or digest drifting on one arm fails.

Per-trial `est_cost_usd` is **$0.35** on every spec, with the basis
recorded in its hypothesis and in `cohort.json`: Research-Harbor's
08:42Z ruling on HAR-126 (HAR-116 $4.81/40 and G2 wave-1 ~$2.4/20 put
measured cost at ~$0.12; $0.35 is ~3x). Total est: $14.00 (40 specs,
two arms) or $21.00 (60 specs, three arms).

## 3. Arm-order rule (PREREG section 3)

CSV row order (header excluded), 1-based. Three arms repeat this fixed
six-row cycle through all 20 tasks:

| Row mod 6 | Order |
|---:|---|
| 1 | stock → tuned → gepa |
| 2 | tuned → gepa → stock |
| 3 | gepa → stock → tuned |
| 4 | gepa → tuned → stock |
| 5 | stock → gepa → tuned |
| 0 | tuned → stock → gepa |

Two-arm fallback (no candidate admitted): odd rows stock then tuned,
even rows tuned then stock. Each task's order is recorded in
`cohort.json`. Filenames do not enforce the schedule: the operator
ticks position waves serially per task (section 6, step 6) and preserves
actual start/end times.

## 4. Generate + check

Three arms (admitted candidate):

```bash
uv run --no-sync python research/experiments/ovn-sft-v0/make_g5_specs.py \
  --tree research/experiments/har126-lf2/harness-lf2 \
  --tree-digest sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be \
  --gepa-candidate <CANDIDATE_PATH> --gepa-sha256 sha256:<64hex> \
  [--out-dir research/experiments/ovn-sft-v0/g5-specs]
```

Two arms (no candidate): omit both `--gepa-*` flags (they are required
together). Revalidate without staging or writing:

```bash
uv run --no-sync python research/experiments/ovn-sft-v0/make_g5_specs.py \
  --check <specs-dir> \
  --tree research/experiments/har126-lf2/harness-lf2 \
  --tree-digest sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be \
  [--gepa-candidate <CANDIDATE_PATH> --gepa-sha256 sha256:<64hex>]
```

Proof ($0, placeholder candidate, specs into /tmp, nothing submitted):

```
eval: 20 tasks v2 3b997fdcff04…; arms stock/tuned/gepa
wrote 60 specs + cohort.json to /tmp/ovn-g5-proof
per-trial est $0.35; total est $21.00 (est $0.35/trial per Research-Harbor 08:42Z ruling …)
check ok: 60 specs + cohort.json in /tmp/ovn-g5-proof
arms stock/tuned/gepa on terminus-2; per-trial est $0.35; total est $21.00
```

(The placeholder file lives under the gitignored `tasks/` dir because
`read_spec` requires a repo-relative instruction path; it was never
submitted. Two-arm proof into `/tmp/ovn-g5-proof-2arm`: 40 specs,
`check ok`, orders alternate `stock->tuned` / `tuned->stock`.)

## 5. Endpoint

Infra's LoRA-enabled server is `tools/modal-mimo-serve/serve_lora.py`,
Modal app **`evallab-mimo-v26-9b-lora`** (class `MimoLoraServer`). It
reuses `serve.py`'s SGLang v0.5.20 image, weights revision, GPU,
context 65536, reasoning parser `mimo` and secrets; the only additions
are `--enable-lora --lora-paths <name>=<dir> --max-lora-rank 64
--max-loras-per-batch 1 --lora-strict-loading` plus the read-only SFT
volume. Production (`evallab-mimo-v26-9b`) is never touched.

URL pattern (confirm from the `modal deploy` output / `modal app list`;
production's observed form is
`https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct`):

```
https://p-makhnatch--evallab-mimo-v26-9b-lora-<server-slug>.us-east.modal.direct
```

Chain, as in G2: agent → secret proxy (stamps `/t/<attempt_id>/`,
upstream from **`EVALLAB_MIMO_SELFHOSTED_UPSTREAM`**) → capture hop
(strips the token, records byte-identical bodies) → Modal. Capture
every arm identically.

## 6. Operator steps for G5

1. `evallab spend day`. Do not launch if G5 would breach the $30
   overnight envelope (G5 cap $10 per the plan; HAR-126's description
   says $8 — treat $8 as binding until Research-Harbor clarifies) or
   the dated $35 UTC-day override state.
2. Deploy/warm the LoRA app (Infra may own this): set
   `EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter` and
   `EVALLAB_MIMO_LORA_NAME=har129`, `modal deploy
   tools/modal-mimo-serve/serve_lora.py`, warm from zero, record the
   app URL and the adapter sha256.
3. Full-chain smoke for **both** model names. `evallab capture smoke`
   takes **no model argument** (only `--upstream/--out/--key-env/--max-tokens`;
   it sends one Terminus-shaped call as the base selector), so it
   proves the base leg only:
   `evallab capture smoke --upstream <LORA_URL>` → expect status 200
   with echoed model `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. Then probe
   the adapter leg through the same proxy → capture → upstream chain
   with `model: selfhosted/...:har129` and require status 200 with
   echoed model `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129` (an
   adapter-arm reply echoing the base id fails the runner identity
   check). Post both.
4. `evallab capture serve --upstream <LORA_URL> --out
   ~/Developer/eval-lab-results/har126-capture/g5/`; point the secret
   proxy's upstream (`EVALLAB_MIMO_SELFHOSTED_UPSTREAM`) at the capture
   address.
5. Generate (section 4, real admitted candidate or two-arm fallback),
   `--check`, `evallab submit` each spec (record queue IDs), and freeze
   the arm-admission/family-size decision with its time **before the
   first trial**.
6. Tick in **position waves** at one pinned concurrency/scheduling
   policy (all first arms, then seconds, then thirds), so each task's
   next arm starts only after its predecessor finishes; task groups may
   run concurrently. Preserve start/end times, per-call latency/queue
   wait and telemetry (`2026-10-01/g5-telemetry.jsonl`).
7. `evallab capture link` per job, `process-job` (counts verdict +
   decision pages) on every job, publish to the results home, then
   write `RESULTS.md` strictly per PREREG (paired tables, N/20, Holm,
   missing-outcome bounds; tuned-vs-GEPA exploratory).

## 7. Capture-smoke coverage answer

**No**: `evallab capture smoke` does not accept a model argument and
covers the base name only. The `:har129` leg needs the manual probe in
step 3 (plus Infra's parity record and the runner's identity check on
the first tuned trial).

## 8. PREREG requirements this prep does not meet (by design — no G5 ran)

- Pre-first-trial arm-admission/family-size freeze and its time.
- G4 adapter sha256, parity proof and serving record (Infra owns).
- GEPA admission (selected, gated candidate bytes/placement/digest and
  selection evidence on main) — only the branch handoff exists.
- `RESULTS.md`: paired tables, exact McNemar/Holm grooves, N/20,
  sensitivity bounds, secondary metrics, reconciled spend.
- Runtime duties at launch: pinned concurrency/scheduling, per-task
  serial enforcement with actual times, infra-only retries approved
  from the reserve, G6 blind-to-arm review.

No deviation from PREREG is introduced by this prep: the generator
refuses non-v2 eval bytes, non-lf2 trees, non-120/2.5M limits and any
cross-arm drift beyond the permitted fields.
