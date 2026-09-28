"""Tinker chat_sl SFT launcher for exported Terminus-2 conversations (HAR-81).

A thin, spend-guarded Eval Lab front end to ``tinker-cookbook``'s
``recipes.chat_sl.train`` over a :mod:`evallab.sft_terminus` export
directory (``conversations.jsonl`` + ``manifest.json``).

Eval Lab never imports tinker or tinker-cookbook: the toolchain lives in the
isolated, locked uv project ``tools/tinker-sft`` (own ``uv.lock``, never a
root workspace member), and both code paths shell out to it through
``uv run --project tools/tinker-sft --locked``:

- ``dry-run`` is offline and free: it runs ``tools/tinker-sft/measure.py``,
  which renders every conversation with the chosen tinker-cookbook renderer
  and the model's Hugging Face tokenizer (tokenizer files only, never
  weights) and prints token statistics as one JSON object. The launcher
  turns that into the report with truncation at ``max_length`` and a cost
  estimate from the pinned Tinker train price table.
- ``train`` refuses to run without ``--confirm-spend``; with it, it invokes
  the pinned chat_sl trainer in the same isolated project with recorded
  hyperparameters and writes a training manifest linking the data digest to
  the Tinker run and final ``sampler_path`` read from the log dir's
  ``checkpoints.jsonl``.

Credentials are referenced by environment variable name only
(``TINKER_API_KEY``); values are never read, logged, or written.

chat_sl 0.5.7 entrypoint note: ``chz.entrypoint`` parses ``key=value``
arguments with underscore field names (``dataset=... model_name=...``);
``--flag`` style is rejected by chz, so the launcher always emits the
verified ``key=value`` form.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.tracing import TraceError

CONTRACT_VERSION = "evallab.sft_tinker/1"
TRAINING_MANIFEST_FILE = "training-manifest.json"
CONVERSATIONS_FILE = "conversations.jsonl"
EXPORT_MANIFEST_FILE = "manifest.json"
CHECKPOINTS_FILE = "checkpoints.jsonl"
CHAT_SL_MODULE = "tinker_cookbook.recipes.chat_sl.train"
TINKER_API_KEY_ENV = "TINKER_API_KEY"
TRAIN_ON_WHAT = "all_assistant_messages"
ALLOWED_ROLES = frozenset({"system", "user", "assistant"})

#: Isolated, locked toolchain project (tinker==0.30.4,
#: tinker-cookbook==0.5.7). Never imported; always invoked by subprocess.
PROJECT_DIR = Path(__file__).resolve().parents[2] / "tools" / "tinker-sft"
MEASURE_SCRIPT = PROJECT_DIR / "measure.py"

#: Tinker train prices, USD per 1M train tokens (HAR-81 brief, 2026-09-28).
TRAIN_PRICE_PER_MTOKEN_USD: dict[str, float] = {
    "Qwen/Qwen3.6-35B-A3B": 1.177,
    "Qwen/Qwen3.8-27B": 4.103,
    "Qwen/Qwen3.5-9B": 1.463,
}

#: Protocol decision: the student runs with thinking disabled, so the
#: default renderer per base model is the matching ``*_disable_thinking``
#: renderer from tinker-cookbook 0.5.7.
DEFAULT_RENDERER: dict[str, str] = {
    "Qwen/Qwen3.6-35B-A3B": "qwen3_5_disable_thinking",
    "Qwen/Qwen3.5-9B": "qwen3_5_disable_thinking",
    "Qwen/Qwen3.8-27B": "qwen3_8_disable_thinking",
}

#: chat_sl 0.5.7 defaults (tinker-cookbook CLIConfig), recorded in every
#: training manifest so a run is reproducible from the manifest alone.
DEFAULT_TRAINER_CONFIG = {
    "learning_rate": 1e-4,
    "lora_rank": 32,
    "batch_size": 256,
    "num_epochs": 1,
    "max_length": 16384,
}

_TINKER_RUN_RE = re.compile(r"^tinker://([^:]+):")


def _require_price(model: str) -> float:
    price = TRAIN_PRICE_PER_MTOKEN_USD.get(model)
    if price is None:
        raise TraceError(
            f"no pinned Tinker train price for {model!r}; known models: "
            + ", ".join(sorted(TRAIN_PRICE_PER_MTOKEN_USD))
        )
    return price


def default_renderer_for(model: str) -> str:
    renderer = DEFAULT_RENDERER.get(model)
    if renderer is None:
        raise TraceError(
            f"no default renderer for {model!r}; pass --renderer explicitly "
            "(known defaults: " + ", ".join(f"{k}={v}" for k, v in sorted(DEFAULT_RENDERER.items())) + ")"
        )
    return renderer


# ---------------------------------------------------------------------------
# Data loading and validation
# ---------------------------------------------------------------------------


def load_conversations(path: Path) -> list[list[dict[str, Any]]]:
    """Load and strictly validate chat_sl conversation rows."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise TraceError(f"cannot read {path}: {exc}") from exc
    conversations: list[list[dict[str, Any]]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise TraceError(f"{path}:{line_number} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or set(row) != {"messages"}:
            raise TraceError(
                f"{path}:{line_number} must be an object with exactly a 'messages' key"
            )
        messages = row["messages"]
        if not isinstance(messages, list) or not messages:
            raise TraceError(f"{path}:{line_number} 'messages' must be a non-empty list")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise TraceError(
                    f"{path}:{line_number} messages must have exactly role/content keys"
                )
            if message["role"] not in ALLOWED_ROLES or not isinstance(message["content"], str):
                raise TraceError(
                    f"{path}:{line_number} message has unsupported role or non-string content"
                )
        conversations.append(messages)
    if not conversations:
        raise TraceError(f"{path} contains no conversations")
    return conversations


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def verify_export_dir(data_dir: Path) -> dict[str, Any]:
    """Load the export manifest and re-verify the conversations digest."""
    manifest_path = data_dir / EXPORT_MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise TraceError(f"cannot read {manifest_path}: {exc}") from exc
    expected = manifest.get("conversations_sha256")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise TraceError(f"{manifest_path} lacks a sha256 conversations_sha256")
    actual = _sha256_file(data_dir / CONVERSATIONS_FILE)
    if actual != expected:
        raise TraceError(
            f"digest mismatch: manifest pins {expected} but "
            f"{data_dir / CONVERSATIONS_FILE} hashes to {actual}"
        )
    return manifest


# ---------------------------------------------------------------------------
# Isolated toolchain project (subprocess only; never imported)
# ---------------------------------------------------------------------------


def _uv_executable() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise TraceError(
            "uv executable not found on PATH; it is required to run the "
            f"isolated toolchain project at {PROJECT_DIR}"
        )
    return uv


def measure_command(
    conversations_path: Path,
    *,
    model: str,
    renderer_name: str,
    max_length: int,
    project_dir: Path = PROJECT_DIR,
) -> list[str]:
    """Offline render-measurement invocation inside the isolated project."""
    return [
        _uv_executable(),
        "run",
        "--project",
        project_dir.resolve().as_posix(),
        "--locked",
        "python",
        (project_dir / "measure.py").resolve().as_posix(),
        "--conversations",
        conversations_path.resolve().as_posix(),
        "--model",
        model,
        "--renderer",
        renderer_name,
        "--max-length",
        str(int(max_length)),
        "--train-on-what",
        TRAIN_ON_WHAT,
    ]


def training_command(
    dataset_path: Path,
    *,
    model: str,
    renderer_name: str,
    learning_rate: float,
    lora_rank: int,
    batch_size: int,
    num_epochs: int,
    max_length: int,
    log_dir: Path,
    project_dir: Path = PROJECT_DIR,
) -> list[str]:
    """The pinned chat_sl trainer invocation (chz ``key=value`` arguments)."""
    return [
        _uv_executable(),
        "run",
        "--project",
        project_dir.resolve().as_posix(),
        "--locked",
        "python",
        "-m",
        CHAT_SL_MODULE,
        f"dataset={dataset_path.resolve().as_posix()}",
        f"model_name={model}",
        f"renderer_name={renderer_name}",
        f"train_on_what={TRAIN_ON_WHAT}",
        f"learning_rate={float(learning_rate)!r}",
        f"lora_rank={int(lora_rank)}",
        f"batch_size={int(batch_size)}",
        f"num_epochs={int(num_epochs)}",
        f"max_length={int(max_length)}",
        f"log_path={log_dir.resolve().as_posix()}",
        "behavior_if_log_dir_exists=raise",
    ]


#: A measure runner maps an argv to ``(returncode, stdout)``. Injectable so
#: tests exercise parsing and gating offline without uv or the toolchain.
MeasureRunner = Callable[[list[str]], "tuple[int, str]"]


def _subprocess_measure_runner(argv: list[str]) -> tuple[int, str]:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout


@dataclass
class RenderStats:
    conversations: int
    total_tokens: int
    total_tokens_after_truncation: int
    supervised_tokens_after_truncation: float
    truncated_conversations: int
    longest_conversation_tokens: int
    median_conversation_tokens: float
    renderer_extension_property: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "conversations": self.conversations,
            "total_tokens": self.total_tokens,
            "total_tokens_after_truncation": self.total_tokens_after_truncation,
            "supervised_tokens_after_truncation": round(
                self.supervised_tokens_after_truncation, 1
            ),
            "truncated_conversations": self.truncated_conversations,
            "longest_conversation_tokens": self.longest_conversation_tokens,
            "median_conversation_tokens": self.median_conversation_tokens,
            "renderer_extension_property": self.renderer_extension_property,
        }


_STAT_FIELDS = (
    "conversations",
    "total_tokens",
    "total_tokens_after_truncation",
    "supervised_tokens_after_truncation",
    "truncated_conversations",
    "longest_conversation_tokens",
    "median_conversation_tokens",
)

def run_measure(
    conversations_path: Path,
    *,
    model: str,
    renderer_name: str,
    max_length: int,
    project_dir: Path = PROJECT_DIR,
    measure_runner: MeasureRunner | None = None,
) -> RenderStats:
    """Run the isolated measure script and parse its JSON statistics."""
    argv = measure_command(
        conversations_path,
        model=model,
        renderer_name=renderer_name,
        max_length=max_length,
        project_dir=project_dir,
    )
    execute = measure_runner or _subprocess_measure_runner
    returncode, stdout = execute(argv)
    if returncode != 0:
        raise TraceError(
            f"measure script failed with exit code {returncode}: {' '.join(argv)}"
        )
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise TraceError(f"measure script stdout is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or any(field not in payload for field in _STAT_FIELDS):
        raise TraceError("measure script stdout lacks the expected statistic fields")
    try:
        return RenderStats(
            conversations=int(payload["conversations"]),
            total_tokens=int(payload["total_tokens"]),
            total_tokens_after_truncation=int(payload["total_tokens_after_truncation"]),
            supervised_tokens_after_truncation=float(payload["supervised_tokens_after_truncation"]),
            truncated_conversations=int(payload["truncated_conversations"]),
            longest_conversation_tokens=int(payload["longest_conversation_tokens"]),
            median_conversation_tokens=float(payload["median_conversation_tokens"]),
            renderer_extension_property=payload.get("renderer_extension_property"),
        )
    except (TypeError, ValueError) as exc:
        raise TraceError(f"measure script statistics have wrong types: {exc}") from exc


def estimate_cost_usd(total_tokens: int, model: str, epochs: int) -> float:
    """Tinker train cost estimate: tokens x epochs x pinned price."""
    price = _require_price(model)
    return total_tokens * epochs * price / 1_000_000


def dry_run(
    data_dir: Path,
    *,
    model: str,
    renderer_name: str | None,
    max_length: int,
    epochs: int,
    project_dir: Path = PROJECT_DIR,
    measure_runner: MeasureRunner | None = None,
) -> dict[str, Any]:
    """Offline render + cost report. Never contacts the Tinker service."""
    manifest = verify_export_dir(data_dir)
    conversations = load_conversations(data_dir / CONVERSATIONS_FILE)
    renderer_name = renderer_name or default_renderer_for(model)
    stats = run_measure(
        data_dir / CONVERSATIONS_FILE,
        model=model,
        renderer_name=renderer_name,
        max_length=max_length,
        project_dir=project_dir,
        measure_runner=measure_runner,
    )
    if stats.conversations != len(conversations):
        raise TraceError(
            f"measure script counted {stats.conversations} conversations but "
            f"{data_dir / CONVERSATIONS_FILE} holds {len(conversations)}"
        )
    price = _require_price(model)
    return {
        "contract": CONTRACT_VERSION,
        "mode": "dry-run",
        "data_dir": data_dir.as_posix(),
        "conversations_sha256": manifest["conversations_sha256"],
        "split_manifest_digest": (manifest.get("split_manifest") or {}).get("manifest_digest"),
        "model": model,
        "renderer": renderer_name,
        "train_on_what": TRAIN_ON_WHAT,
        "max_length": max_length,
        "epochs": epochs,
        "price_per_mtoken_usd": price,
        "cost_formula": "total_tokens_after_truncation * epochs * price_per_mtoken / 1e6",
        "render": stats.to_json(),
        "estimated_cost_usd": round(estimate_cost_usd(stats.total_tokens_after_truncation, model, epochs), 2),
        "notes": _dry_run_notes(stats),
    }


def _dry_run_notes(stats: RenderStats) -> list[str]:
    """Machine-visible caveats the renderer itself would only log."""
    if stats.renderer_extension_property is False:
        return [
            "renderer lacks the sequence-extension property: with "
            f"train_on_what={TRAIN_ON_WHAT}, earlier assistant turns train on "
            "token prefixes that differ from their generation-time prompts; "
            "tinker-cookbook recommends per-assistant-message conversations "
            "with last_assistant_message instead (protocol currently pins "
            f"{TRAIN_ON_WHAT})"
        ]
    return []


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def read_final_sampler_path(log_dir: Path) -> dict[str, Any]:
    """Final checkpoint record from ``checkpoints.jsonl`` (fails closed)."""
    path = log_dir / CHECKPOINTS_FILE
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise TraceError(f"cannot read {path}: {exc}") from exc
    final = [row for row in rows if isinstance(row, dict) and row.get("final")]
    chosen = final[-1] if final else (rows[-1] if rows else None)
    if not isinstance(chosen, dict) or not chosen.get("sampler_path"):
        raise TraceError(f"{path} has no checkpoint with a sampler_path")
    return chosen


def run_training(
    data_dir: Path,
    *,
    model: str,
    renderer_name: str | None,
    log_dir: Path,
    learning_rate: float,
    lora_rank: int,
    batch_size: int,
    num_epochs: int,
    max_length: int,
    confirm_spend: bool,
    manifest_out: Path | None = None,
    project_dir: Path = PROJECT_DIR,
    runner: Callable[[list[str]], int] | None = None,
) -> dict[str, Any]:
    """Guarded chat_sl training invocation; writes the training manifest."""
    export_manifest = verify_export_dir(data_dir)
    renderer_name = renderer_name or default_renderer_for(model)
    if not confirm_spend:
        raise TraceError(
            "real training spends money and requires --confirm-spend; run "
            "`python -m evallab.sft_tinker dry-run` first for the offline "
            "token and cost report"
        )
    if not os.environ.get(TINKER_API_KEY_ENV):
        raise TraceError(
            f"environment variable {TINKER_API_KEY_ENV} is not set; the "
            "trainer needs it (value is never read or logged by this launcher)"
        )
    if log_dir.exists() and any(log_dir.iterdir()):
        raise TraceError(f"log directory is not empty: {log_dir}")
    log_dir.mkdir(parents=True, exist_ok=True)

    command = training_command(
        data_dir / CONVERSATIONS_FILE,
        model=model,
        renderer_name=renderer_name,
        learning_rate=learning_rate,
        lora_rank=lora_rank,
        batch_size=batch_size,
        num_epochs=num_epochs,
        max_length=max_length,
        log_dir=log_dir,
        project_dir=project_dir,
    )
    execute = runner or (lambda argv: subprocess.run(argv, check=False).returncode)
    exit_code = execute(command)
    if exit_code != 0:
        raise TraceError(f"chat_sl trainer exited with {exit_code}: {' '.join(command)}")

    checkpoint = read_final_sampler_path(log_dir)
    sampler_path = str(checkpoint["sampler_path"])
    match = _TINKER_RUN_RE.match(sampler_path)
    manifest = {
        "contract": CONTRACT_VERSION,
        "mode": "train",
        "data_dir": data_dir.as_posix(),
        "conversations_sha256": export_manifest["conversations_sha256"],
        "split_manifest_digest": (export_manifest.get("split_manifest") or {}).get(
            "manifest_digest"
        ),
        "teacher_model": export_manifest.get("teacher_model"),
        "model": model,
        "renderer": renderer_name,
        "train_on_what": TRAIN_ON_WHAT,
        "hyperparameters": {
            "learning_rate": float(learning_rate),
            "lora_rank": int(lora_rank),
            "batch_size": int(batch_size),
            "num_epochs": int(num_epochs),
            "max_length": int(max_length),
        },
        "trainer": {
            "module": CHAT_SL_MODULE,
            "project": project_dir.resolve().as_posix(),
            "command": command,
            "log_dir": log_dir.as_posix(),
            "exit_code": exit_code,
            "checkpoints_sha256": _sha256_file(log_dir / CHECKPOINTS_FILE),
        },
        "tinker_run_id": match.group(1) if match else None,
        "final_checkpoint": checkpoint,
        "sampler_path": sampler_path,
    }
    out_path = manifest_out or (log_dir / TRAINING_MANIFEST_FILE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="offline render + token + cost report (no spend)")
    dry.add_argument("--data", type=Path, required=True, help="evallab.sft_terminus export dir")
    dry.add_argument("--model", required=True, help="student base model id")
    dry.add_argument("--renderer", default=None, help="renderer name (default: per-model)")
    dry.add_argument("--max-length", type=int, default=DEFAULT_TRAINER_CONFIG["max_length"])
    dry.add_argument("--epochs", type=int, default=DEFAULT_TRAINER_CONFIG["num_epochs"])

    train = sub.add_parser("train", help="run chat_sl SFT (requires --confirm-spend)")
    train.add_argument("--data", type=Path, required=True, help="evallab.sft_terminus export dir")
    train.add_argument("--model", required=True, help="student base model id")
    train.add_argument("--renderer", default=None, help="renderer name (default: per-model)")
    train.add_argument("--log-dir", type=Path, required=True, help="new, empty log directory")
    train.add_argument("--lr", type=float, default=DEFAULT_TRAINER_CONFIG["learning_rate"])
    train.add_argument("--lora-rank", type=int, default=DEFAULT_TRAINER_CONFIG["lora_rank"])
    train.add_argument(
        "--batch-size", type=int, default=DEFAULT_TRAINER_CONFIG["batch_size"]
    )
    train.add_argument("--epochs", type=int, default=DEFAULT_TRAINER_CONFIG["num_epochs"])
    train.add_argument("--max-length", type=int, default=DEFAULT_TRAINER_CONFIG["max_length"])
    train.add_argument("--manifest-out", type=Path, default=None)
    train.add_argument(
        "--confirm-spend",
        action="store_true",
        help="explicit acknowledgement that this spends real money",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "dry-run":
            report = dry_run(
                args.data,
                model=args.model,
                renderer_name=args.renderer,
                max_length=args.max_length,
                epochs=args.epochs,
            )
        else:
            report = run_training(
                args.data,
                model=args.model,
                renderer_name=args.renderer,
                log_dir=args.log_dir,
                learning_rate=args.lr,
                lora_rank=args.lora_rank,
                batch_size=args.batch_size,
                num_epochs=args.epochs,
                max_length=args.max_length,
                confirm_spend=args.confirm_spend,
                manifest_out=args.manifest_out,
            )
    except TraceError as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
