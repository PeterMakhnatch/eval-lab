#!/usr/bin/env python3
"""Promote a Harbor Codex canary job into ``research/evidence/runs/``.

The lab's only real agent-trajectory corpus lives in the gitignored runtime
``runs/`` tree of one workstation. This script copies a reviewed subset into the
versioned evidence tree so the scientific record survives that machine.

``evallab`` has no ``promote`` subcommand: ``evallab gc`` only compresses and
prunes *unpromoted* jobs and treats ``research/evidence`` as a protected layout
(``src/evallab/gc.py:202-208``). ``docs/analysis-loop.md`` forbids *automatic*
promotion, so promotion stays a human-reviewed pull request and this script is
the deterministic, re-runnable mechanism behind it.

Three redaction rules are applied. ``AGENTS.md`` forbids committing unredacted
model prompts, and one ``library/tasks/*/tests`` verifier keeps its attack-vector
corpus outside the repository on purpose.

R1 -- prompt redaction (``agent/trajectory.json``).
    Every ATIF step whose ``source`` is ``system`` or ``user`` carries verbatim
    prompt text: the Codex vendor system prompt (``<skills_instructions>``,
    ``You are `/root`...``, ``<multi_agent_mode>``, ``<plugins_instructions>``),
    the harness ``<recommended_plugins>`` preamble, and the task instruction.
    Their ``message`` becomes ``null`` plus ``message_sha256`` and
    ``message_chars``. ``agent``-source messages, ``tool_calls`` and
    ``observation`` are the agent's own output and the environment's response,
    not prompts, and stay verbatim.

R2 -- raw model I/O omission (``agent/sessions/**``).
    Codex rollout JSONL holds the full untruncated request/response stream
    including ``payload.encrypted_content`` reasoning blobs. Omitted entirely;
    the SHA-256 of each omitted file is recorded so provenance survives.

R3 -- verifier-only payload (``<trial>/verifier/*``).
    ``library/tasks/terminal-bench-html-js-filter/tests/test_outputs.py`` renders
    its attack-vector corpus, which is deliberately kept out of the repository
    inside the verifier image, and pytest echoes the whole failed batch into both
    ``verifier/test-stdout.txt`` and the CTRF ``trace``. R3 is a whitelist: it
    keeps what is provably a fact and drops whole payloads rather than trying to
    pattern-match corpus fragments out of them. It is scoped to ``verifier/`` and
    never touches ``agent/``, where long strings are the agent's own patches.

    R3a -- verifier JSON: every string value longer than 1024 bytes becomes
    ``<<evallab-redacted: N bytes, sha256:...>>``. The largest legitimate string
    in a promoted CTRF report is an 85-byte test name, so this removes exactly
    ``results.tests[].trace`` and keeps every reward, status, name and timing.
    The document stays valid JSON.

    R3b -- verifier text: a file larger than 4096 bytes is promoted as a digest
    marker only, with no body. Line-level or signature-level filtering was tried
    and rejected: the rendered corpus spans hundreds of short lines, so no
    per-line predicate is safe by construction. The scored facts survive in
    ``verifier/reward.txt``, ``verifier/ctrf.redacted.json`` and ``result.json``.

Every promoted file records the SHA-256 of its unredacted parent in
``PROMOTION.json`` next to the SHA-256 of the promoted bytes.

    python research/evidence/promote_codex_bundle.py --source-runs ../../runs \
        --job canary-event-summary-codex-20260815
    python research/evidence/promote_codex_bundle.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VERIFIER_JSON_STRING_LIMIT = 1024
VERIFIER_TEXT_LIMIT = 4096
PROMPT_SOURCES = frozenset({"system", "user"})
MANIFEST_NAME = "PROMOTION.json"
EVIDENCE_RUNS = Path(__file__).resolve().parent / "runs"


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _marker(text: str) -> str:
    raw = text.encode("utf-8")
    return f"<<evallab-redacted: {len(raw)} bytes, {sha256_bytes(raw)}>>"


def redact_trajectory(raw: bytes) -> bytes:
    """R1: drop prompt text from system/user ATIF steps, keep the rest."""
    document = json.loads(raw)
    for step in document.get("steps", []):
        if step.get("source") not in PROMPT_SOURCES:
            continue
        message = step.get("message")
        if message is None:
            continue
        step["message"] = None
        step["message_sha256"] = sha256_bytes(message.encode("utf-8"))
        step["message_chars"] = len(message)
    document["evallab_redaction"] = {
        "rule": "R1",
        "removed": "verbatim message text of every system-source and user-source step",
        "reason": "AGENTS.md forbids committing unredacted model prompts",
    }
    return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _redact_json_strings(node: Any) -> tuple[Any, int]:
    """R3a: replace oversize JSON string values with digest markers."""
    if isinstance(node, str):
        if len(node.encode("utf-8")) > VERIFIER_JSON_STRING_LIMIT:
            return _marker(node), 1
        return node, 0
    if isinstance(node, dict):
        count = 0
        result: dict[str, Any] = {}
        for key, value in node.items():
            result[key], hits = _redact_json_strings(value)
            count += hits
        return result, count
    if isinstance(node, list):
        count = 0
        items = []
        for value in node:
            item, hits = _redact_json_strings(value)
            items.append(item)
            count += hits
        return items, count
    return node, 0


def redact_verifier(path: Path, raw: bytes) -> tuple[bytes, int]:
    """R3: reduce a verifier artifact to the facts it can safely carry."""
    if path.suffix == ".json":
        document, hits = _redact_json_strings(json.loads(raw))
        if hits == 0:
            return raw, 0
        return json.dumps(document, indent=4, ensure_ascii=False).encode("utf-8") + b"\n", hits
    if len(raw) <= VERIFIER_TEXT_LIMIT:
        return raw, 0
    body = _marker(raw.decode("utf-8", errors="replace"))
    return f"{body}\n".encode(), 1


def classify(relative: Path) -> str:
    parts = relative.parts
    if "sessions" in parts and "agent" in parts:
        return "omit-R2"
    if relative.name == "trajectory.json" and "agent" in parts:
        return "redact-R1"
    if "verifier" in parts:
        return "maybe-redact-R3"
    return "verbatim"


def promote(job_dir: Path, destination: Path, *, force: bool = False) -> dict[str, Any]:
    if not job_dir.is_dir():
        raise SystemExit(f"source job directory not found: {job_dir}")
    if destination.exists():
        if not force:
            raise SystemExit(
                f"{destination} already exists; agents/STRUCTURE.md calls promoted "
                "bundles immutable. Pass --force to re-promote deliberately."
            )
        shutil.rmtree(destination)

    entries: list[dict[str, Any]] = []
    promoted_bytes = 0
    for source in sorted(p for p in job_dir.rglob("*") if p.is_file()):
        relative = source.relative_to(job_dir)
        raw = source.read_bytes()
        parent_digest = sha256_bytes(raw)
        action = classify(relative)

        if action == "omit-R2":
            entries.append(
                {
                    "source_path": str(relative),
                    "promoted_path": None,
                    "action": "omitted",
                    "rule": "R2",
                    "source_bytes": len(raw),
                    "source_sha256": parent_digest,
                }
            )
            continue

        if action == "redact-R1":
            body = redact_trajectory(raw)
            target = relative.with_name("trajectory.redacted.json")
            rule, applied = "R1", "redacted"
        elif action == "maybe-redact-R3":
            body, hits = redact_verifier(source, raw)
            if hits:
                target = relative.with_name(
                    f"{relative.stem}.redacted{relative.suffix}"
                )
                rule, applied = "R3", "redacted"
            else:
                target, rule, applied = relative, None, "verbatim"
        else:
            body, target, rule, applied = raw, relative, None, "verbatim"

        out = destination / target
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        promoted_bytes += len(body)
        entries.append(
            {
                "source_path": str(relative),
                "promoted_path": str(target),
                "action": applied,
                "rule": rule,
                "source_bytes": len(raw),
                "source_sha256": parent_digest,
                "promoted_bytes": len(body),
                "promoted_sha256": sha256_bytes(body),
            }
        )

    job_result = job_dir / "result.json"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle": destination.name,
        "source_job_runtime_path": f"runs/{job_dir.name}",
        "source_job_result_sha256": sha256_file(job_result) if job_result.is_file() else None,
        "promoted_by": "research/evidence/promote_codex_bundle.py",
        "redaction_rules": {
            "R1": "system/user ATIF step message text removed; sha256 and length kept",
            "R2": "agent/sessions/** raw model I/O omitted; sha256 recorded",
            "R3a": (
                "verifier/* JSON string values over "
                f"{VERIFIER_JSON_STRING_LIMIT} bytes replaced by digest markers"
            ),
            "R3b": (
                f"verifier/* text files over {VERIFIER_TEXT_LIMIT} bytes promoted "
                "as a whole-file digest marker with no body"
            ),
        },
        "totals": {
            "source_files": len(entries),
            "promoted_files": sum(1 for e in entries if e["promoted_path"]),
            "omitted_files": sum(1 for e in entries if not e["promoted_path"]),
            "promoted_bytes": promoted_bytes,
            "source_bytes": sum(e["source_bytes"] for e in entries),
        },
        "files": entries,
    }
    (destination / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return manifest


def verify(evidence_runs: Path) -> int:
    """Recompute promoted digests from every PROMOTION.json. Parent-free."""
    failures = 0
    checked = 0
    manifests = sorted(evidence_runs.glob(f"*/{MANIFEST_NAME}"))
    if not manifests:
        print(f"no {MANIFEST_NAME} under {evidence_runs}")
        return 1
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text())
        bundle = manifest_path.parent
        for entry in manifest["files"]:
            promoted = entry.get("promoted_path")
            if not promoted:
                continue
            path = bundle / promoted
            if not path.is_file():
                print(f"MISSING {bundle.name}/{promoted}")
                failures += 1
                continue
            actual = sha256_file(path)
            if actual != entry["promoted_sha256"]:
                print(f"DIGEST MISMATCH {bundle.name}/{promoted}: {actual}")
                failures += 1
            checked += 1
        extra = {
            str(p.relative_to(bundle))
            for p in bundle.rglob("*")
            if p.is_file() and p.name != MANIFEST_NAME
        } - {e["promoted_path"] for e in manifest["files"] if e.get("promoted_path")}
        for name in sorted(extra):
            print(f"UNMANIFESTED {bundle.name}/{name}")
            failures += 1
        print(f"{manifest_path.parent.name}: {len(manifest['files'])} source files recorded")
    print(f"verified {checked} promoted files across {len(manifests)} bundles, {failures} failures")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-runs", type=Path, help="runtime runs/ directory")
    parser.add_argument("--job", action="append", default=[], help="job directory name")
    parser.add_argument(
        "--evidence-runs", type=Path, default=EVIDENCE_RUNS, help="destination runs/ tree"
    )
    parser.add_argument("--verify", action="store_true", help="recheck promoted digests only")
    parser.add_argument("--force", action="store_true", help="re-promote over an existing bundle")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(args.evidence_runs)
    if not args.source_runs or not args.job:
        parser.error("--source-runs and at least one --job are required")

    total = 0
    for job in args.job:
        manifest = promote(args.source_runs / job, args.evidence_runs / job, force=args.force)
        total += manifest["totals"]["promoted_bytes"]
        print(
            f"{job}: {manifest['totals']['promoted_files']} promoted "
            f"({manifest['totals']['promoted_bytes']} B) "
            f"{manifest['totals']['omitted_files']} omitted "
            f"from {manifest['totals']['source_bytes']} B source"
        )
    print(f"total promoted bytes: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
