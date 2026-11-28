from __future__ import annotations

import re

from ._forensics_shell_cmds import (
    is_curl_mutating,
    is_git_read_only,
    is_less_read_only,
    is_printf_read_only,
    is_rg_read_only,
    is_sed_inplace,
    is_sort_read_only,
    is_uniq_read_only,
)
from ._forensics_shell_lex import (
    FILE_TOKEN_RE,
    has_unquoted_command_sub,
    split_shell_tokens,
    strip_quotes_linear,
)
from ._forensics_shell_split import (
    split_shell_segments,
    strip_heredocs,
    strip_heredocs_checked,
    strip_unquoted_redirects,
)

__all__ = [
    "extract_bash_read_paths",
    "has_unquoted_command_sub",
    "is_mutating_shell_cmd",
    "split_shell_segments",
    "strip_heredocs",
    "strip_quotes_linear",
]

_DEVNULL_RE = re.compile(r"(?:[12&]?)>(?:>|\|)?[ \t]*/dev/null(?=$|[ \t\n;|&)])")
_REDIR_OUT_RE = re.compile(r">(?!&(?:[0-9]|-)(?=$|[ \t\n;|&)]))")
_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_UNQUOTED_VAR_RE = re.compile(r"\$(?:[A-Za-z0-9_@#?*!$-]|\{)")
_GET_FN_PREFIX_RE = re.compile(r"^get\(\)[ \t\n]*\{[ \t\n]*")

_TRUSTED_BIN_DIRS = frozenset({"/bin", "/usr/bin", "/usr/local/bin"})
_BENIGN_ENV_VARS = frozenset(
    {
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_MESSAGES",
        "LANG",
        "TZ",
        "TERM",
        "NO_COLOR",
        "CLICOLOR",
        "COLUMNS",
        "LINES",
    }
)
_READ_ONLY_CMDS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "pwd",
        "echo",
        "sleep",
        "jq",
        "wc",
        "cut",
        "tr",
        "true",
        ":",
        "nl",
        "more",
    }
)
_READ_CMDS = frozenset({"cat", "head", "tail", "sed", "less", "more", "grep", "rg", "nl"})
_PATTERN_READ_CMDS = frozenset({"grep", "rg", "sed"})
_PATTERN_LONG_FLAGS = frozenset({"--regexp", "--file", "--expression"})
_PATTERN_LONG_PREFIXES = ("--regexp=", "--file=", "--expression=")
_ENV_VAL_LONG = frozenset({"--unset", "--chdir", "--argv0"})
_ENV_VAL_PREFIXES = ("--unset=", "--chdir=", "--argv0=")
_ENV_BOOL_LONG = frozenset({"--ignore-environment", "--null", "--debug"})
_ENV_BOOL_SHORT = frozenset("i0v")
_ENV_VAL_SHORT = frozenset("uCaP")
_SUDO_VAL_LONG = frozenset({"--user", "--group"})
_SUDO_VAL_SHORT = frozenset("ugCphrtU")
_TIMEOUT_VAL_LONG = frozenset({"--kill-after", "--signal"})
_TIMEOUT_VAL_SHORT = frozenset("ks")
_GREP_SHARED_VALS = frozenset(
    {
        "-e",
        "-f",
        "-m",
        "-A",
        "-B",
        "-C",
        "--max-count",
        "--regexp",
        "--file",
        "--context",
        "--after-context",
        "--before-context",
    }
)
_CMD_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "nl": frozenset({"-n", "-b", "-d", "-i", "-l", "-s", "-v", "-w"}),
    "grep": _GREP_SHARED_VALS | {"-d", "--include", "--exclude"},
    "rg": _GREP_SHARED_VALS | {"-g", "-t", "-T", "--glob", "--type", "--type-not"},
    "sed": frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"}),
    "cat": frozenset(),
    "less": frozenset(),
    "more": frozenset(),
}


def _trusted_cmd_base(tok: str) -> str | None:
    if not tok or "$" in tok:
        return None
    parent, sep, base = tok.rpartition("/")
    if not base:
        return None
    if sep and parent not in _TRUSTED_BIN_DIRS:
        return None
    return base


def _skip_wrapper_opts(
    tokens: list[str],
    idx: int,
    long_vals: frozenset[str],
    short_vals: frozenset[str],
) -> int:
    n = len(tokens)
    while idx < n and tokens[idx].startswith("-"):
        t = tokens[idx]
        if t == "--":
            return idx + 1
        if t in long_vals:
            idx += 2
            continue
        if t.startswith("-") and not t.startswith("--"):
            flags = t[1:]
            step = 1
            for c_idx, ch in enumerate(flags):
                if ch in short_vals:
                    step = 2 if c_idx + 1 == len(flags) else 1
                    break
            idx += step
            continue
        idx += 1
    return idx


def _step_env_opts(tokens: list[str], idx: int, *, allow_sudo_timeout: bool) -> int | None:
    n = len(tokens)
    while idx < n and tokens[idx].startswith("-"):
        t = tokens[idx]
        if t == "--":
            return idx + 1
        if t == "-":
            idx += 1
            continue
        if t in ("-S", "--split-string") or t.startswith("--split-string="):
            if not allow_sudo_timeout:
                return None
            idx += 2 if t in ("-S", "--split-string") else 1
            continue
        if not allow_sudo_timeout and (
            t in ("--chdir", "--argv0") or t.startswith(("--chdir=", "--argv0="))
        ):
            return None
        if t in _ENV_VAL_LONG:
            idx += 2
            continue
        if t in _ENV_BOOL_LONG or t.startswith(_ENV_VAL_PREFIXES):
            idx += 1
            continue
        if not t.startswith("--"):
            flags = t[1:]
            step = 1
            for c_idx, ch in enumerate(flags):
                at_end = c_idx + 1 == len(flags)
                if ch in ("S", "P", "C", "a") and not allow_sudo_timeout:
                    return None
                if ch == "S" or ch in _ENV_VAL_SHORT:
                    step = 2 if at_end else 1
                    break
                if ch not in _ENV_BOOL_SHORT and not allow_sudo_timeout:
                    return None
            idx += step
            continue
        if not allow_sudo_timeout:
            return None
        idx += 1
    return idx


def _unwrap_cmd_tokens(tokens: list[str], *, allow_sudo_timeout: bool) -> list[str] | None:
    idx = 0
    n = len(tokens)
    saw_env = False
    saw_assign = False
    while idx < n:
        tok = tokens[idx]
        if _VAR_ASSIGN_RE.match(tok):
            if not allow_sudo_timeout and tok.split("=", 1)[0] not in _BENIGN_ENV_VARS:
                return None
            saw_assign = True
            idx += 1
            continue
        base = _trusted_cmd_base(tok)
        if base == "env":
            saw_env = True
            next_idx = _step_env_opts(tokens, idx + 1, allow_sudo_timeout=allow_sudo_timeout)
            if next_idx is None:
                return None
            idx = next_idx
            continue
        if allow_sudo_timeout and base == "sudo":
            idx = _skip_wrapper_opts(tokens, idx + 1, _SUDO_VAL_LONG, _SUDO_VAL_SHORT)
            continue
        if allow_sudo_timeout and base == "timeout":
            idx = _skip_wrapper_opts(tokens, idx + 1, _TIMEOUT_VAL_LONG, _TIMEOUT_VAL_SHORT)
            if idx < n:
                idx += 1
            continue
        break
    rest = tokens[idx:]
    if not allow_sudo_timeout and not rest and saw_assign and not saw_env:
        return None
    return rest


def _is_read_only_atom(atom: str) -> bool:
    s = atom.strip(" \t\n")
    if not s or s == "}":
        return True
    s = _GET_FN_PREFIX_RE.sub("", s).strip(" \t\n")
    if not s or s == "}":
        return True
    if has_unquoted_command_sub(s):
        return False
    unquoted = strip_quotes_linear(s)
    if _UNQUOTED_VAR_RE.search(unquoted):
        return False
    if _REDIR_OUT_RE.search(_DEVNULL_RE.sub("", unquoted)):
        return False
    cleaned, _, _ = strip_unquoted_redirects(s)
    rest = _unwrap_cmd_tokens(split_shell_tokens(cleaned), allow_sudo_timeout=False)
    if rest is None:
        return False
    if not rest:
        return True
    cmd_name = _trusted_cmd_base(rest[0])
    if cmd_name is None:
        return False
    args = rest[1:]
    if cmd_name == "curl":
        return not is_curl_mutating(args)
    if cmd_name == "sed":
        return not is_sed_inplace(args, check_scripts=True)
    if cmd_name == "git":
        return is_git_read_only(args)
    if cmd_name == "sort":
        return is_sort_read_only(args)
    if cmd_name == "uniq":
        return is_uniq_read_only(args)
    if cmd_name == "rg":
        return is_rg_read_only(args)
    if cmd_name == "less":
        return is_less_read_only(args)
    if cmd_name == "printf":
        return is_printf_read_only(args)
    if cmd_name == "get":
        return len(args) == 1 and args[0].startswith("/")
    return cmd_name in _READ_ONLY_CMDS


def is_mutating_shell_cmd(cmd: str) -> bool:
    if not cmd or not cmd.strip(" \t\r\n"):
        return False
    stripped, heredoc_mutates = strip_heredocs_checked(cmd)
    if heredoc_mutates:
        return True
    segs = [seg for seg in split_shell_segments(stripped) if seg.strip(" \t\r\n")]
    if not segs:
        return False
    for seg in segs:
        atoms = [a for a in split_shell_segments(seg, split_pipes=True) if a.strip(" \t\r\n")]
        if not atoms:
            continue
        if not all(_is_read_only_atom(atom) for atom in atoms):
            return True
    return False


def _extract_seg_read_paths(
    cmd_name: str,
    args: list[str],
    redirected: set[str],
    in_redirected: list[str],
) -> list[str]:
    val_flags = _CMD_VALUE_FLAGS.get(cmd_name, frozenset())
    needs_pattern = cmd_name in _PATTERN_READ_CMDS
    has_pattern = False
    skip_mode = ""
    end_opts = False
    found: list[str] = []
    for arg in args:
        if skip_mode:
            mode, skip_mode = skip_mode, ""
            if mode == "any" or arg.isdigit():
                continue
        if not end_opts:
            if arg == "--":
                end_opts = True
                continue
            if arg.startswith("--"):
                if arg in _PATTERN_LONG_FLAGS or arg.startswith(_PATTERN_LONG_PREFIXES):
                    has_pattern = True
                if arg in val_flags:
                    skip_mode = "digits" if cmd_name == "sed" and arg == "--line-length" else "any"
                continue
            if arg.startswith("-") and arg != "-":
                flags = arg[1:]
                for idx, ch in enumerate(flags):
                    flag = f"-{ch}"
                    if needs_pattern and flag in ("-e", "-f"):
                        has_pattern = True
                    if flag in val_flags:
                        rest = flags[idx + 1 :]
                        if cmd_name == "sed" and ch == "l":
                            if not rest:
                                skip_mode = "digits"
                                break
                            if rest.isdigit():
                                break
                            continue
                        if not rest:
                            skip_mode = "any"
                        break
                continue
        if needs_pattern and not has_pattern:
            has_pattern = True
            continue
        cand = arg.strip("'\"\r")
        if FILE_TOKEN_RE.match(cand) and cand not in redirected:
            found.append(cand)
    found.extend(in_p for in_p in in_redirected if in_p not in redirected)
    return found


def extract_bash_read_paths(cmd: str) -> list[str]:
    paths: list[str] = []
    for seg in split_shell_segments(strip_heredocs(cmd), split_pipes=True):
        if not seg.strip():
            continue
        cleaned, redirected, in_redirected = strip_unquoted_redirects(seg)
        rest = _unwrap_cmd_tokens(split_shell_tokens(cleaned), allow_sudo_timeout=True)
        if not rest:
            continue
        cmd_name = _trusted_cmd_base(rest[0])
        if cmd_name is None or cmd_name not in _READ_CMDS:
            continue
        args = rest[1:]
        if cmd_name == "sed" and is_sed_inplace(args):
            continue
        paths.extend(_extract_seg_read_paths(cmd_name, args, redirected, in_redirected))
    return paths
