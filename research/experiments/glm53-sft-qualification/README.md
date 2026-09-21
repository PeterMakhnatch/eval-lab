# HAR-66: GLM-5.3-Flash exact-model SFT/serving qualification packet

CPU/source qualification only. No weights, GPU, or paid calls were made.
Machine-readable decision contract: `next-task.json`. Probe receipt: `verification.json`.

## Verdict

- **Primary (Helper HAR-64):** TRL `SFTTrainer` + PEFT LoRA on attention +
  dense-MLP + shared-expert `nn.Linear` targets, transformers pinned to
  git commit `770e4c40` (PyPI 5.16.0 does **not** register `glm5_next`).
- **Fallback:** NVIDIA NeMo AutoModel full language-backbone SFT at commit
  `cb60ebf5` (validated 72×H100 topology; multi-node only).
- **Serving:** SGLang image `lmsysorg/sglang:glm-5.3-flash`; adapters must be
  merged first (no dynamic LoRA for `glm5_next` in vLLM/SGLang).

## Files

| File | Purpose |
|---|---|
| `next-task.json` | Full decision packet: pins, gates, costs, consumer contract |
| `probe.py` | Metadata/tokenizer/template qualification (reproducible, `uv run --no-project`) |
| `verification.json` | Actual probe receipt on transformers 5.16.0 wheel |
| `template-sources.json` | Hash-pinned public-asset manifest (weights excluded) |
| `recipe-trl-lora.py` | Helper's consumer skeleton with every hard-won constraint inline |
| `recipe-nemo-fullsift.yaml` | Corrected NeMo recipe (checkpointing enabled, our data source) |
| `serving-sglang.sh` | Pinned serve commands for 8×H100 FP8 base and 8×H200 BF16 |

## Non-obvious facts Helper must respect

1. **Wheel gap:** `transformers==5.16.0` PyPI raises on `AutoConfig` for this
   model. The tokenizer loads; the architecture does not. Pin the git commit.
2. **Load class:** `AutoModelForImageTextToText`, never `AutoModelForCausalLM`
   (absent from that mapping; `SFTTrainer(model=str)` would KeyError).
3. **Routed experts are 3D `nn.Parameter`**, not `nn.Linear` — LoRA
   `target_modules` cannot touch them. The qualified fallback list is in
   `next-task.json` `lora_target_modules`.
4. **FP8 base cannot train**: dequantize to BF16 (`torch_dtype=bfloat16`).
5. **No assistant mask:** the stock template lacks `{% generation %}` tags;
   `assistant_only_loss` supervises zero tokens. Use prompt-completion records.
6. **Tool arguments:** decode OpenAI `function.arguments` JSON strings to
   mappings before rendering; the template rejects raw strings.
7. **`reasoning_effort`:** only `low`/`high` are honored; anything else
   (including OMP's `xhigh`) silently becomes `Max`.
8. **`clear_thinking`:** defaults false; when true it only clears reasoning
   before the *latest user turn* (turn-sensitive, not global).
9. **Terminal stop:** the template appends no EOS after the final assistant
   turn; define an explicit target-boundary policy.
10. **Hopper serving:** BF16 KV + tilelang DSA only; FP8 KV + trtllm is
    Blackwell-only.

## Cost request (not authorized spend)

Option A: 1×8 H200 via Modal ≈ $36.32/hr → 4–8 h pilot ≈ **$145–291** plus
400 GiB volume ($36/mo). See `next-task.json.costed_qualification_request`.
