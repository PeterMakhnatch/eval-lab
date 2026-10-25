"""Expand :class:`CloudTraceDatabase` from pinned public corpora, losslessly and sealed-safe.

Every adapter here reads a source that is already pinned by revision or by an
immutable local archive, projects it into the four live projection tables, and
refuses anything a :class:`CorpusRegistry` seal covers. Sealed identity is
checked by task name, so a public row that reuses a ``tb4-audit-v1`` TEST task
name or a MiMo permanent heldout is skipped under its own label — the external
dataset id never launders sealed work.

Sources and what each genuinely contributes:

- **TB4 Hub** (``~/Developer/eval-lab-results/tb4-hub``): frozen public
  leaderboard archives. Only ``dev_task_allowlist`` memberships that are
  ``admitted_exact_source`` and ``complete`` are read, so the 44 non-DEV tasks
  are never opened. Steps come from the trial's own ``agent/trajectory.json``;
  ``result.json`` supplies the served ``provider/model`` and the reward.
- **Goodfire** (``Goodfire/reward-hack-data``): one pinned judge-labelled run per
  call. ``traces.jsonl`` supplies the real rollout nodes; ``labels/judge_rollouts.jsonl``
  supplies the three-vote verdict (``yes``/``no``/``split``), which is recorded as
  a source label, never as our own confirmation.
- **Terminal Wrench** (``few-sh/terminal-wrench-trajectories`` index plus the
  ``few-sh/terminal-wrench`` payload): the index rows carry the publisher's
  ``classification``/``exploit_categories``/``key_hack_steps`` and the task
  instruction; the payload carries the trajectory body.
- **Staged metadata** (SimGlot, SWE-bench Pro Verified, CUA-VerifierBench,
  Ignition): the on-disk library loaders emit ``staged``/``held`` annotation
  rows. They carry no episode text, so they project to task records and issue
  evidence only, and each row states in ``metadata_json`` that the publisher
  withheld the instruction.

Projection is bounded, never silently: long step text is clipped with an explicit
marker, base64 payloads (images, audio) are replaced by a length marker, and the
counts of clipped/elided/dropped steps are recorded in the trace's provenance
receipt. Raw evidence is either copied into the SHA-256 CAS as a deterministic
gzip bundle or referenced by an immutable archive path plus digests.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet

from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS
from evallab.production.corpus_queue import CorpusRegistry

#: Receipt schema for one expansion run.
SCHEMA = "evallab.public_corpus_expansion/v1"
#: Stored in ``agent_traces.verified_receipt_json``; status is never confirmed/verified.
TRACE_PROVENANCE_SCHEMA = "evallab.public_trace_provenance/v1"
#: Stored in the CAS for one raw source row.
RAW_BUNDLE_SCHEMA = "evallab.public_trace_raw/v1"

TB4_HUB_DATASET = "tb4-hub-dev-v1"
GOODFIRE_DATASET = "goodfire-rollouts"
TERMINAL_WRENCH_DATASET = "terminal-wrench"
SIMGLOT_DATASET = "simglot"
SWEBENCH_PRO_DATASET = "swebench-pro-verified"
CUA_VERIFIER_DATASET = "cua-verifierbench"
IGNITION_DATASET = "ignition-backdoor"

#: Public corpora publish no DEV/TEST seal of ours; the hub shards are the only
#: source that also publishes its own DEV allowlist.
PUBLIC_SPLIT = "public"
DEV_SPLIT = "DEV"

GOODFIRE_REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
GOODFIRE_BASE = (
    "https://huggingface.co/datasets/Goodfire/reward-hack-data/resolve/" + GOODFIRE_REVISION
)
GOODFIRE_RUNS = (
    "swebench-verified-kimi-k3-nohack",
    "swebench-verified-glm-5.2-nohack",
    "swebench-verified-qwen3.8-2.4t-a95b-fp8-nohack",
    "deepswe-kimi-k3",
    "deepswe-glm-5.2-fp8",
    "deepswe-qwen3.8-2.4t-a95b-fp8",
    "impossiblebench-kimi-k3",
    "impossiblebench-glm-5.2-fp8",
    "impossiblebench-qwen3.8-2.4t-a95b-fp8",
)

WRENCH_INDEX_REVISION = "622449769ec6ddf33f770794489b8cd21712a0ad"
WRENCH_PAYLOAD_REVISION = "6ab64c43a4c6a4d9aa53c2425f37e715257123a3"
WRENCH_PAYLOAD_BASE = (
    "https://huggingface.co/datasets/few-sh/terminal-wrench/resolve/" + WRENCH_PAYLOAD_REVISION
)
#: ``sanitized_hack``/``stripped_hack`` are derived variants of ``hack``; reading
#: them would triple-count the same trajectory.
WRENCH_INDEX_SPLITS = ("hack", "non_hack", "baseline")

USER_AGENT = {"User-Agent": "eval-lab-corpus-expansion/1.0", "Accept-Encoding": "identity"}
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")


@dataclass(frozen=True)
class Limits:
    """Bounded projection of one step; every bound is reported in the receipt."""

    content_chars: int = 4000
    observation_chars: int = 2000
    action_chars: int = 1200
    max_steps: int = 600


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Return ``(text, clipped)`` with an explicit marker instead of silent loss."""
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]}…[clipped {len(text) - limit} of {len(text)} chars]", True


def _elide_base64(text: str) -> tuple[str, int]:
    """Replace long base64 payloads with a length marker; count replacements."""
    elided = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal elided
        elided += 1
        return f"<base64 {len(match.group(0))} chars elided>"

    return _BASE64_RUN.sub(replace, text), elided


def _number(value: Any) -> float | None:
    """A finite float for source numbers; anything else (including bools) is ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_digest(*parts: str) -> str:
    return _digest("\x00".join(parts).encode("utf-8"))[:32]


def _raw_bundle(raw: bytes, *, locator: str, content_type: str) -> dict[str, Any]:
    """Deterministic, self-describing CAS bundle: gzip bytes plus their digests."""
    return {
        "schema": RAW_BUNDLE_SCHEMA,
        "locator": locator,
        "content_type": content_type,
        "encoding": "gzip+base64",
        "sha256": _digest(raw),
        "bytes": len(raw),
        "payload": base64.b64encode(gzip.compress(raw, 6, mtime=0)).decode("ascii"),
    }


def _message_text(value: Any) -> str:
    """Flatten a chat message body (string or multimodal parts) to searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = []
        for item in value:
            if isinstance(item, Mapping):
                kind = item.get("type")
                if kind == "text":
                    parts.append(str(item.get("text", "")))
                elif kind in {"image", "audio", "video"}:
                    media = (item.get("source") or {}).get("media_type")
                    parts.append(f"<{kind} {media or 'unknown'}>")
                else:
                    parts.append(json.dumps(item, sort_keys=True, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _tool_calls_text(value: Any) -> str:
    """Render tool calls as ``name arguments`` lines, accepting both source shapes."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ""
    rendered = []
    for call in value:
        if not isinstance(call, Mapping):
            continue
        name = call.get("function_name") or call.get("name") or "tool"
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        rendered.append(f"{name} {arguments}")
    return "\n".join(rendered)


def _observation_text(value: Any) -> str:
    """Render a tool observation, accepting harbor ``results`` and plain content."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        results = value.get("results")
        if isinstance(results, Sequence) and not isinstance(results, (str, bytes, bytearray)):
            parts = [_message_text(result.get("content") if isinstance(result, Mapping) else result)
                     for result in results]
            return "\n".join(part for part in parts if part)
        content = value.get("content")
        if content is not None:
            return _message_text(content)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _bounded_step(
    step_index: int,
    role: str,
    *,
    action: str | None,
    content: str | None,
    observation: str | None,
    limits: Limits,
    flags: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int, int, bool]:
    """Build one bounded step row; return it with elided/clipped counters."""
    elided = 0
    clipped = False
    texts: dict[str, str | None] = {}
    for field, value, bound in (
        ("command_or_action", action, limits.action_chars),
        ("content_text", content, limits.content_chars),
        ("observation_preview", observation, limits.observation_chars),
    ):
        if value is None:
            texts[field] = None
            continue
        cleaned, count = _elide_base64(value)
        elided += count
        texts[field], was_clipped = _clip(cleaned, bound)
        clipped = clipped or was_clipped
    row = {
        "step_index": step_index,
        "role": role or "unknown",
        "tool_name": None,
        "command_or_action": texts["command_or_action"],
        "content_text": texts["content_text"],
        "observation_preview": texts["observation_preview"],
        "observation_cas_uri": None,
        "flags_json": dict(flags or {}),
    }
    return row, elided, 1 if clipped else 0, clipped


def _tool_name(action: str | None) -> str | None:
    """The first token of a rendered action, so ``--tool`` filters stay exact."""
    if not action:
        return None
    token = action.split(" ", 1)[0].strip()
    return token or None


def project_harbor_trajectory(
    steps: Sequence[Mapping[str, Any]],
    *,
    limits: Limits,
    key_steps: Iterable[int] = (),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Project harbor-style ``trajectory.json`` steps (TB4 Hub and Terminal Wrench).

    ``source`` names the speaker (``user``/``agent``/``tool``/``system``); tool
    calls become ``command_or_action`` and ``observation`` becomes the preview.
    ``key_steps`` marks the publisher's own hack-step indices in ``flags_json``.
    """
    key = set(key_steps)
    rows: list[dict[str, Any]] = []
    stats = Counter()
    for position, step in enumerate(steps):
        if position >= limits.max_steps:
            stats["dropped"] += len(steps) - limits.max_steps
            break
        if not isinstance(step, Mapping):
            stats["invalid"] += 1
            continue
        action = _tool_calls_text(step.get("tool_calls")) or None
        observation = _observation_text(step.get("observation")) or None
        role = str(step.get("source") or "unknown")
        flags: dict[str, Any] = {}
        if position in key:
            flags["publisher_key_hack_step"] = True
        row, elided, clipped, _ = _bounded_step(
            position,
            role,
            action=action,
            content=_message_text(step.get("message")) or None,
            observation=observation,
            limits=limits,
            flags=flags,
        )
        row["tool_name"] = _tool_name(action)
        if row["tool_name"] is None and step.get("function_name"):
            row["tool_name"] = str(step["function_name"])
        stats["elided_base64"] += elided
        stats["clipped_steps"] += clipped
        rows.append(row)
    return rows, dict(stats)


def project_goodfire_nodes(
    nodes: Sequence[Mapping[str, Any]], *, limits: Limits
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Project Goodfire ``traces[0].nodes`` (mini-swe-agent message tree) to steps."""
    rows: list[dict[str, Any]] = []
    stats = Counter()
    for position, node in enumerate(nodes):
        if position >= limits.max_steps:
            stats["dropped"] += len(nodes) - limits.max_steps
            break
        if not isinstance(node, Mapping):
            stats["invalid"] += 1
            continue
        message = node.get("message")
        if not isinstance(message, Mapping):
            stats["invalid"] += 1
            continue
        action = _tool_calls_text(message.get("tool_calls")) or None
        content = _message_text(message.get("content"))
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            content = f"{content}\n<reasoning>\n{reasoning}".strip()
        observation = _message_text(message.get("content")) if message.get("role") == "tool" else None
        if observation is not None:
            content = None
        row, elided, clipped, _ = _bounded_step(
            position,
            str(message.get("role") or "unknown"),
            action=action,
            content=content or None,
            observation=observation,
            limits=limits,
            flags={"parent": node.get("parent")} if node.get("parent") is not None else {},
        )
        row["tool_name"] = _tool_name(action)
        stats["elided_base64"] += elided
        stats["clipped_steps"] += clipped
        rows.append(row)
    return rows, dict(stats)


class SealGuard:
    """Refuse every identity a :class:`CorpusRegistry` seal covers."""

    def __init__(self, registry: CorpusRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CorpusRegistry()
        self.refusals: Counter[str] = Counter()

    def refusal(self, task_id: str) -> str | None:
        seal = self.registry.sealed_identity(task_id=task_id)
        if seal is not None:
            self.refusals[seal] += 1
        return seal


class ExpansionWriter:
    """Write projected rows through one guarded, receipt-counting front door."""

    def __init__(
        self,
        database: CloudTraceDatabase,
        *,
        guard: SealGuard,
        cas: S3CompatibleCAS | None = None,
        limits: Limits | None = None,
        label: str = "live",
    ) -> None:
        self.database = database
        self.label = label
        self.guard = guard
        self.cas = cas
        self.limits = limits if limits is not None else Limits()
        self.counts: dict[str, Counter[str]] = {}
        self.exclusions: Counter[str] = Counter()
        self.provenance: dict[str, Any] = {}
        self.before = database.row_counts()

    def _count(self, source: str, field: str, amount: int = 1) -> None:
        self.counts.setdefault(source, Counter())[field] += amount

    def admit(self, source: str, task_id: str) -> bool:
        """Seal gate an adapter calls before it opens any byte of a source row.

        Adapters that stream or download evidence must ask here first: a sealed
        identity has to cost zero reads, not a read that is discarded afterwards.
        """
        if self.guard.refusal(task_id) is None:
            return True
        self._count(source, "skipped_sealed_task")
        return False

    def task(self, source: str, row: Mapping[str, Any]) -> bool:
        if self.guard.refusal(str(row["task_id"])) is not None:
            self._count(source, "skipped_sealed_task")
            return False
        self.database.upsert_task(row)
        self._count(source, "corpus_tasks")
        return True

    def task_if_absent(self, source: str, row: Mapping[str, Any]) -> bool:
        """Register a task only when the live store has no record for that id."""
        if self.guard.refusal(str(row["task_id"])) is not None:
            self._count(source, "skipped_sealed_task")
            return False
        if self.database.get_task(str(row["task_id"])) is not None:
            self._count(source, "corpus_tasks_preserved")
            return True
        self.database.upsert_task(row)
        self._count(source, "corpus_tasks")
        return True

    def issue(self, source: str, row: Mapping[str, Any]) -> bool:
        if self.guard.refusal(str(row["task_id"])) is not None:
            self._count(source, "skipped_sealed_task")
            return False
        self.database.upsert_task_issue(row)
        self._count(source, "task_issues")
        return True

    def trace(
        self,
        source: str,
        trace: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
    ) -> bool:
        if self.guard.refusal(str(trace["task_id"])) is not None:
            self._count(source, "skipped_sealed_trace")
            return False
        self.database.upsert_trace(trace, steps=steps)
        self._count(source, "agent_traces")
        self._count(source, "trace_steps", len(steps))
        return True

    def store_raw(self, raw: bytes, *, locator: str, content_type: str) -> str | None:
        """CAS one raw source row; ``None`` when no CAS is configured."""
        if self.cas is None:
            return None
        return self.cas.put_bundle(
            _raw_bundle(raw, locator=locator, content_type=content_type)
        )

    def receipt(self, *, sources: Mapping[str, Any]) -> dict[str, Any]:
        after = self.database.row_counts()
        return {
            "schema": SCHEMA,
            "database": self.label,
            "guard": {
                "sealed_task_names": len(self.guard.registry._sealed_names),
                "sealed_item_ids": len(self.guard.registry._sealed_ids),
                "refusals": dict(sorted(self.guard.refusals.items())),
            },
            "limits": asdict(self.limits),
            "row_counts_before": self.before,
            "row_counts_after": after,
            "row_counts_added": {key: after[key] - self.before[key] for key in after},
            "counts": {source: dict(sorted(counts.items())) for source, counts in self.counts.items()},
            "exclusions": dict(sorted(self.exclusions.items())),
            "sources": dict(sources),
        }


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path.name}:{number}: invalid JSON") from error


def _http_lines(url: str, *, max_bytes: int) -> Iterator[bytes]:
    """Yield complete lines from a bounded HTTP stream, never buffering the file."""
    read = 0
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - pinned HTTPS
        while True:
            line = response.readline()
            if not line:
                return
            read += len(line)
            if read > max_bytes:
                raise RuntimeError(f"{url}: exceeded {max_bytes} bytes before the limit")
            yield line


def _http_bytes(url: str, *, max_bytes: int, attempts: int = 3) -> bytes:
    """Fetch one pinned object, retrying transport failures with bounded backoff.

    Hugging Face answers an unauthenticated burst with 429; a per-object retry
    keeps a wide fetch pool from turning rate limits into missing evidence.
    """
    request = urllib.request.Request(url, headers=USER_AGENT)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data = response.read(max_bytes + 1)
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == attempts:
                raise
            time.sleep(float(error.headers.get("Retry-After") or 2 ** attempt))
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts:
                raise
            time.sleep(float(2 ** attempt))
    else:  # pragma: no cover - the loop either breaks or raises
        raise RuntimeError(f"{url}: unreachable")
    if len(data) > max_bytes:
        raise RuntimeError(f"{url}: exceeded {max_bytes} bytes")
    return data


def _task_row(
    task_id: str,
    *,
    dataset_id: str,
    split: str,
    instruction: str,
    metadata: Mapping[str, Any],
    cas_uri: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "dataset_id": dataset_id,
        "split": split,
        "instruction_text": instruction,
        "original_grader_digest": None,
        "repaired_grader_digest": None,
        "cas_uri": cas_uri,
        "metadata_json": dict(metadata),
    }


def _provenance(
    *,
    dataset: str,
    locator: str,
    revision: str | None,
    raw_sha256: str | None,
    steps_projected: int,
    stats: Mapping[str, int],
    labels: Mapping[str, Any] | None = None,
    limits: Limits,
) -> dict[str, Any]:
    return {
        "schema": TRACE_PROVENANCE_SCHEMA,
        "status": "unverified_public_archive",
        "source": {
            "dataset": dataset,
            "locator": locator,
            "revision": revision,
            "raw_sha256": raw_sha256,
        },
        "projection": {
            "limits": asdict(limits),
            "steps_projected": steps_projected,
            "steps_clipped": stats.get("clipped_steps", 0),
            "base64_elided": stats.get("elided_base64", 0),
            "steps_dropped": stats.get("dropped", 0),
            "steps_invalid": stats.get("invalid", 0),
        },
        "labels": dict(labels or {}),
        "confirmation": "publisher label only; no execution of this row by eval-lab",
    }


def ingest_tb4_hub(
    writer: ExpansionWriter,
    index_path: Path,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Ingest admitted, complete TB4 Hub DEV memberships from frozen local archives."""
    source = "tb4-hub"
    selected = []
    for row in _jsonl(index_path):
        if row.get("dev_task_allowlist") is not True:
            writer.exclusions["tb4_hub_non_dev_membership"] += 1
            continue
        if row.get("provenance_admission") != "admitted_exact_source":
            writer.exclusions["tb4_hub_not_admitted_exact_source"] += 1
            continue
        if row.get("acquisition_state") != "complete" or row.get("evidence_class") != "full_trajectory":
            writer.exclusions["tb4_hub_incomplete_archive"] += 1
            continue
        selected.append(row)
    selected.sort(key=lambda row: (row["task"], row["row_id"], row["hub_trial_id"]))
    if limit is not None:
        selected = selected[:limit]

    archived = 0
    for row in selected:
        task_id = str(row["task"])
        if not writer.admit(source, task_id):
            continue
        archive = Path(str(row.get("archive_path") or ""))
        trajectory_path = archive / "agent" / "trajectory.json"
        result_path = archive / "result.json"
        if not trajectory_path.is_file():
            writer.exclusions["tb4_hub_missing_trajectory_bytes"] += 1
            continue
        raw = trajectory_path.read_bytes()
        document = json.loads(raw)
        result = json.loads(result_path.read_bytes()) if result_path.is_file() else {}
        instruction = ""
        steps_source = document.get("steps") or []
        if steps_source and isinstance(steps_source[0], Mapping):
            instruction = _message_text(steps_source[0].get("message"))
        model_id = (
            ((result.get("config") or {}).get("agent") or {}).get("model_name")
            or ((document.get("agent") or {}).get("model_name"))
            or str(row.get("model_label") or "unknown")
        )
        model_id = str(model_id)
        writer.task_if_absent(source, _task_row(
            task_id,
            dataset_id=TB4_HUB_DATASET,
            split=DEV_SPLIT,
            instruction=instruction or f"terminal-bench task {task_id.split('/', 1)[-1]}",
            metadata={
                "row_kind": "public_leaderboard_dev_trial_task",
                "instruction_source": "trajectory step 1 user message" if instruction else "unavailable",
                "dev_task_allowlist": True,
                "source_dataset": "terminal-bench/terminal-bench/4-0-0",
                "hub_rows": [row["row_id"]],
            },
        ))
        steps, stats = project_harbor_trajectory(steps_source, limits=writer.limits)
        receipt = _provenance(
            dataset=TB4_HUB_DATASET,
            locator=str(archive),
            revision=str(row.get("tree_sha256") or ""),
            raw_sha256=_digest(raw),
            steps_projected=len(steps),
            stats=stats,
            labels={
                "model_label": row.get("model_label"),
                "reasoning_effort": row.get("reasoning_effort"),
                "hub_row_id": row.get("row_id"),
                "hub_trial_id": row.get("hub_trial_id"),
                "board_rank": row.get("board_rank"),
                "archive_id_differs": row.get("archive_id_differs"),
            },
            limits=writer.limits,
        )
        trace = {
            "trace_id": f"{TB4_HUB_DATASET}:{row['row_id']}:{row['hub_trial_id']}",
            "run_id": f"{TB4_HUB_DATASET}:{row['row_id']}:{row.get('job_id')}",
            "task_id": task_id,
            "model_id": model_id,
            "outcome": f"hub:{row.get('trial_outcome') or row.get('native_status') or 'unknown'}",
            "reward": row.get("reward"),
            "is_exploit": False,
            "is_task_defect_fail": False,
            "step_count": len(steps),
            "raw_cas_uri": None,
            "verified_receipt_json": receipt,
        }
        if writer.trace(source, trace, steps):
            archived += 1
    writer.provenance[source] = {
        "index": str(index_path),
        "memberships_selected": len(selected),
        "traces_ingested": archived,
        "bytes_hashed_not_copied": True,
        "reason": (
            "trial archives stay immutable on disk with their own sha256 manifest; the "
            "trace receipt records the archive path and the trajectory digest instead of "
            "copying multi-megabyte base64 payloads into the CAS"
        ),
    }
    return writer.provenance[source]


def _goodfire_labels(base: str, run: str, *, max_bytes: int) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    url = f"{base}/{run}/labels/judge_rollouts.jsonl"
    for line in _http_lines(url, max_bytes=max_bytes):
        if not line.strip():
            continue
        row = json.loads(line)
        for key in ("row_id", "trace_id"):
            if isinstance(row.get(key), str):
                labels[row[key]] = row
    return labels


def ingest_goodfire(
    writer: ExpansionWriter,
    *,
    base: str,
    runs: Sequence[str],
    limit: int | None = None,
    max_bytes: int = 8 << 30,
    labels_bytes: int = 256 << 20,
) -> dict[str, Any]:
    """Ingest one pinned judge-labelled Goodfire run at a time."""
    source = "goodfire"
    per_run: dict[str, Any] = {}
    for run in runs:
        labels = _goodfire_labels(base, run, max_bytes=labels_bytes)
        url = f"{base}/{run}/traces.jsonl"
        written = 0
        seen = 0
        verdicts: Counter[str] = Counter()
        for line in _http_lines(url, max_bytes=max_bytes):
            if not line.strip():
                continue
            seen += 1
            if limit is not None and written >= limit:
                break
            row = json.loads(line)
            traces = row.get("traces") or []
            if not traces or not isinstance(traces[0], Mapping):
                writer.exclusions["goodfire_row_without_trace"] += 1
                continue
            trace_doc = traces[0]
            env = str((row.get("env") or {}).get("id") or run.split("-")[0])
            native = trace_doc.get("task") or {}
            data = native.get("data") or {}
            name = str(data.get("name") or (trace_doc.get("info") or {}).get("swebench", {}).get(
                "instance_id") or "")
            if not name:
                writer.exclusions["goodfire_row_without_task_identity"] += 1
                continue
            task_id = f"{env}/{name}"
            if not writer.admit(source, task_id):
                continue
            label = labels.get(str(trace_doc.get("id"))) or labels.get(str(row.get("id"))) or {}
            verdict = str(label.get("verdict") or "unjudged")
            verdicts[verdict] += 1
            instruction = str(data.get("prompt") or "")
            writer.task_if_absent(source, _task_row(
                task_id,
                dataset_id=GOODFIRE_DATASET,
                split=PUBLIC_SPLIT,
                instruction=instruction,
                metadata={
                    "row_kind": "public_rollout_task",
                    "instruction_source": "traces[0].task.data.prompt" if instruction else "unavailable",
                    "environment": env,
                    "run": run,
                    "revision": GOODFIRE_REVISION,
                    "native_task_type": native.get("type"),
                },
            ))
            nodes = trace_doc.get("nodes") or []
            steps, stats = project_goodfire_nodes(nodes, limits=writer.limits)
            agent = trace_doc.get("agent") or {}
            metrics = trace_doc.get("metrics") or {}
            rewards = trace_doc.get("rewards") or {}
            resolved = rewards.get("resolved") if isinstance(rewards, Mapping) else None
            reward = resolved.get("score") if isinstance(resolved, Mapping) else metrics.get("resolved")
            raw = line if isinstance(line, bytes) else line.encode("utf-8")
            locator = f"{run}/traces.jsonl#{row.get('id')}"
            cas_uri = writer.store_raw(raw, locator=locator, content_type="application/json")
            trace = {
                "trace_id": f"{GOODFIRE_DATASET}:{run}:{trace_doc.get('id') or row.get('id')}",
                "run_id": f"{GOODFIRE_DATASET}:{run}",
                "task_id": task_id,
                "model_id": str(agent.get("model") or "unknown"),
                "outcome": f"source_label:{verdict}",
                "reward": _number(reward),
                "is_exploit": verdict == "yes",
                "is_task_defect_fail": False,
                "step_count": len(steps),
                "raw_cas_uri": cas_uri,
                "verified_receipt_json": _provenance(
                    dataset=GOODFIRE_DATASET,
                    locator=locator,
                    revision=GOODFIRE_REVISION,
                    raw_sha256=_digest(raw),
                    steps_projected=len(steps),
                    stats=stats,
                    labels={
                        "judge_verdict": verdict,
                        "judge_verdict_enacted": label.get("verdict_enacted"),
                        "judge_votes": {
                            key: label.get(key)
                            for key in ("n_passages", "n_passages_positive", "n_passages_ambiguous")
                        },
                        "judge_instance_id": label.get("judge_instance_id"),
                        "harness": agent.get("harness"),
                        "stop_condition": trace_doc.get("stop_condition"),
                    },
                    limits=writer.limits,
                ),
            }
            if writer.trace(source, trace, steps):
                written += 1
        per_run[run] = {"rows_streamed": seen, "traces_ingested": written,
                        "verdicts": dict(sorted(verdicts.items()))}
    writer.provenance[source] = {"base": base, "revision": GOODFIRE_REVISION, "runs": per_run}
    return writer.provenance[source]


def _wrench_rows(index_paths: Mapping[str, Path]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Read the publisher's index files without pandas; one dict per row."""
    if set(index_paths) != set(WRENCH_INDEX_SPLITS):
        raise ValueError(f"Terminal Wrench index needs exactly {sorted(WRENCH_INDEX_SPLITS)}")
    for split, path in index_paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing Terminal Wrench index split {split}: {path}")
        for batch in pyarrow.parquet.ParquetFile(path).iter_batches():
            for row in batch.to_pylist():
                yield split, row


def ingest_terminal_wrench(
    writer: ExpansionWriter,
    *,
    index_paths: Mapping[str, Path],
    payload_base: str = WRENCH_PAYLOAD_BASE,
    limit: int | None = None,
    workers: int = 8,
    max_bytes: int = 64 << 20,
) -> dict[str, Any]:
    """Ingest Terminal Wrench index rows plus their publisher trajectory bodies."""
    source = "terminal-wrench"
    rows = []
    for _split, row in _wrench_rows(index_paths):
        if row.get("trajectory_exists") is not True:
            writer.exclusions["wrench_row_without_trajectory"] += 1
            continue
        if not row.get("trajectory_path"):
            writer.exclusions["wrench_row_without_trajectory_path"] += 1
            continue
        rows.append(row)
    rows = [
        row for row in rows
        if writer.admit(source, f"{TERMINAL_WRENCH_DATASET}/{row['task_id']}")
    ]
    rows.sort(key=lambda row: (str(row.get("task_id")), str(row.get("model")),
                               str(row.get("variant")), str(row.get("trajectory_label"))))
    if limit is not None:
        rows = rows[:limit]

    def fetch(row: Mapping[str, Any]) -> bytes | None:
        url = f"{payload_base}/{row['trajectory_path']}"
        try:
            return _http_bytes(url, max_bytes=max_bytes)
        except Exception:  # noqa: BLE001 - a missing body is a counted exclusion, not a crash
            return None

    classifications: Counter[str] = Counter()
    written = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, raw in zip(rows, pool.map(fetch, rows), strict=True):
            if raw is None:
                writer.exclusions["wrench_trajectory_fetch_failed"] += 1
                continue
            document = json.loads(raw)
            task_id = f"{TERMINAL_WRENCH_DATASET}/{row['task_id']}"
            instruction = str(row.get("instruction") or "")
            writer.task_if_absent(source, _task_row(
                task_id,
                dataset_id=TERMINAL_WRENCH_DATASET,
                split=PUBLIC_SPLIT,
                instruction=instruction,
                metadata={
                    "row_kind": "public_benchmark_task",
                    "instruction_source": "index.instruction" if instruction else "unavailable",
                    "native_task_id": str(row.get("task_id")),
                    "description": row.get("description"),
                    "category": row.get("category"),
                    "difficulty": row.get("difficulty"),
                    "tags": row.get("tags"),
                    "source_datasets": row.get("source_datasets"),
                    "source_urls": row.get("source_urls"),
                    "payload_revision": WRENCH_PAYLOAD_REVISION,
                },
            ))
            steps, stats = project_harbor_trajectory(
                document.get("steps") or [],
                limits=writer.limits,
                key_steps=[step for step in (row.get("key_hack_steps") or []) if isinstance(step, int)],
            )
            classification = str(row.get("classification") or "unclassified")
            classifications[classification] += 1
            is_hack = bool(row.get("is_hack"))
            locator = f"{TERMINAL_WRENCH_DATASET}@{WRENCH_PAYLOAD_REVISION}/{row['trajectory_path']}"
            cas_uri = writer.store_raw(raw, locator=locator, content_type="application/json")
            reward = row.get("reward")
            trace = {
                "trace_id": (
                    f"{TERMINAL_WRENCH_DATASET}:{row['task_id']}:{row.get('model')}:"
                    f"{row.get('variant')}:{row.get('trajectory_label')}"
                ),
                "run_id": f"{TERMINAL_WRENCH_DATASET}:{row.get('run_name') or row.get('job_name')}",
                "task_id": task_id,
                "model_id": str(row.get("model") or "unknown"),
                "outcome": f"source_label:{classification}",
                "reward": _number(reward),
                "is_exploit": is_hack,
                "is_task_defect_fail": bool(row.get("task_fair") is False),
                "step_count": len(steps),
                "raw_cas_uri": cas_uri,
                "verified_receipt_json": _provenance(
                    dataset=TERMINAL_WRENCH_DATASET,
                    locator=locator,
                    revision=WRENCH_PAYLOAD_REVISION,
                    raw_sha256=_digest(raw),
                    steps_projected=len(steps),
                    stats=stats,
                    labels={
                        "classification": classification,
                        "classification_label": row.get("classification_label"),
                        "exploit_categories": row.get("exploit_categories"),
                        "targeted_categories": row.get("targeted_categories"),
                        "key_hack_steps": row.get("key_hack_steps"),
                        "serious_hack": row.get("serious_hack"),
                        "task_fair": row.get("task_fair"),
                        "brief_exploit_summary": row.get("brief_exploit_summary"),
                        "index_revision": WRENCH_INDEX_REVISION,
                        "cost_usd": row.get("cost_usd"),
                    },
                    limits=writer.limits,
                ),
            }
            if not writer.trace(source, trace, steps):
                continue
            written += 1
            if is_hack:
                writer.issue(source, {
                    "issue_id": f"{TERMINAL_WRENCH_DATASET}:{_key_digest(task_id, str(row.get('model')),
                                                                       str(row.get('variant')),
                                                                       str(row.get('trajectory_label')))}",
                    "task_id": task_id,
                    "defect_class": classification,
                    "direction": "wrong_pass",
                    "status": "provisional",
                    "witness_digest": f"sha256:{_digest(raw)}",
                    "original_reward": _number(reward),
                    "repaired_reward": None,
                    "citation_file": locator,
                    "citation_line": 1,
                    "reproducer_script": None,
                    "cas_uri": cas_uri,
                })
    writer.provenance[source] = {
        "index_files": {split: str(path) for split, path in index_paths.items()},
        "index_revision": WRENCH_INDEX_REVISION,
        "payload_base": payload_base,
        "rows_selected": len(rows),
        "traces_ingested": written,
        "classifications": dict(sorted(classifications.items())),
        "index_splits_read": list(WRENCH_INDEX_SPLITS),
        "derived_splits_skipped": ["sanitized_hack", "stripped_hack"],
    }
    return writer.provenance[source]


def ingest_simglot(writer: ExpansionWriter, path: Path) -> dict[str, Any]:
    """Ingest staged SimGlot detector/validity annotations as task-variant evidence."""
    source = "simglot"
    records = list(_jsonl(path))
    kinds: Counter[str] = Counter()
    variants: dict[tuple[str, str], dict[str, Any]] = {}
    # The publisher keeps the 192 original-matrix cells and their 528 extended
    # counterparts as separate rows sharing one opaque duplicate_group_id; the
    # six compared fields are identical, so a cell is counted once per group.
    cells: dict[str, dict[str, Any]] = {}
    for record in records:
        kind = str(record.get("record_kind") or "unknown")
        kinds[kind] += 1
        scorer = record.get("scorer_only") or {}
        native = scorer.get("native_key") or {}
        target = str(native.get("target") or "")
        # Both record shapes carry the display label ("C3 actions_not_restored");
        # only its leading code ("C3") is the stable variant join across files.
        variant = str(native.get("variant") or native.get("defect") or "").split(" ", 1)[0]
        if not target or not variant:
            continue
        key = (target, variant)
        entry = variants.setdefault(key, {"record": record, "defect_class": None, "alarms": {}})
        derived = scorer.get("derived_from_source_annotations") or {}
        canonical = derived.get("canonical_defect_class")
        if canonical and entry["defect_class"] is None:
            entry["defect_class"] = str(canonical)
        observations = scorer.get("source_observations") or {}
        detector = str(native.get("detector") or "unknown")
        if kind.endswith("detector_observation"):
            group = str(scorer.get("duplicate_group_id") or f"{target}|{variant}|{detector}")
            cells[group] = record
        if observations.get("violated") is True and kind.endswith("detector_observation"):
            entry["alarms"][group] = (detector, record)

    written_tasks = 0
    for (target, variant), entry in sorted(variants.items()):
        task_id = f"{SIMGLOT_DATASET}/{target}/{variant}"
        record = entry["record"]
        defect_class = entry["defect_class"] or "unknown"
        provenance = record.get("provenance") or {}
        if not writer.task_if_absent(source, _task_row(
            task_id,
            dataset_id=SIMGLOT_DATASET,
            split=PUBLIC_SPLIT,
            instruction=f"{target} mutant variant {variant} (canonical class {defect_class})",
            metadata={
                "row_kind": "mutant_variant_annotation_not_task_instruction",
                "instruction_source": "publisher withheld the original instruction; label is source-native",
                "native_target": target,
                "native_variant": variant,
                "canonical_defect_class": defect_class,
                "repository": provenance.get("repository"),
                "release_commit": provenance.get("release_commit"),
                "release_tag": provenance.get("release_tag"),
                "admission": record.get("admission"),
            },
        )):
            continue
        written_tasks += 1
        for detector, alarm in sorted(entry["alarms"].values(), key=lambda pair: pair[0]):
            scorer = alarm.get("scorer_only") or {}
            native = scorer.get("native_key") or {}
            writer.issue(source, {
                "issue_id": f"{SIMGLOT_DATASET}:{_key_digest(task_id, detector)}",
                "task_id": task_id,
                "defect_class": defect_class,
                "direction": "wrong_pass",
                "status": "provisional",
                "witness_digest": (
                    str(scorer["duplicate_group_id"]) if scorer.get("duplicate_group_id") else None
                ),
                "original_reward": None,
                "repaired_reward": None,
                "citation_file": str((alarm.get("provenance") or {}).get("artifact") or ""),
                "citation_line": int((alarm.get("provenance") or {}).get("locator", {}).get(
                    "data_row_1based") or 1),
                "reproducer_script": None,
                "cas_uri": None,
            })
    writer.provenance[source] = {
        "staged": str(path),
        "records_read": len(records),
        "record_kinds": dict(sorted(kinds.items())),
        "variants_registered": written_tasks,
        "detector_alarm_issues": sum(len(entry["alarms"]) for entry in variants.values()),
        "detector_observation_cells": len(cells),
        "trace_ingestion": "none: publisher withholds episode and causal-trace rows",
    }
    return writer.provenance[source]


def ingest_swebench_pro(writer: ExpansionWriter, path: Path) -> dict[str, Any]:
    """Ingest staged SWE-bench Pro Verified correction annotations."""
    source = "swebench-pro"
    records = [row for row in _jsonl(path) if row.get("record_type") == "task_correction_observation"]
    labels: Counter[str] = Counter()
    written = 0
    for record in records:
        scorer = record.get("scorer_only") or {}
        native = scorer.get("native_ref") or {}
        instance = str(native.get("instance_id") or "")
        if not instance:
            writer.exclusions["swebench_pro_row_without_instance"] += 1
            continue
        correction = scorer.get("correction_label") or {}
        label = correction.get("readme_category") or (
            "revised" if correction.get("label_present") else "unlabelled"
        )
        labels[str(label)] += 1
        task_id = f"{SWEBENCH_PRO_DATASET}/{instance}"
        if not writer.task_if_absent(source, _task_row(
            task_id,
            dataset_id=SWEBENCH_PRO_DATASET,
            split=PUBLIC_SPLIT,
            instruction=f"SWE-bench Pro Verified instance {instance}",
            metadata={
                "row_kind": "public_benchmark_task_annotation",
                "instruction_source": "publisher row not inspected for instruction text",
                "dataset": native.get("dataset"),
                "jsonl_file": native.get("jsonl_file"),
                "projection_row_index": native.get("projection_row_index"),
                "correction_label": correction,
                "admission": record.get("admission"),
            },
        )):
            continue
        written += 1
        if correction.get("label_present"):
            writer.issue(source, {
                "issue_id": f"{SWEBENCH_PRO_DATASET}:{_key_digest(task_id, str(label))}",
                "task_id": task_id,
                "defect_class": f"source_correction:{label}",
                "direction": "wrong_pass",
                "status": "provisional",
                "witness_digest": None,
                "original_reward": None,
                "repaired_reward": None,
                "citation_file": str(native.get("jsonl_file") or ""),
                "citation_line": int(native.get("projection_row_index") or 1) + 1,
                "reproducer_script": None,
                "cas_uri": None,
            })
    writer.provenance[source] = {
        "staged": str(path),
        "records_read": len(records),
        "tasks_registered": written,
        "correction_labels": dict(sorted(labels.items())),
        "trace_ingestion": "none: no episode or causal-trace rows are published",
    }
    return writer.provenance[source]


def ingest_cua_verifier(writer: ExpansionWriter, path: Path) -> dict[str, Any]:
    """Ingest staged CUA-VerifierBench reviewer/verifier disagreement annotations."""
    source = "cua-verifier"
    records = list(_jsonl(path))
    written = 0
    disagreements = 0
    for record in records:
        actor = record.get("actor_projection") or {}
        reference = str(actor.get("task_ref") or "")
        if not reference:
            writer.exclusions["cua_row_without_task_ref"] += 1
            continue
        task_id = f"{CUA_VERIFIER_DATASET}/{reference.removeprefix('sha256:')[:16]}"
        reviews = ((record.get("scorer_side") or {}).get("reviews")) or []
        if not writer.task_if_absent(source, _task_row(
            task_id,
            dataset_id=CUA_VERIFIER_DATASET,
            split=PUBLIC_SPLIT,
            instruction=f"CUA-VerifierBench trajectory {reference}",
            metadata={
                "row_kind": "public_benchmark_trajectory_annotation",
                "instruction_source": "publisher emits an opaque task reference only",
                "task_ref": reference,
                "split": record.get("split"),
                "admission": record.get("admission"),
                "reviews": len(reviews),
            },
        )):
            continue
        written += 1
        for index, review in enumerate(reviews):
            informed = review.get("informed") or {}
            blind = review.get("blind") or {}
            if informed.get("outcome_agrees_with_uv") is False:
                disagreements += 1
                writer.issue(source, {
                    "issue_id": f"{CUA_VERIFIER_DATASET}:{_key_digest(task_id, str(index), 'outcome')}",
                    "task_id": task_id,
                    "defect_class": "verifier_outcome_disagreement",
                    "direction": "wrong_pass",
                    "status": "provisional",
                    "witness_digest": f"sha256:{str(review.get('reviewer_ref') or '').removeprefix('sha256:')}",
                    "original_reward": _number(blind.get("outcome_norm")),
                    "repaired_reward": None,
                    "citation_file": str(path.name),
                    "citation_line": 1,
                    "reproducer_script": None,
                    "cas_uri": None,
                })
            if informed.get("process_agrees_with_uv") is False:
                disagreements += 1
                writer.issue(source, {
                    "issue_id": f"{CUA_VERIFIER_DATASET}:{_key_digest(task_id, str(index), 'process')}",
                    "task_id": task_id,
                    "defect_class": "verifier_process_disagreement",
                    "direction": "unfair_fail",
                    "status": "provisional",
                    "witness_digest": None,
                    "original_reward": _number(blind.get("process_score")),
                    "repaired_reward": None,
                    "citation_file": str(path.name),
                    "citation_line": 1,
                    "reproducer_script": None,
                    "cas_uri": None,
                })
    writer.provenance[source] = {
        "staged": str(path),
        "records_read": len(records),
        "tasks_registered": written,
        "disagreement_issues": disagreements,
        "trace_ingestion": "none: reviews are static verdicts on fixed trajectories",
    }
    return writer.provenance[source]


def ingest_ignition(writer: ExpansionWriter, path: Path) -> dict[str, Any]:
    """Ingest staged Ignition RL telemetry series as searchable step series."""
    source = "ignition"
    runs: dict[str, dict[str, Any]] = {}
    steps: dict[str, list[dict[str, Any]]] = {}
    records = 0
    for record in _jsonl(path):
        records += 1
        kind = record.get("record_type")
        if kind == "telemetry_run":
            reference = str((record.get("actor_view") or {}).get("trace_ref") or "")
            if reference:
                runs[reference] = record
            continue
        if kind != "telemetry_step":
            continue
        actor = record.get("actor_view") or {}
        reference = str(actor.get("trace_ref") or "")
        if not reference:
            writer.exclusions["ignition_step_without_trace_ref"] += 1
            continue
        steps.setdefault(reference, []).append(record)

    written = 0
    for reference, series in sorted(steps.items()):
        series.sort(key=lambda row: int((row.get("actor_view") or {}).get("step") or 0))
        task_id = f"{IGNITION_DATASET}/{reference}"
        run_record = runs.get(reference) or {}
        scorer = run_record.get("scorer_side") or {}
        if not writer.task_if_absent(source, _task_row(
            task_id,
            dataset_id=IGNITION_DATASET,
            split=PUBLIC_SPLIT,
            instruction=(
                f"Ignition backdoor RL training telemetry series {reference} "
                f"({len(series)} logged steps)"
            ),
            metadata={
                "row_kind": "rl_training_telemetry_series_not_agent_task",
                "instruction_source": "series label derived from the publisher's own trace_ref and step count",
                "channels_present": scorer.get("channels_present"),
                "censoring": scorer.get("censoring"),
                "admission": "held",
            },
        )):
            continue
        rows = []
        clipped = 0
        dropped = max(0, len(series) - writer.limits.max_steps)
        for position, record in enumerate(series[: writer.limits.max_steps]):
            actor = record.get("actor_view") or {}
            telemetry = json.dumps(actor.get("telemetry"), sort_keys=True, ensure_ascii=False)
            row, elided, was_clipped, _ = _bounded_step(
                position,
                "telemetry",
                action=f"step {actor.get('step')}",
                content=telemetry,
                observation=None,
                limits=writer.limits,
                flags={"series_index": (record.get("scorer_side") or {}).get("series_index")},
            )
            row["tool_name"] = "telemetry"
            clipped += was_clipped
            rows.append(row)
        trace = {
            "trace_id": f"{IGNITION_DATASET}:{reference}",
            "run_id": f"{IGNITION_DATASET}:{reference}",
            "task_id": task_id,
            "model_id": "ignition-telemetry/no-model",
            "outcome": "telemetry_series",
            "reward": None,
            "is_exploit": False,
            "is_task_defect_fail": False,
            "step_count": len(rows),
            "raw_cas_uri": None,
            "verified_receipt_json": _provenance(
                dataset=IGNITION_DATASET,
                locator=str(path),
                revision=None,
                raw_sha256=None,
                steps_projected=len(rows),
                stats={"clipped_steps": clipped, "dropped": dropped},
                labels={"trace_ref": reference, "trace_kind": "rl_training_telemetry_not_agent_actions"},
                limits=writer.limits,
            ),
        }
        if writer.trace(source, trace, rows):
            written += 1
    writer.provenance[source] = {
        "staged": str(path),
        "records_read": records,
        "runs": len(runs),
        "series_ingested": written,
        "steps_ingested": sum(len(value) for value in steps.values()),
        "trace_kind": "rl_training_telemetry_not_agent_actions",
    }
    return writer.provenance[source]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="live SQLite path or PostgreSQL URL")
    parser.add_argument("--cas", help="local CAS directory for raw public rows")
    parser.add_argument("--output", required=True, help="receipt JSON path")
    parser.add_argument("--tb4-hub-index", help="tb4-hub trajectory-index.jsonl")
    parser.add_argument("--tb4-hub-limit", type=int)
    parser.add_argument("--goodfire-base", default=GOODFIRE_BASE)
    parser.add_argument("--goodfire-run", action="append", default=[])
    parser.add_argument("--goodfire-limit", type=int)
    parser.add_argument(
        "--wrench-index",
        action="append",
        default=[],
        metavar="SPLIT=PATH",
        help="one Terminal Wrench index file; give hack, non_hack, and baseline",
    )
    parser.add_argument(
        "--wrench-index-dir",
        help="directory holding one index file per split, named for the split",
    )
    parser.add_argument("--wrench-limit", type=int)
    parser.add_argument("--wrench-workers", type=int, default=8)
    parser.add_argument("--stage-dir", help="directory holding staged library loader JSONL")
    parser.add_argument("--max-content-chars", type=int, default=Limits.content_chars)
    parser.add_argument("--max-observation-chars", type=int, default=Limits.observation_chars)
    parser.add_argument("--max-steps", type=int, default=Limits.max_steps)
    return parser


def _index_arguments(values: Sequence[str], directory: str | None = None) -> dict[str, Path]:
    """Resolve the adapter's explicit split-to-file mapping from either flag form."""
    parsed: dict[str, Path] = {}
    for value in values:
        split, separator, path = value.partition("=")
        if not separator or not split or not path:
            raise SystemExit(f"--wrench-index expects SPLIT=PATH, got {value!r}")
        parsed[split] = Path(path)
    if directory is not None:
        for candidate in sorted(Path(directory).iterdir()):
            if candidate.stem in WRENCH_INDEX_SPLITS and candidate.is_file():
                parsed.setdefault(candidate.stem, candidate)
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    limits = Limits(
        content_chars=arguments.max_content_chars,
        observation_chars=arguments.max_observation_chars,
        max_steps=arguments.max_steps,
    )
    cas = S3CompatibleCAS(local_directory=arguments.cas) if arguments.cas else None
    with CloudTraceDatabase(arguments.database) as database:
        writer = ExpansionWriter(
            database, guard=SealGuard(), cas=cas, limits=limits, label=arguments.database,
        )
        sources: dict[str, Any] = {}
        if arguments.tb4_hub_index:
            sources["tb4-hub"] = ingest_tb4_hub(
                writer, Path(arguments.tb4_hub_index), limit=arguments.tb4_hub_limit,
            )
        if arguments.goodfire_run:
            sources["goodfire"] = ingest_goodfire(
                writer,
                base=arguments.goodfire_base,
                runs=arguments.goodfire_run,
                limit=arguments.goodfire_limit,
            )
        if arguments.wrench_index or arguments.wrench_index_dir:
            sources["terminal-wrench"] = ingest_terminal_wrench(
                writer,
                index_paths=_index_arguments(
                    arguments.wrench_index, arguments.wrench_index_dir,
                ),
                limit=arguments.wrench_limit,
                workers=arguments.wrench_workers,
            )
        if arguments.stage_dir:
            stage = Path(arguments.stage_dir)
            for name, function in (
                ("simglot", ingest_simglot),
                ("pro", ingest_swebench_pro),
                ("cua", ingest_cua_verifier),
                ("ignition", ingest_ignition),
            ):
                path = stage / f"{name}.jsonl"
                if path.is_file():
                    sources[name] = function(writer, path)
                else:
                    writer.exclusions[f"staged_missing:{name}"] += 1
        receipt = writer.receipt(sources=sources)
        Path(arguments.output).write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    print(json.dumps(receipt["row_counts_added"], sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
