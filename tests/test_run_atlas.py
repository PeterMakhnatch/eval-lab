"""Run projection, blinding, and redaction contracts with synthetic traces."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from evallab.production import run_atlas as atlas

SOURCE_NAMES = ("goodfire", "CheatBench", "ImpossibleBench", "hackbench")
HIDDEN_VALUES = ("PRIVATE_LABEL_POSITIVE", "PRIVATE_ROOT_METADATA", "PRIVATE_STEP_EXTRA")


@pytest.fixture
def atlas_run() -> atlas.AtlasRun:
    run_id = "goodfire-reward-hack-data/private-campaign/private-rollout"
    return atlas.AtlasRun(
        run_id=run_id, source="goodfire-reward-hack-data", split="DEV", outcome="fail",
        doc={
            "schema_version": "ATIF-v1.6",
            "metadata": {
                "run_id": run_id, "split": "DEV", "outcome": "fail",
                "label": HIDDEN_VALUES[0], "private": HIDDEN_VALUES[1],
            },
            "extra": {"cheatbench": {"label": HIDDEN_VALUES[0]}},
            "steps": [
                {
                    "step_id": 10, "source": "user",
                    "message": "Inspect " + " / ".join(SOURCE_NAMES) + " and fix the parser.",
                    "extra": {"label": HIDDEN_VALUES[2]},
                },
                {
                    "step_id": 11, "source": "user",
                    "message": "  [CheatBench annotation: " + HIDDEN_VALUES[0] + "]",
                },
                {
                    "step_id": 40, "source": "agent",
                    "message": "I will inspect the parser and check the change.",
                    "reasoning_content": "Inspect the parser before editing it.",
                    "tool_calls": [{
                        "tool_call_id": "call-1", "function_name": "shell",
                        "arguments": {"command": "cat parser.py"},
                    }],
                    "observation": {"results": [{"content": "def parse(value): ..."}]},
                    "extra": {"label": HIDDEN_VALUES[2]},
                },
                {
                    "step_id": 95, "source": "agent", "message": "I changed the parser.",
                    "extra": {"label": HIDDEN_VALUES[2]},
                },
            ],
        },
    )


@pytest.mark.parametrize("source_name", [
    "swebench-verified", "SWE-bench", "swe_rebench", "terminal-bench", "Terminal Bench",
    "BountyBench", "deepswe", "cybench", "SimGlot", "mimo-defect-map-v1", "tb4-audit-v1",
    "tb2-to-tb21-v1", "qa-trace-sft-v0", "kimi-tb4-dev-swarm-20261026", "hacktrace-act-v1",
])
def test_default_rendering_redacts_benchmark_and_dataset_names(source_name: str) -> None:
    text = f"Task from {source_name} | Inspect {source_name} | read {source_name} | Output from {source_name}"
    rendered = atlas.redact_sources(text)
    assert source_name.casefold() not in rendered.casefold()
    assert rendered.count("[source]") == 4


@pytest.mark.parametrize("filename", [
    "mimo_build_env.tar.gz.b64", "mimo_test_command.sh", "MIMO_build_env.tar.gz.b64",
])
def test_bare_mimo_token_in_task_filenames_is_redacted(filename: str) -> None:
    rendered = atlas.redact_sources(f"Extract {filename}; tar xf {filename}; {filename}: OK")
    assert "mimo" not in rendered.casefold()
    assert rendered.count("[source]") == 3
    # The longer dataset name still wins, so no fragment of it survives as a hint.
    assert atlas.redact_sources("mimo-defect-map-v1") == "[source]-v1"
    # A word that only starts with those letters is left alone.
    assert atlas.redact_sources("mimosas and mimicry") == "mimosas and mimicry"


def test_source_pattern_redacts_extra_ids_literally_and_before_shorter_source_names() -> None:
    pattern = atlas.source_pattern(["openai-swe-bench-annotations-2024", "my.dataset+v1"])
    text = (
        "openai-swe-bench-annotations-2024 | my.dataset+v1 | "
        "myXdatasettv1 | my.datasetv1 | CheatBench"
    )
    assert atlas.redact_sources(text, pattern) == (
        "[source] | [source] | myXdatasettv1 | my.datasetv1 | [source]"
    )


def test_split_provenance_redaction_preserves_device_paths_and_development_text() -> None:
    text = "DEV benchmark task; /dev/null; dev server; DEVICE"
    expected = "[split] benchmark task; /dev/null; dev server; DEVICE"
    assert atlas.redact_sources(text) == expected


def test_blind_run_removes_annotations_metadata_and_step_extras(atlas_run: atlas.AtlasRun) -> None:
    original = copy.deepcopy(atlas_run.doc)
    blind = atlas.blind_run(atlas_run)
    assert blind.origins == [10, 40, 95]
    assert [step["step_id"] for step in blind.steps] == [0, 1, 2]
    assert all("extra" not in step for step in blind.steps)
    rendered = atlas.redact_sources(json.dumps(blind.steps))
    assert "[CheatBench annotation" not in rendered
    for private in (atlas_run.run_id, *HIDDEN_VALUES):
        assert private not in rendered
    assert atlas_run.doc == original
    assert rendered.count("[source]") == len(SOURCE_NAMES)
    assert all(name.casefold() not in rendered.casefold() for name in SOURCE_NAMES)


def test_cloud_doc_uses_native_ids_and_attaches_tools_without_publisher_flags(tmp_path: Path) -> None:
    forbidden = ("source_judge_flags", "source_passages", "passage_labels", "passage_positive")
    flags = {key: f"PRIVATE_{key}_VALUE" for key in forbidden}
    def call(call_id):
        return json.dumps([{"id": call_id, "name": "shell", "arguments": "inspect"}])
    rows = [
        ("trace-private", 31, "tool", "shell", None, "late first output", "unused preview", None,
         json.dumps(flags | {"tool_call_id": "first"})),
        ("trace-private", 7, "user", None, None, "Fix the parser.", None, None, json.dumps(flags)),
        ("trace-private", 12, "assistant", "shell", call("first"), "First action", "first preview", None,
         json.dumps(flags | {"reasoning_content": "Keep this private chain of reasoning intact."})),
        ("trace-private", 30, "assistant", "shell", call("second"), "Second action", None, None,
         json.dumps(flags)),
        ("trace-private", 32, "tool", "shell", None, "", "second output", None,
         json.dumps(flags | {"tool_call_id": "second"})),
        ("other-trace", 99, "assistant", None, None, "OTHER_TRACE_PRIVATE", None, None, "{}"),
    ]
    with closing(sqlite3.connect(tmp_path / "traces.sqlite")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE trace_steps (
                trace_id TEXT NOT NULL, step_index INTEGER NOT NULL CHECK(step_index >= 0),
                role TEXT NOT NULL, tool_name TEXT, command_or_action TEXT, content_text TEXT,
                observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT NOT NULL,
                PRIMARY KEY(trace_id, step_index)
            )
        """)
        connection.executemany("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        doc = atlas.cloud_doc(connection, "trace-private")
    assert [step["step_id"] for step in doc["steps"]] == [7, 12, 30]
    first, second = doc["steps"][1:]
    assert first["tool_calls"][0]["tool_call_id"] == "first"
    assert second["tool_calls"][0]["tool_call_id"] == "second"
    assert first["observation"]["results"] == [
        {"content": "first preview"}, {"content": "late first output"},
    ]
    assert second["observation"]["results"] == [{"content": "second output"}]
    reasoning = "Keep this private chain of reasoning intact."
    assert first["reasoning_content"] == reasoning
    blind = atlas.blind_run(atlas.AtlasRun("trace-private", "synthetic", "DEV", "unknown", doc))
    assert blind.origins == [7, 12, 30]
    rendered = atlas.redact_sources(json.dumps(blind.steps))
    assert reasoning in rendered
    for text in (json.dumps(doc), rendered):
        assert "trace-private" not in text and "OTHER_TRACE_PRIVATE" not in text
        for key, value in flags.items():
            assert key not in text and value not in text


def test_cloud_doc_tool_arguments_are_atif_objects_for_monitor_consumers(tmp_path: Path) -> None:
    from evallab.qa import trace_cheap

    native = json.dumps([{"id": "c1", "name": "bash", "arguments": json.dumps({"command": "ls -la"})}])
    rows = [
        ("t", 0, "assistant", "bash", native, "Look around", None, None, "{}"),
        ("t", 1, "system", "retained-journal", "task_quality.started", "event", None, None, "{}"),
    ]
    with closing(sqlite3.connect(tmp_path / "traces.sqlite")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE trace_steps (
                trace_id TEXT NOT NULL, step_index INTEGER NOT NULL, role TEXT NOT NULL,
                tool_name TEXT, command_or_action TEXT, content_text TEXT,
                observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT NOT NULL
            )
        """)
        connection.executemany("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        doc = atlas.cloud_doc(connection, "t")
    agent, system = doc["steps"]
    assert agent["tool_calls"][0]["arguments"] == {"command": "ls -la"}
    assert system["tool_calls"][0]["arguments"] == {"raw": "task_quality.started"}
    # The deterministic monitor rules read arguments as mappings (rule replay crashed on strings).
    assert "ls -la" in json.dumps(trace_cheap.actions_only([agent]))


def test_cloud_doc_drops_off_path_assistant_and_tool_rows_without_renumbering(
    tmp_path: Path,
) -> None:
    off_path_texts = (
        "OFF_PATH_AGENT_MESSAGE", "OFF_PATH_REASONING", "OFF_PATH_CALL_ARGUMENTS",
        "OFF_PATH_PREVIEW", "OFF_PATH_TOOL_RESULT", "OFF_PATH_TOOL_PREVIEW",
    )
    rows = [
        ("branching-trace", 3, "user", None, None, "Fix the parser.", None, None, "{}"),
        (
            "branching-trace", 9, "assistant", "shell",
            json.dumps([{"id": "kept-call", "name": "shell", "arguments": "inspect parser"}]),
            "Kept first action", None, None, json.dumps({"on_final_parent_path": True}),
        ),
        (
            "branching-trace", 20, "assistant", "shell",
            json.dumps([{
                "id": "off-path-call", "name": "shell", "arguments": off_path_texts[2],
            }]),
            off_path_texts[0], off_path_texts[3], None,
            json.dumps({"on_final_parent_path": False, "reasoning_content": off_path_texts[1]}),
        ),
        (
            "branching-trace", 21, "tool", "shell", None,
            off_path_texts[4], off_path_texts[5], None,
            json.dumps({"on_final_parent_path": False, "tool_call_id": "off-path-call"}),
        ),
        (
            "branching-trace", 22, "tool", "shell", None, "Kept tool result", None, None,
            json.dumps({"on_final_parent_path": True, "tool_call_id": "kept-call"}),
        ),
        (
            "branching-trace", 50, "assistant", None, None, "Kept final action", None, None,
            json.dumps({"on_final_parent_path": True}),
        ),
    ]
    with closing(sqlite3.connect(tmp_path / "branching.sqlite")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE trace_steps (
                trace_id TEXT NOT NULL, step_index INTEGER NOT NULL CHECK(step_index >= 0),
                role TEXT NOT NULL, tool_name TEXT, command_or_action TEXT, content_text TEXT,
                observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT NOT NULL,
                PRIMARY KEY(trace_id, step_index)
            )
        """)
        connection.executemany("INSERT INTO trace_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        doc = atlas.cloud_doc(connection, "branching-trace")
    assert [step["step_id"] for step in doc["steps"]] == [3, 9, 50]
    assert doc["steps"][1]["observation"]["results"] == [{"content": "Kept tool result"}]
    blind = atlas.blind_run(atlas.AtlasRun("branching-trace", "synthetic", "DEV", "unknown", doc))
    assert blind.origins == [3, 9, 50]
    rendered = atlas.redact_sources(json.dumps(blind.steps))
    for text in (json.dumps(doc), rendered):
        assert "Kept first action" in text and "Kept final action" in text
        assert "Kept tool result" in text
        assert "off-path-call" not in text
        assert all(off_path not in text for off_path in off_path_texts)


def test_source_redaction_opus_preserves_audio_extension_and_codecs() -> None:
    """Opus model references are redacted while audio extensions and codec tokens stay 100% intact."""
    assert atlas.redact_sources("claude-opus-5-5") == "[source]"
    assert atlas.redact_sources("Opus 4.6") == "[source]"
    assert atlas.redact_sources("claude-opus") == "[source]"
    assert atlas.redact_sources("opus-4") == "[source]"
    assert atlas.redact_sources("opus-5") == "[source]"
    assert atlas.redact_sources("standalone Opus model") == "standalone [source] model"

    assert atlas.redact_sources("ffmpeg -i input.opus out.wav") == "ffmpeg -i input.opus out.wav"
    assert atlas.redact_sources("*.opus") == "*.opus"
    assert atlas.redact_sources("audio.opus") == "audio.opus"
    assert atlas.redact_sources("sample.opus") == "sample.opus"
    assert atlas.redact_sources("track_01.opus") == "track_01.opus"
    assert atlas.redact_sources(".opus'") == ".opus'"
    assert atlas.redact_sources("libopus") == "libopus"
    assert atlas.redact_sources("opusenc input.wav out.opus") == "opusenc input.wav out.opus"
    assert atlas.redact_sources("opusdec in.opus out.wav") == "opusdec in.opus out.wav"


