---
status: living
audience:
  - runner
  - analyst
  - operator
---

# Harbor ecosystem corpus

Provenance record for the Harbor-ecosystem material vendored under this directory.
The executable part is `vendor/`: third-party tools pinned by commit, each with a
`PIN` file naming the upstream repository, commit, and licence. Nothing here is a
task package, and nothing here is registered.

## What is here

| Path | Upstream | Pin | Licence | Why |
|---|---|---|---|---|
| `vendor/vestige` | `ASSERT-KTH/vestige` | `d03e8a7f1bc2e7dc372b193a1ea2d81da08741fe` | MIT | pass@k vs pass^k reliability statistics over repeated trials |
| `vendor/deepagents-harbor` | `langchain-ai/deepagents` | `07d2952d346d81d06bd181db8c560a77f2b51bc8` | MIT | Wilson intervals, minimum detectable effect, infra-vs-capability failure classification |
| `vendor/agent-data-protocol` | `neulab/agent-data-protocol` | `040a279b46b2388ae42b43449f8645b9781c7bf7` | MIT | raw -> ATIF -> standardized -> SFT serialization reference |
| `vendor/atifact` | `waldekmastykarz/atifact` | `db0bf0fcac29adc26778185383d42f2d7c8ceba6` | MIT | converts non-ATIF agent logs (Codex CLI, Claude Code, HAR) to ATIF v1.7 |
| `vendor/harden-v0` | `few-sh/harden-v0` | `342b8474e0c0cf96e4a8313fd2e26c7a11d51193` | Apache-2.0 | adversarial verifier-hardening loop with an exploit journal |

Vendored trees are excluded from this repository's linters (`pyproject.toml`
`extend-exclude`): reformatting a pinned copy would desync it from the commit its
`PIN` names.

## Contamination class — binding

Everything reachable from this corpus is **behaviour-study and format-reference
material only**.

- Model **exposure is unknown and unknowable** for any public task pack or
  trajectory corpus referenced here. Do not treat a pass under our harness as
  evidence about the capability the pack was built to isolate.
- External verifiers were validated under their authors' harness and task-version
  assumptions, not ours. Passing one here measures transfer, not capability.
- No number derived from external packs or corpora enters a capability claim, a
  reliability curve presented as agent capability, or an eval card's Result
  section. They may appear in Methods/Transfer-observations, labelled with pack
  name, revision, trial count, and the exposure caveat.
- Never train on a split that is also evaluated. Imported outcomes stay the
  original authors' outcomes, flagged external, never recomputed into ours.

The per-track statement of the same rule, with the observed evidence behind it,
is `research/analysis/harbor-native-track/contamination-claim-boundaries.md`.

## fetch ≠ register

Acquiring or vendoring anything here **never** registers a task, a dataset, or a
verifier. Registration is a separate, human-only decision recorded in
`library/registry/`; `evallab registry audit` is its authority. Acquisition is
pinned-only: an unpinned ref (`latest`, `head`, `main`, `master`) is refused by
`evallab.fetch`, and a vendored tree without a `PIN` file is not a source of
truth.

## Not acquired

Deliberately **pending**, with the reason recorded so an absent directory is
never mistaken for missing data:

- `harbor-index` rollouts — 1,476 trials are not publicly enumerable; see
  `research/external/harbor-index/README.md` for every probe and its result.
- Recovery-Bench — STRICT HOLD (no root licence; upstream replay failed 11/20
  audited instances). The native substitute is `harbor traces export
  --filter failure` plus `--load-trajectory`.
- GPU RL stacks (SkyRL, NeMo Gym, Prime `verifiers`) — read-only; this
  workstation has no CUDA.
