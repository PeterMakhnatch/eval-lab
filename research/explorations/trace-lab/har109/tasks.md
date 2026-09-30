# HAR-109 pre-read: the 10 HAR-104 Python tasks (HAR-105 part 3)

Pre-run read of the task files alone — no containers, no runs, $0.
Purpose: make the HAR-104 hand reads fast and pin fairness judgments
(instruction vs hidden tests) before any trace exists.

**Sources.** Task packages live in the pinned snapshot store
`derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/<id>/`
(each: `instruction.md`, `task.toml`, `environment/Dockerfile`,
`environment/setup/setup.sh`, `tests/test.patch`, `tests/test_command.sh`).
Selection + nop evidence: `research/experiments/har105-exploration/README.md`
(part 3 table) and `research/experiments/har105-exploration/python_selection.json`.
Nop specs: `research/experiments/har105-exploration/specs/har105-qual-py-*.json`.
All 10 nops: reward 0, no trial exception, qualification `ok`.

**Grading mechanics (same for all 10).** `tests/test.sh` records the agent's
diff against the base commit, resets the files the hidden patch touches,
applies `tests/test.patch`, and runs `tests/test_command.sh`; reward 1 iff the
test command exits 0. There is **no reference solution in the package** — the
image history is truncated at the base commit and `.git` is hidden while the
agent works (`environment/setup/setup.sh`). The agent must write the fix from
the repo at hand. Every image pins a different
`docker.io/xiaomimimo/mimo-v2.6-RL-harbor-code@sha256:<digest>`
(`environment/Dockerfile:3`); all setups run the identical mimoagent
open-source-code setup, differing only in workdir (`/testbed` vs
`/workspace/repo`). All tasks: `[agent].timeout_sec = 3600`,
`network_mode = "public"`, answer-leak blocklist blocks github/search
(`environment/setup/files/blocklist`). No hidden-test file paths, exact
messages, or network needs are withheld beyond what is noted per task.

## 1. format-code-task-000226 — waitress: don't `urlsplit` the request path

- **Asks:** fix `waitress/parser.py:257` so a request-target like
  `//testing/whatever` keeps `testing` instead of losing it as a fake netloc
  (`instruction.md:3`, with the `urlsplit('//testing/whatever')` demo).
- **Hidden tests check:** 6 new tests in `waitress/tests/test_parser.py`
  (`tests/test.patch:18-75`): `split_uri` keeps path `//testing/whatever`
  with empty proxy scheme/netloc, plain and with query/fragment; `//` alone;
  and one end-to-end `parse_header(b'GET //testing/whatever HTTP/1.1\n')`
  case. Test command runs the whole `Test_split_uri` class, so pre-existing
  tests must keep passing (`tests/test.patch:90`).
- **Instruction sufficient?** Yes. Exact file:line, exact repro, exact
  expected outcome. The fix (don't route origin-form targets through
  `urlsplit`, or re-attach netloc) is a few lines.
- **Traps:** over-fixing proxy-URL handling (`http://...` targets must still
  split); the `//` bare case.
- **Nop:** 6 of 13 unittest failures, reward 0 (feature missing, setup clean).
- **Verdict: sound.** Evidence: `instruction.md:3`, `tests/test.patch:18-75`.

## 2. format-code-task-001896 — linkpreview: generic-HTML fallback metadata

- **Asks:** `link_preview(...)` should fill `site_name/title/description/image`
  from ordinary HTML when OG/Twitter/microdata/JSON-LD give nothing, plus
  favicon discovery (`favicon`, `absolute_favicon`, `to_dict` keys). The
  instruction is a complete behavioral spec: hostname strips credentials/port,
  title-tag-over-h1, meta-description then post-h1 paragraph then first
  paragraph, h1-following/later image then first image skipping `src`-less
  `<img>`, `None` on empty/malformed HTML, favicon rel whitelist with
  `(href, sizes, rel)` tuples, `sizes=None` when absent/`"any"`, integer pairs
  accepting `"32x32 16x16"` and `"64\u00d764"`, dup preservation, doc order,
  relative-href resolution (`instruction.md:3`).
- **Hidden tests check:** 18 tests in new
  `usercase-test-coderl/test_generic_html_fallback.py`
  (`tests/test.patch:6-339`), one per spec bullet, plus `parser=None`
  passthrough, referential transparency, no caller-arg mutation, and
  no-global-state (`tests/test.patch:250-339`).
- **Instruction sufficient?** Yes — the tests are a near-verbatim encoding of
  the instruction. No file paths are stated, but the package is tiny and the
  failing import (`linkpreview.link_preview`) names the entry point.
- **Traps:** the `\u00d7` (`×`) sizes separator; `"any"` → `None` sizes;
  keeping duplicate favicon rows; absolute-URL resolution against the page URL.
- **Nop:** 16 failed, 2 passed (`no attribute 'favicon'`), reward 0.
- **Verdict: sound.** Evidence: `instruction.md:3`, `tests/test.patch:24-224`.

## 3. format-code-task-000927 — soupsieve: add CSS-identifier `escape`

- **Asks:** implement the CSS escape algorithm (NULL→U+FFFD, control/DEL/first-digit
  code-point escapes, lone `-`, passthrough set) (`instruction.md:3`).
- **Hidden tests check:** 22 tests in new `TestEscape` class in
  `tests/test_api.py` (`tests/test.patch:20-132`), run as
  `pytest tests/test_api.py::TestEscape` — only the new tests gate reward
  (`tests/test.patch:8,143`). The first test requires the function to be
  exposed as callable `sv.escape` (`tests/test.patch:23-27`).
- **Instruction sufficient?** Mostly. The algorithm is fully specified, but
  the instruction never names the export (`sv.escape`, i.e. top-level
  `soupsieve.escape`); the agent must infer it from the repo's `as sv`
  convention and existing `tests/test_api.py` imports. Standard exploration,
  but it is an inference, not a statement.
- **Traps:** exact code-point formats (`\31 `, `\1 `, `\-`, `\ `);
  `--abc`/`-a`/`-_abc` pass through while `-` alone and `-1abc` escape.
- **Nop:** 22 failed (`no attribute 'escape'`), reward 0.
- **Verdict: sound.** Evidence: `instruction.md:3`, `tests/test.patch:23-27`.

## 4. format-code-task-002256 — persist-queue: `full()` wrong when unbounded

- **Asks:** `Queue(path)` with default/`0` `maxsize` reports `full() is True`
  while empty; instruction gives full sync repro transcripts for both the
  broken and the correct bounded behavior (`instruction.md:3`).
- **Hidden tests check:** sync `Queue` *and* `AsyncQueue`, default maxsize
  empty/with-items/after-drain, explicit `maxsize=0`, and bounded
  empty/at-capacity/after-get (`tests/test.patch:10-140`), run with
  `-k "full_default_maxsize or full_bounded or full_explicit_zero"`
  (`tests/test.patch:152,164`).
- **Instruction sufficient?** Yes for the bug, no for the scope: the
  instruction shows only the sync `Queue`, never mentions `AsyncQueue`, yet
  4 of the hidden tests gate the async class (`tests/test.patch:10-62`). The
  correct fix (`full()` is `False` whenever `maxsize <= 0`) trivially covers
  both, but only if the agent generalizes beyond the letter of the report.
- **Traps:** fixing only the sync class; `maxsize=0` means infinite (stdlib
  `queue.Queue` semantics) — `full()` must be `False` even with items.
- **Nop:** 5 failed, 6 passed, reward 0.
- **Verdict: sound** (one-line semantic fix, both classes), with the
  async-scope gap noted. Evidence: `instruction.md:3`,
  `tests/test.patch:10-62`.

## 5. format-code-task-001832 — siuba: new pure `rename` table verb

- **Asks:** export `rename(__data, **kwargs)` from the package working on
  pandas frames, grouped frames, and SQL lazy tables; string or `_.col`
  targets; multi-rename; purity (same result, no input mutation, no
  fs/network/global-state side effects); grouped group-key update;
  `ValueError` (pandas) / `KeyError` (SQL) on non-simple targets
  (`instruction.md:3`).
- **Hidden tests check:** 15 tests in new
  `usercase-test-coderl/test_rename_acceptance.py`
  (`tests/test.patch:60-277`): pandas single/multi × expression/string,
  grouped-pandas group-key update, SQL label checks (`df.a AS "A"`),
  grouped-SQL group-key update, both error types, transparency, no-mutation,
  fresh-subprocess equality, and a monkeypatched no-fs/no-socket purity test.
- **Instruction sufficient?** Yes — unusually, the instruction states even the
  error-type split and the purity requirement the tests enforce. No file paths
  stated; `from siuba import _, group_by, rename` (`tests/test.patch:25`)
  tells the reader the expected export surface.
- **Traps:** SQL `AS` labeling must match loosely-quoted regex
  (`tests/test.patch:54-57`); grouped lazy `group_by` must follow the rename;
  the purity test fails on any import-time or call-time file/socket use.
- **Nop:** collection error `cannot import name 'rename'` (the missing feature
  itself), reward 0.
- **Verdict: sound.** Evidence: `instruction.md:3`, `tests/test.patch:25`.

## 6. format-code-task-000383 — quickfix FIX client: market-data subscribe

- **Asks:** add `subscribe_to_data(**kargs)` / `unsubscribe_to_data(**kargs)`
  sending FIX 4.4 `MarketDataRequest` (`35=V`) on the quote session with exact
  tags: generated `262`, `263=1` (sub) / `2` (unsub), `264=1`, `267=2`
  bid+offer, `265=0`, one `146/55` symbol group defaulting to `EUR/USD`;
  honoring `264/267/265` overrides; `ValueError` + no send on non-integer
  `264`/`267` (`instruction.md:3`).
- **Hidden tests check:** 13 tests in new
  `usercase-test-coderl/test_market_data_requests.py`
  (`tests/test.patch:58-376`): exact tag snapshot incl. bid/offer group
  (`tests/test.patch:134-178`), quote-session routing, ID uniqueness
  per client, overrides on both methods, `ValueError`-without-send
  parametrization, 10 construct/use cycles.
- **Instruction sufficient?** Functionally yes, but the instruction never names
  the class (`Tier1FXAuto`), its module (`fixapp`), its constructor
  `(DataStream(), FixDecoder(), OrderStore(), settings)`, or the two-session
  cfg fixture — all must be discovered from the repo
  (`tests/test.patch:95-108`). Cross-client IDs need only be non-empty, not
  unique (`tests/test.patch:331-332`), which is more lenient than the
  instruction's "generated" wording.
- **Traps:** routing to the quote (not trade) session; `265` may arrive as int
  (`tests/test.patch:240`); per-call unique `262`.
- **Nop:** 13 failed (`no attribute 'subscribe_to_data'`), reward 0.
- **Verdict: sound** (discovery-heavy but fully determined by repo + tests).
  Evidence: `instruction.md:3`, `tests/test.patch:95-108`.

## 7. format-code-task-002407 — python-control: labels lost on responses

- **Asks:** `forced_response` drops the system's `input/state/output` labels,
  breaking `to_pandas()`; reporter shows the manual workaround
  (`instruction.md:3`).
- **Hidden tests check:** far more than asked — labels on `forced_response`,
  `input_output_response`, `step_response`, `impulse_response`,
  `initial_response`, *plus* `input=`/`output=` selection subsetting
  (`input=0` → `input_labels == ["Va"]`, etc.) (`tests/test.patch:23-103`).
  9 tests; a `forced_response`-only fix fails 8 of them.
- **Instruction sufficient?** No. The instruction names one function and one
  symptom; the tests demand a systematic fix across five response
  constructors including selection-subset label slicing that is nowhere
  hinted. The natural fix location (shared `TimeResponseData` construction)
  generalizes for free — but the agent is not told that, and a literal
  reading ("pass it forward in `forced_response`") under-delivers.
- **Traps:** selection subsetting (`tests/test.patch:56-87`); `state_labels`
  on step/impulse/initial only needs presence + length, not names.
- **Nop:** 8 failed, 1 passed, reward 0.
- **Verdict: suspect** — instruction narrower than tests. Evidence:
  `instruction.md:3` (forced_response only) vs `tests/test.patch:36-103`.

## 8. format-code-task-002391 — pip-audit: collapse duplicate advisories

- **Asks:** `Auditor(service, options=AuditOptions()).audit(source)` merges
  alias-linked `VulnerabilityResult`s per dependency; PYSEC id wins regardless
  of order; merged alias set minus own id (`instruction.md:3`, with the
  2- and 3-record examples).
- **Hidden tests check:** 8 tests
  (`tests/test.patch:112-398`): both orderings, 3-way transitive collapse,
  per-dependency independence, distinct advisories preserved, default options,
  and two freshness tests — repeated audits on one `Auditor` must reflect
  current service results, and two `Auditor`s stay independent
  (`tests/test.patch:311-398`).
- **Instruction sufficient?** Yes for the core, with one unstated property:
  the no-stale-cache requirement. An implementation that caches per-service
  results passes every example in the instruction yet fails
  `test_same_auditor_uses_current_service_results_on_repeated_audits`
  (`tests/test.patch:311-353`).
- **Traps:** transitive alias linkage (id↔alias↔alias chains); keeping own id
  out of its alias set; PYSEC-wins even when second.
- **Nop:** 6 failed, 2 passed, reward 0.
- **Verdict: sound**, freshness trap noted. Evidence: `instruction.md:3`,
  `tests/test.patch:311-353`.

## 9. format-code-task-002259 — PHARE: MHD population in `populateDict`

- **Asks:** for `model_options` containing `MHDModel`, populate the C++
  dict with MHD algo values (from `eta/nu/gamma/hyper_mode`), an `mhd_state`
  entry, dimension-appropriate initializer functions from the model's
  callables, and `default` MHD tagging under AMR tagging; leave the Hybrid
  path alone (`instruction.md:3`).
- **Hidden tests check:** 8 tests with a stubbed `pybindlibs.dictator` spy
  (no C++ build needed) (`tests/test.patch:22-94`): exact 2D entry table
  (`tests/test.patch:143-169`), `Simulator.setup()` ordering before C++
  construction, Hybrid-path isolation, `MHDModel`-before-`Simulation` and
  double-registration `RuntimeError`s, `clearDict` idempotency, session
  isolation, 100-cycle repeat (`tests/test.patch:263-491`).
- **Instruction sufficient?** Partly. The core population is specified, but
  none of the lifecycle/error semantics (the two `RuntimeError`s, clearDict
  behavior, session isolation) appear in the instruction
  (`tests/test.patch:333-357`). 1D support is exercised but only 2D values
  are spelled out. This is the heaviest discovery load in the set.
- **Traps:** initializer arity per dimension (spy calls `fn(x)`, `fn(x,y)`,
  `fn(x,y,z)` per ndim — `tests/test.patch:65-76`); exact dict paths/values;
  not breaking the Hybrid path.
- **Nop:** 5 failed, 3 passed (`KeyError` on the new MHD keys), reward 0.
- **Verdict: suspect** — core sound, lifecycle/error contract unstated.
  Evidence: `instruction.md:3` vs `tests/test.patch:333-380`.

## 10. format-code-task-002864 — sqlglot: Trino `JSON_QUERY` WITH/WITHOUT WRAPPER

- **Asks:** `parse_one(sql, read="trino")` raises `ParseError: Expecting )` on
  `json_query(... 'strict $.comment' WITH|WITHOUT ARRAY WRAPPER)`
  (`instruction.md:3`, sqlglot 26.3.9).
- **Hidden tests check:** 6 new tests in `tests/dialects/test_trino.py`
  (`tests/test.patch:18-67`): WITH/WITHOUT ARRAY WRAPPER identity, bare
  WITH/WITHOUT WRAPPER, existing UNCONDITIONAL/CONDITIONAL forms preserved,
  and the issue's full SELECT round-trip. Test command runs the *whole*
  `TestTrino` class, so no regressions allowed (`tests/test.patch:82`).
- **Instruction sufficient?** Yes. Bare `WITH WRAPPER` (no ARRAY) is not in
  the issue text but is the same grammar production; a correct grammar fix
  covers it. The "preserve existing options" test pins the regression risk
  explicitly.
- **Traps:** breaking `WITH UNCONDITIONAL WRAPPER` / `WITHOUT CONDITIONAL
  WRAPPER` forms; round-trip must emit both wrappers in one statement.
- **Nop:** 5 errors, all `ParseError` on the new syntax, reward 0.
- **Verdict: sound.** Evidence: `instruction.md:3`, `tests/test.patch:18-67`.

## Summary table

| # | task | repo | workdir | hidden-test vehicle | nop (HAR-105, reward 0) | a-priori verdict |
|---|---|---|---|---|---|---|
| 1 | 000226 | Pylons/waitress | /testbed | 6 tests, `Test_split_uri` (+1 parse_header) | 6/13 unittest fail | sound |
| 2 | 001896 | meyt/linkpreview | /workspace/repo | 18 tests, new file | 16 fail, 2 pass (`favicon` missing) | sound |
| 3 | 000927 | facelessuser/soupsieve | /testbed | 22 tests, new `TestEscape` only | 22 fail (`escape` missing) | sound |
| 4 | 002256 | peter-wangxu/persist-queue | /testbed | 11 tests, sync + async (`-k` filter) | 5 fail, 6 pass | sound* |
| 5 | 001832 | machow/siuba | /workspace/repo | 15 tests, new file | collection error (`rename` missing) | sound |
| 6 | 000383 | quickfix FIX client | /workspace/repo | 13 tests, new file | 13 fail (`subscribe_to_data` missing) | sound* |
| 7 | 002407 | python-control/python-control | /testbed | 9 tests, 5 response fns + selection | 8 fail, 1 pass | suspect |
| 8 | 002391 | pypa/pip-audit | /workspace/repo | 8 tests, new file | 6 fail, 2 pass | sound* |
| 9 | 002259 | PHAREHUB/PHARE | /workspace/repo | 8 tests, dictator spy | 5 fail, 3 pass (MHD `KeyError`) | suspect |
| 10 | 002864 | tobymao/sqlglot | /testbed | 6 tests, full `TestTrino` class | 5 errors (`ParseError`) | sound |

`*` sound with a noted gap: 002256 (async scope unstated), 000383
(construction/discovery unstated), 002391 (freshness unstated).

**Hand-read guidance.** For 002407 and 002259, a reward-0 run whose trace shows
a correct narrow fix is *task-limited*, not model-limited — attribute to
`task`, not `model`. For the other eight, reward 0 with genuine attempts
points at the model/harness. All ten are feature-addition-shaped (nop shows
missing-attribute/import/parse errors, never setup errors), so expect
`first_failure_what` entries about locating code and iterating against
self-written repros rather than fighting the environment.
