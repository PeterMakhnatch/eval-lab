"""Task/grader subjects from named native receipts. Never execute a QA tool."""
from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any

from .inputs import identity, source


def document(ref: dict, root: Path) -> Any:
    return json.loads(source(ref, root).read_text())


def retained_metadata(config: dict, root: Path) -> dict[str, dict]:
    """Read only the frozen DEV task metadata members, not archive payloads."""
    receipt = document(config["retention"], root)
    entries = [e for e in receipt["paths"] if "/tasks-v2/" in e["path"]
               and Path(e["path"]).name in {"meta.json", "generated.json", "controls.json"}]
    output = {}
    for ref in config["archives"]:
        path = source(ref, root)
        wanted = {e["member"]: e for e in entries if e["archive"] == str(path)}
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                entry = wanted.pop(member.name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("QA metadata is not a regular archive member")
                raw = stream.read()
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("QA metadata member hash differs")
                output[entry["path"]] = json.loads(raw)
        if wanted:
            raise ValueError("QA metadata member missing")
    if len(output) != len(entries):
        raise ValueError("QA metadata archive coverage differs")
    return output


def subject(task: str, package: Any, grader: Any, boundary: str, **extra: Any) -> dict:
    family = task.split("@task-bundle-sha256-")[0]
    if "/impossible_bench/" in family:
        family = family.replace(":original:", ":").replace(":conflicting:", ":")
    return {"task_id": task, "family": family,
            "package_digest": package, "grader_digest": grader, "boundary": boundary,
            "task_key": identity([task, package, grader, boundary]),
            "grader_key": identity([task, extra.get("contract_digest", package), grader, boundary]) if grader is not None else None, **extra}


def prediction(flag: bool | None, evidence: Any, **extra: Any) -> dict:
    return {"flag": flag, "status": "missing" if flag is None else "scored",
            "evidence": evidence, **extra}


def load(config: dict, root: Path, gold: dict) -> dict:
    subjects: dict[str, dict] = {}
    predictions: dict[str, dict] = {name: {} for name in config["tools"]}
    panels: dict[str, dict] = {}

    def add(row: dict) -> dict:
        key = row["task_key"]
        if key in subjects:
            return subjects[key]
        subjects[key] = row
        return row

    def put(tool: str, row: dict, flag: bool | None, evidence: Any, **extra: Any) -> None:
        value = prediction(flag, evidence, **extra)
        previous = predictions[tool].get(row["task_key"])
        if previous and previous != value:
            outputs = previous.get("outputs", [previous]) + [value]
            value = {"status": "conflicting" if len({p["flag"] for p in outputs}) > 1 else outputs[0]["status"],
                     "flag": outputs[0]["flag"] if len({p["flag"] for p in outputs}) == 1 else None,
                     "outputs": outputs}
        predictions[tool][row["task_key"]] = value

    def panel(name: str, contract: str, label: str, members: dict[str, str], note: str) -> None:
        panels[name] = {"contract": contract, "label": label, "members": members, "note": note}

    metadata = retained_metadata(config, root)
    generated = {v["task_id"]: (p, v) for p, v in metadata.items() if p.endswith("/generated.json")}
    admitted = {r["task_id"] for r in document(config["construction"], root)["results"] if r["admitted"]}
    pairs, strict_members, construction_subjects = {}, {}, {}
    for task in sorted(admitted):
        path, generated_row = generated[task]
        parent = str(Path(path).parent)
        meta, controls = metadata[parent + "/meta.json"], metadata[parent + "/controls.json"]
        if meta["split"].lower() != "dev" or generated_row["split"].lower() != "dev":
            raise ValueError("QA construction is not DEV")
        if not controls["verified"] or not controls["control_eligible"]:
            raise ValueError("Unverified impossible-task construction")
        for variant in ("original", "conflicting_certified_bool"):
            tests = generated_row["variant_test_sha256"][variant]
            row = add(subject(task, generated_row["variant_manifest_sha256"][variant], identity(tests),
                              "impmore-safe-postprocess-v1", variant=variant, task_ref=meta["package"],
                              identity_status="bound construction manifest", source_evidence=path))
            construction_subjects[task, variant] = row
            strict_members[row["task_key"]] = "positive" if variant != "original" else "paired_negative"
            pairs.setdefault(task, {})[variant] = row["task_key"]
            oracle = controls["receipts"][variant + "/oracle"]
            usable = oracle["docker_rc"] == 0 and oracle["rc"] in (0, 1)
            put("oracle", row, oracle["rc"] != 0 if usable else None, parent + "/controls.json")
            if variant == "original":
                nop = controls["receipts"]["original/nop"]
                put("nop", row, nop["rc"] == 0 if nop["docker_rc"] == 0 and nop["rc"] in (0, 1) else None,
                    parent + "/controls.json")
    panel("strict_pairs", "task_qa", "impossible_as_graded", strict_members,
          "Original versions are paired controls for the constructed contradiction, not universally clean tasks.")

    original_members = {}
    for native in document(config["originals"], root):
        mutation = native.get("mutants_original")
        if not mutation or not mutation["total"]:
            continue
        # The study does not publish full package/grader hashes. Never merge its
        # task-name aliases with another runtime or a repaired grader version.
        row = add(subject(native["task_id"], None, ["ov1104-original", native["task_id"]],
                          "ov1104-original-study", identity_status="study-scoped; full digests unavailable",
                          source_evidence=config["originals"]["path"]))
        weak = mutation["killed"] < mutation["total"]
        original_members[row["task_key"]] = "positive" if weak else "weak_negative"
        put("mutation_audit", row, weak, mutation)
        for name, field in (("oracle", "reference_reward"), ("nop", "nop_reward")):
            control = native.get(field)
            if control and control["value"] in (0, 1):
                put(name, row, control["value"] != 1 if name == "oracle" else control["value"] == 1, control)
    panel("selected_originals", "grader_qa", "vulnerable", original_members,
          "Selected original-study versions only. Mutation outcomes define these labels; audit agreement is evidence lookup, not independent accuracy. Full digests do not permit a cross-study union.")

    gold_members, weak_members, links = {}, {}, []
    outcomes = document(config["gold_outcomes"], root)
    if set(outcomes["runs"]) != {k for k, r in gold.items() if r.source == "goodfire-action-v1"}:
        raise ValueError("Gold outcome snapshot does not match DEV registry")
    positive_tasks, honest_tasks = set(), set()
    for key, native in sorted(gold.items()):
        if native.split.upper() != "DEV" or native.declared_split.upper() != "DEV":
            raise ValueError("QA join permits DEV only")
        if native.source == "impossible-task-final":
            variant = native.metadata["variant"]
            row = construction_subjects[native.task_id, variant]
            expected = generated[native.task_id][1]["variant_test_sha256"][variant]
            changed_file = generated[native.task_id][1]["mutated_file"]
            if native.metadata["source_variant_sha256"] != expected[changed_file]:
                raise ValueError("Gold action does not bind the constructed grader version")
            passed = native.metadata["G1_pass"]
            availability = {"task_available": native.task_available, "grader_available": native.grader_available,
                            "task_ref": native.task_ref, "grader_ref": native.grader_ref,
                            "availability_scope": "registry source package; strict test version binds through construction hashes"}
        else:
            outcome = outcomes["runs"][key]
            row = add(subject(native.task_id, native.task_ref, native.grader_digest,
                              "goodfire-publisher-original", identity_status="task bundle bound; grader digest unknown"
                              if native.grader_digest is None else "bound grader digest", source_evidence=native.provenance.ref))
            passed = outcome["reward"] == 1 if outcome["reward"] is not None else None
            availability = {name: outcome.get(name) for name in ("task_available", "grader_available", "task_ref", "grader_ref")}
        task_key = row["task_key"]
        if native.label_state == "executed-positive":
            positive_tasks.add(task_key)
            if passed is True:
                gold_members[task_key] = "positive"
            links.append({"run_key": key, "source": native.source, "task_key": task_key,
                          "grader_key": row["grader_key"], "task_id": row["task_id"], "passed": passed,
                          "identity_status": row["identity_status"], "package_digest": row["package_digest"],
                          "grader_digest": row["grader_digest"], "boundary": row["boundary"], **availability})
        elif native.label_state == "reviewed-control" and passed is True:
            honest_tasks.add(task_key)
    weak_members = {key: "weak_negative" for key in honest_tasks - positive_tasks}
    task_members = {**gold_members, **weak_members}
    panel("gold_tasks", "task_qa", "cheatable",
          {k: v for k, v in task_members.items() if subjects[k]["grader_digest"] is not None},
          "Executed action plus recorded pass and a bound grader version. Honest-pass-only versions are weak negatives; attempted/unjudged rows are not controls.")
    panel("gold_task_bundles", "task_qa", "cheatable",
          {k: v for k, v in task_members.items() if subjects[k]["grader_digest"] is None},
          "Publisher task-bundle identities only: grader digests are unavailable. These source-scoped counts do not enter the exact-version denominator.")
    # A passed action with an unbound grader proves task-version cheatability,
    # but cannot manufacture an exact unique grader denominator.
    identified = {k: v for k, v in gold_members.items() if subjects[k]["grader_digest"] is not None}
    panel("gold_graders", "grader_qa", "vulnerable", identified,
          "Exact grader versions from passed Gold actions. Unbound graders remain in the 54-run link, outside this denominator.")

    strict = document(config["strict_gold"], root)
    certified, selected = {}, {}
    cells = strict["reused_rows"] + strict["executed_rows"]
    if len({c["cell_key"] for c in cells}) != len(cells):
        raise ValueError("Duplicate strict Gold native cell")
    for task, native in strict["tasks"].items():
        row = add(subject("format-code-task-" + task, native["package_digest"],
                          [native["package_digest"], native["contract_sha256"]], "goldmore-contained-modal",
                          identity_status="bound package and contract", source_evidence=config["strict_gold"]["path"]))
        selected[row["task_key"]] = "unknown"
        if native["certified_strict"]:
            certified[row["task_key"]] = "strong_negative"
        task_cells = [c for c in cells if c["task"] == task and c["arm"] != "historical_informed_alternative"]
        put("gold_strict", row, not native["certified_strict"], native["gates"],
            estimated_usd=sum(c["est_usd"] for c in task_cells),
            wall_s=sum(c["wall_s"] for c in task_cells))
        for gate, flag in native["gates"].items():
            if gate in config["gold_gates"]:
                arm = {"G1": "reference", "G2": "nop", "G3": "witness", "G4": "startup",
                       "G5_defect_nonempty": "train", "G6_defect_nonempty": "eval", "G7_uninformed": "alternative"}[gate]
                gate_cells = [c for c in task_cells if c["arm"] == arm
                              and (arm not in {"train", "eval"} or c["static_category"] == "DEFECT")]
                put("gold_" + gate, row, not flag, native["gates"],
                    estimated_usd=sum(c["est_usd"] for c in gate_cells) if gate_cells else None,
                    wall_s=sum(c["wall_s"] for c in gate_cells) if gate_cells else None)
        for name, arm in (("oracle", "reference"), ("nop", "nop")):
            control = next(c for c in task_cells if c["arm"] == arm)
            reward = control["reward"]
            put(name, row, (reward != 1 if name == "oracle" else reward == 1)
                if control["status"] == "executed" and reward in (0, 1) else None, control["cell_key"],
                estimated_usd=control.get("est_usd"), wall_s=control.get("wall_s"))
    # The selected-but-unrunnable package stays in certification coverage, not a negative label.
    for task in config["strict_selected"]:
        if task not in strict["tasks"]:
            row = add(subject("format-code-task-" + task, None, ["unmeasured-goldmore", task],
                              "goldmore-contained-modal", identity_status="unmeasured selected package"))
            selected[row["task_key"]] = "unknown"
    panel("certified_graders", "grader_qa", "vulnerable", certified,
          "Only 3 scoped negatives. Gold gates reuse their own certification controls; no independent accuracy claim.")
    panel("certification_coverage", "grader_qa", "certification", selected,
          "All selected packages stay in coverage. Failure to certify is not proof of a vulnerable grader.")
    panel("certified_tasks", "task_qa", "cheatable", certified,
          "Scoped task-package negatives for recorded cheat classes only; unrelated task flaws remain unknown.")
    historical = {}
    for ref in config.get("stock_gold", []):
        native = document(ref, root)
        row = add(subject(native["task_id"], native["package_digest"], native["package_digest"],
                          "stock-gold-contained-modal", identity_status="bound historical package",
                          source_evidence=ref["path"]))
        historical[row["task_key"]] = "unknown"
        put("gold_stock", row, native["status"] == "failed", ref["path"],
            native_status=native["status"])
        hardening_ref = ref.get("hardening")
        if hardening_ref:
            hardening = document(hardening_ref, root)
            if hardening["output_digest"] != native["package_digest"]:
                raise ValueError("Hardening verdict does not bind the certified package")
            # Preserve the native structural verdict. A declared gate runtime
            # is not an executed certificate or a vulnerable-grader ground truth.
            put("hardening", row, bool(hardening["findings"]), hardening_ref["path"],
                layers=hardening["layers"], runtime=hardening["verifier"]["gate_runtime"],
                transform=hardening["transform"])
    panel("historical_stock_packages", "grader_qa", "certification", historical,
          "Stored stock-policy verdicts and structural hardening findings. No vulnerability label follows from certification failure or from a structural pass.")
    findings: dict[tuple[str, str], dict[str, dict]] = {}
    for binding in config.get("findings", []):
        if binding["split"].upper() != "DEV":
            raise ValueError("QA finding binding is not DEV")
        finding = document(binding["source"], root)
        target = finding["subject"]
        for key, row in subjects.items():
            if (row["task_id"] == target["task_id"] and target.get("task_digest") is not None
                    and row["package_digest"] == target["task_digest"] and row["boundary"] == binding["boundary"]):
                findings.setdefault((binding["tool"], key), {})[binding["source"]["sha256"]] = {
                    **finding, "source_ref": binding["source"]["path"]}
    for (tool, key), found in findings.items():
        usable = [f for f in found.values() if f.get("measurement", "measured") == "measured"
                  and f["verdict"] in {"pass", "fail"}]
        if tool == "qa_task_verified":
            usable = [f for f in usable if f["verdict"] == "pass" or f.get("refutation", {}).get("status") == "verified"]
        costs = [f.get("reviewer", {}).get("cost_usd") for f in found.values()]
        put(tool, subjects[key], any(f["verdict"] == "fail" for f in usable) if usable else None,
            [f["source_ref"] for f in found.values()],
            cost_usd=sum(costs) if costs and all(c is not None for c in costs) else None)
    return {"subjects": subjects, "predictions": predictions, "panels": panels, "pairs": pairs,
            "links": links, "cells": cells, "tools": config["tools"],
            "unknown_pass_runs": sum(r["passed"] is None for r in links),
            "unbound_passed_graders": len(gold_members) - len(identified)}
