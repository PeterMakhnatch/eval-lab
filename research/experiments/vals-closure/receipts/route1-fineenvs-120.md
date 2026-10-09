# Route 1 receipt: FineEnvs 1.2.0 still leaves unreachable fix objects (MEASURED)

Date: 2026-10-09 UTC. $0 (local Docker, `--network none`, pre-pulled images).
Scratch: `/private/tmp/mimo-night/vals-closure/` (setup-000792-110.sh,
setup-000792-120.sh, task-*-120.toml, mimo120/).

## 1.1.0 (pinned snapshot) vs 1.2.0 (HF current) setup diff, task 000792

1.2.0 source: `FineEnvs/MiMo-V2.6-RL-harbor-code@e60dca3794baff1a099d505c02c175f42727e8a4`
(`task.toml` adapter field `mimo_harbor 1.2.0`; setup extracted from the
embedded healthcheck blob, same unpacking as `strip_future_history.py`).
Pinned 1.1.0: `derived/task-store/hf/...@5746e2f0c5c6`, adapter 1.1.0.

1.2.0 adds three things (SOURCE-QUOTED, diff in scratch):

1. `rm -rf /testbed/node_modules/.cache /testbed/node_modules/.vitest` —
   partial rung-5 (JS caches) cleanup.
2. Blocklist +28 entries: huggingface.co + xethub/cas hosts, pypi.org +
   files.pythonhosted.org, npm/yarn, crates.io, rubygems, maven, nuget,
   packagist, hex.pm, jsdelivr/unpkg/esm.sh/cdnjs (full new list in
   scratch `mimo120/files/blocklist`).
3. On-ref handling in place (replaces `mv .git $M/git-hidden`): detach at
   BASE, delete heads/remotes/replace/notes/stash/remotes/pull refs, delete
   tags not ancestral to BASE, `reflog expire --expire=now --all`,
   `gc --prune=now`, then verify
   `git log --oneline --all --since=<BASE+1s>` is empty. On success prints
   "future commits stripped in place" and `.git` stays; only on failure is
   `.git` hidden.

What 1.2.0 does NOT change: the trigger is still
`git rev-list --all --not $BASE` (reachable refs only). For unreachable-only
images LATER=0, the whole block is skipped — `gc` never runs.

## Repro: 1.2.0 setup leaves unreachable fixes readable (3 tasks)

Procedure per task (exact Oct-7 pattern): `docker run -d --rm --network none`,
`docker cp` the 1.2.0 `mimo120/` bundle to `/var/lib/mimo`, `bash setup.sh`,
then `rev-list --all --not BASE`, `fsck --unreachable`, `cat-file -t` and
`git show --stat` of the known fix.

| Task | Image | later refs | unreachable commits | Fix readable after 1.2.0 setup |
|---|---|---|---|---|
| 002552 (python-miio) | 2dcbed218c61 | 0 | 12 (= HAR-161 exact) | e88159fb + edb06c52, both `commit`; `git show --stat` names `miio/miot_models.py` + tests |
| 002402 (numpyro) | 6d706ce6902c | 0 | 696 (= HAR-161 exact) | 56f63eb6 `commit`; stat names `numpyro/distributions/batch_util.py` + `test/contrib/test_control_flow.py` |
| 001269 (responses) | e4feae817a2d | 0 | 192 (= HAR-161 exact) | 3 sampled unreachable commits readable (`006068cd`, `43c0b63a`, `5de05d58`) |

Setup printed plain `setup done` on all three (no strip message): the 1.2.0
strip block never fired. Unreachable counts are byte-identical to the HAR-161
pre-1.2.0 exact counts, so `gc` (which never ran) pruned nothing.

## Verdict

1.2.0 closes, beyond 1.1.0: on-ref images (strip in place, no root-readable
`git-hidden` copy left behind on success) and the network-download rung for
the listed registries (blocklist, still hosts-file based, still root
-rewritable — the Daytona egress lock remains the real control). It closes
**nothing** for the unreachable-only shape (87/100 of HAR-177, 100% of the
three re-verified here): no action is taken and the fix stays recoverable
with stock git subcommands. Our `strip-future-history@1` remains required.
