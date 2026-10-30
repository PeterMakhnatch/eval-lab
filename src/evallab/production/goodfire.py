"""Goodfire DEV run-card cache and fast accessor (B2).

Compact derived cache outside the live read-only trace store.
Cards: run_id + task statement + steps split narration/call/output (correct split) + full lengths + capped text per channel (with drop marker).
NO labels, NO receipts, NO passage marks ever enter the cache.
DEV admission uses the shared CorpusRegistry gate exactly.

CLI targets:
  evallab goodfire list --dev
  evallab goodfire card <run>
  evallab goodfire build-cache
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.run_atlas import (
    DEFAULT_DATABASE,
    GOODFIRE_DATASET,
    cloud_trace_ids,
    connect_readonly,
)

CACHE_DIR = Path.home() / ".local/state/evallab-goodfire-cache"
CACHE_PATH = CACHE_DIR / "goodfire-dev.sqlite"
CARD_VERSION = "goodfire-card/v1"


@dataclass(frozen=True)
class GoodfireCard:
    run_id: str
    task_id: str
    task_statement: str | None
    n_steps: int
    char_total: int
    char_narration: int
    char_tool_call: int
    char_tool_output: int
    steps: list[
        dict[str, Any]
    ]  # [{"i": int, "k": "narration|call|output", "l": int, "t": "capped text with ...[truncated N chars]... marker if needed"}, ...]
    # "l" = full pre-cap length (for char_*); "t" added for B2b content cards.

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _open_cache(path: Path = CACHE_PATH, *, write: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if write:
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            """CREATE TABLE IF NOT EXISTS run_cards (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                task_statement TEXT,
                n_steps INTEGER NOT NULL,
                char_total INTEGER NOT NULL,
                char_narration INTEGER NOT NULL,
                char_tool_call INTEGER NOT NULL,
                char_tool_output INTEGER NOT NULL,
                steps_json TEXT NOT NULL,
                card_version TEXT NOT NULL DEFAULT 'goodfire-card/v1',
                built_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS run_cards_task ON run_cards(task_id)")
        return con
    if not path.is_file():
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE run_cards (run_id TEXT)")
        return con
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _cap(text: str, limit: int = 4000) -> str:
    """Cap per-channel text; explicit dropped count in marker. Never silent slice.
    (DQ-4 style fix for truncation visibility.)
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n...[truncated {dropped} chars]..."


def _extract_channels(row: sqlite3.Row | dict) -> list[dict[str, Any]]:
    """Emit separate channel entries for narration / call / output.
    Narration = asst reasoning + text; call = command_or_action; output = tool preview/content.
    Returns list so one physical step can contribute narration+call.
    "l": full length for char_* totals; "t": capped (4000) with drop marker.
    No labels, flags, or passage data ever read or stored.
    """

    if isinstance(row, dict):
        getter = row.get
    else:

        def getter(k: str, d: str = "") -> str:
            try:
                if k in row:
                    return row[k]  # type: ignore[index]
                return d
            except Exception:
                return d

    def get(k: str, d: str = "") -> str:
        v = getter(k, d)  # type: ignore[operator]
        return v if v is not None else d

    role = (get("role") or "").lower()
    step_i = int(get("step_index", "0") or 0)
    content = get("content_text") or ""
    preview = get("observation_preview") or ""
    cmd = get("command_or_action") or ""
    fl = get("flags_json") or "{}"
    try:
        flags = json.loads(fl) if isinstance(fl, str) else (fl or {})
    except Exception:
        flags = {}
    reasoning = (flags.get("reasoning_content") or "") if isinstance(flags, dict) else ""

    chans: list[dict[str, Any]] = []
    if role in ("assistant", "agent"):
        nar_text = (reasoning + "\n" + content).strip()
        if nar_text:
            nar_len = len(nar_text)
            chans.append({"i": step_i, "k": "narration", "l": nar_len, "t": _cap(nar_text)})
        if cmd:
            call_text = cmd if isinstance(cmd, str) else json.dumps(cmd, ensure_ascii=False)
            call_len = len(call_text)
            chans.append({"i": step_i, "k": "call", "l": call_len, "t": _cap(call_text)})
    elif role == "tool":
        out_text = content or preview
        if out_text:
            out_len = len(out_text)
            chans.append({"i": step_i, "k": "output", "l": out_len, "t": _cap(out_text)})
    else:
        txt = content or preview or ""
        if txt:
            txt_len = len(txt)
            chans.append({"i": step_i, "k": "narration", "l": txt_len, "t": _cap(txt)})
    return chans


def _classify_step(row: sqlite3.Row) -> tuple[str, int]:
    """Return (kind, charlen). Delegates to _extract_channels (now emits call).
    Legacy single return; build path updated to use _extract_channels for separate entries.
    """
    chans = _extract_channels(row)
    if not chans:
        return "narration", 0
    return chans[0]["k"], chans[0]["l"]


def _extract_task_statement(task_row: dict | None) -> str | None:
    if not task_row:
        return None
    txt = task_row.get("instruction_text") or ""
    if not txt:
        return None
    flat = " ".join(txt.split())
    return flat[:4000] if len(flat) > 4000 else flat


def build_goodfire_dev_cache(
    *,
    source_db: Path = DEFAULT_DATABASE,
    force: bool = False,
    batch: int = 256,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    """Build or refresh the compact DEV-only Goodfire card cache.

    Uses read-only connection + same admission gate.
    Per channel: "l" full chars for totals; "t" = capped text (4000) with
    explicit "...[truncated N chars]..." marker on cut. Narration/call now
    separate entries. No labels/flags/passage marks. Idempotent upsert.
    """
    start = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con_src = connect_readonly(source_db)
    try:
        registry = CorpusRegistry(repo_root)
        identities = [
            dict(r)
            for r in con_src.execute(
                "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
                " CASE WHEN json_valid(metadata_json)"
                " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
                " CASE WHEN json_valid(metadata_json)"
                " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
                " FROM corpus_tasks ORDER BY task_id"
            )
        ]
        admitted = admitted_cloud_task_ids(identities, registry=registry)
        trace_ids = cloud_trace_ids(con_src, admitted=admitted, dataset_ids=[GOODFIRE_DATASET])

        needed_task_ids: set[str] = set()
        for row in con_src.execute(
            "SELECT DISTINCT task_id FROM corpus_tasks WHERE dataset_id = ? AND lower(split)='dev'",
            (GOODFIRE_DATASET,),
        ):
            if row[0] in admitted:
                needed_task_ids.add(row[0])
        task_stmts: dict[str, str] = {}
        if needed_task_ids:
            ph = ",".join("?" * len(needed_task_ids))
            for row in con_src.execute(
                f"SELECT task_id, instruction_text FROM corpus_tasks WHERE task_id IN ({ph})",
                list(needed_task_ids),
            ):
                task_stmts[row[0]] = _extract_task_statement({"instruction_text": row[1]}) or ""

        con_dst = _open_cache(write=True)
        try:
            if force:
                con_dst.execute("DELETE FROM run_cards")
            inserted = 0
            for start_i in range(0, len(trace_ids), batch):
                page = trace_ids[start_i : start_i + batch]
                if not page:
                    continue
                ph = ",".join("?" * len(page))
                rows = con_src.execute(
                    f"SELECT a.trace_id, a.task_id, s.step_index, s.role, s.content_text, "
                    f"s.observation_preview, s.command_or_action, s.flags_json "
                    f"FROM agent_traces a JOIN trace_steps s USING(trace_id) "
                    f"WHERE a.trace_id IN ({ph}) "
                    f"ORDER BY a.trace_id, s.step_index",
                    page,
                ).fetchall()
                by_run: dict[str, list] = {}
                for r in rows:
                    by_run.setdefault(r["trace_id"], []).append(r)
                for rid in page:
                    steps_raw = by_run.get(rid, [])
                    task_id = steps_raw[0]["task_id"] if steps_raw else ""
                    steps_list: list[dict] = []
                    c_nar = c_call = c_out = 0
                    for sr in steps_raw:
                        for ch in _extract_channels(sr):
                            steps_list.append(ch)
                            k = ch["k"]
                            ln = ch["l"]
                            if k == "narration":
                                c_nar += ln
                            elif k == "call":
                                c_call += ln
                            else:
                                c_out += ln
                    total = c_nar + c_call + c_out
                    stmt = task_stmts.get(task_id)
                    con_dst.execute(
                        """INSERT OR REPLACE INTO run_cards
                        (run_id, task_id, task_statement, n_steps, char_total, char_narration,
                         char_tool_call, char_tool_output, steps_json, card_version, built_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            rid,
                            task_id,
                            stmt,
                            len(steps_raw),
                            total,
                            c_nar,
                            c_call,
                            c_out,
                            json.dumps(steps_list, separators=(",", ":")),
                            CARD_VERSION,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        ),
                    )
                    inserted += 1
            con_dst.commit()
            stats = {
                "admitted_goodfire_dev_tasks": len(needed_task_ids),
                "traces_written": inserted,
                "seconds": round(time.time() - start, 3),
                "cache": str(CACHE_PATH),
            }
            return stats
        finally:
            con_dst.close()
    finally:
        con_src.close()


def list_goodfire_dev_run_ids(*, limit: int | None = None, order: str = "sha256") -> list[str]:
    """Return admitted Goodfire DEV run_ids. order='sha256' (deterministic) or 'db'."""
    con = _open_cache()
    try:
        rows = con.execute("SELECT run_id FROM run_cards ORDER BY run_id").fetchall()
        ids = [r[0] for r in rows]
        if order == "sha256":
            ids.sort(key=lambda rid: hashlib.sha256(rid.encode()).hexdigest())
        if limit:
            ids = ids[:limit]
        return ids
    finally:
        con.close()


def get_goodfire_card(run_id: str) -> GoodfireCard | None:
    con = _open_cache()
    try:
        row = con.execute(
            "SELECT run_id, task_id, task_statement, n_steps, char_total, char_narration, "
            "char_tool_call, char_tool_output, steps_json FROM run_cards WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        steps = json.loads(row[8]) if row[8] else []
        return GoodfireCard(
            run_id=row[0],
            task_id=row[1],
            task_statement=row[2],
            n_steps=row[3],
            char_total=row[4],
            char_narration=row[5],
            char_tool_call=row[6],
            char_tool_output=row[7],
            steps=steps,
        )
    finally:
        con.close()


# CLI helpers (called from cli.py) -- full dispatch signature
def _goodfire_list_command(args: Any, root: Path | None = None, *, harbor: Any = None) -> int:
    ids = list_goodfire_dev_run_ids(
        limit=getattr(args, "limit", None), order=getattr(args, "order", "sha256")
    )
    for rid in ids:
        print(rid)
    return 0


def _goodfire_card_command(args: Any, root: Path | None = None, *, harbor: Any = None) -> int:
    card = get_goodfire_card(args.run_id)
    if card is None:
        print(f"not found (or not DEV goodfire): {args.run_id}")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(card.to_dict(), indent=2))
    else:
        print(f"# {card.run_id}")
        print(f"task={card.task_id} steps={card.n_steps}")
        if card.task_statement:
            print(
                "task_statement:",
                card.task_statement[:200] + ("..." if len(card.task_statement) > 200 else ""),
            )
        print(
            f"chars: total={card.char_total} nar={card.char_narration} call={card.char_tool_call} out={card.char_tool_output}"
        )
        for s in card.steps[: min(30, len(card.steps))]:
            t = s.get("t", "")
            t_head = t[:80].replace("\n", " ") + ("..." if len(t) > 80 else "")
            print(f"  [{s['i']}] {s['k']} l={s['l']} t={t_head}")
        if len(card.steps) > 30:
            print("  ...")
    return 0


def _goodfire_build_command(args: Any, root: Path | None = None, *, harbor: Any = None) -> int:
    src_db = getattr(args, "source_db", None) or DEFAULT_DATABASE
    if not isinstance(src_db, Path):
        src_db = Path(src_db)
    stats = build_goodfire_dev_cache(
        source_db=src_db,
        force=getattr(args, "force", False),
        repo_root=(root or Path(".")),
    )
    print(json.dumps(stats, indent=2))
    return 0


def register_subcommands(subparsers: Any) -> None:
    """Register under an existing parser (e.g. the goodfire top level)."""
    p_list = subparsers.add_parser("list", help="List admitted Goodfire DEV run ids (no labels)")
    p_list.add_argument("--dev", action="store_true", required=True)
    p_list.add_argument("--limit", type=int)
    p_list.add_argument("--order", choices=["sha256", "db"], default="sha256")
    p_list.set_defaults(func=_goodfire_list_command)

    p_card = subparsers.add_parser("card", help="Print compact card for one Goodfire DEV run")
    p_card.add_argument("run_id")
    p_card.add_argument("--json", action="store_true")
    p_card.set_defaults(func=_goodfire_card_command)

    p_build = subparsers.add_parser(
        "build-cache", help="Rebuild the derived Goodfire DEV card cache (read-only source)"
    )
    p_build.add_argument("--force", action="store_true")
    p_build.add_argument("--source-db", type=Path, default=DEFAULT_DATABASE)
    p_build.set_defaults(func=_goodfire_build_command)
