#!/usr/bin/env python3
"""Seeker SFT pilot (pE-SELF-5): LoRA-train `Qwen/Qwen3-8B` on `seeker_tool_sft.jsonl`
(90 `ConfinedTools`-executed OpenAI tool-calling conversations) and generate Seeker
submissions on TB4 DEV tasks (base vs trained) for differential replay.

The training corpus is `research/datasets/seeker-sft-v1/seeker_tool_sft.jsonl`
(56 confirmed `1/0` differential positive trajectories + 4 single-version `1/1`
replay trajectories + 30 executed-clean negative controls). The generation harness
is the exact `evallab.seeker` contract (`prompt.py` + `confine.py` are copied into
the image verbatim), so a generated submission is directly replayable by
`evallab.candidate_runner` (upstream 1 / PM port 0).

Usage (from the repo root, with the Modal CLI authenticated):

    uv run modal run research/experiments/seeker-sft-pilot-20261021/pilot_modal.py --mode validate
    uv run modal run research/experiments/seeker-sft-pilot-20261021/pilot_modal.py --mode pilot
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
# 4 witness-seeded DEV tasks (their confirmed 1/0 witnesses are in the training data)
MEMORIZATION_TASKS = (
    "cargo-flight-dispatch",
    "fin-saccr-rwa",
    "html-js-filter",
    "vf2-speedup-networkx",
)
# 2 strictly-unseen DEV tasks (no training row for these tasks)
TRANSFER_TASKS = ("uefi-bootkit", "vpp-loss-divergence")
TASKS = MEMORIZATION_TASKS + TRANSFER_TASKS
ARMS = ("base", "trained")
MAX_TURNS = 8
MAX_NEW_TOKENS = 768
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
out_vol = modal.Volume.from_name("seeker-sft-pilot-out", create_if_missing=True)
app = modal.App("seeker-sft-pilot-20261021")


def _load_conversations() -> list[dict[str, Any]]:
    text = Path("/pilot/seeker_tool_sft.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


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
    """Free CPU check: render every training conversation with the Qwen3 chat template."""
    sys.path.insert(0, "/pilot")
    import confine  # type: ignore[import-not-found]
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL)
    conversations = _load_conversations()
    rows: list[dict[str, Any]] = []
    mask_supported = True
    for conv in conversations:
        messages = conv["messages"]
        text = tok.apply_chat_template(messages, tools=confine.TOOL_SCHEMAS, tokenize=False)
        ids = tok(text, truncation=True, max_length=MAX_TRAIN_LENGTH)["input_ids"]
        try:
            rendered = tok.apply_chat_template(
                messages,
                tools=confine.TOOL_SCHEMAS,
                tokenize=False,
                return_assistant_tokens_mask=True,
            )
            mask_supported = mask_supported and isinstance(rendered, tuple)
        except Exception:  # noqa: BLE001 - template capability probe
            mask_supported = False
        rows.append(
            {
                "example_id": conv["example_id"],
                "conditioning_mode": conv["conditioning_mode"],
                "exploit_found": conv["exploit_found"],
                "tokens": len(ids),
                "chars": len(text),
                "truncated": len(ids) >= MAX_TRAIN_LENGTH,
            }
        )
    sample = tok.apply_chat_template(
        conversations[0]["messages"], tools=confine.TOOL_SCHEMAS, tokenize=False
    )
    return {
        "model": MODEL,
        "conversations": len(rows),
        "total_tokens": sum(r["tokens"] for r in rows),
        "max_tokens": max(r["tokens"] for r in rows),
        "truncated": sum(1 for r in rows if r["truncated"]),
        "assistant_mask_supported": mask_supported,
        "sample_head": sample[:1500],
        "rows": rows,
    }


@app.function(
    image=image,
    gpu="A100-40GB",
    timeout=7200,
    volumes={"/hf": hf_cache, "/out": out_vol},
)
def train_and_generate(config: dict[str, Any]) -> dict[str, Any]:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    sys.path.insert(0, "/pilot")
    import confine  # type: ignore[import-not-found]
    import prompt as seeker_prompt  # type: ignore[import-not-found]

    torch.manual_seed(0)
    tok = AutoTokenizer.from_pretrained(MODEL)
    conversations = _load_conversations()
    texts = [
        tok.apply_chat_template(c["messages"], tools=confine.TOOL_SCHEMAS, tokenize=False)
        for c in conversations
    ]
    examples = [tok(t, truncation=True, max_length=MAX_TRAIN_LENGTH)["input_ids"] for t in texts]

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
        for ids in examples:
            input_ids = torch.tensor([ids], device="cuda")
            out = model(input_ids=input_ids, labels=input_ids)
            (out.loss / GRAD_ACCUM).backward()
            losses.append(round(float(out.loss.detach()), 4))
            step += 1
            if step % GRAD_ACCUM == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
    if step % GRAD_ACCUM:
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)

    adapter_dir = Path("/out/adapter")
    if adapter_dir.exists():
        shutil.rmtree(adapter_dir)
    model.save_pretrained(adapter_dir)
    tok.save_pretrained(adapter_dir)
    out_vol.commit()

    results: dict[str, Any] = {
        "model": MODEL,
        "train_examples": len(examples),
        "train_steps": step,
        "loss_first": losses[0] if losses else None,
        "loss_last": losses[-1] if losses else None,
        "loss_mean_last_10": (round(sum(losses[-10:]) / len(losses[-10:]), 4) if losses else None),
        "runs": {},
    }

    for arm in ARMS:
        # LoRA is additive, so disabling the adapter layers reproduces the exact
        # base weights without a second model copy.
        if arm == "base":
            model.disable_adapter_layers()
        else:
            model.enable_adapter_layers()
        model.eval()
        for task in TASKS:
            ws = _build_workspace(task, Path("/out/workspaces") / arm)
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
                        max_new_tokens=MAX_NEW_TOKENS,
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
            results["runs"][f"{arm}:{task}"] = {
                "arm": arm,
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
def main(mode: str = "validate") -> None:
    if mode == "validate":
        report = validate_data.remote()
        out = HERE / "validation.json"
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in ("rows", "sample_head")},
                indent=2,
                sort_keys=True,
            )
        )
        print(f"wrote {out}")
        return
    if mode == "pilot":
        results = train_and_generate.remote({"epochs": EPOCHS})
        out = HERE / "pilot-results.json"
        out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary = {
            key: {k: v for k, v in run.items() if k not in ("trace", "submission")}
            for key, run in results["runs"].items()
        }
        print(
            json.dumps(
                {**{k: v for k, v in results.items() if k != "runs"}, "runs": summary},
                indent=2,
                sort_keys=True,
            )
        )
        print(f"wrote {out}")
        return
    raise SystemExit(f"unknown mode: {mode}")
