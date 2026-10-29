#!/usr/bin/env python3
"""TQ-UNVERIFIED driver: build alarm units, calibrate, adjudicate with two models, score once.

Stages (each writes only under STATE; nothing here changes a gate, prompt, cohort or stored score):
  build       frozen gate cells + sealed labels -> cases.json (private)
  match       terra matcher: which findings on defective AutomationBench tasks name the documented defect
  select      alarm units + calibration units -> units.json (private)
  adjudicate  one adjudicator over every unit (quota-gated, attested, resumable)
  score       final labels, tables, results.json

Hosted models read the staged corpora for evaluation only; nothing leaves the subscription routes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _load(name: str):
    qualified = f"tq_unverified_{name}"
    spec = importlib.util.spec_from_file_location(qualified, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


core = _load("core")
prompts = _load("prompts")

HOME = Path.home()
STUDY = HOME / "Developer/eval-lab-results/2026-10-28/oq10-task-quality-cross/study-v2"
R29 = HOME / "Developer/eval-lab-results/2026-10-29/r29-oq1-codex-cross-arm"
AUTOMATION_BUNDLE = (
    HOME / ".local/state/evallab-cross-test/parsewave__automationbench-verified/4a8e10612540"
)
CONFIG = STUDY / "native-runtime/controlled-subscription-oq10.yml"
CONFIG_SHA = "0df78b7a1aa1aad2dc08e5bef3520d9236b9c8918f0d5bef9bc000ef09fb7fb9"
STATE = HOME / ".local/state/evallab-fleet/2026-10-29/TQ-UNVERIFIED"
QUOTA_STOP = 0.85
PROVIDERS = {"sol": "openai-codex", "opus": "anthropic", "matcher": "openai-codex"}
GATES = {"sonnet": STUDY / "cells/sonnet", "terra": R29 / "cells/codex"}
CLASSES = ("ab_unchanged", "ab_defective", "pai_aligned", "pai_misaligned")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------- quota and calls


class Stopped(RuntimeError):
    pass


class Quota:
    def __init__(self, provider: str, stop: Path) -> None:
        self.provider, self.stop = provider, stop
        self.lock = threading.Lock()
        self.last = 0.0
        self.fraction: float | None = None

    @staticmethod
    def weekly(report: dict, provider: str) -> float:
        fractions = []
        for entry in report.get("reports", []):
            if entry.get("provider") != provider:
                continue
            if entry.get("error"):
                raise RuntimeError("quota report error")
            for limit in entry.get("limits", []):
                window = limit.get("window", {})
                if window.get("id") == "7d" or window.get("durationMs") == 604800000:
                    amount = limit["amount"]
                    fraction = amount.get("usedFraction")
                    if fraction is None:
                        fraction = amount["used"] / amount["limit"]
                    fractions.append(float(fraction))
        if not fractions or any(not 0 <= f <= 1 for f in fractions):
            raise RuntimeError("weekly quota unavailable")
        return max(fractions)

    def admit(self) -> float:
        if self.stop.exists():
            raise Stopped("stop file present")
        with self.lock:
            if self.fraction is None or time.monotonic() - self.last > 10:
                done = subprocess.run(
                    [
                        "omp",
                        "usage",
                        "--provider",
                        self.provider,
                        "--json",
                        "--redact",
                        "--no-extensions",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=90,
                )
                self.fraction = self.weekly(json.loads(done.stdout), self.provider)
                self.last = time.monotonic()
            fraction = self.fraction
        if fraction >= QUOTA_STOP:
            raise Stopped(f"{self.provider} 7-day quota {fraction:.0%} >= 85%")
        return fraction


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    explicit = {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "CLOUD_ML_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    for key in tuple(env):
        upper = key.upper()
        if (
            upper.startswith(("OMP_", "PI_", "CLAUDE_CODE_USE_"))
            or any(
                p in upper
                for p in ("API_KEY", "API_TOKEN", "AUTH_TOKEN", "OAUTH_TOKEN", "ACCESS_TOKEN")
            )
            or upper.endswith(("_BASE_URL", "_API_BASE", "_ENDPOINT"))
            or upper in explicit
        ):
            env.pop(key)
    env["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def session_receipt(directory: Path, route: str, returncode: int | None) -> dict[str, Any]:
    """Attest one finished session from its JSONL log: served identity, fallback, tools, answer text."""
    receipt: dict[str, Any] = {
        "route": route,
        "status": "unanswered",
        "returncode": returncode,
        "served_identities": [],
        "model_fallback": False,
        "tool_calls": 0,
        "stop_reason": None,
        "response": None,
        "refusal": False,
    }
    sessions = list((directory / "sessions").rglob("*.jsonl"))
    if len(sessions) != 1:
        receipt["error"] = "expected exactly one session JSONL"
        return receipt
    rows = []
    for line in sessions[0].read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    receipt["model_fallback"] = any(
        r.get("resolvedModelIsFallback") is True or r.get("isFallback") is True for r in rows
    )
    messages = [
        r["message"]
        for r in rows
        if r.get("type") == "message" and r["message"].get("role") == "assistant"
    ]
    usage = Counter()
    for message in messages:
        content = message.get("content", [])
        if isinstance(content, list):
            receipt["tool_calls"] += sum(
                1
                for part in content
                if isinstance(part, dict)
                and part.get("type") in ("toolCall", "tool_call", "toolUse", "tool_use")
            )
        for key in ("input", "output", "cacheRead", "cacheWrite"):
            usage[key] += int((message.get("usage") or {}).get(key, 0) or 0)
    receipt["usage"] = dict(usage)
    receipt["served_identities"] = sorted(
        {f"{m.get('provider')}/{m.get('model')}" for m in messages}
    )
    if not messages:
        receipt["error"] = "no assistant message"
        return receipt
    last = messages[-1]
    receipt["stop_reason"] = last.get("stopReason")
    text = "".join(
        part.get("text", "")
        for part in last.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )
    receipt["response"] = text
    if (
        receipt["model_fallback"]
        or receipt["tool_calls"]
        or receipt["served_identities"] != [route]
    ):
        receipt["status"] = "invalid"
        receipt["error"] = "served identity, fallback or tool protocol violation"
    elif last.get("stopReason") in ("error", "aborted", "toolUse") or not text.strip():
        receipt["status"] = "unanswered"
        receipt["refusal"] = last.get("stopReason") == "refusal" or not text.strip()
    else:
        receipt["status"] = "answered"
    return receipt


def omp_call(
    quota: Quota, route: str, system: str, prompt: str, directory: Path, label: str
) -> dict[str, Any]:
    """One consumed call. A finished attempt is never resent; an interrupted one is finalized from its log."""
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / "receipt.json"
    if final.exists():
        return read_json(final)
    raw = prompt.encode()
    if (directory / "call-consumed.json").exists():
        receipt = session_receipt(directory, route, None)
        write_json(final, receipt)
        return receipt
    fraction = quota.admit()
    (directory / "sessions").mkdir(exist_ok=True)
    (directory / "prompt.txt").write_bytes(raw)
    argv = [
        "omp",
        "-p",
        "--config",
        str(CONFIG),
        "--model",
        route,
        "--thinking",
        "high",
        "--no-tools",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--no-lsp",
        "--no-title",
        "--no-prewalk",
        "--no-pty",
        "--mode",
        "json",
        "--session-dir",
        str(directory / "sessions"),
        "--max-time",
        "900s",
        "--system-prompt",
        system,
        "@" + str(directory / "prompt.txt"),
    ]
    write_json(
        directory / "call-consumed.json",
        {
            "argv": argv,
            "label": label,
            "quota_fraction": fraction,
            "started_at": time.time(),
            "prompt_sha256": sha(raw),
        },
    )
    started = time.monotonic()
    with (directory / "stdout.jsonl").open("x") as out, (directory / "stderr.txt").open("x") as err:
        try:
            returncode = subprocess.run(
                argv, cwd=directory, env=_environment(), stdout=out, stderr=err, timeout=960
            ).returncode
        except subprocess.TimeoutExpired:
            returncode = -1
    receipt = session_receipt(directory, route, returncode)
    receipt["wall_seconds"] = round(time.monotonic() - started, 1)
    write_json(final, receipt)
    return receipt


# ---------------------------------------------------------------------------- build / match / select


def cmd_build(_: argparse.Namespace) -> None:
    if sha(CONFIG.read_bytes()) != CONFIG_SHA:
        raise SystemExit("controlled config drifted")
    selection = read_json(STUDY / "scorer-only/selection.json")["cases"]
    cases: dict[str, Any] = {}
    for row in selection:
        corpus = row["corpus"]
        if corpus.startswith("automationbench"):
            klass = "ab_defective" if row["positive"] else "ab_unchanged"
        else:
            klass = "pai_misaligned" if row["positive"] else "pai_aligned"
        cases[row["case_id"]] = {
            "class": klass,
            "corpus": corpus,
            "subcorpus": row["subcorpus"],
            "task_id": row["task_id"],
            "gate_status": {},
            "findings": [],
        }
    for gate, directory in GATES.items():
        for path in sorted(directory.glob("C*.json")):
            cell = read_json(path)
            entry = cases[cell["case_id"]]
            entry["gate_status"][gate] = {
                "status": cell["status"],
                "alarm": bool(cell.get("flags", {}).get(core.GATE_KEY)),
            }
            entry["findings"].extend(core.eligible_findings(cell, gate))
    for case_id, entry in cases.items():
        entry["candidates"] = core.shuffle_and_label(case_id, core.dedupe(entry.pop("findings")))
        entry["unit_id"] = core.unit_id(case_id)
    write_json(STATE / "cases.json", cases)
    summary = Counter()
    for entry in cases.values():
        for gate, status in entry["gate_status"].items():
            summary[
                (
                    entry["class"],
                    entry["subcorpus"],
                    gate,
                    "alarm" if status["alarm"] else status["status"],
                )
            ] += 1
    for key, value in sorted(summary.items()):
        print(key, value)


def source_text(case_id: str) -> str:
    return (STUDY / "inputs" / case_id / "source.txt").read_text()


def cmd_match(args: argparse.Namespace) -> None:
    cases = read_json(STATE / "cases.json")
    fixes = {}
    with (AUTOMATION_BUNDLE / "fixes.csv").open(newline="", encoding="utf-8") as stream:
        csv.field_size_limit(10**9)
        for row in csv.DictReader(stream):
            fixes[row["task_name"]] = row
    quota = Quota(PROVIDERS["matcher"], STATE / "STOP")
    out = read_json(STATE / "match.json") if (STATE / "match.json").exists() else {}
    todo = [
        cid
        for cid, c in sorted(cases.items())
        if c["class"] == "ab_defective" and c["candidates"] and cid not in out
    ]
    lock = threading.Lock()
    calls = [0]

    def work(case_id: str) -> None:
        row = fixes[cases[case_id]["task_id"]]
        documented = "\n\n".join(
            f"{label}: {row[key]}"
            for label, key in (
                ("what was wrong", "what_was_wrong"),
                ("finding", "finding"),
                ("what the fix does", "what_the_fix_does"),
            )
            if row.get(key)
        )
        candidates = cases[case_id]["candidates"]
        ids = [c["finding_id"] for c in candidates]
        prompt = prompts.render_matcher_prompt(documented, candidates)
        verdicts, receipt = None, {"served_identities": [], "status": "unanswered"}
        for attempt in (1, 2):
            with lock:
                if calls[0] >= 60:
                    return
                calls[0] += 1
            try:
                receipt = omp_call(
                    quota,
                    core.MATCHER_ROUTE,
                    prompts.MATCHER_SYSTEM,
                    prompt,
                    STATE / "calls/matcher" / f"{case_id}-{attempt}",
                    "tq-unverified-matcher",
                )
            except Stopped:
                return
            if receipt["status"] == "answered":
                try:
                    verdicts = prompts.parse_matches(receipt["response"], ids)
                    break
                except ValueError:
                    continue
        with lock:
            out[case_id] = {
                "matches": verdicts,
                "served": receipt["served_identities"],
                "status": receipt["status"],
            }
            write_json(STATE / "match.json", out)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, todo))
    print(f"matcher calls this run: {calls[0]}; cases matched: {len(out)}")


def cmd_select(_: argparse.Namespace) -> None:
    cases = read_json(STATE / "cases.json")
    match = read_json(STATE / "match.json")
    alarm_units = [
        cid
        for cid, c in cases.items()
        if c["class"] in ("ab_unchanged", "pai_aligned") and c["candidates"]
    ]
    matched, unmatched = [], []
    for case_id, c in cases.items():
        if c["class"] != "ab_defective" or not c["candidates"]:
            continue
        verdicts = (match.get(case_id) or {}).get("matches") or {}
        (matched if any(v == "match" for v in verdicts.values()) else unmatched).append(case_id)
    key = lambda cid: core.order_key("cal", cid)  # noqa: E731
    matched.sort(key=key)
    unmatched.sort(key=key)
    budget = core.CALL_CAP - 5 - len(alarm_units)
    known = min(core.KNOWN_REAL_TARGET, len(matched))
    off_only = min(core.OFF_TARGET_ONLY_TARGET, len(unmatched))
    while known + off_only > budget and off_only > 0:
        off_only -= 1
    while known + off_only > budget and known > core.KNOWN_REAL_FLOOR:
        known -= 1
    if known + off_only > budget or known < core.KNOWN_REAL_FLOOR:
        raise SystemExit(
            f"BLOCKED: budget {budget} cannot hold {core.KNOWN_REAL_FLOOR} known-real calibration tasks "
            f"(alarm units {len(alarm_units)}, matched {len(matched)})"
        )
    units = []
    for case_id in alarm_units:
        units.append({"case_id": case_id, "kind": "alarm"})
    for case_id in matched[:known]:
        units.append({"case_id": case_id, "kind": "cal_known"})
    for case_id in unmatched[:off_only]:
        units.append({"case_id": case_id, "kind": "cal_off_only"})
    for unit in units:
        case = cases[unit["case_id"]]
        unit["unit_id"] = case["unit_id"]
        unit["class"], unit["subcorpus"] = case["class"], case["subcorpus"]
        unit["candidates"] = case["candidates"]
        verdicts = (match.get(unit["case_id"]) or {}).get("matches") or {}
        unit["known_real"] = (
            [fid for fid, v in verdicts.items() if v == "match"] if unit["kind"] != "alarm" else []
        )
    units.sort(key=lambda u: core.order_key("order", u["unit_id"]))
    write_json(STATE / "units.json", {"budget": budget, "units": units})
    print(
        Counter(u["kind"] for u in units),
        "budget",
        budget,
        "matched avail",
        len(matched),
        "unmatched",
        len(unmatched),
    )


# ---------------------------------------------------------------------------- adjudicate


def cmd_adjudicate(args: argparse.Namespace) -> None:
    route = core.ROUTES[args.who]
    suffix = "" if args.set == "main" else f"-{args.set}"
    units = read_json(STATE / f"units{suffix}.json")["units"]
    quota = Quota(PROVIDERS[args.who], STATE / "STOP")
    results_path = STATE / f"adjudication-{args.who}{suffix}.json"
    results = read_json(results_path) if results_path.exists() else {}
    lock = threading.Lock()
    prior = sum(
        r["calls"]
        for path in STATE.glob(f"adjudication-{args.who}*.json")
        if path != results_path
        for r in read_json(path).values()
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
        files = core.parse_source(source_text(unit["case_id"]))
        ids = [c["finding_id"] for c in unit["candidates"]]
        prompt = prompts.render_adjudicator_prompt(source_text(unit["case_id"]), unit["candidates"])
        verdicts, receipts, calls = None, [], 0
        for attempt in (1, 2):
            with lock:
                if state["calls"] >= core.CALL_CAP:
                    state["stopped"] = "call cap"
                    return
                state["calls"] += 1
            try:
                receipt = omp_call(
                    quota,
                    route,
                    prompts.ADJUDICATOR_SYSTEM,
                    prompt,
                    STATE / "calls" / args.who / f"{unit['unit_id']}-{attempt}",
                    "tq-unverified-adjudicator",
                )
            except Stopped as exc:
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
                if state["consecutive"] >= 3:
                    state["stopped"] = "3 consecutive failures to answer"
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
            write_json(results_path, results)
            print(
                f"{args.who} {len(results)}/{len(units)} calls={state['calls']} quota={quota.fraction}",
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


# ---------------------------------------------------------------------------- score


def _load_adjudications() -> dict[str, dict[str, Any]]:
    return {
        who: read_json(STATE / f"adjudication-{who}.json")
        for who in ("sol", "opus")
        if (STATE / f"adjudication-{who}.json").exists()
    }


def _labels(adj: dict[str, Any], unit_id: str, fid: str) -> str | None:
    entry = adj.get(unit_id)
    if not entry or not entry["verdicts"]:
        return None
    return entry["verdicts"][fid]["effective"]


def cmd_score(_: argparse.Namespace) -> None:
    cases = read_json(STATE / "cases.json")
    units = read_json(STATE / "units.json")["units"]
    adjudications = _load_adjudications()
    sol, opus = adjudications.get("sol", {}), adjudications.get("opus", {})
    rows: list[dict[str, Any]] = []  # one per (unit, candidate)
    for unit in units:
        for cand in unit["candidates"]:
            fid = cand["finding_id"]
            a, b = _labels(sol, unit["unit_id"], fid), _labels(opus, unit["unit_id"], fid)
            rows.append(
                {
                    "unit_id": unit["unit_id"],
                    "finding_id": fid,
                    "kind": unit["kind"],
                    "class": unit["class"],
                    "subcorpus": unit["subcorpus"],
                    "gates": sorted({s["gate"] for s in cand["sources"]}),
                    "known_real": fid in unit["known_real"],
                    "sol": a,
                    "opus": b,
                    "final": core.final_label(a, b),
                }
            )
    out: dict[str, Any] = {
        "seed": core.SEED,
        "units": len(units),
        "unit_kinds": dict(Counter(u["kind"] for u in units)),
    }

    def tally(items: list[dict[str, Any]], field: str) -> dict[str, Any]:
        counts = Counter(r[field] or "missing" for r in items)
        n = len(items)
        return {
            "n": n,
            **{
                k: core.share(counts.get(k, 0), n)
                for k in ("real_defect", "not_a_defect", "uncertain")
            },
            "missing": counts.get("missing", 0),
        }

    known = [r for r in rows if r["known_real"]]
    off_target = [
        r for r in rows if r["kind"] in ("cal_known", "cal_off_only") and not r["known_real"]
    ]
    out["calibration"] = {
        "known_real_items": len(known),
        "recall": {
            f: core.share(sum(r[f] == "real_defect" for r in known), len(known))
            for f in ("final", "sol", "opus")
        },
        "off_target": {f: tally(off_target, f) for f in ("final", "sol", "opus")},
    }
    validated = (
        bool(known)
        and sum(r["final"] == "real_defect" for r in known) / len(known) >= core.VALIDITY_RECALL
    )
    out["validated"] = validated
    pairs = [(r["sol"], r["opus"]) for r in rows if r["sol"] and r["opus"]]
    out["agreement"] = {
        "pairs": len(pairs),
        "kappa": core.kappa(pairs),
        "raw_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
    }
    out["downgrades"] = {
        who: sum(
            1
            for e in adj.values()
            if e["verdicts"]
            for v in e["verdicts"].values()
            if v["downgraded"]
        )
        for who, adj in adjudications.items()
    }
    out["coverage"] = {
        who: {
            "units_answered": sum(1 for e in adj.values() if e["verdicts"]),
            "calls": sum(e["calls"] for e in adj.values()),
        }
        for who, adj in adjudications.items()
    }
    out["served"] = {
        who: sorted(
            {i for e in adj.values() for r in e["receipts"] for i in (r["served_identities"] or [])}
        )
        for who, adj in adjudications.items()
    }
    out["fallbacks"] = {
        who: sum(1 for e in adj.values() for r in e["receipts"] if r["model_fallback"])
        for who, adj in adjudications.items()
    }

    tables: dict[str, Any] = {}
    gates_seen = ("sonnet", "terra")
    for klass, sub in (
        ("ab_unchanged", ""),
        ("pai_aligned", "multilingual"),
        ("pai_aligned", "swe_gym"),
    ):
        pool = [
            c
            for c in cases.values()
            if c["class"] == klass and (not sub or sub in c["subcorpus"].lower())
        ]
        name = klass if not sub else f"{klass}:{sub}"
        tables[name] = {"tasks": len(pool)}
        for gate in gates_seen:
            decided = [c for c in pool if c["gate_status"].get(gate, {}).get("status") == "ok"]
            alarmed = [c for c in decided if c["gate_status"][gate]["alarm"]]
            per_rule = {}
            for field in ("final", "sol", "opus"):
                labels = []
                for case in alarmed:
                    own = [
                        r for r in rows if r["unit_id"] == case["unit_id"] and gate in r["gates"]
                    ]
                    labels.append(core.alarm_label([r[field] or "uncertain" for r in own]))
                counts = Counter(labels)
                n = len(labels)
                per_rule[field] = {
                    "n_alarms": n,
                    "real": core.share(counts["real"], n),
                    "not_a_defect": core.share(counts["not_a_defect"], n),
                    "uncertain": core.share(counts["uncertain"], n),
                }
            r_, f_, u_ = (per_rule["final"][k]["k"] for k in ("real", "not_a_defect", "uncertain"))
            tables[name][gate] = {
                "decided_tasks": len(decided),
                "alarms": len(alarmed),
                "alarm_labels": per_rule,
                "real_alarm_rate_of_tasks": core.share(r_, len(decided)),
                "false_alarm_rate_of_tasks": {
                    "lower": core.share(f_, len(decided)),
                    "upper": core.share(f_ + u_, len(decided)),
                },
                "precision_real_over_all_alarms": core.share(r_, r_ + f_ + u_),
                "precision_real_over_decided_alarms": core.share(r_, r_ + f_),
                "reading": core.headline_reading(r_, r_ + f_ + u_, validated),
            }
            findings = [
                r
                for r in rows
                if r["class"] == klass
                and (not sub or sub in r["subcorpus"].lower())
                and gate in r["gates"]
                and r["kind"] == "alarm"
            ]
            tables[name][gate]["findings"] = {
                f: tally(findings, f) for f in ("final", "sol", "opus")
            }
    out["tables"] = tables
    # AutomationBench corrected balanced accuracy (bounds over uncertain).
    ab = {}
    defective = [c for c in cases.values() if c["class"] == "ab_defective"]
    for gate in gates_seen:
        decided_def = [c for c in defective if c["gate_status"].get(gate, {}).get("status") == "ok"]
        t = tables["ab_unchanged"][gate]
        lab = t["alarm_labels"]["final"]
        ab[gate] = (
            core.corrected_balanced_accuracy(
                defective=len(decided_def),
                defective_flagged=sum(c["gate_status"][gate]["alarm"] for c in decided_def),
                unchanged=t["decided_tasks"],
                unchanged_flagged=t["alarms"],
                real_alarms=lab["real"]["k"],
                uncertain_alarms=lab["uncertain"]["k"],
            )
            if t["decided_tasks"]
            else None
        )
    out["ab_corrected_balanced_accuracy"] = ab
    swap_path = STATE / "units-swap.json"
    if swap_path.exists():
        swap_units = read_json(swap_path)["units"]
        control: dict[str, Any] = {"units": len(swap_units)}
        for who in ("sol", "opus"):
            path = STATE / f"adjudication-{who}-swap.json"
            adj = read_json(path) if path.exists() else {}
            counts: Counter[str] = Counter()
            for unit in swap_units:
                entry = adj.get(unit["unit_id"])
                for cand in unit["candidates"]:
                    label = (
                        entry["verdicts"][cand["finding_id"]]["effective"]
                        if entry and entry["verdicts"]
                        else "missing"
                    )
                    counts[label] += 1
            total = sum(counts.values())
            control[who] = {
                "items": total,
                **{
                    k: core.share(counts.get(k, 0), total)
                    for k in ("real_defect", "not_a_defect", "uncertain")
                },
                "missing": counts.get("missing", 0),
            }
        out["swap_control_post_hoc"] = control
    write_json(STATE / "results-private.json", {**out, "rows": rows})
    write_json(STATE / "results.json", out)
    print(json.dumps(out, indent=1)[:6000])


def cmd_swap_select(_: argparse.Namespace) -> None:
    """Post-hoc negative control: another task's findings shown against this task's source (not a defect of it)."""
    cases = read_json(STATE / "cases.json")
    alarm_cases = sorted(
        (c["unit_id"], cid)
        for cid, c in cases.items()
        if c["class"] in ("ab_unchanged", "pai_aligned") and c["candidates"]
    )
    by_family: dict[str, list[str]] = {}
    for _, cid in alarm_cases:
        by_family.setdefault(cases[cid]["corpus"] + "|" + cases[cid]["subcorpus"], []).append(cid)
    chosen = sorted((cid for _, cid in alarm_cases), key=lambda cid: core.order_key("swap", cid))[
        :20
    ]
    units = []
    for cid in chosen:
        family = cases[cid]["corpus"] + "|" + cases[cid]["subcorpus"]
        donors = sorted(
            (d for d in by_family[family] if d != cid),
            key=lambda d: core.order_key("donor:" + cid, d),
        )
        donor = cases[donors[0]]
        candidates = [
            {**{k: v for k, v in cand.items() if k != "finding_id"}, "finding_id": f"F{i + 1}"}
            for i, cand in enumerate(donor["candidates"][:3])
        ]
        units.append(
            {
                "case_id": cid,
                "kind": "swap_control",
                "unit_id": "S" + core.order_key("swapunit", cid)[:10],
                "class": cases[cid]["class"],
                "subcorpus": cases[cid]["subcorpus"],
                "candidates": candidates,
                "known_real": [],
                "donor_case": donors[0],
            }
        )
    units.sort(key=lambda u: core.order_key("order", u["unit_id"]))
    write_json(STATE / "units-swap.json", {"units": units})
    print(len(units), "swap units;", sum(len(u["candidates"]) for u in units), "candidates")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("build", cmd_build),
        ("match", cmd_match),
        ("select", cmd_select),
        ("swap-select", cmd_swap_select),
        ("score", cmd_score),
    ):
        sub.add_parser(name).set_defaults(func=fn)
    adj = sub.add_parser("adjudicate")
    adj.add_argument("--who", choices=("sol", "opus"), required=True)
    adj.add_argument("--concurrency", type=int, default=4)
    adj.add_argument("--limit", type=int, default=0)
    adj.add_argument("--set", choices=("main", "swap"), default="main")
    adj.set_defaults(func=cmd_adjudicate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
