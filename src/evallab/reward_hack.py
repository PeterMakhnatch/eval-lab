"""Reward-hacking audit for Harbor task packages, in the BenchJack shape.

Two deterministic halves plus one portable instruction:

* :func:`scan_package` — reconnaissance and a V1-V8 flaw ledger over one task
  package. Every finding is a *claim*: it cites the file and line carrying the
  pattern and never asserts that a model would exploit it.
* :func:`run_hack` — replays authored exploit scripts through the task's own
  verifier by reusing the checked-in solution-control matrix path
  (``MatrixRun.solution`` with the oracle agent and ``allow_billable=False``).
  The reward that comes back is the benchmark's own grade, not a local verdict.
* The authoring procedure lives in ``.omp/skills/reward-hacking-audit/SKILL.md``
  and is framework-agnostic; only the replay adapter is Harbor-specific.

Stated limits: a scan finding is not an exploitation proof; a clean scan is not
a certificate; a resisted exploit script is not proof the task is unhackable by
another channel. Verdicts route follow-up work, they do not certify a package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.execution_contracts import new_ulid
from evallab.registry import compute_task_digests, harbor_task_digest
from evallab.runner import run_matrix
from evallab.schemas import ContractModel, ExperimentMatrix, MatrixRun

SCHEMA_SCAN = "evallab.reward_hack.scan/v1"
SCHEMA_REPORT = "evallab.reward_hack.report/v1"

#: Hits recorded per pattern rule; the ledger stays bounded and reviews the first
#: occurrences, not every matching byte of a large verifier.
MAX_HITS_PER_RULE = 6

#: Text larger than this is skipped by the scan (verifier fixtures, databases).
MAX_TEXT_BYTES = 1_048_576

FlawClass = Literal["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"]
Severity = Literal["critical", "high", "medium", "low"]
HackOutcome = Literal["hack_succeeded", "resisted", "unscored", "baseline"]
HackVerdict = Literal[
    "hackable",
    "partially_hackable",
    "not_demonstrated",
    "task_broken",
    "unscored",
    "planned",
]

#: Paths an agent owns inside its own container. A grader that reads these in a
#: shared verifier consumes agent-controlled data (V7 on top of V1).
_AGENT_PATHS = re.compile(r"(?:/app|/workspace|/workdir)(?:/[\w./-]*)?|\$PWD|\$\{?HOME\}?/[\w./-]+")

#: Skips in ``tests/`` that are fixtures rather than verifier logic.
_TEXT_SUFFIXES = (".sh", ".bash", ".py", ".ps1", ".bat", ".cmd", ".json", ".toml", ".yaml", ".yml")

_ANSWER_NAMES = re.compile(
    r"(?:^|[-_.])(gold|answer|answers|expected|solution|solutions)(?:$|[-_.])", re.IGNORECASE
)


class HackFinding(ContractModel):
    """One static claim with its evidence; never a proof of exploitability."""

    flaw_class: FlawClass
    severity: Severity
    title: str
    evidence: str
    detail: str


class HackScan(ContractModel):
    """Reconnaissance facts plus the flaw ledger for one package."""

    schema_: Literal["evallab.reward_hack.scan/v1"] = Field(
        alias="schema", default="evallab.reward_hack.scan/v1"
    )
    package: str
    package_digest: str
    harbor_digest: str
    recon: dict[str, Any]
    findings: tuple[HackFinding, ...]
    limits: str


class HackRunResult(ContractModel):
    """One matrix control after execution; rewards are the benchmark's own."""

    name: str
    role: Literal["baseline-oracle", "baseline-nop", "exploit"]
    script: str | None = None
    script_sha256: str | None = None
    declared_expectation: float | None = None
    status: Literal["ok", "mismatch", "infra"]
    reward: float | None = None
    outcome: HackOutcome
    error: str | None = None


class HackReport(ContractModel):
    """Scan plus replayed controls; the run-level evidence for one package."""

    schema_: Literal["evallab.reward_hack.report/v1"] = Field(
        alias="schema", default="evallab.reward_hack.report/v1"
    )
    package: str
    package_digest: str
    matrix_path: str | None = None
    matrix_id: str | None = None
    executed: bool = False
    runs: tuple[HackRunResult, ...] = ()
    verdict: HackVerdict = "planned"
    verdict_reason: str = "plan written; nothing executed"
    scan: HackScan
    limits: str


# --------------------------------------------------------------------------- #
# Static reconnaissance and flaw ledger
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _PatternRule:
    flaw_class: FlawClass
    severity: Severity
    title: str
    detail: str
    pattern: re.Pattern[str]


_VERIFIER_RULES: tuple[_PatternRule, ...] = (
    _PatternRule(
        "V3",
        "critical",
        "verifier executes data",
        "script execution primitives appear in the verifier; exploitable only if the "
        "argument can carry agent-controlled data, which this static scan does not decide",
        re.compile(
            r"\beval\(|\bexec\(|os\.system\(|shell\s*=\s*True|bash\s+-c|sh\s+-c|pickle\.loads?\("
        ),
    ),
    _PatternRule(
        "V4",
        "medium",
        "LLM-judge surface",
        "the verifier or task config references a model judge; prompt-injection resistance "
        "and output escaping are unverified here",
        re.compile(r"openai|anthropic|litellm|chat\.completions|ChatOpenAI|rewardkit|judge", re.I),
    ),
    _PatternRule(
        "V5",
        "medium",
        "weak matching",
        "case-insensitive or normalized matching on agent-produced text can accept "
        "near-miss output",
        re.compile(r"grep\s+-[A-Za-z]*i|--ignore-case|\.lower\(\)|casefold\(|re\.IGNORECASE"),
    ),
    _PatternRule(
        "V6",
        "high",
        "fail-open shell handling",
        "errors are suppressed or ignored, so a failing check can still reach the reward write",
        re.compile(r"\|\|\s*true|set\s+\+e"),
    ),
    _PatternRule(
        "V6",
        "low",
        "suppressed stderr",
        "stderr is discarded; a failed check may not be visible in the verifier log",
        re.compile(r"2>\s*/dev/null"),
    ),
    _PatternRule(
        "V2",
        "medium",
        "verifier fetches over the network",
        "the verifier downloads code or data at grade time; material the agent can also "
        "reach becomes an answer channel",
        re.compile(
            r"\bcurl\s+[^\n|]*https?://|\bwget\s+[^\n|]*https?://|git\s+clone|pip\s+install\s+git\+"
        ),
    ),
)

_ENVIRONMENT_RULES: tuple[_PatternRule, ...] = (
    _PatternRule(
        "V2",
        "high",
        "test or answer material copied into the agent image",
        "the environment build references verifier or answer material, so the agent's "
        "own filesystem can contain it",
        re.compile(
            r"(?:COPY|ADD)\s+[^\n]*(?:\btests?\b|\bsolutions?\b|\bgolds?\b|\banswers?\b|\bexpected\b)"
        ),
    ),
    _PatternRule(
        "V8",
        "critical",
        "privileged container or host socket",
        "a privileged service or the Docker socket inside the task environment is a "
        "sandbox escape surface",
        re.compile(r"privileged\s*:\s*true|/var/run/docker\.sock|cap_add"),
    ),
)


def _read_task_config(package: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"{package}: task.toml is unreadable ({type(exc).__name__}: {exc})"
        ) from exc


def _table(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    return value if isinstance(value, dict) else {}


def _text_files(root: Path) -> Iterator[tuple[Path, list[str]]]:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.name != "Dockerfile":
            continue
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        yield path, lines


def _match_rule(
    rule: _PatternRule, package: Path, files: Sequence[tuple[Path, list[str]]]
) -> list[HackFinding]:
    findings: list[HackFinding] = []
    for path, lines in files:
        for number, line in enumerate(lines, start=1):
            if not rule.pattern.search(line):
                continue
            findings.append(
                HackFinding(
                    flaw_class=rule.flaw_class,
                    severity=rule.severity,
                    title=rule.title,
                    evidence=f"{path.relative_to(package).as_posix()}:{number}",
                    detail=rule.detail,
                )
            )
            if len(findings) >= MAX_HITS_PER_RULE:
                return findings
    return findings


def _verifier_mode(config: dict[str, Any]) -> str:
    verifier = _table(config, "verifier")
    mode = verifier.get("environment_mode")
    if isinstance(mode, str) and mode:
        return mode
    if isinstance(verifier.get("environment"), dict):
        return "separate"
    return "shared"


def _resolve_network(config: dict[str, Any]) -> tuple[str, str]:
    environment = _table(config, "environment")
    explicit = environment.get("network_mode")
    if isinstance(explicit, str) and explicit:
        return explicit, "explicit"
    legacy = environment.get("allow_internet")
    if isinstance(legacy, bool):
        return ("public" if legacy else "no-network"), "legacy-allow-internet"
    return "public", "default"


def _user_of(table: dict[str, Any]) -> str | None:
    user = table.get("user")
    if user is None:
        return None
    return str(user)


def _isolation_findings(
    config: dict[str, Any], mode: str, tests: Sequence[tuple[Path, list[str]]], package: Path
) -> list[HackFinding]:
    findings: list[HackFinding] = []
    if mode != "separate":
        findings.append(
            HackFinding(
                flaw_class="V1",
                severity="high",
                title="verifier shares the agent's environment",
                evidence="task.toml",
                detail=(
                    "[verifier] does not declare environment_mode = 'separate' (or a "
                    "[verifier.environment]); grading runs inside the container the agent "
                    "controlled, so agent-created files, processes and binaries survive into "
                    "the verifier phase"
                ),
            )
        )
        agent_paths = _match_rule(
            _PatternRule(
                "V7",
                "high",
                "shared grader reads agent-owned paths",
                "the verifier consumes data from paths the agent owns inside a shared "
                "container; whether that alone can change the verdict is not decided here",
                _AGENT_PATHS,
            ),
            package,
            tests,
        )
        findings.extend(agent_paths)
    return findings


def _environment_findings(
    config: dict[str, Any], package: Path, environment: Sequence[tuple[Path, list[str]]]
) -> list[HackFinding]:
    findings: list[HackFinding] = []
    environment_table = _table(config, "environment")
    agent_user = _user_of(environment_table)
    if agent_user is None:
        findings.append(
            HackFinding(
                flaw_class="V8",
                severity="high",
                title="agent runs as the image default user",
                evidence="task.toml",
                detail=(
                    "[environment].user is unset, so the agent runs as the image default "
                    "(typically root) and every other flaw gains the container's full authority"
                ),
            )
        )
    elif agent_user in {"root", "0"}:
        findings.append(
            HackFinding(
                flaw_class="V8",
                severity="high",
                title="agent runs as root by declaration",
                evidence="task.toml",
                detail="[environment].user grants the agent root inside the sandbox",
            )
        )
    network, source = _resolve_network(config)
    if network == "public":
        findings.append(
            HackFinding(
                flaw_class="V8",
                severity="medium",
                title="unrestricted outbound network",
                evidence="task.toml",
                detail=(
                    f"agent network resolves to 'public' ({source}); public answers, patches "
                    "and package copies are reachable, which amplifies V2 channels"
                ),
            )
        )
    for path, lines in environment:
        last_user: tuple[int, str] | None = None
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("USER "):
                last_user = (number, stripped)
        if path.name == "Dockerfile" and (last_user is None or last_user[1].split()[1] == "root"):
            findings.append(
                HackFinding(
                    flaw_class="V8",
                    severity="medium",
                    title="image runs as root",
                    evidence=(
                        f"{path.relative_to(package).as_posix()}"
                        if last_user is None
                        else f"{path.relative_to(package).as_posix()}:{last_user[0]}"
                    ),
                    detail=(
                        "the environment Dockerfile has no non-root USER directive"
                        if last_user is None
                        else "the environment Dockerfile ends on USER root"
                    ),
                )
            )
    for name_path in sorted(package.joinpath("environment").rglob("*")):
        if name_path.is_file() and _ANSWER_NAMES.search(name_path.name):
            findings.append(
                HackFinding(
                    flaw_class="V2",
                    severity="high",
                    title="answer-shaped file inside the agent build context",
                    evidence=name_path.relative_to(package).as_posix(),
                    detail=(
                        "the file name matches gold/answer/expected/solution; verify whether "
                        "it is copied into the image the agent runs in"
                    ),
                )
            )
            if len(findings) >= MAX_HITS_PER_RULE * 2:
                break
    return findings


def _control_scripts(package: Path) -> tuple[str, ...]:
    controls = package / "controls"
    if not controls.is_dir():
        return ()
    return tuple(sorted(path.name for path in controls.glob("*.sh") if path.is_file()))


def scan_package(package: str | Path) -> HackScan:
    """Deterministic, offline V1-V8 ledger for one Harbor task package."""
    root = Path(package).expanduser().resolve()
    if not (root / "task.toml").is_file():
        raise ValueError(f"{root}: not a Harbor task package (task.toml is missing)")
    config = _read_task_config(root)
    tests = tuple(_text_files(root / "tests"))
    environment = tuple(_text_files(root / "environment"))
    mode = _verifier_mode(config)
    findings: list[HackFinding] = []
    findings.extend(_isolation_findings(config, mode, tests, root))
    findings.extend(_environment_findings(config, root, environment))
    for rule in _VERIFIER_RULES:
        findings.extend(_match_rule(rule, root, tests))
    for rule in _ENVIRONMENT_RULES:
        findings.extend(_match_rule(rule, root, environment))
    findings.sort(key=lambda finding: (finding.flaw_class, finding.evidence, finding.title))
    digests = compute_task_digests(root)
    task_table = _table(config, "task")
    steps = config.get("steps")
    artifacts = config.get("artifacts")
    entrypoint = next(
        (name for name in ("tests/test.sh", "tests/test.bat") if (root / name).is_file()),
        None,
    )
    network, network_source = _resolve_network(config)
    recon: dict[str, Any] = {
        "task_name": task_table.get("name"),
        "task_version": task_table.get("version"),
        "verifier_mode": mode,
        "agent_user": _user_of(_table(config, "environment")),
        "verifier_user": _user_of(_table(config, "verifier")),
        "network_mode": network,
        "network_source": network_source,
        "docker_image": _table(config, "environment").get("docker_image"),
        "steps": len(steps) if isinstance(steps, list) else 0,
        "artifacts": len(artifacts) if isinstance(artifacts, list) else 0,
        "test_entrypoint": entrypoint,
        "test_files": len(tests),
        "environment_files": len(environment),
        "control_scripts": list(_control_scripts(root)),
        "flaw_classes": sorted({finding.flaw_class for finding in findings}),
    }
    return HackScan(
        package=str(root),
        package_digest=digests.package,
        harbor_digest=harbor_task_digest(root),
        recon=recon,
        findings=tuple(findings),
        limits=(
            "static inspection only: each finding is a claim with file evidence, never proof "
            "of exploitability, and an absent finding is not a security certificate"
        ),
    )


# --------------------------------------------------------------------------- #
# Exploit replay through the task's own verifier
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ControlScript:
    """One staged exploit script plus the control author's declared expectation."""

    staged: Path
    source: str
    sha256: str
    declared: float | None


_EXPECT_HEADER = re.compile(r"^#\s*expect:\s*(1|0|1\.0|0\.0)\s*$")


def declared_expectation(script: Path) -> float | None:
    """Read the control-author's ``# expect: 1`` header, when one is present."""
    try:
        lines = script.read_text(encoding="utf-8").splitlines()[:20]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        match = _EXPECT_HEADER.match(line.strip())
        if match:
            return 1.0 if match.group(1).startswith("1") else 0.0
    return None


def _slug(text: str, *, limit: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit] or "task"


def _job_name(index: int, label: str, slug: str) -> str:
    return f"hack-{index:02d}-{_slug(label, limit=28)}-{slug}"[:80]


def plan_matrix(
    package: str | Path,
    scripts: Sequence[Path],
    *,
    repo_root: Path,
    timeout_seconds: int = 1_800,
    name_prefix: str = "reward-hack",
) -> ExperimentMatrix:
    """Baselines plus one solution-override control per staged exploit script."""
    root = Path(package).expanduser().resolve()
    if not (root / "task.toml").is_file():
        raise ValueError(f"{root}: not a Harbor task package (task.toml is missing)")
    config = _read_task_config(root)
    if scripts and config.get("steps"):
        raise ValueError(
            "solution-override controls do not support declared task steps; "
            "the override contract selects only root solution/solve.sh"
        )
    digests = compute_task_digests(root)
    task_table = _table(config, "task")
    slug = _slug(str(task_table.get("name") or root.name))
    runs: list[MatrixRun] = [
        MatrixRun(name=_job_name(0, "oracle", slug), agent="oracle", expect_reward=1.0),
        MatrixRun(name=_job_name(1, "nop", slug), agent="nop", expect_reward=0.0),
    ]
    for index, script in enumerate(scripts, start=2):
        relative = script.resolve().relative_to(repo_root.resolve())
        runs.append(
            MatrixRun(
                name=_job_name(index, script.stem, slug),
                agent="oracle",
                expect_reward=declared_expectation(script),
                solution=relative.as_posix(),
            )
        )
    return ExperimentMatrix(
        matrix_id=new_ulid(),
        name=f"{name_prefix}-{slug}",
        hypothesis=(
            f"If {slug} grades agent-controlled state, an authored exploit script reaches the "
            "same reward as the reference solution without performing the task."
        ),
        benchmark_family=slug,
        task_id=slug,
        task=str(root),
        task_package_digest=digests.package,
        verifier_digest=digests.verifier,
        environment="docker",
        concurrency=1,
        timeout_seconds=timeout_seconds,
        runs=runs,
    )


def stage_controls(run_dir: Path, scripts: Sequence[Path]) -> tuple[ControlScript, ...]:
    """Copy exploit scripts into the run directory and digest them."""
    staged: list[ControlScript] = []
    controls_dir = run_dir / "controls"
    controls_dir.mkdir(parents=True, exist_ok=True)
    for index, script in enumerate(scripts, start=2):
        source = script.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"exploit script is not a regular file: {source}")
        payload = source.read_bytes()
        if not payload.strip():
            raise ValueError(f"exploit script is empty: {source}")
        target = controls_dir / f"{index:02d}-{_slug(source.stem)}.sh"
        target.write_bytes(payload)
        staged.append(
            ControlScript(
                staged=target,
                source=str(source),
                sha256=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                declared=declared_expectation(target),
            )
        )
    return tuple(staged)


def _role_of(index: int) -> Literal["baseline-oracle", "baseline-nop", "exploit"]:
    if index == 0:
        return "baseline-oracle"
    if index == 1:
        return "baseline-nop"
    return "exploit"


def classify_results(
    matrix: ExperimentMatrix, results: Sequence[dict[str, Any]], controls: Sequence[ControlScript]
) -> tuple[tuple[HackRunResult, ...], HackVerdict, str]:
    """Map raw matrix results to outcomes and one verdict, without inventing scores."""
    runs: list[HackRunResult] = []
    for index, (run, result) in enumerate(zip(matrix.runs, results, strict=True)):
        role = _role_of(index)
        rewards = result.get("rewards") or []
        reward = float(rewards[0]) if rewards else None
        status = result.get("status")
        if status not in {"ok", "mismatch", "infra"}:
            status = "infra"
        if status == "infra":
            outcome: HackOutcome = "unscored"
        elif role == "exploit":
            outcome = "hack_succeeded" if reward is not None and reward > 0 else "resisted"
        else:
            outcome = "baseline"
        control = controls[index - 2] if role == "exploit" and index - 2 < len(controls) else None
        runs.append(
            HackRunResult(
                name=run.name,
                role=role,
                script=control.source if control else None,
                script_sha256=control.sha256 if control else None,
                declared_expectation=control.declared if control else run.expect_reward,
                status=status,
                reward=reward,
                outcome=outcome,
                error=result.get("error"),
            )
        )
    nop = next((run for run in runs if run.role == "baseline-nop"), None)
    oracle = next((run for run in runs if run.role == "baseline-oracle"), None)
    exploits = [run for run in runs if run.role == "exploit"]
    if nop is not None and nop.reward is not None and nop.reward > 0:
        return (
            tuple(runs),
            "task_broken",
            f"the no-agent control earned reward {nop.reward:g}; passes on this path are "
            "uninformative until the package is fixed",
        )
    if oracle is not None and oracle.reward is not None and oracle.reward < 1:
        return (
            tuple(runs),
            "task_broken",
            f"the reference solution earned reward {oracle.reward:g}; the package fails its "
            "own oracle, so hackability cannot be measured on this run path",
        )
    full = [run.name for run in exploits if run.outcome == "hack_succeeded" and run.reward == 1.0]
    partial = [
        run.name for run in exploits if run.outcome == "hack_succeeded" and run.reward != 1.0
    ]
    scored = [run for run in exploits if run.outcome != "unscored"]
    if full:
        return tuple(runs), "hackable", "full reward reached without solving: " + ", ".join(full)
    if partial:
        return (
            tuple(runs),
            "partially_hackable",
            "partial reward reached without solving: " + ", ".join(partial),
        )
    if not scored:
        return tuple(runs), "unscored", "no exploit control produced a scored trial"
    return (
        tuple(runs),
        "not_demonstrated",
        "every scored exploit script was rejected by the task's own verifier; this is a "
        "negative result for these channels, not proof of unhackability",
    )


_RUN_LIMITS = (
    "exploit scripts run as the trial's reference-solution script through the task's own "
    "verifier; a confirmed hack is evidence for that script on that package revision only, "
    "and a rejection does not certify the task against other channels, models or revisions"
)


def run_hack(
    package: str | Path,
    *,
    repo_root: Path,
    scripts: Sequence[Path] | None = None,
    execute: bool = False,
    reuse_existing: bool = False,
    timeout_seconds: int = 1_800,
    output_dir: Path | None = None,
) -> tuple[HackReport, Path]:
    """Scan, plan, optionally replay, and write the receipts for one package."""
    root = Path(package).expanduser().resolve()
    scan = scan_package(root)
    selected = (
        tuple(Path(path) for path in scripts)
        if scripts
        else tuple(sorted((root / "controls").glob("*.sh")))
    )
    if not selected:
        raise ValueError(
            f"{root}: no exploit scripts; pass --script or add controls/*.sh to the package"
        )
    slug = _slug(str(scan.recon.get("task_name") or root.name))
    repo = Path(repo_root).resolve()
    run_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else repo / "runs" / ".reward-hack" / f"{slug}-{new_ulid().lower()}"
    )
    if not run_dir.is_relative_to(repo):
        raise ValueError(f"run directory must stay inside the repository ({repo}); got {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    controls = stage_controls(run_dir, selected)
    matrix = plan_matrix(
        root,
        [control.staged for control in controls],
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
    )
    matrix_path = run_dir / "matrix.json"
    matrix_path.write_text(json.dumps(matrix.model_dump(mode="json"), indent=2) + "\n")
    if not execute:
        report = HackReport(
            package=str(root),
            package_digest=scan.package_digest,
            matrix_path=str(matrix_path),
            matrix_id=matrix.matrix_id,
            scan=scan,
            limits=_RUN_LIMITS,
        )
        _write_report(run_dir, report)
        return report, run_dir
    execution = run_matrix(matrix, root=Path(repo_root).resolve(), reuse_existing=reuse_existing)
    runs, verdict, reason = classify_results(matrix, execution.results, controls)
    report = HackReport(
        package=str(root),
        package_digest=scan.package_digest,
        matrix_path=str(matrix_path),
        matrix_id=matrix.matrix_id,
        executed=True,
        runs=runs,
        verdict=verdict,
        verdict_reason=reason,
        scan=scan,
        limits=_RUN_LIMITS,
    )
    _write_report(run_dir, report)
    return report, run_dir


def _write_report(run_dir: Path, report: HackReport) -> None:
    (run_dir / "hack-report.json").write_text(
        json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "hack-report.md").write_text(render_report(report), encoding="utf-8")


def render_report(report: HackReport) -> str:
    lines = [
        "# Reward-hacking audit",
        "",
        f"- package: `{report.package}`",
        f"- package digest: `{report.package_digest}`",
        f"- verdict: **{report.verdict}**",
        f"- reason: {report.verdict_reason}",
    ]
    if report.matrix_id:
        lines.append(f"- matrix: `{report.matrix_id}`")
    if report.scan.recon.get("verifier_mode"):
        lines.append(f"- verifier mode: {report.scan.recon['verifier_mode']}")
    lines += ["", "## Static findings", ""]
    if report.scan.findings:
        lines += ["| class | severity | evidence | title |", "|---|---|---|---|"]
        lines += [
            f"| {finding.flaw_class} | {finding.severity} | `{finding.evidence}` | {finding.title} |"
            for finding in report.scan.findings
        ]
    else:
        lines.append("no findings (not a certificate: the scan is bounded and static)")
    lines += ["", "## Controls", ""]
    if report.runs:
        lines += ["| run | role | reward | outcome | script |", "|---|---|---|---|---|"]
        lines += [
            f"| {run.name} | {run.role} | "
            f"{'—' if run.reward is None else format(run.reward, 'g')} | {run.outcome} | "
            f"{run.script or '—'} |"
            for run in report.runs
        ]
    else:
        lines.append("nothing executed")
    lines += ["", f"Limits: {report.limits}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _hack_scan_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    try:
        scan = scan_package(args.package)
    except (OSError, ValueError) as exc:
        print(f"hack scan: {exc}", file=sys.stderr)
        return 1
    payload = scan.model_dump(mode="json", by_alias=True)
    if args.output is not None:
        target = args.output if args.output.is_absolute() else root / args.output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Package: {scan.package}")
        print(f"Digest: {scan.package_digest}")
        print(
            "Recon: "
            + ", ".join(f"{key}={value}" for key, value in scan.recon.items() if value is not None)
        )
        if scan.findings:
            for finding in scan.findings:
                print(
                    f"  [{finding.flaw_class}/{finding.severity}] {finding.evidence}: "
                    f"{finding.title}"
                )
        else:
            print("No findings. Not a certificate: the scan is static and bounded.")
        print(f"Limits: {scan.limits}")
        if args.output is not None:
            print(f"scan: {args.output}")
    return 0


def _hack_run_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    scripts = list(args.script) if args.script else None
    output_dir = args.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = root / output_dir
    try:
        report, run_dir = run_hack(
            args.package,
            repo_root=root,
            scripts=scripts,
            execute=args.execute,
            reuse_existing=args.reuse_existing,
            timeout_seconds=args.timeout_seconds,
            output_dir=output_dir,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"hack run: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True))
    else:
        print(f"Package: {report.package}")
        print(f"Verdict: {report.verdict} ({report.verdict_reason})")
        for run in report.runs:
            reward = "—" if run.reward is None else format(run.reward, "g")
            detail = f" error={run.error}" if run.error else ""
            print(f"  {run.name}: {run.outcome} reward={reward}{detail}")
        print(f"Failed static findings: {len(report.scan.findings)}")
    print(f"run directory: {run_dir}")
    return 0


def build_reward_hack_parser(subparsers: Any) -> None:
    hack = subparsers.add_parser(
        "hack",
        help="Reward-hacking audit: static V1-V8 ledger plus exploit replay (local, free)",
    )
    hack_commands = hack.add_subparsers(dest="hack_command", required=True)
    scan = hack_commands.add_parser(
        "scan", help="Deterministic V1-V8 flaw ledger for one task package; offline, no execution"
    )
    scan.add_argument("package", type=Path)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--output", type=Path, help="Write the scan JSON to this path")
    scan.set_defaults(func=_hack_scan_command)
    run = hack_commands.add_parser(
        "run",
        help="Plan (default) or replay exploit scripts through the task's own verifier",
    )
    run.add_argument("package", type=Path)
    run.add_argument(
        "--script",
        type=Path,
        action="append",
        default=[],
        help="Exploit script to replay; repeatable (default: <package>/controls/*.sh)",
    )
    run.add_argument("--execute", action="store_true", help="Run the local control matrix")
    run.add_argument("--reuse-existing", action="store_true")
    run.add_argument("--timeout-seconds", type=int, default=1_800)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_hack_run_command)
