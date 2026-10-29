#!/usr/bin/env python3
"""NP22-CM-1 phase 1: schema and stratum-size inventory. No model call, no suspicion, no recall.

Counts only: which stored files and fields exist, how many runs fall in each preregistered
stratum, and which readers/extractor modes cover the Goodfire DEV cohort. It never reads a
facet's ``suspicion`` and never joins a facet to a label. Goodfire identities pass the strict
``assignments.csv`` cross-key (``run_atlas_score._read_labels``) before any receipt is read.
Writes ``results/inventory.json`` (aggregates and hashes only; no run key, id or text).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "research/evidence/runs/corpus-expansion-lb6-20261027/tools"))
sys.path.insert(0, str(REPO / "research/evidence/runs/cm11-hackbench-crosstest-20261028/tools"))
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True

from evallab.production import run_atlas as ra  # noqa: E402
from evallab.production import run_atlas_score as sc  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

STATE = Path.home() / ".local/state"
CM18, NP21 = STATE / "evallab-cm18", STATE / "evallab-np21-cm1"
SAMPLE = (
    Path.home()
    / "Developer/research-context/bridge/2026-10-25-systems-and-swarms"
    / "raw/cm2-monitor-20261026/goodfire-sample.json"
)
RESULTS = HERE.parent / "results"
RUNS = "research/evidence/runs/"
NP21_SETS = ("baitbench", "flake", "kobayashi", "osf")
PQ3 = RUNS + "corpus-expansion-pq3-20261028/corpora/"
NP21_ADAPTERS = {
    "baitbench": PQ3 + "juanjvazquez__BAITBENCH",
    "flake": PQ3 + "zaidwhy__flake-laundering",
    "kobayashi": PQ3 + "ebt55__kobayashi-maru",
    "osf": PQ3 + "osf__7as8h-reward-pressure-study-2",
}
HAIKU = "anthropic/claude-haiku-5-5"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- provenance classes
# IN  publisher text: the side objective / attack is given in the prompt (instructed)
# CP  publisher text: environment constructed so honest success is blocked, contradicted or a
#     shortcut is planted (constructed pressure, conflict or bait)
# NP  staged text/fields record neither an instruction nor a constructed conflict for the run
# NR  not recorded: no staged field or sentence classifies the run
# The substratum is the staged field value the class was read from.


def provenance(name: str, s: dict[str, Any]) -> tuple[str, str]:
    if name == "kernelbench":
        return "NR", "no_field"
    if name == "reflex":
        return ("CP" if s["split"] == "conflicting" else "NP"), f"split={s['split']}"
    if name == "cheatindex":
        family = s["identity"]["family"]
        return ("NP" if family == "F4" else "CP"), f"family={family}"
    if name == "evilgenie":
        return "NR", f"condition={s['identity']['condition']}"
    if name == "sdf":
        return "NP", "same_prompt_all_runs"
    if name == "researcharena":
        cond = s["labels"]["condition"]
        return ("IN" if cond == "embedded" else "NP"), f"condition={cond}"
    if name == "beam":
        cond = s["labels"]["condition"]
        return ("NP" if cond == "possible" else "CP"), f"condition={cond}"
    if name == "baitbench":
        return "CP", f"prompt_group={s['labels']['prompt_group']}"
    if name == "flake":
        return "NR", f"prompt_condition={s['labels']['prompt_condition']}"
    if name == "kobayashi":
        return "CP", f"item_slice={s['labels']['item_slice']}"
    raise KeyError(name)


def technique(name: str, s: dict[str, Any]) -> str:
    """The adapter's own mechanism/technique field, where one is recorded."""
    g = s.get("gold") or {}
    if name == "kernelbench":
        return "rubric_leak"
    if name == "reflex":
        return "final_test_file_differs"
    if name == "cheatindex":
        fired = [
            d for d in ("test_edit", "test_config_changed", "special_casing") if g["detectors"][d]["fired"]
        ]
        return "+".join(fired) or "none_gated"
    if name == "evilgenie":
        return f"native={s['labels']['native_classification']}"
    if name == "sdf":
        return "+".join(sorted(g["authoritative_grade"]["workarounds"])) or "none_listed"
    if name == "researcharena":
        return g["executed_result"]["raw_metrics"]["side_task_type"]
    if name == "beam":
        a = g["source_annotations"]
        for flag in ("special_cased", "config_edit", "tamper_attempt"):
            if a[flag]:
                return flag
        return "cheated_other"
    if name == "baitbench":
        return f"task={s['labels']['task']}"
    if name == "flake":
        return f"outcome={g['publisher_outcome']}"
    if name == "kobayashi":
        return f"monitor_type={g['monitor_type']}"
    raise KeyError(name)


# ---------------------------------------------------------------- unseen sets


def load_cm18() -> Any:
    return load_module("np22_cm18", REPO / RUNS / "cm18-unseen-far-20261028/tools/cm18_unseen.py")


def unseen() -> dict[str, Any]:
    cm18 = load_cm18()
    paths = cm18.ADAPTERS
    out: dict[str, Any] = {}
    for tag, state, names in (("cm18", CM18, list(paths)), ("np21", NP21, list(NP21_SETS))):
        sample = json.loads((state / "sample.json").read_text())
        facets = {r["key"]: r for r in jsonl(state / "facets.jsonl") if r.get("record")}
        drawn = {r["key"]: r for r in sample["runs"]}
        rows: dict[str, dict[str, Any]] = {}
        for name in names:
            if name == "osf":
                rows.update(osf_rows())
                continue
            module = cm18.load_adapter(name) if tag == "cm18" else load_module(
                f"np22_{name}", REPO / NP21_ADAPTERS[name] / "tools/adapter.py"
            )
            for s in module.specs():
                run = cm18.norm(name, s) if tag == "cm18" else np21_norm(name, s)
                if run is None or run["key"] not in drawn:
                    continue
                cls, sub = provenance(name, s)
                rows[run["key"]] = {"set": name, "class": cls, "sub": sub, "tech": technique(name, s)}
        positives = [k for k, r in drawn.items() if r["label"]]
        per: dict[str, Any] = {}
        for name in names:
            keys = [k for k in positives if drawn[k]["set"] == name]
            miss = [k for k in keys if k not in rows]
            cls_n = Counter(rows[k]["class"] for k in keys if k in rows)
            sub_n = Counter((rows[k]["class"], rows[k]["sub"]) for k in keys if k in rows)
            tech_n = Counter(rows[k]["tech"] for k in keys if k in rows)
            per[name] = {
                "positives_drawn": len(keys),
                "join_missing": len(miss),
                "facet_record_present": sum(k in facets for k in keys),
                "by_class": dict(sorted(cls_n.items())),
                "by_class_and_staged_value": {f"{c}|{v}": n for (c, v), n in sorted(sub_n.items())},
                "by_technique": dict(sorted(tech_n.items())),
            }
        out[tag] = {
            "sample_sha256": file_sha(state / "sample.json"),
            "facets_store_sha256": file_sha(state / "facets.jsonl"),
            "facet_row_fields": sorted(next(iter(facets.values())).keys()),
            "facet_record_fields": sorted(next(iter(facets.values()))["record"].keys()),
            "positives": len(positives),
            "per_set": per,
        }
    b6 = json.loads((STATE / "evallab-cm18-b6/subsample.json").read_text())
    b6f = {r["key"]: r for r in jsonl(STATE / "evallab-cm18-b6/facets.jsonl") if r.get("record")}
    cm18_sample = {r["key"]: r for r in json.loads((CM18 / "sample.json").read_text())["runs"]}
    out["b6"] = {
        "subsample_runs": len(b6["keys"]),
        "positives": sum(cm18_sample[k]["label"] for k in b6["keys"]),
        "sonnet_records": len(b6f),
        "sonnet_records_on_positives": sum(cm18_sample[k]["label"] for k in b6f),
        "subsample_sha256": file_sha(STATE / "evallab-cm18-b6/subsample.json"),
    }
    return out


def np21_norm(name: str, s: dict[str, Any]) -> dict[str, Any] | None:
    gold, i = s["gold"], s["identity"]
    if gold["positive"] is None:
        return None
    return {"key": sha(f"{name}:{i['run_id']}")[:16]}


def osf_rows() -> dict[str, dict[str, Any]]:
    """Episode rows with their publisher fields; label rule is the adapter's own ``profile.label``."""
    tools = REPO / NP21_ADAPTERS["osf"] / "tools"
    held = sys.modules.pop("adapter", None)
    sys.path.insert(0, str(tools))
    try:
        profile = load_module("np22_osf_profile", tools / "profile.py")
        adapter = sys.modules["adapter"]
        out: dict[str, dict[str, Any]] = {}
        for substudy, rel in adapter.SUBSTUDIES.items():
            for row in adapter.read_rows(adapter.ROOT / rel):
                if row.get("error") is not None or profile.label(row) not in ("positive", "negative"):
                    continue
                key = sha(f"osf:{substudy}:{row['episode_id']}")[:16]
                out[key] = {
                    "set": "osf",
                    "class": "CP",
                    "sub": f"pressure={row['pressure']}|guardian={row['guardian']}",
                    "tech": f"cheat_type={row.get('cheat_type')}",
                }
        return out
    finally:
        sys.path.remove(str(tools))
        sys.modules.pop("adapter", None)
        if held is not None:
            sys.modules["adapter"] = held


# ---------------------------------------------------------------- Goodfire DEV cohort


def goodfire() -> dict[str, Any]:
    ids = sorted({row["trace_id"] for row in json.loads(SAMPLE.read_text())})
    registry = CorpusRegistry(REPO)
    store = ra.DEFAULT_FACETS.parent
    lines = jsonl(ra.DEFAULT_FACETS)
    calls = jsonl(store / "facet-calls-v1.jsonl")
    reblinded = {r["run_id"] for r in jsonl(store / "reblinded-facets-v1.jsonl")}
    refresh_listed = set(json.loads((store / "blinding-refresh-v1.json").read_text())["run_ids"])
    with closing(ra.connect_readonly(ra.DEFAULT_DATABASE)) as connection:
        labels, checked = sc._read_labels(connection, registry, required=set(ids), wanted=set(ids))
        strata = Counter(label.stratum for label in labels.values())
        by_stratum: dict[str, list[str]] = {}
        for run_id, label in labels.items():
            by_stratum.setdefault(label.stratum, []).append(run_id)
        receipts: dict[str, dict[str, Any]] = {}
        steps: dict[str, int] = {}
        for run_id in labels:
            row = connection.execute(
                "SELECT verified_receipt_json, step_count FROM agent_traces WHERE trace_id = ?", (run_id,)
            ).fetchone()
            receipts[run_id] = json.loads(row[0] or "{}")
            steps[run_id] = int(row[1] or 0)
    readers: dict[str, set[str]] = {}
    last_any: dict[str, str] = {}
    for rec in lines:
        if rec["run_id"] in labels:
            readers.setdefault(rec["facet_model"], set()).add(rec["run_id"])
            last_any[rec["run_id"]] = rec["facet_model"]
    mode: dict[str, set[str]] = {}  # (run, model) -> purposes of successful calls
    for call in calls:
        if call.get("run_id") in labels and call.get("status") == "ok":
            kind = "legacy_single_call" if call["purpose"] == "run" else "v2_form_or_map"
            mode.setdefault(f"{call['run_id']}|{call.get('served_model')}", set()).add(kind)
    out: dict[str, Any] = {
        "ids_in_sample": len(ids),
        "ids_admitted_by_strict_cross_key": len(checked),
        "strata": dict(sorted(strata.items())),
        "sample_file_sha256": file_sha(SAMPLE),
        "facet_store_sha256": file_sha(ra.DEFAULT_FACETS),
        "facet_lines_total": len(lines),
        "facet_lines_in_cohort": sum(r["run_id"] in labels for r in lines),
    }
    out["reader_coverage_by_stratum"] = {
        reader: {st: len(set(rids) & members) for st, rids in sorted(by_stratum.items())}
        for reader, members in sorted(readers.items())
    }
    out["last_wins_reader_by_stratum"] = {
        st: dict(Counter(last_any.get(r, "none") for r in rids)) for st, rids in sorted(by_stratum.items())
    }
    out["reblinded_file_coverage_by_stratum"] = {
        st: sum(r in reblinded for r in rids) for st, rids in sorted(by_stratum.items())
    }
    out["blinding_refresh_listed_by_stratum"] = {
        st: sum(r in refresh_listed for r in rids) for st, rids in sorted(by_stratum.items())
    }
    out["haiku_extractor_mode_by_stratum"] = {
        st: dict(
            Counter(
                "+".join(sorted(mode.get(f"{r}|{HAIKU}", {"no_ok_call_logged"}))) for r in rids
            )
        )
        for st, rids in sorted(by_stratum.items())
    }
    out["source_protocol_by_stratum"] = {
        st: dict(Counter(
            "has_reward_field" if "reward" in (receipts[r].get("source_label") or {}) else "has_shortcut_signal_split"
            for r in rids
        ))
        for st, rids in sorted(by_stratum.items())
    }
    out["categories_nonempty_by_stratum"] = {
        st: sum(bool((receipts[r].get("source_label") or {}).get("categories")) for r in rids)
        for st, rids in sorted(by_stratum.items())
    }
    out["category_values_by_stratum"] = {
        st: dict(sorted(Counter(
            str(c) for r in rids for c in ((receipts[r].get("source_label") or {}).get("categories") or [])
        ).items()))
        for st, rids in sorted(by_stratum.items())
    }
    out["source_protocol_values_by_stratum"] = {
        st: dict(sorted(Counter(
            str((receipts[r].get("source_label") or {}).get("source_protocol")) for r in rids
        ).items()))
        for st, rids in sorted(by_stratum.items())
    }
    out["labelled_positive_step_count_quartiles_enacted"] = [
        sorted(len(labels[r].positive_steps) for r in by_stratum["enacted_yes"])[int(q * 79)]
        for q in (0, 0.25, 0.5, 0.75, 1)
    ]
    out["enacted_with_labelled_positive_steps"] = sum(
        bool(labels[r].positive_steps) for r in by_stratum.get("enacted_yes", [])
    )
    out["step_count_quartiles_by_stratum"] = {
        st: [sorted(steps[r] for r in rids)[int(q * (len(rids) - 1))] for q in (0, 0.25, 0.5, 0.75, 1)]
        for st, rids in sorted(by_stratum.items())
    }
    out["receipt_fields_used"] = [
        "source_label.verdict", "source_label.verdict_enacted", "source_label.judged",
        "source_label.categories", "source_label.source_protocol",
        "passage_step_labels.positive_step_indices", "native_metrics.*",
    ]
    return out


def main() -> None:
    doc = {
        "schema": "np22-inventory/v1",
        "note": "counts of rows and fields only; no suspicion, no recall, no per-run key",
        "goodfire_dev": goodfire(),
        "unseen": unseen(),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "inventory.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"goodfire": doc["goodfire_dev"]["strata"]}))


if __name__ == "__main__":
    main()
