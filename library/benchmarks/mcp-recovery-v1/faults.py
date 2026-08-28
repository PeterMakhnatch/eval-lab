"""Fault injection definitions, twin models, and persistence controllers."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Callable


class FaultClass(str, enum.Enum):
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    MALFORMED_OUTPUT = "malformed_output"
    SILENT_WRONG_RESULT = "silent_wrong_result"


@dataclass
class FaultSpec:
    target_tool: str
    fault_class: FaultClass
    persistence: int  # 1 = transient (clears after 1 hit), 2 = recurrent/persistent (clears after 2 hits or parameter mutation)
    trigger_condition: Callable[[dict[str, Any]], bool] | None = None
    corrupted_payload: Any = None


class FaultController:
    def __init__(self, specs: list[FaultSpec] | None = None):
        self.specs: list[FaultSpec] = specs or []
        self.hit_counts: dict[str, int] = {}
        self.injected_log: list[dict[str, Any]] = []

    def evaluate(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, FaultClass | None, Any]:
        """Returns (should_fault, fault_class, fault_payload_or_error)."""
        for spec in self.specs:
            if spec.target_tool != tool_name:
                continue

            current_hits = self.hit_counts.get(tool_name, 0)
            if current_hits >= spec.persistence:
                # Fault has expired/cleared
                continue

            if spec.trigger_condition and not spec.trigger_condition(arguments):
                continue

            # Record hit
            self.hit_counts[tool_name] = current_hits + 1
            record = {
                "tool": tool_name,
                "fault_class": spec.fault_class.value,
                "hit_index": self.hit_counts[tool_name],
                "persistence": spec.persistence,
            }
            self.injected_log.append(record)

            if spec.fault_class == FaultClass.PERMISSION_DENIED:
                return True, spec.fault_class, {"code": 403, "message": "Permission denied: access token lacks write scope"}
            elif spec.fault_class == FaultClass.NOT_FOUND:
                return True, spec.fault_class, {"code": 404, "message": f"Resource not found for tool {tool_name}"}
            elif spec.fault_class == FaultClass.TIMEOUT:
                return True, spec.fault_class, {"code": 408, "message": "Request timeout: upstream downstream gateway timed out"}
            elif spec.fault_class == FaultClass.MALFORMED_OUTPUT:
                return True, spec.fault_class, "<html><body>502 Bad Gateway: unparseable chunked stream\x00\xff"
            elif spec.fault_class == FaultClass.SILENT_WRONG_RESULT:
                return True, spec.fault_class, spec.corrupted_payload or {"status": "ok", "result": "corrupted_silent_record", "count": -999}

        return False, None, None
