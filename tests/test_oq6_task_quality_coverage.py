"""Synthetic-only union/custody proofs; no retained data, model or provider I/O."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

EXPERIMENT = Path(__file__).resolve().parents[1] / "research/experiments/oq6-task-quality-gate-rerun"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coverage = load_module("oq6_task_quality_coverage", EXPERIMENT / "coverage.py")
summary = load_module("oq6_coverage_original_summary", EXPERIMENT / "result_summary.py")
SONNET = "synthetic/sonnet"
KIMI = "synthetic/kimi"
DISABLED = "synthetic/disabled"
GATES = ("old_0_8", "new_verified_0_5")
OWNED = (
    "call-consumed.json", "prompt.txt", "system.txt", "controlled-subscription.yml",
    "stdout.jsonl", "stderr.txt", "response.txt", "sessions",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return sha(path.read_bytes())


def artifacts(directory):
    return {
        str(path.relative_to(directory)): {"sha256": sha(path.read_bytes()), "bytes": path.stat().st_size}
        for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "receipt.json"
    }


def quality_row(case, *, complete=False, matched=0, eligible=0, contradictions=0, required=None):
    stats = {
        "matched_keys": matched, "eligible_findings": eligible,
        "key_match_claims": matched, "source_contradictions": contradictions,
        "plausible_unkeyed": eligible - matched - contradictions, "unclear": 0,
        "matched_key_ids": ["synthetic-key-" + case["case_id"]] if matched else [],
    }
    return {
        "case_id": case["case_id"], "key_count": case["key_count"], "complete": complete,
        "scorer_required": required, "parse_status": "complete" if complete else "missing",
        "judge_status": "complete" if complete else "missing", "parsed_findings": eligible if complete else None,
        "gate_added_verified": 0 if complete else None, "gate_added_unverified": 0 if complete else None,
        "gates": {gate: dict(stats) for gate in GATES},
    }


class SyntheticPaired:
    """Injected arithmetic seam: assertions test joining, not a second scorer."""

    def __init__(self):
        self.aggregated = []

    def evaluate_cell(self, case, payloads, judgment, keys, *, audit_status="answered", raw_findings_count=None):
        assert keys is None  # Coverage must never hydrate a gold/key body.
        row = quality_row(case)
        if audit_status != "answered" or payloads is None:
            return row
        if not isinstance(payloads, list) or type(raw_findings_count) is not int or len(payloads) != raw_findings_count:
            row["parse_status"] = "invalid"
            return row
        row["parse_status"] = "complete"
        row["scorer_required"] = any(item["eligible"] for item in payloads)
        # A locally evaluable empty union still cannot finalize a missing cell.
        row["complete"] = not row["scorer_required"]
        return row

    def aggregate_cells(self, rows):
        assert len({row["case_id"] for row in rows}) == len(rows)
        self.aggregated.append([row["case_id"] for row in rows])
        complete = [row for row in rows if row["complete"]]
        planned_keys = sum(row["key_count"] for row in rows)
        evaluable_keys = sum(row["key_count"] for row in complete)
        all_complete = len(complete) == len(rows)
        gates = {}
        for gate in GATES:
            fields = ("matched_keys", "eligible_findings", "key_match_claims", "source_contradictions", "plausible_unkeyed", "unclear")
            totals = {field: sum(row["gates"][gate][field] for row in complete) if complete else None for field in fields}
            eligible = totals["eligible_findings"]
            recall = totals["matched_keys"] / evaluable_keys if evaluable_keys else None
            precision = totals["matched_keys"] / eligible if eligible else None
            contradiction = totals["source_contradictions"] / eligible if eligible else None
            gates[gate] = {
                **totals, "evaluable_tasks": len(complete), "evaluable_keys": evaluable_keys,
                "observed_recall": recall, "full_cohort_recall": recall if all_complete else None,
                "key_backed_precision": precision, "full_cohort_key_backed_precision": precision if all_complete else None,
                "source_contradiction_proportion": contradiction,
                "full_cohort_source_contradiction_proportion": contradiction if all_complete else None,
                "true_population_false_positive_rate": None,
                "matched_gold_keys": [{"case_id": row["case_id"], "key_id": key} for row in complete for key in row["gates"][gate]["matched_key_ids"]],
            }
        return {
            "planned_tasks": len(rows), "complete_tasks": len(complete),
            "complete_case_ids": [row["case_id"] for row in complete],
            "missing_tasks": [row["case_id"] for row in rows if not row["complete"]],
            "planned_keys": planned_keys, "evaluable_keys": evaluable_keys,
            "task_coverage": len(complete) / len(rows) if rows else None,
            "key_coverage": evaluable_keys / planned_keys if planned_keys else None,
            "full_cohort_complete": all_complete, "gates": gates,
            "true_population_false_positive_rate": None,
        }


class SyntheticTransport:
    OWNED_ARTIFACTS = OWNED

    def __init__(self):
        self.recovered = []
        self.inspected = []

    def _existing(self, directory, request):
        self.inspected.append(directory)
        receipt = json.loads((directory / "receipt.json").read_bytes())
        if (receipt["artifacts"] != artifacts(directory) or
                receipt.get("status") == "answered" and (
                    receipt.get("response_sha256") != sha((directory / "response.txt").read_bytes()) or
                    receipt.get("served_identities") != [request["requested_route"]] or
                    receipt.get("isolation_valid") is not True or
                    receipt.get("native_prompt_matches_frozen_bytes") is not True or
                    receipt.get("native_response_matches_stdout") is not True)):
            return self._abandoned(directory, request, "synthetic_custody_failure")
        return receipt

    def _abandoned(self, directory, request, error):
        self.recovered.append(directory)
        # As in the pinned helper, this is an in-memory snapshot, not a receipt.
        return {
            "attempt_consumed": True, "status": "unanswered", "artifacts_are_snapshot": True,
            "requested_route": request["requested_route"], "error": error,
            "tokens_in": None, "tokens_out": None, "reasoning_tokens": None,
            "served_identity": None, "wall_seconds": None,
        }


def accounting(receipts):
    result = {"transport_attempts": len(receipts), "paid_spend_usd": 0, "subscription_usd": None}
    for output, field in (("input", "tokens_in"), ("output", "tokens_out"), ("reasoning", "reasoning_tokens")):
        missing = sum(row.get(field) is None for row in receipts)
        known = sum(row[field] for row in receipts if row.get(field) is not None)
        result[output + "_reported_sum"] = known
        result[output + "_missing_usage_attempts"] = missing
        result[output + "_tokens"] = None if missing else known
    return result


def case(cid, keys=1, *, stage="dev", prepared=True):
    return {
        "case_id": cid, "stage": stage, "key_count": keys,
        "preparation_status": "prepared" if prepared else "unavailable_input",
        "scheduling_priority": int("".join(character for character in cid if character.isdigit()) or "0"),
        "source_sha256": sha(("synthetic source " + cid).encode()),
        # Deliberately nonexistent: metadata-only coverage may not read these.
        "keys_path": "/not-accessible/synthetic-gold/" + cid + ".json",
        "source_path": "/not-accessible/synthetic-source/" + cid,
        "prompts": {arm: {"sha256": sha(("prompt " + arm + " " + cid).encode())} for arm in ("A", "B")},
    }


class Study:
    def __init__(self, root, cases=None):
        self.root = root
        self.cases = cases or [case("D1", 3), case("D2", 2), case("D3", 0), case("X1", stage="cross")]
        self.index = {item["case_id"]: item for item in self.cases}
        self.transport = SyntheticTransport()
        self.paired = SyntheticPaired()
        self.science = SimpleNamespace(
            paired=self.paired, transport=self.transport, accounting=accounting,
            paired_ab_report=summary.paired_ab_report,
            scorer_route=SONNET,
            request_pins={"contract_sha256": sha(b"synthetic contract"),
                          "attestation_source_sha256": sha(b"synthetic attestation")},
        )
        config = root / "config.yml"
        config.write_bytes(b"synthetic controlled config\n")
        protocol = {
            "models": [SONNET, KIMI, DISABLED], "fixed_scorer": SONNET,
            "config": {"path": str(config), "sha256": sha(config.read_bytes())},
            "runtime": {"path": "/synthetic/immutable/runtime"}, "gates": list(GATES),
            "DEV": {"tasks": sum(item["stage"] == "dev" for item in self.cases)},
            "arms": {"A": "synthetic ablated", "B": "synthetic history"},
            "code_files": [], "pre_cross_data_files": [],
            "execution": {
                "route_order": [SONNET, KIMI], "disabled_routes": {DISABLED: "synthetic disabled"},
                "science_root": str(root / "science"), "raw_root": str(root / "phase4"),
            },
        }
        plan = {
            "cases": self.cases,
            "workers": [{"route": route, "arm": arm, "case_ids": [item["case_id"] for item in self.cases]}
                        for route in (SONNET, KIMI) for arm in ("A", "B")],
        }
        self.parent = self.freeze(protocol, plan, root / "frozen-parent")
        self.continuations = []

    def freeze(self, protocol, plan, directory):
        plan_path = directory / "plan.json"
        plan_sha = write_json(plan_path, plan)
        protocol["execution"].update(plan_path=str(plan_path), plan_sha256=plan_sha)
        protocol_path = directory / "protocol.json"
        digest = write_json(protocol_path, protocol)
        protocol_path.with_suffix(".sha256").write_text(digest + "\n")
        return {
            "protocol": protocol, "plan": plan, "path": protocol_path,
            "digest": digest, "plan_sha": plan_sha, "root": Path(protocol["execution"]["raw_root"]),
        }

    def base(self, phase, route, arm, cid):
        item = self.index[cid]
        return phase["root"] / "calls" / route.replace("/", "--") / arm / item["stage"] / cid

    def attempt(self, phase, route, arm, cid, *, role="audit", status="answered", tokens=10, marker_only=False):
        directory = self.base(phase, route, arm, cid) / role
        directory.mkdir(parents=True, exist_ok=True)
        requested = route if role == "audit" else SONNET
        prompt = ("prompt " + arm + " " + cid).encode() if role == "audit" else b"synthetic anonymous union"
        system = b"synthetic fixed system"
        config = Path(phase["protocol"]["config"]["path"]).read_bytes()
        request = {
            "requested_route": requested, "requested_model": requested,
            "requested_provider": requested.split("/", 1)[0], "requested_model_id": requested.split("/", 1)[1],
            "prompt_sha256": sha(prompt), "system_sha256": sha(system), "config_sha256": sha(config),
            "expected_array": "findings" if role == "audit" else "judgments", "timeout_seconds": 173,
            "contract_sha256": sha(b"synthetic contract"), "attestation_source_sha256": sha(b"synthetic attestation"),
        }
        write_json(directory / "call-consumed.json", {**request, "attempt_limit": 1})
        if marker_only:
            return directory
        (directory / "prompt.txt").write_bytes(prompt)
        (directory / "system.txt").write_bytes(system)
        (directory / "controlled-subscription.yml").write_bytes(config)
        response = json.dumps({request["expected_array"]: [], "private_body": "sensitive-body-" + cid}).encode()
        (directory / "response.txt").write_bytes(response)
        receipt = {
            **request, "attempt_consumed": True, "attempt_limit": 1, "status": status,
            "call_consumed_sha256": sha((directory / "call-consumed.json").read_bytes()),
            "call_consumed_path": str(directory / "call-consumed.json"),
            "receipt_path": str(directory / "receipt.json"), "response_path": str(directory / "response.txt"),
            "response_sha256": sha(response), "answer": json.loads(response) if status == "answered" else None,
            "served_identities": [requested], "served_identity": requested,
            "isolation_valid": True, "native_prompt_matches_frozen_bytes": True,
            "native_response_matches_stdout": True, "errors": [], "artifacts": artifacts(directory),
            "model_change_events": [], "fallback_events": [], "model_fallback": False,
            "tokens_in": tokens, "tokens_out": 2 if tokens is not None else None,
            "reasoning_tokens": None, "wall_seconds": 1,
        }
        write_json(directory / "receipt.json", receipt)
        return directory

    def prediction(self, phase, route, arm, cid, *, required=True):
        directory = self.base(phase, route, arm, cid)
        receipt_path = directory / "audit" / "receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        return write_json(directory / "prediction.json", {
            "schema": "oq6.frozen-prediction/v1", "case_id": cid, "route": route, "arm": arm,
            "audit_status": "answered", "source_sha256": self.index[cid]["source_sha256"],
            "audit_receipt_sha256": sha(receipt_path.read_bytes()), "response_sha256": receipt["response_sha256"],
            "parse_error": None, "parsed_payloads": [{"eligible": True}] if required else [],
            "raw_findings_count": 1 if required else 0,
        })

    def final(self, phase, route, arm, cid, *, complete=True, matched=1, eligible=1, contradictions=0, required=True):
        directory = self.base(phase, route, arm, cid)
        if not (directory / "audit" / "receipt.json").is_file():
            self.attempt(phase, route, arm, cid)
        if complete and required and not (directory / "scorer" / "receipt.json").is_file():
            self.attempt(phase, route, arm, cid, role="scorer")
        row = quality_row(self.index[cid], complete=complete, matched=matched, eligible=eligible,
                          contradictions=contradictions, required=required)
        write_json(directory / "cell.json", {
            "case_id": cid, "route": route, "arm": arm, "stage": self.index[cid]["stage"],
            "freeze_sha256": phase["digest"], "paired": row,
        })

    def missing_outcome(self, phase, route, arm, cid):
        write_json(phase["root"] / "outcomes" / (route.replace("/", "--") + "__" + arm) / (cid + ".json"), {
            "case_id": cid, "route": route, "arm": arm, "stage": self.index[cid]["stage"],
            "freeze_sha256": phase["digest"], "plan_sha256": phase["plan_sha"], "status": "missing",
            "paired": quality_row(self.index[cid]), "reason": "cutoff_unsent_or_pending",
        })

    def continuation(self, route=SONNET, *, stage="dev"):
        cases = [item for item in self.cases if item["stage"] == stage]
        snapshot = {
            "schema": coverage.SNAPSHOT_SCHEMA, "stage": stage,
            "parent_root": str(self.parent["root"]), "parent_protocol_sha256": self.parent["digest"],
            "parent_plan_sha256": self.parent["plan_sha"], "captured_at": "2000-01-01T00:00:00Z", "workers": {},
        }
        workers = []
        raw_root = self.root / ("phase5-" + stage)
        for arm in ("A", "B"):
            key = route.replace("/", "--") + "__" + arm
            records = {}
            remaining, prior, resume = [], [], []
            for item in cases:
                cid = item["case_id"]
                base = self.base(self.parent, route, arm, cid)
                roles = {}
                for role in ("audit", "scorer"):
                    directory = base / role
                    receipt_path = directory / "receipt.json"
                    try:
                        receipt = json.loads(receipt_path.read_bytes())
                    except (OSError, ValueError):
                        receipt = {}
                    owned_files = [
                        {"path": str(child.resolve()), "sha256": sha(child.read_bytes())}
                        for child in sorted(directory.rglob("*")) if child.is_file() and child != receipt_path
                    ]
                    roles[role] = {
                        "directory": str(directory), "consumed": receipt_path.exists() or any((directory / name).exists() for name in OWNED),
                        "receipt_path": str(receipt_path) if receipt_path.is_file() else None,
                        "receipt_sha256": sha(receipt_path.read_bytes()) if receipt_path.is_file() else None,
                        "status": receipt.get("status"), "owned_files": owned_files,
                    }
                if ((base / "cell.json").is_file() or roles["scorer"]["consumed"] or
                        roles["audit"]["consumed"] and roles["audit"]["status"] != "answered" or
                        (base / "prediction.json").is_file() and not roles["audit"]["consumed"]):
                    action = "prior_terminal"
                elif item["preparation_status"] != "prepared":
                    action = "unavailable_input"
                elif roles["audit"]["consumed"]:
                    action = "resume_scorer"
                else:
                    action = "audit"
                record = {"action": action, **roles}
                for field in ("cell", "prediction"):
                    path = base / (field + ".json")
                    record[field + "_path"] = str(path) if path.is_file() else None
                    record[field + "_sha256"] = sha(path.read_bytes()) if path.is_file() else None
                records[cid] = record
                if action in {"prior_terminal", "unavailable_input"}:
                    prior.append(cid)
                else:
                    remaining.append(cid)
                    if action == "resume_scorer":
                        resume.append(cid)
                        target = raw_root / "calls" / route.replace("/", "--") / arm / stage / cid
                        target.mkdir(parents=True, exist_ok=True)
                        (target / "audit").symlink_to(base / "audit", target_is_directory=True)
                        if (base / "prediction.json").is_file():
                            (target / "prediction.json").write_bytes((base / "prediction.json").read_bytes())
            snapshot["workers"][key] = {"route": route, "arm": arm, "cases": records}
            workers.append({"route": route, "arm": arm, "case_ids": remaining, "prior_case_ids": prior, "resume_scorer_case_ids": resume})
        directory = self.root / ("frozen-" + stage + "-" + route.replace("/", "--"))
        snapshot_path = directory / "snapshot.json"
        snapshot_sha = write_json(snapshot_path, snapshot)
        references = {
            "stage": stage, "parent_protocol_path": str(self.parent["path"]),
            "parent_protocol_sha256": self.parent["digest"],
            "parent_plan_path": self.parent["protocol"]["execution"]["plan_path"],
            "parent_plan_sha256": self.parent["plan_sha"], "parent_root": str(self.parent["root"]),
            "prior_snapshot_path": str(snapshot_path), "prior_snapshot_sha256": snapshot_sha,
        }
        plan = {"cases": cases, "workers": workers, "continuation": {"schema": coverage.PLAN_SCHEMA, **references}}
        protocol = copy.deepcopy(self.parent["protocol"])
        protocol["execution"].update(raw_root=str(raw_root), route_order=[route])
        protocol["continuation"] = {"schema": coverage.FREEZE_SCHEMA, **references, "checkpoint_is_completion": False}
        phase = self.freeze(protocol, plan, directory)
        phase["snapshot_path"] = snapshot_path
        self.continuations.append(phase)
        return phase

    def build(self, phases=None):
        return coverage.build_coverage(self.parent["path"], [phase["path"] for phase in self.continuations if phases is None or phase in phases],
                                       science_loader=lambda protocol: self.science)


def group(report, route=SONNET, arm="A", *, stage="dev", phase="unique_union"):
    groups = report["unique_union_groups"] if phase == "unique_union" else report["phase_groups"]
    return next(item for item in groups if (item["phase"], item["auditor_route"], item["arm"], item["stage"]) == (phase, route, arm, stage))


def file_state(root):
    return {
        str(path.relative_to(root)): ("symlink", str(path.readlink())) if path.is_symlink() else ("file", sha(path.read_bytes()))
        for path in root.rglob("*") if path.is_file() or path.is_symlink()
    }


def test_every_active_arm_retains_all399_prepared_and_disabled_are_not_acceptance(tmp_path):
    study = Study(tmp_path, [case("D" + str(index), int(index <= 44)) for index in range(1, 400)])
    report = study.build()
    for route in (SONNET, KIMI):
        for arm in ("A", "B"):
            value = group(report, route, arm)
            assert value["represented_tasks"] == value["prepared_tasks"] == value["quality"]["planned_tasks"] == 399
            assert value["quality"]["planned_keys"] == 44
            assert value["auditor_unsent_backlog"] == 399
            assert value["auditor_dispatched"] == value["scorer_dispatched"] == 0
            assert value["scorer_requirement_known_required"] == 0
            assert value["scorer_requirement_unknown"] == 399
            assert all(gate["observed_recall"] is None for gate in value["quality"]["gates"].values())
    disabled = group(report, DISABLED)
    assert disabled["active"] is False and disabled["disabled_missing_tasks"] == 399
    assert disabled["auditor_unsent_backlog"] == 0
    assert report["acceptance_verdict"] is None
    assert report["disabled_catalog_completeness_is_not_acceptance"] is True
    assert report["all_DEV_complete_including_disabled_catalog"] is False
    assert report["true_population_false_positive_rate"] is None


def test_cached_audits_origin_accounting_scorer_resume_unequal_subsets_and_union(tmp_path):
    study = Study(tmp_path)
    study.final(study.parent, SONNET, "A", "D1", matched=2, eligible=2)
    study.attempt(study.parent, SONNET, "A", "D2")
    study.prediction(study.parent, SONNET, "A", "D2")
    study.missing_outcome(study.parent, SONNET, "A", "D2")
    study.attempt(study.parent, SONNET, "B", "D1")
    study.prediction(study.parent, SONNET, "B", "D1")
    study.final(study.parent, SONNET, "B", "D2")
    study.final(study.parent, SONNET, "B", "D3", matched=0, eligible=0, required=False)
    study.final(study.parent, SONNET, "A", "X1", matched=0, eligible=0, required=False)
    baseline = study.build()
    continuation = study.continuation()
    assert continuation["plan"]["workers"][0]["case_ids"] == ["D2", "D3"]
    assert continuation["plan"]["workers"][1]["case_ids"] == ["D1"]
    study.final(continuation, SONNET, "A", "D2", matched=2, eligible=2)
    study.final(continuation, SONNET, "A", "D3", matched=0, eligible=0, required=False)
    study.final(continuation, SONNET, "B", "D1", matched=2, eligible=2)
    before = file_state(tmp_path)
    report = study.build()
    assert file_state(tmp_path) == before
    assert group(report, phase="phase4") == group(baseline, phase="phase4")
    assert group(report, stage="cross", phase="phase4") == group(baseline, stage="cross", phase="phase4")
    for arm, denominator in (("A", 2), ("B", 1)):
        phase = group(report, arm=arm, phase="phase5-1")
        assert phase["represented_tasks"] == phase["prepared_tasks"] == 3
        assert phase["scheduled_tasks"] == phase["quality"]["planned_tasks"] == denominator
        assert phase["scorer_dispatched"] == 1
        assert phase["auditor_dispatched"] == (1 if arm == "A" else 0)
        total = group(report, arm=arm)
        assert total["fully_union_scored"] == total["auditor_dispatched"] == 3
        assert total["quality"]["full_cohort_complete"] is True
        assert total["quality"]["planned_keys"] == 5
        assert total["quality"]["gates"][GATES[0]]["full_cohort_recall"] is not None
    cached = [attempt for attempt in report["native_attempts"] if attempt["aliases"]]
    assert len(cached) == 2
    assert all(attempt["origin_phase"] == "phase4" and attempt["role"] == "audit" for attempt in cached)
    assert len({attempt["directory"] for attempt in report["native_attempts"]}) == 11
    assert report["combined_accounting"]["transport_attempts"] == 11
    assert report["combined_accounting"]["input_reported_sum"] == 110
    comparisons = report["same_route_AB_shared_complete"]
    original = next(item for item in comparisons if item["phase"] == "phase4" and item["auditor_route"] == SONNET and item["stage"] == "dev")
    assert original["status"] == "unavailable"
    continued = next(item for item in comparisons if item["phase"] == "phase5-1")
    assert continued["status"] == "unavailable"  # Fully completed but disjoint phase subsets.
    union = next(item for item in comparisons if item["phase"] == "unique_union" and item["auditor_route"] == SONNET and item["stage"] == "dev")
    assert union["shared_case_ids"] == ["D1", "D2", "D3"]
    assert union["shared_complete_tasks"] == 3 and union["shared_evaluable_keys"] == 5
    assert report["active_DEV_auditor_coverage_complete"] is False  # Kimi still has honest backlog.


def test_union_missingness_exact_observed_denominators_and_scorer_only_backlog(tmp_path):
    study = Study(tmp_path, [case("D1", 2), case("D2", 3), case("D3", 0)])
    study.final(study.parent, SONNET, "A", "D1", matched=1, eligible=3, contradictions=1)
    study.attempt(study.parent, SONNET, "A", "D2")
    study.prediction(study.parent, SONNET, "A", "D2")
    study.missing_outcome(study.parent, SONNET, "A", "D2")
    report = study.build()
    value = group(report)
    assert value["quality"]["planned_keys"] == 5 and value["quality"]["evaluable_keys"] == 2
    assert value["fully_union_scored"] == 1 and value["terminal_consumed_failures"] == 0
    assert value["auditor_dispatched"] == value["auditor_answered"] == 2
    assert value["scorer_dispatched"] == value["scorer_only_unsent_backlog"] == value["auditor_unsent_backlog"] == 1
    assert value["scorer_requirement_known_required"] == 2
    assert value["scorer_requirement_unknown"] == 1
    for gate in value["quality"]["gates"].values():
        assert gate["observed_recall"] == 0.5 and gate["full_cohort_recall"] is None
        assert gate["source_contradiction_proportion"] == pytest.approx(1 / 3)
        assert gate["full_cohort_source_contradiction_proportion"] is None
        assert gate["true_population_false_positive_rate"] is None
    assert "D2" in value["quality"]["missing_tasks"] and "D3" in value["quality"]["missing_tasks"]


def test_ab_intersection_exact_keys_does_not_pool_other_routes_or_unshared_cells(tmp_path):
    study = Study(tmp_path, [case("D1", 2), case("D2", 3), case("D3", 5)])
    for route, arm, cid in ((SONNET, "A", "D1"), (SONNET, "A", "D2"), (SONNET, "B", "D2"), (SONNET, "B", "D3"), (KIMI, "A", "D3")):
        study.final(study.parent, route, arm, cid)
    report = study.build()
    shared = next(item for item in report["same_route_AB_shared_complete"] if item["phase"] == "unique_union" and item["auditor_route"] == SONNET and item["stage"] == "dev")
    assert shared["shared_case_ids"] == ["D2"] and shared["shared_evaluable_keys"] == 3
    assert shared["A"]["planned_tasks"] == shared["B"]["planned_tasks"] == 1
    assert shared["A"]["gates"][GATES[0]]["observed_recall"] == pytest.approx(1 / 3)
    kimi = next(item for item in report["same_route_AB_shared_complete"] if item["phase"] == "unique_union" and item["auditor_route"] == KIMI and item["stage"] == "dev")
    assert kimi["status"] == "unavailable" and kimi["A"] is None and kimi["B"] is None


def test_unknown_usage_marker_and_owned_only_orphans_are_consumed_without_fake_receipts(tmp_path):
    study = Study(tmp_path)
    marker = study.attempt(study.parent, SONNET, "A", "D1", marker_only=True)
    owned_only = study.base(study.parent, SONNET, "A", "D2") / "audit"
    owned_only.mkdir(parents=True)
    (owned_only / "sessions").mkdir()  # An empty owned directory is consumption too.
    study.final(study.parent, SONNET, "B", "D3", matched=0, eligible=0, required=False)
    before = file_state(tmp_path)
    report = study.build()
    assert file_state(tmp_path) == before
    value = group(report)
    assert value["auditor_dispatched"] == value["terminal_consumed_failures"] == 2
    assert value["auditor_answered"] == 0
    assert report["combined_accounting"]["input_tokens"] is None
    assert report["combined_accounting"]["input_reported_sum"] == 10
    assert report["combined_accounting"]["input_missing_usage_attempts"] == 2
    assert len(study.transport.recovered) == 2
    assert not (marker / "receipt.json").exists() and not (owned_only / "receipt.json").exists()


def test_duplicate_consumed_role_rejected_even_with_identical_receipt_bytes(tmp_path):
    study = Study(tmp_path)
    original = study.attempt(study.parent, SONNET, "A", "D1", status="unanswered")
    continuation = study.continuation()
    duplicate = study.base(continuation, SONNET, "A", "D1") / "audit"
    duplicate.mkdir(parents=True)
    for path in original.iterdir():
        if path.is_file():
            (duplicate / path.name).write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="duplicate consumed role"):
        study.build()


def test_aliasing_a_native_attempt_across_different_cells_is_rejected(tmp_path):
    study = Study(tmp_path)
    original = study.attempt(study.parent, SONNET, "A", "D1")
    alias = study.base(study.parent, SONNET, "A", "D2") / "audit"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="duplicate native consumption"):
        study.build()


def test_terminal_consumed_failure_cannot_be_overwritten_or_added_to_remaining(tmp_path):
    study = Study(tmp_path)
    study.attempt(study.parent, SONNET, "A", "D1", status="unanswered")
    continuation = study.continuation()
    value = study.build()
    assert group(value)["terminal_consumed_failures"] == 1
    assert next(item for item in group(value)["slots"] if item["case_id"] == "D1")["phase"] == "phase4"
    worker = continuation["plan"]["workers"][0]
    worker["prior_case_ids"].remove("D1")
    worker["case_ids"].insert(0, "D1")
    study.freeze(continuation["protocol"], continuation["plan"], continuation["path"].parent)
    with pytest.raises(ValueError, match="prior subset"):
        study.build()


def test_empty_union_without_final_cell_remains_missing_and_needs_no_native_scorer(tmp_path):
    study = Study(tmp_path)
    study.attempt(study.parent, SONNET, "A", "D1")
    study.prediction(study.parent, SONNET, "A", "D1", required=False)
    report = study.build()
    value = group(report)
    assert value["fully_union_scored"] == value["scorer_only_unsent_backlog"] == value["scorer_dispatched"] == 0
    assert value["classification_counts"]["answered_unfinalized_no_scorer_required"] == 1
    assert value["scorer_requirement_known_not_required"] == 1
    assert value["quality"]["gates"][GATES[0]]["observed_recall"] is None


@pytest.mark.parametrize("target", ["protocol", "plan", "snapshot"])
def test_frozen_digest_drift_rejected_before_science_use(tmp_path, target):
    study = Study(tmp_path)
    continuation = study.continuation()
    path = {
        "protocol": continuation["path"],
        "plan": Path(continuation["protocol"]["execution"]["plan_path"]),
        "snapshot": continuation["snapshot_path"],
    }[target]
    path.write_bytes(path.read_bytes() + b"\n")
    loader = Mock(side_effect=AssertionError("science must not be used"))
    with pytest.raises(ValueError, match="digest drift"):
        coverage.build_coverage(study.parent["path"], [continuation["path"]], science_loader=loader)
    loader.assert_not_called()


def test_snapshot_owned_artifact_drift_rejected_before_native_inspection(tmp_path):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    study.prediction(study.parent, SONNET, "A", "D1")
    study.continuation()
    (native / "system.txt").write_bytes(b"changed synthetic system")
    with pytest.raises(ValueError, match="snapshot owned artifact custody"):
        study.build()
    assert study.transport.inspected == study.transport.recovered == []


def test_marker_receipt_request_conflict_is_terminal_not_silently_reused(tmp_path):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    receipt = json.loads((native / "receipt.json").read_bytes())
    receipt["timeout_seconds"] = 174
    write_json(native / "receipt.json", receipt)
    report = study.build()
    assert group(report)["terminal_consumed_failures"] == 1
    assert group(report)["auditor_answered"] == 0
    assert "marker_receipt_request_custody_drift" in report["native_attempts"][0]["custody_errors"]
    assert report["combined_accounting"]["input_tokens"] is None


def test_invalid_receipt_marker_hash_recovers_terminal_unknown_usage(tmp_path):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    receipt = json.loads((native / "receipt.json").read_bytes())
    receipt["call_consumed_sha256"] = "0" * 64
    write_json(native / "receipt.json", receipt)
    report = study.build()
    assert group(report)["terminal_consumed_failures"] == 1
    assert group(report)["auditor_answered"] == 0
    attempt = report["native_attempts"][0]
    assert attempt["complete_native_receipt"] is False
    assert attempt["receipt"]["tokens_in"] is None
    assert "consumption_marker_hash_drift" in attempt["custody_errors"]


def test_original_cross_unsent_only_fill_has_separate_phase_and_preserves_phase4(tmp_path):
    cases = [case("D1")] + [case("X" + str(index), stage="cross", prepared=index <= 12) for index in range(1, 16)]
    study = Study(tmp_path, cases)
    study.final(study.parent, SONNET, "A", "X1", matched=0, eligible=0, required=False)
    study.attempt(study.parent, SONNET, "A", "X2", status="unanswered")
    original = study.build()
    dev = study.continuation(stage="dev")
    cross = study.continuation(stage="cross")
    assert dev["root"] != cross["root"]
    actions = json.loads(cross["snapshot_path"].read_bytes())["workers"][SONNET.replace("/", "--") + "__A"]["cases"]
    assert actions["X1"]["action"] == actions["X2"]["action"] == "prior_terminal"
    assert all(actions["X" + str(index)]["action"] == "unavailable_input" for index in (13, 14, 15))
    assert all(not actions["X" + str(index)]["audit"]["consumed"] for index in (13, 14, 15))
    study.final(cross, SONNET, "A", "X3", matched=0, eligible=0, required=False)
    report = study.build()
    assert group(report, stage="cross", phase="phase4") == group(original, stage="cross", phase="phase4")
    total = group(report, stage="cross")
    assert total["represented_tasks"] == total["quality"]["planned_tasks"] == 15
    assert total["prepared_tasks"] == 12 and total["unavailable_input_tasks"] == 3
    assert total["auditor_dispatched"] == 3 and total["terminal_consumed_failures"] == 1
    assert total["fully_union_scored"] == 2
    assert group(report, stage="cross", phase="phase5-2")["scheduled_tasks"] == 10
    assert group(report, stage="cross", phase="phase5-2")["auditor_dispatched"] == 1
    assert total["quality"]["full_cohort_complete"] is False
    assert group(report, stage="dev", phase="phase4") == group(original, stage="dev", phase="phase4")


def test_changed_original_stage_cohort_or_science_is_not_admitted(tmp_path):
    study = Study(tmp_path)
    continuation = study.continuation()
    continuation["plan"]["cases"] = copy.deepcopy(continuation["plan"]["cases"])
    continuation["plan"]["cases"][0]["key_count"] += 1
    study.freeze(continuation["protocol"], continuation["plan"], continuation["path"].parent)
    with pytest.raises(ValueError, match="original stage metadata"):
        study.build()


def test_cli_aggregate_only_is_identifier_body_free_and_private_output_is_complete(tmp_path, monkeypatch, capsys):
    study = Study(tmp_path)
    study.final(study.parent, SONNET, "A", "D1")
    report = study.build()
    # Unrecognized extensions must not leak even if a helper grows private fields.
    report["phase_groups"][0]["quality"]["raw_body"] = "sensitive-extension-body"
    report["combined_accounting"]["private_body"] = "sensitive-extension-body"
    monkeypatch.setattr(coverage, "build_coverage", lambda *args, **kwargs: report)
    output = tmp_path / "complete-private.json"
    assert coverage.main(["--parent-protocol", str(study.parent["path"]), "--aggregate-only", "--private-output", str(output)]) == 0
    public_text = capsys.readouterr().out
    public = json.loads(public_text)
    private_text = output.read_text()
    assert public["schema"] == "oq6.stage-continuation-coverage-summary/v1"
    for private in ("D1", "X1", "synthetic-key-", "sensitive-body-", "sensitive-extension-body", str(tmp_path), "shared_case_ids", "matched_gold_keys"):
        assert private not in public_text
    assert "sensitive-body-D1" in private_text and "synthetic-key-D1" in private_text
    assert json.loads(private_text)["native_attempts"]
    assert public["disabled_strata"] and all(not item["active"] for item in report["unique_union_groups"] if item["auditor_route"] == DISABLED)
    with pytest.raises(FileExistsError):
        coverage.main(["--parent-protocol", str(study.parent["path"]), "--private-output", str(output)])


def test_cli_will_not_write_reports_under_immutable_evidence_roots(tmp_path, monkeypatch):
    study = Study(tmp_path)
    report = study.build()
    monkeypatch.setattr(coverage, "build_coverage", lambda *args, **kwargs: report)
    destination = study.parent["root"] / "report.json"
    with pytest.raises(ValueError, match="outside immutable evidence"):
        coverage.main(["--parent-protocol", str(study.parent["path"]), "--private-output", str(destination)])
    assert not destination.exists()


def test_pending_scorer_resume_preserves_prior_authoritative_missing_quality(tmp_path):
    study = Study(tmp_path)
    study.attempt(study.parent, SONNET, "A", "D1")
    study.prediction(study.parent, SONNET, "A", "D1")
    study.missing_outcome(study.parent, SONNET, "A", "D1")
    outcome = study.parent["root"] / "outcomes" / (SONNET.replace("/", "--") + "__A") / "D1.json"
    record = json.loads(outcome.read_bytes())
    record["paired"].update(parse_status="complete", parsed_findings=1, scorer_required=True)
    write_json(outcome, record)
    study.continuation()
    report = study.build()
    selected = next(item for item in group(report)["slots"] if item["case_id"] == "D1")
    assert selected["row"] == record["paired"]
    assert selected["quality_origin_phase"] == "phase4"
    assert selected["classification"] == "scorer_only_unsent_backlog"
    assert group(report)["fully_union_scored"] == 0


def test_active_completion_can_be_true_while_disabled_catalog_stays_incomplete(tmp_path):
    study = Study(tmp_path, [case("D1", 0)])
    for route in (SONNET, KIMI):
        for arm in ("A", "B"):
            study.final(study.parent, route, arm, "D1", matched=0, eligible=0, required=False)
    report = study.build()
    assert report["active_DEV_auditor_coverage_complete"] is True
    assert report["active_DEV_all_quality_complete"] is True
    assert report["all_DEV_complete_including_disabled_catalog"] is False
    assert report["acceptance_verdict"] is None


def test_complete_same_phase_subset_is_not_full_represented_ab_coverage(tmp_path):
    study = Study(tmp_path)
    for arm in ("A", "B"):
        study.final(study.parent, SONNET, arm, "D1")
        study.final(study.parent, SONNET, arm, "D3", matched=0, eligible=0, required=False)
    continuation = study.continuation()
    for arm in ("A", "B"):
        study.final(continuation, SONNET, arm, "D2")
    report = study.build()
    phase = next(item for item in report["same_route_AB_shared_complete"] if item["phase"] == "phase5-1")
    assert phase["shared_complete_tasks"] == 1 and phase["shared_evaluable_keys"] == 2
    assert phase["not_full_original_stage_coverage"] is True
    assert phase["not_full_ALL_DEV_coverage"] is True
    union = next(item for item in report["same_route_AB_shared_complete"] if item["phase"] == "unique_union" and item["auditor_route"] == SONNET and item["stage"] == "dev")
    assert union["not_full_original_stage_coverage"] is False


def test_production_helper_loader_uses_original_frozen_sources_without_bytecode(tmp_path):
    science_root = tmp_path / "science"
    science_root.mkdir()
    scheduler = tmp_path / "frozen-runtime" / "research/experiments/oq6-task-quality-gate-rerun/runner.py"
    scheduler.parent.mkdir(parents=True)
    scheduler.write_text(
        "from types import SimpleNamespace, ModuleType\n"
        "HELPERS = ('paired', 'transport', 'scorer')\n"
        "def load_science(protocol):\n"
        "    scorer = ModuleType('scorer')\n"
        "    scorer.SCORER_ROUTE = 'synthetic/scorer'\n"
        "    return SimpleNamespace(paired=ModuleType('paired'), transport=ModuleType('transport'), scorer=scorer)\n"
    )
    aggregate = science_root / "aggregate.py"
    aggregate.write_text("def accounting(receipts):\n    return {'synthetic_original_calls': len(receipts)}\n")
    paired_summary = tmp_path / "frozen-runtime" / "result_summary.py"
    paired_summary.write_text("def paired_ab_report(left, right, aggregate):\n    return {'synthetic_pinned_AB': True}\n")
    attestation = science_root / "attestation.py"
    attestation.write_text("# synthetic native attestation source\n")
    contract = science_root / "transport-contract.json"
    contract_sha = write_json(contract, {"session_attestation": str(attestation)})
    protocol = {
        "execution": {"science_root": str(science_root)},
        "code_files": [{"path": str(path), "sha256": sha(path.read_bytes())}
                       for path in (scheduler, aggregate, paired_summary, attestation)],
        "pre_cross_data_files": [{"path": str(contract), "sha256": contract_sha}],
    }
    before = file_state(tmp_path)
    loaded = coverage.load_science(protocol)
    assert loaded.accounting([{}]) == {"synthetic_original_calls": 1}
    assert loaded.paired_ab_report([], [], None) == {"synthetic_pinned_AB": True}
    assert file_state(tmp_path) == before


def test_snapshot_covers_auxiliary_native_files_not_only_consumption_sentinels(tmp_path):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    (native / "native-auxiliary.json").write_text('{"synthetic_metadata":true}\n')
    receipt = json.loads((native / "receipt.json").read_bytes())
    receipt["artifacts"] = artifacts(native)
    write_json(native / "receipt.json", receipt)
    study.prediction(study.parent, SONNET, "A", "D1")
    study.continuation()
    before = file_state(tmp_path)
    report = study.build()
    assert group(report)["auditor_answered"] == group(report)["scorer_only_unsent_backlog"] == 1
    assert any(path.endswith("native-auxiliary.json") for path in report["native_attempts"][0]["owned_files"])
    assert file_state(tmp_path) == before


def test_truncated_prediction_is_terminal_consumed_not_a_scorer_backlog(tmp_path):
    study = Study(tmp_path)
    study.attempt(study.parent, SONNET, "A", "D1")
    study.prediction(study.parent, SONNET, "A", "D1")
    path = study.base(study.parent, SONNET, "A", "D1") / "prediction.json"
    prediction = json.loads(path.read_bytes())
    prediction["raw_findings_count"] = 2
    write_json(path, prediction)
    report = study.build()
    value = group(report)
    assert value["auditor_answered"] == value["terminal_consumed_failures"] == 1
    assert value["scorer_dispatched"] == value["scorer_only_unsent_backlog"] == 0


@pytest.mark.parametrize("raw", [b"{}", b"{"])
def test_orphan_prediction_is_terminal_unconsumed_not_a_quota_backlog(tmp_path, raw):
    study = Study(tmp_path)
    path = study.base(study.parent, SONNET, "A", "D1") / "prediction.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    phase = study.continuation()
    before = file_state(tmp_path)
    report = study.build()
    public = coverage.aggregate_summary(report)
    slot = next(item for item in group(report)["slots"] if item["case_id"] == "D1")
    assert slot["terminal"] is True
    assert slot["classification"] == "terminal_unconsumed_missing"
    assert "D1" not in phase["plan"]["workers"][0]["case_ids"]
    for result in (report, public):
        value = group(result)
        assert value["terminal_unconsumed_missing"] == 1
        assert value["auditor_unsent_backlog"] == value["represented_tasks"] - 1
        assert value["scorer_only_unsent_backlog"] == value["terminal_consumed_failures"] == 0
        assert value["auditor_dispatched"] == value["scorer_dispatched"] == 0
    assert report["native_attempts"] == []
    assert file_state(tmp_path) == before


@pytest.mark.parametrize("changes", [
    [{"source": "native", "identity": SONNET}],
    [{"source": "stdout", "identity": SONNET}, {"source": "native", "identity": SONNET}],
    [{"source": "native", "identity": None}],
])
def test_same_route_initial_selection_preserves_scorer_only_resume(tmp_path, changes):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    receipt = json.loads((native / "receipt.json").read_bytes())
    receipt["model_change_events"] = changes
    write_json(native / "receipt.json", receipt)
    study.prediction(study.parent, SONNET, "A", "D1")
    phase = study.continuation()
    study.final(phase, SONNET, "A", "D1")
    before = file_state(tmp_path)
    report = study.build()
    assert group(report)["auditor_answered"] == group(report)["scorer_answered"] == 1
    assert group(report)["fully_union_scored"] == 1
    assert group(report, phase="phase5-1")["auditor_dispatched"] == 0
    assert group(report, phase="phase5-1")["scorer_dispatched"] == 1
    assert len(report["native_attempts"]) == 2
    assert file_state(tmp_path) == before


@pytest.mark.parametrize("changes", [
    [{"source": "native", "identity": KIMI}],
    [{"identity": SONNET, "resolvedModelIsFallback": True}],
    [{"identity": SONNET, "resolvedModelIsFallback": "false"}],
    [{}],
    [None],
    None,
])
def test_changed_fallback_or_malformed_selection_stays_terminal(tmp_path, changes):
    study = Study(tmp_path)
    native = study.attempt(study.parent, SONNET, "A", "D1")
    receipt = json.loads((native / "receipt.json").read_bytes())
    receipt["model_change_events"] = changes
    write_json(native / "receipt.json", receipt)
    before = file_state(tmp_path)
    report = study.build()
    assert group(report)["auditor_dispatched"] == group(report)["terminal_consumed_failures"] == 1
    assert group(report)["auditor_answered"] == group(report)["scorer_only_unsent_backlog"] == 0
    assert report["native_attempts"][0]["custody_errors"] == ["answered_receipt_custody_drift"]
    assert file_state(tmp_path) == before
