"""Deterministic, model-free task lint: ``evallab qa env-lint <task-dir>``.

Static checks (no execution) plus optional dynamic controls. Every hit is a
``qa.finding/v1`` with ``file:line`` evidence, and a ``leads.json`` the qa env
harness can consume directly as located leads for an agent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from evallab.qa.models import Finding, FindingSubject, Reviewer, dump_finding

AGENT = "env-lint"
_TEXT = {".py", ".sh", ".toml", ".md", ".txt", ".yaml", ".yml", ".json", ""}


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _files(task: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(task.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            out[p.relative_to(task).as_posix()] = p
    return out


def _hits(files: dict[str, Path], scope: tuple[str, ...], pat: str) -> list[tuple[str, int, str]]:
    rx = re.compile(pat, re.I)
    out = []
    for rel, p in files.items():
        if scope and not rel.startswith(scope):
            continue
        if p.suffix not in _TEXT and p.name != "Dockerfile":
            continue
        for i, line in enumerate(_lines(p), 1):
            if rx.search(line):
                out.append((rel, i, line.strip()[:160]))
    return out


def lint_task(task_dir: Path, *, dynamic: bool = False) -> list[Finding]:
    task = task_dir.expanduser().resolve(strict=True)
    files = _files(task)
    task_id = task.name
    findings: list[Finding] = []

    def add(check: str, category: str, severity: str, summary: str, hits: list, *, verdict: str = "fail") -> None:
        if not hits:
            return
        refs = [f"{r}:{n}" for r, n, _ in hits[:6]]
        findings.append(Finding(
            subject=FindingSubject(kind="task", task_id=task_id), stage=f"env-lint:{check}",
            verdict=verdict, category=category, severity=severity if verdict == "fail" else None,
            fault="eval" if verdict == "fail" else None, summary=f"{summary} [{hits[0][2]}]",
            evidence_refs=refs, reviewer=Reviewer(agent=AGENT, model=None, cost_usd=0.0),
            measurement="measured", root_cause=[check]))

    tests = ("tests/",)
    env = ("environment/", "tests/")
    # grader writable / runs agent code
    add("grader_writable", "tamperable", "high",
        "Grader/test files appear writable or co-located with agent workspace",
        _hits(files, env, r"chmod\s+(-R\s+)?[0-7]*7[0-7]{2}\b|COPY\s+tests|cp\s+.*tests.*(/app|/workspace)") +
        _hits(files, ("task.toml",), r"environment_mode\s*=\s*\"shared\"|separate\s*=\s*false"))
    add("grader_runs_agent_code", "tamperable", "high",
        "Grader executes/imports code from the agent workspace",
        _hits(files, tests, r"(from|import)\s+\S*(app|workspace|solution)|/app/\S+\.py|importlib.*(/app|workspace)|exec\(|subprocess.*(/app|python.*\.py)"))
    # oracle readable
    if any(r.startswith("solution/") for r in files):
        add("oracle_readable", "reachable", "high",
            "Reference solution present in task package (readable if shipped to agent view)",
            _hits(files, ("environment/",), r"COPY\s+.*solution|solution/") or
            [(r, 1, "solution file present") for r in files if r.startswith("solution/")][:1])
    # root / network
    add("agent_root", "tamperable", "medium", "Agent appears to run as root (no non-root USER)",
        [(r, n, t) for r, n, t in _hits(files, ("environment/",), r"^USER\s+root|user\s*=\s*.root.|run_as.*root")] or
        ([(r, 1, "no USER directive in Dockerfile")] if any(r.endswith("Dockerfile") for r in files) and not _hits(files, ("environment/",), r"^USER\s+(?!root)") else []))
    add("network_access", "reachable", "medium", "Network access not disabled / network use in environment",
        _hits(files, env, r"curl\s|wget\s|pip install|apt-get|urllib|requests\.get|network\s*=\s*.enabled.|allow.*internet"))
    # timing windows
    add("timing_window", "weak", "medium", "Grader timing window / untimed or masked measurement",
        _hits(files, tests, r"perf_counter|time\.time\(\)|timeout|elapsed|speedup|faster than|time_limit|untimed|warmup|skip.*tim|mask"))
    # subset checks
    add("subset_check", "weak", "high", "Test checks only a subset/threshold of outputs",
        _hits(files, tests, r">=\s*\d+\s*of\s*\d+|>\s*\d+\s*of\s*\d+|at least\s+\d+|len\(.+\)\s*>=\s*\d+|count\s*>=\s*\d+|sum\(.+\)\s*>|majority|threshold"))
    # git history
    if (task / ".git").exists() or _hits(files, (), r"\.git/"):
        add("git_history", "reachable", "medium", "Git history present in task package (answer-leak risk)",
            [( ".git", 1, ".git present")] if (task / ".git").exists() else [])
    # flaky-looking assertions
    add("flaky_assertion", "weak", "low", "Flaky-looking assertion (time/random/network in tests)",
        _hits(files, tests, r"sleep\(|random\.|np\.random|requests\.|urllib|socket\.|time\.time\(\).*<=|assert.*<\s*\d+\s*#.*time"))
    # unstated-requirement candidates: instruction obligations with no test reference
    instr = task / "instruction.md"
    if instr.is_file():
        test_text = "\n".join("\n".join(_lines(p)) for r, p in files.items() if r.startswith("tests/")).lower()
        for i, line in enumerate(_lines(instr), 1):
            m = re.findall(r"\b(must|shall|required to|ensure that|exactly|sorted|descending|ascending|case-insensitive|deduplicat\w*|round\w* to|format\w* as)\b[^.\n]{0,80}", line, re.I)
            for kw in m:
                key = re.escape(kw.lower().split()[0])
                if not re.search(key, test_text):
                    add("unstated_requirement", "prompt_misaligned", "low",
                        f"Instruction requirement '{kw}' has no matching test reference (candidate)",
                        [( "instruction.md", i, line.strip()[:160])])
                    break
            if any(f.stage == "env-lint:unstated_requirement" for f in findings):
                break
    if dynamic:
        findings.extend(_dynamic(task, task_id))
    return findings


def _dynamic(task: Path, task_id: str) -> list[Finding]:
    """Blank must score 0, reference 1. At most 2 containers, sequential."""
    try:
        from evallab.candidate_runner import run_candidate  # type: ignore
    except Exception:
        return [Finding(subject=FindingSubject(kind="task", task_id=task_id), stage="env-lint:dynamic",
                        verdict="unknown", category="weak", summary="dynamic controls unavailable (candidate_runner not importable)",
                        reviewer=Reviewer(agent=AGENT, cost_usd=0.0), measurement="not_run")]
    out: list[Finding] = []
    for name in ("blank", "reference"):
        try:
            res = run_candidate(task, None, oracle=name == "reference", blank=name == "blank")  # type: ignore[operator]
            reward = float(res.get("reward", -1)) if isinstance(res, dict) else -1.0
        except Exception as exc:  # noqa: BLE001
            out.append(Finding(subject=FindingSubject(kind="task", task_id=task_id), stage=f"env-lint:dynamic-{name}",
                               verdict="unknown", category="weak", summary=f"dynamic {name} error: {exc}"[:200],
                               reviewer=Reviewer(agent=AGENT, cost_usd=0.0), measurement="ran_without_usable_result"))
            continue
        ok = (reward == 0.0) if name == "blank" else (reward == 1.0)
        out.append(Finding(subject=FindingSubject(kind="task", task_id=task_id), stage=f"env-lint:dynamic-{name}",
                           verdict="pass" if ok else "fail", category="weak",
                           severity=None if ok else "high", fault=None if ok else "eval",
                           summary=f"dynamic {name} reward={reward} (expected {0 if name=='blank' else 1})",
                           reviewer=Reviewer(agent=AGENT, cost_usd=0.0), measurement="measured",
                           root_cause=[f"dynamic_{name}"]))
    return out


def leads(findings: list[Finding]) -> list[dict[str, Any]]:
    return [{"check": f.stage.removeprefix("env-lint:"), "category": f.category, "severity": f.severity,
             "summary": f.summary, "evidence": f.evidence_refs, "task_id": f.subject.task_id,
             "verdict": f.verdict} for f in findings]


def add_env_lint_commands(sub: Any) -> None:
    p = sub.add_parser("env-lint", help="Deterministic model-free task lint ($0 static; --dynamic runs controls)")
    p.add_argument("task_dir", type=Path)
    p.add_argument("--out", type=Path, default=None, help="Findings dir (default: <task>-env-lint/)")
    p.add_argument("--leads", type=Path, default=None, help="Leads JSON for qa env harness (default: <out>/leads.json)")
    p.add_argument("--dynamic", action="store_true", help="Also run blank/reference controls (local Docker, <=2 containers)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd)


def _cmd(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        findings = lint_task(args.task_dir, dynamic=args.dynamic)
    except Exception as exc:  # noqa: BLE001
        print(f"error: evallab qa env-lint: {exc}", file=sys.stderr)
        return 2
    out = args.out or (root / f"{args.task_dir.name}-env-lint")
    out.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(findings):
        dump_finding(f, out / f"{i:03d}-{f.stage.removeprefix('env-lint:')}.json")
    leads_path = args.leads or (out / "leads.json")
    leads_path.write_text(json.dumps({"schema": "qa.env-lint.leads/v1", "task": args.task_dir.name, "leads": leads(findings)}, indent=2) + "\n")
    if args.json:
        print(json.dumps(leads(findings), indent=2))
    else:
        for f in findings:
            print(f"{f.category:18s} {f.stage:32s} {','.join(f.evidence_refs[:2])}  {f.summary}")
        print(f"{len(findings)} leads -> {out} (leads: {leads_path})")
    return 0
