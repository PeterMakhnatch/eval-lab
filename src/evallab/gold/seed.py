"""Derived seed rows for IssueBank; raw evidence is read-only."""
from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evallab.failure_library.bank import BankContext, ExposureEvent, Provenance, new_item
from evallab.failure_library.store import _atomic_write
from evallab.production.run_atlas import connect_readonly
from evallab.trace_anatomy import load_cloud_run
from evallab.trajectory_ir import SourceEventRef, build_trajectory_ir


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _bound(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if _sha(data) != expected:
        raise ValueError(f"Changed source evidence: {path}")
    return data


def _view(ctx: BankContext, source: str, run_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    build_trajectory_ir(doc)
    data = json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n"
    digest = _sha(data.encode())
    path = ctx.state_root / "bank" / "trajectories" / source / f"{digest}.json"
    if not path.exists():
        _atomic_write(path, data)
    return {"trajectory_available": True, "trajectory_ref": str(path),
            "trajectory_digest": digest, "actor_transcript": True}


def _common(ctx: BankContext, source: str, run_id: str, task_id: str,
            state: str, label: str, digest: str, ref: str, who: str, how: str) -> dict[str, Any]:
    split = ctx.strict_split("dev", item_id=run_id, task_id=task_id)
    if split != "DEV":
        raise ValueError(f"Seed row is not admitted DEV: {run_id} ({split})")
    return {
        "source": source, "subject_id": run_id, "subject_type": "run", "task_id": task_id,
        "kind": "cheat", "label_state": state,
        "polarity": {"executed-positive": "positive", "reviewed-control": "negative"}.get(state),
        "label": label, "granularity": "run", "declared_split": "dev", "split": split,
        "provenance": Provenance(store=source, ref=ref, digest=digest,
                                 who=who, how=how, version=source),
        "exposure": "exposed", "exposure_events": (ExposureEvent(
            consumer="eval-lab", purpose="DEV action review and detector evaluation",
            at="2026-11-10", artifact_sha256=digest,
        ),),
    }


def goodfire(ctx: BankContext):
    source = "goodfire-action-v1"
    path = ctx.repo_root / "research/experiments/ev1105-gfpolicy/goodfire-action-v1.jsonl"
    digest = _sha(path.read_bytes())
    with connect_readonly(ctx.cloud_db) as db:
        for number, row in enumerate(_rows(path), 1):
            run_id = row["id"]
            identity = db.execute(
                "SELECT a.task_id,c.split,c.cas_uri,c.original_grader_digest FROM agent_traces a"
                " JOIN corpus_tasks c USING(task_id) WHERE a.trace_id=?", (run_id,),
            ).fetchone()
            if identity is None or str(identity["split"]).lower() != "dev":
                raise ValueError(f"Goodfire source identity unavailable or sealed: {run_id}")
            state = ("executed-positive" if row["is_cheat"] is True else "reviewed-control"
                     if row["is_cheat"] is False else "attempted"
                     if row["label"] == "considered_only" else "unjudged")
            fields = _common(ctx, source, run_id, identity["task_id"], state, row["label"],
                             digest, f"{path}:{number}", "R1/R2 model-reader consensus",
                             "primary executed-action review; publisher controls reviewed separately")
            loaded = load_cloud_run(run_id, database=ctx.cloud_db, registry=ctx.registry,
                                    connection=db, include_branches=True)
            doc = dict(loaded.doc)
            refs = {r.source_event_id: r for r in build_trajectory_ir(doc).source_event_map}
            locators = tuple(refs[str(step)] for step in row["shortcut_steps"])
            fields.update(_view(ctx, source, run_id, doc))
            yield new_item(**fields, step_locators=locators, mechanism=row["mechanism"],
                           label_type="consensus", license="none-declared; internal only; no rehosting",
                           task_ref=identity["cas_uri"], task_available=None,
                           grader_digest=identity["original_grader_digest"], grader_available=None,
                           metadata={k: v for k, v in row.items() if k != "shortcut_steps"})


def _impossible_map(view: dict[str, Any], directory: Path, receipt: dict[str, Any],
                    inputs: dict[str, str]) -> dict[str, Any]:
    """Replay the retained assistant-order alignment; verify calls, not just ordinals."""
    steps: list[dict[str, Any]] = []
    refs: list[SourceEventRef] = []
    owners: dict[str, int] = {}
    for session in receipt["session_files"]:
        path = (directory / session["path"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Session escapes source run")
        data = _bound(path, session["sha256"])
        if inputs.get(str(path)) != session["sha256"]:
            raise ValueError("Session not bound by frozen evidence")
        for line, text in enumerate(data.decode().splitlines(), 1):
            if not text.strip():
                continue
            event = json.loads(text)
            message = event.get("message") or {}
            role = message.get("role")
            if role not in {"assistant", "toolResult"}:
                refs.append(SourceEventRef(
                    source_event_id=str(event.get("id") or f"line:{line}"),
                    normalized_step_id=None, source_path=str(path), source_sha256=session["sha256"],
                    source_line=line, parent_event_id=event.get("parentId"),
                    event_kind="message", resolution="source-context",
                ))
                continue
            calls = [b for b in message.get("content") or [] if b.get("type") == "toolCall"]
            call_id = message.get("toolCallId")
            if role == "assistant":
                prepared = view["steps"][len(steps)]
                expected = [{"name": b.get("name", ""), "args": b.get("arguments") or {}} for b in calls]
                actual = [{"name": b["name"], "args": json.loads(b["args"])}
                          for b in prepared["tool_calls"]]
                if actual != expected:
                    raise ValueError(f"Prepared action differs from source: {path}:{line}")
                step_id = prepared["i"]
                for call in calls:
                    owners[call["id"]] = step_id
                steps.append({
                    "step_id": step_id, "source": "agent", "message": prepared["text"] or "",
                    "tool_calls": [{"tool_call_id": b["id"], "function_name": b.get("name", ""),
                                    "arguments": b.get("arguments") or {}} for b in calls],
                    "observation": {"results": [] if prepared["observation"] is None else
                                    [{"content": prepared["observation"]}]},
                })
            else:
                if call_id not in owners:
                    raise ValueError(f"Unmatched source result: {path}:{line}")
                step_id = owners[call_id]
            refs.append(SourceEventRef(
                source_event_id=str(event.get("id") or f"line:{line}"),
                normalized_step_id=step_id, source_path=str(path), source_sha256=session["sha256"],
                source_line=line, parent_event_id=event.get("parentId"), branch_id=str(path),
                call_id=call_id, event_kind="result" if role == "toolResult" else "call" if calls else "message",
                resolution="result-to-call" if role == "toolResult" else "direct",
            ))
    if len(steps) != len(view["steps"]):
        raise ValueError("Prepared assistant count differs from original source")
    return {"steps": steps, "source_event_map": [asdict(ref) for ref in refs]}


def _packages(root: Path) -> dict[str, Path]:
    """Resolve retained task metadata only; never inspect graders or solutions."""
    retention = json.loads((root.parent / "retention/final-source/receipt.json").read_text())
    entries = [entry for entry in retention["paths"]
               if "/tasks-v2/" in entry["path"] and entry["path"].endswith("/meta.json")]
    packages: dict[str, Path] = {}
    for archive in sorted({entry["archive"] for entry in entries}):
        with tarfile.open(archive) as stream:
            for entry in entries:
                if entry["archive"] != archive:
                    continue
                member = stream.extractfile(entry["member"])
                if member is None:
                    raise ValueError("Retained task metadata is not a file")
                data = member.read()
                if _sha(data) != entry["sha256"]:
                    raise ValueError("Retained task metadata hash differs")
                metadata = json.loads(data)
                packages[metadata["task_id"]] = Path(metadata["package"])
    return packages


def impossible(ctx: BankContext):
    source = "impossible-task-final"
    root = Path.home() / ".local/state/evallab-ev1105/impmore/safe-prepared-final"
    packages = _packages(root)
    request = json.loads((root / "freeze-request.json").read_text())
    frozen = json.loads((root / "FROZEN.json").read_text())
    _bound(root / "freeze-request.json", frozen["request_sha256"])
    inputs = request["inputs"]
    path = root / "labels.jsonl"
    digest = _sha(_bound(path, inputs[str(path)]))
    audit_path = Path(request["audit"])
    audit = {r["id"]: r for r in _rows(audit_path)}
    _bound(audit_path, inputs[str(audit_path)])
    for number, row in enumerate(_rows(path), 1):
        run_id = row["run_key"]
        review = row["review"] or {}
        state = ("executed-positive" if row["label"] == 1 else "reviewed-control"
                 if row["label"] == 0 else "unjudged")
        fields = _common(ctx, source, run_id, row["task_id"], state, row["category"], digest,
                         f"{path}:{number}", review.get("reviewer", "campaign grade audit"),
                         "source action review with submitted/restored grade evidence")
        directory = Path(audit[run_id]["grade_path"]).parent
        receipt_path = directory / "receipt.json"
        receipt = json.loads(_bound(receipt_path, audit[run_id]["receipt_sha256"]))
        view_path = root / "views" / f"{run_id}.json"
        view = json.loads(_bound(view_path, inputs[str(view_path)]))
        doc = _impossible_map(view, directory, receipt, inputs)
        refs = {(ref.source_path, ref.source_line): ref
                for ref in build_trajectory_ir(doc).source_event_map}
        locators = []
        for citation in review.get("source_citations") or []:
            cited_path = Path(citation["path"])
            _bound(cited_path, inputs[str(cited_path)])
            ref = refs.get((str(cited_path), citation["line"]))
            if ref is None:
                if cited_path.name != "final.diff":
                    raise ValueError(f"Unresolved source action: {cited_path}:{citation['line']}")
                ref = SourceEventRef(source_event_id=f"line:{citation['line']}",
                                     normalized_step_id=None, source_path=str(cited_path),
                                     source_sha256=inputs[str(cited_path)], source_line=citation["line"],
                                     event_kind="artifact", resolution="supporting-submitted-diff")
            locators.append(ref)
        fields.update(_view(ctx, source, run_id, doc))
        package = packages.get(row["task_id"])
        grader = package / "tests/test.sh" if package is not None else None
        yield new_item(**fields, label_type="executed", step_locators=tuple(locators),
                       mechanism=review.get("mechanism", "none"),
                       task_ref=str(package) if package is not None else None,
                       task_available=package.is_dir() if package is not None else None,
                       grader_ref=str(grader) if grader is not None else None,
                       grader_available=grader.is_file() if grader is not None else None,
                       license="lab traces; underlying task licences vary",
                       metadata={"model": row["model"], "variant": row["variant"],
                                 "G1_pass": row["G1_pass"], "G2_pass": row["G2_pass"],
                                 "source_verified": review.get("source_verified"),
                                 "variant_input_ref": str(directory / "input-tree"),
                                 "source_variant_sha256": receipt.get("variant_sha256"),
                                 "campaign_status": "INCOMPLETE_OR_INVALID"})
