"""Stdlib-only, Python 3.5-compatible payload for a native separate verifier.

This is not an OS sandbox. The parent supplies CPU/no-network containment and
immutable inputs. Apply the recorded patch on its base, restore trusted tests,
and score only complete observed test outcomes. Errors and skips stay unscored.
"""

import os
import re

#: Expected ``schema_version`` of the verifier config file.
CONFIG_SCHEMA_VERSION = "heldout-run/v1"
#: Expected ``schema_version`` of the embedded suite object.
SUITE_SCHEMA_VERSION = "heldout-suite/v1"
#: ``schema_version`` written into ``heldout-result.json``.
RESULT_SCHEMA_VERSION = "heldout-result/v1"
#: Provenance marker identifying this payload in result reports.
HELDOUT_VERIFIER_VERSION = "heldout-verifier/v1"

#: Supported test frameworks.
FRAMEWORKS = ("pytest", "unittest")

#: Upper bound for the configured test command timeout (seconds).
MAX_TIMEOUT_SEC = 3600
#: Timeout for each individual git plumbing call (seconds).
GIT_TIMEOUT_SEC = 300
#: Bytes of each captured test stream kept on disk (tail is kept).
MAX_LOG_BYTES = 131072
#: Refuse to scan an agent diff larger than this (bytes).
MAX_DIFF_BYTES = 256 * 1024 * 1024
#: Refuse an agent diff touching more distinct targets than this.
MAX_DIFF_TARGETS = 200000
#: Bytes of a process stderr tail embedded in error strings.
ERROR_TAIL_CHARS = 2000

#: Full git SHAs accepted for base/fix commits (SHA-1 or SHA-256 hex).
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
#: Drive-letter prefixed paths (defense on any platform).
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
#: Error text marking a pytest/unittest collection or import failure.
_COLLECTION_ERROR_RE = re.compile(
    r"error collecting|collection error|cannot import|no module named|"
    r"ModuleNotFoundError|ImportError|SyntaxError",
    re.IGNORECASE,
)
#: Tokens of a ``diff --git`` header line (handles C-style quoting).
_DIFF_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')
#: Gitlinks need another repository's bytes; ordinary symlinks are captured fully.
_LINK_MODE_RE = re.compile(
    r"^(?:(?:new file|deleted file|old|new) mode 160000|index \S+ 160000)$"
)
#: Dotted Python label (unittest target).
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
#: Qualname shape (``test_x`` or ``Class.test_x``).
_QUALNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
#: Safe environment variable name.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Bare interpreter basename for unittest command prefixes.
_PYTHON_BIN_RE = re.compile(r"^python[\d.]*$")

#: Child environment allowlist: non-secret process basics only. The pinned
#: config ``env`` (e.g. ``PYTHONPATH``) is layered on top. Host secrets are
#: never sourced: nothing else is inherited.
_CHILD_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LANGUAGE", "TZ", "TMPDIR")


def relpath_error(path):
    """Return a reason string when ``path`` is unsafe, else None.

    Rejects absolute paths, drive-qualified paths, NUL bytes, empty/dot
    segments (including ``..`` traversal and ``//``), and any ``.git``
    component.
    """
    if not isinstance(path, str) or not path:
        return "empty path"
    if "\x00" in path:
        return "NUL byte in path"
    if path.startswith("/") or path.startswith("\\"):
        return "absolute path"
    if _DRIVE_RE.match(path):
        return "drive-qualified path"
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return "dot-segment in path"
        if part == ".git":
            return ".git component in path"
    return None


def _c_unquote(token):
    """Decode one git C-style quoted path token to text."""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        raw = token[1:-1]
    else:
        return token
    out = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            nxt = raw[index + 1]
            if nxt == "n":
                out += b"\n"
                index += 2
            elif nxt == "t":
                out += b"\t"
                index += 2
            elif nxt == "\\":
                out += b"\\"
                index += 2
            elif nxt == '"':
                out += b'"'
                index += 2
            elif nxt in "01234567":
                match = re.match(r"[0-7]{1,3}", raw[index + 1 :])
                assert match is not None  # The preceding character is an octal digit.
                out.append(int(match.group(0), 8))
                index += 1 + len(match.group(0))
            else:
                out += nxt.encode("utf-8", "replace")
                index += 2
        else:
            out += char.encode("utf-8", "replace")
            index += 1
    return out.decode("utf-8", "replace")


def _strip_git_prefix(value, prefix):
    """Strip a ``diff --git`` ``a/``/``b/`` prefix; None for ``/dev/null``."""
    if value == "/dev/null":
        return None
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def scan_agent_diff(diff_path):
    """Scan a captured ``agent.diff`` for unreconstructable or unsafe content.

    Returns ``(problems, target_count)`` where ``problems`` is a list of
    ``code: detail`` strings (empty when the diff is fully applicable
    text) and ``target_count`` is the number of distinct target paths.
    Codes: ``binary_placeholder`` (a ``Binary files ... differ`` stanza
    with no reconstructable content, which ``git apply`` cannot restore),
    ``unsafe_path`` (absolute/traversal/``.git``/submodule introduction),
    ``diff_too_large``, ``diff_unreadable``.
    """
    try:
        size = os.path.getsize(diff_path)
    except OSError as exc:
        return (["diff_unreadable: cannot stat agent diff: {}".format(exc)], 0)
    if size > MAX_DIFF_BYTES:
        return (
            ["diff_too_large: agent diff is {} bytes (> {})".format(size, MAX_DIFF_BYTES)],
            0,
        )
    problems = []
    targets = set()
    try:
        with open(diff_path, "rb") as handle:
            for raw in handle:
                if len(raw) > 65536 and raw.startswith(b"diff --git "):
                    problems.append("unsafe_path: oversized diff header cannot be verified")
                    break
                text = raw[:65536].decode("utf-8", "replace").rstrip("\r\n")
                if text.startswith("Binary files ") and text.endswith(" differ"):
                    problems.append(
                        "binary_placeholder: unreconstructable binary stanza: {}".format(text[:200])
                    )
                    continue
                if _LINK_MODE_RE.search(text):
                    problems.append(
                        "unsafe_path: submodule mode requires unavailable repository bytes: {}".format(
                            text.strip()[:200]
                        )
                    )
                    continue
                candidates = []
                if text.startswith("diff --git "):
                    tokens = [_c_unquote(token) for token in _DIFF_TOKEN_RE.findall(text[11:])]
                    if len(tokens) >= 2:
                        src = _strip_git_prefix(tokens[-2], "a/")
                        dst = _strip_git_prefix(tokens[-1], "b/")
                        if src is not None:
                            candidates.append(src)
                        if dst is not None:
                            candidates.append(dst)
                        if src is None and dst is None:
                            problems.append("unsafe_path: diff header has no target")
                elif text.startswith("rename from "):
                    candidates.append(_c_unquote(text[12:].strip()))
                elif text.startswith("rename to "):
                    candidates.append(_c_unquote(text[10:].strip()))
                elif text.startswith("--- ") or text.startswith("+++ "):
                    rest = text[4:].strip()
                    if rest == "/dev/null":
                        continue
                    if rest.startswith("a/") or rest.startswith("b/"):
                        candidates.append(rest[2:])
                    elif text.startswith("+++ "):
                        candidates.append(rest.split("\t")[0])
                for candidate in candidates:
                    if not candidate or candidate in targets:
                        continue
                    targets.add(candidate)
                    if len(targets) > MAX_DIFF_TARGETS:
                        problems.append(
                            "diff_too_large: agent diff touches more than {} paths".format(
                                MAX_DIFF_TARGETS
                            )
                        )
                        return (problems, len(targets))
                    bad = relpath_error(candidate)
                    if bad is not None:
                        problems.append("unsafe_path: diff target {!r}: {}".format(candidate, bad))
    except OSError as exc:
        return (["diff_unreadable: cannot read agent diff: {}".format(exc)], 0)
    return (problems, len(targets))


