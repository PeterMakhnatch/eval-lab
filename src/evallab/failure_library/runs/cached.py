"""Normalize native Wrench, Goodfire, CheatBench and redistributed MALT rows."""

from __future__ import annotations

from typing import Any

from evallab.failure_library.records import RunRecord, TaskRef
from evallab.failure_library.runs.common import label

WRENCH_INDEX_REVISION = "622449769ec6ddf33f770794489b8cd21712a0ad"
GOODFIRE_REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
CHEATBENCH_REVISION = "461360b34e93644955563dcc9243c6cc1a53bd0f"









def redistributed_malt(row: dict[str, Any], ref: str) -> RunRecord:
    if row["source_dataset"] != "metr_malt":
        raise ValueError("not a redistributed MALT row")
    source = ref + "&field=verification"
    labels = [label(str(row["label"]), "human", source)]
    if row["label"] == "hacking":
        labels.append(label("reward_hacked", "human", source))
    elif row["label"] in {"clean", "normal"}:
        labels.append(label("clean", "human", source))
    return RunRecord(
        run_id="metr-malt:" + str(row["source_id"]),
        collection="metr-malt",
        task=TaskRef(
            source_id="metr-malt",
            task_id="unresolved-run:" + str(row["source_id"]),
            upstream_repo="https://huggingface.co/datasets/metr-evals/malt-public",
        ),
        model=row.get("response_model"),
        harness="METR (redistributed text)",
        reward=None,
        outcome="unknown",
        labels=labels,
        transcript_ref=ref + "&field=response_hacking,response_clean",
        # The redistribution omits task ids. No honest task-family split is possible.
        split="heldout",
        license="CC-BY-SA-4.0",
    )
