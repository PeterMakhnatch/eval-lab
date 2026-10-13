"""Build the auditor SFT corpus: raw Terminal Wrench serious-exploit histories.

Reads the pinned public Terminal Wrench index (``index/tasks.json`` +
``index/trajectories.json`` at a recorded upstream commit), deterministically
selects complete short rewarded serious-exploit histories, downloads the raw
ATIF trajectories, converts them to native-compatible chat rows, and writes
``histories.jsonl`` (200 full histories) + ``conversations.jsonl`` (one exact
prefix ending at EACH recorded assistant turn, for
train_on_what=last_assistant_message) + ``examples.jsonl`` (deterministic
example-to-history/step mapping) + manifest + per-history provenance +
dispositions + a sealed history-level group-disjoint split.

- Only the ``raw`` upstream view is used. Sanitized/stripped variants rewrite
  or empty assistant text and are never training inputs.
- The original audit instruction (first user step, hacker framing included)
  and every terminal observation are kept verbatim.
- Assistant turns are a DERIVED structured-action behavior-cloning target,
  not raw teacher outputs: upstream stores parsed-mode trajectories (the
  model's raw JSON emission was not retained), so each assistant turn is
  rendered as complete valid native Terminus JSON
  (``{"analysis", "plan", "commands", "task_complete"}`` per the stock
  Harbor ``terminus-json`` parser) from the verbatim parsed ``Analysis:`` /
  ``Plan:`` text and the verbatim recorded ``tool_calls`` arguments. Every
  turn is gated through the actual installed stock parser with zero error
  before acceptance. Provenance keeps the original raw bytes hash plus the
  declared transform; nothing here claims to be the exact original history.
- Histories with ambiguous analysis/plan markers, non-text (multimodal)
  content, copied-context steps, fallback stand-ins, empty assistant text,
  or unknown tool calls are refused, never repaired.

Free preparation CLI (no spend, no Docker, no model calls)::

    uv run python research/experiments/auditor-sft-v1/prepare_data.py prepare \\
        --index-dir derived/auditor-sft-v1/source-cache/index \\
        --cache-dir derived/auditor-sft-v1/source-cache \\
        --out derived/auditor-sft-v1/data \\
        --holdout derived/auditor-sft-v1/holdout.json

``--dry-run`` ranks candidates from the index without downloading.
``--val-fraction 0`` (default) puts every history in train: the five frozen
TW eval tasks are the held-out set, so no additional dev split is kept.
"""
import argparse
import hashlib
import json
import random
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.sft_terminus import convert_segment

CONTRACT_VERSION = "auditor-sft-v1.prepare_data/1"
SEED = 20261013
TARGET_N = 200
MAX_EPISODES = 4
PER_TASK_CAP = 2
PER_MODEL_CAP = 100
CATEGORY_FLOOR = 8
VAL_FRACTION = 0.0
REWARD_MIN = 1.0

TW_REPO = "few-sh/terminal-wrench"
#: Pinned upstream commit (HEAD resolved 2026-10-13; commit date 2026-04-18).
SOURCE_REVISION = "d8a29613235a0ef56a8b70b3142626a533da28c2"
TW_BASE = f"https://raw.githubusercontent.com/{TW_REPO}"
INDEX_FILES = ("tasks.json", "trajectories.json")
SERIOUS = "rewarded_serious_exploit"
USER_AGENT = "eval-lab-auditor-sft-v1"

CONVERSATIONS_FILE = "conversations.jsonl"
HISTORIES_FILE = "histories.jsonl"
EXAMPLES_FILE = "examples.jsonl"
MANIFEST_FILE = "manifest.json"
DISPOSITIONS_FILE = "dispositions.jsonl"
PROVENANCE_FILE = "provenance.jsonl"
SPLIT_FILE = "split.json"
RANKING_FILE = "ranking.json"

#: Declared transform recorded per history in provenance.jsonl.
TRANSFORM_VERSION = "auditor-sft-v1.derived-structured-action/1"
#: Stock native parser filename inside the installed Harbor package.
STOCK_PARSER_REL = Path("harbor/agents/terminus_2/terminus_json_plain_parser.py")

#: Recorded in the manifest so training-side readers see the exact caveats.
FIDELITY_LIMITS = [
    "selection_evidence_is_publisher_labels: index classification "
    f"{SERIOUS!r} (+reward>=1.0) is selection evidence, not fresh exploit "
    "certification; no replay through verifiers was performed here.",
    "derived_structured_action_target: assistant turns are NOT raw teacher "
    "outputs. Upstream retains parsed Analysis/Plan text plus recorded "
    "tool_calls, not the model's raw JSON emission. Each turn is re-rendered "
    "as complete valid native Terminus JSON "
    '{"analysis","plan","commands","task_complete"} from the verbatim split '
    "prose and verbatim tool_calls arguments, gated through the actual "
    "installed stock parser with zero error. Provenance keeps the original "
    "raw bytes hash plus this declared transform.",
    "no_repo_overlap_proof: grouping is TW task_id merged on "
    "instruction-sha256 collisions; the public index carries no repository "
    "metadata, so cross-task shared-repo overlap cannot be ruled out.",
    "masking_last_assistant: conversations.jsonl is one exact prefix per "
    "recorded assistant turn for TrainOnWhat last_assistant_message (only "
    "each row's final assistant turn supervised). Pinned smoke: "
    "qwen3_5_disable_thinking.has_extension_property=False, and cookbook "
    "warns all_assistant_messages trains earlier turns on incorrect "
    "generation prefixes — hence per-turn prefixes, not all-assistant loss.",
    "pricing_basis: Tinker SFT train cost is billed on ALL rendered sequence "
    "tokens (not the supervised subset); size the corpus against total "
    "rendered tokens from the parent's estimator measurement.",
]

TERMINUS_NAMES = frozenset({"terminus-2", "terminus2"})


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def raw_trajectory_url(row: dict[str, Any], revision: str) -> str:
    """Pinned raw-view ATIF URL for one index row."""
    return (
        f"{TW_BASE}/{revision}/tasks/{row['task_id']}/{row['model']}/"
        f"hack_trajectories/{row['trajectory_label']}/trial/agent/trajectory.json"
    )


def conversation_id(row: dict[str, Any]) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in
                   f"{row['task_id']}-{row['model']}-{row['trajectory_label']}")
    return f"tw-{safe}"


def group_key(task_id: str) -> str:
    return f"tw-task:{task_id}"


def fetch_index(index_dir: Path, revision: str) -> dict[str, Path]:
    """Download pinned index files once; return paths."""
    index_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in INDEX_FILES:
        dest = index_dir / name
        if not (dest.exists() and dest.stat().st_size > 0):
            url = f"{TW_BASE}/{revision}/index/{name}"
            dest.write_bytes(_get(url))
        paths[name] = dest
    return paths


RECEIPT_FILE = "revision.receipt.json"


def _read_receipt(cache_dir: Path) -> dict[str, Any] | None:
    """The cache's bound source revision, or None when never sealed."""
    path = cache_dir / RECEIPT_FILE
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable cache receipt {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("contract") != CONTRACT_VERSION:
        raise ValueError(f"foreign cache receipt {path}: refusing unbound cache")
    return doc


def _cache_has_files(index_dir: Path, cache_dir: Path) -> bool:
    if any((index_dir / n).exists() for n in INDEX_FILES):
        return True
    raw_dir = cache_dir / "raw"
    return raw_dir.is_dir() and any(raw_dir.iterdir())


def bind_cache(cache_dir: Path, index_dir: Path, revision: str) -> dict[str, Path]:
    """Fetch the pinned index into a revision-bound cache, refusing mix-ups.

    A cache directory is sealed to exactly one ``--source-revision`` by
    ``revision.receipt.json``. A requested revision that differs from the
    receipt, a receipt whose recorded index hashes no longer match the files
    on disk, or pre-existing cache files with no receipt at all all refuse
    loudly: use a fresh ``--cache-dir`` for a new pin. Flat ``raw/`` reuse
    is then sound because every byte in the cache came from the bound pin.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    receipt = _read_receipt(cache_dir)
    if receipt is not None:
        if receipt.get("source_revision") != revision:
            raise ValueError(
                f"cache {cache_dir} is bound to source revision "
                f"{receipt.get('source_revision')}, refusing requested "
                f"{revision}: use a fresh --cache-dir for a new pin")
        if Path(receipt.get("index_dir", "")) != index_dir:
            raise ValueError(
                f"cache {cache_dir} is bound to index dir "
                f"{receipt.get('index_dir')}, refusing {index_dir}")
        paths = fetch_index(index_dir, revision)
        for name, path in paths.items():
            if _sha256_file(path) != (receipt.get("index") or {}).get(name):
                raise ValueError(
                    f"cache index file {path} no longer matches its receipt: "
                    "refusing possibly mixed cache")
        return paths
    if _cache_has_files(index_dir, cache_dir):
        raise ValueError(
            f"cache {cache_dir} holds index/raw files with no revision receipt "
            "(written before cache binding): refusing silent reuse — move them "
            "aside or use a fresh --cache-dir and re-fetch under the pin")
    paths = fetch_index(index_dir, revision)
    index_hashes = {n: _sha256_file(p) for n, p in paths.items()}
    (cache_dir / RECEIPT_FILE).write_text(json.dumps({
        "contract": CONTRACT_VERSION,
        "source_revision": revision,
        "index_dir": index_dir.as_posix(),
        "index": index_hashes,
        "created_utc": datetime.now(UTC).isoformat(),
    }, indent=2))
    return paths


def load_holdout(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"task_ids": set(), "groups": set(), "sha256": None, "path": None}
    doc = json.loads(path.read_text())
    ids = set(doc.get("held_out_task_ids") or doc.get("heldout_task_ids")
              or doc.get("task_ids") or [])
    # Also accept per-task records.
    tasks = doc.get("tasks") or doc.get("held_out_tasks") or []
    for entry in tasks:
        if isinstance(entry, dict) and entry.get("task_id"):
            ids.add(entry["task_id"])
        elif isinstance(entry, str):
            ids.add(entry)
    groups = set(doc.get("held_out_groups") or doc.get("groups") or [])
    return {"task_ids": ids, "groups": groups,
            "sha256": _sha256_file(path), "path": path.as_posix()}


def _eligible(row: dict[str, Any], holdout_ids: set[str]) -> str | None:
    """None when selectable, else the exclusion reason."""
    if row.get("classification") != SERIOUS:
        return f"classification:{row.get('classification')}"
    reward = row.get("reward")
    if not isinstance(reward, (int, float)) or reward < REWARD_MIN:
        return "reward_below_threshold"
    episodes = row.get("episode_count")
    if not isinstance(episodes, int) or episodes < 1 or episodes > MAX_EPISODES:
        return "episode_count_out_of_range"
    if row.get("exploit_blocked_by_verifier"):
        return "exploit_blocked_by_verifier"
    if row.get("attacker_failed_due_to_budget"):
        return "attacker_failed_due_to_budget"
    if row.get("task_id") in holdout_ids:
        return "held_out_eval_task"
    if not row.get("task_id") or not row.get("model") or not row.get("trajectory_label"):
        return "missing_identity"
    return None

def heldout_instruction_hashes(
    holdout_ids: set[str],
    trajectories: list[dict[str, Any]],
    revision: str,
    cache_dir: Path,
) -> set[str]:
    """Instruction hashes of held-out tasks (one raw fetch per task id).

    Catches instruction-level duplicates of held-out tasks under a different
    TW task_id, complementing the exact task_id exclusion. Failures yield an
    empty contribution for that task (recorded by the caller), never a pass.
    """
    hashes: set[str] = set()
    raw_dir = cache_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for task_id in sorted(holdout_ids):
        rows = [r for r in trajectories if r.get("task_id") == task_id]
        if not rows:
            continue
        rows.sort(key=lambda r: (r.get("classification") != SERIOUS,
                                 r.get("model", ""), r.get("trajectory_label", "")))
        row = rows[0]
        cid = conversation_id(row)
        dest = raw_dir / f"{cid}.json"
        try:
            if not (dest.exists() and dest.stat().st_size > 0):
                dest.write_bytes(_get(raw_trajectory_url(row, revision)))
            doc = json.loads(dest.read_bytes())
            steps = doc.get("steps") or []
            first = next((s for s in steps
                          if isinstance(s, dict) and s.get("source") == "user"
                          and isinstance(s.get("message"), str)), None)
            if first is not None:
                hashes.add(hashlib.sha256(first["message"].encode()).hexdigest())
        except Exception:  # noqa: BLE001 - network/JSON failure; skip this task.
            continue
    return hashes


def rank_candidates(
    trajectories: list[dict[str, Any]],
    *,
    seed: int = SEED,
    target: int = TARGET_N,
    holdout_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic ranked selection with dispositions for every candidate.

    Returns (ranked_selected, dispositions): ``ranked_selected`` is the
    ordered take list (target long, caps honored); ``dispositions`` covers
    every index row examined.
    """
    holdout_ids = holdout_ids or set()
    rng = random.Random(seed)
    pool = [r for r in trajectories if _eligible(r, holdout_ids) is None]
    pool.sort(key=lambda r: (r["task_id"], r["model"], r["trajectory_label"]))
    order = pool[:]
    rng.shuffle(order)

    dispositions: list[dict[str, Any]] = []
    for row in trajectories:
        reason = _eligible(row, holdout_ids)
        dispositions.append({
            "conversation_id": conversation_id(row) if row.get("task_id") else None,
            "task_id": row.get("task_id"),
            "model": row.get("model"),
            "trajectory_label": row.get("trajectory_label"),
            "stage": "index",
            "disposition": "candidate" if reason is None else "excluded",
            "reasons": [] if reason is None else [reason],
            "categories": row.get("observed_categories") or [],
            "episode_count": row.get("episode_count"),
        })

    selected: list[dict[str, Any]] = []
    per_task: dict[str, int] = {}
    per_model: dict[str, int] = {}
    have_cats: dict[str, int] = {}

    def take(row: dict[str, Any]) -> bool:
        if len(selected) >= target:
            return False
        if per_task.get(row["task_id"], 0) >= PER_TASK_CAP:
            return False
        if per_model.get(row["model"], 0) >= PER_MODEL_CAP:
            return False
        selected.append(row)
        per_task[row["task_id"]] = per_task.get(row["task_id"], 0) + 1
        per_model[row["model"]] = per_model.get(row["model"], 0) + 1
        for cat in row.get("observed_categories") or []:
            have_cats[cat] = have_cats.get(cat, 0) + 1
        return True

    # Phase A: category floor over the seeded order.
    for row in order:
        if len(selected) >= target:
            break
        cats = row.get("observed_categories") or []
        if any(have_cats.get(c, 0) < CATEGORY_FLOOR for c in cats):
            take(row)
    # Phase B: greedy fill in seeded order.
    for row in order:
        if len(selected) >= target:
            break
        if row in selected:
            continue
        take(row)

    rank_of = {id(r): i for i, r in enumerate(selected)}
    for disp in dispositions:
        if disp["disposition"] != "candidate":
            continue
        match = next((r for r in pool if conversation_id(r) == disp["conversation_id"]
                      and r.get("model") == disp["model"]), None)
        if match is not None and id(match) in rank_of:
            disp["disposition"] = "selected"
            disp["rank"] = rank_of[id(match)]
        else:
            disp["disposition"] = "excluded"
            disp["reasons"] = ["beyond_target_or_cap"]
    return selected, dispositions


def split_analysis_plan(text: str) -> tuple[str, str] | None:
    """Split parsed ``Analysis:``/``Plan:`` prose; None unless unambiguous.

    Requires the message to start with ``Analysis:`` and to contain exactly
    one line-start ``Plan:`` marker, with non-empty prose on both sides.
    Anything else refuses: reasoning is never invented.
    """
    if not text.startswith("Analysis:"):
        return None
    parts = re.split(r"(?m)^Plan:", text)
    if len(parts) != 2:
        return None
    analysis = parts[0][len("Analysis:"):].strip()
    plan = parts[1].strip()
    if not analysis or not plan:
        return None
    return analysis, plan


def render_native_response(
    analysis: str, plan: str, tool_calls: list[dict[str, Any]]
) -> tuple[str, bool]:
    """DERIVED structured-action target: complete native Terminus JSON.

    Returns ``(bare JSON response, task_complete)`` with keys in the stock
    parser's required order. Verbatim prose and verbatim command arguments;
    raises on unknown tools or mistyped arguments. This is a behavior-cloning
    target derived from recorded tool calls, not a raw teacher output.
    """
    commands: list[dict[str, Any]] = []
    complete = False
    for call in tool_calls:
        name = call.get("function_name")
        args = call.get("arguments") or {}
        if name == "bash_command":
            keys = args.get("keystrokes")
            dur = args.get("duration", 1.0)
            if not isinstance(keys, str) or not keys:
                raise ValueError("bash_command without string keystrokes")
            if not isinstance(dur, (int, float)):
                raise ValueError("bash_command without numeric duration")
            commands.append({"keystrokes": keys, "duration": dur})
        elif name == "mark_task_complete":
            complete = True
        else:
            raise ValueError(f"unknown tool call: {name!r}")
    payload = {"analysis": analysis, "plan": plan,
               "commands": commands, "task_complete": complete}
    return json.dumps(payload), complete


def _stock_parser_path() -> Path:
    """Locate the installed stock parser file (never imports harbor)."""
    for base in sys.path:
        candidate = Path(base) / STOCK_PARSER_REL
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"stock parser not found on sys.path: {STOCK_PARSER_REL}")


_STOCK_PARSER: Any = None
_STOCK_PARSER_PATH: Path | None = None


def stock_parser() -> Any:
    """The actual installed Harbor stock JSON parser (Harbor 0.24.0)."""
    global _STOCK_PARSER, _STOCK_PARSER_PATH
    if _STOCK_PARSER is None:
        import importlib.util
        _STOCK_PARSER_PATH = _stock_parser_path()
        spec = importlib.util.spec_from_file_location(
            "auditor_stock_parser", _STOCK_PARSER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STOCK_PARSER = module.TerminusJSONPlainParser()
    return _STOCK_PARSER


def gate_native_response(content: str, analysis: str, plan: str,
                         commands: list[dict[str, Any]], complete: bool) -> str | None:
    """Stock-parser acceptance; None when the turn is exactly valid."""
    result = stock_parser().parse_response(content)
    if result.error:
        return f"stock_parser_error:{result.error[:120]}"
    if result.analysis != analysis or result.plan != plan:
        return "stock_parser_prose_mismatch"
    if result.is_task_complete != complete:
        return "stock_parser_completion_mismatch"
    got = [(c.keystrokes, c.duration) for c in result.commands]
    want = [(c["keystrokes"], float(c["duration"])) for c in commands]
    if got != want:
        return "stock_parser_command_mismatch"
    return None


def _obs_text(step: dict[str, Any]) -> tuple[str | None, bool]:
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    texts: list[str] = []
    for result in results or []:
        if not isinstance(result, dict):
            return None, True
        content = result.get("content")
        if isinstance(content, str):
            if content:
                texts.append(content)
        elif content is None:
            continue
        else:
            return None, True
    return ("\n".join(texts) if texts else None), False


def convert_trajectory(doc: dict[str, Any]) -> tuple[list[dict[str, str]] | None, str | None, dict[str, Any]]:
    """Raw ATIF -> chat messages. Returns (messages, refusal_reason, flags).

    Mirrors ``sft_terminus.convert_segment`` for user/system/observation
    handling (verbatim text, context-management markers skipped, final
    observation dropped). Every assistant turn is a DERIVED structured-action
    target: complete valid native Terminus JSON built from the verbatim split
    Analysis/Plan prose and verbatim recorded tool calls, gated through the
    actual installed stock parser (see module docstring).
    """
    flags: dict[str, Any] = {"reasoning_dropped": 0, "system_markers": 0,
                             "assistant_turns": 0, "stock_gated_turns": 0,
                             "assistant_step_ids": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        return None, "not_atif_steps", flags
    agent = doc.get("agent") or {}
    name = agent.get("name") if isinstance(agent, dict) else None
    if not isinstance(name, str) or name.strip().lower().replace("_", "-") not in TERMINUS_NAMES:
        return None, "not_terminus_2", flags
    steps = doc["steps"]
    if any(not isinstance(s, dict) or s.get("is_copied_context") for s in steps):
        return None, "copied_or_malformed_context", flags

    # Validity oracle: reuse the existing faithful boundary where valid.
    try:
        oracle = convert_segment(steps, keep_reasoning=False)
    except Exception as exc:  # noqa: BLE001 - any oracle failure refuses.
        return None, f"oracle_rejected:{type(exc).__name__}", flags
    if oracle.multimodal:
        return None, "multimodal_content", flags

    try:
        stock_parser()
    except Exception as exc:  # noqa: BLE001 - no silent ungated export.
        return None, f"stock_parser_unavailable:{type(exc).__name__}", flags

    items: list[Any] = []
    agent_positions: list[int] = []
    for step in steps:
        source = step.get("source")
        if source in ("user", "system"):
            message = step.get("message")
            if not isinstance(message, str):
                return None, "non_string_instruction", flags
            if source == "system":
                extra = step.get("extra")
                if (isinstance(extra, dict) and extra.get("context_management")) or not message:
                    flags["system_markers"] += 1
                    continue
            if not message:
                return None, "empty_instruction", flags
            items.append({"role": source, "content": message})
        elif source == "agent":
            text = step.get("message")
            if not isinstance(text, str) or not text:
                return None, "empty_assistant_text", flags
            reasoning = step.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                flags["reasoning_dropped"] += 1
            split = split_analysis_plan(text)
            if split is None:
                return None, "ambiguous_analysis_plan_markers", flags
            analysis, plan = split
            tool_calls = step.get("tool_calls") or []
            try:
                content, _complete = render_native_response(analysis, plan, tool_calls)
            except ValueError as exc:
                return None, f"tool_render:{exc}", flags
            commands = json.loads(content)["commands"]
            gate = gate_native_response(content, analysis, plan, commands, _complete)
            if gate is not None:
                return None, gate, flags
            flags["assistant_turns"] += 1
            flags["stock_gated_turns"] += 1
            flags["assistant_step_ids"].append(
                step.get("step_id") if isinstance(step.get("step_id"), int) else None)
            block_items: list[dict[str, str]] = [{"role": "assistant", "content": content}]
            obs, bad = _obs_text(step)
            if bad:
                return None, "non_string_observation", flags
            if obs:
                block_items.append({"role": "user", "content": obs})
            agent_positions.append(len(items))
            items.append(block_items)
        else:
            return None, f"unknown_step_source:{source}", flags

    if not agent_positions:
        return None, "no_agent_turns", flags
    # The final agent step's observation was never sent back to the model.
    items[agent_positions[-1]] = items[agent_positions[-1]][:1]
    messages: list[dict[str, str]] = []
    for item in items:
        messages.extend(item if isinstance(item, list) else [item])

    # Alignment with the oracle: same turn count and roles; user turns byte
    # identical (assistant turns are the derived JSON target, not raw text).
    if len(messages) != len(oracle.messages):
        return None, "oracle_turn_mismatch", flags
    for mine, ref in zip(messages, oracle.messages, strict=True):
        if mine["role"] != ref["role"]:
            return None, "oracle_role_mismatch", flags
        if mine["role"] != "assistant" and mine["content"] != ref["content"]:
            return None, "oracle_text_diverged", flags
    for message in messages:
        if set(message) != {"role", "content"} or message["role"] not in ("system", "user", "assistant"):
            return None, "bad_message_shape", flags
        if not isinstance(message["content"], str):
            return None, "bad_message_shape", flags
    return messages, None, flags


def instruction_sha256(messages: list[dict[str, str]]) -> str | None:
    for message in messages:
        if message["role"] == "user":
            return hashlib.sha256(message["content"].encode()).hexdigest()
    return None


def assign_split(
    groups: list[str], *, seed: int = SEED, val_fraction: float = VAL_FRACTION
) -> dict[str, str]:
    """Sealed group-disjoint train/val assignment (whole groups to val)."""
    ordered = sorted(set(groups))
    if val_fraction <= 0:
        # No dev split: the frozen eval tasks are the held-out set, so every
        # history trains.
        return {g: "train" for g in ordered}
    rng = random.Random(seed)
    rng.shuffle(ordered)
    n_val = max(1, round(len(ordered) * val_fraction))
    val = set(ordered[:n_val])
    return {g: ("val" if g in val else "train") for g in ordered}


def _merge_groups(task_ids: list[str], instr_hashes: list[str | None]) -> dict[str, str]:
    """Union task groups sharing an instruction hash (conservative merge)."""
    parent = {t: t for t in task_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: dict[str, str] = {}
    for task, h in zip(task_ids, instr_hashes, strict=True):
        if h is None:
            continue
        if h in seen:
            a, b = find(task), find(seen[h])
            parent[a] = b
        else:
            seen[h] = task
    return {t: group_key(find(t)) for t in task_ids}


def example_id(history_id: str, assistant_ordinal: int) -> str:
    """Deterministic supervised-example id for one assistant turn."""
    return f"{history_id}__a{assistant_ordinal:02d}"


def expand_prefixes(
    history_id: str, messages: list[dict[str, str]]
) -> list[tuple[str, list[dict[str, str]]]]:
    """One prefix per assistant turn, each ending exactly at that turn.

    No extra turns, no truncation: prefix k is messages[0..i] where messages[i]
    is the k-th assistant turn. Built for train_on_what=last_assistant_message
    (only the final assistant turn of each prefix is supervised, so earlier
    turns are never trained on incorrect generation prefixes).
    """
    out: list[tuple[str, list[dict[str, str]]]] = []
    ordinal = 0
    for i, message in enumerate(messages):
        if message["role"] == "assistant":
            out.append((example_id(history_id, ordinal), messages[:i + 1]))
            ordinal += 1
    return out


def cmd_prepare(args: argparse.Namespace) -> int:
    revision = args.source_revision
    index_dir: Path = args.index_dir
    cache_dir: Path = args.cache_dir
    out_dir: Path = args.out
    raw_dir = cache_dir / "raw"

    try:
        index_paths = bind_cache(cache_dir, index_dir, revision)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    index_hashes = {n: _sha256_file(index_paths[n]) for n in INDEX_FILES}
    trajectories = json.loads((index_dir / "trajectories.json").read_text())
    holdout = load_holdout(args.holdout)

    selected, dispositions = rank_candidates(
        trajectories, seed=args.seed, target=args.target,
        holdout_ids=holdout["task_ids"],
    )
    # Instruction-level duplicates of held-out tasks under another task_id.
    ho_instr = heldout_instruction_hashes(
        holdout["task_ids"], trajectories, revision, cache_dir)
    holdout["instruction_hashes"] = sorted(ho_instr)
    by_conv = {d["conversation_id"]: d for d in dispositions
               if d["conversation_id"]}

    failed: list[dict[str, Any]] = []
    if args.dry_run:
        (out_dir).mkdir(parents=True, exist_ok=True)
        (out_dir / RANKING_FILE).write_text(json.dumps(
            [{"conversation_id": conversation_id(r), "task_id": r["task_id"],
              "model": r["model"], "trajectory_label": r["trajectory_label"],
              "episode_count": r.get("episode_count"),
              "categories": r.get("observed_categories") or []} for r in selected],
            indent=2))
        print(json.dumps({"dry_run": True, "candidates": len(selected),
                          "index_total": len(trajectories)}, indent=2))
        return 0

    if out_dir.exists() and any(out_dir.iterdir()):
        print(f"error: --out must be a new/empty directory: {out_dir}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Download in rank order; conversion refusals/failures backfill from the
    # ranked standby tail so the corpus stays at target when the pool allows.
    queue = list(selected)
    standby = [r for r in
               sorted([r for r in trajectories
                       if _eligible(r, holdout["task_ids"]) is None],
                      key=lambda r: (r["task_id"], r["model"], r["trajectory_label"]))
               if r not in queue]
    rng = random.Random(args.seed + 1)
    rng.shuffle(standby)
    converted: list[dict[str, Any]] = []
    seen_tasks: dict[str, int] = {}
    seen_models: dict[str, int] = {}
    fetched = 0
    fetch_failures = 0

    def budget_ok(row: dict[str, Any]) -> bool:
        return (seen_tasks.get(row["task_id"], 0) < PER_TASK_CAP
                and seen_models.get(row["model"], 0) < PER_MODEL_CAP)

    idx = 0
    while len(converted) < args.target and idx < len(queue):
        row = queue[idx]
        idx += 1
        if not budget_ok(row):
            continue
        cid = conversation_id(row)
        dest = raw_dir / f"{cid}.json"
        try:
            if not (dest.exists() and dest.stat().st_size > 0):
                dest.write_bytes(_get(raw_trajectory_url(row, revision)))
                fetched += 1
            raw_bytes = dest.read_bytes()
            doc = json.loads(raw_bytes)
        except Exception as exc:  # noqa: BLE001 - network/JSON failure.
            fetch_failures += 1
            failed.append({"conversation_id": cid, "stage": "fetch",
                           "reason": f"{type(exc).__name__}"})
            queue.extend([r for r in standby
                          if r not in queue and budget_ok(r)][:1])
            continue
        messages, refusal, flags = convert_trajectory(doc)
        disp = by_conv.get(cid, {})
        if refusal is not None:
            disp.update({"disposition": "excluded", "stage": "convert",
                         "reasons": (disp.get("reasons") or []) + [refusal]})
            queue.extend([r for r in standby
                          if r not in queue and budget_ok(r)][:1])
            continue
        assert messages is not None
        instr_hash = instruction_sha256(messages)
        if instr_hash in ho_instr:
            disp.update({"disposition": "excluded", "stage": "convert",
                         "reasons": (disp.get("reasons") or [])
                         + ["held_out_instruction_group"]})
            queue.extend([r for r in standby
                          if r not in queue and budget_ok(r)][:1])
            continue
        seen_tasks[row["task_id"]] = seen_tasks.get(row["task_id"], 0) + 1
        seen_models[row["model"]] = seen_models.get(row["model"], 0) + 1
        disp.update({"disposition": "selected", "stage": "convert",
                     "reasons": [], "n_messages": len(messages),
                     "n_assistant_turns": flags["assistant_turns"]})
        converted.append({
            "id": cid, "row": row, "messages": messages,
            "raw_sha256": _sha256_bytes(raw_bytes),
            "raw_bytes": len(raw_bytes),
            "instruction_sha256": instr_hash,
            "flags": flags,
            "url": raw_trajectory_url(row, revision),
        })

    if len(converted) < args.target:
        print(f"error: only {len(converted)}/{args.target} histories converted; "
              f"fetch_failures={fetch_failures}")
        return 1

    merged = _merge_groups(
        [c["row"]["task_id"] for c in converted],
        [c["instruction_sha256"] for c in converted],
    )
    for c in converted:
        c["group"] = merged[c["row"]["task_id"]]
    split = assign_split([c["group"] for c in converted], seed=args.seed,
                         val_fraction=args.val_fraction)
    for c in converted:
        c["split"] = split[c["group"]]

    hist_path = out_dir / HISTORIES_FILE
    with open(hist_path, "w") as fh:
        for c in converted:
            fh.write(json.dumps({"history_id": c["id"],
                                 "messages": c["messages"]}) + "\n")
    histories_sha256 = _sha256_file(hist_path)

    # Supervised export: one prefix ending at EACH recorded assistant turn.
    # Split stays at history/task level: every prefix inherits its history's
    # split and group, so no history is ever shared across splits.
    examples: list[dict[str, Any]] = []
    for c in converted:
        step_ids = c["flags"]["assistant_step_ids"]
        for ordinal, (eid, prefix) in enumerate(expand_prefixes(c["id"], c["messages"])):
            examples.append({
                "example_id": eid, "history_id": c["id"],
                "assistant_ordinal": ordinal,
                "atif_step_id": step_ids[ordinal] if ordinal < len(step_ids) else None,
                "split": c["split"], "group": c["group"],
                "prefix_messages": len(prefix),
            })
    conv_path = out_dir / CONVERSATIONS_FILE
    with open(conv_path, "w") as fh:
        for c in converted:
            for _eid, prefix in expand_prefixes(c["id"], c["messages"]):
                fh.write(json.dumps({"messages": prefix}) + "\n")
    conversations_sha256 = _sha256_file(conv_path)
    with open(out_dir / EXAMPLES_FILE, "w") as fh:
        for e in examples:
            fh.write(json.dumps(e) + "\n")

    # Native-compat gate: the existing offline Tinker loader must accept every
    # prefix row, and every row must end at its supervised assistant turn
    # (train_on_what=last_assistant_message). Import is local-only, no spend.
    try:
        from evallab.sft_tinker import load_conversations
        loaded = load_conversations(conv_path)
        assert len(loaded) == len(examples)
        assert all(row and row[-1]["role"] == "assistant" for row in loaded)
    except Exception as exc:  # noqa: BLE001 - gate failure aborts.
        print(f"error: native-compat gate failed: {exc}")
        return 1

    prov_path = out_dir / PROVENANCE_FILE
    with open(prov_path, "w") as fh:
        for c in converted:
            row = c["row"]
            fh.write(json.dumps({
                "conversation_id": c["id"], "split": c["split"],
                "group": c["group"], "tw_task_id": row["task_id"],
                "tw_model": row["model"],
                "tw_trajectory_label": row["trajectory_label"],
                "tw_source_dataset": row.get("source_dataset"),
                "tw_tree": "hack_trajectories", "tw_variant": "raw",
                "source_repo": TW_REPO, "source_revision": revision,
                "trajectory_url": c["url"], "raw_sha256": c["raw_sha256"],
                "raw_bytes": c["raw_bytes"],
                "transform": TRANSFORM_VERSION,
                "transform_note": "assistant turns are DERIVED native Terminus "
                "JSON from verbatim split prose + verbatim tool_calls, gated "
                "through the stock parser; not raw teacher outputs",
                "instruction_sha256": c["instruction_sha256"],
                "index_classification": row.get("classification"),
                "index_classification_source": row.get("classification_source"),
                "index_reward": row.get("reward"),
                "observed_categories": row.get("observed_categories") or [],
                "episode_count": row.get("episode_count"),
                "n_messages": len(c["messages"]),
                "n_assistant_turns": c["flags"]["assistant_turns"],
                "reasoning_dropped": c["flags"]["reasoning_dropped"],
            }) + "\n")

    with open(out_dir / DISPOSITIONS_FILE, "w") as fh:
        for d in dispositions:
            fh.write(json.dumps(d) + "\n")
        for f in failed:
            fh.write(json.dumps(f) + "\n")

    split_doc = {
        "contract": CONTRACT_VERSION + ".split/1",
        "seed": args.seed,
        "val_fraction": args.val_fraction,
        "level": "history",
        "group_assignment": split,
        "histories": {c["id"]: c["split"] for c in converted},
        "examples": {e["example_id"]: e["split"] for e in examples},
        "holdout_task_ids": sorted(holdout["task_ids"]),
    }
    (out_dir / SPLIT_FILE).write_text(json.dumps(split_doc, indent=2))

    char_total = sum(len(m["content"]) for c in converted for m in c["messages"])
    manifest = {
        "contract": CONTRACT_VERSION,
        "created_utc": datetime.now(UTC).isoformat(),
        "source": {
            "repo": TW_REPO, "revision": revision,
            "index": {n: index_hashes[n] for n in INDEX_FILES},
            "variant": "raw", "tree": "hack_trajectories",
        },
        "selector": {
            "seed": args.seed, "target": args.target,
            "max_episodes": MAX_EPISODES, "per_task_cap": PER_TASK_CAP,
            "per_model_cap": PER_MODEL_CAP, "category_floor": CATEGORY_FLOOR,
            "classification": SERIOUS, "reward_min": REWARD_MIN,
        },
        "holdout": {
            "path": holdout["path"], "sha256": holdout["sha256"],
            "excluded_task_ids": sorted(holdout["task_ids"]),
            "excluded_instruction_hashes": holdout.get("instruction_hashes") or [],
        },
        "split": {"seed": args.seed, "val_fraction": args.val_fraction,
                  "level": "history",
                  "train_histories": sum(1 for c in converted if c["split"] == "train"),
                  "val_histories": sum(1 for c in converted if c["split"] == "val"),
                  "train_examples": sum(1 for e in examples if e["split"] == "train"),
                  "val_examples": sum(1 for e in examples if e["split"] == "val")},
        "counts": {
            "histories": len(converted),
            "supervised_examples": len(examples),
            "distinct_tasks": len({c["row"]["task_id"] for c in converted}),
            "distinct_groups": len({c["group"] for c in converted}),
            "by_model": {m: sum(1 for c in converted if c["row"]["model"] == m)
                         for m in sorted({c["row"]["model"] for c in converted})},
            "by_category": {},
            "fetch_downloaded": fetched, "fetch_failures": fetch_failures,
        },
        "chars_total_histories": char_total,
        "histories_sha256": histories_sha256,
        "conversations_sha256": conversations_sha256,
        "keep_reasoning": False,
        "assistant_transform": TRANSFORM_VERSION,
        "train_on_what": "last_assistant_message",
        "stock_parser": {
            "path": (_STOCK_PARSER_PATH or _stock_parser_path()).as_posix(),
            "sha256": _sha256_file(_STOCK_PARSER_PATH or _stock_parser_path()),
            "gated_assistant_turns": sum(c["flags"]["stock_gated_turns"]
                                        for c in converted),
        },
        "fidelity_limits": FIDELITY_LIMITS,
        "masking_note": ("conversations.jsonl holds one exact prefix per recorded "
                         "assistant turn (no extra turns, no truncation), built for "
                         "TrainOnWhat last_assistant_message: only each row's final "
                         "assistant turn is supervised, so earlier turns are never "
                         "trained on incorrect generation prefixes "
                         "(qwen3_5_disable_thinking.has_extension_property=False). "
                         "Train cost is billed on ALL rendered sequence tokens; the "
                         "parent measures exact billable tokens offline (free, no "
                         "spend) with the real isolated entrypoint: "
                         "uv run --project tools/tinker-sft --locked python "
                         "tools/tinker-sft/measure.py --conversations "
                         "<out>/conversations.jsonl --model Qwen/Qwen3.6-35B-A3B "
                         "--renderer qwen3_5_disable_thinking --max-length 65536 "
                         "--train-on-what last_assistant_message"),
    }
    cats: dict[str, int] = {}
    datasets: dict[str, int] = {}
    for c in converted:
        for cat in c["row"].get("observed_categories") or []:
            cats[cat] = cats.get(cat, 0) + 1
        ds = c["row"].get("source_dataset") or "unknown"
        datasets[ds] = datasets.get(ds, 0) + 1
    manifest["counts"]["by_category"] = dict(sorted(cats.items()))
    manifest["counts"]["by_source_dataset"] = dict(sorted(datasets.items()))
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))

    print(json.dumps({
        "out": out_dir.as_posix(),
        "histories": len(converted),
        "supervised_examples": len(examples),
        "train_histories": manifest["split"]["train_histories"],
        "val_histories": manifest["split"]["val_histories"],
        "distinct_tasks": manifest["counts"]["distinct_tasks"],
        "by_model": manifest["counts"]["by_model"],
        "by_category": manifest["counts"]["by_category"],
        "histories_sha256": histories_sha256,
        "conversations_sha256": conversations_sha256,
        "fetch_downloaded": fetched, "fetch_failures": fetch_failures,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="select, fetch, convert, and seal the corpus")
    prep.add_argument("--index-dir", type=Path, required=True)
    prep.add_argument("--cache-dir", type=Path, required=True)
    prep.add_argument("--out", type=Path, required=True)
    prep.add_argument("--holdout", type=Path, default=None)
    prep.add_argument("--source-revision", default=SOURCE_REVISION)
    prep.add_argument("--seed", type=int, default=SEED)
    prep.add_argument("--target", type=int, default=TARGET_N)
    prep.add_argument("--val-fraction", type=float, default=VAL_FRACTION)
    prep.add_argument("--dry-run", action="store_true")
    prep.set_defaults(func=cmd_prepare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
