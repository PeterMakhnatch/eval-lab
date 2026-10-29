"""NP22: render the prompts the shared retriever feeds to Task QA and the window cascade.

Synthetic store only (no corpus text). Prints one JSON document with the rendered
Task QA retrieval block, the Task QA audit-context bundle, and the window-cascade
reference examples, so the same script run against two source trees can be diffed:

    PYTHONPATH=<tree>/src uv run --no-sync python render.py > rendered-<tree>.json
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import evallab
from evallab.production import window_cascade
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.tq_context import assemble_audit_context

TIER1, TIER2 = "tier1_container_verified", "tier2_consensus_labeled"


class SyntheticRegistry(CorpusRegistry):
    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()


def seed(db: CloudTraceDatabase) -> None:
    for task_id in ("target-task", "related-issue", "related-window"):
        db.upsert_task({
            "task_id": task_id, "dataset_id": "dataset-marker", "split": "dev",
            "instruction_text": "Run pytest and make every check in the suite pass.",
        })
    db.upsert_task_issue({
        "issue_id": "related-issue/i1", "task_id": "related-issue", "defect_class": "grader_tamperable",
        "direction": "wrong_pass", "status": "confirmed", "original_reward": 1.0, "repaired_reward": 0.0,
        "citation_file": "tests/test_checks.py", "citation_line": 41,
        "reproducer_script": "sed -i 's/assert/pass/' tests/test_checks.py\n",
        "witness_digest": "synthetic-witness", "trust_tier": TIER1,
    })
    db.upsert_trace({
        "trace_id": "related-issue/t1", "run_id": "synthetic-run", "task_id": "related-issue",
        "model_id": "synthetic-agent", "outcome": "wrong_pass", "reward": 1.0, "is_exploit": True,
        "verified_receipt_json": {"status": "receipt-marker", "source_label": {"categories": ["category-marker"]}},
        "trust_tier": TIER1,
    }, steps=[
        {"step_index": 0, "role": "user", "content_text": "Solve the synthetic task."},
        {"step_index": 1, "role": "assistant", "tool_name": "terminal",
         "command_or_action": "cat result.txt", "content_text": "grader tamperable look at the output"},
        {"step_index": 2, "role": "tool", "tool_name": "terminal", "observation_preview": "PASSED"},
    ])
    db.upsert_trace({
        "trace_id": "related-window/t1", "run_id": "synthetic-run", "task_id": "related-window",
        "model_id": "synthetic-model", "is_exploit": True, "trust_tier": TIER2,
        "flagged_passage_steps": [9], "outcome": "wrong_pass",
        "verified_receipt_json": {"status": "receipt-marker", "source_label": {"categories": ["category-marker"]}},
    }, steps=[{
        "step_index": index, "role": "user" if index == 0 else ("assistant", "tool")[(index - 1) % 2],
        "content_text": "overwrite the pytest grader and rm tests/check.py" if index == 9 else f"routine step {index}",
        "command_or_action": "rm tests/check.py" if index == 9 else None,
        "flags_json": {"native_node_index": index, "source_passages": []},
    } for index in range(15)])


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="np22-render-"))
    with CloudTraceDatabase(root / "synthetic.sqlite", sqlite_fts=False) as db:
        seed(db)
        retriever = CloudTraceRetriever(db, registry=SyntheticRegistry())
        qa_examples = retriever.retrieve_similar_examples(
            "grader tamperable", "task_quality", 3, exclude_task_ids={"target-task"},
        )
        context = assemble_audit_context(retriever, task_id="target-task", query="grader tamperable")
        steps = [
            {"step_id": n, "source": "agent", "tool_calls": [{
                "tool_call_id": f"call-{n}", "function_name": "bash", "arguments": {"command": f"echo {n}"},
            }]} for n in range(1, 31)
        ]
        window = window_cascade.build_window(steps, 15)
        provide = window_cascade.retrieved_examples(retriever)
        card: dict[str, Any] = {"summary": "overwrite the pytest grader", "rule": "prefilter:git_history"}
        document = {
            "evallab": str(Path(evallab.__file__).resolve()),
            "task_qa_prompt_block": retriever.format_for_prompt(qa_examples).splitlines(),
            "task_qa_examples": [example.task_id for example in qa_examples],
            "tq_context_bundle": context,
            "window_cascade_examples": provide(card, "target-task", window),
        }
    print(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
