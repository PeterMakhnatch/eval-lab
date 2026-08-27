"""CRAFT: the deterministic half of the task-corpus analyzer (WS-A).

Purpose: make eval design patterns queryable. `craft scan` reads task
directories and emits one `CraftRecord` per task to Parquet at
`derived/parquet/craft/`, from which `sql/craft_views.sql` builds the DuckDB
views the rest of WS-A/B/D consume.

Three rules shape every decision in this module, and they outrank coverage:

1. **Never guess a facet.** A field this module cannot establish from the bytes
   on disk is `None`, and the record says which fields those were
   (`unresolved_facets`) so a sparse column is legible instead of suspicious. A
   confidently wrong facet distribution would be worse than a sparse one,
   because the whole point of the corpus is to answer "what do these verifiers
   actually do" without a human re-reading 551 task directories.
2. **Structure, not text.** Verifier detection parses Python test modules with
   `ast`, lexes shell verifiers with `shlex` (comments stripped, so a `pytest`
   mentioned in a comment is not evidence), parses `package.json` as JSON, and
   matches candidate reference artifacts against the actual `tests/` file
   inventory. Substring search over source text is not used to decide a facet.
3. **Read-only over the corpus.** Scanning never writes inside a scanned root,
   and no task bytes are copied out: a record references its task by
   `task_ref` plus `task_digest`, never by content.

`craft classify` (the LLM facet pass) and `craft patterns` are deliberately not
here. Classify submits through the queue and must carry `purpose="craft"` once
`ExperimentSpec.purpose` becomes required (WS-E item 1); patterns depends on
classify for several facets. See `docs/craft.md` for the facet-by-facet split
between what this module determines and what genuinely needs the model.

Entry point: `python -m evallab.craft scan --tb3`. CLI wiring into
`evallab craft ...` is described in `docs/craft.md` and left undone here
because `cli.py` is leased to another mission this round.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import Field

from evallab.schemas import ContractModel
from evallab.storage.paths import derived_root_from_environment

#: A shell assignment, used to surface the value a verifier binds to a name.
#: `(?!=)` keeps `pkg==1.2.3` out: that is a pinned package, not an assignment.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?!=)(?P<value>.*)$", re.DOTALL)

#: Bumped whenever a facet's *meaning* or extraction rule changes, so a row
#: written by an older scanner is never silently compared with a newer one.
FACETS_SCHEMA_VERSION = "craft/1"

#: Number of tasks processed per classification batch.
#: Bounded at 10 to balance grouping efficiency against memory overhead and
#: prompt context window limits when classify is extended to LLM passes
#: (docs/platform-architecture.md §6).
DEFAULT_BATCH_SIZE: int = 10

#: Where the TB3 corpus lives on this workstation. Injectable (env var, then
#: `--tb3-root`) because `agents/CHECKS.md` forbids tests that depend on a
#: developer's host layout.
TB3_ROOT_ENV = "EVALLAB_TB3_ROOT"
DEFAULT_TB3_ROOT = Path("~/Developer/agent-evals/terminal-bench/tasks")

#: `source_repo` for the in-repository corpus. TB3 reports the name recorded in
#: its own `dataset.toml`, falling back to this shape when that file is absent.
LIBRARY_SOURCE_REPO = "eval-lab/library"
TB3_FALLBACK_SOURCE_REPO = "terminal-bench/terminal-bench"

#: A task directory is one that carries both files. Harbor's own layout adds
#: `environment/`, `tests/`, and `solution/`, but those are checked per facet
#: rather than required for discovery, so a partial task is still recorded (with
#: nulls) instead of vanishing from the corpus.
TASK_MANIFEST = "task.toml"
TASK_INSTRUCTION = "instruction.md"

InstructionStyle = Literal["imperative", "narrative", "spec"]
VerifierType = Literal["pytest", "diff", "golden_file", "judge", "hybrid"]
DifficultyMechanism = Literal["conceptual", "clerical", "volume", "mixed"]
AntiCheat = Literal["hidden_tests", "answer_outside_image", "digest_check", "process_check"]
BaseImagePin = Literal["digest", "tag", "bare"]

#: Verifier *mechanism* families. The first four are the spec's `verifier_type`
#: enum. The rest are mechanisms this corpus actually contains that the enum
#: cannot name, recorded so a null `verifier_type` is legible rather than blank:
#:
#: - `unit_js`   — a JS/TS test runner (vitest, playwright) drives the reward.
#: - `shell_only` — the verifier is shell and none of the four mechanisms were
#:   observed in it; in practice it compares an answer file against an expected
#:   value written into the script (198 `gpqa-diamond` shards do exactly that).
#: - `scorer_script` — a Python module that computes the reward itself, with no
#:   test framework, no committed reference, and no model call.
#:
#: `shell_only` and `scorer_script` are residual: they are recorded only when no
#: mechanism family fired, so they can never promote a task to `hybrid`.
_ENUM_FAMILIES = ("pytest", "diff", "golden_file", "judge")
_RESIDUAL_FAMILIES = ("shell_only", "scorer_script")

#: Client libraries whose presence in a verifier means a model decides the
#: reward. Import-level detection only: a verifier that shells out to an unknown
#: binary is not claimed either way.
_JUDGE_MODULES = frozenset(
    {"openai", "anthropic", "litellm", "cohere", "mistralai", "ollama", "dspy", "instructor"}
)

#: JS/TS test runners, read from a `package.json` dependency table rather than
#: from file names, so a stray `*.test.ts` fixture is not mistaken for a runner.
_JS_RUNNERS = frozenset(
    {"vitest", "jest", "mocha", "ava", "playwright", "@playwright/test", "@jest/globals"}
)

#: Extensions treated as verifier *code* (as opposed to fixtures or data).
_VERIFIER_CODE_SUFFIXES = frozenset({".py", ".sh", ".bash", ".ts", ".tsx", ".mjs", ".cjs", ".js"})

#: Lockfiles: any of these is, on its own, an exact-version dependency pin.
_LOCKFILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
        "composer.lock",
        "conda-lock.yml",
        "flake.lock",
    }
)

#: Installer front-ends and the pin separator a package argument must contain.
#: `None` marks an installer that is pinned by construction because it refuses
#: to resolve anything outside an existing lockfile.
#:
#: Longest prefix wins, so `uv pip install` is matched before `pip install`
#: would be, and `python3 -m pip install` is listed explicitly rather than
#: silently missed — 4 TB3 environments install that way.
_INSTALLERS: dict[tuple[str, ...], str | None] = {
    ("pip", "install"): "==",
    ("pip3", "install"): "==",
    ("python", "-m", "pip", "install"): "==",
    ("python3", "-m", "pip", "install"): "==",
    ("uv", "pip", "install"): "==",
    ("uv", "sync"): None,
    ("poetry", "install"): None,
    ("apt-get", "install"): "=",
    ("apt", "install"): "=",
    ("apk", "add"): "=",
    ("npm", "install"): "@",
    ("npm", "ci"): None,
    ("pnpm", "install"): None,
    ("yarn", "install"): None,
    ("cargo", "install"): "--version",
    ("conda", "install"): "=",
    ("conda", "create"): "=",
}

#: Arguments that name a manifest rather than a package: the pin, if any, lives
#: in the referenced file, which is scored as its own site.
_PIN_DEFERRING_SUFFIXES = (".txt", ".lock", ".toml", ".json", ".yml", ".yaml", ".cfg")

#: Extension → language. Deliberately excludes `Dockerfile`, YAML, JSON, and
#: Markdown: every Harbor environment has a Dockerfile, so recording it would
#: make the column constant, and the rest are data/config rather than the
#: implementation language of the environment. `.v` is resolved by sibling
#: evidence rather than guessed, because it is both Coq and Verilog.
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyx": "cython",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".cs": "csharp",
    ".rb": "ruby",
    ".pl": "perl",
    ".pm": "perl",
    ".php": "php",
    ".lua": "lua",
    ".sql": "sql",
    ".r": "r",
    ".jl": "julia",
    ".hs": "haskell",
    ".swift": "swift",
    ".ex": "elixir",
    ".exs": "elixir",
    ".clj": "clojure",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".lean": "lean",
    ".f90": "fortran",
    ".f": "fortran",
    ".bas": "basic",
    ".vba": "basic",
    ".asm": "assembly",
    ".s": "assembly",
    ".nix": "nix",
    ".sv": "verilog",
    ".vhd": "vhdl",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
}

#: Facets this module never fills, with the reason each is out of deterministic
#: reach. Recorded on every row so the null is self-explaining in the data, not
#: only in a document. Keep in sync with the table in `docs/craft.md`.
LLM_ONLY_FACETS: dict[str, str] = {
    "instruction_style": (
        "rhetorical register (imperative|narrative|spec) is a judgement about "
        "prose, and the labels are not mutually exclusive in this corpus: most "
        "instructions open with narrative scene-setting and close with a "
        "requirements list, so any verb-initial-sentence proxy mislabels the "
        "majority"
    ),
    "difficulty_mechanism": (
        "why a task is hard (conceptual|clerical|volume|mixed) requires reading "
        "the instruction against the solution; file counts and instruction "
        "length are correlates of size, not of mechanism"
    ),
}


class CraftRecord(ContractModel):
    """One task's deterministic facets.

    Fields follow `docs/build-plan.md` WS-A. Three columns are additions to that
    list, each forced by rule 1 or rule 2 above and marked here:

    - `verifier_signals` — the mechanism families actually observed. Without it,
      `verifier_type` is an unsourced label, and the `unit_js` mechanism the
      spec's enum cannot name would be invisible.
    - `unresolved_facets` — which columns are null *because they are
      undeterminable*, so `GROUP BY` can separate that from "absent from this
      task". Rule 1 requires the record itself to say so.
    - `base_image_pin` — how the environment's base image is referenced.
      `pinned_deps` is one bit about package versions and cannot also carry it,
      and reproducibility of the image is the question the column exists for.

    There is deliberately no timestamp column: a scan-time field would change
    every row on every run and destroy the idempotence guarantee.
    """

    task_ref: str
    source_repo: str
    version: str | None = None
    task_digest: str
    instruction_chars: int | None = None
    instruction_style: InstructionStyle | None = None
    env_n_files: int | None = None
    env_languages: list[str] = Field(default_factory=list)
    env_services_n: int | None = None
    env_multi_container: bool | None = None
    verifier_type: VerifierType | None = None
    anti_cheat: list[AntiCheat] = Field(default_factory=list)
    answer_hiding: str | None = None
    difficulty_mechanism: DifficultyMechanism | None = None
    human_minutes: int | None = None
    pinned_deps: bool | None = None
    facets_schema_version: str = FACETS_SCHEMA_VERSION
    verifier_signals: list[str] = Field(default_factory=list)
    unresolved_facets: list[str] = Field(default_factory=list)
    base_image_pin: BaseImagePin | None = None


CRAFT_SCHEMA = pa.schema(
    [
        pa.field("task_ref", pa.string(), nullable=False),
        pa.field("source_repo", pa.string(), nullable=False),
        pa.field("version", pa.string()),
        pa.field("task_digest", pa.string(), nullable=False),
        pa.field("instruction_chars", pa.int64()),
        pa.field("instruction_style", pa.string()),
        pa.field("env_n_files", pa.int64()),
        pa.field("env_languages", pa.list_(pa.string())),
        pa.field("env_services_n", pa.int64()),
        pa.field("env_multi_container", pa.bool_()),
        pa.field("verifier_type", pa.string()),
        pa.field("anti_cheat", pa.list_(pa.string())),
        pa.field("answer_hiding", pa.string()),
        pa.field("difficulty_mechanism", pa.string()),
        pa.field("human_minutes", pa.int64()),
        pa.field("pinned_deps", pa.bool_()),
        pa.field("facets_schema_version", pa.string(), nullable=False),
        pa.field("verifier_signals", pa.list_(pa.string())),
        pa.field("unresolved_facets", pa.list_(pa.string())),
        pa.field("base_image_pin", pa.string()),
    ]
)


# --------------------------------------------------------------------------- #
# digests and file inventory
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> Iterator[Path]:
    """Every path under `root`, in a stable relative-path order.

    Symlinks are yielded but never followed, so a loop cannot hang a scan and a
    link's *target* is what gets recorded.
    """
    yield from sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())


def task_digest(task_dir: Path) -> str:
    """A content digest over the whole task directory.

    This is craft's own digest, not the upstream `dataset.toml` pin: it has to
    cover corpora that have no upstream manifest, and it is the thing
    idempotence is defined against ("same digests ⇒ no row churn"). Directory
    entries and symlink targets are included so a moved or relinked file changes
    the digest even when no file content did.
    """
    lines: list[str] = []
    for path in _walk(task_dir):
        rel = path.relative_to(task_dir).as_posix()
        if path.is_symlink():
            lines.append(f"l {rel} {os.readlink(path)}")
        elif path.is_dir():
            lines.append(f"d {rel}")
        else:
            lines.append(f"f {rel} {path.stat().st_size} {_sha256_file(path)}")
    payload = "\n".join(lines).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


# --------------------------------------------------------------------------- #
# lexical helpers: shell without substring search
# --------------------------------------------------------------------------- #


def shell_words(text: str) -> tuple[str, ...]:
    """Lex a shell script into words, dropping comments.

    `shlex` is what makes shell evidence structural rather than textual: the
    verifier that documents "the pytest call lives in an `if` condition" in a
    comment must not thereby count as a pytest verifier, and 59 of the 74 TB3
    `test.sh` files mention their runner in prose.

    A `NAME=value` assignment additionally yields its right-hand side, so
    `REF="/opt/grader/reference.FCStd"` surfaces the path it binds. The token
    itself is always kept: splitting every `=` would shred `httpx==0.27.2` into
    two words and report a pinned dependency as unpinned, which an earlier
    revision of this function did to 20 TB3 environments.
    """
    lexer = shlex.shlex(text, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        # An unterminated quote means the tail is not lexable; keep nothing
        # rather than half a word.
        tokens = []
    words: list[str] = []
    for token in tokens:
        word = token.strip("\"'")
        if not word:
            continue
        words.append(word)
        match = _ASSIGNMENT.match(word)
        if match is not None:
            value = match.group("value").strip("\"'")
            if value and value != word:
                words.append(value)
    return tuple(words)


def _shell_commands(text: str) -> list[list[str]]:
    """Split a shell fragment into individual commands at `&&`, `;`, `|`."""
    commands: list[list[str]] = [[]]
    for word in shell_words(text):
        if word in {"&&", "||", ";", "|", "&"}:
            commands.append([])
        else:
            commands[-1].append(word)
    return [command for command in commands if command]


# --------------------------------------------------------------------------- #
# verifier detection (rule 2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PythonModuleFacts:
    """What `ast` says about one verifier module."""

    imports: frozenset[str]
    string_constants: frozenset[str]
    has_test_callable: bool


def python_module_facts(source: bytes) -> PythonModuleFacts | None:
    """Parse a verifier module; `None` when it does not parse as Python."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    imports: set[str] = set()
    constants: set[str] = set()
    has_test_callable = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                has_test_callable = True
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            has_test_callable = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            constants.add(node.value)
    return PythonModuleFacts(
        imports=frozenset(imports),
        string_constants=frozenset(constants),
        has_test_callable=has_test_callable,
    )


@dataclass(frozen=True)
class VerifierEvidence:
    """Mechanism families observed in a task's `tests/` tree, plus coverage."""

    families: frozenset[str]
    modules_parsed: int
    modules_unparsed: int
    reference_files: tuple[str, ...]
    signs_expectations: bool
    digest_check: bool
    process_check: bool


def inspect_verifier(tests_dir: Path) -> VerifierEvidence:
    """Classify a verifier by structure: AST for Python, lexing for shell.

    A *reference artifact* is a non-code file that actually exists under
    `tests/` and whose name appears as a path in the verifier's own Python
    string constants or shell words. Matching candidates against the real file
    inventory is what keeps this from degenerating into a keyword list: the
    evidence for `golden_file` is always a file a reviewer can open.
    """
    families: set[str] = set()
    parsed = 0
    unparsed = 0
    references: set[str] = set()
    signs = False
    digest_check = False
    process_check = False

    if not tests_dir.is_dir():
        return VerifierEvidence(frozenset(), 0, 0, (), False, False, False)

    files = [path for path in _walk(tests_dir) if path.is_file() and not path.is_symlink()]
    inventory = {
        path.name: path.relative_to(tests_dir).as_posix()
        for path in files
        if path.suffix not in _VERIFIER_CODE_SUFFIXES
    }

    for path in files:
        if path.suffix != ".py":
            continue
        facts = python_module_facts(path.read_bytes())
        if facts is None:
            unparsed += 1
            continue
        parsed += 1
        if facts.has_test_callable or "pytest" in facts.imports:
            families.add("pytest")
        if "difflib" in facts.imports:
            families.add("diff")
        if facts.imports & _JUDGE_MODULES:
            families.add("judge")
        if {"hashlib", "hmac"} & facts.imports:
            digest_check = True
        if "hmac" in facts.imports:
            signs = True
        if "psutil" in facts.imports:
            process_check = True
        for constant in facts.string_constants:
            if constant.startswith("/proc/") or constant == "/proc":
                process_check = True
            hit = inventory.get(constant.rsplit("/", 1)[-1])
            if hit is not None:
                references.add(hit)

    for path in files:
        if path.suffix not in {".sh", ".bash"}:
            continue
        words = set(shell_words(path.read_text(encoding="utf-8", errors="replace")))
        if any(word == "pytest" or word.endswith("/pytest") for word in words):
            families.add("pytest")
        if "diff" in words or "difftool" in words:
            families.add("diff")
        if words & {"sha256sum", "shasum", "md5sum", "b2sum", "sha1sum"}:
            digest_check = True
        if words & {"pgrep", "pidof", "lsof", "pmap", "pstree"}:
            process_check = True
        for word in words:
            hit = inventory.get(word.rsplit("/", 1)[-1])
            if hit is not None:
                references.add(hit)

    for path in files:
        if path.name != "package.json":
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        declared: set[str] = set()
        for table in ("dependencies", "devDependencies"):
            value = document.get(table)
            if isinstance(value, dict):
                declared.update(value)
        if declared & _JS_RUNNERS:
            families.add("unit_js")

    if references:
        families.add("golden_file")

    if not families:
        # Residual characterisation: say what the verifier *is* so the null
        # `verifier_type` carries evidence instead of silence.
        if parsed:
            families.add("scorer_script")
        elif any(path.suffix in {".sh", ".bash"} for path in files):
            families.add("shell_only")

    return VerifierEvidence(
        families=frozenset(families),
        modules_parsed=parsed,
        modules_unparsed=unparsed,
        reference_files=tuple(sorted(references)),
        signs_expectations=signs,
        digest_check=digest_check,
        process_check=process_check,
    )


def _verifier_type(families: frozenset[str]) -> VerifierType | None:
    """Map observed mechanism families onto the spec's enum, or refuse to.

    `hybrid` means "more than one mechanism", which stays true even when one of
    them is a mechanism the enum cannot name — `golden_file` + `unit_js` really
    is two mechanisms. A *single* mechanism the enum cannot name yields `None`:
    calling a vitest verifier `pytest`, or a shell answer-comparison
    `golden_file`, would be the confidently-wrong answer rule 1 forbids, and the
    gap is a finding about the enum rather than about the task.
    """
    mechanisms = families - frozenset(_RESIDUAL_FAMILIES)
    if len(mechanisms) > 1:
        return "hybrid"
    if len(mechanisms) == 1:
        only = next(iter(mechanisms))
        if only in _ENUM_FAMILIES:
            return only  # type: ignore[return-value]
    return None


# --------------------------------------------------------------------------- #
# environment facets
# --------------------------------------------------------------------------- #


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile instructions as `(verb, argument)`, continuations joined.

    A real (if small) parse of the Dockerfile grammar: comments dropped,
    backslash continuations folded, so `RUN pip install \\\n  foo==1.2` is one
    instruction rather than two unrelated lines.
    """
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].rstrip() + " "
            continue
        joined.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        joined.append(buffer.strip())
    instructions: list[tuple[str, str]] = []
    for line in joined:
        head, _, tail = line.partition(" ")
        if head:
            instructions.append((head.upper(), tail.strip()))
    return instructions


def base_image_pin(dockerfile: Path) -> BaseImagePin | None:
    """How the first `FROM` names its image: by digest, by tag, or bare."""
    if not dockerfile.is_file():
        return None
    for verb, argument in _dockerfile_instructions(
        dockerfile.read_text(encoding="utf-8", errors="replace")
    ):
        if verb != "FROM" or not argument:
            continue
        reference = argument.split()[0]
        if "@sha256:" in reference:
            return "digest"
        return "tag" if ":" in reference.rsplit("/", 1)[-1] else "bare"
    return None


def _requirements_pinned(path: Path) -> bool:
    specs = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]
    return bool(specs) and all("==" in spec for spec in specs)


def _install_pinned(command: Sequence[str], heads: tuple[str, ...], separator: str | None) -> bool:
    """True when every package argument of one installer call carries a pin.

    `separator is None` means the installer resolves only from a lockfile
    (`npm ci`, `pnpm install`, `uv sync`), so the call is pinned by
    construction. Flags are skipped, and an argument naming a manifest
    (`-r requirements.txt`, `pyproject.toml`) defers to that file, which
    `pinned_dependencies` scores as its own site — otherwise
    `pip install -r requirements.txt` would read as an unpinned package named
    `requirements.txt`.
    """
    if separator is None:
        return True
    arguments = list(command[len(heads) :])
    if separator == "--version":
        return "--version" in arguments
    packages = [
        word
        for word in arguments
        if not word.startswith("-")
        and not word.endswith(_PIN_DEFERRING_SUFFIXES)
        and word not in {".", "..", "/", "*"}
    ]
    if not packages:
        return True
    return all(separator in package for package in packages)


def _matching_installer(command: Sequence[str]) -> tuple[tuple[str, ...], str | None] | None:
    """The longest `_INSTALLERS` prefix this command starts with, if any."""
    best: tuple[tuple[str, ...], str | None] | None = None
    for heads, separator in _INSTALLERS.items():
        if tuple(command[: len(heads)]) != heads:
            continue
        if best is None or len(heads) > len(best[0]):
            best = (heads, separator)
    return best


def pinned_dependencies(environment: Path) -> bool | None:
    """Whether every dependency declaration in the environment pins versions.

    Three-valued on purpose. The spec types this `bool`, but a task whose
    environment declares no dependencies at all has no fact to report, and rule
    1 outranks the type: `None` there, rather than a `False` that reads as
    "declared and unpinned".

    Sites considered: lockfiles, `requirements*.txt`, and installer invocations
    inside `RUN` instructions (lexed, then split at `&&`/`;`/`|`). The base
    image is deliberately *not* a site here — it is reported separately as
    `base_image_pin`, because one boolean cannot answer both questions.
    """
    if not environment.is_dir():
        return None
    sites: list[bool] = []
    for path in _walk(environment):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in _LOCKFILES:
            sites.append(True)
            continue
        if path.name.startswith("requirements") and path.suffix == ".txt":
            sites.append(_requirements_pinned(path))
    for path in _walk(environment):
        if not path.is_file() or path.is_symlink():
            continue
        if not path.name.startswith("Dockerfile"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for verb, argument in _dockerfile_instructions(text):
            if verb != "RUN":
                continue
            for command in _shell_commands(argument):
                matched = _matching_installer(command)
                if matched is not None:
                    heads, separator = matched
                    sites.append(_install_pinned(command, heads, separator))
    if not sites:
        return None
    return all(sites)


def environment_services(environment: Path) -> int | None:
    """Number of declared environment containers.

    A `docker-compose.yaml` beside the environment Dockerfile is Harbor's
    multi-container declaration, so its `services` count is the answer. A lone
    Dockerfile is one service. Neither present ⇒ nothing observed ⇒ `None`.
    """
    for name in ("docker-compose.yaml", "docker-compose.yml"):
        candidate = environment / name
        if not candidate.is_file():
            continue
        try:
            document = yaml.safe_load(candidate.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError:
            return None
        if isinstance(document, dict):
            services = document.get("services")
            if isinstance(services, dict) and services:
                return len(services)
        return None
    if (environment / "Dockerfile").is_file():
        return 1
    return None


def environment_languages(environment: Path) -> list[str]:
    """Languages present in the environment, by extension, without guessing.

    `.v` is Coq only when a `_CoqProject` sits beside it and Verilog only when
    a SystemVerilog sibling does; otherwise it contributes nothing, because the
    extension alone does not distinguish them.
    """
    if not environment.is_dir():
        return []
    files = [path for path in _walk(environment) if path.is_file() and not path.is_symlink()]
    names = {path.name for path in files}
    languages: set[str] = set()
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".v":
            if "_CoqProject" in names or any(name.endswith(".vo") for name in names):
                languages.add("coq")
            elif any(name.endswith((".sv", ".vh")) for name in names):
                languages.add("verilog")
            continue
        language = _LANGUAGE_BY_SUFFIX.get(suffix)
        if language is not None:
            languages.add(language)
    return sorted(languages)


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskSource:
    """One corpus root, with the `source_repo` its records carry."""

    root: Path
    source_repo: str
    label: str


@dataclass(frozen=True)
class SkippedTask:
    """A discovered directory that could not be recorded, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class ScanResult:
    records: tuple[CraftRecord, ...]
    skipped: tuple[SkippedTask, ...]
    sources: tuple[TaskSource, ...]


def discover_tasks(root: Path) -> list[Path]:
    """Task directories under `root`, deepest-path-stable order.

    Structural definition: a directory holding both `task.toml` and
    `instruction.md`. Nothing else qualifies, which is why the TB3 root's
    `README.md` and `dataset.toml` are not tasks.
    """
    if not root.is_dir():
        return []
    manifests = sorted(
        (path.parent for path in root.rglob(TASK_MANIFEST) if path.is_file()),
        key=lambda item: item.as_posix(),
    )
    return [path for path in manifests if (path / TASK_INSTRUCTION).is_file()]


def _instruction_chars(instruction: Path) -> int | None:
    try:
        return len(instruction.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def _human_minutes(metadata: dict[str, Any]) -> int | None:
    hours = metadata.get("expert_time_estimate_hours")
    if isinstance(hours, bool) or not isinstance(hours, int | float):
        return None
    if hours <= 0:
        return None
    return round(hours * 60)


def _answer_hiding(*, isolated: bool, references: Sequence[str], signed: bool) -> str | None:
    """A deterministic composite of the hiding mechanisms actually observed.

    Enumerable rather than prose: each code names structure a reviewer can
    check. A description of *what specifically* is withheld ("the expected
    mutation report") is a reading task and stays with the LLM pass.
    """
    codes: list[str] = []
    if isolated:
        codes.append("separate_verifier_image")
    if references:
        codes.append("reference_artifact_in_tests")
    if signed:
        codes.append("signed_expectations")
    return "+".join(codes) if codes else None


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    """A manifest sub-table, or an empty one when it is missing or not a table."""
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def scan_task(task_dir: Path, source: TaskSource) -> CraftRecord:
    """Extract one task's deterministic facets. Never writes."""
    manifest = tomllib.loads((task_dir / TASK_MANIFEST).read_bytes().decode("utf-8"))
    task_table = _table(manifest, "task")
    metadata = _table(manifest, "metadata")
    verifier = _table(manifest, "verifier")

    declared_name = task_table.get("name")
    task_ref = (
        declared_name
        if isinstance(declared_name, str) and declared_name
        else task_dir.relative_to(source.root).as_posix()
    )

    version = task_table.get("version")
    environment = task_dir / "environment"
    evidence = inspect_verifier(task_dir / "tests")

    # `environment_mode = "separate"` is the declaration that the verifier is
    # built from `tests/` into its own image. `task_workbench.py:1503` treats
    # its absence as `verifier_not_isolated`, so absence is recorded as False
    # rather than unknown.
    isolated = verifier.get("environment_mode") == "separate"

    anti_cheat: list[AntiCheat] = []
    if isolated:
        anti_cheat.append("hidden_tests")
        # Entailed by the same evidence, not independently observed: the agent
        # image's build context is `environment/`, so in separate mode nothing
        # under `tests/` or `solution/` can enter it. A content-equality test
        # between the two trees was tried and rejected as unsound — fixture
        # applications and input data are legitimately byte-identical across
        # `environment/` and `tests/` in 164 file pairs of this corpus, so
        # equality proves duplication of inputs, not a leaked answer.
        anti_cheat.append("answer_outside_image")
    if evidence.digest_check:
        anti_cheat.append("digest_check")
    if evidence.process_check:
        anti_cheat.append("process_check")

    services = environment_services(environment)
    unresolved = sorted(LLM_ONLY_FACETS)
    verifier_type = _verifier_type(evidence.families)
    if verifier_type is None:
        unresolved.append("verifier_type")
    if services is None:
        unresolved.extend(("env_services_n", "env_multi_container"))
    if _human_minutes(metadata) is None:
        unresolved.append("human_minutes")
    if not isinstance(version, str) or not version:
        unresolved.append("version")

    return CraftRecord(
        task_ref=task_ref,
        source_repo=source.source_repo,
        version=version if isinstance(version, str) and version else None,
        task_digest=task_digest(task_dir),
        instruction_chars=_instruction_chars(task_dir / TASK_INSTRUCTION),
        instruction_style=None,
        env_n_files=(
            sum(1 for path in _walk(environment) if path.is_file())
            if environment.is_dir()
            else None
        ),
        env_languages=environment_languages(environment),
        env_services_n=services,
        env_multi_container=None if services is None else services > 1,
        verifier_type=verifier_type,
        anti_cheat=anti_cheat,
        answer_hiding=_answer_hiding(
            isolated=isolated,
            references=evidence.reference_files,
            signed=evidence.signs_expectations,
        ),
        difficulty_mechanism=None,
        human_minutes=_human_minutes(metadata),
        pinned_deps=pinned_dependencies(environment),
        verifier_signals=sorted(evidence.families),
        unresolved_facets=sorted(set(unresolved)),
        base_image_pin=base_image_pin(environment / "Dockerfile"),
    )


def scan_tasks_batch(
    task_dirs: Sequence[Path],
    source: TaskSource,
) -> tuple[list[CraftRecord], list[SkippedTask]]:
    """Extract deterministic facets for a batch of tasks from a single source."""
    records: list[CraftRecord] = []
    skipped: list[SkippedTask] = []
    for task_dir in task_dirs:
        try:
            records.append(scan_task(task_dir, source))
        except (tomllib.TOMLDecodeError, UnicodeError) as error:
            skipped.append(
                SkippedTask(
                    path=task_dir.as_posix(),
                    reason=f"{TASK_MANIFEST} does not parse: {type(error).__name__}",
                )
            )
        except OSError as error:
            skipped.append(SkippedTask(path=task_dir.as_posix(), reason=f"unreadable: {error}"))
    return records, skipped


def scan(
    sources: Sequence[TaskSource],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ScanResult:
    """Scan every source in batches, in source order then task order."""
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive; got {batch_size}")
    records: list[CraftRecord] = []
    skipped: list[SkippedTask] = []
    for source in sources:
        tasks = discover_tasks(source.root)
        for offset in range(0, len(tasks), batch_size):
            batch = tasks[offset : offset + batch_size]
            batch_records, batch_skipped = scan_tasks_batch(batch, source)
            records.extend(batch_records)
            skipped.extend(batch_skipped)
    records.sort(key=lambda item: (item.source_repo, item.task_ref))
    return ScanResult(tuple(records), tuple(skipped), tuple(sources))


# --------------------------------------------------------------------------- #
# corpus roots
# --------------------------------------------------------------------------- #


def tb3_root(explicit: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = environment.get(TB3_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_TB3_ROOT.expanduser().resolve()


def tb3_source(root: Path) -> TaskSource:
    """The TB3 corpus, naming itself from its own `dataset.toml` when present."""
    source_repo = TB3_FALLBACK_SOURCE_REPO
    manifest = root / "dataset.toml"
    if manifest.is_file():
        try:
            document = tomllib.loads(manifest.read_bytes().decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeError, OSError):
            document = {}
        dataset = document.get("dataset")
        if isinstance(dataset, dict) and isinstance(dataset.get("name"), str):
            source_repo = dataset["name"]
    return TaskSource(root=root, source_repo=source_repo, label="tb3")


def library_source(repo_root: Path) -> TaskSource:
    return TaskSource(
        root=(repo_root / "library").resolve(),
        source_repo=LIBRARY_SOURCE_REPO,
        label="library",
    )


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Parquet output and idempotence
# --------------------------------------------------------------------------- #


PARQUET_NAME = "craft.parquet"


def records_digest(records: Sequence[CraftRecord]) -> str:
    """Digest over the record set itself, independent of Parquet encoding.

    The witness for idempotence: two scans of an unchanged corpus print the
    same value. Comparing the Parquet bytes would also work today but would
    couple the guarantee to a pyarrow version.
    """
    payload = json.dumps(
        [record.model_dump(mode="json") for record in records],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class Churn:
    """Row-level difference between an existing Parquet table and a new scan."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    digest_changed: tuple[str, ...]
    facets_changed: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.digest_changed or self.facets_changed)

    def describe(self) -> str:
        return (
            f"added={len(self.added)} removed={len(self.removed)} "
            f"digest_changed={len(self.digest_changed)} "
            f"facets_changed={len(self.facets_changed)}"
        )


def _read_existing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    table = pq.read_table(path)
    rows = table.to_pylist()
    return {f"{row['source_repo']}\t{row['task_ref']}": row for row in rows}


def _row_key(record: CraftRecord) -> str:
    return f"{record.source_repo}\t{record.task_ref}"


def compute_churn(records: Sequence[CraftRecord], existing: dict[str, dict[str, Any]]) -> Churn:
    """What a re-scan would change, keyed by `(source_repo, task_ref)`.

    `facets_changed` is the interesting column: a row whose `task_digest` is
    unchanged but whose facets moved means the *scanner* changed its mind, which
    is a scanner bug or an intended `facets_schema_version` bump — never noise.
    """
    fresh = {_row_key(record): record for record in records}
    added = tuple(sorted(set(fresh) - set(existing)))
    removed = tuple(sorted(set(existing) - set(fresh)))
    digest_changed: list[str] = []
    facets_changed: list[str] = []
    for key in sorted(set(fresh) & set(existing)):
        old = existing[key]
        new = fresh[key].model_dump(mode="json")
        if old.get("task_digest") != new["task_digest"]:
            digest_changed.append(key)
            continue
        for column, value in new.items():
            previous = old.get(column)
            if isinstance(value, list):
                previous = list(previous) if previous is not None else []
            if previous != value:
                facets_changed.append(key)
                break
    return Churn(added, removed, tuple(digest_changed), tuple(facets_changed))


@dataclass(frozen=True)
class WriteResult:
    path: Path
    rows: int
    digest: str
    churn: Churn
    rewritten: bool


def write_records(records: Sequence[CraftRecord], output_root: Path) -> WriteResult:
    """Write `craft.parquet` under `output_root`, only when content changed.

    Skipping an identical write is part of the idempotence guarantee: a re-scan
    of an unchanged corpus leaves the file's bytes *and* its mtime alone, so
    nothing downstream sees a new artifact where there is no new fact.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / PARQUET_NAME
    churn = compute_churn(records, _read_existing(path))
    table = pa.Table.from_pylist(
        [record.model_dump(mode="json") for record in records], schema=CRAFT_SCHEMA
    )
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
    )
    rewritten = True
    if path.is_file() and _sha256_file(path) == _sha256_file(temporary):
        temporary.unlink()
        rewritten = False
    else:
        temporary.replace(path)
    return WriteResult(
        path=path,
        rows=len(records),
        digest=records_digest(records),
        churn=churn,
        rewritten=rewritten,
    )


def assert_output_outside_corpora(output_root: Path, sources: Iterable[TaskSource]) -> None:
    """Refuse to write inside a scanned corpus.

    The TB3 corpus is read-only by mission rule and the one-folder law forbids
    writing outside the repository; both are cheap to enforce here rather than
    to remember.
    """
    resolved = output_root.resolve()
    for source in sources:
        root = source.root.resolve()
        if resolved == root or resolved.is_relative_to(root):
            raise ValueError(
                f"refusing to write craft output to {resolved}: it is inside the "
                f"scanned corpus {root}, which craft treats as read-only"
            )


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def distribution(records: Sequence[CraftRecord]) -> dict[str, dict[str, int]]:
    """The counts WS-A exists to produce, computed from the records in memory.

    Mirrors what `sql/craft_views.sql` returns from Parquet; the CLI prints it
    so a scan is self-describing without a DuckDB round trip.
    """
    def tally(values: Iterable[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = "null" if value is None else str(value)
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    signals: dict[str, int] = {}
    anti_cheat: dict[str, int] = {}
    for record in records:
        for signal in record.verifier_signals:
            signals[signal] = signals.get(signal, 0) + 1
        for code in record.anti_cheat:
            anti_cheat[code] = anti_cheat.get(code, 0) + 1
    return {
        "verifier_type": tally(record.verifier_type for record in records),
        "verifier_signals": dict(sorted(signals.items(), key=lambda item: (-item[1], item[0]))),
        "env_multi_container": tally(record.env_multi_container for record in records),
        "pinned_deps": tally(record.pinned_deps for record in records),
        "base_image_pin": tally(record.base_image_pin for record in records),
        "human_minutes_present": tally(record.human_minutes is not None for record in records),
        "anti_cheat": dict(sorted(anti_cheat.items(), key=lambda item: (-item[1], item[0]))),
        "answer_hiding": tally(record.answer_hiding for record in records),
    }


def _render(result: ScanResult, write: WriteResult, sources: Sequence[TaskSource]) -> str:
    lines = [f"craft scan {FACETS_SCHEMA_VERSION}"]
    for source in sources:
        count = sum(1 for record in result.records if record.source_repo == source.source_repo)
        lines.append(f"  source {source.label}: {count} tasks from {source.root}")
    lines.append(f"  parquet: {write.path} rows={write.rows} rewritten={write.rewritten}")
    lines.append(f"  records_digest: {write.digest}")
    lines.append(f"  churn: {write.churn.describe()}")
    for skipped in result.skipped:
        lines.append(f"  skipped: {skipped.path} — {skipped.reason}")
    for facet, counts in distribution(result.records).items():
        rendered = " ".join(f"{key}={value}" for key, value in counts.items())
        lines.append(f"  {facet}: {rendered}")
    for facet, reason in sorted(LLM_ONLY_FACETS.items()):
        lines.append(f"  null-by-design {facet}: {reason}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.craft",
        description="Deterministic task-corpus scanner (WS-A, scan half only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="extract facets to Parquet; no model calls")
    scan_parser.add_argument("directories", nargs="*", type=Path, help="extra corpus roots")
    scan_parser.add_argument("--tb3", action="store_true", help="scan the TB3 corpus")
    scan_parser.add_argument(
        "--all-local", action="store_true", help="scan the TB3 corpus and in-repo library/"
    )
    scan_parser.add_argument("--library", action="store_true", help="scan in-repo library/")
    scan_parser.add_argument("--tb3-root", type=Path, default=None, help="override the TB3 root")
    scan_parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"number of tasks per classification batch (default: {DEFAULT_BATCH_SIZE})",
    )
    scan_parser.add_argument(
        "--out", type=Path, default=None, help="derived Parquet root (default: derived/parquet)"
    )
    scan_parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    return parser


def _sources_from_args(args: argparse.Namespace, repo_root: Path) -> list[TaskSource]:
    sources: list[TaskSource] = []
    if args.tb3 or args.all_local:
        sources.append(tb3_source(tb3_root(args.tb3_root)))
    if args.library or args.all_local:
        sources.append(library_source(repo_root))
    for directory in args.directories:
        resolved = directory.expanduser().resolve()
        sources.append(
            TaskSource(root=resolved, source_repo=resolved.name, label=resolved.as_posix())
        )
    return sources


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = repository_root()
    sources = _sources_from_args(args, repo_root)
    if not sources:
        print(
            "craft scan: nothing to scan — pass --tb3, --library, --all-local, or a directory",
            file=sys.stderr,
        )
        return 2
    missing = [source for source in sources if not source.root.is_dir()]
    if missing:
        for source in missing:
            print(f"craft scan: corpus root not found: {source.root}", file=sys.stderr)
        return 2

    output_root = (
        args.out.expanduser().resolve()
        if args.out is not None
        else derived_root_from_environment(repo_root) / "craft"
    )
    assert_output_outside_corpora(output_root, sources)
    result = scan(sources, batch_size=args.batch_size)
    write = write_records(result.records, output_root)
    if args.json:
        print(
            json.dumps(
                {
                    "facets_schema_version": FACETS_SCHEMA_VERSION,
                    "parquet": write.path.as_posix(),
                    "rows": write.rows,
                    "rewritten": write.rewritten,
                    "records_digest": write.digest,
                    "churn": {
                        "added": list(write.churn.added),
                        "removed": list(write.churn.removed),
                        "digest_changed": list(write.churn.digest_changed),
                        "facets_changed": list(write.churn.facets_changed),
                    },
                    "skipped": [
                        {"path": item.path, "reason": item.reason} for item in result.skipped
                    ],
                    "distribution": distribution(result.records),
                    "null_by_design": LLM_ONLY_FACETS,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render(result, write, sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
