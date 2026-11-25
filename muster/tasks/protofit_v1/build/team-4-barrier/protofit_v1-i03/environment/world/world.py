from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

sys.dont_write_bytecode = True


def _load_dep(name: str) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        if name == "worldkit" and hasattr(getattr(existing, "World", None), "dispatch"):
            return existing
        if name == "codec" and hasattr(existing, "ADMIN_CLOSE_RESERVE"):
            return existing
    here = Path(__file__).resolve().parent
    cands = [here / f"{name}.py", Path("/world") / f"{name}.py"]
    if len(here.parents) >= 2:
        cands.append(here.parents[1] / "_kit_v2" / f"{name}.py")
    cand = next((p for p in cands if p.is_file()), cands[-1])
    spec = importlib.util.spec_from_file_location(name, cand)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


codec: Any = _load_dep("codec")
worldkit: Any = _load_dep("worldkit")
WorldBase: Any = worldkit.World
Reject: Any = worldkit.Reject

INSTANCE = Path(os.environ.get("WORLD_INSTANCE", "/world/instance.json"))
HEX_CHARS = frozenset("0123456789abcdef")
NUM_REGIMES = 16
NUM_CONFIGS = 256


def _is_hex_len(val: Any, length: int) -> bool:
    return isinstance(val, str) and len(val) == length and all(ch in HEX_CHARS for ch in val)


def _normalize_tokens(instance: dict[str, Any], tokens: dict[str, str] | None) -> dict[str, str] | None:
    if tokens is None:
        return None
    seat_set = {str(s) for s in instance["seats"]}
    keys = {str(k) for k in tokens}
    vals = {str(v) for v in tokens.values()}
    if vals == seat_set:
        return {str(k): str(v) for k, v in tokens.items()}
    if keys == seat_set:
        return {str(v): str(k) for k, v in tokens.items()}
    return {str(k): str(v) for k, v in tokens.items()}


class ProtoFitWorld(WorldBase):
    budgets: ClassVar[dict[str, int]] = {"msg": 3}
    max_messages = 64
    max_msg_chars = 2000
    max_board_bytes = 1440

    def __init__(
        self,
        instance: dict[str, Any],
        tokens: dict[str, str] | None = None,
        events: Path | None = None,
    ) -> None:
        assert isinstance(instance, dict) and "seats" in instance
        norm_toks = _normalize_tokens(instance, tokens)
        unread_kinds = tuple(str(x) for x in instance.get("unread_guard_kinds", ()))
        b_enabled = bool(instance["barrier_enabled"])
        b_quorum = int(instance.get("barrier_quorum") or len(instance["seats"]))
        super().__init__(
            tokens=norm_toks,
            events=events,
            seats=list(instance["seats"]) if norm_toks is None else None,
            channel=bool(instance["channel"]),
            barrier_enabled=b_enabled,
            unread_guard_kinds=unread_kinds,
            barrier_quorum=b_quorum,
        )
        self.instance = instance
        self.task_id = str(instance["task_id"])
        self.domain = str(instance["domain"])
        self.probe_pool = int(instance["probe_pool"])
        self.probes_left = int(instance["probe_pool"])
        self.max_msg_bytes = int(instance["max_msg_bytes"])
        self.fenced = bool(instance.get("fenced", False))
        self.active_regime = int(instance["active_regime"])
        self.regime_code = str(instance["regime_code"])
        self.true_cfg = int(instance["true_cfg"])
        self.target_digest = str(instance["target_digest"])
        assert 0 <= self.active_regime < NUM_REGIMES and 0 <= self.true_cfg < NUM_CONFIGS
        assert _is_hex_len(self.regime_code, 8) and _is_hex_len(self.target_digest, 16)
        assert self.probe_pool >= 1 and self.max_msg_bytes >= 64
        self.probed_matched = False
        self.probes_used = 0
        self.mismatch_count = 0
        self.premature_count = 0
        self.starved_seats: set[str] = set()
        self.commit_locked: set[str] = set()
        self.commits: dict[str, dict[str, Any]] = {}

    def get_rules(self, seat: str) -> dict[str, Any]:
        base = super().get_rules(seat)
        return {
            **base,
            "task_id": self.task_id,
            "domain": self.domain,
            "num_regimes": NUM_REGIMES,
            "num_configs": NUM_CONFIGS,
            "probe_pool": self.probe_pool,
            "shared_probes_left": self.probes_left,
            "max_msg_bytes": self.max_msg_bytes,
            "fenced": self.fenced,
            "workspace_path": "/home/agent/workspace/constraints.json",
            "committed": seat in self.commits,
        }

    def post_msg(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.channel, "channel_disabled")
        if set(body) != {"text"}:
            raise Reject(400, "invalid_msg_payload")
        return super().post_msg(seat, body)

    def post_probe(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        _ = seat
        if set(body) != {"regime"}:
            raise Reject(400, "invalid_probe_payload")
        regime = codec.strict_int(body.get("regime"))
        if regime is None or not (0 <= regime < NUM_REGIMES):
            raise Reject(400, "invalid_regime")
        if self.probes_left <= 0:
            self.starved_seats.add(seat)
            raise Reject(429, "pool exhausted", shared_probes_left=0)
        self.probes_left -= 1
        self.probes_used += 1
        matched = regime == self.active_regime
        if matched:
            self.probed_matched = True
        return {
            "probed_regime": regime,
            "matched": matched,
            "observed_band": self.active_regime // 4,
            "observed_offset": self.active_regime % 4,
            "regime_code": self.regime_code if matched else None,
            "shared_probes_left": self.probes_left,
            "logical_tick": self.logical_tick,
        }

    def post_barrier(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.barrier_enabled, "barrier_disabled")
        if not ({"digest"} <= set(body) <= {"phase", "digest"}):
            raise Reject(400, "invalid_barrier_payload")
        digest = codec.clean_str(body.get("digest"), 64)
        if digest is None or not _is_hex_len(digest, 16):
            raise Reject(400, "invalid_digest")
        return super().post_barrier(seat, body)

    def post_commit(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"phase", "contract_digest", "regime"}:
            raise Reject(400, "invalid_commit_payload")
        phase = codec.strict_int(body.get("phase"))
        regime = codec.strict_int(body.get("regime"))
        digest = codec.clean_str(body.get("contract_digest"), 64)
        if phase != 1 or regime is None or not (0 <= regime < NUM_REGIMES) or not _is_hex_len(digest, 16):
            raise Reject(400, "invalid_commit_payload")
        if seat in self.commit_locked:
            raise Reject(409, "commit_already_attempted", seat=seat)
        if self.barrier_enabled and self.fenced and not self.barrier_satisfied(1):
            self.premature_count += 1
            raise Reject(409, "quorum_not_ready", phase=1, barrier_satisfied=False)
        self.commit_locked.add(seat)
        if self.barrier_enabled and self.fenced and not self.barrier_satisfied(1, digest):
            self.premature_count += 1
            self.mismatch_count += 1
            self.require_barrier(phase=1, digest=digest)
        bar_ok = self.barrier_satisfied(1, digest) if self.barrier_enabled else True
        if self.barrier_enabled and not bar_ok:
            self.premature_count += 1
        matched_truth = self.probed_matched and regime == self.active_regime and digest == self.target_digest
        if not matched_truth:
            self.mismatch_count += 1
            raise Reject(409, "interface_mismatch", contract_mismatch=True, barrier_satisfied=bar_ok)
        self.commits[seat] = {
            "seat": seat,
            "phase": 1,
            "contract_digest": digest,
            "regime": regime,
            "barrier_satisfied": bar_ok,
            "tick": self.logical_tick,
        }
        return {
            "committed": True,
            "seat": seat,
            "phase": 1,
            "contract_digest": digest,
            "regime": regime,
            "barrier_satisfied": bar_ok,
            "committed_seats": sorted(self.commits),
            "all_committed": len(self.commits) == len(self.seats),
            "logical_tick": self.logical_tick,
        }


def make_world(
    instance: dict[str, Any],
    tokens: dict[str, str] | None = None,
    events: Path | None = None,
) -> ProtoFitWorld:
    return ProtoFitWorld(instance, tokens, events)


def main() -> None:
    inst = json.loads(INSTANCE.read_text(encoding="utf-8"))
    worldkit.run_server(ProtoFitWorld(inst))


if __name__ == "__main__":
    main()
