---
status: living
audience:
  - operator
  - builder
---
# Automated Self-Repairing Daily Digest

## 1. Overview and Architecture

The daily digest system (`scripts/daily_digest.py`) generates a consolidated, immutable daily summary of repository activity, research findings, and pull request health for `eval-lab` and its companion `research-context` repository.

Digests are stored in `digests/YYYY-MM-DD.md` and committed as durable artifacts.

### Key Capabilities

1. **Morning Snapshot**: Captures the current git commit SHA, commit volume across repos, and GitHub PR health breakdown.
2. **Knowledge Synthesis**: Extracts summaries from frontmatter of newly authored or modified research documents and records landed pull requests.
3. **PR Pipeline Visibility**: Classifies active GitHub PRs into merged, green-and-unreviewed, and blocked/conflicted states.
4. **Document Index**: Compiles a verified table of research reports created or updated during the day with working disk-validated Markdown links.
5. **Watermark & Self-Repair**: Discovers missing digest dates between the last recorded run and today, automatically generating backfilled digests in chronological order.
6. **Graceful Degradation**: Gracefully handles missing CLI tools (e.g., `gh`), unauthenticated environments, or missing sibling repositories without failing.

---

## 2. Structure of the Daily Digest

Each generated digest markdown file contains standard YAML frontmatter followed by exactly four sections:

```markdown
---
date: YYYY-MM-DD
author: digest-automation
summary: Daily eval lab digest for YYYY-MM-DD.
status: distilled
---
```

### Section 1: Morning snapshot
- `origin/main: <short_sha>` (or HEAD short SHA, or unavailable error explanation)
- `Commits merged in window: <count_a> in eval-lab, <count_b> in research-context`
- `Open PRs: <count> open PRs (<green> green, <failing> failing, <conflicted> conflicted)` (or unavailable message if `gh` is not available/authenticated)

### Section 2: What changed and what was learned
- Merged PR titles during the window: `- PR #<num>: <title>`
- Summaries from new or updated research documents: `- <doc_name>: <summary>`
- Default fallback if none: `- No PRs merged or reports updated in this window.`

### Section 3: Landed versus in flight
- `### Merged PRs`: PRs merged on target date (or `- None.`)
- `### Green-and-unreviewed PRs`: Open PRs with passing status checks whose review decision is not yet approved (or `- None.`)
- `### Blocked or conflicted PRs`: Open PRs with failing checks or merge conflicts/blockers (or `- None.`)
- Emits `unavailable: gh CLI unavailable or not authenticated` if `gh` is unavailable.

### Section 4: Documents created or changed
A Markdown table with relative links verified to exist on disk:

```markdown
| Time | Author | Document | Summary |
|---|---|---|---|
| 00:00:00 | researcher | [trajectory-analysis](../research/analysis/trajectory-analysis.md) | Initial benchmark comparison findings. |
```

---

## 3. Watermark and Self-Repair Contract

### State File
The execution state is tracked in `runs/digest-state.json`:

```json
{
  "last_successful_digest_date": "2026-08-31",
  "generated_dates": [
    "2026-08-16",
    "2026-08-17",
    "2026-08-31"
  ]
}
```

### Self-Repair / Backfill Behavior
1. When `--backfill` is enabled (or when checking watermark), the system determines `last_successful_digest_date`.
2. If `runs/digest-state.json` does not exist, the latest date is discovered from existing `digests/*.md` files.
3. The missing date range is computed: `[last_successful_digest_date + 1 day, ..., target_date]`.
4. For each missing date in order, `scripts/daily_digest.py` generates the corresponding `digests/YYYY-MM-DD.md`.
5. The watermark file is updated with `last_successful_digest_date` and recorded `generated_dates`.

### Determinism & Idempotency
Given identical repository, git, and filesystem state, generating a digest for a date produces the identical SHA256 content on repeated invocations.

---

## 4. Usage and Automation

### CLI Usage

```bash
# Generate today's digest
uv run python scripts/daily_digest.py

# Generate digest for a specific date
uv run python scripts/daily_digest.py --date 2026-08-31

# Backfill all missing days up to target date
uv run python scripts/daily_digest.py --date 2026-08-31 --backfill

# Via Makefile
make digest
```

### Launchd Scheduled Execution (macOS)

The service is managed as a user launch agent via `launchd`:
- **Label**: `com.petermakhnatch.evallab.digest`
- **Schedule**: Every morning at 07:00 local time (`StartCalendarInterval`: Hour 7, Minute 0), with interval execution every 4 hours (`StartInterval`: 14400), and `RunAtLoad: true`.
- **Logs**:
  - `runs/logs/digest.out`
  - `runs/logs/digest.err`

#### Installation

```bash
bash scripts/launchd/install.sh
```

#### Verification

```bash
launchctl print "gui/$(id -u)/com.petermakhnatch.evallab.digest"
```

#### Uninstallation

```bash
bash scripts/launchd/uninstall.sh
```
