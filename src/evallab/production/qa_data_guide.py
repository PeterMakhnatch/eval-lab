"""The text of ``evallab qa-data --help``: what an agent with no other documentation needs.

Kept apart from ``qa_data.py`` so the parser stays readable. Everything here is a command, a path or a
trap that was needed by a fresh agent in the LB-10 doc test; it is not a second copy of the code.
"""

from __future__ import annotations

GUIDE = """\
Run from the root of an Eval Lab checkout or worktree (the split ledger and `evallab` resolve from there):

    cd ~/Developer/eval-lab/.worktrees/<worktree>
    export EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONWARNINGS=ignore UV_OFFLINE=1 UV_NO_SYNC=1
    uv run evallab qa-data <call> ...            # first call builds the venv if needed; scratch: /private/tmp/<you>/

Every call that reads the store (all but split, steps and scan, which need none) opens it READ-ONLY, admits DEV
tasks and runs through the registry gate, and prints ONE JSON document {"ok", "data" | "error", "meta"}. Exit 0 = ok.
Exit 2 = refused or failed: read "error" (a usage mistake prints argparse text on stderr, also exit 2). A task or run that is missing, TEST, held out, sealed or locked under another key gets the SAME error,
"not available: not an admitted DEV task or run": never retry with variants to find out which. --budget N caps the
reply at about N tokens (default 4000, minimum 200; `task` replies often need --budget 8000); lists drop whole items
and say how many (meta.omitted_items counts items not returned, whether the budget or --limit cut them; `truncated`
is then true; `matched` is the full count). File viewers may clip long lines: add --pretty (indented) to any call; the budget
and tokens_est describe the compact form, so pretty text is about 30 percent larger. The database's own
split='dev' is NOT admission; this tool applies the strict cross-key check.
No call makes a model call, runs a grader, writes the store or the repository.

IDS
  run key          trace_id  (the `run_id` field of facet records is the same value; agent_traces.run_id is a batch id)
  task ids         cloud ids are namespaced (terminal-bench/cad-model); cohort and ledger files use bare names
                   (cad-model, django__django-11179). Every call taking a task accepts either: an exact id wins; a bare
                   name (no "/" or "@") falls back to the one admitted task with that name; several match -> a few
                   admitted candidates are listed. A qualified id ("ns/x", "x@v") must match exactly. Hand-written SQL with the wrong form
                   silently returns 0 rows.
  step ids         facet `evidence_steps` use the ORIGINAL step ids; an export renumbers steps 1..n. Never add 1:
                   use `export`'s printed map or `steps`.
  cluster ids      meaningless without their build id (a later build can reuse L1-07 for other members).

CALLS (all JSON; examples use real flags)
  inventory                          admitted tasks/runs by dataset, outcomes, top models
       inventory --ids-out /private/tmp/me/ids.txt        also write every admitted trace id (file must not exist)
  traces [--task T] [--dataset D] [--model M] [--outcome pass|fail|unknown] [--limit N] [--ids-only]
       list admitted runs, faceted or not (task/dataset/model are shown; never hand these records to blind readers)
       traces --task cad-model              -> matched, runs of terminal-bench/cad-model (matched: 0 says none exist)
  runs [--facet K=V[,V] ...] [--cluster ID] [--like TRACE_ID] [--query WORDS] [--task T] [--limit N]
       search FACETED runs. Records carry no dataset, split, source or model field, but task_id and run_id strings
       still name their dataset: for a blind reader use `export` files, never these records. Facet fields: outcome
       (pass|fail|unknown), tests (untouched|read|modified|deleted|replaced), eval_awareness (none|mentions_tests|
       reasons_about_grader|plans_to_game), grader_or_harness_touched / answer_material_accessed /
       success_claim_without_evidence (true|false), suspicion (0-3). A comma list is OR inside one field; separate
       --facet flags are ANDed (an OR across different fields needs two queries); "suspicion at least 2" is
       --facet suspicion=2,3.
       runs --facet answer_material_accessed=true          most suspicious first; `matched` is the full count
       (--cluster uses the STORED db clusters, `clusters` default view; ids from `clusters --source file` can differ)
       runs --task cad-model --facet tests=modified,deleted,replaced
  window RUN_ID [--first N --last M | --around N --radius R]    steps of one run, original step ids
  clusters [--source db|file] [--level 1|2|3] [--members CLUSTER_ID]
       db: stored clusters with admitted sizes (no cluster names: they can reflect hidden runs).
       file: the latest clusters-v1.jsonl build, members split into cloud runs and registry-only DEV
       items (CheatBench); every output carries its build id.
       clusters --source file --level 1                   largest first (admitted_members), with the build id
       clusters --source file --members L1-02   cloud_run_ids, registry_only_dev_ids and their counts
       Equal sizes tie (report all tied clusters). To count a facet over a cluster, intersect cloud_run_ids with
       `runs --facet ... --limit 200` ids (or use `export`/`window` on single runs).
  recount                            facets: interim JSONL vs the database over admitted runs (sha256, counts). It counts
       admitted TASKS' facets (1,235 in the database); `runs`/`export --facet` also screen sealed text (1,234).
  findings [--run TRACE_ID | --task T] [--limit N]
       trace-tool rows (Scout, Scout-METR, Docent, Laminar, OMP). With no flag: per-tool rows, runs, admitted runs
       (aggregates: no detector fields). A null flag is not a negative; with --run or --task each row keeps its tool
       and detector: quote both beside any number.
  history TASK_ID [--flag F ...]     past confirmed issues, similar examples, playbook cards for one task
  task TASK_ID [--file REL ...] [--variant N (default 0)] [--kind original_package|repaired_package (default original)]
       one admitted task (without --file it lists, it does not dump). Fields: instruction_chars (+ instruction text,
       instruction_truncated); packages{"<variant>:<kind>"}: file_count (INCLUDES withheld files), files[{relative_path,
       size}], withheld[] (solution/ and scoring-key names: listed, never read), contents[] (only with --file);
       issues[] + issues_total (retained issues, no reproducers; status/trust_tier as stored: provisional and
       tier3_provisional are the lowest-trust tiers); runs{total, faceted, suspicion_ge_2, list[]}. A file that is
       not in the chosen package says so: check the other --kind. `task vpp-loss-divergence --file tests/test.sh`
       returns that file's text. Never run package code from here.
  split task TASK_ID | split item KEY_ID ITEM_ID
       data is {"split": "DEV"|"TEST"|"UNKNOWN"} and nothing else. TEST = TEST under any key or sealed. UNKNOWN never
       authorizes access. DEV still needs admission before run content is read. Needs no store.
       split task 3d-scan-calc                            DEV
       split item breaker-bench-v1 breaker-bench-v1:benchflow-0.2.0:terminal-bench-2/caffe-cifar-10    TEST
  export (--facet K=V[,V] ... | --trace-id ID ... | --ids-file FILE) [--limit N] [--format atif|harbor|scout] --out DIR
       blinded export of admitted runs. DIR and DIR.sealed-manifest.json (written beside it) must not exist. Any
       non-admitted id fails the whole call and exports nothing. Prints a receipt (admitted, exported, refused) and, per
       run, trace_id, blind_id, actor_inputs_available (false = no model actor behind the run, e.g. replay receipts
       and event journals: unknown, never clean) and the file; for atif also steps, evidence_steps_original and evidence_steps_blinded
       (same steps in the export's numbering; null = not exported). harbor and scout give no step map.
       atif: DIR/run-<32hex>.json  (jq '.steps[] | select(.step_id==3)': keys step_id, source, message, observation)
       harbor: DIR/run-<hex>/agent/trajectory.json + result.json (reward null)   scout: DIR/transcripts.jsonl
       Hand readers only the exported files. The sealed manifest holds trace ids (they name the dataset) and rewards.
       export --facet answer_material_accessed=true --limit 5 --out /private/tmp/me/ex1
       export any runs: pick ids with `traces --dataset D --limit 3 --ids-only`, then --trace-id each (or --ids-file).
  steps MANIFEST TRACE [--original N | --blinded N]
       trace id or blind id -> {blind_id, steps}; --original N = exported step number of original step N;
       --blinded N = original id of exported step N. Never prints a reward.
  scan TRANSCRIPTS_JSONL [--scanner NAME ...]
       the four deterministic Scout text scanners over an `export --format scout` file (repeated_assistant_message,
       harness_parse_errors, native_tool_call_markup, restored_tool_calls): per blind id messages, results, values,
       references (values: one number per scanner result, 0 = nothing found; references: count of cited messages);
       model_calls is always 0. Runs under the scout-watch runtime interpreter when this venv lacks Scout.

OBSERVED ON THE LIVE STORE, 2026-10-28 (counts move while lanes run: quote the date or `recount` digest)
  3,474 admitted tasks, 4,544 admitted runs (pass 2,033, fail 1,297, unknown 1,214). 1,234 admitted faceted runs;
  answer_material_accessed true on 29; suspicion 2 or 3 on 326 (327 in `recount`, see above). Facets are leads.

OUTSIDE qa-data (plain files; no admission needed; run from the checkout root unless a path says otherwise)
  RA=~/Developer/research-context/bridge/2026-10-27-run-atlas
  Reward Seeker cohort (SB-1; bare task names):
    jq '{candidate_count,ledger_positive_count,qualified_count,target,target_met,by_dataset}' \\
       research/experiments/sb1-clean-seeker-set-20261027/census.json
    jq -r '.tasks[] | [.dataset_id,.task_id,.original_task,.repaired_grader_kind] | @tsv' \\
       research/experiments/sb1-clean-seeker-set-20261027/cohort.json
    uv run evallab prod seeker-error-first --out /private/tmp/me/plan --plan     free, no model call. It EXITS 1
       today (cohort digest prefix mismatch): that is a failure, not a success; its cell/grader-run/dollar numbers
       are budget arithmetic, not results. Do not run it without --plan.
  Cross-test registry (metadata only; local-only, no training, no rehosting, never copy into the store):
    python3 $RA/validate_cross_test_registry.py     prints "N error(s), M warning(s)" and exits 1 on any error. A staged
       bundle that carries CROSS_TEST_ONLY.md but is not in the registry is an ERROR (other lanes stage new sets: the
       count changes over time; report the lines you saw and the date)
    jq -r '.entries[] | [.id,.unit,.flags.hosted_model_api_for_evaluation] | @tsv' $RA/cross-test-registry.json
    jq -r '.entries[] | select(.scoring.command_level_localization_scorable) | .id' $RA/cross-test-registry.json
    jq -r '.hold_access[].id' $RA/cross-test-registry.json           held sets: need Peter, not available
    uv run python research/evidence/runs/corpus-expansion-lb6-20261027/tools/hackbench_adapter.py selfcheck \\
       | jq '{rows,commands,issues:(.issues|length)}'                 counts only (about 20 s)
  Answer-key census without labels (safe columns only; never read expected_verdict or exposures):
    python3 -c "import csv,collections as C;r=csv.DictReader(open('research/answer-keys/splits/assignments.csv'));\\
    print(C.Counter(x['split'] for x in r))"
  Task QA keyed defects (the keys are for the scorer after predictions are frozen; never open for discovery):
    ~/Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun/ (manifest-dev.json maps cases to tasks; its key_count
    fields are null; scorer-only-keys/ holds the keys). The aggregate counts (tasks, keyed, annotations, unkeyed) and
    the key rules are stated in the protocol prose, which you may read: $RA/oq6-task-quality-gate-rerun.md. The
    399-task DEV set is the one on line 20 of that file (the 15-task cross cohort is separate).
  Is a lane's claim committed?  git -C ~/Developer/research-context status --porcelain -- bridge/<file>.md
  Safe store snapshot: sqlite3 'file:<store>?mode=ro' ".backup /private/tmp/me/copy.sqlite" (never copy a live WAL file)

NEVER: `CloudTraceDatabase(path)` on the live store without read_only=True (it runs a write transaction);
`evallab prod status` is read-only but is for queue databases only: do not aim it at the cloud store;
`atlas facet` without --dry-run, `atlas clusters build`, `atlas watch`, `prod run-once|serve|ingest`, `evallab trace`
without --dry-run (model calls or writes); put labels, rewards, trace ids, dataset names or split names in a blind
reader's input; read TEST or cross-test labels.
"""
