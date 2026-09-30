"""Rule pass: what the hidden tests pin down, and whether the instruction mentions it.

Reads a task dir: code tasks use `tests/test.patch`, terminal tasks use `tests/test_outputs.py`.
Extracts the specifics a test pins:
- raised exception types;
- regex or literal messages;
- long asserted string literals;
- new API names the tests call or import.

Each specific is checked against `instruction.md`. Names that already appear in the patch's unchanged
context or removed lines are treated as pre-existing and are not flagged.

The rules are recall-oriented evidence for the model pass. `rule_label` is a deliberately simple rule-only
verdict, kept so the model's added value can be measured.
"""

from __future__ import annotations

import base64
import builtins
import io
import re
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path

HARNESS_FILES = {"mimo_build_env.tar.gz.b64", "mimo_test_command.sh"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL = re.compile(r"(?:\.|\b)([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_ATTR = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]{2,})\b")
_FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+\(?([\w\s,]+)\)?")
_RAISES = re.compile(r"(?:pytest\.raises|assertRaises(?:Regex)?)\(\s*\(?([\w., ]+?)\)?\s*(?:,|\))")
_MATCH = re.compile(r"""(?:match\s*=\s*|assertRaisesRegex\([\w.]+\s*,\s*)[rbuf]*(["'])(.+?)\1""")
_STR = re.compile(r"""[rbuf]*(["'])((?:(?!\1).){12,}?)\1""")
_COMMON = set(dir(builtins)) | {
    "pytest", "raises", "mark", "parametrize", "fixture", "assert", "self", "cls", "mock", "patch", "approx",
    "assertEqual", "assertTrue", "assertFalse", "assertIn", "assertIs", "assertIsNone", "assertIsNotNone",
    "assertRaises", "assertRaisesRegex", "assertAlmostEqual", "assertNotEqual", "assertNotIn", "assertIsInstance",
    "assertCountEqual", "assertListEqual", "assertDictEqual", "setUp", "tearDown", "tmp_path", "tmpdir",
    "monkeypatch", "capsys", "caplog", "subTest", "skipif", "xfail", "join", "items", "keys", "values", "append",
    "extend", "get", "split", "strip", "format", "startswith", "endswith", "replace", "lower", "upper", "read",
    "write", "open", "copy", "update", "encode", "decode", "path", "shape", "dtype", "array", "numpy", "asarray",
    "unittest", "TestCase", "main", "sleep", "time", "json", "loads", "dumps", "exists", "mkdir", "read_text",
    "write_text", "run", "call", "return_value", "side_effect", "called", "assert_called_once_with", "MagicMock",
}


@dataclass
class Specific:
    kind: str  # exception | message | literal | name
    value: str
    file: str
    line: int
    stated: bool


def _decode_test_command(patch: str) -> str | None:
    m = re.search(r"\+\+\+ b/mimo_build_env\.tar\.gz\.b64\n@@[^\n]*\n((?:\+[^\n]*\n?)+)", patch)
    if not m:
        return None
    blob = "".join(line[1:] for line in m.group(1).splitlines())
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("test_command.sh"):
                    return tar.extractfile(member).read().decode(errors="replace")
    except Exception:
        return None
    return None


def split_patch(patch: str) -> list[dict]:
    """Per-file hunks: added lines (with new-file line numbers), plus context and removed text."""
    files, cur, new_line = [], None, 0
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            cur = {"file": raw.split(" b/", 1)[-1], "added": [], "context": []}
            files.append(cur)
        elif cur is None or raw.startswith(("--- ", "+++ ", "index ", "new file", "deleted file")):
            continue
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 0
            cur["context"].append(raw)  # hunk headers name the enclosing def, which pre-exists
        elif raw.startswith("+"):
            cur["added"].append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            cur["context"].append(raw[1:])
        else:
            cur["context"].append(raw[1:] if raw.startswith(" ") else raw)
            new_line += 1
    return [f for f in files if f["file"] not in HARNESS_FILES]


def load_tests(task_dir: Path) -> tuple[list[dict], str, str | None]:
    """(files, tests_text_for_model, decoded_test_command)."""
    patch_path = task_dir / "tests" / "test.patch"
    if patch_path.is_file():
        patch = patch_path.read_text(errors="replace")
        files = split_patch(patch)
        shown = re.sub(
            r"(\+\+\+ b/mimo_build_env\.tar\.gz\.b64\n@@[^\n]*\n)(?:\+[^\n]*\n?)+",
            r"\1+<base64 build env elided>\n",
            patch,
        )
        return files, shown, _decode_test_command(patch)
    text = ""
    files = []
    for path in sorted((task_dir / "tests").glob("test_*.py")):
        body = path.read_text(errors="replace")
        text += f"# ===== tests/{path.name} =====\n{body}\n"
        files.append({"file": f"tests/{path.name}", "added": list(enumerate(body.splitlines(), 1)), "context": []})
    return files, text, None


def _mentioned(value: str, instruction: str, instruction_idents: set[str]) -> bool:
    if value.lower() in instruction.lower():
        return True
    if _IDENT.fullmatch(value):
        # snake/camel variants: register_type_strategy ~ "register type strategy"
        return value.lower() in instruction_idents or value.replace("_", " ").lower() in instruction.lower()
    words = [w for w in re.findall(r"[a-z]{4,}", value.lower())]
    if not words:
        return False
    hit = sum(w in instruction.lower() for w in words)
    return hit / len(words) >= 0.6


def extract(task_dir: Path) -> dict:
    instruction = (task_dir / "instruction.md").read_text(errors="replace")
    instruction_idents = {m.group(0).lower() for m in _IDENT.finditer(instruction)}
    files, tests_text, test_command = load_tests(task_dir)
    specifics: list[Specific] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, file: str, line: int) -> None:
        value = value.strip()
        if not value or (kind, value) in seen:
            return
        seen.add((kind, value))
        specifics.append(Specific(kind, value, file, line, _mentioned(value, instruction, instruction_idents)))

    for f in files:
        existing = {m.group(0) for text in f["context"] for m in _IDENT.finditer(text)}
        defined = set()
        for _, text in f["added"]:
            for m in re.finditer(r"^\s*(?:def|class)\s+(\w+)|^\s*(\w+)\s*=(?!=)|\bas\s+(\w+)|for\s+(\w+)\s+in", text):
                defined.update(g for g in m.groups() if g)
        for n, text in f["added"]:
            code = text.split("#", 1)[0]
            for m in _RAISES.finditer(code):
                for exc in re.split(r"[,\s]+", m.group(1)):
                    exc = exc.split(".")[-1]
                    if exc and exc not in {"Exception", "BaseException"}:
                        add("exception", exc, f["file"], n)
            for m in _MATCH.finditer(code):
                add("message", m.group(2), f["file"], n)
            if re.search(r"\bassert|assert\w*\(|==", code):
                for m in _STR.finditer(code):
                    if not re.search(r"match\s*=\s*$", code[: m.start()]):
                        add("literal", m.group(2), f["file"], n)
            mi = _FROM_IMPORT.match(code)
            names = [x.strip() for x in mi.group(2).split(",")] if mi else []
            names += [m.group(1) for m in _CALL.finditer(code)] + [m.group(1) for m in _ATTR.finditer(code)]
            for name in names:
                if (
                    name
                    and name not in _COMMON
                    and name not in existing
                    and name not in defined
                    and not name.startswith(("test_", "assert", "_"))
                    and not name.isupper()
                ):
                    add("name", name, f["file"], n)
    unstated = [s for s in specifics if not s.stated]
    return {
        "task_id": task_dir.name,
        "instruction": instruction,
        "tests_text": tests_text,
        "test_command": test_command,
        "specifics": [asdict(s) for s in specifics],
        "rule_label": rule_label(unstated),
    }


def rule_label(unstated: list[Specific]) -> str:
    """Rule-only verdict: an unstated exact message is broken; any other unstated message or name is suspect."""
    if any(s.kind == "message" or (s.kind == "literal" and len(s.value) >= 25) for s in unstated):
        return "broken"
    if any(s.kind in {"exception", "name", "literal"} for s in unstated):
        return "suspect"
    return "sound"
