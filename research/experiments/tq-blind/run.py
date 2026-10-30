#!/usr/bin/env python3
"""TQ-BLIND driver: re-adjudicate the TQ-UNVERIFIED units whose prompts named the benchmark, with identity redacted.

Stages (every file is written under STATE; nothing here changes a gate, prompt, cohort or stored score):
  prepare     re-render each stored TQ-UNVERIFIED prompt, select the units that contain an identity token, redact,
              prove with counts that only the tokens changed          -> blind-units.json, redaction-proof.json
  adjudicate  one adjudicator over those units, arm ``redacted`` (primary) or ``control`` (same prompt as
              TQ-UNVERIFIED, test-retest noise baseline); quota-gated, attested, resumable
  score       per-finding flips, per-gate both-real counts, calibration recall, decision rule -> results.json

The adjudicator system prompt, instructions, finding order, opaque ids, call settings and routes are those of
TQ-UNVERIFIED (loaded from ``research/experiments/tq-unverified-alarms``); only the prompt text differs by the
redaction.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "tq-unverified-alarms"


def _load(name: str, path: Path):
    qualified = f"tq_blind_{name}"
    spec = importlib.util.spec_from_file_location(qualified, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


base = _load("base_run", BASE / "run.py")
core, prompts = base.core, base.prompts
redactor = _load("redact", HERE / "redact.py")

SEED = "TQ-BLIND-20261029"
SOURCE_STATE = base.STATE
STATE = Path.home() / ".local/state/evallab-fleet/2026-10-29/TQ-BLIND"
CALL_CAP = 100
ARMS = ("redacted", "control")
WHO = ("sol", "opus")
GATES = ("sonnet", "terra")
STOP_CONSECUTIVE = 3


def source_prompt(unit: dict[str, Any]) -> tuple[str, str]:
    """(source text, rendered adjudicator prompt) exactly as TQ-UNVERIFIED rendered them."""
    source = base.source_text(unit["case_id"])
    return source, prompts.render_adjudicator_prompt(source, unit["candidates"])


def cmd_prepare(_: argparse.Namespace) -> None:
    units = base.read_json(SOURCE_STATE / "units.json")["units"]
    selected, proofs, untouched = [], {}, 0
    for unit in units:
        source, prompt = source_prompt(unit)
        stored = (SOURCE_STATE / "calls/sol" / f"{unit['unit_id']}-1/prompt.txt").read_bytes()
        if stored != prompt.encode():
            raise SystemExit(f"rendered prompt differs from the stored one for {unit['unit_id']}")
        red_prompt, counts = redactor.redact(prompt)
        if not counts:
            untouched += 1
            continue
        proof = redactor.verify(prompt, red_prompt)
        red_source, _ = redactor.redact(source)
        if not (
            proof["segments_identical"]
            and proof["placeholder_counts_equal"]
            and proof["residual_tokens"] == 0
        ):
            raise SystemExit(f"redaction proof failed for {unit['unit_id']}: {proof}")
        selected.append(unit)
        proofs[unit["unit_id"]] = {
            **proof,
            "kind": unit["kind"],
            "class": unit["class"],
            "prompt_sha256": base.sha(prompt.encode()),
            "redacted_prompt_sha256": base.sha(red_prompt.encode()),
            "source_tokens": sum(redactor.redact(source)[1].values()),
            "redacted_source_sha256": base.sha(red_source.encode()),
        }
    totals: Counter[str] = Counter()
    for proof in proofs.values():
        totals.update(proof["per_rule"])
    summary = {
        "units_total": len(units),
        "units_with_tokens": len(selected),
        "units_without_tokens": untouched,
        "kinds": dict(Counter(u["kind"] for u in selected)),
        "tokens_total": sum(totals.values()),
        "tokens_per_rule": dict(totals),
        "all_segments_identical": all(p["segments_identical"] for p in proofs.values()),
        "all_placeholder_counts_equal": all(p["placeholder_counts_equal"] for p in proofs.values()),
        "residual_tokens_total": sum(p["residual_tokens"] for p in proofs.values()),
        "residual_zapier_words_total": sum(p["residual_zapier_words"] for p in proofs.values()),
        "bytes_original": sum(p["bytes_original"] for p in proofs.values()),
        "bytes_redacted": sum(p["bytes_redacted"] for p in proofs.values()),
    }
    base.write_json(STATE / "blind-units.json", {"units": selected})
    base.write_json(STATE / "redaction-proof.json", {"summary": summary, "per_unit": proofs})
    print(json.dumps(summary, indent=1))


def cmd_adjudicate(args: argparse.Namespace) -> None:
    route = core.ROUTES[args.who]
    units = base.read_json(STATE / "blind-units.json")["units"]
    quota = base.Quota(base.PROVIDERS[args.who], STATE / "STOP")
    results_path = STATE / f"adjudication-{args.who}-{args.arm}.json"
    results = base.read_json(results_path) if results_path.exists() else {}
    lock = threading.Lock()
    prior = sum(
        r["calls"]
        for path in STATE.glob(f"adjudication-{args.who}-*.json")
        if path != results_path
        for r in base.read_json(path).values()
    )
    state = {
        "calls": prior + sum(r["calls"] for r in results.values()),
        "consecutive": 0,
        "stopped": None,
    }
    pending = [u for u in units if u["unit_id"] not in results]
    if args.limit:
        pending = pending[: args.limit]

    def work(unit: dict[str, Any]) -> None:
        if state["stopped"]:
            return
        source, prompt = source_prompt(unit)
        if args.arm == "redacted":
            source, _ = redactor.redact(source)
            prompt, _ = redactor.redact(prompt)
        files = core.parse_source(source)
        ids = [c["finding_id"] for c in unit["candidates"]]
        verdicts, receipts, calls = None, [], 0
        for attempt in (1, 2):
            with lock:
                if state["calls"] >= CALL_CAP:
                    state["stopped"] = "call cap"
                    return
                state["calls"] += 1
            try:
                receipt = base.omp_call(
                    quota,
                    route,
                    prompts.ADJUDICATOR_SYSTEM,
                    prompt,
                    STATE / "calls" / args.arm / args.who / f"{unit['unit_id']}-{attempt}",
                    f"tq-blind-{args.arm}",
                )
            except base.Stopped as exc:
                with lock:
                    state["calls"] -= 1
                    state["stopped"] = str(exc)
                return
            calls += 1
            receipts.append(
                {
                    k: receipt.get(k)
                    for k in (
                        "status",
                        "served_identities",
                        "model_fallback",
                        "tool_calls",
                        "refusal",
                        "stop_reason",
                        "usage",
                        "wall_seconds",
                        "error",
                    )
                }
            )
            ok = False
            if receipt["status"] == "answered":
                try:
                    verdicts = prompts.parse_verdicts(receipt["response"], ids)
                    ok = True
                except ValueError as exc:
                    receipts[-1]["parse_error"] = str(exc)
            with lock:
                state["consecutive"] = 0 if ok else state["consecutive"] + 1
                if state["consecutive"] >= STOP_CONSECUTIVE:
                    state["stopped"] = f"{STOP_CONSECUTIVE} consecutive failures to answer"
            if ok or state["stopped"]:
                break
        entry: dict[str, Any] = {"calls": calls, "receipts": receipts, "verdicts": None}
        if verdicts is not None:
            entry["verdicts"] = {}
            for fid, verdict in verdicts.items():
                effective, downgraded = core.effective_verdict(verdict, files)
                entry["verdicts"][fid] = {
                    **verdict,
                    "effective": effective,
                    "downgraded": downgraded,
                    "verified_evidence": core.verified_evidence(verdict["evidence"], files),
                }
        with lock:
            results[unit["unit_id"]] = entry
            base.write_json(results_path, results)
            print(
                f"{args.who}/{args.arm} {len(results)}/{len(units)} calls={state['calls']} "
                f"quota={quota.fraction}",
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(work, pending))
    print(
        json.dumps(
            {
                "done": len(results),
                "units": len(units),
                "calls": state["calls"],
                "stopped": state["stopped"],
            }
        )
    )


# ---------------------------------------------------------------------------- scoring (pure, tested)

VERDICTS = ("real_defect", "not_a_defect", "uncertain")


def transition_matrix(pairs: list[tuple[str | None, str | None]]) -> dict[str, Any]:
    """Counts of (before -> after) verdict pairs; ``None`` is a missing verdict."""
    cells: Counter[tuple[str, str]] = Counter((a or "missing", b or "missing") for a, b in pairs)
    flips = sum(n for (a, b), n in cells.items() if a != b)
    return {
        "n": len(pairs),
        "unchanged": len(pairs) - flips,
        "flips": flips,
        "flip_rate": round(flips / len(pairs), 4) if pairs else None,
        "cells": {f"{a}->{b}": n for (a, b), n in sorted(cells.items())},
    }


def alarm_tables(
    rows: list[dict[str, Any]], alarmed: dict[str, list[str]], field: str
) -> dict[str, Any]:
    """Per gate: real / not-a-defect / uncertain alarm counts under one label field (final, sol or opus)."""
    out: dict[str, Any] = {}
    for gate, unit_ids in alarmed.items():
        labels = []
        for uid in unit_ids:
            own = [
                r[field] or "uncertain" for r in rows if r["unit_id"] == uid and gate in r["gates"]
            ]
            labels.append(core.alarm_label(own))
        counts = Counter(labels)
        n = len(labels)
        out[gate] = {
            "n_alarms": n,
            "real": core.share(counts["real"], n),
            "not_a_defect": core.share(counts["not_a_defect"], n),
            "uncertain": core.share(counts["uncertain"], n),
        }
    return out


def decision(
    original: dict[str, Any], arm: dict[str, Any], recall: dict[str, Any]
) -> dict[str, Any]:
    """Preregistered rule. ``original``/``arm``: per-gate share dicts of both-real alarms (k, n, wilson95).

    stands     both gates' both-real share is at or above the lower bound of the original Wilson interval
    weakened   either gate below its original lower bound but both still >= core.REAL_HIGH (0.60)
    changed    either gate below 0.60
    The calibration validity gate (recall >= core.VALIDITY_RECALL) is reported separately.
    """
    per_gate: dict[str, Any] = {}
    verdict = "stands"
    for gate, orig in original.items():
        now = arm[gate]
        low = orig["wilson95"][0]
        rate = now["k"] / now["n"] if now["n"] else 0.0
        inside = rate >= low
        per_gate[gate] = {
            "original": f"{orig['k']}/{orig['n']}",
            "redacted": f"{now['k']}/{now['n']}",
            "rate": round(rate, 4),
            "original_wilson_lower": low,
            "at_or_above_original_lower": inside,
            "at_or_above_0_60": rate >= core.REAL_HIGH,
        }
        if rate < core.REAL_HIGH:
            verdict = "changed"
        elif not inside and verdict == "stands":
            verdict = "weakened"
    rate = recall["k"] / recall["n"] if recall["n"] else 0.0
    return {
        "alarm_conclusion": verdict,
        "per_gate": per_gate,
        "calibration_recall": {**recall, "validity_gate_passed": rate >= core.VALIDITY_RECALL},
    }


def _effective(adj: dict[str, Any], unit_id: str, fid: str) -> str | None:
    entry = adj.get(unit_id)
    if not entry or not entry["verdicts"]:
        return None
    return entry["verdicts"][fid]["effective"]


def cmd_score(_: argparse.Namespace) -> None:
    units = base.read_json(STATE / "blind-units.json")["units"]
    cases = base.read_json(SOURCE_STATE / "cases.json")
    original = {who: base.read_json(SOURCE_STATE / f"adjudication-{who}.json") for who in WHO}
    arms: dict[str, dict[str, Any]] = {"original": original}
    for arm in ARMS:
        loaded = {}
        for who in WHO:
            path = STATE / f"adjudication-{who}-{arm}.json"
            loaded[who] = base.read_json(path) if path.exists() else {}
        if any(loaded[who] for who in WHO):
            arms[arm] = loaded

    def rows_for(adjs: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for unit in units:
            for cand in unit["candidates"]:
                fid = cand["finding_id"]
                a, b = (_effective(adjs[w], unit["unit_id"], fid) for w in WHO)
                rows.append(
                    {
                        "unit_id": unit["unit_id"],
                        "finding_id": fid,
                        "kind": unit["kind"],
                        "class": unit["class"],
                        "gates": sorted({s["gate"] for s in cand["sources"]}),
                        "known_real": fid in unit["known_real"],
                        "sol": a,
                        "opus": b,
                        "final": core.final_label(a, b),
                    }
                )
        return rows

    rows = {name: rows_for(adjs) for name, adjs in arms.items()}
    unchanged_cases = [c for c in cases.values() if c["class"] == "ab_unchanged"]
    alarmed = {
        gate: [
            c["unit_id"]
            for c in unchanged_cases
            if c["gate_status"].get(gate, {}).get("status") == "ok"
            and c["gate_status"][gate]["alarm"]
        ]
        for gate in GATES
    }
    out: dict[str, Any] = {
        "seed": SEED,
        "units": len(units),
        "findings": len(rows["original"]),
        "arms_present": list(arms),
    }
    summary: dict[str, Any] = {}
    for name, arm_rows in rows.items():
        known = [r for r in arm_rows if r["known_real"]]
        recall = {
            f: core.share(sum(r[f] == "real_defect" for r in known), len(known))
            for f in ("final", "sol", "opus")
        }
        off = [
            r
            for r in arm_rows
            if r["kind"] in ("cal_known", "cal_off_only") and not r["known_real"]
        ]
        pairs = [(r["sol"], r["opus"]) for r in arm_rows if r["sol"] and r["opus"]]
        summary[name] = {
            "alarm_labels": {
                f: alarm_tables(arm_rows, alarmed, f) for f in ("final", "sol", "opus")
            },
            "calibration_recall": recall,
            "off_target_real_share": {
                f: core.share(sum(r[f] == "real_defect" for r in off), len(off))
                for f in ("final", "sol", "opus")
            },
            "kappa": {"pairs": len(pairs), "kappa": core.kappa(pairs)},
            "finding_verdicts": {
                f: dict(Counter(r[f] or "missing" for r in arm_rows))
                for f in ("final", "sol", "opus")
            },
        }
    out["arms"] = summary
    # Reproduction check: the "original" arm restricted to these units must match the TQ-UNVERIFIED tables.
    flips: dict[str, Any] = {}
    for name in arms:
        if name == "original":
            continue
        flips[name] = {
            f: transition_matrix(
                [(o[f], n[f]) for o, n in zip(rows["original"], rows[name], strict=True)]
            )
            for f in ("sol", "opus", "final")
        }
        flips[name]["real_flag_changes_final"] = _real_flag_changes(rows["original"], rows[name])
    if "control" in rows and "redacted" in rows:
        flips["redacted_vs_control"] = {
            f: transition_matrix(
                [(o[f], n[f]) for o, n in zip(rows["control"], rows["redacted"], strict=True)]
            )
            for f in ("sol", "opus", "final")
        }
    out["flips"] = flips
    if "redacted" in summary:
        orig_real = {g: summary["original"]["alarm_labels"]["final"][g]["real"] for g in GATES}
        out["decision"] = decision(
            orig_real,
            {g: summary["redacted"]["alarm_labels"]["final"][g]["real"] for g in GATES},
            summary["redacted"]["calibration_recall"]["final"],
        )
    if "control" in summary:
        orig_real = {g: summary["original"]["alarm_labels"]["final"][g]["real"] for g in GATES}
        out["decision_control_noise_reference"] = decision(
            orig_real,
            {g: summary["control"]["alarm_labels"]["final"][g]["real"] for g in GATES},
            summary["control"]["calibration_recall"]["final"],
        )
    out["coverage"] = {
        f"{who}-{arm}": {
            "units_answered": sum(1 for e in adjs[who].values() if e["verdicts"]),
            "calls": sum(e["calls"] for e in adjs[who].values()),
            "refusals": sum(r["refusal"] for e in adjs[who].values() for r in e["receipts"]),
            "fallbacks": sum(
                bool(r["model_fallback"]) for e in adjs[who].values() for r in e["receipts"]
            ),
            "served": sorted(
                {
                    i
                    for e in adjs[who].values()
                    for r in e["receipts"]
                    for i in (r["served_identities"] or [])
                }
            ),
            "downgrades": sum(
                1
                for e in adjs[who].values()
                if e["verdicts"]
                for v in e["verdicts"].values()
                if v["downgraded"]
            ),
        }
        for arm, adjs in arms.items()
        if arm != "original"
        for who in WHO
    }
    out["redaction"] = base.read_json(STATE / "redaction-proof.json")["summary"]
    base.write_json(STATE / "results-private.json", {**out, "rows": rows})
    base.write_json(STATE / "results.json", out)
    print(json.dumps(out, indent=1)[:9000])


def _real_flag_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, int]:
    """Final-label movements into and out of real_defect (the verdict the headline counts)."""
    lost = sum(
        b["final"] == "real_defect" and a["final"] != "real_defect"
        for b, a in zip(before, after, strict=True)
    )
    gained = sum(
        b["final"] != "real_defect" and a["final"] == "real_defect"
        for b, a in zip(before, after, strict=True)
    )
    return {"real_lost": lost, "real_gained": gained}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare").set_defaults(func=cmd_prepare)
    sub.add_parser("score").set_defaults(func=cmd_score)
    adj = sub.add_parser("adjudicate")
    adj.add_argument("--who", choices=WHO, required=True)
    adj.add_argument("--arm", choices=ARMS, required=True)
    adj.add_argument("--concurrency", type=int, default=4)
    adj.add_argument("--limit", type=int, default=0)
    adj.set_defaults(func=cmd_adjudicate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
