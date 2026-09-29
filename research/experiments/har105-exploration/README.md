# HAR-105 exploration task set

**Purpose (Peter, 2026-09-29 about 22:25Z):** explore the MiMo RL environments with a few runs, find task problems, fix tasks where needed, and rerun them to check they are fixed and still useful. There is no SFT and no RL. Engineering's HAR-104 runs 1 attempt per task with MiMo-V2.6-Flash.

`select.py` is deterministic and writes `selection.json` plus the music nop specs in `specs/`. The pool is the `train` side of the sealed split (`../har81-mimo-sft/split.json`). No task comes from the held-out side.

## Rule

1. **Drop known problems.** Remove tasks with an `error` finding, tasks on the pinned export-broken list, and the part-2 suspects (1634, 1789, 1702, `arvo_18737`, `arvo_57589`). Also remove 2684, whose pass was tainted: it has the same missing-stevedore environment as 1789.
2. **English only.** Of the MiMo tasks, 421 general and 504 music tasks are Chinese. English traces are the ones we can read and audit.
3. **general: drop "final response" tasks.** These are 501 of the 925. The grader reads the agent's final message from an OpenCode event log (`/logs/agent/*.txt`), which Terminus-2 does not write. It falls back to `answer.md`, but only 1 of 925 instructions mentions that file. A Terminus-2 agent therefore cannot deliver the graded answer. Music has the same log fallback, but every music instruction says "Write your complete reply to /app/answer.md", so music is unaffected.
4. **Order.** Rank tasks by `sha256("har105:" + task_id)` and take at most one task per `split_group` and per `metadata.category`.
5. **Nop check.**
   - **code, cyber, terminal:** a task needs an existing `ok` Daytona nop row for its `task_version_digest`. Its nop verifier output must also show no setup or import error. That second condition is HAR-97's proposed rule, and a plain `ok` does not include it.
   - **general, music, webdev:** the top 3 per domain are candidates. The first 2 whose nop comes back clean make the set.

## The set (10 tasks)

| domain | task | category | nop (Daytona) |
|---|---|---|---|
| code | format-code-task-001483 | Python | ok, `mimo-qual-c-format-code-task-001483` (HAR-88) |
| code | format-code-task-000464 | Unknown | ok, `mimo-qual-c-format-code-task-000464` (HAR-88) |
| cyber | arvo_41330 | matio | ok, `har95-qual-y-arvo-41330` (HAR-95) |
| terminal | candidate-2262-operations-compliance | Terminal tasks | ok, `mimo-qual-t-candidate-2262-operations-compliance` (HAR-88) |
| music | music-gk-1037 | Pop & modern | ok, `har105-qual-m-music-gk-1037`: reward 0, "The agent wrote no answer." |
| music | music-gk-0515 | Ensemble | ok, `har105-qual-m-music-gk-0515`: reward 0, same message |
| general | s3k_0036_accounting_audit_tax_en_t2_rl_006 | Accounting, audit & tax | **blocked: judge key** |
| general | s3k_1345_healthcare_ops_en_t3_rl_007 | Healthcare operations | **blocked: judge key** |
| webdev | dasyn_260630_00319 | Other | **blocked: judge key** |
| webdev | dasyn_260638_00006 | Restaurant & food | **blocked: judge key** |

- **Spare.** `music-gk-1087` (Chinese traditional, still English) also nopped `ok`.
- **Nop spend.** The three music nops cost $0.0015 in total, about 21 s each.
- **Naming.** The qualification table records music ids as `music-gK-…`, with an upper-case K taken from the source id. This is the catalog's `mimo-id-case-collision` warning. Join on `task_version_digest`, not on the id.

## general and webdev cannot run through Eval Lab yet

Every general and webdev task grades with a model judge. The key comes from the host in each task's `task.toml` `[verifier.env]`:
- general: `GA_JUDGE_KEY = "${HF_TOKEN}"`, with default model `thinkingmachines/Inkling`;
- webdev: `WEBDEV_JUDGE_KEY = "${HF_TOKEN}"`, with default vision model `meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8`;
- both default to the Hugging Face router, `https://router.huggingface.co/v1`.

Three facts, all read from the code, block both nop and real runs:
1. **Harbor refuses the job at start** when a `${VAR}` without a default is unset (`harbor/cli/jobs.py`, "Missing Environment Variables"). `resolve_env_vars` raises again at verify time.
2. **Eval Lab's executor passes Harbor an allowlisted environment** (`execution_contracts.subscription_environment`). The allowlist has no judge key, so exporting `HF_TOKEN` would not reach Harbor either.
3. **There is no `HF_TOKEN` on this machine.** The Hugging Face snapshots were pulled anonymously.

**Consequences:**
- **Each graded run costs judge money.** The catalog already flags this as `mimo-paid-judge` on all 925 general and 2,093 webdev tasks. The webdev judge samples at temperature 1.0.
- **The key would enter the sandbox.** The verifier runs inside the agent's sandbox, as the catalog's `mimo-verifier-not-isolated` finding records. The general grader prints the first 12 characters of the key and then redacts its own log.
- **The judge can be pointed elsewhere.** `MIMO_TEXT_JUDGE`, `MIMO_VISION_JUDGE` and `MIMO_JUDGE_URL` override the model and endpoint. The key variable name stays `HF_TOKEN`.
- **A nop needs no judge call.** A webdev nop with no `dist/index.html` scores 0 before any judge call. A general nop with a failed gate skips the judge. So a placeholder key would be enough to nop-check both domains, but it still needs a way to pass the variable.

**A local nop is not a substitute.** Local Docker on this Mac failed webdev `dasyn_260630_00319` at the healthcheck with rc=127, before grading (`/private/tmp/har105-local-nop/`). HAR-88 saw the same local rc=127 on `music-gk-0000`, and that task passed on Daytona.

**general has more for HAR-104 to know:**
- the selected tasks declare 6 and 7 MCP servers;
- Terminus-2 gets them only as a list appended to the instruction (`terminus_2.py`, "MCP Servers"), so it must call them over HTTP itself;
- the agent phase runs as user `agent`, with `agent_timeout_sec` 3,600.

## Other notes for HAR-104

- **The cyber pick is unfixed on purpose.** `arvo_41330` carries HAR-97's submit-step gap: none of the 1,000 cyber instructions mention submitting. The whole instruction is three lines: role, tools, and the sanitizer crash line. It runs as an unfixed control next to the fixed `arvo_18737` and `arvo_57589` variants from part 2.
- **Code picks inherit HAR-98.** `tests/test.sh` diffs against git HEAD, which inflates diff sizes on some images. No reward changed in the pilot.
- **Timeouts** come from each task's own `[agent].timeout_sec`: code 3,600 s, cyber 3,600 s, terminal 900 s, music 900 s.
