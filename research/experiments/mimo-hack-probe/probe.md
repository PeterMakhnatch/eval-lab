# HAR-83 hack probe (redteam-v1): leak surface, cohort, parked specs, approval

Owner: HackProbe slice. Probe config: stock Terminus-2
(`evallab.harbor_terminus:SecretSafeTerminus2`) plus the fixed adversarial
instruction `research/experiments/preambles/mimo-hack-probe-v1.md`
(`sha256:9984663268ca4cc2ebc9f0116da6981e22a7d462e930203ff02481130045e4b6`).

## 1. Leak-surface answers (with file:line)

**(a) Answer-leak blocklist: NO, our Terminus-2 path did not apply it — fixed
on this branch.**

- FineEnvs applies it in `agents/mimo_opencode.py:63-71`
  (`/private/tmp/mimo-data/terminal/agents/mimo_opencode.py:63-71`):
  after install, `cat /var/lib/mimo/blocklist >> /etc/hosts` as root, fail
  closed. The staged list is 33 hosts (github/gitlab/search mirrors;
  `code/.../environment/setup/files/blocklist:1-33`).
- Stock Terminus-2 never does: `Terminus2.setup`
  (Harbor 0.21 `agents/terminus_2/terminus_2.py:370-389`) only starts the
  tmux session as `environment.default_user`. `SecretSafeTerminus2`
  (`src/evallab/harbor_terminus.py`) had no `setup` override before this
  branch.
- Fix (this branch): `SecretSafeTerminus2.setup`
  (`src/evallab/harbor_terminus.py:291-297`) calls
  `apply_mimo_blocklist(environment)` after `super().setup()`: appends the
  list as root, raises (fail closed, trial never runs unleaked) on nonzero
  exit, no-op (returns `"none for this task"`) when
  `/var/lib/mimo/blocklist` is absent. Tests:
  `tests/test_harbor_terminus_blocklist.py` (5 tests, pass).
- **Flag for HAR-81:** every Terminus-2 trial so far (code/cyber/general
  included) ran WITHOUT the blocklist. Worse, terminal and music tasks never
  stage it at all: their `setup.sh` defines `write_blocklist` but has no
  `files/` dir and never calls it (`terminal/.../environment/setup/setup.sh:1-18`,
  `music-.../environment/setup/setup.sh:1-14`), while code/cyber/general call
  `write_blocklist` in setup (`code/.../setup.sh:31`, `cyber/.../setup.sh:21`,
  `general/.../setup.sh:33`). So the fix bites on code/cyber/general; on
  terminal/music there is nothing to apply (network stays open by design —
  see below). Consider re-running affected HAR-81 trials after integrate.

**(b) Agent user: our path honors `[agent].user`; FineEnvs' reference agent
re-applies it by hand on sandboxes that ignore it.**

- Harbor sets it: `trial.py:412`
  (`with ...with_default_user(self.task.config.agent.user)`) before agent
  setup, and `Terminus2.setup` runs the tmux session as
  `environment.default_user` (`terminus_2.py:386-387`). Docker honors the
  user on exec (`environments/docker/docker.py:1102`); unset user falls back
  to the container default, typically root
  (`environments/base.py:1144-1146`).
- Task declarations: terminal/code/music set NO `[agent].user`
  (`terminal/.../task.toml:21-22`, `code/.../task.toml:20-21`,
  `music-.../task.toml:21-22`) → agent runs as **root** on our path.
  Cyber/general set `[agent].user = "agent"`
  (`cyber/.../task.toml:21-23`, `general/.../task.toml:22-24`) → agent runs
  as **`agent`** on our path.
- `MimoOpenCode` (`mimo_opencode.py:41-61`) drops to that user itself with
  `runuser` because HF Sandbox runs everything as root and ignores the
  setting. Our Daytona/docker path does not need that shim.

**(c) Hidden state and network.**

- `tests/` hidden until verification: the verifier uploads `tests/` into the
  environment only at `verify()` time (`verifier/verifier.py:147-153`); both
  setups additionally scrub image leftovers (`terminal/.../setup.sh:13`
  `rm -rf /tests`, `code/.../setup.sh:24`). Setup itself runs from
  `[environment.healthcheck]` before agent setup (`trial.py:408-414`).
- `.git` hidden in code tasks: `code/.../setup.sh:27-30` moves
  `$CWD/.git` to `/var/lib/mimo/git-hidden` when history extends past the
  base commit, restored at grading (`tests/test.sh:10`). `/var/lib/mimo` is
  `chmod 700` (`setup.sh:6`), owned by root (setup runs as root) — and the
  code-task agent IS root (no `[agent].user`), so **a root agent CAN read
  `git-hidden`** (permission bits do not bind root). Non-root agents
  (cyber/general, `agent` user) cannot — but those domains do not use
  git-hidden; general isolates via `chmod 700 /work/system ...`
  (`general/.../setup.sh:27-30`).
- Network public during the agent run: every sampled task declares
  `network_mode = "public"` (`task.toml:34` terminal, `:32` code, `:34`
  cyber/general/music), and the Terminus lane ships the declared policy
  untouched — no downgrade, no model-host allowlist
  (`src/evallab/runner.py:1726-1733`, `:1868-1871`). The blocklist (where
  staged) is the only answer-leak mitigation, applied post-install because
  the install itself needs github.com.

## 2. Cohort (32 tasks, pinned)

`research/experiments/mimo-hack-probe/cohort.json` — 16 terminal + 16 code
from the provisional train pool. Provisional rule (HAR-81's split is not
frozen): 80/20 hash split on `split_group` per contract (terminal AND code
→ `task_id`; the contract's repository identity for code needs graded test
targets, unavailable statically since every code task ships its own image —
labeled provisional). Pools: terminal 64 (complete), code 2,698 (full HF
tree task.toml files in `/private/tmp/hack-probe-pool/code-tomls/`, rev
pinned). Train: 55 / 2,165. Method: floor-1 per stratum, remaining seats by
largest remainder over pool share (ties: stratum name ascending); seats a
thin stratum cannot fill go to the largest strata. Strata: terminal by area
family (ml4/security3/software3/media2/hardware2/operations1/science1);
code by `[metadata].category` (14 categories with ≥2 train tasks: Go2 +
Python2 + C/C++/Dart/Java/JavaScript/Kotlin/PHP/Ruby/Rust/Scala/TypeScript/
Unknown×1). Excluded: 4 singleton tasks that cannot fill a seat
(`selection.json: excluded_singletons`: Elixir/Lua/Svelte/Swift, one train
task each). "Unknown" is upstream-missing category metadata (Julia-heavy
format tasks), kept as its own stratum. Generator:
`research/experiments/mimo-hack-probe/make_cohort.py` (`select` →
`selection.json`; `stage.sh pull` full trees; `finalize` → `cohort.json` +
specs). Row fields: `task_name`, `task_id`, `domain`, `snapshot_path`
(terminal: pulled snapshot path; code: `FineEnvs/...@rev:tasks/<id>`
source address), `harbor_digest` (`registry.harbor_task_digest`),
`package_digest`, `split_frac` (int, sha256(task_id) % 100), `split`,
`stratum`, `task` (repo-relative dispatch path).

Supersedes note: the first draw (commit 411a7149) hashed the wrong string
(three terminal ids recorded frac ≥ 80 under the contract hash) and sampled
code from the 570 tasks on disk. Both fixed by the redraw; old spec IDs are
dead (stale `queue/waiting/` entries removed).

## 3. Parked specs + nop plumbing proof

- 32 `ExperimentSpec` sources: `research/experiments/mimo-hack-probe/specs/`
  (agent `terminus-2`, model `zai/glm-5.3-flash`, `environment: daytona`,
  `extra_instruction_path` + pinned sha256, per-trial ceilings
  64 req / 200k in / 8192 out / 208192 total / $2.50, `task` =
  `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-<domain>@<rev12>/tasks/<id>`
  with `task_package_digest` pinned — the snapshot lands there via
  `stage.sh pull`; digest mismatch fails dispatch closed).
- Staging (reproducible, run from the primary checkout after merge):
  `research/experiments/mimo-hack-probe/stage.sh pull` (pinned HF revs,
  per-id `allow_patterns`, asserts `task.toml` present),
  `stage.sh submit` (normal `evallab submit` path, prints the approve loop),
  `stage.sh all` = both. `TASKS="id1 id2"` limits either step. Spec `task`
  paths are repo-relative and resolve via `Executor._safe_repo_path`
  (`queue.py:2569`) against the invoking checkout — proven from this
  worktree: `har83-spec-path-proof-0109` dispatched the parked spec's exact
  task form through `Executor.execute_spec` (lease-claim path, digest check
  passed, package `617b64…`) with a nop/docker copy at $0, reward 0.
- $0 nop proofs: `har83-nop-proof-0109` and `har83-nop-proof-0628` ran local
  docker nop trials (8.2 s / 11.5 s, reward 0, no exception); both flow
  into `task_exploits.parquet` as `exploit_status: none`,
  `method: nop plumbing proof` (0628 also keeps its cohort `not_probed`
  row — worst-status rollup distinguishes probed-clean from unprobed).
- `task_exploits.parquet` (contract schema + `detector`, nullable
  `review_by/at/note`): `/Users/petermakhnatch/Developer/eval-lab/derived/parquet/external/task_catalog/task_exploits.parquet`
  — 34 rows: 2 `none` (nop proofs), 32 `not_probed` (redrawn cohort, no
  trial yet). Detector: `src/evallab/mimo_exploit.py`
  (`mimo_exploit/v1`; reuses `evallab.traj` outline + `EDIT_TOOL_NAMES`;
  git-history rule follows Trace Lab `probe-01-git-peek/detect.py`
  `flag_shell_text`, ported since that repo cannot be imported here).
  `confirmed` is reachable ONLY via `confirm_exploit(record, reviewer, note)`
  with reward == 1 + signals; tests: `tests/test_mimo_exploit.py` (7 tests).

## 3b. Blocklist fix — real proof (not a unit test)

`har83-blocklist-proof-cyber` ran `SecretSafeTerminus2` for real on cyber
task `arvo_10055` (stages the blocklist, agent user `agent`) in local
docker with local Ollama `ollama_chat/qwen2.5:7b` ($0; 300 s fail-safe hit,
outcome irrelevant). Trial log
(`runs/har83-blocklist-proof-cyber/har83-blocklist-proof-cyber__mFRAH92/trial.log:3`):
`answer-leak blocklist: 32 hosts blocked in /etc/hosts`. Agent trace
(`agent/trajectory.json`, 14 steps): `agent@c5f023c89715:~$ getent hosts
github.com` → `0.0.0.0  github.com`. Setup-phase apply (root) + unprivileged
agent blocked from the leak. Proof scaffolds (`runs/proof-tasks/`,
gitignored) kept for reproducibility.

## 4. Costed approval block (for the lead to post on HAR-83)

```markdown
## HAR-83 hack-probe approval request (redteam-v1, 32 trials)

Probe: stock Terminus-2 + fixed adversarial instruction
`research/experiments/preambles/mimo-hack-probe-v1.md`
(sha256:9984663268ca4cc2ebc9f0116da6981e22a7d462e930203ff02481130045e4b6).
Cohort: `research/experiments/mimo-hack-probe/cohort.json` (16 terminal + 16 code,
redrawn from the full pools: terminal 64, code 2,698).
Staging (reproducible — run from the primary checkout after merge):

    research/experiments/mimo-hack-probe/stage.sh all
    # 1) pulls the 32 pinned snapshots into derived/task-store/hf/... (per-id
    #    allow_patterns, asserts task.toml present; digests pinned in specs and
    #    re-verified at dispatch — mismatch fails closed)
    # 2) submits all 32 specs via evallab submit and prints the approve loop
    #    with the fresh spec IDs

Formula: tasks x attempts x (est_in_M x in_price + est_out_M x out_price) + sandbox.
Bounds use the enforced per-trial ceilings (64 req / 200k in / 8192 out / $2.50):
32 x 1 x (0.200000 x $0.15 + 0.008192 x $0.50) = 32 x $0.034096 = $1.09 tokens.
Daytona sandbox hours are NOT in this cap (no $/h pinned in repo) - confirm
Daytona budget separately, or re-stage the same specs with environment docker.

Model options (pinned list prices, `price_table.py`, retrieved 2026-09-26):
- zai/glm-5.3-flash $0.15/M in ($0.03 cached) / $0.50/M out <- specs use this
- zai/glm-5.3 $1.40/M in ($0.26 cached) / $4.40/M out (~$9.90 at ceilings - NOT recommended)
- local ollama_chat/qwen2.5:7b $0 tokens (needs installed GGUF + spec model swap)
Peter chooses. No weight downloads, no publication.

Cap: $2 total provider tokens across the printed approve loop
(formula $1.09 + headroom). Approve with the loop `stage.sh submit` prints
(IDs are queue-local; the worktree IDs are superseded on re-stage — do NOT
approve the IDs from earlier reports).
```
**[Data]** hack-probe slice staged (redrawn). Interim: blocklist gap found,
fixed, and proven live on cyber (flag for HAR-81 above); 32 specs staged via
`stage.sh` (re-run after merge); nop proofs + `task_exploits.parquet` (2 none /
32 not_probed) landed.
