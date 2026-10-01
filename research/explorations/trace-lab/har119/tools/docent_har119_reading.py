"""HAR-119 blind Docent reading: SAME frozen prompt/schema/model as HAR-81.

PROMPT and SCHEMA are extracted verbatim from
research/explorations/trace-lab/docent/har81_blind_reading.py via AST (the
module is never imported: importing it would re-submit the HAR-81 plan).
Model is openai/gpt-5.6-luna; context config exposes only the blind
trace_lab metadata subset. One reading over the 12 HAR-119 runs, then per-run
outputs are fetched via DQL into predictions/docent/raw/ and mapped to
predictions/docent.jsonl per ../predictions/MAPPING.md (frozen).

Usage: keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
  python research/explorations/trace-lab/har119/tools/docent_har119_reading.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]
HAR119 = HERE.parent.parent
PRED = HAR119 / "predictions"

CID = "db32cc8f-e610-4373-a8dd-89b697a87a60"
MODEL = "openai/gpt-5.6-luna"

HAR81_SCRIPT = (
    WORKTREE / "research" / "explorations" / "trace-lab" / "docent" / "har81_blind_reading.py"
)


def frozen_prompt_schema() -> tuple[str, dict]:
    tree = ast.parse(HAR81_SCRIPT.read_text())
    prompt = schema = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", "")
            if name == "PROMPT":
                prompt = ast.literal_eval(node.value)
            elif name == "SCHEMA":
                schema = ast.literal_eval(node.value)
    assert isinstance(prompt, str) and isinstance(schema, dict), "frozen text missing"
    return prompt, schema


def map_blame(owner: str | None) -> str | None:
    return {
        "model": "model",
        "harness": "harness",
        "task_or_grader": "task",
        "none_passed": "none",
    }.get(owner or "")  # unclear or other: not expressed


def main() -> int:
    from docent import Docent
    from docent.data_models.context_config import AgentRunContextConfig
    from docent.data_models.metadata_util import GlobFilter

    prompt, schema = frozen_prompt_schema()
    assert "TERMINAL STUCK" in prompt and "FIRST MISTAKE" in prompt
    assert set(schema["required"]) == {
        "reasoning",
        "terminal_stuck",
        "stuck_start_evidence",
        "stuck_end_evidence",
        "stuck_cause",
        "false_completion_claim",
        "claim_evidence",
        "failure_owner",
        "owner_evidence",
        "first_mistake_evidence",
    }

    sel = json.loads((HAR119 / "selection.json").read_text())
    trials: list[str] = [r["trial"] for r in sel["runs"]]

    client = Docent()
    client.plan_name = "har119-blind-trial-reading"
    client.plan_markdown(
        "Blind trial reading (HAR-119): stuck terminal, completion claims, failure owner",
        """## Behavior
We review 12 blind agent runs (MiMo driving Terminus-2) and extract, per run, whether the terminal was stuck, whether a false completion claim was made, and who owns the failure.

## Measurement
One frozen LLM reading per run (same prompt/schema/model as HAR-81) with transcript citations, stratified by reward in DQL. No probe-03 tags, no hand labels in metadata.
""",
    )
    rows = client.query(
        CID,
        """SELECT id AS run FROM agent_runs
ORDER BY MD5(CONCAT(id, 'har119-blind'))""",
        name="All 12 HAR-119 runs",
    )
    reading = client.read(
        prompt_template=[
            "Evaluate this agent run. The blind run metadata (trial, task, reward, exception, episodes) is included; the full transcript follows: ",
            rows.run.as_type("agent_run"),
        ],
        context_configs={
            "run": AgentRunContextConfig(
                agent_run_metadata=GlobFilter(
                    include=(
                        "trace_lab.trial",
                        "trace_lab.task",
                        "trace_lab.reward",
                        "trace_lab.exception_type",
                        "trace_lab.n_episodes",
                    )
                ),
            ),
        },
        model=MODEL,
        output_schema=schema,
        name="Judge stuck terminal, completion claim, failure owner",
    )
    print("plan steps registered; blocking for reading results...")
    results = reading.results
    print(f"reading {reading.id}: {len(results)} results")

    q = f"""SELECT trial_name, out_json, err_json, intok, outtok FROM (
SELECT CAST(ar.id AS TEXT) AS arid,
ar.metadata_json->'trace_lab'->>'trial' AS trial_name,
rr.output AS out_json, rr.error AS err_json,
rr.input_tokens AS intok, rr.output_tokens AS outtok
FROM reading_results rr
JOIN reading_result_links rrl ON rrl.result_id = rr.id
JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
WHERE rrl.reading_id = '{reading.id}') AS subq ORDER BY trial_name"""
    fetched = client.dql_result_to_dicts(client.execute_dql(CID, q))
    print(f"fetched {len(fetched)} rows")
    raw_dir = PRED / "docent" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_trial: dict[str, dict] = {}
    for r in fetched:
        o = r["out_json"]
        if isinstance(o, str):
            o = json.loads(o)
        by_trial[r["trial_name"]] = {
            "trial": r["trial_name"],
            "output": o,
            "error": r["err_json"],
            "input_tokens": r["intok"],
            "output_tokens": r["outtok"],
            "reading_id": str(reading.id),
            "collection_id": CID,
            "model": MODEL,
        }
    missing = [t for t in trials if t not in by_trial]
    if missing:
        print(f"missing reading rows: {missing}", file=sys.stderr)
        return 2
    for t, rec in by_trial.items():
        (raw_dir / f"{t}.docent.json").write_text(json.dumps(rec, indent=1))

    lines: list[str] = []
    for t in trials:
        rec = by_trial[t]
        out = rec["output"] or {}
        owner = out.get("failure_owner")
        lines.append(
            json.dumps(
                {
                    "trial": t,
                    "stop_reason": None,
                    "first_failure_ref": None,
                    "blame": map_blame(owner),
                    "loop_kind": None,
                    "loop_span": None,
                    "pass_copied": None,
                    "raw_terminal_stuck": out.get("terminal_stuck"),
                    "raw_stuck_cause": out.get("stuck_cause"),
                    "raw_stuck_start_evidence": out.get("stuck_start_evidence"),
                    "raw_stuck_end_evidence": out.get("stuck_end_evidence"),
                    "raw_false_completion_claim": out.get("false_completion_claim"),
                    "raw_claim_evidence": out.get("claim_evidence"),
                    "raw_failure_owner": owner,
                    "raw_owner_evidence": out.get("owner_evidence"),
                    "raw_first_mistake_evidence": out.get("first_mistake_evidence"),
                    "raw_reasoning": out.get("reasoning"),
                    "raw_error": rec["error"],
                    "raw_input_tokens": rec["input_tokens"],
                    "raw_output_tokens": rec["output_tokens"],
                    "tool": "docent",
                    "source": (
                        f"docent reading {reading.id} ({MODEL}) on collection {CID}; "
                        f"prompt/schema verbatim from har81_blind_reading.py; "
                        f"blame<-failure_owner (unclear->null/not expressed)"
                    ),
                }
            )
        )
    (PRED / "docent.jsonl").write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} rows to predictions/docent.jsonl")
    total_in = sum(r["input_tokens"] or 0 for r in by_trial.values())
    total_out = sum(r["output_tokens"] or 0 for r in by_trial.values())
    print(f"tokens: {total_in} in / {total_out} out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
