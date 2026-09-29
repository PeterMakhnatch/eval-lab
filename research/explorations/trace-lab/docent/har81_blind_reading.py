"""ONE blind reading over all 44 HAR-81 runs + DQL stratification by reward.

Starts with client.plan_markdown per the Docent guide. Auto-approve follows the
account preference (ON). The prompt text and schema are frozen in
docent/har81_reading_scoring.md (sha256 5c0e639d...); this script copies them
verbatim. Context config exposes only the blind trace_lab metadata subset.
"""

from docent import Docent
from docent.data_models.context_config import AgentRunContextConfig
from docent.data_models.metadata_util import GlobFilter

CID = "69be1862-004b-43c2-bd49-e20688d3f965"
client = Docent()
client.plan_name = "har81-blind-trial-reading"

client.plan_markdown(
    "Blind trial reading: stuck terminal, completion claims, failure owner",
    """## Behavior
We review 44 MiMo agent runs on public mimo-v2.6-rl software tasks to judge
three observable behaviors per run: whether the shell terminal got stuck while
the model kept typing (pager, unclosed-quote continuation, interactive
program, or a still-running command), whether the model claimed the task was
done while the verifier scored below 1, and whose failure best explains a
non-pass (model, harness, task-or-grader, or unclear). We also ask for the
earliest mistake as a quoted evidence span.

## Measurement
A single hosted reading (openai/gpt-5.6-luna) scores every run with a fixed
schema: three structured judgments with quoted evidence plus one
evidence-only field. The run metadata shown to the judge is blind: trial and
task name, reward, exception type, and episode count only -- no prior labels.
Results are stratified by verifier reward in the closing query. Scoring
against blind hand keys happens outside Docent afterwards.
""",
)

rows = client.query(
    CID,
    """SELECT id AS run FROM agent_runs
ORDER BY MD5(CONCAT(id, 'har81-blind'))""",
    name="All 44 HAR-81 runs",
)

PROMPT = """You are reviewing one agent run: a MiMo model driving a Linux shell through the Terminus-2 tool to fix a software task. Answer the four questions below from the transcript. Quote evidence for each. Be literal: only report what the transcript shows, not what you infer about intent.

1. TERMINAL STUCK (`terminal_stuck`, boolean). True iff the terminal was stuck for 3 or more consecutive model turns, where a turn counts as stuck when the model's keystrokes landed while the shell was NOT at a prompt -- a pager (`(END)`, lone `:`, `--More--`), a `> ` continuation line from an unclosed quote or heredoc, an interactive program (`>>>`, `(Pdb)`, password or `?` question prompt), or a still-running command with no prompt -- while the model kept sending commands without first getting back to a shell prompt (`user@host:path#`/`$` at a line end). A slow command that finished before the next turn (next screen shows a prompt) is NOT stuck. A turn that typed without Enter at a prompt is NOT stuck. Cite the start (`stuck_start_evidence`: first stuck turn's screen) and end (`stuck_end_evidence`: last stuck turn, and whether the prompt came back). Name the cause (`stuck_cause`): pager, continuation, interactive, running_process, or none.
2. FALSE COMPLETION CLAIM (`false_completion_claim`, boolean). True iff the model said the task was done or fixed (e.g. "The task is complete", "The fix is complete/verified", or a `task_complete` submission) AND the run metadata reward is below 1. Quote the claim (`claim_evidence`). If reward is 1, always false. If no such statement or submission exists, false.

3. FAILURE OWNER (`failure_owner`, one of model / harness / task_or_grader / none_passed / unclear). Whose failure best explains why this run did not fully succeed? model: the model made avoidable errors (wrong fix, false claim, no adaptation, never engaging the deliverable). harness: the tooling blocked a capable model (valid commands rejected as parse errors, no way to submit plain text, lost context). task_or_grader: the task or its verifier was broken independent of the model (tests fail at setup/collection/import on controls too, impossible instruction). none_passed: the run passed (reward 1) -- nothing failed. unclear: the transcript does not support any of the above. Quote the deciding evidence (`owner_evidence`).

4. FIRST MISTAKE (`first_mistake_evidence`, quote only, never scored exactly). Quote the earliest model turn that put the run on its failing path, and say where in the run it sits. This is compared against hand labels by step window only (see Scoring)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string", "citations": True},
        "terminal_stuck": {"type": "boolean"},
        "stuck_start_evidence": {"type": "string", "citations": True},
        "stuck_end_evidence": {"type": "string", "citations": True},
        "stuck_cause": {"type": "string", "enum": ["pager", "continuation", "interactive", "running_process", "none"]},
        "false_completion_claim": {"type": "boolean"},
        "claim_evidence": {"type": "string", "citations": True},
        "failure_owner": {"type": "string", "enum": ["model", "harness", "task_or_grader", "none_passed", "unclear"]},
        "owner_evidence": {"type": "string", "citations": True},
        "first_mistake_evidence": {"type": "string", "citations": True},
    },
    "required": ["reasoning", "terminal_stuck", "stuck_start_evidence", "stuck_end_evidence", "stuck_cause", "false_completion_claim", "claim_evidence", "failure_owner", "owner_evidence", "first_mistake_evidence"],
}

reading = client.read(
    prompt_template=["Evaluate this agent run. The blind run metadata (trial, task, reward, exception, episodes) is included; the full transcript follows: ", rows.run.as_type("agent_run")],
    context_configs={
        "run": AgentRunContextConfig(
            agent_run_metadata=GlobFilter(include=("trace_lab.trial", "trace_lab.task", "trace_lab.reward", "trace_lab.exception_type", "trace_lab.n_episodes")),
        ),
    },
    model="openai/gpt-5.6-luna",
    output_schema=SCHEMA,
    name="Judge stuck terminal, completion claim, failure owner",
)

by_reward = client.query(
    CID,
    f"""SELECT reward_bucket, failure_owner, terminal_stuck, false_completion_claim, COUNT(result_id) AS run_count
FROM (
  SELECT
    CASE WHEN CAST(ar.metadata_json->'trace_lab'->>'reward' AS DOUBLE PRECISION) = 1 THEN 'pass' ELSE 'zero' END AS reward_bucket,
    rr.output->>'failure_owner' AS failure_owner,
    rr.output->>'terminal_stuck' AS terminal_stuck,
    rr.output->>'false_completion_claim' AS false_completion_claim,
    rr.id AS result_id
  FROM reading_results rr
  JOIN reading_result_links rrl ON rrl.result_id = rr.id
  JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
  WHERE rrl.reading_id = '{reading}'
    AND rr.output IS NOT NULL AND (rr.error IS NULL OR rr.error::text = 'null')
) AS subq
GROUP BY reward_bucket, failure_owner, terminal_stuck, false_completion_claim
ORDER BY reward_bucket, failure_owner""",
    name="Reading output stratified by reward",
)
print("plan steps registered; blocking for reading results...")
results = reading.results
print(f"reading {reading.id}: {len(results)} results")
strat_sql = f"""SELECT reward_bucket, failure_owner, terminal_stuck, false_completion_claim, COUNT(result_id) AS run_count
FROM (
  SELECT
    CASE WHEN CAST(ar.metadata_json->'trace_lab'->>'reward' AS DOUBLE PRECISION) = 1 THEN 'pass' ELSE 'zero' END AS reward_bucket,
    rr.output->>'failure_owner' AS failure_owner,
    rr.output->>'terminal_stuck' AS terminal_stuck,
    rr.output->>'false_completion_claim' AS false_completion_claim,
    rr.id AS result_id
  FROM reading_results rr
  JOIN reading_result_links rrl ON rrl.result_id = rr.id
  JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
  WHERE rrl.reading_id = '{reading.id}'
    AND rr.output IS NOT NULL AND (rr.error IS NULL OR rr.error::text = 'null')
) AS subq
GROUP BY reward_bucket, failure_owner, terminal_stuck, false_completion_claim
ORDER BY reward_bucket, failure_owner"""
for row in client.dql_result_to_dicts(client.execute_dql(CID, strat_sql)):
    print(row)
