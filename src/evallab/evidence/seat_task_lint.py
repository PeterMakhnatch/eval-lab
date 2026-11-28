from __future__ import annotations

import json
import os
import re
import tomllib
from collections import Counter
from functools import partial
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._multiagent_io import as_dict
from ._task_lint_compose import (
    LintSeverity,
    find_compose,
    iter_bounded_files,
    lint_compose_file,
    read_bounded_text,
    read_world_helper_text,
    safe_contained_path,
)
from ._task_lint_contracts import (
    has_world_server_py,
    lint_compose_isolation_contracts,
    lint_control_policy_contract,
    lint_seat_token_contract,
    lint_static_security_contract,
    lint_world_instance_contract,
)

TaskEcosystem = Literal["olam_seats", "bespoke_team"]

_REWARD_REF_RE = re.compile(
    r"reward\.(?:json|txt)|/logs/verifier|\bwrite_rewards\s*\(",
    re.I,
)
_GRADE_OUT_RE = re.compile(r"MULTI_AGENT_HARNESS_GRADE_(?:OUT|APP)")
_WORLD_LOG_RE = re.compile(r"/logs/world(?:/[^\"'\s]+\.jsonl|/?$)", re.I)


class TaskLintFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: LintSeverity
    check: str
    path: str | None = None
    message: str


class TaskLintReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-task-lint/v1"
    task_dir: str
    task_name: str
    ecosystem: TaskEcosystem
    declared_seats: list[str] = Field(default_factory=list)
    verifier_mode: str = "shared"
    passed: bool
    error_count: int = 0
    warn_count: int = 0
    info_count: int = 0
    findings: list[TaskLintFinding] = Field(default_factory=list)


class BatchTaskLintReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-task-lint-batch/v1"
    root_dir: str
    n_tasks: int
    n_passed: int
    n_failed: int
    ecosystems: dict[str, int] = Field(default_factory=dict)
    finding_counts: dict[str, int] = Field(default_factory=dict)
    tasks: list[TaskLintReport] = Field(default_factory=list)


def _add(
    out: list[TaskLintFinding],
    code: str,
    sev: LintSeverity,
    check: str,
    msg: str,
    path: str | None = None,
) -> None:
    out.append(TaskLintFinding(code=code, severity=sev, check=check, path=path, message=msg))


def _lint_olam_agents_table(
    toml_data: dict[str, Any], findings: list[TaskLintFinding]
) -> list[str]:
    emit = partial(_add, findings)
    agents_tbl = toml_data.get("agents")
    if not isinstance(agents_tbl, dict) or not agents_tbl:
        msg = "Olam seat task declares no [agents.<seat>] entries"
        emit("missing_agents_table", "error", "seat_contract", msg, "task.toml")
        return []
    seats = sorted(str(k) for k in agents_tbl)
    if "main" in agents_tbl:
        msg = "Service 'main' must never be declared under [agents.main]"
        emit("main_declared_as_seat", "error", "main_not_declared_as_seat", msg, "task.toml")
    if not any(
        isinstance(v, dict) and v.get("under_test", True) is not False
        for k, v in agents_tbl.items()
        if k != "main"
    ):
        msg = "No seat has under_test=true (default)"
        emit("no_under_test_seat", "warn", "seat_contract", msg, "task.toml")
    return seats


def _lint_orphan_seat_instructions(
    root_res: Path,
    inst_dir: Path,
    used_inst: set[Path],
    seats: list[str],
    findings: list[TaskLintFinding],
) -> None:
    emit = partial(_add, findings)
    if not inst_dir.is_symlink() and not inst_dir.exists():
        return
    if safe_contained_path(root_res, inst_dir) is None:
        msg = "instructions/ symlink escapes task directory"
        emit("unsafe_path_escape", "error", "isolation_hazards", msg, "instructions")
        return
    if not inst_dir.is_dir():
        return
    seat_set = set(seats)
    for md in sorted(inst_dir.glob("*.md"))[:256]:
        rel_md = f"instructions/{md.name}"
        md_res = safe_contained_path(root_res, md)
        if md_res is None:
            msg = f"Instruction symlink {rel_md} escapes task directory"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, rel_md)
        elif md_res not in used_inst and md.stem not in seat_set:
            msg = f"Instruction {rel_md} has no matching [agents.{md.stem}]"
            emit("orphan_seat_instruction", "warn", "seat_instructions_present", msg, rel_md)


def _lint_olam_seat_instructions(
    task_dir: Path,
    agents_tbl: Any,
    seats: list[str],
    findings: list[TaskLintFinding],
) -> dict[str, tuple[str, str]]:
    emit = partial(_add, findings)
    root_res = task_dir.resolve()
    inst_dir = task_dir / "instructions"
    used_inst: set[Path] = set()
    seat_texts: dict[str, tuple[str, str]] = {}
    for seat in seats:
        if seat == "main":
            continue
        cfg = agents_tbl.get(seat) if isinstance(agents_tbl, dict) else {}
        custom_inst = cfg.get("instruction") if isinstance(cfg, dict) else None
        raw_inst = (
            custom_inst.strip() if isinstance(custom_inst, str) and custom_inst.strip() else None
        )
        inst_file = (task_dir / raw_inst) if raw_inst else (inst_dir / f"{seat}.md")
        resolved_inst = safe_contained_path(root_res, inst_file)
        if resolved_inst is None:
            rel_err = raw_inst or f"instructions/{seat}.md"
            msg = f"Instruction path for seat '{seat}' escapes task directory ({rel_err})"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, rel_err)
            continue
        used_inst.add(resolved_inst)
        rel_inst = resolved_inst.relative_to(root_res).as_posix()
        txt, escaped = read_bounded_text(root_res, inst_file)
        if escaped:
            emit(
                "unsafe_path_escape", "error", "isolation_hazards", f"{rel_inst} escapes", rel_inst
            )
        elif txt is None or not txt.strip():
            msg = f"Missing or empty instruction file for seat '{seat}' ({rel_inst})"
            emit("missing_seat_instruction", "error", "seat_instructions_present", msg, rel_inst)
        else:
            seat_texts[seat] = (rel_inst, txt)
    _lint_orphan_seat_instructions(root_res, inst_dir, used_inst, seats, findings)
    return seat_texts


def _lint_bespoke_structure(
    task_dir: Path,
    toml_data: dict[str, Any],
    compose_path: Path | None,
    compose_escaped: bool,
    findings: list[TaskLintFinding],
) -> None:
    add = partial(_add, findings)
    inst_txt, inst_esc = read_bounded_text(task_dir, task_dir / "instruction.md")
    if inst_esc:
        msg = "instruction.md symlink escapes task dir"
        add("unsafe_path_escape", "error", "isolation_hazards", msg, "instruction.md")
    elif inst_txt is None or not inst_txt.strip():
        msg = "Task requires non-empty instruction.md"
        add("missing_instruction_md", "error", "seat_instructions_present", msg, "instruction.md")

    env_cfg = as_dict(toml_data.get("environment"))
    has_prebuilt = isinstance(img := env_cfg.get("docker_image"), str) and bool(img.strip())
    df_rel = "environment/Dockerfile"
    df_txt, df_esc = read_bounded_text(task_dir, task_dir / "environment" / "Dockerfile")
    if df_esc:
        add("unsafe_path_escape", "error", "isolation_hazards", f"{df_rel} escapes", df_rel)
    elif df_txt is None:
        if not has_prebuilt:
            msg = "Task requires environment/Dockerfile or [environment].docker_image"
            add("missing_environment_dockerfile", "error", "environment_contract", msg, df_rel)
    elif re.search(r"^\s*ADD\s+https?://", df_txt, re.I | re.M):
        msg = "environment/Dockerfile uses ADD with remote URL; prefer pinned downloads"
        add("dockerfile_unpinned_add_url", "warn", "isolation_hazards", msg, df_rel)

    for sec_name in ("agent", "environment"):
        sec_cfg = toml_data.get(sec_name)
        if (
            isinstance(sec_cfg, dict)
            and str(sec_cfg.get("network_mode") or "").strip().lower() == "public"
        ):
            msg = f"[{sec_name}].network_mode='public'; prefer 'no-network'"
            add("agent_public_network_mode", "warn", "isolation_hazards", msg, "task.toml")

    if compose_path is not None or compose_escaped:
        lint_compose_file(
            task_dir, compose_path, compose_escaped, [], add, require_seat_contract=False
        )


def _lint_separate_verifier_dockerfile(
    task_dir: Path,
    v_mode: str,
    v_env: dict[str, Any],
    v_net_public: bool,
    findings: list[TaskLintFinding],
) -> None:
    add = partial(_add, findings)
    df_p = "tests/Dockerfile"
    df_txt, df_esc = read_bounded_text(task_dir, task_dir / "tests" / "Dockerfile")
    if df_esc:
        add("unsafe_path_escape", "error", "isolation_hazards", f"{df_p} escapes task dir", df_p)
    elif v_mode == "separate":
        has_img = isinstance(v_env.get("docker_image"), str) and bool(v_env["docker_image"].strip())
        if df_txt is None and not has_img:
            msg = f"verifier.environment_mode='separate' requires {df_p}"
            add(
                "separate_verifier_missing_dockerfile",
                "error",
                "separate_verifier_readiness",
                msg,
                df_p,
            )
        elif df_txt is not None:
            if "/tests" not in df_txt:
                msg = f"{df_p} must COPY verifier files into /tests"
                add(
                    "separate_verifier_tests_not_baked",
                    "error",
                    "separate_verifier_readiness",
                    msg,
                    df_p,
                )
            if "/logs/verifier" not in df_txt:
                msg = f"{df_p} should pre-create /logs/verifier"
                add(
                    "separate_verifier_missing_logs_dir",
                    "warn",
                    "separate_verifier_readiness",
                    msg,
                    df_p,
                )
        if v_net_public:
            msg = "Separate verifier has network_mode='public'"
            add(
                "separate_verifier_public_network",
                "warn",
                "separate_verifier_readiness",
                msg,
                "task.toml",
            )
    elif df_txt is not None:
        msg = f"{df_p} present without separate verifier mode"
        add(
            "tests_dockerfile_without_separate_mode",
            "info",
            "separate_verifier_readiness",
            msg,
            df_p,
        )


def _lint_verifier_scripts(
    task_dir: Path, ecosystem: TaskEcosystem, findings: list[TaskLintFinding]
) -> None:
    add = partial(_add, findings)
    tests_dir, test_sh = task_dir / "tests", task_dir / "tests" / "test.sh"
    test_sh_res = safe_contained_path(task_dir, test_sh)
    if test_sh_res is None:
        msg = "tests/test.sh symlink escapes task dir"
        add("unsafe_path_escape", "error", "isolation_hazards", msg, "tests/test.sh")
    elif not test_sh_res.is_file():
        add(
            "missing_verifier_test_sh",
            "error",
            "reward_key_contract",
            "Missing tests/test.sh",
            "tests/test.sh",
        )
    elif not os.access(test_sh_res, os.X_OK):
        msg = "tests/test.sh is not executable (+x)"
        add("verifier_test_sh_not_executable", "warn", "reward_key_contract", msg, "tests/test.sh")

    v_files, escaped_tests = iter_bounded_files(
        task_dir, tests_dir, suffixes=frozenset({".sh", ".py", ""})
    )
    for esc in escaped_tests:
        msg = f"Verifier path escapes task dir ({esc.name})"
        add("unsafe_path_escape", "error", "isolation_hazards", msg, "tests/")
    v_texts = [
        txt
        for p in v_files
        if p.name.lower() != "dockerfile" and (txt := read_bounded_text(task_dir, p)[0]) is not None
    ]
    combined = "\n".join(v_texts)
    helper_txt = read_world_helper_text(task_dir, add)
    if v_texts and not _REWARD_REF_RE.search(combined) and not _REWARD_REF_RE.search(helper_txt):
        msg = "Verifier scripts omit /logs/verifier or reward.json/reward.txt"
        add("verifier_missing_reward_output", "error", "reward_key_contract", msg, "tests/")
    if ecosystem == "bespoke_team" and v_texts and not _GRADE_OUT_RE.search(combined):
        msg = "Verifier omits MULTI_AGENT_HARNESS_GRADE_OUT/APP"
        add("bespoke_grade_at_env_missing", "info", "reward_key_contract", msg, "tests/")


def _lint_verifier_and_artifacts(
    task_dir: Path,
    ecosystem: TaskEcosystem,
    toml_data: dict[str, Any],
    findings: list[TaskLintFinding],
) -> str:
    v_cfg = as_dict(toml_data.get("verifier"))
    raw_v_env = v_cfg.get("environment")
    v_env = as_dict(raw_v_env)
    raw_mode = (
        str(v_cfg.get("environment_mode") or "").strip() or str(v_env.get("mode") or "").strip()
    )
    v_mode = (raw_mode or ("separate" if isinstance(raw_v_env, dict) else "shared")).lower()
    v_net_public = any(
        str(d.get("network_mode") or "").strip().lower() == "public" for d in (v_cfg, v_env)
    )
    add = partial(_add, findings)
    if v_mode not in ("shared", "separate"):
        msg = f"Unsupported verifier environment_mode '{v_mode}'"
        add(
            "invalid_verifier_environment_mode",
            "error",
            "separate_verifier_readiness",
            msg,
            "task.toml",
        )
    elif v_mode == "shared" and isinstance(raw_v_env, dict):
        msg = "[verifier].environment_mode='shared' is incompatible with [verifier.environment]"
        add(
            "verifier_shared_mode_with_environment",
            "error",
            "separate_verifier_readiness",
            msg,
            "task.toml",
        )

    _lint_verifier_scripts(task_dir, ecosystem, findings)
    _lint_separate_verifier_dockerfile(task_dir, v_mode, v_env, v_net_public, findings)

    raw_arts = toml_data.get("artifacts")
    artifacts = [
        src.strip()
        for a in (raw_arts if isinstance(raw_arts, list) else ())
        if isinstance(src := (a.get("source") if isinstance(a, dict) else a), str) and src.strip()
    ]
    if ecosystem == "olam_seats" and not any(_WORLD_LOG_RE.search(a) for a in artifacts):
        sev: LintSeverity = "error" if has_world_server_py(task_dir) else "warn"
        msg = "task.toml artifacts omits /logs/world/*.jsonl"
        add("missing_world_log_artifact", sev, "world_log_artifact_paths", msg, "task.toml")
    return v_mode


def _dedup_and_sort(findings: list[TaskLintFinding]) -> list[TaskLintFinding]:
    seen: set[tuple[str, str, str, str | None, str]] = set()
    seen_escapes: set[str | None] = set()
    unique: list[TaskLintFinding] = []
    for f in findings:
        if f.code == "unsafe_path_escape":
            if f.path in seen_escapes:
                continue
            seen_escapes.add(f.path)
        key = (f.code, f.severity, f.check, f.path, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    unique.sort(
        key=lambda f: (
            {"error": 0, "warn": 1, "info": 2}[f.severity],
            f.code,
            f.path or "",
            f.message,
        )
    )
    return unique


def _early_fail(root: Path, code: str, msg: str) -> TaskLintReport:
    return TaskLintReport(
        task_dir=root.as_posix(),
        task_name=root.name,
        ecosystem="bespoke_team",
        passed=False,
        error_count=1,
        findings=[
            TaskLintFinding(
                code=code, severity="error", check="task_toml", path="task.toml", message=msg
            )
        ],
    )


def lint_task_dir(task_dir: Path) -> TaskLintReport:
    assert isinstance(task_dir, Path)
    root = task_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"task directory does not exist: {task_dir}")
    toml_txt, toml_esc = read_bounded_text(root, root / "task.toml")
    if toml_esc:
        return _early_fail(root, "unsafe_path_escape", "task.toml symlink escapes task directory")
    if toml_txt is None:
        return _early_fail(root, "missing_task_toml", "Missing task.toml")
    try:
        toml_data = tomllib.loads(toml_txt)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError) as exc:
        return _early_fail(root, "invalid_task_toml", f"Malformed TOML: {exc}")

    raw_findings: list[TaskLintFinding] = []
    emit = partial(_add, raw_findings)
    compose_path, compose_escaped = find_compose(root)
    is_olam = bool(isinstance(toml_data.get("agents"), dict) or (root / "instructions").is_dir())
    ecosystem: TaskEcosystem = "olam_seats" if is_olam else "bespoke_team"
    task_name = str(as_dict(toml_data.get("task")).get("name") or root.name)

    seats: list[str] = []
    seat_inst_texts: dict[str, tuple[str, str]] = {}
    if ecosystem == "olam_seats":
        seats = _lint_olam_agents_table(toml_data, raw_findings)
        seat_inst_texts = _lint_olam_seat_instructions(
            root, toml_data.get("agents"), seats, raw_findings
        )
        compose, rel_compose, net_aliases = lint_compose_file(
            root, compose_path, compose_escaped, seats, emit, require_seat_contract=True
        )
        lint_seat_token_contract(root, compose, rel_compose, seats, emit)
        lint_compose_isolation_contracts(
            root, toml_data, compose, rel_compose, seats, net_aliases, emit
        )
    else:
        _lint_bespoke_structure(root, toml_data, compose_path, compose_escaped, raw_findings)

    instance = lint_world_instance_contract(root, toml_data, seats, seat_inst_texts, emit)
    lint_control_policy_contract(root, toml_data, emit)
    lint_static_security_contract(root, toml_data, seats, seat_inst_texts, instance, emit)
    v_mode = _lint_verifier_and_artifacts(root, ecosystem, toml_data, raw_findings)

    findings = _dedup_and_sort(raw_findings)
    err_n = sum(1 for f in findings if f.severity == "error")
    warn_n = sum(1 for f in findings if f.severity == "warn")
    info_n = sum(1 for f in findings if f.severity == "info")
    assert err_n + warn_n + info_n == len(findings)
    return TaskLintReport(
        task_dir=root.as_posix(),
        task_name=task_name,
        ecosystem=ecosystem,
        declared_seats=seats,
        verifier_mode=v_mode,
        passed=err_n == 0,
        error_count=err_n,
        warn_count=warn_n,
        info_count=info_n,
        findings=findings,
    )


def lint_task_path(path: Path) -> TaskLintReport | BatchTaskLintReport:
    assert isinstance(path, Path)
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"path is not a directory: {path}")
    if (root / "task.toml").is_file() or (root / "task.toml").is_symlink():
        return lint_task_dir(root)
    toml_files, escaped_tomls = iter_bounded_files(
        root, root, filename="task.toml", prune_on_match=True, max_files=2_000
    )
    task_dirs = sorted({p.parent for p in toml_files})
    task_dir_set = set(task_dirs)
    reports = [lint_task_dir(td) for td in task_dirs]
    for esc in escaped_tomls:
        if any(anc in task_dir_set for anc in esc.parents):
            continue
        esc_dir = esc.parent if esc.name == "task.toml" else esc
        reports.append(
            _early_fail(
                esc_dir, "unsafe_path_escape", f"Escaping symlink under task root ({esc.name})"
            )
        )
    if not reports:
        raise ValueError(f"no task.toml directories found under {path}")
    n_passed = sum(1 for r in reports if r.passed)
    n_failed = sum(1 for r in reports if not r.passed)
    assert n_passed + n_failed == len(reports)
    return BatchTaskLintReport(
        root_dir=root.as_posix(),
        n_tasks=len(reports),
        n_passed=n_passed,
        n_failed=n_failed,
        ecosystems=dict(sorted(Counter(r.ecosystem for r in reports).items())),
        finding_counts=dict(sorted(Counter(f.code for r in reports for f in r.findings).items())),
        tasks=reports,
    )


def format_task_lint_json(report: TaskLintReport | BatchTaskLintReport) -> str:
    return json.dumps(report.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2)


def render_task_lint_text(report: TaskLintReport | BatchTaskLintReport) -> str:
    if isinstance(report, TaskLintReport):
        status = "PASS" if report.passed else "FAIL"
        lines = [
            f"[{status}] {report.task_name} ({report.ecosystem}, verifier={report.verifier_mode}, "
            f"errors={report.error_count}, warns={report.warn_count}, info={report.info_count})"
        ]
        for f in report.findings:
            lines.append(f"  {f.severity.upper():5s} {f.code} ({f.path or '-'}): {f.message}")
        return "\n".join(lines)

    lines = [
        f"Linted {report.n_tasks} tasks ({report.ecosystems}): "
        f"passed={report.n_passed}, failed={report.n_failed}"
    ]
    for tr in report.tasks:
        status = "PASS" if tr.passed else "FAIL"
        lines.append(
            f"  [{status}] {tr.task_name} ({tr.ecosystem}): "
            f"errors={tr.error_count}, warns={tr.warn_count}, info={tr.info_count}"
        )
    return "\n".join(lines)
