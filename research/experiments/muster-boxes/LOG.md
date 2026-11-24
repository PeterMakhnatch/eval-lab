# Running log for Peter

Times are New York time (ET). Newest entry first. Each entry says what is done and what is observed.

## Next 3 items (written 11:02 AM ET, 2026-11-19)

1. Finish the 4 lanes that stopped at the Fable usage limit (end-of-play freeze, wrapped-secret redaction,
   tidegauge seat support, the `scarce` family). New agents on Opus 5.5 continue from the saved work.
2. Push the per-seat secret scrub fix and open draft pull requests inside the fork for the finished fixes.
3. Start the first scored experiment (E1, `boxes`, 5 arms x 10 instances x 3 attempts, DeepSeek Flash
   with thinking off, about $1.50) and report it with `tidegauge`.

## 2026-11-24

**3:35 AM. E8 `standin` is pre-registered (Amendment 12). E9 runs. The task family `standin` is on `main`.**
- E9 timing: Amendment 11 was committed at 2:50:20 AM ET and pushed to a branch on the server at 2:51:42
  AM; the first scored trial started at 2:51:58 AM; the commit reached `main` at about 2:54 AM (the land
  queue held 4 to 5 lands of other lanes). `main` holds the same commit, `1b3d69092`.
- E9 so far (health only, no score read): `team-8` and `team-48` ended with 7 of 7 trials and 0 stalled
  seats; `zones-48` runs.
- `standin` landed at `35624d017` after 3 passes of an independent review. Pass 1: 3 blockers (the credit
  rule, a verdict in the unused-claim trap, the timing of the promised report). Pass 2: 1 blocker (a new
  credit exception gave credit for a box that a stand-in had opened; it switched off the stale-read trap
  for an agent that posts numbers early, as 19% of the real agents in E5 and E7 do). Pass 3: land, with 2
  notes that are also in the family (1 fixed form for the announcements; 2 more known limits).
- An independent review of the E8 draft said "fix first": the `naive` check roster could not run (1 script
  for each cell); the check rule compared 1 trial with a mean over 10 layouts; the rerun folder layout
  crashed the analysis; in `wait-t` the 30 s rule lets a slow agent pass without a wait on purpose; the
  `deadclaim` test was true by construction for an agent that obeys its rule; the E9 watcher would stop E8
  early. I took the reviewer's better primary test for `deadclaim` (released claims) and its text changes.
  A second pass found a rounding fault in my comparison script and 2 gaps in the money gates; all 3 are
  closed.
- Analysis code on `main` before any E8 trial with a model: `3c4b19d1d` (`analysis/standin_probes.py`).
- `campaign`: the fixes for the 3 blockers of the first safety review landed (`f0640316b`). A second review
  says go for a scripted run and "fix first" for a paid run: the new match of Harbor by its command line
  also hits read-only tools, and 1 process with several lanes stops them one after another. A third round
  of fixes is in work. E8's scripted `oracle` check is its first run with a real Harbor.
- F26: 1 sentence corrected (the 43 repeats of E5 `team-8` are of a box that any post read by the agent had
  named; a plan or an intent counts there).
- Money at this time: $5.06 of the $10.

**2:50 AM. E9 `horde` is pre-registered (Amendment 11). 3 pilot trials with 48 real agents.**
- Pilot, 1 trial each on `horde-i01`, DeepSeek Flash, Daytona with the 4 vCPU snapshot, not scored:
  `zones-48` 0.9875 (1 repeat, $0.048, 168 s), `team-48` 0.4875 (36 repeats, 162 messages, $0.107, 231 s),
  `chief-48` 1.0 (0 repeats, 113 messages, $0.081, 293 s). 0 of 144 seats ended with an exception. Folder
  `muster-runs/night-2026-11-24/e9/pilot/`; timeline pages in `night-2026-11-24/storyboards/`.
- An independent review of the draft, the task copy and the run scripts said "fix first": the analysis code
  was not on `main` and its draft needed `team-32`, which E9 does not run; no automatic stop existed for
  Daytona time against the $10 limit. It also asked for absolute paths in the command, 2 reruns for trials
  with a stalled seat, the arm order `team-8`, `team-48`, `zones-48`, `chief-48`, `team-16`, `solo-96`,
  exact definitions for the statistics, corrected facts, and expectations with ranges. All are in Amendment
  11. A second pass said "commit and run" after 2 more sentences (a wrong statement of mine about the E6
  single agent, and a check that the watcher runs).
- New run files in `muster-runs/night-2026-11-24/`: `night_lane.sh`, `night_arm.sh` (as the E7 scripts; a
  stop signal now ends the lane), `night_watch.sh` (writes the STOP file when less than a given amount of
  the $10 is left), `night_money.py` (model cost since 10:45 PM plus Daytona time at list price).
- Analysis code on `main` before any scored trial: `2ff1a4366` (`analysis/horde_scaling.py`;
  `crowd_scaling.py` counts `GET /new` as a board read; the registered E5, E6 and E7 outputs are
  byte-identical). `d9c2de245`: `storyboard.py` draws `GET /new` reads. `e5ff4f279`:
  `analysis/standin_probes.py` for E8.
- Money at this time: $3.81 of the $10.

**2:08 AM. E5 is complete and audited (F25). Night tools are on `main`. A blind coding study (F26).**
- Peter's instruction at about 12:15 AM ET: work through the night, mostly systems work and benchmark
  crafting for multi-agent evaluation, with $10 for experiments. Plan: `muster-runs/night-2026-11-24/PLAN.md`.
- The lane for the E5 arms that were left open ran from 10:45 PM to 1:46 AM ET: 249 trials with a reward,
  $1.68, 0 stalled seats. 1 `solo-6` trial failed before the agent started and ran again once. Result:
  `claim-2` 98.2%, `claim-3` 94.3%, `claim-4` 86.8% (`claim-8` 73.5%); `indep-3` 42.5%, `indep-4` 38.8%;
  `solo-8` 26.3%, `solo-6` 19.8%, `solo-3` 6.9%; the route anchor `team-3-e1` 84.8% against 85.7% in E1.
  Output: `muster-runs/e5/analysis/e5-full.txt` and `.json`; merged trials `muster-runs/e5/final-full/`.
- An independent audit ran before the text. Every number matched; the reward equals the event log in 449 of
  449 trials. Its findings are in F25: the new arms ran 8 hours after the arms that they are compared with
  (a shift of about 10 points is not excluded); the contrast against `union-k` does not measure
  interference; the blocking of claimed boxes starts at 3 to 4 agents and the 30-turn cap binds the claim
  arms; `indep-2` stays at 18 trials; the folder names do not separate first runs from reruns.
- Tools that landed in the night, each built by a subagent in its own worktree and checked by me or by an
  independent reviewer:
  - `7c31b6824` `tidegauge`: findings for stalled, lost, zero-call and turn-cap seats and for trials that
    never started; `tidegauge process` (trials at once over several run folders); `tidegauge live`. On the
    E6 `team-8` folder it marks the 6 trials and 14 seats of F23.
  - `08049782d` `campaign`: a runner that reads 1 plan file (arms, lanes, money floor, cap, stop rules,
    reruns). A safety review with fake processes found 3 blockers, all of 1 kind: the runner can exit, or
    start the next arm, while Harbor still runs. The fixes are in work. It has not run a paid campaign.
  - `f1ed0404c`, `01f6f2b3e` `crowd_rules.py --parser v2` (post hoc numbers in F24).
  - `ab8ccb8a6` `analysis/seat_codes.py` and `2258b8a6e` `analysis/storyboard.py` (a timeline page for 1
    trial and an overview page for an arm; examples in `muster-runs/night-2026-11-24/storyboards/`).
  - `1994fccbf` the task family `horde`: 240 boxes, 24 rooms, 80 flags, 96 opens in total, for 1 agent and
    for 8, 16, 32 and 48 agents with no rule, a fixed plan (`zones`) or a coordinator (`chief`). 2 review
    rounds. Scripted check on Daytona (the best policy in every seat, no model): 8 of 8 trials score 1.0,
    3 of them with 48 seats, on the 2 vCPU / 4 GiB sandbox. No real-agent result yet.
- Fork: draft pull request 114 in Peter's fork records the last agent step of each seat, marks a seat that
  stalls, and offers an idle limit. It marks the same 14 seats of F23.
- Blind coding study of 96 transcripts with 4 raters, and a script that applies the log rules to 27 arms:
  F26, `muster-runs/forensics-2026-11-24/REPORT.md`.
- Research files: `research-context/multi-agent/2026-11-24-behaviour-probe-designs-and-run-validity-evidence.md`
  and `2026-11-24-muster-failure-mode-coding-of-team-transcripts.md`. One correction to an earlier text of
  mine: Generality Labs did vary the team size at a fixed total budget (`RESULTS.md`, other work).
- Money of the night so far: $1.68 of model cost and about $1.79 of Daytona time at list price (10.6
  sandbox hours). Recorded model cost of E5, E6 and E7: $10.01.
- Next: the task family `standin` (1 real agent among scripted teammates, 4 behaviour probes) is in its
  second review; then its pre-registration (E8) and a first run of `horde` with real agents.

## 2026-11-23

**10:49 PM. E7 done and audited (F24). The E5 extra arms run.**
- The 2 E7 lanes ran from 9:28 PM to 10:36 PM ET: 9 arms, 180 trials, every trial with a reward, 0 stalled
  seats (Amendment 10a did not apply), 0 reruns, $2.03. E5 + E6 + E7 are at $8.33 of the $12 cap.
- Result at 8 seats: `team-8` 58.2%, `zones-8` 97.0%, `chief-8` 94.3%, `share-8` 45.0%, `solo-24` 100.0%.
  Primary: `zones-8 - team-8` = 38.8 [34.2, 43.8], helps; `chief-8 - team-8` = 36.0 [31.2, 40.8], helps;
  `share-8 - team-8` = -13.2 [-25.2, -1.3], p 0.074, not resolved. Outputs:
  `muster-runs/e7/analysis/e7-all-arms.txt`, `.json`, `e7-rules.txt`, `.json`, `e7-baseline-*.txt`.
- An independent audit ran before the text. Every number matched; 2 interval ends differ by 0.5 points from
  the bootstrap seed. The reward equals distinct flag boxes / 20 in 180 of 180 trials. Lanes, order, peak of
  8 trials, settings, task copy, money rules and the stop rule agree with Amendments 10 and 10a. Its findings
  are in F24: `chief` obedience is not measured at both sizes; the `chief-4` loss comes from 1 reading of the
  rule text in 7 of 20 trials; `solo-24 - zones-8` lies on the 5-point margin; 2 of the 3 pilot trials had
  ended when Amendment 10a was committed; I looked at 8 complete arms 5 minutes before the last arm ended.
- The pre-registered blind hand check of 40 coordinator posts (a second agent that saw no score): 0 wrong
  lists, 23 exact, 11 refused by the parser, 6 without a list. File:
  `muster-runs/e7/analysis/chief-hand-check.json`. Sampler: `muster-runs/e7/analysis/chief_posts.py`.
- The lane for the E5 extra arms (Amendment 10, last in the order) started at 10:45 PM ET: 1 lane, 4 trials
  at once, `solo-8 solo-6 solo-3 indep-3 indep-4 claim-4 claim-2 claim-3 team-3-e1`.
- Audit scripts: `muster-runs/infra-evidence-2026-11-23/e6-audit/` and `e7-audit/`.

**9:36 PM. E6 done and audited (F23). A model route fault in `team-8`. Amendment 10a. E7 runs.**
- Peter added credits at 7:06 PM ET (the account then held $29.96). The E6 lane ran from 7:08 PM to 9:23 PM:
  12 arms in the registered order, 8 trials at once, 300 trials, every trial with a reward, $3.27. With
  `solo-72` of the afternoon, E6 has 13 arms, 320 trials and $3.36.
- Result: `solo-72` 70.5%, `team-8` 46.4%, `scout-8` 40.8%, `indep-8` 32.0%, `solo-9` 7.7%. Primary 1
  (`team-8 - indep-8`) = 14.4 points [8.9, 20.8], resolved. Primary 2 (`solo-72 - team-8`) = 24.1
  [16.6, 31.8], 1 agent is better. Primary 3 = 21.6 [13.6, 28.8], pass. Output:
  `muster-runs/e6/analysis/e6-all-arms.txt` and `.json`.
- OBSERVED, a fault: the first 8 trials of `team-8` started at 7:08:02 PM. 14 of their 64 seats got no model
  response after 7:08:55 to 7:09:00 PM and ended at the 600 s limit with 0 opens (6 trials). No other arm of
  E5 or E6 has such a seat. I found it in the exception counts; the audit found it on its own. The trials
  count (Amendment 8). The check without them is in F23 (`e6-team8-without-stalled.txt`).
- Amendment 10a (committed at 9:26:11 PM, before the E7 pilot ended and before any scored E7 trial): in E7,
  a trial with such a stalled seat is unmeasured and runs again once. Detector: `muster-runs/stalled.py`.
- An independent audit ran before the E6 text. Every number matched (largest difference at an interval end:
  0.29 points, from the bootstrap seed). The reward equals distinct flag boxes / 80 in 320 of 320 trials; no
  seat opened more than its budget; all 980 seats have thinking off and the provider pin; the task copy equals
  the landed build. Its findings are in F23: the stalled seats; 12 trials at once at 3:50 PM when `solo-72`
  started next to 2 E5 rerun lanes; "interference" was the wrong name for `indep-k - union-k`;
  `solo-36 - team-4` has an interval above 0 but a test p of 0.08; 50 scout seats stop early with a false
  belief; the recorded cost is a list price, not a bill.
- E7 pilot (not scored, 9:23 PM to 9:26 PM): `zones-8` 1.0, `chief-8` 0.9, `share-8` 0.55; 0 exceptions; the
  analysis scripts ran on it. No plumbing fault. The 2 E7 lanes of Amendment 10 started at 9:28 PM.

**5:46 PM. Fork PRs #101 and #102 for the seat-count defects (drafts, base `mirror/olam-main-711ca65a`).**
- #101: Daytona sets `COMPOSE_BAKE=false`, as the kernel Docker environment does (+1 line, 3 tests that fail
  on head). Live at 120 seats on the 4 vCPU snapshot: the build passed in 2 of 2 trials on the branch and
  failed in 1 control trial on upstream head (`NotFound ... no such job`). $0.07 of sandbox time.
- #102: a test only. Upstream head already raises an error when a scripted seat exits non-zero (`f42969ac`);
  the fork's `main` at `6aeb609e` does not. The test fails with the `6aeb609e` code.
- OBSERVED in the proof run: 1 of the 2 branch trials at 120 seats lost 2 seats with `ConnectionResetError`
  on 4 vCPU and scored 0.19. On head code the trial result names the seat. So the larger sandbox makes the
  kit's listen-queue defect rare, not absent; F22 is corrected.
- The fixed 120 s limit for `docker compose up` and the sessions that stay open are in the body of #101 with
  their numbers, without a code change (they did not fail in a run).
- No sandbox is left on Daytona. Snapshots: `muster-dind-mirror-v1` and `muster-dind-mirror-4c8g`.

**5:43 PM. Seat-count probe (F22): 48 seats pass, 64 to 80 can give a wrong score with no error, 120 pass on a larger sandbox.**
- A worker ran 37 scripted trials on Daytona with 8 to 120 seats (no model call, $0.37 of sandbox time at
  list price). The table is in F22. Report and run folders:
  `muster-runs/infra-evidence-2026-11-23/seat-scale/` (`REPORT.md`).
- I checked its run folders: 37 trials; the trial of `k64-x3` with reward 0.2625 and the 3 trials of `k80-x3`
  with rewards 0.9875, 0.3 and 0.275 have no exception in `result.json` and 1 seat with a non-zero
  `exit-code.txt`; the 5 trials at 120 seats with `COMPOSE_BAKE=false` on 4 vCPU and 8 GiB have reward 1.0.
- 6 defects are in the report with file, line and patch text. 4 are in the fork: Daytona does not set
  `COMPOSE_BAKE=false`; `docker compose up` has a fixed limit of 120 s; 1 session for each command stays open;
  the scripted agent does not report a non-zero exit. 2 are in the frozen task kit: the world server's listen
  queue of 5, and a policy that retries only refused connections. A worker prepares draft pull requests on the
  fork for the first and the fourth. The kit defects need a new kit version; they do not touch trials with 8
  seats or fewer (0 dropped connections at 8 to 24 seats).
- New snapshot for large trials: `muster-dind-mirror-4c8g` (4 vCPU, 8 GiB, the same registry mirror).
- F19 has 1 more check, not pre-registered: without the 8-seat arm the slope is -14.3 points for each
  doubling [-25.5, -3.0], p 0.047 (`muster-runs/e5/analysis/e5-slope-without-k8.txt`).
- The account is still empty at 5:42 PM. No model trial ran since 4:05 PM.

**5:14 PM. E7 `crowdrules` is ready and pre-registered (Amendment 10). 0 model trials: the account is empty.**
- Family `muster/tasks/crowdrules` (landed at `e356b421a`): `team-k`, `share-k`, `chief-k`, `zones-k` at k = 4
  and 8, 10 instances each, 24 opens in total. Best play under each rule: 1.0, but 0.98 for `share-8` and 0.995
  for `share-4` (exact, by a dynamic program). The simulation has 2 new checks (the oracle never repeats a box;
  the oracle never opens a box outside its rule); a planted fault fails 3 seeds. 14 scripted trials on local
  Docker and 8 on Daytona (1 for each condition) equal their reference values.
- Analysis (landed at `d02fae0e9`): `crowd_rules.py` measures obedience (the share of opens that the rule
  allows); `crowd_scaling.py` reads the rule arms and prints the 3 primary contrasts with Holm's rule. Checked
  on copies of E5 arms: `claim-8` under the label `zones-8` gives again 19.2 [14.0, 24.5] against `team-8`. E5
  and E6 output is byte-equal before and after the change.
- OBSERVED, a new reference value: E5 teams scored as if a rule applied obey it in 14% (`share-8`), 28%
  (`zones-8`), 26% (`share-4`), 27% (`zones-4`) and 9% (`chief-8`) of their opens. A team that ignores the
  `share` rule would be at 1 / k.
- An independent review came before the commit of Amendment 10. It found 4 blockers and 13 smaller points:
  - The E7 lane would have used the plain Daytona image, the one that hit the pull limit (F21).
  - The floor check started an arm when the balance could not be read. The E5 and E6 lane scripts had the same
    fault. All 3 now stop.
  - The named analysis could not read the E7 arm labels and could not pair `solo-24` with the rule arms.
  - `chief` obedience had no definition for free text. In E5, p1 wrote 0 lists as JSON. The parser now returns
    "not parsed" when it is not sure: 20 exact lists, 17 "not parsed" and 0 wrong lists on 40 hand-labelled
    plan posts of E5.
  - The smaller points are in the amendment: the contrast is the whole rule paragraph and not ownership alone,
    reruns and missing data, the stop rule over both lanes, the money range and the reserve, the exact decision
    rule, the lanes and their commands, the pins, the power.
- A fault of mine: I edited `run_arm.sh` while 5 scripted checks ran it. All 5 broke before a result ($0, no
  model call; the ledger has a `void` line). The E7 runner is a new file, `e7_arm.sh`.
- OBSERVED: 1 of 9 scripted Daytona trials for E7 failed in the image build inside the sandbox
  (`failed to receive status: rpc error: code = Unavailable desc = error reading from server: EOF`). The
  second trial passed. E7 stops when 3 trials in a row end without a reward.
- To run when credits exist, in this order:
  - E6, 1 lane in the registered order with 8 trials at once: `E5_FLOOR=1.00 ~/Developer/muster-runs/e6/e6_lane.sh
    8 team-8 indep-8 scout-8 solo-9 solo-18 team-2 indep-2 solo-36 team-4 indep-4 scout-4 scout-2`.
  - E7 pilot: `~/Developer/muster-runs/e7/e7_arm.sh ~/Developer/muster-runs/e7/tasks/crowdrules/zones-8/crowdrules-i01
    zones-8 1 1 0.05 ~/Developer/muster-runs/e7/pilot` (the same for `share-8` and `chief-8`).
  - E7: the 2 lane commands of Amendment 10.
  - E5 extra arms: `E5_FLOOR=1.00 ~/Developer/muster-runs/e5/e5_fill_lane.sh 4 solo-8 solo-6 solo-3 indep-3
    indep-4 claim-4 claim-2 claim-3 team-3-e1`.
  - E7 analysis, from `analysis/`: `python3 crowd_scaling.py --total-budget 24 --planned-trials 20 --arm
    solo-24=$R/solo-24 --arm team-8=$R/team-8 --arm zones-8=$R/zones-8 --arm share-8=$R/share-8 --arm
    chief-8=$R/chief-8 --arm team-4=$R/team-4 --arm zones-4=$R/zones-4 --arm share-4=$R/share-4 --arm
    chief-4=$R/chief-4` and `python3 crowd_rules.py` with the same 8 team and rule arms.

**4:31 PM. Independent audit of F19 to F21, and the corrections. Fork sync table.**
- A worker that did not run E5 recomputed the results with its own code (no import of `crowd_scaling.py`,
  other bootstrap seeds): every arm mean, the budget split, messages, costs, both primary tests, the
  contrasts, `union-2` and the missing-as-zero check match; every interval end is within 0.25 points. The
  reward equals `reward.json` in 200 of 200 trials, and no seat opened more than its budget. Thinking is off
  in all 817 seats, and every seat is pinned to `deepinfra/fp8` without fallback. The `team-k` instruction
  texts differ only in the numbers. No primary verdict changed.
- The audit found errors in my text, now corrected in `FINDINGS.md` and `RESULTS.md`:
  - Wrong numbers: the F19 heading said $2.64 (it is $2.68); "160 seats at once" (the peak was 96 seats in 20
    trials); the Daytona latency sentence compared an agent phase with a whole local trial (115 s against
    43 s is right); "286 trials started" at 3:52 PM (290); the `solo-12` cost was missing ($0.0035).
  - Overclaims: "8 random agents would repeat 3.8 opens, the team more than twice that" used the wrong
    chance level (random choice among the 20 flag boxes repeats about 9.5); "all the growth is in later
    repeats" ignored that round 1 gets shorter as k grows (the repeat rate in round 1 doubles); "a free
    channel recovers about half" (it is 28.5 of 74.2 points); 2 sentences compared E5 with F11 and F14, which
    E5 cannot support without an anchor; "a rule must give each box 1 owner" (no such rule was tested).
  - Deviations that I had not disclosed: 5 lanes (20 trials at once, not 8); the order of Amendment 7a was
    not kept inside the lanes; the order of Amendment 9 was not kept (both groups ran at once, so the last
    credits went to `indep-2` reruns and not to `solo-8`, `solo-6`, `solo-3`); Amendment 9 was written after
    the primary results were seen. F19 now lists them.
  - A bug on `main`: `crowd_messages.py` called `parse_arm` with the old signature and did not run after the
    per-seat mode landed. Fixed in this commit; its output for E5 is in `muster-runs/e5/analysis/e5-messages.txt`.
- Lesson for the next experiment: run the audit before the results land, not after.
- Fork: a worker ran a trial merge of all 84 open pull requests into upstream head `711ca65a`, read-only
  (`research-context/multi-agent/olam-labs/2026-11-23-fork-main-sync-impact.md`). `main` can fast-forward. Of
  the 33 pull requests against `main`, 16 are clean, 6 conflict only in changelog text, 7 have code conflicts,
  3 are partly superseded and 1 is superseded. Upstream's change of the upload code is the main cost. 1 clean
  merge fails a test, so "clean" is not "safe". The decision to sync is Peter's.

**4:09 PM. The OpenRouter account is empty. E5 and E6 stop here until credits are added. F19, F20, F21.**
- OBSERVED: the account held $0.16 at 4:04 PM. The last 2 `indep-2` reruns failed with "This request would
  exceed your available credits". My lane spent $2.99 of the $23.19 that the account held at 2:25 PM.
- E5 final count: 313 trials started, 200 with a reward, 111 failed at the pull limit, 2 at the empty account.
  23 failed trials were run again through the mirror (21 with a reward). Complete: `solo-24`, `solo-12`,
  `team-2`, `team-3`, `team-4`, `team-8`, `claim-8`, `indep-8` (20 each) and `indep-2` (18). Incomplete:
  `indep-3` 7, `indep-4` 11, `claim-4` 4. Not run: `claim-2`, `claim-3`, `team-3-e1`, `solo-8`, `solo-6`,
  `solo-3`. Regrade matches for every checked arm (`solo-12` 20 of 20, `indep-2` 18 of 18).
- New numbers since 3:52 PM: `solo-12` 45.2%, `indep-2` 67.5%, `union-2` 52.0%. `team-2 - indep-2` = 16.5
  points [5.3, 29.2]; `indep-2 - union-2` = 15.5 points [4.2, 25.0]. Output:
  `muster-runs/e5/analysis/e5-all-arms.txt` and `.json`. Check with the 2 unmeasured `indep-2` trials counted
  as 0 (the only counted arm that has any): `indep-2` 60.5%, `team-2 - indep-2` 23.5 [12.0, 35.2],
  `indep-2 - solo-12` 15.2 [4.5, 25.8], `indep-2 - union-2` 8.5 [-5.5, 21.8]. So the last contrast is not
  robust; the other 2 keep their sign.
- E6: 1 scored arm is complete, `solo-72` = 70.5% (se 3.0, 20 trials, $0.096; 93.5% of best scripted play;
  no seat reached 30 turns; regrade 20 of 20). The other 12 arms wait for credits.
- F20 comes from `analysis/crowd_messages.py` (a worker; landed `8a3108e79`). `crowd_scaling.py` has a
  per-seat-budget mode with the E6 tests (landed `ec0a8afd3`).
- New in the kit: `muster/probes/isocheck.sh`, the seat isolation probe (a seat serves a port, posts its
  addresses on the channel, and tries to reach every other seat and the internet). Run it as a scripted seat
  on a task with a channel: roster `{"each": {"import_path": "muster.agents.script:ScriptAgent", "kwargs":
  {"script": "muster/probes/isocheck.sh"}}}`, then read `seats/*/agent/script.txt` for PASS and FAIL lines.
- Spend today: OpenRouter $2.99 (ledger `muster-runs/e5/spend-ledger.jsonl`, 44 settled entries: E5 scored
  $2.68, E5 pilots $0.13, E6 scored $0.10, E6 pilot $0.08). Daytona: 11.8 sandbox hours, about $2.00 at list
  price (2 vCPU, 4 GiB), plus $0.05 for the probes.
- To continue when credits exist (no other change needed):
  `E5_HARBOR=~/Developer/muster-runs/e5/harbor-daytona-mirror.sh E5_FLOOR=1.00
  ~/Developer/muster-runs/e6/e6_lane.sh 4 team-8 indep-8 scout-8 solo-9 solo-18 team-2 indep-2 solo-36 team-4
  indep-4 scout-4 scout-2`, and for E5
  `~/Developer/muster-runs/e5/e5_fill_lane.sh 4 solo-8 solo-6 solo-3 indep-3 indep-4 claim-4 claim-2 claim-3
  team-3-e1` with the same 2 variables. Expected cost: E6 about $2.90, E5 about $2.00.
- Next experiment after E6 (E7, not registered): rules that give each box 1 owner at 8 seats without a
  message for each box: a fixed share by seat name, 1 agent that assigns, and a claim that the world enforces
  and that expires (the last one belongs to the lane that builds server-side claims).
- Fork, after the entry above: #99 (draft, base `mirror/olam-main-711ca65a`) adds a `registry_mirrors` option
  to the Daytona DinD environment. It writes `/etc/docker/daemon.json` before `dockerd` starts, merges an
  existing file, and refuses URLs with credentials. With `dind_snapshot` it warns that resource overrides do
  not size the sandbox and logs the real size. 32 new unit tests pass. A live sandbox lists the mirror in
  `docker info`. Docker Hub did not rate-limit any more at 21:07Z, so the control did not fail.

**3:52 PM. E5 primary arms done ($2.54, 290 trials started). Docker Hub pull limit on Daytona, fixed.**
- Result of the complete arms (20 trials each, 0 exceptions, 140 of 140 rewards replay with `muster regrade`
  against the clean landed `crowd` tree): `solo-24` 100.0%, `team-2` 84.0%, `team-3` 79.2%, `team-4` 69.2%,
  `team-8` 54.2%, `claim-8` 73.5%, `indep-8` 25.8%.
  - Primary test 1 passes: the slope of `team-k` is -15.5 points for each doubling of k, 95% interval
    [-20.1, -10.7], exact sign-flip p 0.002.
  - Primary test 2 fails: `solo-24 - claim-8` is 26.5 points [20.8, 32.3], above the 5-point margin. The claim
    rule adds 19.2 points [14.0, 24.5] at 8 seats. It removes the repeats (8.4 to 1.25 of 24 opens) but 2.6
    opens stay unused, and a trial costs $0.050 against $0.018 for `team-8` and $0.0017 for `solo-24`.
  - Output: `muster-runs/e5/analysis/primary-arms.txt` and `.json`.
- Incident, 20:38Z to 20:43Z: `docker compose build` inside new Daytona sandboxes got `429 Too Many Requests`
  from Docker Hub for `python:3.13-slim`. Every sandbox pulls the base image again, and Daytona's addresses
  share one anonymous limit. 111 E5 trials failed before any seat started (Amendment 9 has the counts). The 5
  lanes ran on and marked 2 arms as started with 0 rewards. I stopped the lanes at 3:41 PM.
- Fix (a worker, 15 minutes, $0.03 of Daytona): a Daytona snapshot `muster-dind-mirror-v1` = the same
  `docker:28.3.3-dind` image plus `/etc/docker/daemon.json` with `registry-mirrors: https://mirror.gcr.io`, at
  2 vCPU and 4 GiB. Harbor takes it with `--ek dind_snapshot=muster-dind-mirror-v1`
  (`muster-runs/e5/harbor-daytona-mirror.sh`). A control sandbox on the plain image still got 429 at the same
  time. 3 scripted trials equal their reference values, regrade matches, and the isolation probe passes
  (20 of 20 checks for each seat). Trap: with a snapshot, Harbor passes no resources, so `--override-cpus` and
  `--override-memory-mb` are recorded in `result.json` but have no effect; the snapshot's size applies.
- New tools in the run folder: `e5_fill.py` (plans the reruns of trials without a reward, merges the arm
  folders) and `e5_fill_lane.sh`. A failed trial stays on disk; it is never counted as 0.
- Money at 3:48 PM: $2.38 in the account, still falling while E5 was stopped. Amendment 9 sets the order for
  the last credits. E6 `field` is pre-registered (Amendments 8 and 8a); its pilot (not scored, $0.08) gave
  `solo-72` 75.0% (the best scripted play), `team-8` 53.8%, `scout-8` 58.8%, `indep-8` 16.2%.
- Fork: a worker brought the 3 open muster PRs to upstream head `711ca65a` as drafts against a new mirror
  branch `mirror/olam-main-711ca65a` (the fork's `main` is untouched): #96 wrapped secrets (replaces #34), #97
  preflight on the server path and a roster check for the job (replaces #13), #98 end-of-play kill for every
  seat and for detached children, with the result in `result.json` (replaces #35). A Docker test shows that
  upstream head pays for 2 late posts of detached children and that #98 pays for none.

**3:10 PM. E5 pilot done, scored runs started.** Amendment 7a (`4fc68d35c`, committed 2:58 PM, landed as
`94fc1b90d`) moved the arms of the primary tests first and set the floor to $2, because other lanes drained the
shared OpenRouter account ($23.19 at 2:25 PM, $11.77 at 3:03 PM; E5 had spent $0.14).
- The `crowd` family (17 conditions x 10 instances, commit `bef4f0d08`) passed `generate.py --check`, the
  gate, and 17 scripted Docker trials at their reference values. The runs use a pinned copy
  (`muster-runs/e5/tasks/crowd`, hash in `crowd.pin.json`).
- Scripted checks on Daytona with each task's own solution: `solo-24` 1.0, `team-3` 1.0, `team-8` 1.0,
  `claim-8` 1.0, `indep-4` 1.0, `indep-8` 0.8, `solo-3` 0.15. Each equals `metadata.oracle_reward`.
  My first check of `solo-24` and `indep-8` used the family's template script, which still has unfilled
  placeholders, and scored 0. That was my error, not a task fault.
- Pilot with model seats (i01, 1 trial each, not scored, $0.114): `solo-24` 100%, `claim-2` 100%, `team-8` 45%
  (12 of 24 opens were repeats; p1, p2, p3 and p7 all opened box 1 first), `indep-8` 25%, `claim-8` 65% (1
  repeat, but 8 opens were never used; 109 messages). Thinking was off, no seat reached 30 turns, and a trial
  took 104 s to 277 s on Daytona.
- Deviation from Amendment 7a: 3 lanes of 4 trials run at once (12 trials, not 8), to end the primary arms
  before the account is empty. Decided at 3:04 PM. It changes no analysis rule.
- Pin check after the family landed (`1cec139f2`, 3:17 PM): every tracked file of the landed
  `muster/tasks/crowd/build` is byte-equal with the copy that the trials use. The copy also has 340
  `__pycache__` folders (Python 3.12 bytecode that the simulation wrote in the worktree; git ignores them), so
  its tree hash differs from a clean checkout. The task images use Python 3.13, which never loads 3.12
  bytecode. The copy stays unchanged for all E5 arms. `muster regrade` against the clean landed tree is the
  check that the rewards do not depend on it.
- 3:18 PM: a 4th lane started (16 trials at once), for the same reason as the 3rd. No trial or seat
  exception in the first 62 results.

**2:50 PM. Plan for today, after research and an independent review ($0.008 spent so far).** Peter asked for
research, a plan, a double check and then work on multi-agent infrastructure, with up to $50 of credits.
- Research: 9 subagents wrote 6 evidence files and 28 source files in `research-context/multi-agent/`
  (commit `a42f6d2a`): executed-work map, agent-count scaling designs, coordination primitives, MAC Bench run and
  port plan, Olam upstream delta, new-threads intake. The synthesis with 12 corrections to older shelf text is
  `multi-agent/2026-11-23-evidence-wave-synthesis-and-muster-plan.md`.
- Another lane pre-registered at 2:05 PM ET (`eval-lab-results/multiagent-campaign-2026-11-23/`): server claim
  locks and unread guards at 3 seats (`boxes_v2`), `scarce` with real models, a `relay_v1` family and MAC Bench
  runs. It uses the muster kit. I do not repeat it. My part: the number of seats at a fixed total budget (E5).
- OBSERVED: the OpenRouter key shows $46.27 under its own limit, but the account holds $23.19 of credits. The
  account figure limits all lanes together. DeepSeek's own API has $2.54, which the other campaign reserved.
- Infrastructure results today (all $0 of model cost):
  - The kit runs unchanged on upstream `olamlabs/harbor-multiagent` `711ca65a`: 16 scripted trials on each
    runner over the 4 families give the same rewards, and `muster regrade` matches 16 of 16. The new runner is
    in `/tmp/fable-infra/HarborHeadCompat/`.
  - Muster trials run on Daytona with no code change (Docker inside the sandbox). 13 scripted trials, 0
    failures, rewards equal to the local reference, seat isolation holds (a probe policy: 21 checks pass for
    each seat), 8 trials at once in 69 s, about $0.001 of sandbox time for each trial. Spend: $0.015. The
    wrapper `muster-runs/e5/harbor-daytona.sh` adds `--env daytona` to `harbor run`.
  - Fork PRs against upstream head: #33 is superseded by upstream `a9c661c0` (closed). #34 is still needed.
    #13 is partly superseded. #35 needs a redesign on top of upstream's kill. Each PR has a comment with the
    details. The fork's `main` is still at `6aeb609e`; a sync changes the base of 83 open PRs of 4 lanes, so
    Peter decides it.
  - `analysis/crowd_scaling.py` (landed `e282e7e8e`) reproduces E1 and F14 from the event logs: rewards
    98.5 / 85.7 / 51.5 / 30.8, the F13 split 69% / 27% / 4%, and F14's +15.1 points (exact sign-flip p 8/1024).
  - Route pilot (not scored, `muster-runs/e5/route-pilot`): E1 `team-3` i01 and `solo-x3` i01 on the E5 roster
    through local Docker: reward 1.0 and 1.0, thinking off (0 of 28 agent steps have reasoning text), cost
    $0.0078 and $0.0009, 62 s and 31 s.
- E5 is pre-registered as Amendment 7. The review (`go with changes`) replaced both primary tests, added the
  k = 3 arms, the E1 route anchor and the union control, fixed the turn cap, and moved the cheap arms first.

## 2026-11-21

**12:12 PM. E4 `ledger` done (Peter: "run e4"). Real cost $0.72 (63 trials, pilot included). F18.**
Pre-registered as Amendment 6 (`a06875d2`, landed before the first trial). Runs 15:53 to 17:09 UTC, Saturday,
off-peak. Outputs: `muster-runs/e4/{pilot-*,solo,indep-3,team-3}`, tasks copied to `muster-runs/e4/tasks`.
- Result: `solo` 1.000, `team-3` 1.000, `indep-3` 0.000 (20 trials each). Test 1 passes, test 2 does not
  apply (no team losses), no leak in `indep-3`. Every answering seat had read both other reports first.
- Cost from tokens, at half of Harbor's recorded peak price: pilot $0.018, `solo` $0.016, `team-3` $0.083,
  `indep-3` $0.609 (Harbor recorded $1.449 in total). The balance went from $4.69 to $3.63 ($1.06); the rest of
  the drop is other agents' spend in the same window, which I cannot separate.
- Incident: my shell job hit its 60-minute limit and killed the run script and its cost watchdog, but not
  `harbor run` (it continued as an orphan, PID 5550). I found it within 1 minute and restarted a watchdog with
  the same $0.80 cap, and the job finished the 20 `indep-3` trials. No trial was lost or counted twice.
  Next time: start long arms with no shell deadline, and kill the whole process group on a timeout.
- Next for this question: `ledger` is at the team's ceiling. A harder version (more items, a message limit
  or a deadline) can find where pooling fails. It needs a new family version and its own pre-registration.

**4:17 AM. $0 follow-up to F14: how agents follow the report-first rule.** Question and test, fixed before I
looked at the numbers: the rule is "followed" if at least 90% of opens come after a post and a read by the
same seat since its previous open; claims "work" if at most 10% of opens hit a box that another seat
claimed first. New columns in `analysis/races.py`: `post_read_share`, `post_then_read_share`.

| Arm | Post and read before the open | Post, then read, then open | Repeats |
|---|---|---|---|
| `team-3-reportfirst` | 78% | 69% | 0.9 |
| `team-3-rerun` | 28% | 6% | 5.7 |
| `team-3-roles` | 27% | 2% | 2.8 |
| `team-3-shared` | 22% | 3% | 3.9 |
| `team-3-shared-single` | 17% | 0% | 5.55 |

- OBSERVED: "followed" fails (78%, below 90%). "Claims work" passes: 6.5% of report-first opens hit a box that
  another seat had claimed first ("will open box N", "opening box N", "claim box N"); 27 of those 47 opens came
  after the actor had read the claim. In a sample of report-first opens without a fresh post, the box was in an
  earlier batch claim of the same seat ("will open 14,15,16,17,18 now"; "claims room4 boxes 36,37,38,39,40").
- INFERRED: agents keep the purpose of the rule, not its letter. They claim a batch, then open it. That is
  enough to cut repeats from 5.7 to 0.9. Box numbers in free text are a weak signal (room numbers look like
  box numbers), so only the structural measures above are in the table.
- E4 (`ledger`) stays unstarted. muster now runs from Eval Lab, and a paid `muster run` is a billable Eval Lab
  command, which needs Peter's OK.

**4:12 AM. muster now lives in Eval Lab (`main` at `2ad853fc1`, $0). This log moved with it.** 4 commits,
landed with `make land` after `make gate` passed:
1. `8bc316398` imports the muster repository at `eed7a2f` unchanged, so all of it stays in Eval Lab history.
2. `902216776` makes `muster/` a top-level package (`uv run muster ...`). The task families sit in
   `muster/tasks/` as frozen task supply, byte-identical: the gate runs each family's `generate.py --check`.
   The studies (this log, findings, results page, pre-registration, rosters, analysis) are in this folder.
3. `2b8ab7ef8` puts the analysis scripts in Eval Lab style. The F13 script now reads the frozen build when a
   recorded task path is gone, and gives the recorded numbers again (69% of `team-3` repeats "not sent").
4. `2ad853fc1` applies the Eval Lab rules to the code (no comments or docstrings, functions at most 70 lines,
   asserts) and replaces the ~6,000 lines of unit tests with a seeded simulation (`muster/sim.py`: 48 seeds,
   12 task conditions, 26,232 invariant checks in 5 s). Checks: CLI output byte-identical on 144 cases over
   20 real job folders; the old suite passes on the new code (722 passed); the simulation catches 2 planted
   bugs.
- The simulation found 2 real bugs, both fixed and replayed in every gate: `muster regrade` exited 0 when no
  trial could be compared (for example, "30 no-task" on E1, whose old task paths are gone); it now exits 3.
  An event log with invalid UTF-8 crashed `regrade` and `events`; it is now reported as a log problem.
- `muster regrade muster-runs/e1run/team-3 --task-root muster/tasks/boxes/build/team-3` gives 30 of 30 match:
  the frozen build reproduces the E1 rewards.
- The GitHub repository `PeterMakhnatch/muster` stays as it is, with a note that points here. You decide
  whether to archive it.

**4:10 AM. Next real-agent experiment, proposed, not started (needs about $0.40 of DeepSeek budget).**
`boxes` favors 1 agent, because 1 agent can hold the whole state. The pre-registered E4 (`ledger`, a hidden-
profile task) tests the opposite case: each seat holds 1 of 3 warehouse ledgers, every item is in at least 2
ledgers, and the first accepted answer is final. Arms `solo` (1 seat holds all 3 ledgers), `indep-3`,
`team-3`, 10 instances x 2 attempts, the E1 roster. Primary effect: `solo` minus `team-3`, the cost of split
information. Pass/fail test to fix before the run: the team pools well if `team-3` scores at least 2/3 of
`solo` and the difference is resolved, and `premature` (an answer before every other seat's message was
read) explains most team losses. I start it when the budget allows and Astra's claim is clear.

**3:55 AM. tidegauge now lives in Eval Lab (`main` at `ee91f81ff`, $0).** Its GitHub repository was gone, and
`~/Developer/tidegauge` was the only copy. Peter's rules put new systems in top-level Eval Lab packages.
3 commits, landed with `make land` after `make gate` passed:
1. `563112515` imports the tidegauge tree at `8ced196` unchanged (source, 203 tests, docs, scripts), so all of
   it stays in Eval Lab history.
2. `db181202a` makes `tidegauge/` a top-level package (`uv run tidegauge ...`), registers it in AGENTS.md,
   and adds it to the gate (ty, compileall, `python -m tidegauge.sim check`).
3. `ee91f81ff` applies the Eval Lab rules: no comments or docstrings, functions at most 70 lines, asserts.
   The unit tests become a seeded simulation (`tidegauge/sim.py`: 25 seeds, 819 scenarios, 8,578 invariant
   checks in 11 s, with file faults, null calibration and power). Checks: the CLI output is byte-identical on
   83 commands over real muster jobs, and the old suite passes on the new code (317 passed, 3 skipped).
   The simulation catches 2 planted bugs.
- The simulation found 1 real bug: an arm with uneven attempts and no valid reward made `audit` and `score`
  exit 3 ("no trials"). Fixed; its seed replays in every gate.
- `~/Developer/tidegauge` is unchanged; its untracked `data/` (2.7 GB of leaderboard downloads) stays there.
  You can remove the folder when you want; I did not.

**3:50 AM. The race numbers of F15 to F17 are now reproducible ($0).** `experiments/analysis/races.py JOB_DIR ...`
prints, per arm, the reward, new flags, new empty boxes, repeats (seen or not seen by the actor before it
opened), the busiest seat's opens, messages, opens per batch, and the share of "obvious next box" opens.
On the 7 arms of F14 to F17 it gives every number in this log and in `docs/FINDINGS.md`.

**3:40 AM. F17 result (real agents, paid run): fixed roles close most of the gap, but not as well as a claim.**
20 trials of `team-3-roles`, 3:06 to 3:34 AM ET (Saturday, off-peak).

| Arm | Score | New flags | New empty | Repeats | Obvious next box |
|---|---|---|---|---|---|
| `team-3-rerun` | 83.8% | 16.75 | 3.95 | 5.7 | 69% |
| `team-3-roles` | 94.5% | 18.9 | 4.65 | 2.8 | 35% |
| `team-3-reportfirst` (F14) | 98.8% | 19.77 | 3.33 | 0.9 | 54% |

- OBSERVED: +10.8 pts over `team-3-rerun`; the tasks question is resolved (+1.9 to +19.6, sign-flip p 0.040),
  the rerun question is not (p 0.059). `team-3-reportfirst` is +4.3 pts above `team-3-roles`, resolved on the
  tasks question (sign-flip p 0.011). 91% of the opens follow the seat's order. Repeats fall by half. Empty
  opens rise from 3.95 to 4.65: each seat scouts rooms on its own, with different boxes. 20 of 20 rewards
  replay exactly. No key in the outputs.
- Pass/fail test from 3:08 AM: 94.5% is below 94.6%, so the test fails, by 0.1 pts. Roles do not work as
  well as a claim; F14 is still resolved above them.
- INFERRED: a role breaks the tie inside a room. It does not split the scouting of rooms, so 2 seats still
  scout the same room with different boxes, and some still meet in a treasure room.
- **Cost of this paid run: $0.271** (from tokens at the off-peak price; Harbor records $0.542). Balance $6.11
  at 3:34 AM. My paid spend today: $1.32; in total under the $10 budget: $2.06. This is my last paid run tonight.

**3:08 AM. F17 design, written before any trial (my own choice; the queue is empty).** I said at 3:00 AM that
I would make no more paid runs tonight. I change that for 1 small run: it answers the open question of F16,
costs about $0.25, and the balance is $6.52. Question: does a fixed role per seat break the tie between alike
agents as well as a claim does, without any message rule? Arm `team-3-roles`: the plain `team-3` tasks (own
opens only, channel) plus 1 line per seat. p1: "Your order inside every room: open its boxes from the lowest
number upward (box 1 of the room first)." p2: the same, "from the highest number downward (box 10 of the room
first)". p3: "from the middle outward (boxes 5, 6, 4, 7, 3, 8, 2, 9, 1, 10 of the room)". The roles say
nothing about which rooms to scout. Tasks from `muster-runs/f17/derive.py`. 10 instances x 2 attempts.
Pass/fail test, fixed now: roles work as well as a claim if the arm scores at least 94.6% and `tidegauge
compare` against `team-3-rerun` resolves the gain as positive (tasks question, both tests p < 0.05).
Secondary: repeats per trial (F14 report-first: 0.9; `team-3-rerun`: 5.7). Budget: stop at $0.30 real.

**3:03 AM. $0 follow-up to F16: the repeats are the "obvious next box".** For each open I ask whether the box
is the lowest unopened box of its room, as far as the actor knew (its own opens, and `team_opens` where shown).

| Arm | Opens that are the obvious next box | Repeats that are the obvious next box |
|---|---|---|
| `team-3-rerun` | 69% | 75% (86 of 114) |
| `team-3-shared` | 84% | 51% (40 of 78) |
| `team-3-shared-single` | 87% | 93% (103 of 111) |
| `team-3-reportfirst` | 54% | 52% (14 of 27) |

INFERRED: with 1 open at a time, agents almost always take the lowest free box of the treasure room, so 2
agents in the same room take the same box. The report-first agents spread out (54% obvious). Candidate next
experiment (needs about $0.25, not started): a seat rule without messages, for example p1 takes boxes from
the low end, p2 from the high end, p3 from the middle. It tests whether a fixed role breaks the tie as well as
a claim does. E2 showed that a random number per seat does not.

**3:00 AM. F16 result (real agents, paid run): 1 open at a time does not stop repeats. Alike agents choose the
same box at the same time.** 20 trials of `team-3-shared-single`, 2:32 to 2:59 AM ET (Saturday, off-peak).

| Arm | Score | New flags | Repeats | Opens per batch | Repeats where the earlier open was already shown |
|---|---|---|---|---|---|
| `team-3-shared` (F15) | 88.5% | 17.7 | 3.9 | 2.4 | 35 of 78 |
| `team-3-shared-single` | 90.3% | 18.05 | 5.55 | 1.02 | 7 of 111 |
| `team-3-reportfirst` (F14) | 98.8% | 19.77 | 0.9 | 1.2 | |

- OBSERVED: agents follow the rule: 1.02 opens per batch, 1% of opens in batches of 3 or more. The score is
  +1.7 pts over `team-3-shared` (not resolved, sign-flip p 0.55) and +6.5 pts over `team-3-rerun` (not
  resolved, p 0.18). 104 of the 111 repeats happen before the earlier open reaches the actor; the other 7
  come 0.3 to 1.0 s after it. 20 of 20 rewards replay exactly. No key in the outputs.
- Pass/fail test from 2:31 AM: the rule is followed and the arm stays below 94.6%. So batching is not the main
  cause. INFERRED: alike agents choose the same box at nearly the same time. A claim before the open (F14)
  removes most of these repeats; seeing the opens after they happen does not.
- **Cost of this paid run: $0.254** (from tokens at the off-peak price; Harbor records $0.509). Balance $6.57
  at 3:00 AM. My paid spend today: $0.26 + $0.53 + $0.25 = $1.05; in total under the $10 budget: $1.79.
  I make no more paid runs tonight, so that Astra keeps her first claim.

**2:40 AM. F15 result (real agents, paid run): neither budget nor fragmentation alone explains the gap.**
60 trials, 1:23 to 2:32 AM ET (Saturday, off-peak).

| Arm | Score | New flags | New empty | Repeats | Max opens of 1 seat | Opens per batch | Real cost |
|---|---|---|---|---|---|---|---|
| `solo-x3-rerun` | 100.0% | 20.0 | 3.75 | 0 | 23.8 | 12.5 | $0.037 |
| `team-3-rerun` (F14c) | 83.8% | 16.75 | 3.95 | 5.7 | 9 | 2.5 | $0.256 |
| `team-3-shared` | 88.5% | 17.7 | 3.7 | 3.9 | 9 | 2.4 | $0.253 |
| `team-3-pool` | 93.2% | 18.65 | 3.65 | 4.65 | 15.6 | 2.9 | $0.244 |

- OBSERVED: G = +16.2 pts (`solo-x3-rerun` against `team-3-rerun`, resolved, sign-flip p 0.008).
  `team-3-shared`: +4.7 pts, not resolved (sign-flip p 0.44), 29% of G. `team-3-pool`: +9.5 pts, 59% of G;
  the tasks question is resolved (+2.1 to +16.9, sign-flip p 0.032), the rerun question is not (p 0.082).
  60 of 60 rewards replay exactly. No key in the outputs.
- Pass/fail test from 1:25 AM: neither arm reaches 94.6% (2/3 of G). So neither factor alone explains the gap.
- OBSERVED: in `team-3-shared`, all 78 repeats are races: 43 happen before the earlier open reaches the actor,
  35 come 0 to 1.5 s after the answer that shows the box, in the same batch of commands. In `team-3-pool`
  repeats stay (4.65), but the seat that finds a treasure room keeps opening there (the busiest seat makes
  15.6 opens on average), so fewer flags stay out of reach.
- INFERRED: the gap has 2 parts. Pooling the budget recovers flags that a 9-open seat cannot reach; it does
  not stop repeats. The shared view removes few repeats, because agents act in batches before they read.
  F16 tests the batching part.
- **Cost of this paid run: $0.534** (from tokens at the off-peak price; Harbor records $1.068 at the peak
  price). Balance $7.90 at 1:21 AM, $6.91 at 2:33 AM; other lanes ran at the same time. My paid spend today:
  $0.26 (F14c) + $0.53 (F15) = $0.79; in total under the $10 budget: $1.53.

**2:31 AM. F16 design, written before any F16 trial (my own choice; the queue is empty).** Observed at $0 in
F15 (before the `team-3-pool` result): in `team-3-shared` every repeat is a race. 43 of 78 repeats happen
before the earlier open reaches the actor, and the other 35 come 0 to 1.5 s after the answer that shows the
box, in the same batch of commands. Agents send 2.4 opens per batch (a batch = opens less than 2 s apart),
`solo-x3` sends 12.5, `team-3-reportfirst` 1.2. Question: does the team reach the single agent when each
seat opens 1 box at a time and reads the shared list between opens, without any message protocol?
Arm `team-3-shared-single`: the `team-3-shared` tasks plus 1 instruction line: "Open only 1 box per command.
Read its answer, with `team_opens`, before you choose the next box. Do not open a box that `team_opens`
already lists." Tasks from `muster-runs/f16/derive.py`. 10 instances x 2 attempts, same roster.
Pass/fail test, fixed now: batching explains the rest of the gap if the arm scores at least 94.6%
(`team-3-rerun` 83.8% plus 2/3 of G = 16.2 pts) and `tidegauge compare` against `team-3-shared` resolves the gain as
positive. Check that the rule was followed: opens per batch at most 1.5. If the rule is followed and the arm
stays below 94.6%, the cause is simultaneous choices by alike agents, which only claims (F14) remove.
Budget: stop at $0.45 real.

**1:25 AM. F15 design, written before any trial (Peter's queue item 2, real agents).** Question: why does 1
agent with the budget of 3 (`solo-x3`, 98.5%) beat the talking team (`team-3`, 83.8% to 85.7%)? Both have 27
opens in total. 2 things differ: (a) budget: the agent holds all 27 opens, the team has 9 per seat; (b) context
fragmentation: the agent sees all its opens in 1 context, each team seat sees only its own opens and the
messages. 2 new arms change 1 factor each, from `team-3` (same instances, tokens, channel, roster):

- `team-3-pool`: the 3 seats share 1 pool of 27 opens. Fragmentation stays.
- `team-3-shared`: 9 opens per seat, but the world shows every open of every seat: each open answer has
  `team_opens`, and `GET /opened` returns it. The budget split stays.
- Controls: `team-3-rerun` (F14c, 11-21, 83.8%) and a new `solo-x3-rerun` today.

Each arm: 10 instances x 2 attempts = 20 trials, DeepSeek Flash, thinking off, 1 trial at a time.
Tasks come from `muster-runs/f15/derive.py` (copies of `build/team-3`, a world subclass, 1 or 2 changed
instruction sentences); a $0 world check passes. Gap G = `solo-x3-rerun` minus `team-3-rerun`.
Pass/fail test, fixed now:

- Fragmentation explains the gap if `team-3-shared` closes at least 2/3 of G and `tidegauge compare`
  against `team-3-rerun` resolves it as positive (tasks question, t-test and sign-flip both p < 0.05).
- Budget explains the gap if `team-3-pool` meets the same 2 conditions.
- If neither arm meets them, neither factor alone explains the gap.

Prediction from F13 (the team leaves only 0.6 opens unused, and repeats cost the flags): `team-3-shared`
closes the gap, `team-3-pool` does not. Budget: DeepSeek direct, Saturday off-peak, caps on real cost:
`solo-x3-rerun` $0.10, `team-3-shared` $0.45, `team-3-pool` $0.40; total under $1. Balance at 1:21 AM: $7.90.

**1:20 AM. F14c result (real agents, paid run): F14 holds. The plain team did not improve over time.**
20 trials of `team-3-rerun`, 12:54 to 1:19 AM ET (Saturday, off-peak).

| Arm | Score | Repeats per trial | Repeats before the first opener posts |
|---|---|---|---|
| E1 `team-3` (11-19) | 85.7% | 4.9 | 69% |
| `team-3-rerun` (11-21) | 83.8% | 5.7 | 62% (3.55 of 5.7) |
| `team-3-reportfirst` (11-20) | 98.8% | 0.9 | 48% (0.43 of 0.9) |

- OBSERVED: `tidegauge compare` E1 `team-3` against `team-3-rerun`: -1.9 pts, not resolved (p 0.66).
  `team-3-rerun` against `team-3-reportfirst`: +15.1 pts, resolved for both questions (rerun +2.3 to +27.9,
  tasks +6.6 to +23.6, sign-flip p 0.0075); higher on 8 of 10 instances. 20 of 20 rewards replay exactly.
  No key in the outputs.
- Pass/fail test from 1:00 AM: `team-3-rerun` 83.8% is at most 92%, and the compare is resolved as positive.
  So F14 holds: the gain comes from the report-first protocol, not from drift after 11-19.
- Fork PR #34: the rerun of the failed CI job passes. All checks on #13, #33, #34 and #35 are green.
- **Cost of this paid run: $0.256** (from tokens at the off-peak price; Harbor records $0.512 at the peak price).
  The DeepSeek balance went from $8.34 to $7.94; other lanes ran at the same time. My total paid spend under
  the $10 budget: $0.74 + $0.26 = $1.00. I make no more paid runs without a new budget.

**12:58 AM. Fable now runs as an OMP chat on Opus 5.5.** Checked at $0: the 4 fork draft pull requests have no
new review comment since 11-20, 2:44 PM. #13, #33 and #35 are green. #34 had 1 failed CI test,
`test_superserve.py::test_cancellation_kills_the_command` (`ProcessLookupError`). #34 does not touch that
file, so I started a rerun of the failed jobs. `docs/TOOLS-COMPARISON.md` exists (written 11-20).
DeepSeek balance at 12:52 AM: $8.34.

**1:00 AM. F14c design, written before any trial (time control for F14, real agents).** Question: is the F14
gain (+13.2 pts for `team-3-reportfirst` over E1 `team-3`) real, or did the model or the provider change
after 11-19? Arm `team-3-rerun`: the committed E1 `team-3` tasks without change, run again on 11-21, after F14,
with the same roster `t2-deepseek-flash-plain`, 10 instances x 2 attempts = 20 trials, 1 trial at a time.
Primary metric: reward. Pass/fail test, fixed now: F14 holds if `team-3-rerun` scores 92% or less and
`tidegauge compare` against `team-3-reportfirst` resolves the gain as positive. F14 is drift if `team-3-rerun`
scores 95% or more, or the compare is not resolved. A result between 92% and 95% is undecided. Secondary:
repeats per trial from
`experiments/analysis/budget_identity.py` (E1 `team-3`: 4.9). Budget: DeepSeek direct only, Saturday off-peak,
stop at $0.26 real, so my total paid spend stays under $1.

## 2026-11-20

**3:58 PM. F14 result (real agents, paid run): when agents report before they act, the team matches the
budget-matched single agent.** 30 trials of `team-3-reportfirst`, DeepSeek Flash, 3:08 to 3:57 PM ET (off-peak).

| Arm | Score | Repeats per trial | Messages per trial | Real cost per trial |
|---|---|---|---|---|
| E1 `team-3` (11-19) | 85.7% | 4.9 | 14.0 | $0.012 |
| `team-3-reportfirst` | 98.8% | 0.9 | 34.1 | $0.025 |
| E1 `solo-x3`, 1 agent with 27 opens | 98.5% | 0 | 0 | $0.002 |

- OBSERVED: `tidegauge compare`: +13.2 pts, 95% rerun interval +9.0 to +17.3, resolved for both questions
  (sign-flip p 0.002); higher on all 10 instances. 2 or more agents start with the same box in 10% of the trials
  (47% in `team-3`). 30 of 30 rewards replay exactly. No key in the outputs.
- INFERRED: this confirms the F13 mechanism: the gap was acting before reporting, not a lack of ability. The
  line is an explicit protocol, so it measures the value of that protocol, not what agents find alone. The
  protocol doubles the messages and the cost. Time is not controlled (E1 ran on 11-19).
- **Cost of this paid run: about $0.74** (estimated from tokens at the off-peak price; the DeepSeek balance went
  from $11.49 to $10.76). My total paid spend under the $10 budget: $0.74.

**3:06 PM. F14 design, written before any trial (the F13 follow-up, real agents).** Question: does the team
lose to the budget-matched single agent because agents act before they report (F13)? Arm `team-3-reportfirst`:
the E1 `team-3` tasks (10 instances, 3 attempts, the same roster `t2-deepseek-flash-plain`) with 1 added
instruction line: "Before you open a box, post its number with POST /msg and read GET /msgs. Do not open a box
that another agent posted first." Primary metric: reward, compared with E1 `team-3` (85.7%) by
`tidegauge compare`. Secondary: repeats from `experiments/analysis/budget_identity.py`. Expectation: fewer
repeats and a higher reward. Caveat: E1 `team-3` ran on 11-19, so time is not controlled. Budget: DeepSeek
direct only, off-peak, stop at $0.80 real; balance before the run $11.49.

**2:48 PM. Handoff: I stop here because the weekly Claude quota is almost used.** Peter's new rules apply from now:
work only in Eval Lab or the fork, no cross-review, prefer real LLM agents in seats, no paid calls without a
budget. The 3 assigned items are done (F13, review answers on #13 #33 #34 #35, `docs/TOOLS-COMPARISON.md`).
The review of #20 was posted before the new rules; I do no more cross-reviews.
Next item that fits the rules (needs a budget or a free model route): the F13 follow-up, a `team-3` variant
whose instruction asks each agent to post an open before it acts (30 real-agent trials, about $0.35 at
DeepSeek off-peak). Open decision for Peter: where tidegauge lives, because its GitHub repository is gone.

**2:47 PM. Review of Opus pull request #20 posted (merge, no blocker).** A probe with a fake secret shows that the
branch removes the secret in 3 cases where `main` leaves it or changes other bytes (a character across byte 8192,
invalid bytes after the sample, CRLF lines). It works together with #34. Next: stop here, because the weekly
Claude quota is almost used.

**2:46 PM. Done at $0:** F13 in `docs/FINDINGS.md` (the talking team loses to the budget-matched agent through
repeated opens; 69% happen before the first opener reports). Fork PR #34: the reviewer's new P1 (quadratic
scan) is fixed, 120 KB of escapes from 28.3 s to 0.08 s. Review residuals on #13 and #35 answered as stated
limits; #33 has "merge as is". One-page tool comparison: `docs/TOOLS-COMPARISON.md`.
Observed: `gh` cannot find the GitHub repositories of tidegauge, harbor-sextant and harbor-control-gate.
Next: an adversarial review of 1 Opus pull request, if the quota allows.

## 2026-11-19

**11:46 PM. E1 and E2 are done: 210 trials, real cost about $1.22 of the approved $2**
(Harbor records $2.25 at DeepSeek's peak price; the runs used the off-peak hours, which cost half). Results in
`docs/FINDINGS.md` F11 and F12. Talking adds 34.2 pts and is resolved, but 1 agent with the same total budget
scores 12.8 pts above the team. All 210 rewards replay exactly.

**12:43 PM. tidegauge: a tasks verdict now needs the t-test and the exact sign-flip test** (PeterMakhnatch/tidegauge#3,
merged, CI green). On the flotilla c1 data 4 verdicts change to "not resolved". On the Terminal-Bench 2.0
leaderboard 17 of 2,775 `compare` verdicts change, all near p 0.04 to 0.05. `gate` now says "undecided"
instead of exiting with an error when no task has 2 or more trials.

**12:09 PM. The fork's own CI passes on all 4 draft pull requests** (#13: 11 checks, #33, #34, #35: 12 checks each,
with Windows and the Docker and Podman runtime tests). Next: `decompose` should not call a small 0/1 effect
resolved from the t-test alone; an agent adds the exact sign-flip test to the tasks verdict.

**12:02 PM. tidegauge `--task-key` merged** (PeterMakhnatch/tidegauge#2, CI green on Python 3.12 to 3.14).
All lanes of this session are finished. Open work: the 4 draft pull requests in the fork wait for their CI
and for your rewrite of the upstream text; E1 and E2 wait for a budget decision.

**12:01 PM. First measured result with the new tools, at $0 (F10):** `tidegauge decompose` on 276 trials that the
flotilla lane already ran reproduces that lane's coordination table exactly. The exact test resolves the
coordination gain for 1 of 4 models (DeepSeek, +91.7 pts, p 0.001). The t-test also calls 3 smaller gains
resolved; the exact test does not. No talking team is resolved as different from 1 agent with all the facts.
tidegauge gets `--task-key` for this (PeterMakhnatch/tidegauge#2, CI running).

**11:55 AM. Pull request #13 answers its review:** 5 new commits fix 4 of the reviewer's points (roster checked
against the tasks before any trial, server keeps each participant's env, every failing seat is named, the
default agent no longer masks a seat). 2 points are stated as limits. A reply with the table is on the pull
request. `muster matrix --policy-env WAIT_SEC=5` gives scripted seats a shorter wait. The ledger lane measured 29 s
instead of about 140 s for 1 roster with a waiting oracle. I checked only the job config (dry run).

**11:53 AM. Fourth draft pull request in the fork:** seat processes freeze when play ends, before grading
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/35). A seat that acts 12 s after play: rewards 1, 1, 1 on
`main`, 0, 0, 0 on the branch. A seat that cannot be frozen makes the reward invalid. 85 seat tests pass.

**11:44 AM. Third draft pull request in the fork:** wrapped API keys stay out of trial output
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/34). mini-swe-agent now gets the key from a private
file, and the scrub also matches a key that a line break splits. 154 touched tests pass.
**F9 (no model, $0):** in `scarce`, a seat that understates its need always scores, and an honest seat pays.

**11:25 AM. tidegauge reads multi-seat trials and has `decompose`** (merged, PeterMakhnatch/tidegauge#1,
CI green on Python 3.12 to 3.14). `decompose` splits a team's gain into headcount and coordination, each
with both error bars. On the 1-trial pilot it prints the numbers and says why it cannot decide.
Next, at $0: `decompose` on the real trials of the flotilla lane, which already paid for them.

**11:18 AM. The `scarce` family is done** (finished by an Opus agent, verified here): 157 tests pass,
4 real Docker runs give the expected reward with the expected seat paying, and `muster regrade` matches 10 of 10
trials. A task can now declare the findings that its design produces (`[metadata] expected_findings`).
`scarce` declares `tested-seat-not-needed`, because only 2 of 3 seats can score. muster CI is green.

**11:08 AM. Paid runs stopped: the shared $20 ceiling is reached.** The 4 agents share 1 ceiling. The other
lanes count about $19.3 (OpenRouter and Modal). This lane spent about $0.45 to $0.62 (mostly DeepSeek).
E1 stopped after 2 trials ($0.01). E1 and E2 need about $2 more: that is your decision.
The 5 leftover containers and 2 networks of the 2 stopped trials are removed (11:12 AM).

**11:04 AM. Second draft pull request in the fork:** every seat's model key is now scrubbed
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/33). The 2 new tests fail on `main` (3 failed)
and pass on the branch (5 passed). **E1 and E2 started:** 7 arms, 210 trials, about $2, pre-registered.

**11:01 AM.** The Fable usage limit stopped 5 subagents at about 6:50 AM. Their work is saved in their
worktrees. The session continues on Opus 5.5.

**06:49 AM. Control matrices on the 3 families (no model, $0).** Every all-oracle trial gives the exact
expected reward. In `boxes` and `commons`, the `spoof` policy (impersonation, a guessed token, a second take,
a call to close the world) gives exactly the rewards of `nop` in every roster: no exploit found.

**06:00 AM. Control matrices on 4 seat tasks of the other agents (no model, $0).**

| Task (branch) | Observed |
|---|---|
| `hello-seats` with world-issued tokens (`flotilla/hello-seats-identity`) | Fixed. Impersonation now gives 0. On the upstream example it gives the full reward. |
| `kit-check` (`pm/multiagent-control-fixtures`) | 7 of 8 control rosters end with `RewardFileNotFoundError` or a timeout. No reward is written when a seat does nothing. |
| `cramped-room-parity` (`pm/overcooked-parity`) | 3 of 4 control rosters end with `RewardFileNotFoundError`. |
| `dispatch-handoff` (`experiments/seatlab-handoff`) | 3 of 4 control rosters end with `RewardFileNotFoundError`. |

Inferred: a verifier that writes no reward when a seat fails makes the trial invalid instead of 0. Harbor's
job mean then leaves the trial out, and the team score goes up. The fix is to write reward 0.

**05:30 AM. Pilot with real models (not scored, $0.38).** `boxes` instance 1, 1 attempt for each arm.

| Arm | Thinking on | Thinking off |
|---|---|---|
| 1 agent | 0.45 ($0.010) | 0.25 ($0.003) |
| 3 agents, no channel | 0.95 ($0.148) | 0.75 ($0.012) |
| 3 agents, channel | 0.85 ($0.105) | 0.90 ($0.022) |

Observed: without a channel, the 3 agents with thinking on each invented a split of the rooms. With thinking
off, 2 of 3 agents opened the same first boxes. With a channel, agents sent 15 to 20 messages, and 13 of 25
actions happened while a message of another agent was posted and not yet read.
Claude Haiku 5.5 alone: 0.45 ($0.002). GPT-6 Luna alone: 0.30 ($0.002).

**05:25 AM. Kit: each seat has its own internal network.** Before this, seats shared 1 network, and a seat
could read a file that another seat served. So no "no channel" condition was silent. Now a seat reaches the
world and nothing else. Verified on real Docker. Since 11:00 AM the networks are IPv6 only: Docker's IPv4
pool ran out at about 30 networks, which stopped 1 trial of a control matrix.

**05:20 AM. Draft pull request in the fork:** participants are now checked before a trial starts
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/13). On `main` a missing model key fails after
47 s of setup and play. On the branch it fails in about 2 s.

**04:30 AM. A model key was written to a local log in plain text** (smoke test, OpenRouter key, about 8 minutes,
then redacted in place). 2 causes in the fork: the scrub set reads the key of the first seat only, and the
scrub does not match a key that a harness prints across 2 lines. Both have fix branches. A scan of 2,865 run
files and 21,675 repository files found no other copy. You decide if the OpenRouter key needs rotation.
