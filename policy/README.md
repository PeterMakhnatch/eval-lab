# Standing approvals and canary policy

## Purpose
`policy/` defines the human operator's steering wheel for eval-lab. It holds
committed standing approvals, budget ceilings, and pinned canary suites governing
what the lab is permitted to execute unattended.

## What lives here / entry points
- `policy/standing-approvals.yaml`: Operational bounds, daily cost ceilings, and auto-run authorizations.
- `policy/canary-suite.yaml`: Pinned canary task specifications for drift detection.
- `policy/continuous-loop-policy.example.yaml`: Example configuration template for continuous loops.

## Invariants or rules
- Human steering wheel: `policy/` contains Peter-owned standing approvals and canary suites placed at root for visibility (cited in `agents/STRUCTURE.md`).
- Agents never loosen policy: Agents must never weaken standing approvals, raise budget ceilings, or authorize unapproved billable runs (cited in `AGENTS.md`).
- No billable auto-run: The committed standing policy never permits billable agents under auto-run rules without explicit human approval (cited in `tests/test_paid_authorization.py` and `AGENTS.md`).

## Tests or checks
- `uv run pytest tests/test_paid_authorization.py`
- `uv run pytest tests/test_canary.py`
- `uv run pytest tests/test_repository_contract.py`

## What not to add here
- Do not add agent-authored policy relaxations or temporary execution overrides.
- Do not store task definitions or run results here; use `library/` or `runs/`.
- Do not add unapproved service configurations or credential tokens.
