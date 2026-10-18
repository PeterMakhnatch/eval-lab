"""One-use BASE serving reservation; no command grants spend or refunds a reservation.

``quote`` is provider-free. ``deploy``, ``health`` and ``binding`` are paid parent
operations. ``watch``/``stop`` use control-plane metadata only. A detached local
watchdog starts before deployment; a scheduled, same-app remote guard and an
absolute in-container process deadline do not depend on that controller.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

APP_NAME = "evallab-mimo-v26-9b"
MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
CONTROLS_SHA256 = "ca3d4460296cdfa017741d3557f3455bccf74b8f26f24643ff4a2c1f50501e80"
WINDOW_ENV = "EVALLAB_MIMO_SERVING_WINDOW"
STATE_DICT = "evallab-mimo-v26-9b-window-state"
GPU_RATES = {"A100-80GB": Decimal("0.000694"), "L40S": Decimal("0.000542")}
CPU_RATE = Decimal("0.0000131")
MEMORY_RATE = Decimal("0.00000222")
GUARD_CPU = Decimal("0.125")
GUARD_MEMORY_MIB = 256
GUARD_RATE = GUARD_CPU * CPU_RATE + Decimal(GUARD_MEMORY_MIB) / 1024 * MEMORY_RATE
MAX_SECONDS = 7200


class WindowError(ValueError):
    """Secret-free admission/lifecycle error safe to show to the operator."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise WindowError(message)


def money(value: Any) -> Decimal:
    require(isinstance(value, str), "money and rates must be decimal strings")
    result = Decimal(value)
    require(result.is_finite() and result >= 0, "money and rates must be finite and nonnegative")
    return result


def instant(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
            "timestamps must have an explicit UTC offset")
    return parsed.timestamp()


def utc(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, UTC).isoformat()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resource_rate(gpu: str, cpu_limit: Any, memory_mib_limit: int) -> Decimal:
    require(gpu in GPU_RATES, "only the existing exact-model A100-80GB/L40S configurations are supported")
    cpu = Decimal(str(cpu_limit))
    require(cpu.is_finite() and 4 <= cpu <= 8, "hard_cpu_limit must be in 4..8 physical cores")
    require(type(memory_mib_limit) is int and 16384 <= memory_mib_limit <= 65536,
            "hard_memory_mib_limit must be an integer in 16384..65536")
    return GPU_RATES[gpu] + cpu * CPU_RATE + Decimal(memory_mib_limit) / 1024 * MEMORY_RATE + GUARD_RATE


def validate_config(config: dict[str, Any]) -> None:
    require(config["app_name"] == APP_NAME, "a window must reuse the single canonical BASE app")
    rate = resource_rate(config["gpu"], config["hard_cpu_limit"], config["hard_memory_mib_limit"])
    require(money(config["usd_per_second_upper_bound"]) >= rate, "serving rate understates enforced resource bounds")
    require(type(config["maximum_seconds"]) is int and 1 <= config["maximum_seconds"] <= MAX_SECONDS,
            "maximum_seconds must be an integer in 1..7200")
    reserve = config["shutdown_reserve_seconds"]
    require(type(reserve) is int and 120 <= reserve < config["maximum_seconds"],
            "shutdown reserve must be at least 120s and less than maximum_seconds")
    start, expiry, end = (instant(config[key]) for key in ("starts_at", "expires_at", "stop_deadline"))
    require(0 < end - start <= config["maximum_seconds"], "absolute deadline exceeds admitted wall time")
    require(abs(end - expiry - reserve) < 0.001, "shutdown reserve is not inside the absolute wall allowance")
    require(money(config["usd_per_second_upper_bound"]) * config["maximum_seconds"] <= money(config["cap_usd"]),
            "full wall allowance does not fit the serving reservation")
    require(bool(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", config["reservation_id"])), "invalid reservation_id")
    require(bool(config["owner"]), "owner is required")


def admitted_config(path: Path, digest: str, owner: str, deadline: str, *, now: float) -> dict[str, Any]:
    raw = path.read_bytes()
    require(sha256(raw) == digest, "parent budget-state SHA256 mismatch")
    budget = json.loads(raw)
    require(budget["schema"] == "mimo.phase2-envelope/v1" and budget["admitted"] is True,
            "parent envelope is not explicitly admitted")
    require(budget["ledger_tag"] == "2026-10-17-v7", "wrong parent ledger")
    require(money(budget["approved_usd"]) > 0, "parent total authorization must be positive")
    require(budget["controls"]["sha256"] == "sha256:" + CONTROLS_SHA256, "parent envelope is not bound to the repaired19 controls")
    require(sha256(Path(budget["controls"]["path"]).read_bytes()) == CONTROLS_SHA256,
            "parent controls bytes changed")
    serving = budget["serving"]
    require("native" in budget and "heldout_regrade" in budget, "parent envelope lacks complementary reservations")
    caps = [money(section["cap_usd"]) for section in budget.values()
            if isinstance(section, dict) and "cap_usd" in section]
    require(sum(caps, Decimal("0")) <= money(budget["approved_usd"]),
            "all declared reservations exceed the parent combined total")
    end = instant(deadline)
    require(end > now, "parent deadline has already expired")
    config = {key: serving[key] for key in (
        "app_name", "gpu", "maximum_seconds", "usd_per_second_upper_bound", "shutdown_reserve_seconds",
        "hard_cpu_limit", "hard_memory_mib_limit", "cap_usd",
    )}
    config.update(schema="mimo.serving-window-config/v1", reservation_id=budget["reservation_id"],
                  window_id=budget["reservation_id"], owner=owner,
                  budget_state={"path": str(path.resolve()), "sha256": "sha256:" + digest},
                  starts_at=utc(now), expires_at=utc(end - serving["shutdown_reserve_seconds"]),
                  stop_deadline=utc(end))
    validate_config(config)
    require(instant(config["expires_at"]) > now, "deadline leaves no serving time before shutdown reserve")
    return config


def config_from_env() -> dict[str, Any] | None:
    raw = os.environ.get(WINDOW_ENV)
    if raw is None:
        return None
    config = json.loads(raw)
    validate_config(config)
    return config


def ownership_tags(config: dict[str, Any]) -> dict[str, str]:
    return {"evallab_window_id": config["window_id"],
            "evallab_window_config_sha256": sha256(encoded(config)),
            "evallab_budget_sha256": config["budget_state"]["sha256"]}


class Journal:
    """Owned atomic receipt with an immutable config and monotone exposure high-water mark."""

    def __init__(self, path: Path):
        self.path = path.resolve()

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def create(self, config: dict[str, Any]) -> None:
        with self.locked():
            value = {"schema": "mimo.serving-window-journal/v1", "config": config,
                     "config_sha256": sha256(encoded(config)), "state": "reserved_not_deployed",
                     "reserved_usd": config["cap_usd"], "wall_exposure_upper_bound_usd": "0",
                     "app_id": None, "upstream": None, "stop_guard": {"armed": False}}
            with self.path.open("xb") as target:
                target.write(encoded(value))
                target.flush()
                os.fsync(target.fileno())

    def read(self) -> dict[str, Any]:
        value = json.loads(self.path.read_bytes())
        require(value["config_sha256"] == sha256(encoded(value["config"])), "immutable window config changed")
        require(value["reserved_usd"] == value["config"]["cap_usd"], "window reservation cannot be reset or refunded")
        return value

    def update(self, **changes: Any) -> dict[str, Any]:
        require(not ({"config", "config_sha256", "reserved_usd"} & changes.keys()), "immutable journal fields cannot change")
        with self.locked():
            value = self.read()
            value.update(changes)
            config = value["config"]
            end = instant(value["stopped_at"]) if value.get("stopped_at") else time.time()
            elapsed = max(0.0, end - instant(config["starts_at"]))
            exposure = Decimal(str(elapsed)) * money(config["usd_per_second_upper_bound"])
            value["wall_exposure_upper_bound_usd"] = str(max(money(value["wall_exposure_upper_bound_usd"]), exposure))
            value["cap_exceeded"] = money(value["wall_exposure_upper_bound_usd"]) > money(config["cap_usd"])
            value["updated_at"] = utc()
            temporary = self.path.with_suffix(self.path.suffix + ".new")
            with temporary.open("wb") as target:
                target.write(encoded(value))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return value

    def artifact(self, name: str, value: Any) -> dict[str, str]:
        directory = self.path.parent / (self.path.name + ".artifacts")
        directory.mkdir(exist_ok=True)
        path = directory / f"{name}-{time.time_ns()}.json"
        raw = encoded(value)
        with path.open("xb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        return {"path": str(path), "sha256": "sha256:" + sha256(raw)}


async def _modal_rpc(method: str, request: Any, timeout: float) -> Any:
    from modal.client import _Client

    client = await _Client.from_env()
    # SDK 1.5.5 requires retry=None with a finite timeout; never implicitly retry.
    return await getattr(client.stub, method)(request, timeout=timeout, retry=None)


class ModalControl:
    """Pinned Modal 1.5.5 control-plane RPCs, never a GPU health request."""

    def __init__(self, environment: str):
        import modal
        from modal._utils.async_utils import synchronizer
        from modal_proto import api_pb2

        self.api = api_pb2
        self.rpc = synchronizer.create_blocking(_modal_rpc)
        self.environment = environment
        self.store = modal.Dict.from_name(STATE_DICT, environment_name=environment, create_if_missing=True)

    def apps(self):
        return self.rpc("AppList", self.api.AppListRequest(environment_name=self.environment), 10).apps

    def tags(self, app_id: str) -> dict[str, str]:
        response = self.rpc("AppGetTags", self.api.AppGetTagsRequest(app_id=app_id), 5)
        return dict(response.tags)

    def verify_owner(self, config: dict[str, Any], app_id: str) -> None:
        tags = self.tags(app_id)
        require(all(tags.get(key) == value for key, value in ownership_tags(config).items()),
                "refusing to operate on an app without this exact window/config ownership")

    def owned_app(self, config: dict[str, Any], app_id: str | None) -> str | None:
        if app_id:
            self.verify_owner(config, app_id)
            return app_id
        candidates = [row for row in self.apps() if (row.name or row.description) == APP_NAME]
        expected = ownership_tags(config)
        owned = []
        for row in candidates:
            tags = self.tags(row.app_id)
            if all(tags.get(key) == value for key, value in expected.items()):
                owned.append(row.app_id)
        require(len(owned) <= 1, "multiple apps claim the same one-use window")
        return owned[0] if owned else None

    def observation(self, config: dict[str, Any], app_id: str) -> dict[str, Any]:
        self.verify_owner(config, app_id)
        lifecycle = self.rpc("AppGetLifecycle", self.api.AppGetLifecycleRequest(app_id=app_id), 5).lifecycle
        tasks = self.rpc("TaskList", self.api.TaskListRequest(app_id=app_id), 5).tasks
        return {"checked_at": utc(), "app_id": app_id, "app_state": int(lifecycle.app_state),
                "stopped": lifecycle.app_state == self.api.APP_STATE_STOPPED, "active_containers": len(tasks),
                "tasks": [{"task_id": task.task_id, "started_at": task.started_at,
                           "enqueued_at": task.enqueued_at} for task in tasks]}

    def stop(self, config: dict[str, Any], app_id: str) -> None:
        self.verify_owner(config, app_id)
        self.rpc("AppStop", self.api.AppStopRequest(app_id=app_id,
            source=self.api.APP_STOP_SOURCE_PYTHON_CLIENT), 5)


def remote_guard(config: dict[str, Any], app_id: str) -> None:
    """Same-app scheduled guard; baked deadline cannot be renewed by a caller."""
    control = ModalControl(config["environment"])
    control.verify_owner(config, app_id)
    require(control.store.get("reservation:" + config["reservation_id"]) == sha256(encoded(config)),
            "remote one-use reservation does not match the deployed guard")
    record = {"armed": True, "app_id": app_id, "deadline": config["expires_at"],
              "config_sha256": sha256(encoded(config)), "armed_at": utc(),
              "mechanism": "same-app scheduled guard plus detached local watchdog and container absolute deadline"}
    control.store.put("guard:" + config["reservation_id"], record)
    wait = max(0, instant(config["expires_at"]) - time.time())
    monotonic_deadline = time.monotonic() + wait
    while time.time() < instant(config["expires_at"]) and time.monotonic() < monotonic_deadline:
        time.sleep(min(1, max(0, monotonic_deadline - time.monotonic())))
    while True:
        try:
            control.stop(config, app_id)
            return  # Stopping this app normally terminates this guard too.
        except Exception as exc:
            record.update(last_stop_error=type(exc).__name__, last_stop_attempt_at=utc())
            control.store.put("guard:" + config["reservation_id"], record)
            if time.time() >= instant(config["stop_deadline"]):
                raise
            time.sleep(1)


def arm_container_deadline(config: dict[str, Any], process: subprocess.Popen[bytes]) -> None:
    """Kill the complete server process group even if all controllers disappear."""
    wait = max(0, instant(config["expires_at"]) - time.time())

    def expire() -> None:
        threading.Event().wait(wait)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        os._exit(78)

    threading.Thread(target=expire, daemon=True, name="owned-window-deadline").start()


def watch(journal: Journal, *, once: bool = False, stop_now: bool = False) -> dict[str, Any]:
    value = journal.read()
    control = ModalControl(value["config"]["environment"])
    while True:
        try:
            value = journal.update(watchdog_pid=os.getpid(), watchdog_heartbeat_at=utc())
            config = value["config"]
            if value.get("stopped_at"):
                return value
            app_id = control.owned_app(config, value["app_id"])
            if app_id:
                changes: dict[str, Any] = {"app_id": app_id}
                observation = control.observation(config, app_id)
                if not observation["stopped"] and (stop_now or time.time() >= instant(config["expires_at"])):
                    journal.update(state="stopping", stop_requested_at=value.get("stop_requested_at") or utc())
                    control.stop(config, app_id)
                    observation = control.observation(config, app_id)
                changes["observation"] = observation
                if observation["stopped"] and observation["active_containers"] == 0:
                    changes.update(state="stopped", stopped_at=utc(), stop=journal.artifact("stop", observation))
                value = journal.update(**changes)
                if value.get("stopped_at"):
                    return value
            if once:
                return value
        except Exception as exc:
            value = journal.update(last_control_error=type(exc).__name__, last_control_error_at=utc())
            if once:
                raise
        time.sleep(1 if stop_now or time.time() >= instant(value["config"]["expires_at"]) else 5)


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    config = admitted_config(args.budget_state, args.budget_state_sha256, args.owner, args.deadline, now=time.time())
    config["environment"] = args.environment
    journal = Journal(args.state)
    journal.create(config)  # Exclusive creation; never a resume/redeploy/reset operation.
    control = ModalControl(args.environment)
    for row in control.apps():
        if (row.name or row.description) in {APP_NAME, APP_NAME + "-lora"}:
            require(row.state == control.api.APP_STATE_STOPPED and row.n_running_tasks == 0,
                    "another BASE/LoRA app is live; do not replace it or open a parallel GPU window")
    require(control.store.put("reservation:" + config["reservation_id"], sha256(encoded(config)), skip_if_exists=True),
            "parent reservation has already been consumed; a new local journal cannot reset it")
    log_path = journal.path.with_suffix(journal.path.suffix + ".watchdog.log")
    with log_path.open("ab") as logs:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch", "--state", str(journal.path)],
            stdin=subprocess.DEVNULL, stdout=logs, stderr=logs, start_new_session=True)
    journal.update(watchdog_spawned_pid=process.pid, watchdog_log=str(log_path))
    arm_until = time.monotonic() + 20
    while not journal.read().get("watchdog_heartbeat_at"):
        require(process.poll() is None and time.monotonic() < arm_until, "independent local watchdog failed to start")
        time.sleep(0.2)
    require(time.time() < instant(config["expires_at"]), "window expired during admission")
    os.environ[WINDOW_ENV] = json.dumps(config)
    os.environ["EVALLAB_MIMO_GPU"] = config["gpu"]
    os.environ["EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS"] = "60"
    serve = importlib.import_module("serve")
    require(config == serve.WINDOW, "serve.py did not retain this exact window config")
    # No weights download, readiness request, autoscaler minimum, or second app.
    serve.app.deploy(environment_name=args.environment, strategy="recreate")
    app_id = serve.app.app_id
    require(isinstance(app_id, str) and app_id.startswith("ap-"), "deployment did not return an actual app ID")
    control.verify_owner(config, app_id)
    upstream = serve.MimoServer.get_url()
    require(isinstance(upstream, str), "deployment did not return a server URL")
    validate_upstream(upstream)
    deployment = {"deployed_at": utc(), "app_id": app_id, "app_name": APP_NAME, "upstream": upstream,
                  "model_id": serve.MODEL_ID, "model_revision": serve.MODEL_REVISION,
                  "image": serve.SGLANG_IMAGE, "config_sha256": sha256(encoded(config)),
                  "source": [{"path": str(Path(__file__).with_name(name)),
                              "sha256": sha256(Path(__file__).with_name(name).read_bytes())}
                             for name in ("serve.py", "serving_window.py")]}
    journal.update(app_id=app_id, upstream=upstream, state="deployed_not_ready",
                   model_revision=serve.MODEL_REVISION, image=serve.SGLANG_IMAGE,
                   deployment=journal.artifact("deployment", deployment))
    require(time.time() < instant(config["expires_at"]), "window expired during deployment; watchdog must complete stop")
    serve.stop_window.spawn()  # Schedule is also baked into the deployment, covering a controller crash here.
    arm_until = min(instant(config["expires_at"]), time.time() + 90)
    while time.time() < arm_until:
        guard = control.store.get("guard:" + config["reservation_id"])
        if guard and guard.get("armed") is True and guard.get("app_id") == app_id:
            require(guard["config_sha256"] == sha256(encoded(config)) and guard["deadline"] == config["expires_at"],
                    "armed guard does not bind this exact app/deadline")
            guard["evidence"] = journal.artifact("guard", guard)
            return journal.update(stop_guard=guard, state="armed_not_ready")
        time.sleep(1)
    journal.update(state="guard_not_armed")
    control.stop(config, app_id)
    raise RuntimeError("remote guard did not arm; stop requested and no readiness/model request admitted")


def validate_upstream(upstream: str) -> None:
    parsed = urllib.parse.urlsplit(upstream)
    require(parsed.scheme == "https" and parsed.hostname is not None and parsed.hostname.endswith(".modal.run")
            and parsed.username is None and parsed.password is None and parsed.port is None
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "server URL must be an actual HTTPS Modal origin without credentials or /v1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(journal: Journal, *, binding: bool, timeout: float) -> dict[str, Any]:
    require(timeout > 0, "probe timeout must be positive")
    value = journal.read()
    config, guard = value["config"], value["stop_guard"]
    require(not value.get("stopped_at") and guard.get("armed") is True and guard.get("app_id") == value["app_id"],
            "no paid readiness request before this exact remote guard is armed")
    require(guard["deadline"] == config["expires_at"], "guard deadline changed")
    control = ModalControl(config["environment"])
    control.verify_owner(config, value["app_id"])
    require(time.time() + timeout < instant(config["expires_at"]), "probe cannot fit before shutdown reserve")
    key = os.environ.get("MIMO_SELFHOSTED_API_KEY")
    require(bool(key), "MIMO_SELFHOSTED_API_KEY is absent; use keys has, then keys run")
    upstream = value["upstream"].rstrip("/")
    validate_upstream(upstream)
    opener = urllib.request.build_opener(_NoRedirect())
    deadline = time.monotonic() + timeout

    def request(path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        require(time.time() < instant(config["expires_at"]), "serving window expired")
        remaining = min(deadline - time.monotonic(), instant(config["expires_at"]) - time.time())
        require(remaining > 0, "probe timeout exhausted")
        req = urllib.request.Request(upstream + path, data=encoded(body) if body is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with opener.open(req, timeout=remaining) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw and path != "/health" else None

    if not binding:
        started = time.monotonic()
        statuses: dict[str, int] = {}
        while True:
            try:
                status, _ = request("/health")
                require(status == 200, "health returned a non-200 response")
                return journal.update(health={"authenticated": True, "method": "GET", "path": "/health",
                    "status_code": status, "checked_at": utc(), "upstream": upstream,
                    "ready_after_s": time.monotonic() - started, "prior_statuses": statuses})
            except urllib.error.HTTPError as exc:
                statuses[str(exc.code)] = statuses.get(str(exc.code), 0) + 1
                if exc.code not in {502, 503, 504}:
                    raise ValueError(f"health failed with HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                statuses[type(exc).__name__] = statuses.get(type(exc).__name__, 0) + 1
            require(time.monotonic() + 2 < deadline, "health did not become ready within admitted probe timeout")
            time.sleep(2)
    status, models = request("/v1/models")
    require(status == 200 and [row.get("id") for row in models.get("data", [])] == [MODEL_ID],
            "authenticated models response is not exclusively the exact BASE model")
    readiness = {"authenticated": True, "method": "GET", "path": "/v1/models", "status_code": status,
                 "checked_at": utc(), "upstream": upstream, "response": journal.artifact("models", models)}
    body = {"model": MODEL_ID, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 1,
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "chat_template_kwargs": {"enable_thinking": True}}
    status, response = request("/v1/chat/completions", body)
    response_ref = journal.artifact("binding", response)
    require(status == 200 and response.get("model") == MODEL_ID and bool(response.get("choices")),
            "authenticated completion did not return the exact requested BASE binding")
    require(time.time() < instant(config["expires_at"]), "binding response arrived after serving expiry")
    binding_receipt = {"authenticated": True, "method": "POST", "path": "/v1/chat/completions",
        "status_code": status, "checked_at": utc(), "upstream": upstream, "requested_model": MODEL_ID,
        "returned_model": response["model"], "max_tokens": 1, "response": response_ref}
    return journal.update(state="active", readiness=readiness, authenticated_binding=binding_receipt)


def receipt(value: dict[str, Any]) -> dict[str, Any]:
    config = value["config"]
    state = value["state"]
    if state == "active" and time.time() >= instant(config["expires_at"]):
        state = "expired"
    return {"schema": "envcheck.modal-serving-window/v1", "window_id": config["window_id"],
            "owner": config["owner"], "state": state, "app_name": config["app_name"],
            "app_id": value["app_id"], "starts_at": config["starts_at"], "expires_at": config["expires_at"],
            "stop_deadline": config["stop_deadline"], "upstream": value["upstream"], "allowed_models": [MODEL_ID],
            "budget": {"envelope": config["budget_state"]}, "reservation_id": config["reservation_id"],
            "config_sha256": value["config_sha256"], "reserved_usd": value["reserved_usd"],
            "wall_exposure_upper_bound_usd": value["wall_exposure_upper_bound_usd"],
            "model_id": MODEL_ID, "model_revision": value.get("model_revision"), "image": value.get("image"),
            "usd_per_second_upper_bound": config["usd_per_second_upper_bound"],
            "resources": {"gpu": config["gpu"], "gpu_count": 1, "cpu_request": 4,
                          "hard_cpu_limit": config["hard_cpu_limit"], "memory_request_mib": 16384,
                          "hard_memory_mib_limit": config["hard_memory_mib_limit"],
                          "max_gpu_containers": 1, "max_guard_containers": 1},
            **{key: value.get(key) for key in ("deployment", "stop_guard", "health", "readiness", "authenticated_binding",
                                               "observation", "stop", "stopped_at", "cap_exceeded")}}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    quote = commands.add_parser("quote", help="provider-free bound; not an admission")
    quote.add_argument("--gpu", choices=tuple(GPU_RATES), required=True)
    quote.add_argument("--cpu-limit", type=float, required=True)
    quote.add_argument("--memory-mib-limit", type=int, required=True)
    quote.add_argument("--cap-usd", required=True)
    quote.add_argument("--shutdown-reserve-seconds", type=int, required=True)
    start = commands.add_parser("deploy", help="paid parent action; consumes one admitted reservation")
    start.add_argument("--budget-state", type=Path, required=True)
    start.add_argument("--budget-state-sha256", required=True)
    start.add_argument("--owner", required=True)
    start.add_argument("--deadline", required=True, help="absolute UTC completed-stop deadline, not stop-request time")
    start.add_argument("--environment", required=True)
    start.add_argument("--state", type=Path, required=True)
    for name in ("watch", "health", "binding", "stop", "receipt"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, required=True)
        if name == "watch":
            command.add_argument("--once", action="store_true", help="one free metadata observation; does not wake GPU")
        if name in {"health", "binding"}:
            command.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "quote":
            rate = resource_rate(args.gpu, args.cpu_limit, args.memory_mib_limit)
            wall = int((money(args.cap_usd) / rate).to_integral_value(rounding=ROUND_FLOOR))
            require(args.shutdown_reserve_seconds >= 120 and wall > args.shutdown_reserve_seconds,
                    "quote needs at least 120s shutdown reserve and positive serving time")
            result = {"usd_per_second_upper_bound": str(rate), "usd_per_hour_upper_bound": str(rate * 3600),
                      "maximum_wall_seconds": min(wall, MAX_SECONDS),
                      "before_shutdown_seconds": min(wall, MAX_SECONDS) - args.shutdown_reserve_seconds,
                      "guard_rate_usd_per_second": str(GUARD_RATE),
                      "excludes": ["volume storage", "network egress", "any separately invoked download/build compute"],
                      "qualified": False, "admitted": False}
        elif args.command == "deploy":
            result = receipt(deploy(args))
        else:
            journal = Journal(args.state)
            if args.command in {"watch", "stop"}:
                value = watch(journal, once=getattr(args, "once", False), stop_now=args.command == "stop")
            elif args.command in {"health", "binding"}:
                value = probe(journal, binding=args.command == "binding", timeout=args.timeout)
            else:
                value = journal.read()
            result = receipt(value)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        # Never print HTTP bodies/headers or SDK exception strings.
        detail = str(exc) if isinstance(exc, WindowError) else type(exc).__name__
        print(f"serving-window failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
