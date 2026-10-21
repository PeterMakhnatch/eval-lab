"""Read-only access to one archived trial directory (a native Harbor trial dir).

Downloaded content is untrusted DATA: nothing here executes, imports or shells out on it.
Files that are absent are reported as absent (``None``), never as empty-and-clean.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

FAILED = {"failed", "error", "broken"}


def short_name(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def line_of(text: str, pattern: str) -> str:
    """First ``verifier/test-stdout.txt:N: line`` matching pattern, for evidence citations."""
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line):
            return f"verifier/test-stdout.txt:{number}: {line.strip()[:160]}"
    return ""


class Trial:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self._cache: dict[str, Any] = {}

    # --- plain files ------------------------------------------------------------------
    def path(self, rel: str) -> Path:
        return self.dir / rel

    def text(self, rel: str) -> str | None:
        p = self.dir / rel
        if not p.is_file():
            return None
        return p.read_text(errors="replace")

    def json(self, rel: str) -> Any:
        key = f"json:{rel}"
        if key not in self._cache:
            text = self.text(rel)
            try:
                self._cache[key] = None if text is None else json.loads(text)
            except ValueError:
                self._cache[key] = None
        return self._cache[key]

    # --- verifier -----------------------------------------------------------------------
    @property
    def stdout(self) -> str:
        return self.text("verifier/test-stdout.txt") or ""

    @property
    def has_result(self) -> bool:
        return (self.dir / "result.json").is_file()

    @property
    def reward(self) -> float | None:
        """Verifier reward recorded in the archive's own result.json (None when absent)."""
        res = self.json("result.json")
        value = ((res or {}).get("verifier_result") or {}).get("rewards", {}) if isinstance(res, dict) else {}
        raw = value.get("reward") if isinstance(value, dict) else None
        return None if raw is None else float(raw)

    @property
    def has_verifier(self) -> bool:
        return (self.dir / "verifier").is_dir()

    @property
    def ctrf_tests(self) -> list[dict[str, Any]] | None:
        ctrf = self.json("verifier/ctrf.json")
        if ctrf is None:
            return None
        return ctrf.get("results", ctrf).get("tests", [])

    @property
    def failed(self) -> set[str] | None:
        """Short names of failed tests (parametrized ids kept), None when no CTRF evidence."""
        tests = self.ctrf_tests
        if tests is None:
            return None
        return {short_name(t["name"]).split("[")[0] for t in tests if t.get("status") in FAILED}

    @property
    def failed_full(self) -> list[str] | None:
        tests = self.ctrf_tests
        if tests is None:
            return None
        return [short_name(t["name"]) for t in tests if t.get("status") in FAILED]

    @property
    def passed(self) -> set[str]:
        return {short_name(t["name"]).split("[")[0] for t in (self.ctrf_tests or [])
                if t.get("status") == "passed"}

    def test_message(self, name: str) -> str:
        for t in self.ctrf_tests or []:
            if short_name(t["name"]).split("[")[0] == name and t.get("status") in FAILED:
                return str(t.get("message") or t.get("trace") or "")
        return ""

    # --- agent side ----------------------------------------------------------------------
    def trajectory(self) -> dict[str, Any] | None:
        if "traj" not in self._cache:
            text = self.text("agent/trajectory.json")
            if text is None:
                self._cache["traj"] = None
            else:
                # One public export replaces numeric cache fields with the bare token
                # [REDACTED]; normalise only those metrics, never actions.
                text = re.sub(
                    r'(?m)^(\s*"(?:cached_tokens|cache_read_input_tokens)"\s*:\s*)\[REDACTED\](\s*,?\s*)$',
                    r"\1null\2", text)
                try:
                    self._cache["traj"] = json.loads(text)
                except ValueError:
                    self._cache["traj"] = None
        return self._cache["traj"]

    def actions(self) -> list[tuple[int, str, str]]:
        """(step_id, tool name, flattened argument strings) of recorded agent tool calls."""
        doc = self.trajectory() or {}
        out: list[tuple[int, str, str]] = []
        for step in doc.get("steps", []):
            if step.get("source") not in {"agent", "assistant"}:
                continue
            for call in step.get("tool_calls") or []:
                args = call.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"command": args}
                flat = "\n".join(_strings(args))
                if flat:
                    out.append((step.get("step_id", 0), call.get("function_name", ""), flat))
        return out

    def final_message(self) -> tuple[str, str]:
        doc = self.trajectory() or {}
        for step in reversed(doc.get("steps", [])):
            if step.get("source") in {"agent", "assistant"} and isinstance(step.get("message"), str) \
                    and step["message"]:
                return step["message"], f"agent/trajectory.json step {step.get('step_id', '?')}"
        return "", ""

    # --- artifacts ---------------------------------------------------------------------------
    def artifact_files(self) -> Iterator[Path]:
        root = self.dir / "artifacts"
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name != "manifest.json":
                    yield p

    def artifact_text(self, suffixes: tuple[str, ...] = ()) -> list[tuple[str, str]]:
        out = []
        for p in self.artifact_files():
            if suffixes and p.suffix not in suffixes:
                continue
            if any(bit in p.parts for bit in ("__pycache__", "node_modules", ".git", ".next")):
                continue
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                out.append((str(p.relative_to(self.dir)), p.read_text(errors="replace")))
            except OSError:
                continue
        return out


def _strings(value: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(value, str):
        yield f"{prefix}: {value}" if prefix else value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, f"{prefix}[{index}]")
