"""VerifierCheck durable store.

Append-only DuckDB audit tables, hash-chained JSONL trajectory logs, and
EnvCheck-compatible finding export.

Table layout (all append-only; there are intentionally no UPDATE/DELETE
helpers — a hypothesis status change is a new version row in ``hypotheses``):

- ``campaigns``: one row per audit campaign (benchmark pin under test).
- ``tasks``: tasks enrolled in a campaign, with their frozen requirement map.
- ``trials``: graded submissions (contract args JSON, reward, job dir).
- ``hypotheses``: versioned rows per hypothesis id; the latest row per ``id``
  is the current status. History is embedded as JSON with ``{from,to,note}``
  entries mirroring ``vcheck.transition_hypothesis``.
- ``findings``: promoted defects (defect JSON, confidence).
- ``claims``: individual claims belonging to a finding.
- ``cases``: graded case evidence belonging to a finding.

Column names follow the CORE contracts (``vcheck.Hypothesis`` uses ``id``;
``GradeResult`` carries ``reward``/``status``) so rows round-trip without
renaming. Status changes never mutate: they append a new version row.

Trajectory records share one shape everywhere (see ``vcheck_client``):
``{sequence, kind, data, previous, sha256}`` where ``sha256`` is the hex
digest of the canonical JSON of ``{sequence, kind, data, previous}``.
``append_event``/``verify_chain`` here are the file-backed pair of the
in-memory ``chain_append`` helper owned by ``vcheck_client``; the digest
algorithm MUST stay identical.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

#: Filename of the hash-chained trajectory log inside a run directory.
TRAJECTORY_FILENAME = "trajectory.jsonl"

#: ``previous`` pointer of the first record in a chain.
GENESIS = "genesis"

#: Allowed trajectory record kinds (shared vocabulary).
TRAJECTORY_KINDS = frozenset({"request", "response", "submission", "verdict", "control", "note"})

#: Hypothesis status lifecycle: candidate -> one terminal state.
HYPOTHESIS_STATUSES = (
    "candidate",
    "promoted",
    "attached",
    "merged",
    "rejected",
    "inconclusive",
)

#: Terminal hypothesis states: no outgoing transition is legal.
_TERMINAL_HYPOTHESIS_STATUSES = frozenset(HYPOTHESIS_STATUSES[1:])

#: Trial condition vocabulary (shared).
TRIAL_CONDITIONS = frozenset(
    {"target_baseline", "target_hinted", "control_legit", "auditor_authored"}
)

#: Claim assertion vocabulary (shared).
CLAIM_ASSERTIONS = frozenset({"grader_defect", "observed_outcome", "demonstrated_exploitation"})

_STORE_TABLES = frozenset(
    {"campaigns", "tasks", "trials", "hypotheses", "findings", "claims", "cases"}
)

_CAMPAIGNS_DDL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT,
    benchmark TEXT,
    pin TEXT,
    manifest_json TEXT,
    created_at TEXT
)
"""

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT,
    campaign_id TEXT,
    task TEXT,
    pin TEXT,
    requirement_map_sha TEXT,
    requirement_map_json TEXT,
    created_at TEXT
)
"""

_TRIALS_DDL = """
CREATE TABLE IF NOT EXISTS trials (
    id TEXT,
    campaign_id TEXT,
    task TEXT,
    condition TEXT,
    args_json TEXT,
    reward DOUBLE,
    status TEXT,
    job_dir TEXT,
    created_at TEXT
)
"""

_HYPOTHESES_DDL = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT,
    task TEXT,
    statement TEXT,
    status TEXT,
    history_json TEXT,
    requirement_ids_json TEXT,
    evidence_json TEXT,
    created_at TEXT
)
"""

_FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT,
    slug TEXT,
    title TEXT,
    status TEXT,
    confidence TEXT,
    defect_json TEXT,
    kinds_json TEXT,
    created_at TEXT
)
"""

_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT,
    finding TEXT,
    label TEXT,
    assertion TEXT,
    text TEXT,
    confidence TEXT,
    core BOOLEAN,
    effects_json TEXT,
    created_at TEXT
)
"""

_CASES_DDL = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT,
    finding TEXT,
    input TEXT,
    task TEXT,
    path TEXT,
    sha256 TEXT,
    grader_verdict_json TEXT,
    intended_json TEXT,
    shows TEXT,
    effect TEXT,
    created_at TEXT
)
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    # Identical to vcheck_client._canonical_hash (including default=str).
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _record_sha(sequence: int, kind: str, data: dict[str, Any], previous: str) -> str:
    """Digest algorithm shared with ``vcheck_client.chain_append``; keep identical."""
    return hashlib.sha256(
        _canonical_bytes({"sequence": sequence, "kind": kind, "data": data, "previous": previous})
    ).hexdigest()


def open_store(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (creating) the DuckDB audit store at ``path`` with all tables present."""
    resolved = Path(path)
    if resolved.parent != Path(resolved.parent.anchor or "."):
        resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(resolved))
    for ddl in (
        _CAMPAIGNS_DDL,
        _TASKS_DDL,
        _TRIALS_DDL,
        _HYPOTHESES_DDL,
        _FINDINGS_DDL,
        _CLAIMS_DDL,
        _CASES_DDL,
    ):
        conn.execute(ddl)
    return conn


def count_rows(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    """Return the row count of an audit table (test/ops helper; read-only)."""
    if table not in _STORE_TABLES:
        raise ValueError(f"unknown vcheck store table: {table!r}")
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def record_campaign(
    conn: duckdb.DuckDBPyConnection,
    campaign_id: str,
    benchmark: str,
    pin: str,
    manifest: dict[str, Any] | None = None,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one campaign row."""
    row = {
        "id": campaign_id,
        "benchmark": benchmark,
        "pin": pin,
        "manifest_json": json.dumps(manifest or {}, sort_keys=True),
        "created_at": at or _utcnow(),
    }
    conn.execute("INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)", list(row.values()))
    return row


def record_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    campaign_id: str,
    task: str,
    pin: str,
    requirement_map_sha: str = "",
    requirement_map: dict[str, Any] | None = None,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one enrolled-task row with its frozen requirement map."""
    row = {
        "id": task_id,
        "campaign_id": campaign_id,
        "task": task,
        "pin": pin,
        "requirement_map_sha": requirement_map_sha,
        "requirement_map_json": json.dumps(requirement_map or {}, sort_keys=True),
        "created_at": at or _utcnow(),
    }
    conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def record_trial(
    conn: duckdb.DuckDBPyConnection,
    trial_id: str,
    campaign_id: str,
    task: str,
    args: dict[str, Any],
    reward: float | None,
    job_dir: str,
    condition: str = "target_baseline",
    status: str = "graded",
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one graded-trial row (contract args JSON, reward, job dir)."""
    if condition not in TRIAL_CONDITIONS:
        raise ValueError(f"unknown trial condition: {condition!r}")
    row = {
        "id": trial_id,
        "campaign_id": campaign_id,
        "task": task,
        "condition": condition,
        "args_json": json.dumps(args, sort_keys=True),
        "reward": reward,
        "status": status,
        "job_dir": job_dir,
        "created_at": at or _utcnow(),
    }
    conn.execute("INSERT INTO trials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def raise_hypothesis(
    conn: duckdb.DuckDBPyConnection,
    hypothesis_id: str,
    task: str,
    statement: str,
    requirement_ids: list[str] | tuple[str, ...] = (),
    evidence: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append the initial (``candidate``) version row of a hypothesis."""
    existing = conn.execute(
        "SELECT COUNT(*) FROM hypotheses WHERE id = ?", [hypothesis_id]
    ).fetchone()
    if existing and existing[0]:
        raise ValueError(f"hypothesis already exists: {hypothesis_id!r}")
    stamped = at or _utcnow()
    row = {
        "id": hypothesis_id,
        "task": task,
        "statement": statement,
        "status": "candidate",
        "history_json": json.dumps(
            [{"from": "", "to": "candidate", "note": "raised", "at": stamped}]
        ),
        "requirement_ids_json": json.dumps(list(requirement_ids)),
        "evidence_json": json.dumps(list(evidence)),
        "created_at": stamped,
    }
    conn.execute("INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def transition_hypothesis(
    conn: duckdb.DuckDBPyConnection,
    hypothesis_id: str,
    to_status: str,
    note: str = "",
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append a new version row moving a hypothesis to ``to_status``.

    Only ``candidate -> promoted | attached | merged | rejected |
    inconclusive`` is legal; the row count of ``hypotheses`` grows by one.
    History entries are ``{from, to, note, at}``, mirroring ``vcheck``.
    """
    if to_status not in HYPOTHESIS_STATUSES:
        raise ValueError(f"unknown hypothesis status: {to_status!r}")
    versions = conn.execute(
        "SELECT task, statement, status, history_json, requirement_ids_json,"
        " evidence_json FROM hypotheses WHERE id = ? ORDER BY rowid",
        [hypothesis_id],
    ).fetchall()
    if not versions:
        raise ValueError(f"unknown hypothesis: {hypothesis_id!r}")
    current = versions[-1][2]
    if current in _TERMINAL_HYPOTHESIS_STATUSES:
        raise ValueError(
            f"hypothesis {hypothesis_id!r} is terminal ({current}); "
            f"cannot transition to {to_status!r}"
        )
    if to_status == "candidate":
        raise ValueError("cannot transition back to 'candidate'")
    stamped = at or _utcnow()
    history = json.loads(versions[-1][3])
    history.append({"from": current, "to": to_status, "note": note, "at": stamped})
    row = {
        "id": hypothesis_id,
        "task": versions[-1][0],
        "statement": versions[-1][1],
        "status": to_status,
        "history_json": json.dumps(history),
        "requirement_ids_json": versions[-1][4],
        "evidence_json": versions[-1][5],
        "created_at": stamped,
    }
    conn.execute("INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def hypothesis_history(conn: duckdb.DuckDBPyConnection, hypothesis_id: str) -> list[dict[str, Any]]:
    """Return every version row of a hypothesis, oldest first."""
    rows = conn.execute(
        "SELECT task, statement, status, history_json, requirement_ids_json,"
        " evidence_json, created_at FROM hypotheses"
        " WHERE id = ? ORDER BY rowid",
        [hypothesis_id],
    ).fetchall()
    return [
        {
            "id": hypothesis_id,
            "task": task,
            "statement": statement,
            "status": status,
            "history": json.loads(history_json),
            "requirement_ids": json.loads(requirement_ids_json),
            "evidence": json.loads(evidence_json),
            "created_at": created_at,
        }
        for task, statement, status, history_json, requirement_ids_json, evidence_json, created_at in rows
    ]


def latest_hypotheses(
    conn: duckdb.DuckDBPyConnection, task: str | None = None
) -> list[dict[str, Any]]:
    """Return the latest version row per hypothesis id, optionally by task."""
    rows = conn.execute(
        "SELECT id, task, statement, status, history_json,"
        " requirement_ids_json, evidence_json, created_at"
        " FROM hypotheses ORDER BY rowid"
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for (
        hypothesis_id,
        task_name,
        statement,
        status,
        history_json,
        requirement_ids_json,
        evidence_json,
        created_at,
    ) in rows:
        latest[hypothesis_id] = {
            "id": hypothesis_id,
            "task": task_name,
            "statement": statement,
            "status": status,
            "history": json.loads(history_json),
            "requirement_ids": json.loads(requirement_ids_json),
            "evidence": json.loads(evidence_json),
            "created_at": created_at,
        }
    values = list(latest.values())
    if task is not None:
        values = [row for row in values if row["task"] == task]
    return values


def record_finding(
    conn: duckdb.DuckDBPyConnection,
    finding_id: str,
    defect: dict[str, Any],
    confidence: str,
    slug: str = "",
    title: str = "",
    status: str = "open",
    kinds: list[str] | tuple[str, ...] = (),
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one finding row."""
    row = {
        "id": finding_id,
        "slug": slug,
        "title": title,
        "status": status,
        "confidence": confidence,
        "defect_json": json.dumps(defect, sort_keys=True),
        "kinds_json": json.dumps(list(kinds)),
        "created_at": at or _utcnow(),
    }
    conn.execute("INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def record_claim(
    conn: duckdb.DuckDBPyConnection,
    finding: str,
    assertion: str,
    text: str,
    effects: list[str] | tuple[str, ...] = (),
    label: str = "",
    confidence: str = "",
    core: bool = False,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one claim row belonging to a finding."""
    if assertion not in CLAIM_ASSERTIONS:
        raise ValueError(f"unknown claim assertion: {assertion!r}")
    row = {
        "id": f"{finding}:{label}" if label else finding,
        "finding": finding,
        "label": label,
        "assertion": assertion,
        "text": text,
        "confidence": confidence,
        "core": core,
        "effects_json": json.dumps(list(effects)),
        "created_at": at or _utcnow(),
    }
    conn.execute("INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", list(row.values()))
    return row


def record_case(
    conn: duckdb.DuckDBPyConnection,
    finding: str,
    input: str,
    path: str,
    sha256: str,
    grader_verdict: dict[str, Any],
    intended: dict[str, Any],
    shows: str,
    task: str = "",
    effect: str | None = None,
    case_id: str = "",
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one case-evidence row belonging to a finding."""
    row = {
        "id": case_id or f"{finding}:{input}",
        "finding": finding,
        "input": input,
        "task": task,
        "path": path,
        "sha256": sha256,
        "grader_verdict_json": json.dumps(grader_verdict, sort_keys=True),
        "intended_json": json.dumps(intended, sort_keys=True),
        "shows": shows,
        "effect": effect,
        "created_at": at or _utcnow(),
    }
    conn.execute(
        "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        list(row.values()),
    )
    return row


def _read_trajectory_records(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def append_event(
    run_dir: str | Path,
    kind: str,
    data: dict[str, Any],
    previous: str | None = None,
) -> dict[str, Any]:
    """Append one hash-chained record to ``<run_dir>/trajectory.jsonl``.

    ``previous`` defaults to the sha256 of the last record (or ``genesis``
    for the first), so callers that already track the chain may pass it
    explicitly while fresh writers can omit it.
    """
    if kind not in TRAJECTORY_KINDS:
        raise ValueError(f"unknown trajectory kind: {kind!r}")
    if not isinstance(data, dict):
        raise TypeError(f"trajectory data must be a dict, got {type(data).__name__}")
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    log_path = run_path / TRAJECTORY_FILENAME
    records = _read_trajectory_records(log_path)
    sequence = len(records) + 1
    if previous is None:
        previous = records[-1]["sha256"] if records else GENESIS
    record = {
        "sequence": sequence,
        "kind": kind,
        "data": data,
        "previous": previous,
        "sha256": _record_sha(sequence, kind, data, previous),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def verify_chain(path: str | Path) -> bool:
    """Return True iff the JSONL trajectory at ``path`` is intact.

    ``path`` may be the ``trajectory.jsonl`` file or its run directory.
    An empty log verifies; a missing file does not.
    """
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / TRAJECTORY_FILENAME
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    expected_previous = GENESIS
    sequence = 0
    try:
        for line in lines:
            if not line.strip():
                continue
            sequence += 1
            record = json.loads(line)
            if (
                record["sequence"] != sequence
                or record["previous"] != expected_previous
                or record["sha256"]
                != _record_sha(
                    record["sequence"], record["kind"], record["data"], record["previous"]
                )
            ):
                return False
            expected_previous = record["sha256"]
    except (ValueError, KeyError, TypeError):
        return False
    return True


def _write_case_input(inputs_dir: Path, run_dir: Path, case: dict[str, Any]) -> tuple[str, str]:
    """Write one case's submission bytes; return (relative path, sha256 hex)."""
    name = str(case["input"])
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError(f"invalid case input name: {name!r}")
    filename = str(case.get("filename") or name)
    if "/" in filename or filename in {".", ".."}:
        raise ValueError(f"invalid case filename: {filename!r}")
    content = case.get("content")
    if content is None:
        src = case.get("src_path")
        if src is None:
            raise ValueError(f"case {name!r} needs 'content' bytes or a 'src_path' to copy")
        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = run_dir / str(src)
        content = src_path.read_bytes()
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError(f"case {name!r} content must be bytes or str, got {type(content).__name__}")
    target = inputs_dir / name / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(content))
    digest = hashlib.sha256(bytes(content)).hexdigest()
    return f"inputs/{name}/{filename}", digest


def _normalise_cases(
    cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split raw case dicts into export rows and findings.jsonl case records."""
    rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for case in cases:
        verdict = dict(case.get("grader_verdict") or {})
        intended = dict(case.get("intended_verdict") or case.get("intended") or {})
        rows.append(
            {
                "input": str(case["input"]),
                "task": str(case.get("task", "")),
                "shows": str(case.get("shows", "")),
                "grader_verdict": verdict,
                "intended": intended,
                "effect": case.get("effect"),
                "sweep": str(case.get("sweep", "")),
                "case": str(case.get("case", "")),
                "submission_kind": str(case.get("submission_kind", "")),
                "reproducibility": str(case.get("reproducibility", "deterministic_local")),
            }
        )
        records.append(
            {
                "case": case.get("case", ""),
                "effect": case.get("effect"),
                "expected": {
                    "from": "expected",
                    "kind": "exact",
                    "verdict": {
                        "status": verdict.get("status"),
                        "reward": verdict.get("reward"),
                    },
                },
                "grader_verdict": verdict,
                "input": str(case["input"]),
                "intended_verdict": intended,
                "label": case.get("label", str(case["input"])),
                "path": "",  # filled in once the file is written
                "sha256": case.get("sha256", ""),
                "shows": str(case.get("shows", "")),
                "submission_kind": case.get("submission_kind", ""),
                "sweep": case.get("sweep", ""),
                "task": case.get("task", ""),
            }
        )
    return rows, records


def export_finding(
    run_dir: str | Path,
    finding: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    post: str = "vcheck",
    at: str | None = None,
) -> Path:
    """Write an EnvCheck-compatible finding folder and its findings.jsonl record.

    Creates ``<run_dir>/findings/<slug>/`` holding ``inputs/<name>/<file>``,
    ``expected.json``, ``REPRODUCE.md``, ``README.md`` and ``findings.jsonl``
    (appended, one record per line), and returns the folder path.
    """
    slug = str(finding.get("slug", ""))
    if not slug:
        raise ValueError("finding needs a 'slug'")
    run_path = Path(run_dir)
    export_dir = run_path / "findings" / slug
    inputs_dir = export_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    rows, case_records = _normalise_cases(cases)
    for row, record, case in zip(rows, case_records, cases, strict=True):
        relative, digest = _write_case_input(inputs_dir, run_path, case)
        row["path"] = relative
        row["sha256"] = digest
        record["path"] = relative
        record["sha256"] = digest

    defect = dict(finding.get("defect") or {})
    family = str(defect.get("family", ""))
    klass = str(defect.get("class", ""))
    kinds = list(finding.get("kinds") or defect.get("kinds") or ["verifier-gap"])
    claims = list(finding.get("claims") or [])
    effects = list(finding.get("effects") or [])
    for index, claim in enumerate(claims, start=1):
        claim.setdefault("label", f"C{index}")
    for index, effect in enumerate(effects, start=1):
        effect.setdefault("label", f"E{index}")
    stamped = at or _utcnow()
    model = str(finding.get("model", "glm-5.3"))
    pin = str(finding.get("pin", ""))
    benchmark = str(finding.get("benchmark", ""))
    title = str(finding.get("title", slug))
    confidence = str(finding.get("confidence", ""))
    status = str(finding.get("status", "open"))
    finding_id = str(finding.get("id", finding.get("finding_id", "")))
    grader = str(finding.get("grader", "task_verifier"))
    grader_command = str(
        finding.get("grader_command", "uv run evallab vcheck grade --task <task> <input>")
    )

    expected_inputs = []
    for row in rows:
        verdict = row["grader_verdict"]
        expected_inputs.append(
            {
                "input": row["input"],
                "case": row["case"],
                "shows": row["shows"],
                "task": row["task"],
                "sweep": row["sweep"],
                "path": row["path"],
                "sha256": row["sha256"],
                "reproducibility": row["reproducibility"],
                "expected": {
                    "kind": "exact",
                    "verdict": {
                        "status": verdict.get("status"),
                        "reward": verdict.get("reward"),
                    },
                    "from": "expected",
                },
            }
        )
    (export_dir / "expected.json").write_text(
        json.dumps(
            {
                "format": "envcheck-expected/1",
                "post": post,
                "finding": slug,
                "id": finding_id,
                "inputs": expected_inputs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    claim_lines = (
        "\n".join(
            f"- **{claim.get('label', '')}**"
            f" ({'core; ' if claim.get('core', True) else ''}"
            f"{claim.get('assertion', 'grader_defect')};"
            f" confidence {claim.get('confidence', confidence)}):"
            f" {claim.get('text', '')}"
            for claim in claims
        )
        or "- (no claims recorded)"
    )
    effect_lines = (
        "\n".join(
            f"- **{effect.get('label', '')}**"
            f" ({effect.get('kind', 'reward_without_intent')};"
            f" trigger: {effect.get('trigger', '')}):"
            f" exploited by an agent:"
            f" {'yes' if effect.get('exploited_by_agent') else 'no'}"
            for effect in effects
        )
        or "- (no effects recorded)"
    )
    input_rows = "\n".join(
        f"| `{row['input']}` | {row['shows']} | `{row['task']}` |"
        f" `{row['path']}` | `{row['sha256'][:16]}` |"
        for row in rows
    )
    per_input_commands = "\n".join(
        "```sh\n"
        + grader_command.replace("<task>", row["task"] or "<task>").replace("<input>", row["path"])
        + "\n```"
        for row in rows
    )
    pass_lines = "\n".join(
        f"- `{row['input']}`: status `{row['grader_verdict'].get('status')}`,"
        f" reward `{row['grader_verdict'].get('reward')}`"
        for row in rows
    )
    (export_dir / "REPRODUCE.md").write_text(
        f"# Reproduce: {title}\n\n"
        f"Finding `{slug}`{f' (`{finding_id}`)' if finding_id else ''},"
        f" post `{post}`.\n\n"
        "Run the benchmark's own grader on each input below and compare its verdict"
        " with `expected.json`. Report `match` or `mismatch` for every input.\n\n"
        "## What the claims assert\n\n"
        f"{claim_lines}\n\n"
        "Effects, with whether an agent exploited each.\n\n"
        f"{effect_lines}\n\n"
        "## Versioning\n\n"
        f"- **Pin:** `{pin or 'unpinned (record the checkout commit)'}`\n"
        f"- **Environment:** `{benchmark or 'benchmark under test'}`\n\n"
        "## Setup\n\n"
        "1. Check out the benchmark at the pin above.\n"
        "2. Build the task environment images the grader needs.\n"
        "3. `cd` into this finding's folder; every command below runs from there.\n\n"
        "## Grader invocation\n\n"
        f"- **Grader:** {grader}\n"
        "- **Command:**\n\n```sh\n"
        f"{grader_command}\n```\n\n"
        f"{per_input_commands}\n\n"
        "- **Working directory:** this finding's folder.\n\n"
        "## Inputs\n\n"
        "| Input | Shows | Task | File | sha256 |\n"
        "|---|---|---|---|---|\n"
        f"{input_rows}\n\n"
        "## Pass condition\n\n"
        "Grade each of these inputs once. Its verdict must equal `expected` in"
        " `expected.json`: the same status, and when graded the same reward"
        " as a number:\n"
        f"{pass_lines}\n",
        encoding="utf-8",
    )
    claim_index = "\n".join(
        f"- [{claim.get('label', '')}](#recorded-claims)"
        f" **{claim.get('confidence', confidence)}**: {claim.get('text', '')}"
        f" ({claim.get('assertion', 'grader_defect')})"
        for claim in claims
    )
    reproduce_steps = "\n".join(
        f"{n}. Input `{row['path']}`: observed status"
        f" `{row['grader_verdict'].get('status')}`, reward"
        f" `{row['grader_verdict'].get('reward')}`."
        for n, row in enumerate(rows, start=2)
    )
    (export_dir / "README.md").write_text(
        f"# {title}\n\n"
        f"- **Status:** {status}\n"
        f"- **Confidence:** {confidence}\n"
        f"- **Environment:** `{benchmark or 'benchmark under test'}`"
        f" at `{pin or 'unpinned'}`\n\n"
        "## Claims\n\n"
        f"{claim_index or '- (no claims recorded)'}\n\n"
        "## Description\n\n"
        f"{finding.get('description', '')}\n\n"
        "## How to reproduce\n\n"
        "1. Set up as in [REPRODUCE.md](REPRODUCE.md).\n"
        f"{reproduce_steps}\n\n"
        "## Recorded claims\n\n"
        f"{claim_lines}\n\n"
        "Effects, with whether an agent exploited each.\n\n"
        f"{effect_lines}\n",
        encoding="utf-8",
    )

    record = {
        "format": "envcheck-finding/1",
        "slug": slug,
        "id": finding_id,
        "title": title,
        "status": status,
        "confidence": confidence,
        "post": post,
        "kinds": kinds,
        "defect": {"kinds": kinds, "family": family, "class": klass, **defect},
        "claims": claims,
        "effects": effects,
        "cases": case_records,
        "discovered_by": {
            "method": "harness",
            "model": model,
            "who": "eval-lab",
            "date": stamped,
        },
        "sweeps": finding.get("sweeps", []),
        "evidence": finding.get("evidence", []),
    }
    findings_path = export_dir / "findings.jsonl"
    with findings_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return export_dir
