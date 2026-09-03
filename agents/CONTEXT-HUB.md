# 🎯 CONTEXT HUB — Single Source of Truth

> **EVERY AGENT READS THIS FILE FIRST, EVERY SESSION. NO EXCEPTIONS.**
> This file supersedes any older instruction, mission note, or remembered context.
> Last updated: 2026-09-03 by Peter (via Main agent)

---

## 1. CURRENT FOCUS (LOCKED)

**We evaluate multi-turn tool agents on τ³-bench (Sierra). Nothing else.**

- Benchmark: `sierra-research/tau3-bench` (375 tasks: airline/retail/telecom/banking_knowledge)
- Harness: Harbor (`~/Developer/agent-evals/harbor`), adapter at `adapters/tau3-bench/`
- Proven working: oracle PASS on `tau3-retail-1` (reward 1.0, 2026-09-01)
- Why: unsaturated (GPT-5.2 pass³ = 44.8%, banking = 6.2%), state+memory+tool-use is the capability we study
- τ³ reward is partly LLM-judged → pin the assertion model identity; never compare across different assertion models

### NOT in scope right now (do not run, do not propose):
- Terminal-Bench full runs (except the one registered canary task until migrated)
- BFCL (not containerized; no Harbor adapter)
- SWE-bench, GAIA, new benchmark exploration
- Any new "capability vertical" — we are narrowing, not expanding

---

## 2. MODEL & AGENT POLICY

| Purpose | What to use | Never use |
|---|---|---|
| Eval trial agents | **zai-opencode** (GLM Coding Plan, flat-rate) | ~~codex~~ (credits=0, lockout risk) |
| User simulator / τ³ verifier | `glm-5.3-flash` via `https://api.z.ai/api/coding/paas/v4` | OpenAI keys |
| Interactive driver | gemini-3.8-flash:low | :max on default roles |
| Deep review (rare) | claude-opus-5 / gpt-5.6-terra:high, explicitly invoked | — |

**Canary suite now runs `zai-opencode`** (changed from codex 2026-09-03). Canary tasks are still the old trio; migrating them to τ³ requires task registration — see Open Items.

---

## 3. HOW WORK FLOWS (READ BEFORE DOING ANYTHING)

- All eval dispatch goes through the queue + PolicyGate (`queue/events.jsonl` is the only authorization ledger; billable runs need `uv run evallab approve <spec-id> --actor peter`)
- Integration merges happen on `integrate/spine-batch1` (local merge commits by lane agents — that's why commit count >> PR count; PRs are for repo-facing changes only)
- Overnight: nightly (02:30 digest), morning briefing (08:00 ET launchd → `digests/*-morning-briefing.md` + macOS notification)
- **This file is the hub.** If your context contradicts it, the hub wins. Update the hub when decisions change — don't brief agents one-by-one.

---

## 4. OPEN ITEMS (owner → next action)

1. **Peter → approve/reject** 4 specs in `queue/waiting/` (3 codex canaries from last night — will re-enqueue as zai after this config change; 1 zai funcdag k3)
2. **Platform lane → register τ³ tasks** into `library/tasks/` so canaries + baselines run tau3-* instead of the old trio
3. **Storage lane → CAS authority gap**: 79-88 catalog jobs lack CAS records (blocks nightly projection)
4. **Main → commit** `scripts/morning-briefing.sh` + canary agent swap in one PR

---

## 5. DECISION LOG (append-only, newest first)

- **2026-09-03** — Canary agents switched codex → zai-opencode. Central context hub created. Focus locked to τ³-bench.
- **2026-09-02** — SPADE/TASTE studied; synthetic generator (`src/evallab/synthetic_tool_memory.py`) merged to branch. Portfolio narrative: "why multi-turn tool use is a working-memory problem."
- **2026-09-01** — τ³-bench proven in Harbor (oracle 1.0 on retail-1). ZAI coding-plan endpoint validated for user-sim + verifier.
- **2026-08-31** — DeepSeek lane decommissioned for trials (balance=0). ZAI flat-rate is the eval substrate.

---

## 6. KEY PATHS

| What | Where |
|---|---|
| This hub | `agents/CONTEXT-HUB.md` |
| Mission board | `agents/missions/ACTIVE.md` |
| Queue ledger | `queue/events.jsonl` |
| **Eval roster (models/agents/benchmarks)** | `policy/eval-roster.yaml` — see §7 runbook |
| Morning briefings | `digests/*-morning-briefing.md` |
| τ³ adapter | `~/Developer/agent-evals/harbor/adapters/tau3-bench/` |
| Threat model | `eval-lab-threat-model.md` |
| Portfolio roadmap | `docs/PORTFOLIO-ROADMAP-AND-SYSTEM-STATE.md` |

---

## 7. 🔧 RUNBOOK: Changing Models, Agents, or Benchmarks

**The only file you edit is [`policy/eval-roster.yaml`](../policy/eval-roster.yaml).**

To change what the lab runs evals with (agent, model, simulator, benchmark, bans):

1. Edit `policy/eval-roster.yaml` — update the field + `updated:`/`updated_by:`.
2. Append one line to §5 Decision Log in this file saying what changed and why.
3. Commit both in the same commit.
4. Verify: `uv run python -m evallab.roster` — must print `✅ all consumers in sync` or list drift.

**Rules for agents:**
- You READ the roster. You never edit it without a decision-log entry (Peter approves).
- Never "fix" model/agent config elsewhere (canary-suite, queue specs, run scripts) by hand — if drift is detected, REPORT it; the fix is a roster edit per this runbook.
- The 08:00 briefing runs the drift check automatically. Banned agents in live specs show up there.
- Reviewers: reject PRs that change model/agent/benchmark selection in any consumer file without a matching roster change in the same PR.
