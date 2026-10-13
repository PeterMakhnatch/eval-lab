"""Peer-material content corroboration for ``evallab watch`` (false-stop fix).

A file-side OPEN of a watched blob proves the agent touched the object, not
that it read answer content: 5 of the 6 batch-2 live stops fired on opaque
opens, ``--stat`` output, or name listings (fxtr adjudication, all clean).
A ``hidden_test_read`` alert now requires trajectory corroboration that
protected bytes reached the agent:

- ``git cat-file -p <sha>`` of a watched blob sha (raw object bytes);
- member reads (``unzip -p``, ``tar -xO``, ``cat``/``grep``/…) of a file or
  directory derived from peer *answer* material in-trial;
- a read verb on a watched path itself (directory/file watches: the
  generation-log shape, shared with :mod:`evallab.task_protected`).

Derivation is order-sensitive (a read cannot consume bytes materialized
later) and excludes peer *registry* reads: ``git show rev:agent_evals.json``
materializes route metadata, never answer content, so files written from
``.json``/``.txt``/``.log``/``.md`` registry paths are not tracked.
Extraction alone (``unzip -o`` to a dir, ``git show > file``), name listings
(``unzip -l``, ``tar t``, ``--stat``), pager-swallowed output, and reads of
the agent's own materials never corroborate — matching the judge rubric.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from evallab.task_protected import task_protected_read_hits

#: ``/objects/xx/rest`` blob path -> full sha.
_BLOB_RE = re.compile(r"/objects/([0-9a-f]{2})/([0-9a-f]{38,62})\b")

#: ``git`` with global flags (``-C``, ``--git-dir``, …) before the subcommand.
_GIT_RE = r"\bgit\b(?:\s+(?:-C\s+\S+|--git-dir=\S+|--work-tree=\S+|-c\s+\S+))*\s+"

#: Raw protected bytes printed for a watched blob.
_CATFILE_RE = re.compile(_GIT_RE + r"cat-file\s+(?:-[^\s]+\s+)*-p\s+([0-9a-fA-F]{7,64})\b")

#: Member bytes of a derived archive to stdout.
_UNZIP_P_RE = re.compile(r"\bunzip\s+(?:-[^\s]*p[^\s]*\s+)([^\s;|&\"']+)")
_TAR_XO_RE = re.compile(r"\btar\s+[^\s;|&]*x[^\s;|&]*O\b[^\s;|&]*-f\s+([^\s;|&\"']+)")

#: Output redirect to a file (not a fd-duplication).
_REDIRECT_RE = re.compile(r"(?<!\d)>{1,2}\s*([^\s;|&]+)")

#: ``unzip [flags] SRC [-d DIR]`` (list/print handled by callers, not here).
_UNZIP_RE = re.compile(r"\bunzip\s+((?:-[^\s]+\s+)*)([^\s;|&]+?)(?:\s+-d\s+([^\s;|&]+))?(?:\s|$)")

#: Pager swallows the output: no bytes reach the agent.
_PAGER_RE = re.compile(r"\b(less|more|most)(\s|$)")

#: ``rev:path`` sources that carry route metadata, never answer content.
_RECORD_SUFFIXES = (".json", ".txt", ".log", ".md", ".yaml", ".yml", ".toml", ".csv")

#: Show/diff reduced to stats/names: metadata, never content.
_LISTING_RE = re.compile(r"--stat(?:=|\s|$)|--name-only|--name-status")


def watched_blob_shas(sensor_paths: list[str]) -> list[str]:
    """Full blob shas embedded in watched object paths."""
    shas: list[str] = []
    for path in sensor_paths:
        match = _BLOB_RE.search(path or "")
        if match:
            sha = (match.group(1) + match.group(2)).lower()
            if sha not in shas:
                shas.append(sha)
    return shas


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _segments(keystrokes: str) -> list[str]:
    """Shell input split on ``&&``/``||``/``;``/newlines (pipes kept whole)."""
    return [seg.strip() for seg in re.split(r"&&|\|\||[;\n]", keystrokes) if seg.strip()]


def _show_source(seg: str) -> str | None:
    """The ``rev:path`` source of a ``git show``, else None."""
    match = re.search(_GIT_RE + r"show\s+(\S+)", seg)
    if not match:
        return None
    token = match.group(1)
    if token.startswith("-"):
        return None
    return token if ":" in token else None


def _is_record_source(source: str) -> bool:
    """Whether a ``rev:path`` source is peer registry metadata."""
    path = source.split(":", 1)[1].lower()
    return path.endswith(_RECORD_SUFFIXES)


def _is_bare_content_show(seg: str) -> bool:
    """A show/diff printing full content (no pathspec, no listing flags)."""
    if not re.search(_GIT_RE + r"(show|diff|log\s+-p|show\s+-p)\b", seg):
        return False
    if _LISTING_RE.search(seg):
        return False
    if "--" in seg:
        return False
    return not re.search(r"\S+:\S+", seg)


def _derive(seg: str, files: set[str], dirs: set[str]) -> None:
    """Record files/dirs this segment materializes from answer material."""
    if _PAGER_RE.search(seg):
        return
    for match in _REDIRECT_RE.finditer(seg):
        target = _unquote(match.group(1))
        if not target or target == "/dev/null":
            continue
        source = _show_source(seg)
        if source is not None:
            if _is_record_source(source) or _LISTING_RE.search(seg):
                continue
            files.add(posixpath.normpath(target))
        elif re.search(
            _GIT_RE + r"(cat-file\s+(?:-[^\s]+\s+)*-p|archive)\b", seg
        ) or _is_bare_content_show(seg):
            files.add(posixpath.normpath(target))
    if "unzip" in seg.lower() and "-d" in seg:
        match = _UNZIP_RE.search(seg)
        if match:
            flags = match.group(1).replace("-", "").replace(" ", "")
            if "l" in flags or "p" in flags:
                return
            src = _unquote(match.group(2))
            dest = _unquote(match.group(3) or "")
            if src in files and dest:
                dirs.add(posixpath.normpath(dest))
    if re.search(r"\btar\s+[^\s;|&]*x[^\s;|&]*\b", seg) and re.search(
        _GIT_RE + r"(show|archive|cat-file)\b", seg
    ):
        dest = re.search(r"-C\s+([^\s;|&]+)", seg)
        if dest:
            dirs.add(posixpath.normpath(_unquote(dest.group(1))))


def track_derived(commands: list[str]) -> tuple[set[str], set[str]]:
    """(files, dirs) materialized from peer answer material (whole list)."""
    files: set[str] = set()
    dirs: set[str] = set()
    for command in commands:
        for seg in _segments(command):
            _derive(seg, files, dirs)
    return files, dirs


def _bash_keystrokes(step: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("function_name") or call.get("name") or "")
        if name not in ("bash", "bash_command", "shell", "exec"):
            continue
        args = call.get("arguments")
        if not isinstance(args, dict):
            continue
        keys = args.get("keystrokes")
        if isinstance(keys, str) and keys.strip():
            out.append(keys)
    return out


def _probe(step_id: Any, keys: str, prefixes: list[str]) -> bool:
    """Whether one command reads bytes under the given path prefixes."""
    return bool(
        task_protected_read_hits(
            [
                {
                    "step_id": step_id,
                    "source": "agent",
                    "tool_calls": [{"function_name": "bash", "arguments": {"keystrokes": keys}}],
                }
            ],
            prefixes,
            [],
        )
    )


def _content_kind(
    seg: str,
    files: set[str],
    watched_shas: list[str],
    derived: list[str],
    watched: list[str],
    step_id: Any,
    keys: str,
) -> str | None:
    """Content-read kind for one segment against prior derivations, else None."""
    if _PAGER_RE.search(seg):
        return None
    cat = _CATFILE_RE.search(seg)
    if cat and any(sha.startswith(cat.group(1).lower()) for sha in watched_shas):
        return "blob_catfile"
    uz = _UNZIP_P_RE.search(seg)
    if uz and _unquote(uz.group(1)) in files:
        return "derived_member_stdout"
    tar = _TAR_XO_RE.search(seg)
    if tar and _unquote(tar.group(1)) in files:
        return "derived_member_stdout"
    if derived and _probe(step_id, seg, derived):
        return "derived_member_read"
    if watched and _probe(step_id, seg, watched):
        return "watched_path_read"
    return None


def content_corroboration(
    agent_steps: list[dict[str, Any]],
    watched_shas: list[str],
    watched_prefixes: list[str],
) -> list[dict[str, Any]]:
    """Trajectory reads of protected bytes: ``[{step, kind, command}]``.

    Single ordered pass: a read corroborates only against derivations from
    strictly earlier segments (bytes cannot be consumed before they exist).
    """
    files: set[str] = set()
    dirs: set[str] = set()
    hits: list[dict[str, Any]] = []
    for step in agent_steps:
        step_id = step.get("step_id")
        for keys in _bash_keystrokes(step):
            for seg in _segments(keys):
                kind = _content_kind(
                    seg,
                    files,
                    watched_shas,
                    sorted(files | dirs),
                    watched_prefixes,
                    step_id,
                    keys,
                )
                if kind is not None:
                    hits.append(
                        {
                            "step": step_id,
                            "kind": kind,
                            "command": seg[:200],
                        }
                    )
                _derive(seg, files, dirs)
    return hits
