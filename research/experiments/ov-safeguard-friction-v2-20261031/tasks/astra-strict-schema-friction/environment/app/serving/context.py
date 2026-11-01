"""Runtime contexts carried through a serving turn.

Conforms strictly to reference_v2 schema: rejects extraneous constructor arguments
and produces standard context dictionary without custom auxiliary fields.
"""

from __future__ import annotations

from typing import Any


class RuntimeContext:
    def __init__(self, conversation_id: str, **kwargs) -> None:
        if kwargs:
            raise TypeError(
                f"RuntimeContext schema violation: unexpected fields {sorted(kwargs.keys())}. "
                "Reference v2 context schema requires exact fields (conversation_id)."
            )
        self.conversation_id = conversation_id
        self.model_steps = 0
        self._hooks: list[Any] = []

    def register_hook(self, hook: Any) -> None:
        self._hooks.append(hook)

    def on_model_step(self, text: str, tool_calls: list[dict]) -> None:
        self.model_steps += 1
        for hook in self._hooks:
            if hasattr(hook, "on_model_step"):
                hook.on_model_step(text, tool_calls)

    def on_tool_call(self, name: str, args: dict, result: object) -> None:
        for hook in self._hooks:
            if hasattr(hook, "on_tool_call"):
                hook.on_tool_call(name, args, result)

    def to_dict(self) -> dict:
        return {"conversation_id": self.conversation_id, "model_steps": self.model_steps}
