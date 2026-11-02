"""Exceptions for quota router."""

from __future__ import annotations


class QuotaRouterError(Exception):
    """Base exception for quota router errors."""


class RouteExhausted(QuotaRouterError):
    """Raised when a route exceeds its quota gate or capacity."""

    @classmethod
    def to_application_error(cls, message: str) -> Exception:
        """Convert to Temporal ApplicationError if temporalio is available."""
        try:
            from temporalio.exceptions import ApplicationError

            return ApplicationError(message, non_retryable=True)
        except ImportError:
            return cls(message)


class MalformedUsageError(QuotaRouterError):
    """Raised when `omp usage` output cannot be parsed."""


class UnknownRouteError(QuotaRouterError):
    """Raised when an unrecognized route is queried."""
