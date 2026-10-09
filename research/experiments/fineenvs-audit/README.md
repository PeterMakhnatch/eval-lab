# fineenvs-audit — evidence (2026-10-09, $0)

Companion to `docs/mimo/fineenvs-audit.md` (the full audit; per-item V1–V7/E1–E6 table
lives there). This folder records *what was compared at which revisions* and the
$0 re-verify, so the doc's `MEASURED` claims are re-checkable. No large files: only
this README (digests, diff hunks, command transcripts in compressed form).

## Version commits (from `git log` on `--filter=blob:none` clones + `git ls-remote`)

Code: pin `5746e2f0` (09-27) → 1.2.0 `b0e054a6`→tip `e60dca37` (10-05) → 1.3.0
`86004f41`→tip `cf87dbe4` (10-09, = `ls-remote` HEAD). Cyber: `763882a` → `520b091`→
`3164ed3` → `0e0aa5a`→`ec95572`. General: `10b732c` → `7f601c0`→`3fb672c` →
`4647264`→`85c8884`. Terminal: `fe1c2b6` → `8e8d504`→`64cf515` → `9b0b0b2`→`b74a53f`.
Webdev: `e1a6293` → `719be70`→`082fb12` → `27537f7`→`884ed0a`. Music: `e1a66d4` →
`6afe02d`(+`5036010`)→`cbe3cf0` → `d30566e`→`9873f05`. `ls-remote` HEAD on all six
repos 2026-10-09 == the 1.3.0 tips: nothing newer landed. Adapter source on GitHub
`main` (`5e0b46e`): `ADAPTER_VERSION = "1.1.0"`, no 1.2.0/1.3.0 commit on any branch.

## File diffs (per-file HTTP fetch at revisions, `diff` locally)

- Code 000792 `setup.sh`: pin(34 lines, hide-only L27–30) → 1.2 tip(53 lines, strip
  L32–44 inside `LATER>0` gate L28) → 1.3 tip(+POSTAMBLE L54–57, comment L8–10).
  `setup.sh` byte-identical across 000792/001809/002552 at tip.
- Code 000792 `blocklist`: 33 lines → 62 lines (`33a34,62`: 12 Hub + 17
  registry/CDN hosts) → unchanged. `test.sh`, `Dockerfile`, `instruction.md`:
  identical all three versions. `task.toml`: adapter 1.1.0→1.2.0→1.3.0,
  `source_revision 639865fd…` and `docker_image` unchanged.
- Digest check 1.1→1.2→1.3 on code tasks 000041/000158/000160/000324/000666/
  000832/001809/002402/002552: 9/9 identical (e.g. 002552 always `…2dcbed218c61`).
- Cyber `arvo_8944`: setup pin==1.2 tip; 1.3 adds POSTAMBLE only. `test.sh` re-runs
  the PoC with the server's code (6 lines). Instruction pin==1.2 tip.
- Terminal `candidate-2996-ml-evaluation`: pin→1.2 = +`write_blocklist` call only;
  blocklist file 404 at pin → 200 (1,562 B) at tip. Guard
  `tests/anti_hack_guard.py` byte-identical pin→tip. Instruction pin==1.2 tip.
- General `s3k_0000…`: setup pin==1.2 tip (venv `mcp==1.26.0`, root-only isolation,
  Hub fetch before block); 1.3 adds POSTAMBLE only. Instruction pin==1.2 tip.
- Webdev/music: setups gain the POSTAMBLE text 1.2→1.3 but ship no `files/blocklist`
  (HTTP 404 at tip) → no-op. Code README: pin has no "differs"/Daytona sections;
  1.2 adds them; 1.3 adds only the adapter-version row + "Patches" changelog
  (33-line diff).

## Re-verify transcript (local Docker, `--network none`, root, published tip setup)

- `002552` (img `2dcbed218c61`): setup prints only `setup done` (no strip);
  `BASE=d5179059` (2024-10-27 00:39); `rev-list --all --not BASE` = 0;
  `fsck --unreachable` = 12 commits / 7,699 objects; `e88159fb` (00:45, +6 min)
  diffs `miio/miot_models.py` +9 — the commit the cheat ladder applies for 1.0.
- `001809` (img `5601987ef363`): `BASE=1998a340e` (2022-11-15); unreachable = 5,067
  commits / 102,059 objects, incl. `21000439b` (2024-06-20). Ladder `git_history`
  clean here (fix not cleanly recoverable) — presence ≠ exploitability per task.
- Containers removed after. Spend: $0 (no paid compute, no model calls).

## Upstream inputs quoted

- mimoagent@467f0a1: `opensource_code.py` L88–93/L156–186/L190–240,
  `datasets/base.py` L101/L190–208/L594–598/L630/L758/L792–873/L876–882/L895–909,
  `arvo.py` L49–55/L104–107, `agents/antihack.py` L1, `kubernetes.py` L83–86.
- HF `XiaomiMiMo/MiMo-V2.6-RL-oss` discussions #3 (Syntology, general 50-env run) and
  #6 (raimondasl, cyber↔CyberGym overlap + `fix_binary` follow-ups).
- Eval Lab receipts: `research/experiments/{cheat-tamper-ladder,separate-verifier-v2}/
  README.md`; read-first docs in `~/Developer/research-context/analysis/`
  (`2026-10-08-mimo-data-issues.md`, `2026-10-08-mimo-clean-plan.md`,
  `2026-10-09-fineenvs-1.3.0-check.md`, `2026-10-09-mimo-grader-fix.md`).
