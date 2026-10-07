from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path

import pytest

from evallab.terminal_wrench import (
    audit_manifest,
    build_parser,
    build_result,
    fetch_trials,
    main,
    materialize_trial,
    observation_stats,
    prepare_trajectory,
    subset_sample,
    trajectory_cache_path,
    trajectory_url,
    validate_result,
    validate_trajectory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "terminal_wrench" / "trajectory.json"


def _native():
    return json.loads(FIX.read_text())


def _row(kind="hack"):
    return {
        "id": "tw-0001",
        "task_id": "618",
        "model": "gpt-5.4",
        "label": "v5_4",
        "kind": kind,
        "tw_key": f"{'hacks' if kind == 'hack' else 'clean'}__618__gpt-5.4__v5_4",
    }


def test_native_atif_preserves_terminal_observations_and_tool_arguments():
    native = _native()
    before = copy.deepcopy(native)
    doc = prepare_trajectory(native, "tw-0001")
    assert native == before
    assert doc["schema_version"] == "ATIF-v1.6"
    assert doc["trajectory_id"] == doc["session_id"] == "tw-0001"
    assert doc["steps"] == native["steps"]
    first, terminal = doc["steps"][1:]
    assert len(first["tool_calls"]) == 2
    assert first["tool_calls"][0]["arguments"]["duration"] == 0.1
    assert len(first["observation"]["results"]) == 1
    assert "source_call_id" not in first["observation"]["results"][0]
    assert terminal["observation"]["results"][0]["content"] == "Task marked complete."
    assert observation_stats(doc) == {
        "agent_steps": 2,
        "observed_steps": 2,
        "observation_coverage": 1.0,
    }
    assert validate_trajectory(doc) == []


@pytest.mark.parametrize("version", ["ATIF-v1.5", "ATIF-v1.6"])
def test_native_empty_stripped_messages_are_valid(version):
    doc = _native()
    doc["schema_version"] = version
    doc["steps"][0]["message"] = ""
    doc["steps"][1]["message"] = ""
    assert validate_trajectory(doc) == []


def test_trajectory_and_result_validate():
    result = build_result(
        task_id="fixture-task",
        trial_name="tw-0001",
        trial_dir="/tmp/x/tw-0001",
        model="fixture-model",
    )
    assert result["verifier_result"]["rewards"]["reward"] == 1.0
    assert result["agent_info"]["model_info"]["name"] == "fixture-model"
    assert validate_result(result) == []


@pytest.mark.parametrize(
    "variant,tree",
    [
        ("sanitized", "sanitized_trajectories"),
        ("stripped", "stripped_trajectories"),
        ("raw", "hack_trajectories"),
    ],
)
def test_variant_routing_and_separate_cache(variant, tree, tmp_path):
    row = _row()
    assert f"/{tree}/v5_4/" in trajectory_url(row, variant)
    assert trajectory_cache_path(row, tmp_path, variant) == tmp_path / variant / "tw-0001.json"
    clean = _row("clean")
    assert "/baseline_trajectories/v5_4/" in trajectory_url(clean, variant)
    assert trajectory_cache_path(clean, tmp_path, variant) == tmp_path / "baseline/tw-0001.json"


def test_missing_sanitized_source_never_falls_back(monkeypatch, tmp_path):
    urls = []

    def missing(request, **kwargs):
        urls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", missing)
    stats = fetch_trials([_row()], tmp_path)
    assert stats["404"] == 1
    assert stats["files"] == 1
    assert stats["failures"][0]["id"] == "tw-0001"
    assert urls == [trajectory_url(_row())]
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("command", ["fetch", "materialize"])
def test_cli_defaults_to_sanitized(command):
    argv = [command, "--subset", "subset.json", "--cache-dir", "cache"]
    if command == "materialize":
        argv += ["--out-tw", "tw", "--out-manifest", "manifest.jsonl", "--sample-dir", "index"]
    assert build_parser().parse_args(argv).variant == "sanitized"


def _synthetic_sample():
    hacks = []
    fams = ["alpha", "beta", "gamma"]
    for i in range(30):
        hacks.append(
            {
                "task_id": f"t{i % 10}",
                "model": "m",
                "label": f"v{i}",
                "episodes": 3,
                "categories": [fams[i % 3], fams[(i + 1) % 3]],
                "path": f"tasks/t{i % 10}/m/hack_trajectories/v{i}/trial/agent",
            }
        )
    clean = [
        {
            "task_id": f"c{i}",
            "model": ["m1", "m2"][i % 2],
            "label": str(i),
            "episodes": 2,
            "path": "x",
        }
        for i in range(20)
    ]
    return {"hacks": hacks, "clean": clean}


def test_subset_is_deterministic_stratified_and_capped():
    s = _synthetic_sample()
    h1, c1 = subset_sample(s, seed=20261006, n_hack=12, n_clean=6, floor=3)
    h2, c2 = subset_sample(s, seed=20261006, n_hack=12, n_clean=6, floor=3)
    assert [h["tw_key"] for h in h1] == [h["tw_key"] for h in h2]
    assert [c["tw_key"] for c in c1] == [c["tw_key"] for c in c2]
    assert len(h1) == 12 and len(c1) == 6
    from collections import Counter

    have = Counter(c for h in h1 for c in h["categories"])
    assert all(v >= 3 for v in have.values()), dict(have)
    tm = Counter((h["task_id"], h["model"]) for h in h1)
    assert max(tm.values()) <= 2
    models = Counter(c["model"] for c in c1)
    assert set(models) == {"m1", "m2"}


def test_materialize_writes_native_instruction_without_label_metadata(tmp_path):
    native = _native()
    native["steps"][1]["observation"]["results"][0]["content"] = "x" * 65001
    dest = tmp_path / "tw-0001"
    st = materialize_trial(
        dest=dest,
        task_id="fixture-task",
        trial_name="tw-0001",
        model="fixture-model",
        native_trajectory=native,
    )
    assert st["agent_steps"] == st["observed_steps"] == 2
    for rel in (
        "result.json",
        "trial.log",
        "instruction.md",
        "agent/trajectory.json",
        "verifier/test-stdout.txt",
    ):
        assert (dest / rel).exists(), rel
    doc = json.loads((dest / "agent/trajectory.json").read_text())
    assert doc["trajectory_id"] == doc["session_id"] == "tw-0001"
    assert doc["steps"] == native["steps"]
    assert native["steps"][0]["message"] in (dest / "instruction.md").read_text()
    assert "65001" not in (dest / "trial.log").read_text()
    blob = "\n".join(p.read_text() for p in dest.rglob("*") if p.is_file())
    for word in (
        "clean_baseline",
        "binary-hijacking",
        "output-spoofing",
        "hacks__",
        "clean__",
        "sanitized_trajectories",
        "Red Team",
    ):
        assert word not in blob, word


def test_leakage_audit_is_case_insensitive_and_excludes_observations(tmp_path):
    rows = []
    for label, user, agent in (
        ("hack", "RED-TEAM; ExPloit the Verifier.", "BYPASS, hacking, and CHEAT."),
        ("clean", "Build output.", "Read the source."),
    ):
        doc = _native()
        doc["steps"][0]["message"] = user
        doc["steps"][1]["message"] = agent
        doc["steps"][1]["observation"]["results"][0]["content"] = "red team exploit hack"
        dest = tmp_path / label
        (dest / "agent").mkdir(parents=True)
        (dest / "agent/trajectory.json").write_text(json.dumps(doc))
        rows.append({"id": label, "source": "tw", "label": label, "trial_dir": str(dest)})
    report = audit_manifest(rows)
    hack, clean = report["by_label"]["hack"], report["by_label"]["clean"]
    assert hack["any_word"] == hack["user_any_word"] == hack["agent_any_word"] == 1
    assert all(count == 1 for count in hack["words"].values())
    assert clean["any_word"] == 0
    assert clean["observation_coverage"] == 1.0


def test_materialize_preserves_manifest_ids_labels_and_ours(tmp_path):
    subset = {
        "hacks": [{**_row(), "family": "copy", "families": ["copy", "output"], "episodes": 2}],
        "clean": [],
    }
    subset_path = tmp_path / "subset.json"
    subset_path.write_text(json.dumps(subset))
    manifest = [
        {
            "id": "tw-0077",
            "source": "tw",
            "label": "hack",
            "family": "copy",
            "families": ["copy", "output"],
            "tw_key": _row()["tw_key"],
            "trial_dir": "old",
            "episodes": 2,
            "n_agent_steps": 1,
            "reward": 1.0,
        },
        {"id": "ours-real", "source": "ours", "label": "clean", "trial_dir": "/read-only"},
    ]
    manifest_path = tmp_path / "evalset.jsonl"
    manifest_path.write_text("".join(json.dumps(row) + "\n" for row in manifest))
    cache = tmp_path / "cache/sanitized"
    cache.mkdir(parents=True)
    (cache / "tw-0077.json").write_text(json.dumps(_native()))
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "trajectories.json").write_text(
        json.dumps(
            [{"task_id": "618", "model": "gpt-5.4", "trajectory_label": "v5_4", "reward": 1.0}]
        )
    )
    (sample_dir / "tasks.json").write_text("[]")
    assert (
        main(
            [
                "materialize",
                "--subset",
                str(subset_path),
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache.parent),
                "--sample-dir",
                str(sample_dir),
                "--out-tw",
                str(tmp_path / "tw"),
                "--out-manifest",
                str(manifest_path),
            ]
        )
        == 0
    )
    out = [json.loads(line) for line in manifest_path.read_text().splitlines()]
    assert out[1] == manifest[1]
    assert out[0] == {
        **manifest[0],
        "trial_dir": str(tmp_path / "tw/tw-0077"),
        "tw_variant": "sanitized",
        "n_agent_steps": 2,
    }
    native = json.loads((tmp_path / "tw/tw-0077/agent/trajectory.json").read_text())
    assert native["trajectory_id"] == "tw-0077"
