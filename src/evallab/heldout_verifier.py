"""Stdlib-only, Python 3.5-compatible payload for a native separate verifier.

This is not an OS sandbox. The parent supplies CPU/no-network containment and
immutable inputs. Apply the recorded patch on its base, restore trusted tests,
and score only complete observed test outcomes. Errors and skips stay unscored.
"""

import argparse
import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import runpy
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET

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


def is_hex64(value):
    """Return True when ``value`` is an unprefixed lowercase SHA256 hex digest."""
    return isinstance(value, str) and _HEX64_RE.match(value) is not None


def is_commit_sha(value):
    """Return True when ``value`` is a full 40- or 64-char hex commit SHA."""
    return isinstance(value, str) and (
        _HEX40_RE.match(value) is not None or _HEX64_RE.match(value) is not None
    )


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


def is_safe_relpath(path):
    """Return True when ``path`` is a safe repository-relative path."""
    return relpath_error(path) is None


def sha256_hex_of_bytes(data):
    """Return the unprefixed hex SHA256 of ``data`` bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_hex_of_file(path):
    """Return the unprefixed hex SHA256 of a file, streaming from disk."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_within(base_dir, relpath):
    """Resolve ``relpath`` under ``base_dir``; return abspath or None on escape."""
    if relpath_error(relpath) is not None:
        return None
    joined = os.path.normpath(os.path.join(base_dir, relpath))
    base = os.path.normpath(base_dir)
    if joined == base or not joined.startswith(base + os.sep):
        return None
    return joined


def _reason_of(error):
    """Map a ``code: detail`` error string to its machine-readable reason code."""
    if not error or ":" not in error:
        return "unknown"
    return error.split(":", 1)[0].strip() or "unknown"


def _is_nonempty_str(value):
    return isinstance(value, str) and len(value) > 0


def _check_tests_entry(entry):
    errors = []
    if not isinstance(entry, dict):
        return ["schema: suite test entry must be an object"]
    node_id = entry.get("node_id")
    path = entry.get("path")
    qualname = entry.get("qualname")
    fingerprint = entry.get("fingerprint")
    change = entry.get("change")
    if not _is_nonempty_str(node_id):
        errors.append("schema: test entry needs a non-empty node_id")
    if not _is_nonempty_str(path):
        errors.append("schema: test entry needs a non-empty path")
    else:
        bad = relpath_error(path)
        if bad is not None:
            errors.append("unsafe_path: test path {!r}: {}".format(path, bad))
        if not path.endswith(".py"):
            errors.append("schema: test path must end with .py: {!r}".format(path))
    if not _is_nonempty_str(qualname) or _QUALNAME_RE.match(qualname) is None:
        errors.append("schema: bad qualname: {!r}".format(qualname))
    if not is_hex64(fingerprint):
        errors.append("schema: bad fingerprint: {!r}".format(fingerprint))
    if change not in ("added", "modified"):
        errors.append("schema: bad change label: {!r}".format(change))
    if (
        _is_nonempty_str(node_id)
        and _is_nonempty_str(path)
        and _is_nonempty_str(qualname)
        and _QUALNAME_RE.match(qualname) is not None
    ):
        expected = path + "::" + qualname.replace(".", "::")
        if node_id != expected:
            errors.append(
                "schema: node_id {!r} does not match path+qualname {!r}".format(
                    node_id, expected
                )
            )
    return errors


def _check_file_entry(entry):
    errors = []
    if not isinstance(entry, dict):
        return ["schema: suite file entry must be an object"]
    path = entry.get("path")
    mode = entry.get("mode")
    content = entry.get("content_base64")
    digest = entry.get("sha256")
    if not _is_nonempty_str(path):
        errors.append("schema: file entry needs a non-empty path")
    else:
        bad = relpath_error(path)
        if bad is not None:
            errors.append("unsafe_path: file path {!r}: {}".format(path, bad))
    if mode is None:
        if content is not None or digest is not None:
            errors.append(
                "schema: deletion entry must carry null content and digest: {!r}".format(
                    path
                )
            )
        return errors
    if mode not in ("100644", "100755"):
        errors.append("schema: unsupported file mode {!r} for {!r}".format(mode, path))
        return errors
    if not isinstance(content, str):
        errors.append("digest_mismatch: missing content for {!r}".format(path))
        return errors
    if not is_hex64(digest):
        errors.append("schema: bad sha256 for {!r}".format(path))
        return errors
    try:
        raw = base64.b64decode(content.encode("ascii"), validate=True)
    except Exception:
        errors.append("digest_mismatch: content of {!r} is not valid base64".format(path))
        return errors
    if sha256_hex_of_bytes(raw) != digest:
        errors.append("digest_mismatch: sha256 mismatch for {!r}".format(path))
    return errors


def validate_suite(suite):
    """Validate an embedded ``heldout-suite/v1`` object.

    Returns a list of ``code: detail`` error strings (empty when valid).
    Codes are ``schema`` (malformed metadata), ``unsafe_path`` (a path
    that must never be written), and ``digest_mismatch`` (payload bytes
    that do not match their recorded digest).
    """
    if not isinstance(suite, dict):
        return ["schema: suite must be an object"]
    errors = []
    if suite.get("schema_version") != SUITE_SCHEMA_VERSION:
        errors.append(
            "schema: suite schema_version must be {!r}".format(SUITE_SCHEMA_VERSION)
        )
    if not _is_nonempty_str(suite.get("task_name")):
        errors.append("schema: suite needs a non-empty task_name")
    workdir = suite.get("workdir")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        errors.append("schema: suite workdir must be an absolute path")
    if not is_commit_sha(suite.get("base_commit")):
        errors.append("schema: suite base_commit must be a full hex SHA")
    if not is_commit_sha(suite.get("fix_commit")):
        errors.append("schema: suite fix_commit must be a full hex SHA")
    fix_parent = suite.get("fix_parent")
    if fix_parent is not None and not is_commit_sha(fix_parent):
        errors.append("schema: suite fix_parent must be a full hex SHA or null")
    if suite.get("status") not in ("ready", "no_extra_tests", "unavailable"):
        errors.append("schema: suite status must be ready|no_extra_tests|unavailable")
    tests = suite.get("tests")
    if not isinstance(tests, list):
        errors.append("schema: suite tests must be a list")
        tests = []
    seen_nodes = set()
    for entry in tests:
        errors.extend(_check_tests_entry(entry))
        node_id = entry.get("node_id") if isinstance(entry, dict) else None
        if _is_nonempty_str(node_id):
            if node_id in seen_nodes:
                errors.append("schema: duplicate node_id {!r}".format(node_id))
            seen_nodes.add(node_id)
    files = suite.get("files")
    if not isinstance(files, list):
        errors.append("schema: suite files must be a list")
        files = []
    seen_paths = set()
    for entry in files:
        errors.extend(_check_file_entry(entry))
        file_path = entry.get("path") if isinstance(entry, dict) else None
        if _is_nonempty_str(file_path):
            if file_path in seen_paths:
                errors.append("schema: duplicate file path {!r}".format(file_path))
            seen_paths.add(file_path)
    payload_paths = {
        entry.get("path") for entry in files
        if isinstance(entry, dict) and entry.get("mode") is not None
    }
    for entry in tests:
        if isinstance(entry, dict) and entry.get("path") not in payload_paths:
            errors.append("schema: selected test has no authoritative file payload")
    for key in ("exclusions", "refusals", "notes"):
        if not isinstance(suite.get(key), list):
            errors.append("schema: suite {} must be a list".format(key))
    if suite.get("status") == "ready" and not tests and isinstance(suite.get("tests"), list):
        errors.append("schema: ready suite has no selected tests")
    return errors


def validate_config(config):
    """Validate a ``heldout-run/v1`` config object (suite payload excluded).

    The embedded suite itself is validated separately by
    :func:`validate_suite` so the caller can report ``suite_not_ready``
    distinctly. Returns ``code: detail`` errors; empty means valid.
    """
    if not isinstance(config, dict):
        return ["schema: config must be an object"]
    errors = []
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        errors.append(
            "schema: config schema_version must be {!r}".format(CONFIG_SCHEMA_VERSION)
        )
    if not isinstance(config.get("suite"), dict):
        errors.append("schema: config suite must be an object")
    patch = config.get("agent_patch")
    if not isinstance(patch, dict):
        errors.append("schema: config agent_patch must be an object")
    else:
        if not _is_nonempty_str(patch.get("path")):
            errors.append("schema: agent_patch.path must be a non-empty string")
        if not is_hex64(patch.get("sha256")):
            errors.append("schema: agent_patch.sha256 must be unprefixed hex SHA256")
    framework = config.get("framework")
    if framework not in FRAMEWORKS:
        errors.append("schema: framework must be one of {!r}".format(list(FRAMEWORKS)))
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not _is_nonempty_str(token) for token in command)
    ):
        errors.append("schema: command must be a non-empty list of non-empty strings")
    elif framework == "pytest":
        if not any("pytest" in token for token in command):
            errors.append("schema: pytest command prefix must reference pytest")
    elif framework == "unittest":
        binary = os.path.basename(command[0])
        if _PYTHON_BIN_RE.match(binary) is None:
            errors.append(
                "schema: unittest command must be a Python interpreter prefix"
            )
        if any(token == "-m" or "unittest" in token for token in command):
            errors.append(
                "schema: unittest command must be a bare interpreter prefix "
                "(no -m/unittest tokens)"
            )
    env = config.get("env")
    if env is None:
        pass
    elif not isinstance(env, dict):
        errors.append("schema: env must be an object when present")
    else:
        for key, value in env.items():
            if not isinstance(key, str) or _ENV_NAME_RE.match(key) is None:
                errors.append("schema: bad env name: {!r}".format(key))
            if not isinstance(value, str):
                errors.append("schema: env value for {!r} must be a string".format(key))
    bootstrap = config.get("bootstrap")
    if bootstrap is not None and not _is_nonempty_str(bootstrap):
        errors.append("schema: bootstrap must be null or a non-empty string")
    if framework == "pytest" and bootstrap is not None:
        errors.append("schema: bootstrap is only for unittest setup; pytest needs null")
    timeout = config.get("timeout_sec")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not timeout > 0
        or not timeout <= MAX_TIMEOUT_SEC
    ):
        errors.append(
            "schema: timeout_sec must be a positive number <= {}".format(
                MAX_TIMEOUT_SEC
            )
        )
    return errors


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


def run_command(argv, cwd, env, timeout_sec):
    """Run a process group with a deadline and bounded in-memory output tails."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return {
            "returncode": None, "stdout": "", "stderr": "", "timed_out": False,
            "error": "spawn failed: {}".format(exc), "duration_sec": 0.0,
        }
    tails = [bytearray(), bytearray()]

    def drain(stream, destination):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                destination.extend(chunk)
                if len(destination) > MAX_LOG_BYTES:
                    del destination[:-MAX_LOG_BYTES]
        finally:
            stream.close()

    readers = []
    for index, stream in enumerate((proc.stdout, proc.stderr)):
        reader = threading.Thread(target=drain, args=(stream, tails[index]))
        reader.daemon = True
        reader.start()
        readers.append(reader)
    timed_out = False
    try:
        proc.wait(timeout=float(timeout_sec))
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=0.2)
    if any(reader.is_alive() for reader in readers):
        # A descendant may keep a pipe open after the test parent exits.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        for reader in readers:
            reader.join(timeout=1)
    incomplete = any(reader.is_alive() for reader in readers)
    return {
        "returncode": proc.returncode,
        "stdout": bytes(tails[0]).decode("utf-8", "replace"),
        "stderr": bytes(tails[1]).decode("utf-8", "replace"),
        "timed_out": timed_out,
        "error": "incomplete child output" if incomplete else None,
        "duration_sec": time.monotonic() - started,
    }


def _minimal_env():
    """Non-secret process basics so git/python can execute."""
    env = {}
    for key in _CHILD_ENV_ALLOW:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if "PATH" not in env:
        env["PATH"] = "/usr/bin:/bin"
    return env


def _child_env(config_env):
    """Build the test child environment: allowlist plus pinned config env."""
    env = _minimal_env()
    if config_env:
        for key, value in config_env.items():
            env[key] = value
    return env


def _git_env():
    env = _minimal_env()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _tail(text, limit):
    if len(text) > limit:
        return text[-limit:]
    return text


def restore_base(workdir, base_commit, timeout=GIT_TIMEOUT_SEC):
    """Reset the workspace to the exact recorded base commit.

    Runs ``git reset --hard <base>`` plus ``git clean -fdq`` (untracked
    agent droppings go; ignored build artifacts stay, since ``git add -A``
    never captured ignored files). Returns None on success or a
    ``code: detail`` error (``missing_workdir``, ``missing_base`` when the
    base commit is absent -- never a silent pristine-base fallback -- or
    ``git_error``).
    """
    if not os.path.isdir(workdir):
        return "missing_workdir: workdir is not a directory: {!r}".format(workdir)
    env = _git_env()
    probe = run_command(["git", "rev-parse", "--git-dir"], workdir, env, timeout)
    if probe["timed_out"] or probe["returncode"] != 0:
        return "git_error: workdir is not a git repository: {}".format(
            _tail((probe["stderr"] or probe["error"] or ""), ERROR_TAIL_CHARS).strip()
        )
    present = run_command(
        ["git", "cat-file", "-e", base_commit + "^{commit}"], workdir, env, timeout
    )
    if present["timed_out"] or present["returncode"] != 0:
        return (
            "missing_base: base commit {!r} is not present; "
            "refusing pristine-base fallback".format(base_commit)
        )
    reset = run_command(["git", "reset", "--hard", base_commit], workdir, env, timeout)
    if reset["timed_out"] or reset["returncode"] != 0:
        return "git_error: reset to base failed: {}".format(
            _tail(reset["stderr"] or reset["error"] or "", ERROR_TAIL_CHARS).strip()
        )
    clean = run_command(["git", "clean", "-fdq"], workdir, env, timeout)
    if clean["timed_out"] or clean["returncode"] != 0:
        return "git_error: untracked cleanup failed: {}".format(
            _tail(clean["stderr"] or clean["error"] or "", ERROR_TAIL_CHARS).strip()
        )
    return None


def apply_agent_diff(workdir, diff_path, timeout=GIT_TIMEOUT_SEC):
    """Apply the complete captured agent diff; check before mutating.

    Returns None on success or ``apply_conflict``/``git_error`` details.
    """
    env = _git_env()
    check = run_command(
        ["git", "apply", "--check", "--whitespace=nowarn", diff_path],
        workdir,
        env,
        timeout,
    )
    if check["timed_out"] or check["returncode"] != 0:
        return "apply_conflict: agent diff does not apply cleanly: {}".format(
            _tail(check["stderr"] or check["error"] or "", ERROR_TAIL_CHARS).strip()
        )
    applied = run_command(
        ["git", "apply", "--whitespace=nowarn", diff_path], workdir, env, timeout
    )
    if applied["timed_out"] or applied["returncode"] != 0:
        return "git_error: agent diff apply failed: {}".format(
            _tail(applied["stderr"] or applied["error"] or "", ERROR_TAIL_CHARS).strip()
        )
    return None


def _ensure_dir(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST or not os.path.isdir(path):
            return "cannot create directory {!r}: {}".format(path, exc)
    return None


def overlay_suite_files(workdir, files):
    """Write authoritative suite files verbatim over the patched workspace.

    Deletion entries (null mode/content) remove the path. Refuses unsafe
    paths, writes through pre-existing symlinks, non-directory path
    collisions, and digest mismatches. Returns None or a ``code: detail``
    error (``unsafe_path``, ``digest_mismatch``, ``overlay_error``).
    """
    base = os.path.normpath(workdir)
    for entry in files:
        rel = entry.get("path")
        mode = entry.get("mode")
        content = entry.get("content_base64")
        digest = entry.get("sha256")
        bad = relpath_error(rel) if isinstance(rel, str) else "empty path"
        if bad is not None:
            return "unsafe_path: suite file {!r}: {}".format(rel, bad)
        target = os.path.normpath(os.path.join(base, rel))
        if target == base or not target.startswith(base + os.sep):
            return "unsafe_path: suite file escapes workdir: {!r}".format(rel)
        parts = rel.split("/")
        prefix = base
        for part in parts[:-1]:
            prefix = os.path.join(prefix, part)
            try:
                info = os.lstat(prefix)
            except OSError:
                break
            if stat.S_ISLNK(info.st_mode):
                return "unsafe_path: suite file crosses symlink: {!r}".format(rel)
            if not stat.S_ISDIR(info.st_mode):
                return "overlay_error: path component is not a directory: {!r}".format(
                    prefix
                )
        if mode is None:
            try:
                if os.path.islink(target) or os.path.isfile(target):
                    os.remove(target)
                elif os.path.isdir(target):
                    return "overlay_error: refusing to delete directory: {!r}".format(
                        rel
                    )
            except OSError as exc:
                return "overlay_error: cannot delete {!r}: {}".format(rel, exc)
            continue
        if os.path.islink(target):
            return "unsafe_path: refusing to write through symlink: {!r}".format(rel)
        try:
            raw = base64.b64decode(content.encode("ascii"), validate=True)
        except Exception:
            return "digest_mismatch: content of {!r} is not valid base64".format(rel)
        if sha256_hex_of_bytes(raw) != digest:
            return "digest_mismatch: sha256 mismatch for {!r}".format(rel)
        parent = os.path.dirname(target)
        problem = _ensure_dir(parent)
        if problem is not None:
            return "overlay_error: {}".format(problem)
        try:
            with open(target, "wb") as handle:
                handle.write(raw)
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
        except OSError as exc:
            return "overlay_error: cannot write {!r}: {}".format(rel, exc)
        try:
            if sha256_hex_of_file(target) != digest:
                return "digest_mismatch: read-back mismatch for {!r}".format(rel)
        except OSError as exc:
            return "overlay_error: cannot read back {!r}: {}".format(rel, exc)
    return None


def node_id_to_unittest_label(node_id):
    """Convert ``path.py::Class::test_x`` to dotted ``path.Class.test_x``.

    Raises ValueError on malformed ids.
    """
    if not isinstance(node_id, str) or "::" not in node_id:
        raise ValueError("malformed node id: {!r}".format(node_id))
    parts = node_id.split("::")
    rel = parts[0]
    rest = parts[1:]
    if not rel.endswith(".py") or not rest or any(not piece for piece in rest):
        raise ValueError("malformed node id: {!r}".format(node_id))
    label = ".".join([rel[:-3].replace("/", ".")] + rest)
    if _LABEL_RE.match(label) is None:
        raise ValueError("node id is not a dotted label: {!r}".format(node_id))
    return label


def parse_junit_xml(data):
    """Count observed testcase elements, refusing contradictory or malformed totals."""
    result = {
        "tests": 0, "failures": 0, "errors": 0, "skipped": 0,
        "passed": 0, "testcases": 0, "has_collection_error": False,
        "unparseable": False,
    }
    observed_ids = []
    try:
        root = ET.fromstring(data)
        if root.tag not in ("testsuite", "testsuites"):
            raise ValueError("not a JUnit report")
        for case in root.iter("testcase"):
            result["tests"] += 1
            result["failures"] += bool(case.findall("failure"))
            result["errors"] += bool(case.findall("error"))
            result["skipped"] += bool(case.findall("skipped"))
            observed_ids.append(
                (case.get("classname") or "") + "."
                + (case.get("name") or "").split("[", 1)[0]
            )
        for element in root.iter("error"):
            text = (element.get("message") or "") + (element.text or "")
            if _COLLECTION_ERROR_RE.search(text):
                result["has_collection_error"] = True
        for suite in root.iter("testsuite"):
            cases = list(suite.iter("testcase"))
            observed = {
                "tests": len(cases),
                "failures": sum(bool(case.findall("failure")) for case in cases),
                "errors": sum(bool(case.findall("error")) for case in cases),
                "skipped": sum(bool(case.findall("skipped")) for case in cases),
            }
            for key, count in observed.items():
                declared = suite.get(key)
                if declared is not None and int(declared) != count:
                    raise ValueError("JUnit total disagrees with testcase elements")
        result["testcases"] = result["tests"]
        result["passed"] = max(
            0, result["tests"] - result["failures"] - result["errors"] - result["skipped"]
        )
    except (ET.ParseError, TypeError, ValueError):
        result["unparseable"] = True
    return {**result, "observed": observed_ids}


def decide_pytest_outcome(counts):
    """Score only a complete observed suite; errors/skips take precedence over failures."""
    if counts.get("unparseable"):
        return ("unscored", None, "missing_report")
    if counts.get("tests", 0) <= 0:
        return ("unscored", None, "zero_tests")
    if counts.get("has_collection_error"):
        return ("unscored", None, "collection_error")
    if counts.get("errors", 0) > 0:
        return ("unscored", None, "test_errors")
    if counts.get("skipped", 0) > 0:
        return ("unscored", None, "incomplete_skipped")
    if counts.get("failures", 0) > 0:
        return ("failed", 0.0, "fail_observed")
    if counts.get("passed", 0) > 0:
        return ("passed", 1.0, "pass_observed")
    return ("unscored", None, "unknown")


def _run_unittest_child(config_path, report_path):
    """Bootstrap and collect in the SAME interpreter, observing TestResult directly."""
    counts = {
        "tests": 0, "failures": 0, "errors": 0, "skipped": 0, "passed": 0,
        "has_collection_error": False, "unparseable": False,
    }
    observed_ids = []
    details = {}
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        sys.path.insert(0, os.getcwd())
        bootstrap = config.get("bootstrap")
        if bootstrap is not None:
            path = resolve_within(os.path.dirname(os.path.abspath(config_path)), bootstrap)
            if path is None:
                raise ValueError("bootstrap escapes config directory")
            runpy.run_path(path, run_name="__heldout_bootstrap__")
        labels = [
            node_id_to_unittest_label(entry["node_id"])
            for entry in config["suite"]["tests"]
        ]
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(labels)
        class ObservedResult(unittest.TestResult):
            def startTest(self, test):
                observed_ids.append(test.id())
                super().startTest(test)

        observed = ObservedResult()
        observed.startTestRun()
        try:
            suite.run(observed)
        finally:
            observed.stopTestRun()
        for records in (observed.failures, observed.errors):
            for _, traceback_text in records:
                sys.stderr.write(traceback_text)
        counts.update({
            "tests": observed.testsRun, "failures": len(observed.failures),
            "errors": len(observed.errors), "skipped": len(observed.skipped),
            "has_collection_error": bool(loader.errors),
        })
        counts["passed"] = max(
            0, counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
        )
    except BaseException as exc:
        counts["errors"] += 1
        counts["has_collection_error"] = True
        details["detail"] = str(exc)[:ERROR_TAIL_CHARS]
    _write_json(report_path, {**counts, **details, "observed": observed_ids})
    outcome, unused, reason = decide_pytest_outcome(counts)
    return 0 if outcome == "passed" else 1 if outcome == "failed" else 2


def _read_unittest_report(path):
    with open(path, encoding="utf-8") as handle:
        counts = json.load(handle)
    if not isinstance(counts, dict):
        raise ValueError("missing unittest result")
    for key in ("tests", "failures", "errors", "skipped", "passed"):
        if isinstance(counts.get(key), bool) or not isinstance(counts.get(key), int):
            raise ValueError("invalid unittest counts")
        if counts[key] < 0:
            raise ValueError("negative unittest counts")
    if not isinstance(counts.get("observed"), list) or not all(
        isinstance(identifier, str) for identifier in counts["observed"]
    ):
        raise ValueError("missing observed unittest identities")
    return counts


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_bounded_tail(path, text):
    """Write at most ``MAX_LOG_BYTES`` (tail) of ``text`` with a marker."""
    encoded = text.encode("utf-8", "replace")
    with open(path, "wb") as handle:
        if len(encoded) > MAX_LOG_BYTES:
            handle.write(
                "[... truncated: showing last {} of {} bytes ...]\n".format(
                    MAX_LOG_BYTES, len(encoded)
                ).encode("ascii")
            )
            handle.write(encoded[-MAX_LOG_BYTES:])
        else:
            handle.write(encoded)


def _clear_stale_rewards(logs_dir):
    """Remove stale reward artifacts so unscored attempts inherit nothing."""
    for name in ("reward.json", "reward.txt"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(logs_dir, name))


def run_verification(config_path, logs_dir):
    """Run one holdout verification; return a process exit code.

    0 means scored (``reward.json`` written, outcome passed/failed);
    nonzero means unscored (result written, no reward file) or CLI misuse.
    ``heldout-result.json`` uses ``heldout-result/v1`` with
    ``outcome`` = ``passed``|``failed``|``unscored``,
    ``holdout_pass`` = 1.0|0.0|null,
    ``counts`` = {tests, failures, errors, skipped}, ``reason``, plus
    provenance and log-file fields the parent uses to validate the reward.
    """
    try:
        problem = _ensure_dir(logs_dir)
    except Exception as exc:
        sys.stderr.write("heldout verifier: cannot create logs dir: {}\n".format(exc))
        return 1
    if problem is not None:
        sys.stderr.write("heldout verifier: {}\n".format(problem))
        return 1
    logs_abs = os.path.abspath(logs_dir)

    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "verifier": HELDOUT_VERIFIER_VERSION,
        "outcome": "unscored",
        "holdout_pass": None,
        "reason": None,
        "detail": None,
        "task_name": None,
        "base_commit": None,
        "fix_commit": None,
        "framework": None,
        "command": None,
        "timeout_sec": None,
        "tests_selected": 0,
        "diff_targets": 0,
        "passed": 0,
        "counts": {"tests": 0, "failures": 0, "errors": 0, "skipped": 0},
        "agent_patch": None,
        "bootstrap": None,
        "reward_written": False,
        "log_files": [],
    }

    def finish(code, outcome, holdout, reason, detail):
        result["outcome"] = outcome
        result["holdout_pass"] = holdout
        result["reason"] = reason
        result["detail"] = detail
        try:
            if outcome in ("passed", "failed") and holdout is not None:
                _write_json(
                    os.path.join(logs_abs, "reward.json"), {"holdout_pass": holdout}
                )
                result["reward_written"] = True
            else:
                _clear_stale_rewards(logs_abs)
                result["reward_written"] = False
            _write_json(os.path.join(logs_abs, "heldout-result.json"), result)
        except (OSError, ValueError, TypeError) as exc:
            sys.stderr.write(
                "heldout verifier: cannot write result: {}\n".format(exc)
            )
            return 2
        sys.stdout.write(
            "heldout outcome={} holdout_pass={} reason={}\n".format(
                outcome, holdout, reason
            )
        )
        return code

    try:
        _clear_stale_rewards(logs_abs)
        try:
            with open(config_path, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError) as exc:
            return finish(2, "unscored", None, "invalid_config", "cannot load config: {}".format(exc))
        config_dir = os.path.dirname(os.path.abspath(config_path))
        config_errors = validate_config(config)
        if config_errors:
            first = config_errors[0]
            code = _reason_of(first)
            if code == "schema":
                code = "invalid_config"
            return finish(2, "unscored", None, code, "; ".join(config_errors[:5]))
        suite = config["suite"]
        if suite.get("status") != "ready":
            result["task_name"] = suite.get("task_name")
            return finish(
                2,
                "unscored",
                None,
                "suite_not_ready",
                "suite status is {!r}".format(suite.get("status")),
            )
        suite_errors = validate_suite(suite)
        if suite_errors:
            first = suite_errors[0]
            code = _reason_of(first)
            if code == "schema":
                code = "invalid_suite"
            result["task_name"] = suite.get("task_name")
            return finish(2, "unscored", None, code, "; ".join(suite_errors[:5]))
        task_name = suite["task_name"]
        base_commit = suite["base_commit"]
        workdir = suite["workdir"]
        framework = config["framework"]
        command = list(config["command"])
        timeout_sec = float(config["timeout_sec"])
        result["task_name"] = task_name
        result["base_commit"] = base_commit
        result["fix_commit"] = suite.get("fix_commit")
        result["framework"] = framework
        result["command"] = command
        result["timeout_sec"] = timeout_sec
        result["tests_selected"] = len(suite["tests"])
        patch_rel = config["agent_patch"]["path"]
        patch_expect = config["agent_patch"]["sha256"]
        result["agent_patch"] = {"path": patch_rel, "sha256": patch_expect}
        patch_abs = resolve_within(config_dir, patch_rel)
        if patch_abs is None:
            return finish(
                2, "unscored", None, "unsafe_path", "agent patch escapes config dir"
            )
        if not os.path.isfile(patch_abs):
            return finish(
                2, "unscored", None, "missing_patch", "agent diff not found"
            )
        try:
            patch_actual = sha256_hex_of_file(patch_abs)
        except OSError as exc:
            return finish(
                2, "unscored", None, "missing_patch", "cannot hash agent diff: {}".format(exc)
            )
        if patch_actual != patch_expect:
            return finish(
                2,
                "unscored",
                None,
                "hash_mismatch",
                "agent diff digest changed after capture",
            )
        bootstrap_rel = config.get("bootstrap")
        bootstrap_abs = None
        if bootstrap_rel is not None:
            result["bootstrap"] = bootstrap_rel
            bootstrap_abs = resolve_within(config_dir, bootstrap_rel)
            if bootstrap_abs is None or not os.path.isfile(bootstrap_abs):
                return finish(
                    2, "unscored", None, "bootstrap_error", "bootstrap file unavailable"
                )
        if os.path.getsize(patch_abs) == 0:
            diff_problems, diff_targets = [], 0
        else:
            diff_problems, diff_targets = scan_agent_diff(patch_abs)
        result["diff_targets"] = diff_targets
        if diff_problems:
            first = diff_problems[0]
            return finish(2, "unscored", None, _reason_of(first), "; ".join(diff_problems[:3]))
        problem = restore_base(workdir, base_commit)
        if problem is not None:
            return finish(2, "unscored", None, _reason_of(problem), problem)
        if os.path.getsize(patch_abs) > 0:
            problem = apply_agent_diff(workdir, patch_abs)
            if problem is not None:
                return finish(2, "unscored", None, _reason_of(problem), problem)
        problem = overlay_suite_files(workdir, suite["files"])
        if problem is not None:
            return finish(2, "unscored", None, _reason_of(problem), problem)
        child_env = _child_env(config.get("env"))
        if framework == "unittest":
            report_path = os.path.join(logs_abs, "unittest-result.json")
            test_argv = [
                command[0], os.path.abspath(__file__), "--unittest-child",
                os.path.abspath(config_path), report_path,
            ]
        else:
            report_path = os.path.join(logs_abs, "pytest-junit.xml")
            node_ids = [entry["node_id"] for entry in suite["tests"]]
            test_argv = list(command) + node_ids + ["--junitxml", report_path]
        with contextlib.suppress(FileNotFoundError):
            os.unlink(report_path)
        run = run_command(test_argv, workdir, child_env, timeout_sec)
        _write_bounded_tail(os.path.join(logs_abs, "test-stdout.txt"), run["stdout"])
        _write_bounded_tail(os.path.join(logs_abs, "test-stderr.txt"), run["stderr"])
        result["log_files"] = ["test-stdout.txt", "test-stderr.txt"]
        if run["timed_out"]:
            return finish(2, "unscored", None, "timeout", "test command timed out")
        if run["error"] is not None:
            return finish(2, "unscored", None, "infra_error", run["error"])
        try:
            if framework == "unittest":
                counts = _read_unittest_report(report_path)
            else:
                with open(report_path, "rb") as handle:
                    data = handle.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("test report exceeds 8 MiB")
                counts = parse_junit_xml(data)
        except (OSError, ValueError):
            return finish(2, "unscored", None, "missing_report", "no valid test result")
        result["log_files"].append(os.path.basename(report_path))
        result["counts"] = {
            key: counts[key] for key in ("tests", "failures", "errors", "skipped")
        }
        result["passed"] = counts["passed"]
        outcome, holdout, reason = decide_pytest_outcome(counts)
        if outcome == "unscored":
            return finish(2, outcome, None, reason, reason)
        observed = counts.get("observed", [])
        for selected in suite["tests"]:
            expected = selected["path"][:-3].replace("/", ".") + "." + selected["qualname"]
            if not any(
                identity == expected or identity.endswith("." + expected)
                for identity in observed
            ):
                return finish(
                    2, "unscored", None, "incomplete_selection",
                    "selected test did not execute: " + selected["node_id"],
                )
        expected_exit = 0 if outcome == "passed" else 1
        if run["returncode"] != expected_exit:
            return finish(
                2, "unscored", None, "inconsistent_exit", "test exit disagrees with report"
            )
        return finish(0, outcome, holdout, reason, reason)
    except Exception as exc:
        try:
            return finish(
                2, "unscored", None, "infra_error", "verifier crashed: {!r}".format(exc)
            )
        except Exception:
            sys.stderr.write("heldout verifier: result write failed\n")
            return 2


def main(argv=None):
    """CLI entry point: ``heldout_verifier.py --config PATH --logs DIR``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--unittest-child":
        if len(argv) != 3:
            return 2
        return _run_unittest_child(argv[1], argv[2])
    parser = argparse.ArgumentParser(
        description="Offline HAR-197 holdout verifier payload (stdlib only)."
    )
    parser.add_argument("--config", required=True, help="heldout-run.json path")
    parser.add_argument("--logs", required=True, help="logs directory for outputs")
    args = parser.parse_args(argv)
    return run_verification(args.config, args.logs)


if __name__ == "__main__":
    sys.exit(main())
