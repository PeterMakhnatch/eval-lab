# Curated Harbor task library

CURATOR-owned inventory for the eval-lab canary suite and registry.

**Included:** 19 locally verified tasks (oracle `k=3` pass, nop fail).  
**Sources:** `frontier-bench` @ `3d694e91` and `terminal-bench` @ `4e77c91d` (same 74-name TB3-era suite).  
**License:** Apache-2.0 (upstream repo LICENSE).  
**Runs:** `~/Developer/helab-curator/runs/` (`-n` ≤ 2, free oracle/nop only).

Each include has `curated/<task>/CARD.md`. Rejections (GPU/cloud/oracle-fail/leakage) are in `REJECTED.md`.

## Canary nominees (best 5)

These are the recommended first-line smoke set: diverse domains, cheap oracles, clean separate verifiers, nop fails hard.

| Priority | Task | Why |
| --- | --- | --- |
| 1 | **html-js-filter** | Small Security/AppSec; Playwright outcome tests; ~2 min/oracle trial |
| 2 | **foodstuff-beta-activity** | Fastest Science card (~37s k=3); numeric results file |
| 3 | **fin-saccr-rwa** | Finance/ops; hidden goldens; already a lab reference shape |
| 4 | **interleaved-vigenere** | Crypto; regenerated eval texts; 2 GB RAM |
| 5 | **bun-sourcemap-leak** | Systems/release pipeline; 1 CPU / 2 GB; leak-focused outcome tests |

## Full library (19)

| Task | Domain | Oracle k=3 | Nop |
| --- | --- | --- | --- |
| html-js-filter | Security/AppSec | 1.0 | 0.0 |
| foodstuff-beta-activity | Science/Chemistry | 1.0 | 0.0 |
| interleaved-vigenere | Security/Cryptography | 1.0 | 0.0 |
| music-harmony | Media/Music | 1.0 | 0.0 |
| cli-2ph-simplex | Software/Algorithms | 1.0 | 0.0 |
| fin-saccr-rwa | Operations/Finance | 1.0 | 0.0 |
| data-anonymization | Software/Data engineering | 1.0 | 0.0 |
| cargo-flight-dispatch | Operations/Logistics | 1.0 | 0.0 |
| session-window-debug | Software/Systems | 1.0 | 0.0 |
| sound-change-cascade | Science/Linguistics | 1.0 | 0.0 |
| kv-live-surgery | Software/Systems | 1.0 | 0.0 |
| photonic-waveguide-routing | Software/Algorithms | 1.0 | 0.0 |
| bun-sourcemap-leak | Software/Systems | 1.0 | 0.0 |
| production-planning | Operations/Supply chain | 1.0 | 0.0 |
| vf2-speedup-networkx | Software/Algorithms | 1.0 | 0.0 |
| embedding-drift-monitor | ML/Inference | 1.0 | 0.0 |
| shadow-relay | Security/Forensics | 1.0 | 0.0 |
| react-lead-form | Software/Frontend | 1.0 | 0.0 |
| formal-crypto | Security/Cryptography | 1.0 | 0.0 |

## Notes for BUILDER

- Do not copy full task trees here; cards **point at** source paths + commit + Harbor digest.
- All listed jobs used `jobs_dir=~/Developer/helab-curator/runs`.
- TB clone has no separate `original-tasks/`; TB3/Frontier names coincide.
