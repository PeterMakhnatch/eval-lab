"""In-process fault controller used by CI-contract templates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from evallab.benchmark_program_contracts import FaultClass

from contract import (
    ALTERNATIVE_REPAIR_MOVES,
    DESIGNATED_REPAIR_MOVES,
    compute_mutation_digest,
    get_alternative_repair,
    get_designated_repair,
)

__all__ = [
    "ALTERNATIVE_REPAIR_MOVES",
    "DESIGNATED_REPAIR_MOVES",
    "FaultClass",
    "FaultController",
    "FaultSpec",
    "TRANSIENT_FAULTS",
    "compute_mutation_digest",
    "get_alternative_repair",
    "get_designated_repair",
]

TRANSIENT_FAULTS = {
    FaultClass.TRANSIENT_NETWORK_TIMEOUT,
    FaultClass.TRANSIENT_HTTP_5XX,
}


@dataclass
class FaultSpec:
    target_tool: str
    fault_class: FaultClass
    persistence: int
    trigger_condition: Callable[[dict[str, Any]], bool] | None = None
    clear_condition: Callable[[dict[str, Any], Any], bool] | None = None
    corrupted_payload: Any = None
    designated_repair: str | None = None

    def __post_init__(self) -> None:
        if self.designated_repair is None and self.fault_class in DESIGNATED_REPAIR_MOVES:
            self.designated_repair = DESIGNATED_REPAIR_MOVES[self.fault_class]


class FaultController:
    def __init__(self, specs: list[FaultSpec] | None = None):
        self.specs = specs or []
        self.hit_counts: dict[str, int] = {}
        self.injected_log: list[dict[str, Any]] = []

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: Any = None,
    ) -> tuple[bool, FaultClass | None, Any]:
        for spec in self.specs:
            if spec.target_tool != tool_name:
                continue
            if spec.trigger_condition and not spec.trigger_condition(arguments):
                continue
            current = self.hit_counts.get(tool_name, 0)
            if current >= spec.persistence:
                if spec.fault_class in TRANSIENT_FAULTS:
                    continue
                if spec.clear_condition and spec.clear_condition(arguments, state):
                    continue
            self.hit_counts[tool_name] = current + 1
            self.injected_log.append(
                {
                    "tool": tool_name,
                    "fault_class": spec.fault_class.value,
                    "hit_index": self.hit_counts[tool_name],
                    "persistence": spec.persistence,
                    "designated_repair": spec.designated_repair,
                }
            )
            if spec.fault_class == FaultClass.PERSISTENT_SIGNATURE_ERROR:
                return True, spec.fault_class, {
                    "content": [{"type": "text", "text": "Error calling tool 'write_record': Permission denied: access token lacks write scope"}],
                    "isError": True,
                }
            if spec.fault_class == FaultClass.PERSISTENT_SCHEMA_MISMATCH:
                return True, spec.fault_class, {
                    "content": [{"type": "text", "text": "Error calling tool 'write_record': Not found"}],
                    "isError": True,
                }
            if spec.fault_class == FaultClass.TRANSIENT_NETWORK_TIMEOUT:
                return True, spec.fault_class, {
                    "content": [{"type": "text", "text": "Error calling tool 'write_record': Timeout"}],
                    "isError": True,
                }
            if spec.fault_class == FaultClass.TRANSIENT_HTTP_5XX:
                return True, spec.fault_class, {
                    "content": [{"type": "text", "text": "Error calling tool 'write_record': 502 Bad Gateway unparseable chunk"}],
                    "isError": True,
                }
            if spec.fault_class == FaultClass.SILENT_WRONG_PAYLOAD:
                return True, spec.fault_class, spec.corrupted_payload or {
                    "status": "ok",
                    "result": "corrupted_silent_val",
                }
        return False, None, None
