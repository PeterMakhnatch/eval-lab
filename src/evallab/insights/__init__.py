"""Insight engine: always-on analysts over Eval Lab experiment results.

Experiments write reports and receipts; this package turns them into a ledger
of checked claims, computes deterministic statistical signals over the whole
ledger, and runs analyst agents ("lenses") that look for what statistics alone
does not show: connections between experiments, mechanisms that explain
several results, flaws in load-bearing claims, anomalies and new experiments.

Every agent output is checked by code against the ledger, graded by a referee
from a different model family and ranked by a synthesizer before it reaches
the board. Nothing here accepts a finding: a named human does, through
``evallab insights review``. See ``docs/insights.md``.
"""
