"""Modal LoRA SFT pilot for qa-sft-mimo-v0 (recipe + eval in one GPU run).

Base: Qwen/Qwen3-8B (dense). Chosen over Qwen3-30B-A3B MoE because dense
LoRA tooling (peft + transformers Trainer) is the reliable path inside the
$8 cap; the MoE route risks expert-parallel/checkpoint issues for no
expected gain at 142 training examples.

Protocol (pre-declared): base zero-shot on the 29 test prompts first (same
stack comparison), LoRA train on 142 train prompts, dev (24) for checkpoint
selection between epoch 2 and 3, then the selected adapter ONCE on test.
Scorer matches the zero-shot baseline: positive = any weak finding,
negative false alarm = any finding, unparsed counted separately.

Upload data first (prompt/target JSONL built locally from published
artifacts; no tb4-audit-v1 data):
  modal volume create qa-sft-pilot (once)
  modal volume put qa-sft-pilot pilot-train.jsonl pilot-dev.jsonl pilot-test.jsonl pilot-tb4.jsonl /
Run:
  modal run research/datasets/qa-sft-mimo-v0/pilot_modal.py
Outputs land in the volume under /out (adapter + results.json).
"""

import json
import pathlib

import modal

app = modal.App("qa-sft-mimo-v0-pilot")
vol = modal.Volume.from_name("qa-sft-pilot", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("torch", "transformers", "peft", "accelerate", "datasets")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "0"})
)
BASE = "Qwen/Qwen3-8B"


def score(rows):
    import re

    pos = det = neg = fa = unparsed = 0
    for row in rows:
        try:
            text = row["generation"]
            found = json.loads(text[text.index("{") : text.rindex("}") + 1]).get("findings")
            if not isinstance(found, list):
                raise ValueError
        except Exception:
            found = []
            unparsed += 1
        if row["label"] == "positive":
            pos += 1
            det += bool(found)
        else:
            neg += 1
            fa += bool(found)
    return {"positives": pos, "detected": det, "negatives": neg, "false_alarms": fa, "unparsed": unparsed}


@app.function(image=image, gpu="A10G", volumes={"/data": vol}, timeout=3 * 60 * 60)
def run():
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def load(name):
        return [json.loads(line) for line in pathlib.Path(f"/data/{name}").read_text().splitlines() if line]

    train, dev, test, tb4 = load("pilot-train.jsonl"), load("pilot-dev.jsonl"), load("pilot-test.jsonl"), load("pilot-tb4.jsonl")

    def generate(model, rows):
        out = []
        for row in rows:
            ids = tok(row["prompt"], return_tensors="pt", truncation=True, max_length=2048).to(model.device)
            with torch.no_grad():
                gen = model.generate(**ids, max_new_tokens=300, do_sample=False, pad_token_id=tok.eos_token_id)
            out.append({**row, "generation": tok.decode(gen[0][ids["input_ids"].shape[1] :], skip_special_tokens=True)})
        return out

    model = AutoModelForCausalLM.from_pretrained(BASE, torch_dtype=torch.bfloat16, device_map="auto")
    results = {"base": BASE, "test_base_zero_shot": score(generate(model, test))}
    pathlib.Path("/data/out").mkdir(exist_ok=True)
    pathlib.Path("/data/out/results.json").write_text(json.dumps(results, indent=2))
    vol.commit()

    def encode(row):
        prompt_ids = tok(row["prompt"], add_special_tokens=True)["input_ids"]
        target_ids = tok(row["target"] + tok.eos_token, add_special_tokens=False)["input_ids"]
        input_ids = (prompt_ids + target_ids)[:2048]
        labels = ([-100] * len(prompt_ids) + target_ids)[:2048]
        return {"input_ids": input_ids, "labels": labels}

    def collate(batch):
        maxlen = max(len(b["input_ids"]) for b in batch)
        input_ids, labels, attn = [], [], []
        for b in batch:
            pad = maxlen - len(b["input_ids"])
            input_ids.append(b["input_ids"] + [tok.pad_token_id] * pad)
            labels.append(b["labels"] + [-100] * pad)
            attn.append([1] * len(b["input_ids"]) + [0] * pad)
        return {"input_ids": torch.tensor(input_ids), "labels": torch.tensor(labels), "attention_mask": torch.tensor(attn)}

    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.0, task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
    model = get_peft_model(model, lora)
    model.gradient_checkpointing_enable()
    args = TrainingArguments(output_dir="/tmp/ckpt", per_device_train_batch_size=1, gradient_accumulation_steps=8,
                             num_train_epochs=3, learning_rate=1e-4, logging_steps=10, save_strategy="epoch",
                             bf16=True, report_to=[], seed=17, gradient_checkpointing=True)
    encoded = [encode(row) for row in train]
    Trainer(model=model, args=args, train_dataset=encoded, data_collator=collate).train()

    dev_scores = {}
    for epoch in (2, 3):
        ckpt = f"/tmp/ckpt/checkpoint-{epoch * 18}"  # 142 ex / 8 eff.batch = 18 steps per epoch
        try:
            adapter = PeftModel.from_pretrained(model.base_model.model, ckpt)
        except Exception:
            import glob

            candidates = sorted(glob.glob("/tmp/ckpt/checkpoint-*"))
            adapter = PeftModel.from_pretrained(model.base_model.model, candidates[epoch - 1])
        dev_scores[f"epoch{epoch}"] = score(generate(adapter, dev))
    best = max(dev_scores, key=lambda k: dev_scores[k]["detected"] - dev_scores[k]["false_alarms"])
    results["dev_by_checkpoint"] = dev_scores
    results["selected"] = best

    import glob

    candidates = sorted(glob.glob("/tmp/ckpt/checkpoint-*"))
    adapter = PeftModel.from_pretrained(model.base_model.model, candidates[int(best[-1]) - 1])
    results["test_trained_once"] = score(generate(adapter, test))
    results["tb4_dev_probe"] = generate(adapter, tb4)
    pathlib.Path("/data/out").mkdir(exist_ok=True)
    adapter.save_pretrained("/data/out/adapter")
    pathlib.Path("/data/out/results.json").write_text(json.dumps(results, indent=2))
    vol.commit()
    print(json.dumps({k: v for k, v in results.items() if k != "tb4_dev_probe"}, indent=2))


@app.local_entrypoint()
def main():
    run.remote()
