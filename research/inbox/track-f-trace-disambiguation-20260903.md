# Track F: two TRACE references

Track F uses two unrelated works named TRACE. They must always be qualified on first mention.

## TRACE-Capability

- Name: **TRACE-Capability**
- Paper: *Turning Recurrent Agent failures into Capability-targeted training Environments*
- arXiv: https://arxiv.org/abs/2604.05336
- Source: https://github.com/ScalingIntelligence/TRACE
- Verified revision: `d2db23085409555b3f13ea426f42d62cf0bbc43d`
- Track F role: primary methodology reference for Tracks B/C capability-deficit extraction and targeted curriculum candidates.
- Exact borrowed concepts: applicability-aware `NA`/`PRESENT`/`LACKING` distinctions and deterministic post-label metrics `Cov`, `ER-`, `ER+`, and `Delta` from `pipeline/aggregate_capabilities.py::compute_metrics`.
- Disposition: **ADAPT methodology only**. Eval Lab does not adopt TRACE's LLM labeling, executable `GameSpec` registry, environment generator, GRPO/LoRA pipeline, or MoE gate.

## TRACE-Benchmark-Evolution

- Name: **TRACE-Benchmark-Evolution**
- Paper: *Towards Self-Evolving Benchmarks: Synthesizing Agent Trajectories via Test-Time Exploration under Validate-by-Reproduce Paradigm*
- Authors: Guo et al.
- arXiv: https://arxiv.org/abs/2510.00415
- Published: 2025-10-01
- Track F role: separate, optional synthesis/validation reference for evolving benchmark tasks through proposal mining, problem formation/free exploration, and multi-level validate-by-reproduce checks.
- Disposition: **optional synthesis reference only**. It is not the source for capability-deficit metrics and must not replace TRACE-Capability in Tracks B/C.

## Required architecture invariant

Tracks B/C follow **TRACE-Capability (2604.05336)** for `Cov`/`ER-`/`ER+`/`Delta` and capability-targeted curriculum methodology. **TRACE-Benchmark-Evolution (2510.00415)** may inform an optional later task-synthesis validation plan only. Neither work becomes an evidence authority, executable admission mechanism, trainer, or replacement for Harbor.

Use the qualified names above on every first mention. Avoid bare “TRACE” wherever both references could be intended.
