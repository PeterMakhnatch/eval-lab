"""Failure Library: provenance-first broken tasks and failed or hacked runs.

A :class:`~evallab.failure_library.records.Finding` states that one exact task
version is broken and carries the evidence that makes the claim checkable.
A :class:`~evallab.failure_library.records.RunRecord` preserves one agent
attempt worth learning from. Verification tiers are derived from evidence
kinds, never asserted, so a judge label can never pass for an executed one.

Findings are compact and live in Git under ``research/failure-library/``;
run records are large and live in local state with a committed manifest.
See ``research/failure-library/README.md``.
"""
