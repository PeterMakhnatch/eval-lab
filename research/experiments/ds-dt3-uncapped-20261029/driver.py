"""DT-3: read CheatBench DEV runs twice — production capped text vs uncapped text.

The only changed variable between the two arms is the importer bound in
``evallab.cheatbench`` (``MSG_CAP``/``OBS_CAP`` on vs off). Everything else is the
production Run Atlas path: ``blind_run``, ``render_segments``/``plan_chunks`` at the
production ``CHUNK_CHARS``, ``READER_SYSTEM``/``MAP_INSTRUCTIONS``/``FORM_INSTRUCTIONS``,
``parse_form`` and ``validate_record``.

Reader: headless ``omp -p --model deepseek/deepseek-flash`` (fallback off in the squad
config). The served provider/model is read back from the ``agent_end`` event of the
JSON stream and, for the batch, from the sessions written under ``--session-dir``. A
call served by any other model is a failed call.

Usage::

    uv run --project . python driver.py census --out census.json
    uv run --project . python driver.py read --arm capped --out readings-capped.jsonl
    uv run --project . python driver.py score --capped ... --uncapped ... --out result.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import inspect
import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.cheatbench import convert
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_atlas import (
    CHUNK_CHARS,
    AtlasRun,
    Completion,
    FacetExtractor,
    TransportError,
    blind_run,
    plan_chunks,
    render_segments,
)
from evallab.results_home import results_root

DATASET = "cheatbench-actions-v1"
ROUTE = "deepseek/deepseek-flash"
MARK = "\u2026[truncated]"
PARQUET = Path("/tmp/dt3/cb/full.parquet")
CONFIG = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/worker.yml"
)
SESSIONS = Path("/tmp/dt3/sessions")
MAX_TOKENS_ESTIMATE = 700_000
CHARS_PER_TOKEN = 3.6
PRICE_IN, PRICE_CACHED, PRICE_OUT = 0.30 / 1e6, 0.006 / 1e6, 1.20 / 1e6


def repo_root() -> Path:
    return Path(os.environ.get("EVALLAB_ROOT", Path.cwd()))


# --------------------------------------------------------------------------- cohort


def cohort() -> list[dict[str, Any]]:
    """DEV items whose production capped document carries the importer cap marker.

    Ordered by ``sha256(item_id)`` — a partial read is a prefix, never a choice.
    """
    registry = CorpusRegistry(repo_root())
    root = results_root()
    rows: list[dict[str, Any]] = []
    for item in registry.items(dataset_id=DATASET, split="dev"):
        reference = (item.metadata.get("trial_ref") or {}).get("path")
        if not reference:
            continue
        body = (root / reference / "agent" / "trajectory.json").read_text(encoding="utf-8")
        doc = json.loads(body)
        capped_fields = 0
        for step in doc.get("steps") or []:
            if MARK in (step.get("message") or ""):
                capped_fields += 1
            for result in (step.get("observation") or {}).get("results") or []:
                if MARK in (result.get("content") or ""):
                    capped_fields += 1
        rows.append(
            {
                "item_id": item.item_id,
                "trace_id": item.metadata.get("source_id"),
                "doc_item_id": doc["metadata"]["item_id"],
                "task_id": item.task_id,
                "capped_fields": capped_fields,
                "capped_chars": len(body),
                "doc": doc if capped_fields else None,
            }
        )
    selected = [row for row in rows if row["capped_fields"]]
    selected.sort(key=lambda row: hashlib.sha256(row["item_id"].encode()).hexdigest())
    return selected


def raw_rows(wanted: set[str], path: Path = PARQUET) -> dict[str, dict]:
    """The parquet rows for ``wanted`` trace ids, without materializing the whole file."""
    found: dict[str, dict] = {}
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=64, columns=["trace_id", "trace", "benchmark"]):
        for row in batch.to_pylist():
            trace_id = str(row.get("trace_id"))
            if trace_id in wanted and trace_id not in found:
                found[trace_id] = row
    return found


def meta_of(row: Mapping[str, Any]) -> dict:
    return {"model": row.get("model") or "", "benchmark": row.get("benchmark") or ""}


# ----------------------------------------------------------------- rendered sizes


def rendered(arm: str, doc: Mapping[str, Any]) -> tuple[int, int, list[int]]:
    """(rendered chars, chunk count, chars per chunk) for one blind document."""
    run = AtlasRun(run_id="size", source=DATASET, split="DEV", outcome="unknown", doc=doc)
    blind = blind_run(run)
    segments = render_segments(blind, chunk_chars=CHUNK_CHARS)
    chunks = plan_chunks(segments, chunk_chars=CHUNK_CHARS)
    sizes = [sum(len(segment.text) + 2 for segment in chunk) for chunk in chunks]
    return sum(sizes), len(chunks), sizes


# ------------------------------------------------------------------------ transport


def _stream_receipt(stdout: str) -> dict[str, Any]:
    end = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "agent_end":
            end = event
    if not isinstance(end, dict) or not isinstance(end.get("messages"), list):
        return {"served": None, "text": "", "usage": {}, "stop_reason": None, "error": None}
    replies = [m for m in end["messages"] if isinstance(m, dict) and m.get("role") == "assistant"]
    if not replies:
        return {"served": None, "text": "", "usage": {}, "stop_reason": None, "error": None}
    reply = replies[0]
    content = reply.get("content")
    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = content if isinstance(content, str) else ""
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    return {
        "served": f"{reply.get('provider')}/{reply.get('model')}",
        "text": text,
        "usage": usage,
        "stop_reason": reply.get("stopReason"),
        "error": reply.get("errorMessage"),
    }


def prompt_of(messages: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    """(system, single-turn prompt). Repair turns are appended as a labelled tail."""
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    user = next((m["content"] for m in messages if m.get("role") == "user"), "")
    tail = []
    rest = list(messages)[2:] if len(messages) > 2 else []
    for message in rest:
        role = "Your previous reply" if message.get("role") == "assistant" else "Follow-up"
        tail.append(f"\n\n---\n{role}:\n{message.get('content', '')}")
    return system, user + "".join(tail)


class OmpTransport:
    """One headless ``omp -p`` call per request, attested from the JSON stream."""

    def __init__(
        self,
        *,
        config: Path = CONFIG,
        session_dir: Path = SESSIONS,
        timeout_seconds: float = 1200.0,
    ) -> None:
        self.config = config
        self.session_dir = session_dir
        self.timeout_seconds = timeout_seconds
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.receipts: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def complete(
        self,
        model: str,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
    ) -> Completion:
        system, prompt = prompt_of(messages)
        argv = [
            "omp", "-p", "--mode", "json", "--no-tools", "--no-lsp", "--no-extensions",
            "--no-skills", "--no-rules", "--no-title", "--no-pty",
            "--config", str(self.config), "--model", model,
            "--system-prompt", system, "--session-dir", str(self.session_dir),
            "--thinking", "low",
        ]  # fmt: skip
        started = time.monotonic()
        try:
            process = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd="/tmp/dt3",
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(
                f"omp timeout after {self.timeout_seconds}s", retryable=True
            ) from exc
        elapsed = round(time.monotonic() - started, 3)
        receipt = _stream_receipt(process.stdout or "")
        with self._lock:
            self.receipts.append(
                {
                    "requested_model": model,
                    "served": receipt["served"],
                    "prompt_chars": len(prompt) + len(system),
                    "usage": receipt["usage"],
                    "stop_reason": receipt["stop_reason"],
                    "error": receipt["error"],
                    "returncode": process.returncode,
                    "elapsed_seconds": elapsed,
                    "max_tokens": max_tokens,
                }
            )
        if process.returncode != 0:
            raise TransportError(
                f"omp exited {process.returncode}: {(process.stderr or '').strip()[-300:]}",
                retryable=True,
            )
        if receipt["served"] != model:
            raise TransportError(
                f"served {receipt['served']!r}, requested {model!r}", retryable=True
            )
        if not receipt["text"].strip():
            raise TransportError(f"no assistant text ({receipt['stop_reason']})", retryable=True)
        usage = receipt["usage"]
        fresh = int(usage.get("input") or 0)
        cached = int(usage.get("cacheRead") or 0)
        written = int(usage.get("cacheWrite") or 0)
        return Completion(
            text=receipt["text"],
            served_model=receipt["served"],
            input_tokens=fresh + cached + written,
            output_tokens=int(usage.get("output") or 0),
            latency_seconds=elapsed,
        )


# ---------------------------------------------------------------------------- read


def read_arm(
    arm: str, rows: list[dict[str, Any]], *, workers: int, out: Path, max_usd: float
) -> dict[str, Any]:
    extractor = FacetExtractor(OmpTransport())
    spent = {"usd": 0.0, "in": 0, "out": 0}
    lock = threading.Lock()
    stop = threading.Event()
    written: set[str] = set()

    def one(row: dict[str, Any]) -> dict[str, Any]:
        if stop.is_set():
            return {"run_id": row["item_id"], "arm": arm, "status": "budget_stop"}
        doc = row["doc"] if arm == "capped" else row["uncapped_doc"]
        run = AtlasRun(
            run_id=row["item_id"], source=DATASET, split="DEV", outcome="unknown", doc=doc
        )
        outcome = extractor.extract(run, ROUTE)
        tokens_in = sum(call.get("input_tokens", 0) for call in outcome.calls)
        tokens_out = sum(call.get("output_tokens", 0) for call in outcome.calls)
        usd = tokens_in * PRICE_IN + tokens_out * PRICE_OUT
        with lock:
            spent["usd"] += usd
            spent["in"] += tokens_in
            spent["out"] += tokens_out
            total = spent["usd"]
        record = {
            "run_id": row["item_id"],
            "arm": arm,
            "status": "ok" if outcome.record else "failed",
            "error": outcome.error,
            "record": outcome.record,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "chunks": sum(
                1 for call in outcome.calls if str(call.get("purpose", "")).startswith("map")
            ),
            "calls": [c for c in outcome.calls if c.get("status") == "ok"],
        }
        with lock:
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            written.add(row["item_id"])
            if total >= max_usd:
                stop.set()
        return record

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for record in pool.map(one, rows):
            results.append({k: record.get(k) for k in ("run_id", "status", "error")})
            print(json.dumps(results[-1]), flush=True)
    ok = sum(1 for r in results if r["status"] == "ok")
    return {
        "arm": arm,
        "attempted": len(results),
        "ok": ok,
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "budget_stop": sum(1 for r in results if r["status"] == "budget_stop"),
        "input_tokens": spent["in"],
        "output_tokens": spent["out"],
        "usd_upper": round(spent["usd"], 4),
    }


# ---------------------------------------------------------------------- attestation


def attestation(session_dir: Path, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Served routes per call, from the JSON streams and from the session logs."""
    sessions = sorted(session_dir.rglob("*.json")) + sorted(session_dir.rglob("*.jsonl"))
    observed: dict[str, int] = {}
    files = 0
    for path in sessions:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            stack = [event]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if node.get("role") == "assistant" and node.get("model"):
                        key = f"{node.get('provider')}/{node.get('model')}"
                        observed[key] = observed.get(key, 0) + 1
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
    stream = {}
    for receipt in receipts:
        stream[receipt["served"]] = stream.get(receipt["served"], 0) + 1
    return {
        "requested_model": ROUTE,
        "stream_served": stream,
        "session_served": observed,
        "session_files": files,
        "calls": len(receipts),
        "all_calls_attested": bool(receipts) and set(stream) == {ROUTE},
        "sessions_all_attested": bool(observed) and set(observed) == {ROUTE},
    }


def census(rows: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    wanted = {row["trace_id"] for row in rows}
    rows_by_id = raw_rows(wanted)
    entries = []
    for row in rows:
        source = rows_by_id[row["trace_id"]]
        trace = json.loads(source["trace"])
        doc, _ = convert(row["trace_id"], trace, meta_of(source), caps=False)
        reproduced, _ = convert(row["trace_id"], trace, meta_of(source), caps=True)
        capped_chars, capped_chunks, _ = rendered("capped", row["doc"])
        full_chars, full_chunks, sizes = rendered("uncapped", doc)
        entries.append(
            {
                "item_id": row["item_id"],
                "trace_id": row["trace_id"],
                "capped_fields": row["capped_fields"],
                "capped_doc_chars": row["capped_chars"],
                "uncapped_doc_chars": len(json.dumps(doc)),
                "capped_rendered_chars": capped_chars,
                "uncapped_rendered_chars": full_chars,
                "delta_chars": full_chars - capped_chars,
                "capped_chunks": capped_chunks,
                "uncapped_chunks": full_chunks,
                "uncapped_chunk_sizes": sizes,
                "uncapped_tokens_est": round(full_chars / CHARS_PER_TOKEN),
                "capped_reproduced": reproduced == row["doc"],
                "skipped": full_chars / CHARS_PER_TOKEN > MAX_TOKENS_ESTIMATE,
            }
        )
    keep = [entry for entry in entries if not entry["skipped"]]
    summary = {
        "cohort": len(entries),
        "skipped": len(entries) - len(keep),
        "capped_chars": sum(e["capped_rendered_chars"] for e in keep),
        "uncapped_chars": sum(e["uncapped_rendered_chars"] for e in keep),
        "capped_tokens_est": round(sum(e["capped_rendered_chars"] for e in keep) / CHARS_PER_TOKEN),
        "uncapped_tokens_est": round(
            sum(e["uncapped_rendered_chars"] for e in keep) / CHARS_PER_TOKEN
        ),
        "max_uncapped_tokens_est": max(e["uncapped_tokens_est"] for e in entries),
        "capped_reproduced": sum(1 for e in entries if e["capped_reproduced"]),
        "entries": entries,
    }
    out.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {k: v for k, v in summary.items() if k != "entries"}


def require_uncapped_importer() -> None:
    """Fail closed when the importer predates the opt-in uncapped mode (PR #1633)."""
    if "caps" not in inspect.signature(convert).parameters:
        raise SystemExit(
            "this study needs convert(..., caps=...): land the cheatbench uncapped import "
            "(PR #1633) before running it"
        )


def main() -> int:
    require_uncapped_importer()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    census_p = sub.add_parser("census")
    census_p.add_argument("--out", type=Path, required=True)
    census_p.add_argument("--docs-out", type=Path)
    read_p = sub.add_parser("read")
    read_p.add_argument("--arm", choices=("capped", "uncapped"), required=True)
    read_p.add_argument("--out", type=Path, required=True)
    read_p.add_argument("--census", type=Path)
    read_p.add_argument("--workers", type=int, default=6)
    read_p.add_argument("--limit", type=int)
    read_p.add_argument("--max-usd", type=float, default=1.8)
    read_p.add_argument("--session-dir", type=Path, default=SESSIONS)
    args = parser.parse_args()

    rows = cohort()
    if args.command == "census":
        summary = census(rows, args.out)
        print(json.dumps(summary, indent=1, sort_keys=True))
        return 0

    if args.census is not None:
        keep = {
            entry["item_id"]
            for entry in json.loads(args.census.read_text())["entries"]
            if not entry["skipped"]
        }
        rows = [row for row in rows if row["item_id"] in keep]
    if args.limit:
        rows = rows[: args.limit]
    if args.arm == "uncapped":
        wanted = {row["trace_id"] for row in rows}
        raw = raw_rows(wanted)
        for row in rows:
            source = raw[row["trace_id"]]
            row["uncapped_doc"], _ = convert(
                row["trace_id"], json.loads(source["trace"]), meta_of(source), caps=False
            )
    summary = read_arm(args.arm, rows, workers=args.workers, out=args.out, max_usd=args.max_usd)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
