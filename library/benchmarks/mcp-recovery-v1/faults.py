"""In-process fault controller used by CI-contract templates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from evallab.benchmark_program_contracts import FaultClass

__all__ = ["FaultClass", "FaultSpec", "FaultController"]


@dataclass
class FaultSpec:
    target_tool: str
    fault_class: FaultClass
    persistence: int
    trigger_condition: Callable[[dict[str, Any]], bool] | None = None
    corrupted_payload: Any = None


class FaultController:
    def __init__(self, specs: list[FaultSpec] | None = None):
        self.specs = specs or []
        self.hit_counts: dict[str, int] = {}
        self.injected_log: list[dict[str, Any]] = []

    def evaluate(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, FaultClass | None, Any]:
        for spec in self.specs:
            if spec.target_tool != tool_name:
                continue
            current = self.hit_counts.get(tool_name, 0)
            if current >= spec.persistence:
                continue
            if spec.trigger_condition and not spec.trigger_condition(arguments):
                continue
            self.hit_counts[tool_name] = current + 1
            self.injected_log.append(
                {
                    "tool": tool_name,
                    "fault_class": spec.fault_class.value,
                    "hit_index": self.hit_counts[tool_name],
                    "persistence": spec.persistence,
                }
            )
            if spec.fault_class == FaultClass.PERSISTENT_SIGNATURE_ERROR:
                return True, spec.fault_class, {"code": 403, "message": "Permission denied: access token lacks write scope"}
            if spec.fault_class == FaultClass.PERSISTENT_SCHEMA_MISMATCH:
                return True, spec.fault_class, {"code": 404, "message": f"Resource not found for tool {tool_name}"}
            if spec.fault_class == FaultClass.TRANSIENT_NETWORK_TIMEOUT:
                return True, spec.fault_class, {"code": 408, "message": "Request timeout"}
            if spec.fault_class == FaultClass.TRANSIENT_HTTP_5XX:
                return True, spec.fault_class, "<html>502 Bad Gateway"
            if spec.fault_class == FaultClass.SILENT_WRONG_PAYLOAD:
                return True, spec.fault_class, spec.corrupted_payload or {"status": "ok", "result": "corrupted_silent_record"}
        return False, None, None
