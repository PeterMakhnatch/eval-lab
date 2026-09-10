"""Post-job Harbor traces export via the Python API (per-trial isolation).

Run after each future job (from eval-lab root)::

  uvx --from 'harbor[huggingface]==0.21.0' python scripts/export_harbor_traces.py runs/<job> [...]

Each job lands in ``derived/harbor-traces/<job>/`` as a HF dataset
(``conversations`` + ``conversations_sharegpt`` + instruction/verifier
metadata columns) plus a merged entry in ``export-summary.json``.

Why per-trial calls instead of one root-level ``export_traces`` call:
upstream ``traces_utils.export_traces`` (0.21.0, ``traces_utils.py:1241``)
resolves every trial's agent name through the ``AgentName`` enum with no
guard, so one trial naming a custom agent class
(``evallab.harbor_codex:PinnedCodex``) aborts the whole export with
``ValueError: ... is not a valid AgentName`` -- even though that trial's
``trajectory.json`` is valid ATIF. Non-ATIF agents (e.g. ``oracle``) abort
the same loop with ``NotImplementedError``. Calling ``export_traces`` once
per trial dir (``iter_trial_dirs`` yields a root that is itself a trial
dir) isolates those failures. For the custom-agent case we additionally
retry from a *shadow* trial dir (symlinks + ``result.json`` rewritten with
the trajectory's own ``agent.name``), so the trial still exports; the
rename happens entirely at this call boundary and the installed harbor
package is NOT patched. See the upstream issue draft filed alongside the
harbor corpus knowledge base (``UPSTREAM-ISSUE-agentname-crash.md``).

Oracle-only jobs have no ``agent/trajectory.json``; every trial is skipped
with ``NotImplementedError`` and no dataset dir is written. That is the
correct outcome, not a failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _trial_agent_label(trial_dir: Path) -> str:
    """Best-effort agent label for skip records (stdlib only)."""
    try:
        raw = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return "unknown-agent"
    if not isinstance(raw, dict):
        return "unknown-agent"
    config = raw.get("config")
    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent")
    agent_cfg = agent_cfg if isinstance(agent_cfg, dict) else {}
    agent_info = raw.get("agent_info")
    agent_info = agent_info if isinstance(agent_info, dict) else {}
    label = (
        agent_cfg.get("name")
        or agent_info.get("name")
        or config.get("agent_name")
        or raw.get("agent_name")
        or "unknown-agent"
    )
    return str(label)


def _trial_rewards(trial_dir: Path) -> object:
    try:
        raw = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    verifier_result = raw.get("verifier_result")
    if isinstance(verifier_result, dict):
        return verifier_result.get("rewards")
    return None


def _shadow_trial_dir(trial_dir: Path, staging: Path) -> Path:
    """Mirror a trial into ``staging`` with the registry agent name restored.

    Upstream reads the agent name from ``result.json`` (``config.agent.name``),
    which names our custom wrapper class (e.g. ``evallab.harbor_codex:PinnedCodex``)
    and is not a registry ``AgentName``. The ATIF trajectory itself carries the
    real agent name (``agent.name``, e.g. ``codex``). The shadow dir symlinks
    every entry of the trial and rewrites only ``result.json`` with the
    trajectory's agent name, so the export derives from the same bytes.
    """
    traj = json.loads((trial_dir / "agent" / "trajectory.json").read_text())
    traj_agent = (traj.get("agent") or {}).get("name")
    if not traj_agent:
        raise ValueError(f"no agent.name in {trial_dir}/agent/trajectory.json")
    staging.mkdir(parents=True, exist_ok=True)
    for child in trial_dir.iterdir():
        link = staging / child.name
        if child.name == "result.json" or link.exists():
            continue
        link.symlink_to(child.resolve())
    raw = json.loads((trial_dir / "result.json").read_text())
    config = raw.get("config")
    if isinstance(config, dict):
        agent_cfg = config.get("agent")
        if isinstance(agent_cfg, dict):
            agent_cfg["name"] = traj_agent
        agents = config.get("agents")
        if isinstance(agents, list):
            for item in agents:
                if isinstance(item, dict):
                    item["name"] = traj_agent
    agent_info = raw.get("agent_info")
    if isinstance(agent_info, dict) and agent_info.get("name"):
        agent_info["name"] = traj_agent
    (staging / "result.json").write_text(json.dumps(raw, indent=4))
    return staging


def _export_call(root: Path, *, episodes: str):
    from harbor.utils.traces_utils import export_traces

    return export_traces(
        root=root,
        recursive=False,
        episodes=episodes,
        to_sharegpt=True,
        push=False,
        verbose=False,
        success_filter=None,
        export_subagents=True,
        merge_subagents=True,
        include_instruction=True,
        include_verifier_output=True,
        use_rich_progress=False,
    )


def export_job(job_dir: Path, out_root: Path, *, episodes: str) -> dict:
    from datasets import concatenate_datasets

    from harbor.utils.traces_utils import iter_trial_dirs

    trial_dirs = sorted(
        (p for p in iter_trial_dirs(job_dir, recursive=True) if p.is_dir()),
        key=lambda p: p.name,
    )
    mains = []
    exported_trials: list[str] = []
    skipped: list[dict] = []
    rewards: list[object] = []
    for trial_dir in trial_dirs:
        rewards.append(_trial_rewards(trial_dir))
        staging: Path | None = None
        try:
            ds = _export_call(trial_dir, episodes=episodes)
        except ValueError as exc:
            if "not a valid AgentName" not in str(exc):
                raise
            # Custom agent class in result.json: retry from a shadow trial
            # dir that carries the trajectory's registry agent name. Harbor
            # stays unpatched; the rename happens at this call boundary.
            staging = out_root / ".staging" / job_dir.name / trial_dir.name
            try:
                ds = _export_call(_shadow_trial_dir(trial_dir, staging), episodes=episodes)
            except Exception as retry_exc:  # noqa: BLE001 - skip is data
                skipped.append(
                    {
                        "trial": trial_dir.name,
                        "agent": _trial_agent_label(trial_dir),
                        "error": f"{type(retry_exc).__name__}: {retry_exc}",
                    }
                )
                print(
                    f"SKIP {job_dir.name}/{trial_dir.name}: "
                    f"{type(retry_exc).__name__}: {retry_exc}"
                )
                continue
        except Exception as exc:  # noqa: BLE001 - per-trial skip is data
            skipped.append(
                {
                    "trial": trial_dir.name,
                    "agent": _trial_agent_label(trial_dir),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"SKIP {job_dir.name}/{trial_dir.name}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(ds, dict):  # merge_subagents=True: not expected, but stay total
            ds = ds.get("main")
            if ds is None:
                skipped.append(
                    {
                        "trial": trial_dir.name,
                        "agent": _trial_agent_label(trial_dir),
                        "error": "empty export (no main split)",
                    }
                )
                continue
        mains.append(ds)
        exported_trials.append(trial_dir.name)
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        print(f"OK {job_dir.name}/{trial_dir.name}: rows={len(ds)}")

    entry: dict = {
        "job": job_dir.name,
        "trials": len(trial_dirs),
        "exported_trials": exported_trials,
        "skipped": skipped,
        "rewards": rewards,
    }
    if not mains:
        entry["rows"] = 0
        entry["outcome"] = "no_atif_trajectories"
        entry["saved_to"] = None
        return entry
    main = concatenate_datasets(mains) if len(mains) > 1 else mains[0]
    dest = out_root / job_dir.name
    if dest.exists():
        shutil.rmtree(dest)  # save_to_disk refuses an existing dir; re-export is idempotent
    main.save_to_disk(str(dest))
    entry["rows"] = len(main)
    entry["subagent_rows"] = {}
    entry["column_names"] = main.column_names
    entry["outcome"] = "exported"
    entry["saved_to"] = str(dest)
    return entry


def _merge_summary(out_root: Path, entry: dict) -> Path:
    summary_path = out_root / "export-summary.json"
    existing: list[dict] = []
    if summary_path.is_file():
        loaded = json.loads(summary_path.read_text())
        if isinstance(loaded, list):
            existing = [item for item in loaded if item.get("job") != entry["job"]]
    existing.append(entry)
    existing.sort(key=lambda item: str(item.get("job")))
    summary_path.write_text(json.dumps(existing, indent=2) + "\n")
    return summary_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", type=Path, nargs="+", help="job dir(s) under runs/")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("derived/harbor-traces"),
        help="traces output root (default: derived/harbor-traces)",
    )
    parser.add_argument(
        "--episodes",
        default="all",
        choices=("all", "last"),
        help="episodes per trial (default: all)",
    )
    parser.add_argument(
        "--no-write-summary",
        action="store_true",
        help="skip merging export-summary.json (verification runs)",
    )
    args = parser.parse_args(argv)

    out_root = args.out
    out_root.mkdir(parents=True, exist_ok=True)
    for job_dir in args.jobs:
        if not job_dir.is_dir():
            print(f"SKIP {job_dir}: not a directory", file=sys.stderr)
            continue
        entry = export_job(job_dir, out_root, episodes=args.episodes)
        if not args.no_write_summary:
            summary_path = _merge_summary(out_root, entry)
            print(f"job {entry['job']}: rows={entry['rows']} "
                  f"exported={len(entry['exported_trials'])}/{entry['trials']} "
                  f"-> {entry['saved_to']} (summary: {summary_path})")
        else:
            print(f"job {entry['job']}: rows={entry['rows']} "
                  f"exported={len(entry['exported_trials'])}/{entry['trials']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
