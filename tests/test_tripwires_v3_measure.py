"""Behavioral tests for the tripwires v3 measurement runner (denominators, firewall, freeze)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab import tripwires

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "research/experiments/tripwires-v3-hackbook-20261021/run.py"
_spec = importlib.util.spec_from_file_location("tripwires_v3_measure_run", RUN)
assert _spec and _spec.loader
run = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run
_spec.loader.exec_module(run)

REGISTRY = (
    {
        "id": "hb_verdict",
        "entry_ids": ["e1"],
        "source_links": ["https://example.test/e1"],
        "signature": "synthetic",
        "scope": "offline",
        "eligibility": "verdict",
    },
    {
        "id": "hb_diag",
        "entry_ids": ["e2"],
        "source_links": ["https://example.test/e2"],
        "signature": "synthetic",
        "scope": "causal",
        "eligibility": "diagnostic",
    },
)


def report(hackbook: list[dict[str, Any]] | None = None, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {name: [] for name in run.FLAG_RULES}
    out["content_rules"] = dict.fromkeys(tripwires.CONTENT_RULES)
    out["hackbook"] = hackbook if hackbook is not None else []
    out.update(over)
    return out


def hit(rule: str, step: int | None, evidence: str = "ev") -> dict[str, Any]:
    return {
        "rule": rule,
        "entry_ids": ["x"],
        "source_links": ["u"],
        "step": step,
        "evidence": evidence,
    }


# ----------------------------------------------------------------- report -> fired rules


def test_missing_hackbook_section_and_unregistered_rule_are_errors() -> None:
    no_section = report()
    del no_section["hackbook"]
    with pytest.raises(ValueError, match="no `hackbook` section"):
        run.hits_from_report(no_section, REGISTRY)
    with pytest.raises(ValueError, match="unregistered rule"):
        run.hits_from_report(report(hackbook=[hit("made_up", 1)]), REGISTRY)


# --------------------------------------------------------------- synthetic published data


def _write_traj(root: Path, rel: str, agent_steps: int = 2, shell: str = "ls") -> str:
    steps: list[dict[str, Any]] = [{"step_id": 1, "source": "user", "message": "task"}]
    steps += [
        {
            "step_id": i + 2,
            "source": "agent",
            "message": "",
            "tool_calls": [{"function_name": "bash", "arguments": {"keystrokes": shell}}],
        }
        for i in range(agent_steps)
    ]
    path = root / rel / "agent/trajectory.json"
    path.parent.mkdir(parents=True)
    data = json.dumps({"steps": steps}).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _item(
    root: Path, name: str, label: str, *, split: str = "test", task: str = "bench/t", **traj: Any
) -> dict[str, Any]:
    full = _write_traj(root, f"pub/{name}/full", **traj)
    actions = _write_traj(root, f"pub/{name}/actions", **traj)
    return {
        "schema": "qa.answer_key/v1",
        "item_id": name,
        "key_id": "k",
        "subject": {"kind": "trace", "task_id": task, "trial_id": name},
        "expected_verdict": "pass" if label in ("real_pass", "clean_pass") else "fail",
        "expected_category": None if label in ("real_pass", "clean_pass") else "weak",
        "label_source": "synthetic",
        "split": split,
        "source_label": label,
        "trial_ref": {"kind": "published", "path": f"pub/{name}/full"},
        "actions_only_ref": {"kind": "published", "path": f"pub/{name}/actions"},
        "trajectory_sha256": {"full": full, "actions": actions},
    }


def _key(
    keys_dir: Path, cohort: str, items: list[dict[str, Any]], coverage: list[dict] = ()
) -> None:
    directory = keys_dir / cohort
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("synthetic\n")
    (directory / "items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
    if coverage:
        (directory / "coverage.jsonl").write_text("".join(json.dumps(c) + "\n" for c in coverage))


def scan_by_command(
    agent_steps: list[dict[str, Any]], steps: list[dict[str, Any]]
) -> dict[str, Any]:
    """Stand-in for the shared scan: `evil` fires the verdict rule, `diag` only the diagnostic."""
    command = agent_steps[0]["tool_calls"][0]["arguments"]["keystrokes"]
    hits = {"evil": [hit("hb_verdict", agent_steps[0]["step_id"])], "diag": [hit("hb_diag", 2)]}
    return report(hackbook=hits.get(command, []))


# ----------------------------------------------------------------------- denominators


def test_denominators_exclude_unknown_and_other_and_never_default_to_zero(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    items = [
        _item(root, "p_hit", "wrong_pass", shell="evil"),
        _item(root, "p_miss", "wrong_pass"),
        _item(root, "n_diag", "real_pass", shell="diag"),
        _item(root, "n_clean", "real_pass"),
        _item(root, "n_alarm", "real_pass", shell="evil"),
        _item(root, "other", "not_a_pass", shell="evil"),
        _item(root, "empty", "wrong_pass", agent_steps=0),
        _item(root, "weird", "mystery"),
        _item(root, "dev_only", "wrong_pass", split="dev"),
    ]
    items.append(
        {**_item(root, "gone", "real_pass"), "trial_ref": {"kind": "published", "path": "nope"}}
    )
    mismatch = _item(root, "tampered", "real_pass")
    mismatch["trajectory_sha256"]["actions"] = "0" * 64
    items.append(mismatch)
    _key(keys, "mimo-fresh-window3", items, coverage=[{"reason": "unmeasured"}] * 3)

    result, records = run.measure_cohort(
        run.cohort_by_id("mimo-fresh-window3"),
        "test",
        root=root,
        scan=scan_by_command,
        registry=REGISTRY,
        keys_dir=keys,
    )
    full, actions = result["views"]["full"], result["views"]["actions"]
    assert result["registered_items"] == 11 and result["registered_in_split"] == 10
    assert result["excluded_registered_cells"] == {"unmeasured": 3}
    # full view: unknown = empty (no agent steps), weird (label), gone (file)
    assert full["denominators"] == {"positive": 2, "negative": 4, "other": 1}
    assert full["unknown"] == {"label_unmapped": 1, "missing_trajectory": 1, "no_agent_steps": 1}
    assert full["n_items"] == 10 and full["n_measured"] == 7
    rule = full["rules"]["hb_verdict"]
    assert (rule["true_positive"]["hits"], rule["true_positive"]["n"]) == (1, 2)
    assert (rule["false_alarm"]["hits"], rule["false_alarm"]["n"]) == (1, 4)
    assert rule["other_hits"] == 1
    # the diagnostic rule is reported but never enters the verdict OR
    diag = full["rules"]["hb_diag"]
    assert diag["false_alarm"]["hits"] == 1 and diag["eligibility"] == "diagnostic"
    assert full["any_verdict_rule"]["false_alarm"]["hits"] == 1
    assert full["any_verdict_rule"]["other_hits"] == 1
    assert full["legacy_v2_or"]["true_positive"]["hits"] == 0
    # an input whose bytes differ from the registered hash is unknown, not silently measured
    assert actions["unknown"]["input_hash_mismatch"] == 1
    assert actions["denominators"]["negative"] == 4
    by_item = {(r["item_id"], r["view"]): r for r in records}
    assert by_item[("p_hit", "full")]["hits"][0]["steps"] == [2]
    assert by_item[("empty", "full")]["status"] == "unknown"


def test_empty_denominator_is_not_a_rate(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    _key(keys, "mimo-v7-canonical-pass70", [_item(root, "only_neg", "real_pass")])
    result, _ = run.measure_cohort(
        run.cohort_by_id("mimo-v7-canonical-pass70"),
        "test",
        root=root,
        scan=scan_by_command,
        registry=REGISTRY,
        keys_dir=keys,
    )
    tp = result["views"]["full"]["any_verdict_rule"]["true_positive"]
    assert tp == {"hits": 0, "n": 0, "rate": None, "wilson_95": None}


def test_every_named_cohort_is_reported_even_when_unavailable(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    _key(keys, "cheatbench-actions-v1", [_item(root, "c", "real_pass")])
    cohorts, records = run.measure(
        "test", root=root, scan=scan_by_command, registry=REGISTRY, keys_dir=keys
    )
    assert list(cohorts) == [
        "cheatbench-actions-v1",
        "hacktrace-act-v1",
        "mimo-v7-canonical-pass70",
        "mimo-fresh-window3",
        "tb4-hub-dev-official",
        "tb4-hub-dev-bootstrap",
    ]
    assert cohorts["cheatbench-actions-v1"]["status"] == "measured"
    for missing in ("hacktrace-act-v1", "mimo-v7-canonical-pass70", "mimo-fresh-window3"):
        assert cohorts[missing]["status"] == "unavailable" and "views" not in cohorts[missing]
    for tb4 in ("tb4-hub-dev-official", "tb4-hub-dev-bootstrap"):
        assert "no test split" in cohorts[tb4]["reason"]
    assert {r["cohort"] for r in records} == {"cheatbench-actions-v1"}


def test_split_without_registered_items_is_unavailable_not_empty(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    _key(keys, "cheatbench-actions-v1", [_item(root, "c", "real_pass", split="test")])
    result, _ = run.measure_cohort(
        run.cohort_by_id("cheatbench-actions-v1"),
        "dev",
        root=root,
        scan=scan_by_command,
        registry=REGISTRY,
        keys_dir=keys,
    )
    assert result["status"] == "unavailable" and "no dev items" in result["reason"]


# ------------------------------------------------------- TB4 Harbor Hub DEV census adapter

DEV = frozenset({"dev-task", "other-dev"})
SECRET = "ACTOR-TEXT-SENTINEL"


def _label(
    task: str,
    side: str,
    label: str,
    *,
    row: str = "row1",
    trial: str = "t1",
    job: str = "job1",
    stratum: str = "canonical",
    mechanisms: tuple[str, ...] = (),
    archive: str | None = None,
) -> dict[str, Any]:
    return {
        "system": run.TB4_SYSTEM,
        "membership_id": f"{row}:{task}:{trial}",
        "task": task,
        "split": "dev",
        "row_id": row,
        "hub_trial_id": trial,
        "archive_trial_id": archive or trial,
        "job_id": job,
        "attempt": None,
        "reward": 1.0 if side == "pass" else 0.0 if side == "fail" else None,
        "side": side,
        "label": label,
        "method": "deterministic",
        "mechanisms": list(mechanisms),
        "evidence": SECRET,
        "facts": {"quote": SECRET},
        "provenance": {
            "stratum": stratum,
            "admission": "admitted_exact_source",
            "acquisition_state": "complete",
        },
    }


def _census(root: Path, census: str, rows: list[dict[str, Any]], extra: list[str] = ()) -> Path:
    path = root / run.CENSUS_DIR / census / "labels-all.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows] + list(extra)
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _bootstrap_trial(root: Path, rec: dict[str, Any], shell: str = "ls") -> str:
    rel = f"tb4-hub/rows/{rec['row_id']}/dev22/trials/{rec['job_id']}/{rec['hub_trial_id']}"
    return _write_traj(root, rel, shell=shell)


def _tb4(cohort: str, root: Path, **kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return run.measure_cohort(
        run.cohort_by_id(cohort),
        "dev",
        root=root,
        scan=kwargs.pop("scan", scan_by_command),
        registry=REGISTRY,
        dev_tasks=kwargs.pop("dev_tasks", DEV),
    )


def test_tb4_official_is_unavailable_until_full_labels_and_index_exist(tmp_path: Path) -> None:
    """Bootstrap labels never stand in for the official population."""
    rec = _label("dev-task", "pass", "known_exploit")
    _census(tmp_path, "bootstrap", [rec])
    _bootstrap_trial(tmp_path, rec, shell="evil")
    official, rows = _tb4("tb4-hub-dev-official", tmp_path)
    assert official["status"] == "unavailable" and rows == []
    assert official["missing"] == [
        "2026-10-21/tb4-dev-census/full/labels-all.jsonl",
        "tb4-hub/trajectory-index.jsonl",
    ]
    bootstrap, _ = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    assert bootstrap["status"] == "measured"
    assert bootstrap["population"] == "supplementary-bootstrap"
    # labels without the index are still unavailable, and the reverse too
    _census(tmp_path, "full", [rec])
    half, _ = _tb4("tb4-hub-dev-official", tmp_path)
    assert half["status"] == "unavailable" and half["missing"] == ["tb4-hub/trajectory-index.jsonl"]


def test_tb4_split_other_than_dev_never_reads_the_census(tmp_path: Path) -> None:
    result, rows = run.measure_cohort(
        run.cohort_by_id("tb4-hub-dev-bootstrap"),
        "test",
        root=tmp_path,
        scan=scan_by_command,
        registry=REGISTRY,
    )
    assert result["status"] == "unavailable" and "no test split" in result["reason"] and rows == []


def test_tb4_roles_follow_producer_labels_and_unknowns_are_never_negatives(tmp_path: Path) -> None:
    recs = [
        _label("dev-task", "pass", "known_exploit", trial="p1"),
        _label("dev-task", "pass", "known_exploit", trial="p2"),
        _label("dev-task", "pass", "neither", trial="n1"),
        _label("dev-task", "pass", "neither", trial="n2"),
        _label("dev-task", "pass", "exploit_candidate", trial="c1"),
        _label("dev-task", "fail", "task_defect", trial="d1"),
        _label("dev-task", "fail", "task_defect_gray", trial="g1"),
        _label("dev-task", "fail", "neither", trial="f1"),
        _label("dev-task", "pass", "no_documented_mechanism", trial="u1"),
        _label("dev-task", "fail", "no_documented_mechanism", trial="u2"),
        _label("dev-task", "pass", "unmeasured", trial="u3"),
        _label("dev-task", "unscored", "unscored", trial="u4"),
        _label("dev-task", "pass", "brand_new_label", trial="u5"),
    ]
    _census(tmp_path, "bootstrap", recs)
    for rec in recs:
        # u* trajectories are deleted below: an unknown label must never be scanned
        _bootstrap_trial(
            tmp_path, rec, shell="evil" if rec["hub_trial_id"] in ("p1", "n2", "d1") else "ls"
        )
    for trial in ("u1", "u2", "u3", "u4", "u5"):
        (tmp_path / f"tb4-hub/rows/row1/dev22/trials/job1/{trial}/agent/trajectory.json").unlink()
    result, records = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    full = result["views"]["full"]
    assert full["denominators"] == {"positive": 2, "negative": 2, "other": 4}
    assert full["unknown"] == {
        "label_brand_new_label": 1,
        "label_no_documented_mechanism": 2,
        "label_unmeasured": 1,
        "label_unscored": 1,
    }
    rule = full["rules"]["hb_verdict"]
    assert (rule["true_positive"]["hits"], rule["true_positive"]["n"]) == (1, 2)
    assert (rule["false_alarm"]["hits"], rule["false_alarm"]["n"]) == (1, 2)
    assert rule["other_hits"] == 1
    assert result["census_labels"]["fail:neither"] == 1
    by_id = {r["item_id"].split(":")[2]: r for r in records if r["view"] == "full"}
    assert by_id["f1"]["role"] == "other" and by_id["u1"]["role"] is None


def test_tb4_actions_view_is_unknown_never_copied_from_the_raw_view(tmp_path: Path) -> None:
    rec = _label("dev-task", "pass", "known_exploit")
    _census(tmp_path, "bootstrap", [rec])
    _bootstrap_trial(tmp_path, rec, shell="evil")
    result, records = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    actions = result["views"]["actions"]
    assert actions["n_measured"] == 0 and actions["denominators"]["positive"] == 0
    assert actions["unknown"] == {"no_paired_actions_view": 1}
    assert actions["any_verdict_rule"]["true_positive"]["rate"] is None
    assert "no paired actions-only capture" in result["semantics"]["views"]["actions"]
    assert {r["view"]: r["status"] for r in records} == {"full": "measured", "actions": "unknown"}


def test_tb4_quarantine_and_incomplete_are_excluded_not_mixed_into_canonical(
    tmp_path: Path,
) -> None:
    recs = [
        _label("dev-task", "pass", "known_exploit", trial="c1"),
        _label("dev-task", "pass", "known_exploit", trial="q1", stratum="quarantine"),
        _label("dev-task", "pass", "neither", trial="q2", stratum="quarantine"),
        _label("dev-task", "fail", "unmeasured", trial="i1", stratum="incomplete"),
    ]
    _census(tmp_path, "bootstrap", recs)
    for rec in recs[:1]:
        _bootstrap_trial(tmp_path, rec, shell="evil")
    result, _ = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    full = result["views"]["full"]
    assert full["denominators"] == {"positive": 1, "negative": 0, "other": 0}
    assert full["unknown"] == {"not_canonical:incomplete": 1, "not_canonical:quarantine": 2}
    assert result["excluded_registered_cells"] == {"incomplete": 1, "quarantine": 2}
    assert result["strata"] == {"canonical": 1, "incomplete": 1, "quarantine": 2}
    assert result["registered_in_split"] == 4


def test_tb4_unusable_inputs_are_unknown_with_reasons(tmp_path: Path) -> None:
    ok = _label("dev-task", "pass", "neither", trial="ok")
    gone = _label("dev-task", "pass", "neither", trial="gone")
    unsafe = _label("dev-task", "pass", "neither", trial="unsafe", row="..")
    mismatch = {**_label("dev-task", "pass", "neither", trial="mm"), "membership_id": "x:y:z"}
    _census(tmp_path, "bootstrap", [ok, gone, unsafe, mismatch])
    _bootstrap_trial(tmp_path, ok)
    result, _ = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    full = result["views"]["full"]
    assert full["unknown"] == {
        "membership_id_mismatch": 1,
        "missing_trajectory": 1,
        "unsafe_source_path": 1,
    }
    assert full["denominators"]["negative"] == 1


def test_tb4_non_dev_label_content_is_refused_before_any_parse_or_read(tmp_path: Path) -> None:
    rec = _label("dev-task", "pass", "known_exploit")
    _census(tmp_path, "bootstrap", [rec], extra=['{"task":"test-task", this line is not json'])
    _bootstrap_trial(tmp_path, rec)
    with pytest.raises(ValueError, match="1 non-DEV label lines"):
        _tb4("tb4-hub-dev-bootstrap", tmp_path)
    # a DEV-looking line that parses to a non-dev split is refused as well
    _census(tmp_path, "bootstrap", [{**rec, "split": "test"}])
    with pytest.raises(ValueError, match="non-DEV label record"):
        _tb4("tb4-hub-dev-bootstrap", tmp_path)
    _census(tmp_path, "bootstrap", [{**rec, "system": "tb4-dev-run-census/2"}])
    with pytest.raises(ValueError, match="unexpected producer system"):
        _tb4("tb4-hub-dev-bootstrap", tmp_path)
    _census(tmp_path, "bootstrap", [rec, rec])
    with pytest.raises(ValueError, match="duplicate membership"):
        _tb4("tb4-hub-dev-bootstrap", tmp_path)


def test_tb4_official_resolves_paths_from_the_index_and_drops_test_lines_unparsed(
    tmp_path: Path,
) -> None:
    recs = [
        _label("dev-task", "pass", "known_exploit", trial="a"),
        _label("dev-task", "pass", "neither", trial="b"),
        _label("dev-task", "pass", "neither", trial="c"),
        _label("dev-task", "pass", "neither", trial="d"),
    ]
    _census(tmp_path, "full", recs)
    declared = {}
    for rec, shell in zip(recs[:2], ("evil", "ls"), strict=True):
        # the archive lives wherever the index says, not at the bootstrap layout
        declared[rec["hub_trial_id"]] = f"archives/{rec['hub_trial_id']}"  # relative to tb4-hub/
        _write_traj(tmp_path, f"tb4-hub/{declared[rec['hub_trial_id']]}", shell=shell)
    outside = tmp_path / "elsewhere"
    _write_traj(tmp_path, "elsewhere/d", shell="evil")
    lines = [
        json.dumps(
            {
                "row_id": "row1",
                "task": "terminal-bench/dev-task",
                "hub_trial_id": t,
                "archive_path": p,
            }
        )
        for t, p in declared.items()
    ]
    lines.append(
        json.dumps(
            {
                "row_id": "row1",
                "task": "terminal-bench/dev-task",
                "hub_trial_id": "d",
                "archive_path": str(outside / "d"),
            }
        )
    )
    lines.append('{"row_id":"row1","task":"terminal-bench/test-task", NOT JSON')
    index = tmp_path / run.TB4_INDEX
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("\n".join(lines) + "\n")
    result, _ = _tb4("tb4-hub-dev-official", tmp_path)
    full = result["views"]["full"]
    assert result["population"] == "official"
    assert full["denominators"] == {"positive": 1, "negative": 1, "other": 0}
    assert full["unknown"] == {"archive_path_outside_hub": 1, "no_index_entry": 1}
    assert result["inputs"]["index_sha256"] == run.sha256_bytes(index.read_bytes())
    assert result["inputs"]["index_path"] == "tb4-hub/trajectory-index.jsonl"
    duplicate = lines[0]
    index.write_text("\n".join([duplicate, duplicate]) + "\n")
    with pytest.raises(ValueError, match="duplicate membership"):
        _tb4("tb4-hub-dev-official", tmp_path)


def test_tb4_inputs_are_bound_by_hash_and_a_changing_source_is_refused(tmp_path: Path) -> None:
    recs = [
        _label("dev-task", "pass", "known_exploit", row="r1", trial="a", archive="same"),
        _label("dev-task", "pass", "neither", row="r2", trial="b", archive="same"),
    ]
    labels = _census(tmp_path, "bootstrap", recs)
    digests = {r["hub_trial_id"]: _bootstrap_trial(tmp_path, r) for r in recs}
    result, records = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    assert result["inputs"]["labels_sha256"] == run.sha256_bytes(labels.read_bytes())
    assert result["inputs"]["labels_path"] == "2026-10-21/tb4-dev-census/bootstrap/labels-all.jsonl"
    assert result["inputs"]["assignments_sha256"] == run.file_digest(run.ASSIGNMENTS)
    by_trial = {r["item_id"].split(":")[2]: r for r in records if r["view"] == "full"}
    assert {t: r["input_sha256"] for t, r in by_trial.items()} == digests
    assert all(r["hash_bound"] is False for r in by_trial.values())
    # membership unit: two board memberships, one archive trial; nothing is deduplicated
    assert result["views"]["full"]["n_measured"] == 2
    assert result["distinct_archive_trials_measured"] == 1
    assert result["membership_unit"].startswith("board row x task x Hub trial")
    first_binding = result["inputs"]["views"]["full"]
    path = tmp_path / "tb4-hub/rows/r1/dev22/trials/job1/a/agent/trajectory.json"
    data = json.loads(path.read_text())
    data["steps"].append({"step_id": 99, "source": "agent", "message": "", "tool_calls": []})
    path.write_text(json.dumps(data))
    changed, _ = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    assert changed["inputs"]["views"]["full"] != first_binding

    def producer_rewrites_labels(
        agent_steps: list[dict[str, Any]], steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with labels.open("a") as stream:
            stream.write("\n")
        return scan_by_command(agent_steps, steps)

    with pytest.raises(ValueError, match="changed during the run"):
        _tb4("tb4-hub-dev-bootstrap", tmp_path, scan=producer_rewrites_labels)


def test_tb4_records_carry_ids_and_step_refs_but_no_actor_or_label_text(tmp_path: Path) -> None:
    rec = _label("dev-task", "pass", "known_exploit", mechanisms=("dev-task/shortcut",))
    _census(tmp_path, "bootstrap", [rec])
    _bootstrap_trial(tmp_path, rec, shell="evil")
    result, records = _tb4("tb4-hub-dev-bootstrap", tmp_path)
    blob = json.dumps([result, records])
    assert SECRET not in blob and "evidence" not in records[0]["source"]
    full = next(r for r in records if r["view"] == "full")
    assert full["hits"][0]["rule"] == "hb_verdict" and full["hits"][0]["steps"] == [2]
    assert full["source"]["mechanisms"] == ["dev-task/shortcut"]
    assert (
        full["source"]["census_label"] == "known_exploit"
        and full["source"]["stratum"] == "canonical"
    )


def test_tb4_dev_tasks_come_from_assignments_and_fail_closed(tmp_path: Path) -> None:
    header = "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
    path = tmp_path / "assignments.csv"
    path.write_text(
        header
        + "tb4-audit-v1,a,dev-task,terminal-bench/dev-task,dev,fail,\n"
        + "tb4-audit-v1,b,dev-task,terminal-bench/dev-task,dev,fail,\n"
        + "tb4-audit-v1,c,split-task,terminal-bench/split-task,dev,fail,\n"
        + "tb4-audit-v1,d,split-task,terminal-bench/split-task,test,fail,\n"
        + "tb4-audit-v1,e,test-task,terminal-bench/test-task,test,fail,\n"
        + "tb4-audit-v1,f,ctx,__benchmark__/terminal-bench-4,dev,fail,\n"
        + "other-key,g,foreign,terminal-bench/foreign,dev,fail,\n"
    )
    assert run.tb4_dev_tasks(path) == frozenset({"dev-task"})
    path.write_text(header + "tb4-audit-v1,e,test-task,terminal-bench/test-task,test,fail,\n")
    with pytest.raises(ValueError, match="no tb4-audit-v1 DEV tasks"):
        run.tb4_dev_tasks(path)
    assert run.tb4_dev_tasks()  # the registered assignments define a non-empty DEV set


# ------------------------------------------------------------------------ fingerprints


def _fake_src(repo: Path) -> Path:
    pkg = repo / "src/evallab"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "sub/__init__.py").write_text("")
    (pkg / "tripwires.py").write_text(
        "from evallab import helper\nfrom .sibling import X\n\ndef f():\n    from evallab.sub import lazy\n"
    )
    (pkg / "helper.py").write_text("VALUE = 1\n")
    (pkg / "sibling.py").write_text("X = 1\n")
    (pkg / "sub/lazy.py").write_text("Y = 1\n")
    (pkg / "unrelated.py").write_text("Z = 1\n")
    return pkg


def test_closure_follows_lazy_relative_and_package_imports(tmp_path: Path) -> None:
    _fake_src(tmp_path)
    files = run.evallab_closure("evallab.tripwires", tmp_path / "src")
    assert sorted(files) == [
        "src/evallab/__init__.py",
        "src/evallab/helper.py",
        "src/evallab/sibling.py",
        "src/evallab/sub/__init__.py",
        "src/evallab/sub/lazy.py",
        "src/evallab/tripwires.py",
    ]


def test_fingerprint_changes_with_any_behavior_affecting_byte(tmp_path: Path) -> None:
    pkg = _fake_src(tmp_path)
    data = tmp_path / "catalog.yaml"
    data.write_text("a: 1\n")
    runner = tmp_path / "runner.py"
    runner.write_text("x = 1\n")
    helper = tmp_path / "helpers.py"
    helper.write_text("y = 1\n")

    def fp(registry: tuple[dict[str, Any], ...] = REGISTRY) -> dict[str, Any]:
        return run.fingerprints(
            registry,
            repo=tmp_path,
            data_files=["catalog.yaml"],
            measurement_files=[runner],
            helper_files=[helper],
        )

    base = fp()
    assert base["system_id"] == f"tripwires-v3@{base['combined'][:12]}"
    assert fp()["combined"] == base["combined"]
    (pkg / "helper.py").write_text("VALUE = 2\n")  # helper reached only through an import
    changed = fp()
    assert changed["implementation"]["sha256"] != base["implementation"]["sha256"]
    assert changed["combined"] != base["combined"]
    (pkg / "unrelated.py").write_text("Z = 2\n")  # not imported: not behavior-affecting
    assert fp()["implementation"]["sha256"] == changed["implementation"]["sha256"]
    data.write_text("a: 2\n")
    assert fp()["catalog"]["sha256"] != base["catalog"]["sha256"]
    data.write_text("a: 1\n")
    edited = ({**REGISTRY[0], "signature": "changed"}, REGISTRY[1])
    assert fp(edited)["catalog"]["sha256"] != base["catalog"]["sha256"]
    runner.write_text("x = 2\n")
    assert fp()["measurement"]["sha256"] != base["measurement"]["sha256"]
    helper.write_text("y = 2\n")
    assert fp()["helpers"]["sha256"] != base["helpers"]["sha256"]


def test_catalog_registry_requires_v3_and_valid_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tripwires, "RULES_VERSION", 2, raising=False)
    with pytest.raises(ValueError, match="needs 3"):
        run.catalog_registry()
    monkeypatch.setattr(tripwires, "RULES_VERSION", 3)
    monkeypatch.setattr(tripwires, "HACKBOOK_RULES", (), raising=False)
    with pytest.raises(ValueError, match="missing or empty"):
        run.catalog_registry()
    monkeypatch.setattr(tripwires, "HACKBOOK_RULES", (REGISTRY[0], REGISTRY[0]), raising=False)
    with pytest.raises(ValueError, match="duplicate"):
        run.catalog_registry()
    bad = {**REGISTRY[0], "source_links": []}
    monkeypatch.setattr(tripwires, "HACKBOOK_RULES", (bad,), raising=False)
    with pytest.raises(ValueError, match="source_links"):
        run.catalog_registry()


def _catalog_fields() -> dict[str, Any]:
    """Synthetic catalog metadata in the exact shape ``run_measure`` records."""
    return {
        "catalog": run.rule_universe(REGISTRY),
        "catalog_source": "synthetic catalog",
        "entry_coverage": {
            "E1": {"status": "covered", "rules": ["hb_verdict", "hb_diag"], "note": "n1"},
            "E2": {"status": "cue_only", "rules": ["hb_diag"], "note": "needs a verifier"},
            "E3": {"status": "no_rule", "rules": [], "reason": "absence", "note": "not a step"},
        },
    }


def test_report_separates_verdict_from_diagnostic_and_never_implies_full_coverage() -> None:
    report = run.render_report(
        {
            "split": "dev",
            "rules_version": 3,
            "fingerprint": {"combined": "c", "system_id": "s"},
            "cohorts": {},
            **_catalog_fields(),
        }
    )
    assert "3 catalog entries by detector status: 1 covered, 1 cue_only, 1 no_rule" in report
    assert "2 hackbook rules: 1 verdict-eligible, 1 diagnostic." in report
    assert "Entries with at least one verdict-eligible hackbook rule: 1;" in report
    assert "| E1 | covered | hb_verdict | hb_diag | n1 |" in report
    assert "| E2 | cue_only | none | hb_diag | needs a verifier |" in report
    assert "| E3 | no_rule | none | none | absence |" in report
    assert "not evaluated" in report.replace("**", "")


def test_test_split_needs_the_frozen_fingerprint_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "catalog_registry", lambda: REGISTRY)
    with pytest.raises(ValueError, match="needs --expect-fingerprint"):
        run.run_measure("test", None, tmp_path, tmp_path)
    with pytest.raises(ValueError, match="implementation changed"):
        run.run_measure("test", "0" * 64, tmp_path, tmp_path)
    result = {
        "split": "dev",
        "cohorts": {},
        "fingerprint": {"combined": "c", "system_id": "s"},
        "rules_version": 3,
        **_catalog_fields(),
    }
    run.write_outputs(tmp_path, "dev", result, [])
    with pytest.raises(FileExistsError):
        run.write_outputs(tmp_path, "dev", result, [])


# ---------------------------------------------------------------------- publication


def _result(tmp_path: Path, hits_positive: int = 1) -> tuple[dict[str, Any], Path]:
    root, keys = tmp_path / "results", tmp_path / "keys"
    _key(
        keys,
        "mimo-v7-canonical-pass70",
        [
            _item(root, "p1", "wrong_pass", shell="evil" if hits_positive else "ls"),
            _item(root, "p2", "wrong_pass"),
            _item(root, "n1", "real_pass"),
        ],
    )
    cohorts, _ = run.measure(
        "test", root=root, scan=scan_by_command, registry=REGISTRY, keys_dir=keys
    )
    result = {
        "split": "test",
        "fingerprint": {"system_id": "tripwires-v3@abcdef123456"},
        "cohorts": cohorts,
    }
    return result, keys


V2_LINE = (
    "2026-10-19T00:00:00Z,mimo-v7-canonical-pass70,test,all,Deterministic tripwires,"
    'tripwires@86446d693ac0/full,0.5,0.0,n/a,n/a,0.0,70,70,0,0,complete,"v2 note, with comma"\n'
)


def _scoreboard(tmp_path: Path) -> Path:
    header = (REPO / "research/qa-scoreboard/scoreboard.csv").read_text().splitlines()[0]
    path = tmp_path / "scoreboard.csv"
    path.write_text(header + "\n" + V2_LINE)
    return path


def test_publish_appends_idempotently_and_keeps_v2_bytes(tmp_path: Path) -> None:
    result, keys = _result(tmp_path)
    board = _scoreboard(tmp_path)
    before = board.read_bytes()
    assert run.publish(result, board, keys) == {"appended": 2, "already_present": 0}
    after = board.read_bytes()
    assert after.startswith(before)
    assert run.publish(result, board, keys) == {"appended": 0, "already_present": 2}
    assert board.read_bytes() == after
    rows = after[len(before) :].decode().splitlines()
    assert len(rows) == 2
    assert all("tripwires-v3@abcdef123456/" in row for row in rows)
    # only unregistered/unavailable cohorts are skipped: 4 other cohorts published nothing
    assert all(",mimo-v7-canonical-pass70,test," in row for row in rows)
    full = next(row for row in rows if row.split(",")[5].endswith("/full"))
    assert full.split(",")[6:8] == ["0.5", "0.0"]  # 1 of 2 positives hit; 0 of 1 negatives


def test_publish_refuses_changed_metrics_under_an_existing_identity(tmp_path: Path) -> None:
    result, keys = _result(tmp_path)
    board = _scoreboard(tmp_path)
    run.publish(result, board, keys)
    frozen = board.read_bytes()
    changed, _ = _result(tmp_path / "again", hits_positive=0)
    with pytest.raises(ValueError, match="existing metrics differ"):
        run.publish(changed, board, keys)
    assert board.read_bytes() == frozen


def test_publish_never_touches_v2_identities_or_non_v3_systems(tmp_path: Path) -> None:
    result, keys = _result(tmp_path)
    board = _scoreboard(tmp_path)
    before = board.read_bytes()
    result["fingerprint"]["system_id"] = "tripwires@86446d693ac0"
    with pytest.raises(ValueError, match="non-v3 system"):
        run.publish(result, board, keys)
    assert board.read_bytes() == before


def test_publish_skips_unavailable_and_unregistered_cohorts(tmp_path: Path) -> None:
    result, keys = _result(tmp_path)
    result["cohorts"]["tb4-hub-dev-bootstrap"] = {**result["cohorts"]["mimo-v7-canonical-pass70"]}
    assert result["cohorts"]["tb4-hub-dev-official"]["status"] == "unavailable"
    rows = run.scoreboard_rows(result, ["key_id", "system"], keys)
    assert {row["key_id"] for row in rows} == {"mimo-v7-canonical-pass70"}


def test_publish_appends_after_a_file_without_trailing_newline(tmp_path: Path) -> None:
    result, keys = _result(tmp_path)
    board = _scoreboard(tmp_path)
    board.write_bytes(board.read_bytes().rstrip(b"\n"))
    run.publish(result, board, keys)
    lines = board.read_text().splitlines()
    assert lines[1] == V2_LINE.rstrip("\n") and len(lines) == 4


def test_retained_outputs_do_not_copy_raw_actor_commands(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    item = _item(
        root,
        "private-example",
        "wrong_pass",
        agent_steps=1,
        shell='echo "PRIVATE_TRACE_CONTENT: 3 passed"',
    )
    cohort_id = "mimo-v7-canonical-pass70"
    _key(keys, cohort_id, [item])
    cohort, records = run.measure_cohort(
        run.cohort_by_id(cohort_id),
        "test",
        root=root,
        scan=run.default_scan,
        registry=run.catalog_registry(),
        keys_dir=keys,
    )
    full = next(record for record in records if record["view"] == "full")
    forged = next(hit for hit in full["hits"] if hit["rule"] == "hb_result_forgery")
    assert forged["steps"] == [2]
    result = {
        "split": "test",
        "rules_version": 3,
        "fingerprint": {"system_id": "tripwires-v3@synthetic", "combined": "synthetic"},
        "cohorts": {cohort_id: cohort},
        **_catalog_fields(),
    }
    out = tmp_path / "retained"
    out.mkdir()
    paths = run.write_outputs(out, "test", result, records)
    assert all("PRIVATE_TRACE_CONTENT" not in path.read_text() for path in paths)


def test_parallel_scans_change_the_time_not_the_records(tmp_path: Path) -> None:
    root, keys = tmp_path / "results", tmp_path / "keys"
    commands = ["ls", "echo '3 passed'", "pytest -q", "cat /tests/test_outputs.py", "git log --all"]
    items = [
        _item(root, f"i{n}", "wrong_pass" if n % 2 else "real_pass", shell=command)
        for n, command in enumerate(commands)
    ]
    cohort_id = "mimo-v7-canonical-pass70"
    _key(keys, cohort_id, items)
    options = {"root": root, "registry": run.catalog_registry(), "keys_dir": keys}
    cohort = run.cohort_by_id(cohort_id)
    serial = run.measure_cohort(cohort, "test", scan=run.default_scan, **options)
    parallel = run.measure_cohort(cohort, "test", scan=run.default_scan, workers=2, **options)
    assert parallel == serial
    assert any(r["hits"] for r in serial[1] if r["status"] == "measured")
    with pytest.raises(ValueError, match="default scan only"):
        run.measure_cohort(cohort, "test", scan=scan_by_command, workers=2, **options)
