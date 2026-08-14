# Rejected candidates

One-line reasons. Sources: frontier-bench `3d694e91`, terminal-bench `4e77c91d` (same 74 task names).

## GPU / accelerator (not locally runnable)

- `exam-pdf-eval` — GPU-dependent
- `fp8-rmsnorm-gemm` — GPU kernel task
- `jax-speedrun-gpu` — GPU-required
- `math-eval-grader` — GPU-dependent

## Cloud / heavy inference / not a laptop canary

- `vllm-deepseek-streaming` — vLLM serving image; not a free local canary
- `sglang-qwen-burst` — LLM serving / burst load; cloud-shaped
- `gpt2-codegolf` — 8 GB + 5 h agent budget; too heavy for lab canary
- `takens-embedding-lean` — 16 GB RAM, 8 h timeout
- `live-database-cutover` — 16 GB RAM
- `payments-pipeline-fix` — 12 GB RAM + Kafka-shaped pipeline
- `memcached-backdoor` — `FROM --platform=linux/amd64` + 12 GB

## Size / runtime / infra (skip before oracle)

- `cumulative-layout-shift` — 400+ files, 3 h, Playwright-scale frontend
- `rs-archive-clone` — 225 files, 4 h
- `medical-claims-processing` — 204 files, 10 GB, multimodal
- `vba-userform-port` — 109 files
- `freight-dispatch-shift` — 86 files
- `nextjs-performance` — Node/Next perf, 65 files
- `distributed-dedup` — Spark/JVM-scale, 5 h
- `layout-config-recreation` / `layout-config-recreation2` — long design/render loops
- `ctr-optimization` — 5 h live-sim marketing
- `intrastat-meldung` — multi-service 3 h
- `lake-temp-glm` — 3 h earth-model
- `atrx-vep-crispr` — 5 h bio
- `biped-contact-dynamics` — 4 h robotics
- `retro-console-soc` — 4 h RTL
- `ks-solver-cpp` — 4 h physics
- `mvcc-lsm-compaction` — 4 h DB engine
- `coq-block-bound` — 4 h Coq
- `wdm-design` — 5 h physics
- `satb-audio-transcription` — 8 GB audio
- `protein-autointerp-disulfide` — 8 GB
- `vpp-loss-divergence` / `pretrain-shard-corruption` / `mp-checkpoint-consolidation` — ML training/checkpoint scale
- `erp-procurement-planning` — Odoo image, not a light canary
- `fix-uautomizer-soundness` — env Dockerfile copies programs into `/app/tests/` (leakage smell)
- `uefi-bootkit` — firmware/forensics, 8 GB
- `hof-topology-interpenetration` — 8 GB chemistry
- `legacy-utility-triage` — 8 GB GUI/VNC-shaped claims
- `heat-pump-warranty` — large claims packet
- `batched-eval-parity` — 4 h eval-harness
- `ontology-kg-querying` — 4 h
- `telecom-entity-resolution` — 2.5 h, 16 h expert estimate
- `lean-midpoint-proof` — 4 h Lean (oracle may be long)
- `cad-model` / `freecad-*` — CAD toolchains, 2.5 h
- `glycan-ms2-elucidation` / `gsea-proteomics` / `roy-polymorph-cn` — science stacks, 2.5 h
- `risk-scorer-replay` — multi-stage ML eval image
- `wal-recovery-ordering` — heavy DB
- `formal-crypto` — SageMath 10.7 stack (screen later if needed)

## Screened later (oracle/nop or verifier)

- `ico-path-patch` — oracle k=3 mean 0.0 (17 pytest failures on patched binary); exclude rather than weaken verifier
