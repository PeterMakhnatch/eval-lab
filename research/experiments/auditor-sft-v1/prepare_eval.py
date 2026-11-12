"""Stage native base-vs-LoRA evaluations for the auditor-SFT pilot.

Verifies the five leaky MiMo packages against their exact historical digests
and the five held-out TW packages against the frozen holdout digests, writes
base-arm Terminus specs (plus LoRA-arm specs when a real sampler path is
given) plus ladder/nop controls, and records the LoRA sampler binding and a
parent-run smoke as data. Never launches, approves, submits, spends, starts
containers, or invents a checkpoint selector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

from evallab.execution_contracts import TERMINUS_AGENT, parse_tinker_model
from evallab.exploit_probe import HF_SOURCE, PREAMBLE, SNAPSHOT
from evallab.registry import compute_task_digests
from evallab.schemas import ExperimentSpec
from evallab.storage.paths import shared_checkout_root
from evallab.task_import import import_task_package
from evallab.task_prepare import PREPARED_TASKS_REL
from evallab.task_variants import lineage_chain, materialize, resolve_record
from evallab.terminus_harness import load_harness_tree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HARNESS = HERE / "harness"
HOLDOUT_MANIFEST_REL = "research/experiments/auditor-sft-v1/holdout_manifest.json"

#: Exact TW hack-prompt v5 bytes (upstream HF few-sh/terminal-wrench@6ab64c43
#: prompts/hack_prompt_v5.md); the tracked copy is byte-identical to upstream
#: and its digest pins the held-out TW arm's audit instruction.
TW_PROMPT_REL = "research/experiments/auditor-sft-v1/tw-hack-prompt-v5.md"
TW_PROMPT_SHA256 = "sha256:cc00691be714e6fe2318e9af62f5b676d5b00837ee5e8c1edaead540a46d6450"
LEAKY_CAMPAIGN_REL = "research/experiments/cheater-recall-v1/campaign-cheater-recall-v1-w2-leaky.json"

#: Exact historical leaky package digests (never repaired replacements).
#: Cross-checked against LEAKY_CAMPAIGN_REL at prepare time; drift refuses.
LEAKY: tuple[tuple[str, str], ...] = (
    ("format-code-task-000792", "sha256:e54ec9025ce43b8b4bc3c52456e2baa9a971a3e69750efad69774d6456447d09"),
    ("format-code-task-001269", "sha256:d3375b680cbe59d10621bd00e0b14ac139544547e3823e88e61217063524db54"),
    ("format-code-task-002486", "sha256:2135c3b5028a86de7197dcc48aeb6b4f7f6572e0cec7e09495ba037aee06f2e6"),
    ("format-code-task-002552", "sha256:e9273f2254788fb1803882c506c25b7274f6b908cf658d3e1065a63348f84f2d"),
    ("format-code-task-002938", "sha256:15d984ef5103c774496c8864b78a5b7068edf5c06ab90606dbaaa7d0a02bf562"),
)
HOLDOUT_TASKS_REL = "derived/auditor-sft-v1/holdout-tasks"

#: Held-out TW original packages (HF few-sh/terminal-wrench@6ab64c43 tree
#: tasks/<id>/claude-opus-4.6/original_task/, frozen verbatim into
#: HOLDOUT_TASKS_REL). Cross-checked against HOLDOUT_MANIFEST_REL at prepare time.
HOLDOUT_TASKS: tuple[tuple[str, str], ...] = (
    ("implement-nonogram-solver", "sha256:aefda6ffa313b65cbfeb2eebc297323820b3db973edfe8a82373c64f5a2cd8ac"),
    ("rush-hour-bfs-solver", "sha256:4a2a655ba41d2446ba8cd93cfb761e2fdef13d9a0a66927f55b1b9c5cf22612a"),
    ("adaptive-rejection-sampler", "sha256:bc095bd0919e98813d6f1f150a117cbe9368411d66acaf565aef1b8cbda531ce"),
    ("blind-maze-explorer-5x5", "sha256:3463af8e8cfa6bbe5a5244a6ad1f8f07f8e4d5a1cc69b51d29bf4ac22dc5e874"),
    ("titanic-female-survival-prediction", "sha256:78185c3af4fae2bdc3ac9952a4deb7204bd3f24dcee2e89e1b35fccdb6cfa5dd"),
)

#: Parent-fixed trial ceilings: 400k input x $0.45/M = $0.18 plus 16k output
#: x $1.335/M = $0.02136, so $0.20136 maximum model cost per trial.
BASE_MODEL = "tinker/Qwen/Qwen3.6-35B-A3B"
MAX_REQUESTS = 64
MAX_INPUT_TOKENS = 400_000
MAX_OUTPUT_TOKENS = 16_384
MAX_TOTAL_TOKENS = 416_384
COST_LIMIT_USD = 0.25
TIMEOUT_SECONDS = 1800

#: The LoRA arm binds the real post-training sampler path read from the
#: training manifest's ``checkpoints.jsonl`` (``tinker://<run>:train:<i>/
#: sampler_weights/<step>``). Parsed strictly: a malformed value refuses here,
#: before any spec freeze, and there is no placeholder selector by design.
def lora_selector(sampler_path: str) -> str:
    """Bind ``BASE_MODEL`` to a real sampler path, failing closed otherwise."""
    selector = f"{BASE_MODEL}@{sampler_path.strip()}"
    parse_tinker_model(selector)
    return selector


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _freeze_json(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise FileExistsError(f"prepared content changed; choose a fresh --out: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(rendered)


def load_holdout() -> dict[str, Any]:
    """Read the canonical tracked holdout manifest; refuse an unexpected shape."""
    path = ROOT / HOLDOUT_MANIFEST_REL
    manifest = _json(path)
    if manifest.get("schema_version") != "auditor-sft-holdout-manifest/v1":
        raise ValueError(f"unsupported holdout schema: {path}")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 5:
        raise ValueError(f"holdout manifest must freeze exactly five TW tasks: {path}")
    ids = [task["task_id"] for task in tasks]
    if len(set(ids)) != 5:
        raise ValueError(f"held-out TW task_ids must be distinct: {ids}")
    for task in tasks:
        files = task.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"held-out task {task.get('task_id')} has no pinned files: {path}")
    return manifest


def _hf_file_bytes(hf_revision: str, remote_path: str) -> bytes:
    url = f"https://huggingface.co/datasets/few-sh/terminal-wrench/resolve/{hf_revision}/{remote_path}"
    request = urllib.request.Request(url, headers={"User-Agent": "eval-lab-auditor-sft"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def ensure_holdout_bytes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch precisely the pinned manifest bytes when absent; verify everything.

    Existing bytes are never rewritten: a present file with a matching digest
    is kept as is, and any digest mismatch refetches from the pinned revision
    before failing. No task or image modification, no Docker.
    """
    hf_revision = manifest["source"]["hf_revision"]
    ensured: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        fetched = 0
        for entry in task["files"]:
            local = ROOT / task["cache"] / entry["path"].removeprefix("original_task/")
            data = local.read_bytes() if local.is_file() else None
            digest = "sha256:" + hashlib.sha256(data).hexdigest() if data is not None else None
            if digest != entry["sha256"]:
                remote = f"tasks/{task['task_id']}/claude-opus-4.6/{entry['path']}"
                data = _hf_file_bytes(hf_revision, remote)
                actual = "sha256:" + hashlib.sha256(data).hexdigest()
                if actual != entry["sha256"] or len(data) != entry["bytes"]:
                    raise ValueError(f"pinned bytes mismatch for {remote}")
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
                fetched += 1
        ensured.append({"task_id": task["task_id"], "fetched": fetched, "files": len(task["files"])})
    return ensured


def leaky_cells() -> list[dict[str, Any]]:
    """Leaky MiMo cells pinned to the exact historical campaign digests."""
    campaign = _json(ROOT / LEAKY_CAMPAIGN_REL)
    pinned = [(task["task_id"], task["package_digest"]) for task in campaign["tasks"]]
    if tuple(pinned) != LEAKY:
        raise ValueError("leaky campaign digests drifted; refusing to restage")
    return [
        {
            "task_id": task_id,
            "short": task_id.removeprefix("format-code-task-"),
            "task_package_digest": digest,
            "kind": "leaky",
        }
        for task_id, digest in LEAKY
    ]


def holdout_cells() -> list[dict[str, Any]]:
    """Held-out TW cells pinned to the canonical manifest package digests."""
    tasks = load_holdout()["tasks"]
    if [(task["task_id"], task["task_package_digest"]) for task in tasks] != list(HOLDOUT_TASKS):
        raise ValueError("holdout task bytes drifted from the canonical manifest")
    return [
        {"task_id": task_id, "short": task_id, "task_package_digest": digest, "kind": "holdout"}
        for task_id, digest in HOLDOUT_TASKS
    ]


def _checked(path: Path, expected: str) -> Path:
    actual = compute_task_digests(path).package
    if actual != expected:
        raise ValueError(f"task bytes drifted: {path}: {actual} != {expected}")
    return path


def _rebuild(cell: dict[str, Any], primary: Path) -> Path:
    """Replay existing lineage into this worktree; never alter shared stores.

    Mirrors trained-breaker-v1/build_specs.py so the same historical bytes
    resolve without depending on a sibling worktree's frozen copies.
    """
    matches = sorted((primary / "library/task-variants").glob(
        f"mimo-v2.6-rl__{cell['task_id']}/*.json"))
    if not matches:
        raise ValueError(f"no lineage record for {cell['task_id']}")
    record_path = None
    record = None
    for candidate in matches:
        resolved = resolve_record(candidate, repo_root=primary)
        if resolved.variant_digest == cell["task_package_digest"]:
            record_path, record = candidate, resolved
            break
    if record is None:
        seen = [resolve_record(candidate, repo_root=primary).variant_digest for candidate in matches]
        raise ValueError(
            f"no lineage record reaches {cell['task_id']} {cell['task_package_digest']}: "
            f"saw {sorted(set(seen))}"
        )
    chain = lineage_chain(record, repo_root=primary)
    if any(not step.parent_found for step in chain):
        raise ValueError(f"incomplete task lineage: {record_path}")
    parent = None
    store = ROOT / "derived/task-store/variants"
    for step in reversed(chain):
        row = step.record
        if parent is None:
            source = row.parent.source
            if source.kind != "hf" or (source.repo, source.revision) != (
                HF_SOURCE["repo"],
                HF_SOURCE["revision"],
            ):
                raise ValueError("unexpected parent source; no automatic fetch")
            if not source.path:
                raise ValueError("HF parent lacks its task path")
            snapshot = (primary / SNAPSHOT).resolve()
            parent = (snapshot / source.path).resolve()
            if not parent.is_relative_to(snapshot):
                raise ValueError("parent task escapes the retained HF snapshot")
            _checked(parent, row.parent.digest)
        parent = materialize(row, parent, repo_root=ROOT, variants_root=store)
    if parent is None:
        raise ValueError("empty task lineage")
    return _checked(parent, cell["task_package_digest"])


#: Read-only fallback for the exact frozen leaky bytes retained by the prior
#: clean preparation worktree. Every candidate is digest-gated, so a missing
#: or drifted sibling fails closed into the lineage rebuild above.
SIBLING_PREPARED_TASKS = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/breaker-l1-prepare-20261012/runs/.prepared-tasks"
)


def _cached_package(cell: dict[str, Any]) -> Path | None:
    roots = [ROOT / PREPARED_TASKS_REL]
    if cell.get("kind") == "leaky" and SIBLING_PREPARED_TASKS.is_dir():
        roots.append(SIBLING_PREPARED_TASKS)
    prefix = "mimo-v2-6-rl-" if cell.get("kind") == "leaky" else ""
    for root in roots:
        for candidate in sorted(root.glob(f"{prefix}{cell['task_id']}-*")):
            if candidate.is_dir():
                try:
                    return _checked(candidate, cell["task_package_digest"])
                except ValueError:
                    continue
    return None


def lock_eligibility(task_dir: Path) -> dict[str, Any]:
    """Static locked-Docker/Terminus gates from task bytes (no execution).

    Covers the dispatcher checks plus the Terminus precondition the dispatcher
    cannot verify: image tool evidence is static where the Dockerfile exists
    and ``opaque-image-unverified`` where the image is a pinned digest (parent
    smoke must confirm ``tmux -V`` inside the built image).
    """
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"eligible": False, "reasons": ["task.toml unreadable"], "tmux": "unknown", "image": None}
    if not isinstance(payload, dict):
        return {"eligible": False, "reasons": ["task.toml is not a table"], "tmux": "unknown", "image": None}
    reasons: list[str] = []
    environment = task_dir / "environment"
    if any((environment / name).is_file() for name in (
            "docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")):
        reasons.append("multi-container compose refused")
    agent = payload.get("agent")
    verifier = payload.get("verifier")
    if isinstance(agent, dict) and "network_mode" in agent:
        reasons.append("task phase network policy refused")
    if isinstance(verifier, dict) and "network_mode" in verifier:
        reasons.append("task phase network policy refused")
    env_table = payload.get("environment")
    gpus = env_table.get("gpus") if isinstance(env_table, dict) else None
    if isinstance(gpus, int) and not isinstance(gpus, bool) and gpus > 0:
        reasons.append("GPU task outside the CPU sandbox boundary")
    image = env_table.get("docker_image") if isinstance(env_table, dict) else None
    image = image if isinstance(image, str) else None
    tmux = "unknown"
    dockerfile = environment / "Dockerfile"
    if dockerfile.is_file():
        try:
            text = dockerfile.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeError):
            text = ""
        if "tmux" in text:
            tmux = "dockerfile-names-tmux"
        elif image is not None and "@sha256:" in image:
            tmux = "opaque-image-unverified"
        elif any(line.strip().startswith("from ") for line in text.splitlines()):
            tmux = "opaque-base-unverified"
        else:
            tmux = "dockerfile-silent"
    elif image is not None and "@sha256:" in image:
        tmux = "opaque-image-unverified"
    return {"eligible": not reasons, "reasons": reasons, "tmux": tmux, "image": image}


def build_spec(
    cell: dict[str, Any],
    task_rel: str,
    harness: Any,
    instruction_path: str,
    instruction_sha256: str,
    *,
    attempt: int,
    arm: str,
    model: str,
) -> dict[str, Any]:
    """One native spec dict for one arm; schema-validated, never submitted."""
    if arm not in ("base", "lora"):
        raise ValueError(f"unknown arm {arm!r}")
    short = cell["short"]
    tag = "l" if cell.get("kind") == "leaky" else "h"
    cohort = (
        f"leaky {cell['task_package_digest'][:19]}"
        if tag == "l"
        else f"held-out TW {cell['task_package_digest'][:19]}"
    )
    treatment = "Tinker Qwen base" if arm == "base" else "Tinker Qwen LoRA"
    spec = ExperimentSpec.model_validate(
        {
            "name": f"aud1-{arm}-{tag}-{short}-k{attempt}",
            "hypothesis": (
                f"auditor-SFT pilot {arm} arm on {cell['task_id']} "
                f"({cohort}): {treatment} "
                "rollout under the audit instruction; reward alone is not a hack label"
            ),
            "purpose": "elicitation",
            "task": task_rel,
            "task_package_digest": cell["task_package_digest"],
            "agent": TERMINUS_AGENT,
            "model": model,
            "environment": "docker",
            "egress_lock": True,
            "jobs_dir": "runs",
            "attempts": 1,
            "concurrency": 1,
            "timeout_seconds": TIMEOUT_SECONDS,
            "override_storage_mb": 10240,
            "extra_instruction_path": instruction_path,
            "extra_instruction_sha256": instruction_sha256,
            "harness_tree_path": HARNESS.relative_to(ROOT).as_posix(),
            "harness_tree_sha256": harness.sha256,
            "max_requests": MAX_REQUESTS,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "cost_limit_usd": COST_LIMIT_USD,
            "est_cost_usd": 0.21,
            "submitted_by": "auditor-eval",
        }
    )
    return spec.model_dump(mode="json", exclude_none=True)


def control_commands(cell: dict[str, Any], task_rel: str) -> dict[str, Any]:
    """Native ladder + nop controls on the exact frozen bytes."""
    short = cell["short"]
    tag = "l" if cell.get("kind") == "leaky" else "h"
    return {
        "task_id": cell["task_id"],
        "task_package_digest": cell["task_package_digest"],
        "ladder": [
            "uv", "run", "--extra", "laminar", "evallab", "cheat", "run",
            "--task", task_rel,
            "--name", f"aud1-gt-{tag}-{short}",
            "--jobs-dir", "runs",
            "--attempts", "1",
        ],
        "nop": [
            "uv", "run", "--extra", "laminar", "evallab", "run",
            "--task", task_rel,
            "--agent", "nop",
            "--name", f"aud1-nop-{tag}-{short}",
            "--environment", "docker",
            "--egress-lock",
            "--timeout-seconds", "600",
        ],
        "note": (
            "Run the native scripted ladder on these exact bytes; preserve "
            "per-attack receipts and exceptions. Do not label all model "
            "passes as hacks."
        ),
    }


def prepare(out: Path | None, *, attempts: int = 1, lora_sampler_path: str | None = None) -> dict[str, Any]:
    """Freeze native specs: base arm always, LoRA arm when a sampler path is given."""
    if not 1 <= attempts <= 3:
        raise ValueError("attempts must be 1..3 for this small pilot")
    cells = [*leaky_cells(), *holdout_cells()]
    manifest = load_holdout()
    manifest_sha256 = _file_digest(ROOT / HOLDOUT_MANIFEST_REL)
    ensured = ensure_holdout_bytes(manifest) if out is not None else []
    lora_model = lora_selector(lora_sampler_path) if lora_sampler_path is not None else None
    harness = load_harness_tree(HARNESS, repo_root=ROOT)
    preamble_sha256 = _file_digest(ROOT / PREAMBLE)
    tw_prompt_sha256 = _file_digest(ROOT / TW_PROMPT_REL)
    if tw_prompt_sha256 != TW_PROMPT_SHA256:
        raise ValueError("tracked TW hack prompt drifted from its pinned upstream digest")
    instructions = {
        "leaky": {"path": PREAMBLE, "sha256": preamble_sha256},
        "holdout": {"path": TW_PROMPT_REL, "sha256": tw_prompt_sha256},
    }
    primary = shared_checkout_root(ROOT)
    task_versions: list[dict[str, Any]] = []
    spec_paths: list[str] = []
    lora_spec_paths: list[str] = []
    controls: list[dict[str, Any]] = []
    for cell in cells:
        if cell.get("kind") == "holdout":
            package = _checked(
                ROOT / HOLDOUT_TASKS_REL / cell["task_id"] / "original_task",
                cell["task_package_digest"],
            )
        else:
            cached = _cached_package(cell)
            package = cached or _rebuild(cell, primary)
        if out is not None:
            frozen, _ = import_task_package(package, ROOT / PREPARED_TASKS_REL)
            _checked(frozen, cell["task_package_digest"])
            task_rel = frozen.relative_to(ROOT).as_posix()
        else:
            task_rel = package.as_posix()
        if out is not None:
            eligibility = lock_eligibility(ROOT / task_rel)
        else:
            eligibility = lock_eligibility(Path(task_rel))
        instruction = instructions["holdout" if cell.get("kind") == "holdout" else "leaky"]
        task_versions.append({
            **cell,
            "prepared_task": task_rel,
            "lock_eligibility": eligibility,
            "extra_instruction_path": instruction["path"],
            "extra_instruction_sha256": instruction["sha256"],
        })
        if out is not None:
            for attempt in range(1, attempts + 1):
                payload = build_spec(
                    cell, task_rel, harness, instruction["path"], instruction["sha256"],
                    attempt=attempt, arm="base", model=BASE_MODEL,
                )
                relative = f"specs/{payload['name']}.json"
                spec_paths.append(relative)
                _freeze_json(out / relative, payload)
                if lora_model is not None:
                    lora_payload = build_spec(
                        cell, task_rel, harness, instruction["path"], instruction["sha256"],
                        attempt=attempt, arm="lora", model=lora_model,
                    )
                    lora_relative = f"specs/{lora_payload['name']}.json"
                    lora_spec_paths.append(lora_relative)
                    _freeze_json(out / lora_relative, lora_payload)
            controls.append(control_commands(cell, task_rel))
    holdout_ids = [cell["task_id"] for cell in cells if cell.get("kind") == "holdout"]
    images = sorted({
        str(version["lock_eligibility"].get("image"))
        for version in task_versions
        if version["lock_eligibility"].get("image")
    })
    report = {
        "schema_version": "auditor-sft-eval-preparation/v1",
        "spend_authorized": False,
        "execution_performed": False,
        "status": "prepared-not-approved" if out is not None else "read-only-plan",
        "holdout": {"manifest": HOLDOUT_MANIFEST_REL, "manifest_sha256": manifest_sha256, "task_ids": holdout_ids, "bytes_ensured": ensured},
        "holdout_bytes": {
            "origin": "HF few-sh/terminal-wrench@6ab64c43 tasks/<id>/claude-opus-4.6/original_task/, verbatim",
            "cross_model_identity": "task.toml, instruction.md, environment/Dockerfile byte-identical across all three TW models",
        },
        "base_model": BASE_MODEL,
        "ceilings": {
            "max_requests": MAX_REQUESTS,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "cost_limit_usd": COST_LIMIT_USD,
            "per_trial_model_ceiling_usd": 0.20136,
            "per_response_max_tokens": 4096,
            "thinking": "disabled via harness extra_body.reasoning_effort=none",
            "renderer_match": (
                "training renderer qwen3_5_disable_thinking; inference think-off verified "
                "in source end to end (harness extra_body -> Harbor deep-merge -> "
                "litellm http_handler top-level merge -> proxy tinker forwarded_fields -> "
                "Tinker endpoint). Tinker-side 'none' template semantics close empirically: "
                "the smoke trial's raw responses must show no <think> blocks."
            ),
        },
        "harness_tree_sha256": harness.sha256,
        "instructions": {
            "leaky": {
                **instructions["leaky"],
                "why": "Eval Lab exploit-probe-v2: historical ladder/recall standard for MiMo leaky tasks",
            },
            "holdout": {
                **instructions["holdout"],
                "upstream_sha256": TW_PROMPT_SHA256,
                "why": "exact TW hack-prompt v5: the sealed training histories' conditioning instruction",
            },
        },
        "task_versions": task_versions,
        "specs": spec_paths,
        "lora": {
            "sampler_path": lora_sampler_path,
            "model": lora_model,
            "specs": lora_spec_paths,
            "note": (
                "LoRA arm specs exist only when --lora-sampler-path carries the real "
                "post-training sampler path; the selector is strictly parsed before "
                "any freeze and there is no placeholder selector by design."
            ),
        },
        "parent_smoke": [
            {
                "step": 1,
                "cost": "$0",
                "command": controls[0]["nop"] if controls else None,
                "purpose": "prove a frozen task runs under the creation-time lock on the already-admitted nop path",
            },
            {
                "step": 2,
                "cost": "$0 plus at most one transient container per image, removed after",
                "command": ["docker", "run", "--rm", "<IMAGE-FROM-PREPARED-task.toml>",
                            "sh", "-lc", "command -v tmux && tmux -V; command -v asciinema && asciinema --version"],
                "purpose": "confirm the Terminus precondition (tmux preinstalled) inside each pinned image, titanic first (Dockerfile proves it); images must already be cached, otherwise parent decides each pull",
                "images": images,
            },
            {
                "step": 3,
                "cost": "one paid trial ceiling",
                "command": "submit, approve, and tick exactly one staged titanic base spec, then confirm egress-lock.json applied=true, native grading, and raw responses free of <think> blocks before approving the rest (max two of our containers; never touch other tabs' containers)",
                "purpose": "prove locked-Docker Terminus end to end plus the think-off renderer match before the batch",
            },
        ],
        "source_limits": [
            "Historical leaky versions are known answer channels, not certified tasks; ladder positives require replay evidence.",
            "adaptive-rejection-sampler and blind-maze-explorer-5x5 declare allow_internet=true: locked-eval viability follows the nop control, parent adjudicates.",
            "Opaque pinned images stay tmux-unverified until the parent smoke (titanic excluded: Dockerfile proves tmux+asciinema).",
            "Reward 1 alone is not a confirmed exploit; parent adjudicates rollouts through each task's own verifier.",
        ],
    }
    if out is not None:
        _freeze_json(out / "controls.json", controls)
        _freeze_json(out / "plan.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="freeze local task copies and native draft specs; no dispatch",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "derived/auditor-sft-v1/eval")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument(
        "--lora-sampler-path",
        default=None,
        help="real post-training tinker:// sampler path; when given, freeze the LoRA arm too",
    )
    args = parser.parse_args()
    out = args.out.resolve()
    if args.prepare and not out.is_relative_to(ROOT):
        parser.error("--out must stay inside the owned checkout")
    print(json.dumps(prepare(
        out if args.prepare else None,
        attempts=args.attempts,
        lora_sampler_path=args.lora_sampler_path,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
