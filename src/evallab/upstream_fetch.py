"""Detect agent trials that fetched upstream remote content (answer-leak guard).

A trial that downloads the upstream package (``pip download waitress==2.0.0``)
or curls the upstream file (``curl .../soupsieve/master/...``) can score a
verifier reward without doing the task's work. The verifier reward stays as
recorded; GEPA's objective score for such trials is forced to 0
(``UPSTREAM_FETCH_ZERO``) so the optimiser cannot select for the leak.

The detector is pure: :func:`detect_upstream_fetch` maps executed shell
commands to :class:`Finding` records. :func:`commands_from_trial` extracts
those commands from a Harbor trial directory (ATIF ``agent/trajectory*.json``,
``tool_calls[].arguments.keystrokes``). Shell parsing is deliberately narrow:
quote-aware operator splitting, backslash-continuation joining, and heredoc
grouping, with no subshell evaluation.

Conservative by decision: any remote fetch counts for the GEPA score (task
images are pre-baked, so the model has no legitimate need to reach the
network). Informational queries (``pip show/list``, ``git log/diff``,
``grep http``) and local installs (``-e .``, local paths, local ``-r`` files)
are not findings.

``Finding`` is an attempt, not an acquisition. Counts use
``assess_upstream_fetch`` to require recorded positive outcome evidence; failed
and unknown attempts remain visible but do not exclude a pass. This does not
loosen the separate, opt-in GEPA ``UPSTREAM_FETCH_ZERO`` attempt policy.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "KNOWN_SCORE_RULES",
    "UPSTREAM_FETCH_ZERO",
    "Finding",
    "commands_from_trial",
    "detect_upstream_fetch",
    "assess_upstream_fetch",
    "confirmed_fetch",
    "format_fetch_notice",
]

#: Campaign score rule id: force the GEPA objective score to 0.0 when
#: :func:`detect_upstream_fetch` reports any finding for the trial.
UPSTREAM_FETCH_ZERO = "upstream_fetch_zero"

#: Score rule ids accepted by ``load_campaign``.
KNOWN_SCORE_RULES = frozenset({UPSTREAM_FETCH_ZERO})

_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class Finding:
    """One executed remote-content fetch attempt (outcome not assessed)."""

    #: ATIF step id, or -1 when the caller did not supply one.
    step_index: int
    #: Stable machine-readable kind, e.g. ``pip-download-remote-package``.
    kind: str
    #: First 200 chars of the offending command, whitespace-collapsed.
    excerpt: str
    #: True when the fetch names the task's own repo/package (strong signal).
    names_task_repo: bool
    #: Exact matcher operand; retained for target-bound outcome assessment.
    target: str


# ---------------------------------------------------------------------------
# Shell splitting (narrow, no evaluation)
# ---------------------------------------------------------------------------

_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _join_continuations(text: str) -> str:
    return re.sub(r"\\\r?\n", " ", text)


def _split_units(text: str) -> list[tuple[str, str]]:
    """Split keystrokes into (head, body) units; heredoc bodies stay attached."""
    units: list[tuple[str, str]] = []
    pending: list[str] = []
    head_lines: list[str] = []
    body_lines: list[str] = []

    def flush() -> None:
        if head_lines or body_lines:
            units.append(("\n".join(head_lines), "\n".join(body_lines)))
        head_lines.clear()
        body_lines.clear()

    for raw_line in _join_continuations(text).splitlines():
        line = raw_line
        if pending:
            body_lines.append(line)
            if line.strip() in pending:
                pending.remove(line.strip())
                if not pending:
                    flush()
            continue
        flush()
        head_lines.append(line)
        for match in _HEREDOC_OPEN.finditer(line):
            pending.append(match.group(2))
        if not pending:
            flush()
    flush()
    return units


def _split_operators(line: str) -> list[str]:
    """Split one command line on && || ; | outside quotes (backslash-aware)."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            buf.append(ch)
            quote = ch
        elif ch == "&" and line[i + 1 : i + 2] == "&" or ch == "|" and line[i + 1 : i + 2] == "|":
            parts.append("".join(buf))
            buf = []
            i += 1
        elif ch in (";", "|"):
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


_PREFIX_WITH_ARG = {"timeout", "sudo"}
_TIMEOUT_ARG = re.compile(r"^\d+[smh]?$")


def _strip_prefix(argv: list[str]) -> list[str]:
    out = list(argv)
    while out:
        head = out[0]
        if head == "sudo":
            out.pop(0)
        elif head == "timeout" and len(out) > 1 and _TIMEOUT_ARG.match(out[1]):
            out = out[2:]
        elif head == "env" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            out.pop(0)
        else:
            break
    return out


def _argv(segment: str) -> list[str] | None:
    try:
        return _strip_prefix(shlex.split(segment, posix=True))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Repo matching
# ---------------------------------------------------------------------------

def _repo_parts(task_repo: str | None) -> set[str]:
    if not task_repo:
        return set()
    parts = {part.lower() for part in re.split(r"[/:]", task_repo) if part}
    return {part for part in parts if len(part) >= 2}


def _package_base(operand: str) -> str:
    base = operand.split("[", 1)[0]
    base = re.split(r"===|==|>=|<=|~=|!=|>|<|;", base, maxsplit=1)[0]
    return base.strip().lower()


def _names_repo(*, operand: str = "", url: str = "", task_repo: str | None) -> bool:
    if not task_repo:
        return False
    parts = _repo_parts(task_repo)
    base = _package_base(operand)
    if base and base in parts:
        return True
    lowered = url.lower()
    if not lowered:
        return False
    repo_path = task_repo.lower()
    if repo_path and repo_path in lowered:
        return True
    return any(len(part) >= 3 and part in lowered for part in parts)


# ---------------------------------------------------------------------------
# Per-tool matchers
# ---------------------------------------------------------------------------

_LOOPBACK = re.compile(
    r"^https?://(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|169\.254\.169\.254)([:/]|$)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_REMOTE_URL = re.compile(r"^(https?://|ssh://|git://|git\+|git@)", re.IGNORECASE)
_ARCHIVE_FILE = re.compile(r"\.(whl|tar\.gz|tgz|zip)$", re.IGNORECASE)


def _is_loopback_url(url: str) -> bool:
    return _LOOPBACK.match(url.strip("<>(),;\"'")) is not None


def _pip_findings(argv: list[str], *, tool: str, task_repo: str | None) -> list[tuple[str, str, bool]]:
    """Match pip/uv-pip argv. Returns (kind, operand-or-url, names_repo)."""
    out: list[tuple[str, str, bool]] = []
    if len(argv) < 2:
        return out
    sub, rest = argv[1], argv[2:]
    if sub == "index":
        out.append(("pip-index-query", " ".join(rest[:2]), False))
        return out
    if sub not in {"install", "download", "wheel"}:
        return out
    kind = f"{tool}-{sub}-remote-package"
    remote_index = False
    no_index = False
    operands: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in ("-e", "--editable"):
            value = rest[i + 1] if i + 1 < len(rest) else ""
            if _REMOTE_URL.match(value):
                out.append((kind, value, _names_repo(operand=value, task_repo=task_repo)))
            i += 2
        elif token in ("-r", "--requirement"):
            value = rest[i + 1] if i + 1 < len(rest) else ""
            if _REMOTE_URL.match(value):
                out.append(("pip-requirements-url", value, _names_repo(url=value, task_repo=task_repo)))
            i += 2
        elif token in ("-c", "--constraint"):
            i += 2
        elif token in ("-f", "--find-links", "--index-url", "--extra-index-url"):
            value = rest[i + 1] if i + 1 < len(rest) else ""
            if _REMOTE_URL.match(value):
                remote_index = True
                out.append(("pip-remote-index-url", value, _names_repo(url=value, task_repo=task_repo)))
            i += 2
        elif token == "--no-index":
            no_index = True
            i += 1
        elif token.startswith("-"):
            if "=" in token:
                i += 1
            elif token in (
                "-d", "--dest", "--destination", "-o", "--output", "-t", "--target",
                "--prefix", "--root", "--src", "--platform", "--python-version",
                "--implementation", "--abi", "--no-binary", "--only-binary",
            ):
                i += 2
            else:
                i += 1
        else:
            operands.append(token)
            i += 1
    for operand in operands:
        if re.match(r"^(\d+)?>&?\d*(/.*)?$", operand) or operand.startswith(("<", ">")):
            continue  # shell redirection, not an install operand
        if _REMOTE_URL.match(operand):
            out.append((kind, operand, _names_repo(operand=operand, task_repo=task_repo)))
        elif _ARCHIVE_FILE.search(operand) or operand.startswith(("./", "../", "/", "~")):
            continue  # local file install
        elif no_index and not remote_index:
            continue  # --no-index with no remote index cannot fetch
        elif operand:
            out.append((kind, operand, _names_repo(operand=operand, task_repo=task_repo)))
    return out


def _transfer_findings(argv: list[str], *, tool: str, kind: str, task_repo: str | None) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for token in argv[1:]:
        cleaned = token.strip("<>(),;\"'")
        match = _URL.search(cleaned)
        if not match:
            continue
        url = match.group(0)
        if _is_loopback_url(url):
            continue
        out.append((kind, url, _names_repo(url=url, task_repo=task_repo)))
    return out


def _git_findings(argv: list[str], *, task_repo: str | None) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    if len(argv) < 2:
        return out
    sub = argv[1]
    if sub == "clone" and len(argv) > 2:
        url = argv[2]
        if _REMOTE_URL.match(url):
            out.append(("git-clone-remote-url", url, _names_repo(url=url, task_repo=task_repo)))
    elif sub in {"fetch", "pull"}:
        for token in argv[2:]:
            if _REMOTE_URL.match(token) and not _is_loopback_url(token):
                out.append((f"git-{sub}-remote-url", token, _names_repo(url=token, task_repo=task_repo)))
    elif sub == "remote" and len(argv) > 3 and argv[2] == "add":
        url = argv[3]
        if _REMOTE_URL.match(url):
            out.append(("git-remote-add-url", url, _names_repo(url=url, task_repo=task_repo)))
    return out


_FETCH_CALLS = (
    "urlopen(", "urlretrieve(",
    "requests.get(", "requests.post(", "requests.request(", "requests.session(",
    "urllib.request.", "httpx.", "http.client.", "socket.create_connection(",
)
_NODE_FETCH_CALLS = ("fetch(", "https.get", "https.request", "http.get", "http.request")


def _script_findings(
    head: str, body: str, *, interpreter: str, task_repo: str | None,
) -> list[tuple[str, str, bool]]:
    """Match python/node -c / stdin-heredoc code that fetches remote URLs."""
    out: list[tuple[str, str, bool]] = []
    argv = _argv(head)
    code: str | None = None
    if argv is not None and len(argv) >= 2:
        if argv[1] in {"-c", "-e"} and len(argv) > 2:
            code = " ".join(argv[2:])
        elif argv[1] == "-" or (len(argv) > 2 and argv[2] == "-"):
            code = body
        elif argv[1] == "-m":
            return out  # module runs (pytest, pip handled separately) are not -c/heredocs
    if code is None:
        # Regex fallback for heads shlex cannot parse: `-c` or stdin-heredoc only.
        if re.search(r"(^|\s)-(c|e)\s", head):
            code = head
        elif re.search(r"(^|\s)-\s*(<<|$)", head):
            code = head + "\n" + body
        else:
            return out
    lowered = code.lower()
    tokens = _NODE_FETCH_CALLS if interpreter == "node" else _FETCH_CALLS
    if not any(token in lowered for token in tokens):
        return out
    for match in _URL.finditer(code):
        url = match.group(0).rstrip(").,;\"'")
        if _is_loopback_url(url):
            continue
        kind = "node-remote-fetch" if interpreter == "node" else "python-remote-fetch"
        out.append((kind, url, _names_repo(url=url, task_repo=task_repo)))
    return out


def _head_findings(head: str, *, task_repo: str | None) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for segment in _split_operators(head):
        argv = _argv(segment)
        if argv is not None:
            if not argv:
                continue
            tool = argv[0].split("/")[-1]
            if tool == "pip" or (tool == "uv" and len(argv) > 1 and argv[1] == "pip"):
                pip_argv = argv if tool == "pip" else [tool + "-pip", *argv[2:]]
                out.extend(_pip_findings(pip_argv, tool="pip" if tool == "pip" else "uv-pip", task_repo=task_repo))
            elif tool == "python" and len(argv) > 2 and argv[1] == "-m" and argv[2] == "pip":
                out.extend(_pip_findings(["pip", *argv[3:]], tool="pip", task_repo=task_repo))
            elif tool in {"curl"}:
                out.extend(_transfer_findings(argv, tool=tool, kind="curl-remote-url", task_repo=task_repo))
            elif tool in {"wget"}:
                out.extend(_transfer_findings(argv, tool=tool, kind="wget-remote-url", task_repo=task_repo))
            elif tool in {"http", "https"}:
                out.extend(_transfer_findings(argv, tool=tool, kind="httpie-remote-url", task_repo=task_repo))
            elif tool == "git":
                out.extend(_git_findings(argv, task_repo=task_repo))
            elif tool in {"apt", "apt-get"} and len(argv) > 1 and argv[1] == "source":
                out.append(("apt-source", " ".join(argv[2:4]), False))
            continue
        lowered = segment.lower()
        if re.search(r"(^|\s|\()pip\s+(install|download|wheel|index)\b", lowered):
            out.append(("pip-unparsed-remote", segment[:80], False))
        elif re.search(r"(^|\s)(curl|wget)\s+https?://", lowered):
            url = _URL.search(segment)
            if url and not _is_loopback_url(url.group(0)):
                out.append(("curl-unparsed-remote-url", url.group(0), _names_repo(url=url.group(0), task_repo=task_repo)))
    return out


def _excerpt(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:_EXCERPT_CHARS]


def detect_upstream_fetch(
    commands: Iterable[str | tuple[int, str]],
    *,
    task_repo: str | None = None,
) -> list[Finding]:
    """Flag executed remote-content fetches in agent shell commands.

    ``commands`` is an iterable of ``(step_index, command_text)`` tuples;
    bare strings are accepted with ``step_index=-1``. ``task_repo`` (e.g.
    ``"Pylons/waitress"``) marks findings that name the task's own repo or
    package as strong; every remote fetch is reported regardless.
    """
    findings: list[Finding] = []
    for item in commands:
        if isinstance(item, str):
            step_index, text = -1, item
        else:
            step_index, text = item
        if not text or not text.strip():
            continue
        for head, body in _split_units(text):
            for kind, operand, strong in _head_findings(head, task_repo=task_repo):
                findings.append(Finding(
                    step_index=step_index,
                    kind=kind,
                    excerpt=_excerpt(head if kind != "pip-unparsed-remote" else operand),
                    names_task_repo=strong,
                    target=operand,
                ))
            lowered_head = head.lower()
            interpreter: str | None = None
            if re.search(r"(^|\s|;)(sudo\s+)?(timeout\s+\S+\s+)?(python3?|python)\b", lowered_head):
                interpreter = "python"
            elif re.search(r"(^|\s|;)(sudo\s+)?(timeout\s+\S+\s+)?(node|nodejs)\b", lowered_head):
                interpreter = "node"
            if interpreter is not None:
                for kind, url, strong in _script_findings(head, body, interpreter=interpreter, task_repo=task_repo):
                    findings.append(Finding(
                        step_index=step_index,
                        kind=kind,
                        excerpt=_excerpt(url),
                        names_task_repo=strong,
                        target=url,
                    ))
    return findings


def confirmed_fetch(flag: dict) -> bool:
    """No command-only or missing-evidence fallback at decisive consumers."""
    evidence = flag.get("outcome_evidence")
    if (
        flag.get("kind") != "upstream_fetch"
        or flag.get("outcome") != "succeeded"
        or not isinstance(evidence, list)
        or not evidence
        or not flag.get("target")
        or not flag.get("document")
        or not flag.get("call_id")
    ):
        return False
    return all(
        isinstance(item, dict)
        and item.get("target") == flag["target"]
        and item.get("document") == flag["document"]
        for item in evidence
    ) and any(
        item.get("step") == flag.get("step") and item.get("call_id") == flag["call_id"]
        for item in evidence
    )


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROMPT = re.compile(r"^(?:\S+@\S+:[^\n]*[#$]|[#$])(?:\s|$)")
_PACKAGE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _output_lines(content: str, command: str) -> list[str]:
    """Drop terminal headers and command echoes, including wrapped echoes.

    A terminal observation is a screen/window, not pure stdout. Text appearing
    in the sent command cannot be positive evidence (echo/printf/heredocs).
    """
    content = _ANSI.sub("", content)
    for marker in ("New Terminal Output:", "Current Terminal Screen:"):
        if marker in content:
            content = content.split(marker, 1)[1]
            # Buffered output before the current echo is not this command's
            # result. Match wrapped echo text without whitespace.
            offsets = [index for index, char in enumerate(content) if not char.isspace()]
            compact = "".join(content[index] for index in offsets)
            sent = re.sub(r"\s+", "", command)
            echo = compact.rfind(sent) if sent else -1
            if echo < 0:
                return []
            content = content[offsets[echo + len(sent) - 1] + 1 :]
            boundary = re.search(r"(?m)^\S+@\S+:[^\n]*[#$](?:\s|$)", content)
            if boundary:
                content = content[:boundary.start()]
            break
    flat_command = re.sub(r"\s+", "", command)
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or _PROMPT.match(line):
            continue
        candidate = line.removeprefix("> ").strip()
        if candidate and re.sub(r"\s+", "", candidate) in flat_command:
            continue
        lines.append(line)
    return lines


def _normal_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _package_identity(target: str) -> tuple[str, str | None] | None:
    # URL/VCS operands cannot safely be inferred to be a package name.
    if _REMOTE_URL.match(target):
        return None
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?(?:==([^;]+))?", target)
    if not match:
        return None
    return _normal_package(match[1]), match[2]


def _acquisition_target(target: str) -> str:
    identity = _package_identity(target)
    return identity[0] if identity else target


def _segments(command: str) -> list[str]:
    return [segment for head, _body in _split_units(command) for segment in _split_operators(head)]


def _fetch_segment(command: str, finding: Finding) -> str | None:
    matching = [
        segment
        for segment in _segments(command)
        if any(
            item.kind == finding.kind and item.target == finding.target
            for item in detect_upstream_fetch([segment])
        )
    ]
    return matching[0] if len(matching) == 1 else None


def _option(argv: list[str], names: set[str]) -> str | None:
    for index, token in enumerate(argv):
        if token in names and index + 1 < len(argv):
            return argv[index + 1]
        for name in names:
            if token.startswith(name + "="):
                return token[len(name) + 1 :]
    return None


def _pip_artifact(command: str, finding: Finding, lines: list[str]) -> str | None:
    """Exact package/version listing in the fetch's own destination.

    A listing alone is insufficient: the caller also requires observed use of
    this artifact in the same document, before a new attempt at this target.
    """
    identity = _package_identity(finding.target)
    segment = _fetch_segment(command, finding)
    if not identity or not identity[1] or not segment:
        return None
    argv = _argv(segment) or []
    dest = _option(argv, {"-d", "--dest", "--destination", "--wheel-dir", "-w"})
    if not dest or not dest.startswith("/"):
        return None
    listing = any(
        (args := _argv(part))
        and args[0].rsplit("/", 1)[-1] in {"ls", "find"}
        and dest in args
        for part in _segments(command)
    )
    if not listing:
        return None
    name, version = identity
    for line in lines:
        # Full-line filename/path: never a substring of an echoed command.
        value = line.removeprefix(dest.rstrip("/") + "/")
        wheel = re.fullmatch(r"([A-Za-z0-9_.]+)-([^-]+)-[^/ ]+\.whl", value)
        archive = re.fullmatch(r"(.+)-([^-]+)\.(?:tar\.gz|tgz|zip)", value)
        match = wheel or archive
        if match and _normal_package(match[1]) == name and match[2] == version:
            return dest.rstrip("/") + "/" + value
    return None


def _call_observations(step: dict, call_id: str | None) -> list[dict]:
    """Bind by source_call_id or an explicit, unique recorded command window."""
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    if not call_id or not isinstance(results, list):
        return []
    calls = step.get("tool_calls")
    if not isinstance(calls, list) or sum(
        isinstance(call, dict) and call.get("tool_call_id") == call_id for call in calls
    ) != 1:
        return []
    call = next(call for call in calls if isinstance(call, dict) and call.get("tool_call_id") == call_id)
    arguments = call.get("arguments")
    command = arguments.get("keystrokes") if isinstance(arguments, dict) else None
    same_commands = sum(
        isinstance(item, dict) and isinstance(item.get("arguments"), dict)
        and item["arguments"].get("keystrokes") == command
        for item in calls
    )
    bound = []
    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source_call_id")
        if source == call_id:
            bound.append({**result, "binding": "source_call_id"})
        elif source is None and isinstance(command, str) and same_commands == 1:
            content = str(result.get("content") or "")
            if any(marker in content for marker in ("New Terminal Output:", "Current Terminal Screen:")) and _output_lines(content, command):
                # Do not manufacture a source_call_id: retain how this
                # legacy terminal window was attributed.
                bound.append({**result, "binding": "terminal-command-window"})
    return bound


def _executed_calls(step: dict, layer: dict | None) -> list[tuple[str | None, str]]:
    if step.get("is_copied_context"):
        return []
    if isinstance(layer, dict) and layer.get("kind") == "parse_error":
        return []
    calls = step.get("tool_calls")
    extracted = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        args = call.get("arguments")
        command = args.get("keystrokes") if isinstance(args, dict) else None
        if isinstance(command, str) and command.strip():
            extracted.append((call.get("tool_call_id"), command))
    if extracted:
        return extracted
    # Recorded/replayed attempts may lack raw calls. Preserve them as unknown,
    # never attach a different call's output to a reconstructed command.
    if isinstance(layer, dict):
        command = layer.get("executed_keystrokes")
        if isinstance(command, str) and command.strip():
            return [(None, command)]
        return []
    from evallab import probe03

    replay = probe03._replay_keystrokes(None, str(step.get("message") or ""))
    return [(None, "\n".join(replay))] if replay else []


def _artifact_use(artifact: str, command: str, lines: list[str]) -> bool:
    """Observed extraction/content, not a proposed read or a successful tail."""
    for segment in _segments(command):
        argv = _argv(segment) or []
        if not argv or artifact not in argv:
            continue
        tool = argv[0].rsplit("/", 1)[-1]
        if tool in {"unzip", "tar"}:
            destination = _option(argv, {"-d"} if tool == "unzip" else {"-C", "--directory"})
            if not destination:
                return False
            prefix = destination.rstrip("/") + "/"
            readers = [
                args for part in _segments(command)
                if (args := _argv(part))
                and args[0].rsplit("/", 1)[-1] in {"cat", "sed", "head", "tail", "grep"}
                and any(value.startswith(prefix) for value in args[1:])
            ]
            files = [line for line in lines if line.startswith(prefix) and line.endswith(".py")]
            source = [line for line in lines if re.match(r"(?:\d+:)?\s*(?:def |class |from |import |self\.)", line)]
            return bool(readers and files and source)
        if tool in {"cat", "head", "tail", "sed", "awk", "zcat"}:
            return any(re.match(r"(?:\d+:)?\s*(?:def |class |from |import )", line) for line in lines)
    return False


def _observed_outcome(finding: Finding, command: str, results: list[dict]) -> tuple[str, str | None, str]:
    """Assess a single call/target, not the trial-wide absence of errors."""
    if _fetch_segment(command, finding) is None:
        return "unknown", None, "multiple/ambiguous fetch commands in the call"
    lines = [
        line
        for result in results
        for line in _output_lines(str(result.get("content") or ""), command)
    ]
    identity = _package_identity(finding.target) if "remote-package" in finding.kind else None
    if identity:
        name, _version = identity
        for line in lines:
            if line.startswith("ERROR:") and any(
                _normal_package(_package_base(token)) == name
                for token in re.findall(r"[A-Za-z0-9_.-]+(?:==[A-Za-z0-9_.+!-]+)?", line)
            ):
                return "failed", line, "target-bound package failure"
        fetch_segment = _fetch_segment(command, finding)
        other_segments = [part for part in _segments(command) if part != fetch_segment]
        if any(
            not (args := _argv(part)) or args[0].rsplit("/", 1)[-1] not in {"cd", "tail", "head", "ls"}
            for part in other_segments
        ):
            return "unknown", None, "output-producing companion command cannot prove acquisition"
        for line in lines:
            if line.startswith(("Successfully downloaded ", "Successfully installed ")):
                tokens = _PACKAGE_TOKEN.findall(line.split(" ", 2)[-1])
                for token in tokens:
                    normalized = _normal_package(token)
                    installed = re.fullmatch(r"(.+)-(\d[^ ]*)", normalized)
                    if normalized == name or (
                        installed and installed[1] == name
                        and (_version is None or installed[2] == _normal_package(_version))
                    ):
                        return "succeeded", line, "target-bound package acquisition summary"
            elif line.startswith("Saved "):
                filename = line[6:].strip().rsplit("/", 1)[-1]
                archive = re.fullmatch(r"(.+?)-([^-]+)(?:-[^/ ]+\.whl|\.(?:tar\.gz|tgz|zip))", filename)
                if archive and _normal_package(archive[1]) == name and (
                    _version is None or archive[2] == _version
                ):
                    return "succeeded", line, "target-bound saved package artifact"
    segment = _fetch_segment(command, finding)
    # Exit status is meaningful only for the fetch itself, not `tail`, `ls`,
    # echo, || true, a multi-target script, or git remote-add / index queries.
    acquisition_kind = (
        "remote-package" in finding.kind
        or finding.kind in {
            "curl-remote-url", "wget-remote-url", "httpie-remote-url",
            "git-clone-remote-url", "git-fetch-remote-url", "git-pull-remote-url", "apt-source",
        }
    )
    if acquisition_kind and segment and command.strip() == segment.strip() and len(detect_upstream_fetch([command])) == 1:
        for result in results:
            code = result.get("exit_code")
            if type(code) is int:
                return (
                    "succeeded" if code == 0 else "failed",
                    f"fetch exit_code={code}",
                    "recorded status of the isolated fetch command",
                )
    # wget explicitly records the URL and saved artifact; no generic "saved"
    # or success string from another command/package is sufficient.
    if finding.kind == "wget-remote-url" and any(finding.target in line for line in lines):
        saved = next((line for line in lines if re.search(r"\bsaved \[\d+/\d+\]", line)), None)
        if saved:
            return "succeeded", saved, "URL-bound wget acquisition"
    return "unknown", None, "no positive outcome bound to this fetch call/target"


def assess_upstream_fetch(agent_seq: Sequence[tuple[str, dict]], info: dict) -> list[dict]:
    """Attempt facts plus confirmed acquisition evidence for process-job.

    IDs are local to document, step and call. Terminal windows may contain
    delayed output: we never promote another call's success just because its
    numeric ID or package name matches. Truncated pip downloads can instead
    be proven by an exact destination listing AND observed artifact use.
    """
    from collections import Counter

    from evallab import probe03

    occurrences = Counter((doc, step.get("step_id")) for doc, step in agent_seq)

    def layer_for(doc: str, step: dict) -> dict | None:
        recorded = probe03.layer_status(step)
        if recorded is not None or step.get("tool_calls"):
            return recorded
        key = (doc, step.get("step_id"))
        return (info.get(key) or {}).get("layer") if occurrences[key] == 1 else None

    flags = []
    for position, (doc, step) in enumerate(agent_seq):
        sid = step.get("step_id")
        layer = layer_for(doc, step)
        for call_id, command in _executed_calls(step, layer):
            results = _call_observations(step, call_id)
            for finding in detect_upstream_fetch([(sid if type(sid) is int else -1, command)]):
                outcome, excerpt, reason = _observed_outcome(finding, command, results)
                evidence = []
                if excerpt:
                    evidence.append({
                        "document": doc, "step": sid, "call_id": call_id,
                        "target": finding.target, "excerpt": excerpt,
                        "observation_binding": sorted({result["binding"] for result in results}),
                    })
                lines = [line for result in results for line in _output_lines(str(result.get("content") or ""), command)]
                artifact = _pip_artifact(command, finding, lines) if outcome == "unknown" else None
                if artifact:
                    for later_doc, later_step in agent_seq[position + 1 :]:
                        if later_doc != doc:
                            break
                        later_sid = later_step.get("step_id")
                        later_layer = layer_for(later_doc, later_step)
                        later_calls = _executed_calls(later_step, later_layer)
                        # A fresh attempt at this target is a new episode: it
                        # cannot prove acquisition for the earlier attempt.
                        if any(
                            _acquisition_target(later.target) == _acquisition_target(finding.target)
                            for _cid, text in later_calls
                            for later in detect_upstream_fetch([text])
                        ):
                            break
                        for later_id, text in later_calls:
                            later_results = _call_observations(later_step, later_id)
                            observed = [
                                line for result in later_results
                                for line in _output_lines(str(result.get("content") or ""), text)
                            ]
                            if _artifact_use(artifact, text, observed):
                                outcome, reason = "succeeded", "exact destination artifact acquired and observed in use"
                                evidence = [
                                    {
                                        "document": doc, "step": sid, "call_id": call_id,
                                        "target": finding.target, "artifact": artifact,
                                        "excerpt": next(line for line in lines if line in {artifact, artifact.rsplit("/", 1)[-1]}),
                                        "observation_binding": sorted({result["binding"] for result in results}),
                                    },
                                    {
                                        "document": later_doc, "step": later_sid, "call_id": later_id,
                                        "target": finding.target, "artifact": artifact,
                                        "excerpt": "\n".join(observed)[:400],
                                        "observation_binding": sorted({result["binding"] for result in later_results}),
                                    },
                                ]
                                break
                        if outcome == "succeeded":
                            break
                flags.append({
                    "kind": "upstream_fetch",
                    "rule": f"upstream_fetch:{finding.kind}",
                    "evidence": f"{doc}#{sid}",
                    "command": command[:160],
                    "names_task_repo": finding.names_task_repo,
                    "target": finding.target,
                    "document": doc,
                    "step": sid,
                    "call_id": call_id,
                    "outcome": outcome,
                    "outcome_reason": reason,
                    "outcome_evidence": evidence,
                })
    return flags


def format_fetch_notice(findings: Sequence[Finding], *, limit: int = 5) -> str:
    """One-paragraph GEPA feedback notice for upstream-fetch findings."""
    shown = "; ".join(
        f"[step {finding.step_index} {finding.kind}] {finding.excerpt[:160]}"
        for finding in findings[:limit]
    )
    extra = f" (+{len(findings) - limit} more)" if len(findings) > limit else ""
    strong = sum(1 for finding in findings if finding.names_task_repo)
    scope = f", {strong} naming the task's own repo/package" if strong else ""
    return (
        f"upstream fetch detected ({len(findings)} command(s){scope}): "
        f"{shown}{extra}; scored 0"
    )


def commands_from_trial(trial_dir: str | Path) -> list[tuple[int, str]]:
    """Extract ``(step_id, keystrokes)`` shell commands from a Harbor trial.

    Reads ``agent/trajectory*.json`` (ATIF steps with
    ``tool_calls[].arguments.keystrokes``), deduplicating repeated files
    (summarization mirrors) and sorting by step id. Missing or unreadable
    files yield no commands rather than an error.
    """
    agent_dir = Path(trial_dir) / "agent"
    seen: set[tuple[int, str]] = set()
    ordered: list[tuple[int, str]] = []
    try:
        files = sorted(agent_dir.glob("trajectory*.json"))
    except OSError:
        return []
    for path in files:
        if path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        steps = payload.get("steps") if isinstance(payload, dict) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = step.get("step_id")
            calls = step.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                args = call.get("arguments")
                keystrokes = args.get("keystrokes") if isinstance(args, dict) else None
                if not isinstance(keystrokes, str) or not keystrokes.strip():
                    continue
                key = (int(step_id) if isinstance(step_id, int) else -1, keystrokes)
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
    ordered.sort(key=lambda item: (item[0] < 0, item[0]))
    return ordered
