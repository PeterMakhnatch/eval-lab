# Terminal-Bench 2.0 sample @ Harbor Hub 2.0

## Source and pin

- **Lane:** `harbor download terminal-bench-sample@2.0`
- **Never:** `@latest`
- **Task git:** `https://github.com/laude-institute/terminal-bench-2-0-sample` @ `7e917f35c281188532772312d4ad91ca9274febc`
- **Family:** Terminal-Bench 2.0 public sample (10 tasks). Full TB2 Hub pin is `terminal-bench@2.0` (89 tasks @ `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`); not copied here.
- **On-disk:** `library/benchmarks/terminal-bench-sample/terminal-bench-sample/`

## License

Apache-2.0 (Terminal-Bench repos). Tasks carry the TB canary GUID.

## Counts / subset

- **Full pin:** 10 tasks
- **Materialized:** full 10 (~12 MB; `sqlite-with-gcov` is a 12 MB source tarball)
- **Verified sample (4):** `regex-log`, `log-summary-date-ranges`, `chess-best-move`, `fix-code-vulnerability`

## Not verified (and why)

| Task | Reason |
| --- | --- |
| `qemu-alpine-ssh`, `qemu-startup` | Nested VM; not a laptop canary |
| `polyglot-c-py`, `build-cython-ext` | Imported non-Python toolchains; AGENTS.md asks before new languages in imported tasks |
| `sqlite-with-gcov` | Heavy compile + 12 MB tarball; skip for the 3–5 sample |
| `configure-git-webserver` | Extra sysadmin task; four CPU tasks already fill the sample |

## Lane / resources

CPU Docker; official images `ghcr.io/laude-institute/terminal-bench/<task>:2.0`. No GPU. QEMU tasks would need nested virtualization — not run.

## Sample verification (`-n` ≤ 2)

Harbor 0.21.0; `-k 1 -n 2`; jobs in this worktree `./runs/`.

| Task | Oracle job | Oracle | Nop job | Nop |
| --- | --- | --- | --- | --- |
| regex-log | `oracle-ingest-tbs-regex-log` | **1.0** | `nop-ingest-tbs-regex-log` | **0.0** |
| log-summary-date-ranges | `oracle-ingest-tbs-log-summary-date-ranges` | **1.0** | `nop-ingest-tbs-log-summary-date-ranges` | **0.0** |
| chess-best-move | `oracle-ingest-tbs-chess-best-move` | **1.0** | `nop-ingest-tbs-chess-best-move` | **0.0** |
| fix-code-vulnerability | `oracle-ingest-tbs-fix-code-vulnerability` | **1.0** | `nop-ingest-tbs-fix-code-vulnerability` | **0.0** |
