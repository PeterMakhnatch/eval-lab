"""Docent strong-model reading for the HAR-119 hand-label fields.

One template reading asks the shared question set from
``model_readers.prompts`` (RATER_GUIDE distilled) with a JSON
output schema, over blind agent runs. Evidence fields carry transcript
block citations automatically; ``build`` maps the first citation block of
each evidence field back to a trajectory step via the upload's own
block->step map (same method as ``../har119/tools/docent_block_map.py``).

Usage (validation on HAR-104, then the frozen run on the HAR-119 set)::

    keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \\
      python research/explorations/trace-lab/model-readers/docent_strong/reading.py \\
      --collection b418ff53-5244-4297-8157-4450dcb1f1d7 \\
      --plan-name model-readers-draft-validation \\
      --model anthropic/claude-opus-5-5 \\
      --trials har104-d-000383__PmZMZ6z \\
      --out /tmp/validation.jsonl --raw-dir /tmp/validation_raw

Frozen run (after PROMPTS.sha256/FROZEN_AT are written)::

    keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \\
      python research/explorations/trace-lab/model-readers/docent_strong/reading.py \\
      --collection db32cc8f-e610-4373-a8dd-89b697a87a60 \\
      --plan-name model-readers-strong-trial-reading \\
      --model anthropic/claude-opus-5-5 \\
      --block-map research/explorations/trace-lab/har119/predictions/docent_block_map.json \\
      --out research/explorations/trace-lab/model-readers/predictions/docent_strong.jsonl \\
      --raw-dir research/explorations/trace-lab/model-readers/predictions/docent_strong_raw
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
MODEL_READERS = HERE.parent.parent
if str(MODEL_READERS) not in sys.path:
    sys.path.insert(0, str(MODEL_READERS))

from prompts import DOCENT_MODEL, DOCENT_PROMPT, DOCENT_SCHEMA  # noqa: E402

BLIND_META = (
    "trace_lab.trial",
    "trace_lab.task",
    "trace_lab.reward",
    "trace_lab.exception_type",
    "trace_lab.n_episodes",
)


def first_block(evidence: object) -> int | None:
    """First cited block_idx of an evidence field (Docent citation shape)."""
    if isinstance(evidence, dict):
        for cite in evidence.get("citations") or []:
            try:
                return int(cite["target"]["item"]["block_idx"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def evidence_text(evidence: object) -> str | None:
    if isinstance(evidence, dict):
        text = evidence.get("text")
        return str(text) if text is not None else None
    return str(evidence) if evidence is not None else None


def main(argv: list[str] | None = None) -> int:
    from docent import Docent
    from docent.data_models.context_config import AgentRunContextConfig
    from docent.data_models.metadata_util import GlobFilter

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--plan-name", required=True)
    parser.add_argument("--model", default=DOCENT_MODEL)
    parser.add_argument("--trials", nargs="*", default=None,
                        help="trace_lab.trial subset; default all runs")
    parser.add_argument("--out", required=True)
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--block-map", default=None,
                        help="block->step map JSON ({trial: {block: step}})")
    args = parser.parse_args(argv)

    assert "FIRST FAILURE" in DOCENT_PROMPT and "UPSTREAM FETCH" in DOCENT_PROMPT
    assert set(DOCENT_SCHEMA["required"]) == {
        "reasoning", "stop_reason", "stop_evidence", "first_failure_is_pass",
        "first_failure_what", "first_failure_evidence", "blame", "blame_evidence",
        "loop_kind", "loop_start_evidence", "loop_end_evidence",
        "upstream_fetch", "upstream_evidence",
    }

    block_map: dict[str, dict[str, int | None]] = {}
    if args.block_map:
        block_map = json.loads(Path(args.block_map).read_text())

    client = Docent()
    client.plan_name = args.plan_name
    client.plan_markdown(
        "Strong-model trial reading: stop, first failure, blame, loops, copied passes",
        """## Behavior
We review blind agent runs (a MiMo model driving the Terminus-2 harness on Python bug-fix tasks) and extract, per run, how it ended, the earliest failing turn, who owns the failure (model, harness, task, infra, or nobody on an earned pass), what kind of loop consumed it (completion-claim vs repetition, with span), and whether a passing result stands on upstream-fetched code.

## Measurement
One frozen LLM reading per run with transcript block citations, over the blind trace_lab metadata subset only (trial, task, reward, exception, episodes). No hand labels in metadata. First-citation blocks map back to trajectory steps through the upload's own block map; quotes are checked against the mapped step.
""",
    )
    trial_filter = ""
    if args.trials:
        quoted = ", ".join(f"'{t}'" for t in args.trials)
        trial_filter = f"WHERE ar.metadata_json->'trace_lab'->>'trial' IN ({quoted})"
    rows = client.query(
        args.collection,
        f"""SELECT id AS run FROM agent_runs ar
{trial_filter}
ORDER BY MD5(CONCAT(CAST(id AS TEXT), 'model-readers'))""",
        name="Blind trial runs",
    )
    reading = client.read(
        prompt_template=[
            DOCENT_PROMPT,
            rows.run.as_type("agent_run"),
        ],
        context_configs={
            "run": AgentRunContextConfig(
                agent_run_metadata=GlobFilter(include=BLIND_META),
            ),
        },
        model=args.model,
        output_schema=DOCENT_SCHEMA,
        name="Judge stop, first failure, blame, loop, copied pass",
    )
    print("plan steps registered; blocking for reading results...")
    results = reading.results
    print(f"reading {reading.id}: {len(results)} results")

    q = f"""SELECT trial_name, reward, out_json, err_json, intok, outtok FROM (
SELECT CAST(ar.id AS TEXT) AS arid,
ar.metadata_json->'trace_lab'->>'trial' AS trial_name,
CAST(ar.metadata_json->'trace_lab'->>'reward' AS NUMERIC) AS reward,
rr.output AS out_json, rr.error AS err_json,
rr.input_tokens AS intok, rr.output_tokens AS outtok
FROM reading_results rr
JOIN reading_result_links rrl ON rrl.result_id = rr.id
JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
WHERE rrl.reading_id = '{reading.id}') AS subq ORDER BY trial_name"""
    fetched = client.dql_result_to_dicts(client.execute_dql(args.collection, q))
    print(f"fetched {len(fetched)} rows")
    if args.trials:
        missing = [t for t in args.trials if t not in {r["trial_name"] for r in fetched}]
        if missing:
            print(f"missing reading rows: {missing}", file=sys.stderr)
            return 2
    if block_map:
        # Every cited block must resolve, like har119's block-map follow-up.
        for r in fetched:
            o = r["out_json"]
            if isinstance(o, str):
                o = json.loads(o)
            trial_map = block_map.get(r["trial_name"], {})
            for field, ev in (o or {}).items():
                if not isinstance(ev, dict):
                    continue
                for c in ev.get("citations") or []:
                    item = (c.get("target") or {}).get("item") or {}
                    if "block_idx" in item:
                        assert str(item["block_idx"]) in trial_map, (r["trial_name"], field, item)

    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    total_in = total_out = 0
    for r in fetched:
        trial = r["trial_name"]
        o = r["out_json"]
        if isinstance(o, str):
            o = json.loads(o)
        o = o or {}
        total_in += r["intok"] or 0
        total_out += r["outtok"] or 0
        (raw_dir / f"{trial}.docent_strong.json").write_text(
            json.dumps(
                {"trial": trial, "output": o, "error": r["err_json"],
                 "input_tokens": r["intok"], "output_tokens": r["outtok"],
                 "reading_id": str(reading.id), "collection_id": args.collection,
                 "model": args.model},
                indent=1,
            )
        )
        trial_map = block_map.get(trial, {})

        def step_of_evidence(
            evidence: object, _trial_map: dict[str, int | None] = trial_map
        ) -> tuple[int | None, int | None]:
            block = first_block(evidence)
            if block is None:
                return None, None
            step = _trial_map.get(str(block))
            return block, (int(step) if isinstance(step, int) else None)

        is_pass = bool(o.get("first_failure_is_pass"))
        ff_block, ff_step = step_of_evidence(o.get("first_failure_evidence"))
        ff_ref = None if is_pass or ff_step is None else f"head#{ff_step}"
        kind = o.get("loop_kind")
        start_block, start_step = step_of_evidence(o.get("loop_start_evidence"))
        end_block, end_step = step_of_evidence(o.get("loop_end_evidence"))
        if kind == "none":
            span = None
        else:
            span = [
                f"head#{start_step}" if start_step is not None else None,
                f"head#{end_step}" if end_step is not None else None,
            ]
            if span == [None, None]:
                span = None
        reward = r.get("reward")
        try:
            reward_f = float(reward) if reward is not None else None
        except (TypeError, ValueError):
            reward_f = None
        pass_copied = None if reward_f is None or reward_f < 1.0 else bool(o.get("upstream_fetch"))

        lines.append(
            json.dumps(
                {
                    "trial": trial,
                    "stop_reason": o.get("stop_reason"),
                    "first_failure_ref": ff_ref,
                    "blame": o.get("blame"),
                    "loop_kind": "none" if kind == "none" else kind,
                    "loop_span": None if kind == "none" else span,
                    "pass_copied": pass_copied,
                    "raw_first_failure_what": o.get("first_failure_what"),
                    "raw_first_failure_is_pass": is_pass,
                    "raw_first_failure_block": ff_block,
                    "raw_first_failure_step": ff_step,
                    "raw_first_failure_evidence": o.get("first_failure_evidence"),
                    "raw_loop_start_block": start_block,
                    "raw_loop_start_step": start_step,
                    "raw_loop_end_block": end_block,
                    "raw_loop_end_step": end_step,
                    "raw_upstream_fetch": bool(o.get("upstream_fetch")),
                    "raw_reward": reward_f,
                    "raw_stop_evidence": o.get("stop_evidence"),
                    "raw_blame_evidence": o.get("blame_evidence"),
                    "raw_reasoning": o.get("reasoning"),
                    "raw_error": r["err_json"],
                    "raw_input_tokens": r["intok"],
                    "raw_output_tokens": r["outtok"],
                    "tool": "docent_strong",
                    "source": (
                        f"docent reading {reading.id} ({args.model}) on "
                        f"collection {args.collection}; first-citation block "
                        f"-> step via upload block map"
                    ),
                }
            )
        )
    # Keep selection order when callers pass --trials in that order.
    if args.trials:
        order = {t: i for i, t in enumerate(args.trials)}
        lines.sort(key=lambda line: order.get(json.loads(line)["trial"], 0))
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} rows to {args.out}")
    print(f"tokens: {total_in} in / {total_out} out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
