from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from typing import Any

from evallab.edit_signals import step_bash_commands
from evallab.task_protected import _READ_VERB_RE, task_protected_read_hits

_BLOB_RE = re.compile(r"/objects/([0-9a-f]{2})/([0-9a-f]{38,62})\b")

_GIT_RE = r"\bgit\b(?:\s+(?:-C\s+\S+|--git-dir=\S+|--work-tree=\S+|-c\s+\S+))*\s+"

_CATFILE_RE = re.compile(_GIT_RE + r"cat-file\s+(?:-[^\s]+\s+)*-p\s+([0-9a-fA-F]{7,64})\b")

_UNZIP_P_RE = re.compile(r"\bunzip\s+(?:-[^\s]*p[^\s]*\s+)([^\s;|&\"']+)")
_TAR_XO_RE = re.compile(r"\btar\s+[^\s;|&]*x[^\s;|&]*O\b[^\s;|&]*-f\s+([^\s;|&\"']+)")

_REDIRECT_RE = re.compile(r"(?<!\d)>{1,2}\s*([^\s;|&]+)")

_UNZIP_RE = re.compile(r"\bunzip\s+((?:-[^\s]+\s+)*)([^\s;|&]+?)(?:\s+-d\s+([^\s;|&]+))?(?:\s|$)")

_PAGER_RE = re.compile(r"\b(less|more|most)(\s|$)")

_RECORD_SUFFIXES = (".json", ".txt", ".log", ".md", ".yaml", ".yml", ".toml", ".csv")

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz", ".7z", ".rar", ".jar", ".whl")
_LISTING_RE = re.compile(r"--stat(?:=|\s|$)|--name-only|--name-status")
_DEFAULT_BRANCHES = frozenset({"main", "master", "head", "@", "trunk"})

_PEER_RECORD_NAMES = frozenset({"agent_evals.json", "message.txt", "verification.log"})

_PEER_TREE_DOCS = frozenset({"readme.md"})
_WRITER_CAT_RE = re.compile(r"(^|[;&\n])\s*(sudo\s+)?cat\s*>{1,2}")


def watched_blob_shas(sensor_paths: list[str]) -> list[str]:
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
    return [seg.strip() for seg in re.split(r"&&|\|\||[;\n]", keystrokes) if seg.strip()]


def _show_source(seg: str) -> str | None:
    match = re.search(_GIT_RE + r"show\s+(\S+)", seg)
    if not match:
        return None
    token = match.group(1)
    if token.startswith("-"):
        return None
    return token if ":" in token else None


def _is_record_source(source: str) -> bool:
    path = source.split(":", 1)[1].lower()
    return path.endswith(_RECORD_SUFFIXES)


def _is_bare_content_show(seg: str) -> bool:
    if not re.search(_GIT_RE + r"(show|diff|log\s+-p|show\s+-p)\b", seg):
        return False
    if _LISTING_RE.search(seg):
        return False
    if "--" in seg:
        return False
    return not re.search(r"\S+:\S+", seg)


def _derive(seg: str, files: set[str], dirs: set[str], cwd: str | None) -> None:
    if _PAGER_RE.search(seg):
        return
    for match in _REDIRECT_RE.finditer(seg):
        target = _resolve(_unquote(match.group(1)), cwd)
        if target is None or target == "/dev/null":
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
            src = _resolve(_unquote(match.group(2)), cwd)
            dest = _resolve(_unquote(match.group(3) or ""), cwd)
            if src is not None and src in files and dest is not None:
                dirs.add(dest)
    if re.search(r"\btar\s+[^\s;|&]*x[^\s;|&]*\b", seg) and re.search(
        _GIT_RE + r"(show|archive|cat-file)\b", seg
    ):
        dest = re.search(r"-C\s+([^\s;|&]+)", seg)
        if dest:
            resolved = _resolve(_unquote(dest.group(1)), cwd)
            if resolved is not None:
                dirs.add(resolved)


def _probe(step_id: Any, keys: str, prefixes: list[str]) -> bool:
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


def _resolve(path: str, cwd: str | None) -> str | None:
    path = _unquote(path.strip())
    if not path:
        return None
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    if cwd is None:
        return None
    return posixpath.normpath(posixpath.join(cwd, path))


def _base_branch(rev: str) -> str:
    return rev.split("/", 1)[-1].lower() if "/" in rev else rev.lower()


def _rev_is_peer(rev: str) -> bool:
    rev = rev.strip()
    if not rev or rev == "-" or rev.lower() in _DEFAULT_BRANCHES:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", rev):
        return True
    if rev in ("HEAD", "@") or rev.startswith(("HEAD", "@")):
        return False
    return _base_branch(rev) not in _DEFAULT_BRANCHES


def _is_local_source(src: str) -> str | None:
    src = _unquote(src)
    if src.startswith("file://"):
        src = src[len("file://") :]
    if not src.startswith("/") or "://" in src or "@" in src.split("/")[0]:
        return None
    return src


def _cd_target(seg: str) -> str | None:
    stripped = seg.strip()
    if stripped.startswith("("):
        return None
    match = re.match(r"cd\s+(\S+)", stripped)
    if not match:
        return None
    target = _unquote(match.group(1))
    if target in ("", "~", "-"):
        return None
    return target


def _clone_peer_dir(seg: str, cwd: str | None) -> str | None:
    match = re.search(_GIT_RE + r"clone\s+(.*)$", seg)
    if not match:
        return None
    body = match.group(1)
    branch: str | None = None
    branch_match = re.search(r"-b\s+(\S+)|--branch[=\s]+(\S+)", body)
    if branch_match:
        branch = branch_match.group(1) or branch_match.group(2)
    if branch is None or not _rev_is_peer(branch):
        return None
    positionals: list[str] = []
    skip_next = False
    for token in body.split():
        if skip_next:
            skip_next = False
            continue
        if token in ("-b", "--branch", "--depth", "-c", "--config"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    if not positionals:
        return None
    src = _is_local_source(positionals[0])
    if src is None:
        return None
    dst = positionals[1] if len(positionals) > 1 else posixpath.basename(src.rstrip("/"))
    return _resolve(dst, cwd)


def _checkout_tree(seg: str) -> str | None:
    match = re.search(_GIT_RE + r"(checkout|switch)\s+(.*)$", seg)
    if not match:
        return None
    rest = match.group(2)
    if "--" in rest:
        return None
    tokens = rest.split()
    args: list[str] = []
    skip_next = False
    create = False
    for token in tokens:
        if skip_next:
            skip_next = False
            if token in ("-b", "-B", "-c", "--orphan"):
                create = True
                continue
            args.append(token)
            continue
        if token in ("-b", "-B", "-c", "--orphan"):
            skip_next = True
            create = True
            continue
        if token.startswith("-"):
            continue
        args.append(token)
    if not args:
        return None
    if create:
        return "peer" if len(args) > 1 and _rev_is_peer(args[1]) else None
    rev = args[0]
    if rev == "-":
        return None
    if "." in rev and "/" not in rev and not re.fullmatch(r"[0-9a-fA-F]{7,64}", rev):
        return None
    return "peer" if _rev_is_peer(rev) else "own"


def _tree_member_hit(seg: str) -> bool:
    if not _READ_VERB_RE.search(seg) or _WRITER_CAT_RE.search(seg):
        return False
    for token in re.findall(r"[^\s;|&<>]+", seg):
        token = _unquote(token)
        if not token or token.startswith("-") or token.isdigit():
            continue
        if token.startswith("/dev/"):
            continue
        if "/" not in token and "." not in token:
            continue
        if posixpath.basename(token).lower() not in _PEER_RECORD_NAMES:
            bare = token[2:] if token.startswith("./") else token
            if "/" not in bare and bare.lower() in _PEER_TREE_DOCS:
                continue
            return True
    return False


def _only_records(seg: str) -> bool:
    found = False
    for token in re.findall(r"[^\s;|&<>]+", seg):
        token = _unquote(token)
        if not token or token.startswith("-") or token.isdigit():
            continue
        if token.startswith("/dev/"):
            continue
        if "/" not in token and "." not in token:
            continue
        found = True
        if posixpath.basename(token).lower() not in _PEER_RECORD_NAMES:
            return False
    return found


def _resolved_read(seg: str, prefixes: list[str], cwd: str | None) -> bool:
    if cwd is None or not prefixes:
        return False
    if not _READ_VERB_RE.search(seg) or _WRITER_CAT_RE.search(seg):
        return False
    for token in re.findall(r"[^\s;|&<>]+", seg):
        token = _unquote(token)
        if not token or token.startswith("-") or token.isdigit():
            continue
        if token.startswith("/dev/"):
            continue
        if "/" not in token and "." not in token:
            continue
        resolved = _resolve(token, cwd)
        if resolved is None:
            continue
        if any(resolved == p or resolved.startswith(p + "/") for p in prefixes):
            return True
    return False


def _content_kind(
    seg: str,
    files: set[str],
    watched_shas: list[str],
    derived: list[str],
    watched: list[str],
    tree: str,
    cwd: str | None,
    step_id: Any,
) -> str | None:
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
    if (
        derived
        and not _only_records(seg)
        and (_probe(step_id, seg, derived) or _resolved_read(seg, derived, cwd))
    ):
        return "derived_member_read"
    if watched and (_probe(step_id, seg, watched) or _resolved_read(seg, watched, cwd)):
        return "watched_path_read"
    if tree == "peer" and _tree_member_hit(seg):
        return "peer_tree_member_read"
    return None


def _update_state(seg: str, state: dict[str, Any]) -> None:
    target = _cd_target(seg)
    if target is not None:
        cwd = state["cwd"]
        if posixpath.isabs(target):
            state["cwd"] = posixpath.normpath(target)
        elif cwd is not None:
            state["cwd"] = posixpath.normpath(posixpath.join(cwd, target))
    peer_dir = _clone_peer_dir(seg, state["cwd"])
    if peer_dir is not None:
        state["peer_dirs"].add(peer_dir)
    tree = _checkout_tree(seg)
    if tree is not None:
        state["tree"] = tree
    _derive(seg, state["files"], state["dirs"], state["cwd"])


class CorroborationState:

    def __init__(
        self,
        watched_shas: Sequence[str] = (),
        watched_prefixes: Sequence[str] = (),
    ) -> None:
        self._watched_shas = list(watched_shas)
        self._watched_prefixes = list(watched_prefixes)
        self._state: dict[str, Any] = {
            "cwd": None,
            "tree": "own",
            "files": set(),
            "dirs": set(),
            "peer_dirs": set(),
        }

    def snapshot(self) -> tuple[Any, str, set[str], set[str], set[str]]:
        state = self._state
        return (
            state["cwd"],
            state["tree"],
            set(state["files"]),
            set(state["dirs"]),
            set(state["peer_dirs"]),
        )

    def restore(self, snapshot: tuple[Any, str, set[str], set[str], set[str]]) -> None:
        cwd, tree, files, dirs, peer_dirs = snapshot
        self._state = {
            "cwd": cwd,
            "tree": tree,
            "files": set(files),
            "dirs": set(dirs),
            "peer_dirs": set(peer_dirs),
        }

    def advance(self, step: Mapping[str, Any]) -> list[dict[str, Any]]:
        state = self._state
        hits: list[dict[str, Any]] = []
        step_id = step.get("step_id")
        for keys in step_bash_commands(dict(step)):
            for seg in _segments(keys):
                kind = _content_kind(
                    seg,
                    state["files"],
                    self._watched_shas,
                    sorted(state["files"] | state["dirs"] | state["peer_dirs"]),
                    self._watched_prefixes,
                    state["tree"],
                    state["cwd"],
                    step_id,
                )
                if kind is not None:
                    hits.append({"step": step_id, "kind": kind, "command": seg[:200]})
                _update_state(seg, state)
        return hits


def content_corroboration(
    agent_steps: list[dict[str, Any]],
    watched_shas: list[str],
    watched_prefixes: list[str],
) -> list[dict[str, Any]]:
    state = CorroborationState(watched_shas, watched_prefixes)
    hits: list[dict[str, Any]] = []
    for step in agent_steps:
        hits.extend(state.advance(step))
    return hits
