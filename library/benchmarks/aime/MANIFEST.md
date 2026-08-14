# AIME @ Harbor Hub 1.0

## Source and pin

- **Lane:** Harbor Hub / legacy registry `aime@1.0` (`harbor download aime@1.0`)
- **Never:** `@latest`
- **Task git:** `https://github.com/laude-institute/harbor-datasets.git` @ `414014c23ce4d32128073d12b057252c918cccf4`
- **Upstream problems:** [GAIR-NLP/AIME-Preview](https://github.com/GAIR-NLP/AIME-Preview) (AIME 2024, 2025-I, 2025-II)
- **Adapter (unused this pass):** `harbor/adapters/aime` (same 60-item scope)
- **On-disk:** `library/benchmarks/aime/aime/` (60 tasks: `aime_60` …)

## License

MAA contest problems — **eval use inside this private lab only**; do not republish problem text. Harbor packaging follows the Hub dataset.

## Counts / subset

- **Full pin:** 60 tasks
- **Materialized:** full 60 (1.4 MB)
- **Verified sample:** `aime_60`–`aime_64` (5 tasks), free oracle + nop

## Lane / resources

- CPU-only Ubuntu/Python; integer written to `/app/answer.txt`
- No GPU, no cloud

## Sample verification (`-n` ≤ 2)

Jobs under `.worktrees/ingest/runs/`:

| Task | Oracle job | Oracle | Nop job | Nop |
| --- | --- | --- | --- | --- |
| aime_60 | `oracle-ingest-aime-60` | **1.0** | `nop-ingest-aime-60` | **0.0** |
| aime_61 | `oracle-ingest-aime-61` | **1.0** | `nop-ingest-aime-61` | **0.0** |
| aime_62 | `oracle-ingest-aime-62` | **1.0** | `nop-ingest-aime-62` | **0.0** |
| aime_63 | `oracle-ingest-aime-63` | **1.0** | `nop-ingest-aime-63` | **0.0** |
| aime_64 | `oracle-ingest-aime-64` | **1.0** | `nop-ingest-aime-64` | **0.0** |

Harbor 0.21.0; `-k 1 -n 2`; jobs in this worktree `./runs/`. Oracle writes the official integer to `/app/answer.txt`; nop leaves it empty. Extra job `oracle-ingest-aime` also scored aime_64 at 1.0 (first multi-`-p` attempt only ran the last path).
