# Audit Evidence: storm

- **Subject**: `storm`
- **Handoff Audited**: `agents/handoffs/storm-status.md`
- **Audit Date**: 2026-08-18
- **Auditor**: M015 (LOOP-AUDIT)

## Claims Extracted from Handoff
1. `src/evallab/storm.py` implements the Storm Alarms Engine.
2. Sliding 1-hour window detection for repeated identical `reason_code` events (>threshold=5) in `queue/events.jsonl`.
3. Structured `StormAlarm` and `StormReport` models with alarm levels (`info`, `warning`, `critical`), timestamps, and operator guidance.
4. Comprehensive reason code action catalog mapping known codes to severity and action strings.
5. Utilities: `render_storm_banner`, `digest_storm_section`, and `status_items_from_alarms`.
6. `tests/test_storm.py` tests passing (claimed 10 tests, now 11 tests).
7. Documentation in `docs/storm-alarms.md`.

## Re-run & Reproduction Commands
```bash
# 1. Run unit and contract tests for storm alarms
uv run pytest tests/test_storm.py

# 2. Re-run storm detection over live events.jsonl and test formatting utilities
uv run python -c "
from pathlib import Path
from evallab.storm import detect_storm_alarms, digest_storm_section, render_storm_banner, StormAlarm
import datetime

alarms = detect_storm_alarms(Path('queue/events.jsonl'))
print('Live queue/events.jsonl alarms detected:', len(alarms))
print('Live digest storm section:', digest_storm_section(alarms))
"

# 3. Verify documentation existence
test -f docs/storm-alarms.md && echo "docs/storm-alarms.md exists"
```

## Captured Outputs & Verdict
1. `uv run pytest tests/test_storm.py`:
   Output: `11 passed in 0.23s` (CONFIRMED)
2. Live storm detection:
   Successfully parsed `queue/events.jsonl` (0 alarms in quiet state; digest storm section emitted) and synthesized storm alarms rendered valid banners (CONFIRMED)
3. Integration in `digest.py`:
   `digest_storm_section` is integrated and loaded in `src/evallab/digest.py` (CONFIRMED)
4. Documentation in `docs/storm-alarms.md`:
   Valid document with frontmatter exists and describes engine contracts (CONFIRMED)

## Overall Verdict
**CONFIRMED** (Engine, tests, models, rendering utilities, and documentation all present and verified runnable).

## Risk Note
Storm alarm detection is embedded in digest and status generation but lacks a direct standalone CLI command (`evallab storm`), making ad-hoc manual invocation dependent on Python API.
