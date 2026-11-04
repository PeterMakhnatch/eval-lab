"""Bounded J4 recovery without changing requests, successful receipts, or route gates.

Private --error-receipts/--resume-source-errors directories contain JSON objects:
{prompt_sha256, model_requested, status, payload, timestamp}. `payload` is the
actual complete gateway response, never a reconstructed or synthetic response.
The observer also stores `request` and exception-only transport observations.
Only a matching failed call in calls-j4.jsonl (or calls-j3.jsonl) can authorize
recovery of a historic stop. All unknown errors remain stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import j4

CONFIG = Path("/private/tmp/ov1106/shared/nofallback.yml")


def now() -> str:
    return datetime.now(UTC).isoformat()


def policy(payload: Any) -> bool:
    if isinstance(payload, dict):
        message = payload.get("message")
        machine_code = isinstance(message, str) and re.search(r"\(code=cyber_policy\)\s*$", message)
        return payload.get("code") == "cyber_policy" or bool(machine_code) or any(policy(v) for v in payload.values())
    return isinstance(payload, list) and any(policy(v) for v in payload)


class Driver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out).resolve()
        if self.out.is_relative_to(j4.HERE.parents[2]):
            raise ValueError("raw driver receipts must stay outside the repository")
        self.rows = [json.loads(line) for line in Path(args.runs).read_bytes().splitlines() if line.strip()]
        self.items = [j4.prepare(row, j4.code_identity(args.baseline_j3)) for row in self.rows]
        self.original_request = j4.Gateway.request
        self.source_dirs = [self.out / "transport-errors"] + [Path(p) for p in args.error_receipts]
        self.session = uuid.uuid4().hex
        self.state_path = self.out / "driver-state.json"
        self.state = json.loads(self.state_path.read_bytes()) if self.state_path.exists() else {"retries": 0, "events": []}

    def event(self, action: str, **values: Any) -> None:
        self.state["events"].append({"timestamp": now(), "action": action, **values})
        j4.atomic_json(self.state_path, self.state)

    def archive(self, path: Path, reason: str) -> None:
        target = self.out / "stop-history" / f"{uuid.uuid4().hex}-{path.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
        self.event("archive", source=str(path), archive=str(target), reason=reason)

    async def observe(self, gateway: Any, path: str, body: dict[str, Any]) -> Any:
        # Observe the same object passed to the existing request method. Do not
        # normalize, retry, rephrase, or replace any request or response here.
        messages = body.get("messages", [])
        prompt = messages[-1].get("content") if messages else None
        record = {"timestamp": now(), "session": self.session, "path": path,
                  "request": body, "model_requested": body.get("model"),
                  "prompt_sha256": j4.sha(prompt.encode()) if isinstance(prompt, str) else None}
        try:
            status, payload = await self.original_request(gateway, path, body)
        except Exception as exc:
            record.update(status=None, exception=f"{type(exc).__name__}: {exc}")
            self.capture(record)
            raise
        if status != 200 or j4._gateway.base._served_model(payload) != body.get("model"):
            record.update(status=status, payload=payload)
            self.capture(record)
        return status, payload

    def capture(self, record: dict[str, Any]) -> None:
        path = self.out / "transport-errors" / f"{record['prompt_sha256']}-{uuid.uuid4().hex}.json"
        j4.atomic_json(path, record)

    def admission(self) -> None:
        raw = Path(self.args.runs).read_bytes()
        j4.verify_freeze(self.args, self.rows, raw)
        if any(str(row.get("split", "")).lower() != "dev" for row in self.rows):
            freeze = json.loads(Path(self.args.freeze).read_bytes())
            root = Path(j4.git("rev-parse", "--show-toplevel").decode().strip())
            relative = str(Path(__file__).resolve().relative_to(root))
            if freeze.get("files", {}).get(relative) != j4.sha(Path(__file__).read_bytes()):
                raise ValueError("held-out freeze must include exact run_study.py hash")

    def observations(self, prompt_sha: str) -> list[tuple[Path, dict[str, Any]]]:
        found = []
        for directory in self.source_dirs:
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                value = json.loads(path.read_bytes())
                if value.get("prompt_sha256") == prompt_sha and value.get("model_requested") == j4.MODEL:
                    request = value.get("request")
                    if request is not None:
                        messages = request.get("messages", [])
                        prompt = messages[-1].get("content") if messages else None
                        if not isinstance(prompt, str) or j4.sha(prompt.encode()) != prompt_sha or request.get("model") != j4.MODEL:
                            raise ValueError(f"invalid raw request identity: {path}")
                    found.append((path, value))
        return found

    def failed_call(self, stop: dict[str, Any]) -> dict[str, Any] | None:
        path = self.out / ("calls-j3.jsonl" if self.args.baseline_j3 else "calls-j4.jsonl")
        if not path.exists():
            return None
        for line in reversed(path.read_bytes().splitlines()):
            call = json.loads(line)
            if (call.get("status") == stop.get("status")
                    and call.get("model_served") == stop.get("model_served")
                    and call.get("model_requested") == j4.MODEL and call.get("ts") and stop.get("timestamp")):
                called = datetime.fromisoformat(call["ts"].replace("Z", "+00:00"))
                stopped = datetime.fromisoformat(stop["timestamp"].replace("Z", "+00:00"))
                elapsed = (stopped - called).total_seconds()
                if 0 <= elapsed <= float(call.get("latency_s") or 0) + 2:
                    return call
        return None

    def record_refusal(self, call: dict[str, Any], path: Path, observation: dict[str, Any]) -> None:
        item = next((item for item in self.items if item["row"]["run_key"] == call["run_key"]), None)
        if item is None:
            raise ValueError("failed call is not in the selected runs")
        index = call["window"]
        if item["identity"]["prompts_sha256"][index] != call["prompt_sha256"]:
            raise ValueError("failed call prompt identity mismatch")
        result = j4.load_receipt(item, self.out)
        if any(entry["window"] == index for entry in result["windows"]):
            return
        window = item["windows"][index]
        result["windows"].append({**call, "parsed": None,
            "error": call.get("error") or "upstream cyber_policy refusal",
            "raw_response": observation["payload"], "provenance_path": str(path.resolve()),
            "provenance_sha256": j4.sha(path.read_bytes()),
            "start": window["start"], "end": window["end"], "steps": window["steps"]})
        result["windows"].sort(key=lambda entry: entry["window"])
        # score_run finalizes only after all original windows have receipts.
        result.update(complete=False, scores=None, score=None)
        result["tokens"] = j4.tokens(result["windows"])
        j4.atomic_json(j4.cache_path(self.out, call["run_key"]), result)
        self.event("record_policy_refusal", run_key=call["run_key"], window=index,
                   prompt_sha256=call["prompt_sha256"], provenance_sha256=j4.sha(path.read_bytes()))

    async def retry(self, reason: str, delay: float | None = None) -> bool:
        count = self.state["retries"]
        if count >= self.args.max_retries:
            self.event("retry_exhausted", reason=reason)
            return False
        seconds = delay if delay is not None else self.args.backoff_seconds * 2 ** count
        self.state["retries"] += 1
        self.event("backoff", reason=reason, seconds=seconds)
        await asyncio.sleep(seconds)
        return True

    async def recover_route(self) -> bool:
        path = self.out / "route-stop.json"
        if not path.exists():
            return True
        stop = json.loads(path.read_bytes())
        if stop.get("model_served") or stop.get("model_requested") != j4.MODEL:
            self.event("blocked", reason="wrong_or_unknown_route", stop=stop)
            return False
        call = self.failed_call(stop)
        if call is None:
            self.event("blocked", reason="missing_matching_call")
            return False
        candidates = []
        for p, value in self.observations(call["prompt_sha256"]):
            http_error = value.get("status") == stop.get("status") and "payload" in value
            transport_error = stop.get("status") == -1 and value.get("exception") and value.get("status") is None
            if not http_error and not transport_error:
                continue
            if value.get("session"):
                started = datetime.fromisoformat(value["timestamp"])
                called = datetime.fromisoformat(call["ts"].replace("Z", "+00:00"))
                if not 0 <= (started - called).total_seconds() < 2:
                    continue
            candidates.append((p, value))
        if stop.get("status") == -1 and candidates:
            if await self.retry("non-HTTP transport transient"):
                self.archive(path, "bounded same-route transport retry")
                return True
            return False
        if not candidates:
            self.event("blocked", reason="missing_raw_error", prompt_sha256=call["prompt_sha256"])
            return False
        # A sidecar never authorizes a nonempty wrong served route.
        if any(j4._gateway.base._served_model(v["payload"]) for _, v in candidates):
            self.event("blocked", reason="nonempty_served_route_in_raw_error")
            return False
        p, v = max(candidates, key=lambda pair: pair[1].get("timestamp", ""))
        if policy(v["payload"]) and isinstance(stop.get("status"), int) and stop["status"] >= 400:
            self.record_refusal(call, p, v)
            self.event("policy_backoff", seconds=self.args.backoff_seconds)
            await asyncio.sleep(self.args.backoff_seconds)
            self.archive(path, "actual upstream cyber_policy refusal recorded; no resubmission")
            return True
        transient = stop.get("status") == 429 or isinstance(stop.get("status"), int) and 500 <= stop["status"] <= 599
        if transient and await self.retry("unserved HTTP transient"):
            self.archive(path, "bounded same-route transient retry")
            return True
        self.event("blocked", reason="unknown_or_exhausted_error", stop=stop)
        return False

    async def recover_quota(self) -> bool:
        path = self.out / "quota-stop.json"
        if not path.exists():
            return True
        stop = json.loads(path.read_bytes())
        if stop.get("ok") is not False or stop.get("fractions") is not None:
            self.event("blocked", reason="quota_cap_or_unknown", stop=stop)
            return False
        # read_quota's compact error omits stderr. Inspect its actual private
        # final attempt receipt; never infer SQLITE_BUSY from a generic failure.
        receipts = sorted((self.out / "quota").glob(f"*-{stop.get('batch', 0):06d}-{stop.get('phase', 'before')}-*.json"))
        if not receipts:
            return False
        receipt = json.loads(receipts[-1].read_bytes())
        if "SQLITE_BUSY" not in (receipt.get("stdout", "") + receipt.get("stderr", "") + receipt.get("error", "")):
            return False
        while await self.retry("quota SQLITE_BUSY exhaustion", self.args.quota_backoff_seconds):
            state = await j4.read_quota(self.out, stop.get("batch", 0), "driver-readmission")
            self.event("quota_readmission", quota=state)
            if state.get("ok") is True and state.get("stop") is False:
                fractions = state.get("fractions", {})
                valid = all(isinstance(fractions.get(key), (int, float))
                            and not isinstance(fractions[key], bool) and math.isfinite(fractions[key])
                            and 0 <= fractions[key] < cap
                            for key, cap in (("codex_7d", .82), ("gemini_weekly", .78)))
                if valid:
                    self.archive(path, "fresh valid below-cap quota admission")
                    return True
            fresh = sorted((self.out / "quota").glob(
                f"*-{stop.get('batch', 0):06d}-driver-readmission-*.json"))
            raw = json.loads(fresh[-1].read_bytes()) if fresh else {}
            busy = "SQLITE_BUSY" in (raw.get("stdout", "") + raw.get("stderr", "") + raw.get("error", ""))
            # Cap and unknown readings cannot authorize any further recovery.
            if state.get("ok") or not busy:
                historic = self.out / "stop-history" / f"{uuid.uuid4().hex}-quota-stop.json"
                j4.atomic_json(historic, stop)
                j4.atomic_json(path, {"reason": "quota_stop", "phase": "driver-readmission",
                                     "batch": stop.get("batch", 0), **state})
                self.event("retain_fresh_quota_stop", archive=str(historic), quota=state)
                return False
        return False

    async def run(self) -> None:
        self.admission()
        os.environ["PI_CONFIG_FILES"] = str(CONFIG)
        self.event("start", drivercodeSHA=j4.sha(Path(__file__).read_bytes()),
                   configSHA=j4.sha(CONFIG.read_bytes()), argv=vars(self.args), process_argv=sys.argv)
        async def observed(gateway: Any, path: str, body: dict[str, Any]) -> Any:
            return await self.observe(gateway, path, body)
        j4.Gateway.request = observed
        try:
            while True:
                if not await self.recover_quota() or not await self.recover_route():
                    return
                await j4.main_async(self.args)
                if not (self.out / "route-stop.json").exists() and not (self.out / "quota-stop.json").exists():
                    return
        finally:
            j4.Gateway.request = self.original_request


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--runs", required=True)
    result.add_argument("--out", required=True)
    result.add_argument("--freeze", "--freeze-path", dest="freeze")
    result.add_argument("--prereg-sha")
    result.add_argument("--baseline-j3", action="store_true")
    result.add_argument("--concurrency", type=int, default=12)
    result.add_argument("--batch-size", type=int, default=10)
    result.add_argument("--plan-only", action="store_true")
    result.add_argument("--max-retries", type=int, choices=range(4), default=3)
    result.add_argument("--backoff-seconds", type=float, default=30)
    result.add_argument("--quota-backoff-seconds", type=float, default=30)
    result.add_argument("--error-receipts", "--resume-source-errors", action="append", default=[])
    return result


async def main_async(args: argparse.Namespace) -> None:
    if args.backoff_seconds < 0 or args.quota_backoff_seconds < 0:
        raise ValueError("backoff must not be negative")
    await Driver(args).run()


if __name__ == "__main__":
    asyncio.run(main_async(parser().parse_args()))
