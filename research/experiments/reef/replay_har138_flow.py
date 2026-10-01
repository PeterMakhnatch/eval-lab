#!/usr/bin/env python3
"""HAR-138 flow replay: Lab evidence -> Reef record/report -> served harness.

CPU-only replay, not a live experiment. One saved Harbor trial enters a local
Reef service as a labelled trajectory PROJECTION (counts, hashes, saved reward;
never the original provider request, logprobs, or prompts); a deterministic
fixture then exercises proposal/selection/publication and pinned pull over real
local HTTP via the actual reef-client SDK. Requires the release-ID SDK repair;
the published 0.2.0/0.2.1 source does not implement that contract.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

from aiohttp import web
from reef.artifact import InMemoryRepositoryBackend
from reef.dispatcher import Dispatcher
from reef.inference.http import InferenceProxyRuntime
from reef.recipe.cordis import CordisRecipe
from reef.service.app import create_app
from reef.storage.sqlite import SQLiteScenarioStorage
from reef.train.cordis_backend.strategies import resolve_episode_scorer, resolve_proposer
from reef_client.client import ReefClient, ReefClientError

SDK_SOURCE = "reef/.worktrees/lab-flow-release-contract/third_party/reef-client"
HARNESS_RELEASE_SIDECAR = ".reef-harness-release"

#: Hermetic fixture from Reef's own harness tests: episodes run this binary through the real pi-adapter path.
PI_FAKE = """\
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

prompt = sys.argv[sys.argv.index("-p") + 1]
agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
session_dir = Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
session_dir.mkdir(parents=True, exist_ok=True)
rules_path = agent_dir / "AGENTS.md"
event = {"type": "agent_end", "rules": rules_path.read_text() if rules_path.exists() else ""}
(session_dir / "session.jsonl").write_text(json.dumps(event) + "\\n")
"""
CREATE_RULES = {
    "op": "create",
    "id": "r1",
    "options": {"name": "rules", "config": {"text": "marker rules"}},
}
GROW_RULES = {"op": "update", "id": "r1", "options": {"config": {"text": "marker marker rules"}}}


def require_fixed_sdk() -> str:
    import reef_client

    problems = [] if hasattr(ReefClient, "harness_releases") else ["missing harness_releases"]
    for method in ("harness_pull", "inference", "inference_with_record", "report", "post"):
        params = inspect.signature(getattr(ReefClient, method)).parameters
        if "release_id" not in params or "artifact_version" in params or "version" in params:
            problems.append(f"{method} still speaks artifact_version")
    if getattr(reef_client.client, "HARNESS_RELEASE_SIDECAR", None) != HARNESS_RELEASE_SIDECAR:
        problems.append("missing release-ID sidecar support")
    if problems:
        sys.exit(
            "refusing incompatible reef-client ("
            + "; ".join(problems)
            + "); install the reviewed patched SDK editable "
            f"from {SDK_SOURCE}; no raw-HTTP fallback"
        )
    return reef_client.__file__


def load_trial(root: Path) -> tuple[Path, Path, dict, dict | None]:
    result_path = root / "result.json"
    candidate = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    if isinstance(candidate.get("trial_name"), str):
        trial_dir, job, result = root, root.parent, candidate
    else:
        found = sorted(p for p in root.iterdir() if p.is_dir() and (p / "result.json").is_file())
        if len(found) != 1:
            sys.exit(
                f"--trial holds {len(found)} nested trials with result.json; refusing to guess"
            )
        trial_dir, job = found[0], root
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if (
        isinstance(reward, bool)
        or not isinstance(reward, (int, float))
        or not math.isfinite(reward)
    ):
        sys.exit("trial carries no valid saved reward; refusing (never coerced to 0)")
    processed_path = job / "processed" / f"trial-{result.get('trial_name')}.json"
    processed = (
        json.loads(processed_path.read_text(encoding="utf-8")) if processed_path.is_file() else None
    )
    if processed is not None and (
        not processed.get("scored") or processed.get("reward") != float(reward)
    ):
        sys.exit("processed projection disagrees with result.json reward; refusing")
    return trial_dir, job, result, processed


async def expect_status(awaitable, statuses: tuple[int, ...], name: str) -> int:
    try:
        answer = await awaitable
    except ReefClientError as exc:
        assert exc.status in statuses, f"{name}: status {exc.status}, want {statuses}"
        return exc.status
    raise AssertionError(f"{name}: expected refusal, got {answer!r}")


async def wait_release(client: ReefClient, scenario: str, previous: str | None) -> tuple[str, dict]:
    headers = {"x-reef-scenario": scenario}
    deadline = asyncio.get_running_loop().time() + 30.0
    while True:
        try:
            manifest = await asyncio.to_thread(client.get, "/reef/harness", extra_headers=headers)
        except ReefClientError as exc:
            if exc.status != 404:
                raise
            manifest = {}
        current = manifest.get("release_id")
        if isinstance(current, str) and current and current != previous:
            return current, manifest
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("fixture step did not publish")
        await asyncio.sleep(0.2)


async def run(trial_arg: str, out: Path, scenario: str, sdk_path: str) -> dict:
    checks: list[dict] = []
    trial_dir, job, result, processed = load_trial(Path(trial_arg))
    trial_name, reward = result["trial_name"], float(result["verifier_result"]["rewards"]["reward"])
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for name in ("result.json", "config.json", "trial.log")
        if (p := trial_dir / name).is_file()
    }
    if processed is not None:
        hashes[f"trial-{trial_name}.json"] = hashlib.sha256(
            (job / "processed" / f"trial-{trial_name}.json").read_bytes()
        ).hexdigest()
    for path in sorted((trial_dir / "agent").glob("trajectory*.json")):
        hashes[str(path.relative_to(trial_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    agent = result.get("agent_result") or {}
    projection = {
        "origin": "lab_replay_trajectory_projection",
        "trial_name": trial_name,
        "task": result.get("task_name"),
        "model_name": (processed or {}).get("model_name"),
        "saved_reward": reward,
        "reward_source": "verifier_result",
        "agent_steps": (processed or {}).get("agent_steps"),
        "stitched_steps": (processed or {}).get("stitched_steps"),
        "stop_reason": (processed or {}).get("stop_reason"),
        "input_tokens": agent.get("n_input_tokens"),
        "output_tokens": agent.get("n_output_tokens"),
        "source_hashes": hashes,
        "note": "counts/hashes only; no request history, logprobs, or prompts",
    }
    scratch = Path(tempfile.mkdtemp(prefix="har138-replay-"))
    dispatcher, runner = None, None
    try:
        fake = scratch / "fake-pi"
        fake.write_text(PI_FAKE, encoding="utf-8")
        fake.chmod(0o755)
        recipe = CordisRecipe(
            resolve_proposer(lambda nodes, samples, model: None),
            resolve_episode_scorer(
                lambda task, result: float(result.trajectory[-1]["rules"].count("marker"))
            ),
            ("har138 fixture task",),
            binary=str(fake),
            adapter="pi",
            seed=(),
            runtime=InferenceProxyRuntime(
                model_path="har138-replay-fixture", base_url="http://127.0.0.1:9"
            ),
            proposals_dir=str(scratch / "inbox"),
            batch_policy="reports",
            batch_size=1,
        )
        initial = scratch / "initial"
        initial.mkdir()
        dispatcher = Dispatcher(
            recipe,
            InMemoryRepositoryBackend.factory(initial, root=scratch / "repository"),
            local_artifact_dir=scratch / "staged",
            agent_record_dir=scratch / "records",
            scenario_storage=SQLiteScenarioStorage(scratch / "records"),
        )
        bound = dispatcher.get_or_create_scenario(scenario)
        base_release = bound.repository.base_artifact.release_id
        runner = web.AppRunner(create_app(dispatcher))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        client = ReefClient(f"http://127.0.0.1:{runner.addresses[0][1]}")
        call = asyncio.to_thread
        import_id = f"lab-replay:{trial_name}:projection:1"
        record = {"agent_record_id": import_id, "request_type": "inference", "payload": projection}
        assert (await call(client.post, "/reef/records", scenario, record))[0][
            "agent_record_id"
        ] == import_id
        stored_import = json.dumps(dispatcher.read_record(scenario, import_id), sort_keys=True)
        assert stored_import != "null"
        checks.append(
            {"name": "import_trajectory_projection", "status": "pass", "detail": import_id}
        )
        assert (await call(client.post, "/reef/records", scenario, record))[0][
            "agent_record_id"
        ] == import_id
        assert (
            json.dumps(dispatcher.read_record(scenario, import_id), sort_keys=True) == stored_import
        )
        checks.append(
            {
                "name": "import_retry_idempotent",
                "status": "pass",
                "detail": "identical re-import dedups",
            }
        )
        foreign_id = f"lab-replay:{trial_name}:foreign:1"
        await call(
            client.post,
            "/reef/records",
            scenario + ":foreign",
            {
                "agent_record_id": foreign_id,
                "request_type": "inference",
                "payload": {"origin": "lab_replay_foreign_probe"},
            },
        )
        first, _ = await call(
            client.post,
            "/reef/harness/proposals",
            scenario,
            {
                "mutations": [CREATE_RULES],
                "reason": "har138 fixture: add marker rules",
                "session": "har138-fixture",
                "release_id": base_release,
            },
        )
        assert first["admitted"] is True
        report1 = {
            "score": reward,
            "feedback": {"origin": "lab_replay", "note": "saved verifier reward, replayed"},
            "references": [import_id],
            "agent_record_id": f"lab-replay:{trial_name}:report:1",
        }
        assert (await call(client.report, scenario, report1))["agent_record_id"] == report1[
            "agent_record_id"
        ]
        checks.append(
            {"name": "report_links_imported_inference", "status": "pass", "detail": import_id}
        )
        release1, manifest1 = await wait_release(client, scenario, None)
        checks.append(
            {
                "name": "fixture_step1_publishes",
                "status": "pass",
                "detail": f"proposal {first['proposal_id']} release {release1[:8]}",
            }
        )
        stored_report = json.dumps(
            dispatcher.read_record(scenario, report1["agent_record_id"]), sort_keys=True
        )
        assert stored_report != "null"
        assert (await call(client.report, scenario, report1))["agent_record_id"] == report1[
            "agent_record_id"
        ]
        assert (
            json.dumps(dispatcher.read_record(scenario, report1["agent_record_id"]), sort_keys=True)
            == stored_report
        )
        checks.append(
            {
                "name": "report_retry_idempotent",
                "status": "pass",
                "detail": "identical resend dedups",
            }
        )
        status = await expect_status(
            call(client.report, scenario, dict(report1, score=-reward or 0.5)), (409,), "conflict"
        )
        assert (
            json.dumps(dispatcher.read_record(scenario, report1["agent_record_id"]), sort_keys=True)
            == stored_report
        )
        checks.append(
            {
                "name": "same_id_conflict_rejected",
                "status": "pass",
                "detail": f"status {status}; stored report unchanged",
            }
        )
        for probe, refs in (
            ("missing_reference", ["har138-no-such-record"]),
            ("foreign_reference", [foreign_id]),
        ):
            body = {
                "score": reward,
                "references": refs,
                "agent_record_id": f"lab-replay:{trial_name}:{probe}",
            }
            status = await expect_status(call(client.report, scenario, body), (400,), probe)
            checks.append(
                {"name": f"{probe}_rejected", "status": "pass", "detail": f"status {status}"}
            )
        assert any(
            row.get("release_id") == release1
            for row in await call(client.harness_releases, scenario)
        )
        second, _ = await call(
            client.post,
            "/reef/harness/proposals",
            scenario,
            {
                "mutations": [GROW_RULES],
                "reason": "har138 fixture: grow marker rules",
                "session": "har138-fixture",
                "release_id": release1,
            },
        )
        assert second["admitted"] is True
        fixture_id = "har138:synthetic-fixture:step2"
        await call(
            client.post,
            "/reef/records",
            scenario,
            {
                "agent_record_id": fixture_id,
                "request_type": "inference",
                "payload": {"origin": "synthetic_protocol_control", "fixture_step": 2},
            },
        )
        report2 = {
            "score": 0.0,
            "feedback": {"origin": "synthetic_protocol_control", "note": "not a Lab trial result"},
            "references": [fixture_id],
            "agent_record_id": "har138:synthetic-fixture:report2",
        }
        # Unpinned: x-reef-release-id binds the scenario base, never an evolved head.
        assert (await call(client.report, scenario, report2))["agent_record_id"] == report2[
            "agent_record_id"
        ]
        release2, manifest2 = await wait_release(client, scenario, release1)
        checks.append(
            {
                "name": "fixture_step2_publishes",
                "status": "pass",
                "detail": f"proposal {second['proposal_id']} release {release2[:8]}",
            }
        )
        head_dir = out / "pull-head"
        assert await call(client.harness_pull, scenario, head_dir) == release2
        sidecar = json.loads((head_dir / HARNESS_RELEASE_SIDECAR).read_text(encoding="utf-8"))
        assert (
            sidecar["release_id"] == release2 and sidecar["content_id"] == manifest2["content_id"]
        )
        assert sidecar["files"] == sorted(manifest2["files"])
        head_bytes = {rel: (head_dir / rel).read_bytes() for rel in sidecar["files"]}
        assert head_bytes == {rel: text.encode("utf-8") for rel, text in manifest2["files"].items()}
        checks.append(
            {
                "name": "pull_head_preserves_identity",
                "status": "pass",
                "detail": f"release {release2[:8]} content {sidecar['content_id'][:16]}",
            }
        )
        pinned_dir = out / "pull-pinned"
        assert (
            await call(client.harness_pull, scenario, pinned_dir, release_id=release1) == release1
        )
        pinned_sidecar = json.loads(
            (pinned_dir / HARNESS_RELEASE_SIDECAR).read_text(encoding="utf-8")
        )
        assert (
            pinned_sidecar["release_id"] == release1 and pinned_sidecar["files"] == sidecar["files"]
        )
        pinned_bytes = {rel: (pinned_dir / rel).read_bytes() for rel in pinned_sidecar["files"]}
        assert pinned_sidecar["content_id"] == manifest1["content_id"]
        assert pinned_bytes == {
            rel: text.encode("utf-8") for rel, text in manifest1["files"].items()
        }
        assert any(pinned_bytes[rel] != head_bytes[rel] for rel in head_bytes)
        checks.append(
            {
                "name": "pinned_older_release_verified",
                "status": "pass",
                "detail": f"release {release1[:8]} distinct bytes",
            }
        )
        status = await expect_status(
            call(
                client.harness_pull,
                scenario,
                out / "pull-unknown",
                release_id="har138-no-such-release",
            ),
            (404,),
            "unknown_release",
        )
        assert not (out / "pull-unknown").exists()
        checks.append(
            {"name": "unknown_release_refused", "status": "pass", "detail": f"status {status}"}
        )
        file_hashes = {rel: hashlib.sha256(b).hexdigest() for rel, b in head_bytes.items()}
        pinned_hashes = {rel: hashlib.sha256(b).hexdigest() for rel, b in pinned_bytes.items()}
        return {
            "schema": "har138-flow-replay/v1",
            "status": "pass",
            "implementation": {
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "reef_version": importlib.metadata.version("reef-infra"),
                "reef_app_sha256": hashlib.sha256(
                    Path(inspect.getfile(create_app)).read_bytes()
                ).hexdigest(),
                "sdk_client_sha256": hashlib.sha256(
                    (Path(sdk_path).parent / "client.py").read_bytes()
                ).hexdigest(),
            },
            "sdk": {
                "version": importlib.metadata.version("reef-client"),
                "source": SDK_SOURCE,
                "installed_at": sdk_path,
                "sidecar": HARNESS_RELEASE_SIDECAR,
            },
            "source_trial": {
                "job": str(job),
                "trial_dir": str(trial_dir),
                "trial": trial_name,
                "task": result.get("task_name"),
                "saved_reward": reward,
                "hashes": hashes,
                "projection": "counts/hashes only; no request history, logprobs, or prompts",
            },
            "scenario": scenario,
            "protocol_checks": checks,
            "fixture": {
                "proposals": [first["proposal_id"], second["proposal_id"]],
                "releases": [release1, release2],
                "content_id": manifest2["content_id"],
                "second_step_trigger": "separate synthetic inference/report; the saved Lab reward is not rewritten",
                "selection": "fixture-selected (marker rules beat the empty seed); NOT measured improvement",
                "pulls": [
                    {"dir": "pull-head", "release_id": release2, "files": file_hashes},
                    {"dir": "pull-pinned", "release_id": release1, "files": pinned_hashes},
                ],
            },
            "limitations": [
                "replay, not live: no provider request, so it cannot predict how a changed agent acts",
                "single saved trial; no statistical claim",
                "harness gain is fixture-selected, not earned from evidence",
                "skill channel not exercised: the fixture seed serves no skills/SKILL.md",
            ],
        }
    finally:
        if runner is not None:
            await runner.cleanup()
        if dispatcher is not None:
            dispatcher.close()
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    args = argparse.ArgumentParser(
        description="HAR-138 Lab-evidence -> Reef record/report -> served-harness replay"
    )
    args.add_argument(
        "--trial", required=True, help="published Harbor job dir holding one nested trial"
    )
    args.add_argument("--out", required=True, help="NEW output folder (refused when it exists)")
    args.add_argument("--scenario", default="har138-replay")
    opts = args.parse_args()
    out = Path(opts.out)
    if out.exists():
        sys.exit(f"--out {out} exists; refusing to overwrite")
    sdk_path = require_fixed_sdk()
    out.mkdir(parents=True)
    receipt = asyncio.run(run(opts.trial, out, opts.scenario, sdk_path))
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "receipt": str(out / "receipt.json"),
                "checks": len(receipt["protocol_checks"]),
                "status": "pass",
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
