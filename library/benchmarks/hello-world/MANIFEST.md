# hello-world@1.0 @ Harbor Hub 1.0

## Source and pin

- **Lane:** `harbor download hello-world@1.0`
- **Never:** `@latest`
- **Task git:** export (no `.git` after Harbor download)
- **Harbor sync digest:** `sha256:cff230d09ea952d092daf99796d0c52ec5bfb92d86f13021af902ce7b6b36720`
- **On-disk:** `library/benchmarks/hello-world/` (hello-world/ …)

## License

See upstream Harbor dataset card; lab-internal eval use.

## Counts / subset

- **Full pin:** 1 tasks
- **Materialized:** full 1
- **Verified sample:** hello-world

## Lane / resources

- CPU Harbor Docker task images unless a task.toml says otherwise
- No GPU assumed; skip cloud-only content
- Lane: hub

## Sample verification (`-n` ≤ 2)

Harbor `-k 1 -n 2`; jobs under this worktree `./runs/`.

| Task | Oracle job | Oracle | Nop job | Nop |
| --- | --- | --- | --- | --- |
| hello-world | `oracle-fetch-hello-world-hello-world` | **1.0** | `nop-fetch-hello-world-hello-world` | **0.0** |

