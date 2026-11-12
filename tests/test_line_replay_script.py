"""Standard replay entry point on tiny synthetic frozen sets that run the real replay.

Each set has a few $0 runs, cached AutoReview verdicts and no third-party data.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTE = "google-antigravity/gemini-3.8-flash"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _rows(path: Path, rows) -> None:
    _write(path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "line_replay_script", ROOT / "scripts/line-replay.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seal(case) -> None:
    """Recompute manifest, provenance and the pin the way a reviewed refresh PR would."""
    root = case.root
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or relative.parts[0] not in ("inputs", "spec.json"):
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "kind": "input",
            }
        )
    _write(
        root / "MANIFEST.json",
        {
            "schema": "evallab.line_replay_manifest/v1",
            "files": files,
            "input_files": len(files),
            "run_count": case.runs,
            "corpora": {"synthetic": case.runs},
        },
    )
    pin = {
        "baseline_code_rev": "synthetic-revision",
        "baseline_sha256": _sha(root / "baseline.json"),
        "manifest_sha256": _sha(root / "MANIFEST.json"),
        "spec_sha256": _sha(root / "spec.json"),
    }
    _write(
        root / "baseline-provenance.json",
        dict(
            pin,
            code_rev=pin["baseline_code_rev"],
            code_dirty=False,
            authorized_by="synthetic reviewer",
            reason="synthetic fixture",
        ),
    )
    pin_file = case.pins
    _write(
        pin_file,
        {
            "schema": "evallab.line_replay_pins/v1",
            "sets": {"replay-v1": {k: v for k, v in pin.items()}},
        },
    )


def _verdict(run_id):
    """A stored AutoReview reading for a run it never needed to read."""
    return {
        "id": run_id,
        "split": "dev",
        "decision": "keep",
        "evidence_steps": [],
        "stage_decisions": {
            "autoreview": {
                "decision": "pass",
                "reasons": ["AUTOREVIEW_NOT_ELIGIBLE"],
                "details": {"eligible": False},
            }
        },
    }


@pytest.fixture
def make_set(tmp_path, monkeypatch):
    """Build a sealed set whose real replay passes.

    Cheats c0.. are labelled cheats; the first ``caught`` of them sit on a task marked
    weak, which sends them to review. Honest runs h0.. are kept.

    None of these synthetic cases tests corpus assignments or the current Git state.
    Only those external probes are stubbed; replay, sealing, receipt checks and
    regression detection stay real.
    """
    from evallab import hack_drop as hd
    from evallab.line import core

    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: None)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    module = _load_script()

    def build(cheats=2, caught=2, clean=1, claim_all_dropped=False):
        base = tmp_path / "work"
        root = base / "replay-v1"
        names = [f"c{i}" for i in range(cheats)] + [f"h{i}" for i in range(clean)]
        manifest, labels, cache = [], [], []
        for index, name in enumerate(names):
            run = root / "inputs/rollouts" / name
            _write(
                run / "trajectory.json",
                {
                    "schema_version": "ATIF-v1.4",
                    "session_id": name,
                    "agent": {"name": "a", "version": "1"},
                    "steps": [{"step_id": 1, "source": "agent", "message": "hi"}],
                },
            )
            task = "weak-task" if name.startswith("c") and index < caught else "ok-task"
            _write(run / "run.json", {"id": name, "task_id": task, "split": "dev", "reward": 1.0})
            manifest.append({"id": name, "path": str(run), "task_id": task})
            labels.append({"id": name, "is_cheat": name.startswith("c"), "split": "dev"})
            cache.append(_verdict(name))
        _rows(root / "inputs/corpora/manifest.jsonl", manifest)
        _rows(root / "inputs/corpora/labels.jsonl", labels)
        _rows(root / "inputs/cached/kept.jsonl", cache)
        _write(
            root / "inputs/status/task-status.json",
            {
                "schema": "evallab.task_status/v1",
                "tasks": [
                    {"task_id": "weak-task", "status": "weak", "reason": "SYNTHETIC_WEAK"},
                    {"task_id": "ok-task", "status": "certified", "reason": "SYNTHETIC_OK"},
                ],
            },
        )
        _write(root / "inputs/station/verdicts.jsonl", "")
        _write(
            root / "spec.json",
            {
                "schema": "evallab.line_spec/v1",
                "batch_id": "replay-v1",
                "split": "dev",
                "sources": [
                    {
                        "corpus": "synthetic",
                        "rollouts": str(root / "inputs/corpora/manifest.jsonl"),
                        "labels": str(root / "inputs/corpora/labels.jsonl"),
                    }
                ],
                "task_status": str(root / "inputs/status/task-status.json"),
                "task_action": "review",
                "autoreview": {
                    "mode": "cached",
                    "cache": str(root / "inputs/cached"),
                    "route": None,
                    "approval": None,
                    "max_usd": 0,
                },
                "models": {"allowed": [ROUTE]},
                "audit": {"rate": 0.05, "seed": "synthetic"},
                "judge": {
                    "queue": str(root / "scratch-queue.jsonl"),
                    "write": False,
                    "verdicts": str(root / "inputs/station/verdicts.jsonl"),
                },
                "labels_policy": {"attempted_cheat": "exclude"},
                "out_root": str(root / "batches"),
            },
        )
        from evallab.line.replay import replay

        result, status = replay(
            root / "spec.json", root / "baseline.json", ROOT, write_baseline=True
        )
        assert status == 0, result
        shutil.rmtree(root / "batches")
        if claim_all_dropped:  # an older, better detector: every cheat was dropped
            data = json.loads((root / "baseline.json").read_text())
            group = data["scoreboard"]["by_corpus"]["synthetic"]
            for metric in ("drop_or_review_recall", "drop_recall"):
                group["metrics"][metric]["k"] = cheats
            for row in data["decisions"]:
                if row["run_id"].startswith("c"):
                    row["decision"] = "drop"
            _write(root / "baseline.json", data)
        case = SimpleNamespace(
            module=module, root=root, pins=base / "replay_sets.json", runs=len(names)
        )
        seal(case)
        monkeypatch.setattr(module, "PINS_PATH", case.pins)
        monkeypatch.setenv("EVALLAB_LINE_REPLAY_SET", str(root))
        return case

    return build


def _batches(case) -> list[Path]:
    return list((case.root / "batches").glob("*")) if (case.root / "batches").exists() else []


def test_clean_set_passes_and_summarises_each_corpus(make_set, capsys):
    case = make_set()
    assert case.module.main([]) == 0
    out = capsys.readouterr().out
    assert "Verdict: PASS" in out
    assert "Changed decisions: 0" in out
    assert "synthetic: recall 2/2 (100.0%) -> 2/2 (100.0%); false alarms 0/1 (0.0%) -> 0/1" in out


def test_real_regression_exits_one_and_names_what_regressed(make_set, capsys):
    case = make_set(cheats=3, caught=1, claim_all_dropped=True)
    assert case.module.main([]) == 1
    out = capsys.readouterr().out
    assert "Verdict: REGRESSION" in out
    assert "regression: synthetic drop_or_review_recall" in out
    assert "Changed decisions: 3" in out
    report = next(case.root.glob("batches/*/changed-decisions.json"))
    assert {row["run_id"] for row in json.loads(report.read_text())} == {"c0", "c1", "c2"}


def test_replay_input_error_exits_two_and_is_explained(make_set, capsys):
    case = make_set()
    spec = json.loads((case.root / "spec.json").read_text())
    spec["autoreview"]["mode"] = "live"  # replay refuses live AutoReview
    _write(case.root / "spec.json", spec)
    seal(case)
    assert case.module.main([]) == 2
    err = capsys.readouterr().err
    assert "live AutoReview" in err


def test_tracked_pin_names_replay_v1_with_full_hashes():
    pin = _load_script()._load_pin("replay-v1")
    assert len(pin["baseline_code_rev"]) == 40
    assert all(len(pin[key]) == 64 for key in ("manifest_sha256", "spec_sha256", "baseline_sha256"))


def test_absent_set_is_the_only_skip(make_set, tmp_path, capsys):
    case = make_set()
    assert case.module.main(["--set", str(tmp_path / "nowhere")]) == 77
    assert "SKIP" in capsys.readouterr().err
    assert not _batches(case)


@pytest.mark.parametrize(
    "name", ["spec.json", "MANIFEST.json", "baseline.json", "baseline-provenance.json"]
)
def test_existing_but_broken_set_is_invalid_not_skip(make_set, name, capsys):
    case = make_set()
    (case.root / name).unlink()
    assert case.module.main([]) == 2
    err = capsys.readouterr().err
    assert "SKIP" not in err and name in err
    assert not _batches(case)


def test_empty_directory_regular_file_and_dangling_symlink_are_invalid(make_set, tmp_path):
    case = make_set()
    empty = tmp_path / "empty"
    empty.mkdir()
    plain = tmp_path / "plain"
    plain.write_text("x")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "gone")
    for path in (empty, plain, dangling):
        assert case.module.main(["--set", str(path)]) == 2


@pytest.mark.parametrize("name", ["baseline.json", "MANIFEST.json", "spec.json"])
def test_editing_a_pinned_file_is_refused_until_the_pin_is_updated(make_set, name, capsys):
    case = make_set()
    path = case.root / name
    data = json.loads(path.read_text())
    data["tampered"] = True
    _write(path, data)
    assert case.module.main([]) == 2
    assert "pin" in capsys.readouterr().err
    assert not _batches(case)


def test_baseline_edit_with_consistent_manifest_and_provenance_still_needs_the_pin(make_set):
    case = make_set()
    data = json.loads((case.root / "baseline.json").read_text())
    data["scoreboard"]["by_corpus"]["synthetic"]["metrics"]["drop_or_review_recall"]["k"] = 0
    _write(case.root / "baseline.json", data)
    pins = json.loads(case.pins.read_text())
    original = dict(pins["sets"]["replay-v1"])
    seal(case)  # the attacker also rewrites provenance and manifest, but not the tracked pin
    pins["sets"]["replay-v1"] = original
    _write(case.pins, pins)
    assert case.module.main([]) == 2
    assert not _batches(case)


def test_provenance_is_mandatory_and_must_agree_with_the_pin(make_set, capsys):
    case = make_set()
    path = case.root / "baseline-provenance.json"
    good = json.loads(path.read_text())
    for field, value in [
        ("baseline_sha256", "0" * 64),
        ("manifest_sha256", "1" * 64),
        ("spec_sha256", "2" * 64),
        ("code_rev", "other"),
        ("code_dirty", True),
        ("authorized_by", ""),
        ("reason", None),
    ]:
        _write(path, dict(good, **{field: value}))
        assert case.module.main([]) == 2, field
        assert "baseline provenance" in capsys.readouterr().err, field


def test_unknown_set_name_and_broken_pin_file_are_invalid(make_set):
    case = make_set()
    assert case.module.main(["--name", "replay-v9"]) == 2
    case.pins.write_text("{broken")
    assert case.module.main([]) == 2
    case.pins.unlink()
    assert case.module.main([]) == 2


def test_unlisted_file_the_spec_reads_is_refused_before_replay(make_set, capsys):
    case = make_set()
    _write(case.root / "inputs/cached/review.jsonl", "{corrupt\n")
    assert case.module.main([]) == 2
    assert "unlisted" in capsys.readouterr().err
    assert not _batches(case)


def test_listed_input_changed_is_refused_even_at_the_same_size(make_set, capsys):
    case = make_set()
    path = case.root / "inputs/corpora/labels.jsonl"
    data = path.read_bytes()
    path.write_bytes(data.replace(b"true", b"fals"))
    assert case.module.main([]) == 2
    assert "labels.jsonl" in capsys.readouterr().err


def test_symlink_inside_the_set_is_refused(make_set, tmp_path, capsys):
    case = make_set()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (case.root / "inputs/link.json").symlink_to(outside)
    assert case.module.main([]) == 2
    assert "symlink" in capsys.readouterr().err
    assert not _batches(case)


def test_copy_of_the_set_does_not_replay_the_original(make_set, tmp_path, capsys):
    case = make_set()
    copy = tmp_path / "copy"
    shutil.copytree(case.root, copy)
    assert case.module.main(["--set", str(copy)]) == 2
    assert "outside" in capsys.readouterr().err
    assert not _batches(case)
    assert not (copy / "batches").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec["sources"][0].update(labels="/tmp/elsewhere-labels.jsonl"),
        lambda spec: spec["autoreview"].update(cache="inputs/../../cached"),
        lambda spec: spec["judge"].pop("verdicts"),
        lambda spec: spec["judge"].update(queue="/tmp/elsewhere-queue.jsonl"),
        lambda spec: spec.update(out_root="/tmp/elsewhere-batches"),
        lambda spec: spec.update(task_status="/tmp/elsewhere-status.json"),
    ],
)
def test_every_spec_path_must_resolve_inside_the_set(make_set, mutate, capsys):
    case = make_set()
    spec = json.loads((case.root / "spec.json").read_text())
    mutate(spec)
    _write(case.root / "spec.json", spec)
    seal(case)
    assert case.module.main([]) == 2
    assert not _batches(case)


def test_rollout_manifest_rows_must_resolve_inside_the_set(make_set):
    case = make_set()
    path = case.root / "inputs/corpora/manifest.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["path"] = "/tmp/elsewhere-run"
    _rows(path, rows)
    seal(case)
    assert case.module.main([]) == 2


def test_symlinked_input_directory_is_refused_even_though_it_resolves_elsewhere(
    make_set, tmp_path, capsys
):
    case = make_set()
    outside = tmp_path / "outside-cache"
    shutil.move(case.root / "inputs/cached", outside)
    (case.root / "inputs/cached").symlink_to(outside)
    assert case.module.main([]) == 2
    assert "escapes set" in capsys.readouterr().err


def _group(recall, alarms):
    return {
        "cheats": recall[1],
        "clean": alarms[1],
        "metrics": {
            "drop_or_review_recall": {"k": recall[0], "n": recall[1]},
            "clean_false_alarm_rate": {"k": alarms[0], "n": alarms[1]},
        },
    }


def test_summary_survives_a_corpus_missing_or_new(tmp_path, capsys):
    module = _load_script()
    batch = tmp_path / "batch"
    _write(
        batch / "scoreboard.json",
        {"by_corpus": {"b": _group((1, 2), (0, 2)), "n": _group((0, 0), (0, 0))}},
    )
    baseline = {
        "scoreboard": {"by_corpus": {"a": _group((1, 1), (0, 1)), "b": _group((2, 2), (0, 2))}}
    }
    result = {
        "batch": str(batch),
        "changed_runs": [],
        "regressions": [{"kind": "corpus_missing", "corpus": "a", "details": "gone"}],
    }
    module._summary(result, baseline, 1)
    out = capsys.readouterr().out
    assert "a: MISSING from this run" in out and "n: NEW in this run" in out
    assert "b: recall 2/2 (100.0%) -> 1/2 (50.0%)" in out
    assert "corpus_missing a: gone" in out


def test_summary_reports_unavailable_changes_and_zero_denominators(tmp_path, capsys):
    module = _load_script()
    batch = tmp_path / "batch"
    group = _group((0, 0), (0, 0))
    _write(batch / "scoreboard.json", {"by_corpus": {"a": group}})
    result = {
        "batch": str(batch),
        "changed_runs": None,
        "changed_runs_unavailable": "inputs differ",
    }
    module._summary(result, {"scoreboard": {"by_corpus": {"a": group}}}, 2)
    out = capsys.readouterr().out
    assert "Changed decisions: unavailable (inputs differ)" in out
    assert "0/0 (n/a) -> 0/0 (n/a)" in out


@pytest.mark.parametrize("status", [0, 1, 2])
@pytest.mark.parametrize("breakage", ["no-batch", "no-scoreboard", "bad-changes"])
def test_a_broken_summary_is_never_a_pass(make_set, monkeypatch, status, breakage, capsys):
    case = make_set()
    batch = case.root.parent.parent / "scratch-batch"
    batch.mkdir()
    result = {"batch": str(batch), "changed_runs": []}
    if breakage == "no-batch":
        result.pop("batch")
    elif breakage == "bad-changes":
        result["changed_runs"] = "many"
        _write(batch / "scoreboard.json", {"by_corpus": {}})
    monkeypatch.setattr(case.module, "replay", lambda *args: (result, status))
    code = case.module.main([])
    assert code == (status or 2)
    assert "summary unavailable" in capsys.readouterr().err


def test_default_location_and_env_override(monkeypatch, tmp_path, capsys):
    module = _load_script()
    monkeypatch.delenv("EVALLAB_LINE_REPLAY_SET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert module.main([]) == 77
    assert str(tmp_path / "Developer/eval-lab-results/line/replay-v1") in capsys.readouterr().err
    monkeypatch.setenv("EVALLAB_LINE_REPLAY_SET", str(tmp_path / "elsewhere"))
    assert module.main([]) == 77
    assert str(tmp_path / "elsewhere") in capsys.readouterr().err
