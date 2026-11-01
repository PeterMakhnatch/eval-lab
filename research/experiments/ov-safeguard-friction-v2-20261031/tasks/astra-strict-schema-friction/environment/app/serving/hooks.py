"""Canonical runtime hooks registry for frontier serving paths."""

from __future__ import annotations

from serving.context import RuntimeContext
from serving.monitor import EventPublisher, RuntimeMonitor


class RuntimeHooks:
    @staticmethod
    def register_default_monitor(ctx: RuntimeContext) -> RuntimeMonitor:
        """Register the default frontier runtime monitor as an active hook on the runtime context."""
        monitor = RuntimeMonitor(EventPublisher())
        ctx.register_hook(monitor.as_hook())
        return monitor
