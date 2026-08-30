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

R1 -- prompt redaction (``agent/trajectory.json``, promoted at the same path).
    Every ATIF step whose ``source`` is ``system`` or ``user`` carries verbatim
    prompt text: the Codex vendor system prompt (``<skills_instructions>``,
    ``You are `/root`...``, ``<multi_agent_mode>``, ``<plugins_instructions>``),
    the harness ``<recommended_plugins>`` preamble, and the task instruction.
    Their ``message`` becomes ``<<evallab-redacted: N bytes, sha256:...>>`` plus
    ``message_sha256`` and ``message_chars``. ``agent``-source messages,
    ``tool_calls`` and ``observation`` are the agent's own output and the
    environment's response, not prompts, and stay verbatim.

    The promoted document keeps the canonical ``agent/trajectory.json`` path and
    stays a valid ATIF-v1.7 document, so ``evallab trajectories``, ``trace``,
    ``facts``, the explorer, the analysis worker and the calibration label audit
    can all read it. ``atif.py:279-280`` requires ``steps[].message`` to be text
    or content parts, so the text is replaced, never nulled. The redaction is
    recorded in-band under ``evallab_redaction``.

R2 -- raw model I/O / runtime state omission (``agent/sessions/**``,
    ``agent/codex.txt``, ``agent/opencode.txt``, ``agent/opencode/**``, root
    ``job.log``, and per-trial ``trial.log``).
    Codex rollout JSONL holds the full untruncated request/response stream
    including ``payload.encrypted_content`` reasoning blobs. OpenCode writes a
    sibling raw stream at ``agent/opencode.txt`` and a whole runtime state tree
    at ``agent/opencode/**`` -- the ``opencode.db``/``opencode.db-wal``/
    ``opencode.db-shm`` SQLite store, ``log/opencode.log``, ``snapshot/**``,
    ``repos/**``, ``locks/**`` and the XDG ``auth.json`` link -- which together
    are the full session transcript and credential/runtime state, not evidence.
    All of these are omitted entirely; the SHA-256 of each omitted file is
    recorded so provenance survives.

    Symlinks are enumerated explicitly and never dereferenced. OpenCode's XDG
    ``auth.json`` is a live symlink to a host credential store, and it must not
    be read: a live link would disclose the target's bytes into the digest, and
    a broken link cannot be read at all. Every symlink under an R2 omission path
    is recorded with ``entry_type: "symlink"`` and the SHA-256/length of its
    *link-target string* (the link itself, not the target's content). Any
    symlink outside an R2 omission path fails closed -- promotion refuses to
    copy or dereference it.
    ``PROMOTION.json`` manifests are versioned. Schema v2 requires
    ``entry_type`` on every R2 omission record and
    ``link_target``/length/hash on symlink omissions, and ``verify`` rejects
    stripped fields or a version downgrade. Every committed bundle has been
    migrated to v2; non-v2 manifests fail closed.

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

R4 -- redacted quota sidecar (``<trial>/agent/quota/<rollout>.rate-limits.json``).
    R2 is right to omit the rollout, but the rollout is also the only place the
    provider's own quota reading is recorded: each ``token_count`` event carries
    ``payload.rate_limits`` -- ``used_percent``, ``window_minutes``,
    ``resets_at``, ``credits``, ``plan_type``. Omitting the file whole therefore
    discarded the one measured quota signal at the exact moment the evidence
    became permanent, leaving the lab's quota history only in a gitignored
    ``runs/`` tree on a single workstation.

    R4 writes, beside each omitted rollout, a sidecar carrying only the event
    timestamp and an explicit whitelist of ``payload.rate_limits`` scalars --
    exactly the fields ``src/evallab/quota.py`` reads, and nothing else. Like
    R3 it is a whitelist rather than a filter: nothing outside
    ``payload.rate_limits`` is ever read, and an unrecognised key inside it is
    dropped with only its *name* recorded, so a field the provider adds later
    cannot leak by default. Message text, prompts, reasoning blobs, session
    titles and tokens are therefore unreachable by construction, not by
    pattern-matching. A whitelisted string longer than
    ``RATE_LIMIT_STRING_LIMIT`` bytes becomes a digest marker, bounding the
    sidecar's content without inspecting it.

    The sidecar is a derivative, not a promoted copy: it records the SHA-256 and
    byte count of the omitted parent rollout, so it is auditable against the
    original exactly as R2's own omission record is, and the operator surfaces
    label it ``withheld`` -- a few hundred bytes surviving from a ~194 kB
    rollout -- so no reader mistakes it for the session.

    What the sidecar cannot support is documented next to the format in
    ``docs/quota-accounting.md``: the reading is account-wide and cannot be
    decomposed into the lab's share, it is a point-in-time snapshot rather than
    a series, and it is only as fresh as the trial that recorded it.

Every promoted file records the SHA-256 of its unredacted parent in
``PROMOTION.json`` next to the SHA-256 of the promoted bytes.

    python scripts/promote_codex_bundle.py --source-runs runs \
        --job canary-event-summary-codex-20260815
    python scripts/promote_codex_bundle.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

#: Manifest schema. v2 requires ``entry_type`` on every R2 omission record and
#: ``link_target``/length/hash on symlink omissions, so ``verify`` can reject
#: deletion or version-downgrade of those fields. Every committed bundle is v2;
#: older manifests must be deliberately re-promoted before verification.
SCHEMA_VERSION = 2
VERIFIER_JSON_STRING_LIMIT = 1024
VERIFIER_TEXT_LIMIT = 4096
PROMPT_SOURCES = frozenset({"system", "user"})
MANIFEST_NAME = "PROMOTION.json"
EVIDENCE_RUNS = Path(__file__).resolve().parents[1] / "research" / "evidence" / "runs"

#: R4 constants and bounding limits.
RATE_LIMIT_STRING_LIMIT = 128
SIDECAR_SCHEMA_VERSION = 1
SIDECAR_DIRNAME = "quota"
SIDECAR_SUFFIX = ".rate-limits.json"
ROLLOUT_PREFIX = "rollout-"
MAX_ROLLOUT_LINE_BYTES = 65536
MAX_QUOTA_SNAPSHOTS = 1000
MAX_SIDECAR_BYTES = 1024 * 1024

#: Streaming read chunk size and hard file size limit.
STREAM_CHUNK_BYTES = 65536
MAX_SOURCE_BYTES = 256 * 1024 * 1024

#: Basenames that carry credential/runtime/raw log material regardless of location.
#: Matched case-folded and Unicode-normalized (NFC).
FORBIDDEN_BASENAMES = frozenset({
    "job.log",
    "trial.log",
    "codex.txt",
    "opencode.txt",
    "auth.json",
    "credentials.json",
    ".netrc",
    ".env",
    "opencode.db",
    "opencode.db-wal",
    "opencode.db-shm",
})

#: Container and archive extensions rejected from verbatim promotion.
ARCHIVE_SUFFIXES = frozenset({
    ".zip",
    ".tar",
    ".tgz",
    ".tar.gz",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".7z",
    ".rar",
    ".jar",
    ".whl",
    ".apk",
    ".deb",
    ".rpm",
})

#: Benign metrics containing 'token' that must never be treated as secret keys.
_BENIGN_METRIC_WORDS = frozenset({
    "token_count",
    "token_counts",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "tokens_per_second",
    "step_tokens",
    "max_tokens",
    "max_output_tokens",
    "num_tokens",
    "n_tokens",
    "token_usage",
    "usage_tokens",
})

#: Words and pattern matching secret-shaped auth/credential keys.
_SECRET_AUTH_KEYS = frozenset({
    "token",
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "auth",
    "auth_secret",
    "auth_token",
    "authorization",
    "bearer",
    "bearer_token",
    "client_secret",
    "credential",
    "credentials",
    "github_token",
    "gitlab_token",
    "id_token",
    "jwt",
    "jwt_token",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secrets",
    "secret_key",
    "session_key",
    "session_token",
    "signing_key",
    "ssh_key",
    "webhook_secret",
})

_SECRET_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|access_?(?:key|token)|auth(?:orization|_secret|_token)?|bearer(?:_token)?|"
    r"client_?secret|credential(?:s|_key)?|github_?token|gitlab_?token|id_?token|jwt(?:_token)?|"
    r"passphrase|password|passwd|private_?key|pwd|refresh_?token|secret(?:s|_key)?|"
    r"session_?(?:key|token)|signing_?key|ssh_?key|webhook_?secret)(?:$|_)"
)


def _normalize_key_name(key: str) -> str:
    """Normalize camelCase, kebab-case, dot-notation, and Unicode to snake_case."""
    nfc = unicodedata.normalize("NFC", key)
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", nfc)
    s2 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s1)
    s3 = re.sub(r"[^a-zA-Z0-9]+", "_", s2)
    return s3.strip("_").casefold()


def _is_secret_key(key: str) -> bool:
    """Detect if a JSON key name is secret-shaped across casing styles while preserving benign metrics."""
    normalized = _normalize_key_name(key)
    if (
        normalized in _BENIGN_METRIC_WORDS
        or normalized.endswith(("_tokens", "_token_count", "_tokens_per_second", "_token_usage"))
        or normalized.startswith(("token_count", "tokens_per_"))
    ):
        return False
    if normalized in _SECRET_AUTH_KEYS:
        return True
    return bool(_SECRET_PATTERN.search(normalized))


#: R4 whitelist. Exactly the fields ``src/evallab/quota.py`` reads from
#: ``payload.rate_limits``, with the type each must have. Anything else the
#: provider sends is dropped and only its name is recorded. ``bool`` is listed
#: separately from ``int`` on purpose: ``isinstance(True, int)`` is ``True``, so
#: a numeric field would otherwise silently accept a flag.
RATE_LIMIT_SCALARS: dict[str, tuple[type, ...]] = {
    "limit_id": (str,),
    "limit_name": (str,),
    "plan_type": (str,),
    "rate_limit_reached_type": (str,),
}
RATE_LIMIT_WINDOWS: dict[str, tuple[type, ...]] = {
    "used_percent": (int, float),
    "window_minutes": (int, float),
    "resets_at": (int, float),
}
RATE_LIMIT_CREDITS: dict[str, tuple[type, ...]] = {
    "has_credits": (bool,),
    "unlimited": (bool,),
    "balance": (str, int, float),
}
RATE_LIMIT_WINDOW_KEYS = ("primary", "secondary")


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _stream_sha256(path: Path) -> tuple[str, int]:
    """Compute SHA-256 digest and byte count in streaming chunks without unbounded memory usage."""
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        while chunk := f.read(STREAM_CHUNK_BYTES):
            total += len(chunk)
            h.update(chunk)
    return f"sha256:{h.hexdigest()}", total


def _canonical(name: str) -> str:
    """Normalize a path component or name to Unicode NFC and casefold for matching."""
    return unicodedata.normalize("NFC", name).casefold()


def _validate_job_name(job: str | Path) -> str:
    """Validate that a job argument is exactly one safe, non-empty basename.

    Rejects '.', '..', empty strings, whitespace, path separators, absolute paths,
    and ambiguous Unicode or traversal variants before any filesystem or delete operations.
    """
    raw = str(job)
    if not raw or not raw.strip():
        raise SystemExit("job name cannot be empty")
    normalized = unicodedata.normalize("NFC", raw.strip())
    # Reject any path separators (forward or backslash)
    if "/" in normalized or "\\" in normalized:
        raise SystemExit(f"job name cannot contain path separators: {raw!r}")
    if normalized in {".", "..", ""}:
        raise SystemExit(f"invalid job name (must not be '.' or '..'): {raw!r}")
    p = Path(normalized)
    if p.is_absolute() or len(p.parts) != 1 or p.parts[0] in {".", "..", ""}:
        raise SystemExit(f"invalid job name (must be a single non-traversal basename): {raw!r}")
    return normalized


def _is_archive_like(relative: Path) -> bool:
    """Whether a path carries an archive/container extension that could embed unredacted secrets."""
    name_lower = _canonical(relative.name)
    if any(name_lower.endswith(s) for s in ARCHIVE_SUFFIXES):
        return True
    return any(_canonical(s) in ARCHIVE_SUFFIXES for s in relative.suffixes)


def _assert_within(base: Path, target: Path) -> None:
    """Ensure `target` resolves strictly inside `base` to prevent traversal attacks."""
    resolved_base = base.resolve()
    resolved_target = target.resolve()
    if resolved_base != resolved_target and resolved_base not in resolved_target.parents:
        raise SystemExit(f"path traversal detected: {target} resolves outside {base}")


def _marker(text: str) -> str:
    raw = text.encode("utf-8")
    return f"<<evallab-redacted: {len(raw)} bytes, {sha256_bytes(raw)}>>"


def redact_trajectory(raw: bytes) -> bytes:
    """R1: replace prompt text in system/user ATIF steps with a digest marker.

    The result must stay a valid ATIF-v1.7 document, because every consumer in
    this repository reads it: ``atif.py:279-280`` requires ``steps[].message`` to
    be text or content parts, so the text is replaced rather than nulled.
    Also redacts secret-shaped JSON values nested inside agent step messages.
    """
    document = json.loads(raw)
    redacted = 0
    for step in document.get("steps", []):
        source = step.get("source")
        message = step.get("message")
        if source in PROMPT_SOURCES:
            if isinstance(message, str) and message:
                step["message"] = _marker(message)
                step["message_sha256"] = sha256_bytes(message.encode("utf-8"))
                step["message_chars"] = len(message)
                redacted += 1
            elif isinstance(message, list):
                # Valid ATIF v1.7 content parts list
                for part in message:
                    if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                        text = part["text"]
                        part["text"] = _marker(text)
                        part["text_sha256"] = sha256_bytes(text.encode("utf-8"))
                        redacted += 1
        else:
            # Check agent message (str with possible leading whitespace or content parts list)
            if isinstance(message, str):
                trimmed = message.strip()
                if trimmed.startswith(("{", "[")):
                    try:
                        parsed = json.loads(trimmed)
                        redacted_json, hits = _redact_json_strings(parsed)
                        if hits > 0:
                            step["message"] = json.dumps(redacted_json, ensure_ascii=False)
                            redacted += hits
                    except Exception:
                        pass
            elif isinstance(message, list):
                for part in message:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text = part["text"]
                        trimmed = text.strip()
                        if trimmed.startswith(("{", "[")):
                            try:
                                parsed = json.loads(trimmed)
                                redacted_json, hits = _redact_json_strings(parsed)
                                if hits > 0:
                                    part["text"] = json.dumps(redacted_json, ensure_ascii=False)
                                    redacted += hits
                            except Exception:
                                pass
    document["evallab_redaction"] = {
        "rule": "R1",
        "removed": "verbatim message text of every system-source and user-source step",
        "reason": "AGENTS.md forbids committing unredacted model prompts",
        "steps_redacted": redacted,
        "recover": "message_sha256 identifies the original text; the unredacted "
        "parent digest is in PROMOTION.json",
    }
    return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _redact_json_strings(
    node: Any, parent_key: str | None = None, under_secret: bool = False
) -> tuple[Any, int]:
    """R3a & Secret redaction: replace oversize strings and secret-shaped keys with digest markers."""
    is_secret = under_secret or (parent_key is not None and _is_secret_key(parent_key))
    if is_secret and isinstance(node, (str, int, float, bool)) and str(node):
        return _marker(str(node)), 1
    if isinstance(node, str):
        if len(node.encode("utf-8")) > VERIFIER_JSON_STRING_LIMIT:
            return _marker(node), 1
        return node, 0
    if isinstance(node, dict):
        count = 0
        result: dict[str, Any] = {}
        for key, value in node.items():
            result[key], hits = _redact_json_strings(
                value, parent_key=key, under_secret=is_secret
            )
            count += hits
        return result, count
    if isinstance(node, list):
        count = 0
        items = []
        for value in node:
            item, hits = _redact_json_strings(
                value, parent_key=parent_key, under_secret=is_secret
            )
            items.append(item)
            count += hits
        return items, count
    return node, 0


def redact_verifier(path: Path, raw: bytes) -> tuple[bytes, int]:
    """R3: reduce a verifier artifact to the facts it can safely carry."""
    if _canonical(path.suffix) == ".json":
        document, hits = _redact_json_strings(json.loads(raw))
        if hits == 0:
            return raw, 0
        return json.dumps(document, indent=4, ensure_ascii=False).encode("utf-8") + b"\n", hits
    if len(raw) <= VERIFIER_TEXT_LIMIT:
        return raw, 0
    body = _marker(raw.decode("utf-8", errors="replace"))
    return f"{body}\n".encode(), 1


def _whitelisted_scalar(value: Any, kinds: tuple[type, ...]) -> Any | None:
    """A value of a declared type, bounded in size, or ``None`` to drop it.

    ``bool`` is rejected for numeric fields because ``isinstance(True, int)`` is
    ``True``; ``src/evallab/quota.py`` makes the same distinction.
    """
    if isinstance(value, bool) and bool not in kinds:
        return None
    if not isinstance(value, kinds):
        return None
    if isinstance(value, str) and len(value.encode("utf-8")) > RATE_LIMIT_STRING_LIMIT:
        return _marker(value)
    return value


def _whitelisted_group(
    payload: Any, fields: dict[str, tuple[type, ...]], prefix: str, dropped: list[str]
) -> dict[str, Any] | None:
    """Rebuild one nested quota object from the whitelist, field by field."""
    if not isinstance(payload, dict):
        return None
    group: dict[str, Any] = {}
    for key, value in payload.items():
        kinds = fields.get(key)
        if kinds is None:
            dropped.append(f"{prefix}.{key}")
            continue
        kept = _whitelisted_scalar(value, kinds)
        if kept is None and value is not None:
            dropped.append(f"{prefix}.{key}")
            continue
        group[key] = kept
    return group


def redact_rate_limits(limits: Any) -> tuple[dict[str, Any], list[str]]:
    """R4: rebuild ``payload.rate_limits`` from the whitelist, nothing else.

    Returns the safe object and the *names* of the fields dropped. Values are
    never copied out of an unrecognised field, so a key the provider adds later
    is reported but cannot leak.
    """
    dropped: list[str] = []
    if not isinstance(limits, dict):
        return {}, dropped
    safe: dict[str, Any] = {}
    for key, value in limits.items():
        if key in RATE_LIMIT_SCALARS:
            kept = _whitelisted_scalar(value, RATE_LIMIT_SCALARS[key])
            if kept is None and value is not None:
                dropped.append(key)
                continue
            safe[key] = kept
        elif key in RATE_LIMIT_WINDOW_KEYS:
            safe[key] = _whitelisted_group(value, RATE_LIMIT_WINDOWS, key, dropped)
        elif key == "credits":
            safe[key] = _whitelisted_group(value, RATE_LIMIT_CREDITS, key, dropped)
        else:
            dropped.append(key)
    return safe, dropped


def _timestamp(value: Any) -> str | None:
    """An ISO-8601 instant, or ``None``. Never a free-text passthrough.

    The timestamp is the only non-``rate_limits`` value R4 copies, so it is
    parsed before it is kept: a field carrying anything but an instant is
    dropped rather than promoted.
    """
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def rate_limit_snapshots(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Every provider quota snapshot in one rollout, reduced to the whitelist.

    Mirrors ``evallab.quota._rate_limit_snapshots``: a ``token_count`` event
    whose payload carries ``rate_limits``. Only that subtree and the event
    timestamp are read; the rest of the line is never touched.
    """
    snapshots: list[dict[str, Any]] = []
    dropped: set[str] = set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if len(snapshots) >= MAX_QUOTA_SNAPSHOTS:
            break
        if "rate_limits" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        observed_at = _timestamp(event.get("timestamp"))
        limits, missed = redact_rate_limits(payload.get("rate_limits"))
        dropped.update(missed)
        if observed_at is None or not limits:
            continue
        snapshots.append({"timestamp": observed_at, "rate_limits": limits})
    snapshots.sort(key=lambda item: item["timestamp"])
    return snapshots, sorted(dropped)


def stream_rollout_digest_and_quota(
    relative: Path, path: Path
) -> tuple[str, int, bytes | None]:
    """Single-pass streaming: compute SHA-256 digest, byte count, and quota sidecar document.

    Reads line-by-line with bounded line lengths and snapshot limits so that digest and sidecar
    are derived from a single coherent file pass without unbounded memory.
    """
    h = hashlib.sha256()
    total_bytes = 0
    snapshots: list[dict[str, Any]] = []
    dropped: set[str] = set()

    with path.open("rb") as f:
        while chunk := f.readline(MAX_ROLLOUT_LINE_BYTES):
            total_bytes += len(chunk)
            h.update(chunk)
            if len(snapshots) >= MAX_QUOTA_SNAPSHOTS or b"rate_limits" not in chunk:
                continue
            try:
                line_str = chunk.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                event = json.loads(line_str)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            observed_at = _timestamp(event.get("timestamp"))
            limits, missed = redact_rate_limits(payload.get("rate_limits"))
            dropped.update(missed)
            if observed_at is None or not limits:
                continue
            snapshots.append({"timestamp": observed_at, "rate_limits": limits})

    digest = f"sha256:{h.hexdigest()}"
    snapshots.sort(key=lambda item: item["timestamp"])
    if not snapshots:
        return digest, total_bytes, None

    doc = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "rule": "R4",
        "kind": "evallab-rate-limits-sidecar",
        "source_path": str(relative),
        "source_bytes": total_bytes,
        "source_sha256": digest,
        "source_omitted_by_rule": "R2",
        "kept": (
            "the event timestamp and a whitelist of payload.rate_limits scalars; "
            "no message, prompt, reasoning, session title or token is read"
        ),
        "dropped_field_names": sorted(dropped),
        "snapshot_count": len(snapshots),
        "limits": (
            "account-scope, not the lab's share; a point-in-time snapshot, not a "
            "series; only as fresh as the trial that recorded it. See "
            "docs/quota-accounting.md."
        ),
        "snapshots": snapshots,
    }
    body = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(body) > MAX_SIDECAR_BYTES:
        while len(body) > MAX_SIDECAR_BYTES and len(snapshots) > 1:
            snapshots = snapshots[: len(snapshots) // 2]
            doc["snapshots"] = snapshots
            doc["snapshot_count"] = len(snapshots)
            body = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    return digest, total_bytes, body


def sidecar_path(relative: Path) -> Path:
    """Where the sidecar for an omitted rollout goes: ``<trial>/agent/quota/``.

    Deliberately *not* under ``agent/sessions/``. That prefix is the structural
    signal that a path holds raw model I/O, and ``git ls-files`` finding nothing
    under it in committed evidence must stay a true check.
    Supports case-folded agent components.
    """
    parts = relative.parts
    norm_parts = [_canonical(p) for p in parts]
    if "agent" in norm_parts:
        agent_idx = norm_parts.index("agent")
    else:
        agent_idx = len(parts) - 2 if len(parts) >= 2 else 0
    stem = relative.name.removesuffix(".jsonl")
    return Path(*parts[: agent_idx + 1], SIDECAR_DIRNAME, f"{stem}{SIDECAR_SUFFIX}")


def rate_limits_sidecar(relative: Path, raw: bytes) -> bytes | None:
    """R4: the redacted quota sidecar for one omitted rollout, or ``None``.

    ``None`` when the file is not a rollout or records no quota snapshot, so a
    bundle only grows a sidecar where there is a reading to preserve.
    """
    canonical_name = _canonical(relative.name)
    if not canonical_name.startswith(ROLLOUT_PREFIX) or not canonical_name.endswith(".jsonl"):
        return None
    snapshots, dropped = rate_limit_snapshots(raw)
    if not snapshots:
        return None
    document = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "rule": "R4",
        "kind": "evallab-rate-limits-sidecar",
        "source_path": str(relative),
        "source_bytes": len(raw),
        "source_sha256": sha256_bytes(raw),
        "source_omitted_by_rule": "R2",
        "kept": (
            "the event timestamp and a whitelist of payload.rate_limits scalars; "
            "no message, prompt, reasoning, session title or token is read"
        ),
        "dropped_field_names": dropped,
        "snapshot_count": len(snapshots),
        "limits": (
            "account-scope, not the lab's share; a point-in-time snapshot, not a "
            "series; only as fresh as the trial that recorded it. See "
            "docs/quota-accounting.md."
        ),
        "snapshots": snapshots,
    }
    return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def omit_r2(relative: Path) -> bool:
    """Whether a path is raw model I/O or runtime state that R2 omits.

    Pure path inspection -- never dereferences, never stats the target -- so it
    is safe to call on symlinks whose targets may be gone or hostile.
    ``agent/sessions/**`` and ``agent/codex.txt`` are the Codex rollout/raw
    event streams. ``agent/opencode.txt`` and the whole ``agent/opencode/**``
    tree are OpenCode's raw stream plus its SQLite/WAL/log/snapshot/auth
    runtime state. Harbor's root ``job.log`` and per-trial ``trial.log`` repeat
    the unredacted task instruction and command, so they are raw prompt-bearing
    runtime streams rather than durable evidence.
    Unicode-normalized (NFC) and case-folded matching prevents bypass via
    confusables, case variations, or alternative decomposition forms.
    """
    norm_name = _canonical(relative.name)
    if norm_name in FORBIDDEN_BASENAMES:
        return True
    norm_parts = [_canonical(p) for p in relative.parts]
    if "agent" not in norm_parts:
        return False
    return "sessions" in norm_parts or "opencode" in norm_parts


def _omission_reason(relative: Path) -> str:
    """Deterministic human-readable explanation for an R2 omission."""
    norm_name = _canonical(relative.name)
    if norm_name in FORBIDDEN_BASENAMES:
        return f"R2: forbidden basename '{relative.name}'"
    norm_parts = [_canonical(p) for p in relative.parts]
    if "agent" in norm_parts:
        if "sessions" in norm_parts:
            return "R2: raw session rollout stream under agent/sessions"
        if "opencode" in norm_parts:
            return "R2: OpenCode runtime state under agent/opencode"
    return "R2: raw prompt or runtime state"


def classify(relative: Path) -> str:
    if omit_r2(relative):
        return "omit-R2"
    norm_name = _canonical(relative.name)
    norm_parts = [_canonical(p) for p in relative.parts]
    if norm_name == "trajectory.json" and "agent" in norm_parts:
        return "redact-R1"
    if "verifier" in norm_parts:
        return "maybe-redact-R3"
    return "verbatim"


def omission_record(
    relative: Path,
    *,
    entry_type: str,
    raw: bytes | None = None,
    link_target: str | None = None,
    reason: str | None = None,
    rule: str = "R2",
    digest: str | None = None,
    size: int | None = None,
) -> dict[str, Any]:
    """An omission entry.

    A ``file`` records the SHA-256/length of the source bytes. A ``symlink``
    records the SHA-256/length of its *link-target string* only -- the link
    itself, never the target's content, which would disclose the target and
    cannot be read for a broken link anyway. This is the schema ``verify``
    re-checks source-free.
    """
    record: dict[str, Any] = {
        "source_path": str(relative),
        "promoted_path": None,
        "action": "omitted",
        "rule": rule,
        "entry_type": entry_type,
    }
    if reason is not None:
        record["reason"] = reason
    if entry_type == "symlink":
        assert link_target is not None
        target_bytes = link_target.encode("utf-8")
        record["link_target"] = link_target
        record["source_bytes"] = len(target_bytes)
        record["source_sha256"] = sha256_bytes(target_bytes)
    else:
        if digest is not None and size is not None:
            record["source_bytes"] = size
            record["source_sha256"] = digest
        else:
            assert raw is not None
            record["source_bytes"] = len(raw)
            record["source_sha256"] = sha256_bytes(raw)
    return record


def promote(job_dir: Path, destination: Path, *, force: bool = False) -> dict[str, Any]:
    # Validate destination basename and strict parent containment before any existence or delete
    _validate_job_name(destination.name)
    resolved_dest = destination.resolve()
    resolved_dest_parent = destination.parent.resolve()
    if (
        resolved_dest == resolved_dest_parent
        or resolved_dest.parent != resolved_dest_parent
        or str(destination) in {".", "..", "/", ""}
    ):
        raise SystemExit(
            f"destination must be a strict child of its parent directory: {destination}"
        )

    # Validate source basename and strict parent containment
    _validate_job_name(job_dir.name)
    resolved_source = job_dir.resolve()
    resolved_source_parent = job_dir.parent.resolve()
    if (
        resolved_source == resolved_source_parent
        or resolved_source.parent != resolved_source_parent
        or str(job_dir) in {".", "..", "/", ""}
    ):
        raise SystemExit(
            f"source job directory must be a strict child of its parent: {job_dir}"
        )

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
    for source in sorted(job_dir.rglob("*")):
        relative = source.relative_to(job_dir)
        # Path traversal guard: refuse paths that contain '..' or '.' or are absolute
        if any(part in {"..", ".", ""} for part in relative.parts) or relative.is_absolute():
            raise SystemExit(f"refusing path with invalid component: {relative}")

        # Symlinks are enumerated explicitly and handled by path alone. Their
        # content is never read: a live link would dereference the target
        # (disclosing its bytes into the digest), and a broken link cannot be
        # read at all. Only the link-target *string* is recorded.
        if source.is_symlink():
            if not omit_r2(relative):
                raise SystemExit(
                    "refusing to promote a symlink outside an R2 omission path: "
                    f"{relative} (promotion never dereferences symlinks)"
                )
            try:
                link_target = str(source.readlink())
            except OSError as exc:  # pragma: no cover - filesystem failure
                raise SystemExit(f"cannot read link target for {relative}: {exc}") from exc
            reason = _omission_reason(relative)
            entries.append(
                omission_record(
                    relative,
                    entry_type="symlink",
                    link_target=link_target,
                    reason=reason,
                )
            )
            continue

        # Skip real directories before non-regular file inspection
        if source.is_dir():
            continue

        try:
            st = source.lstat()
        except OSError as exc:
            raise SystemExit(f"cannot stat source file {relative}: {exc}") from exc

        # Non-regular files (device nodes, FIFOs, sockets)
        if not stat.S_ISREG(st.st_mode):
            if omit_r2(relative):
                reason = f"hardened: non-regular file under R2 (mode {oct(st.st_mode)})"
                digest, size = _stream_sha256(source) if source.is_file() else (sha256_bytes(b""), 0)
                entries.append(
                    omission_record(
                        relative,
                        entry_type="file",
                        digest=digest,
                        size=size,
                        reason=reason,
                        rule="R2",
                    )
                )
                continue
            raise SystemExit(
                f"refusing to promote non-regular file outside R2: {relative} (mode {oct(st.st_mode)})"
            )

        # Hardlink refusal outside R2
        if st.st_nlink > 1 and not omit_r2(relative):
            raise SystemExit(
                f"refusing to promote hardlinked file outside R2: {relative} (nlink {st.st_nlink})"
            )

        # Archive/container payload rejection (outside R2)
        if not omit_r2(relative) and _is_archive_like(relative):
            digest, size = _stream_sha256(source)
            reason = f"hardened: archive/container payload '{relative.name}'"
            entries.append(
                omission_record(
                    relative,
                    entry_type="file",
                    digest=digest,
                    size=size,
                    reason=reason,
                    rule="hardened",
                )
            )
            continue

        action = classify(relative)

        if action == "omit-R2":
            reason = _omission_reason(relative)
            is_rollout = (
                _canonical(relative.name).startswith(ROLLOUT_PREFIX)
                and _canonical(relative.name).endswith(".jsonl")
            )

            if is_rollout:
                parent_digest, file_size, body = stream_rollout_digest_and_quota(
                    relative, source
                )
                raw_bytes = None
            elif st.st_size > MAX_SOURCE_BYTES:
                parent_digest, file_size = _stream_sha256(source)
                raw_bytes = None
                body = None
            else:
                raw_bytes = source.read_bytes()
                parent_digest = sha256_bytes(raw_bytes)
                file_size = len(raw_bytes)
                body = None

            omission = omission_record(
                relative,
                entry_type="file",
                raw=raw_bytes,
                digest=parent_digest,
                size=file_size,
                reason=reason,
            )
            entries.append(omission)

            if body is None:
                continue
            target = sidecar_path(relative)
            omission["quota_sidecar_path"] = str(target)
            out = destination / target
            _assert_within(destination, out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            promoted_bytes += len(body)
            entries.append(
                {
                    "source_path": str(relative),
                    "promoted_path": str(target),
                    "action": "redacted",
                    "rule": "R4",
                    "derived_from": str(relative),
                    "source_bytes": file_size,
                    "source_sha256": parent_digest,
                    "promoted_bytes": len(body),
                    "promoted_sha256": sha256_bytes(body),
                }
            )
            continue

        # Streaming size limit check for promoted files
        if st.st_size > MAX_SOURCE_BYTES:
            digest, size = _stream_sha256(source)
            reason = f"hardened: oversized file ({size} bytes > {MAX_SOURCE_BYTES})"
            entries.append(
                omission_record(
                    relative,
                    entry_type="file",
                    digest=digest,
                    size=size,
                    reason=reason,
                    rule="hardened",
                )
            )
            continue

        raw = source.read_bytes()
        parent_digest = sha256_bytes(raw)

        if action == "redact-R1":
            body = redact_trajectory(raw)
            target = relative
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
        _assert_within(destination, out)
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

    # A sidecar is derived from a source file already counted, so counting it as
    # a source file would inflate `source_files` and double-count its bytes.
    sources = [e for e in entries if "derived_from" not in e]
    job_result = job_dir / "result.json"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle": destination.name,
        "source_job_runtime_path": f"runs/{job_dir.name}",
        "source_job_result_sha256": _stream_sha256(job_result)[0] if job_result.is_file() else None,
        "promoted_by": "scripts/promote_codex_bundle.py",
        "redaction_rules": {
            "R1": "system/user ATIF step message text removed; sha256 and length kept; nested secret-shaped JSON keys redacted",
            "R2": "agent/sessions/**, agent/codex.txt, agent/opencode.txt, agent/opencode/**, job.log, trial.log, and forbidden basenames (Unicode-normalized and case-folded) omitted; sha256 recorded; symlinks digest-recorded by link-target string and never dereferenced; non-R2 symlinks fail closed",
            "R3a": (
                "verifier/* JSON string values over "
                f"{VERIFIER_JSON_STRING_LIMIT} bytes or secret-shaped keys replaced by digest markers"
            ),
            "R3b": (
                f"verifier/* text files over {VERIFIER_TEXT_LIMIT} bytes promoted "
                "as a whole-file digest marker with no body"
            ),
            "R4": (
                "each omitted rollout leaves a quota sidecar under "
                f"<trial>/agent/{SIDECAR_DIRNAME}/ holding only the event timestamp "
                "and a whitelist of payload.rate_limits scalars, with the parent "
                "rollout's sha256; account-scope point-in-time readings, no message, "
                "prompt, reasoning, session title or token"
            ),
            "hardened": "archive/container payloads (.zip, .tar, .gz, etc.), non-regular devices, hardlinks outside R2, oversized files, and path traversal attempts are refused or omitted",
        },
        "totals": {
            "source_files": len(sources),
            "promoted_files": sum(1 for e in sources if e["promoted_path"]),
            "omitted_files": sum(1 for e in sources if not e["promoted_path"]),
            "quota_sidecars": len(entries) - len(sources),
            "promoted_bytes": promoted_bytes,
            "source_bytes": sum(e["source_bytes"] for e in sources),
        },
        "files": entries,
    }
    manifest_out = destination / MANIFEST_NAME
    _assert_within(destination, manifest_out)
    manifest_out.write_text(
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
        manifest_v2 = manifest.get("schema_version") == SCHEMA_VERSION
        if not manifest_v2:
            print(
                f"NON-V2 MANIFEST {bundle.name}: every bundle must use "
                f"schema v{SCHEMA_VERSION}"
            )
            failures += 1

        # Promotion must never produce a symlink; reject any that appear.
        for path in bundle.rglob("*"):
            if path.is_symlink():
                print(f"UNALLOWED SYMLINK {bundle.name}/{path.relative_to(bundle)}")
                failures += 1

        for entry in manifest["files"]:
            # Validate the omission record schema, source-free. Every bundle
            # is schema v2, which requires ``entry_type`` on every omission and
            # ``link_target``/length/hash on symlink omissions. Deleting those
            # fields or downgrading the manifest is therefore caught.
            if entry.get("action") == "omitted":
                name = f"{bundle.name}/{entry.get('source_path')}"
                if entry.get("promoted_path") is not None:
                    print(f"BAD OMISSION {name}: promoted_path must be null")
                    failures += 1
                entry_type = entry.get("entry_type")
                if entry_type not in {"file", "symlink"}:
                    print(
                        f"BAD OMISSION {name}: v2 record missing/invalid "
                        f"entry_type {entry_type!r}"
                    )
                    failures += 1
                if entry_type == "symlink":
                    target = entry.get("link_target")
                    if not isinstance(target, str) or not target:
                        print(f"BAD OMISSION {name}: symlink missing link_target")
                        failures += 1
                        continue
                    target_bytes = target.encode("utf-8")
                    if entry.get("source_sha256") != sha256_bytes(target_bytes):
                        print(f"BAD OMISSION {name}: link_target sha256 mismatch")
                        failures += 1
                    if entry.get("source_bytes") != len(target_bytes):
                        print(f"BAD OMISSION {name}: link_target length mismatch")
                        failures += 1
                else:
                    if not entry.get("source_sha256") or not isinstance(entry.get("source_bytes"), int):
                        print(f"BAD OMISSION {name}: file missing source_sha256 or source_bytes")
                        failures += 1
            promoted = entry.get("promoted_path")
            if not promoted:
                continue

            # Strict containment check: reject path traversal in manifest promoted_path
            p_obj = Path(promoted)
            if any(p in {"..", ".", ""} for p in p_obj.parts) or p_obj.is_absolute():
                print(f"PATH TRAVERSAL IN MANIFEST {bundle.name}/{promoted}")
                failures += 1
                continue

            path = bundle / promoted
            resolved_bundle = bundle.resolve()
            resolved_path = path.resolve()
            if resolved_path != resolved_bundle and resolved_bundle not in resolved_path.parents:
                print(f"PATH TRAVERSAL IN MANIFEST {bundle.name}/{promoted}")
                failures += 1
                continue

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
        sidecars = sum(1 for e in manifest["files"] if e.get("rule") == "R4")
        print(
            f"{manifest_path.parent.name}: "
            f"{len(manifest['files']) - sidecars} source files recorded"
            + (f", {sidecars} quota sidecars" if sidecars else "")
        )
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

    source_root = args.source_runs.resolve()
    evidence_root = args.evidence_runs.resolve()
    if not source_root.is_dir():
        raise SystemExit(f"source runs directory not found: {args.source_runs}")

    # Validate every job name before any filesystem operations or delete
    validated_jobs: list[tuple[str, Path, Path]] = []
    for job in args.job:
        safe_job = _validate_job_name(job)
        job_source = (source_root / safe_job).resolve()
        job_dest = (evidence_root / safe_job).resolve()
        if job_source.parent != source_root:
            raise SystemExit(f"invalid job source path escape: {job}")
        if job_dest.parent != evidence_root:
            raise SystemExit(f"invalid job destination path escape: {job}")
        validated_jobs.append((safe_job, job_source, job_dest))

    total = 0
    for job_name, job_source, job_dest in validated_jobs:
        manifest = promote(job_source, job_dest, force=args.force)
        totals = manifest["totals"]
        total += totals["promoted_bytes"]
        print(
            f"{job_name}: {totals['promoted_files']} promoted "
            f"({totals['promoted_bytes']} B) "
            f"{totals['omitted_files']} omitted "
            f"({totals['quota_sidecars']} quota sidecar(s) kept) "
            f"from {totals['source_bytes']} B source"
        )
    print(f"total promoted bytes: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
