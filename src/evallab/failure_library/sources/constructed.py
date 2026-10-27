"""Constructed-broken task collections; originals are retained as controls, not defects."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TypedDict

from evallab.failure_library.records import Evidence, Finding, TaskRef
from evallab.failure_library.sources.swe_family import SplitPolicy, pinned_bytes, source_finding


class ImpossiblePin(TypedDict):
    revision: str
    identity: str
    source_id: str
    hashes: dict[str, str]


IMPOSSIBLE_PINS: dict[str, ImpossiblePin] = {
    "fjzzq2002/impossible_swebench": {
        "revision": "9c2d34f364b7229e8c0ff807c646100bdc18bbb5",
        "identity": "instance_id",
        "source_id": "impossible-swebench",
        "hashes": {
            "conflicting": "25d96f864431f9279375c94df05ffdaeeafbcacde0be8b2b0a0333daefa4186d",
            "oneoff": "6308b6fa360213dc40a5bb5e39217efcc440f48c76b8caaf00bbf3497cb67462",
            "original": "4ad148f9812e9a347ec529690bff8fed49b8ec7d0256e3b6768d375fed499f3b",
        },
    },
    "fjzzq2002/impossible_livecodebench": {
        "revision": "98650ffc3f28a01b261669b6d19fcd7773823710",
        "identity": "task_id",
        "source_id": "impossible-livecodebench",
        "hashes": {
            "conflicting": "9a13116c6cf48e8c2dc64ca2facdac4122aad30fe1aa52ab552a3b35638c22c8",
            "oneoff": "31a89b06195a223268c4e66ea9ae87c5ed5b744bb7d230ed950d2e012c2c6c56",
            "original": "3f128b7a8419fd4f2d1c11f3eca7cd5bceac0e2385810950e926276d07a784c5",
        },
    },
}
DJINN_DATASET = "EleutherAI/djinn-problems-v1.0"
DJINN_REVISION = "8c752336b9dda0339bbb652825691689a496d387"
DJINN_HASHES = {
    "coverage": "a2752bf25bc841944ec735ba40f71f3934f9abe3f1cc5d6f9f1be139610a2968",
    "gradient": "a0d9533c6953eb27856a79fae7f26fa051fcc05abf6a96bbc9173097d5a47afb",
    "train": "efa550d73c930a0b86399fb23822315e758309b8490f49697003a18696ba1d66",
}
TAMPER_CLASSES = frozenset(
    {
        "test_skipping",
        "verifier_logic_override",
        "result_manipulation",
        "environment_variable_manipulation",
    }
)


def parse_impossible(
    dataset: str, rows: Iterable[Mapping[str, Any]], policy: SplitPolicy
) -> list[Finding]:
    pin = IMPOSSIBLE_PINS[dataset]
    findings = []
    for row in rows:
        base_id = str(row[pin["identity"]])
        variant = row["impossible_type"]
        if variant == "original":
            continue
        if variant not in {"oneoff", "conflicting"}:
            raise ValueError(f"Unknown ImpossibleBench variant: {variant}")
        revision = pin["revision"]
        member = f"data/{variant}-00000-of-00001.parquet"
        task = TaskRef(
            source_id=pin["source_id"],
            task_id=f"{base_id}/{variant}",
            upstream_repo="https://huggingface.co/datasets/" + dataset,
            upstream_revision=revision,
            task_path=f"{member}#{pin['identity']}={base_id}",
        )
        url = f"{task.upstream_repo}/blob/{revision}/{member}"
        group = (
            "swe-bench/" if dataset.endswith("impossible_swebench") else "livecodebench/"
        ) + base_id
        findings.append(
            source_finding(
                "impossiblebench",
                task,
                "TS10" if variant == "conflicting" else "TS06",
                f"{dataset}/{variant}/{base_id}",
                f"ImpossibleBench constructed {variant} variant of {base_id}; "
                + (
                    "conflicting grading conditions make the task infeasible as written."
                    if variant == "conflicting"
                    else "a deliberately changed expected result disagrees with the intended specification."
                ),
                [
                    Evidence(
                        kind="construction",
                        url=url,
                        note="Deliberate published test intervention, not a natural-defect discovery "
                        "or an executed reward-hack label. Original control retained in raw data.",
                    )
                ],
                finder="ImpossibleBench authors",
                source_url=url,
                split=policy.get(base_id, group),
                direction="unfair_fail",
            )
        )
    return sorted(findings, key=lambda item: item.finding_id)


def _parquet_rows(
    collection: str,
    dataset: str,
    revision: str,
    split: str,
    digest: str,
    columns: list[str],
    state_root: Path | None,
) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    member = f"data/{split}-00000-of-00001.parquet"
    raw = pinned_bytes(
        collection,
        revision,
        member,
        digest,
        state_root=state_root,
        url=f"https://huggingface.co/datasets/{dataset}/resolve/{revision}/{member}",
    )
    return pq.read_table(io.BytesIO(raw), columns=columns).to_pylist()


def build_impossiblebench(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    policy = SplitPolicy(root)
    findings = []
    for dataset, pin in IMPOSSIBLE_PINS.items():
        for split, digest in pin["hashes"].items():
            rows = _parquet_rows(
                "impossiblebench",
                dataset,
                pin["revision"],
                split,
                digest,
                [pin["identity"], "impossible_type"],
                state_root,
            )
            if any(row["impossible_type"] != split for row in rows):
                raise ValueError("ImpossibleBench split and variant labels disagree")
            findings.extend(parse_impossible(dataset, rows, policy))
    return sorted(findings, key=lambda item: item.finding_id)


def djinn_family(row: Mapping[str, Any]) -> str:
    """Content-bind aliases across source IDs, mechanism classes, and splits."""
    material = "\x1f".join((" ".join(row["description"].split()), row["function_name"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_djinn(
    split: str, rows: Iterable[Mapping[str, Any]], policy: SplitPolicy
) -> list[Finding]:
    if split not in DJINN_HASHES:
        raise ValueError(f"Unknown Djinn split: {split}")
    findings = []
    for row in rows:
        task_id = row["id"]
        mechanism = row["exploit_type"]
        code = "TS04" if mechanism in TAMPER_CLASSES else "TS01"
        member = f"data/{split}-00000-of-00001.parquet"
        task = TaskRef(
            source_id="djinn",
            task_id=task_id,
            upstream_repo="https://huggingface.co/datasets/" + DJINN_DATASET,
            upstream_revision=DJINN_REVISION,
            task_path=f"{member}#id={task_id}",
        )
        url = f"{task.upstream_repo}/blob/{DJINN_REVISION}/{member}"
        findings.append(
            source_finding(
                "djinn",
                task,
                code,
                f"{member}/{task_id}",
                f"Djinn constructed dual-verifier problem; insecure-verifier mechanism "
                f"{mechanism} (source split {split}). A secure companion is not evidence "
                "that the intentionally insecure verifier was repaired in place.",
                [
                    Evidence(
                        kind="construction",
                        url=url,
                        note="Published constructed exploitable insecure verifier. Source hardening "
                        "probe claims are not task-bound execution receipts in this import.",
                    )
                ],
                finder="EleutherAI Djinn authors",
                source_url=url,
                split=policy.get(task_id, "djinn/" + djinn_family(row)),
                direction="wrong_pass",
                license="apache-2.0",
            )
        )
    return sorted(findings, key=lambda item: item.finding_id)


def build_djinn(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    policy = SplitPolicy(root)
    findings = []
    for split, digest in DJINN_HASHES.items():
        rows = _parquet_rows(
            "djinn",
            DJINN_DATASET,
            DJINN_REVISION,
            split,
            digest,
            ["id", "exploit_type", "description", "function_name"],
            state_root,
        )
        findings.extend(parse_djinn(split, rows, policy))
    return sorted(findings, key=lambda item: item.finding_id)


COLLECTIONS = {"impossiblebench": build_impossiblebench, "djinn": build_djinn}
