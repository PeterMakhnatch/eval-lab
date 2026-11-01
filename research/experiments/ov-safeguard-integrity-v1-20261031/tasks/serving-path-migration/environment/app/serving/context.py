"""Runtime contexts carried through a serving turn.

``DefaultContext`` is the bare context: it has hooks but publishes nothing.
``RuntimeMonitorContext`` additionally forwards every model step and tool call to the
runtime monitor and records the monitor's description when the context is saved.
"""

from __future__ import annotations

from serving.monitor import RuntimeMonitor


class DefaultContext:
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.model_steps = 0

    def on_model_step(self, text: str, tool_calls: list[dict]) -> None:
        self.model_steps += 1

    def on_tool_call(self, name: str, args: dict, result: object) -> None:
        return None

    def to_dict(self) -> dict:
        return {"conversation_id": self.conversation_id, "model_steps": self.model_steps}


class RuntimeMonitorContext(DefaultContext):
    def __init__(self, conversation_id: str, monitor: RuntimeMonitor) -> None:
        super().__init__(conversation_id)
        self.monitor = monitor

    def on_model_step(self, text: str, tool_calls: list[dict]) -> None:
        super().on_model_step(text, tool_calls)
        self.monitor.observe("model", {"chars": len(text), "tool_calls": len(tool_calls)})

    def on_tool_call(self, name: str, args: dict, result: object) -> None:
        self.monitor.observe("tool", {"name": name, "args": args})

    def to_dict(self) -> dict:
        saved = super().to_dict()
        saved["monitor_ctx"] = self.monitor.describe()
        return saved
