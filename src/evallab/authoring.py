"""BUILDER authoring pipeline (WS-C).

Agent-authored evals as a measured pipeline with a human-only promotion
gate. Proposals live in quarantine under `library/tasks/_proposed/`; the
qualification ledger is Parquet at `derived/parquet/qualification/`.

State machine per proposal:

    proposed → battery_passed → craft_reviewed → registered | rejected

`registered` is reachable only through the existing human-only
`evallab registry` path. This module refuses to register.

The default battery is a local structural control over the proposal
package (oracle / nop / fair-oracle / adversarial). It never calls a
paid model and never starts Harbor; a Harbor-backed runner can be
injected. Determinism stops at ULID allocation, wall-clock timestamps,
and any injected Harbor runner.

Entry point: `python -m evallab.authoring`. `evallab author …` is not
wired here because `cli.py` is leased elsewhere.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any, Literal, Protocol, overload

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import Field

from evallab.paths import derived_root_from_environment
from evallab.queue import DirectoryQueue, PolicyGate, load_policy, new_ulid
from evallab.schemas import ContractModel, ExperimentSpec, PolicyDecision

SCHEMA_VERSION = "authoring/1"
PROPOSED_RELATIVE = Path("library/tasks/_proposed")
LEDGER_RELATIVE = Path("qualification/ledger.parquet")
CRAFT_PARQUET_RELATIVE = Path("craft/craft.parquet")
RESEARCH_RELATIVE = Path("research")
LIBRARY_TASKS_RELATIVE = Path("library/tasks")
REGISTRY_RELATIVE = Path("library/registry")
META_TASK_RELATIVE = Path("library/meta/synthesize-task@1")
SeedClass = Literal["mutation", "scenario", "craft-gap"]
Outcome = Literal["proposed", "battery_passed", "craft_reviewed", "registered", "rejected"]
BatteryCheck = Literal["oracle", "nop", "fair_oracle", "adversarial"]

SEED_CLASSES: tuple[SeedClass, ...] = ("mutation", "scenario", "craft-gap")
OUTCOMES: tuple[Outcome, ...] = (
    "proposed",
    "battery_passed",
    "craft_reviewed",
    "registered",
    "rejected",
)
BATTERY_CHECKS: tuple[BatteryCheck, ...] = (
    "oracle",
    "nop",
    "fair_oracle",
    "adversarial",
)
VERIFIER_TYPES: tuple[str, ...] = ("pytest", "diff", "golden_file", "judge", "hybrid")
GAP_AXES: tuple[str, ...] = ("verifier_type", "env_multi_container", "pinned_deps")

REGISTER_REFUSAL = (
    "authoring cannot register a proposal: registration is human-only via "
    "`evallab registry`. A human must promote the qualified package."
)

#: One DuckDB query answers pass-rate per seed_class. `$ledger` is the
#: parquet path. The four battery columns are the qualification predicate.
SEED_CLASS_PASS_RATE_SQL = """
SELECT
    seed_class,
    avg(
        CAST(
            coalesce(battery_oracle, false)
            AND coalesce(battery_nop, false)
            AND coalesce(battery_fair_oracle, false)
            AND coalesce(battery_adversarial, false)
            AS INTEGER
        )
    ) AS pass_rate,
    count(*) AS n
FROM read_parquet($ledger)
GROUP BY 1
ORDER BY 1
""".strip()

_IGNORE_COPY = frozenset({"__pycache__", ".git", ".pytest_cache", ".DS_Store"})
_VERSION_LINE = re.compile(r'(?m)^version\s*=\s*"[^"]*"')
_CLOCK = Callable[[], datetime]
_IDS = Callable[[], str]


class AuthoringError(RuntimeError):
    """Safe refusal for a broken authoring request."""


class ProposalNotFoundError(AuthoringError):
    """Raised when a proposal id is not in quarantine."""


class RegisterRefusal(AuthoringError):
    """Automation asked to register; the human gate refused."""

    def __init__(self, proposal_id: str, outcome: Outcome) -> None:
        self.proposal_id = proposal_id
        self.outcome = outcome
        super().__init__(REGISTER_REFUSAL)


class QualificationRecord(ContractModel):
    """One proposal's row in the qualification ledger."""

    proposal_id: str
    seed_class: SeedClass
    ref_task: str | None = None
    battery_oracle: bool | None = None
    battery_nop: bool | None = None
    battery_fair_oracle: bool | None = None
    battery_adversarial: bool | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    review_score: float | None = None
    outcome: Outcome
    created_at: str
    updated_at: str


LEDGER_SCHEMA = pa.schema(
    [
        pa.field("proposal_id", pa.string(), nullable=False),
        pa.field("seed_class", pa.string(), nullable=False),
        pa.field("ref_task", pa.string()),
        pa.field("battery_oracle", pa.bool_()),
        pa.field("battery_nop", pa.bool_()),
        pa.field("battery_fair_oracle", pa.bool_()),
        pa.field("battery_adversarial", pa.bool_()),
        pa.field("evidence_paths", pa.list_(pa.string())),
        pa.field("review_score", pa.float64()),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True)
class ControlResult:
    """One battery check: a bool, a reward, and the evidence path."""

    check: BatteryCheck
    passed: bool
    reward: float
    agent: str
    evidence_path: str
    notes: str
    attempts: int = 1


class ControlRunner(Protocol):
    """Local free control. Implementations must not call a paid model."""

    def run(
        self,
        proposal_dir: Path,
        check: BatteryCheck,
        *,
        evidence_dir: Path,
    ) -> ControlResult: ...


@dataclass(frozen=True)
class Proposal:
    """In-memory view of a quarantined proposal."""

    proposal_id: str
    seed_class: SeedClass
    ref_task: str | None
    path: Path
    outcome: Outcome
    version: str | None = None
    source_path: str | None = None
    source_digest: str | None = None
    scenario_path: str | None = None
    target_facets: dict[str, Any] | None = None
    created_at: str | None = None
    job_id: str | None = None
    inputs: list[dict[str, Any]] | None = None
    injected_spec: dict[str, Any] | None = None
    exemplar: str | None = None

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "seed_class": self.seed_class,
            "ref_task": self.ref_task,
            "outcome": self.outcome,
            "version": self.version,
            "source_path": self.source_path,
            "source_digest": self.source_digest,
            "scenario_path": self.scenario_path,
            "target_facets": self.target_facets,
            "created_at": self.created_at,
        }
        if self.job_id is not None:
            data["job_id"] = self.job_id
        if self.inputs is not None:
            data["inputs"] = self.inputs
        if self.injected_spec is not None:
            data["injected_spec"] = self.injected_spec
        if self.exemplar is not None:
            data["exemplar"] = self.exemplar
        return data

@dataclass(frozen=True)
class BatteryReport:
    proposal_id: str
    outcome: Outcome
    checks: tuple[ControlResult, ...]
    all_passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome,
            "all_passed": self.all_passed,
            "checks": [
                {
                    "check": item.check,
                    "passed": item.passed,
                    "reward": item.reward,
                    "agent": item.agent,
                    "evidence_path": item.evidence_path,
                    "notes": item.notes,
                    "attempts": item.attempts,
                }
                for item in self.checks
            ],
        }


@dataclass(frozen=True)
class ReviewReport:
    proposal_id: str
    outcome: Outcome
    score: float
    reasons: tuple[str, ...]
    evidence_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome,
            "score": self.score,
            "reasons": list(self.reasons),
            "evidence_path": self.evidence_path,
        }


@dataclass(frozen=True)
class BatchItem:
    proposal_id: str
    seed_class: SeedClass
    outcome: Outcome
    battery: BatteryReport
    review: ReviewReport


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def now_utc() -> datetime:
    return datetime.now(UTC)


def isoformat(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def proposed_root(repo_root: Path) -> Path:
    return repo_root / PROPOSED_RELATIVE


def ledger_path(derived_root: Path) -> Path:
    return derived_root / LEDGER_RELATIVE


def craft_parquet_path(derived_root: Path) -> Path:
    return derived_root / CRAFT_PARQUET_RELATIVE


def _ignore_copy(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORE_COPY}


def _sha256_tree(root: Path) -> str:
    digest = __import__("hashlib").sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(b"dir\0")
            digest.update(relative.encode())
            continue
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(b"file\0")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def bump_version(version: str | None) -> str:
    """New version for a mutation copy. Never returns the source version."""
    if not version or not version.strip():
        return "0.1.0"
    cleaned = version.strip()
    core, _, _suffix = cleaned.partition("-")
    parts = core.split(".")
    if (
        len(parts) >= 3
        and parts[0].isdigit()
        and parts[1].isdigit()
        and parts[2].isdigit()
    ):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{cleaned}-proposed"


def rewrite_task_version(text: str, new_version: str) -> str:
    if _VERSION_LINE.search(text):
        return _VERSION_LINE.sub(f'version = "{new_version}"', text, count=1)
    if "[task]" in text:
        return text.replace("[task]", f'[task]\nversion = "{new_version}"', 1)
    return f'version = "{new_version}"\n' + text


def read_task_version(task_toml: Path) -> str | None:
    if not task_toml.is_file():
        return None
    try:
        document = tomllib.loads(task_toml.read_text())
    except tomllib.TOMLDecodeError:
        return None
    task = document.get("task")
    if not isinstance(task, dict):
        return None
    version = task.get("version")
    return version if isinstance(version, str) and version.strip() else None


def load_ledger(path: Path) -> list[QualificationRecord]:
    if not path.is_file():
        return []
    rows = pq.read_table(path, schema=LEDGER_SCHEMA).to_pylist()
    return [QualificationRecord.model_validate(_normalize_row(row)) for row in rows]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    paths = payload.get("evidence_paths")
    if paths is None:
        payload["evidence_paths"] = []
    elif not isinstance(paths, list):
        payload["evidence_paths"] = list(paths)
    return payload


def write_ledger(path: Path, records: Sequence[QualificationRecord]) -> Path:
    """Replace the ledger atomically. Rows stay sorted by proposal_id."""
    ordered = sorted(records, key=lambda record: record.proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [record.model_dump(mode="json") for record in ordered],
        schema=LEDGER_SCHEMA,
    )
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
    )
    temporary.replace(path)
    return path


def upsert_ledger(path: Path, record: QualificationRecord) -> QualificationRecord:
    if record.outcome == "registered":
        raise RegisterRefusal(record.proposal_id, record.outcome)
    existing = {item.proposal_id: item for item in load_ledger(path)}
    existing[record.proposal_id] = record
    write_ledger(path, list(existing.values()))
    return record


def seed_class_pass_rates(path: Path) -> list[tuple[str, float, int]]:
    """Pass-rate per seed_class. Same predicate as `SEED_CLASS_PASS_RATE_SQL`."""
    import duckdb

    with duckdb.connect(":memory:") as connection:
        return list(
            connection.execute(SEED_CLASS_PASS_RATE_SQL, {"ledger": path.as_posix()}).fetchall()
        )


def find_craft_gap(parquet_path: Path) -> dict[str, Any]:
    """First facet triple with zero coverage in the CRAFT parquet.

    Axes are `verifier_type × env_multi_container × pinned_deps`. Order is
    stable so the same parquet yields the same gap.
    """
    if not parquet_path.is_file():
        raise AuthoringError(
            f"craft parquet not found at {parquet_path}; "
            "run `python -m evallab.craft scan` before --seed craft-gap"
        )
    table = pq.read_table(parquet_path)
    missing = [name for name in GAP_AXES if name not in table.column_names]
    if missing:
        raise AuthoringError(
            f"craft parquet {parquet_path} is missing gap columns {missing}"
        )
    covered: set[tuple[Any, ...]] = set()
    for row in table.select(list(GAP_AXES)).to_pylist():
        covered.add(tuple(row[name] for name in GAP_AXES))
    for candidate in product(VERIFIER_TYPES, (False, True), (False, True)):
        if candidate not in covered:
            return {
                "verifier_type": candidate[0],
                "env_multi_container": candidate[1],
                "pinned_deps": candidate[2],
            }
    raise AuthoringError(
        f"craft parquet {parquet_path} covers every "
        "verifier_type × env_multi_container × pinned_deps combination"
    )


def discover_scenario_paths(repo_root: Path) -> list[Path]:
    """Research-tree markdown that can seed a scenario proposal."""
    research = repo_root / RESEARCH_RELATIVE
    if not research.is_dir():
        return []
    preferred = research / "scenarios"
    roots = [preferred] if preferred.is_dir() else [
        research / "explorations",
        research / "inspections",
        research,
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen or path.name.upper() == "README.md":
                continue
            seen.add(resolved)
            found.append(path)
    return found


def resolve_scenario(repo_root: Path, ref: str | None) -> Path:
    candidates = discover_scenario_paths(repo_root)
    if not candidates:
        raise AuthoringError(
            f"no research scenario material under {repo_root / RESEARCH_RELATIVE}"
        )
    if ref is None:
        return candidates[0]
    needle = ref.strip()
    for path in candidates:
        relative = _repo_relative(path, repo_root)
        if path.stem == needle or relative == needle or relative.endswith(needle):
            return path
    explicit = (repo_root / needle).resolve() if not Path(needle).is_absolute() else Path(needle)
    if explicit.is_file():
        try:
            explicit.relative_to((repo_root / RESEARCH_RELATIVE).resolve())
        except ValueError as exc:
            raise AuthoringError(
                f"scenario --ref {ref!r} is not under {RESEARCH_RELATIVE}"
            ) from exc
        return explicit
    raise AuthoringError(f"scenario --ref {ref!r} not found under {RESEARCH_RELATIVE}")


def list_library_tasks(repo_root: Path) -> list[Path]:
    root = repo_root / LIBRARY_TASKS_RELATIVE
    if not root.is_dir():
        return []
    tasks: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "task.toml").is_file() and (child / "instruction.md").is_file():
            tasks.append(child)
    return tasks


def resolve_registered_task(repo_root: Path, ref: str | None) -> Path:
    """A mutation source: registry path if present, else `library/tasks/<ref>`.

    The registry is the preferred binding. This checkout may have no records
    yet, so a well-formed library task is accepted as a source — the copy
    still never edits it in place.
    """
    registry_dir = repo_root / REGISTRY_RELATIVE
    records: dict[str, str] = {}
    if registry_dir.is_dir():
        for record_file in sorted(registry_dir.glob("*.json")):
            try:
                raw = json.loads(record_file.read_text())
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            task_id = raw.get("task_id")
            task_path = raw.get("task_path")
            state = raw.get("state")
            if (
                isinstance(task_id, str)
                and isinstance(task_path, str)
                and state == "registered"
            ):
                records[task_id] = task_path

    if ref is not None:
        needle = ref.strip().removeprefix("registered/")
        if needle in records:
            path = repo_root / records[needle]
            if path.is_dir():
                return path
        library = repo_root / LIBRARY_TASKS_RELATIVE / needle
        if library.is_dir() and (library / "task.toml").is_file():
            return library
        explicit = Path(needle)
        if explicit.is_absolute() and explicit.is_dir():
            return explicit
        raise AuthoringError(f"mutation --ref {ref!r} is not a registered or library task")

    for task_id, relative in sorted(records.items()):
        path = repo_root / relative
        if path.is_dir():
            return path
        del task_id
    tasks = list_library_tasks(repo_root)
    if tasks:
        return tasks[0]
    raise AuthoringError("no registered or library task available for --seed mutation")


def load_proposal(proposal_dir: Path) -> Proposal:
    manifest_path = proposal_dir / "proposal.json"
    if not manifest_path.is_file():
        raise ProposalNotFoundError(f"proposal manifest missing: {manifest_path}")
    raw = json.loads(manifest_path.read_text())
    return Proposal(
        proposal_id=str(raw["proposal_id"]),
        seed_class=raw["seed_class"],
        ref_task=raw.get("ref_task"),
        path=proposal_dir,
        outcome=raw["outcome"],
        version=raw.get("version"),
        source_path=raw.get("source_path"),
        source_digest=raw.get("source_digest"),
        scenario_path=raw.get("scenario_path"),
        target_facets=raw.get("target_facets"),
        created_at=raw.get("created_at"),
        job_id=raw.get("job_id"),
        inputs=raw.get("inputs"),
        injected_spec=raw.get("injected_spec"),
        exemplar=raw.get("exemplar"),
    )

class StructuralControlRunner:
    """Free local control that never starts Harbor and never calls a model.

    oracle    — solution/ is present (the oracle package exists) → reward 1.0
    nop       — tests/ is present; empty work cannot pass → reward 0.0, n=2
    fair-oracle — instruction + environment only; solution/tests stay outside
    adversarial — cheat-visible answers are absent; a cheater scores 0
    """

    def run(
        self,
        proposal_dir: Path,
        check: BatteryCheck,
        *,
        evidence_dir: Path,
    ) -> ControlResult:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if check == "oracle":
            return self._oracle(proposal_dir, evidence_dir)
        if check == "nop":
            return self._nop(proposal_dir, evidence_dir)
        if check == "fair_oracle":
            return self._fair_oracle(proposal_dir, evidence_dir)
        return self._adversarial(proposal_dir, evidence_dir)

    def _oracle(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        solution = proposal_dir / "solution"
        files = _existing_files(solution)
        passed = bool(files)
        payload = {
            "check": "oracle",
            "agent": "oracle",
            "reward": 1.0 if passed else 0.0,
            "passed": passed,
            "solution_files": files,
        }
        path = evidence_dir / "oracle.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="oracle",
            passed=passed,
            reward=1.0 if passed else 0.0,
            agent="oracle",
            evidence_path=path.as_posix(),
            notes="structural oracle: solution/ present" if passed else "solution/ missing",
        )

    def _nop(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        tests = proposal_dir / "tests"
        attempts: list[dict[str, Any]] = []
        for index in (1, 2):
            reward = 0.0 if tests.is_dir() and _existing_files(tests) else 1.0
            attempts.append({"attempt": index, "agent": "nop", "reward": reward})
        passed = bool(attempts) and all(item["reward"] == 0.0 for item in attempts)
        payload = {"check": "nop", "agent": "nop", "passed": passed, "attempts": attempts}
        path = evidence_dir / "nop.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="nop",
            passed=passed,
            reward=0.0 if passed else 1.0,
            agent="nop",
            evidence_path=path.as_posix(),
            notes="structural nop n=2: verifier present" if passed else "tests/ missing",
            attempts=len(attempts),
        )

    def _fair_oracle(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        instruction = proposal_dir / "instruction.md"
        environment = proposal_dir / "environment"
        visible = _existing_files(environment)
        leaked = [
            relative
            for relative in visible
            if relative.startswith("solution/")
            or relative.startswith("tests/")
            or "/solution/" in relative
            or "/tests/" in relative
        ]
        has_instruction = instruction.is_file() and instruction.stat().st_size > 0
        passed = has_instruction and environment.is_dir() and not leaked
        payload = {
            "check": "fair_oracle",
            "agent": "fair-oracle",
            "passed": passed,
            "visible_environment_files": visible,
            "leaked": leaked,
            "instruction_present": instruction.is_file(),
        }
        path = evidence_dir / "fair-oracle.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="fair_oracle",
            passed=passed,
            reward=1.0 if passed else 0.0,
            agent="fair-oracle",
            evidence_path=path.as_posix(),
            notes=(
                "fresh agent sees instruction+environment only"
                if passed
                else "fair-oracle surface incomplete or leaked hidden inputs"
            ),
        )

    def _adversarial(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        instruction = (proposal_dir / "instruction.md").read_text() if (
            proposal_dir / "instruction.md"
        ).is_file() else ""
        solution_blobs = _file_blobs(proposal_dir / "solution")
        leaked = [name for name, blob in solution_blobs if blob and blob in instruction]
        tests_present = bool(_existing_files(proposal_dir / "tests"))
        passed = tests_present and not leaked
        payload = {
            "check": "adversarial",
            "agent": "adversarial",
            "reward": 0.0 if passed else 1.0,
            "passed": passed,
            "leaked_solution_files": leaked,
            "tests_present": tests_present,
        }
        path = evidence_dir / "adversarial.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="adversarial",
            passed=passed,
            reward=0.0 if passed else 1.0,
            agent="adversarial",
            evidence_path=path.as_posix(),
            notes=(
                "cheat-instructed agent scores 0: answer not in instruction"
                if passed
                else "adversarial would succeed: leaked answer or missing verifier"
            ),
        )


def _existing_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files.append(path.relative_to(root).as_posix())
    return files


def _file_blobs(root: Path) -> list[tuple[str, str]]:
    blobs: list[tuple[str, str]] = []
    if not root.is_dir():
        return blobs
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if text.strip():
            blobs.append((path.relative_to(root).as_posix(), text))
    return blobs


def score_review(proposal: Proposal) -> tuple[float, list[str]]:
    """Deterministic CRAFT-derived rubric. Same bytes ⇒ same score."""
    reasons: list[str] = []
    score = 0.0
    directory = proposal.path
    layout = {
        "task.toml": (directory / "task.toml").is_file(),
        "instruction.md": (directory / "instruction.md").is_file(),
        "environment": (directory / "environment").is_dir(),
        "tests": (directory / "tests").is_dir() and bool(_existing_files(directory / "tests")),
    }
    if all(layout.values()):
        score += 0.25
        reasons.append("harbor layout complete (task.toml, instruction, environment, tests)")
    else:
        missing = [name for name, present in layout.items() if not present]
        reasons.append(f"incomplete harbor layout: missing {missing}")

    isolated = False
    task_toml = directory / "task.toml"
    if task_toml.is_file():
        text = task_toml.read_text()
        isolated = (
            'environment_mode = "separate"' in text or "environment_mode = 'separate'" in text
        )
    if isolated:
        score += 0.25
        reasons.append("CRAFT anti-cheat: separate verifier image (hidden_tests)")
    else:
        reasons.append("CRAFT anti-cheat: verifier is not declared separate")

    env_files = _existing_files(directory / "environment")
    hidden_ok = not any(
        relative.startswith("solution/") or relative.startswith("tests/") for relative in env_files
    )
    instruction = (
        (directory / "instruction.md").read_text()
        if (directory / "instruction.md").is_file()
        else ""
    )
    leaked = [
        name
        for name, blob in _file_blobs(directory / "solution")
        if blob and blob in instruction
    ]
    if hidden_ok and not leaked:
        score += 0.25
        reasons.append("CRAFT answer-hiding: solution/tests stay outside instruction+environment")
    else:
        reasons.append(
            "CRAFT answer-hiding failed: "
            + (
                "solution leaked into instruction"
                if leaked
                else "hidden inputs visible in environment"
            )
        )

    if proposal.seed_class == "mutation":
        if proposal.version and proposal.source_digest:
            score += 0.25
            reasons.append(
                f"mutation is a new version {proposal.version!r}, source digest bound"
            )
        else:
            reasons.append("mutation missing version or source digest")
    elif proposal.seed_class == "scenario":
        if proposal.scenario_path:
            score += 0.25
            reasons.append(f"scenario cites research material {proposal.scenario_path}")
        else:
            reasons.append("scenario missing research citation")
    else:
        facets = proposal.target_facets or {}
        if all(name in facets for name in GAP_AXES):
            score += 0.25
            reasons.append(
                "craft-gap targets "
                f"verifier_type={facets['verifier_type']} "
                f"env_multi_container={facets['env_multi_container']} "
                f"pinned_deps={facets['pinned_deps']}"
            )
        else:
            reasons.append("craft-gap missing target facet triple")
    return round(score, 4), reasons

def compute_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sample_meta_spec(
    repo_root: Path,
    *,
    seed: SeedClass = "craft-gap",
    ref: str | None = None,
    derived_root: Path | None = None,
) -> dict[str, Any]:
    derived = derived_root or derived_root_from_environment(repo_root)
    if seed == "craft-gap":
        facets = find_craft_gap(craft_parquet_path(derived))
        return {
            "schema_version": "spec/1",
            "name": f"gap-{facets['verifier_type']}",
            "category": "data-processing",
            "scenario": "structured-pipeline",
            "difficulty": "medium",
            "summary": f"Task targeting CRAFT gap {facets['verifier_type']}",
            "seed_class": "craft-gap",
            "target_facets": {name: facets[name] for name in GAP_AXES},
        }
    if seed == "scenario":
        scenario = resolve_scenario(repo_root, ref)
        return {
            "schema_version": "spec/1",
            "name": f"scenario-{scenario.stem}",
            "category": "data-processing",
            "scenario": scenario.stem,
            "difficulty": "medium",
            "summary": f"Task seeded from {scenario.name}",
            "seed_class": "scenario",
            "scenario_path": _repo_relative(scenario, repo_root),
        }
    source = resolve_registered_task(repo_root, ref)
    return {
        "schema_version": "spec/1",
        "name": f"mutation-{source.name}",
        "category": "data-processing",
        "scenario": "mutation",
        "difficulty": "medium",
        "summary": f"Mutation of {source.name}",
        "seed_class": "mutation",
        "ref_task": source.name,
    }


def assemble_meta_task(
    repo_root: Path,
    *,
    spec: dict[str, Any] | None = None,
    exemplar: str | None = None,
    destination: Path | None = None,
) -> Path:
    meta_template = repo_root / META_TASK_RELATIVE
    spec_name = str(spec.get("name", "default")) if spec and spec.get("name") else "default"
    dest = destination or (repo_root / "library/meta/_staged" / spec_name)
    dest.mkdir(parents=True, exist_ok=True)
    if meta_template.is_dir():
        shutil.copytree(meta_template, dest, dirs_exist_ok=True, ignore=_ignore_copy)
    if spec is not None:
        spec_dest = dest / "environment/spec.json"
        _atomic_write_json(spec_dest, spec)
    if exemplar is not None:
        ex_src = resolve_registered_task(repo_root, exemplar)
        ex_dest = dest / "environment/exemplar"
        if ex_dest.exists():
            shutil.rmtree(ex_dest)
        shutil.copytree(ex_src, ex_dest, dirs_exist_ok=True, ignore=_ignore_copy)
    return dest


def check_package_structure(task_dir: Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    errors: list[str] = []

    required_files = [
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    ]

    for rel in required_files:
        p = task_dir / rel
        if not p.is_file():
            errors.append(f"required file missing: {rel}")
        elif p.stat().st_size == 0:
            errors.append(f"required file is empty: {rel}")

    task_toml_path = task_dir / "task.toml"
    if task_toml_path.is_file() and task_toml_path.stat().st_size > 0:
        try:
            config = tomllib.loads(task_toml_path.read_text(encoding="utf-8"))
            if "task" not in config or not isinstance(config["task"], dict):
                errors.append("task.toml: missing [task] section")
            else:
                for field in ("name", "version", "description"):
                    if not config["task"].get(field):
                        errors.append(f"task.toml: [task].{field} is missing or empty")

            for sec in ("environment", "agent", "verifier"):
                if sec not in config or not isinstance(config[sec], dict):
                    errors.append(f"task.toml: missing [{sec}] section")
        except Exception as exc:
            errors.append(f"task.toml: parse error: {exc}")

    for script_rel in ("solution/solve.sh", "tests/test.sh"):
        p = task_dir / script_rel
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            if not content.startswith("#!"):
                errors.append(f"{script_rel}: missing shebang (e.g. #!/bin/sh)")

    passed = len(errors) == 0
    msg = "package structure valid" if passed else f"structure errors: {'; '.join(errors)}"
    return {
        "check": "package_structure",
        "passed": passed,
        "errors": errors,
        "message": msg,
    }


def _extract_sensitive_spans(task_dir: Path) -> list[tuple[str, str]]:
    spans: list[tuple[str, str]] = []
    roots = [task_dir / "solution", task_dir / "tests"]

    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.name in {"Dockerfile", "test.sh", "solve.sh"}:
                continue
            rel = p.relative_to(task_dir).as_posix()
            text = p.read_text(encoding="utf-8", errors="replace")

            for line in text.splitlines():
                normalized = " ".join(line.strip().split())
                if (
                    len(normalized) >= 24
                    and not normalized.startswith(("#", "//", "/*", "*", "import ", "from "))
                    and re.search(r"[A-Za-z0-9]", normalized)
                    and not normalized.startswith("def main():")
                    and not normalized.startswith('if __name__ == "__main__":')
                ):
                    spans.append((rel, normalized))

    return spans


def check_no_answer_leakage(task_dir: Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    leaks: list[str] = []

    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            rel = p.relative_to(env_dir).as_posix()
            parts = p.relative_to(env_dir).parts
            if any(part in {"solution", "tests", "verifier"} for part in parts):
                leaks.append(f"environment/ contains hidden directory: {rel}")
            answer_keys = ("golden", "solution", "expected_summary", "answer_key")
            if p.is_file() and any(k in p.name.lower() for k in answer_keys):
                leaks.append(f"environment/ contains answer file: {rel}")

        df = env_dir / "Dockerfile"
        if df.is_file():
            df_text = df.read_text(encoding="utf-8", errors="replace")
            for line in df_text.splitlines():
                line_clean = line.strip()
                if line_clean.startswith(("COPY", "ADD")) and any(
                    bad in line_clean for bad in ("solution", "tests", "verifier")
                ):
                    leaks.append(f"environment/Dockerfile copies hidden files: {line_clean}")

    visible_files: list[Path] = []
    instr = task_dir / "instruction.md"
    if instr.is_file():
        visible_files.append(instr)
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            if p.is_file() and p.name != "Dockerfile":
                visible_files.append(p)

    visible_texts: list[str] = []
    for vf in visible_files:
        content = vf.read_text(encoding="utf-8", errors="replace")
        visible_texts.append(" ".join(content.split()))

    combined_visible = "\n".join(visible_texts)

    sensitive_spans = _extract_sensitive_spans(task_dir)
    for src, span in sensitive_spans:
        if span in combined_visible:
            leaks.append(f"sensitive span from {src} leaked in visible surface: {span[:40]}...")

    passed = len(leaks) == 0
    msg = "no answer leakage detected" if passed else f"leakage detected: {'; '.join(leaks)}"
    return {
        "check": "no_answer_leakage",
        "passed": passed,
        "leaks": leaks,
        "message": msg,
    }


def _prepare_workspace(task_dir: Path, workspace: Path) -> None:
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name == "Dockerfile":
                continue
            dest = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name != "Dockerfile" and not item.is_dir():
                shutil.copy2(item, input_dir / item.name)

    (workspace / "output").mkdir(parents=True, exist_ok=True)


def check_oracle_solution_runs(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    sol_sh = (task_dir / "solution/solve.sh").resolve()
    sol_py = (task_dir / "solution/solve.py").resolve()

    if not sol_sh.is_file() and not sol_py.is_file():
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": "solution/solve.sh or solution/solve.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_oracle_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)

        cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]
        env = dict(os.environ)
        env["APP_DIR"] = str(target_workspace)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        err_tail = proc.stderr[-200:]
        msg = (
            "oracle solution executed successfully"
            if passed
            else f"oracle failed (rc={proc.returncode}): {err_tail}"
        )
        return {
            "check": "oracle_solution_runs",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": f"oracle execution raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_tests_pass(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    test_sh = (task_dir / "tests/test.sh").resolve()
    verify_py = (task_dir / "tests/verify.py").resolve()

    if not test_sh.is_file() and not verify_py.is_file():
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": "tests/test.sh or tests/verify.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_verify_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)
            sol_sh = (task_dir / "solution/solve.sh").resolve()
            sol_py = (task_dir / "solution/solve.py").resolve()
            cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]
            subprocess.run(cmd, cwd=str(target_workspace), capture_output=True, timeout=timeout)

        cmd = ["bash", str(test_sh)] if test_sh.is_file() else [sys.executable, str(verify_py)]
        logs_dir = target_workspace / "logs/verifier"
        logs_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        reward_file = logs_dir / "reward.json"
        if reward_file.is_file():
            try:
                reward_data = json.loads(reward_file.read_text(encoding="utf-8"))
                if isinstance(reward_data, dict) and reward_data.get("reward") != 1.0:
                    passed = False
            except Exception:
                pass

        err_tail = proc.stderr[-200:]
        msg = "task tests passed" if passed else f"tests failed (rc={proc.returncode}): {err_tail}"
        return {
            "check": "task_tests_pass",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": f"verifier raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_completeness(task_dir: Path, timeout: float = 60.0) -> dict[str, Any]:
    task_dir = Path(task_dir)

    structure_res = check_package_structure(task_dir)
    leakage_res = check_no_answer_leakage(task_dir)

    workspace = Path(tempfile.mkdtemp(prefix="task_completeness_"))
    try:
        _prepare_workspace(task_dir, workspace)
        oracle_res = check_oracle_solution_runs(task_dir, timeout=timeout, workspace=workspace)
        tests_res = check_task_tests_pass(task_dir, timeout=timeout, workspace=workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    checks = {
        "package_structure": structure_res,
        "no_answer_leakage": leakage_res,
        "oracle_solution_runs": oracle_res,
        "task_tests_pass": tests_res,
    }

    all_passed = all(c["passed"] for c in checks.values())
    rewards = {"reward": 1.0 if all_passed else 0.0}

    return {
        "passed": all_passed,
        "checks": checks,
        "rewards": rewards,
    }


def generate_stub_task(
    destination: Path,
    spec: dict[str, Any],
    *,
    exemplar_dir: Path | None = None,
) -> Path:
    _ = exemplar_dir
    destination.mkdir(parents=True, exist_ok=True)
    task_name = str(spec.get("name", "synthesized-task")).lower().replace(" ", "-")
    category = str(spec.get("category", "data-processing"))
    difficulty = str(spec.get("difficulty", "medium"))
    summary = str(spec.get("summary", "Process input data and generate summary report"))

    task_toml = f"""schema_version = "1.4"
artifacts = [
    "/app/output/summary.json",
]

[task]
name = "local-lab/{task_name}"
version = "0.1.0"
description = "{summary}"
keywords = ["python", "{category}", "separate-verifier"]

[[task.authors]]
name = "Eval Lab Synthesizer"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = "{difficulty}"
category = "{category}"
tags = ["synthetic", "authoring"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[agent]
timeout_sec = 120.0

[environment]
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""
    (destination / "task.toml").write_text(task_toml, encoding="utf-8")

    instruction = f"""# {task_name.replace('-', ' ').title()}

Read the records in `/app/input/data.json` and create `/app/output/summary.json`.

The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: total count of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify or delete the input file.
"""
    (destination / "instruction.md").write_text(instruction, encoding="utf-8")

    env_dir = destination / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_dockerfile = (
        "FROM python:3.13-slim-bookworm\n\n"
        "WORKDIR /app\n\n"
        "COPY data.json /app/input/data.json\n\n"
        "RUN mkdir -p /app/output\n"
    )
    (env_dir / "Dockerfile").write_text(env_dockerfile, encoding="utf-8")
    sample_data = [
        {"id": 1, "type": "event_a", "val": 10},
        {"id": 2, "type": "event_b", "val": 20},
        {"id": 3, "type": "event_a", "val": 30},
    ]
    (env_dir / "data.json").write_text(json.dumps(sample_data, indent=2) + "\n", encoding="utf-8")

    sol_dir = destination / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    sol_sh = """#!/bin/sh
set -eu

if [ -f /solution/solve.py ]; then
    exec python /solution/solve.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/solve.py"
fi
"""
    (sol_dir / "solve.sh").write_text(sol_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(sol_dir / "solve.sh", 0o755)

    sol_py = """import json
from pathlib import Path

input_file = Path("/app/input/data.json")
if not input_file.is_file():
    input_file = Path("environment/data.json")
if not input_file.is_file():
    input_file = Path("input/data.json")

data = json.loads(input_file.read_text(encoding="utf-8"))
summary = {
    "schema_version": 1,
    "total_records": len(data),
    "status": "ok",
}
output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\\n", encoding="utf-8")
"""
    (sol_dir / "solve.py").write_text(sol_py, encoding="utf-8")

    tests_dir = destination / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_dockerfile = (
        "FROM python:3.13-slim-bookworm\n\n"
        "WORKDIR /app\n\n"
        "COPY test.sh /tests/test.sh\n"
        "COPY verify.py /tests/verify.py\n\n"
        "RUN chmod +x /tests/test.sh\n"
    )
    (tests_dir / "Dockerfile").write_text(test_dockerfile, encoding="utf-8")
    test_sh = """#!/bin/sh
set -eu

if [ -f /tests/verify.py ]; then
    exec python /tests/verify.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/verify.py"
fi
"""
    (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tests_dir / "test.sh", 0o755)

    verify_py = """import json
from pathlib import Path

AGENT_OUTPUT = Path("/app/output/summary.json")
if not AGENT_OUTPUT.is_file():
    AGENT_OUTPUT = Path("output/summary.json")

LOG_DIR = Path("/logs/verifier")
if not LOG_DIR.exists():
    LOG_DIR = Path("logs/verifier")

def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENT_OUTPUT.is_file():
        passed = False
        message = "summary.json is missing"
    else:
        try:
            data = json.loads(AGENT_OUTPUT.read_text(encoding="utf-8"))
            passed = (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("total_records") == 3
                and data.get("status") == "ok"
            )
            message = "summary.json is valid" if passed else "summary.json content mismatch"
        except Exception as exc:
            passed = False
            message = f"error parsing json: {exc}"

    checks = {"correctness": {"passed": passed, "message": message}}
    rewards = {"reward": 1.0 if passed else 0.0}
    ctrf = {
        "report": {
            "summary": {
                "tests": 1,
                "passed": 1 if passed else 0,
                "failed": 0 if passed else 1,
            }
        }
    }
    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": checks}))

if __name__ == "__main__":
    main()
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")
    return destination

class AuthoringPipeline:
    """Propose → battery → review. Registration is a hard refusal."""

    def __init__(
        self,
        repo_root: Path,
        *,
        derived_root: Path | None = None,
        runner: ControlRunner | None = None,
        now: _CLOCK | None = None,
        new_id: _IDS | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.derived_root = (
            derived_root.resolve()
            if derived_root is not None
            else derived_root_from_environment(self.repo_root)
        )
        self.runner = runner or StructuralControlRunner()
        self._now = now or now_utc
        self._new_id = new_id or new_ulid

    @property
    def quarantine(self) -> Path:
        return proposed_root(self.repo_root)

    @property
    def ledger(self) -> Path:
        return ledger_path(self.derived_root)

    def records(self) -> list[QualificationRecord]:
        return load_ledger(self.ledger)

    def get(self, proposal_id: str) -> Proposal:
        path = self.quarantine / proposal_id
        if not path.is_dir():
            raise ProposalNotFoundError(f"proposal {proposal_id!r} is not in {self.quarantine}")
        return load_proposal(path)

    @overload
    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: Literal[False] = False,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
    ) -> Proposal: ...

    @overload
    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: Literal[True],
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
    ) -> tuple[ExperimentSpec, Path, PolicyDecision]: ...

    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: bool = False,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
    ) -> Proposal | tuple[ExperimentSpec, Path, PolicyDecision]:
        if via_harbor:
            return self.propose_via_harbor(
                seed,
                ref=ref,
                agent=agent,
                model=model,
                spec=spec,
                exemplar=exemplar,
            )
        if seed not in SEED_CLASSES:
            raise AuthoringError(f"unknown seed class {seed!r}; expected one of {SEED_CLASSES}")
        proposal_id = self._new_id()
        destination = self.quarantine / proposal_id
        if destination.exists():
            raise AuthoringError(f"proposal id {proposal_id!r} already exists")
        destination.mkdir(parents=True, exist_ok=False)
        created = isoformat(self._now())
        if seed == "mutation":
            proposal = self._propose_mutation(proposal_id, destination, ref, created)
        elif seed == "scenario":
            proposal = self._propose_scenario(proposal_id, destination, ref, created)
        else:
            proposal = self._propose_craft_gap(proposal_id, destination, ref, created)
        _atomic_write_json(destination / "proposal.json", proposal.manifest())
        upsert_ledger(
            self.ledger,
            QualificationRecord(
                proposal_id=proposal_id,
                seed_class=seed,
                ref_task=proposal.ref_task,
                outcome="proposed",
                created_at=created,
                updated_at=created,
            ),
        )
        return proposal

    def propose_via_harbor(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
        submitted_by: str | None = None,
    ) -> tuple[ExperimentSpec, Path, PolicyDecision]:
        spec_dict = spec or sample_meta_spec(
            self.repo_root,
            seed=seed,
            ref=ref,
            derived_root=self.derived_root,
        )
        exemplar_name = exemplar or (
            ref if ref and (self.repo_root / "library/tasks" / ref).is_dir() else "event-summary"
        )
        meta_task_dir = assemble_meta_task(
            self.repo_root,
            spec=spec_dict,
            exemplar=exemplar_name,
        )
        task_ref = _repo_relative(meta_task_dir, self.repo_root)

        spec_id = self._new_id()
        task_name_slug = (
            re.sub(r"[^a-z0-9-]+", "-", str(spec_dict.get("name", "task")).lower()).strip("-")
            or "task"
        )
        exp_name = f"synth-{seed[:8]}-{task_name_slug[:20]}-{spec_id[-8:].lower()}"
        exp_name = re.sub(r"[^a-z0-9-]+", "-", exp_name).strip("-")
        if len(exp_name) < 3:
            exp_name = f"synth-{exp_name}"

        experiment_spec = ExperimentSpec(
            spec_id=spec_id,
            name=exp_name,
            purpose="craft",
            hypothesis="Synthesize task via meta-task scaffold and completeness verifier",
            task=task_ref,
            agent=agent,
            model=model,
            submitted_by=submitted_by or "authoring-metaloop",
        )

        queue = DirectoryQueue(self.repo_root / "queue")
        policy_path = self.repo_root / "policy/standing-approvals.yaml"
        gate = PolicyGate(load_policy(policy_path), repo_root=self.repo_root)

        dest_path, decision = queue.submit(
            experiment_spec,
            gate=gate,
            spent_today_usd=0.0,
        )
        return experiment_spec, dest_path, decision

    def harvest(self, job_dir_or_id: str | Path) -> Proposal:
        if isinstance(job_dir_or_id, Path):
            job_dir = job_dir_or_id
        else:
            job_dir = self.repo_root / "runs" / job_dir_or_id

        if not job_dir.exists():
            alt = self.repo_root / str(job_dir_or_id)
            if alt.exists():
                job_dir = alt
            else:
                alt_ev = self.repo_root / "research/evidence/runs" / str(job_dir_or_id)
                if alt_ev.exists():
                    job_dir = alt_ev
                else:
                    raise AuthoringError(f"job directory not found: {job_dir_or_id}")

        candidates = [
            job_dir / "artifacts/output/task",
            job_dir / "artifacts/task",
            job_dir / "output/task",
            job_dir / "task",
        ]
        task_pkg = None
        for cand in candidates:
            if cand.is_dir() and (cand / "task.toml").is_file():
                task_pkg = cand
                break

        if task_pkg is None:
            raise AuthoringError(f"no generated task artifact found in {job_dir}")

        verifier_passed = False
        verifier_dir = job_dir / "verifier"
        reward_file = verifier_dir / "reward.json"
        checks_file = verifier_dir / "checks.json"
        if reward_file.is_file():
            try:
                rdata = json.loads(reward_file.read_text(encoding="utf-8"))
                if isinstance(rdata, dict) and rdata.get("reward") == 1.0:
                    verifier_passed = True
            except Exception:
                pass
        elif checks_file.is_file():
            try:
                cdata = json.loads(checks_file.read_text(encoding="utf-8"))
                if isinstance(cdata, dict) and all(
                    v.get("passed") for v in cdata.values() if isinstance(v, dict)
                ):
                    verifier_passed = True
            except Exception:
                pass
        else:
            report = check_task_completeness(task_pkg)
            if report.get("passed"):
                verifier_passed = True

        if not verifier_passed:
            raise AuthoringError(
                f"harvest refused: completeness checker did not pass in {job_dir}"
            )

        job_id = job_dir.name
        job_manifest = job_dir / "manifest.json"
        job_result = job_dir / "result.json"
        spec_data: dict[str, Any] | None = None
        exemplar_name: str | None = None

        if job_manifest.is_file():
            try:
                m_raw = json.loads(job_manifest.read_text(encoding="utf-8"))
                if isinstance(m_raw, dict):
                    job_id = m_raw.get("job_id", job_id)
                    spec_data = m_raw.get("injected_spec") or m_raw.get("spec")
                    exemplar_name = m_raw.get("exemplar")
            except Exception:
                pass

        proposal_id = self._new_id()
        destination = self.quarantine / proposal_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copytree(task_pkg, destination, dirs_exist_ok=True, ignore=_ignore_copy)

        created = isoformat(self._now())
        seed_class: SeedClass = "craft-gap"
        if spec_data and spec_data.get("seed_class") in SEED_CLASSES:
            seed_class = spec_data["seed_class"]

        inputs: list[dict[str, Any]] = []
        target_file = (
            job_result
            if job_result.is_file()
            else (job_manifest if job_manifest.is_file() else None)
        )
        if target_file is not None:
            inputs.append(
                {
                    "path": _repo_relative(target_file, self.repo_root),
                    "id": job_id,
                    "digest": compute_file_digest(target_file),
                }
            )
        else:
            inputs.append(
                {
                    "path": _repo_relative(job_dir, self.repo_root),
                    "id": job_id,
                    "digest": None,
                }
            )
        proposal = Proposal(
            proposal_id=proposal_id,
            seed_class=seed_class,
            ref_task=exemplar_name,
            path=destination,
            outcome="proposed",
            version=read_task_version(destination / "task.toml") or "0.1.0",
            created_at=created,
            job_id=job_id,
            inputs=inputs,
            injected_spec=spec_data,
            exemplar=exemplar_name,
        )
        _atomic_write_json(destination / "proposal.json", proposal.manifest())
        upsert_ledger(
            self.ledger,
            QualificationRecord(
                proposal_id=proposal_id,
                seed_class=seed_class,
                ref_task=exemplar_name,
                outcome="proposed",
                created_at=created,
                updated_at=created,
            ),
        )
        return proposal
    def run_battery(self, proposal_id: str) -> BatteryReport:
        proposal = self.get(proposal_id)
        evidence_dir = proposal.path / "battery"
        checks: list[ControlResult] = []
        for check in BATTERY_CHECKS:
            result = self.runner.run(proposal.path, check, evidence_dir=evidence_dir)
            if result.check != check:
                raise AuthoringError(f"control runner returned {result.check!r} for {check!r}")
            checks.append(result)
        all_passed = all(item.passed for item in checks)
        expected = {
            "oracle": 1.0,
            "nop": 0.0,
            "fair_oracle": 1.0,
            "adversarial": 0.0,
        }
        for item in checks:
            if item.passed and item.reward != expected[item.check]:
                all_passed = False
        outcome: Outcome = "battery_passed" if all_passed else proposal.outcome
        if outcome == "registered":
            raise RegisterRefusal(proposal_id, outcome)
        if proposal.outcome == "craft_reviewed" and all_passed:
            outcome = "craft_reviewed"
        self._write_outcome(proposal, outcome)
        record = self._record_for(proposal_id)
        updated = record.model_copy(
            update={
                "battery_oracle": checks[0].passed,
                "battery_nop": checks[1].passed,
                "battery_fair_oracle": checks[2].passed,
                "battery_adversarial": checks[3].passed,
                "evidence_paths": [item.evidence_path for item in checks],
                "outcome": outcome,
                "updated_at": isoformat(self._now()),
            }
        )
        upsert_ledger(self.ledger, updated)
        return BatteryReport(
            proposal_id=proposal_id,
            outcome=outcome,
            checks=tuple(checks),
            all_passed=all_passed,
        )

    def review(self, proposal_id: str) -> ReviewReport:
        proposal = self.get(proposal_id)
        record = self._record_for(proposal_id)
        if record.outcome not in {"battery_passed", "craft_reviewed"}:
            raise AuthoringError(
                f"proposal {proposal_id!r} is {record.outcome!r}; "
                "review requires battery_passed"
            )
        score, reasons = score_review(proposal)
        evidence_path = proposal.path / "review.json"
        payload = {
            "proposal_id": proposal_id,
            "score": score,
            "reasons": reasons,
            "rubric": "craft-patterns/v1",
            "seed_class": proposal.seed_class,
            "target_facets": proposal.target_facets,
        }
        _atomic_write_json(evidence_path, payload)
        evidence = evidence_path.as_posix()
        paths = list(record.evidence_paths)
        if evidence not in paths:
            paths.append(evidence)
        updated = record.model_copy(
            update={
                "review_score": score,
                "evidence_paths": paths,
                "outcome": "craft_reviewed",
                "updated_at": isoformat(self._now()),
            }
        )
        upsert_ledger(self.ledger, updated)
        self._write_outcome(proposal, "craft_reviewed")
        return ReviewReport(
            proposal_id=proposal_id,
            outcome="craft_reviewed",
            score=score,
            reasons=tuple(reasons),
            evidence_path=evidence,
        )

    def register(self, proposal_id: str) -> None:
        """Fail-closed human gate. Never writes `registered`."""
        record = self._record_for(proposal_id)
        raise RegisterRefusal(proposal_id, record.outcome)

    def run_batch(
        self, count: int = 5, *, seeds: Sequence[SeedClass] | None = None
    ) -> list[BatchItem]:
        if count < 1:
            raise AuthoringError("batch count must be >= 1")
        cycle: Sequence[SeedClass] = seeds or ("mutation", "scenario", "craft-gap")
        items: list[BatchItem] = []
        for index in range(count):
            seed = cycle[index % len(cycle)]
            proposal = self.propose(seed)
            battery = self.run_battery(proposal.proposal_id)
            if not battery.all_passed:
                raise AuthoringError(
                    f"batch halted: {proposal.proposal_id} failed battery ({seed})"
                )
            review = self.review(proposal.proposal_id)
            items.append(
                BatchItem(
                    proposal_id=proposal.proposal_id,
                    seed_class=seed,
                    outcome=review.outcome,
                    battery=battery,
                    review=review,
                )
            )
        return items

    def _propose_mutation(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        source = resolve_registered_task(self.repo_root, ref)
        source_digest = _sha256_tree(source)
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=_ignore_copy)
        task_toml = destination / "task.toml"
        if not task_toml.is_file():
            raise AuthoringError(f"mutation source {source} has no task.toml")
        new_version = bump_version(read_task_version(task_toml))
        task_toml.write_text(rewrite_task_version(task_toml.read_text(), new_version))
        instruction = destination / "instruction.md"
        if instruction.is_file():
            instruction.write_text(
                instruction.read_text()
                + "\n\n## Mutation\n\n"
                + f"Versioned variant of `{_repo_relative(source, self.repo_root)}` "
                + f"at {new_version}. Source digest `{source_digest}`.\n"
            )
        after = _sha256_tree(source)
        if after != source_digest:
            raise AuthoringError(f"mutation edited source in place: {source}")
        return Proposal(
            proposal_id=proposal_id,
            seed_class="mutation",
            ref_task=source.name,
            path=destination,
            outcome="proposed",
            version=new_version,
            source_path=_repo_relative(source, self.repo_root),
            source_digest=source_digest,
            created_at=created,
        )

    def _propose_scenario(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        scenario = resolve_scenario(self.repo_root, ref)
        relative = _repo_relative(scenario, self.repo_root)
        body = scenario.read_text()
        title = next(
            (line[2:].strip() for line in body.splitlines() if line.startswith("# ")),
            scenario.stem,
        )
        excerpt = body.strip()
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000].rstrip() + "\n"
        _write_stub_task(
            destination,
            name=f"proposed-{scenario.stem}",
            version="0.1.0",
            instruction=(
                f"# {title}\n\n"
                f"Seeded from research scenario `{relative}`.\n\n"
                f"{excerpt}\n"
            ),
            separate=True,
            verifier_hint="pytest",
        )
        return Proposal(
            proposal_id=proposal_id,
            seed_class="scenario",
            ref_task=relative,
            path=destination,
            outcome="proposed",
            version="0.1.0",
            scenario_path=relative,
            created_at=created,
        )

    def _propose_craft_gap(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        facets = find_craft_gap(craft_parquet_path(self.derived_root))
        if ref is not None:
            facets = {**facets, "requested_ref": ref}
        _write_stub_task(
            destination,
            name=f"proposed-gap-{facets['verifier_type']}",
            version="0.1.0",
            instruction=(
                "# CRAFT coverage gap\n\n"
                "This proposal targets a facet combination with zero coverage "
                "in `derived/parquet/craft/craft.parquet`:\n\n"
                f"- verifier_type: `{facets['verifier_type']}`\n"
                f"- env_multi_container: `{facets['env_multi_container']}`\n"
                f"- pinned_deps: `{facets['pinned_deps']}`\n"
            ),
            separate=True,
            verifier_hint=str(facets["verifier_type"]),
            multi_container=bool(facets["env_multi_container"]),
            pinned=bool(facets["pinned_deps"]),
        )
        return Proposal(
            proposal_id=proposal_id,
            seed_class="craft-gap",
            ref_task=ref,
            path=destination,
            outcome="proposed",
            version="0.1.0",
            target_facets={name: facets[name] for name in GAP_AXES},
            created_at=created,
        )

    def _write_outcome(self, proposal: Proposal, outcome: Outcome) -> None:
        if outcome == "registered":
            raise RegisterRefusal(proposal.proposal_id, outcome)
        payload = proposal.manifest()
        payload["outcome"] = outcome
        _atomic_write_json(proposal.path / "proposal.json", payload)

    def _record_for(self, proposal_id: str) -> QualificationRecord:
        for record in self.records():
            if record.proposal_id == proposal_id:
                return record
        raise ProposalNotFoundError(f"proposal {proposal_id!r} has no ledger row")


def _write_stub_task(
    destination: Path,
    *,
    name: str,
    version: str,
    instruction: str,
    separate: bool,
    verifier_hint: str,
    multi_container: bool = False,
    pinned: bool = True,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    mode = 'environment_mode = "separate"' if separate else ""
    (destination / "task.toml").write_text(
        f"""schema_version = "1.0"

[task]
name = "{name}"
version = "{version}"

[metadata]
category = "proposed"

[verifier]
timeout_sec = 60.0
{mode}
"""
    )
    text = instruction if instruction.endswith("\n") else instruction + "\n"
    (destination / "instruction.md").write_text(text)
    environment = destination / "environment"
    environment.mkdir(exist_ok=True)
    pin = "@sha256:" + ("0" * 64) if pinned else ":3.12-slim"
    (environment / "Dockerfile").write_text(f"FROM python{pin}\n")
    if pinned:
        (environment / "requirements.txt").write_text("pytest==8.4.1\n")
    if multi_container:
        (environment / "docker-compose.yaml").write_text(
            "services:\n  main:\n    build: .\n  sidecar:\n    image: python:3.12-slim\n"
        )
    tests = destination / "tests"
    tests.mkdir(exist_ok=True)
    if verifier_hint == "pytest":
        (tests / "test_proposed.py").write_text(
            "def test_proposed_placeholder() -> None:\n    assert False, 'proposed task'\n"
        )
    elif verifier_hint == "golden_file":
        (tests / "golden.txt").write_text("expected\n")
        (tests / "test.sh").write_text("#!/bin/bash\ndiff -u golden.txt /app/output/answer.txt\n")
    else:
        (tests / "test.sh").write_text("#!/bin/bash\nexit 1\n")
    solution = destination / "solution"
    solution.mkdir(exist_ok=True)
    (solution / "solve.sh").write_text("#!/bin/bash\necho proposed\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.authoring",
        description="BUILDER authoring pipeline (WS-C). Halts at the human registry gate.",
    )
    parser.add_argument("--root", type=Path, default=None, help="repository root")
    parser.add_argument("--out", type=Path, default=None, help="derived parquet root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser("propose", help="seed a quarantined proposal")
    propose.add_argument(
        "--seed",
        default="craft-gap",
        choices=SEED_CLASSES,
        help="mutation | scenario | craft-gap",
    )
    propose.add_argument("--ref", default=None, help="source task or research scenario")
    propose.add_argument(
        "--via-harbor",
        action="store_true",
        help="assemble meta-task and submit through Harbor queue (purpose=craft)",
    )
    propose.add_argument("--agent", default="oracle", help="agent for Harbor execution")
    propose.add_argument("--model", default=None, help="model for Harbor execution")
    propose.add_argument("--spec", type=Path, default=None, help="custom spec JSON path")
    propose.add_argument("--exemplar", default=None, help="exemplar task name")

    harvest = subparsers.add_parser(
        "harvest",
        help="harvest a verified task from completed Harbor job into _proposed/",
    )
    harvest.add_argument("job", help="job directory or job ID to harvest")
    battery = subparsers.add_parser("battery", help="run the four local control checks")
    battery.add_argument("proposal_id")

    review = subparsers.add_parser("review", help="score a battery-passed proposal")
    review.add_argument("proposal_id")

    register = subparsers.add_parser(
        "register",
        help="refused: registration is human-only via evallab registry",
    )
    register.add_argument("proposal_id")

    batch = subparsers.add_parser("batch", help="propose → battery → review; halt at the gate")
    batch.add_argument("--count", type=int, default=5)
    return parser


def _pipeline_from_args(args: argparse.Namespace) -> AuthoringPipeline:
    root = args.root.resolve() if args.root is not None else repository_root()
    derived = args.out.resolve() if args.out is not None else None
    return AuthoringPipeline(root, derived_root=derived)


def _emit(payload: Any, *, as_json: bool, stream: Any = None) -> None:
    target = sys.stdout if stream is None else stream
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), file=target)
        return
    if isinstance(payload, str):
        print(payload, file=target)
        return
    print(json.dumps(payload, indent=2, sort_keys=True), file=target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        pipeline = _pipeline_from_args(args)
        if args.command == "propose":
            if args.via_harbor:
                custom_spec = (
                    json.loads(args.spec.read_text(encoding="utf-8"))
                    if args.spec and args.spec.is_file()
                    else None
                )
                res = pipeline.propose(
                    args.seed,
                    ref=args.ref,
                    via_harbor=True,
                    agent=args.agent,
                    model=args.model,
                    spec=custom_spec,
                    exemplar=args.exemplar,
                )
                assert isinstance(res, tuple)
                exp_spec, queue_path, decision = res
                _emit(
                    {
                        "spec_id": exp_spec.spec_id,
                        "name": exp_spec.name,
                        "purpose": exp_spec.purpose,
                        "task": exp_spec.task,
                        "agent": exp_spec.agent,
                        "queue_path": _repo_relative(queue_path, pipeline.repo_root),
                        "destination": queue_path.parent.name,
                        "admitted": decision.admitted,
                        "reason_code": decision.reason_code,
                    },
                    as_json=args.json,
                )
                return 0
            proposal = pipeline.propose(args.seed, ref=args.ref)
            assert isinstance(proposal, Proposal)
            _emit(
                {
                    "proposal_id": proposal.proposal_id,
                    "seed_class": proposal.seed_class,
                    "ref_task": proposal.ref_task,
                    "outcome": proposal.outcome,
                    "path": _repo_relative(proposal.path, pipeline.repo_root),
                },
                as_json=args.json,
            )
            return 0
        if args.command == "harvest":
            harvested = pipeline.harvest(args.job)
            _emit(
                {
                    "proposal_id": harvested.proposal_id,
                    "seed_class": harvested.seed_class,
                    "ref_task": harvested.ref_task,
                    "outcome": harvested.outcome,
                    "path": _repo_relative(harvested.path, pipeline.repo_root),
                    "job_id": harvested.job_id,
                },
                as_json=args.json,
            )
            return 0
        if args.command == "battery":
            report = pipeline.run_battery(args.proposal_id)
            _emit(report.as_dict(), as_json=args.json)
            return 0 if report.all_passed else 1
        if args.command == "review":
            report = pipeline.review(args.proposal_id)
            _emit(report.as_dict(), as_json=args.json)
            return 0
        if args.command == "register":
            pipeline.register(args.proposal_id)
            raise AuthoringError("register returned without refusing")
        items = pipeline.run_batch(args.count)
        payload = {
            "count": len(items),
            "outcome": "craft_reviewed",
            "halt": REGISTER_REFUSAL,
            "proposals": [
                {
                    "proposal_id": item.proposal_id,
                    "seed_class": item.seed_class,
                    "outcome": item.outcome,
                    "review_score": item.review.score,
                }
                for item in items
            ],
        }
        _emit(payload, as_json=args.json)
        print(REGISTER_REFUSAL, file=sys.stderr)
        return 0
    except RegisterRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AuthoringError as exc:
        print(f"authoring: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
