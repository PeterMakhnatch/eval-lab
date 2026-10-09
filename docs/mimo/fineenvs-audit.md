# FineEnvs MiMo-V2.6-RL-harbor audit (1.1 → 1.2.0 → 1.3.0)

Tiers: `MEASURED` (ran or diffed it), `SOURCE-QUOTED` (quoting a file/commit with a
line number), `[INFERENCE]` (best reading, marked). Times UTC. "Pin" = Eval Lab's
pinned pre-release revisions (e.g. code `5746e2f0`, 2026-09-27), whose tasks record
`adapter = "mimo_harbor 1.1.0"`.

## Verdict

FineEnvs patched the network route twice (1.2.0 extended the blocklist, 1.3.0 applies
it at the end of setup for every agent) and ported Xiaomi's git-strip recipe in 1.2.0 —
but wrapped it in the same reachability gate Xiaomi's assert uses, so the strip never
runs on exactly the images that need it (unreachable-only, 87/100 of Eval Lab's sample,
67% of code tasks per Vals). No image was rebuilt in any version: every `docker_image`
digest compared is identical across 1.1 → 1.2.0 → 1.3.0. The dataset card's claim that
the strip removes "future commits, **including unreachable objects**" is false for the
setup path (`MEASURED`: the current published 1.3.0 setup leaves post-base commits
recoverable on 2/2 cached images re-tested today, including the exact fix commit our
cheat ladder already exploited). `test.sh`, Dockerfiles, and instruction prompts are
byte-identical across all three versions on every task sampled: grader tampering,
mtimes, installed copies, and prompt wording are unaddressed. The GitHub adapter source
on `main` still says `ADAPTER_VERSION = "1.1.0"` while the published tasks say 1.3.0,
so the published 1.2.0/1.3.0 generator cannot be reviewed or re-run from source.

## Sources and method ($0)

- GitHub `adithya-s-k/FineEnvs` cloned 2026-10-09 (`5e0b46e`); adapter at
  `tooling/mimo-rl-explorer/mimo_harbor`, `ADAPTER_VERSION = "1.1.0"`
  (`adapter.py:29`, `SOURCE-QUOTED`). No 1.2.0/1.3.0 generator commit exists on any
  branch (`MEASURED`: `git log --all -- mimo_harbor` ends at the 1.1.0 commit `f67b989`,
  2026-09-26).
- All six HF dataset repos cloned with `--filter=blob:none` (commit metadata only;
  blob fetches fail, so file content was pulled per-file over HTTP at pinned revisions
  and diffed locally, `MEASURED`). Version boundaries from commit messages + dates
  (`SOURCE-QUOTED`):

| Domain | 1.1-era pin (Eval Lab) | 1.2.0 root → tip | 1.3.0 root → tip (= HEAD today) |
|---|---|---|---|
| code | `5746e2f0` 09-27 | `b0e054a6` → `e60dca37` 10-05 | `86004f41` → `cf87dbe4` 10-09 |
| cyber | `763882a` 09-27 | `520b091` → `3164ed3` 10-05 | `0e0aa5a` → `ec95572` 10-09 |
| general | `10b732c` 09-27 | `7f601c0` → `3fb672c` 10-05 | `4647264` → `85c8884` 10-09 |
| terminal | `fe1c2b6` 09-27 | `8e8d504` → `64cf515` 10-05 | `9b0b0b2` → `b74a53f` 10-09 |
| webdev | `e1a6293` 09-27 | `719be70` → `082fb12` 10-05 | `27537f7` → `884ed0a` 10-09 |
| music | `e1a66d4` 09-27 | `6afe02d` (+`5036010`) → `cbe3cf0` 10-05 | `d30566e` → `9873f05` 10-09 |

  Per-task content lives in the `part N` batches after each root commit, so roots were
  compared via tips (`MEASURED`). `git ls-remote` on all six repos 2026-10-09 returns
  exactly the 1.3.0 tips above: **nothing newer than 1.3.0 has landed today** (`MEASURED`).
- Xiaomi `mimoagent@467f0a1` cloned 2026-10-09 (HEAD is `467f0a1`, `MEASURED`).
- HF discussions on `XiaomiMiMo/MiMo-V2.6-RL-oss` #3 (Syntology, general) and #6
  (raimondasl, cyber overlap) read 2026-10-09 (`SOURCE-QUOTED`).
- Sample tasks diffed pin → 1.2 tip → 1.3 tip: code `format-code-task-{000792,000041,
  000158,000160,000324,000666,000832,001809,002402,002552}`, cyber `arvo_8944`,
  terminal `candidate-2996-ml-evaluation`, general
  `s3k_0000_accounting_audit_tax_en_t1_rl_008`, webdev `dasyn_260630_00001`, music
  `music-gk-0000` (`MEASURED`).

## What each version changed

### September pre-release ("1.1", adapter 1.1.0, initial upload 2026-09-26 + parity 09-27)

- Adapter commits: `6b90767` (adapter for all 7,780 envs), `af7124d` (publish with
  credits/licenses/NOTICE), `f67b989` (1.1.0: titles, categories, difficulty, keywords
  in every task) — all 2026-09-26, all before the HF uploads started the same evening
  (`MEASURED`: adapter dates vs first HF upload 21:16 UTC). Published tasks record
  `adapter = "mimo_harbor 1.1.0"` (`SOURCE-QUOTED`, task.toml at pin).
- Code `setup.sh` (pin, 34 lines): computes `BASE`, gates on
  `LATER=$(git rev-list --all --not "$BASE" …)` (L23), and only moves `.git` to
  `/var/lib/mimo/git-hidden` when `LATER > 0` (L27–30). Unreachable-only images get no
  action; the hidden copy stays root-readable (`SOURCE-QUOTED`).
- Blocklist `files/blocklist`: 33 lines / 32 hosts (code sites, search engines, Go
  proxy). **No PyPI, no npm, no crates, no Hub hosts** (`SOURCE-QUOTED`, full text at
  pin). It is only *staged* (`write_blocklist` copies the file); the reference agent
  applies it after installing (adapter README, "The agent" §1, `SOURCE-QUOTED`).
- Already present at pin: `/tmp` + language-cache scrubs and `git clean -fdx` with the
  keep-list (code setup L24–26); general venv + `mcp==1.26.0` sidecar fix and
  root-only systems isolation (general setup L16–34, `SOURCE-QUOTED` — i.e. the HF #3
  general start failure was already worked around in the Harbor setup before 1.2.0);
  cyber verify server + `agent` user (cyber setup L13–22); terminal `anti_hack_guard.py`
  + pristine-manifest fixtures in `tests/` (byte-identical pin → 1.3 tip, `MEASURED`).
- Terminal gap at pin: `setup.sh` *defines* `write_blocklist` (L9) but never calls it,
  and ships no `files/blocklist` (HTTP 404 at pin, `MEASURED`) — the blocklist was dead
  code for terminal until 1.2.0.

### 1.2.0 (2026-10-05, "initial public release" per the card)

- Code `setup.sh` (1.2 tip, 53 lines): adds `node_modules/.cache` + `.vitest` cleanup
  (L26) and replaces the hide with a port of mimoagent's strip recipe — detach HEAD,
  delete branches/remotes/replace/notes/stash/remotes/pull refs, delete post-base tags,
  expire reflog, `gc --prune=now` (L32–44) — verified by post-base timestamp commit
  count (L41–44). **The whole block stays inside `if [ "$LATER" -gt 0 ]`** (L28), so
  unreachable-only images still skip it (`SOURCE-QUOTED`). `test.sh`, Dockerfile,
  `instruction.md` byte-identical to pin (`MEASURED`).
- Blocklist 33 → 62 lines: +12 Hub hosts (`huggingface.co`, `hf.co`, xet/cas/transfer
  endpoints) and +17 registries/CDNs (`pypi.org`, `files.pythonhosted.org`, npm, yarn,
  crates, rubygems, maven, nuget, packagist, hex, jsdelivr, unpkg, esm.sh, cdnjs)
  (`MEASURED`, diff hunks `33a34,62`). Still staged-only.
- Terminal: `write_blocklist` actually called (1-line diff, `MEASURED`) and
  `files/blocklist` (62 lines) added — HTTP 200 at tip vs 404 at pin (`MEASURED`).
  Cyber/general/webdev/music `setup.sh` byte-identical pin → 1.2 tip (`MEASURED`;
  cyber/general already had blocklist files + staging).
- Card + jobs (not per-task files): new "Where this differs from Xiaomi's setup, in
  plain terms" section, `jobs/<kind>.daytona.yaml` (builds Dockerfile, runs the agent
  unprivileged), `task.toml` adapter string `1.1.0` → `1.2.0`, re-rendered healthcheck
  payload (`MEASURED`: README headings diff pin → 1.2 tip; `jobs/code.daytona.yaml`
  HTTP 200 at tip). `source_revision` (`639865fd…`) unchanged (`SOURCE-QUOTED`).
- Music quirk: two 1.2.0 commits (`6afe02d` 12:18, `5036010` 13:04); per-task content
  compared at the later tip `cbe3cf0` (`MEASURED`).

### 1.3.0 (2026-10-09, "environment-level answer-leak enforcement")

- One functional change, same shape in every domain that carries a blocklist: the
  staged list is appended to `/etc/hosts` as the **last** setup step (POSTAMBLE,
  code setup L54–57; identical hunk in cyber L25–28, general L37–40, terminal L20–23),
  with `hosts.preblock` backup and an idempotence guard (`MEASURED`). The header
  comment is rewritten to say staging exists so General can download workplace files
  from the Hub *before* the block lands (code setup L8–10, `SOURCE-QUOTED`).
- Blocklist file, `test.sh`, Dockerfile, `instruction.md`, task list (2,698 → 2,698),
  `source_revision`: all unchanged 1.2 tip → 1.3 tip on every sample (`MEASURED`).
  Within the 1.3.0 push itself only the comment + POSTAMBLE moved (root `86004f41` →
  tip `cf87dbe4`, `MEASURED`).
- Webdev/music `setup.sh` got the same POSTAMBLE text, but both domains ship **no**
  `files/blocklist` (HTTP 404 at tip, `MEASURED`), so the `if [ -f … ]` guard never
  fires there — the card's "webdev and music are unchanged" is functionally true
  (`[INFERENCE]` from the 404 + the guard; no behavior to change).
- Card gains the "Patches, updates and fixes" changelog (1.3.0 + retrospective 1.2.0
  entries) and per-task adapter strings become `mimo_harbor 1.3.0` (`MEASURED`).
- **No image rebuilt**: `docker_image` digests identical 1.1 → 1.2.0 → 1.3.0 on 9/9
  sampled code tasks (`MEASURED`, full digests in evidence), and the 1.3.0 check's
  census found 0 of 2,206 compared digests differing 1.2 → 1.3
  (`research-context/analysis/2026-10-09-fineenvs-1.3.0-check.md`, `MEASURED` there).
  All defects baked into images therefore survive every version.

### Adapter source vs published tasks (stale source)

GitHub `main` still renders `ADAPTER_VERSION = "1.1.0"` (`adapter.py:29`,
`SOURCE-QUOTED`), and its README still documents 1.1-era behavior ("adds 32 hosts",
"hides `.git` if the image's history reaches past the base" — the strip, the 62-host
list, and the POSTAMBLE are absent, `SOURCE-QUOTED`). There is no 1.2.0/1.3.0 generator
commit on any branch (`MEASURED`). Anyone re-running the adapter from source gets 1.1.0
tasks, not what the Hub serves — the published hardening is unreviewable and
unreproducible from the repo. (The clean plan already noted the 1.2.0/1.1.0 skew; it is
now 1.3.0/1.1.0.)

## How each mechanism maps to Xiaomi's mimoagent@467f0a1

| FineEnvs mechanism | Xiaomi source | Relationship |
|---|---|---|
| `LATER` gate (`rev-list --all --not $BASE`, code setup L23–25) | `opensource_code.py:156–186` `_assert_history_truncated` (same command, L170–173) | copied, blind spot included: rev-list sees branches, not unreachable objects (`SOURCE-QUOTED`) |
| 1.2.0 strip recipe (setup L32–44) | `base.py:826–873` `_strip_future_commits` (detach, delete refs/tags, expire, `gc.pruneExpire=now` + `cruftPacks=false`) | near-verbatim shell port, **including** the timestamp-based verify (`base.py:876–882` → setup L41–44) (`SOURCE-QUOTED`) |
| Gating the strip on the assert | `opensource_code.py:88–93` (`_GIT_LEAK_PREVENTION_DEFAULT = "none"`: "nothing to strip at rollout time") vs `base.py:101` (default `"strip"`, unconditional) | FineEnvs combined the two: strip recipe + assert gate. Xiaomi never gates strip on reachability — strip-mode datasets (DeepSWE `deepswe.py:204`, generic `generic.py:65`) call `_prevent_git_hack()` unconditionally. The gate is FineEnvs' own bug (`[INFERENCE]` from the two call sites) |
| Blocklist staging → 1.3.0 POSTAMBLE to `/etc/hosts` | `base.py:190–208` (apply "at the very end of setup"; write failure **aborts**) + `kubernetes.py:83–86` ("DNS-level only") | concept ported; two weakenings: setup.sh is best-effort (no abort), and a root agent can rewrite `/etc/hosts` either way (`SOURCE-QUOTED`) |
| Blocklist *content* (62 hosts) | no in-code default — `answer_leak_blocklist` is user config (`kubernetes.py:83`, `modal.py:119`) | content authored by FineEnvs (`[INFERENCE]`; nothing to quote because there is nothing there) |
| `/tmp` + `.m2`/julia/gradle/bazel scrubs, `git clean` keep-list (setup L26–29) | `base.py:594–598`, `:630`, `:758` (three build-env purges: residue, artifacts, global caches) | ported; Eval Lab's `purge-installed-copies`/`purge-build-caches` go further (tracked `build/`, site-packages, `.pytest_cache`) |
| Code grading: reset patch files → apply → exit-code reward (test.sh L14–39) | `opensource_code.py:190–240` (`_reset_test_files`, `_apply_test_patch`, `reward = 1.0 if rc == 0`) | faithful, tamper surface included: reset touches only patch files, no untracked cleanup, `sh -c` runner (`SOURCE-QUOTED` both sides) |
| Cyber grading re-runs the PoC with the server's code (test.sh L1–6) | `arvo.py:104–107` reads `/root/last_result.json` | **stricter than Xiaomi**: closes the last_result overwrite (adapter README deviations table, `SOURCE-QUOTED`). But `/root/binary` is re-run without a checksum and everything is root-readable on prebuilt-image backends — the binary-swap route stays open (`[INFERENCE]`) |
| Terminal `anti_hack_guard.py` + pristine manifest | no counterpart in `terminal/` scope; agent-side `agents/antihack.py:1` is a tool-call regex guard, a different thing | conversion-authored (`[INFERENCE]`: present since the 09-27 pin, Harbor-mechanics docstring, no upstream origin). Checks `/app` only — `sitecustomize.py`/`.pth` elsewhere still run at grading (our terminal ledger, `MEASURED` there) |
| Unprivileged `agent` user (cyber/general setup, `agent_user` file; `[agent].user` + Daytona yaml) | Xiaomi runs agents in pods as non-root; arvo `REPO_PATH = "/home/agent"` (`arvo.py:49`) | approximated where the backend allows user switching; on HF Sandbox everything runs as root, so `/root/fix_binary` (135 cyber images, HF #6 follow-up), `/root/server.py`, `/root/expected_func.json` stay in reach (`[INFERENCE]`) |

## Checklist status (V1–V7, E1–E6)

"Fixed" means the published 1.3.0 tasks, verified above or in the read-first docs.

| Item | FineEnvs status | Evidence | Eval Lab mechanism |
|---|---|---|---|
| V1 future history on branches | fixed (1.1 hide; 1.2 strip when the gate fires) | on-ref control `000324` stripped, log 1088→1 (1.3.0-check, `MEASURED` there) | `strip-future-history@1` (unconditional rebuild, no gate) |
| V2 unreachable git objects | **not fixed** | 1.3.0 setup leaves 12 (002552) / 5,067 (001809) unreachable commits, incl. post-base `e88159fb` — re-verified today on cached images, § Re-verify (`MEASURED`); 6/6 recoverable in 1.3.0-check (`MEASURED` there); Vals 67% (`SOURCE-QUOTED`) | `strip-future-history@1`; ladder shows strip-002552 → 0 unreachable, `git_history` clean (`SOURCE-QUOTED`, cheat-tamper-ladder README) |
| V3 mtimes point at fix files | **not fixed** | 002402: 5 fix files share one later mtime, tree == base (1.3.0-check, `MEASURED`); strip uses `read-tree`, never touches work-tree files (`src/evallab/strip_future_history.py` L71–91 per issues catalogue, `SOURCE-QUOTED`) | `mtime-normalize@1` |
| V4 own pack-file parser (git blocked → parse `.git` directly) | **not fixed** | `.git` fully present on unreachable-only images; nothing in any version restricts git-object reads (`MEASURED`: fsck/cat-file work post-setup) | strip removes the objects → route dead on stripped images, covered by automation (clean plan, `MEASURED` there) |
| V5 build/module caches as fix source | partial | 1.2.0 added node-cache line; `/tmp`, m2/julia/gradle/bazel scrubs since 1.1. `build/` trees, site-packages copies, `.pytest_cache` naming failing tests untouched (`SOURCE-QUOTED` setup L26–29 vs issues catalogue A2–A3) | `purge-installed-copies@1`, `purge-build-caches@1` |
| V6 network fetch of fix release | fixed in 1.3.0 (for setups that carry a blocklist) | POSTAMBLE in code/cyber/general/terminal; 62-host list incl. PyPI/npm/crates/Hub (`MEASURED`). Caveats: DNS-only, root-rewritable, needs a network-connected sandbox to matter | network lock mandatory since HAR-140 (Daytona `network_block_all`); grading unchanged on 1,141/1,149 (issues catalogue A4, `MEASURED` there) |
| V7 prompt wording | **not fixed** | `instruction.md` byte-identical pin → 1.3 tip on all four samples (`MEASURED`) | structural (`separate-verifier@2`), not prompt |
| E1 installed/build copies | **not fixed** | `git clean` excludes `lib`; no `build/` or site-packages purge in any version (`SOURCE-QUOTED` setup L28) | `purge-installed-copies@1` (oracle 1 / nop 0 on 001269, 002308) |
| E2 grader tamper (conftest, sitecustomize, PATH shadow, reward writer, in-source exit, pytest monkeypatch, Go TestMain) | **not fixed** (code); partial (terminal guard, `/app` only) | `test.sh` byte-identical across versions; reset covers only patch-touched files (L14–30), reward = exit code (L39) (`SOURCE-QUOTED`). Six tamper attacks score 1.0 on our hardened grading, which already exceeds FineEnvs' (grader-fix doc, `MEASURED` there) | `separate-verifier@2` (patch-only verifier, 4 Python tasks oracle 1 / nop 0 / ladder clean) |
| E3 grader in agent container, agent root | **not fixed** | `[verifier] user = "root"` in every task.toml sampled; Harbor uploads `tests/` post-agent but planted files persist into grading (`SOURCE-QUOTED` + E2 evidence) | `separate-verifier@2` (clean checkout, agent contributes patch bytes only) |
| E4 no reference fixes | **not fixed** | nothing ships; hidden tests exist in no upstream commit (issues catalogue C8) | 192 Python oracle-pass fixes indexed; 657 never attempted (clean program, `MEASURED` there) |
| E5 broken envs | partial | general start failure worked around in-setup since 1.1 (venv + `mcp==1.26.0`, `SOURCE-QUOTED`); music bookworm image (card, `SOURCE-QUOTED`); C1–C4 class (deleted build outputs, login shell, pins, network graders) unaddressed upstream | ledger repairs: Python 1,147 keep / 1 fix / 32 discard; cyber/terminal/music ledgers (clean plan) |
| E6 network-dependent graders | **not fixed** | graders that download (pip, binaries, judges, renders) unchanged; general `fetch.py` runs *before* the 1.3.0 block lands by design (setup L8–10 + L16, `SOURCE-QUOTED`) | prefetch repairs (6 tasks), music `abcmidi` offline fix, judge-override env vars |

Related upstream reports, untouched by any version: HF #3 general reward-integrity items
(do-nothing reward 18–20/50, wrong-file judge fallback, missing `state.db.pinned_backup`,
unprotected `src_protect`) — cf. our zero-weight/gate variants; HF #6 cyber overlap
(278/1,000 share CyberGym bugs, 164 duplicate ID pairs, 135 `fix_binary` images) and the
open questions (image build provenance, agent uid) — cf. our cyber ledger (583 keep /
287 out / 130 drop); judge temperature/severity (general/webdev temp-1.0, 1,500-char
brief cap) — cf. our judge-reliability study (`SOURCE-QUOTED` discussions + clean plan).

## Re-verify ($0, 2026-10-09, current published 1.3.0 setup)

Containers from cached digests, `--platform linux/amd64 --network none`, as root; staged
the published tip `setup.sh` + `files/blocklist` (task-uniform: byte-identical across
000792/001809/002552, `MEASURED`) to `/var/lib/mimo` and ran it (`MEASURED`):

| Task (image) | Setup output | `LATER` | Unreachable commits post-setup | Post-base recovery |
|---|---|---|---|---|
| 002552 python-miio (`…@sha256:2dcbed218c61`, container `2dcbed218c61`) | `setup done`, no strip line | 0 | 12 commits / 7,699 objects | `e88159fb` (2024-10-27 00:45, +6 min after base `d5179059` 00:39) diffs `miio/miot_models.py` +9 — the same commit our `git_history` cheat applies for reward 1.0 (cheat-tamper-ladder README, `SOURCE-QUOTED`) |
| 001809 moto SQS (`…@sha256:5601987ef363`, container `5601987ef363`) | `setup done`, no strip line | 0 | 5,067 commits / 102,059 objects | `21000439b` (2024-06-20) vs base `1998a340e` (2022-11-15); ladder `git_history` reports clean here (fix not cleanly recoverable on this one — `SOURCE-QUOTED` receipt), so presence ≠ exploitability per task |

The gate never fires (`LATER=0` both), the strip never runs, the objects stay readable
to a root agent via `git fsck`/`git cat-file`/`git show`. This reproduces the 1.3.0-check
result (6/6 recoverable) against today's tip, so 1.3.0 being the latest changes nothing:
**the current published setup still leaves unreachable fix objects** (`MEASURED`).

## Approach judgment (FineEnvs vs detector-side vs rebuild-images)

Three ways to handle baked-in answers:

1. **Patch at setup (FineEnvs).** Cheap, no image access needed, ships as text. Fails
   exactly where it matters: the gate checks reachability while the leak is
   unreachability; best-effort shell (no abort on failure, unlike mimoagent's
   `raise`); root agent shares the container with the grader and the hidden `.git`.
   Text patches also drift from their source (adapter repo stuck at 1.1.0) with no
   check pinning generator to output.
2. **Keep the env, verify separately (detector-side).** Accept that the rollout
   container is hostile: extract the agent's patch, grade it in a clean container that
   never contained the leak, and scan the patch for tamper signatures. Costs one clean
   checkout per grade; closes whole classes (git exfil, conftest, PATH shadow, reward
   writer) instead of individual routes.
3. **Rebuild the images.** The only fix for baked-in content: prune objects, normalize
   mtimes, delete build copies at build time. Requires the image build pipeline, which
   only Xiaomi has — and its harness asserts rather than strips for code tasks, so a
   rebuild would need a harness change too.

Eval Lab uses **(2) plus build-equivalent transforms**: `strip-future-history@1`
(rebuilds `.git` from the base closure unconditionally — the in-place `gc` recipe made
deterministic), `mtime-normalize@1`, `purge-installed-copies@1`,
`purge-build-caches@1` materialized into derived variants (i.e. we re-ship clean
repos, the part of (3) we can do without the image builds), and
`separate-verifier@2` (grade-the-patch, the detector-side idea made structural).
Rationale: (1) cannot work against a root agent holding unreachable objects — we
measured it failing; (3) is upstream-only. What remains ours to prove is per-task
acceptance on the materialized clean set (oracle 1, nop 0, ladder clean) and the
non-Python ports.

## Upstream report (draft — do not post anywhere)

Concrete bugs for FineEnvs (`FineEnvs/MiMo-V2.6-RL-harbor-*`, 1.3.0 tips verified
2026-10-09; nothing newer on the Hub as of `ls-remote` today):

1. **The git strip never runs on unreachable-only images.** `tasks/*/environment/setup/
   setup.sh` L25/L30 (code; same shape all domains): `LATER=$(git rev-list --all --not
   "$BASE" …)` counts only *reachable* future commits, and the strip block (L32–46)
   runs only `if [ "$LATER" -gt 0 ]`. Unreachable-only images — the common case
   (87/100 usable Python per HAR-177; 67% of code per Vals) — skip it entirely.
   Minimal repro (no model, no network):
   ```bash
   img=docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:2dcbed218c61  # code 002552
   docker run -d --platform linux/amd64 --network none --name repro "$img" sleep 3600
   # stage the published setup.sh + files/blocklist to /var/lib/mimo, run setup.sh
   docker exec repro bash -c 'cd /testbed && git rev-list --all --not $(cat /var/lib/mimo/base) | wc -l'  # 0: gate skips
   docker exec repro bash -c 'cd /testbed && git fsck --unreachable --no-reflogs | grep -c "unreachable commit"'  # 12
   docker exec repro bash -c 'cd /testbed && git show e88159fbac2f2f2673641586fa46517632e7b1f2 --stat'  # the fix
   ```
   Suggested fix: drop the gate (run the strip unconditionally like
   `_prevent_git_hack` in strip mode), or fail setup closed when unreachable
   post-base objects exist (`git fsck --unreachable` / `cat-file --batch-all-objects`
   against base ancestry). Note the ported verify (`git log --all
   --since=base+1s`) only sees reachable commits — it cannot verify unreachable
   removal.
2. **Card claim is false.** Code README "Patches" §1.2.0: the strip "physically
   removes fix-bearing future commits, **including unreachable objects**" — repro #1
   shows it does not run at all on such images. Same for "No answer in the git
   history… we delete those later commits" ("Where this differs", §3).
3. **Grader-tamper routes (code `tests/test.sh`, unchanged since September).** Only
   patch-touched files are reset (L14–30), so a planted root `conftest.py`, Go
   `TestMain`, `sitecustomize.py`, PATH shadow, `reward.txt` writer, or in-source
   `os._exit(0)` survives into grading; reward = test exit code (L39); runner is
   `sh -c` without login shell (L36). We measured six such attacks scoring 1.0 on
   grading strictly stronger than this (see grader-fix doc). Suggested fix: clean
   untracked files before grading, or grade the extracted patch in a fresh container.
4. **File mtimes name the fix.** E.g. code 002402: five upstream-fix files share one
   later mtime, tree == base. `find -newermt` points at the answer. Suggested fix:
   normalize all work-tree mtimes at the end of setup (one `touch -d` pass).
5. **Published generator is ahead of source.** Hub tasks say `mimo_harbor 1.3.0`;
   GitHub `main` `adapter.py:29` says `1.1.0` with no 1.2.0/1.3.0 commit, and its
   README still documents hide-only behavior and 32 hosts. Suggested fix: push the
   generator that built 1.2.0/1.3.0 so outputs are reviewable and reproducible.
6. **(Minor, already fixed in 1.2.0.)** Terminal 1.1 `setup.sh` defined
   `write_blocklist` but never called it and shipped no blocklist file — terminal had
   no staging at all until 1.2.0.

Draft only. Nothing here has been posted, filed, or messaged to anyone.
