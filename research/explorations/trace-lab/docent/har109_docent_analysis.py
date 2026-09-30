"""HAR-109 item-5 Docent analysis for the 10 HAR-104 Python runs (PLAN ONLY).

DO NOT RUN until (a) the 10 HAR-104 runs are uploaded to the collection
below, and (b) Peter's hand labels are frozen. Run with one command
(see docent/HAR109_PLAN.md).

Pipeline (Transluce contrastive pattern, adapted: no cross-model pairs here,
so each run is judged against its own transcript plus the blind Eval Lab
metadata, then aggregated):
  1. plan_markdown (Behavior + Measurement) -- required first call.
  2. DQL: list the 10 runs (trial, task, verdict, stop_reason).
  3. Template reading (10 prompts, one per run): first-failure step window
     with quoted evidence, what the model tried, whether the task looks fair,
     harness/model/task attribution with quoted evidence. Blind context:
     only the trace_lab subset {trial, task, reward, stop_reason, verdict,
     domain} is exposed -- no probe-03 or hand-label fields exist.
  4. DQL: aggregate the reading outputs (attribution x verdict/stop counts,
     fairness split, window widths).
  5. Synthesis reading over the 10 per-run results: 5-8 snake_case failure
     modes with run counts.

$0 until executed. Expected use on execution: ~1.3M hosted reading input
tokens + ~12K output (see HAR109_PLAN.md); hosted reading models were free
within weekly limits on 2026-09-29.
"""

from __future__ import annotations

import argparse

from docent import Docent
from docent.data_models.context_config import AgentRunContextConfig
from docent.data_models.metadata_util import GlobFilter

MODEL = "openai/gpt-5.6-luna"

PROMPT = """You are reviewing one agent run: a MiMo model driving a Linux shell through the Terminus-2 tool to fix a Python task. Answer from the transcript only; quote evidence for each answer. Be literal: report what the transcript shows, not intent.

1. FIRST FAILURE (`first_failure_window`: start_step/end_step as 1-based agent-turn ordinals, plus `evidence` quote). The earliest model turn that put the run on its failing path. Give a NARROW window (at most ~5 turns) around it and quote the turn. For passes, window the most error-prone stretch instead and say the run passed.
2. APPROACH (`approach`, free text, 3-6 sentences). What did the model try, in order: reproduction, edits, verification? Name files/commands.
3. TASK FAIRNESS (`task_fair` boolean, `fairness_evidence` quote). False iff the transcript shows the task or its verifier was broken independent of the model (setup/collection/import errors on controls too, impossible instruction, tests that fail without the model's change). A hard-but-solvable task is fair.
4. ATTRIBUTION (`attribution`: model | harness | task_or_grader | unclear, `attribution_evidence` quote). Whose failure best explains the outcome? model: avoidable errors (wrong fix, false completion claim, no adaptation). harness: tooling blocked a capable model (valid commands rejected, lost context, no way to submit). task_or_grader: broken task/verifier per (3). unclear: the transcript does not support any of the above. Passed runs: model iff the fix earned it, else unclear."""

SCHEMA = {
    "type": "object",
    "properties": {
        "first_failure_window": {
            "type": "object",
            "properties": {
                "start_step": {"type": "integer", "minimum": 1},
                "end_step": {"type": "integer", "minimum": 1},
                "evidence": {"type": "string"},
            },
            "required": ["start_step", "end_step", "evidence"],
        },
        "approach": {"type": "string"},
        "task_fair": {"type": "boolean"},
        "fairness_evidence": {"type": "string", "citations": True},
        "attribution": {"type": "string",
                        "enum": ["model", "harness", "task_or_grader", "unclear"]},
        "attribution_evidence": {"type": "string", "citations": True},
    },
    "required": ["first_failure_window", "approach", "task_fair", "fairness_evidence",
                 "attribution", "attribution_evidence"],
}

SYNTH_PROMPT = """Group these 10 per-run analyses into 5-8 snake_case failure modes (e.g. `false_completion_claim`, `unreproduced_before_edit`). For each mode: run count, one-line decision rule a second judge could follow, and the strongest quoted evidence. Passed runs group under `passed_clean` or a mode only with quoted justification."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--collection", required=True, help="HAR-104 collection UUID")
    args = parser.parse_args(argv)

    client = Docent()
    client.plan_name = "har109-har104-python-analysis"

    client.plan_markdown(
        "HAR-104 Python runs: first failure, approach, fairness, attribution",
        """## Behavior
Each of the 10 runs is one MiMo attempt at a Python fix task. We want, per run: the step window where it went wrong (or most error-prone), what the model tried, whether the task itself looks fair, and who owns the outcome (model, harness, or task/grader).

## Measurement
One structured reading per run with quoted evidence for every claim, over the transcript plus only the blind Eval Lab metadata (trial, task, reward, stop reason, verdict, domain). A DQL step then aggregates the 10 outputs (attribution by verdict/stop, fairness split, window widths), and a final synthesis reading groups runs into 5-8 named failure modes. Scores against frozen hand labels use docent/har109_scoring.md (step windows, never exact steps; deterministic rules outrank readings).""",
    )

    runs = client.query(
        args.collection,
        """SELECT id AS run, metadata_json->'trace_lab'->>'trial' AS trial
FROM agent_runs ORDER BY trial""",
        name="All 10 HAR-104 runs",
    )

    per_run = client.read(
        prompt_template=["Review this run. Blind metadata (trial, task, reward, stop, verdict, domain) is included; cite transcript evidence.\n\n" + PROMPT,
                         runs.run.as_type("agent_run")],
        context_configs={
            "run": AgentRunContextConfig(
                agent_run_metadata=GlobFilter(include=("trace_lab.trial", "trace_lab.task",
                                                       "trace_lab.reward", "trace_lab.stop_reason",
                                                       "trace_lab.verdict", "trace_lab.domain")),
            ),
        },
        model=MODEL,
        output_schema=SCHEMA,
        max_new_tokens=1500,
        name="Per-run failure, fairness, attribution",
    )

    client.query(
        args.collection,
        f"""SELECT attribution, verdict, stop_reason, COUNT(result_id) AS run_count,
       ROUND(CAST(AVG(window_width) AS NUMERIC), 1) AS mean_window_width
FROM (
  SELECT
    rr.output->>'attribution' AS attribution,
    ar.metadata_json->'trace_lab'->>'verdict' AS verdict,
    ar.metadata_json->'trace_lab'->>'stop_reason' AS stop_reason,
    (CAST(rr.output->'first_failure_window'->>'end_step' AS INTEGER)
     - CAST(rr.output->'first_failure_window'->>'start_step' AS INTEGER) + 1) AS window_width,
    rr.id AS result_id
  FROM reading_results rr
  JOIN reading_result_links rrl ON rrl.result_id = rr.id
  JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
  WHERE rrl.reading_id = '{per_run.id}'
    AND rr.output IS NOT NULL AND (rr.error IS NULL OR rr.error::text = 'null')
) AS subq
GROUP BY attribution, verdict, stop_reason
ORDER BY run_count DESC""",
        name="Attribution aggregate",
    )

    agg = client.query(
        args.collection,
        f"""SELECT array_agg(result_id ORDER BY result_id) AS results FROM (
SELECT rr.id AS result_id FROM reading_results rr
JOIN reading_result_links rrl ON rrl.result_id = rr.id
WHERE rrl.reading_id = '{per_run.id}') AS s""",
        name="Per-run results for synthesis",
    )
    client.read(
        prompt_template=[SYNTH_PROMPT, agg.results.as_type("reading_result", is_list=True)],
        model=MODEL,
        max_new_tokens=2500,
        name="Failure-mode synthesis",
    )
    print("plan steps registered; blocking for reading results...")
    results = per_run.results
    print(f"per-run reading {per_run.id}: {len(results)} results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
