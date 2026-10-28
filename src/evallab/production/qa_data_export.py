"""Admission-first blinded exports and reward-free joins between original and exported steps."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_export import export_runs
from evallab.production.qa_data import UNAVAILABLE, QaContext, QaDataError

_MAX_RUNS = 200


def _manifest(path: Path) -> dict[str, dict[str, Any]]:
    """Read only the manifest's identity/step join; rewards never leave this module."""
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise QaDataError("cannot read sealed manifest") from exc
    if not isinstance(value, dict) or any(
        not isinstance(blind_id, str) or not isinstance(entry, dict)
        or not isinstance(entry.get("trace_id"), str)
        or not isinstance(entry.get("step_origin"), list)
        or any(type(step) is not int for step in entry["step_origin"])
        for blind_id, entry in value.items()
    ):
        raise QaDataError("invalid sealed manifest")
    return {
        blind_id: {
            "trace_id": entry["trace_id"], "step_origin": entry["step_origin"],
            "actor_inputs_available": entry.get("actor_inputs_available"),
        }
        for blind_id, entry in value.items()
    }


def steps(
    manifest: Path, trace: str, *, original: int | None, blinded: int | None,
) -> dict[str, Any]:
    """Map original step ids to one-based exported positions, without revealing sealed rewards."""
    if original is not None and blinded is not None:
        raise QaDataError("give only one of --original and --blinded")
    entries = _manifest(manifest)
    blind_id = trace if trace in entries else next(
        (key for key, entry in entries.items() if entry["trace_id"] == trace), None,
    )
    if blind_id is None:
        raise QaDataError("not in this manifest")
    origins = entries[blind_id]["step_origin"]
    result: dict[str, Any] = {"blind_id": blind_id, "steps": len(origins)}
    if original is not None:
        result.update(original=original, blinded=origins.index(original) + 1 if original in origins else None)
    if blinded is not None:
        result.update(blinded=blinded, original=origins[blinded - 1] if 1 <= blinded <= len(origins) else None)
    return result


def _evidence(ctx: QaContext, ids: Sequence[str]) -> dict[str, list[int]]:
    """Admission, current trace ownership and facet citations share one read snapshot."""
    with ctx.database._transaction():
        admitted, _sealed = ctx.retriever._admitted_ids()
        selected = json.dumps(ids)
        owners = ctx.database._rows(
            "SELECT trace_id, task_id FROM agent_traces WHERE trace_id IN (SELECT value FROM json_each(?))",
            (selected,),
        )
        if len(owners) != len(ids) or any(row["task_id"] not in admitted for row in owners):
            raise QaDataError(UNAVAILABLE)
        rows = ctx.database._rows(
            "SELECT f.run_id, f.evidence_steps FROM run_facets f"
            " JOIN agent_traces a ON a.trace_id = f.run_id AND a.task_id = f.task_id"
            " WHERE a.task_id IN (SELECT value FROM json_each(?))"
            " AND a.trace_id IN (SELECT value FROM json_each(?))",
            (json.dumps(admitted), selected),
        )
    return {row["run_id"]: row["evidence_steps"] for row in rows}


def export(
    ctx: QaContext, *, facets: dict[str, Any], trace_ids: Sequence[str], ids_file: Path | None,
    limit: int | None, format: str, out: Path,
) -> dict[str, Any]:
    """Publish up to 200 blinded runs; budget omissions affect the reply, never the export."""
    explicit = bool(trace_ids) or ids_file is not None
    if bool(facets) == explicit:
        raise QaDataError("give either --facet or --trace-id/--ids-file, not both or neither")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= _MAX_RUNS):
        raise QaDataError(f"--limit must be between 1 and {_MAX_RUNS}")
    database_path = ctx.database_path
    if database_path is None or ctx.database._postgres or database_path == ":memory:":
        raise QaDataError("export requires a SQLite file store")
    if database_path.startswith("sqlite:///"):
        database_path = database_path[len("sqlite:///"):]
    if format not in {"atif", "harbor", "scout"}:
        raise QaDataError("format must be atif, harbor or scout")
    if facets:
        try:
            rows = ctx.database.search_runs(
                facets, registry=ctx.retriever.registry, sealed_task_ids=ctx.retriever._sealed_ids, limit=None,
            )
        except ValueError as exc:
            raise QaDataError(str(exc)) from exc
        ids = [row["run_id"] for row in rows]
    else:
        ids = list(trace_ids)
        if ids_file is not None:
            try:
                lines = ids_file.expanduser().read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise QaDataError("cannot read ids file") from exc
            ids.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))
        # Validate every requested id, including those beyond the limit: one bad id refuses everything.
        for trace_id in ids:
            ctx.task_of_run(trace_id)
    requested_ids = list(dict.fromkeys(ids))
    ids = requested_ids[:limit] if limit is not None else requested_ids
    if not ids:
        raise QaDataError("no admitted runs selected")
    if len(ids) > _MAX_RUNS:
        raise QaDataError(f"export selects more than {_MAX_RUNS} runs; narrow the selection or give --limit")
    evidence = _evidence(ctx, ids)
    output = out.expanduser()
    if not output.name:
        raise QaDataError("output directory and sealed manifest must both be new; refusing overwrite")
    manifest_path = output.with_name(output.name + ".sealed-manifest.json")
    if any(path.exists() or path.is_symlink() for path in (output, manifest_path)):
        raise QaDataError("output directory and sealed manifest must both be new; refusing overwrite")
    output = output.parent.resolve() / output.name
    manifest_path = manifest_path.parent.resolve() / manifest_path.name
    result: dict[str, Any] = {
        "output_directory": str(output), "sealed_manifest": str(manifest_path),
        "receipt": {
            "admitted": len(requested_ids) if explicit else len(ids),
            "exported": len(ids), "refused": 0, "format": format,
            "manifest_sha256": "0" * 64,
        },
        "runs": [], "omitted": len(ids), "truncated": False,
    }
    if format != "atif":
        result["step_map"] = "atif only"
    # Reserve all fixed keys, worst-case omission digits and the list's separators before publishing.
    if not ctx.budget.take(result) or not ctx.budget.take("," * (len(ids) - 1)):
        raise QaDataError("the export receipt does not fit; raise --budget")
    try:
        receipt = export_runs(
            database_path, output, format=format, registry=ctx.retriever.registry,
            trace_ids=requested_ids if explicit else ids, limit=limit if explicit else None,
            manifest=manifest_path, strict=True,
        )
    except ValueError as exc:
        raise QaDataError(
            UNAVAILABLE if str(exc) == "requested traces are missing or not admitted" else str(exc),
        ) from exc
    result["receipt"] = {key: receipt[key] for key in result["receipt"]}
    by_trace = {
        entry["trace_id"]: (blind_id, entry["step_origin"], entry["actor_inputs_available"])
        for blind_id, entry in _manifest(manifest_path).items()
    }
    taken = []
    for trace_id in ids:
        blind_id, origins, actor_inputs = by_trace[trace_id]
        # False means no model actor to read (replay receipts, event journals): unknown, not clean.
        item: dict[str, Any] = {"trace_id": trace_id, "blind_id": blind_id, "actor_inputs_available": actor_inputs}
        if format == "atif":
            cited = evidence.get(trace_id, [])
            positions = {original: index for index, original in enumerate(origins, start=1)}
            item.update(
                file=str(output / f"{blind_id}.json"), steps=len(origins),
                evidence_steps_original=cited, evidence_steps_blinded=[positions.get(step) for step in cited],
            )
        else:
            item["path"] = str(output / blind_id if format == "harbor" else output / "transcripts.jsonl")
        if ctx.budget.take(item):
            taken.append(item)
    result.update(runs=taken, omitted=len(ids) - len(taken), truncated=len(taken) < len(ids))
    return result
