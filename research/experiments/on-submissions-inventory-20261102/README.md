# Real Agent Submissions Inventory for MiMo Packages (A0-SUBMISSIONS-INVENTORY)

This research experiment inventories real agent submissions with original-grader outcomes across the MiMo evaluation package universe, providing the ground-truth submission dataset for downstream gold grader replay (A8).

## Key Deliverables

- `inventory.py`: Deterministic inventory script that scans stored trial runs and regrade receipts across `eval-lab-results`, extracts patch paths, verifies sha256 digests, records original grader outcomes, and joins against A1's gold universe (`gold-universe.json`).
- `counts.json`: Summary counts, model breakdowns, gold universe join results, and sha256 checksums of the external deliverables.
- External deliverables:
  - `submissions-inventory.json` at `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A0/submissions-inventory.json` and mirrored to bridge gates at `.../gates/submissions-inventory.json`.
  - Durable receipts (`inventory_receipt.json`, `patch_manifest.jsonl`) at `.../raw/A0-SUBMISSIONS-INVENTORY/receipts/` and `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A0/`.

## Key Findings

1. **Total Submissions Universe:**
   - 691 total trials from real agent models (`XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` variants, `zai/glm-5.3-flash`, `tinker/Qwen/Qwen3.6-35B-A3B`, `openrouter-metered/openai/gpt-oss-120b`, `openrouter-metered/xiaomi/mimo-v2.6-flash`).
   - 509 trials with recoverable workspace patches (`agent.kept.diff`, `agent.diff`, `agent.full.diff`, or `uncommitted.diff`).
   - 604 trials with recorded original grader reward.
   - 202 trials accepted by original grader (`reward == 1.0`) with a non-empty recoverable patch.
   - 280 trials rejected by original grader (`reward == 0.0`) with a non-empty recoverable patch.
   - 616 strictly DEV trials (unsealed per `CorpusRegistry`), 75 non-DEV (TEST / heldout).

2. **Gold Universe Join (A1):**
   - 32 packages in the executable gold universe (`gold_ok`).
   - **Only 2 / 32 (6.2%, 95% Wilson CI: 1.7% - 20.1%)** have $\ge 3$ accepted real submissions (`format-code-task-002391` with 5, `format-code-task-002552` with 8).
   - 26 / 32 gold tasks have 0 real agent trials in stored history.
   - 4 / 32 gold tasks have 1 or 2 accepted submissions (`000838`: 1, `001809`: 2, `001985`: 2, `002139`: 2).
