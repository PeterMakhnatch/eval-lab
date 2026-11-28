from __future__ import annotations

from dataclasses import dataclass


def _fset(spec: str) -> frozenset[str]:
    return frozenset(spec.split())


def _tup(spec: str) -> tuple[str, ...]:
    return tuple(spec.split())


SAFE_OUT_TARGETS = frozenset({"-", "/dev/null"})
_READ_ONLY_GIT_SUBS = _fset("status log diff show rev-parse ls-files blame grep")
_GIT_SAFE_VAL_FLAGS = _fset("-C --git-dir --work-tree --namespace --super-prefix --attr-source")
_GIT_SAFE_VAL_PREFIXES = tuple(f"{f}=" for f in _GIT_SAFE_VAL_FLAGS if f.startswith("--"))
_GIT_SAFE_BOOL_FLAGS = _fset(
    "-p --paginate -P --no-pager --no-replace-objects --bare --literal-pathspecs "
    "--glob-pathspecs --noglob-pathspecs --icase-pathspecs --no-optional-locks "
    "--no-lazy-fetch -v --version -h --help"
)
_SAFE_GIT_CONFIG_KEYS = frozenset({"safe.directory", "core.quotepath", "core.autocrlf"})
_GIT_SUB_VAL_LONG = _fset(
    "--regexp --file --max-count --max-depth --threads --after-context --before-context "
    "--context --grep --author --committer --since --after --until --before --format "
    "--pretty --date --decorate-refs --decorate-refs-exclude --branches --tags --remotes "
    "--glob --exclude --skip --max-parents --min-parents --unified --inter-hunk-context "
    "--word-diff-regex --diff-filter --find-renames --find-copies --abbrev --src-prefix "
    "--dst-prefix --line-prefix --path-format --prefix --default --exclude-from "
    "--exclude-per-directory"
)
_GIT_GREP_VAL_SHORT = frozenset("efmABC")
_GIT_SUB_VAL_SHORT = frozenset("SGLUn")
_RG_VAL_SHORT = frozenset("efEmjgdtTABCMr")
_RG_VAL_LONG = _fset(
    "--regexp --file --pre-glob --dfa-size-limit --encoding --engine --max-count "
    "--regex-size-limit --threads --glob --iglob --ignore-file --max-depth --max-filesize "
    "--type --type-not --type-add --type-clear --after-context --before-context --color "
    "--colors --context --context-separator --field-context-separator --field-match-separator "
    "--hyperlink-format --max-columns --path-separator --replace --sort --sortr --generate"
)
_LESS_VAL_SHORT = frozenset('bDhjkpPtTxyz"#')
_LESS_VAL_LONG = _fset(
    "--buffers --color --max-back-scroll --jump-target --lesskey-file --lesskey-src "
    "--lesskey-context --pattern --prompt --tag --tag-file --tabs --max-forw-scroll --window "
    "--quotes --shift --header --intr --line-num-width --match-shift --modelines --rscroll "
    "--search-options --status-col-width --wordwrap"
)
_SORT_VAL_LONG = _fset(
    "--field-separator --key --temporary-directory --buffer-size --files0-from "
    "--random-source --batch-size --parallel"
)
_SORT_VAL_SHORT = frozenset("tkTS")
_UNIQ_VAL_LONG = _fset("--skip-fields --skip-chars --check-chars")
_UNIQ_VAL_SHORT = frozenset("fsw")
_SED_BENIGN_LONG = _tup(
    "--quiet --silent --regexp-extended --extended-regexp --posix "
    "--unbuffered --null-data --separate --sandbox --debug"
)
_CURL_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CURL_DATA_LONG = _fset("--data --data-raw --data-binary --data-urlencode --data-ascii --json")
_CURL_DATA_LONG_PREFIXES = tuple(f"{flag}=" for flag in _CURL_DATA_LONG)
_CURL_NON_GET_BODY_LONG = _fset("--form --form-string --upload-file")
_CURL_NON_GET_BODY_PREFIXES = tuple(f"{flag}=" for flag in _CURL_NON_GET_BODY_LONG)
_CURL_SAFEABLE_OUT_LONG = _fset(
    "--output --dump-header --cookie-jar --trace --trace-ascii --stderr --libcurl"
)
_CURL_SAFEABLE_OUT_PREFIXES = tuple(f"{flag}=" for flag in _CURL_SAFEABLE_OUT_LONG)
_CURL_ALWAYS_MUT_LONG = _fset("--etag-save --config --hsts --alt-svc --output-dir")
_CURL_ALWAYS_MUT_PREFIXES = _tup(
    "--etag-save= --config= --hsts= --alt-svc= --output-dir= "
    "--remote-name --remote-header-name --create-dirs"
)
_CURL_BENIGN_VAL_LONG = _fset(
    "--header --user --user-agent --referer --cookie --proxy --proxy-user --noproxy "
    "--connect-timeout --max-time --resolve --cacert --cert --key --retry --retry-delay "
    "--retry-max-time --max-redirs --interface --unix-socket --oauth2-bearer --url"
)
_CURL_BENIGN_VAL_SHORT = frozenset("HuAbemxryYzUECPQt")


def _skip_sed_regex(script: str, i: int, delim: str) -> int:
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == delim:
            return i + 1
        if ch == "[":
            i += 1
            if i < n and script[i] == "^":
                i += 1
            if i < n and script[i] == "]":
                i += 1
            while i < n and script[i] != "]":
                if script[i] == "[" and i + 1 < n and script[i + 1] in (":", ".", "="):
                    tag = script[i + 1]
                    i += 2
                    while i + 1 < n and not (script[i] == tag and script[i + 1] == "]"):
                        i += 1
                    i = i + 2 if i + 1 < n else n
                    continue
                i += 1
            if i < n:
                i += 1
            continue
        i += 1
    return n


def _skip_sed_delim(script: str, i: int, delim: str) -> int:
    n = len(script)
    while i < n and script[i] != delim:
        i += 2 if script[i] == "\\" else 1
    return i + 1 if i < n else n


def _sed_script_mutates(script: str) -> bool:
    if "${" in script:
        return True
    i, n = 0, len(script)
    while i < n:
        while i < n and script[i] in " \t\r\n;{}!":
            i += 1
        if i >= n:
            break
        if script[i] == "#":
            while i < n and script[i] != "\n":
                i += 1
            continue
        while i < n and script[i] in "0123456789$,+~ \t/\\":
            if script[i : i + 3] == "$((":
                end = script.find("))", i + 3)
                i = end + 2 if end != -1 else n
                continue
            if script[i] == "/" or (script[i] == "\\" and i + 1 < n):
                delim = script[i + 1] if script[i] == "\\" else "/"
                start = i + 2 if script[i] == "\\" else i + 1
                i = _skip_sed_regex(script, start, delim)
                while i < n and script[i] in "IM":
                    i += 1
                continue
            i += 1
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            break
        cmd = script[i]
        if cmd == "{":
            i += 1
        elif cmd in ("w", "W", "e"):
            return True
        elif cmd == "s" and i + 1 < n:
            delim = script[i + 1]
            i = _skip_sed_delim(script, _skip_sed_regex(script, i + 2, delim), delim)
            while i < n and script[i] not in ";\n}":
                if script[i] in ("w", "W", "e"):
                    return True
                i += 1
        elif cmd == "y" and i + 1 < n:
            delim = script[i + 1]
            i = _skip_sed_delim(script, _skip_sed_delim(script, i + 2, delim), delim)
        elif cmd in ("a", "i", "c"):
            while i < n and script[i] != "\n":
                i += 2 if script[i] == "\\" else 1
        elif cmd in (":", "b", "t", "T", "r", "R", "v"):
            while i < n and script[i] not in ";\n}":
                i += 2 if script[i] == "\\" else 1
        else:
            i += 1
    return False


def _parse_sed_long_opt(arg: str, scripts: list[str], *, check_scripts: bool) -> tuple[str, bool]:
    opt_name, has_eq, opt_val = arg.partition("=")
    if "--in-place".startswith(opt_name):
        return "mut", False
    if "--expression".startswith(opt_name):
        if has_eq:
            scripts.append(opt_val)
            return "ok", True
        return "expr", True
    if opt_name == "--f" or "--file".startswith(opt_name):
        if check_scripts:
            return "mut", True
        return ("ok" if has_eq else "file"), True
    if "--line-length".startswith(opt_name):
        if has_eq:
            return ("mut" if check_scripts and not opt_val.isdigit() else "ok"), False
        return "line_len", False
    if not has_eq and any(b.startswith(opt_name) for b in _SED_BENIGN_LONG):
        return "ok", False
    return ("mut" if check_scripts else "ok"), False


def _parse_sed_short_opts(
    flags: str, scripts: list[str], *, check_scripts: bool
) -> tuple[str, bool]:
    for idx, ch in enumerate(flags):
        rest = flags[idx + 1 :]
        if ch == "e":
            if not rest:
                return "expr", True
            scripts.append(rest)
            return "ok", True
        if ch == "f":
            if check_scripts:
                return "mut", True
            return ("file" if not rest else "ok"), True
        if ch == "l":
            if not rest:
                return "line_len", False
            if rest.isdigit():
                return "ok", False
            continue
        if ch in ("i", "I"):
            return "mut", False
    return "ok", False


def is_sed_inplace(args: list[str], *, check_scripts: bool = False) -> bool:
    state = "ok"
    scripts: list[str] = []
    has_explicit_script = False
    end_opts = False
    for arg in args:
        if state == "expr":
            state = "ok"
            scripts.append(arg)
            continue
        if state == "file":
            state = "ok"
            if check_scripts:
                return True
            continue
        if state == "line_len":
            state = "ok"
            if arg.isdigit():
                continue
        if not end_opts:
            if arg == "--":
                end_opts = True
                continue
            if arg.startswith("--"):
                state, saw_s = _parse_sed_long_opt(arg, scripts, check_scripts=check_scripts)
                has_explicit_script = has_explicit_script or saw_s
                if state == "mut":
                    return True
                continue
            if arg.startswith("-") and arg != "-":
                state, saw_s = _parse_sed_short_opts(arg[1:], scripts, check_scripts=check_scripts)
                has_explicit_script = has_explicit_script or saw_s
                if state == "mut":
                    return True
                continue
        if not has_explicit_script:
            scripts.append(arg)
            has_explicit_script = True
    return check_scripts and _sed_script_mutates("\n".join(scripts))


@dataclass(slots=True)
class _CurlSegState:
    has_write_method: bool = False
    has_file_out: bool = False
    has_data_body: bool = False
    has_non_get_body: bool = False
    force_get: bool = False


def _curl_seg_mutates(st: _CurlSegState) -> bool:
    return (
        st.has_file_out
        or st.has_write_method
        or st.has_non_get_body
        or (st.has_data_body and not st.force_get)
    )


def _apply_curl_long_opt(st: _CurlSegState, arg: str) -> str:
    low = arg.lower()
    if low == "--request":
        return "method"
    if low.startswith("--request="):
        if arg.split("=", 1)[1].strip("'\"").upper() not in _CURL_SAFE_METHODS:
            st.has_write_method = True
    elif low == "--get":
        st.force_get = True
    elif low == "--write-out":
        return "write_out"
    elif low.startswith("--write-out="):
        if "%output{" in low.split("=", 1)[1]:
            st.has_file_out = True
    elif low in _CURL_SAFEABLE_OUT_LONG:
        return "out"
    elif low.startswith(_CURL_SAFEABLE_OUT_PREFIXES):
        if arg.split("=", 1)[1].strip("'\"") not in SAFE_OUT_TARGETS:
            st.has_file_out = True
    elif low in _CURL_DATA_LONG:
        st.has_data_body = True
        return "val"
    elif low.startswith(_CURL_DATA_LONG_PREFIXES):
        st.has_data_body = True
    elif low in _CURL_NON_GET_BODY_LONG:
        st.has_non_get_body = True
        return "val"
    elif low.startswith(_CURL_NON_GET_BODY_PREFIXES):
        st.has_non_get_body = True
    elif low in _CURL_ALWAYS_MUT_LONG:
        st.has_file_out = True
        return "val"
    elif low.startswith(_CURL_ALWAYS_MUT_PREFIXES) or low == "--remote-name-all":
        st.has_file_out = True
    elif low in _CURL_BENIGN_VAL_LONG:
        return "val"
    return ""


def _apply_curl_short_opts(st: _CurlSegState, flags: str) -> str:
    for idx, ch in enumerate(flags):
        rest = flags[idx + 1 :]
        if ch in ("o", "D", "c"):
            if not rest:
                return "out"
            if rest.strip("'\"") not in SAFE_OUT_TARGETS:
                st.has_file_out = True
            return ""
        if ch in ("O", "J", "K"):
            st.has_file_out = True
            if ch == "K":
                return "val" if not rest else ""
        elif ch == "G":
            st.force_get = True
        elif ch == "d":
            st.has_data_body = True
            return "val" if not rest else ""
        elif ch in ("F", "T"):
            st.has_non_get_body = True
            return "val" if not rest else ""
        elif ch == "X":
            if not rest:
                return "method"
            if rest.strip("'\"").upper() not in _CURL_SAFE_METHODS:
                st.has_write_method = True
            return ""
        elif ch == "w":
            if not rest:
                return "write_out"
            if "%output{" in rest.lower():
                st.has_file_out = True
            return ""
        elif ch in _CURL_BENIGN_VAL_SHORT:
            return "val" if not rest else ""
    return ""


def is_curl_mutating(args: list[str]) -> bool:
    st = _CurlSegState()
    expect = ""
    end_opts = False
    for arg in args:
        if expect:
            cur, expect = expect, ""
            if cur == "method" and arg.strip("'\"").upper() not in _CURL_SAFE_METHODS:
                st.has_write_method = True
            elif cur == "out" and arg.strip("'\"") not in SAFE_OUT_TARGETS:
                st.has_file_out = True
            elif cur == "write_out" and "%output{" in arg.lower():
                st.has_file_out = True
            continue
        if not end_opts and arg in ("--next", "-:"):
            if _curl_seg_mutates(st):
                return True
            st = _CurlSegState()
            continue
        if not end_opts and arg == "--":
            end_opts = True
            continue
        if end_opts or not arg.startswith("-") or arg == "-":
            continue
        expect = (
            _apply_curl_long_opt(st, arg)
            if arg.startswith("--")
            else _apply_curl_short_opts(st, arg[1:])
        )
    return _curl_seg_mutates(st)


def is_printf_read_only(args: list[str]) -> bool:
    if not args:
        return True
    first = args[0]
    return first in ("-", "--") or not first.startswith("-")


def _is_safe_git_config(kv: str) -> bool:
    key = kv.split("=", 1)[0].strip().lower()
    return key in _SAFE_GIT_CONFIG_KEYS or key.startswith("color.")


def _scan_short_cluster(
    flags: str, val_short: frozenset[str], bad_ch: str = ""
) -> tuple[bool, bool]:
    for idx, ch in enumerate(flags):
        if bad_ch and ch == bad_ch:
            return False, False
        if ch in val_short:
            return True, idx + 1 == len(flags)
    return True, False


def _check_git_sub_args(sub: str, sub_args: list[str]) -> bool:
    expect_val = False
    val_short = _GIT_GREP_VAL_SHORT if sub == "grep" else _GIT_SUB_VAL_SHORT
    bad_ch = "O" if sub == "grep" else ""
    for sub_arg in sub_args:
        if expect_val:
            expect_val = False
            continue
        if sub_arg == "--":
            break
        if sub_arg in ("--output", "--ext-diff", "--textconv") or sub_arg.startswith("--output="):
            return False
        if sub == "grep" and (
            sub_arg == "--open-files-in-pager" or sub_arg.startswith("--open-files-in-pager=")
        ):
            return False
        if sub_arg in _GIT_SUB_VAL_LONG:
            expect_val = True
        elif sub_arg.startswith("-") and not sub_arg.startswith("--") and sub_arg != "-":
            ok, expect_val = _scan_short_cluster(sub_arg[1:], val_short, bad_ch)
            if not ok:
                return False
    return True


def is_git_read_only(args: list[str]) -> bool:
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "-c":
            if idx + 1 >= len(args) or not _is_safe_git_config(args[idx + 1]):
                return False
            idx += 2
        elif tok.startswith("-c") and len(tok) > 2:
            if not _is_safe_git_config(tok[2:]):
                return False
            idx += 1
        elif tok in _GIT_SAFE_VAL_FLAGS:
            if idx + 1 >= len(args):
                return False
            idx += 2
        elif (
            (tok.startswith("-C") and len(tok) > 2)
            or tok.startswith(_GIT_SAFE_VAL_PREFIXES)
            or tok in _GIT_SAFE_BOOL_FLAGS
        ):
            idx += 1
        elif tok.startswith("-"):
            return False
        else:
            sub = tok.lower()
            return sub in _READ_ONLY_GIT_SUBS and _check_git_sub_args(sub, args[idx + 1 :])
    return True


def is_sort_read_only(args: list[str]) -> bool:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            break
        if arg.startswith("--"):
            opt_name = arg.split("=", 1)[0]
            if (len(opt_name) >= 3 and "--output".startswith(opt_name)) or (
                len(opt_name) >= 4 and "--compress-program".startswith(opt_name)
            ):
                return False
            skip_next = arg in _SORT_VAL_LONG
        elif arg.startswith("-") and arg != "-":
            ok, skip_next = _scan_short_cluster(arg[1:], _SORT_VAL_SHORT, "o")
            if not ok:
                return False
    return True


def is_uniq_read_only(args: list[str]) -> bool:
    skip_next = False
    end_opts = False
    pos: list[str] = []
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if not end_opts:
            if arg == "--":
                end_opts = True
                continue
            if arg.startswith("--"):
                opt_name, has_eq, _ = arg.partition("=")
                skip_next = not has_eq and any(f.startswith(opt_name) for f in _UNIQ_VAL_LONG)
                continue
            if arg.startswith("+") and arg[1:].isdigit():
                continue
            if arg.startswith("-") and arg != "-":
                _, skip_next = _scan_short_cluster(arg[1:], _UNIQ_VAL_SHORT)
                continue
        pos.append(arg)
        if len(pos) > 2 or (len(pos) == 2 and pos[1] != "/dev/null"):
            return False
    return True


def is_rg_read_only(args: list[str]) -> bool:
    expect_val = False
    for arg in args:
        if expect_val:
            expect_val = False
            continue
        if arg == "--":
            break
        if arg in ("--pre", "--hostname-bin") or arg.startswith(("--pre=", "--hostname-bin=")):
            return False
        if arg in _RG_VAL_LONG:
            expect_val = True
        elif arg.startswith("-") and not arg.startswith("--") and arg != "-":
            _, expect_val = _scan_short_cluster(arg[1:], _RG_VAL_SHORT)
    return True


def is_less_read_only(args: list[str]) -> bool:
    expect_log = False
    expect_val = False
    for arg in args:
        if expect_log:
            expect_log = False
            if arg.strip("'\"") not in SAFE_OUT_TARGETS:
                return False
            continue
        if expect_val:
            expect_val = False
            continue
        if arg == "--":
            break
        if arg.startswith("+"):
            if any(c in arg for c in ("!", "v", "|")) or arg[1:2] == "s":
                return False
            continue
        low = arg.lower()
        if low == "--log-file":
            expect_log = True
            continue
        if low.startswith("--log-file="):
            if arg.split("=", 1)[1].strip("'\"") not in SAFE_OUT_TARGETS:
                return False
            continue
        if low in _LESS_VAL_LONG:
            expect_val = True
            continue
        if arg.startswith("-") and not arg.startswith("--") and arg != "-":
            flags = arg[1:]
            for idx, ch in enumerate(flags):
                rest = flags[idx + 1 :]
                if ch in ("o", "O"):
                    if not rest:
                        expect_log = True
                    elif rest.strip("'\"") not in SAFE_OUT_TARGETS:
                        return False
                    break
                if ch in _LESS_VAL_SHORT:
                    if not rest:
                        expect_val = True
                    break
    return not expect_log
