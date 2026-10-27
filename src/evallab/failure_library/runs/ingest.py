"""Source dispatch, conservative split admission, provenance notes and regex sidecars."""

from __future__ import annotations

import hashlib
import heapq
import json
import shutil
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.failure_library.records import RunRecord
from evallab.failure_library.runs import cached, published
from evallab.failure_library.runs.common import (
    SplitPolicy,
    agent_text,
    awareness,
    label,
    link_findings,
)
from evallab.failure_library.runs.io import (
    MALT_REDISTRIBUTION_REVISION,
    REPOSITORIES,
    digest,
    jsonl_rows,
    parquet_rows,
    raw_root,
    reference,
)
from evallab.failure_library.store import LibraryStore


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


def _wrench(
    root: Path, inputs: Inputs, *, state: Path, transcript_root: Path | None
) -> Iterator[Candidate]:
    paths = {}
    for name in ("baseline", "hack", "non_hack", "tasks"):
        matches = sorted(root.rglob(name + ".parquet"))
        matches += sorted(root.glob(f"data/{name}/train-*.parquet"))
        if len(matches) != 1:
            raise ValueError(f"need exactly one {name} parquet in {root}; got {len(matches)}")
        paths[name] = inputs.use(matches[0])
    tasks = {str(row["task_id"]): row for _, row in parquet_rows(paths["tasks"])}
    for partition in ("baseline", "hack", "non_hack"):
        path = paths[partition]
        for index, row in parquet_rows(path):
            relative = row["original_trajectory_path"]
            native = state / "raw" / "terminal-wrench" / cached.WRENCH_REVISION / relative
            if not native.exists() and transcript_root is not None:
                original = transcript_root / relative
                if original.is_file():
                    native.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, native)
                    native.chmod(0o444)
            text: Iterable[str] = ()
            if native.is_file():
                inputs.use(native)
                ref = native.resolve().as_uri() + "#field=steps"
                text = agent_text(json.loads(native.read_text(encoding="utf-8")))
            else:
                ref = (
                    "https://huggingface.co/datasets/few-sh/terminal-wrench/resolve/"
                    + cached.WRENCH_REVISION
                    + "/"
                    + relative
                    + "#field=steps"
                )
            aliases = tuple(tasks[str(row["task_id"])].get("aliases") or [])
            run = cached.wrench(row, ref, label_ref=reference(path, index))
            yield Candidate(run, text, aliases, native.is_file())
    inputs.details["original_runs_only"] = True
    inputs.details["alternate_views_excluded"] = ["sanitized_hack", "stripped_hack"]


def _goodfire(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    stream_counts = {}
    for path in sorted(root.glob("*/traces.jsonl")):
        inputs.use(path)
        stream = path.parent.name
        possible_labels = [
            path.parent / "labels/judge_rollouts.jsonl",
            path.parent / "generic_monitor/monitor_rollouts.jsonl",
        ]
        label_path = next((p for p in possible_labels if p.is_file()), None)
        judgments = {}
        if label_path:
            inputs.use(label_path)
            judgments = {
                str(row["trace_id"]): (row, reference(label_path, index, jsonl=True))
                for index, row in jsonl_rows(label_path)
            }
        count = 0
        for index, row in jsonl_rows(path):
            trace = row["traces"][0]
            judgment, label_ref = judgments.get(str(trace["id"]), (None, None))
            ref = reference(path, index, jsonl=True, field="traces[0]")
            run = cached.goodfire(row, ref, stream=stream, judgment=judgment, label_ref=label_ref)
            task = trace["task"]["data"]
            family = str(task.get("name", run.task.task_id))
            yield Candidate(run, agent_text(trace.get("nodes", [])), family=family)
            count += 1
        stream_counts[stream] = count
    if not stream_counts:
        raise ValueError(f"no native Goodfire streams in {root}")
    inputs.details["streams"] = stream_counts
    inputs.details["live_db_coding_streams_at_intake"] = 3959
    inputs.details["generic_monitor_streams_unimported_at_intake"] = 2999
    inputs.details["external_upload_allowed"] = False


def _cheatbench(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    paths = sorted(root.rglob("full.parquet"))
    if len(paths) != 1:
        raise ValueError(f"need one CheatBench full.parquet in {root}")
    path = inputs.use(paths[0])
    for index, row in parquet_rows(path):
        ref = reference(path, index, field="trace")
        native = json.loads(row["trace"])
        # Envelopes have source-native shapes; only visit explicitly typed messages.
        texts = tuple(agent_text(native.get("messages", native.get("events", []))))
        yield Candidate(cached.cheatbench(row, ref), texts, text_available=bool(texts))


def _olmo(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    paths = sorted(root.glob("data/*.parquet"))
    if not paths:
        raise ValueError(f"no OLMo parquet in {root}")
    for path in paths:
        inputs.use(path)
        for index, row in parquet_rows(path):
            ref = reference(path, index, field="messages")
            yield Candidate(published.olmo(row, ref), agent_text(row["messages"]))


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


def _ablation(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    paths = sorted(root.rglob("trajectories.jsonl")) + sorted(root.rglob("trajectories.jsonl.gz"))
    paths += sorted(root.rglob("episodes.jsonl"))
    paths += sorted(root.rglob("records.jsonl.gz"))
    inputs.details["files_by_native_envelope"] = dict(
        sorted(Counter(path.name for path in paths).items())
    )
    if not paths:
        raise ValueError(f"no ablation native rollout streams in {root}")
    for path in paths:
        inputs.use(path)
        relative = str(path.relative_to(root))
        for index, row in jsonl_rows(path):
            ref = reference(path, index, jsonl=True)
            identity = (
                relative
                + ":"
                + str(row.get("trajectory_id", row.get("rollout_id", row.get("episode", index))))
            )
            if path.name == "episodes.jsonl":
                run = published.ablation_cheatbench(row, ref, identity=identity)
                texts = agent_text(row.get("harbor_trajectory") or {})
            elif path.name == "records.jsonl.gz":
                run = published.ablation_record(row, ref, identity=identity)
                texts = agent_text(row.get("items") or [])
            else:
                run = published.ablation(row, ref, identity=identity)
                texts = agent_text(row.get("messages") or [])
            family = str(row.get("task_id", row.get("id", run.task.task_id)))
            if path.name == "records.jsonl.gz" and row.get("env") == "grader":
                family = str((row.get("labels") or {}).get("family", family))
            yield Candidate(run, texts, family=family)
    inputs.details["row_identity"] = "source relative path plus native trajectory/episode id"
    inputs.details["license"] = "No dataset license declared by upstream; preserve locally"
    inputs.details["later_rhenvs_records"] = (
        "All records.jsonl.gz exports included; plain oracle test flags stay executed, static/elicitation flags stay construction. Native family=heldout and MBPP split=test remain heldout. No scalar rewards are invented."
    )


def _termgrade(root: Path, inputs: Inputs) -> Iterator[Candidate]:
    tests_path = inputs.use(root / "data/tests.parquet")
    tests = {
        row["trial"]: (row, reference(tests_path, index)) for index, row in parquet_rows(tests_path)
    }
    scalar_path = inputs.use(root / "data/trials.parquet")
    scalars = {row["trial"]: row for _, row in parquet_rows(scalar_path)}
    for path in sorted(root.glob("data/terminus/*.parquet")):
        inputs.use(path)
        for index, row in parquet_rows(path):
            merged = {**scalars[row["trial"]], **row}
            report, report_ref = tests.get(row["trial"], (None, None))
            ref = reference(path, index, field="terminus_episode")
            run = published.termgrade(merged, ref, tests=report, tests_ref=report_ref)
            episode = row.get("terminus_episode")
            text = agent_text(json.loads(episode)) if episode else ()
            yield Candidate(run, text, text_available=bool(episode))
    inputs.details["per_assertion_reports"] = len(tests)
    inputs.details["trials_in_scalar_table"] = len(scalars)


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


def ingest_collection(
    collection: str,
    store: LibraryStore,
    *,
    repo_root: Path,
    state_root: Path,
    source_root: Path | None = None,
    transcript_root: Path | None = None,
    sample_limit: int = 20_000,
    findings_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    root = source_root or raw_root(state_root, collection)
    inputs = Inputs()
    if collection == "terminal-wrench":
        candidates = _wrench(root, inputs, state=state_root, transcript_root=transcript_root)
    elif collection == "goodfire-reward-hack-data":
        candidates = _goodfire(root, inputs)
    elif collection == "cheatbench":
        candidates = _cheatbench(root, inputs)
    elif collection == "olmo-reward-hacking":
        candidates = _olmo(root, inputs)
    elif collection == "hacktrace":
        candidates = _hacktrace(root, inputs, limit=sample_limit)
    elif collection == "reward-hacking-ablation":
        candidates = _ablation(root, inputs)
    elif collection == "termgrade":
        candidates = _termgrade(root, inputs)
    elif collection == "metr-malt":
        candidates = _malt(root, inputs)
    else:
        raise ValueError(f"unknown run collection {collection}")
    policy = SplitPolicy(repo_root / "research/answer-keys/splits/assignments.csv")
    runs = []
    aliases = {}
    sidecar = {}
    for candidate in candidates:
        run = candidate.run
        force = None if run.split == "unassigned" else run.split
        split = policy.assign(
            run.task.source_id,
            candidate.family or run.task.task_id,
            aliases=candidate.aliases,
            force=force,
        )
        run = run.model_copy(update={"split": split})
        runs.append(run)
        aliases[run.run_id] = candidate.aliases
        sidecar[run.run_id] = awareness(
            run.run_id, candidate.texts, available=candidate.text_available
        )
    if not runs:
        raise ValueError(f"no runs decoded for {collection}")
    findings = list(store.iter_findings())
    for findings_root in findings_roots:
        reader = LibraryStore(
            findings_root=findings_root,
            manifest_root=store.manifest_root,
            runs_root=store.runs_root,
        )
        findings.extend(reader.iter_findings())
    runs = link_findings(runs, findings, aliases)
    revision = REPOSITORIES[collection][1]
    if collection == "metr-malt" and inputs.details.get("redistribution_revision"):
        revision = "ktolnos/rh-bench@" + MALT_REDISTRIBUTION_REVISION
    manifest = store.write_runs(collection, runs, source_revision=revision)
    sidecar_path = state_root / "sidecars" / f"{collection}.eval-awareness.jsonl"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        "".join(json.dumps(sidecar[key], sort_keys=True) + "\n" for key in sorted(sidecar)),
        encoding="utf-8",
    )
    inventory = [
        {"path": str(path.resolve()), "sha256": digest(path), "bytes": path.stat().st_size}
        for path in sorted(inputs.files)
    ]
    provenance = {
        "collection": collection,
        "source_repo": REPOSITORIES[collection][0],
        "source_revision": revision,
        "files": inventory,
        "details": inputs.details,
        "splits": dict(sorted(Counter(run.split for run in runs).items())),
        "linked_runs": sum(bool(run.finding_ids) for run in runs),
        "eval_awareness": {
            "available": sum(bool(item["text_available"]) for item in sidecar.values()),
            "mentions": sum(bool(item["eval_awareness"]) for item in sidecar.values()),
            "sidecar_sha256": digest(sidecar_path),
            "by_signal": {
                key: sum(bool(item[key]) for item in sidecar.values())
                for key in ("tested", "monitored", "graded")
            },
        },
    }
    provenance_root = state_root / "provenance"
    provenance_root.mkdir(parents=True, exist_ok=True)
    provenance_path = provenance_root / f"{collection}.json"
    provenance_path.write_text(
        json.dumps(provenance, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    note = store.manifest_root / f"{collection}.md"
    note.write_text(
        _note(manifest.model_dump(mode="json"), provenance, digest(provenance_path)),
        encoding="utf-8",
    )
    return {
        **manifest.model_dump(mode="json"),
        "provenance_sha256": digest(provenance_path),
        "splits": provenance["splits"],
        "linked_runs": provenance["linked_runs"],
        "eval_awareness": provenance["eval_awareness"],
    }


def _note(manifest: dict[str, Any], provenance: dict[str, Any], source_sha: str) -> str:
    collection = manifest["collection"]
    content = [
        f"# {collection} run collection",
        "",
        f"- Source: `https://huggingface.co/datasets/{provenance['source_repo']}`.",
        f"- Pinned source revision: `{manifest['source_revision']}`.",
        f"- Normalized rows: **{manifest['count']}**; JSONL SHA256 `{manifest['sha256']}`.",
        f"- Raw-source inventory: `{source_sha}` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/{collection}.json`).",
        f"- Splits: `{json.dumps(provenance['splits'], sort_keys=True)}`.",
        f"- Label counts (labels, not distinct runs): `{json.dumps(manifest['label_kinds'], sort_keys=True)}`.",
        f"- Outcome counts: `{json.dumps(manifest['outcomes'], sort_keys=True)}`.",
        f"- Identity-linked runs: {provenance['linked_runs']}; links across revisions are investigation leads, not defect confirmations.",
        "",
        "## Admission and provenance",
        "",
        "Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.",
        "",
        "Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.",
        "Rebuild with `evallab failures runs ingest "
        + collection
        + "` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify "
        + collection
        + "` and `evallab failures stats --json`.",
        "",
        "",
        "RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.",
        "",
        "## Eval-awareness first cut",
        "",
        f"Agent-prose regex only: `{json.dumps(provenance['eval_awareness'], sort_keys=True)}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.",
        "",
        "## Source-specific details",
        "",
        "```json",
        json.dumps(provenance["details"], sort_keys=True, indent=2),
        "```",
        "",
    ]
    if collection == "terminal-wrench":
        content += [
            "## RS mapping",
            "",
            "| Native category | RS |",
            "|---|---|",
            *[f"| {key} | {value} |" for key, value in sorted(cached.WRENCH_CODES.items())],
            "",
            "Only the 2,352 baseline + 3,632 hack + 2,657 non_hack original run lineages are ingested; sanitized and stripped views are excluded. Native transcripts not already cached keep pinned HF references, with regex coverage reported separately.",
            "",
        ]
    if collection == "metr-malt":
        content += [
            "## Full-source access",
            "",
            "The authenticated HF_TOKEN request returned HTTP 403 GatedRepo. Peter must request/accept access at https://huggingface.co/datasets/metr-evals/malt-public before the full 7,179-row default config can be downloaded. The local access-receipt.json records this attempt without credentials. The 572 redistributed rows are retained as text-only, heldout benchmarking data under the redistribution's CC-BY-SA-4.0 terms, not promoted to full native MALT runs.",
            "",
        ]
    return "\n".join(content)
