---
status: historical
audience:
  - operator
  - builder
  - runner
---

# Research status — 2026-09-18

Projection of live catalog, queue state, and `PROGRAM.json`.
Answers what happened yesterday and what is running now deterministically.
Generated catalog snapshot; historical record, not a living contract.

## RECENT (Yesterday: 2026-09-17)

No completed trials observed in the reporting window.

### Evidence Quality Ledger

- **Evaluated Trials:** 320 (Passed: 262, Warnings: 10, Failed: 36, Quarantined: 12)
- **Top Quarantine/Failure Reasons:** `missing_trajectory_file`: 36, `infrastructure_exception:Traceback (most recent call last):`: 12

## RUNNING NOW

Nothing in `queue/running/` or `queue/approved/`.

## NEXT

- `[proposed]` **screening-metered-action-clean4k-zai-opencode-k1** (`01M1JV4X8RMW5JBBB0W00287FN`): task=`registered/action-memory-clean4k`, agent=`zai-opencode` [purpose: baseline]
- `[proposed]` **screening-metered-action-neutral16k-zai-opencode-k1** (`01M1JV4X8SM5TYSG5C60XEZBPY`): task=`registered/action-memory-neutral16k`, agent=`zai-opencode` [purpose: baseline]
- `[waiting]` **canary-event-summary-codex-20260916** (`01M2MEM67KDY1XM56E10P6Z726`): task=`canary/event-summary`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2MEM67KDY1XM56E10P6Z726 --actor <you>
  refuse:    uv run evallab reject 01M2MEM67KDY1XM56E10P6Z726 --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            223h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-event-summary-codex-20260917** (`01M2Q9J3DVEVBF1WXTCGVC1P4Q`): task=`canary/event-summary`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2Q9J3DVEVBF1WXTCGVC1P4Q --actor <you>
  refuse:    uv run evallab reject 01M2Q9J3DVEVBF1WXTCGVC1P4Q --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            250h05m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-event-summary-codex-20260918** (`01M2SKF2CA69Y84JRHSRTGQ2ED`): task=`canary/event-summary`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2SKF2CA69Y84JRHSRTGQ2ED --actor <you>
  refuse:    uv run evallab reject 01M2SKF2CA69Y84JRHSRTGQ2ED --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            271h36m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-terminal-bench-html-js-filter-codex-20260916** (`01M2MEM66944KFZ1V593VNQ3VG`): task=`canary/terminal-bench-html-js-filter`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2MEM66944KFZ1V593VNQ3VG --actor <you>
  refuse:    uv run evallab reject 01M2MEM66944KFZ1V593VNQ3VG --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            223h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-terminal-bench-html-js-filter-codex-20260917** (`01M2Q9J3CB7J7X7AP9S71YDQD3`): task=`canary/terminal-bench-html-js-filter`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2Q9J3CB7J7X7AP9S71YDQD3 --actor <you>
  refuse:    uv run evallab reject 01M2Q9J3CB7J7X7AP9S71YDQD3 --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            250h05m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-terminal-bench-html-js-filter-codex-20260918** (`01M2SKF2B2F6440C252Y5CW50G`): task=`canary/terminal-bench-html-js-filter`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2SKF2B2F6440C252Y5CW50G --actor <you>
  refuse:    uv run evallab reject 01M2SKF2B2F6440C252Y5CW50G --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            271h36m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-transaction-reconciliation-codex-20260916** (`01M2MEM512GJ0TKFEVPG6BYFS2`): task=`canary/transaction-reconciliation`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2MEM512GJ0TKFEVPG6BYFS2 --actor <you>
  refuse:    uv run evallab reject 01M2MEM512GJ0TKFEVPG6BYFS2 --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            223h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-transaction-reconciliation-codex-20260917** (`01M2Q9J1P31Y9S8PFFFRD182YZ`): task=`canary/transaction-reconciliation`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2Q9J1P31Y9S8PFFFRD182YZ --actor <you>
  refuse:    uv run evallab reject 01M2Q9J1P31Y9S8PFFFRD182YZ --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            250h05m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **canary-transaction-reconciliation-codex-20260918** (`01M2SKF0X4A93C9QBGYQVQ3D08`): task=`canary/transaction-reconciliation`, agent=`codex` [purpose: drift] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M2SKF0X4A93C9QBGYQVQ3D08 --actor <you>
  refuse:    uv run evallab reject 01M2SKF0X4A93C9QBGYQVQ3D08 --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         99.0 [observed]
  remaining_percent    1.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-09-07T06:24:34+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-09-06T22:55:57.992000+00:00
  staleness            271h36m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               fix-code-vulnerability__wWg7Wii/agent/sessions/2026/09/06/rollout-2026-09-06T22-55-18-01a078ee-e866-7e52-b9e7-6ecf20b7eb3a.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **funcdag-easy-terra-nightly** (`01M1W4WSTB1G2KVKJHJC9836FZ`): task=`library/tasks/experimental/syn-funcdag-easy`, agent=`codex` [purpose: baseline] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M1W4WSTB1G2KVKJHJC9836FZ --actor <you>
  refuse:    uv run evallab reject 01M1W4WSTB1G2KVKJHJC9836FZ --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         19.0 [observed]
  remaining_percent    81.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-08-31T00:44:18+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-08-24T21:24:15.634000+00:00
  staleness            310h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               gaia2-adapt-hard-1__XJL887e/agent/sessions/2026/08/24/rollout-2026-08-24T21-15-17-01a035a0-ada5-7df1-8efc-ed58e5a50cdd.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **funcdag-easy-terra-proof** (`01M1W4WEEFRE5VZE4V54JQS4HB`): task=`library/tasks/experimental/syn-funcdag-easy`, agent=`codex` [purpose: baseline] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M1W4WEEFRE5VZE4V54JQS4HB --actor <you>
  refuse:    uv run evallab reject 01M1W4WEEFRE5VZE4V54JQS4HB --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         19.0 [observed]
  remaining_percent    81.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-08-31T00:44:18+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-08-24T21:24:15.634000+00:00
  staleness            310h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               gaia2-adapt-hard-1__XJL887e/agent/sessions/2026/08/24/rollout-2026-08-24T21-15-17-01a035a0-ada5-7df1-8efc-ed58e5a50cdd.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **funcdag-hard-terra-nightly** (`01M1W4WSZYNRKGME4A43VD0E1D`): task=`library/tasks/experimental/syn-funcdag-hard`, agent=`codex` [purpose: baseline] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M1W4WSZYNRKGME4A43VD0E1D --actor <you>
  refuse:    uv run evallab reject 01M1W4WSZYNRKGME4A43VD0E1D --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         19.0 [observed]
  remaining_percent    81.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-08-31T00:44:18+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-08-24T21:24:15.634000+00:00
  staleness            310h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               gaia2-adapt-hard-1__XJL887e/agent/sessions/2026/08/24/rollout-2026-08-24T21-15-17-01a035a0-ada5-7df1-8efc-ed58e5a50cdd.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **funcdag-medium-terra-nightly** (`01M1W4WSZD4FX8764A54YCQFRP`): task=`library/tasks/experimental/syn-funcdag-medium`, agent=`codex` [purpose: baseline] — *Reason/Blocker:* paid_run_unauthorized: codex is a billable agent. Paid execution here draws on Peter's ChatGPT/Codex subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M1W4WSZD4FX8764A54YCQFRP --actor <you>
  refuse:    uv run evallab reject 01M1W4WSZD4FX8764A54YCQFRP --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.
Peter's ChatGPT/Codex subscription allowance/policy state (scope: account, NOT the lab; provider-reported):
  used_percent         19.0 [observed]
  remaining_percent    81.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-08-31T00:44:18+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  observed_at          2026-08-24T21:24:15.634000+00:00
  staleness            310h35m old
    a stale reading warns; it never refuses. The reading exists only because a paid trial recorded it, so refusing on age would make the first paid run after any quiet period impossible. Age is printed above precisely because you, not this gate, are the one judging whether it is still true.
  source               gaia2-adapt-hard-1__XJL887e/agent/sessions/2026/08/24/rollout-2026-08-24T21-15-17-01a035a0-ada5-7df1-8efc-ed58e5a50cdd.jsonl
  NOTE: resets_at has already passed, so this reading describes a window that has since rolled over. It cannot refuse anything, and it cannot reassure you either.
- `[waiting]` **screening-metered-event-summary-zai-opencode-k1** (`01M1KXAY7CHKF9YGXV2NAE1R6J`): task=`registered/event-summary`, agent=`zai-opencode` [purpose: baseline] — *Reason/Blocker:* verifier_digest_mismatch: spec verifier_digest 'sha256:bee722a27298eb06f5010b18da7c27295b1ff6236aa03ce58c5e5b1df4d0d61d' does not match registered verifier 'sha256:1f499d550a3c39e1cb3e6ce9f88ac7afcd6ec2e1998bb46d3ce34d4c8e094a15'

### Program Ledger Next Actions

1. **EXP-S02-txn-recon-k** (`status: waiting`): Does changing only attempt count on transaction-reconciliation change interval width more than the point estimate?
   - *Blocker:* k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
   - *Next Action:* Peter: register n=5 or raise ceiling / measure per-attempt cost from 2026-08-15 actual 0.079/3≈0.026 (would be <$3 at k=5) but canary still caps attempts at 3.
1. **EXP-S03-preamble-ab** (`status: designed`): Does a short contract-discipline preamble change Codex pass@3 on event-summary?
   - *Blocker:* ExperimentSpec still has no extra_instruction_path; build_command does not forward --extra-instruction-path (confirmed grep on src/evallab/schemas.py ExperimentSpec).
   - *Next Action:* BUILDER adds the field. Then submit treatment only; pair with 2026-08-15 control. Do not submit a fake second control.
1. **EXP-S04-claude-vs-codex** (`status: designed`): Does claude-code complete a scored event-summary canary trial, and how does pass@3 sit beside Codex?
   - *Blocker:* Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
   - *Next Action:* Peter decides whether a separate authorized workflow should verify/provision the keychain item; only then consider Study 04, without expanding to three tasks first.
1. **EXP-S05-curated-nominees** (`status: waiting`): What is Codex pass@5 on CURATOR's five nominated cards?
   - *Blocker:* Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
   - *Next Action:* Peter registers a slice or promotes nominees with digests. PROGRAM does not copy frontier-bench trees.
1. **EXP-S06-query-optimize-register** (`status: waiting`): Does standing policy admit Codex on lab-authored query-optimize, and is the family valid?
   - *Blocker:* out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
   - *Next Action:* Peter decides whether to register. Do not add to nightly canary suite.
1. **EXP-N2-event-summary-sol-vs-terra** (`status: designed`): On event-summary, does gpt-5.6-sol differ from the already-scored gpt-5.6-terra pin?
   - *Blocker:* Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
   - *Next Action:* Do not submit this draft. Do not approve/reject/delete the proposed spec from this role. Peter decides.
1. **EXP-N3-claude-code-event-summary** (`status: designed`): Can claude-code produce a scored event-summary canary trial?
   - *Blocker:* Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.
   - *Next Action:* Peter decides whether to verify/provision harbor-practice-claude-oauth in a separate authorized workflow; then reassess the existing Study 04 spec.

## TASK DECISIONS

Human-owned, unresolved decisions from active proposals and policy review.

- **EXP-S01-canary-codex-k3**: none on the 2026-08-15 scored jobs
- **EXP-S02-txn-recon-k**: k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
- **EXP-S03-preamble-ab**: ExperimentSpec still has no extra_instruction_path; build_command does not forward --extra-instruction-path (confirmed grep on src/evallab/schemas.py ExperimentSpec).
- **EXP-S04-claude-vs-codex**: Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
- **EXP-S05-curated-nominees**: Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
- **EXP-S06-query-optimize-register**: out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
- **EXP-N1-html-js-official-tests**: tests/test_outputs.py is hidden in the separate verifier and must never be copied, mounted, or made runnable in the evaluated agent image.
- **EXP-N2-event-summary-sol-vs-terra**: Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
- **EXP-N3-claude-code-event-summary**: Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.

## SYSTEM HEALTH & OPERATIONAL SMOKE

- Catalog accessible: yes
- Operational smoke/control specs count: 6
- Active storm alarms: 0 (quiet: no alarms in window)
