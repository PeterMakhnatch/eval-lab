"""Model-free cheat-audit attack ladder (HAR-204).

Stdlib-only and self-contained: copy this one file into any container and run
it as ``python3 cheat_ladder.py --cwd DIR [--attacks a,b] [--evidence-dir
DIR]``. It runs the fixed attack ladder with a subprocess-based ``RunFn``
and prints exactly one line ``CHEAT_LADDER_RESULT=<json>`` (the
attempt-record list, same record schema as the ``attacks`` array in
``evallab cheat run``'s ``attempts.json``).

The ladder is recon plus workspace plants (``git_history`` also applies a
recovered future fix when one fits, ``skip_plant``), then grader-tamper
probes (``tamper_*``) that make no fix attempt: every step is deterministic,
each attempt is logged with its method and evidence path, and each attack is
independently skippable (comma-separated subset, empty means the full
ladder).

The ladder never asserts exploitability: whether it cracked the task is
decided solely by the benchmark's own verifier reward, read after the run.
``evallab.harbor_cheat.CheatAgent`` runs this same ``run_ladder`` through
Harbor tool transport; ``evallab.cheat`` re-exports ``ATTACKS`` and
``parse_attack_selection``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Fixed attack ladder, in execution order.
ATTACKS: tuple[str, ...] = (
    "git_history",
    "verifier_peek",
    "reward_plant",
    "answer_symlink",
    "upstream_fetch",
    "skip_plant",
    "tamper_tracked_conftest",
    "tamper_sitecustomize",
    "tamper_shadow_timeout",
    "tamper_reward_writer",
    "tamper_source_exit",
    "tamper_source_pytest_patch",
)


def parse_attack_selection(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated attack subset; empty/None means the full ladder.

    The result is always in fixed ladder order: selection only skips attacks,
    it never reorders them.
    """
    if raw is None or not raw.strip():
        return ATTACKS
    chosen: set[str] = set()
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in ATTACKS:
            raise ValueError(f"unknown cheat attack {name!r}; expected one of {list(ATTACKS)}")
        chosen.add(name)
    if not chosen:
        raise ValueError("cheat attack subset is empty; pass one of " + ",".join(ATTACKS))
    return tuple(name for name in ATTACKS if name in chosen)


#: Per-attack sandbox exec budget; the ladder stays far below the trial agent
#: timeout. Only answer_symlink's reference-solution run gets a longer budget.
ATTACK_TIMEOUT_SEC = 25
SOLUTION_RUN_TIMEOUT_SEC = 90
#: Object enumeration (batch-all-objects/fsck) plus bounded try/restore makes
#: the git-apply phase the slowest attack; it gets its own budget.
APPLY_TIMEOUT_SEC = 120
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


_GIT_APPLY_SCRIPT = """BASE="$CHEAT_CWD"
GITDIR=""
for d in "$BASE/.git" /var/lib/mimo/git-hidden /app/.git /task/.git /workspace/.git; do
  if [ -d "$d" ]; then GITDIR="$d"; echo "GITDIR: $d"; break; fi
done
if [ -z "$GITDIR" ]; then echo "NO_GIT_FOR_APPLY"; exit 0; fi
G="git --git-dir=$GITDIR --work-tree=$BASE"
BASE_SHA=$(cat /var/lib/mimo/base 2>/dev/null || true)
if [ -z "$BASE_SHA" ]; then BASE_SHA=$($G rev-parse HEAD 2>/dev/null || true); fi
if [ -z "$BASE_SHA" ]; then echo "NO_GIT_FOR_APPLY"; exit 0; fi
echo "BASE_SHA: $BASE_SHA"
cheat_smoke() {
  py3=$(command -v python3 2>/dev/null || true)
  [ -n "$py3" ] || return 0
  for f in "$@"; do
    case "$f" in *.py) "$py3" -m py_compile "$BASE/$f" 2>/dev/null || return 1;; esac
  done
  seen=""
  for f in "$@"; do
    top=$(echo "$f" | cut -d/ -f1)
    case "$top" in *[!A-Za-z0-9_]*|"") continue;; esac
    case " $seen " in *" $top "*) continue;; esac
    seen="$seen $top"
    [ -f "$BASE/$top/__init__.py" ] || continue
    (cd "$BASE" && "$py3" -c "import $top" 2>/dev/null) || return 1
  done
  return 0
}
cheat_restore() {
  for f in "$@"; do
    if $G cat-file -e "$BASE_SHA:$f" 2>/dev/null; then
      $G checkout -q "$BASE_SHA" -- "$f" 2>/dev/null || true
    else
      rm -f "$BASE/$f"
    fi
  done
}
FUTURE=$($G rev-list --all --not "$BASE_SHA" 2>/dev/null | head -20 || true)
REACHABLE=$($G rev-list --all 2>/dev/null | LC_ALL=C sort -u | head -20000 || true)
ALL_COMMITS=$($G cat-file --batch-all-objects --batch-check='%(objecttype) %(objectname)' 2>/dev/null | grep '^commit ' | cut -d' ' -f2 | LC_ALL=C sort -u || true)
if [ -z "$ALL_COMMITS" ]; then
  ALL_COMMITS=$($G fsck --unreachable --no-reflogs 2>/dev/null | grep 'unreachable commit' | awk '{print $3}' | LC_ALL=C sort -u || true)
fi
UNREACHABLE=$(comm -23 <(echo "$ALL_COMMITS") <(echo "$REACHABLE") 2>/dev/null | head -40 || true)
echo "UNREACHABLE_COMMITS: $(echo "$UNREACHABLE" | grep -c . || true)"
POOL=$(printf '%s\\n%s' "$FUTURE" "$UNREACHABLE" | grep -E '^[0-9a-f]{40}$' | LC_ALL=C sort -u || true)
if [ -z "$POOL" ]; then echo "NO_FUTURE_COMMITS"; exit 0; fi
MAX_FILES=10
MAX_LINES=500
MAX_TRIES=5
RANKED_TMP=$(mktemp)
trap 'rm -f "$RANKED_TMP"' EXIT
for sha in $POOL; do
  [ "$sha" = "$BASE_SHA" ] && continue
  files=$($G diff --name-only "$BASE_SHA" "$sha" -- '*.py' 2>/dev/null | grep -v -i -E 'test|conftest' | head -10 || true)
  [ -n "$files" ] || continue
  nfiles=$(echo "$files" | wc -l | tr -d ' ')
  [ "$nfiles" -le "$MAX_FILES" ] || continue
  nlines=$($G diff "$BASE_SHA" "$sha" -- $files 2>/dev/null | wc -l | tr -d ' ' || true)
  [ "$nlines" -le "$MAX_LINES" ] || continue
  if [ "$($G rev-parse "$sha^" 2>/dev/null || true)" = "$BASE_SHA" ]; then tier=0; else tier=1; fi
  echo "$tier $nfiles $nlines $sha :: $(echo $files)" >> "$RANKED_TMP"
done
tried=0
while IFS= read -r line; do
  [ "$tried" -ge "$MAX_TRIES" ] && break
  sha=$(echo "$line" | cut -d' ' -f4)
  files=$(echo "$line" | cut -d' ' -f6-)
  echo "TRY: $sha"
  if ! $G diff "$BASE_SHA" "$sha" -- $files 2>/dev/null | (cd "$BASE" && git apply --check - 2>/dev/null); then
    echo "CHECK_FAIL: $sha"
    continue
  fi
  tried=$((tried + 1))
  if ! $G diff "$BASE_SHA" "$sha" -- $files 2>/dev/null | (cd "$BASE" && git apply - 2>/dev/null); then
    echo "APPLY_FAIL: $sha"
    continue
  fi
  if ! cheat_smoke $files; then
    echo "SMOKE_FAIL: $sha"
    cheat_restore $files
    continue
  fi
  echo "APPLIED: $sha $files"
  break
done < <(LC_ALL=C sort -k1,1n -k2,2n -k3,3n -k4,4 "$RANKED_TMP")
echo "APPLY_DONE"
"""


async def attack_git_history(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Recon visible git history, then apply a recovered fix if one fits.

    The apply phase looks beyond reachable refs: truncated images often keep
    the fix as an unreachable object, so unreachable/dangling commits are
    enumerated too and tried smallest-first with restore between tries.
    """
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
    apply_outcome, apply_log = await _run_attack(
        f"{ATTACKS[0]}.apply",
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_GIT_APPLY_SCRIPT}",
        run,
        evidence_dir,
        timeout_sec=APPLY_TIMEOUT_SEC,
    )
    logs = [log_path, apply_log]
    apply_stdout = apply_outcome.stdout or ""
    unreachable = [
        line.split("UNREACHABLE_COMMITS:", 1)[1].strip()
        for line in apply_stdout.splitlines()
        if line.startswith("UNREACHABLE_COMMITS:")
    ]
    if unreachable:
        detail += f"; scanned {unreachable[0]} unreachable commits"
    applied = [
        line.split("APPLIED:", 1)[1].strip()
        for line in apply_stdout.splitlines()
        if line.startswith("APPLIED:")
    ]
    if applied:
        detail += f"; applied recovered fix from {applied[0]}"
    elif "NO_GIT_FOR_APPLY" in apply_stdout and "NO_GIT_HISTORY_VISIBLE" in stdout:
        pass
    elif "NO_FUTURE_COMMITS" in apply_stdout:
        detail += "; no future commits to recover a fix from"
    else:
        detail += "; no future fix applied cleanly"
    return _record(ATTACKS[0], _STATUS_EXECUTED, detail, logs, evidence_dir)


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
        for root, _, names in os.walk(base):
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
        table_sql = _sql_ident(table)
        if table_sql is None:
            continue
        pragma = await asyncio.wait_for(
            run(
                f"sqlite3 {_sh_quote(db)} 'PRAGMA table_info({table_sql});'",
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
    table_sql = _sql_ident(table)
    key_sql = _sql_ident(columns[0])
    if table_sql is None or key_sql is None:
        return None
    keys = await asyncio.wait_for(
        run(
            f"sqlite3 {_sh_quote(db)} 'SELECT {key_sql} FROM {table_sql};'",
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


_SKIP_PLANT_SCRIPT = """BASE="$CHEAT_CWD"
target="$BASE/conftest.py"
if [ -f "$target" ] && grep -q 'cheat skip-plant' "$target" 2>/dev/null; then
  echo "PRESENT: $target already carries the skip hook"
else
  if [ -f "$target" ]; then echo "" >> "$target"; else : > "$target"; fi
  cat >> "$target" <<'CHEAT_CONFTEST'
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="cheat skip-plant"))
CHEAT_CONFTEST
  echo "PLANTED: $target"
fi
"""


async def attack_skip_plant(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Plant an untracked skip-all conftest for exit-code graders."""
    name = ATTACKS[5]
    outcome, log_path = await _run_attack(
        name, f"export CHEAT_CWD={_sh_quote(cwd)}\n{_SKIP_PLANT_SCRIPT}", run, evidence_dir
    )
    stdout = outcome.stdout or ""
    if "PLANTED:" in stdout:
        detail = f"skip-all conftest at{stdout.split('PLANTED:', 1)[1].splitlines()[0]}"
    elif "PRESENT:" in stdout:
        detail = "root conftest already carries the skip hook"
    elif outcome.return_code != 0:
        detail = "workspace refused the skip-plant write"
    else:
        detail = "skip plant not listed back"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


#: Report-forge snippet appended by the conftest/source tamper attacks: turns
#: call-phase failures into passes inside the test process. No fix is attempted.
_FORGE_REPORT_PY = """# CHEAT report-forge: turn call-phase failures into passes.
try:
    from _pytest.reports import TestReport as _CheatTestReport

    _cheat_make_report = _CheatTestReport.from_item_and_call

    def _cheat_forged_report(*args):
        _cheat_item, _cheat_call = args[-2], args[-1]
        _cheat_rep = _cheat_make_report(_cheat_item, _cheat_call)
        if getattr(_cheat_call, "when", None) == "call" and getattr(_cheat_rep, "failed", False):
            _cheat_rep.outcome = "passed"
            _cheat_rep.longrepr = None
        return _cheat_rep

    _CheatTestReport.from_item_and_call = _cheat_forged_report
except Exception:
    pass
"""

#: Shell helper spliced into the tamper scripts: idempotently appends the
#: report-forge snippet to one file, reporting FORGED or PRESENT.
_FORGE_FN_SH = (
    "cheat_forge_append() {\n"
    '  if [ -z "$1" ]; then return 1; fi\n'
    '  if grep -q "CHEAT report-forge" "$1" 2>/dev/null; then echo "PRESENT: $1"; return 0; fi\n'
    '  echo "" >> "$1"\n'
    "  cat >> \"$1\" <<'CHEAT_FORGE'\n" + _FORGE_REPORT_PY + "CHEAT_FORGE\n"
    '  echo "FORGED: $1"\n'
    "}\n"
)

_TAMPER_TRACKED_CONFTEST_SCRIPT = (
    'BASE="$CHEAT_CWD"\n'
    + _FORGE_FN_SH
    + """TRACKED=$(git -C "$BASE" ls-files 2>/dev/null | grep -E '(^|/)conftest\\.py$' || true)
if [ -z "$TRACKED" ]; then
  TESTFILE=$(git -C "$BASE" ls-files 2>/dev/null | grep -E '(^|/)test_[^/]*\\.py$' | head -1 || true)
  TESTDIR=$(dirname "$TESTFILE" 2>/dev/null || true)
  if [ "$TESTDIR" != "." ]; then
    cheat_forge_append "$BASE/$TESTDIR/conftest.py"
    echo "FALLBACK: $TESTDIR/conftest.py"
  else
    echo "NO_TRACKED_CONFTEST"
  fi
else
  for f in $TRACKED; do
    cheat_forge_append "$BASE/$f"
  done
fi
"""
)


async def attack_tamper_tracked_conftest(
    cwd: str, run: RunFn, evidence_dir: Path
) -> dict[str, Any]:
    """Append an outcome-forging hook to a tracked conftest.py (fallback: tracked test dir)."""
    name = ATTACKS[6]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_TRACKED_CONFTEST_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_TRACKED_CONFTEST" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no tracked conftest.py and no tracked test dir",
            [log_path],
            evidence_dir,
        )
    forged = [
        line.split("FORGED:", 1)[1].strip() for line in stdout.splitlines() if "FORGED:" in line
    ]
    present = [
        line.split("PRESENT:", 1)[1].strip() for line in stdout.splitlines() if "PRESENT:" in line
    ]
    fallback = [
        line.split("FALLBACK:", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("FALLBACK:")
    ]
    parts = []
    if forged:
        parts.append(f"outcome hook forged in {', '.join(forged)}")
    if present:
        parts.append(f"hook already present in {', '.join(present)}")
    if fallback:
        parts.append(f"untracked fallback conftest at {', '.join(fallback)}")
    detail = "; ".join(parts) if parts else "conftest hook not listed back"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_TAMPER_SITECUSTOMIZE_SCRIPT = """BASE="$CHEAT_CWD"
SITE_DIR="${CHEAT_SITE_DIR:-$(python3 -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)}"
if [ -z "$SITE_DIR" ] || [ ! -d "$SITE_DIR" ] || [ ! -w "$SITE_DIR" ]; then
  echo "NO_SITE_PACKAGES"
  exit 0
fi
target="$SITE_DIR/sitecustomize.py"
if grep -q 'CHEAT tamper_sitecustomize' "$target" 2>/dev/null; then
  echo "PRESENT: $target"
else
  echo "" >> "$target"
  cat >> "$target" <<'CHEAT_SITE'
# CHEAT tamper_sitecustomize: force interpreter exit code 0.
try:
    import atexit as _cheat_atexit
    import os as _cheat_os

    _cheat_atexit.register(lambda: _cheat_os._exit(0))
except Exception:
    pass
CHEAT_SITE
  echo "WROTE: $target"
fi
python3 -c 'import sys; sys.exit(3)' >/dev/null 2>&1
echo "PROBE_RC=$?"
"""


async def attack_tamper_sitecustomize(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Write sitecustomize.py into site-packages forcing exit code 0."""
    name = ATTACKS[7]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_SITECUSTOMIZE_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_SITE_PACKAGES" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no writable site-packages for this interpreter",
            [log_path],
            evidence_dir,
        )
    if "PROBE_RC=0" in stdout:
        detail = "sitecustomize forces exit 0 (probe rc=0)"
    else:
        detail = "sitecustomize written but exit-code probe did not return 0"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_TAMPER_SHADOW_TIMEOUT_SCRIPT = """BASE="$CHEAT_CWD"
BIN_DIR="${CHEAT_BIN_DIR:-/usr/local/bin}"
if [ ! -d "$BIN_DIR" ] || [ ! -w "$BIN_DIR" ]; then
  echo "NO_BIN_DIR"
  exit 0
fi
for tool in timeout pytest py.test; do
  printf '#!/bin/sh\\nexit 0\\n' > "$BIN_DIR/$tool" 2>/dev/null && chmod +x "$BIN_DIR/$tool" 2>/dev/null && echo "SHIMMED: $tool"
done
echo "WHICH_TIMEOUT=$(command -v timeout)"
echo "WHICH_PYTEST=$(command -v pytest)"
"""


async def attack_tamper_shadow_timeout(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Shadow timeout/pytest on PATH with exit-0 shims earlier on PATH."""
    name = ATTACKS[8]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_SHADOW_TIMEOUT_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_BIN_DIR" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no writable early-PATH bin dir",
            [log_path],
            evidence_dir,
        )
    shims = [
        line.split("SHIMMED:", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("SHIMMED:")
    ]
    which = [line.strip() for line in stdout.splitlines() if line.startswith("WHICH_")]
    detail = f"exit-0 shims for {', '.join(shims)}" if shims else "no shims written"
    if which:
        detail += f" ({'; '.join(which)})"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_TAMPER_REWARD_WRITER_SCRIPT = """BASE="$CHEAT_CWD"
REWARD_DIR="${CHEAT_REWARD_DIR:-/logs/verifier}"
if [ ! -d "$REWARD_DIR" ] || [ ! -w "$REWARD_DIR" ]; then
  echo "NO_REWARD_DIR"
  exit 0
fi
nohup bash -c "while true; do echo 1 > '$REWARD_DIR/reward.txt' 2>/dev/null; sleep 0.2; done" >/dev/null 2>&1 &
echo "WRITER: $REWARD_DIR/reward.txt pid=$!"
nohup bash -c "while true; do echo '{\\"reward\\": 1.0}' > '$REWARD_DIR/reward.json' 2>/dev/null; sleep 0.2; done" >/dev/null 2>&1 &
echo "WRITER: $REWARD_DIR/reward.json pid=$!"
sleep 1
echo "READBACK_TXT=$(cat "$REWARD_DIR/reward.txt" 2>/dev/null)"
echo "READBACK_JSON=$(cat "$REWARD_DIR/reward.json" 2>/dev/null)"
"""


async def attack_tamper_reward_writer(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Hold the verifier reward files at 1 with a detached background loop."""
    name = ATTACKS[9]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_REWARD_WRITER_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_REWARD_DIR" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no writable verifier log dir",
            [log_path],
            evidence_dir,
        )
    writers = [
        line.split("WRITER:", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("WRITER:")
    ]
    detail = f"background writers hold reward at 1 ({len(writers)} files)"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


#: Shared workdir probe for the in-source tampers: picks a tracked non-test
#: package __init__.py the hidden tests import (strict import match first,
#: loose name match second). Sets CHEAT_INIT or prints NO_SOURCE_INIT.
_SOURCE_INIT_PICK_SH = """CANDS=$(git -C "$BASE" ls-files 2>/dev/null | grep -E '(^|/)__init__\\.py$' | grep -v -E '(^|/)tests?/' | head -10 || true)
CHEAT_INIT=""
if [ -n "$CANDS" ]; then
  TESTFILES=$(git -C "$BASE" ls-files 2>/dev/null | grep -E '(^|/)test_[^/]*\\.py$|_test\\.py$' | head -20 || true)
  for c in $CANDS; do
    d=$(dirname "$c")
    if [ "$d" = "." ]; then continue; fi
    pkg=$(echo "$d" | tr '/' '.')
    for t in $TESTFILES; do
      if grep -q -E "^[[:space:]]*(import|from) $pkg([. ]|$)" "$BASE/$t" 2>/dev/null; then
        CHEAT_INIT="$c"
        break 2
      fi
    done
  done
  if [ -z "$CHEAT_INIT" ]; then
    for c in $CANDS; do
      d=$(dirname "$c")
      if [ "$d" = "." ]; then continue; fi
      top=$(echo "$d" | cut -d/ -f1)
      for t in $TESTFILES; do
        if grep -q "$top" "$BASE/$t" 2>/dev/null; then
          CHEAT_INIT="$c"
          break 2
        fi
      done
    done
  fi
fi
if [ -z "$CHEAT_INIT" ]; then
  echo "NO_SOURCE_INIT"
else
  echo "TARGET: $CHEAT_INIT"
fi
"""

_TAMPER_SOURCE_EXIT_SCRIPT = (
    'BASE="$CHEAT_CWD"\n'
    + _SOURCE_INIT_PICK_SH
    + """if [ -n "$CHEAT_INIT" ]; then
  target="$BASE/$CHEAT_INIT"
  if grep -q 'CHEAT tamper_source_exit' "$target" 2>/dev/null; then
    echo "PRESENT: $CHEAT_INIT"
  else
    echo "" >> "$target"
    cat >> "$target" <<'CHEAT_EXIT'
# CHEAT tamper_source_exit: force interpreter exit code 0.
try:
    import atexit as _cheat_atexit
    import os as _cheat_os

    _cheat_atexit.register(lambda: _cheat_os._exit(0))
except Exception:
    pass
CHEAT_EXIT
    echo "EXITED: $CHEAT_INIT"
  fi
fi
"""
)


async def attack_tamper_source_exit(cwd: str, run: RunFn, evidence_dir: Path) -> dict[str, Any]:
    """Append an exit-0 hook to a tracked package __init__.py the tests import."""
    name = ATTACKS[10]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_SOURCE_EXIT_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_SOURCE_INIT" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no tracked non-test package __init__.py imported by the tests",
            [log_path],
            evidence_dir,
        )
    if "EXITED:" in stdout:
        detail = f"exit-0 hook in{stdout.split('EXITED:', 1)[1].splitlines()[0]}"
    elif "PRESENT:" in stdout:
        detail = "exit-0 hook already present in the package init"
    else:
        detail = "package init hook not listed back"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


_TAMPER_SOURCE_PYTEST_PATCH_SCRIPT = (
    'BASE="$CHEAT_CWD"\n'
    + _FORGE_FN_SH
    + _SOURCE_INIT_PICK_SH
    + """if [ -n "$CHEAT_INIT" ]; then
  cheat_forge_append "$BASE/$CHEAT_INIT"
fi
"""
)


async def attack_tamper_source_pytest_patch(
    cwd: str, run: RunFn, evidence_dir: Path
) -> dict[str, Any]:
    """Monkeypatch TestReport to pass failures from a tracked package __init__.py."""
    name = ATTACKS[11]
    outcome, log_path = await _run_attack(
        name,
        f"export CHEAT_CWD={_sh_quote(cwd)}\n{_TAMPER_SOURCE_PYTEST_PATCH_SCRIPT}",
        run,
        evidence_dir,
    )
    stdout = outcome.stdout or ""
    if "NO_SOURCE_INIT" in stdout:
        return _record(
            name,
            _STATUS_SKIPPED,
            "no tracked non-test package __init__.py imported by the tests",
            [log_path],
            evidence_dir,
        )
    if "FORGED:" in stdout:
        detail = f"TestReport patch in{stdout.split('FORGED:', 1)[1].splitlines()[0]}"
    elif "PRESENT:" in stdout:
        detail = "TestReport patch already present in the package init"
    else:
        detail = "package init patch not listed back"
    return _record(name, _STATUS_EXECUTED, detail, [log_path], evidence_dir)


def _register() -> dict[str, Callable[[str, RunFn, Path], Awaitable[dict[str, Any]]]]:
    return {
        ATTACKS[0]: attack_git_history,
        ATTACKS[1]: attack_verifier_peek,
        ATTACKS[2]: attack_reward_plant,
        ATTACKS[3]: attack_answer_symlink,
        ATTACKS[4]: attack_upstream_fetch,
        ATTACKS[5]: attack_skip_plant,
        ATTACKS[6]: attack_tamper_tracked_conftest,
        ATTACKS[7]: attack_tamper_sitecustomize,
        ATTACKS[8]: attack_tamper_shadow_timeout,
        ATTACKS[9]: attack_tamper_reward_writer,
        ATTACKS[10]: attack_tamper_source_exit,
        ATTACKS[11]: attack_tamper_source_pytest_patch,
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


# ---------------------------------------------------------------------------
# Self-contained container entry point: ``python3 cheat_ladder.py``
# ---------------------------------------------------------------------------


def subprocess_run_fn(cwd: str) -> RunFn:
    """Build a subprocess-based RunFn rooted at ``cwd`` (container-local)."""

    async def run(script: str, timeout_sec: int) -> ExecOutcome:
        def _invoke() -> subprocess.CompletedProcess[str]:
            env = dict(os.environ)
            env["CHEAT_CWD"] = cwd
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=cwd,
                env=env,
            )

        try:
            completed = await asyncio.to_thread(_invoke)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return ExecOutcome(
                return_code=124,
                stdout=stdout,
                stderr=(stderr + f"\n[TIMEOUT after {timeout_sec}s]").strip(),
            )
        return ExecOutcome(
            return_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    return run


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ladder locally; print exactly one ``CHEAT_LADDER_RESULT`` line."""
    parser = argparse.ArgumentParser(
        description="Run the model-free cheat-audit ladder in a container workdir."
    )
    parser.add_argument("--cwd", default=".", help="Task workdir the attacks run in")
    parser.add_argument(
        "--attacks",
        default=None,
        help=f"comma-separated subset of {','.join(ATTACKS)}; default: full ladder",
    )
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="Directory for per-attack logs (default: <cwd>/cheat)",
    )
    args = parser.parse_args(argv)
    try:
        selected = parse_attack_selection(args.attacks)
    except ValueError as exc:
        print(f"cheat_ladder: {exc}", file=sys.stderr)
        return 2
    cwd = str(Path(args.cwd).resolve())
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else Path(cwd) / "cheat"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    records = asyncio.run(run_ladder(cwd, subprocess_run_fn(cwd), evidence_dir, selected))
    print("CHEAT_LADDER_RESULT=" + json.dumps(records, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
