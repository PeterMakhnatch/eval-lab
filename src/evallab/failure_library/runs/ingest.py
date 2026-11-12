"""Source dispatch, conservative split admission, provenance notes and regex sidecars."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.failure_library.records import RunRecord
from evallab.failure_library.runs import cached, published
from evallab.failure_library.runs.common import (
    agent_text,
    label,
)
from evallab.failure_library.runs.io import (
    MALT_REDISTRIBUTION_REVISION,
    parquet_rows,
    reference,
)


@dataclass
class Candidate:
    run: RunRecord
    texts: Iterable[str]
    aliases: tuple[str, ...] = ()
    text_available: bool = True
    family: str | None = None


@dataclass
class Inputs:
    files: set[Path] = field(default_factory=set)
    details: dict[str, Any] = field(default_factory=dict)

    def use(self, path: Path) -> Path:
        self.files.add(path)
        return path










def _hacktrace(root: Path, inputs: Inputs, *, limit: int) -> Iterator[Candidate]:
    paths = sorted(root.glob("trajectories/*.parquet"))
    if not paths:
        raise ValueError(f"no HackTrace parquet in {root}")
    if not 1 <= limit <= 20_000:
        raise ValueError("HackTrace deterministic sample limit must be 1..20000")
    identities = []
    total = 0
    for path in paths:
        inputs.use(path)
        for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=["id"]):
            for row in batch.to_pylist():
                identities.append(
                    (hashlib.sha256(str(row["id"]).encode()).hexdigest(), str(row["id"]))
                )
                total += 1
    selected = {identity for _, identity in heapq.nsmallest(limit, identities)}
    inputs.details.update(
        {
            "population_rows": total,
            "sample_limit": limit,
            "sampling": "lowest SHA256(native id), tie-break native id; source train/test retained",
        }
    )
    for path in paths:
        for index, row in parquet_rows(path):
            if row["id"] not in selected:
                continue
            ref = reference(path, index, field="messages")
            run = published.hacktrace(row, ref)
            if row["split"] == "test":
                run = run.model_copy(update={"split": "heldout"})
            yield Candidate(run, agent_text(row["messages"]))






def _malt(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    redistributed = root / "redistributed.parquet"
    if redistributed.is_file():
        inputs.use(redistributed)
        for index, row in parquet_rows(redistributed):
            if row["source_dataset"] != "metr_malt":
                continue
            ref = reference(redistributed, index)
            run = cached.redistributed_malt(row, ref)
            # RH-Bench stores a role/content JSON sequence, not native tool events.
            text = row.get("response_hacking") or row.get("response_clean") or ""
            try:
                native = json.loads(text)
            except (ValueError, TypeError):
                native = text
            yield Candidate(run, agent_text(native))
        inputs.details.update(
            {
                "redistribution_repo": "ktolnos/rh-bench",
                "redistribution_revision": MALT_REDISTRIBUTION_REVISION,
                "full_source_access": "HF_TOKEN authenticated; HTTP 403 GatedRepo at intake",
                "missing": "task ids, full original messages/tool events and task revision",
                "split_exception": "all heldout: redistribution omits task-family identity and is benchmark-only",
                "license": "CC-BY-SA-4.0 redistribution; original MALT MIT; benchmarking only",
            }
        )
        return
    paths = sorted(root.glob("data/public-*.parquet"))
    if not paths:
        raise ValueError("MALT needs the gated default parquet or cached redistributed.parquet")
    for path in paths:
        inputs.use(path)
        for index, row in parquet_rows(path):
            meta = row["metadata"]
            ref = reference(path, index, field="samples")
            labels = [
                label(v, "human" if meta["manually_reviewed"] else "construction", ref)
                for v in meta["labels"]
            ]
            run = RunRecord(
                run_id="metr-malt:" + str(meta["run_id"]),
                collection="metr-malt",
                task={
                    "source_id": "metr-malt",
                    "task_id": meta["task_id"],
                    "upstream_repo": "https://huggingface.co/datasets/metr-evals/malt-public",
                },
                model=meta["model"],
                harness="METR",
                outcome="unknown",
                labels=labels,
                transcript_ref=ref,
                license="MIT",
            )
            texts = (
                text for sample in row["samples"] for text in agent_text(sample.get("output", []))
            )
            yield Candidate(run, texts, family=meta["task_id"].split("/")[0])




