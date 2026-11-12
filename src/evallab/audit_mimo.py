"""Read-only MiMo stored-audit plugin and HAR-186 evidence facets.

MiMo identifiers, the frozen upstream snapshot and ledger routing policy live
here, not in the dataset-neutral audit core. Whole-cohort reads load each
CSV/JSON/lineage source once; single-task dossiers use the same projection.
Nothing here generates variants, materializes packages, executes controls,
or writes stores. ``keep`` is the ledger's routing decision, not certification.
Health/solve/verdict classifiers and probe binding reuse the canonical
HAR-172/177 and HAR-161 implementations; typed lineage retains candidate status.

Result keys (all present; ``None`` means absent/unknown, never a negative
claim)::

    health: source-backed dict with ``tag``/``solve``/ledger/lock/bound-probe
        detail, or None when the task has no ledger row.
    verdict: ``{"tag", "verdict", "evidence"}`` from the ledger row, or None.
    tags: ``[health, solve, verdict]`` label strings, or None.
    leak: ``{"found", "channel", "ledger_channel", "evidence", "note"}``.
        A digest-bound probe verdict overrides the stale census-era ledger
        channel; an unbound or digest-mismatched probe never does. A missing
        or ``unknown`` ledger channel is ``found=None``; only a recorded
        ``none_found`` is an explicitly bounded negative.
    repair: strip/repair record detail with candidate-vs-validated status
        preserved exactly (never upgraded). The already-applied repair
        (ledger run_digest equals its variant digest) is preferred over
        parent-matching candidates; ``records`` keeps every matching record.
        None when nothing matching is recorded.
    static_flags: optional static-audit overlay (a..g flags, ev_* excerpts,
        byte counts). Stale health/solve labels are echoed under
        ``ignored_stale`` only. None when no audit was supplied, the file is
        missing, or the task has no row.
    failing_tests: typed HAR-179 row (JSON list cells decoded, counts
        numeric, booleans genuine; empty missing-evidence cells stay None),
        or None.
    exploit_probes: list with the task's probe-verdict row (bound/unbound
        made explicit), or None when the probe source or the task row is
        absent.
    sources: ``{name: {"path", "sha256", "available", ...}}`` provenance.
    errors: list of genuine (non-fatal) read/parse problems; empty normally.
"""

from __future__ import annotations

import re
from pathlib import Path


_TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
# Legacy HAR-186 callers may ask for a safe non-MiMo leaf; its facet stays null.
_LEAF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
FAILING_TESTS = Path("research/experiments/python-task-ledger/failing_tests.csv")
EXPLOIT = Path("research/experiments/har161-exploit/probe_verdicts.json")
POOL = Path("research/experiments/har108-python-census/pool.json")
ORACLE_PILOT = LEDGER.with_name("oracle_pilot.csv")
ORACLE_SWEEP = LEDGER.with_name("oracle_sweep.csv")
LEAK_SCANS = tuple(
    Path("research/experiments/har177-leak-scan") / filename
    for filename in ("validation10.csv", "pilot20.csv", "pilot2864.csv", "sample100.csv")
)




































































