#!/usr/bin/env python3
"""Leak-oracle extractor: turn a task image's leaked future git history into a gold patch.

Given an original task package (task.toml + instruction.md + tests/test.patch)
and the task image's extracted ``.git`` directory, choose the upstream fix
commit and emit ``solution.patch`` (the fix diff restricted to non-test files)
plus ``evidence.json`` explaining the choice.

Method (see HAR-177 leak scan for the .git extraction approach):
  base = HEAD (what setup.sh records as BASE).
  future = `git rev-list --all --not base` + `git fsck --unreachable` commits.
  1. Test-file touch: keep future commits touching a file named in
     tests/test.patch that also exists in the base tree (this drops Xiaomi's
     harness files test_commands.json / mimo_test_command.sh, which upstream
     never touches).
  2. Before-blob continuity: prefer the earliest toucher whose test-file
     before-blob equals the base blob (i.e. the first future commit to touch
     the test file; later same-file commits fail this check).
  3. Instruction keyword overlap: subject/body token overlap with
     instruction.md + task title + added test names breaks ties and confirms
     the pick (this is what separates the fix from later unrelated commits
     that touch the same files, e.g. 002552's edb06c52 "unique_identifier").
  solution.patch = `git diff <fix>^..<fix>` restricted to non-test files,
  verified with `git apply --check` against a pristine base tree.

Selection statuses: ok, ok-divergent, no-identifiable-fix, test-only-fix,
empty-diff, patch-no-apply, needs-tip-decision. Operational failures are
input-error, git-error, git-timeout, unsupported-tree, unsupported-path,
output-error or internal-error; none is a scientific control result.

Python >= 3.7, stdlib only, with Git >= 2.20. SHA-1 repositories and ordinary
UTF-8 paths are supported;
C-quoted/control-character paths, submodules, binary solution patches and
solution symlink changes are rejected explicitly. Base-tree symlinks are
supported only when their targets stay inside the isolated checkout.
Read-only on the task package and the .git dir. Divergent patches can carry
unrelated evolution; applyability is not an oracle-pass claim.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
HARNESS_FILES = {"test_commands.json", "mimo_test_command.sh"}
TEST_PATH_RES = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)testing/"),
    re.compile(r"(^|/)test_.*\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"\.test\.[jt]s$"),
]
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "by",
        "as",
        "at",
        "from",
        "that",
        "this",
        "it",
        "its",
        "into",
        "over",
        "under",
        "after",
        "before",
        "between",
        "through",
        "during",
        "each",
        "other",
        "more",
        "most",
        "such",
        "no",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "can",
        "will",
        "just",
        "should",
        "now",
        "fix",
        "following",
        "issue",
        "bug",
        "error",
        "file",
        "code",
        "test",
        "tests",
        "using",
        "use",
        "used",
        "when",
        "then",
        "there",
        "their",
        "them",
        "they",
        "he",
        "she",
        "we",
        "you",
        "your",
        "our",
        "ours",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "all",
        "any",
        "both",
        "few",
        "many",
        "does",
        "did",
        "done",
        "has",
        "have",
        "had",
        "def",
        "return",
        "self",
        "none",
        "true",
        "false",
        "if",
        "else",
        "elif",
        "while",
    ]
)


class ExtractionError(Exception):
    """A stable operational status with separately recorded diagnostics."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def git_process(
    git_dir: str | None, *args: str, timeout: int = 300, **kwargs
) -> subprocess.CompletedProcess:
    # Do not inherit a caller's index/worktree, prompts, config injection or
    # optional locks. No command below changes refs, the index or source files.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
        }
    )
    command = [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
    ]
    if git_dir is not None:
        command += ["--git-dir", str(git_dir)]
    try:
        return subprocess.run(
            command + list(args), env=env, capture_output=True, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("git-timeout", "Git command exceeded its timeout") from exc
    except OSError as exc:
        raise ExtractionError("git-error", str(exc)) from exc


def run_git(git_dir: str, *args: str, timeout: int = 300, check: bool = True) -> str:
    proc = git_process(git_dir, *args, timeout=timeout, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise ExtractionError(
            "git-error", "git {} failed: {}".format(" ".join(args[:3]), proc.stderr[:500])
        )
    return proc.stdout


def safe_path(path: str) -> str:
    """Accept only literal repository-relative paths, never pathspec magic."""
    if (
        not path
        or path.startswith(("/", ":", '"'))
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or any(part in ("", ".", "..") or part.casefold() == ".git" for part in path.split("/"))
    ):
        raise ExtractionError("unsupported-path", f"unsupported repository path: {path!r}")
    return path


def git_diff(git_dir: str, start: str, end: str, paths: list[str]) -> str:
    if not paths:
        return ""
    return run_git(
        git_dir,
        "--literal-pathspecs",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        start,
        end,
        "--",
        *[safe_path(path) for path in paths],
        timeout=300,
    )


def is_test_path(path: str) -> bool:
    return any(rx.search(path) for rx in TEST_PATH_RES)


def parse_test_patch(patch_text: str) -> list[str]:
    """Repo-relative b-side paths from a unified diff."""
    files: list[str] = []
    for line in patch_text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            b = safe_path(m.group(2))
            if b not in files:
                files.append(b)
        elif line.startswith("diff --git "):
            raise ExtractionError("unsupported-path", "C-quoted diff paths are unsupported")
    return files


def parse_fail_to_pass(patch_text: str, package_dir: Path) -> list[str]:
    """Pytest node ids the hidden tests run (from mimo_test_command.sh hunk)."""
    nodes: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            nodes += re.findall(r"[\w./-]+\.py::[\w:]+", line[1:])
    cmds = package_dir / "tests" / "test_commands.json"
    if not nodes and cmds.is_file():
        try:
            data = json.loads(cmds.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("test_commands must be an object")
            commands = data.get("test_commands", [])
            if not isinstance(commands, list) or not all(
                isinstance(node, str) for node in commands
            ):
                raise ValueError("test_commands must be a list of strings")
            nodes = commands
        except ValueError as exc:
            raise ExtractionError("input-error", "invalid test_commands.json: " + str(exc)) from exc
    return sorted(set(nodes))


def tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) >= 3 and t not in STOPWORDS
    }


PATH_SPAN_RE = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.py")
IMPORT_RE = re.compile(r"(?m)^(?:from|import)\s+([\w.]+)")
CODE_SPAN_RE = re.compile(r"`([^`\n]{3,80})`")
IMPORT_STOP = frozenset(
    [
        "os",
        "sys",
        "types",
        "json",
        "re",
        "pytest",
        "pathlib",
        "datetime",
        "threading",
        "http",
        "urllib",
        "io",
        "tempfile",
        "functools",
        "typing",
        "collections",
        "hashlib",
    ]
)


def test_imports(patch_text: str) -> list[str]:
    """Dotted modules imported by the added test files (generic ones dropped)."""
    added = "\n".join(
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    mods = []
    for mod in IMPORT_RE.findall(added):
        if mod.split(".")[0] not in IMPORT_STOP and mod not in mods:
            mods.append(mod)
    return mods


def extract_source_paths(instruction: str, patch_text: str) -> list[str]:
    """Repo-relative source paths named by the instruction (tracebacks,
    code spans) or the added tests. E.g. numpyro/distributions/batch_util.py
    in 002402's traceback, miio/miot_models.py in 002552's."""
    paths = []
    for text in (instruction, patch_text):
        for m in PATH_SPAN_RE.findall(text):
            # Strip site-packages/venv prefixes down to the repo-relative tail.
            parts = m.split("/")
            for cut in ("site-packages", ".venv", "dist-packages"):
                if cut in parts:
                    parts = parts[parts.index(cut) + 1 :]
                    break
            rel = "/".join(parts)
            if rel and rel not in paths:
                paths.append(rel)
    return paths


LOG_FMT = "%H%x00%P%x00%aI%x00%s%x00%b%x01"
LOG_REC_RE = re.compile(r"(?m)^(?=[0-9a-f]{40}\x00)")


def parse_log_records(out: str) -> list[dict]:
    """Parse `git log` (LOG_FMT + --name-status) output into commit dicts.

    Records split on `^<sha>NUL`: the format's %x01 only terminates the
    header, while bodies and file lists both contain newlines, so naive
    chunking silently drops every commit after the first.
    """
    records = []
    for record in LOG_REC_RE.split(out):
        if not record.strip():
            continue
        fmt, _, tail = record.partition("\x01")
        parts = fmt.split("\x00", 4)
        if len(parts) < 5 or not SHA_RE.match(parts[0]):
            continue
        sha, parents, date, subject, body = parts
        files: list[tuple[str, str]] = []
        for line in tail.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[0]:
                # Renames are deliberately disabled in the metadata commands.
                files.append((fields[0][:1], safe_path(fields[-1])))
        records.append(
            {
                "sha": sha,
                "parents": parents.split(),
                "date": date,
                "subject": subject,
                "body": body,
                "files": files,
            }
        )
    return records


def future_commits(git_dir: str, base: str) -> list[dict]:
    """Every future commit (on-ref beyond base + unreachable) with metadata.

    One `git log` over all refs plus one fsck pass; metadata-only.
    """
    beyond = run_git(
        git_dir,
        "log",
        "--all",
        "--not",
        base,
        f"--format={LOG_FMT}",
        "--name-status",
        "--no-renames",
        "--no-ext-diff",
        timeout=600,
    )
    commits: dict[str, dict] = {}
    for record in parse_log_records(beyond):
        commits[record["sha"]] = {**record, "on_ref": True}
    fsck = git_process(
        git_dir,
        "fsck",
        "--unreachable",
        "--no-reflogs",
        timeout=600,
        encoding="utf-8",
        errors="replace",
    )
    # Truncated task images have broken remote-HEAD refs. Preserve this known
    # diagnostic, but never turn corrupt/missing objects into "no fix".
    errors = [line for line in fsck.stderr.splitlines() if line and not line.startswith("notice:")]
    if fsck.returncode and (
        not errors
        or any(
            not re.match(r"error: (?:refs/[^:]+|HEAD): invalid sha1 pointer ", line)
            for line in errors
        )
    ):
        raise ExtractionError("git-error", "git fsck failed: " + fsck.stderr[:500])
    unreach = []
    for line in fsck.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[:2] == ["unreachable", "commit"] and SHA_RE.match(parts[2]):
            unreach.append(parts[2])
    unreach.sort()
    missing = [s for s in unreach if s not in commits]
    for i in range(0, len(missing), 200):
        out = run_git(
            git_dir,
            "log",
            "--no-walk",
            f"--format={LOG_FMT}",
            "--name-status",
            "--no-renames",
            "--no-ext-diff",
            *missing[i : i + 200],
            timeout=600,
        )
        for record in parse_log_records(out):
            commits[record["sha"]] = {**record, "on_ref": False}
    return [commits[sha] for sha in sorted(commits)]


def blob_at(git_dir: str, commit: str, path: str) -> str | None:
    proc = git_process(
        git_dir,
        "rev-parse",
        "--verify",
        f"{commit}:{safe_path(path)}",
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and SHA_RE.match(out) else None


def base_tree_paths(git_dir: str, base: str) -> set[str]:
    out = run_git(git_dir, "ls-tree", "-rz", "--name-only", base, timeout=300)
    return {safe_path(path) for path in out.split("\x00") if path}


def graph_distance(git_dir: str, base: str, sha: str) -> int | None:
    """Commits on base..sha (None when sha does not descend from base)."""
    ancestry = git_process(
        git_dir,
        "merge-base",
        "--is-ancestor",
        base,
        sha,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    if ancestry.returncode == 1:
        return None
    if ancestry.returncode != 0:
        raise ExtractionError("git-error", ancestry.stderr[:500])
    return int(run_git(git_dir, "rev-list", "--count", f"{base}..{sha}", timeout=120).strip())


def extract_identifiers(patch_text: str, instruction: str) -> list[str]:
    """Distinctive code identifiers the hidden tests / instruction name.

    Sources: import statements in the added test files (module paths point
    at the code under test) and `code spans` in instruction.md, sub-tokenized
    (a span like `gs://bkt/releases/pkg;downloadfilename=x` yields
    `downloadfilename`, the pickaxe-distinctive part). Generic names
    (os, sys, pytest, json ...) are dropped; over-common pickaxe hits are
    dropped later by the >25-hit rule.
    """
    ids: dict[str, str] = {}
    added = "\n".join(
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for mod in IMPORT_RE.findall(added):
        top = mod.split(".")[0]
        if (
            top
            not in (
                "os",
                "sys",
                "types",
                "json",
                "re",
                "pytest",
                "pathlib",
                "datetime",
                "threading",
                "http",
                "urllib",
                "io",
                "tempfile",
                "functools",
                "typing",
                "collections",
                "hashlib",
            )
            and len(top) >= 3
        ):
            ids.setdefault(mod, "test-import")
    for span in CODE_SPAN_RE.findall(instruction):
        span = span.strip()
        if re.fullmatch(r"[\w./-]{3,60}", span) and not span.endswith(".py"):
            ids.setdefault(span, "instruction-codespan")
        # Sub-tokens: snake_case / CamelCase / dotted parts, >= 6 chars.
        for tok in re.findall(r"[A-Za-z][\w.]{5,59}", span):
            for part in re.split(r"[./]", tok):
                if len(part) >= 6 and part.lower() not in STOPWORDS:
                    ids.setdefault(part, "instruction-subtoken")
    # Longest-first: distinctive multi-word identifiers pickaxe cleanly.
    ranked = sorted(ids, key=lambda s: (-len(s), s))
    return [s for s in ranked if not s[0].isdigit()]


def pickaxe(
    git_dir: str, base: str, identifier: str, extra_shas: list[str] | None = None
) -> tuple[list[dict], int]:
    """Commits changing the occurrence count of `identifier` in the future.
    Searches on-ref future history plus any extra (unreachable) shas.
    Identifiers matching >25 commits are non-distinctive: count returned,
    hits dropped. Returns (hits, total).
    """
    args = [
        "log",
        "--all",
        "--not",
        base,
        "--format=%H%x00%aI%x00%s",
        "--no-ext-diff",
        "--no-textconv",
        "-S",
        identifier,
    ]
    outputs = [run_git(git_dir, *args, timeout=600)]
    # Search both sets; dangling commits must not suppress on-ref fixes.
    for offset in range(0, len(extra_shas or []), 200):
        outputs.append(
            run_git(
                git_dir,
                "log",
                "--no-walk",
                "--format=%H%x00%aI%x00%s",
                "--no-ext-diff",
                "--no-textconv",
                "-S",
                identifier,
                *(extra_shas or [])[offset : offset + 200],
                timeout=600,
            )
        )
    out = "\n".join(outputs)
    hits = []
    for line in out.splitlines():
        parts = line.split("\x00")
        if len(parts) >= 3 and SHA_RE.match(parts[0]):
            hits.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    if len(hits) > 25:
        return [], len(hits)
    detailed = []
    for h in hits:
        files = run_git(
            git_dir,
            "show",
            "--format=",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            h["sha"],
            timeout=120,
        ).splitlines()
        detailed.append(
            {**h, "identifier": identifier, "files": [safe_path(path) for path in files if path]}
        )
    return detailed, len(hits)


def ref_tips_with(git_dir: str, identifiers: list[str], base: str) -> list[dict]:
    """Branch tips whose trees contain the identifiers (for divergent futures)."""
    out = run_git(git_dir, "for-each-ref", "--format=%(refname) %(objectname)", timeout=60)
    tips = []
    for line in out.splitlines():
        ref, _, sha = line.partition(" ")
        if not SHA_RE.match(sha) or sha == base:
            continue
        found = []
        for ident in identifiers[:12]:
            proc = git_process(
                git_dir,
                "grep",
                "-l",
                "-F",
                "-e",
                ident,
                sha,
                "--",
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                found.append(ident)
            elif proc.returncode not in (0, 1):
                raise ExtractionError("git-error", proc.stderr[:500])
        if found:
            tips.append({"ref": ref, "sha": sha, "identifiers_found": found})
    return sorted(tips, key=lambda t: (-len(t["identifiers_found"]), t["ref"], t["sha"]))


SUCCESS_STATUSES = frozenset(("ok", "ok-divergent"))


def atomic_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()


def output_is_separate(out_dir: Path, *inputs: Path) -> bool:
    destination = out_dir.resolve()
    return all(
        destination != source.resolve()
        and source.resolve() not in destination.parents
        and destination not in source.resolve().parents
        for source in inputs
    )


def extract(
    task_dir: Path,
    git_dir: str,
    out_dir: Path,
    tip: str | None = None,
    files: list[str] | None = None,
) -> dict:
    """Extract a patch and persist evidence; never emit a patch on failure.

    The returned status is selection/operational evidence, not a verifier
    label. Inputs remain untouched. ``tip`` may be a commit or a ref;
    ``files`` are literal repository-relative paths for S2 only.
    """
    task_dir, git_path, out_dir = Path(task_dir), Path(git_dir), Path(out_dir)
    evidence = {"task": task_dir.name, "fix": None}
    if not output_is_separate(out_dir, task_dir, git_path):
        evidence.update(
            {"status": "input-error", "rationale": "output directory must not overlap either input"}
        )
        return evidence
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        solution = out_dir / "solution.patch"
        if solution.exists() or solution.is_symlink():
            solution.unlink()
        if files is not None:
            if not files or not all(isinstance(path, str) for path in files):
                raise ExtractionError("input-error", "files must be a non-empty list of paths")
            files = sorted(set(safe_path(path) for path in files))
        if tip is not None:
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", tip) or tip.startswith("-"):
                raise ExtractionError("input-error", "tip must be a commit SHA or ref name")
            tip = run_git(str(git_path), "rev-parse", "--verify", tip + "^{commit}").strip()
            if not SHA_RE.match(tip):
                raise ExtractionError(
                    "unsupported-tree", "only SHA-1 Git repositories are supported"
                )
        evidence = _extract(task_dir, str(git_path), out_dir, tip, files)
    except ExtractionError as exc:
        evidence.update({"status": exc.status, "rationale": str(exc)})
    except (OSError, UnicodeError, ValueError) as exc:
        evidence.update({"status": "input-error", "rationale": str(exc)})
    except Exception as exc:
        evidence.update(
            {"status": "internal-error", "error_type": type(exc).__name__, "rationale": str(exc)}
        )
    try:
        if evidence.get("status") not in SUCCESS_STATUSES:
            solution = out_dir / "solution.patch"
            if solution.exists() or solution.is_symlink():
                solution.unlink()
        atomic_text(
            out_dir / "evidence.json", json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        )
    except OSError as exc:
        evidence.update({"status": "output-error", "rationale": str(exc)})
        solution = out_dir / "solution.patch"
        try:
            if solution.exists() or solution.is_symlink():
                solution.unlink()
        except OSError:
            pass
    return evidence


def _extract(
    task_dir: Path, git_dir: str, out_dir: Path, tip: str | None, files: list[str] | None
) -> dict:
    task_toml = (task_dir / "task.toml").read_text(encoding="utf-8")
    image = re.search(r'docker_image\s*=\s*"([^"]+)"', task_toml)
    workdir = re.search(r'workdir\s*=\s*"([^"]+)"', task_toml)
    title = re.search(r"(?m)^(?:title|description)\s*=\s*\"([^\"]+)\"", task_toml)
    patch_text = (task_dir / "tests" / "test.patch").read_text(encoding="utf-8")
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="replace")
    patch_files = parse_test_patch(patch_text)
    fail_to_pass = parse_fail_to_pass(patch_text, task_dir)

    base = run_git(git_dir, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if not SHA_RE.match(base):
        raise ExtractionError("unsupported-tree", "only SHA-1 Git repositories are supported")
    tree_paths = base_tree_paths(git_dir, base)
    # Task test files: named by test.patch AND present in the base tree.
    # This drops Xiaomi harness files (test_commands.json, mimo_test_command.sh).
    task_test_files = [
        f for f in patch_files if f in tree_paths and posixpath.basename(f) not in HARNESS_FILES
    ]
    base_blobs = {f: blob_at(git_dir, base, f) for f in task_test_files}

    issue_tokens = (
        tokens(instruction)
        | tokens(title.group(1) if title else "")
        | tokens(" ".join(fail_to_pass))
    )

    source_paths = extract_source_paths(instruction, patch_text)
    future = future_commits(git_dir, base)
    candidates = []
    for c in future:
        touched = [p for _, p in c["files"] if p in task_test_files]
        if not touched:
            continue
        # Before-blob continuity: first future toucher keeps the base blob.
        continuous = [
            f
            for f in touched
            if c["parents"] and blob_at(git_dir, c["parents"][0], f) == base_blobs[f]
        ]
        # Source-file signal: the fix touches a source file the instruction
        # names (traceback/code span), e.g. batch_util.py for 002402.
        non_test = non_test_files_of([p for _, p in c["files"]], patch_files)
        source_hits = sorted(
            {p for p in non_test for s in source_paths if p == s or p.endswith("/" + s)}
        )
        kw = tokens(c["subject"] + "\n" + c["body"]) & issue_tokens
        subj_kw = tokens(c["subject"]) & issue_tokens
        candidates.append(
            {
                "sha": c["sha"],
                "subject": c["subject"],
                "date": c["date"],
                "on_ref": c["on_ref"],
                "distance": graph_distance(git_dir, base, c["sha"]),
                "touched_test_files": touched,
                "continuous": continuous,
                "source_hits": source_hits,
                "keyword_hits": sorted(kw),
                "subject_hits": sorted(subj_kw),
                "_kw": kw,
                "_subj_kw": subj_kw,
                "files_changed": len(c["files"]),
            }
        )
    # IDF-weighted keywords: distinctive tokens (promote_batch_shape) beat
    # generic ones (jax, python) shared across many candidates.
    doc_freq: dict[str, int] = {}
    for c in candidates:
        for t in c["_kw"] | c["_subj_kw"]:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    for c in candidates:
        idf_kw = {t: 1.0 / (1 + doc_freq[t]) for t in c["_kw"]}
        idf_subj = {t: 1.0 / (1 + doc_freq[t]) for t in c["_subj_kw"]}
        c["score_kw"] = round(
            2 * sum(idf_subj[token] for token in sorted(idf_subj))
            + sum(idf_kw[token] for token in sorted(idf_kw)),
            3,
        )
        c["top_keywords"] = sorted(
            set(idf_subj) | set(idf_kw),
            key=lambda t: (-(idf_subj.get(t, 0) + idf_kw.get(t, 0)), t),
        )[:5]
        del c["_kw"]
        del c["_subj_kw"]

    evidence: dict = {
        "task": task_dir.name,
        "image": image.group(1) if image else None,
        "workdir": workdir.group(1) if workdir else None,
        "base": base,
        "n_future_commits": len(future),
        "n_future_on_ref": sum(1 for c in future if c["on_ref"]),
        "test_patch_files": patch_files,
        "task_test_files": task_test_files,
        "fail_to_pass": fail_to_pass,
        "n_candidates": len(candidates),
        "candidates": sorted(
            candidates,
            key=lambda c: (
                -(1 if c["source_hits"] else 0),
                -(1 if c["continuous"] else 0),
                -c["score_kw"],
                c["distance"] if c["distance"] is not None else 10**9,
                c["sha"],
            ),
        )[:25],
    }

    identifiers = extract_identifiers(patch_text, instruction)
    evidence["identifiers"] = identifiers
    # Reuse the fsck pass already performed by future_commits.
    unreach_shas = [c["sha"] for c in future if not c["on_ref"]]
    evidence["n_unreachable_commits"] = len(unreach_shas)
    pickaxe_hits: list[dict] = []
    pickaxe_dropped: dict[str, int] = {}
    for ident in identifiers[:15]:
        hits, total = pickaxe(git_dir, base, ident, unreach_shas or None)
        if not hits and total > 25:
            pickaxe_dropped[ident] = total
            continue
        for h in hits:
            h["score_kw"] = 3 * len(tokens(h["subject"]) & issue_tokens)
            pickaxe_hits.append(h)
    # De-duplicate by sha, keeping the earliest identifier source.
    seen: dict[str, dict] = {}
    for h in sorted(pickaxe_hits, key=lambda h: (h["date"], h["identifier"])):
        if h["sha"] not in seen:
            seen[h["sha"]] = h
        else:
            # Same commit via a second identifier: corroboration bonus.
            seen[h["sha"]]["score_kw"] += 1
            seen[h["sha"]]["identifier"] += f",{h['identifier']}"
    pickaxe_hits = list(seen.values())
    # Refine with body vocabulary (one batched call for all hits).
    if pickaxe_hits:
        out = run_git(
            git_dir,
            "log",
            "--no-walk",
            "--format=%H%x00%B%x01",
            *[h["sha"] for h in pickaxe_hits],
            timeout=600,
        )
        bodies = {}
        for chunk in out.split("\x01"):
            sha, _, body = chunk.partition("\x00")
            if SHA_RE.match(sha.strip()):
                bodies[sha.strip()] = body
        for h in pickaxe_hits:
            h["score_kw"] = (
                2 * len(tokens(h["subject"]) & issue_tokens)
                + len(tokens(bodies.get(h["sha"], "")) & issue_tokens)
                + (1 if "," in h["identifier"] else 0)
            )
    evidence["pickaxe_dropped_non_distinctive"] = pickaxe_dropped
    evidence["pickaxe_hits"] = [
        {k: h[k] for k in ("sha", "date", "subject", "identifier", "score_kw")}
        for h in sorted(pickaxe_hits, key=lambda h: (-h["score_kw"], h["sha"]))[:25]
    ]

    if not candidates and not pickaxe_hits:
        evidence.update(
            {
                "status": "no-identifiable-fix",
                "fix": None,
                "rationale": (
                    "no future commit touches the task's test files (test.patch "
                    "adds new files absent at base) and no distinctive "
                    f"instruction/test identifier ({len(identifiers)} tried) "
                    "introduces a matching upstream change. The instructed "
                    "behavior may never have existed upstream."
                ),
            }
        )
        return evidence

    # S1 is decisive when a test-touching commit names an instruction source
    # path, is continuous with base, or shares distinctive vocabulary.
    s1_strong = any(c["source_hits"] or c["continuous"] or c["score_kw"] > 0 for c in candidates)
    if not candidates or not s1_strong:
        return extract_s2(
            task_dir,
            git_dir,
            out_dir,
            evidence,
            base,
            patch_files,
            identifiers,
            pickaxe_hits,
            issue_tokens,
            tree_paths,
            test_imports(patch_text),
            tip=tip,
            files=files,
        )

    ranked = sorted(
        candidates,
        key=lambda c: (
            -(1 if c["source_hits"] else 0),
            -(1 if c["continuous"] else 0),
            -c["score_kw"],
            c["distance"] if c["distance"] is not None else 10**9,
            c["files_changed"],
            c["date"],
            c["sha"],
        ),
    )
    fix = ranked[0]
    full = next(c for c in future if c["sha"] == fix["sha"])
    non_test = non_test_files_of([p for _, p in full["files"]], patch_files)
    test_touched_by_fix = sorted(
        {p for _, p in full["files"] if is_test_path(p) or p in patch_files}
    )

    if not non_test:
        evidence.update(
            {
                "status": "test-only-fix",
                "fix": fix,
                "test_files_touched_by_fix": test_touched_by_fix,
                "rationale": (
                    f"chose {fix['sha'][:8]} ({fix['subject']}) but it has no "
                    "non-test changes; needs a sibling source commit (not automated)"
                ),
            }
        )
        return evidence

    diff = own_diff(git_dir, fix["sha"], non_test)
    if not diff.strip():
        evidence.update(
            {
                "status": "empty-diff",
                "fix": fix,
                "rationale": "chosen commit has an empty source diff",
            }
        )
        return evidence
    apply_ok, apply_err = check_applies(git_dir, base, diff)
    evidence["feature_files"] = non_test
    evidence["test_files_touched_by_fix"] = test_touched_by_fix
    return finish(
        out_dir,
        evidence,
        git_dir,
        base,
        diff,
        fix,
        patch_files,
        strategy=(
            "S1: test-file touch; source relevance {}, before-blob "
            "continuity {}, keyword score {}, distance {}.".format(
                fix["source_hits"], fix["continuous"], fix["score_kw"], fix["distance"]
            )
        ),
        status="ok" if apply_ok else "patch-no-apply",
        apply_ok=apply_ok,
        apply_err=apply_err,
    )


S2B_EXCLUDE_RES = [
    re.compile(r"(^|/)CHANGELOG[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)README[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)(docs|doc)/"),
    re.compile(r"(^|/)samples?/"),
]


def s2b_excluded(path: str) -> bool:
    return any(rx.search(path) for rx in S2B_EXCLUDE_RES)


def anchor_paths(git_dir: str, tree: str, modules: list[str]) -> dict[str, str]:
    """Map test-imported modules to repo paths in `tree` (base or tip)."""
    paths = sorted(base_tree_paths(git_dir, tree))
    anchored = {}
    for mod in modules:
        rel = mod.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            hit = next((p for p in paths if p == cand or p.endswith("/" + cand)), None)
            if hit:
                anchored[mod] = hit
                break
        if mod not in anchored and "." not in mod:
            # Top-level package import (e.g. `from nse import NSE`): any
            # source file under a matching directory anchors the feature.
            dirs = sorted(
                {
                    p.rsplit("/", 1)[0]
                    for p in paths
                    if f"/{mod}/" in p.lower() and p.endswith(".py")
                }
            )
            if dirs:
                anchored[mod] = dirs[0] + "/"
    return anchored


def tip_containing(git_dir: str, fix_sha: str) -> str | None:
    """Most recent ref whose history contains the fix commit (a valid tip)."""
    out = run_git(
        git_dir,
        "for-each-ref",
        "--contains",
        fix_sha,
        "--sort=-committerdate",
        "--format=%(objectname)",
        timeout=120,
    )
    for line in out.splitlines():
        if SHA_RE.match(line.strip()):
            return line.strip()
    return None


def non_test_files_of(file_list: list[str], patch_files: list[str]) -> list[str]:
    return sorted(
        {
            safe_path(path)
            for path in file_list
            if path
            and path not in patch_files
            and posixpath.basename(path) not in HARNESS_FILES
            and not is_test_path(path)
        }
    )


def own_diff(git_dir: str, sha: str, files: list[str]) -> str:
    parent = run_git(git_dir, "rev-parse", f"{sha}^", timeout=60).strip()
    return git_diff(git_dir, parent, sha, files)


def extract_s2(
    task_dir: Path,
    git_dir: str,
    out_dir: Path,
    evidence: dict,
    base: str,
    patch_files: list[str],
    identifiers: list[str],
    pickaxe_hits: list[dict],
    issue_tokens: set[str],
    tree_paths: set[str],
    imports: list[str],
    tip: str | None = None,
    files: list[str] | None = None,
) -> dict:
    """Identifier-led oracle for tasks whose tests are Xiaomi-authored.

    Strategy S2a: the pickaxe-convergent introducing commit's own diff
    (works when it is a descendant of base). Strategy S2b (fallback):
    ``git diff base..TIP`` restricted to the feature source files (works
    for divergent/stripped futures; by construction applies on base, but
    may carry unrelated evolution of those files -- recorded as a caveat).
    """
    for h in pickaxe_hits:
        h["distance"] = graph_distance(git_dir, base, h["sha"])
        h["files_nt"] = non_test_files_of(h.get("files", []), patch_files)
    ranked = sorted(
        pickaxe_hits,
        key=lambda h: (
            -h["score_kw"],
            0 if h["distance"] is not None else 1,
            h["date"],
            h["sha"],
        ),
    )
    evidence["pickaxe_ranked"] = [
        {
            k: h[k]
            for k in ("sha", "date", "subject", "identifier", "score_kw", "distance", "files_nt")
        }
        for h in ranked[:15]
    ]
    if not ranked:
        evidence.update({"status": "no-identifiable-fix", "fix": None})
        return evidence
    with_files = [h for h in ranked if h["files_nt"]]
    if not with_files:
        evidence.update(
            {
                "status": "test-only-fix",
                "fix": ranked[0],
                "rationale": "S2 hits touch only test files; no source fix visible",
            }
        )
        return evidence
    relevant = [h for h in with_files if h["score_kw"] > 0] or with_files[:3]
    feature_files = (
        files
        if files is not None
        else sorted({path for hit in relevant for path in hit["files_nt"]})
    )
    best = with_files[0]
    excluded = sorted(path for path in feature_files if s2b_excluded(path))
    feature_files = [
        path for path in non_test_files_of(feature_files, patch_files) if not s2b_excluded(path)
    ]
    evidence["s2b_excluded"] = excluded
    anchored = anchor_paths(git_dir, tip or best["sha"], imports) or anchor_paths(
        git_dir, base, imports
    )
    evidence["test_imports"] = imports
    evidence["anchored_paths"] = anchored
    if anchored and files is None:
        feature_files = anchored_source_files(feature_files, anchored)
        if not feature_files:
            evidence.update(
                {
                    "status": "no-identifiable-fix",
                    "fix": best,
                    "rationale": "identifier hits touch no source module imported by the hidden tests",
                    "feature_files": [],
                }
            )
            return evidence
    evidence["feature_files"] = feature_files
    if not feature_files:
        evidence.update(
            {
                "status": "test-only-fix",
                "fix": best,
                "rationale": "identifier hits contain no eligible source files",
            }
        )
        return evidence
    if best["distance"] is not None and files is None and tip is None:
        own_files = sorted(set(best["files_nt"]) & set(feature_files))
        diff = own_diff(git_dir, best["sha"], own_files)
        if diff.strip():
            ok, err = check_applies(git_dir, base, diff)
            if ok:
                evidence["feature_files"] = own_files
                return finish(
                    out_dir,
                    evidence,
                    git_dir,
                    base,
                    diff,
                    best,
                    patch_files,
                    strategy=(
                        f"S2a: pickaxe identifier '{best['identifier']}' converges on "
                        f"{best['sha'][:8]} '{best['subject']}' (descendant, distance "
                        f"{best['distance']}); own-diff applies on base."
                    ),
                )
            evidence["s2a_apply_failure"] = err[:300]
    if tip is None:
        # Candidate tips: the ref containing the fix, plus identifier-matched
        # refs. A stale tag can be the only ref containing the fix yet carry a
        # destructive diff (000552: v0.1.0 deletes 1336 lines); pick the tip
        # whose diff on the fix's files is smallest and least destructive.
        cands = []
        cf = tip_containing(git_dir, best["sha"])
        if cf:
            cands.append(cf)
        for t in ref_tips_with(git_dir, identifiers, base)[:4]:
            if t["sha"] not in cands:
                cands.append(t["sha"])
        scope = sorted(set(best["files_nt"]) & set(feature_files))
        if not scope:
            scope = feature_files

        def _tip_cost(sha: str) -> tuple[int, int]:
            ns = run_git(
                git_dir,
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--numstat",
                base,
                sha,
                "--",
                *scope,
                timeout=120,
            )
            ins = dels = 0
            for line in ns.splitlines():
                p = line.split("\t")
                if len(p) == 3:
                    try:
                        ins += int(p[0])
                        dels += int(p[1])
                    except ValueError as exc:
                        raise ExtractionError(
                            "unsupported-tree", "binary feature files are unsupported"
                        ) from exc
            return (dels, ins + dels)

        if cands:
            costs = {candidate: _tip_cost(candidate) for candidate in cands}
            tip = min(cands, key=lambda candidate: (costs[candidate], candidate))
            evidence["tip_via"] = "least-destructive"
            evidence["tip_candidates_cost"] = {
                candidate: list(costs[candidate]) for candidate in sorted(costs)
            }
    if tip is None:
        evidence.update(
            {
                "status": "needs-tip-decision",
                "fix": best,
                "rationale": "no ref tip contains the identifiers; manual tip needed",
            }
        )
        return evidence
    evidence["tip"] = tip
    anchored = anchor_paths(git_dir, tip, imports) or anchor_paths(git_dir, base, imports)
    evidence["anchored_paths"] = anchored
    if anchored and files is None:
        feature_files = anchored_source_files(feature_files, anchored)
    # Scope to the highest-scoring introducing commit: it is the fix and
    # touches every file the fix needs. Files touched only by lower-scoring
    # hits are unrelated evolution (002139: the other fetchers).
    if files is None and relevant and feature_files:
        top_score = max(h["score_kw"] for h in relevant)
        top_files = {f for h in relevant if h["score_kw"] == top_score for f in h["files_nt"]}
        feature_files = [f for f in feature_files if f in top_files]
        evidence["feature_files"] = feature_files
    evidence["feature_files"] = feature_files
    if not feature_files:
        evidence.update(
            {
                "status": "no-identifiable-fix",
                "fix": best,
                "rationale": "top-ranked introducing changes do not touch the tested source modules",
            }
        )
        return evidence
    diff = git_diff(git_dir, base, tip, feature_files)
    if not diff.strip():
        evidence.update(
            {
                "status": "empty-diff",
                "fix": best,
                "rationale": "base..tip diff on feature files is empty",
            }
        )
        return evidence
    ok, err = check_applies(git_dir, base, diff)
    status = "ok-divergent" if ok else "patch-no-apply"
    plural = len({h["sha"] for h in relevant})
    return finish(
        out_dir,
        evidence,
        git_dir,
        base,
        diff,
        best,
        patch_files,
        strategy=(
            f"S2b: {plural} introducing commit(s) across divergent history; "
            f"base..{tip[:8]} diff on feature files {feature_files}. "
            f"CAVEAT: carries all base..tip evolution of those files, "
            f"possibly including unrelated refactors."
        ),
        status=status,
        apply_ok=ok,
        apply_err=err,
        tip=tip,
    )


def anchored_source_files(paths: list[str], anchored: dict[str, str]) -> list[str]:
    files = {value for value in anchored.values() if not value.endswith("/")}
    directories = {
        value.rstrip("/") if value.endswith("/") else posixpath.dirname(value)
        for value in anchored.values()
    }
    return sorted(path for path in paths if path in files or posixpath.dirname(path) in directories)


def finish(
    out_dir: Path,
    evidence: dict,
    git_dir: str,
    base: str,
    diff: str,
    fix: dict,
    patch_files: list[str],
    strategy: str,
    status: str = "ok",
    apply_ok: bool = True,
    apply_err: str = "",
    tip: str | None = None,
) -> dict:
    patch_paths = parse_test_patch(diff)
    if (
        set(patch_paths) != set(non_test_files_of(patch_paths, patch_files))
        or re.search(r"(?m)^(?:GIT binary patch|Binary files )", diff)
        or re.search(
            r"(?m)^(?:old mode|new mode|new file mode|deleted file mode) (?:120000|160000)$", diff
        )
        or re.search(r"(?m)^index [^\n]+ (?:120000|160000)$", diff)
    ):
        raise ExtractionError(
            "unsupported-tree", "solution must contain only regular text source changes"
        )
    # Per-file evolution size: flags files whose base..X change dwarfs a
    # surgical fix (unrelated-refactor risk in S2b).
    scope = evidence.get("feature_files") or []
    endpoint = tip or fix["sha"]
    start = base if tip else f"{fix['sha']}^"
    numstat = run_git(
        git_dir,
        "--literal-pathspecs",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--numstat",
        start,
        endpoint,
        "--",
        *scope,
        timeout=120,
    )
    per_file = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                changed = int(parts[0]) + int(parts[1])
            except ValueError:
                changed = -1  # binary
            per_file.append(
                {
                    "file": parts[2],
                    "changed_lines": changed,
                    "unrelated_evolution_risk": changed > 200,
                }
            )
    evidence.update(
        {
            "status": status,
            "fix": fix,
            "strategy": strategy,
            "non_test_files": evidence.get("feature_files", []),
            "solution_patch_bytes": len(diff.encode()),
            "solution_patch_hunks": diff.count("\n@@ "),
            "per_file_changed_lines": per_file,
            "apply_check_on_base": apply_ok,
            "apply_check_error": apply_err,
            "rationale": strategy,
        }
    )
    if status in SUCCESS_STATUSES and apply_ok:
        try:
            atomic_text(out_dir / "solution.patch", diff)
        except OSError as exc:
            raise ExtractionError("output-error", str(exc)) from exc
    return evidence


def unpack_base(git_dir: str, base: str, destination: Path) -> None:
    """Materialize raw base blobs, bypassing unsafe archive/filter semantics.

    Nothing is checked out through Git filters, tar extraction or the source
    index. Symlinks are validated before any filesystem write.
    """
    manifest = {}
    for entry in run_git(git_dir, "ls-tree", "-rz", base).split("\x00"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, sha = metadata.split()
        path = safe_path(path)
        if kind != "blob" or mode not in ("100644", "100755", "120000"):
            raise ExtractionError(
                "unsupported-tree", "submodules and special file modes are unsupported"
            )
        manifest[path] = (mode, sha)
    folded = [path.casefold() for path in manifest]
    if len(set(folded)) != len(folded):
        raise ExtractionError("unsupported-tree", "case-colliding base paths are unsupported")
    links = {path for path, (mode, _) in manifest.items() if mode == "120000"}
    for path in manifest:
        parents = path.split("/")[:-1]
        if any("/".join(parents[:index]) in links for index in range(1, len(parents) + 1)):
            raise ExtractionError("unsupported-tree", "base entry traverses a symlink")
    shas = sorted({sha for _, sha in manifest.values()})
    raw = git_process(
        git_dir,
        "cat-file",
        "--batch",
        input="".join(sha + "\n" for sha in shas).encode("ascii"),
        timeout=300,
    )
    if raw.returncode != 0:
        raise ExtractionError("git-error", raw.stderr.decode("utf-8", errors="replace")[:500])
    blobs = {}
    offset = 0
    for sha in shas:
        header_end = raw.stdout.find(b"\n", offset)
        if header_end < 0:
            raise ExtractionError("git-error", "truncated cat-file header")
        header = raw.stdout[offset:header_end].decode("ascii").split()
        if len(header) != 3 or header[:2] != [sha, "blob"] or not header[2].isdigit():
            raise ExtractionError("git-error", "missing or invalid base blob")
        size = int(header[2])
        offset = header_end + 1
        body = raw.stdout[offset : offset + size]
        offset += size
        if len(body) != size or raw.stdout[offset : offset + 1] != b"\n":
            raise ExtractionError("git-error", "truncated cat-file body")
        offset += 1
        blobs[sha] = body
    if offset != len(raw.stdout):
        raise ExtractionError("git-error", "unexpected cat-file output")
    contents = {path: blobs[sha] for path, (_, sha) in manifest.items()}
    for path in links:
        try:
            target = contents[path].decode("utf-8")
        except UnicodeError as exc:
            raise ExtractionError("unsupported-tree", "non-UTF-8 symlink target") from exc
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if (
            not target
            or target.startswith("/")
            or "\\" in target
            or "\x00" in target
            or normalized == ".."
            or normalized.startswith("../")
        ):
            raise ExtractionError("unsupported-tree", "base symlink escapes checkout")
    # Create links last. All link ancestors and destinations were validated.
    for path in sorted(set(manifest) - links):
        destination_path = destination / path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(contents[path])
        destination_path.chmod(0o755 if manifest[path][0] == "100755" else 0o644)
    for path in sorted(links):
        destination_path = destination / path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.symlink_to(contents[path].decode("utf-8"))


def check_applies(git_dir: str, base: str, diff: str) -> tuple[bool, str]:
    if not diff.strip():
        return False, "empty diff"
    with tempfile.TemporaryDirectory(prefix="oracle-apply-") as tmp:
        unpack_base(git_dir, base, Path(tmp))
        proc = git_process(
            None,
            "apply",
            "--check",
            "--no-index",
            "-",
            input=diff,
            encoding="utf-8",
            errors="replace",
            cwd=tmp,
            timeout=120,
        )
        return proc.returncode == 0, "" if proc.returncode == 0 else proc.stderr[:500]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="original task package dir")
    ap.add_argument("--git-dir", required=True, help="extracted image .git dir")
    ap.add_argument("--out", required=True, help="output dir for solution.patch")
    ap.add_argument("--tip", default=None, help="S2b tip sha (default: auto)")
    ap.add_argument(
        "--files", default=None, help="S2b comma-separated feature files (default: auto)"
    )
    ap.add_argument("--evidence", default=None, help="evidence.json path")
    args = ap.parse_args(argv)
    out = Path(args.out)
    evidence = extract(
        Path(args.task),
        args.git_dir,
        out,
        tip=args.tip,
        files=args.files.split(",") if args.files else None,
    )
    if args.evidence:
        ev_path = Path(args.evidence)
        inputs = (Path(args.task), Path(args.git_dir))
        if (
            not output_is_separate(ev_path, *inputs)
            or ev_path.resolve() == (out / "solution.patch").resolve()
        ):
            evidence.update(
                {
                    "status": "input-error",
                    "rationale": "evidence path must not overwrite input or solution files",
                }
            )
        elif ev_path.resolve() != (out / "evidence.json").resolve():
            try:
                ev_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_text(ev_path, json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            except OSError as exc:
                evidence.update({"status": "output-error", "rationale": str(exc)})
        if evidence.get("status") not in SUCCESS_STATUSES:
            solution = out / "solution.patch"
            if output_is_separate(out, *inputs):
                try:
                    if solution.exists() or solution.is_symlink():
                        solution.unlink()
                    atomic_text(
                        out / "evidence.json", json.dumps(evidence, indent=2, sort_keys=True) + "\n"
                    )
                except OSError as exc:
                    evidence.update({"status": "output-error", "rationale": str(exc)})
    print(json.dumps({k: evidence.get(k) for k in ("status", "rationale")}, indent=2))
    ok = evidence.get("status") in SUCCESS_STATUSES
    sol = out / "solution.patch"
    if ok and sol.is_file():
        print(f"wrote {sol}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
