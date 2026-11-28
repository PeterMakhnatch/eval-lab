from __future__ import annotations

import copy
import hmac
import importlib.util
import json
import math
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from muster import events

TOLERANCE = 1e-6
HEX = frozenset("0123456789abcdef")
MAX_CHAIN_LINES = 1_000_000
MAX_CHAIN_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 65_536
_TRANSIENT_MODS = ("worldkit", "codec")


def _wrap_task_scorer(
    raw_score: Callable[[list[dict], dict], dict], world: Path, task_mods: dict[str, ModuleType]
) -> Callable[[list[dict], dict], dict]:
    world_dir = str(world)

    def isolated_score(events: list[dict], instance: dict) -> dict:
        saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
        caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
        for name in _TRANSIENT_MODS:
            sys.modules.pop(name, None)
        sys.modules.update(task_mods)
        if world_dir in sys.path:
            sys.path.remove(world_dir)
        sys.path.insert(0, world_dir)
        sys.dont_write_bytecode = True
        try:
            result = raw_score(copy.deepcopy(events), copy.deepcopy(instance))
            for name in _TRANSIENT_MODS:
                if name in sys.modules:
                    task_mods[name] = sys.modules[name]
            return result
        finally:
            sys.path[:] = saved_path
            sys.dont_write_bytecode = saved_flag
            for name in _TRANSIENT_MODS:
                sys.modules.pop(name, None)
            sys.modules.update(caller_mods)

    return isolated_score


def load_score(task: Path) -> tuple[Callable[[list[dict], dict], dict] | None, dict | None, str | None]:
    verify, world = task / "tests" / "verify.py", task / "environment" / "world"
    if not verify.is_file():
        return None, None, "the task has no tests/verify.py"
    try:
        instance = json.loads((world / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        return None, None, f"cannot read the task's instance.json: {type(error).__name__}"
    if not isinstance(instance, dict):
        return None, None, "the task's instance.json is not a JSON object"
    mod_name = f"muster_verify_{abs(hash(str(task.resolve())))}"
    spec = importlib.util.spec_from_file_location(mod_name, verify)
    if spec is None or spec.loader is None:
        return None, None, "cannot import tests/verify.py"
    module = importlib.util.module_from_spec(spec)
    saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
    caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
    had_prev, prev_mod = (mod_name in sys.modules), sys.modules.get(mod_name)
    task_mods: dict[str, ModuleType] = {}
    for name in _TRANSIENT_MODS:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(world))
    sys.dont_write_bytecode = True
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
        for name in _TRANSIENT_MODS:
            if name in sys.modules:
                task_mods[name] = sys.modules[name]
    except (Exception, SystemExit) as error:
        if had_prev and prev_mod is not None:
            sys.modules[mod_name] = prev_mod
        else:
            sys.modules.pop(mod_name, None)
        return None, None, f"tests/verify.py does not import: {type(error).__name__}: {error}"
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        for name in _TRANSIENT_MODS:
            sys.modules.pop(name, None)
        sys.modules.update(caller_mods)
    raw_score = getattr(module, "score", None)
    if not callable(raw_score):
        return None, None, "tests/verify.py has no score(events, instance)"
    return _wrap_task_scorer(raw_score, world, task_mods), copy.deepcopy(instance), None


def replay(
    score: Callable[[list[dict], dict], dict],
    event_rows: list[dict],
    instance: dict,
    *,
    hmac_key: str | None = None,
) -> tuple[dict, str | None]:
    if not isinstance(instance, dict):
        return {}, "instance is not a JSON object"
    caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
    had_key = "WORLD_HMAC_KEY" in os.environ
    saved_key = os.environ.get("WORLD_HMAC_KEY")
    if hmac_key is not None:
        os.environ["WORLD_HMAC_KEY"] = hmac_key
    try:
        raw = score(copy.deepcopy(event_rows), copy.deepcopy(instance))
    except (Exception, SystemExit) as error:
        return {}, f"score() failed: {type(error).__name__}: {error}"
    finally:
        if hmac_key is not None:
            if had_key and saved_key is not None:
                os.environ["WORLD_HMAC_KEY"] = saved_key
            else:
                os.environ.pop("WORLD_HMAC_KEY", None)
        for name in _TRANSIENT_MODS:
            if name not in caller_mods:
                sys.modules.pop(name, None)
        sys.modules.update(caller_mods)
    if not isinstance(raw, dict):
        return {}, f"score() returned {type(raw).__name__}, expected dict"
    rewards: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if not isinstance(value, int | float):
            return {}, f"score() gave non-numeric value for {key!r}"
        try:
            num = float(value)
        except OverflowError:
            return {}, "score() gave a value that is not a finite number"
        if not math.isfinite(num):
            return {}, "score() gave a value that is not a finite number"
        rewards[str(key)] = round(num, 6)
    if not rewards:
        return {}, "score() returned no numeric rewards"
    return rewards, None


def differences(recorded: dict[str, float], replayed: dict[str, float]) -> dict[str, list[float | None]]:
    return {
        key: [recorded.get(key), replayed.get(key)]
        for key in sorted(set(recorded) | set(replayed))
        if key not in recorded or key not in replayed or abs(recorded[key] - replayed[key]) > TOLERANCE
    }


def late_actions(graded: list[dict], collected: list[dict]) -> list[dict]:
    late = []
    for event in graded[len(collected) :]:
        if event.get("kind") == "admin.close" and event.get("ok"):
            break
        if event.get("ok") and event.get("seat") is not None:
            late.append({"seq": event["seq"], "seat": event["seat"], "kind": event["kind"]})
    assert len(late) <= max(0, len(graded) - len(collected))
    return late


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in HEX for c in value)


def read_chain_text(path: Path, max_bytes: int = MAX_CHAIN_BYTES) -> tuple[str | None, str | None]:
    assert max_bytes >= 1
    target = Path(path)
    try:
        info = os.lstat(target)
    except OSError as error:
        return None, f"cannot read {target.name}: {error.strerror or error}"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{target.name} is not a regular file"
    if info.st_size > max_bytes:
        return None, f"{target.name} exceeds {max_bytes} bytes"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        return None, f"cannot read {target.name}: {error.strerror or error}"
    chunks: list[bytes] = []
    total = 0
    max_steps = max_bytes // _READ_CHUNK + 3
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, f"{target.name} is not a regular file"
        for _ in range(max_steps):
            want = max(1, min(_READ_CHUNK, max_bytes + 1 - total))
            piece = os.read(descriptor, want)
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > max_bytes:
                return None, f"{target.name} exceeds {max_bytes} bytes"
    except OSError as error:
        return None, f"cannot read {target.name}: {error.strerror or error}"
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{target.name} is not UTF-8 text"


def _link_problem(
    event: object, idx: int, previous: str, hmac_key: str | None, *, has_hmac: bool = False
) -> str | None:
    assert idx >= 1
    if not isinstance(event, dict):
        return "is not a JSON object"
    if type(event.get("seq")) is not int or event["seq"] != idx:
        return f"is not event {idx}"
    if not _is_digest(event.get("prev_hash")) or not _is_digest(event.get("entry_hash")):
        return "has no prev_hash and entry_hash"
    if not hmac.compare_digest(event["prev_hash"], previous):
        return "does not follow the line before it (prev_hash differs)"
    try:
        digest = events.entry_hash(event)
    except ValueError:
        return "holds a value that has no canonical JSON form"
    if not hmac.compare_digest(event["entry_hash"], digest):
        return "was changed after it was written (entry_hash differs)"
    if hmac_key is None:
        return "has no valid hmac digest" if has_hmac and not _is_digest(event.get("hmac")) else None
    given = event.get("hmac")
    if not isinstance(given, str) or not hmac.compare_digest(given, events.entry_hmac(digest, hmac_key)):
        return "has no valid hmac for the key"
    return None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token!r}")


def parse_strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)


def read_json_bounded(path: Path, max_bytes: int = 16 * 1024 * 1024) -> tuple[Any, str | None]:
    text, err = read_chain_text(path, max_bytes)
    if err or text is None:
        return None, err
    try:
        return json.loads(text), None
    except (ValueError, RecursionError) as error:
        return None, f"{path.name} is not valid JSON: {error}"


def check_event_chain(
    text: str, hmac_key: str | None = None, expected_head: str | None = None
) -> tuple[bool | None, str | None, int]:
    lines = text.split("\n")
    if len(lines) > MAX_CHAIN_LINES + 1:
        return False, f"the event log exceeds {MAX_CHAIN_LINES} lines", MAX_CHAIN_LINES
    rows: list[tuple[int, str, dict[str, Any]]] = []
    torn: str | None = None
    for number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.removesuffix("\r")
        if not stripped.strip():
            continue
        try:
            parsed = parse_strict_json(stripped)
        except (ValueError, RecursionError):
            torn = torn or f"line {number} is not valid JSON (torn or changed)"
            continue
        if not isinstance(parsed, dict):
            torn = torn or f"line {number} is not a JSON object"
            continue
        rows.append((number, stripped, parsed))
    if torn is not None:
        return False, torn, len(rows)
    hashed = ["entry_hash" in row or "prev_hash" in row or "hmac" in row for _, _, row in rows]
    has_hmac = any("hmac" in row for _, _, row in rows)
    if not any(hashed):
        if hmac_key is not None or expected_head is not None:
            return False, "log has no hash chain", len(rows)
        return None, None, len(rows)
    previous = events.GENESIS_HASH
    for idx, (number, stripped, row) in enumerate(rows, start=1):
        problem = _link_problem(row, idx, previous, hmac_key, has_hmac=has_hmac)
        if problem is not None:
            return False, f"line {number} {problem}", len(rows)
        canon = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if stripped != canon:
            return False, f"line {number} is not in canonical JSON form", len(rows)
        previous = row["entry_hash"]
    if expected_head is not None and not hmac.compare_digest(previous, expected_head):
        return False, "the last line is not the recorded head (lines were removed at the end)", len(rows)
    return True, None, len(rows)


def verify_event_chain(
    events_path: Path, hmac_key: str | None = None, *, expected_head: str | None = None
) -> tuple[bool, str | None]:
    text, error = read_chain_text(events_path, MAX_CHAIN_BYTES)
    if error is not None or text is None:
        return False, error
    status, problem, _ = check_event_chain(text, hmac_key, expected_head)
    assert (status is False) == (problem is not None)
    return status is not False, problem


def verify_proxy_usage_logs(trial_dir: Path, hmac_key: str | None = None) -> tuple[bool, str | None]:
    pairs = (
        ("proxy-usage.jsonl", "proxy-usage.json"),
        ("artifacts/proxy-usage.jsonl", "artifacts/proxy-usage.json"),
        ("artifacts/logs/proxy/proxy-usage.jsonl", "artifacts/logs/proxy/proxy-usage.json"),
    )
    for rel, srel in pairs:
        if not os.path.lexists(trial_dir / rel):
            continue
        exp_head, exp_rows = None, None
        if os.path.lexists(trial_dir / srel):
            stxt, serr = read_chain_text(trial_dir / srel, 16 * 1024 * 1024)
            if serr is not None or stxt is None:
                return False, f"proxy-usage.json: {serr}"
            try:
                sdoc = json.loads(stxt)
            except (ValueError, RecursionError) as exc:
                return False, f"proxy-usage.json: {exc}"
            if not isinstance(sdoc, dict):
                return False, "proxy-usage.json: not a JSON object"
            raw_h, raw_r = sdoc.get("chain_head"), sdoc.get("chain_rows")
            exp_head = raw_h if isinstance(raw_h, str) and raw_h else None
            exp_rows = raw_r if type(raw_r) is int and raw_r >= 0 else None
        ptxt, perr = read_chain_text(trial_dir / rel, MAX_CHAIN_BYTES)
        if perr is not None or ptxt is None:
            return False, f"proxy-usage.jsonl: {perr}"
        if ptxt == "" and exp_rows == 0 and exp_head in (None, events.GENESIS_HASH):
            continue
        pst, ppr, _ = check_event_chain(ptxt, hmac_key, exp_head)
        if pst is not True:
            return False, f"proxy-usage.jsonl: {ppr or 'log has no hash chain'}"
    return True, None


def regrade_trial(
    recorded: dict[str, float],
    graded: list[dict] | None,
    collected: list[dict] | None,
    score: Callable[[list[dict], dict], dict],
    instance: dict,
    *,
    hmac_key: str | None = None,
) -> dict[str, Any]:
    log = graded if graded is not None else collected
    if log is None:
        return {"status": "no-log", "differences": {}, "late_actions": [], "log": None}
    replayed, error = replay(
        score,
        log,
        copy.deepcopy(instance) if isinstance(instance, dict) else instance,
        hmac_key=hmac_key,
    )
    if error:
        return {"status": "error", "reason": error, "differences": {}, "late_actions": [], "log": None}
    differ = differences(recorded, replayed) if recorded else {}
    late = late_actions(graded, collected) if graded is not None and collected is not None else []
    return {
        "status": "no-recorded-reward" if not recorded else "mismatch" if differ else "match",
        "differences": differ,
        "late_actions": late,
        "log": "graded" if graded is not None else "collected",
        "replayed": replayed,
    }
