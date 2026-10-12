"""Hollow-program battery: fakes a grader must reject, rewrites it must accept.

Ported from Bohrin's battery 2 (``prabhu-gopal/bohrin`` @ ``f030425``,
Apache-2.0; ``src/bohrin/mutate/operators.py`` and ``relations/builtin.py``),
adapted to Harbor tasks: Bohrin rewrites a standalone reference program, here
the reference solution's output is a repository file, so only the functions
the reference solution wrote or changed are rewritten.

Five **fakes** replace every changed function body at once (signature and
docstring kept). Each does no work by construction, so a correct verifier
must give it 0:

* ``empty-implementation`` — ``pass``
* ``constant-implementation`` — ``return`` a constant of the declared or
  inferred return type (``0``, ``""``, ``[]`` ...)
* ``raise-not-implemented`` — ``raise NotImplementedError``
* ``success-exit`` — ``raise SystemExit(0)``
* ``equality-spoofing`` — return an object equal to everything

Four **known-good controls** keep the reference's meaning, so a correct
verifier must give them full reward: the reference itself (``oracle``, the
capture run), ``comment-free`` (comments dropped, same syntax tree),
``reformatted`` (re-laid-out by ``ast.unparse``, same syntax tree) and
``renamed-locals`` (local variables of the changed functions renamed). A
rejected control means the verifier is stricter than the task's meaning, or
reads source text.

Every run is a solution-override control on the oracle channel, like
:mod:`evallab.verifier_mutation`: free, local Docker, no model.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.execution_contracts import new_ulid
from evallab.registry import compute_task_digests
from evallab.schemas import ContractModel
from evallab.verifier_mutation import (
    MatrixRunner,
    MutationRun,
    _Audit,
    _captured_files,
    _captured_root,
    _changed_lines,
    _default_runner,
    _slug,
    _task_config,
    _unified_patch,
    capture_script,
    declared_targets,
    mutant_script,
)

SCHEMA_BATTERY = "evallab.verifier_battery.report/v1"
#: Bohrin battery version these probes follow.
BATTERY = "bohrin-battery-2"

FAKES = (
    "empty-implementation",
    "constant-implementation",
    "raise-not-implemented",
    "success-exit",
    "equality-spoofing",
)
CONTROLS = ("oracle", "comment-free", "reformatted", "renamed-locals")

_SPOOF = (
    'return type("_EnvcheckAlwaysEqual", (), {"__eq__": lambda self, other: True, '
    '"__ne__": lambda self, other: False, "__hash__": lambda self: 0})()'
)
_FAKE_BODIES = {
    "empty-implementation": "pass",
    "raise-not-implemented": "raise NotImplementedError",
    "success-exit": "raise SystemExit(0)",
    "equality-spoofing": _SPOOF,
}
_CONSTANTS = {
    "int": "0",
    "float": "0.0",
    "complex": "0j",
    "str": '""',
    "bytes": 'b""',
    "bool": "False",
    "list": "[]",
    "List": "[]",
    "Sequence": "[]",
    "Iterable": "[]",
    "Collection": "[]",
    "Iterator": "iter(())",
    "Generator": "iter(())",
    "dict": "{}",
    "Dict": "{}",
    "Mapping": "{}",
    "set": "set()",
    "Set": "set()",
    "frozenset": "frozenset()",
    "tuple": "()",
    "Tuple": "()",
}
_SCOPE_ESCAPES = frozenset({"locals", "vars", "eval", "exec", "globals"})

Function = ast.FunctionDef | ast.AsyncFunctionDef


# --------------------------------------------------------------------------- #
# Rewrites
# --------------------------------------------------------------------------- #


def _offsets(source: bytes) -> list[int]:
    starts = [0]
    for index, byte in enumerate(source):
        if byte == 0x0A:
            starts.append(index + 1)
    return starts


def _body(function: Function) -> list[ast.stmt]:
    body = function.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return list(body)


def changed_functions(oracle: bytes, base: bytes | None) -> list[Function]:
    """Outermost functions whose body holds a line the reference solution wrote."""
    try:
        tree = ast.parse(oracle)
    except (SyntaxError, ValueError):
        return []
    changed = _changed_lines(base, oracle)
    found: list[Function] = []
    for node in ast.walk(tree):
        if not isinstance(node, Function):
            continue
        body = _body(node)
        if not body:
            continue
        start, end = node.lineno, node.end_lineno or node.lineno
        if any(start <= line <= end for line in changed):
            found.append(node)
    outer = [
        node
        for node in found
        if not any(
            other is not node
            and other.lineno <= node.lineno
            and (node.end_lineno or 0) <= (other.end_lineno or 0)
            for other in found
        )
    ]
    return sorted(outer, key=lambda node: node.lineno)


def _constant(function: Function) -> str:
    """A zero value of the function's declared, or else inferred, return type."""
    if function.returns is not None:
        name = ast.unparse(function.returns).strip("'\"").split("[", 1)[0].rsplit(".", 1)[-1]
        if name in _CONSTANTS:
            return _CONSTANTS[name]
    for node in ast.walk(function):
        if isinstance(node, ast.Yield | ast.YieldFrom):
            return "iter(())"
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.List | ast.ListComp):
            return "[]"
        if isinstance(value, ast.Dict | ast.DictComp):
            return "{}"
        if isinstance(value, ast.Set | ast.SetComp):
            return "set()"
        if isinstance(value, ast.Tuple):
            return "()"
        if isinstance(value, ast.JoinedStr):
            return '""'
        if isinstance(value, ast.Compare | ast.BoolOp):
            return "False"
        if isinstance(value, ast.Constant) and value.value is not None:
            return repr(type(value.value)())
    return "None"


def _replace_bodies(oracle: bytes, functions: Sequence[Function], statement: Any) -> bytes:
    """Replace each function's body (docstring kept) with ``statement(function)``."""
    starts = _offsets(oracle)
    edits: list[tuple[int, int, bytes]] = []
    for function in functions:
        body = _body(function)
        first, last = body[0], body[-1]
        begin = starts[first.lineno - 1] + first.col_offset
        end = starts[(last.end_lineno or last.lineno) - 1] + (last.end_col_offset or 0)
        edits.append((begin, end, str(statement(function)).encode("utf-8")))
    mutated = oracle
    for begin, end, text in sorted(edits, reverse=True):
        mutated = mutated[:begin] + text + mutated[end:]
    return mutated


def hollow_variants(oracle: bytes, base: bytes | None) -> list[tuple[str, bytes]]:
    """The five fakes for one file, deduplicated; none when no function changed."""
    functions = changed_functions(oracle, base)
    if not functions:
        return []
    variants: list[tuple[str, bytes]] = []
    seen = {oracle}
    for probe in FAKES:
        if probe == "constant-implementation":
            mutated = _replace_bodies(oracle, functions, lambda f: f"return {_constant(f)}")
        else:
            mutated = _replace_bodies(oracle, functions, lambda _f, body=_FAKE_BODIES[probe]: body)
        if mutated in seen or not _compiles(mutated):
            continue
        seen.add(mutated)
        variants.append((probe, mutated))
    return variants


def _compiles(source: bytes) -> bool:
    try:
        compile(source, "<battery>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def _same_tree(left: bytes, right: bytes) -> bool:
    try:
        return ast.dump(ast.parse(left)) == ast.dump(ast.parse(right))
    except (SyntaxError, ValueError):
        return False


def comment_free(oracle: bytes) -> bytes | None:
    """The file without comments, when it had one and the syntax tree is unchanged."""
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(oracle).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    kept = [token for token in tokens if token.type != tokenize.COMMENT]
    if len(kept) == len(tokens):
        return None
    try:
        rewritten = tokenize.untokenize(kept)
    except ValueError:
        return None
    text = rewritten.decode("utf-8") if isinstance(rewritten, bytes) else rewritten
    lines = [line.rstrip() for line in text.split("\n")]
    result = "\n".join(lines).encode("utf-8")
    return result if result != oracle and _same_tree(oracle, result) else None


def reformatted(oracle: bytes) -> bytes | None:
    """The file re-laid-out by ``ast.unparse`` (same syntax tree)."""
    try:
        result = (ast.unparse(ast.parse(oracle)) + "\n").encode("utf-8")
    except (SyntaxError, ValueError):
        return None
    return result if result != oracle and _same_tree(oracle, result) else None


def _renamable(function: Function) -> set[str]:
    """Local names bound in the function's own scope that can be renamed safely."""
    parameters = {
        arg.arg
        for arg in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
            *([function.args.vararg] if function.args.vararg else []),
            *([function.args.kwarg] if function.args.kwarg else []),
        ]
    }
    blocked: set[str] = set()
    for node in ast.walk(function):
        if node is not function and isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef | ast.Match
        ):
            return set()
        if isinstance(node, ast.Global | ast.Nonlocal):
            return set()
        if isinstance(node, ast.Name) and node.id in _SCOPE_ESCAPES:
            return set()
        if isinstance(node, ast.alias):
            blocked.add((node.asname or node.name).split(".", 1)[0])
        if isinstance(node, ast.ExceptHandler) and node.name:
            blocked.add(node.name)
    bound = {
        node.id
        for statement in _body(function)
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and not _inside_comprehension(statement, node)
    }
    return bound - parameters - blocked


def _inside_comprehension(root: ast.AST, target: ast.Name) -> bool:
    return any(
        isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)
        and any(child is target for child in ast.walk(node))
        for node in ast.walk(root)
    )


def renamed_locals(oracle: bytes, base: bytes | None) -> bytes | None:
    """Local variables of the changed functions renamed (``x`` -> ``x_rn``)."""
    functions = changed_functions(oracle, base)
    try:
        tree = ast.parse(oracle)
    except (SyntaxError, ValueError):
        return None
    taken = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    starts = _offsets(oracle)
    edits: list[tuple[int, int, bytes]] = []
    for function in functions:
        names = {name for name in _renamable(function) if f"{name}_rn" not in taken}
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and node.id in names:
                begin = starts[node.lineno - 1] + node.col_offset
                end = starts[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or 0)
                edits.append((begin, end, f"{node.id}_rn".encode()))
    if not edits:
        return None
    mutated = oracle
    for begin, end, text in sorted(set(edits), reverse=True):
        mutated = mutated[:begin] + text + mutated[end:]
    return mutated if _compiles(mutated) else None


def equivalent_variants(oracle: bytes, base: bytes | None) -> list[tuple[str, bytes]]:
    """The rewriting controls for one file (the oracle itself is the capture run)."""
    variants = [
        ("comment-free", comment_free(oracle)),
        ("reformatted", reformatted(oracle)),
        ("renamed-locals", renamed_locals(oracle, base)),
    ]
    return [(name, content) for name, content in variants if content is not None]


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class BatteryProbe(ContractModel):
    id: str
    probe: str
    kind: Literal["fake", "control"]
    file: str
    patch: str
    mutated_sha256: str
    reward: float | None = None
    outcome: Literal["rejected", "rewarded", "partial", "unscored"] | None = None
    job_dir: str | None = None


class BatteryReport(ContractModel):
    schema_: Literal["evallab.verifier_battery.report/v1"] = Field(
        alias="schema", default=SCHEMA_BATTERY
    )
    battery: str = BATTERY
    package: str
    package_digest: str
    task: str
    targets: list[str]
    run_dir: str
    executed: bool = False
    verdict: Literal[
        "planned", "clean", "fake_rewarded", "control_rejected", "no_probes", "task_broken",
        "unscored",
    ] = "planned"  # fmt: skip
    verdict_reason: str = ""
    #: Files the battery rewrote (Python files with a changed function).
    files: list[str] = []
    probes: list[BatteryProbe] = []
    runs: list[MutationRun] = []
    limits: str = (
        "fakes rewrite only functions the reference solution changed; a fake that keeps "
        "reward proves the hidden tests never need those functions to work. A rejected "
        "control is a hypothesis: the verifier may read source text (linters, "
        "inspect.getsource) or the rewrite may not be equivalent in this repository"
    )


def _probe_id(path: str, probe: str, content: bytes) -> str:
    return hashlib.sha256(f"{path}\0{probe}\0".encode() + content).hexdigest()[:10]


def _classify(report: BatteryReport) -> None:
    fakes = [probe for probe in report.probes if probe.kind == "fake"]
    controls = [probe for probe in report.probes if probe.kind == "control"]
    for probe in report.probes:
        if probe.reward is None:
            probe.outcome = "unscored"
        elif probe.reward >= 1:
            probe.outcome = "rewarded"
        elif probe.reward <= 0:
            probe.outcome = "rejected"
        else:
            probe.outcome = "partial"
    rewarded = [probe for probe in fakes if probe.outcome in {"rewarded", "partial"}]
    rejected = [probe for probe in controls if probe.outcome in {"rejected", "partial"}]
    if rewarded:
        report.verdict = "fake_rewarded"
        report.verdict_reason = "fake(s) earned reward: " + ", ".join(
            f"{probe.probe} on {probe.file} ({probe.reward:g})" for probe in rewarded
        )
    elif rejected:
        report.verdict = "control_rejected"
        report.verdict_reason = "known-good control(s) lost reward: " + ", ".join(
            f"{probe.probe} on {probe.file} ({probe.reward:g})" for probe in rejected
        )
    elif any(probe.outcome == "unscored" for probe in report.probes):
        report.verdict = "unscored"
        report.verdict_reason = "a probe produced no scored trial"
    else:
        report.verdict = "clean"
        report.verdict_reason = (
            f"{len(fakes)} fake(s) rejected, {len(controls)} rewrite control(s) and the oracle "
            "accepted"
        )


def render_battery(report: BatteryReport) -> str:
    lines = [
        f"# Hollow-program battery: {report.task}",
        "",
        f"- package digest: `{report.package_digest}`",
        f"- battery: {report.battery}",
        f"- verdict: **{report.verdict}** — {report.verdict_reason or '—'}",
        "",
        "| probe | kind | file | expect | reward | outcome |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {run.role} | control | {run.file or '—'} | {1 if run.role == 'capture-oracle' else 0} "
        f"| {'—' if run.reward is None else format(run.reward, 'g')} | {run.outcome or run.status} |"
        for run in report.runs
        if run.role.startswith("capture")
    ]
    lines += [
        f"| {probe.probe} | {probe.kind} | `{probe.file}` | {0 if probe.kind == 'fake' else 1} | "
        f"{'—' if probe.reward is None else format(probe.reward, 'g')} | {probe.outcome or '—'} |"
        for probe in report.probes
    ]
    lines += ["", f"Limits: {report.limits}", ""]
    return "\n".join(lines)


def _write(run_dir: Path, report: BatteryReport) -> None:
    (run_dir / "battery-report.json").write_text(
        json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "battery-report.md").write_text(render_battery(report), encoding="utf-8")


def run_battery_audit(
    package: str | Path,
    *,
    repo_root: Path,
    targets: Sequence[str] = (),
    execute: bool = False,
    workers: int = 1,
    timeout_seconds: int = 1_800,
    output_dir: Path | None = None,
    runner: MatrixRunner | None = None,
) -> tuple[BatteryReport, Path]:
    """Capture the reference output, then grade the fakes and rewrite controls of it."""
    root = Path(package).expanduser().resolve()
    config = _task_config(root)
    solve_path = root / "solution" / "solve.sh"
    if not solve_path.is_file():
        raise ValueError(f"{root}: no reference solution (solution/solve.sh)")
    chosen = list(targets) or declared_targets(config)
    if not chosen or any(not target.startswith("/") for target in chosen):
        raise ValueError("the battery needs absolute container target paths")
    repo = Path(repo_root).resolve()
    task_name = str((config.get("task") or {}).get("name") or root.name)
    slug = _slug(task_name.rsplit("/", 1)[-1], limit=28)
    nonce = new_ulid().lower()[-6:]
    run_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else repo / "runs" / ".envcheck" / f"battery-{slug}-{nonce}"
    )
    if not run_dir.is_relative_to(repo):
        raise ValueError(f"run directory must stay inside the repository ({repo}); got {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    audit = _Audit(
        package=root,
        repo_root=repo,
        run_dir=run_dir,
        slug=slug,
        nonce=nonce,
        solve=solve_path.read_text(encoding="utf-8"),
        digests=compute_task_digests(root),
        timeout_seconds=timeout_seconds,
        workers=workers,
        runner=runner or _default_runner,
    )
    report = BatteryReport(
        package=str(root),
        package_digest=audit.digests.package,
        task=task_name,
        targets=chosen,
        run_dir=str(run_dir),
    )
    oracle_run = audit.stage(
        "capture-oracle", "capture-oracle", capture_script(audit.solve, chosen)
    )
    base_run = audit.stage("capture-base", "capture-base", capture_script(None, chosen))
    report.runs = [oracle_run, base_run]
    if not execute:
        _write(run_dir, report)
        return report, run_dir
    report.executed = True
    audit.execute(report.runs, "battery-capture")
    for run in report.runs:
        run.outcome = "unscored" if run.status == "infra" else "control"
    if oracle_run.reward is None or base_run.reward is None:
        report.verdict, report.verdict_reason = "unscored", "a capture control was not scored"
        _write(run_dir, report)
        return report, run_dir
    if oracle_run.reward < 1 or base_run.reward > 0:
        report.verdict = "task_broken"
        report.verdict_reason = (
            f"capture controls scored oracle={oracle_run.reward:g} base={base_run.reward:g}"
        )
        _write(run_dir, report)
        return report, run_dir
    oracle_root = _captured_root(repo, oracle_run)
    base_root = _captured_root(repo, base_run)
    staged: list[tuple[BatteryProbe, bytes, MutationRun]] = []
    for target in chosen:
        base_files = _captured_files(base_root, target)
        for path, content in _captured_files(oracle_root, target).items():
            if not path.endswith(".py"):
                continue
            base = base_files.get(path)
            fakes = hollow_variants(content, base)
            if not fakes:
                continue
            variants: list[tuple[Literal["fake", "control"], str, bytes]] = [
                ("fake", name, mutated) for name, mutated in fakes
            ]
            variants += [
                ("control", name, mutated) for name, mutated in equivalent_variants(content, base)
            ]
            report.files.append(path)
            for kind, name, mutated in variants:
                probe = BatteryProbe(
                    id=_probe_id(path, name, mutated),
                    probe=name,
                    kind=kind,
                    file=path,
                    patch=_unified_patch(path, content, mutated),
                    mutated_sha256=hashlib.sha256(mutated).hexdigest(),
                )
                title = f"battery {name} {path}"
                run = audit.stage(
                    "fake" if kind == "fake" else "equivalent",
                    f"b-{probe.id}",
                    mutant_script(audit.solve, path, mutated, title),
                    file=path,
                    mutant=probe.id,
                )
                folder = run_dir / "inputs" / probe.id
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{probe.id}.patch").write_text(probe.patch, encoding="utf-8")
                staged.append((probe, mutated, run))
    if not staged:
        report.verdict = "no_probes"
        report.verdict_reason = "no captured Python target has a function the reference changed"
        _write(run_dir, report)
        return report, run_dir
    runs = [run for _, _, run in staged]
    report.runs.extend(runs)
    audit.execute(runs, "battery")
    for probe, _, run in staged:
        probe.reward = run.reward
        probe.job_dir = run.job_dir
        report.probes.append(probe)
    _classify(report)
    _write(run_dir, report)
    return report, run_dir


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _battery_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    progress = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
    try:
        with progress:
            report, run_dir = run_battery_audit(
                args.package,
                repo_root=root,
                targets=args.target,
                execute=args.execute,
                workers=args.workers,
                timeout_seconds=args.timeout_seconds,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"hack battery: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True))
    else:
        print(f"Task: {report.task} ({report.battery})")
        print(f"Verdict: {report.verdict} ({report.verdict_reason or 'capture plan written'})")
        for probe in report.probes:
            print(f"  {probe.kind:7} {probe.probe:24} {probe.file}: {probe.reward} {probe.outcome}")
    print(f"run directory: {run_dir}", file=sys.stderr if args.json else sys.stdout)
    return 0


def add_battery_parser(hack_commands: Any) -> None:
    battery = hack_commands.add_parser(
        "battery",
        help=(
            "Hollow-program battery (Bohrin battery 2): five fakes of the reference "
            "solution's changed functions must score 0, four known-good rewrites must "
            "score 1 (local, free)"
        ),
    )
    battery.add_argument("package", type=Path)
    battery.add_argument(
        "--target",
        action="append",
        default=[],
        help="Absolute container path of a file the reference solution changes; repeatable "
        "(default: the task's declared artifacts)",
    )
    battery.add_argument("--workers", type=int, default=1, choices=(1, 2))
    battery.add_argument("--execute", action="store_true", help="Run the local control matrices")
    battery.add_argument("--timeout-seconds", type=int, default=1_800)
    battery.add_argument("--json", action="store_true")
    battery.set_defaults(func=_battery_command)
