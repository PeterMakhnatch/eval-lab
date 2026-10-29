"""Source-only OQ10 orchestration; detector prompts and policies remain OQ6 B.

Importing this module reads no benchmark records and calls no model. The caller
must validate the prospective code freeze and route/isolation proof before
hydrating records, and the opaque input freeze before dispatching a native role.
Labels/identity stay in a separate scorer-side manifest; only source-derived
files, the fixed DEV historical context and the unchanged B template are rendered.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

GATES = ("old_0_8", "new_verified_0_5")
CASE_ID = re.compile(r"C[0-9]{4}\Z")
TOKENIZERS = {"anthropic": "ClaudeV5Sonnet", "kimi-code": "KimiK2"}
WRAPPER_RESERVE = 1024
OUTPUT_THINKING_RESERVE = 16_384


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def pin(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": digest, "bytes": path.stat().st_size}


def verify_pin(value: Mapping[str, Any]) -> Path:
    path = Path(value["path"])
    if path.is_symlink() or pin(path)["sha256"] != value["sha256"]:
        raise ValueError("frozen artifact drift: " + str(path))
    return path


def ordered_selection(groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Assign opaque ids only after the frozen independent samplers finish.

    Class/stratum/hash selection belongs to the source helpers. Round-robin
    corpus/subcorpus bands preserve each sampler's order without reading labels.
    """
    cases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    bands: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for corpus, rows in groups.items():
        for row in rows:
            bands.setdefault((corpus, row["subcorpus"]), []).append(row)
    for index in range(max((len(rows) for rows in bands.values()), default=0)):
        for corpus, subcorpus in sorted(bands):
            rows = bands[corpus, subcorpus]
            if index >= len(rows):
                continue
            row = rows[index]
            identity = (corpus, row["task_id"])
            if identity in seen:
                raise ValueError("duplicate selected task identity")
            seen.add(identity)
            cases.append({**dict(row), "case_id": f"C{len(cases) + 1:04d}", "corpus": corpus})
    return cases


def _write_source_files(root: Path, files: Mapping[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for relative, text in sorted(files.items()):
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")) or "\\" in relative:
            raise ValueError("source renderer produced an unsafe package path")
        if not isinstance(text, str):
            raise TypeError("source renderer must return text only")
        destination = root.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(text.encode("utf-8"))


def prepare_case(
    record: Mapping[str, Any], *, case_id: str, output: Path, reader: Any,
    render: Callable[[str, str, list[str], Any], str], display_context: Mapping[str, Any],
    context_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the unchanged 70k/file, 1M extraction, 180k/package reader.

    The new source adapter emits full admitted auditor_input only. It never chooses
    a target-specific grader subset. The existing reader owns order, exclusions,
    omissions and static decoding; missing essential instruction is unavailable.
    No source record is admitted into CorpusRegistry/CloudTraceDB/evidence CAS.
    """
    if CASE_ID.fullmatch(case_id) is None:
        raise ValueError("case id must be an opaque Cdddd value")
    base = output / "inputs" / case_id
    base.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "case_id": case_id, "preparation_status": record["preparation_status"],
        "source_record_sha256": record.get("source_record_sha256"),
        "record_binding_sha256": record["record_binding_sha256"],
        "preparation_error": record.get("preparation_error"), "files": [],
    }
    if record["preparation_status"] != "prepared":
        return result
    normalized = base / "normalized-source"
    _write_source_files(normalized, record["auditor_files"])
    reader.ROOT = output
    package, source, files, omitted, source_manifest = reader.prepare_source(normalized, case_id)
    result.update(
        package=str(package), files=files, omitted=omitted,
        source_manifest=pin(package / "source-manifest.json"),
        extraction_file_cap=reader.FILE_CAP, source_package_char_cap=reader.PACKAGE_CAP,
    )
    if not (package / "instruction.md").is_file():
        result.update(preparation_status="source_unavailable", preparation_error="essential instruction unavailable under the unchanged reader caps")
        return result
    prompt = render(case_id, source, omitted, display_context)
    for name, text in (("source.txt", source), ("prompt.txt", prompt)):
        with (base / name).open("xb") as stream:
            stream.write(text.encode("utf-8"))
    dump_new(base / "context-provenance.json", context_provenance)
    dump_new(base / "context-display.json", display_context)
    result.update(
        preparation_status="prepared", preparation_error=None,
        source=pin(base / "source.txt"), prompt=pin(base / "prompt.txt"),
        context_provenance=pin(base / "context-provenance.json"),
        context_display=pin(base / "context-display.json"),
        source_chars=len(source), prompt_chars=len(prompt),
        decoded_command_count=len(source_manifest["decoded_commands"]),
    )
    return result


def verify_case(case: Mapping[str, Any]) -> None:
    """Check the displayed prompt and the exact files used for citation replay."""
    if case["preparation_status"] != "prepared":
        return
    for key in ("source", "prompt", "source_manifest", "context_provenance", "context_display"):
        verify_pin(case[key])
    package = Path(case["package"])
    for row in case["files"]:
        file = package / row["path"]
        if file.is_symlink() or sha(file.read_bytes()) != row["sha256"]:
            raise ValueError("frozen citation package drift")


def source_binding(case: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a prediction to its frozen native record, rendered view and prompt."""
    return {
        "record_binding_sha256": case["record_binding_sha256"],
        "source_record_sha256": case.get("source_record_sha256"),
        "source_sha256": case.get("source", {}).get("sha256"),
        "prompt_sha256": case.get("prompt", {}).get("sha256"),
    }


def count_window(prompt: str, system: str, *, input_file: Path, encoding: str) -> dict[str, Any]:
    """Use the documented embedded tokenizer, never a hosted count/probe endpoint."""
    expected = (system + "\n" + prompt).encode("utf-8")
    if input_file.exists():
        if input_file.read_bytes() != expected:
            raise ValueError("offline tokenizer input drift")
    else:
        input_file.parent.mkdir(parents=True, exist_ok=True)
        with input_file.open("xb") as stream:
            stream.write(expected)
    completed = subprocess.run(["omp", "toks", str(input_file), "--json"], capture_output=True, check=True)
    encodings = json.loads(completed.stdout)["encodings"]
    matching = [row for row in encodings if row["encoding"] == encoding]
    if len(matching) != 1 or type(matching[0]["tokens"]) is not int:
        raise ValueError("the prospectively specified native tokenizer is unavailable")
    tokens = matching[0]["tokens"]
    return {
        "encoding": encoding, "input_tokens": tokens,
        "wrapper_reserve": WRAPPER_RESERVE, "output_thinking_reserve": OUTPUT_THINKING_RESERVE,
        "estimated_window_tokens": tokens + WRAPPER_RESERVE + OUTPUT_THINKING_RESERVE,
        "raw_stdout_sha256": sha(completed.stdout), "count_input": pin(input_file),
        "basis": "embedded model-family tokenizer plus declared reserves; not a provider-wire token-count guarantee",
    }


def parse_flags(
    case: Mapping[str, Any], native: Mapping[str, Any], *, route: str,
    task_quality_worker: Any, paired: Any, native_helper: Any,
) -> dict[str, Any]:
    """Replay the canonical self-free method once at zero floors, then both gates.

    This is the same unbound audit_proposals invocation used by OQ6 run_slice.py;
    its pinned implementation explicitly does no model/backend/queue work and
    does not access self. Partial parse never becomes an unflagged task.
    """
    payloads = None
    raw_count = None
    error = None
    if native["disposition"] == "answered":
        receipt = native["receipt"]
        if receipt.get("prompt_sha256") != case["prompt"]["sha256"]:
            raise ValueError("attested response does not bind the frozen prompt")
        raw = Path(receipt["response_path"]).read_bytes()
        if sha(raw) != receipt["response_sha256"]:
            raise ValueError("attested response bytes drifted before parsing")
        raw_count = len(native["answer"]["findings"])
        try:
            candidates = task_quality_worker.audit_proposals(
                None, Path(case["package"]), task_id=case["case_id"], response=raw.decode("utf-8"),
                model=route, min_verified_confidence=0, min_unverified_confidence=0,
            )
            payloads = [candidate.as_payload() for candidate in candidates]
            if len(payloads) != raw_count:
                raise ValueError("partial/truncated parse: raw/parsed counts differ")
            paired.gate_memberships(payloads)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            error = type(exc).__name__ + ": " + str(exc)
            payloads = None
    status = "ok" if payloads is not None else "missing"
    flags = native_helper.export_flags(
        payloads, parse_status=status, paired_module=paired, raw_findings_count=raw_count,
    )
    return {
        "schema": "oq10.paired-task-flags/v1", "case_id": case["case_id"], "route": route,
        "status": flags["status"], "flags": {gate: flags[gate] for gate in GATES},
        "raw_findings_count": raw_count,
        "parsed_findings_count": len(payloads) if payloads is not None else None,
        "parsed_payloads": payloads, "memberships": flags["memberships"],
        "parse_error": error, "missing_reason": flags.get("reason"),
        "native_disposition": native["disposition"],
        "native_receipt_sha256": native.get("receipt_file_sha256"),
        **source_binding(case),
    }


def step_role(
    case: Mapping[str, Any], *, role: str, route: str, output: Path, system: str,
    runtime_manifest: Mapping[str, Any], native_helper: Any, paired: Any,
    task_quality_worker: Any, verify_freeze: Callable[[], None],
    call_function: Any = None, admission_function: Any = None,
) -> dict[str, Any]:
    """One disjoint role slot. Quota-UNSENT remains pending; consumed roles reread.

    No role waits for another role's result, and no triage result enters another
    role's prompt. The scheduler may rank tasks only; every planned source-available
    Sonnet/Kimi task remains in its independent arm. The existing transport alone
    owns native consumption, raw receipts and refusal/error terminality.
    """
    verify_freeze()
    final = output / "cells" / role / (case["case_id"] + ".json")
    verify_case(case)
    if final.exists():
        cached = json.loads(final.read_bytes())
        expected = {"case_id": case["case_id"], "role": role, "route": route, **source_binding(case)}
        if any(cached.get(key) != value for key, value in expected.items()):
            raise ValueError("cached terminal cell does not bind the frozen case")
        return cached
    if case["preparation_status"] != "prepared":
        value = {
            "case_id": case["case_id"], "role": role, "route": route,
            "terminal": True, "status": "source_unavailable", "sent": False,
            "flags": {gate: None for gate in GATES}, "reason": case.get("preparation_error"),
            **source_binding(case),
        }
        dump_new(final, value)
        return value
    prompt = Path(case["prompt"]["path"]).read_text(encoding="utf-8")
    window = count_window(
        prompt, system, input_file=output / "window-inputs" / (case["case_id"] + ".txt"),
        encoding=TOKENIZERS[route.split("/", 1)[0]],
    )
    native = native_helper.run_native(
        output / "calls" / role / case["case_id"], route=route, prompt=prompt, system=system,
        runtime_manifest=runtime_manifest, timeout_seconds=600,
        estimate_tokens=lambda supplied_prompt, supplied_system: window["estimated_window_tokens"],
        call_function=call_function, admission_function=admission_function,
    )
    verify_freeze()
    value: dict[str, Any] = {
        "case_id": case["case_id"], "role": role, "route": route,
        "terminal": native["terminal"], "status": native["disposition"],
        "sent": native["sent"], "reason": native["reason"], "window": window,
        "flags": {gate: None for gate in GATES}, "native": native,
        **source_binding(case),
    }
    if native["terminal"]:
        verify_case(case)
        value["prediction"] = parse_flags(
            case, native, route=route, task_quality_worker=task_quality_worker,
            paired=paired, native_helper=native_helper,
        )
        value["flags"] = value["prediction"]["flags"]
        value["native_disposition"] = native["disposition"]
        value["status"] = value["prediction"]["status"]
        dump_new(final, value)
    else:
        dump_new(output / "quota-pending" / role / case["case_id"] / (uuid.uuid4().hex + ".json"), value)
    return value


def triage_order(cases: Sequence[Mapping[str, Any]], cells: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Optional ranking only: all cases retained, no findings injected elsewhere."""
    def key(case: Mapping[str, Any]) -> tuple[bool, float, str]:
        prediction = cells.get(case["case_id"], {}).get("prediction", {})
        payloads = prediction.get("parsed_payloads")
        if payloads is None:
            return (True, 0.0, case["case_id"])
        confidence = max((row["confidence"] for row in payloads), default=0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            raise ValueError("invalid canonical triage confidence")
        return (False, -confidence, case["case_id"])
    return [case["case_id"] for case in sorted(cases, key=key)]
