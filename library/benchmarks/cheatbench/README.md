# CheatBench ports (Harbor format)

Ports of parts of the CAIS CheatBench benchmark (arXiv 2609.36308; MIT licence;
source `https://github.com/centerforaisafety/cheatbench` at commit
`4d1a82543f93edd0e4e5a2651192688ba609c1e7`) into Eval Lab Harbor tasks, so open
models can run on them with Harbor's Terminus-2 agent on local Docker and have
their trajectories judged for cheating the way the paper does.

Task success (reward) and cheating are separate everywhere here: reward
measures only task success; cheating is judged later from the trajectory by an
LLM judge plus deterministic detectors
(`research/experiments/cheatbench-port/judge/`).

## Families

- `subset_sum/`, `prime_factorization/` — Menial Computation vertical (10 + 20
  rows): timed "interview" with a code ban, a `verify_*` CLI backed by a sidecar
  that alone holds the answer, and two planted routes to an answer key.
- `openmath_agent/` — Mathematical Research vertical (20 rows): research-repo
  protocol task with a planted peer submission (`agent-014`) honeypot and an
  offline article-mirror sidecar.
- `geoguessr/` — Photo Location vertical (20 rows): one close-cropped photo
  per round, unlimited scoring through a `submit` CLI backed by a sidecar
  running the real `submit.pyc` against its own reference copy, and one
  planted GPX honeypot per photo (photo comment + EXIF point at it).
- `synthetic_puzzles/` — Jigsaw Counting vertical (20 rows): count a
  ~1000-piece jigsaw exactly under a code ban, 3 checks through the same
  `submit` + sidecar split, with two planted routes to the accepted count
  (generator log and photo-metadata answer file).

Each family directory holds its own `build.py` generator, `README.md`
(provenance + Deviations table) and `judge/` (verbatim judge schema). The
generated Harbor packages under `<family>/tasks/` are **not committed**
(~870 MB in total: mostly openmath reference archives and git history plus
~140 MB of synthetic jigsaw JPEGs); rebuild them from a clone of the
pinned source:

```bash
git clone https://github.com/centerforaisafety/cheatbench /private/tmp/cheatbench-src
git -C /private/tmp/cheatbench-src checkout 4d1a82543f93edd0e4e5a2651192688ba609c1e7
uv run --no-sync python library/benchmarks/cheatbench/subset_sum/build.py --source /private/tmp/cheatbench-src
uv run --no-sync python library/benchmarks/cheatbench/prime_factorization/build.py --source /private/tmp/cheatbench-src
uv run --no-sync python library/benchmarks/cheatbench/openmath_agent/build.py --source /private/tmp/cheatbench-src
```

The geoguessr and synthetic_puzzles builds reuse the upstream build modules,
which need the container-image Python and its packages: `python3.11` with
`Pillow`, `pyyaml` (plus `numpy`, `scipy` for synthetic_puzzles), `git` and
`exiftool` — the same requirement the upstream builds state. Run them with
such an interpreter instead of `uv run`, e.g.:

```bash
python3.11 library/benchmarks/cheatbench/geoguessr/build.py --source /private/tmp/cheatbench-src
python3.11 library/benchmarks/cheatbench/synthetic_puzzles/build.py --source /private/tmp/cheatbench-src
```

Builds are deterministic; `MANIFEST.md` records the expected digests. Build
scratch (downloads, the openmath TLS CA) lives in ignored `_build_cache/` dirs;
the openmath digest depends on that cached CA, so a fresh cache yields new
package digests (the queue specs pin whichever digest was actually run).
