"""Custom error classes for CertifyGold durable workflows."""

from __future__ import annotations


class CertifyWorkflowError(Exception):
    """Base exception for all certification workflow errors."""


class InfraError(CertifyWorkflowError):
    """Transient infrastructure failure.

    Examples: Modal sandbox startup failure, network disconnect, worker restart,
    transient 502/503 from backend provider.
    Eligible for automatic retry with exponential backoff.
    """


class CapReached(CertifyWorkflowError):
    """Execution cannot proceed because spend cap has been reached or would be exceeded.

    Workflow parks waiting for a `raise_cap` signal with a configurable timeout.
    If timeout fires without approval, workflow finalizes and writes a PARTIAL manifest.
    """


class SubmissionFailure(CertifyWorkflowError):
    """Submission or test candidate execution failure within the sandbox.

    Examples: non-zero exit code, test failure, syntax error in candidate solution.
    This represents an evaluated candidate result, NOT an infrastructure failure.
    It is recorded in result.json and must NOT be retried.
    """
