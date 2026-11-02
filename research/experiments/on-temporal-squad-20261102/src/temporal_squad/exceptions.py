from __future__ import annotations

from temporalio.exceptions import ApplicationError


class TemporalSquadError(Exception):
    """Base error for temporal-squad runner."""


class TransientLaunchError(TemporalSquadError):
    """Retryable activity error when launching worker fails transiently."""


class RouteExhausted(TemporalSquadError):
    """Non-retryable activity error when route quota is exhausted."""

    @classmethod
    def to_application_error(cls, message: str = "Route quota exhausted") -> ApplicationError:
        return ApplicationError(message, type="RouteExhausted", non_retryable=True)


class ReportMissing(TemporalSquadError):
    """Error raised when worker report is missing or lacks terminal marker."""

    @classmethod
    def to_application_error(
        cls, message: str = "Report missing or lacks DS-DONE/DS-BLOCKED"
    ) -> ApplicationError:
        return ApplicationError(message, type="ReportMissing", non_retryable=True)


class BudgetExceeded(TemporalSquadError):
    """Non-retryable error when squad or item budget is exceeded."""

    @classmethod
    def to_application_error(cls, message: str = "Budget exceeded") -> ApplicationError:
        return ApplicationError(message, type="BudgetExceeded", non_retryable=True)


class ItemCancelled(TemporalSquadError):
    """Error raised when an item execution is cancelled."""

    @classmethod
    def to_application_error(cls, message: str = "Item was cancelled") -> ApplicationError:
        return ApplicationError(message, type="ItemCancelled", non_retryable=True)
