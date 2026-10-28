"""Watch boundaries: projection-scoped completion, admission vs actor coverage, installer."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_clouddb_blind import corpus as corpus

from evallab.readers import ACTOR_PROJECTION_VERSION, scout_clouddb
from evallab.readers.clouddb_blind import ActorHistoryUnavailable, opaque_id
from evallab.readers.scout_clouddb import StaleProjectionError
from evallab.readers.scout_watch import (
    LEDGER_INDEX_NAME,
    LEDGER_INDEX_SCHEMA,
    argument_argv,
    build_parser,
    completed_runs,
    ledger_coverage,
    run_once,
    watch_arguments,
)

SCANNERS = ("repeated_assistant_message", "harness_parse_errors", "native_tool_call_markup", "restored_tool_calls")
VERSION = ACTOR_PROJECTION_VERSION
STALE = "native-actor-v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts/ops/launchd/install-scout-watch.sh"


def _row(run: str, scanner: str, **changes: object) -> dict:
    return {
        "run_id": run, "tool": "scout", "source": "cloud-trace-db",
        "projection_version": VERSION, "detector": scanner,
        "detector_version": "inspect-scout/0.5.4:1",
        "value": 0, "flag": None if scanner == "restored_tool_calls" else False,
        **changes,
    }


def _keymap_row(trace_id: str) -> dict:
    return {
        "opaque_id": opaque_id(trace_id), "run_id": trace_id, "source": "cloud-trace-db",
        "projection_version": VERSION,
    }


def _complete_rows(runs: Any) -> list[dict]:
    return [_row(run, scanner) for run in runs for scanner in SCANNERS]


def _lines(rows: Any) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


@pytest.fixture
def scout_runtime() -> Any:
    return pytest.importorskip("inspect_scout")


def test_imported_or_partially_scanned_runs_remain_pending(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [_row("complete", scanner) for scanner in SCANNERS]
    rows += [_row("partial", scanner) for scanner in SCANNERS[:-1]]
    rows += [_row("error", scanner, value=None) for scanner in SCANNERS]
    rows += [_row("old-version", scanner, detector_version="inspect-scout/0.5.3:1")
             for scanner in SCANNERS]
    rows += [_row("external", scanner, source="cheatbench-actions-v1") for scanner in SCANNERS]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Informational null flags must not cause repeated scans, but missing values
    # and missing scanners must never be mistaken for completed negative scans.
    assert completed_runs(
        results, scanners=SCANNERS, scout_version="0.5.4", projection_version=VERSION,
    ) == {"complete"}


def test_rows_from_another_projection_never_complete_a_run(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [_row("legacy", scanner) for scanner in SCANNERS]
    rows = [
        {key: value for key, value in row.items() if key != "projection_version"}
        for row in rows
    ]
    rows += [_row("previous", scanner, projection_version=STALE) for scanner in SCANNERS]
    rows += [_row("current", scanner) for scanner in SCANNERS]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Unversioned and previous-projection measurements are retained evidence, not
    # coverage of the current actor projection.
    assert completed_runs(
        results, scanners=SCANNERS, scout_version="0.5.4", projection_version=VERSION,
    ) == {"current"}


def test_recovered_scan_finishes_previously_failed_run(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [_row("recovered", scanner, value=None) for scanner in SCANNERS]
    rows += [_row("recovered", scanner + ":label", value=1) for scanner in SCANNERS]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert completed_runs(
        results, scanners=SCANNERS, scout_version="0.5.4", projection_version=VERSION,
    ) == {"recovered"}
    # A newly added scanner invalidates coverage, rather than silently treating
    # the former scanner set as complete for the new pipeline.
    assert completed_runs(
        results, scanners=(*SCANNERS, "new_rule"), scout_version="0.5.4",
        projection_version=VERSION,
    ) == set()


def test_missing_ledger_means_nothing_has_been_scanned(tmp_path: Path) -> None:
    assert completed_runs(
        tmp_path / "missing", scanners=SCANNERS, scout_version="0.5.4",
        projection_version=VERSION,
    ) == set()
    with pytest.raises(ValueError, match="At least one"):
        completed_runs(
            tmp_path / "missing", scanners=(), scout_version="0.5.4", projection_version=VERSION,
        )


def test_coverage_index_tracks_exact_ledger_bytes_not_length_or_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    results, index = tmp_path / "results-v1.jsonl", tmp_path / LEDGER_INDEX_NAME
    results.write_text(_lines(_complete_rows(["run-a"])), encoding="utf-8")
    arguments = {
        "index_path": index, "scanners": SCANNERS, "scout_version": "0.5.4",
        "projection_version": VERSION,
    }
    assert ledger_coverage(results, **arguments) == {"run-a"}
    stored = json.loads(index.read_text())
    assert stored["ledger_sha256"] == hashlib.sha256(results.read_bytes()).hexdigest()
    assert stored["runs"] == ["run-a"]

    # The stored index answers an unchanged ledger without re-parsing it.
    def refuse(*_: Any, **__: Any) -> set[str]:
        raise AssertionError("coverage recomputed for unchanged ledger bytes")

    monkeypatch.setattr("evallab.readers.scout_watch._coverage", refuse)
    assert ledger_coverage(results, **arguments) == {"run-a"}
    monkeypatch.undo()

    # Same byte length, earlier Scout version. Restoring the mtime proves the
    # index is keyed by content, not by size or timestamp.
    original = results.read_text()
    rewritten = original.replace("inspect-scout/0.5.4:1", "inspect-scout/0.5.3:1")
    assert len(rewritten) == len(original)
    stamp = results.stat().st_mtime_ns
    results.write_text(rewritten, encoding="utf-8")
    os.utime(results, ns=(stamp, stamp))
    assert ledger_coverage(results, **arguments) == set()
    assert json.loads(index.read_text())["ledger_sha256"] == hashlib.sha256(
        rewritten.encode()
    ).hexdigest()


def test_coverage_index_is_scoped_to_projection_scanners_and_schema(tmp_path: Path) -> None:
    results, index = tmp_path / "results-v1.jsonl", tmp_path / LEDGER_INDEX_NAME
    results.write_text(_lines(_complete_rows(["run-a"])), encoding="utf-8")
    assert ledger_coverage(
        results, index_path=index, scanners=SCANNERS, scout_version="0.5.4",
        projection_version=VERSION,
    ) == {"run-a"}
    # Another projection or a larger scanner set never inherits stored coverage.
    assert ledger_coverage(
        results, index_path=index, scanners=SCANNERS, scout_version="0.5.4",
        projection_version=STALE,
    ) == set()
    assert ledger_coverage(
        results, index_path=index, scanners=(*SCANNERS, "new_rule"), scout_version="0.5.4",
        projection_version=VERSION,
    ) == set()
    # A foreign, unreadable or malformed index is ignored rather than trusted.
    for payload in (
        json.dumps({"schema": "other", "runs": ["ghost"]}),
        "not json",
        json.dumps({"schema": LEDGER_INDEX_SCHEMA, "runs": [1, 2]}),
    ):
        index.write_text(payload, encoding="utf-8")
        assert ledger_coverage(
            results, index_path=index, scanners=SCANNERS, scout_version="0.5.4",
            projection_version=VERSION,
        ) == {"run-a"}


def test_unversioned_scout_rows_refuse_before_the_index_is_replaced(tmp_path: Path) -> None:
    results, index = tmp_path / "results-v1.jsonl", tmp_path / LEDGER_INDEX_NAME
    rows = _complete_rows(["run-a"]) + [
        {"tool": "scout", "run_id": "legacy", "value": 0},
    ]
    before = _lines(rows)
    results.write_text(before, encoding="utf-8")
    index.write_text(_lines([{"schema": LEDGER_INDEX_SCHEMA, "runs": ["stale"]}]))
    stored = index.read_bytes()
    with pytest.raises(StaleProjectionError, match="unversioned Scout results"):
        ledger_coverage(
            results, index_path=index, scanners=SCANNERS, scout_version="0.5.4",
            projection_version=VERSION,
        )
    # The ledger and any pre-existing index are evidence: neither is rewritten.
    assert results.read_text() == before
    assert index.read_bytes() == stored


class Pipeline:
    """A stand-in import/scan/collect pair over one synthetic projection."""

    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        self.keymap = self.state / "keymap-v1.jsonl"
        self.results = self.state / "results-v1.jsonl"
        self.transcripts = tmp_path / "transcripts"
        self.scans = tmp_path / "scans"
        self.eligible = 0
        self.available: list[str] = []
        self.quarantined: list[dict[str, Any]] = []
        self.synced: list[Path] = []
        self.scanned: list[list[str]] = []
        self.locations: dict[str, list[str]] = {}
        self.refs: Any = None

    def arguments(self, db_path: Path, repo_root: Path, batch_size: int = 128) -> dict[str, Any]:
        return {
            "db_path": db_path, "repo_root": repo_root, "transcripts_dir": self.transcripts,
            "scans_dir": self.scans, "keymap_path": self.keymap, "results_path": self.results,
            "state_dir": self.state, "batch_size": batch_size,
        }

    def sync(self, db_path: Path, transcripts_dir: Path, keymap_path: Path, *,
             repo_root: Path, cheatbench: bool = False,
             refs: Any = None) -> dict[str, Any]:
        assert cheatbench is False
        self.refs = refs
        self.synced.append(keymap_path)
        keymap_path.parent.mkdir(parents=True, exist_ok=True)
        keyed = {row["opaque_id"] for row in scout_clouddb._jsonl(keymap_path)}
        with keymap_path.open("a", encoding="utf-8") as stream:
            for trace_id in self.available:
                if opaque_id(trace_id) in keyed:
                    continue
                stream.write(json.dumps({
                    "opaque_id": opaque_id(trace_id), "run_id": trace_id,
                    "source": "cloud-trace-db", "projection_version": VERSION,
                }) + "\n")
        return {
            "source": "cloud-trace-db", "projection_version": VERSION,
            "eligible": self.eligible or len(self.available) + len(self.quarantined),
            "inserted": len(self.available), "unchanged": 0,
            "unavailable": len(self.quarantined), "quarantined": list(self.quarantined),
            "transcripts_dir": str(transcripts_dir),
        }

    def scan(self, transcripts_dir: Path, scans_dir: Path, transcript_ids: Any = ()) -> Any:
        selected = list(transcript_ids or ())
        self.scanned.append(selected)
        location = str(scans_dir / f"scan_id={len(self.scanned)}")
        self.locations[location] = selected
        return SimpleNamespace(location=location, complete=True, errors=[])

    def collect(self, scan_location: str, keymap_path: Path, results_path: Path) -> int:
        keymap = {row["opaque_id"]: row["run_id"]
                  for row in scout_clouddb._jsonl(Path(keymap_path))}
        lines = []
        for opaque in self.locations[str(scan_location)]:
            for scanner in SCANNERS:
                lines.append(json.dumps({
                    "schema": "evallab.trace_tool_result/v1", "run_id": keymap[opaque],
                    "source": "cloud-trace-db", "projection_version": VERSION, "tool": "scout",
                    "detector": scanner,
                    "detector_version": f"inspect-scout/{scout_clouddb.SCOUT_VERSION}:1",
                    "value": 0, "flag": False, "evidence_steps": [],
                    "scan": str(scan_location), "at": "2026-10-28T00:00:00+00:00",
                }) + "\n")
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with results_path.open("a", encoding="utf-8") as stream:
            stream.writelines(lines)
        return len(lines)

    def ledger(self) -> list[dict[str, Any]]:
        if not self.results.exists():
            return []
        return [json.loads(line) for line in self.results.read_text().splitlines()]


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    fake = Pipeline(tmp_path)
    monkeypatch.setattr(scout_clouddb, "sync", fake.sync)
    monkeypatch.setattr(scout_clouddb, "scan", fake.scan)
    monkeypatch.setattr(scout_clouddb, "collect", fake.collect)
    return fake


def test_admission_is_not_actor_coverage_and_missing_histories_are_never_scanned(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    pipeline.eligible = 4
    pipeline.available = ["trace-goodfire", "trace-tb2", "trace-tb4"]
    pipeline.quarantined = [{
        "run_id": "trace-mimo", "opaque_id": opaque_id("trace-mimo"),
        "category": "actor_history_unavailable", "step_index": -1,
        "reason": "actor_history_not_retained",
    }]
    report = run_once(**pipeline.arguments(db_path, root))
    assert report["status"] == "scanned"
    # Full admission stays visible even though one run has no actor history.
    assert (report["eligible"], report["admitted"]) == (4, 4)
    assert (report["actor_available"], report["scanned"]) == (3, 3)
    assert report["actor_missing"] == ["trace-mimo"]
    assert report["unavailable"] == 1
    assert report["quarantined"] == pipeline.quarantined
    assert pipeline.scanned == [[
        opaque_id(trace) for trace in ("trace-goodfire", "trace-tb2", "trace-tb4")
    ]]
    scanned_runs = {row["run_id"] for row in pipeline.ledger()}
    assert scanned_runs == {"trace-goodfire", "trace-tb2", "trace-tb4"}
    assert "trace-mimo" not in scanned_runs
    receipt = json.loads((pipeline.state / "watch-last.json").read_text())
    assert receipt["actor_missing"] == ["trace-mimo"]
    assert receipt["quarantined"] == pipeline.quarantined
    assert receipt["actor_available"] == 3

    # An unknown actor history is not retried forever as a clean run, and it stays
    # explicitly unknown rather than disappearing behind "idle".
    idle = run_once(**pipeline.arguments(db_path, root))
    assert idle["status"] == "idle" and idle["scanned"] == 0 and idle["pending"] == 0
    assert idle["actor_missing"] == ["trace-mimo"]
    assert idle["actor_available"] == 3


def test_sealed_or_held_admissions_are_never_scanned_even_when_keyed(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    pipeline.eligible = 4
    # Held/sealed run ids are keyed here on purpose: admission, not the key map,
    # decides what may be read and scanned.
    pipeline.available = [
        "trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4",
        "trace-held", "trace-cross-sealed",
    ]
    report = run_once(**pipeline.arguments(db_path, root))
    assert report["admitted"] == report["actor_available"] == 4
    assert report["actor_missing"] == []
    scanned = {row["run_id"] for row in pipeline.ledger()}
    assert scanned == {"trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4"}
    assert not scanned & {"trace-held", "trace-cross-sealed"}


def test_new_arrivals_are_scanned_within_the_batch_limit(corpus: tuple, pipeline: Pipeline) -> None:
    root, db_path, _ = corpus
    pipeline.available = ["trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4"]
    first = run_once(**pipeline.arguments(db_path, root, batch_size=2))
    assert (first["status"], first["scanned"], first["pending"]) == ("scanned", 2, 4)
    second = run_once(**pipeline.arguments(db_path, root, batch_size=2))
    assert (second["status"], second["scanned"], second["pending"]) == ("scanned", 2, 2)
    assert set(pipeline.scanned[0]) & set(pipeline.scanned[1]) == set()
    assert len({row["run_id"] for row in pipeline.ledger()}) == 4
    third = run_once(**pipeline.arguments(db_path, root, batch_size=2))
    assert (third["status"], third["scanned"], third["pending"]) == ("idle", 0, 0)


def test_unavailable_remainder_never_imports_or_opens_the_scout_database(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, db_path, _ = corpus
    pipeline = Pipeline(tmp_path)
    monkeypatch.setattr(scout_clouddb, "scan", pipeline.scan)
    monkeypatch.setattr(scout_clouddb, "collect", pipeline.collect)
    monkeypatch.setattr(scout_clouddb, "_require_scout", lambda: None)
    real = scout_clouddb.blind_messages

    def unavailable(con: Any, trace_id: str) -> list[dict[str, Any]]:
        if trace_id == "trace-mimo":
            raise ActorHistoryUnavailable(trace_id)
        return real(con, trace_id)

    monkeypatch.setattr(scout_clouddb, "blind_messages", unavailable)
    opened: list[str] = []
    module = types.ModuleType("inspect_scout")
    module.transcripts_db = lambda path: opened.append(str(path))
    monkeypatch.setitem(sys.modules, "inspect_scout", module)
    keyed = ("trace-goodfire", "trace-tb2", "trace-tb4")
    pipeline.state.mkdir(parents=True, exist_ok=True)
    pipeline.keymap.write_text(_lines([_keymap_row(trace) for trace in keyed]), encoding="utf-8")
    pipeline.results.write_text(_lines(_complete_rows(keyed)), encoding="utf-8")

    report = run_once(**pipeline.arguments(db_path, root))

    # The steady state: every available actor history is keyed and complete, the
    # one unkeyed run still has no history, and the transcript database is never
    # opened or imported for the empty insert.
    assert (report["status"], report["scanned"], report["pending"]) == ("idle", 0, 0)
    assert (report["admitted"], report["actor_available"]) == (4, 3)
    assert report["actor_missing"] == ["trace-mimo"]
    assert (report["inserted"], report["unavailable"]) == (0, 1)
    assert opened == []
    index = json.loads((pipeline.state / LEDGER_INDEX_NAME).read_text())
    assert index["runs"] == sorted(keyed)
    assert len(pipeline.keymap.read_text().splitlines()) == 3


def test_new_actor_history_is_inserted_and_scanned_on_the_next_sweep(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scout_runtime: Any,
) -> None:
    root, db_path, _ = corpus
    pipeline = Pipeline(tmp_path)
    monkeypatch.setattr(scout_clouddb, "scan", pipeline.scan)
    monkeypatch.setattr(scout_clouddb, "collect", pipeline.collect)
    real = scout_clouddb.blind_messages

    def unavailable(con: Any, trace_id: str) -> list[dict[str, Any]]:
        if trace_id == "trace-mimo":
            raise ActorHistoryUnavailable(trace_id)
        return real(con, trace_id)

    monkeypatch.setattr(scout_clouddb, "blind_messages", unavailable)
    keyed = ("trace-goodfire", "trace-tb2", "trace-tb4")
    pipeline.state.mkdir(parents=True, exist_ok=True)
    pipeline.keymap.write_text(_lines([_keymap_row(trace) for trace in keyed]), encoding="utf-8")
    pipeline.results.write_text(_lines(_complete_rows(keyed)), encoding="utf-8")
    idle = run_once(**pipeline.arguments(db_path, root))
    assert (idle["status"], idle["actor_missing"], pipeline.scanned) == (
        "idle", ["trace-mimo"], [],
    )

    # The source later retains trace-mimo's actor chronology: the next sweep
    # imports it, keys it, and scans exactly it, preserving the earlier key map.
    monkeypatch.setattr(scout_clouddb, "blind_messages", real)
    prior = pipeline.keymap.read_bytes()
    report = run_once(**pipeline.arguments(db_path, root))
    assert (report["status"], report["scanned"]) == ("scanned", 1)
    assert (report["actor_available"], report["actor_missing"]) == (4, [])
    assert pipeline.scanned == [[opaque_id("trace-mimo")]]
    assert pipeline.keymap.read_bytes().startswith(prior)
    assert json.loads(pipeline.keymap.read_text().splitlines()[-1])["run_id"] == "trace-mimo"


def test_partial_detector_coverage_keeps_only_the_unfinished_run(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    available = ["trace-goodfire", "trace-tb2", "trace-tb4"]
    pipeline.eligible = 4
    pipeline.available = available
    pipeline.state.mkdir(parents=True, exist_ok=True)
    pipeline.keymap.write_text(_lines([_keymap_row(trace) for trace in available]))
    rows = _complete_rows(["trace-goodfire", "trace-tb2"])
    rows += [_row("trace-tb4", scanner) for scanner in SCANNERS[:-1]]
    pipeline.results.write_text(_lines(rows))
    report = run_once(**pipeline.arguments(db_path, root))
    assert (report["pending"], report["scanned"]) == (1, 1)
    assert pipeline.scanned == [[opaque_id("trace-tb4")]]


def test_admission_is_redecided_every_sweep_never_reused(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    pipeline.available = ["trace-goodfire", "trace-tb2"]
    first = run_once(**pipeline.arguments(db_path, root))
    assert (first["admitted"], first["actor_available"], first["scanned"]) == (4, 2, 2)

    assignments = root / "research/answer-keys/splits/assignments.csv"
    assignments.write_text(
        assignments.read_text(encoding="utf-8") + "tb2-to-tb21-v1,task-tb2,task-tb2,test\n",
        encoding="utf-8",
    )
    second = run_once(**pipeline.arguments(db_path, root))
    # A newly TEST-assigned task is dropped even though its transcript is keyed
    # and already covered: admission has no cache to go stale.
    assert (second["admitted"], second["actor_available"]) == (3, 1)
    assert second["scanned"] == 0


def test_stale_projection_results_never_mark_a_run_complete(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    pipeline.available = ["trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4"]
    pipeline.sync(db_path, pipeline.transcripts, pipeline.keymap, repo_root=root)
    # Three runs already have current-projection coverage; the fourth only has
    # rows from the previous projection, which must be rescanned.
    pipeline.scan(pipeline.transcripts, pipeline.scans,
                  [opaque_id(trace) for trace in ("trace-mimo", "trace-tb2", "trace-tb4")])
    pipeline.collect(list(pipeline.locations)[0], pipeline.keymap, pipeline.results)
    stale = "".join(json.dumps(_row("trace-goodfire", scanner, projection_version=STALE)) + "\n"
                    for scanner in SCANNERS).encode()
    with pipeline.results.open("ab") as stream:
        stream.write(stale)
    prior = pipeline.results.read_bytes()

    report = run_once(**pipeline.arguments(db_path, root))
    assert report["pending"] == 1 and report["scanned"] == 1
    assert pipeline.scanned[-1] == [opaque_id("trace-goodfire")]
    assert pipeline.results.read_bytes().startswith(prior)
    assert completed_runs(
        pipeline.results, scanners=SCANNERS, scout_version="0.5.4", projection_version=VERSION,
    ) == {"trace-goodfire", "trace-mimo", "trace-tb2", "trace-tb4"}


def test_stale_results_ledger_is_refused_before_any_work(
    corpus: tuple, pipeline: Pipeline,
) -> None:
    root, db_path, _ = corpus
    pipeline.state.mkdir(parents=True, exist_ok=True)
    pipeline.results.write_text(json.dumps({
        "run_id": "legacy", "tool": "scout", "source": "cloud-trace-db", "value": 0,
    }) + "\n", encoding="utf-8")
    before = pipeline.results.read_bytes()
    with pytest.raises(StaleProjectionError, match="Scout results"):
        run_once(**pipeline.arguments(db_path, root))
    assert pipeline.synced == []
    assert pipeline.scanned == []
    assert pipeline.results.read_bytes() == before
    receipt = json.loads((pipeline.state / "watch-last.json").read_text())
    assert receipt["status"] == "error" and receipt["error_type"] == "StaleProjectionError"


def test_default_paths_and_watch_arguments_name_one_projection() -> None:
    defaults = scout_clouddb
    parsed = vars(build_parser(defaults).parse_args([]))
    expected = watch_arguments(
        repo_root=defaults.DEFAULT_REPO_ROOT, db_path=defaults.DEFAULT_DB,
        trace_tools=defaults.TRACE_TOOLS_STATE, viewer=defaults.DEFAULT_SCANS.parent.parent,
        batch_size=128,
    )
    assert parsed == expected
    assert expected["transcripts_dir"] == defaults.DEFAULT_TRANSCRIPTS
    assert expected["keymap_path"] == defaults.DEFAULT_KEYMAP
    assert expected["results_path"] == defaults.DEFAULT_RESULTS
    assert expected["state_dir"] == defaults.DEFAULT_STATE
    assert expected["scans_dir"] == defaults.DEFAULT_SCANS / "watch"
    assert expected["db_path"] == defaults.DEFAULT_DB
    for key in ("state_dir", "keymap_path", "results_path", "transcripts_dir"):
        assert VERSION in str(expected[key])
    assert vars(build_parser(defaults).parse_args(argument_argv(expected))) == expected


def _stub_uv() -> str:
    return f"""#!{sys.executable}
import subprocess
import sys
from pathlib import Path

argv = sys.argv[1:]
def opt(name):
    return argv[argv.index(name) + 1]

if argv[0] == "sync":
    project = Path(opt("--project"))
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")])
    site = next((project / ".venv").glob("lib/python*/site-packages"))
    # The snapshot's own source first, then this interpreter's dependencies: the
    # installer and the module must agree on one committed revision.
    extra = [path for path in sys.path[1:] if path and Path(path).is_dir()]
    lines = [f"import sys; sys.path.insert(0, {{str(project / 'src')!r}})", *extra]
    (site / "evallab-snapshot.pth").write_text("\\n".join(lines) + "\\n")
elif argv[0] == "pip" and argv[1] == "install":
    if not Path(opt("--python")).is_file():
        sys.exit("stub uv: missing snapshot interpreter")
else:
    sys.exit(f"stub uv: unexpected {{argv}}")
"""


def test_installer_renders_a_committed_snapshots_versioned_paths(tmp_path: Path) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    repo = (tmp_path / "repo").resolve()
    bin_dir = (tmp_path / "bin").resolve()
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(_stub_uv(), encoding="utf-8")
    uv.chmod(0o755)
    env = {
        **os.environ, "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    shutil.copytree(
        REPO_ROOT / "src/evallab", repo / "src/evallab",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    script = repo / "scripts/ops/launchd/install-scout-watch.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(INSTALLER, script)
    script.chmod(0o755)
    for command in (["init", "-q", "-b", "main", str(repo)], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", *command], cwd=repo, env=env, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()

    runtime, state, viewer = (
        (tmp_path / name).resolve() for name in ("runtime", "state", "viewer")
    )
    db = (tmp_path / "cloud.sqlite").resolve()
    db.write_bytes(b"")
    dest = home / "Library/LaunchAgents/com.example.scout-watch.plist"
    done = subprocess.run(
        [str(script), "--label", "com.example.scout-watch", "--runtime-dir", str(runtime),
         "--state-dir", str(state), "--db-path", str(db), "--project", str(viewer),
         "--interval", "60", "--batch-size", "4"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr

    snapshot = (runtime / "runtime" / commit).resolve()
    loaded = plistlib.loads(dest.read_bytes())
    assert loaded["Label"] == "com.example.scout-watch"
    assert loaded["WorkingDirectory"] == str(snapshot)
    assert loaded["StartInterval"] == 60
    arguments = loaded["ProgramArguments"]
    assert arguments[:4] == [
        str(snapshot / ".venv/bin/python"), "-I", "-m", "evallab.readers.scout_watch",
    ]
    expected = watch_arguments(
        repo_root=snapshot, db_path=db, trace_tools=state, viewer=viewer, batch_size=4,
    )
    parsed = vars(build_parser(scout_clouddb).parse_args(arguments[4:]))
    assert parsed == expected
    assert parsed["state_dir"] == state / f"scout-{VERSION}"
    assert parsed["keymap_path"] == state / f"scout-{VERSION}/keymap-v1.jsonl"
    assert parsed["results_path"] == state / f"scout-{VERSION}/results-v1.jsonl"
    assert parsed["transcripts_dir"] == viewer / "data" / f"transcripts_clouddb_dev-{VERSION}"
    assert parsed["scans_dir"] == viewer / "scans" / f"trace-tools-{VERSION}/watch"
    # The unversioned caches of earlier projections are not referenced at all.
    for legacy in (state / "keymap-v1.jsonl", state / "results-v1.jsonl",
                   viewer / "data/transcripts_clouddb_dev", viewer / "scans/trace-tools"):
        assert str(legacy) not in arguments[4:]
    assert f"projection: {VERSION}" in done.stdout
    assert str(parsed["state_dir"]) in done.stdout
