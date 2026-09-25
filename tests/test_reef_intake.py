"""Reef intake: gate episodes, captured traffic, pairing, and import."""

from __future__ import annotations

import json
from pathlib import Path

from evallab.evidence import reef_intake
from evallab.evidence.atif import SUPPORTED_SCHEMA_VERSIONS, _validate_fallback

FIXTURES = Path(__file__).resolve().parent / "fixtures/reef_intake"
STEPS = FIXTURES / "steps"


def _fixture_episodes() -> list[dict]:
    return reef_intake.iter_gate_episodes(STEPS)


def test_all_fixture_episodes_convert_and_validate() -> None:
    episodes = _fixture_episodes()
    assert len(episodes) == 4
    for entry in episodes:
        payload = reef_intake.parse_reef_gate_episode(
            entry["episode_dir"],
            scenario=entry["scenario"],
            step=entry["step"],
            run_label="fixture",
        )
        assert _validate_fallback(payload) is None
        assert payload["schema_version"] in SUPPORTED_SCHEMA_VERSIONS
        assert payload["extra"]["origin"] == "reef"
        reef_meta = payload["extra"]["reef"]
        assert reef_meta["side"] in ("candidate", "current")
        assert reef_meta["step"] == entry["step"]
        assert reef_meta["scenario"] == "aa-gate"
        assert payload["agent"]["name"] == reef_intake.REEF_AGENT_NAME


def test_tool_calls_pair_with_observations_in_one_step() -> None:
    episode_dir = STEPS / "aa-gate/1/episodes/candidate-0"
    payload = reef_intake.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=1)
    tool_step = next(step for step in payload["steps"] if step.get("tool_calls"))
    results = tool_step["observation"]["results"]
    call_ids = {call["tool_call_id"] for call in tool_step["tool_calls"]}
    assert {result["source_call_id"] for result in results} <= call_ids
    assert results[0]["content"] == "7"


def test_empty_reply_is_kept_as_evidence() -> None:
    episode_dir = STEPS / "aa-gate/1/episodes/current-0"
    payload = reef_intake.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=1)
    assert _validate_fallback(payload) is None
    agent_steps = [step for step in payload["steps"] if step["source"] == "agent"]
    assert agent_steps and all(isinstance(step["message"], str) for step in agent_steps)


def test_missing_source_fields_stay_missing(tmp_path: Path) -> None:
    episode_dir = tmp_path / "candidate-0"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps({"task": "[toy] Reply with 7."}), encoding="utf-8"
    )
    (episode_dir / "session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "seq": 0, "data": {"agent": "root"}}),
                json.dumps({"type": "turn/start", "seq": 1, "data": {"turn": 1}}),
                json.dumps({"type": "step/start", "seq": 2, "data": {"turn": 1, "step": 1}}),
                json.dumps(
                    {
                        "type": "assistant/message",
                        "seq": 3,
                        "data": {"step": 1, "content": "", "tool_calls": [], "finish": "stop"},
                    }
                ),
                json.dumps({"type": "step/end", "seq": 4, "data": {"turn": 1, "step": 1}}),
                json.dumps(
                    {"type": "turn/end", "seq": 5, "data": {"turn": 1, "reason": {"kind": "done"}}}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload = reef_intake.parse_reef_gate_episode(episode_dir, scenario="aa-gate", step=9)
    assert _validate_fallback(payload) is None
    reef_meta = payload["extra"]["reef"]
    for absent in (
        "episode_score",
        "failure",
        "path",
        "exit_code",
        "stdout",
        "stderr",
        "residue",
        "proposal_id",
        "release_id",
    ):
        assert absent not in reef_meta
    assert "model_name" not in payload["agent"]
    assert all("metrics" not in step for step in payload["steps"])
    assert all("model_name" not in step for step in payload["steps"])


def test_failed_episode_with_score_stays_unscored(tmp_path: Path) -> None:
    episode_dir = tmp_path / "current-0"
    episode_dir.mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "task": "[toy] Reply with 7.",
                "score": 1.0,
                "failure": {"kind": "timeout"},
                "path": {"stages": ["think"], "reason": "error"},
            }
        ),
        encoding="utf-8",
    )
    (episode_dir / "session.jsonl").write_text(
        json.dumps({"type": "session", "seq": 0, "data": {"agent": "root", "model": "m"}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant/message",
                "seq": 1,
                "data": {"step": 1, "content": "x", "tool_calls": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = reef_intake.parse_reef_gate_episode(episode_dir)
    assert _validate_fallback(payload) is None
    assert "episode_score" not in payload["extra"]["reef"]
    assert payload["extra"]["reef"]["failure"] == {"kind": "timeout"}
    assert reef_intake.episode_passed(1.0) is True
    assert reef_intake.episode_passed(None) is None


def test_unknown_episode_names_are_not_episodes() -> None:
    assert reef_intake.parse_episode_name("candidate-0") == {
        "side": "candidate",
        "task_index": 0,
        "repeat": 0,
    }
    assert reef_intake.parse_episode_name("current-2-3") == {
        "side": "current",
        "task_index": 2,
        "repeat": 3,
    }
    assert reef_intake.parse_episode_name("notes") is None
    assert reef_intake.parse_episode_name("candidate-x") is None


def test_import_writes_historical_layout_not_a_harbor_job(tmp_path: Path) -> None:
    out = tmp_path / "reef-fixture"
    summary = reef_intake.import_reef_gate_run(
        STEPS, out, run_label="fixture", results_path=FIXTURES / "results.jsonl"
    )
    assert summary["episodes_total"] == 4
    assert summary["episode_pass"] == 1
    assert summary["trials"] == 2
    assert summary["published"] == 1
    assert summary["wins_total"] == 1
    assert summary["losses_total"] == 0

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["evidence_kind"] == "historical"
    assert manifest["origin"] == "reef"
    assert len(manifest["trajectories"]) == 4
    for relative in manifest["trajectories"]:
        payload = json.loads((out / relative).read_text(encoding="utf-8"))
        assert _validate_fallback(payload) is None
        assert payload["extra"]["origin"] == "reef"

    pairs = json.loads((out / "pairs.json").read_text(encoding="utf-8"))
    assert [(pair["step"], pair["outcome"], pair["published"]) for pair in pairs] == [
        (1, "W", True),
        (2, "T", False),
    ]

    # Not a native Harbor job: no Harbor trial layout anywhere in the output.
    assert not list(out.rglob("result.json"))
    assert not list(out.rglob("trajectory.json"))
    assert (out / "intake.json").read_text(encoding="utf-8").find('"native_harbor_job": false') != -1


def test_module_cli_imports_fixture_corpus(tmp_path: Path, capsys) -> None:
    out = tmp_path / "cli-out"
    assert (
        reef_intake.main(
            [
                "--steps-root",
                str(STEPS),
                "--out",
                str(out),
                "--run-label",
                "fixture",
                "--results",
                str(FIXTURES / "results.jsonl"),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["episode_pass"] == 1
    assert (out / "summary.json").is_file()


RECORDS = FIXTURES / "records/records-sample.json"
DETAILS = FIXTURES / "records/record-details.json"


def _traffic_documents() -> list[tuple[str, dict]]:
    export = json.loads(RECORDS.read_text(encoding="utf-8"))
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    return reef_intake.parse_reef_traffic_export(export, details, run_label="fixture")


def test_traffic_reports_define_trajectories() -> None:
    documents = dict(_traffic_documents())
    assert sorted(documents) == ["r-1", "r-2"]
    scored = documents["r-1"]
    assert _validate_fallback(scored) is None
    assert scored["session_id"] == "inf-1"
    assert scored["trajectory_id"] == "inf-3"
    reef_meta = scored["extra"]["reef"]
    assert reef_meta["report_id"] == "r-1"
    assert reef_meta["reward"] == 1.0
    assert reef_meta["feedback"] == "fixture pass"
    assert [entry["agent_record_id"] for entry in reef_meta["records"]] == ["inf-1", "inf-2", "inf-3"]
    assert scored["extra"]["origin"] == "reef"
    assert scored["extra"]["transport"] == "reef-capture-records"


def test_traffic_tool_result_joins_its_call() -> None:
    documents = dict(_traffic_documents())
    steps = documents["r-1"]["steps"]
    observed = [
        (step["step_id"], result["source_call_id"], result["content"])
        for step in steps
        for result in step.get("observation", {}).get("results", [])
    ]
    assert observed == [(3, "call_f1", "42")]
    calls = [call for step in steps for call in step.get("tool_calls", [])]
    assert calls == [
        {"tool_call_id": "call_f1", "function_name": "lookup", "arguments": {"key": "v"}}
    ]


def test_traffic_records_stay_verbatim_and_unreferenced_is_skipped(tmp_path: Path) -> None:
    documents = dict(_traffic_documents())
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    retained = documents["r-1"]["extra"]["reef"]["records"]
    assert retained[0]["payload"] == details["inf-1"]["payload"]
    out = tmp_path / "traffic-fixture"
    summary = reef_intake.import_reef_traffic_run(
        RECORDS, DETAILS, out, run_label="fixture", scenario="fixture-smoke"
    )
    assert summary["trajectories_total"] == 2
    assert summary["trajectories_scored"] == 1
    assert summary["trajectories_unscored"] == 1
    assert summary["unreferenced_inference_skipped"] == ["inf-x"]
    assert summary["scenario"] == "fixture-smoke"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["evidence_kind"] == "historical"
    assert manifest["kind"] == "captured-traffic"
    assert not list(out.rglob("result.json"))


def test_traffic_missing_score_stays_unscored() -> None:
    documents = dict(_traffic_documents())
    reef_meta = documents["r-2"]["extra"]["reef"]
    assert "reward" not in reef_meta
    assert "feedback" not in reef_meta
    assert _validate_fallback(documents["r-2"]) is None


def test_traffic_dangling_reference_raises() -> None:
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    details["r-bad"] = {
        "agent_record_id": "r-bad",
        "request_type": "report",
        "references": ["inf-absent"],
        "score": 0.0,
        "payload": {"references": ["inf-absent"], "score": 0.0},
    }
    export = json.loads(RECORDS.read_text(encoding="utf-8"))
    export["pages"][0]["records"].append(
        {"agent_record_id": "r-bad", "request_type": "report", "references": ["inf-absent"]}
    )
    try:
        reef_intake.parse_reef_traffic_export(export, details)
    except reef_intake.ReefGateError as exc:
        assert "inf-absent" in str(exc)
    else:
        raise AssertionError("dangling reference must fail loudly")


def test_traffic_inference_without_payload_raises() -> None:
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    del details["inf-2"]["payload"]
    export = json.loads(RECORDS.read_text(encoding="utf-8"))
    try:
        reef_intake.parse_reef_traffic_export(export, details)
    except reef_intake.ReefGateError as exc:
        assert "inf-2" in str(exc)
    else:
        raise AssertionError("a payload-less inference record must fail loudly")


def test_module_cli_imports_traffic_corpus(tmp_path: Path, capsys) -> None:
    out = tmp_path / "traffic-cli-out"
    assert (
        reef_intake.main(
            [
                "--records",
                str(RECORDS),
                "--record-details",
                str(DETAILS),
                "--scenario",
                "fixture-smoke",
                "--out",
                str(out),
                "--run-label",
                "fixture",
                "--reef-format-ref",
                "818997d7",
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["trajectories_total"] == 2
    assert printed["reef_format_ref"] == "818997d7"
    assert (out / "summary.json").is_file()


MULTI = Path(__file__).resolve().parent / "fixtures/reef_intake/multi"


def test_multi_scenario_steps_keep_their_own_files_and_metadata(tmp_path: Path) -> None:
    """Two scenarios sharing step 1 must not overwrite each other's trajectories."""
    out = tmp_path / "multi-out"
    summary = reef_intake.import_reef_gate_run(
        MULTI / "steps", out, run_label="fixture-multi", results_path=MULTI / "results.jsonl"
    )
    assert summary["episodes_total"] == 4
    assert summary["scenarios"] == ["alpha", "beta"]
    assert summary["trials"] == 2
    assert summary["published"] == 1
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["trajectories"]) == 4
    assert len({path for path in manifest["trajectories"]}) == 4
    by_path = {
        relative: json.loads((out / relative).read_text(encoding="utf-8"))
        for relative in manifest["trajectories"]
    }
    assert by_path["trajectories/alpha/1/candidate-0.json"]["extra"]["reef"]["release_id"] == "rel-a"
    assert by_path["trajectories/beta/1/candidate-0.json"]["extra"]["reef"]["release_id"] == "rel-b"
    pairs = {(pair["scenario"], pair["outcome"], pair["published"]) for pair in json.loads(
        (out / "pairs.json").read_text(encoding="utf-8")
    )}
    assert pairs == {("alpha", "W", True), ("beta", "T", False)}


def test_ambiguous_trial_rows_refuse_rather_than_misattribute(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        '{"trial": 1, "scenario": "alpha", "published": true}\n'
        '{"trial": 1, "scenario": "alpha", "published": false}\n',
        encoding="utf-8",
    )
    try:
        reef_intake.import_reef_gate_run(
            MULTI / "steps", tmp_path / "out", run_label="x", results_path=results
        )
    except reef_intake.ReefGateError as exc:
        assert "alpha" in str(exc)
    else:
        raise AssertionError("ambiguous trial rows must fail loudly")


def test_trial_join_prefers_scenario_then_scenario_only_then_trial() -> None:
    rows = [
        {"trial": 7, "scenario": "beta", "release_id": "rel-single"},
        {"trial": 1, "release_id": "rel-legacy"},
    ]
    assert reef_intake.match_trial_row(rows, "beta", 1) == rows[0]
    assert reef_intake.match_trial_row(rows, "alpha", 1) == rows[1]
    assert reef_intake.match_trial_row(rows, "gamma", 9) == {}


def test_repeated_scenario_rows_require_an_exact_trial_match() -> None:
    rows = [
        {"trial": 5, "scenario": "s", "release_id": "rel-5"},
        {"trial": 6, "scenario": "s", "release_id": "rel-6"},
    ]
    assert reef_intake.match_trial_row(rows, "s", 5) == rows[0]
    try:
        reef_intake.match_trial_row(rows, "s", 99)
    except reef_intake.ReefGateError as exc:
        assert "exact trial" in str(exc)
    else:
        raise AssertionError("repeated-scenario rows without an exact trial must fail loudly")


def test_torn_results_line_fails_with_file_and_line(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        '{"trial": 1, "published": true}\n{"trial": 2, "published": false}\nnot json {\n',
        encoding="utf-8",
    )
    try:
        reef_intake.load_trial_results(results)
    except reef_intake.ReefGateError as exc:
        assert "line 3" in str(exc)
        assert "results.jsonl" in str(exc)
    else:
        raise AssertionError("a torn results line must fail loudly")


def test_pair_totals_agreement_includes_ties(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        '{"trial": 1, "scenario": "alpha", "published": true, "wins": 1, "losses": 0, "ties": 99}\n'
        '{"trial": 1, "scenario": "beta", "published": false, "wins": 0, "losses": 0, "ties": 99}\n',
        encoding="utf-8",
    )
    summary = reef_intake.import_reef_gate_run(
        MULTI / "steps", tmp_path / "out", run_label="x", results_path=results
    )
    assert summary["recorded_pair_totals_agree"] is False


def _traffic_fixture_with_report(report_id: str) -> tuple[dict, dict]:
    export = json.loads(RECORDS.read_text(encoding="utf-8"))
    details = json.loads(DETAILS.read_text(encoding="utf-8"))
    details[report_id] = {
        "agent_record_id": report_id,
        "request_type": "report",
        "references": ["inf-1"],
        "score": 0.0,
        "payload": {"references": ["inf-1"], "score": 0.0},
    }
    export["pages"][0]["records"].append({"agent_record_id": report_id, "request_type": "report"})
    return export, details


def test_malicious_report_ids_never_become_paths(tmp_path: Path) -> None:
    for report_id in ("../escape-pwn", "../../escape-pwn2", "/absolute-pwn", "sub/dir-pwn"):
        export, details = _traffic_fixture_with_report(report_id)
        try:
            reef_intake.parse_reef_traffic_export(export, details)
        except reef_intake.ReefGateError as exc:
            assert "output filename" in str(exc)
        else:
            raise AssertionError(f"report id {report_id!r} must fail loudly")
    export, details = _traffic_fixture_with_report("../../escape-pwn2")
    try:
        reef_intake.import_reef_traffic_run(
            _write_json(tmp_path / "records.json", export),
            _write_json(tmp_path / "details.json", details),
            tmp_path / "out",
            run_label="x",
        )
    except reef_intake.ReefGateError:
        pass
    else:
        raise AssertionError("a path-escaping import must fail loudly")
    assert not (tmp_path / "escape-pwn2").exists()
    assert not (tmp_path / "out").exists()


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
