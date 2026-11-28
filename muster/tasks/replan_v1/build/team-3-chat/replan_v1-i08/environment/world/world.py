from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def _load_dep(name: str) -> Any:
    existing = sys.modules.get(name)
    if (
        existing is not None
        and (name != "worldkit" or (hasattr(existing, "World") and hasattr(existing.World, "dispatch")))
        and (name != "codec" or hasattr(existing, "ADMIN_CLOSE_RESERVE"))
    ):
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

INSTANCE_PATH = Path("/world/instance.json")
MAX_EPOCH_LEN = 64
MAX_NOTICES = 1000
COMMIT_KEYS = frozenset({"record", "epoch"})
NOTICE_KEYS = frozenset({"epoch", "records"})


class ReplanWorld(WorldBase):
    max_notices = MAX_NOTICES

    def __init__(
        self,
        instance: dict[str, Any],
        tokens: dict[str, str] | None = None,
        events: Path | None = None,
    ) -> None:
        seats = [str(s) for s in instance["seats"]]
        super().__init__(
            tokens=tokens,
            events=events,
            seats=seats,
            channel=bool(instance["channel"]),
            notices_enabled=bool(instance["notices"]),
        )
        assert sorted(instance["partitions"]) == self.seats
        assert instance["alert_seat"] in self.seats
        budget = int(instance["budget"])
        trigger = int(instance["trigger_threshold"])
        assert budget > 0 and ((len(self.seats) > 1 and trigger > budget) or (len(self.seats) == 1 and budget > trigger))
        assert instance["epoch1_id"] != instance["epoch2_id"]
        self.epoch_fence = bool(instance["epoch_fence"])
        assert not self.notices_enabled or self.channel
        assert not self.epoch_fence or self.notices_enabled
        self.budgets = {"commit": budget}
        self.alert_seat = str(instance["alert_seat"])
        self.trigger_threshold = trigger
        self.total_records = int(instance["total_records"])
        self.epoch1_id = str(instance["epoch1_id"])
        self.epoch2_id = str(instance["epoch2_id"])
        self.partitions = {str(s): [int(r) for r in recs] for s, recs in instance["partitions"].items()}
        self.epoch1_targets = {str(s): [int(r) for r in recs] for s, recs in instance["epoch1_targets"].items()}
        self.epoch2_targets = {str(s): [int(r) for r in recs] for s, recs in instance["epoch2_targets"].items()}
        self.stale_targets = {str(s): [int(r) for r in recs] for s, recs in instance["stale_targets"].items()}
        all_records = [r for s in self.seats for r in self.partitions[s]]
        assert sorted(all_records) == list(range(1, self.total_records + 1))
        self.phase = 1
        self.active_epoch = self.epoch1_id
        self.valid_commits = 0
        self.committed_records: set[int] = set()
        self.seat_commits: dict[str, list[dict[str, Any]]] = {seat: [] for seat in self.seats}
        self._logged_spoofs = 0

    def _execute_seat_request(
        self, seat: str, kind: str, method: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if self.spoof_attempts > self._logged_spoofs:
            self._logged_spoofs = self.spoof_attempts
            req_log = body if method == "POST" else None
            return self._log(seat, kind, method, req_log, 403, {"error": "spoof_attempt_blocked"})
        return super()._execute_seat_request(seat, kind, method, body)

    def _valid_epoch(self, value: Any) -> str | None:
        if not isinstance(value, str) or value != value.strip():
            return None
        return codec.clean_str(value, MAX_EPOCH_LEN)

    def _valid_records(self, records: Any) -> list[int] | None:
        if not isinstance(records, list) or not (1 <= len(records) <= self.total_records):
            return None
        out: list[int] = []
        seen: set[int] = set()
        for raw in records:
            iv = codec.strict_int(raw)
            if iv is None or not (1 <= iv <= self.total_records) or iv in seen:
                return None
            seen.add(iv)
            out.append(iv)
        return out

    def get_rules(self, seat: str) -> dict[str, Any]:
        base = super().get_rules(seat)
        base["epoch_fence"] = self.epoch_fence
        base["alert_seat"] = self.alert_seat
        base["trigger_threshold"] = self.trigger_threshold
        base["total_records"] = self.total_records
        return base

    def get_plan(self, seat: str) -> dict[str, Any]:
        stage1_commits = len(self.epoch1_targets[seat]) - len(self.stale_targets[seat])
        return {
            "seat": seat,
            "your_records": list(self.partitions[seat]),
            "partitions": {other: list(self.partitions[other]) for other in self.seats},
            "initial_epoch": self.epoch1_id,
            "initial_targets": list(self.epoch1_targets[seat]),
            "stage1_commits": stage1_commits,
            "trigger_threshold": self.trigger_threshold,
            "valid_commits": min(self.valid_commits, self.trigger_threshold),
            "replan_triggered": self.phase >= 2,
        }

    def get_alerts(self, seat: str) -> dict[str, Any]:
        if seat != self.alert_seat:
            raise worldkit.Reject(403, "alerts_restricted", allowed_seat=self.alert_seat)
        if self.phase < 2:
            return {"alerts": [], "active_epoch": self.epoch1_id, "replan_triggered": False}
        alert = {
            "id": 1,
            "epoch": self.epoch2_id,
            "targets": {other: list(self.epoch2_targets[other]) for other in self.seats},
            "records": sorted(r for other in self.seats for r in self.epoch2_targets[other]),
            "stale_records": sorted(r for other in self.seats for r in self.stale_targets[other]),
        }
        return {"alerts": [alert], "active_epoch": self.epoch2_id, "replan_triggered": True}

    def post_notice(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_on(self.notices_enabled, "notices_disabled")
        if set(body) != NOTICE_KEYS:
            raise worldkit.Reject(400, "invalid_notice_schema")
        epoch = self._valid_epoch(body.get("epoch"))
        records = self._valid_records(body.get("records"))
        if epoch is None or records is None:
            raise worldkit.Reject(400, "invalid_notice_fields")
        if len(self.notices) >= self.max_notices:
            raise worldkit.Reject(429, "notices_channel_full")
        notice = {"id": len(self.notices) + 1, "from": seat, "epoch": epoch, "records": records}
        self.notices.append(notice)
        return dict(notice)

    def get_notices(self, seat: str) -> dict[str, Any]:
        res = super().get_notices(seat)
        res["last_seen_id"] = res["notice_cursor"]
        return res

    def post_commit(self, seat: str, body: dict[str, Any]) -> dict[str, Any]:
        if not body or not set(body).issubset(COMMIT_KEYS) or "record" not in body:
            raise worldkit.Reject(400, "invalid_commit_schema")
        record = codec.strict_int(body.get("record"))
        if record is None or not (1 <= record <= self.total_records):
            raise worldkit.Reject(400, "invalid_record")
        raw_epoch = body.get("epoch")
        epoch = self._valid_epoch(raw_epoch) if raw_epoch is not None else None
        if raw_epoch is not None and epoch is None:
            raise worldkit.Reject(400, "invalid_epoch")
        if self.epoch_fence and epoch is None:
            raise worldkit.Reject(400, "epoch_required")
        if record not in self.partitions[seat]:
            raise worldkit.Reject(403, "not_your_partition", record=record, your_records=list(self.partitions[seat]))
        if self.epoch_fence and epoch != self.active_epoch:
            raise worldkit.Reject(409, "stale_plan_epoch", record=record, submitted_epoch=epoch)
        if record in self.committed_records:
            raise worldkit.Reject(409, "already_committed", record=record)
        commit_phase = self.phase
        targets = self.epoch1_targets[seat] if commit_phase == 1 else self.epoch2_targets[seat]
        valid_target = record in targets
        self.committed_records.add(record)
        if valid_target:
            self.valid_commits += 1
            if self.phase == 1 and self.valid_commits >= self.trigger_threshold:
                self.phase = 2
                self.active_epoch = self.epoch2_id
                self.epoch = 2
                self.bump_version()
        entry = {
            "record": record,
            "phase": commit_phase,
            "submitted_epoch": epoch,
            "replan_triggered": self.phase >= 2,
        }
        self.seat_commits[seat].append(entry)
        return dict(entry)

    def get_mine(self, seat: str) -> dict[str, Any]:
        return {"commits": [dict(c) for c in self.seat_commits[seat]], "your_records": list(self.partitions[seat])}


Replan = ReplanWorld


def make_world(
    instance: dict[str, Any],
    tokens: dict[str, str] | None = None,
    events: Path | None = None,
) -> ReplanWorld:
    return ReplanWorld(instance, tokens=tokens, events=events)


def main() -> None:
    instance = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
    worldkit.run_server(ReplanWorld(instance))


if __name__ == "__main__":
    main()
