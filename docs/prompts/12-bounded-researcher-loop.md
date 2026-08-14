# Brief 12 — bounded researcher loop and fleet digest

## Contract

`harbor-lab research` runs one analyst → synthesizer → proposer pass over the
prior catalog day. `harbor-lab nightly` invokes the same pass only after the
headless doctor succeeds and the guarded executor drains and ingests completed
work. A queue `STOP` marker prevents the researcher pass as well as dispatch.

Researcher subprocesses use `codex exec` with:

- a Pydantic-derived JSON output schema;
- a read-only, no-network permissions profile over the reviewed bundle;
- user config, apps, hooks, multi-agent tools, and web search disabled;
- a hard rollout-token budget and wall-clock timeout;
- machine-readable events, with any tool-use event rejecting the result;
- one validation retry, then a recorded failure.

The local call ledger reserves an attributed amount before every attempt. It
enforces six calls per role per day (at most three bounded passes including one
retry each), the committed per-job ceiling, and the combined
catalog-plus-researcher daily ceiling.

## Outputs

- `queue/researchers/passes/<date>/<pass-id>/`: evidence snapshot, validated
  analyst/synthesis/proposal sidecars, and a manifest with digests and budget.
- `queue/proposed/`: strict `ExperimentSpec` drafts only. Researchers never
  write `approved/` and never start Harbor or Docker.
- `digests/DISCOVERIES.md`: append-only draft claims with evidence paths and an
  existing-thread reference or a justified new thread.
- `digests/YYYY-MM-DD.md`: an idempotent Fleet section with role handoffs, queue
  funnel, combined spend/attribution, deferrals, and that day's discoveries.

## Acceptance

1. Inject stub invokers and force the analyst's first response to fail schema
   validation. The second attempt succeeds; synthesis and proposal complete.
2. Confirm exactly four ledger reservations, one proposed spec, one discovery,
   valid sidecars, and the Fleet section.
3. Add `queue/STOP`; the next pass must make zero invocations and record
   `stop_file_present`.
4. With credential-aware health on the integration base, run one real pass:

   ```bash
   uv run harbor-lab research --date YYYY-MM-DD
   ```

5. Run `uv run pytest -q` and `uv run ruff check .` before the PR.
