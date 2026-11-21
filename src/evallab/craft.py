from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import Field

from evallab.execution_contracts import DEEPSEEK_MODEL_SELECTOR, TERMINUS_AGENT, new_ulid
from evallab.schemas import ContractModel
from evallab.storage.paths import derived_root_from_environment

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?!=)(?P<value>.*)$", re.DOTALL)

FACETS_SCHEMA_VERSION = "craft/1"

DEFAULT_BATCH_SIZE: int = 10

TB3_ROOT_ENV = "EVALLAB_TB3_ROOT"
DEFAULT_TB3_ROOT = Path("~/Developer/lab-extras/data/agent-evals/terminal-bench/tasks")

LIBRARY_SOURCE_REPO = "eval-lab/library"
TB3_FALLBACK_SOURCE_REPO = "terminal-bench/terminal-bench"

TB4_ROOT_ENV = "EVALLAB_TB4_ROOT"
DEFAULT_TB4_ROOT = Path("~/Developer/lab-extras/data/agent-evals/terminal-bench-4/tasks")

TB4_VERSION = "4.0.0"
TB4_PIN_TAG = "v4.0.0"
TB4_PIN_COMMIT = "452bf30"

TB4_DATASET_NAME = "terminal-bench/terminal-bench"

TB4_FALLBACK_SOURCE_REPO = "terminal-bench/terminal-bench@4.0.0"

TB4_DATASET_REF = "terminal-bench/terminal-bench@4.0.0"

FLOATING_REF_TOKENS = frozenset({"latest", "head", "main", "master"})

MIGRATION_RECORD = Path(__file__).resolve().parent / "data" / "terminal-bench-4-migration.json"

TB4_TIMEOUT_SECONDS: int = 28_800

ZAI_ALLOWED_MODELS: frozenset[str] = frozenset(
    {"zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"}
)
ZAI_DEFAULT_MODEL: str = "zai-coding-plan/glm-5.3"
ZAI_DEFAULT_AGENT: str = "zai-opencode"
ZAI_MODEL_PREFIX: str = "zai-coding-plan/"

DEEPSEEK_ALLOWED_AGENTS: frozenset[str] = frozenset(
    {"mini-swe-agent", "evallab.harbor_dsh:DeepSeekHarnessAgent"}
)

ZAI_OPENAPI_MODEL_SELECTOR: str = "zai/glm-5.3-flash"
ZAI_OPENAPI_ALLOWED_AGENTS: frozenset[str] = frozenset({"mini-swe-agent", TERMINUS_AGENT})

TB4_GPU_TASK_REFS: frozenset[str] = frozenset(
    {
        "terminal-bench/fp8-rmsnorm-gemm",
        "terminal-bench/jax-speedrun-gpu",
        "terminal-bench/math-eval-grader",
    }
)

TB4_REMOTE_ENVIRONMENTS: dict[str, tuple[str, ...]] = {
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
    "beam": ("BEAM_TOKEN",),
    "daytona": ("DAYTONA_API_KEY",),
}
TB4_DEFAULT_REMOTE_ENVIRONMENT: str = "modal"

TASK_MANIFEST = "task.toml"
TASK_INSTRUCTION = "instruction.md"

InstructionStyle = Literal["imperative", "narrative", "spec"]
VerifierType = Literal["pytest", "diff", "golden_file", "judge", "hybrid"]
DifficultyMechanism = Literal["conceptual", "clerical", "volume", "mixed"]
AntiCheat = Literal["hidden_tests", "answer_outside_image", "digest_check", "process_check"]
BaseImagePin = Literal["digest", "tag", "bare"]

_ENUM_FAMILIES = ("pytest", "diff", "golden_file", "judge")
_RESIDUAL_FAMILIES = ("shell_only", "scorer_script")

_JUDGE_MODULES = frozenset(
    {"openai", "anthropic", "litellm", "cohere", "mistralai", "ollama", "dspy", "instructor"}
)

_JS_RUNNERS = frozenset(
    {"vitest", "jest", "mocha", "ava", "playwright", "@playwright/test", "@jest/globals"}
)

_VERIFIER_CODE_SUFFIXES = frozenset({".py", ".sh", ".bash", ".ts", ".tsx", ".mjs", ".cjs", ".js"})

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

_PIN_DEFERRING_SUFFIXES = (".txt", ".lock", ".toml", ".json", ".yml", ".yaml", ".cfg")

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())


def task_digest(task_dir: Path) -> str:
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


def shell_words(text: str) -> tuple[str, ...]:
    lexer = shlex.shlex(text, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
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
    commands: list[list[str]] = [[]]
    for word in shell_words(text):
        if word in {"&&", "||", ";", "|", "&"}:
            commands.append([])
        else:
            commands[-1].append(word)
    return [command for command in commands if command]


@dataclass(frozen=True)
class PythonModuleFacts:

    imports: frozenset[str]
    string_constants: frozenset[str]
    has_test_callable: bool


def python_module_facts(source: bytes) -> PythonModuleFacts | None:
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

    families: frozenset[str]
    modules_parsed: int
    modules_unparsed: int
    reference_files: tuple[str, ...]
    signs_expectations: bool
    digest_check: bool
    process_check: bool


def inspect_verifier(tests_dir: Path) -> VerifierEvidence:
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
    mechanisms = families - frozenset(_RESIDUAL_FAMILIES)
    if len(mechanisms) > 1:
        return "hybrid"
    if len(mechanisms) == 1:
        only = next(iter(mechanisms))
        if only in _ENUM_FAMILIES:
            return only  # type: ignore[return-value]
    return None


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
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
    best: tuple[tuple[str, ...], str | None] | None = None
    for heads, separator in _INSTALLERS.items():
        if tuple(command[: len(heads)]) != heads:
            continue
        if best is None or len(heads) > len(best[0]):
            best = (heads, separator)
    return best


def pinned_dependencies(environment: Path) -> bool | None:
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


@dataclass(frozen=True)
class TaskSource:

    root: Path
    source_repo: str
    label: str


@dataclass(frozen=True)
class SkippedTask:

    path: str
    reason: str


@dataclass(frozen=True)
class ScanResult:
    records: tuple[CraftRecord, ...]
    skipped: tuple[SkippedTask, ...]
    sources: tuple[TaskSource, ...]


def discover_tasks(root: Path) -> list[Path]:
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
    codes: list[str] = []
    if isolated:
        codes.append("separate_verifier_image")
    if references:
        codes.append("reference_artifact_in_tests")
    if signed:
        codes.append("signed_expectations")
    return "+".join(codes) if codes else None


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    return value if isinstance(value, dict) else {}


def scan_task(task_dir: Path, source: TaskSource) -> CraftRecord:
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

    isolated = verifier.get("environment_mode") == "separate"

    anti_cheat: list[AntiCheat] = []
    if isolated:
        anti_cheat.append("hidden_tests")
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


def _root_from_config(
    *,
    explicit: Path | None,
    environ: Mapping[str, str] | None,
    env_var: str,
    default: Path,
) -> Path:
    environment = os.environ if environ is None else environ
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = environment.get(env_var)
    if configured:
        return Path(configured).expanduser().resolve()
    return default.expanduser().resolve()


def tb3_root(explicit: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    return _root_from_config(
        explicit=explicit, environ=environ, env_var=TB3_ROOT_ENV, default=DEFAULT_TB3_ROOT
    )


def tb4_root(explicit: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    return _root_from_config(
        explicit=explicit, environ=environ, env_var=TB4_ROOT_ENV, default=DEFAULT_TB4_ROOT
    )


def tb3_source(root: Path) -> TaskSource:
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


def tb4_source(root: Path) -> TaskSource:
    tasks_root = root / "tasks"
    discovery_root = tasks_root if tasks_root.is_dir() else root
    return TaskSource(root=discovery_root, source_repo=TB4_FALLBACK_SOURCE_REPO, label="tb4")


def library_source(repo_root: Path) -> TaskSource:
    return TaskSource(
        root=(repo_root / "library").resolve(),
        source_repo=LIBRARY_SOURCE_REPO,
        label="library",
    )


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_migration_record(path: Path | None = None) -> dict[str, Any]:
    record_path = path or MIGRATION_RECORD
    try:
        with record_path.open(encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"cannot read TB4 migration record {record_path}: {error}") from error
    if not isinstance(record, dict):
        raise RuntimeError(f"TB4 migration record {record_path} is not a JSON object")
    return record


def _dataset_identity(root: Path) -> dict[str, str]:
    manifest = root / "dataset.toml"
    if not manifest.is_file():
        return {}
    try:
        document = tomllib.loads(manifest.read_bytes().decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeError, OSError):
        return {}
    dataset = document.get("dataset")
    if not isinstance(dataset, dict):
        return {}
    identity: dict[str, str] = {}
    if isinstance(dataset.get("name"), str):
        identity["name"] = dataset["name"]
    if isinstance(dataset.get("version"), str):
        identity["version"] = dataset["version"]
    return identity


def _is_floating_ref(ref: str) -> bool:
    base = ref.split("@", 1)[0].rstrip("/")
    token = base.rsplit("/", 1)[-1] if "/" in base else base
    return token.lower() in FLOATING_REF_TOKENS or "@latest" in ref or "@head" in ref


def _version_from_ref(ref: str) -> str | None:
    if "@" not in ref:
        return None
    return ref.split("@", 1)[1].strip()


def _git_pin_ref(root: Path) -> str | None:
    try:
        described = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if described.returncode == 0 and described.stdout.strip() == TB4_PIN_TAG:
            return TB4_DATASET_REF
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head.returncode == 0 and head.stdout.strip() == TB4_PIN_COMMIT:
            return TB4_DATASET_REF
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def resolve_tb4_ref(root: Path, ref: str | None = None) -> str:
    identity = _dataset_identity(root)
    name = identity.get("name")
    if not name:
        if _git_pin_ref(root) == TB4_DATASET_REF:
            name = TB4_DATASET_NAME
        else:
            raise ValueError(
                f"not a Terminal-Bench checkout: {root} declares no dataset name"
            )
    if name != TB4_DATASET_NAME:
        raise ValueError(f"wrong dataset: expected {TB4_DATASET_NAME!r}, got {name!r} at {root}")
    effective = ref
    if effective is None and identity.get("version"):
        effective = f"{name}@{identity['version']}"
    if effective is None:
        effective = _git_pin_ref(root)
    if effective is None:
        raise ValueError(
            f"unpinned Terminal-Bench checkout at {root}: no version declared and no "
            f"v4.0.0 tag; pass --ref {TB4_DATASET_REF}"
        )
    if _is_floating_ref(effective):
        raise ValueError(f"floating ref refused: {effective!r} (must pin {TB4_DATASET_REF})")
    version = _version_from_ref(effective)
    if version != TB4_VERSION:
        raise ValueError(
            f"wrong version: expected {TB4_VERSION}, got {version or effective!r} ({effective!r})"
        )
    return effective


def plan_tb4(
    tb4_path: Path,
    *,
    ref: str | None = None,
    tb3_path: Path | None = None,
) -> dict[str, Any]:
    if not tb4_path.is_dir():
        raise FileNotFoundError(f"TB4 corpus root not found: {tb4_path}")
    effective_ref = resolve_tb4_ref(tb4_path, ref=ref)
    record = load_migration_record()

    result = scan([tb4_source(tb4_path)])
    actual_refs = [r.task_ref for r in result.records]
    expected_refs = list(record["expected_inventory"])

    discrepancies: list[str] = []
    if len(actual_refs) != len(expected_refs):
        discrepancies.append(f"task count {len(actual_refs)} != expected {len(expected_refs)}")
    actual_set = set(actual_refs)
    expected_set = set(expected_refs)
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    if missing:
        discrepancies.append(f"{len(missing)} expected task(s) missing: {missing}")
    if unexpected:
        discrepancies.append(f"{len(unexpected)} unexpected task(s): {unexpected}")

    removed_record = list(record["removed_tasks"])
    fixed_record = list(record["fixed_tasks"])

    live: dict[str, Any] = {}
    if tb3_path is not None and tb3_path.is_dir():
        tb3_refs = {r.task_ref for r in scan([tb3_source(tb3_path)]).records}
        removed_detected = sorted(tb3_refs - actual_set)
        added_in_tb4 = sorted(actual_set - tb3_refs)
        live = {
            "tb3_tasks": len(tb3_refs),
            "removed_detected": removed_detected,
            "removed_matches_record": set(removed_detected) == set(removed_record),
            "added_in_tb4": added_in_tb4,
        }

    return {
        "command": "craft plan",
        "dataset_ref": effective_ref,
        "pin": {
            "tag": TB4_PIN_TAG,
            "commit": TB4_PIN_COMMIT,
            "license": record.get("license"),
            "schema_unchanged": record.get("schema_unchanged"),
        },
        "expected_tasks": len(expected_refs),
        "actual_tasks": len(actual_refs),
        "expected_inventory": expected_refs,
        "actual_inventory": actual_refs,
        "removed_tasks": removed_record,
        "fixed_tasks": fixed_record,
        "v3_non_comparable": record.get("v3_non_comparable", False),
        "live": live,
        "discrepancies": discrepancies,
    }


def _render_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"craft plan {FACETS_SCHEMA_VERSION}",
        f"  dataset_ref: {plan['dataset_ref']}",
        f"  pin: tag={plan['pin']['tag']} commit={plan['pin']['commit']} "
        f"license={plan['pin']['license']}",
        f"  tasks: expected={plan['expected_tasks']} actual={plan['actual_tasks']}",
    ]
    live = plan["live"]
    if live:
        lines.append(f"  tb3: {live['tb3_tasks']} tasks")
        removed = ", ".join(live["removed_detected"]) or "none"
        lines.append(f"  removed (detected live): {removed}")
        if live.get("added_in_tb4"):
            lines.append(f"  added in tb4: {', '.join(live['added_in_tb4'])}")
        lines.append(f"  removed matches record: {live['removed_matches_record']}")
    lines.append(f"  removed (record): {', '.join(plan['removed_tasks'])}")
    lines.append(f"  fixed (record): {', '.join(plan['fixed_tasks'])}")
    lines.append(f"  v3 non-comparable: {plan['v3_non_comparable']}")
    for discrepancy in plan["discrepancies"]:
        lines.append(f"  discrepancy: {discrepancy}")
    return "\n".join(lines)


def deterministic_tb4_task_id(dataset_ref: str, task_ref: str) -> str:
    digest = hashlib.sha256(f"{dataset_ref}\0{task_ref}".encode()).digest()
    millis = int.from_bytes(digest[:6], "big") & ((1 << 48) - 1)
    randomness = int.from_bytes(digest[6:16], "big") & ((1 << 80) - 1)
    return new_ulid(timestamp_ms=millis, randomness=randomness)


def compile_tb4(
    tb4_path: Path,
    *,
    ref: str | None = None,
    out: Path | None = None,
    model: str = ZAI_DEFAULT_MODEL,
    agent: str = ZAI_DEFAULT_AGENT,
    tb3_path: Path | None = None,
    include_tasks: Sequence[str] | None = None,
    remote_environment: str = TB4_DEFAULT_REMOTE_ENVIRONMENT,
) -> dict[str, Any]:
    if not tb4_path.is_dir():
        raise FileNotFoundError(f"TB4 corpus root not found: {tb4_path}")

    if tb3_path is not None:
        raise ValueError(
            "accidental TB3/TB4 aggregation refused: TB3 and TB4 cohorts are non-comparable "
            "and cannot be aggregated or mixed into a single compile pass"
        )

    if "highspeed" in model.lower():
        raise ValueError(
            f"invalid model selector {model!r}: highspeed models are refused at compile time (fail closed)"
        )
    if model in ZAI_ALLOWED_MODELS and model.startswith(ZAI_MODEL_PREFIX) and agent == ZAI_DEFAULT_AGENT:
        provider_family = "zai"
    elif model == DEEPSEEK_MODEL_SELECTOR and agent in DEEPSEEK_ALLOWED_AGENTS:
        provider_family = "deepseek"
    elif model == ZAI_OPENAPI_MODEL_SELECTOR and agent in ZAI_OPENAPI_ALLOWED_AGENTS:
        provider_family = "zai-openapi"
    else:
        raise ValueError(
            f"invalid model selector {model!r} with agent {agent!r}: model must be one of {sorted(ZAI_ALLOWED_MODELS)} "
            f"or {DEEPSEEK_MODEL_SELECTOR!r} paired with an agent in {sorted(DEEPSEEK_ALLOWED_AGENTS)} "
            f"or {ZAI_OPENAPI_MODEL_SELECTOR!r} paired with an agent in {sorted(ZAI_OPENAPI_ALLOWED_AGENTS)}; "
            "highspeed, non-Z.ai, and unapproved DeepSeek configurations are refused at compile time (fail closed)"
        )

    if remote_environment != "docker" and remote_environment not in TB4_REMOTE_ENVIRONMENTS:
        raise ValueError(
            f"invalid remote_environment {remote_environment!r}: must be 'docker' or one of "
            f"{sorted(TB4_REMOTE_ENVIRONMENTS)} (HAR-62 backend routing)"
        )

    effective_ref = resolve_tb4_ref(tb4_path, ref=ref)
    record = load_migration_record()

    result = scan([tb4_source(tb4_path)])
    actual_records = {r.task_ref: r for r in result.records}
    actual_refs = sorted(actual_records.keys())
    expected_refs = list(record["expected_inventory"])

    if len(actual_refs) != len(expected_refs):
        raise ValueError(
            f"task count drift: actual {len(actual_refs)} != expected {len(expected_refs)}"
        )
    missing = sorted(set(expected_refs) - set(actual_refs))
    unexpected = sorted(set(actual_refs) - set(expected_refs))
    if missing:
        raise ValueError(f"inventory drift: missing expected task(s): {missing}")
    if unexpected:
        raise ValueError(f"inventory drift: unexpected task(s): {unexpected}")

    manifest_bytes = json.dumps(
        {
            "dataset_ref": effective_ref,
            "expected_inventory": expected_refs,
            "pin": {
                "commit": TB4_PIN_COMMIT,
                "tag": TB4_PIN_TAG,
            },
            "resource_contract": record.get("resource_contract"),
            "task_count": len(expected_refs),
            "versions": {
                "from": record.get("from_version"),
                "to": record.get("to_version"),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    expected_set = set(expected_refs)
    if include_tasks is not None:
        selected_set: set[str] = set()
        for item in include_tasks:
            if item in expected_set:
                selected_set.add(item)
            elif f"terminal-bench/{item}" in expected_set:
                selected_set.add(f"terminal-bench/{item}")
            else:
                raise ValueError(
                    f"unknown include_tasks entry {item!r}: not found in expected TB4 inventory"
                )
    else:
        selected_set = expected_set

    if remote_environment == "docker":
        gpu_selected = sorted(TB4_GPU_TASK_REFS & selected_set)
        if gpu_selected:
            raise ValueError(
                "local docker cannot execute the H100 task class (no CUDA on this host): "
                f"{gpu_selected} require a remote backend — pass a Harbor environment such as "
                f"{TB4_DEFAULT_REMOTE_ENVIRONMENT!r} instead of silently degrading or swapping "
                "the task (HAR-62)"
            )

    task_entries: list[dict[str, Any]] = []
    current_digests: dict[str, str] = {}
    for task_ref in expected_refs:
        rec = actual_records[task_ref]
        task_id = deterministic_tb4_task_id(effective_ref, task_ref)
        task_digest_val = rec.task_digest
        current_digests[task_ref] = task_digest_val
        if task_ref in selected_set:
            is_gpu = task_ref in TB4_GPU_TASK_REFS
            task_entries.append(
                {
                    "task_id": task_id,
                    "task_ref": task_ref,
                    "task_digest": task_digest_val,
                    "timeout_seconds": TB4_TIMEOUT_SECONDS,
                    "agent": agent,
                    "model": model,
                    "environment": remote_environment if is_gpu else "docker",
                    **({"gpu_types": ["H100"]} if is_gpu else {}),
                }
            )

    if out is not None and out.is_file():
        try:
            prior_plan = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(prior_plan, dict) and "tasks" in prior_plan:
                prior_digests = prior_plan.get("task_digests")
                if prior_digests is None:
                    prior_digests = {
                        t["task_ref"]: t.get("task_digest")
                        for t in prior_plan["tasks"]
                        if isinstance(t, dict) and "task_ref" in t
                    }
                if not isinstance(prior_digests, dict) or set(prior_digests) != expected_set:
                    raise ValueError(
                        f"upstream task set drift detected against prior plan at {out}: "
                        "full-inventory task digests required"
                    )
                drifted: list[str] = []
                for t_ref, cur_dig in current_digests.items():
                    prior_dig = prior_digests[t_ref]
                    if prior_dig != cur_dig:
                        drifted.append(f"{t_ref} (prior={prior_dig}, current={cur_dig})")
                if drifted:
                    raise ValueError(
                        f"upstream digest drift detected for {len(drifted)} task(s) against prior plan at {out}: {drifted}"
                    )
        except (json.JSONDecodeError, UnicodeError):
            pass

    allowed_pairs = sorted(
        [[m, ZAI_DEFAULT_AGENT] for m in ZAI_ALLOWED_MODELS]
        + [[DEEPSEEK_MODEL_SELECTOR, a] for a in DEEPSEEK_ALLOWED_AGENTS]
        + [[ZAI_OPENAPI_MODEL_SELECTOR, a] for a in ZAI_OPENAPI_ALLOWED_AGENTS]
    )
    plan: dict[str, Any] = {
        "plan_version": "tb4-job-plan/1",
        "command": "craft compile",
        "dataset_ref": effective_ref,
        "source_identity": record.get("source_identity", TB4_FALLBACK_SOURCE_REPO),
        "versions": {
            "from": record.get("from_version", "3.0.0"),
            "to": record.get("to_version", "4.0.0"),
        },
        "pin": {
            "tag": TB4_PIN_TAG,
            "commit": TB4_PIN_COMMIT,
            "license": record.get("license", "Apache-2.0"),
            "schema_unchanged": record.get("schema_unchanged", True),
        },
        "resource_contract": record.get(
            "resource_contract",
            f"flat 8-hour agent timeout on all {len(expected_refs)} tasks",
        ),
        "timeout_seconds": TB4_TIMEOUT_SECONDS,
        "task_count": len(expected_refs),
        "selected_task_count": len(task_entries),
        "non_comparable": record.get("v3_non_comparable", True),
        "comparability_note": record.get("comparability_note", ""),
        "floating_refs_forbidden": record.get("floating_refs_forbidden", True),
        "provider": {
            "provider_family": provider_family,
            "agent": agent,
            "selected_agent": agent,
            "model_prefix": (
                "zai/" if provider_family == "zai-openapi" else ZAI_MODEL_PREFIX
            ),
            "allowed_models": sorted(ZAI_ALLOWED_MODELS | {ZAI_OPENAPI_MODEL_SELECTOR}),
            "selected_model": model,
            "allowed_pairs": allowed_pairs,
            "highspeed": "refused",
        },
        "environment_routing": {
            "default": "docker",
            "remote_for_gpu": remote_environment,
            "gpu_task_refs": sorted(TB4_GPU_TASK_REFS),
            "required_credentials": {
                name: list(envs) for name, envs in TB4_REMOTE_ENVIRONMENTS.items()
            },
            "note": (
                "63 CPU tasks run on local Docker Desktop; the H100 class routes to "
                f"{remote_environment!r} (fail-closed on its credential env before dispatch). "
                "Routing is representation only: queued execution keeps the standing "
                "cloud_or_remote_environment admission policy, and no spend is authorized "
                "by compilation (HAR-62)."
            ),
        },
        "refuses": {
            "tb3_mixing": True,
            "floating_refs": True,
            "digest_drift": True,
            "gpu_on_local_docker": True,
        },
        "manifest_digest": manifest_digest,
        "task_digests": current_digests,
        "tasks": task_entries,
    }

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        rendered_json = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        out.write_text(rendered_json, encoding="utf-8")
        plan["out_path"] = str(out)

    return plan


def _render_compile(plan: dict[str, Any]) -> str:
    lines = [
        f"craft compile {FACETS_SCHEMA_VERSION}",
        f"  dataset_ref: {plan['dataset_ref']}",
        f"  pin: tag={plan['pin']['tag']} commit={plan['pin']['commit']} license={plan['pin'].get('license')}",
        f"  tasks: {plan['task_count']} tasks, flat timeout {plan['timeout_seconds']}s (8h)",
        f"  provider: agent={plan['provider']['agent']} model={plan['provider']['selected_model']}",
        f"  v3 non-comparable: {plan['non_comparable']}",
        f"  manifest_digest: {plan['manifest_digest']}",
    ]
    if "out_path" in plan:
        lines.append(f"  plan written: {plan['out_path']}")
    return "\n".join(lines)


PARQUET_NAME = "craft.parquet"


def records_digest(records: Sequence[CraftRecord]) -> str:
    payload = json.dumps(
        [record.model_dump(mode="json") for record in records],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class Churn:

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
    resolved = output_root.resolve()
    for source in sources:
        root = source.root.resolve()
        if resolved == root or resolved.is_relative_to(root):
            raise ValueError(
                f"refusing to write craft output to {resolved}: it is inside the "
                f"scanned corpus {root}, which craft treats as read-only"
            )


def distribution(records: Sequence[CraftRecord]) -> dict[str, dict[str, int]]:

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
    scan_parser.add_argument("--tb4", action="store_true", help="scan the pinned TB4 corpus")
    scan_parser.add_argument("--tb4-root", type=Path, default=None, help="override the TB4 root")
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

    plan_parser = subparsers.add_parser(
        "plan", help="read-only v3 -> v4 migration inventory (no Parquet write)"
    )
    plan_parser.add_argument("--tb4-root", type=Path, default=None, help="override the TB4 root")
    plan_parser.add_argument(
        "--tb3-root",
        type=Path,
        default=None,
        help="TB3 root; if supplied, plan validates TB3/TB4 non-aggregation guards",
    )
    plan_parser.add_argument(
        "--ref",
        type=str,
        default=None,
        help=f"pinned dataset ref (default: {TB4_DATASET_REF} or from dataset.toml)",
    )
    plan_parser.add_argument("--json", action="store_true", help="emit the plan as JSON")

    compile_parser = subparsers.add_parser(
        "compile",
        help="compile pinned TB4 v4.0.0 Harbor job plan with flat 8h timeout and drift guards",
    )
    compile_parser.add_argument("--tb4-root", type=Path, default=None, help="override the TB4 root")
    compile_parser.add_argument(
        "--tb3-root",
        type=Path,
        default=None,
        help="TB3 root; if supplied, compile validates TB3/TB4 non-aggregation guards",
    )
    compile_parser.add_argument(
        "--ref",
        type=str,
        default=None,
        help=f"pinned dataset ref (default: {TB4_DATASET_REF} or from dataset.toml)",
    )
    compile_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path for the compiled job plan JSON",
    )
    compile_parser.add_argument(
        "--model",
        type=str,
        default=ZAI_DEFAULT_MODEL,
        help=f"Z.ai model selector (default: {ZAI_DEFAULT_MODEL})",
    )
    compile_parser.add_argument(
        "--agent",
        type=str,
        default=ZAI_DEFAULT_AGENT,
        help=f"agent identifier for trusted-task execution (default: {ZAI_DEFAULT_AGENT})",
    )
    compile_parser.add_argument(
        "--remote-environment",
        dest="remote_environment",
        choices=sorted([*TB4_REMOTE_ENVIRONMENTS, "docker"]),
        default=TB4_DEFAULT_REMOTE_ENVIRONMENT,
        help=(
            "Harbor backend routed for the H100 task class (default: modal; "
            "'docker' refuses GPU tasks rather than degrading them)"
        ),
    )
    compile_parser.add_argument(
        "--include-task",
        dest="include_tasks",
        action="append",
        default=None,
        help="task ref or short name to include in compiled plan (repeatable, default: all tasks)",
    )
    compile_parser.add_argument("--json", action="store_true", help="emit the plan as JSON")

    return parser


def _sources_from_args(args: argparse.Namespace, repo_root: Path) -> list[TaskSource]:
    sources: list[TaskSource] = []
    if args.tb3 or args.all_local:
        sources.append(tb3_source(tb3_root(args.tb3_root)))
    if args.tb4:
        sources.append(tb4_source(tb4_root(args.tb4_root)))
    if args.library or args.all_local:
        sources.append(library_source(repo_root))
    for directory in args.directories:
        resolved = directory.expanduser().resolve()
        sources.append(
            TaskSource(root=resolved, source_repo=resolved.name, label=resolved.as_posix())
        )
    return sources


def _main_plan(args: argparse.Namespace) -> int:
    tb4 = tb4_root(args.tb4_root)
    if not tb4.is_dir():
        print(f"craft plan: TB4 corpus root not found: {tb4}", file=sys.stderr)
        return 2
    tb3 = tb3_root(args.tb3_root) if args.tb3_root is not None else None
    try:
        plan = plan_tb4(tb4, ref=args.ref, tb3_path=tb3)
    except (ValueError, FileNotFoundError) as error:
        print(f"craft plan: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(_render_plan(plan))
    return 0


def _main_compile(args: argparse.Namespace) -> int:
    tb4 = tb4_root(args.tb4_root)
    if not tb4.is_dir():
        print(f"craft compile: TB4 corpus root not found: {tb4}", file=sys.stderr)
        return 2
    tb3 = tb3_root(args.tb3_root) if args.tb3_root is not None else None
    repo_root = repository_root()
    out_path = (
        args.out.expanduser().resolve()
        if args.out is not None
        else derived_root_from_environment(repo_root) / "jobs" / "terminal-bench-4.0.0-plan.json"
    )
    try:
        plan = compile_tb4(
            tb4,
            ref=args.ref,
            out=out_path,
            model=args.model,
            agent=args.agent,
            tb3_path=tb3,
            include_tasks=args.include_tasks,
            remote_environment=args.remote_environment,
        )
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(f"craft compile: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(_render_compile(plan))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        return _main_plan(args)
    if args.command == "compile":
        return _main_compile(args)
    repo_root = repository_root()
    sources = _sources_from_args(args, repo_root)
    if not sources:
        print(
            "craft scan: nothing to scan — pass --tb3, --tb4, --library, --all-local, or a directory",
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
