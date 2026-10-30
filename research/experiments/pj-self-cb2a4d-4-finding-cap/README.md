# pJ-SELF-cb2a4d-4: per-audit finding budget, three versus six

Preregistered in the bridge `opus-vs-swarm-qa.md` before any send. `cap_driver.py` audits the 44 keyed DEV tasks with the
frozen OQ-6 B prompt, no tools, in three arms: **T3** (terra, unchanged prompt: noise control), **T6** (terra, `THREE`
-> `SIX`) and **S6** (Sonnet 5.5, same edit). The production parse and gate are reused; only the three-finding budget check
and the parser's five-candidate limit are relaxed, and only for the SIX arms. `judge_arms.py build` makes one sol key-judge
prompt per (arm, task) from each audit's gate-eligible findings alone (baselines B0 = Sonnet B from OQ-6 v4 and A0 = terra A
from OQ-8 are rebuilt the same way); `metrics` applies the preregistered rule. Tasks whose audit was invalid in any arm are
excluded from every arm, never counted as zero. DEV keys only; cross-test untouched.
