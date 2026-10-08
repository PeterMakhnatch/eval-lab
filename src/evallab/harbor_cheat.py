"""Deterministic model-free Harbor cheat-audit agent (HAR-204).

``CheatAgent`` runs outside the task sandbox and drives the task through
Harbor tool transport (``environment.exec`` only: no uploads, no model calls,
no provider credentials). It executes a small ordered, fixed attack ladder;
every step is deterministic, each attempt is logged with its method and
evidence path, and each attack is independently skippable through
``EVALLAB_CHEAT_ATTACKS`` (comma-separated subset, empty means the full
ladder).

The agent never asserts exploitability: whether the ladder cracked the task
is decided solely by the benchmark's own verifier reward, read after the
trial by ``evallab cheat run`` (see ``evallab/cheat.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.agents.capabilities import AgentCapabilities  # ty: ignore[unresolved-import]
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.cheat import (
    ATTACKS,
    parse_attack_selection,
)
from evallab.execution_contracts import CHEAT_AGENT, CHEAT_ATTACKS_ENV_VAR

CHEAT_AGENT_VERSION = "1.0.0"
ATTEMPTS_SCHEMA = "evallab.cheat.attempts/v1"

#: Per-attack sandbox exec budget; the ladder stays far below the trial agent
#: timeout. Only answer_symlink's reference-solution run gets a longer budget.
ATTACK_TIMEOUT_SEC = 25
SOLUTION_RUN_TIMEOUT_SEC = 90
#: Log truncation bound: evidence stays small and reviewable.
MAX_LOG_CHARS = 6000
#: Guided-apply bounds for the verifier_peek sqlite fix.
MAX_APPLY_ROWS = 1000
MAX_LITERAL_CHARS = 256

_STATUS_EXECUTED = "executed"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ExecOutcome:
    """Minimal sandbox exec result, mirroring Harbor's ExecResult."""

    return_code: int
    stdout: str
    stderr: str


RunFn = Callable[[str, int], Awaitable[ExecOutcome]]
"""Run one sandbox shell script with a timeout; return its outcome."""


def _truncate(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]..."


async def _run_attack(
    name: str,
    script: str,
    run: RunFn,
    evidence_dir: Path,
    *,
    timeout_sec: int = ATTACK_TIMEOUT_SEC,
) -> tuple[ExecOutcome, Path]:
    """Execute one fixed attack script, persisting its raw log as evidence."""
    outcome = await asyncio.wait_for(run(script, timeout_sec), timeout=timeout_sec + 30)
    log_path = evidence_dir / f"{name}.log"
    log_path.write_text(
        f"attack: {name}\n"
        f"return_code: {outcome.return_code}\n"
        f"--- stdout ---\n{_truncate(outcome.stdout or '')}\n"
        f"--- stderr ---\n{_truncate(outcome.stderr or '')}\n",
        encoding="utf-8",
    )
    return outcome, log_path


def _record(
    name: str, status: str, detail: str, evidence: Sequence[Path], evidence_dir: Path
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": [path.relative_to(evidence_dir).as_posix() for path in evidence],
    }


_GIT_HISTORY_SCRIPT = """BASE="$CHEAT_CWD"
seen=""
for d in "$BASE" /app /task /workspace; do
  [ -d "$d/.git" ] || continue
  case " $seen " in *" $d "*) continue;; esac
  seen="$seen $d"
  echo "=== git dir: $d ==="
  git -C "$d" log --oneline -15 2>&1
  echo "--- status ---"
  git -C "$d" status --short 2>&1 | head -20
  echo "--- diff stat vs HEAD ---"
  git -C "$d" diff HEAD --stat 2>&1 | head -20
  echo "--- stash ---"
  git -C "$d" stash list 2>&1 | head -10
done
[ -n "$seen" ] || echo "NO_GIT_HISTORY_VISIBLE"
"""


async def attack_git_history(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Recon: surface git history that could leak a solution (read-only)."""
    script = f"export CHEAT_CWD={_sh_quote(cwd)}\n{_GIT_HISTORY_SCRIPT}"
    outcome, log_path = await _run_attack(ATTACKS[0], script, run, evidence_dir)
    stdout = outcome.stdout or ""
    if "NO_GIT_HISTORY_VISIBLE" in stdout:
        detail = "no git history visible in sandbox"
    else:
        interesting = sorted(
            {
                line.strip()
                for line in stdout.splitlines()
                if re.search(r"solution|answer|gold|expected|exploit|cheat", line, re.IGNORECASE)
            }
        )[:5]
        detail = "git history visible"
        if interesting:
            detail += f"; interesting refs: {'; '.join(interesting)}"
    return _record(ATTACKS[0], _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_VERIFIER_PEEK_SCRIPT = """BASE="$CHEAT_CWD"
for d in "$BASE" /app /task; do
  [ -d "$d/tests" ] || continue
  echo "=== tests dir: $d/tests ==="
  ls -la "$d/tests" 2>&1 | head -30
done
echo "=== EXPECTED-like constants ==="
for d in "$BASE" /app /task; do
  [ -d "$d/tests" ] || continue
  grep -rE --include='*.py' -E '^[A-Z][A-Z0-9_]{2,} *=' "$d/tests" 2>/dev/null | head -40
done
echo "=== sqlite-likes ==="
for d in "$BASE" /app /task; do
  [ -d "$d" ] || continue
  find "$d" -maxdepth 3 \\( -name '*.db' -o -name '*.sqlite*' \\) 2>/dev/null | head -10
done
echo "PEEK_DONE"
"""

_PROBE_MARKER = "CHEAT_PROBE_JSON="


def _probe_script() -> str:
    """Emit one JSON line describing test constants and sqlite files."""
    return r"""python3 - <<'CHEAT_PY'
import ast, glob, json, os

bases = []
for cand in (os.environ.get("CHEAT_CWD", ""), "/app", "/task"):
    if cand and os.path.isdir(os.path.join(cand, "tests")) and cand not in bases:
        bases.append(cand)
files = {}
for base in bases:
    for path in sorted(glob.glob(os.path.join(base, "tests", "*.py"))):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        constants = []
        db_paths = []
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            if isinstance(value, str) and value.endswith((".db", ".sqlite", ".sqlite3")):
                db_paths.append({"name": target.id, "value": value[:256]})
            elif (
                isinstance(value, (list, tuple))
                and value
                and all(isinstance(row, (list, tuple)) for row in value)
            ):
                widths = {len(row) for row in value}
                if len(widths) == 1:
                    constants.append(
                        {
                            "name": target.id,
                            "rows": len(value),
                            "width": next(iter(widths)),
                            "first": repr(value[0])[:300],
                        }
                    )
        dbs = []
        for root, _, names in os.walk(os.path.join(base)):
            for name in names:
                if name.endswith((".db", ".sqlite", ".sqlite3")):
                    dbs.append(os.path.join(root, name))
            if len(dbs) >= 10:
                break
        files[path] = {"constants": constants[:20], "db_paths": db_paths, "db_files": dbs[:10]}
print("CHEAT_PROBE_JSON=" + json.dumps({"bases": bases, "files": files}, separators=(",", ":")))
CHEAT_PY
"""


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _sql_literal(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str) and len(value) <= MAX_LITERAL_CHARS:
        return "'" + value.replace("'", "''") + "'"
    return None


def _sql_ident(value: str) -> str | None:
    return f'"{value}"' if _IDENTIFIER.fullmatch(value) else None


def _guided_apply_script(
    db: str, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> str | None:
    """Build one bounded sqlite3 UPDATE script, or None when unsafe."""
    table_sql = _sql_ident(table)
    col_sql = [_sql_ident(column) for column in columns]
    if table_sql is None or any(column is None for column in col_sql):
        return None
    if not rows or len(rows) > MAX_APPLY_ROWS:
        return None
    statements: list[str] = []
    for row in rows:
        literals = [_sql_literal(value) for value in row]
        if any(literal is None for literal in literals) or len(literals) != len(col_sql):
            return None
        assignments = ", ".join(
            f"{col}={lit}" for col, lit in zip(col_sql[1:], literals[1:], strict=True)
        )
        statements.append(f"UPDATE {table_sql} SET {assignments} WHERE {col_sql[0]}={literals[0]};")
    header = f".mode list\nSELECT 'ROWS_BEFORE:' || (SELECT COUNT(*) FROM {table_sql});\n"
    footer = "\nSELECT 'ROWS_AFTER:' || (SELECT COUNT(*) FROM {table});\nSELECT 'CHANGES:' || changes();\n".replace(
        "{table}", table_sql
    )
    body = "\n".join(statements)
    return f"sqlite3 {_sh_quote(db)} <<'CHEAT_SQL'\n{header}{body}\n{footer}CHEAT_SQL\n"


async def attack_verifier_peek(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Recon plus a test-guided sqlite fix when the verifier leaks expectations."""
    name = ATTACKS[1]
    logs: list[Path] = []
    outcome, log_path = await _run_attack(
        name, f"export CHEAT_CWD={_sh_quote(cwd)}\n{_VERIFIER_PEEK_SCRIPT}", run, evidence_dir
    )
    logs.append(log_path)
    probe = await asyncio.wait_for(
        run(f"export CHEAT_CWD={_sh_quote(cwd)}\n{_probe_script()}", ATTACK_TIMEOUT_SEC),
        timeout=ATTACK_TIMEOUT_SEC + 30,
    )
    probe_path = evidence_dir / f"{name}.probe.log"
    probe_path.write_text(
        f"attack: {name}.probe\nreturn_code: {probe.return_code}\n"
        f"--- stdout ---\n{_truncate(probe.stdout or '')}\n"
        f"--- stderr ---\n{_truncate(probe.stderr or '')}\n",
        encoding="utf-8",
    )
    logs.append(probe_path)
    findings: dict[str, Any] = {}
    for line in (probe.stdout or "").splitlines():
        if line.startswith(_PROBE_MARKER):
            try:
                findings = json.loads(line[len(_PROBE_MARKER) :])
            except ValueError:
                findings = {}
            break
    detail = "verifier tests inspected; no applicable expected-rows shape"
    applied = await _maybe_apply_guided_fix(findings, run, evidence_dir, logs)
    if applied is not None:
        detail = applied
    return _record(name, _STATUS_EXECUTED, detail, logs, evidence_dir)


async def _maybe_apply_guided_fix(
    findings: dict[str, Any], run: RunFn, evidence_dir: Path, logs: list[Path]
) -> str | None:
    """Apply EXPECTED_* rows to a matching sqlite table; None when inapplicable."""
    if not isinstance(findings, dict):
        return None
    for path, info in (findings.get("files") or {}).items():
        if not isinstance(info, dict):
            continue
        constants = info.get("constants") or []
        db_files = info.get("db_files") or []
        if not constants or not db_files:
            continue
        expected = next(
            (
                const
                for const in constants
                if isinstance(const, dict)
                and isinstance(const.get("name"), str)
                and const["name"].startswith("EXPECTED_")
                and const.get("rows", 0) >= 1
            ),
            None,
        )
        if expected is None:
            continue
        rows = await _fetch_constant_rows(path, expected["name"], run)
        if rows is None:
            continue
        for db in db_files:
            result = await _apply_rows_to_db(db, rows, run, evidence_dir, logs)
            if result is not None:
                return result
    return None


async def _fetch_constant_rows(path: str, name: str, run: RunFn) -> list[list[Any]] | None:
    script = (
        "python3 - <<'CHEAT_PY'\n"
        "import ast, json\n"
        f"tree = ast.parse(open({_sh_quote(path)}, encoding='utf-8').read())\n"
        f"for node in tree.body:\n"
        "    if isinstance(node, ast.Assign) and len(node.targets) == 1:\n"
        "        target = node.targets[0]\n"
        f"        if isinstance(target, ast.Name) and target.id == {_sh_quote(name)}:\n"
        "            print(json.dumps(ast.literal_eval(node.value)))\n"
        "            break\n"
        "CHEAT_PY\n"
    )
    try:
        outcome = await asyncio.wait_for(
            run(script, ATTACK_TIMEOUT_SEC), timeout=ATTACK_TIMEOUT_SEC + 30
        )
    except Exception:
        return None
    if outcome.return_code != 0:
        return None
    try:
        rows = json.loads((outcome.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MAX_APPLY_ROWS
        or not all(
            isinstance(row, list) and len(row) == len(rows[0]) and len(row) >= 2 for row in rows
        )
    ):
        return None
    return rows


async def _apply_rows_to_db(
    db: str, rows: list[list[Any]], run: RunFn, evidence_dir: Path, logs: list[Path]
) -> str | None:
    width = len(rows[0])
    tables_outcome = await asyncio.wait_for(
        run(f"sqlite3 {_sh_quote(db)} '.tables'", ATTACK_TIMEOUT_SEC),
        timeout=ATTACK_TIMEOUT_SEC + 30,
    )
    if tables_outcome.return_code != 0:
        return None
    candidates: list[tuple[str, list[str]]] = []
    for table in (tables_outcome.stdout or "").split():
        if _IDENTIFIER.fullmatch(table) is None:
            continue
        pragma = await asyncio.wait_for(
            run(
                f"sqlite3 {_sh_quote(db)} 'PRAGMA table_info({_sh_quote(table)});'",
                ATTACK_TIMEOUT_SEC,
            ),
            timeout=ATTACK_TIMEOUT_SEC + 30,
        )
        if pragma.return_code != 0:
            continue
        columns = [line.split("|")[1] for line in (pragma.stdout or "").splitlines() if "|" in line]
        if len(columns) == width and all(_IDENTIFIER.fullmatch(column) for column in columns):
            candidates.append((table, columns))
    if len(candidates) != 1:
        return None
    table, columns = candidates[0]
    keys = await asyncio.wait_for(
        run(
            f"sqlite3 {_sh_quote(db)} 'SELECT {_sh_quote(columns[0])} FROM {_sh_quote(table)};'",
            ATTACK_TIMEOUT_SEC,
        ),
        timeout=ATTACK_TIMEOUT_SEC + 30,
    )
    if keys.return_code != 0:
        return None
    present = set((keys.stdout or "").splitlines())
    if not all(str(row[0]) in present for row in rows):
        return None
    script = _guided_apply_script(db, table, columns, rows)
    if script is None:
        return None
    outcome = await asyncio.wait_for(
        run(script, ATTACK_TIMEOUT_SEC), timeout=ATTACK_TIMEOUT_SEC + 30
    )
    apply_path = evidence_dir / f"{ATTACKS[1]}.apply.log"
    apply_path.write_text(
        f"attack: {ATTACKS[1]}.apply\ndb: {db}\ntable: {table}\nrows: {len(rows)}\n"
        f"return_code: {outcome.return_code}\n"
        f"--- stdout ---\n{_truncate(outcome.stdout or '')}\n"
        f"--- stderr ---\n{_truncate(outcome.stderr or '')}\n",
        encoding="utf-8",
    )
    logs.append(apply_path)
    if outcome.return_code != 0:
        return f"guided fix attempted on {table} but sqlite refused (see apply log)"
    return f"guided fix applied: {len(rows)} expected rows written to {table} in {db}"


_REWARD_PLANT_SCRIPT = """BASE="$CHEAT_CWD"
planted=""
for f in "$BASE/reward.json" "$BASE/cheat-reward.json"; do
  if printf '{"reward":1.0,"agent":"cheat","note":"planted by cheat audit"}' > "$f" 2>/dev/null; then
    planted="$planted $f"
  else
    echo "REFUSED: $f"
  fi
done
[ -n "$planted" ] && echo "PLANTED:$planted"
ls -la "$BASE/reward.json" "$BASE/cheat-reward.json" 2>&1
"""


async def attack_reward_plant(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Plant reward-claim files in the agent workspace (never verifier paths)."""
    outcome, log_path = await _run_attack(
        ATTACKS[2], f"export CHEAT_CWD={_sh_quote(cwd)}\n{_REWARD_PLANT_SCRIPT}", run, evidence_dir
    )
    stdout = outcome.stdout or ""
    if "PLANTED:" in stdout:
        detail = f"planted reward claims:{stdout.split('PLANTED:', 1)[1].splitlines()[0]}"
    elif outcome.return_code != 0:
        detail = "workspace refused reward-claim writes"
    else:
        detail = "reward claims planted but not listed back"
    return _record(ATTACKS[2], _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_ANSWER_SYMLINK_SCRIPT = """BASE="$CHEAT_CWD"
found=""
for cand in "$BASE/solution/solve.sh" /app/solution/solve.sh /task/solution/solve.sh; do
  if [ -x "$cand" ]; then echo "SOLVE: $cand"; found="$found $cand"; fi
done
[ -n "$found" ] || echo "NO_SOLVE_VISIBLE"
echo "=== answer-likes (maxdepth 2) ==="
for d in "$BASE" /app /task; do
  [ -d "$d" ] || continue
  find "$d" -maxdepth 2 \\( -iname '*answer*' -o -iname '*.gold' -o -iname '*.expected' \\) 2>/dev/null | head -20
done
echo "SYMLINK_PROBE_DONE"
"""


async def attack_answer_symlink(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Reuse a visible reference solution, else record answer-like files."""
    name = ATTACKS[3]
    logs: list[Path] = []
    outcome, log_path = await _run_attack(
        name, f"export CHEAT_CWD={_sh_quote(cwd)}\n{_ANSWER_SYMLINK_SCRIPT}", run, evidence_dir
    )
    logs.append(log_path)
    solves = [
        line.removeprefix("SOLVE: ").strip()
        for line in (outcome.stdout or "").splitlines()
        if line.startswith("SOLVE: ")
    ]
    if not solves:
        return _record(name, _STATUS_EXECUTED, "no reference solution visible", logs, evidence_dir)
    solve_outcome, solve_log = await _run_attack(
        f"{name}.solve",
        f"bash {_sh_quote(solves[0])}",
        run,
        evidence_dir,
        timeout_sec=SOLUTION_RUN_TIMEOUT_SEC,
    )
    logs.append(solve_log)
    detail = (
        f"reference solution executed: {solves[0]} rc={solve_outcome.return_code}"
        if solve_outcome.return_code == 0
        else f"reference solution failed: {solves[0]} rc={solve_outcome.return_code}"
    )
    return _record(name, _STATUS_EXECUTED, detail, logs, evidence_dir)


_UPSTREAM_FETCH_SCRIPT = """for d in "$CHEAT_CWD" /app /task; do
  [ -d "$d/.git" ] || continue
  echo "=== remotes: $d ==="
  git -C "$d" remote -v 2>&1 | head -10
done
echo "=== url-like env names (values redacted) ==="
env | cut -d= -f1 | grep -iE 'url|repo|remote|upstream|origin' | head -20
echo "FETCH_PROBE_DONE"
"""


async def attack_upstream_fetch(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Recon: list configured upstreams; ls-remote the first one (read-only)."""
    name = ATTACKS[4]
    logs: list[Path] = []
    outcome, log_path = await _run_attack(
        name, f"export CHEAT_CWD={_sh_quote(cwd)}\n{_UPSTREAM_FETCH_SCRIPT}", run, evidence_dir
    )
    logs.append(log_path)
    remotes = sorted(
        {
            parts[1]
            for line in (outcome.stdout or "").splitlines()
            if "\t" in line
            for parts in [line.split()]
            if len(parts) >= 2 and parts[0] not in {"===", "FETCH_PROBE_DONE"}
        }
    )[:3]
    if not remotes:
        return _record(name, _STATUS_EXECUTED, "no upstream remote configured", logs, evidence_dir)
    ls_outcome, ls_log = await _run_attack(
        f"{name}.lsremote",
        f"git ls-remote {_sh_quote(remotes[0])} HEAD 2>&1 | head -5",
        run,
        evidence_dir,
    )
    logs.append(ls_log)
    detail = (
        f"upstream reachable: {remotes[0]}"
        if ls_outcome.return_code == 0
        else f"upstream unreachable: {remotes[0]} rc={ls_outcome.return_code}"
    )
    return _record(name, _STATUS_EXECUTED, detail, logs, evidence_dir)


def _register() -> dict[str, Callable[[str, RunFn, Path], Awaitable[dict[str, Any]]]]:
    return {
        ATTACKS[0]: attack_git_history,
        ATTACKS[1]: attack_verifier_peek,
        ATTACKS[2]: attack_reward_plant,
        ATTACKS[3]: attack_answer_symlink,
        ATTACKS[4]: attack_upstream_fetch,
    }


async def run_ladder(
    cwd: str,
    run: RunFn,
    evidence_dir: Path,
    selected: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the fixed ladder in order; failures never abort later attacks."""
    wanted = parse_attack_selection(None if selected is None else ",".join(selected))
    registry = _register()
    records: list[dict[str, Any]] = []
    for name in ATTACKS:
        if name not in wanted:
            records.append(
                _record(name, _STATUS_SKIPPED, "not in --attacks subset", [], evidence_dir)
            )
            continue
        try:
            records.append(await registry[name](cwd, run, evidence_dir))
        except Exception as error:  # noqa: BLE001 - ladder is fail-open per attack
            failure = evidence_dir / f"{name}.error.log"
            failure.write_text(
                f"attack: {name}\nerror: {type(error).__name__}: {error}\n", encoding="utf-8"
            )
            records.append(
                _record(name, _STATUS_FAILED, f"{type(error).__name__}", [failure], evidence_dir)
            )
    return records


def selected_attacks(raw: str | None) -> tuple[str, ...]:
    """Parse the agent-facing attack selection (env var or CLI subset)."""
    return parse_attack_selection(raw)


class CheatAgent(BaseAgent):
    """Deterministic model-free Harbor agent running the fixed cheat ladder."""

    capabilities = AgentCapabilities()

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any):
        if model_name:
            raise ValueError("the cheat agent is model-free and does not accept a model")
        super().__init__(logs_dir=logs_dir, model_name=None, **kwargs)

    @staticmethod
    def name() -> str:
        return CHEAT_AGENT

    def version(self) -> str | None:
        return CHEAT_AGENT_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        """Nothing to install: the agent runs host-side on tool transport."""

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        del instruction
        cwd_result = await environment.exec("pwd", timeout_sec=30)
        if (
            cwd_result.return_code != 0
            or not cwd_result.stdout
            or not cwd_result.stdout.strip().startswith("/")
        ):
            raise RuntimeError("cannot determine cheat task working directory")
        cwd = cwd_result.stdout.strip()
        raw = self._get_env(CHEAT_ATTACKS_ENV_VAR)
        if raw is None:
            raw = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
        try:
            selected = selected_attacks(raw)
        except ValueError as error:
            raise RuntimeError(f"invalid {CHEAT_ATTACKS_ENV_VAR}: {error}") from error

        evidence_dir = self.logs_dir / "cheat"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        async def run_fn(script: str, timeout_sec: int) -> ExecOutcome:
            result = await environment.exec(command=script, cwd=cwd, timeout_sec=timeout_sec)
            return ExecOutcome(
                return_code=result.return_code,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

        records = await run_ladder(cwd, run_fn, evidence_dir, selected)
        attempts = {
            "schema": ATTEMPTS_SCHEMA,
            "agent": CHEAT_AGENT,
            "version": CHEAT_AGENT_VERSION,
            "cwd": cwd,
            "attacks": records,
        }
        (evidence_dir / "attempts.json").write_text(
            json.dumps(attempts, indent=2) + "\n", encoding="utf-8"
        )
        executed = [record["name"] for record in records if record["status"] == _STATUS_EXECUTED]
        context.n_input_tokens = 0
        context.n_output_tokens = 0
        context.cost_usd = 0.0
        context.metadata = {
            "cheat_version": CHEAT_AGENT_VERSION,
            "cheat_executed": executed,
            "cheat_statuses": {record["name"]: record["status"] for record in records},
            "model_free": True,
        }
