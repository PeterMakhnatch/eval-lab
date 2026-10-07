"""Run model-free controls through AgentEnv's local container/task APIs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import socket
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agentenv_bench import world
from agentenv_bench.generate import build_params
from agentenv_bench.s3k_1591 import controls, grader

SYSTEMS = (
    "anaplan_workforce_compensation", "bluesky_approval_workflow",
    "depaul_compensation_audit_register", "workday_hcm",
)
WORLD_TOOL = "bluesky_approval_workflow_bench_bump_row_version"
PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.11-slim@sha256:c20888b6acdd1e63e1c433a185bf3ad162c0288fe484616ce062e0d28add2900"


def utc():
    return datetime.now(UTC).isoformat()


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str) + "\n")


def binary(*args):
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def locked_runtime_requirements(lock_path):
    """Use lock pins for the small protocol runtime; no model SDK in agent image."""
    from packaging.markers import Marker, default_environment
    packages = {package["name"]: package for package in tomllib.loads(lock_path.read_text())["package"]}
    environment = {**default_environment(), "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "aarch64", "python_version": "3.11", "python_full_version": "3.11.15", "extra": ""}
    selected = set()
    def add(name):
        if name in selected:
            return
        selected.add(name)
        for dependency in packages[name].get("dependencies", []):
            if not dependency.get("marker") or Marker(dependency["marker"]).evaluate(environment):
                add(dependency["name"])
    # Protocol 0.1.290 imports regex in _triggers but omits that dependency.
    for name in ("agentenv-framework-protocol", "mcp", "uvicorn", "a2a-sdk", "starlette", "regex"):
        add(name)
    return "".join(f"{name}=={packages[name]['version']}\n" for name in sorted(selected))


def copy_control_package(target: Path):
    """Copy only controls plus their stdlib world typing dependency, never graders."""
    source = Path(controls.__file__)
    destination = target / "agentenv_bench" / "s3k_1591" / "controls.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    shutil.copy2(world.__file__, target / "agentenv_bench" / "world.py")
    cursor = destination.parent
    while cursor != target:
        (cursor / "__init__.py").touch()
        cursor = cursor.parent


def make_images(out, world_root, token):
    build = out / "build"
    build.mkdir()
    package = build / "agentenv_bench" / "aenv"
    package.mkdir(parents=True)
    (build / "agentenv_bench" / "__init__.py").touch()
    (package / "__init__.py").touch()
    for filename in ("agent.py", "server.py"):
        shutil.copy2(Path(__file__).with_name(filename), package / filename)
    copy_control_package(build)
    shutil.copytree(world_root / "work" / "tools", build / "tools")
    shutil.copytree(world_root / "work" / "workspace", build / "workspace")
    mcp_helper = world_root / "installed-agent" / "mcp_http.py"
    shutil.copy2(mcp_helper, build / "mcp_http.py")
    requirements = locked_runtime_requirements(Path(__file__).parents[2] / "uv.lock")
    (build / "requirements.txt").write_text(requirements)
    common = f"FROM {PYTHON_IMAGE}\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nENV PYTHONPATH=/app PYTHONUNBUFFERED=1\n"
    env_dockerfile = common + 'COPY agentenv_bench/aenv/server.py /app/agentenv_bench/aenv/server.py\nCOPY mcp_http.py /app/mcp_http.py\nCOPY tools /work/tools\nENV MCP_HOST=0.0.0.0 MCP_PORT=18765\nEXPOSE 18765\nCMD ["python", "-m", "agentenv_bench.aenv.server"]\n'
    agent_dockerfile = common + 'COPY agentenv_bench /app/agentenv_bench\nRUN rm /app/agentenv_bench/aenv/server.py\nCOPY workspace /work/workspace\nEXPOSE 8000\nENV A2A_HOST=0.0.0.0 A2A_PORT=8000\nCMD ["python", "-m", "agentenv_bench.aenv.agent"]\n'
    images = {}
    for kind, content in (("env", env_dockerfile), ("agent", agent_dockerfile)):
        dockerfile = build / f"Dockerfile.{kind}"
        dockerfile.write_text(content)
        tag = f"agentenv-bench-{kind}:{token}"
        process = subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", tag, "-f", str(dockerfile), str(build)], capture_output=True, text=True)
        (out / f"docker-build-{kind}.log").write_text(process.stdout + process.stderr)
        if process.returncode:
            raise RuntimeError(f"Docker {kind} build failed; see {out / f'docker-build-{kind}.log'}")
        images[kind] = {"tag": tag, "id": binary("docker", "image", "inspect", tag, "--format", "{{.Id}}")}
    binary("docker", "run", "--rm", "--network", "none", images["agent"]["tag"],
           "python", "-c", "from agentenv_bench.aenv.agent import ScriptedAgent; from agentenv_protocol.a2a_agent import create_app; create_app(ScriptedAgent())")
    return images


def configure(out):
    state = out / "local-state"
    state.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        registry_port = listener.getsockname()[1]
    config = out / "agentenv-config.toml"
    config.write_text(f'''[stores]\ndocument = "local"\nobject = "local"\nsecret = "local"\n[stores.image]\nimpl = "agent_env.store.image_store:LocalRegistryImageStore"\n[stores.image.config]\nregistry_host = "localhost:{registry_port}"\nrepository_prefix = "agentenv-bench"\n[sandbox]\ndefault = "local"\nagent_default = "local"\n[runner]\nimpl = "agent_env.runner.local_runner:LocalRunner"\n[runner.config]\nworkers = 1\n''')
    os.environ.update({
        "AGENT_ENV_CONFIG": str(config), "XDG_STATE_HOME": str(state),
        "AGENT_ENV_LOCAL_SANDBOX_DIR": str(out / "sandboxes"),
        "AGENT_SANDBOX_MODE": "container", "AGENT_ENV_SNAPSHOT_AFTER_LOAD": "false",
    })
    return {"config": str(config), "state": str(state), "sandbox_root": str(out / "sandboxes"), "registry_port": registry_port, "registry_container": f"agentenv-registry-{registry_port}"}


def base_params(task_dir, work):
    return build_params(work, "MAC-FY26-0274", task_dir, upstream=True)


def public_params(params):
    return {key: value for key, value in params.items() if key in {
        "case_id", "manager_id", "unrelated_workday_employee",
    }}


def dag(env, universe, agent, verifier_artifact, control, variant, params, token):
    env_id = env.id
    def step(id, type, previous=None, **kwargs):
        return {"id": f"{token}-{id}", "type": type, "depends_on": [f"{token}-{previous}"] if previous else [], **kwargs}
    steps = [
        step("deploy", "deploy_env", env_id=env_id, sandbox_type="local"),
        step("load", "load_artifact", "deploy", env_id=env_id, artifact_id=universe.id, artifact_version=universe.version, snapshot_after_load=False),
        step("rbac", "modify_env_tool_access", "load", env_id=env_id, action="disable", role="solver", tools=[WORLD_TOOL]),
    ]
    previous = "rbac"
    if variant == "race":
        steps.append(step("race", "register_env_triggers", previous, env_id=env_id, watch_roles=["solver"], triggers=[{
            "id": "one-concurrent-edit", "when": {"type": "action", "tool": "bluesky_approval_workflow_submit_merit_batch", "where": {"args.dry_run": {"equals": True}, "args.case_ids[0]": {"equals": params["case_id"]}, "result.accepted": {"equals": True}}},
            "actions": [{"type": "tool", "tool": WORLD_TOOL, "args": {"case_id": params["case_id"]}}],
            "barrier": {"at": "provoking_call"},
        }]))
        previous = "race"
    steps += [
        step("agent", "deploy_agent", previous, env_ids=[env_id], a2a_agent_id=agent.id, a2a_agent_version=agent.version, agent_name="solver", role="solver", sandbox_type="local", env_vars={"LITELLM_API_KEY": "MODEL_DISABLED", "LITELLM_BASE_URL": "http://127.0.0.1:1/v1"}),
        step("prompt", "prompt_agent", "agent", agent_name="solver", prompt=json.dumps({"control": control, "params": public_params(params)})),
        step("verify", "env_outcome_verifier", "prompt", env_id=env_id, file_artifact_id=verifier_artifact.id, file_artifact_version=verifier_artifact.version, verifier_id="outcome", score_aggregator="all_pass"),
        step("teardown", "teardown_sandboxes", "verify", agent_names=["solver"], env_ids=[env_id], fail_task_on_error=False),
    ]
    return {"id": f"mimo-{variant}-{control}-{token}", "steps": steps}


async def run_one(task_data, directory):
    from agent_env.task import Task
    from agent_env.task_step.context import TaskStepContext
    from agent_env.task_step.task_steps.teardown_sandboxes import TeardownSandboxesTaskStep
    task = Task.from_dict(task_data)
    task = Task.put(id=task.id, steps=task.steps)
    context = TaskStepContext()
    dump(directory / "task-dag.json", task.to_dict())
    def complete(index, total, step, current, duration):
        if step.type == "prompt_agent":
            response = current.prompt_responses[-1]
            (directory / "final-report.txt").write_text(response.response)
            if response.agent_trajectory_s3_uri:
                from agent_env.config import get_config
                payload = get_config().get_object_store().get(response.agent_trajectory_s3_uri)
                (directory / "agent-trajectory.json").write_bytes(payload)
            dump(directory / "prompt-response.json", response.__dict__)
    started = time.monotonic()
    try:
        await task.run(context=context, on_step_complete=complete)
    finally:
        if context.deployed_envs or context.deployed_agents or context.deployed_sandboxes:
            await TeardownSandboxesTaskStep(
                id="cleanup", version=None, fail_task_on_error=False,
                agent_names=[agent.agent_name for agent in context.deployed_agents],
                env_ids=[env.env_id for env in context.deployed_envs],
                sandbox_names=[sandbox.sandbox_name for sandbox in context.deployed_sandboxes],
            ).execute(context)
        dump(directory / "context.json", context.to_safe_dict())
    grades = json.loads((directory / "grade.json").read_text())
    rows = grades["rows"]
    rules = [row for row in rows if row["source"] != "report_proxy"]
    upstream_rules = [row for row in rows if row["source"] == "upstream_rule"]
    pass_rules = all(row["result"] for row in rules)
    agent_events = json.loads((directory / "agent-trajectory.json").read_text())
    evidence = next(event for event in agent_events if event.get("type") == "bench_evidence")
    triggers = json.loads((directory / "trigger-state.json").read_text())
    fire_count = sum(trigger["fire_count"] for trigger in triggers["triggers"])
    return {
        "task_id": task.id, "task_version": task.version, "task_instance_id": context.instance_id,
        "rows": rows, "exports": grades["exports"], "bench_verdict": "PASS" if pass_rules else "FAIL",
        "upstream_verdict": ("PASS" if all(row["result"] for row in upstream_rules) else "FAIL") if upstream_rules else "NOT_APPLICABLE",
        "pass_with_report": all(row["result"] for row in rows), "pass_rules": pass_rules,
        "evidence": evidence, "trigger_fire_count": fire_count,
        "agentenv_score": context.metadata["verifications"]["outcome"]["score"],
        "seconds": time.monotonic() - started, "artifact_dir": str(directory),
    }


async def execute(args, out, receipt):
    from agent_env.a2a_agent import A2AAgent
    from agent_env.artifact import (
        DockerImageArtifact,
        EnvironmentArtifact,
        EnvironmentUniverseArtifact,
        FileArtifact,
    )
    from agent_env.env import MCPServerEnv, MultiEnv
    from agent_env.env.bootstrap import put_gateway_env, put_service_db_env
    runtime = configure(out)
    receipt["runtime"] = runtime
    world_root = world.fetch_world(args.task_dir, out / "world-cache")
    pristine = world.materialize(world_root, out / "pristine")
    params = base_params(args.task_dir, pristine)
    receipt["params"] = params
    receipt["input_hashes"] = {
        str(path.relative_to(world_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(world_root.rglob("*")) if path.is_file()
    }
    receipt["task_hashes"] = {str(path.relative_to(args.task_dir)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.task_dir / "task.toml", args.task_dir / "instruction.md", args.task_dir / "tests/verifier/verifier_meta.json")}
    token = uuid4().hex[:10]
    receipt["images"] = await asyncio.to_thread(make_images, out, world_root, token)
    def say(message):
        logging.info("%s", message)
    gateway = await asyncio.to_thread(put_gateway_env, "default", platform="linux/arm64", say=say)
    database = await asyncio.to_thread(put_service_db_env, "default-db", platform="linux/arm64", say=say)
    infra = [gateway.docker_image_artifact, database.db_docker_image_artifact,
             database.db_web_docker_image_artifact, database.db_mcp_docker_image_artifact]
    receipt["infra_images"] = [
        {"artifact_id": artifact.id, "version": artifact.version,
         "registry_image": artifact.image_name,
         "id": binary("docker", "image", "inspect", artifact.image_name, "--format", "{{.Id}}")}
        for artifact in infra
    ]
    image_artifacts = {}
    for kind, image in receipt["images"].items():
        artifact = await asyncio.to_thread(DockerImageArtifact.put, id=f"bench-{kind}-{token}", description="Model-free MiMo bench", image_name=image["tag"])
        image_artifacts[kind] = artifact
        image["registry_image"] = artifact.image_name
    environments = []
    artifacts = []
    for system in SYSTEMS:
        environments.append(MCPServerEnv.put(id=f"{system}-{token}", docker_image_artifact=image_artifacts["env"], environment_name=system))
        file = FileArtifact.put(id=f"seed-{system}-{token}", description="Pristine MiMo SQLite state", file_path=str(pristine / "system" / system / "state.db"))
        artifacts.append(EnvironmentArtifact.put(id=f"seed-env-{system}-{token}", environment_name=system, file_artifact=file))
    env = MultiEnv.put(id=f"mimo-world-{token}", mcp_server_envs=environments, name="mimo")
    universe = EnvironmentUniverseArtifact.put(id=f"mimo-seed-{token}", environment_artifacts=artifacts)
    agent = A2AAgent.put(id=f"mimo-scripted-{token}", docker_image_artifact=image_artifacts["agent"], metadata={"description": "No model; deterministic controls"}, default_env_vars={"LITELLM_API_KEY": "MODEL_DISABLED", "LITELLM_BASE_URL": "http://127.0.0.1:1/v1"})
    variants = ("base", "race") if args.variant == "all" else (args.variant,)
    for variant in variants:
        names = list(controls.CONTROLS) if variant == "base" else ["oracle", "naive", "nop"]
        for name in names:
            directory = out / variant / name
            directory.mkdir(parents=True)
            run_params = dict(params)
            if variant == "race":
                run_params.update(upstream=False, expected_row_version=params["pre_row_version"] + 2)
            dump(directory / "params.json", run_params)
            verifier_file = directory / "verifier.py"
            config = {"run_dir": str(directory)}
            verifier_file.write_text("from agentenv_bench.aenv.verifier import verify as check\nasync def verify(mcp_url):\n    return await check(mcp_url, " + repr(config) + ")\n")
            verifier_artifact = FileArtifact.put(id=f"verifier-{variant}-{name}-{token}", description="Host-only deterministic SQLite verifier", file_path=str(verifier_file))
            task_data = dag(env, universe, agent, verifier_artifact, name, variant, run_params, f"{token}-{variant}-{name}")
            result = await run_one(task_data, directory)
            result.update(variant=variant, control=name, expected="pass" if name == "oracle" else "fail", params=run_params)
            receipt["controls"].append(result)
            dump(out / "receipt.json", receipt)
    receipt["acceptance"] = {
        "control_scores": all(result["agentenv_score"] == (1.0 if result["control"] == "oracle" else 0.0) for result in receipt["controls"]),
        "rbac": all(not result["evidence"]["rbac"]["listed"] and result["evidence"]["rbac"]["call_refused"] for result in receipt["controls"]),
        "race": all(result["trigger_fire_count"] == (0 if result["control"] == "nop" else 1) for result in receipt["controls"] if result["variant"] == "race"),
        "model_free": all(result["evidence"]["model_calls"] == 0 and not result["evidence"]["model_client_modules_loaded"] and not result["evidence"]["non_gateway_network_attempts"] for result in receipt["controls"]),
        "local_only": True,
    }
    if not all(receipt["acceptance"].values()):
        raise RuntimeError("Control score / RBAC / race / model-free acceptance failed; inspect receipt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", choices=("base", "race", "all"), default="all")
    args = parser.parse_args()
    args.task_dir = args.task_dir.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    receipt = {
        "started_utc": utc(), "controls": [], "task_dir": str(args.task_dir),
        "modules": {"world": world.__name__, "controls": controls.__name__, "grader": grader.__name__},
        "tool_versions": {"agentenv-framework": importlib.metadata.version("agentenv-framework"), "agentenv-framework-protocol": importlib.metadata.version("agentenv-framework-protocol"), "docker": binary("docker", "version", "--format", "{{.Server.Version}}"), "uv": binary("uv", "--version")},
        "lock_sha256": hashlib.sha256((Path(__file__).parents[2] / "uv.lock").read_bytes()).hexdigest(),
        "source_hashes": {
            str(path.relative_to(Path(__file__).parents[2])): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parents[1].rglob("*.py"))
        },
        "limits": "Task/grader validity only, never model-capability evidence. Report proxy is not upstream LLM grading.",
    }
    logging.basicConfig(filename=out / "runner.log", level=logging.INFO)
    success = False
    try:
        asyncio.run(execute(args, out, receipt))
        success = True
    except BaseException as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        logging.exception("Bench run failed")
        raise
    finally:
        runtime = receipt.get("runtime", {})
        registry = runtime.get("registry_container")
        if registry:
            cleanup = subprocess.run(["docker", "rm", "-f", "-v", registry], capture_output=True, text=True)
            receipt["registry_teardown"] = {"container": registry, "returncode": cleanup.returncode, "stdout": cleanup.stdout.strip(), "stderr": cleanup.stderr.strip()}
        receipt.update(finished_utc=utc(), elapsed_seconds=time.monotonic() - started, completed=success)
        dump(out / "receipt.json", receipt)
    print(json.dumps({"receipt": str(out / "receipt.json"), "controls": len(receipt["controls"]), "acceptance": receipt["acceptance"]}))


if __name__ == "__main__":
    main()
