"""Frozen, once-only DEV/TEST campaign for Task QA v2 on tb4-audit-v1.

Actor side only. This module never reads the answer key, the scorer, case-level TEST labels or any
prior TEST baseline output; ``score.py`` is the only entry that does, after the actors are closed.
It does not implement a model loop, a runner or a scheduler: every task is one subprocess of the
core entry ``evallab qa env audit`` (parent-supplied), run with the owned interpreter and the
current source root.

Commands (repository root, owned interpreter)::

    campaign.py freeze        --campaign-dir D --condition agent_v2|plain_read --split dev|test ...
    campaign.py verify        --campaign-dir D
    campaign.py authorize-test --campaign-dir D --by WHO --reference WHERE   # TEST only
    campaign.py run           --campaign-dir D                               # agent_v2
    campaign.py close         --campaign-dir D                               # after a crash
    campaign.py plain-prepare --campaign-dir D                               # plain_read
    campaign.py plain-collect --campaign-dir D                               # plain_read

Contract enforced here:

* ``freeze`` pins the exact commit (merged into ``origin/main`` for TEST), every tracked source file
  the actor can import, the model/lane/limits, the hackbook, the owner execution policy, the declared
  upstream pin (plus per-task byte binding), the exported view manifest, the PM port mapping, the
  split-guard exposure registry result and the direction grouping, before any view input is parsed.
  The TEST freeze only hashes ``bundle.json`` bytes; it never parses the public input list.
* Each frozen system exposes each task at most once: an exclusive marker is created before a task's
  files can reach an actor, and no code path retries a launched task, whatever its outcome.
* Source drift (HEAD, tracked or untracked source, hackbook, execution policy, view manifest, PM
  mapping) found before any task stops the campaign: prompts and the agent are never edited between
  tasks, and a campaign never restarts once ``score.py`` has started.
* Tasks the split guard records as ``tuned``/``selected`` for the registry agent are not run (they stay
  unmeasured in every denominator); ``inspected`` records and the one known unregistered exposure are
  disclosed and scored separately.
  Resolving ``items``/``where`` registry records to task names loads the guard's keys in the
  freezing process only; just bare task names and exposure ids are persisted, and no actor process
  (the core subprocess) is given a key, a score or any label.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

EXP = HERE.relative_to(REPO).as_posix()
KEYS = REPO / "research/answer-keys"
VIEWS_MODULE = KEYS / "tb4-audit-v1/views.py"
GUARD_MODULE = KEYS / "splits/guard.py"
KEY_TAXONOMY = KEYS / "tb4-audit-v1/taxonomy.json"
PLAIN_PROMPT = HERE / "plain_read_prompt.md"

FREEZE_SCHEMA = "tb4.qa.v2.freeze/v1"
CONDITIONS = ("agent_v2", "plain_read")
LANES = ("omp", "codex", "openai")
SPLITS = ("dev", "test")
MAX_CONCURRENCY = 2
DEFAULT_SEED = 20261021
PLAIN_BATCH = 6
SESSION_SCHEMA_PREFIX = "qa.env.audit/"
#: Everything an actor process can import or be steered by, plus the scorer-side code whose bytes
#: the comparison depends on. Tracked files only: ``git ls-files`` supplies the exact set.
SOURCE_PATHSPECS = (
    "src/evallab", "pyproject.toml", "uv.lock",
    "research/answer-keys/tb4-audit-v1/views.py",
    "research/answer-keys/tb4-audit-v1/score_audit.py",
    "research/answer-keys/tb4-audit-v1/triage_signals.py",
    "research/answer-keys/tb4-audit-v1/taxonomy.json",
    "research/answer-keys/splits/guard.py",
    "research/answer-keys/splits/exposure.jsonl",
    "research/experiments/tb4-qa-agent-v1-20261020/score.py",
    f"{EXP}/campaign.py", f"{EXP}/score.py", f"{EXP}/plain_read_prompt.md",
)
#: Exposed to earlier systems without a registry record (v1 ``score.py`` ``EXPOSED``). Disclosed and
#: scored with and without; not excluded, because v1 ran it.
KNOWN_UNREGISTERED_EXPOSURES = {
    "biped-contact-dynamics": "exposed during v0 development; no splits/exposure.jsonl record",
}
#: Same class -> key-category conversion v1 and the plain-read baseline applied to their findings.
CATEGORY = {
    "weak_check": "weak", "unstated_requirement": "prompt_misaligned",
    "instruction_grader_contradiction": "prompt_misaligned", "grader_tamperable": "tamperable",
    "answer_reachable": "reachable", "grader_logic_bug": "broken", "overstrict_oracle": "broken",
    "wrong_answer_key": "broken", "env_mismatch_or_flaky": "broken",
    "infeasible_as_written": "broken", "exploitable_llm_judge": "weak",
}
ENTRY = "import sys; from evallab.cli import main; sys.exit(main())"


# ---------------------------------------------------------------------------------------- utils


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owned_path(value: str | Path, *, must_exist: bool = False, kind: str = "path") -> Path:
    """Absolute path with no symlink anywhere on it (the core refuses symlinked evidence paths)."""
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f"{kind} must not traverse symlinks (use /private/tmp, not /tmp): {path}")
    if must_exist and not path.exists():
        raise ValueError(f"{kind} does not exist: {path}")
    return path


def write_new(path: Path, value: Any, *, mode: int = 0o444) -> None:
    """Immutable JSON artifact: exclusive create, sorted keys, no NaN."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(data)
    path.chmod(mode)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False).returncode == 0


def direction_group(direction: str) -> str:
    """Predeclared rule from the taxonomy ``direction`` string alone; ambiguity stays explicit."""
    text = direction.strip().lower()
    has_pass, has_fail = "false_pass" in text, "false_fail" in text
    if has_pass and not has_fail:
        return "wrong_pass_oriented"
    if has_fail and not has_pass and text.startswith("false_fail"):
        return "wrong_fail_oriented"
    return "mixed_or_unspecified"


def direction_groups() -> dict[str, Any]:
    taxonomy = read_json(KEY_TAXONOMY)
    classes = {
        row["key"]: {"direction": row["direction"], "group": direction_group(row["direction"])}
        for row in taxonomy["task_side"]
    }
    return {
        "rule": "false_pass only -> wrong_pass_oriented; string starting false_fail with no false_pass "
                "-> wrong_fail_oriented; anything else (either, or a qualified/both-ways direction) "
                "-> mixed_or_unspecified. Derived from the label-free taxonomy before any TEST view "
                "input; never adjusted from observed matches.",
        "source": {"path": KEY_TAXONOMY.relative_to(REPO).as_posix(),
                   "sha256": sha256_file(KEY_TAXONOMY)},
        "classes": dict(sorted(classes.items())),
    }


def source_files() -> list[str]:
    out = git("ls-files", "-z", "--", *SOURCE_PATHSPECS)
    files = sorted(rel for rel in out.split("\0") if rel)
    missing = [spec for spec in SOURCE_PATHSPECS
               if not any(f == spec or f.startswith(spec + "/") for f in files)]
    if missing:
        raise ValueError(f"frozen source paths are not tracked at HEAD: {missing}")
    return files


def source_dirty() -> str:
    return git("status", "--porcelain", "--untracked-files=all", "--", *SOURCE_PATHSPECS)


def source_manifest() -> dict[str, str]:
    """Per-file digests. Keys are a dict (unordered); the tree digest sorts ``.as_posix()`` strings."""
    return {rel: sha256_file(REPO / rel) for rel in source_files()}


def tree_digest(manifest: dict[str, str]) -> str:
    aggregate = hashlib.sha256()
    for rel in sorted(manifest):
        aggregate.update(f"{manifest[rel]}  ./{rel}\n".encode())
    return "sha256:" + aggregate.hexdigest()


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ------------------------------------------------------------------------------------- registry


def registry_exposure(agent: str, system: str, at: str) -> dict[str, Any]:
    """Resolve the split-guard registry to bare task names for this system.

    The guard loads every key (labels included) in this process only to turn ``items``/``where``
    records into task names. Only names, exposure ids and kinds are returned or persisted.
    """
    guard = load_module(GUARD_MODULE, "_tq1_split_guard")
    keys = guard.load_keys()
    records = guard._jsonl(guard.EXPOSURE)
    errors = guard.validate_exposures(records, keys)
    if errors:
        raise ValueError("split guard rejects the exposure registry: " + "; ".join(errors))
    row = {"agent": agent, "system": system, "date_utc": at}
    buckets: dict[str, dict[str, list[str]]] = {"excluded": {}, "disclosed": {}}
    applied = []
    for record in records:
        if not guard._applies(record, row):
            continue
        names = set(record.get("tasks") or [])
        for key_id, ids in guard.exposed_items(record, keys).items():
            names |= {guard.task_name(item["subject"]["task_id"])
                      for item in keys[key_id]["items"] if item["item_id"] in ids}
        bucket = "excluded" if record["kind"] in {"tuned", "selected"} else "disclosed"
        applied.append({"exposure_id": record["exposure_id"], "kind": record["kind"]})
        for name in names:
            buckets[bucket].setdefault(name, []).append(record["exposure_id"])
    for name, reason in KNOWN_UNREGISTERED_EXPOSURES.items():
        buckets["disclosed"].setdefault(name, []).append(f"unregistered: {reason}")
    return {
        "agent": agent, "row_date_utc": at, "applied_records": applied,
        "excluded_names": {k: sorted(v) for k, v in sorted(buckets["excluded"].items())},
        "disclosed_names": {k: sorted(v) for k, v in sorted(buckets["disclosed"].items())},
        "policy": "tuned/selected -> not run, unmeasured in every denominator; inspected and "
                  "unregistered exposures -> run, disclosed, scored with and without",
    }


# --------------------------------------------------------------------------------------- freeze


def _file_pin(value: str | None, kind: str, *, required: bool) -> dict[str, str] | None:
    if value is None:
        if required:
            raise ValueError(f"{kind} is required")
        return None
    path = owned_path(value, must_exist=True, kind=kind)
    if not path.is_file():
        raise ValueError(f"{kind} must be a regular file: {path}")
    return {"path": str(path), "sha256": sha256_file(path)}


def _upstream_pin(root_arg: str, declared: str) -> dict[str, Any]:
    root = owned_path(root_arg, must_exist=True, kind="--upstream-root")
    if not root.is_dir():
        raise ValueError("--upstream-root must be the directory holding the task packages")
    if len(declared) != 40 or any(c not in "0123456789abcdef" for c in declared):
        raise ValueError("--source-pin must be the declared 40-hex upstream commit")
    observed = None
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=False)
    if proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == root.resolve():
        observed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        if observed != declared:
            raise ValueError(f"upstream HEAD {observed} is not the declared pin {declared}")
    return {
        "root": str(root), "declared_pin": declared, "observed_git_head": observed,
        "note": ("git metadata present; HEAD equals the declared pin" if observed
                 else "extracted tree without git metadata: the declared pin plus the per-task byte "
                      "binding written at run time are the evidence; no HEAD is claimed"),
    }


def _runner(python_arg: str | None) -> dict[str, Any]:
    requested = Path(python_arg or REPO / ".venv/bin/python").expanduser().absolute()
    # Virtualenv launchers are symlinks by design. Preserve argv[0] so Python
    # finds pyvenv.cfg; pin the resolved executable independently.
    python = owned_path(requested.parent, must_exist=True, kind="--python parent") / requested.name
    executable = python.resolve(strict=True)
    if not executable.is_file() or not os.access(python, os.X_OK):
        raise ValueError("--python must name an executable interpreter")
    probe = subprocess.run(
        [str(python), "-c", "import evallab, json, sys; print(json.dumps({'file': evallab.__file__, "
         "'python': sys.version}))"], capture_output=True, text=True, env=child_env(), cwd=REPO,
        check=True)
    info = json.loads(probe.stdout)
    if not Path(info["file"]).resolve().is_relative_to((REPO / "src").resolve()):
        raise ValueError(f"the interpreter imports evallab from {info['file']}, not this source root")
    omp_path = shutil.which("omp")
    omp = subprocess.run([omp_path, "--version"], capture_output=True, text=True, check=False) \
        if omp_path else None
    return {"python": str(python), "python_version": info["python"], "evallab_file": info["file"],
            "python_executable": str(executable), "python_sha256": sha256_file(executable),
            "omp_executable": str(Path(omp_path).resolve()) if omp_path else None,
            "omp_sha256": sha256_file(Path(omp_path)) if omp_path else None,
            "omp_version": omp.stdout.strip() if omp is not None and omp.returncode == 0 else None}


def cmd_freeze(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, kind="--campaign-dir")
    if campaign.exists():
        raise ValueError("--campaign-dir must be new")
    if campaign.is_relative_to(REPO):
        raise ValueError("campaign evidence lives outside the repository")
    agent = args.condition == "agent_v2"
    if args.split == "test" and args.allow_unmerged:
        raise ValueError("--allow-unmerged is DEV only")
    if args.max_tokens is not None and args.max_tokens < 1:
        raise ValueError("--max-tokens must be positive")
    if agent and (args.max_tokens is None or args.lane is None):
        raise ValueError("agent_v2 needs --lane and --max-tokens")
    if agent and (args.max_seconds <= 0 or args.max_tool_calls < 1):
        raise ValueError("agent_v2 needs positive time and tool limits")
    if agent and args.lane == "openai":
        if (not args.endpoint or not args.api_key_env
                or not 0 < args.max_usd < float("inf")
                or args.input_usd_per_million is None or args.output_usd_per_million is None
                or not 0 < args.input_usd_per_million < float("inf")
                or not 0 < args.output_usd_per_million < float("inf")):
            raise ValueError("openai needs endpoint, key-env name, positive finite prices and model cap")
    elif agent and args.max_usd != 0:
        raise ValueError("subscription lanes need --max-usd 0")
    if agent and args.execution_config is None:
        raise ValueError("agent_v2 needs the owner --execution-config (backend/approval/ledger policy)")
    if args.split == "test" and args.pm_port_manifest is None:
        raise ValueError("a TEST freeze must pin the published PM port mapping/provider")

    commit = git("rev-parse", "HEAD")
    dirty = source_dirty()
    if dirty:
        raise ValueError("frozen source is not committed:\n" + dirty)
    has_origin = git_ok("rev-parse", "--verify", "--quiet", "origin/main")
    origin = git("rev-parse", "origin/main") if has_origin else None
    merged = has_origin and git_ok("merge-base", "--is-ancestor", commit, "origin/main")
    if args.split == "test" and not merged:
        raise ValueError("a TEST freeze needs a commit already merged into origin/main")
    if args.split == "dev" and not merged and not args.allow_unmerged:
        raise ValueError("DEV smoke on an unmerged commit needs --allow-unmerged")

    manifest = source_manifest()
    runner = _runner(args.python)
    groups = direction_groups()
    exposures_ledger = (owned_path(args.ledger_root, kind="--ledger-root") if args.ledger_root
                        else campaign / "exposure-ledger")
    if args.split == "test" and (args.ledger_root is None or exposures_ledger.is_relative_to(campaign)):
        raise ValueError("TEST needs a shared --ledger-root outside the campaign directory")
    view_root = owned_path(args.view, must_exist=True, kind="--view")
    bundle = view_root / "bundle.json"
    if not bundle.is_file():
        raise ValueError("--view is not an exported agent view (no bundle.json)")
    inputs: dict[str, Any] = {
        "view": {"root": str(view_root), "split": args.split},
        "view_bundle": {"path": str(bundle), "sha256": sha256_file(bundle)},
        "upstream": _upstream_pin(args.upstream_root, args.source_pin),
        "pm_port": _file_pin(args.pm_port_manifest, "--pm-port-manifest", required=False),
        "pm_port_status": ("pinned" if args.pm_port_manifest else
                           "unpublished: DEV freeze only, no PM port mapping pinned"),
        "execution_config": _file_pin(args.execution_config, "--execution-config", required=agent),
        "hackbook": _file_pin(args.hackbook, "--hackbook", required=agent),
        "taxonomy": {"path": str(KEY_TAXONOMY), "sha256": groups["source"]["sha256"]},
    }
    if not agent:
        inputs["prompt"] = {"path": str(PLAIN_PROMPT), "sha256": sha256_file(PLAIN_PROMPT)}
    settings: dict[str, Any] = {
        "model": args.model, "seed": args.seed, "once_only": True, "retries": 0,
        "concurrency_cap": MAX_CONCURRENCY,
        "native_container_cap_per_lane": 2,
        "native_container_cap_note": "enforced by the core harness admission, not here",
    }
    if agent:
        settings |= {"lane": args.lane, "max_seconds": args.max_seconds, "max_tokens": args.max_tokens,
                     "max_tool_calls": args.max_tool_calls, "max_usd": args.max_usd}
        if args.lane == "codex":
            settings["codex_weekly_ceiling"] = args.codex_weekly_ceiling
        elif args.lane == "openai":
            settings |= {"endpoint": args.endpoint, "api_key_env": args.api_key_env,
                         "input_usd_per_million": args.input_usd_per_million,
                         "output_usd_per_million": args.output_usd_per_million}
    else:
        settings |= {"batch_size": PLAIN_BATCH, "tools": "none: file reads inside assigned directories",
                     "isolation": "instruction-confined subagent directories; weaker than the sealed "
                                  "EnvTools view", "runner": "native OMP subagents launched by the parent"}
    config_digest = sha256_bytes(canonical({
        "condition": args.condition, "tree": tree_digest(manifest), "settings": settings,
        "inputs": {k: v for k, v in inputs.items() if k not in {"view", "view_bundle"}},
        "view_bundle": inputs["view_bundle"]["sha256"],
    }))
    system = (f"qa-env-audit-v2@{commit[:12]}+{config_digest[7:19]}" if agent
              else f"qa-plain-read@{commit[:12]}+{config_digest[7:19]}")
    created = now()
    registry = registry_exposure(args.registry_agent, system, created)
    freeze = {
        "schema": FREEZE_SCHEMA, "condition": args.condition, "split": args.split,
        "created_at": created, "system_id": system, "config_digest": config_digest,
        "git": {"repo_root": str(REPO), "commit": commit, "origin_main": origin,
                "merged_into_origin_main": merged},
        "source": {"pathspecs": list(SOURCE_PATHSPECS), "files": len(manifest),
                   "tree_digest": tree_digest(manifest), "manifest_path": str(campaign / "source-manifest.json"),
                   "tree_digest_ordering": "sorted by relative .as_posix() string"},
        "runner": runner, "settings": settings, "inputs": inputs, "registry": registry,
        "direction_groups": groups,
        "exposure_ledger": {"root": str(exposures_ledger), "key": f"{system}/{args.split}/<task>.json"},
        "test_authorization_required": args.split == "test",
        "view_inputs_parsed_before_freeze": False,
    }
    campaign.mkdir(parents=True)
    write_new(campaign / "source-manifest.json", manifest)
    write_new(campaign / "freeze.json", freeze)
    print(json.dumps({"campaign_dir": str(campaign), "system_id": system, "commit": commit,
                      "freeze_sha256": sha256_file(campaign / "freeze.json"),
                      "registry_excluded": len(registry["excluded_names"]),
                      "registry_disclosed": len(registry["disclosed_names"])}, indent=2))
    return 0


def load_freeze(campaign: Path) -> dict[str, Any]:
    freeze = read_json(campaign / "freeze.json")
    if freeze.get("schema") != FREEZE_SCHEMA:
        raise ValueError("not a TQ-1 freeze")
    return freeze


def freeze_digest(campaign: Path) -> str:
    return sha256_file(campaign / "freeze.json")


def verify_frozen(freeze: dict[str, Any]) -> list[str]:
    bad: list[str] = []
    if freeze["git"]["repo_root"] != str(REPO):
        bad.append(f"repository root {REPO} is not the frozen {freeze['git']['repo_root']}")
    commit = git("rev-parse", "HEAD")
    if commit != freeze["git"]["commit"]:
        bad.append(f"HEAD {commit} is not the frozen commit {freeze['git']['commit']}")
    dirty = source_dirty()
    if dirty:
        bad.append("frozen source has uncommitted or untracked changes: " + dirty.replace("\n", "; "))
    manifest = read_json(Path(freeze["source"]["manifest_path"]))
    if tree_digest(manifest) != freeze["source"]["tree_digest"]:
        bad.append("source manifest does not reproduce the frozen tree digest")
    for rel, digest in manifest.items():
        path = REPO / rel
        if not path.is_file() or sha256_file(path) != digest:
            bad.append(f"frozen file changed: {rel}")
    for name, pin in freeze["inputs"].items():
        if isinstance(pin, dict) and "path" in pin and "sha256" in pin:
            path = Path(pin["path"])
            if not path.is_file() or sha256_file(path) != pin["sha256"]:
                bad.append(f"frozen input changed: {name}")
    python = Path(freeze["runner"]["python"])
    if not python.is_file():
        bad.append("frozen interpreter is missing")
    elif (str(python.resolve()) != freeze["runner"]["python_executable"]
          or sha256_file(python) != freeze["runner"]["python_sha256"]):
        bad.append("frozen interpreter executable changed")
    if freeze["settings"].get("lane") == "omp":
        omp = shutil.which("omp")
        if (not omp or str(Path(omp).resolve()) != freeze["runner"]["omp_executable"]
                or sha256_file(Path(omp)) != freeze["runner"]["omp_sha256"]):
            bad.append("frozen OMP executable changed")
    return bad


def cmd_verify(args: argparse.Namespace) -> int:
    freeze = load_freeze(owned_path(args.campaign_dir, must_exist=True))
    bad = verify_frozen(freeze)
    print(json.dumps({"ok": not bad, "violations": bad, "system_id": freeze["system_id"]}, indent=2))
    return 0 if not bad else 1


def cmd_authorize_test(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, must_exist=True)
    freeze = load_freeze(campaign)
    if freeze["split"] != "test":
        raise ValueError("only a TEST freeze is authorized this way")
    if (campaign / "runs").exists() or (campaign / "plain").exists():
        raise ValueError("authorization must precede every run")
    bad = verify_frozen(freeze)
    if bad:
        raise ValueError("freeze no longer verifies: " + "; ".join(bad))
    write_new(campaign / "authorization.json", {
        "schema": "tb4.qa.v2.test_authorization/v1", "freeze_sha256": freeze_digest(campaign),
        "system_id": freeze["system_id"], "authorized_by": args.by, "reference": args.reference,
        "at": now(),
    })
    return 0


# ----------------------------------------------------------------------------- public records


def public_records(freeze: dict[str, Any]) -> list[dict[str, Any]]:
    """Validated public view records. The first parse of the view inputs happens here, after freeze."""
    provider = load_module(VIEWS_MODULE, "_tq1_views")
    view = Path(freeze["inputs"]["view"]["root"])
    capability = provider.agent_tools(view)
    records = read_jsonl(capability._path("inputs.jsonl"))
    for record in records:
        if record.get("split") != freeze["split"] or record.get("key_id") != "tb4-audit-v1":
            raise ValueError("a public record disagrees with the frozen split/key")
    return sorted(records, key=lambda r: r["package"]["name"])


def task_order(freeze: dict[str, Any], names: list[str]) -> list[str]:
    seed = freeze["settings"]["seed"]
    return sorted(names, key=lambda n: (hashlib.sha256(f"{seed}:{freeze['system_id']}:{n}".encode())
                                        .hexdigest(), n))


def bind_task(freeze: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Bind the original upstream bytes to one public record; raise on any mismatch."""
    from evallab.registry import _should_ignore_file, task_directory_digest

    name = record["package"]["name"]
    upstream = freeze["inputs"]["upstream"]
    source = Path(upstream["root"]) / name
    if source.is_symlink() or not source.is_dir() or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError(f"upstream task is missing or contains symlinks: {name}")
    digest = task_directory_digest(source)
    if digest != record["subject"]["task_digest"]:
        raise ValueError(f"upstream bytes do not match the public record digest: {name}")
    files = {}
    for path in sorted(source.rglob("*"), key=lambda p: p.relative_to(source).as_posix()):
        if path.is_file() and not _should_ignore_file(path):
            data = path.read_bytes()
            files[path.relative_to(source).as_posix()] = {"sha256": sha256_bytes(data), "bytes": len(data)}
    view_files = {}
    view_task = Path(freeze["inputs"]["view"]["root"]) / "tasks" / name
    for path in sorted(view_task.rglob("*"), key=lambda p: p.relative_to(view_task).as_posix()):
        if path.is_file():
            rel = path.relative_to(view_task).as_posix()
            if rel not in files or sha256_bytes(path.read_bytes()) != files[rel]["sha256"]:
                raise ValueError(f"view file differs from the upstream source: {name}/{rel}")
            view_files[rel] = files[rel]["sha256"]
    return {
        "schema": "tb4.qa.v2.source_binding/v1", "task": name,
        "task_id": record["subject"]["task_id"], "task_digest": digest, "public_record": record,
        "upstream_root": upstream["root"], "declared_pin": upstream["declared_pin"],
        "observed_git_head": upstream["observed_git_head"], "source_files": files,
        "view_files": view_files, "view_is_byte_subset_of_source": True, "bound_at": now(),
    }


def recheck_binding(binding: dict[str, Any]) -> list[str]:
    """Source bytes still equal what was bound when the task was exposed."""
    from evallab.registry import task_directory_digest

    source = Path(binding["upstream_root"]) / binding["task"]
    bad = []
    try:
        if task_directory_digest(source) != binding["task_digest"]:
            bad.append("upstream task digest changed after the run")
    except (OSError, ValueError) as exc:
        return [f"upstream task unreadable: {exc}"]
    for rel, meta in binding["source_files"].items():
        path = source / rel
        if not path.is_file() or sha256_file(path) != meta["sha256"]:
            bad.append(f"upstream file changed: {rel}")
    return bad


# -------------------------------------------------------------------------------- exposure marks


def claim_exposure(freeze: dict[str, Any], task: str, campaign: Path) -> dict[str, Any] | None:
    """Exclusive once-only marker; returns ``None`` when this system already exposed the task."""
    marker = Path(freeze["exposure_ledger"]["root"]) / freeze["system_id"] / freeze["split"] / f"{task}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": "tb4.qa.v2.exposure_marker/v1", "system_id": freeze["system_id"],
            "split": freeze["split"], "task": task, "campaign_dir": str(campaign),
            "freeze_sha256": freeze_digest(campaign), "claimed_at": now(), "pid": os.getpid()}
    try:
        write_new(marker, body)
    except FileExistsError:
        return None
    return body


def audit_argv(freeze: dict[str, Any], task_dir: Path, out: Path) -> list[str]:
    s, inputs = freeze["settings"], freeze["inputs"]

    def num(value: float | int) -> str:
        return str(int(value)) if float(value).is_integer() else repr(float(value))

    argv = [
        freeze["runner"]["python"], "-c", ENTRY, "qa", "env", "audit",
        "--lane", s["lane"], "--model", s["model"], "--task", str(task_dir),
        "--view", inputs["view"]["root"], "--out", str(out),
        "--max-seconds", num(s["max_seconds"]), "--max-tokens", str(s["max_tokens"]),
        "--max-tool-calls", str(s["max_tool_calls"]), "--max-usd", num(s["max_usd"]),
        "--execution-config", inputs["execution_config"]["path"],
        "--hackbook", inputs["hackbook"]["path"],
    ]
    if s["lane"] == "codex":
        argv += ["--codex-weekly-ceiling", num(s["codex_weekly_ceiling"])]
    elif s["lane"] == "openai":
        argv += [
            "--endpoint", s["endpoint"], "--api-key-env", s["api_key_env"],
            "--input-usd-per-million", num(s["input_usd_per_million"]),
            "--output-usd-per-million", num(s["output_usd_per_million"]),
        ]
    return argv


# ------------------------------------------------------------------------------------------ run


def _plan(freeze: dict[str, Any], campaign: Path) -> dict[str, Any]:
    path = campaign / "plan.json"
    if path.exists():
        plan = read_json(path)
        if plan["freeze_sha256"] != freeze_digest(campaign):
            raise ValueError("plan.json belongs to another freeze")
        return plan
    records = public_records(freeze)
    excluded = freeze["registry"]["excluded_names"]
    disclosed = freeze["registry"]["disclosed_names"]
    names = [r["package"]["name"] for r in records]
    by_name = {r["package"]["name"]: r for r in records}
    tasks = []
    for name in task_order(freeze, names):
        record = by_name[name]
        tasks.append({
            "task": name, "task_id": record["subject"]["task_id"],
            "task_digest": record["subject"]["task_digest"],
            "registry": ("excluded" if name in excluded else "disclosed" if name in disclosed else "clear"),
            "registry_exposures": excluded.get(name) or disclosed.get(name) or [],
        })
    plan = {"schema": "tb4.qa.v2.plan/v1", "freeze_sha256": freeze_digest(campaign),
            "system_id": freeze["system_id"], "created_at": now(), "tasks": tasks}
    write_new(path, plan)
    return plan


def _run_one(freeze: dict[str, Any], campaign: Path, entry: dict[str, Any], abort: threading.Event,
             records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    name = entry["task"]
    result: dict[str, Any] = {"task": name, "registry": entry["registry"]}
    run_dir = campaign / "runs" / name
    if abort.is_set():
        return result | {"status": "not_started_campaign_aborted"}
    if entry["registry"] == "excluded":
        return result | {"status": "excluded_registry_exposure"}
    bad = verify_frozen(freeze)
    if bad:
        abort.set()
        run_dir.mkdir(parents=True, exist_ok=True)
        write_new(run_dir / "drift.json", {"at": now(), "violations": bad})
        return result | {"status": "not_started_frozen_drift", "violations": bad}
    try:
        binding = bind_task(freeze, records[name])
    except (OSError, ValueError) as exc:
        run_dir.mkdir(parents=True, exist_ok=True)
        write_new(run_dir / "binding-failure.json", {"at": now(), "error": str(exc)})
        return result | {"status": "not_started_binding_failed", "error": str(exc)}
    if claim_exposure(freeze, name, campaign) is None:
        return result | {"status": "skipped_already_exposed_by_this_system"}
    write_new(run_dir / "source-binding.json", binding)
    out = run_dir / "session"
    argv = audit_argv(freeze, Path(binding["upstream_root"]) / name, out)
    started_at, started = now(), time.monotonic()
    proc = None
    try:
        stdout = (run_dir / "core-stdout.json").open("xb")
        stderr = (run_dir / "core-stderr.log").open("xb")
        proc = subprocess.Popen(argv, cwd=REPO, env=child_env(), stdout=stdout, stderr=stderr,
                                stdin=subprocess.DEVNULL)
        write_new(run_dir / "controller-start.json", {
            "task": name, "argv": argv, "pid": proc.pid, "started_at": started_at,
            "freeze_sha256": freeze_digest(campaign), "frozen_verified": True,
            "system_id": freeze["system_id"],
        })
        hard_timeout = float(freeze["settings"]["max_seconds"]) + 300
        status, returncode = "done", None
        try:
            returncode = proc.wait(timeout=hard_timeout)
            if returncode != 0:
                status = "nonzero_exit"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            status, returncode = "controller_timeout_killed", proc.returncode
        finally:
            stdout.close()
            stderr.close()
    except OSError as exc:
        status, returncode = "launch_error", None
        (run_dir).mkdir(parents=True, exist_ok=True)
        result["error"] = f"{type(exc).__name__}: {exc}"
        if not (run_dir / "controller-start.json").exists():
            write_new(run_dir / "controller-start.json", {
                "task": name, "argv": argv, "pid": None, "started_at": started_at,
                "freeze_sha256": freeze_digest(campaign), "frozen_verified": True,
                "system_id": freeze["system_id"]})
    write_new(run_dir / "controller-end.json", {
        "task": name, "status": status, "returncode": returncode, "ended_at": now(),
        "wall_seconds": round(time.monotonic() - started, 3),
        "core_stdout_sha256": sha256_file(run_dir / "core-stdout.json")
        if (run_dir / "core-stdout.json").exists() else None,
        "no_retry": "a launched task is never relaunched, whatever its outcome",
    })
    return result | {"status": status, "returncode": returncode}


def cmd_run(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, must_exist=True)
    freeze = load_freeze(campaign)
    if freeze["condition"] != "agent_v2":
        raise ValueError("run is the agent_v2 entry; plain_read uses plain-prepare")
    if (campaign / "scoring-started.json").exists():
        raise ValueError("scoring has started: the campaign can never restart")
    if (campaign / "closed.json").exists():
        raise ValueError("campaign is closed")
    if freeze["split"] == "test":
        auth_path = campaign / "authorization.json"
        if not auth_path.is_file() or read_json(auth_path)["freeze_sha256"] != freeze_digest(campaign):
            raise ValueError("TEST needs authorize-test bound to this exact freeze")
    bad = verify_frozen(freeze)
    if bad:
        raise ValueError("frozen state drifted before the first task: " + "; ".join(bad))
    concurrency = args.concurrency
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"--concurrency must be 1..{MAX_CONCURRENCY}")
    plan = _plan(freeze, campaign)
    records = {r["package"]["name"]: r for r in public_records(freeze)}
    abort = threading.Event()
    statuses: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_run_one, freeze, campaign, entry, abort, records)
                   for entry in plan["tasks"]]
        for future in futures:
            outcome = future.result()
            statuses[outcome["task"]] = outcome
            print(f"{outcome['task']}: {outcome['status']}", file=sys.stderr)
    return _close(campaign, freeze, plan, statuses)


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close(campaign: Path, freeze: dict[str, Any], plan: dict[str, Any],
           statuses: dict[str, dict[str, Any]]) -> int:
    tasks = {}
    for entry in plan["tasks"]:
        name = entry["task"]
        run_dir = campaign / "runs" / name
        state = statuses.get(name, {}).get("status")
        if state is None:
            if (run_dir / "controller-end.json").exists():
                state = read_json(run_dir / "controller-end.json")["status"]
            elif (run_dir / "controller-start.json").exists():
                pid = read_json(run_dir / "controller-start.json").get("pid")
                if _pid_alive(pid):
                    raise ValueError(f"{name} is still running (pid {pid}); cannot close")
                state = "interrupted_unfinished_unknown"
            elif entry["registry"] == "excluded":
                state = "excluded_registry_exposure"
            else:
                state = "never_started"
        tasks[name] = {"status": state, "registry": entry["registry"],
                       "exposed": (run_dir / "controller-start.json").exists()}
    exposed = sorted(n for n, t in tasks.items() if t["exposed"])
    write_new(campaign / "closed.json", {
        "schema": "tb4.qa.v2.closed/v1", "closed_at": now(), "freeze_sha256": freeze_digest(campaign),
        "system_id": freeze["system_id"], "tasks": tasks,
        "status_counts": dict(sorted(Counter(t["status"] for t in tasks.values()).items())),
    })
    if freeze["split"] == "test" and exposed:
        stamp = datetime.now(UTC)
        draft = {
            "exposure_id": f"tb4-qa-v2-{campaign.name}-test-exposed",
            "date": stamp.date().isoformat(), "system": freeze["system_id"], "kind": "inspected",
            "applies_to": [{"agent": freeze["registry"]["agent"],
                            "from": (stamp + timedelta(days=1)).date().isoformat()}],
            "tasks": exposed,
            "evidence": f"{campaign}/closed.json",
            "note": "DRAFT for the parent to review and append to splits/exposure.jsonl after scoring: "
                    "later systems of this agent saw these tasks' outputs. `from` is the day after "
                    "this run so this system's own row is not flagged by its own record.",
        }
        write_new(campaign / "exposure-record-draft.json", draft)
    print(json.dumps({"closed": str(campaign / "closed.json"), "exposed_tasks": len(exposed)}))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, must_exist=True)
    freeze = load_freeze(campaign)
    if (campaign / "closed.json").exists():
        raise ValueError("campaign is already closed")
    return _close(campaign, freeze, read_json(campaign / "plan.json"), {})


# --------------------------------------------------------------------------------- custody check


def _load(path: Path, problems: list[str], label: str) -> Any:
    if not path.is_file():
        problems.append(f"missing {label}")
        return None
    try:
        return read_json(path)
    except ValueError as exc:
        problems.append(f"unreadable {label}: {exc}")
        return None


def _model_core(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _actual_models(value: Any, depth: int = 0) -> set[str]:
    """Model ids a lane reported under any ``actual_model`` key.

    A value with whitespace is the lane saying it did not report one (for example Codex); it is
    preserved in the raw session record but never compared with the frozen model.
    """
    found: set[str] = set()
    if depth > 6:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "actual_model" and isinstance(item, str) and item.strip() and not any(c.isspace() for c in item):
                found.add(item)
            else:
                found |= _actual_models(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            found |= _actual_models(item, depth + 1)
    return found


def validate_run(freeze: dict[str, Any], campaign: Path, task: str) -> dict[str, Any]:
    """Custody check of one exposed task: journal receipts, subject/digest, source bytes.

    Findings are collected only from ``record-finding`` journal receipts whose finding file hashes to
    its name, matches the receipt and names the bound task and digest. Anything else is quarantined.
    """
    run_dir = campaign / "runs" / task
    problems: list[str] = []
    report: dict[str, Any] = {"task": task, "ok": False, "problems": problems, "findings": [],
                              "quarantined_files": []}
    start = _load(run_dir / "controller-start.json", problems, "controller-start")
    end = _load(run_dir / "controller-end.json", problems, "controller-end")
    binding = _load(run_dir / "source-binding.json", problems, "source-binding")
    session = _load(run_dir / "session" / "agent" / "session.json", problems, "session record")
    if start and start.get("freeze_sha256") != freeze_digest(campaign):
        problems.append("controller start belongs to another freeze")
    if start and start.get("frozen_verified") is not True:
        problems.append("frozen state was not verified before launch")
    if binding is None:
        return report
    problems.extend(recheck_binding(binding))
    report["task_id"], report["task_digest"] = binding["task_id"], binding["task_digest"]
    journal_path = run_dir / "session" / "agent" / "journal.jsonl"
    journal: list[dict[str, Any]] = []
    if journal_path.is_file():
        try:
            journal = read_jsonl(journal_path)
        except ValueError as exc:
            problems.append(f"unreadable journal: {exc}")
    else:
        problems.append("missing journal")
    seqs = [e.get("seq") for e in journal]
    if any(not isinstance(s, int) for s in seqs) or seqs != sorted(set(seqs)):
        problems.append("journal sequence is not strictly increasing")
    report["journal_sha256"] = sha256_file(journal_path) if journal_path.is_file() else None
    report["journal_events"] = len(journal)

    findings_dir = run_dir / "session" / "findings"
    receipts: dict[str, dict[str, Any]] = {}
    native: dict[str, Counter] = {}
    calls: Counter = Counter()
    for event in journal:
        if event.get("event") != "tool_call":
            continue
        tool, observation = event.get("tool"), event.get("observation") or {}
        calls[tool] += 1
        if observation.get("task_id") != binding["task_id"] or \
                observation.get("task_digest") != binding["task_digest"]:
            problems.append(f"journal seq {event.get('seq')}: observation names another task identity")
        result = observation.get("result")
        if isinstance(result, dict) and "execution_status" in result:
            native.setdefault(str(tool), Counter())[str(result["execution_status"])] += 1
        if tool == "record-finding" and isinstance(result, dict) and isinstance(result.get("finding"), dict):
            payload = result["finding"]
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            file = findings_dir / f"{digest}.json"
            if Path(str(result.get("path"))).name != file.name:
                problems.append(f"journal seq {event.get('seq')}: receipt path is not the content hash")
            if not file.is_file() or read_json(file) != payload:
                problems.append(f"journal seq {event.get('seq')}: finding file missing or different")
                continue
            subject = payload.get("subject") or {}
            if subject.get("task_id") != binding["task_id"] or subject.get("task_digest") != binding["task_digest"]:
                problems.append(f"finding {digest[:12]}: subject/digest differs from the bound task")
                continue
            witnessed = False
            for ref in payload.get("evidence_refs") or []:
                if isinstance(ref, str) and ref.startswith("witness:"):
                    obs = run_dir / "session" / "observations" / f"{ref.removeprefix('witness:')}.json"
                    receipt = read_json(obs) if obs.is_file() else None
                    if (not receipt or receipt.get("task_digest") != binding["task_digest"]
                            or receipt.get("execution_status") != "measured"):
                        problems.append(f"finding {digest[:12]}: witness {ref} has no measured receipt")
                    else:
                        witnessed = True
            receipts[digest] = {"row": payload, "sha256": "sha256:" + digest, "file": str(file),
                                "journal_seq": event.get("seq"),
                                "evidence_basis": sorted(result.get("evidence_basis") or []),
                                "self_executed_witness": witnessed}
    if findings_dir.is_dir():
        for file in sorted(findings_dir.glob("*")):
            if file.stem not in receipts:
                report["quarantined_files"].append(str(file))
        if report["quarantined_files"]:
            problems.append(f"{len(report['quarantined_files'])} finding file(s) have no journal receipt")
    if session is not None:
        if not str(session.get("schema", "")).startswith(SESSION_SCHEMA_PREFIX):
            problems.append("session record has an unexpected schema")
        recorded = {r.get("journal_seq") for r in session.get("recorded_findings") or []}
        if recorded != {r["journal_seq"] for r in receipts.values()}:
            problems.append("session recorded_findings disagree with the journal receipts")
        identity = session.get("task_identity")
        if identity and (identity.get("task_id") != binding["task_id"]
                         or identity.get("task_digest") != binding["task_digest"]):
            problems.append("session task identity differs from the bound task")
        declared = freeze["settings"]["model"]
        observed = sorted(_actual_models(session) | {m for e in journal if e.get("event") == "model_turn"
                                                      for m in _actual_models(e)})
        report["declared_model"], report["actual_model_observed"] = declared, observed or None
        if observed and any(_model_core(v) != _model_core(declared) for v in observed):
            problems.append(f"observed model {observed} differs from the frozen {declared}")
        used, spend, quota = session.get("used") or {}, session.get("spend") or {}, session.get("quota") or {}
        readings = [r.get("weekly_used_fraction") for r in quota.get("readings") or []]
        metered = spend.get("metered_usd") if spend.get("known", True) is not False else None
        report |= {
            "status": session.get("status"), "stop_reason": session.get("stop_reason"),
            "session_seconds": used.get("seconds"),
            "tokens": used.get("tokens") if used.get("tokens_known", True) else None,
            "tool_calls_used": used.get("tool_calls"), "budgets": session.get("budgets"),
            "usage": session.get("usage"), "spend": spend or None,
            "metered_usd": metered if isinstance(metered, int | float) and not isinstance(metered, bool) else None,
            "quota_weekly_first": readings[0] if readings else None,
            "quota_weekly_last": readings[-1] if readings else None,
            "transport": session.get("transport"), "errors": len(session.get("errors") or []),
            "final_present": session.get("final") is not None,
        }
    report |= {
        "controller_status": (end or {}).get("status"),
        "controller_wall_seconds": (end or {}).get("wall_seconds"),
        "tool_calls_by_tool": dict(calls),
        "native_execution": {t: dict(c) for t, c in sorted(native.items())},
        "native_measured": sum(c.get("measured", 0) for c in native.values()),
        "native_not_measured": sum(v for c in native.values() for k, v in c.items() if k != "measured"),
        "findings": sorted(receipts.values(), key=lambda r: r["journal_seq"] or 0),
    }
    report["ok"] = not problems
    return report


# ------------------------------------------------------------------------------------ plain read


def _taxonomy_markdown() -> str:
    taxonomy = read_json(KEY_TAXONOMY)
    out = ["# Task-side defect classes", ""]
    for row in taxonomy["task_side"]:
        out += [f"## `{row['key']}`: {row['name']}", "", f"- direction: {row['direction']}",
                f"- definition: {row['definition']}", f"- not this: {row['not_this']}", ""]
    out += ["## Rules", ""] + [f"- {rule}" for rule in taxonomy["label_rules"]]
    return "\n".join(out) + "\n"


def cmd_plain_prepare(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, must_exist=True)
    freeze = load_freeze(campaign)
    if freeze["condition"] != "plain_read":
        raise ValueError("plain-prepare needs a plain_read freeze")
    if (campaign / "plain").exists() or (campaign / "scoring-started.json").exists():
        raise ValueError("the plain-read packets already exist or scoring started")
    if freeze["split"] == "test":
        auth = campaign / "authorization.json"
        if not auth.is_file() or read_json(auth)["freeze_sha256"] != freeze_digest(campaign):
            raise ValueError("TEST needs authorize-test bound to this exact freeze")
    bad = verify_frozen(freeze)
    if bad:
        raise ValueError("frozen state drifted: " + "; ".join(bad))
    plan = _plan(freeze, campaign)
    records = {r["package"]["name"]: r for r in public_records(freeze)}
    plain = campaign / "plain"
    inputs, outputs = plain / "inputs", plain / "outputs"
    outputs.mkdir(parents=True)
    inputs.mkdir()
    (inputs / "taxonomy.md").write_text(_taxonomy_markdown(), encoding="utf-8")
    manifest: dict[str, Any] = {}
    seed = freeze["settings"]["seed"]
    for entry in plan["tasks"]:
        name = entry["task"]
        if entry["registry"] == "excluded":
            manifest[name] = {"status": "excluded_registry_exposure"}
            continue
        try:
            binding = bind_task(freeze, records[name])
        except (OSError, ValueError) as exc:
            manifest[name] = {"status": "not_exposed_binding_failed", "error": str(exc)}
            continue
        if claim_exposure(freeze, name, campaign) is None:
            manifest[name] = {"status": "skipped_already_exposed_by_this_system"}
            continue
        aid = "r-" + hashlib.sha256(f"tb4-plain-read:{seed}:{freeze['system_id']}:{name}".encode()).hexdigest()[:8]
        shutil.copytree(Path(freeze["inputs"]["view"]["root"]) / "tasks" / name, inputs / aid)
        write_new(plain / "bindings" / f"{aid}.json", binding)
        manifest[name] = {"status": "packet_built", "aid": aid, "task_id": binding["task_id"],
                          "task_digest": binding["task_digest"]}
    aids = sorted(m["aid"] for m in manifest.values() if "aid" in m)
    random.Random(f"{seed}:{freeze['system_id']}").shuffle(aids)
    batches = [aids[i:i + PLAIN_BATCH] for i in range(0, len(aids), PLAIN_BATCH)]
    write_new(plain / "private-manifest.json", {
        "schema": "tb4.qa.v2.plain_manifest/v1", "freeze_sha256": freeze_digest(campaign),
        "tasks": manifest, "prepared_at": now()})
    write_new(plain / "launch.json", {
        "schema": "tb4.qa.v2.plain_launch/v1", "system_id": freeze["system_id"],
        "model": freeze["settings"]["model"], "input_root": str(inputs), "output_root": str(outputs),
        "prompt": freeze["inputs"]["prompt"], "taxonomy": str(inputs / "taxonomy.md"),
        "tools": freeze["settings"]["tools"], "isolation": freeze["settings"]["isolation"],
        "batches": [{"batch": i, "aids": b} for i, b in enumerate(batches)],
        "note": "Fresh subagent per batch; each sees only its assigned aid directories, taxonomy.md "
                "and the prompt. Not launched by this entry.",
    })
    print(json.dumps({"packets": len(aids), "batches": len(batches), "launch": str(plain / "launch.json")}))
    return 0


def cmd_plain_collect(args: argparse.Namespace) -> int:
    campaign = owned_path(args.campaign_dir, must_exist=True)
    freeze = load_freeze(campaign)
    if freeze["condition"] != "plain_read":
        raise ValueError("plain-collect needs a plain_read freeze")
    plain = campaign / "plain"
    manifest = read_json(plain / "private-manifest.json")
    rows: list[dict[str, Any]] = []
    tally: dict[str, Any] = {"files": 0, "missing": [], "invalid": [], "problems": [], "outputs": {}}
    taxonomy_classes = set(CATEGORY)
    for name, meta in sorted(manifest["tasks"].items()):
        if "aid" not in meta:
            continue
        aid = meta["aid"]
        problems = recheck_binding(read_json(plain / "bindings" / f"{aid}.json"))
        if problems:
            tally["problems"].append({"task": name, "problems": problems})
            continue
        path = plain / "outputs" / f"{aid}.json"
        if not path.is_file():
            tally["missing"].append(name)
            continue
        try:
            data = read_json(path)
            findings = data["findings"]
            if data.get("aid") != aid or data["task_verdict"] not in {"defects_found", "no_defect_found", "cannot_tell"} \
                    or len(findings) > 8 or (data["task_verdict"] == "no_defect_found" and findings):
                raise ValueError("output violates the frozen output schema")
        except (ValueError, KeyError, TypeError) as exc:
            tally["invalid"].append({"task": name, "error": str(exc)})
            continue
        tally["files"] += 1
        tally["outputs"][name] = sha256_file(path)
        for rank, finding in enumerate(findings):
            cls = finding.get("class")
            if cls not in taxonomy_classes:
                tally["invalid"].append({"task": name, "finding": rank, "error": f"unknown class {cls!r}"})
                continue
            text = "\n".join(str(x) for x in [finding.get("title"), finding.get("summary"),
                                              finding.get("instruction_quote"),
                                              finding.get("grader_quote")] if x)
            rows.append({
                "schema": "qa.finding/v1", "id": None, "verdict": "fail",
                "subject": {"kind": "task", "task_id": meta["task_id"], "task_digest": meta["task_digest"]},
                "category": CATEGORY[cls], "root_cause": [cls], "taxonomy_class": cls,
                "evidence_refs": finding.get("evidence_refs") or [], "summary": text,
                "reviewer": {"agent": "qa-plain-read", "model": freeze["settings"]["model"], "cost_usd": None},
                "confidence": finding.get("confidence"), "rank": rank, "aid": aid,
            })
    out = plain / "collected"
    out.mkdir()
    (out / "findings.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    write_new(out / "collected.json", {
        "schema": "tb4.qa.v2.plain_collected/v1", "freeze_sha256": freeze_digest(campaign),
        "findings": len(rows), "findings_sha256": sha256_file(out / "findings.jsonl"), **tally,
        "usage": None, "usage_note": "subagent token/cost usage is not captured by this entry: null, not zero"})
    write_new(campaign / "closed.json", {
        "schema": "tb4.qa.v2.closed/v1", "closed_at": now(), "freeze_sha256": freeze_digest(campaign),
        "system_id": freeze["system_id"],
        "tasks": {n: {"status": m["status"], "exposed": "aid" in m} for n, m in manifest["tasks"].items()},
        "status_counts": dict(sorted(Counter(m["status"] for m in manifest["tasks"].values()).items()))})
    print(json.dumps({"findings": len(rows), "files": tally["files"], "missing": len(tally["missing"]),
                      "invalid": len(tally["invalid"])}))
    return 0


# ------------------------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze", help="pin everything before any view input is parsed")
    freeze.add_argument("--campaign-dir", required=True)
    freeze.add_argument("--condition", choices=CONDITIONS, required=True)
    freeze.add_argument("--split", choices=SPLITS, required=True)
    freeze.add_argument("--model", default="anthropic/claude-sonnet-5-5")
    freeze.add_argument("--lane", choices=LANES)
    freeze.add_argument("--max-seconds", type=float, default=900)
    freeze.add_argument("--max-tokens", type=int)
    freeze.add_argument("--max-tool-calls", type=int, default=60)
    freeze.add_argument("--max-usd", type=float, default=0)
    freeze.add_argument("--endpoint")
    freeze.add_argument("--api-key-env")
    freeze.add_argument("--input-usd-per-million", type=float)
    freeze.add_argument("--output-usd-per-million", type=float)
    freeze.add_argument("--codex-weekly-ceiling", type=float, default=0.35)
    freeze.add_argument("--execution-config")
    freeze.add_argument("--hackbook")
    freeze.add_argument("--upstream-root", required=True)
    freeze.add_argument("--source-pin", required=True, help="declared 40-hex upstream commit")
    freeze.add_argument("--view", required=True, help="exported label-free agent view root")
    freeze.add_argument("--pm-port-manifest")
    freeze.add_argument("--registry-agent", default="QA Task")
    freeze.add_argument("--ledger-root")
    freeze.add_argument("--seed", type=int, default=DEFAULT_SEED)
    freeze.add_argument("--python")
    freeze.add_argument("--allow-unmerged", action="store_true")
    freeze.set_defaults(func=cmd_freeze)
    for name, func, help_text in (
        ("verify", cmd_verify, "read-only drift check against the freeze"),
        ("run", cmd_run, "run every task once through the core entry"),
        ("close", cmd_close, "close a crashed campaign without relaunching anything"),
        ("plain-prepare", cmd_plain_prepare, "build once-only plain-read packets (does not launch)"),
        ("plain-collect", cmd_plain_collect, "validate and convert plain-read outputs"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--campaign-dir", required=True)
        if name == "run":
            p.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
        p.set_defaults(func=func)
    auth = sub.add_parser("authorize-test", help="record the explicit TEST authorization")
    auth.add_argument("--campaign-dir", required=True)
    auth.add_argument("--by", required=True)
    auth.add_argument("--reference", required=True)
    auth.set_defaults(func=cmd_authorize_test)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
