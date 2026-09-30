"""Outside-code fetch detector.

Flags commands that pull source or package code from outside the sandbox
(pinned/upgraded installs, downloads, clones, and fetches from code hosts)
and later commands that read the fetched location back. The patterns are a
fixed definition, not tuned against any hand labels, and are reusable by
Scout or rules.

A fetch only *may* mean a pass was copied: the detector records evidence, it
does not prove the submitted code came from the fetch.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evallab.trial_diagnosis import sanitize_excerpt

CLIP_CHARS = 160

# Hosts whose content is upstream source or release artifacts.
_CODE_HOSTS = (
    "github.com",
    "gitlab.com",
    "pypi.org",
    "files.pythonhosted.org",
    "raw.githubusercontent.com",
)

_FETCH_TOOLS = frozenset({"curl", "wget", "aria2c"})

# Shell words that only prefix a command and never name the program.
_WRAPPERS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf"})

# Reading a fetched artifact back into the transcript.
_READ_PROGRAMS = frozenset(
    {"cat", "head", "tail", "less", "more", "sed", "awk", "unzip", "tar", "zcat", "gunzip", "bzcat"}
)

# A command that changes files (an edit), used only to order the copied-pass flag.
_EDIT_PROGRAMS = frozenset({"sed", "patch", "tee", "cp", "mv", "install"})
_EDIT_TOOLS = frozenset(
    {
        "edit",
        "write",
        "write_file",
        "str_replace",
        "str_replace_editor",
        "apply_patch",
        "create_file",
        "file_write",
        "notebook_edit",
    }
)

_HOST_RE = re.compile(r"https?://([^/\s\"'`]+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'`|;>&)]+", re.IGNORECASE)
_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+(==|>=|<=|!=|~=|<|>)[^\s]+$")
_PKG_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")
_PATH_RE = re.compile(r"(?:^|[\s'\"=])(/[^\s'\"|;>&]+)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


@dataclass(frozen=True)
class OutsideFetch:
    """One outside-code fetch and, when seen, the step that read it back."""

    step: int
    kind: str
    command: str
    target: str
    read_back_step: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "command": self.command,
            "target": self.target,
            "read_back_step": self.read_back_step,
        }


def _host_of(url: str) -> str | None:
    match = _HOST_RE.match(url)
    if match is None:
        return None
    return match.group(1).split("@")[-1].split(":")[0].lower().removeprefix("www.")


def _is_code_host(host: str | None) -> bool:
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in _CODE_HOSTS)


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies; they are the model's own text, not commands.

    A fix pasted inside ``python - <<'EOF' ... EOF`` cites URLs in comments.
    Those are not fetches, so the body is removed before any scanning.
    """
    lines = command.split("\n")
    kept: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC_RE.search(line)
        if match:
            terminator = match.group(1)
            kept.append(line[: match.start()])
            continue
        kept.append(line)
    return "\n".join(kept)


def _raw_segments(command: str) -> list[str]:
    """Split on ``&&``/``||``/``;``/newline, but not inside quotes.

    A joined step (``python3 -c "...; ..." ; curl URL``) is several commands.
    The semicolon inside the Python string is not a command boundary.
    """
    text = _strip_heredocs(command)
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is not None:
            buf.append(char)
            if char == "\\" and quote == '"' and index + 1 < length:
                buf.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            parts.append("".join(buf))
            buf = []
            index += 2
            continue
        if char in {";", "\n"}:
            parts.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _segments(command: str) -> list[list[str]]:
    """Tokenize each command segment, heredoc bodies removed.

    Unparseable segments fall back to whitespace tokens.
    """
    segments: list[list[str]] = []
    for raw in _raw_segments(command):
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = raw.split()
        if tokens:
            segments.append(tokens)
    return segments


def _program(tokens: list[str]) -> tuple[str, list[str]] | None:
    """First real program of a segment, past env assignments, wrappers, and durations.

    ``timeout 10 curl URL`` names ``curl``: the duration is an argument of the
    wrapper, not the program that fetched.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            _ASSIGN_RE.match(token)
            or token in _WRAPPERS
            or token.startswith("-")
            or _DURATION_RE.match(token)
        ):
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    head = tokens[index].rsplit("/", 1)[-1]
    return head, tokens[index + 1 :]


def _option_value(args: Sequence[str], names: set[str]) -> str | None:
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _pip_requirement(token: str) -> tuple[str, str] | None:
    """A requirement that pins, upgrades, or comes from a URL or VCS."""
    if token.startswith("-") or token in {".", ".."}:
        return None
    lowered = token.lower()
    if lowered.startswith(("git+", "hg+", "svn+", "bzr+")) or "://" in token:
        return "vcs" if token.startswith(("git+", "hg+", "svn+", "bzr+")) else "url", token
    if _PIN_RE.match(token):
        return "pinned", token
    return None


def _match_pip(args: list[str]) -> tuple[str, str] | None:
    """``pip download``, or ``pip install`` of a pinned/upgraded/remote requirement.

    Plain ``pip install -e .`` and ``pip install pytest`` (no version) are not
    fetches: they install what is already local or whatever the index serves.
    """
    if not args or args[0] not in {"install", "download"}:
        return None
    sub, rest = args[0], args[1:]
    positional = [arg for arg in rest if not arg.startswith("-")]
    upgraded = any(arg in {"-U", "--upgrade"} for arg in rest)
    if sub == "download":
        target = positional[0] if positional else _option_value(rest, {"-r", "--requirement"})
        return "pip_download", target or "pip download"
    matches = [found for found in (_pip_requirement(arg) for arg in positional) if found]
    if not matches and not upgraded:
        return None
    if matches and all(kind == "url" or kind == "vcs" for kind, _ in matches):
        kind = "pip_install_url"
    elif matches:
        kind = "pip_install_pinned"
    else:
        kind = "pip_install_upgrade"
    target = ", ".join(token for _, token in matches) or " ".join(positional) or "--upgrade"
    return kind, target


def _match_fetch_tool(program: str, args: list[str]) -> tuple[str, str] | None:
    urls = [arg for arg in args if "://" in arg]
    if not urls:
        urls = [arg for arg in args if not arg.startswith("-")]
    host = _host_of(urls[0]) if urls else None
    if not _is_code_host(host):
        return None
    return program, urls[0]


def _python_fetches(command: str) -> list[tuple[str, str]]:
    """``urllib``/``requests`` calls in this segment whose URL is a code host.

    A URL merely mentioned, or a URL that belongs to a later segment of a
    joined step, is not a Python fetch. Heredoc bodies are the model's own
    text and are not scanned.
    """
    command = _strip_heredocs(command)
    if "urllib" not in command and "requests" not in command:
        return []
    kind = "python_requests" if "requests" in command else "python_urllib"
    found: list[tuple[str, str]] = []
    for url in dict.fromkeys(_URL_RE.findall(command)):
        if not _is_code_host(_host_of(url)):
            continue
        found.append((kind, url.rstrip(".,")))
    return found


def _match_segment(raw: str) -> tuple[str, str] | None:
    """Classify one tool call or one ``&&``/``;`` segment, never its neighbors."""
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()
    found = _program(tokens)
    if found is None:
        return None
    program, args = found
    if program in {"pip", "pip3"}:
        return _match_pip(args)
    if program == "git" and args and args[0] == "clone":
        remotes = [arg for arg in args[1:] if not arg.startswith("-")]
        return "git_clone", remotes[0] if remotes else "git clone"
    if program in _FETCH_TOOLS:
        return _match_fetch_tool(program, args)
    if program in {"python", "python3", "py"}:
        fetches = _python_fetches(raw)
        return fetches[0] if fetches else None
    return None


def match_outside_fetch(command: str) -> tuple[str, str] | None:
    """Classify each tool call and each ``&&``/``;`` segment on its own.

    The kind comes from the segment that holds the fetch. A joined step
    (``python3 -c "import soupsieve" ; timeout 10 curl URL``) is a ``curl``,
    not a Python fetch: the URL is not in the Python call.
    """
    if not command.strip():
        return None
    for raw in _raw_segments(command):
        matched = _match_segment(raw)
        if matched is not None:
            return matched
    return None


def _fetch_locations(command: str, kind: str, target: str) -> list[str]:
    """Paths and names a later command could read the fetch back from."""
    locations: list[str] = []
    for tokens in _segments(command):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program in {"pip", "pip3"}:
            dest = _option_value(args, {"-d", "--dest"})
            if dest:
                locations.append(dest.rstrip("/"))
        elif program == "git" and args and args[0] == "clone":
            positional = [arg for arg in args[1:] if not arg.startswith("-")]
            if len(positional) >= 2:
                locations.append(positional[-1].rstrip("/"))
            elif positional:
                name = positional[0].rstrip("/").rsplit("/", 1)[-1]
                locations.append(name.removesuffix(".git"))
        elif program in _FETCH_TOOLS:
            output = _option_value(args, {"-o", "--output", "-O", "--output-document"})
            if output and output != "-":
                locations.append(output)
    if kind == "pip_download":
        name = _PKG_NAME_RE.match(target.split(",", 1)[0].strip())
        if name:
            locations.append(name.group(1))
    return [loc for loc in dict.fromkeys(locations) if len(loc) >= 2]


def _reads_back(command: str, locations: Sequence[str]) -> bool:
    """Whether a later command reads from one of the fetch's locations."""
    if not locations or not command.strip():
        return False
    for tokens in _segments(command):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program not in _READ_PROGRAMS and program not in {"python", "python3", "py"}:
            continue
        joined = " ".join(args)
        for location in locations:
            if location not in command and location not in joined:
                continue
            if program in {"python", "python3", "py"} and "open(" not in command:
                continue
            return True
    return False


def _is_edit(command: str, tool: str) -> bool:
    """A command that writes files, so the copied-pass flag can be ordered."""
    if tool.lower() in _EDIT_TOOLS:
        return True
    if re.search(r"""open\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][wa]""", command):
        return True
    for tokens in _segments(command):
        found = _program(tokens)
        if found is None:
            continue
        program, args = found
        if program in _EDIT_PROGRAMS and (
            program != "sed" or any(a in {"-i", "--in-place"} for a in args)
        ):
            return True
        if program in {"python", "python3", "py"} and ".write(" in command:
            return True
        if any(arg.startswith(">") for arg in args):
            return True
    return False


def _clip(command: str) -> str:
    return sanitize_excerpt(command, CLIP_CHARS) or ""


def detect_outside_fetches(actions: Sequence[Any]) -> list[OutsideFetch]:
    """Find outside-code fetches in the report's action list.

    ``actions`` are the report's ``_Action`` rows (anything with ``step``,
    ``target``, and ``shell``). A later action that reads a fetch's recorded
    location sets ``read_back_step``; the earliest such read wins.
    """
    fetches: list[OutsideFetch] = []
    pending: list[tuple[int, list[str]]] = []
    for action in actions:
        command = str(getattr(action, "target", "") or "")
        step = int(getattr(action, "step", 0))
        if getattr(action, "shell", False) or match_outside_fetch(command):
            matched = match_outside_fetch(command)
        else:
            matched = None
        if matched is not None:
            kind, target = matched
            fetches.append(OutsideFetch(step, kind, _clip(command), target))
            pending.append((len(fetches) - 1, _fetch_locations(command, kind, target)))
            continue
        for index, locations in pending:
            if fetches[index].read_back_step is None and _reads_back(command, locations):
                fetches[index] = OutsideFetch(
                    fetches[index].step,
                    fetches[index].kind,
                    fetches[index].command,
                    fetches[index].target,
                    step,
                )
    return fetches


def pass_may_be_copied(
    fetches: Sequence[OutsideFetch], reward: float | None, edit_steps: Sequence[int]
) -> dict[str, Any] | None:
    """Flag a passing run whose fetch was read back before the edit that landed.

    "Before the first edit that stayed" is approximated as before the last edit
    seen in the action list: an edit after the read-back is the one that could
    carry the fetched code. The wording stays "may be copied"; a read-back is
    evidence, not proof.
    """
    if reward is None or reward < 1.0:
        return None
    read_back = [
        (fetch, fetch.read_back_step) for fetch in fetches if fetch.read_back_step is not None
    ]
    if not read_back:
        return None
    last_edit = max(edit_steps) if edit_steps else None
    evidence = [fetch for fetch, read_at in read_back if last_edit is None or read_at < last_edit]
    if not evidence:
        return None
    return {
        "flag": "pass_may_be_copied",
        "evidence_steps": [
            {"fetch_step": fetch.step, "read_back_step": fetch.read_back_step} for fetch in evidence
        ],
    }


def outside_fetch_section(
    actions: Sequence[Any], reward: float | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The report's ``outside_fetches`` section and the copied-pass flag."""
    fetches = detect_outside_fetches(actions)
    edit_steps = [
        int(getattr(action, "step", 0))
        for action in actions
        if _is_edit(
            str(getattr(action, "target", "") or ""), str(getattr(action, "tool", "") or "")
        )
    ]
    section = {
        "count": len(fetches),
        "items": [fetch.to_dict() for fetch in fetches],
    }
    return section, pass_may_be_copied(fetches, reward, edit_steps)


def render_outside_fetch_line(section: dict[str, Any]) -> str:
    """One outcome line: the first fetch and its read-back, or ``none``."""
    items = section.get("items") or []
    if not items:
        return "- Outside code fetched: none"
    first = items[0]
    detail = f"step {first['step']} {first['kind'].replace('_', ' ')} {first['target']}"
    if first.get("read_back_step") is not None:
        detail += f" (read back at step {first['read_back_step']})"
    extra = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return f"- Outside code fetched: {detail}{extra}"
