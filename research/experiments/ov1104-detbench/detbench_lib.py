"""Shared helpers for the ov1104 DetBench lane (see DETBENCH-CONTRACT.md).

Everything here is label-free: a view is built from steps, the task text and the
final diff/reward only. Labels travel in ``runs.jsonl`` rows, never in views.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SHARED = Path("/private/tmp/ov1104/shared/detbench")
VIEWS = SHARED / "views"
SIGNALS = SHARED / "signals"
RUNS = SHARED / "runs.jsonl"

ROLE_MAP = {"agent": "agent", "assistant": "agent", "user": "user", "system": "system", "tool": "tool"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_id(native_id: str) -> str:
    """A filesystem-safe file stem for a native id; reversible only through runs.jsonl."""
    cleaned = _SAFE.sub("_", native_id)
    if cleaned == native_id and len(cleaned) <= 120:
        return cleaned
    return f"{cleaned[:100]}__{hashlib.sha256(native_id.encode()).hexdigest()[:12]}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "message"):
            if isinstance(value.get(key), str):
                return value[key]
    if isinstance(value, list):
        parts = [t for t in (_text(v) for v in value) if t]
        return "\n".join(parts) if parts else None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _args_str(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def _call(call: Any) -> dict[str, str] | None:
    if not isinstance(call, Mapping):
        return None
    fn = call.get("function")
    if isinstance(fn, Mapping):
        name = fn.get("name") or ""
        args = fn.get("arguments")
    else:
        name = call.get("function_name") or call.get("name") or ""
        args = call.get("arguments")
        if args is None:
            args = call.get("input")
    return {"name": str(name), "args": _args_str(args)}


def _observation(step: Mapping[str, Any]) -> str | None:
    obs = step.get("observation")
    if obs is None:
        return None
    if isinstance(obs, str):
        return obs
    if isinstance(obs, Mapping):
        results = obs.get("results")
        if isinstance(results, list):
            parts = []
            for r in results:
                t = _text(r.get("content") if isinstance(r, Mapping) else r)
                if t:
                    parts.append(t)
            return "\n".join(parts) if parts else None
        return _text(obs)
    return _text(obs)


def normalize_steps(atif_steps: Iterable[Any]) -> list[dict[str, Any]]:
    """ATIF-style steps (source/message/tool_calls/observation) -> contract view steps."""
    out: list[dict[str, Any]] = []
    for step in atif_steps:
        if not isinstance(step, Mapping):
            continue
        role = ROLE_MAP.get(str(step.get("source") or step.get("role") or "agent"), "agent")
        text = _text(step.get("message"))
        reasoning = _text(step.get("reasoning_content") or step.get("reasoning"))
        if reasoning:
            text = f"<reasoning>\n{reasoning}\n</reasoning>\n{text or ''}"
        raw_calls = step.get("tool_calls")
        calls = [c for c in (_call(x) for x in (raw_calls if isinstance(raw_calls, list) else [])) if c]
        out.append(
            {
                "i": len(out),
                "role": role,
                "text": text,
                "tool_calls": calls,
                "observation": _observation(step),
            }
        )
    return out


def build_view(
    run_key: str,
    atif_steps: Iterable[Any],
    *,
    task_text: str | None = None,
    reward: float | None = None,
    diff: str | None = None,
) -> dict[str, Any]:
    steps = normalize_steps(atif_steps)
    if task_text is None:
        for s in steps:
            if s["role"] == "user" and s["text"]:
                task_text = s["text"]
                break
    return {
        "run_key": run_key,
        "task_text": task_text,
        "steps": steps,
        "final": {"reward": reward, "diff": diff},
    }


def actions_only(view: Mapping[str, Any]) -> dict[str, Any]:
    """Tool calls + observations only: no agent prose, reasoning or task text."""
    steps = []
    for s in view["steps"]:
        if s["role"] == "agent":
            steps.append({"i": s["i"], "role": "agent", "text": None, "tool_calls": s["tool_calls"], "observation": s["observation"]})
        elif s["role"] == "tool":
            steps.append({"i": s["i"], "role": "tool", "text": None, "tool_calls": [], "observation": s["observation"] or s["text"]})
    return {"run_key": view["run_key"], "task_text": None, "steps": steps, "final": dict(view["final"])}


def view_size(view: Mapping[str, Any]) -> tuple[int, int]:
    """(n_steps, n_chars) of the full view: agent steps count; chars over text, args, observations."""
    n_chars = 0
    for s in view["steps"]:
        n_chars += len(s["text"] or "") + len(s["observation"] or "")
        for c in s["tool_calls"]:
            n_chars += len(c["name"]) + len(c["args"])
    n_steps = sum(1 for s in view["steps"] if s["role"] == "agent")
    return n_steps, n_chars


def write_views(set_id: str, native_id: str, view: Mapping[str, Any], root: Path = VIEWS) -> tuple[Path, Path]:
    stem = safe_id(native_id)
    full = root / "full" / set_id / f"{stem}.json"
    acts = root / "actions_only" / set_id / f"{stem}.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    acts.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    acts.write_text(json.dumps(actions_only(view), ensure_ascii=False), encoding="utf-8")
    return full, acts


def runs_row(
    *,
    set_id: str,
    native_id: str,
    split: str,
    label: int,
    label_type: str,
    natural: bool | None,
    cluster: str,
    source_family: str,
    view_full: Path,
    view_actions: Path,
    n_steps: int,
    n_chars: int,
    read_by: list[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "run_key": f"{set_id}:{native_id}",
        "set": set_id,
        "split": split,
        "label": int(label),
        "label_type": label_type,
        "natural": natural,
        "cluster": cluster,
        "source_family": source_family,
        "view": str(view_full),
        "view_actions_only": str(view_actions),
        "n_steps": n_steps,
        "n_chars": n_chars,
        "read_by": read_by or [],
    }
    if extra:
        row.update(extra)
    return row


def view_to_atif(view: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct an ATIF-like doc from a view so evallab's L0 rules can read it."""
    steps = []
    for s in view["steps"]:
        calls = []
        for c in s["tool_calls"]:
            args: Any = c["args"]
            try:
                parsed = json.loads(args) if args else {}
                args = parsed if isinstance(parsed, (dict, list)) else args
            except (ValueError, TypeError):
                pass
            calls.append({"function_name": c["name"], "arguments": args})
        step: dict[str, Any] = {"step_id": s["i"], "source": s["role"] if s["role"] != "tool" else "user", "message": s["text"] or ""}
        if calls:
            step["tool_calls"] = calls
        if s["observation"] is not None:
            step["observation"] = {"results": [{"content": s["observation"]}]}
        steps.append(step)
    return {"steps": steps}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def signal_row(run_key: str, name: str, version: str, score: float | None, *, abstain: str | None = None, evidence_steps: list[int] | None = None, model: str | None = None, cost_usd: float = 0.0) -> dict[str, Any]:
    if score is None and not abstain:
        raise ValueError("null score requires an abstain reason")
    return {
        "run_key": run_key,
        "signal": name,
        "version": version,
        "score": None if score is None else float(score),
        "abstain": abstain,
        "evidence_steps": evidence_steps or [],
        "model": model,
        "cost_usd": cost_usd,
    }
