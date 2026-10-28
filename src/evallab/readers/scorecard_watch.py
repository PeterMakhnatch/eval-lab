"""Refresh the whole-run trace-tool scorecard as immutable, identity-bound snapshots.

One tick recomputes the existing whole-run card,
:func:`evallab.readers.tool_scorecard.scorecard`, and publishes three local
artifacts:

* ``snapshots/<projection>/<identity>.json`` -- an immutable envelope
  (``evallab.trace_tool_scorecard_snapshot/v1``) holding that card beside the
  digest of every input it was derived from. An existing snapshot is never
  rewritten.
* ``current.json`` -- the pointer (``evallab.trace_tool_scorecard_pointer/v1``)
  naming the snapshot of the latest complete valid calculation. It is replaced
  atomically (temp file + ``os.replace``) only after that snapshot is durable.
* ``--card-out`` -- a card copy (``evallab.trace_tool_scorecard/v1``),
  byte-identical to ``python -m evallab.readers.tool_scorecard --out`` output for
  the same card and rewritten at all only when its bytes change. It defaults
  inside the watch state, next to ``current.json``, ``watch.lock``, the tick
  receipt ``watch-last.json`` and ``snapshots/<projection>/<identity>.json``.
  Republishing to the live ``<data_root>/trace-tools/tt1-scorecard.json`` is a
  separate explicit decision: that file currently mixes five readers from the
  unversioned legacy ledger the shared projection rule refuses.

Reuse is content-addressed, never timestamp-based. The identity binds the result
ledger bytes, the actor projection, the three label joins (pre-admission Goodfire
DEV, admitted Goodfire DEV, CheatBench DEV cross-test), the admitted trace ids,
the frozen cascade decisions, and the digests of the three modules that turn them
into a card. When the current inputs already name a stored snapshot, the tick
reports ``unchanged`` and recomputes nothing. New appended result rows -- or any
label, admission, cascade or scorer change -- produce a new identity, a new
snapshot and a moved pointer, while every earlier snapshot stays readable.

A tick never replaces the last valid measurement. ``incomplete`` (missing ledger,
CloudDB, answer-key or cascade input, or a fixed comparison that lost its label
set), ``refused`` (a ledger row from another actor projection or without one, a
malformed row, or a stored snapshot that contradicts its own identity) and
``error`` all leave ``current.json``, ``--card-out`` and every snapshot exactly as
they were; only the receipt ``watch-last.json`` records the failed tick.

Missing, failed, refused and actor-unavailable results stay abstentions: this
module re-derives no metric and rewrites no flag. It publishes the card the shared
scorer produced, whose per-row ``abstain_*`` and ``paired_*`` counters keep
partial coverage and paired comparisons apart. No threshold is tuned, no label is
uploaded, and no scanner, model, remote service or timer is invoked.

The live CloudDB is opened read-only and every query keeps its own short read
lock, so a long calculation can never block the running writers; the inputs are
re-read after scoring instead, and a tick whose inputs moved is refused rather
than bound to a card it cannot reproduce.

``--results-path`` is one results ledger. An unversioned Scout ledger is refused by
the shared rule and a ledger holding rows of another actor projection is refused
here, so a refresh never mixes measurements across projections; readers appending
to a different file are simply not part of this card until they write into the
ledger it is pointed at. Snapshots hold aggregate counts only -- no run id and no
per-run label is stored.

Scheduler use::

    python -m evallab.readers.scorecard_watch --db-path ... --repo-root ... \\
        --results-path ... --cm9-dir ... --stage1-path ... \\
        --state-dir ... --card-out ...
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.readers import ACTOR_PROJECTION_VERSION, tool_scorecard

SNAPSHOT_SCHEMA = "evallab.trace_tool_scorecard_snapshot/v1"
POINTER_SCHEMA = "evallab.trace_tool_scorecard_pointer/v1"
SNAPSHOTS = "snapshots"
CURRENT = "current.json"
LOCK = "watch.lock"
LAST = "watch-last.json"

DEFAULT_TRACE_TOOLS = tool_scorecard.DEFAULT_STAGE1.parent
DEFAULT_STATE = DEFAULT_TRACE_TOOLS / "scorecard-watch"
DEFAULT_CARD = DEFAULT_STATE / "tt1-scorecard.json"


class RefreshIncomplete(ValueError):
    """A required input is absent or empty, so no card may be published this tick."""


class RefreshRefused(ValueError):
    """Inputs are incompatible or corrupt, or a stored snapshot contradicts itself."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(raw: bytes | str) -> str:
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()


def _canonical_digest(value: Any) -> str:
    """Digest of a value's canonical JSON, so equal content always names one snapshot."""
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _file_sha256(path: Path) -> str | None:
    try:
        with Path(path).open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _write_if_changed(path: Path, text: str) -> bool:
    """Replace ``path`` atomically only when its bytes change; True when written."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    _write_atomic(path, text)
    return True


def card_text(card: Mapping[str, Any]) -> str:
    """The card's published bytes: identical to ``tool_scorecard.main --out``."""
    return json.dumps(card, indent=2, allow_nan=False) + "\n"


def _scorer_digests() -> dict[str, str | None]:
    """Digests of the modules that turn these inputs into a card.

    A scoring or admission change must never reuse a snapshot it did not produce.
    """
    from evallab.production import cloud_trace_db, corpus_queue

    modules = (tool_scorecard, cloud_trace_db, corpus_queue)
    return {
        module.__name__: _file_sha256(Path(module.__file__)) if module.__file__ else None
        for module in modules
    }


def _measure(
    *, db_path: Path, repo_root: Path, results_path: Path, cm9_dir: Path, stage1_path: Path,
) -> dict[str, Any]:
    """The exact inputs of one card as digests; the ledger is hashed, not parsed.

    Every value is cheap to recompute, so an unchanged tick never scores again. The
    label joins, admission set and cascade come from the shared scorer's own
    functions -- a second, scorecard-only split rule would be weaker than the
    production gate.
    """
    # The answer-key join first: a missing split authority is absent input, not a
    # registry disagreement about some unrelated row.
    try:
        cheatbench = tool_scorecard.cheatbench_dev_labels(repo_root)
    except FileNotFoundError as error:
        raise RefreshIncomplete("answer_keys_missing") from error
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        goodfire_before = tool_scorecard.goodfire_dev_labels(con)
        admitted = tool_scorecard._admitted_trace_ids(con, repo_root)
        goodfire = {
            run_id: label for run_id, label in goodfire_before.items() if run_id in admitted
        }
    return {
        "projection_version": ACTOR_PROJECTION_VERSION,
        "results_sha256": _file_sha256(results_path),
        "label_sets": {
            "goodfire_dev_before_admission": _canonical_digest(goodfire_before),
            "goodfire_dev": _canonical_digest(goodfire),
            "cheatbench_dev": _canonical_digest(cheatbench),
        },
        "admitted_trace_ids": _canonical_digest(sorted(admitted)),
        "cascade": _canonical_digest(tool_scorecard.load_cascade(cm9_dir, stage1_path)),
        "scorer": _scorer_digests(),
    }


def _ledger_summary(path: Path) -> dict[str, Any]:
    """What the ledger declares, for the snapshot's provenance block.

    ``tool_scorecard`` stays the only authority that scores or refuses rows. This
    pass additionally refuses a ledger holding rows of another actor projection:
    the shared loader scores every row it holds, and a row measured under a
    different projection is not a current measurement.
    """
    tools: Counter[str] = Counter()
    versions: dict[str, set[str]] = defaultdict(set)
    projections: set[str] = set()
    runs: set[str] = set()
    rows = 0
    for row in tool_scorecard._jsonl(path):
        rows += 1
        tool = row.get("tool")
        detector = row.get("detector")
        run_id = row.get("run_id")
        if isinstance(tool, str):
            tools[tool] += 1
        if isinstance(run_id, str):
            runs.add(run_id)
        if isinstance(tool, str) and isinstance(detector, str):
            detector_version = row.get("detector_version")
            if isinstance(detector_version, str):
                versions[f"{tool}/{detector}"].add(detector_version)
        if "projection_version" in row:
            projections.add(str(row["projection_version"]))
            if row["projection_version"] != ACTOR_PROJECTION_VERSION:
                raise RefreshRefused("results_projection_mismatch")
    return {
        "rows": rows,
        "runs": len(runs),
        "tools": dict(sorted(tools.items())),
        "declared_projections": sorted(projections),
        "detector_versions": {
            key: sorted(value) for key, value in sorted(versions.items())
        },
    }


def _require_complete(card: Mapping[str, Any]) -> None:
    """Refuse a card that lost a fixed comparison instead of publishing less coverage.

    The four comparisons and their label sets are fixed by the shared scorer; a card
    missing one is a partial measurement and must not replace the last valid one.
    Per-row abstention and paired counters stay the scorer's own.
    """
    if not any(comparison["rows"] for comparison in card["label_sets"].values()):
        raise RefreshIncomplete("results_empty")
    counts = card["label_counts"]
    if not counts["goodfire_dev_before_admission"]["total"]:
        raise RefreshIncomplete("goodfire_labels_missing")
    if not (counts["goodfire_dev"]["positive"] or counts["goodfire_dev"]["negative"]):
        raise RefreshIncomplete("admitted_labels_missing")
    if not counts["goodfire_cm9_sample"]["total"]:
        raise RefreshIncomplete("cm9_sample_missing")
    if not (counts["cheatbench_dev"]["positive"] or counts["cheatbench_dev"]["negative"]):
        raise RefreshIncomplete("cheatbench_labels_missing")


def _verify_snapshot(path: Path, *, identity_digest: str) -> dict[str, Any]:
    """Read a stored snapshot and prove it is the one that identity names."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RefreshRefused("snapshot_unreadable") from error
    if not isinstance(envelope, dict) or envelope.get("schema") != SNAPSHOT_SCHEMA:
        raise RefreshRefused("snapshot_schema_mismatch")
    if envelope.get("identity_digest") != identity_digest:
        raise RefreshRefused("snapshot_identity_mismatch")
    if _canonical_digest(envelope.get("identity")) != identity_digest:
        raise RefreshRefused("snapshot_identity_unbound")
    card = envelope.get("card")
    if not isinstance(card, dict) or _digest(card_text(card)) != envelope.get("card_sha256"):
        raise RefreshRefused("snapshot_card_corrupt")
    return envelope


def _create_snapshot(
    *, identity: Mapping[str, Any], identity_digest: str, db_path: Path, repo_root: Path,
    results_path: Path, cm9_dir: Path, stage1_path: Path,
) -> dict[str, Any]:
    """Score once and bind the card to the exact inputs it was derived from."""
    from evallab.readers.scout_clouddb import StaleProjectionError, require_current_results

    try:
        require_current_results(results_path)
    except StaleProjectionError as error:
        raise RefreshRefused("results_unversioned_scout") from error
    coverage = _ledger_summary(results_path)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        card = tool_scorecard.scorecard(results_path, con, repo_root, cm9_dir, stage1_path)
    _require_complete(card)
    measured = _measure(
        db_path=db_path, repo_root=repo_root, results_path=results_path,
        cm9_dir=cm9_dir, stage1_path=stage1_path,
    )
    if measured != identity:
        # The short reads above are not one transaction (a long one would block the
        # running writers). Refuse rather than bind a card to inputs it cannot
        # reproduce; the next tick measures the new inputs.
        raise RefreshRefused("inputs_changed_mid_calculation")
    return {
        "schema": SNAPSHOT_SCHEMA,
        "at": _now(),
        "identity_digest": identity_digest,
        "card_sha256": _digest(card_text(card)),
        "identity": identity,
        "sources": {
            "results": str(results_path.resolve()),
            "db": str(db_path.resolve()),
            "repo_root": str(repo_root.resolve()),
            "cm9_dir": str(cm9_dir.resolve()),
            "stage1": str(stage1_path.resolve()),
        },
        "coverage": coverage,
        "card": card,
    }


def _refresh(
    *, db_path: Path, repo_root: Path, results_path: Path, cm9_dir: Path, stage1_path: Path,
    state_dir: Path, card_out: Path, markdown_out: Path | None,
) -> dict[str, Any]:
    db_path, repo_root, results_path = (
        Path(value).expanduser() for value in (db_path, repo_root, results_path)
    )
    cm9_dir, stage1_path = Path(cm9_dir).expanduser(), Path(stage1_path).expanduser()
    state_dir, card_out = Path(state_dir).expanduser(), Path(card_out).expanduser()

    if not results_path.is_file():
        raise RefreshIncomplete("results_missing")
    if not db_path.is_file():
        raise RefreshIncomplete("db_missing")
    if not cm9_dir.is_dir():
        raise RefreshIncomplete("cm9_missing")
    if not stage1_path.is_file():
        raise RefreshIncomplete("stage1_missing")

    identity = _measure(
        db_path=db_path, repo_root=repo_root, results_path=results_path,
        cm9_dir=cm9_dir, stage1_path=stage1_path,
    )
    identity_digest = _canonical_digest(identity)
    relative = Path(SNAPSHOTS) / ACTOR_PROJECTION_VERSION / f"{identity_digest}.json"
    snapshot_path = state_dir / relative
    created = False
    if snapshot_path.exists():
        envelope = _verify_snapshot(snapshot_path, identity_digest=identity_digest)
    else:
        envelope = _create_snapshot(
            identity=identity, identity_digest=identity_digest, db_path=db_path,
            repo_root=repo_root, results_path=results_path, cm9_dir=cm9_dir,
            stage1_path=stage1_path,
        )
        _write_atomic(snapshot_path, json.dumps(envelope, indent=2) + "\n")
        created = True

    card = envelope["card"]
    card_updated = _write_if_changed(card_out, card_text(card))
    markdown_updated = False
    if markdown_out is not None:
        markdown_updated = _write_if_changed(
            Path(markdown_out).expanduser(), tool_scorecard.render_markdown(card),
        )

    pointer = {
        "schema": POINTER_SCHEMA,
        "at": envelope["at"],
        "identity_digest": identity_digest,
        "card_sha256": envelope["card_sha256"],
        "snapshot": relative.as_posix(),
    }
    pointer_path = state_dir / CURRENT
    try:
        before = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = None
    pointer_updated = _write_if_changed(pointer_path, json.dumps(pointer, indent=2) + "\n")
    was_current = isinstance(before, dict) and all(
        before.get(field) == pointer[field] for field in ("identity_digest", "snapshot")
    )
    mixed = {
        key: value for key, value in envelope["coverage"]["detector_versions"].items()
        if len(value) > 1
    }
    return {
        "status": "unchanged" if was_current else "refreshed",
        "identity_digest": identity_digest,
        "snapshot": relative.as_posix(),
        "snapshot_at": envelope["at"],
        "snapshot_created": created,
        "card_sha256": envelope["card_sha256"],
        "card_updated": card_updated,
        "markdown_updated": markdown_updated,
        "pointer_updated": pointer_updated,
        "results_rows": envelope["coverage"]["rows"],
        **({"mixed_detector_versions": mixed} if mixed else {}),
    }


def run_once(
    *, db_path: Path, repo_root: Path, results_path: Path, cm9_dir: Path, stage1_path: Path,
    state_dir: Path, card_out: Path, markdown_out: Path | None = None,
) -> dict[str, Any]:
    """One locked refresh tick; a refused, incomplete, busy or failed run raises nothing."""
    state = Path(state_dir).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (state / LOCK).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"at": _now(), "status": "busy"}
        report: dict[str, Any] = {"at": _now(), "status": "idle"}
        try:
            report.update(_refresh(
                db_path=db_path, repo_root=repo_root, results_path=results_path,
                cm9_dir=cm9_dir, stage1_path=stage1_path, state_dir=state,
                card_out=card_out, markdown_out=markdown_out,
            ))
        except RefreshIncomplete as error:
            report.update(status="incomplete", reason=str(error))
        except RefreshRefused as error:
            report.update(status="refused", reason=str(error))
        except (ValueError, KeyError) as error:
            # The shared scorer's own refusal of a malformed row, ledger or key.
            report.update(status="refused", reason="invalid_input", detail=str(error))
        except Exception as error:
            report.update(status="error", error_type=type(error).__name__, detail=str(error))
        report["wall_seconds"] = round(time.monotonic() - started, 3)
        try:
            _write_atomic(state / LAST, json.dumps(report, indent=2, sort_keys=True) + "\n")
        except OSError:
            report["receipt"] = "unwritable"
        return report


def load_current(state_dir: Path) -> dict[str, Any] | None:
    """The current snapshot envelope, verified against its own pointer.

    ``None`` means this state holds no valid current measurement yet; a pointer that
    fails verification raises instead of returning a stale or unbound card.
    """
    state = Path(state_dir).expanduser()
    try:
        pointer = json.loads((state / CURRENT).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RefreshRefused("pointer_unreadable") from error
    if not isinstance(pointer, dict) or pointer.get("schema") != POINTER_SCHEMA:
        raise RefreshRefused("pointer_schema_mismatch")
    identity_digest = pointer.get("identity_digest")
    relative = pointer.get("snapshot")
    if not isinstance(identity_digest, str):
        raise RefreshRefused("pointer_identity_invalid")
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RefreshRefused("pointer_snapshot_path_invalid")
    envelope = _verify_snapshot(state / relative, identity_digest=identity_digest)
    if envelope.get("card_sha256") != pointer.get("card_sha256"):
        raise RefreshRefused("pointer_card_mismatch")
    return envelope


def watch_arguments(
    *, repo_root: Path, db_path: Path, trace_tools: Path, cm9_dir: Path, stage1_path: Path,
) -> dict[str, Any]:
    """One projection's refresh paths, shared by the defaults and an installer.

    Rendered from the installed snapshot's own code, so a scheduled run never reads
    or extends another projection's caches.
    """
    trace_tools = Path(trace_tools)
    state_dir = trace_tools / "scorecard-watch"
    return {
        "repo_root": Path(repo_root),
        "db_path": Path(db_path),
        "results_path": trace_tools / f"scout-{ACTOR_PROJECTION_VERSION}" / "results-v1.jsonl",
        "cm9_dir": Path(cm9_dir),
        "stage1_path": Path(stage1_path),
        "state_dir": state_dir,
        "card_out": state_dir / "tt1-scorecard.json",
    }


def argument_argv(arguments: Mapping[str, Any]) -> list[str]:
    """Render ``watch_arguments`` as LaunchAgent arguments (paths as absolute)."""
    return [
        "--repo-root", str(arguments["repo_root"]),
        "--db-path", str(arguments["db_path"]),
        "--results-path", str(arguments["results_path"]),
        "--cm9-dir", str(arguments["cm9_dir"]),
        "--stage1-path", str(arguments["stage1_path"]),
        "--state-dir", str(arguments["state_dir"]),
        "--card-out", str(arguments["card_out"]),
    ]


def build_parser() -> argparse.ArgumentParser:
    """CLI parser with the published scorecard's own default locations."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-path", type=Path, default=tool_scorecard.DEFAULT_DB)
    parser.add_argument("--repo-root", type=Path, default=tool_scorecard.DEFAULT_REPO_ROOT)
    parser.add_argument("--results-path", type=Path, default=tool_scorecard.DEFAULT_RESULTS)
    parser.add_argument("--cm9-dir", type=Path, default=tool_scorecard.DEFAULT_CM9_DIR)
    parser.add_argument("--stage1-path", type=Path, default=tool_scorecard.DEFAULT_STAGE1)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--card-out", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--markdown-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    report = run_once(**vars(build_parser().parse_args(argv)))
    print(json.dumps(report, sort_keys=True), flush=True)
    return int(report["status"] in {"incomplete", "refused", "error"})


if __name__ == "__main__":
    raise SystemExit(main())
