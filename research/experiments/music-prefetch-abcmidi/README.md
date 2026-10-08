# music-prefetch-abcmidi: grading without the network (all 1,000 music tasks)

Status: merged variant + locked validation. $0 (local Docker, no model/judge calls, no image pulls).

## Problem

Every music task's grader (`tests/test.sh`) runs
`apt-get install abcmidi=20250216+ds-1` at grade time. Under the egress lock
that install fails and grading exits 1 unscored — MEASURED by the
 cyber/music/terminal investigator on `music-gk-0000` (`--network none`,
no answer: `could not install abcmidi`, exit 1). All 1,000 tasks share one
image (`python@sha256:f77ac9e…`) and byte-identical `setup.sh`, `test.sh`,
`Dockerfile` and `grade.py` (MEASURED: sha256 group-by over all 1,000 task
dirs, one distinct hash each).

## Fix

Transform `music-prefetch-abcmidi@1` (`src/evallab/music_prefetch.py`): setup
installs the same pinned `abcmidi` version before the agent starts, while the
network is open, and fails setup if it cannot. The grader already skips its
own install when `abc2midi` is on PATH (`command -v` guard in `test.sh`), so
grading never touches the network and no grader logic was changed. Only
`environment/setup/setup.sh` and the `task.toml` healthcheck payload change
(`components_changed = ["task_toml", "environment"]`); `tests/` is
byte-identical by construction, and parents without the grader guard are
refused, not rewritten.

Text-only and architecture-aware: the setup block names a version, never an
architecture or a `.deb` URL, so `apt` resolves the image-arch build on its
own. Embedding the `.deb` was rejected by design — variant rules refuse
binary files, and one `.deb` would pin one architecture.

## What ran

```bash
git -C ~/Developer/eval-lab fetch origin
git -C ~/Developer/eval-lab worktree add .worktrees/music-prefetch -b music-prefetch origin/main
cd ~/Developer/eval-lab/.worktrees/music-prefetch && uv sync --frozen
uv run pytest tests/test_music_prefetch.py -q            # 12 tests
uv run python research/experiments/music-prefetch-abcmidi/derive_all.py   # 1000 records
uv run python research/experiments/music-prefetch-abcmidi/validate_sample.py \
  music-gk-0000 music-gk-1086 music-gk-0055 music-gk-0036 music-gk-0414 \
  music-gk-0726 music-gk-0342 music-gk-0690 music-gk-0517 music-gk-0619
for t in <same 10>; do uv run evallab tasks variant-status \
  library/task-variants/mimo-v2.6-rl__${t}/*.json validated --by music-prefetch \
  --evidence "research/experiments/music-prefetch-abcmidi/results.jsonl rows task=${t}: ..."; done
```

Per task, `validate_sample.py` runs the real package bytes in the pinned
image: parent setup + grading with the network open (baseline), then the
variant healthcheck command (the re-embedded payload, not the loose file)
with the network open, then `docker network disconnect bridge` and grading
with `eth0` gone. One container at a time, `--rm` via `rm -f`.

## Results (10-task stratified sample, one per category + median Solo)

`results.jsonl` holds 80 rows (10 tasks x 8 steps), every step exit 0.
`fixed-answer.md` is the fixed valid ABC piece (2-bar C-major etude).

| task | category | parent nop (net) | parent fixed (net) | variant nop (locked) | variant fixed (locked) | net at grade |
|---|---|---|---|---|---|---|
| music-gk-0000 | Piano | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-1086 | Chinese traditional | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0055 | Classical forms | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0036 | Dance | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0414 | Ensemble | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0726 | Folk & world | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0342 | Jazz & blues | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0690 | Pop & modern | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0517 | Solo instrument | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |
| music-gk-0619 | Solo instrument (median) | 0.0 | 0.154 | 0.0 | 0.154 | eth0 absent |

MEASURED details:

- Do-nothing grades 0.0 with the grader actually run, under the lock:
  `reward 0.0: The agent wrote no answer.`, exit 0 (`variant-nop-locked`).
- Determinism: the fixed piece scores 0.154 locked on the variant with a
  `result.json` byte-identical (same gate counts, same six group scores) to
  the parent graded with the network — 10/10 tasks.
- No network attempted during grading: `eth0` is absent from
  `/sys/class/net/` at grade time (`variant-net-state`), and grading still
  exits 0 — any `apt-get` attempt would exit 1. The `abc2midi-info` rows show
  `/usr/bin/abc2midi`, `Version: 20250216+ds-1`, `Architecture: arm64` on
  Debian trixie.
- Architecture: arm64 MEASURED above; amd64 SOURCE-QUOTED —
  `https://packages.debian.org/trixie/amd64/abcmidi/download` serves
  `abcmidi_20250216+ds-1_amd64.deb`, the same version string the setup block
  pins, so the unqualified `apt-get install abcmidi=20250216+ds-1` resolves
  on Daytona's amd64 exactly as validated here on arm64. Daytona-amd64
  execution itself was not run (no approval, no spend).

The 10 sampled records are `validated` with per-task evidence; the other 990
are `candidate`. The sample represents them because the pre- and post-change
`setup.sh` bytes are each a single sha256 fleet-wide (MEASURED over all 1,000
records: 1 distinct before, 1 distinct after), the image is shared, and
`pack_setup` is deterministic — every task received the byte-identical
change, differing only in its per-task `task.toml` wrapper.

## Residual limits (not fixed here)

1. **Open semantic problem (investigator finding, accepted): `grade.py`
   scores ABC validity + human-likeness only and ignores the brief.** None of
   the prompt's requirements (key, tempo, meter, bar count, voices,
   ornaments) is verified — the fixed piece above is C major, 2 bars, while
   `music-gk-0000` asks for E-flat major, 65 BPM, 32 bars, 2 voices — yet it
   scores 0.154. Music reward means "sounds human", not "followed the
   instruction". Fixing it needs per-task brief-compliance checks (key/tempo/
   meter from MIDI + bar count), ~2-3 d, deliberately out of scope.
2. Dense reward floor: any trivial valid 2-bar ABC scores 0.154, so "nop = 0"
   holds only for no/empty/invalid answers — RL-relevant, not a hole.
3. The 990 `candidate` records carry no per-task container evidence (by the
   uniformity argument above); re-running the full fleet locally is ~1 h if
   anyone wants it, full-fleet Daytona validation needs compute approval.
4. `source_id` case mismatch (`music-gK-…` vs `music-gk-…`) left alone:
   harmless (joins use content digests; slugs stay distinct), per investigator.
5. No oracle: no reference pieces ship, so 0.154 is a validity anchor, not a
   solve proof. Model/human anchors would need paid calls.

## Files

- `src/evallab/music_prefetch.py`, `tests/test_music_prefetch.py`
- `research/experiments/music-prefetch-abcmidi/` — this README,
  `derive_all.py`, `derive-manifest.json`, `validate_sample.py`,
  `fixed-answer.md`, `results.jsonl` (80 rows)
- `library/task-variants/mimo-v2.6-rl__music-*/` — 1,000 lineage records
  (10 validated, 990 candidate)
