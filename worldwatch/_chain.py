from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import (
    check_no_symlink_ancestors,
    read_regular_text,
    read_regular_window,
    read_tolerant_json_object,
    split_text_lines_strict,
)
from worldwatch.model import (
    GENESIS_HASH,
    MAX_FILE_BYTES,
    compute_entry_hash,
    compute_entry_hmac,
    is_sha256_hex,
    parse_strict_json,
    strict_int,
)


def _match_entry_hash(row: Mapping[str, Any], eh: str) -> tuple[str | None, str | None]:
    primary_err: str | None = None
    try:
        computed = compute_entry_hash(row, ensure_ascii=False)
        if hmac.compare_digest(eh, computed):
            return computed, None
    except ValueError as exc:
        primary_err = str(exc)
    try:
        fallback = compute_entry_hash(row, ensure_ascii=True)
    except ValueError as exc:
        return None, primary_err or str(exc)
    if hmac.compare_digest(eh, fallback):
        return fallback, None
    if primary_err is not None:
        return None, primary_err
    return None, "entry_hash mismatch"


def verify_event_row_chain(
    row: Mapping[str, Any],
    expected_seq: int,
    prev_hash: str,
    chain_mode: bool | None,
    has_hmac: bool,
    require_seq: bool,
    hmac_key: str | None = None,
) -> tuple[bool | None, str, bool, str | None]:
    assert expected_seq >= 1
    seq_val = strict_int(row.get("seq"))
    if require_seq and seq_val != expected_seq:
        return chain_mode, prev_hash, has_hmac, f"line {expected_seq}: expected int seq={expected_seq}, got {row.get('seq')!r}"
    row_hashed = "entry_hash" in row or "prev_hash" in row or "hmac" in row
    if chain_mode is None:
        chain_mode = False if (not row_hashed and expected_seq > 1) else row_hashed
    elif chain_mode != row_hashed:
        return chain_mode, prev_hash, has_hmac, f"line {expected_seq}: mixed hashed and unhashed rows"
    if not chain_mode:
        return False, prev_hash, has_hmac, None
    has_hmac = has_hmac or ("hmac" in row)
    ph, eh = row.get("prev_hash"), row.get("entry_hash")
    if not is_sha256_hex(ph) or not is_sha256_hex(eh):
        return True, prev_hash, has_hmac, f"line {expected_seq}: missing or malformed prev_hash/entry_hash"
    assert isinstance(ph, str) and isinstance(eh, str)
    if not hmac.compare_digest(ph, prev_hash):
        return True, prev_hash, has_hmac, f"line {expected_seq}: prev_hash mismatch"
    matched, herr = _match_entry_hash(row, eh)
    if matched is None:
        msg = herr if herr == "entry_hash mismatch" else f"canonical hash failure: {herr}"
        return True, prev_hash, has_hmac, f"line {expected_seq}: {msg}"
    if has_hmac:
        mac = row.get("hmac")
        if not is_sha256_hex(mac):
            return True, prev_hash, has_hmac, f"line {expected_seq}: missing or malformed hmac"
        if hmac_key is not None:
            assert isinstance(mac, str)
            if not hmac.compare_digest(mac, compute_entry_hmac(matched, hmac_key)):
                return True, prev_hash, has_hmac, f"line {expected_seq}: hmac verification failed"
    return True, eh, has_hmac, None


def read_jsonl_static(
    path: Path,
    *,
    require_seq: bool = False,
    hmac_key: str | None = None,
    root: Path | None = None,
) -> tuple[list[dict[str, Any]], bool | None, str | None]:
    anc_err = check_no_symlink_ancestors(path, root=root)
    if anc_err is not None:
        return [], False, anc_err
    if not os.path.lexists(path):
        return [], None, None
    text, err = read_regular_text(path, max_bytes=MAX_FILE_BYTES, root=root)
    if err is not None or text is None:
        return [], False, err or f"cannot read {path.name}"
    try:
        lines = split_text_lines_strict(text)
    except ValueError as exc:
        return [], False, str(exc)
    rows: list[dict[str, Any]] = []
    chain_mode: bool | None = None
    prev_hash = GENESIS_HASH
    has_hmac = False
    first_err: str | None = None
    seq_idx = 0
    for line_no, line in enumerate(lines, start=1):
        if line == "":
            first_err = first_err or f"line {line_no}: blank line in JSONL"
            continue
        try:
            parsed = parse_strict_json(line)
        except ValueError as exc:
            first_err = first_err or f"line {line_no}: {exc}"
            continue
        if not isinstance(parsed, dict):
            first_err = first_err or f"line {line_no}: JSON row is not an object"
            continue
        seq_idx += 1
        chain_mode, prev_hash, has_hmac, cerr = verify_event_row_chain(
            parsed, seq_idx, prev_hash, chain_mode, has_hmac, require_seq, hmac_key
        )
        if cerr is not None and first_err is None:
            first_err = cerr
        rows.append(parsed)
    if first_err is not None:
        return rows, False, first_err
    return rows, (True if chain_mode is True else None), None


def _has_olam_world_markers(trial_dir: Path) -> bool:
    seats_dir = trial_dir / "seats"
    if os.path.isdir(seats_dir) and not os.path.islink(seats_dir):
        return True
    for name in ("result.json", "config.json"):
        doc, err = read_tolerant_json_object(trial_dir / name, root=trial_dir)
        if err is not None or doc is None:
            continue
        if doc.get("seat_results") is not None:
            return True
        parts = doc.get("participants")
        cfg_parts = ((doc.get("config") or {}).get("participants")) if isinstance(doc.get("config"), Mapping) else None
        if (isinstance(parts, list) and len(parts) > 0) or (isinstance(cfg_parts, list) and len(cfg_parts) > 0):
            return True
    return False


def _looks_like_world_event_file(cand: Path, trial_dir: Path) -> bool:
    head = read_regular_window(cand, 0, 1, root=trial_dir)
    if head is None:
        return False
    text, err = read_regular_text(cand, max_bytes=MAX_FILE_BYTES, root=trial_dir)
    if err is not None or not text:
        return False
    first_line = text.split("\n", 1)[0].rstrip("\r")
    if not first_line:
        return False
    try:
        obj = parse_strict_json(first_line)
    except ValueError:
        return False
    return (
        isinstance(obj, dict)
        and strict_int(obj.get("seq")) == 1
        and isinstance(obj.get("kind"), str)
        and ("ok" in obj or "status" in obj)
    )


def find_bespoke_board_log(trial_dir: Path) -> Path | None:
    for rel in ("agent/board-events.jsonl", "board-events.jsonl"):
        cand = trial_dir / rel
        if check_no_symlink_ancestors(cand, root=trial_dir) is not None or os.path.lexists(cand):
            return cand
    return None


def find_olam_proxy_log(trial_dir: Path) -> Path | None:
    for rel in ("proxy-usage.jsonl", "artifacts/proxy-usage.jsonl", "artifacts/logs/proxy/proxy-usage.jsonl"):
        cand = trial_dir / rel
        if check_no_symlink_ancestors(cand, root=trial_dir) is not None or os.path.lexists(cand):
            return cand
    return None


def find_olam_event_log(trial_dir: Path) -> Path | None:
    for rel in (
        "verifier/events.jsonl",
        "logs/world/events.jsonl",
        "artifacts/logs/world/events.jsonl",
    ):
        cand = trial_dir / rel
        if check_no_symlink_ancestors(cand, root=trial_dir) is not None:
            return cand
        if os.path.lexists(cand):
            return cand
    art = trial_dir / "artifacts"
    is_world = _has_olam_world_markers(trial_dir)
    if os.path.islink(art):
        return (art / "events.jsonl") if is_world else None
    if os.path.isdir(art):
        stack = [art]
        steps = 0
        found: list[Path] = []
        while stack:
            steps += 1
            if steps > 512:
                break
            cur = stack.pop()
            try:
                for entry in os.scandir(cur):
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False) and entry.name == "events.jsonl":
                        found.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        stack.append(Path(entry.path))
            except OSError:
                continue
        found.sort()
        for cand in found:
            if is_world or _looks_like_world_event_file(cand, trial_dir):
                return cand
    return None
