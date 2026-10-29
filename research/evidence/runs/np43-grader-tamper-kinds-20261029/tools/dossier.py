"""NP43-TW-1 hand-check draw: 20 events per classifier kind (seed 20261029), shown without the classifier's label.

Writes, privately, ``dossier.txt`` (raw step text around each drawn event, cases shuffled and numbered) and ``dossier-key.json``
(case number -> classifier kind and sub-kind). Label-free: no class label of any run is read.
"""

from __future__ import annotations

import json
import pickle
import posixpath
import random
import sys
from pathlib import Path

import classify

STATE = Path.home() / ".local/state"
NP36 = STATE / "evallab-worker-evidence/np36-tw1-20261029"
NP38 = STATE / "evallab-np38-cm1"
OUT = STATE / "evallab-worker-evidence/np43-tw1-20261029"
SEED = 20261029
PER_KIND = 20


def snippet(text: str, needle: str, width: int = 110) -> str:
    at = text.find(needle)
    return text[max(0, at - 40) : at + width].replace("\n", "\\n")


def mentions(step: dict, path: str, base: str) -> list[str]:
    needles = [c for c in (path, base) if len(c) >= 4]
    found = []
    tools = json.dumps(step.get("tool_calls") or [], ensure_ascii=False)
    obs, _ = classify._obs_text(step)
    for where, text in (("call", tools), ("out", obs)):
        for needle in needles:
            if needle in text:
                found.append(f"{where}: {snippet(text, needle)}")
                break
    return found


def render(case: int, run: list[dict], event: dict) -> str:
    agent = classify.gate.agent_steps_of(run)
    i = event["step_index"]
    path = event["path"]
    base = posixpath.basename(classify.norm(path))
    step = agent[i]
    calls = json.dumps(step.get("tool_calls") or [], ensure_ascii=False)
    obs, present = classify._obs_text(step)
    lines = [
        f"=== CASE {case}: agent step {i} of {len(agent)}; path {path}; via {event['via']} mech {event['mech']} reason {event['reason']}",
        f"  event call: {calls[:420]}",
        f"  event output ({'recorded' if present else 'MISSING'}): {obs[:160]!r}",
    ]
    shown = 0
    for j in range(i):
        for item in mentions(agent[j], path, base):
            lines.append(f"  earlier step {j} {item}")
            shown += 1
        if shown >= 5:
            break
    if not shown:
        lines.append("  earlier steps naming the path or its basename: none")
    gaps = [
        j
        for j in range(i)
        if (f := classify.step_facts(agent[j])).listing
        and (not f.obs_present or classify._TRUNC_MARK_RE.search(f.obs))
    ]
    lines.append(f"  earlier listing-type probes with missing/truncated output: {len(gaps)}")
    return "\n".join(lines)


def main() -> int:
    events = json.loads((OUT / "events.json").read_text())
    pool: dict[str, list[tuple[str, str, int]]] = {k: [] for k in "abcd"}
    for population in sorted(events):
        for key in sorted(events[population]):
            for n, event in enumerate(events[population][key]["events"]):
                pool[event["kind"]].append((population, key, n))
    rng = random.Random(SEED)
    drawn = []
    for kind in "abcd":
        items = sorted(pool[kind])
        drawn += [
            (kind, *item)
            for item in (rng.sample(items, PER_KIND) if len(items) > PER_KIND else items)
        ]
    rng.shuffle(drawn)
    needed: dict[str, set[str]] = {}
    for _kind, population, key, _n in drawn:
        needed.setdefault(population, set()).add(key)
    runs: dict[tuple[str, str], list[dict]] = {}
    for population, keys in needed.items():
        if population == "linuxarena":
            for key in keys:
                with (NP38 / "runs" / f"{key}.pkl").open("rb") as handle:
                    runs[(population, key)] = list(pickle.load(handle)["steps"])
            continue
        name = {"pairs": "pairs", "goodfire_other": "goodfire_dev", "cheatbench": "cheatbench"}[
            population
        ]
        with (NP36 / f"{name}.pkl").open("rb") as handle:
            for rid, steps in pickle.load(handle).items():
                if rid in keys:
                    runs[(population, rid)] = list(steps)
    text, key_out = [], {}
    for case, (kind, population, key, n) in enumerate(drawn, start=1):
        event = events[population][key]["events"][n]
        key_out[case] = {"kind": kind, "sub": event["sub"], "population": population}
        text.append(render(case, runs[(population, key)], event))
    (OUT / "dossier.txt").write_text("\n\n".join(text) + "\n")
    (OUT / "dossier-key.json").write_text(json.dumps(key_out, indent=1))
    print({k: len(v) for k, v in pool.items()}, "drawn", len(drawn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
