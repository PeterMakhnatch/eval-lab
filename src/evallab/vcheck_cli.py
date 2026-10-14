"""VerifierCheck (``evallab vcheck``) command handlers and parser.

Red/blue verifier-audit architecture: per-task red agents read the grader and
construct wrong submissions that the task's own verifier scores, while the
harness owns the controls (baseline / honest-legit / broken-negative), frozen
requirement maps, a blind adjudicator, hash-chained trajectories, and
envcheck-findings-compatible export.

This module stays importable offline: sibling slices (CLIENT/CORE/RED/JUDGE/
STORE) are imported lazily inside the handlers that need them, so plan, status,
and unknown-id rejection paths never touch model, Docker, or DuckDB code.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_PLAN = "evallab.vcheck.plan/v1"
SCHEMA_REPORT = "evallab.vcheck.report/v1"

# Model defaults mirror CORE.MODEL_DEFAULTS; prices mirror CLIENT's table so the
# cost estimate works without importing spend-gated code.
DEFAULT_MODEL = "glm-5.3"
VCHECK_PRICES_USD_PER_MTOK = {
    "glm-5.3": (1.40, 4.40),
    "glm-5.3-flash": (0.15, 0.50),
}
RED_MAX_STEPS = 30
# Cost-model assumptions (documented in docs/verifier-check.md).
EST_AVG_INPUT_TOKENS_PER_STEP = 6_000
EST_AVG_OUTPUT_TOKENS_PER_STEP = 1_500


def estimate_task_usd(
    model: str = DEFAULT_MODEL,
    *,
    steps: int = RED_MAX_STEPS,
    avg_input_tokens: int = EST_AVG_INPUT_TOKENS_PER_STEP,
    avg_output_tokens: int = EST_AVG_OUTPUT_TOKENS_PER_STEP,
) -> float:
    """Rough per-task spend for one red-agent audit plus adjudication share."""
    price_in, price_out = VCHECK_PRICES_USD_PER_MTOK.get(
        model, VCHECK_PRICES_USD_PER_MTOK[DEFAULT_MODEL]
    )
    return steps * (avg_input_tokens * price_in + avg_output_tokens * price_out) / 1_000_000


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug or "campaign"


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"manifest is unreadable: {path} ({exc})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ValueError(f"manifest is not JSON: {path}") from exc
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(f"manifest parses as neither JSON nor YAML: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"manifest must be a JSON/YAML object: {path}")
    return data


def _manifest_tasks(manifest: dict[str, Any]) -> list[str]:
    tasks = manifest.get("tasks", [])
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest needs a non-empty 'tasks' list of task ids")
    names: list[str] = []
    seen: set[str] = set()
    for entry in tasks:
        if isinstance(entry, str) and entry.strip():
            names.append(entry.strip())
        elif isinstance(entry, dict):
            for key in ("task", "id", "name"):
                if isinstance(entry.get(key), str) and entry[key].strip():
                    names.append(entry[key].strip())
                    break
            else:
                raise ValueError(f"manifest task entry has no task/id/name: {entry!r}")
        else:
            raise ValueError(f"manifest task entry must be a string or object: {entry!r}")
        if names[-1] in seen:
            raise ValueError(f"manifest has duplicate task id: {names[-1]!r}")
        seen.add(names[-1])
    return names


def _resolve_package_path(raw: str | Path, *, manifest_dir: Path, root: Path) -> Path:
    """Resolve a manifest package path to an absolute path.

    Absolute paths stay absolute. Relative paths resolve against the manifest
    directory first when that path exists, otherwise against the repo root.
    """
    candidate = Path(str(raw)).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    near_manifest = manifest_dir / candidate
    if near_manifest.exists():
        return near_manifest.resolve()
    return (root / candidate).resolve()


def _is_package_dir(raw: str, *, manifest_dir: Path, root: Path) -> bool:
    """Whether a bare string task entry names an existing package directory."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.is_dir()
    return (manifest_dir / candidate).is_dir() or (root / candidate).is_dir()


def _resolve_task_packages(
    manifest: dict[str, Any], *, manifest_path: Path, root: Path
) -> dict[str, str]:
    """Map task ids to resolved absolute package path strings.

    Binds entries that name a package: object entries with ``package``/``path``,
    string entries that name an existing package directory, and the top-level
    ``packages`` task->path mapping. ID-only manifests resolve to ``{}`` so
    plan-only runs stay green; ``--execute`` fails closed on anything unbound.
    Duplicate task ids or conflicting bindings raise ``ValueError``.
    """
    top = manifest.get("packages", {})
    if top is None:
        top = {}
    if not isinstance(top, Mapping):
        raise ValueError("manifest 'packages' must be a task->path mapping")
    normalized_top: dict[str, str] = {}
    for key, value in top.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"manifest 'packages' key must be a task id string: {key!r}")
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError(f"manifest 'packages[{key}]' must be a path string")
        task_key = key.strip()
        text = str(value).strip()
        if task_key in normalized_top and normalized_top[task_key] != text:
            raise ValueError(
                f"manifest 'packages' has conflicting paths for task {task_key!r}"
            )
        normalized_top[task_key] = text
    manifest_dir = manifest_path.parent
    entries = manifest.get("tasks", [])
    bindings: dict[str, str] = {}
    seen: set[str] = set()

    def _bind(task_id: str, raw: str | Path | None) -> None:
        if raw is None:
            return
        if not isinstance(raw, (str, Path)) or not str(raw).strip():
            raise ValueError(f"manifest task {task_id!r} has an empty package path")
        resolved = str(
            _resolve_package_path(str(raw).strip(), manifest_dir=manifest_dir, root=root)
        )
        prior = bindings.get(task_id)
        if prior is not None and prior != resolved:
            raise ValueError(
                f"manifest task {task_id!r} has conflicting package bindings"
            )
        bindings[task_id] = resolved

    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            task_id = entry.strip()
            if task_id in seen:
                raise ValueError(f"manifest has duplicate task id: {task_id!r}")
            seen.add(task_id)
            if task_id in normalized_top:
                _bind(task_id, normalized_top[task_id])
            elif _is_package_dir(task_id, manifest_dir=manifest_dir, root=root):
                _bind(task_id, task_id)
        elif isinstance(entry, dict):
            task_id = None
            for name_key in ("task", "id", "name"):
                if isinstance(entry.get(name_key), str) and entry[name_key].strip():
                    task_id = entry[name_key].strip()
                    break
            if task_id is None:
                raise ValueError(f"manifest task entry has no task/id/name: {entry!r}")
            if task_id in seen:
                raise ValueError(f"manifest has duplicate task id: {task_id!r}")
            seen.add(task_id)
            raw_package = entry.get("package")
            raw_path = entry.get("path")
            if (
                raw_package is not None
                and raw_path is not None
                and str(raw_package).strip() != str(raw_path).strip()
            ):
                raise ValueError(
                    f"manifest task {task_id!r} has conflicting 'package' and 'path'"
                )
            raw_obj = raw_package if raw_package is not None else raw_path
            raw_top = normalized_top.get(task_id)
            if raw_obj is not None and raw_top is not None:
                via_obj = str(
                    _resolve_package_path(
                        str(raw_obj).strip(), manifest_dir=manifest_dir, root=root
                    )
                )
                via_top = str(
                    _resolve_package_path(raw_top, manifest_dir=manifest_dir, root=root)
                )
                if via_obj != via_top:
                    raise ValueError(
                        f"manifest task {task_id!r} has conflicting package bindings"
                    )
            _bind(task_id, str(raw_obj).strip() if raw_obj is not None else raw_top)
        else:
            raise ValueError(
                f"manifest task entry must be a string or object: {entry!r}"
            )
    return bindings


def _manifest_digest(manifest: dict[str, Any]) -> str:
    """CORE.manifest_digest over a built manifest, else a local canonical sha256."""
    try:
        from evallab import vcheck as core

        candidate = _build_manifest(manifest)
        if not isinstance(candidate, dict):
            return str(core.manifest_digest(candidate))
    except Exception:
        pass
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_manifest(manifest: dict[str, Any]) -> Any:
    """Build CORE.CampaignManifest best-effort; raw dict when CORE is absent."""
    try:
        from evallab.vcheck import CampaignManifest, RequirementItem, RequirementMap
    except ImportError:
        return manifest
    try:
        req_maps = []
        entries = manifest.get("requirement_maps") or []
        for entry in entries:
            if isinstance(entry, dict) and "task" in entry and "items" in entry:
                items = [
                    RequirementItem(**item) if isinstance(item, dict) else item
                    for item in entry["items"]
                ]
                req_maps.append(RequirementMap.freeze(str(entry["task"]), items))
            else:
                req_maps.append(entry)
        kwargs = dict(manifest)
        if "requirement_maps" in manifest:
            kwargs["requirement_maps"] = req_maps
        fields = (
            "campaign_id",
            "benchmark",
            "pin",
            "tasks",
            "requirement_maps",
            "models",
            "budgets",
            "prompt_shas",
        )
        return CampaignManifest(**{k: kwargs[k] for k in fields if k in kwargs})
    except Exception:
        return manifest


def _build_grade_fn(root: Path, run_dir: Path) -> Any | None:
    """Free local-Docker grade_fn over CORE.grade_submission; None when absent."""
    grade_submission = None
    for module_name in ("evallab.vcheck", "evallab.verifier_mutation"):
        try:
            module = __import__(module_name, fromlist=["grade_submission"])
        except ImportError:
            continue
        grade_submission = getattr(module, "grade_submission", None)
        if callable(grade_submission):
            break
    if grade_submission is None:
        return None

    def grade_fn(package: Any, submission: Any) -> Any:
        with contextlib.redirect_stdout(sys.stderr):
            return grade_submission(package, submission, repo_root=root, run_dir=run_dir)

    return grade_fn


def _new_run_dir(root: Path, campaign: str) -> Path:
    from evallab.execution_contracts import new_ulid

    run_dir = root / "runs" / ".vcheck" / f"{_slug(campaign)}-{new_ulid().lower()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable run document: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"run document must be a JSON object: {path}")
    return data


def _emit(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    """Print exactly one JSON document on stdout in --json mode."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))


def _vcheck_run_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    manifest_path = _resolve(root, args.manifest)
    try:
        manifest = _load_manifest(manifest_path)
        tasks = _manifest_tasks(manifest)
        packages = _resolve_task_packages(manifest, manifest_path=manifest_path, root=root)
    except ValueError as exc:
        print(f"vcheck run: {exc}", file=sys.stderr)
        return 1
    controls_only = bool(getattr(args, "controls_only", False))
    if controls_only and args.execute:
        print(
            "vcheck run: --controls-only and --execute are mutually exclusive "
            "(controls-only is a $0 validation gate; it never spends budget)",
            file=sys.stderr,
        )
        return 1
    if not controls_only:
        if args.budget_usd is None or not args.approve:
            print(
                "vcheck run: --budget-usd and --approve are both required "
                "(spend-gated red/blue campaign; approval text is recorded in the plan)",
                file=sys.stderr,
            )
            return 1
        if args.budget_usd <= 0:
            print("vcheck run: --budget-usd must be positive", file=sys.stderr)
            return 1
    campaign_id = str(manifest.get("campaign_id") or manifest_path.stem)
    models = manifest.get("models") or {}
    model = str(models.get("red") or DEFAULT_MODEL)
    per_task = estimate_task_usd(model)
    total = per_task * len(tasks)
    try:
        run_dir = _new_run_dir(root, campaign_id)
    except OSError as exc:
        print(f"vcheck run: cannot create run directory ({exc})", file=sys.stderr)
        return 1
    plan: dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "campaign_id": campaign_id,
        "manifest_digest": _manifest_digest(manifest),
        "manifest": manifest,
        "tasks": tasks,
        "packages": packages,
        "models": {"red": model, **{k: v for k, v in models.items() if k != "red"}},
        "budget_usd": args.budget_usd,
        "approval": args.approve,
        "workers": args.workers,
        "estimate_per_task_usd": per_task,
        "estimated_total_usd": total,
        "execute": bool(args.execute),
        "controls_only": controls_only,
        "created_at": _utcnow(),
    }
    _write_json(run_dir / "vcheck-plan.json", plan)
    if controls_only:
        return _controls_only_campaign(
            args, root, manifest, tasks, plan, run_dir, packages=packages
        )
    if total > args.budget_usd:
        print(
            f"vcheck run: estimated ${total:.2f} exceeds --budget-usd ${args.budget_usd:.2f}; "
            "narrow 'tasks' or raise the cap",
            file=sys.stderr,
        )
        return 1
    if not args.execute:
        payload = {
            "verdict": "plan",
            "run_dir": str(run_dir),
            "counts": {"tasks": len(tasks), "hypotheses": 0, "survivors": 0},
            "estimated_total_usd": total,
            "survivors": [],
            "hypotheses": [],
        }
        if args.json:
            _emit(args, payload)
        else:
            print(f"Campaign: {campaign_id}")
            print(f"Tasks: {len(tasks)}")
            print(f"Estimate: ${total:.2f} (${per_task:.2f}/task, model {model})")
            print("Plan only: re-run with --execute to spend budget.")
            print(f"run directory: {run_dir}")
        return 0
    return _execute_campaign(args, root, manifest, tasks, plan, run_dir, packages=packages)


def _execute_campaign(
    args: argparse.Namespace,
    root: Path,
    manifest: dict[str, Any],
    tasks: list[str],
    plan: dict[str, Any],
    run_dir: Path,
    *,
    packages: dict[str, str] | None = None,
) -> int:
    resolved = dict(packages if packages is not None else plan.get("packages") or {})
    missing = [task for task in tasks if task not in resolved]
    if missing:
        print(
            "vcheck run: --execute needs a package for every task; "
            f"missing: {', '.join(missing)} "
            "(bind each task with a package/path entry or top-level 'packages')",
            file=sys.stderr,
        )
        return 1
    absent = [task for task in tasks if not Path(resolved[task]).is_dir()]
    if absent:
        print(
            "vcheck run: package directory missing for tasks: "
            + ", ".join(f"{task} ({resolved[task]})" for task in absent),
            file=sys.stderr,
        )
        return 1
    try:
        from evallab.vcheck import run_wave
    except ImportError as exc:
        print(f"vcheck run: execute backend unavailable ({exc})", file=sys.stderr)
        return 1
    try:
        from evallab.vcheck_client import Budget
    except ImportError as exc:
        print(f"vcheck run: model client unavailable ({exc})", file=sys.stderr)
        return 1
    budget = Budget(cap_usd=args.budget_usd, spent_usd=0.0, approval=args.approve)
    try:
        budget.require_approval()
    except Exception as exc:
        print(f"vcheck run: {exc}", file=sys.stderr)
        return 1
    campaign = _build_manifest({**manifest, "campaign_id": plan["campaign_id"]})
    grade_fn = _build_grade_fn(root, run_dir)
    if grade_fn is None:
        print("vcheck run: grading backend unavailable", file=sys.stderr)
        return 1
    wave_packages = {task: resolved[task] for task in tasks}
    red_factory = _build_red_factory(
        budget=budget,
        grade_fn=grade_fn,
        run_dir=run_dir,
        model=plan["models"].get("red"),
        packages=wave_packages,
    )
    if red_factory is None:
        print("vcheck run: red-agent slice unavailable", file=sys.stderr)
        return 1
    try:
        # Exactly one wave call: a whole-wave TypeError retry could re-run
        # harness controls and red agents (double Docker grading/model spend)
        # if the TypeError escaped from deep inside the wave.
        result = run_wave(
            campaign,
            tasks,
            red_factory,
            broadcast=(),
            grade_fn=grade_fn,
            packages=wave_packages,
        )
    except Exception as exc:
        print(f"vcheck run: wave failed ({exc})", file=sys.stderr)
        return 1
    hypotheses = [_to_jsonable(h) for h in getattr(result, "hypotheses", []) or []]
    hypotheses = [h for h in hypotheses if isinstance(h, dict)]
    survivors = [h for h in hypotheses if h.get("status") in ("promoted", "candidate")]
    counts = {
        "tasks": len(tasks),
        "hypotheses": len(hypotheses),
        "survivors": len(survivors),
        "skipped_task_broken": len(getattr(result, "skipped_task_broken", []) or []),
    }
    report: dict[str, Any] = {
        "schema": SCHEMA_REPORT,
        "campaign_id": plan["campaign_id"],
        "manifest_digest": plan["manifest_digest"],
        "run_dir": str(run_dir),
        "approval": args.approve,
        "budget_usd": args.budget_usd,
        "verdict": "executed",
        "counts": counts,
        "survivors": survivors,
        "hypotheses": hypotheses,
        "broadcast": list(getattr(result, "broadcast", []) or []),
        "created_at": _utcnow(),
    }
    _write_json(run_dir / "vcheck-report.json", report)
    if args.json:
        _emit(args, report)
    else:
        print(f"Campaign: {plan['campaign_id']}")
        print(f"Hypotheses: {counts['hypotheses']} ({counts['survivors']} survivors)")
        print(f"Task-broken skips: {counts['skipped_task_broken']}")
        print(f"run directory: {run_dir}")
    return 0


def _control_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a control/grade field from a dataclass, namespace, or mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _grade_control_row(grade: Any) -> dict[str, Any]:
    """One graded control (baseline/legit/negative) as report JSON."""
    if grade is None:
        return {"reward": None, "status": "unscored"}
    row: dict[str, Any] = {
        "reward": _control_field(grade, "reward"),
        "status": str(_control_field(grade, "status", "?")),
    }
    job_dir = _control_field(grade, "job_dir")
    if job_dir is not None:
        row["job_dir"] = str(job_dir)
    script = _control_field(grade, "script")
    if script is not None and str(script) != "":
        row["script"] = str(script)
    return row


def _control_row_to_json(task: str, package: str, report: Any) -> dict[str, Any]:
    """One task's harness-controls outcome as report JSON."""
    return {
        "task": task,
        "package": str(package),
        "verdict": str(_control_field(report, "verdict", "?")),
        "reason": str(_control_field(report, "reason", "")),
        "baseline": _grade_control_row(_control_field(report, "baseline")),
        "legit": _grade_control_row(_control_field(report, "legit")),
        "negative": _grade_control_row(_control_field(report, "negative")),
    }


def _controls_only_campaign(
    args: argparse.Namespace,
    root: Path,
    manifest: dict[str, Any],
    tasks: list[str],
    plan: dict[str, Any],
    run_dir: Path,
    *,
    packages: dict[str, str] | None = None,
) -> int:
    """Run the $0 harness-controls gate: oracle/empty controls per task.

    Never constructs Budget, red agents, or waves, so no model spend is
    possible. Fails closed before grading when any task lacks a resolved
    existing package. Returns non-zero when any task reports task_broken.
    """
    del manifest
    resolved = dict(packages if packages is not None else plan.get("packages") or {})
    missing = [task for task in tasks if task not in resolved]
    if missing:
        print(
            "vcheck run: --controls-only needs a package for every task; "
            f"missing: {', '.join(missing)} "
            "(bind each task with a package/path entry or top-level 'packages')",
            file=sys.stderr,
        )
        return 1
    absent = [task for task in tasks if not Path(resolved[task]).is_dir()]
    if absent:
        print(
            "vcheck run: package directory missing for tasks: "
            + ", ".join(f"{task} ({resolved[task]})" for task in absent),
            file=sys.stderr,
        )
        return 1
    try:
        from evallab.vcheck import run_controls
    except ImportError as exc:
        print(f"vcheck run: controls backend unavailable ({exc})", file=sys.stderr)
        return 1
    grade_fn = _build_grade_fn(root, run_dir)
    if grade_fn is None:
        print("vcheck run: grading backend unavailable", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    broken: list[str] = []
    for task in tasks:
        try:
            control = run_controls(resolved[task], grade_fn, task=task)
        except Exception as exc:
            print(f"vcheck run: controls failed for {task} ({exc})", file=sys.stderr)
            return 1
        row = _control_row_to_json(task, resolved[task], control)
        rows.append(row)
        if row["verdict"] != "ok":
            broken.append(task)
    report: dict[str, Any] = {
        "schema": SCHEMA_REPORT,
        "campaign_id": plan["campaign_id"],
        "manifest_digest": plan["manifest_digest"],
        "run_dir": str(run_dir),
        "verdict": "controls",
        "counts": {
            "tasks": len(tasks),
            "controls_ok": len(tasks) - len(broken),
            "task_broken": len(broken),
        },
        "controls": rows,
        "task_broken": broken,
        "survivors": [],
        "hypotheses": [],
        "broadcast": [],
        "created_at": _utcnow(),
    }
    _write_json(run_dir / "vcheck-report.json", report)
    if getattr(args, "json", False):
        _emit(args, report)
    else:
        print(f"Campaign: {plan['campaign_id']}")
        print(f"Controls: {len(tasks) - len(broken)} ok, {len(broken)} task-broken")
        for row in rows:
            print(f"  {row['task']}: {row['verdict']} ({row['reason']})")
        print(f"run directory: {run_dir}")
    return 1 if broken else 0


def _build_red_factory(
    *,
    budget: Any,
    grade_fn: Any,
    run_dir: Path,
    model: Any,
    packages: dict[str, str] | None = None,
) -> Any | None:
    """Adapt RED.run_red to CORE's red_factory(task_id, broadcast_hints) shape.

    ``packages`` maps task ids to resolved package paths. The per-task grader
    calls the real grader with the package path (never the bare task id), and
    ``run_red`` receives a task context mapping carrying the task id, package
    path, and task dir so red read/list file tools resolve against the task
    root.
    """
    try:
        from evallab import vcheck_red as red
    except ImportError:
        return None
    run_red = getattr(red, "run_red", None)
    if not callable(run_red):
        for attr in ("make_red_factory", "red_factory"):
            factory = getattr(red, attr, None)
            if callable(factory):
                return factory
        return None
    try:
        from evallab import vcheck_client as client
    except ImportError:
        return None
    try:
        from evallab import vcheck_blue as blue
    except ImportError:
        blue = None

    def factory(task_id: str, broadcast_hints: Any = ()) -> list[Any]:
        hints = list(broadcast_hints or ())
        package = packages.get(task_id) if packages else None

        def probe_fn(hint: str) -> Any:
            if blue is None:
                return {"outcome": "unavailable", "hint": hint}
            probe = getattr(blue, "probe_bfcl", None)
            if callable(probe):
                ctx: Any = (
                    {"task": task_id, "package": package, "task_dir": package}
                    if package is not None
                    else {"task": task_id}
                )
                try:
                    return probe(ctx, hint)
                except TypeError:
                    return probe(task_id, hint)
            return {"outcome": "unavailable", "hint": hint}

        def task_grade_fn(submission: Any) -> Any:
            if package is not None:
                return grade_fn(package, submission)
            return grade_fn(task_id, submission)

        task_ctx: Any = task_id
        if package is not None:
            task_ctx = {
                "task": task_id,
                "task_id": task_id,
                "package": package,
                "task_dir": package,
            }
        return list(
            run_red(
                task_ctx,
                client=client,
                budget=budget,
                broadcast_hints=hints,
                grade_fn=task_grade_fn,
                probe_fn=probe_fn,
                run_dir=run_dir,
                model=model,
            )
        )

    return factory


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    for method in ("model_dump", "to_dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                dumped = fn(mode="json") if method == "model_dump" else fn()
            except TypeError:
                try:
                    dumped = fn()
                except Exception:
                    continue
            except Exception:
                continue
            return _to_jsonable(dumped)
    if hasattr(obj, "__dataclass_fields__"):
        import dataclasses

        return _to_jsonable(dataclasses.asdict(obj))
    return str(obj)


def _candidate_docs(root: Path, run_dir: Path | None) -> list[Path]:
    if run_dir is not None:
        return [run_dir / "vcheck-report.json", run_dir / "vcheck-plan.json"]
    base = root / "runs" / ".vcheck"
    docs: list[Path] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir():
                docs.extend([child / "vcheck-report.json", child / "vcheck-plan.json"])
    return docs


def _find_record(
    root: Path, run_dir: Path | None, record_id: str, keys: tuple[str, ...]
) -> tuple[Path, dict[str, Any], str, int, dict[str, Any]] | None:
    """Find a hypothesis/finding dict by id across run documents."""
    for doc_path in _candidate_docs(root, run_dir):
        if not doc_path.is_file():
            continue
        try:
            doc = _read_json(doc_path)
        except ValueError:
            continue
        for key in keys:
            records = doc.get(key)
            if not isinstance(records, list):
                continue
            for index, record in enumerate(records):
                if isinstance(record, dict) and record.get("id") == record_id:
                    return doc_path, doc, key, index, record
    return None


def _try_judge_backend(record: dict[str, Any], verdict: str) -> Any | None:
    """Delegate to JUDGE.confirm_hypothesis(hypothesis, verdict, actor).

    Returns the backend's object on success, or None when no JUDGE slice has
    landed (caller falls back to the local CORE-shaped transition).
    """
    fn = None
    for module_name in ("evallab.vcheck_judge", "evallab.vcheck"):
        try:
            module = __import__(module_name, fromlist=["confirm_hypothesis"])
        except ImportError:
            continue
        fn = getattr(module, "confirm_hypothesis", None)
        if callable(fn):
            break
    if fn is None:
        return None
    try:
        return fn(record, verdict, "human")
    except TypeError:
        return fn(record, verdict)


def _vcheck_confirm_command(
    args: argparse.Namespace, root: Path, *, harbor: Any | None = None
) -> int:
    del harbor
    run_dir = _resolve(root, args.run_dir) if args.run_dir is not None else None
    if run_dir is not None and not run_dir.is_dir():
        print(f"vcheck confirm: no such run directory: {run_dir}", file=sys.stderr)
        return 1
    found = _find_record(root, run_dir, args.hypothesis_id, ("hypotheses",))
    if found is None:
        print(
            f"vcheck confirm: unknown hypothesis '{args.hypothesis_id}'",
            file=sys.stderr,
        )
        return 1
    doc_path, doc, key, index, record = found
    to_status = "promoted" if args.verdict == "confirm" else "rejected"
    from_status = str(record.get("status", "candidate"))
    if run_dir is None:
        run_dir = doc_path.parent
    # JUDGE.confirm_hypothesis only promotes (verdict = confirmation evidence);
    # rejection is a local CORE-shaped transition.
    try:
        if args.verdict == "confirm":
            backend_result = _try_judge_backend(record, "human confirmation via CLI")
            if backend_result is None:
                _local_transition(record, from_status, to_status, "human confirmation via CLI")
            elif isinstance(backend_result, dict) and backend_result is not record:
                doc[key][index] = backend_result
                record = backend_result
        else:
            _local_transition(record, from_status, to_status, "human rejection via CLI")
        _write_json(doc_path, doc)
    except Exception as exc:
        print(f"vcheck confirm: {exc}", file=sys.stderr)
        return 1
    payload = {
        "verdict": args.verdict,
        "hypothesis_id": args.hypothesis_id,
        "from": from_status,
        "to": str(record.get("status", to_status)),
    }
    if args.json:
        _emit(args, payload)
    else:
        print(f"Hypothesis {args.hypothesis_id}: {from_status} -> {to_status}")
    return 0


def _local_transition(record: dict[str, Any], from_status: str, to_status: str, note: str) -> None:
    """CORE-shaped local status transition (history entries {from, to, note})."""
    history = record.get("history")
    if not isinstance(history, list):
        history = []
        record["history"] = history
    history.append({"from": from_status, "to": to_status, "note": note})
    record["status"] = to_status


def _try_store_export(
    finding_id: str, record: dict[str, Any], run_dir: Path, output_dir: Path
) -> Path | None:
    """Delegate slug-bearing findings to STORE.export_finding.

    Returns the export path, or None when the caller should use the local
    writer (no slug on the record, an explicit --output-dir override, or no
    STORE slice). STORE owns its ``<run_dir>/findings/<slug>/`` layout.
    """
    slug = record.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    if output_dir != run_dir / "findings" / _slug(slug):
        return None
    try:
        module = __import__("evallab.vcheck_store", fromlist=["export_finding"])
    except ImportError:
        return None
    fn = getattr(module, "export_finding", None)
    if not callable(fn):
        return None
    cases = record.get("cases")
    if not isinstance(cases, list):
        cases = []
    return Path(fn(run_dir, record, cases))


def _write_local_export(output_dir: Path, record: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "finding.json", record)
    lines = [
        f"# {record.get('id', 'finding')}",
        "",
        f"- task: `{record.get('task', '?')}`",
        f"- status: **{record.get('status', '?')}**",
        f"- statement: {record.get('statement', '?')}",
        "",
        "## Evidence",
        "",
    ]
    evidence = record.get("evidence")
    if isinstance(evidence, list) and evidence:
        for item in evidence:
            lines.append(f"- {json.dumps(_to_jsonable(item), sort_keys=True)}")
    else:
        lines.append("no structured evidence recorded")
    lines += ["", "## Reproduce", "", "See `finding.json` for the graded submissions.", ""]
    (output_dir / "FINDING.md").write_text("\n".join(lines), encoding="utf-8")
    (output_dir / "REPRODUCE.md").write_text(
        "# Reproduce\n\nRe-grade the submissions in `finding.json` with the task's own"
        " verifier; each must score exactly as recorded there.\n",
        encoding="utf-8",
    )


def _vcheck_export_command(
    args: argparse.Namespace, root: Path, *, harbor: Any | None = None
) -> int:
    del harbor
    run_dir = _resolve(root, args.run_dir) if args.run_dir is not None else None
    found = _find_record(root, run_dir, args.finding_id, ("findings", "hypotheses"))
    if found is None:
        print(f"vcheck export: unknown finding '{args.finding_id}'", file=sys.stderr)
        return 1
    _, _, _, _, record = found
    if run_dir is None:
        for doc_path in _candidate_docs(root, None):
            if doc_path.is_file():
                run_dir = doc_path.parent
                break
    if run_dir is None:
        run_dir = root
    output_dir = (
        _resolve(root, args.output_dir)
        if args.output_dir is not None
        else run_dir / "exports" / _slug(args.finding_id)
    )
    try:
        store_out = _try_store_export(args.finding_id, record, run_dir, output_dir)
        if store_out is None:
            _write_local_export(output_dir, record)
        else:
            output_dir = store_out
    except Exception as exc:
        print(f"vcheck export: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _emit(args, {"finding_id": args.finding_id, "output_dir": str(output_dir)})
    else:
        print(f"export: {output_dir}")
    return 0


def _vcheck_status_command(
    args: argparse.Namespace, root: Path, *, harbor: Any | None = None
) -> int:
    del harbor
    run_dir = _resolve(root, args.run_dir)
    if not run_dir.is_dir():
        print(f"vcheck status: no such run directory: {run_dir}", file=sys.stderr)
        return 1
    plan_path = run_dir / "vcheck-plan.json"
    report_path = run_dir / "vcheck-report.json"
    if not plan_path.is_file() and not report_path.is_file():
        print(f"vcheck status: not a vcheck run directory: {run_dir}", file=sys.stderr)
        return 1
    try:
        plan = _read_json(plan_path) if plan_path.is_file() else {}
        report = _read_json(report_path) if report_path.is_file() else {}
    except ValueError as exc:
        print(f"vcheck status: {exc}", file=sys.stderr)
        return 1
    hypotheses: list[dict[str, Any]] = []
    for doc in (report, plan):
        records = doc.get("hypotheses")
        if isinstance(records, list):
            hypotheses.extend(r for r in records if isinstance(r, dict))
    by_status: dict[str, int] = {}
    for hypothesis in hypotheses:
        status = str(hypothesis.get("status", "?"))
        by_status[status] = by_status.get(status, 0) + 1
    payload = {
        "run_dir": str(run_dir),
        "campaign_id": report.get("campaign_id") or plan.get("campaign_id"),
        "planned": plan_path.is_file(),
        "executed": report_path.is_file(),
        "verdict": report.get("verdict", "plan"),
        "counts": report.get("counts")
        or {"tasks": len(plan.get("tasks", [])) if isinstance(plan.get("tasks"), list) else 0},
        "by_status": by_status,
    }
    if args.json:
        _emit(args, payload)
    else:
        print(f"Campaign: {payload['campaign_id']} ({payload['verdict']})")
        print(f"Planned: {payload['planned']}  Executed: {payload['executed']}")
        print(f"Counts: {json.dumps(payload['counts'], sort_keys=True)}")
        if by_status:
            print(f"Hypotheses: {json.dumps(by_status, sort_keys=True)}")
        print(f"run directory: {run_dir}")
    return 0


def build_vcheck_parser(commands: argparse._SubParsersAction) -> None:
    """Register ``evallab vcheck`` (red/blue verifier-audit campaigns)."""
    vcheck = commands.add_parser(
        "vcheck",
        help="Red/blue verifier audit: wrong submissions vs the task's own grader",
    )
    sub = vcheck.add_subparsers(dest="vcheck_command", required=True)

    run = sub.add_parser("run", help="Plan (default) or execute a red/blue campaign")
    run.add_argument("--manifest", type=Path, required=True, help="Campaign manifest (JSON/YAML)")
    run.add_argument("--budget-usd", type=float, default=None)
    run.add_argument("--approve", type=str, default=None, help="Recorded spend approval text")
    run.add_argument("--execute", action="store_true", help="Spend budget and run the campaign")
    run.add_argument(
        "--controls-only",
        action="store_true",
        help="$0 harness-controls gate: oracle/empty controls per task, no budget or model spend",
    )
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_vcheck_run_command)

    confirm = sub.add_parser("confirm", help="Human confirm/reject of one hypothesis")
    confirm.add_argument("hypothesis_id")
    confirm.add_argument("--verdict", choices=("confirm", "reject"), required=True)
    confirm.add_argument("--run-dir", type=Path, default=None)
    confirm.add_argument("--json", action="store_true")
    confirm.set_defaults(func=_vcheck_confirm_command)

    export = sub.add_parser("export", help="Export one finding (envcheck-findings-compatible)")
    export.add_argument("finding_id")
    export.add_argument("--run-dir", type=Path, default=None)
    export.add_argument("--output-dir", type=Path, default=None)
    export.add_argument("--json", action="store_true")
    export.set_defaults(func=_vcheck_export_command)

    status = sub.add_parser("status", help="Summarize one vcheck run directory")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_vcheck_status_command)
