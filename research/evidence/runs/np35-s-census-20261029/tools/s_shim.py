"""NP35-LB-1 shim: apply the frozen NP29 definition S, unchanged, to runs given as ATIF-like steps.

``test_edits.py`` is imported from the NP29 export under np29/ (sha256 checked against origin/main by run_flake.py).
This module never prints a cell, a path or a command; it returns booleans and hashed keys only.
"""

from __future__ import annotations

import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
NP29 = Path("/private/tmp/np35/np29/research/evidence/runs/np29-test-edits-20261029/tools")
sys.path.insert(0, str(NP29))
import test_edits  # noqa: E402  (NP29, unchanged)

TEST_EDITS_SHA256 = hashlib.sha256((NP29 / "test_edits.py").read_bytes()).hexdigest()


def normalise_steps(steps):
    """Map a corpus's own step dicts to the ATIF shape ``analyse_steps`` reads: ``source`` plus ``tool_calls`` of
    ``{function_name, arguments}``. Adapters supply this mapping; nothing else about a step is changed."""
    return steps


def analyse(steps):
    r = test_edits.analyse_steps(steps)
    return {
        "S": r["flags"]["S"],
        "SI": r["flags"]["SI"],
        "SD": r["flags"]["SD"],
        "L": r["flags"]["L"],
        "forms": r["forms"],
        "targets": r["targets"],
        "agent_steps": r["agent_steps"],
        "calls": r["calls"],
        "unresolved": r["unresolved"],
    }


def task_hash(value) -> str:
    return hashlib.sha256(" ".join(str(value).split()).encode()).hexdigest()[:12]


class Tally:
    """Counts per (class, bucket); distinct task keys per S group. Aggregates only."""

    def __init__(self):
        self.n = Counter()
        self.s = Counter()
        self.s_tasks: dict[str, set] = {}
        self.tasks: dict[str, set] = {}
        self.forms: dict[str, Counter] = {}
        self.calls = Counter()
        self.unresolved = Counter()
        self.no_agent_calls = Counter()
        self.si = Counter()
        self.l = Counter()
        self.ls = Counter()

    def add(self, group: str, task_key, steps, extra=None):
        a = analyse(steps)
        self.n[group] += 1
        self.tasks.setdefault(group, set()).add(task_key)
        self.calls[group] += a["calls"]
        self.unresolved[group] += a["unresolved"]
        self.no_agent_calls[group] += a["calls"] == 0
        self.si[group] += bool(a["SI"])
        self.l[group] += bool(a["L"])
        self.ls[group] += bool(a["L"] and not a["S"])
        if a["S"]:
            self.s[group] += 1
            self.s_tasks.setdefault(group, set()).add(task_key)
            c = self.forms.setdefault(group, Counter())
            for f in a["forms"]:
                c[f] += 1
        return a

    def report(self):
        out = {}
        for g in sorted(self.n):
            out[g] = {
                "runs": self.n[g],
                "S": self.s[g],
                "SI": self.si[g],
                "L": self.l[g],
                "L_not_S": self.ls[g],
                "distinct_tasks_all": len(self.tasks.get(g, ())),
                "distinct_tasks_S": len(self.s_tasks.get(g, ())),
                "runs_without_any_tool_call": int(self.no_agent_calls[g]),
                "unresolved_editor_calls": int(self.unresolved[g]),
                "S_form_counts": dict(self.forms.get(g, {})),
            }
        return out
