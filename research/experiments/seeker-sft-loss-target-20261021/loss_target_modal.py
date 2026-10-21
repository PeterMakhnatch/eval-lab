#!/usr/bin/env python3
"""Seeker SFT loss-target ablation (pE-SELF-6).

pE-SELF-5 trained `Qwen/Qwen3-8B` on `seeker_tool_sft.jsonl` with the loss over
the *whole* rendered conversation and the model learned the submission contract
but not exploit discovery. This experiment tests the obvious data-quality
hypothesis: the corpus is dominated by text the model must only read (the Seeker
system prompt, the task instruction, the tool observations), so the decision
signal is diluted.

Two arms, same data, same seed, same generation protocol:

- `full`      — loss over every token (reproduces pE-SELF-5 in the same run)
- `assistant` — loss masked to assistant tokens only (the standard SFT target)

Usage:

    uv run modal run research/experiments/seeker-sft-loss-target-20261021/loss_target_modal.py --mode validate
    uv run modal run research/experiments/seeker-sft-loss-target-20261021/loss_target_modal.py --mode pilot
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import modal

HERE = Path(__file__).resolve().parent
# Modal mounts this file at /root/<name>.py inside the container, where the repo
# tree does not exist; the local-only paths are guarded below.
REPO = HERE.parents[2] if len(HERE.parents) >= 3 else Path("/root")
SEEKER_DIR = REPO / "src" / "evallab" / "seeker"
DATA_PATH = REPO / "research" / "datasets" / "seeker-sft-v1" / "seeker_tool_sft.jsonl"
VIEW_ROOT = (
    Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/agent-inputs/dev/tasks"
)

MODEL = "Qwen/Qwen3-8B"
MEMORIZATION_TASKS = (
    "cargo-flight-dispatch",
    "fin-saccr-rwa",
    "html-js-filter",
    "vf2-speedup-networkx",
)
TRANSFER_TASKS = ("uefi-bootkit", "vpp-loss-divergence")
TASKS = MEMORIZATION_TASKS + TRANSFER_TASKS
LOSS_TARGETS = ("full", "assistant")
MAX_TURNS = 8
# pE-SELF-5 used 768, which truncates any `write_file` whose body exceeds the
# budget: the training corpus's own `submission/solve.sh` bodies run to ~1021
# tokens at the maximum, so 768 silently cut real submissions off mid-JSON.
DEFAULT_MAX_NEW_TOKENS = 3072
MAX_TRAIN_LENGTH = 8192
EPOCHS = 2
LEARNING_RATE = 1e-4
GRAD_ACCUM = 4

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch==2.6.0",
        "transformers==4.51.3",
        "peft==0.15.0",
        "accelerate==1.6.0",
        "hf_transfer==0.1.9",
    )
    .env(
        {
            "HF_HOME": "/hf",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        }
    )
)
if SEEKER_DIR.is_dir() and DATA_PATH.is_file():  # local build only
    image = (
        image.add_local_file(SEEKER_DIR / "prompt.py", "/pilot/prompt.py")
        .add_local_file(SEEKER_DIR / "confine.py", "/pilot/confine.py")
        .add_local_file(DATA_PATH, "/pilot/seeker_tool_sft.jsonl")
    )
    for _task in TASKS:
        if (VIEW_ROOT / _task).is_dir():
            image = image.add_local_dir(
                VIEW_ROOT / _task,
                f"/tasks/{_task}",
                ignore=["solution", "solution/**", "cheat", "cheat/**"],
            )

hf_cache = modal.Volume.from_name("seeker-sft-hf-cache", create_if_missing=True)
out_vol = modal.Volume.from_name("seeker-sft-loss-target-out", create_if_missing=True)
app = modal.App("seeker-sft-loss-target-20261021")


def _load_conversations() -> list[dict[str, Any]]:
    text = Path("/pilot/seeker_tool_sft.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _render_with_mask(
    tok: Any, messages: list[dict[str, Any]], tools: Any
) -> tuple[list[int], list[bool]]:
    """Token ids plus a per-token assistant mask, built from rendered prefixes.

    The Qwen3 chat template does not expose `return_assistant_tokens_mask`, so the
    assistant spans are recovered by tokenizing the conversation rendered up to and
    including each message and taking the successive boundaries.
    """
    full = tok.apply_chat_template(messages, tools=tools, tokenize=False)
    ids = tok(full, add_special_tokens=False)["input_ids"]
    mask = [False] * len(ids)
    previous = 0
    for index, message in enumerate(messages):
        prefix = tok.apply_chat_template(messages[: index + 1], tools=tools, tokenize=False)
        boundary = len(tok(prefix, add_special_tokens=False)["input_ids"])
        if message["role"] == "assistant":
            for position in range(previous, min(boundary, len(ids))):
                mask[position] = True
        previous = boundary
    if previous != len(ids):
        raise ValueError(
            f"prefix boundaries do not reconstruct the conversation: {previous} != {len(ids)}"
        )
    return ids, mask


def _parse_tool_calls(reply: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in _TOOL_CALL_RE.finditer(reply):
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        name = str(payload.get("name") or "")
        args = payload.get("arguments")
        if not name:
            continue
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "args": args})
    return calls


def _build_workspace(task: str, root: Path) -> Path:
    ws = root / task
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    shutil.copytree(
        Path("/tasks") / task,
        ws / "task",
        ignore=shutil.ignore_patterns("solution", "cheat"),
    )
    (ws / "submission").mkdir()
    return ws


def _submission_files(ws: Path) -> dict[str, str]:
    sub = ws / "submission"
    out: dict[str, str] = {}
    if not sub.is_dir():
        return out
    for path in sorted(sub.rglob("*")):
        if path.is_file():
            rel = path.relative_to(sub).as_posix()
            try:
                out[rel] = path.read_text(encoding="utf-8")[:200_000]
            except (UnicodeDecodeError, OSError):
                out[rel] = "<binary>"
    return out


@app.function(image=image, timeout=1800, volumes={"/hf": hf_cache})
def validate_data() -> dict[str, Any]:
    """Free CPU check: token budget per role and the assistant-mask reconstruction."""
    sys.path.insert(0, "/pilot")
    import confine  # type: ignore[import-not-found]
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL)
    conversations = _load_conversations()
    role_tokens: dict[str, int] = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    rows: list[dict[str, Any]] = []
    for conv in conversations:
        messages = conv["messages"]
        ids, mask = _render_with_mask(tok, messages, confine.TOOL_SCHEMAS)
        previous = 0
        for index, message in enumerate(messages):
            prefix = tok.apply_chat_template(
                messages[: index + 1], tools=confine.TOOL_SCHEMAS, tokenize=False
            )
            boundary = len(tok(prefix, add_special_tokens=False)["input_ids"])
            role_tokens[message["role"]] += boundary - previous
            previous = boundary
        rows.append(
            {
                "example_id": conv["example_id"],
                "tokens": len(ids),
                "assistant_tokens": sum(mask),
                "assistant_fraction": round(sum(mask) / len(ids), 4),
                "truncated": len(ids) >= MAX_TRAIN_LENGTH,
            }
        )
    total = sum(role_tokens.values())
    return {
        "model": MODEL,
        "conversations": len(rows),
        "total_tokens": total,
        "role_tokens": role_tokens,
        "role_fraction": {role: round(count / total, 4) for role, count in role_tokens.items()},
        "assistant_tokens_mean": round(sum(r["assistant_tokens"] for r in rows) / len(rows), 1),
        "assistant_fraction_mean": round(sum(r["assistant_fraction"] for r in rows) / len(rows), 4),
        "assistant_fraction_min": min(r["assistant_fraction"] for r in rows),
        "assistant_fraction_max": max(r["assistant_fraction"] for r in rows),
        "truncated": sum(1 for r in rows if r["truncated"]),
        "rows": rows,
    }


@app.function(
    image=image,
    gpu="A100-40GB",
    timeout=10800,
    volumes={"/hf": hf_cache, "/out": out_vol},
)
def train_and_generate(config: dict[str, Any]) -> dict[str, Any]:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    sys.path.insert(0, "/pilot")
    import confine  # type: ignore[import-not-found]
    import prompt as seeker_prompt  # type: ignore[import-not-found]

    loss_target = str(config.get("loss_target", "assistant"))
    if loss_target not in LOSS_TARGETS:
        raise ValueError(f"unknown loss_target: {loss_target}")
    max_new_tokens = int(config.get("max_new_tokens", DEFAULT_MAX_NEW_TOKENS))

    torch.manual_seed(0)
    tok = AutoTokenizer.from_pretrained(MODEL)
    conversations = _load_conversations()
    examples: list[tuple[list[int], list[int]]] = []
    for conv in conversations:
        ids, mask = _render_with_mask(tok, conv["messages"], confine.TOOL_SCHEMAS)
        ids = ids[:MAX_TRAIN_LENGTH]
        mask = mask[:MAX_TRAIN_LENGTH]
        if loss_target == "assistant":
            labels = [token if keep else -100 for token, keep in zip(ids, mask, strict=True)]
        else:
            labels = list(ids)
        if all(label == -100 for label in labels):
            raise ValueError(f"no supervised tokens for {conv['example_id']}")
        examples.append((ids, labels))

    model = AutoModelForCausalLM.from_pretrained(
        MODEL, torch_dtype=torch.bfloat16, device_map="cuda"
    )
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    lora = LoraConfig(
        r=32,
        lora_alpha=64,
        lora_dropout=0.0,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )
    model = get_peft_model(model, lora)
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=LEARNING_RATE)
    model.train()

    losses: list[float] = []
    step = 0
    for _epoch in range(int(config.get("epochs", EPOCHS))):
        for ids, labels in examples:
            input_ids = torch.tensor([ids], device="cuda")
            label_ids = torch.tensor([labels], device="cuda")
            out = model(input_ids=input_ids, labels=label_ids)
            (out.loss / GRAD_ACCUM).backward()
            losses.append(round(float(out.loss.detach()), 4))
            step += 1
            if step % GRAD_ACCUM == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
    if step % GRAD_ACCUM:
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)

    adapter_dir = Path("/out") / f"adapter-{loss_target}"
    if adapter_dir.exists():
        shutil.rmtree(adapter_dir)
    model.save_pretrained(adapter_dir)
    tok.save_pretrained(adapter_dir)
    out_vol.commit()

    results: dict[str, Any] = {
        "model": MODEL,
        "loss_target": loss_target,
        "max_new_tokens": max_new_tokens,
        "train_examples": len(examples),
        "train_steps": step,
        "loss_first": losses[0] if losses else None,
        "loss_last": losses[-1] if losses else None,
        "loss_mean_last_10": (round(sum(losses[-10:]) / len(losses[-10:]), 4) if losses else None),
        "runs": {},
    }

    model.eval()
    for task in TASKS:
        ws = _build_workspace(task, Path("/out/workspaces") / loss_target)
        tools = confine.ConfinedTools(ws)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": seeker_prompt.SYSTEM_PROMPT},
            {"role": "user", "content": seeker_prompt.user_prompt(task)},
        ]
        trace: list[dict[str, Any]] = []
        for turn in range(MAX_TURNS):
            text = tok.apply_chat_template(
                messages,
                tools=confine.TOOL_SCHEMAS,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = tok(text, return_tensors="pt", truncation=True, max_length=32768)
            inputs = {k: v.to(model.device) for k, v in inputs.items()}
            with torch.no_grad():
                generated = model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    pad_token_id=tok.eos_token_id,
                )
            reply = tok.decode(
                generated[0][inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            )
            calls = _parse_tool_calls(reply)
            trace.append(
                {
                    "turn": turn,
                    "reply": reply[:2000],
                    "calls": [c["name"] for c in calls],
                }
            )
            if not calls:
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Call a tool now: write `submission/solve.sh` and "
                            "`submission/notes.json` with write_file, then call finish."
                        ),
                    }
                )
                continue
            content = _TOOL_CALL_RE.sub("", reply).strip()
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["args"]),
                            },
                        }
                        for c in calls
                    ],
                }
            )
            finished = False
            for call in calls:
                result = tools.call(call["name"], call["args"])
                messages.append({"role": "tool", "content": result[:30_000]})
                if call["name"] == "finish":
                    finished = True
            if finished:
                break
        files = _submission_files(ws)
        notes: dict[str, Any] = {}
        if "notes.json" in files:
            try:
                notes = json.loads(files["notes.json"])
            except ValueError:
                notes = {}
        results["runs"][task] = {
            "task": task,
            "turns": len(trace),
            "tool_calls": sum(len(t["calls"]) for t in trace),
            "finished": any("finish" in t["calls"] for t in trace),
            "files": sorted(files),
            "solve_sh_chars": len(files.get("solve.sh", "")),
            "exploit_found": notes.get("exploit_found"),
            "class": notes.get("class"),
            "mechanism": str(notes.get("mechanism") or "")[:400],
            "trace": trace,
            "submission": files,
        }
        out_vol.commit()
    return results


@app.local_entrypoint()
def main(mode: str = "validate", max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS) -> None:
    if mode == "validate":
        report = validate_data.remote()
        out = HERE / "validation.json"
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2, sort_keys=True)
        )
        print(f"wrote {out}")
        return
    if mode == "pilot":
        results: dict[str, Any] = {"max_new_tokens": max_new_tokens, "arms": {}}
        for loss_target in LOSS_TARGETS:
            results["arms"][loss_target] = train_and_generate.remote(
                {
                    "epochs": EPOCHS,
                    "loss_target": loss_target,
                    "max_new_tokens": max_new_tokens,
                }
            )
        out = HERE / f"loss-target-results-{max_new_tokens}.json"
        out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary = {
            arm: {
                **{k: v for k, v in payload.items() if k != "runs"},
                "runs": {
                    task: {k: v for k, v in run.items() if k not in ("trace", "submission")}
                    for task, run in payload["runs"].items()
                },
            }
            for arm, payload in results["arms"].items()
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"wrote {out}")
        return
    raise SystemExit(f"unknown mode: {mode}")
