"""Auditor-source parsers and evidence joins for the tb4-audit-v1 answer key.

Three external auditors are joined onto the Preference Model (PM) TB4 issue
catalog (``pm_source.parse_pm_catalog`` output):

* **Epoch AI** — the Terminal-Bench 4.0.0 benchmark review
  (``parse_epoch_flags``). Epoch's Errors table gives one row per flagged task:
  task slug, False Positive / False Negative, and one public
  ``harbor-framework/terminal-bench`` GitHub issue. Epoch says it reviewed the
  publicly documented GitHub issues; it reports no executions of its own.
* **Tokenless EnvCheck** — the existing ``envcheck-tb4-39`` answer key
  (``load_envcheck_labels``): 13 tasks with confirmed EnvCheck findings (one
  root cause per Tokenless finding slug), 12 former weak negatives relabelled
  from Epoch issues, and the remaining weak negatives. Optionally enriched with
  the raw Tokenless ``findings.jsonl`` (cases with grader verdicts and
  ``prior_work`` issue links).
* **PM own review** — taken from the catalog's own attribution text.

``annotate_catalog`` joins them per finding, conservatively:

* A source is credited on a finding only when PM explicitly attributes it
  (without contradiction by the source's own data) or when a concrete identity
  links them: the same GitHub issue on the same task as an Epoch row, a
  Tokenless slug spelled out in PM's attribution text, or (only with PM's
  Tokenless attribution) a Tokenless finding's ``prior_work`` issue that names
  exactly one slug on the task. An unattributed prior_work issue is a candidate.
* An Epoch task flag is never applied to every finding on its task, a weak
  negative is never read as "clean", GitHub issues are evidence rather than a
  fourth auditor, and PM's citation of Epoch/Tokenless is provenance, not an
  independent replication. Attribution-based and direct (source-data-backed)
  agreement are reported separately; unresolved and disputed joins are listed.

All functions are pure: no network, no subprocesses, no task/benchmark code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from itertools import combinations
from pathlib import Path
from typing import Any

EPOCH_URL = "https://epoch.ai/benchmarks/terminal-bench-4/review"
TB_REPO = "harbor-framework/terminal-bench"
TB_ISSUE_URL = f"https://github.com/{TB_REPO}/issues/{{number}}"
TOKENLESS_REPO_URL = "https://github.com/tokenless-ai/envcheck-findings"
TOKENLESS_POST = "2026-10-grader-defects"

AUDITORS = ("epoch", "pm", "tokenless")

EPOCH_ERROR_TYPES = {"False Positive": "FP", "False Negative": "FN"}
EPOCH_TABLE_HEADER = ("Task", "Error type", "Link")

# Historical Terminal-Bench checkouts the envcheck-tb4-39 labels are bound to
# (envcheck-tb4-39/README.md "Packages"); verified against the README text and,
# when loaded, the Tokenless sweep pins.
HISTORICAL_PINS = {
    "v4.0.0": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
    "1dcda8716784": "1dcda8716784493721921c23e4bc7f7d988b4494",
}

_ISSUE_HREF_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/harbor-framework/terminal-bench/issues/(\d+)/?(?:[#?].*)?$"
)
_ISSUE_ANY_RE = re.compile(r"github\.com/harbor-framework/terminal-bench/issues/(\d+)")
_EPOCH_OVERLAY_ID_RE = re.compile(r"^harbor-framework/terminal-bench#(\d+)$")
_SHA40_RE = re.compile(r"@([0-9a-f]{40})\b")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", unescape(text)).strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug_in(slug: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(slug)}(?![\w-])", text) is not None


# --------------------------------------------------------------------------
# Epoch review
# --------------------------------------------------------------------------


class _EpochPageParser(HTMLParser):
    """Collects tables (cells with text and hrefs), footnotes and list items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self.text_parts: list[str] = []
        self.footnotes: dict[str, dict[str, Any]] = {}
        self.list_items: list[dict[str, Any]] = []
        self._table: dict[str, Any] | None = None
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._section_stack: list[tuple[str, str | None]] = []
        self._li_stack: list[dict[str, Any]] = []
        self._skip_depth = 0

    def _section(self) -> str | None:
        for _tag, sid in reversed(self._section_stack):
            if sid:
                return sid
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag in ("mdx-section", "section"):
            self._section_stack.append((tag, a.get("id")))
        elif tag == "table":
            self._table = {"section": self._section(), "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"tag": tag, "text": [], "hrefs": [], "link_texts": [], "_in_a": False}
        elif tag == "a":
            href = a.get("href")
            if self._cell is not None and href:
                self._cell["hrefs"].append(href)
                self._cell["link_texts"].append([])
                self._cell["_in_a"] = True
            for li in self._li_stack:
                if href:
                    li["hrefs"].append(href)
        elif tag == "li":
            self._li_stack.append(
                {"id": a.get("id"), "section": self._section(), "text": [], "hrefs": []}
            )
        elif tag == "sup":
            for li in self._li_stack:
                li["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("mdx-section", "section"):
            for i in range(len(self._section_stack) - 1, -1, -1):
                if self._section_stack[i][0] == tag:
                    del self._section_stack[i]
                    break
        elif tag == "a" and self._cell is not None:
            self._cell["_in_a"] = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            cell = self._cell
            self._row.append(
                {
                    "tag": cell["tag"],
                    "text": _norm("".join(cell["text"])),
                    "hrefs": cell["hrefs"],
                    "link_texts": [_norm("".join(t)) for t in cell["link_texts"]],
                }
            )
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "li" and self._li_stack:
            li = self._li_stack.pop()
            item = {
                "id": li["id"],
                "section": li["section"],
                "text": _norm("".join(li["text"])),
                "hrefs": li["hrefs"],
            }
            if li["id"] and li["id"].startswith("user-content-fn-"):
                self.footnotes[li["id"][len("user-content-fn-") :]] = item
            else:
                self.list_items.append(item)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.text_parts.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._cell["_in_a"] and self._cell["link_texts"]:
                self._cell["link_texts"][-1].append(data)
        for li in self._li_stack:
            li["text"].append(data)


def _issue_number(href: str) -> int | None:
    m = _ISSUE_HREF_RE.match(href.strip())
    return int(m.group(1)) if m else None


def _find_claim(pattern: str, text: str) -> dict[str, Any] | None:
    m = re.search(pattern, text)
    if not m:
        return None
    return {"literal": m.group(0), "groups": list(m.groups())}


def parse_epoch_flags(
    html: str, *, source_url: str = EPOCH_URL, retrieved_at: str | None = None
) -> dict:
    """Parse Epoch's TB4 review page into per-task FP/FN flags.

    ``html`` is the raw page decoded as UTF-8; ``source.sha256`` is computed on
    ``html.encode("utf-8")`` and equals the raw-file hash for a lossless UTF-8
    decode. Raises ``ValueError`` when the Errors table is missing, ambiguous or
    malformed; never fills in rows that are not on the page.
    """
    parser = _EpochPageParser()
    parser.feed(html)
    parser.close()

    candidates = [
        t
        for t in parser.tables
        if t["rows"] and tuple(c["text"] for c in t["rows"][0]) == EPOCH_TABLE_HEADER
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one Epoch Errors table with header {EPOCH_TABLE_HEADER}, "
            f"found {len(candidates)}"
        )
    table = candidates[0]
    discrepancies: list[dict[str, Any]] = []

    flags: list[dict[str, Any]] = []
    for idx, row in enumerate(table["rows"][1:], start=1):
        if len(row) != 3 or any(c["tag"] != "td" for c in row):
            raise ValueError(f"Epoch Errors row {idx} is not three <td> cells: {row!r}")
        task_cell, type_cell, link_cell = row
        task = task_cell["text"]
        error_type = type_cell["text"]
        if not task or task_cell["hrefs"]:
            raise ValueError(f"Epoch Errors row {idx} has an unexpected task cell: {task_cell!r}")
        if error_type not in EPOCH_ERROR_TYPES:
            raise ValueError(f"Epoch Errors row {idx} has unknown error type {error_type!r}")
        if len(link_cell["hrefs"]) != 1:
            raise ValueError(
                f"Epoch Errors row {idx} ({task}) has {len(link_cell['hrefs'])} links, expected 1"
            )
        href = link_cell["hrefs"][0]
        number = _issue_number(href)
        if number is None:
            discrepancies.append(
                {
                    "kind": "epoch_link_not_terminal_bench_issue",
                    "row_index": idx,
                    "task": task,
                    "href": href,
                }
            )
        flag = EPOCH_ERROR_TYPES[error_type]
        flags.append(
            {
                "flag_id": f"epoch-tb4:{task}:{flag}:{number if number is not None else 'nolink'}",
                "row_index": idx,
                "task": task,
                "error_type": error_type,
                "flag": flag,
                "issue_number": number,
                "issue_url": TB_ISSUE_URL.format(number=number) if number is not None else None,
                "link_href": href,
                "link_text": link_cell["link_texts"][0] if link_cell["link_texts"] else "",
                "row_text": " | ".join(c["text"] for c in row),
                "section": table["section"],
                "evidence_kind": "epoch_review_of_public_github_issue",
                "measured_by_epoch": False,
            }
        )

    if not flags:
        raise ValueError("Epoch Errors table has no data rows")

    tasks: dict[str, dict[str, Any]] = {}
    for f in flags:
        t = tasks.setdefault(
            f["task"], {"task": f["task"], "flag_ids": [], "flags": [], "issue_numbers": []}
        )
        t["flag_ids"].append(f["flag_id"])
        if f["flag"] not in t["flags"]:
            t["flags"].append(f["flag"])
        if f["issue_number"] is not None and f["issue_number"] not in t["issue_numbers"]:
            t["issue_numbers"].append(f["issue_number"])
    for t in tasks.values():
        t["flags"].sort()
        t["issue_numbers"].sort()

    issue_tasks: dict[int, set[str]] = {}
    for f in flags:
        if f["issue_number"] is not None:
            issue_tasks.setdefault(f["issue_number"], set()).add(f["task"])
    flag_ids = Counter(f["flag_id"] for f in flags)

    text = _norm("".join(parser.text_parts))
    claims = {
        "defective_tasks": _find_claim(
            r"(\d+) out of (\d+) tasks \(([\d.]+)%\) contain publicly noted scoring defects", text
        ),
        "pending_fixes": _find_claim(
            r"(\d+) of the defective tasks have pending updates to fix", text
        ),
        "methodology": _find_claim(
            r"We reviewed the issues publicly documented in the Github repo[^.]*\.", text
        ),
        "partial_review": _find_claim(r"This is a partial review\.[^.]*\.", text),
        "verdict": _find_claim(r"hence our Flawed designation for version (\d+(?:\.\d+)*)", text),
        "license": _find_claim(
            r"Epoch AI’s work is free to use, distribute, and reproduce provided the source and "
            r"authors are credited under the Creative Commons Attribution license",
            text,
        ),
        "citation": _find_claim(r"Epoch AI \((\d{4})\), \"Terminal-Bench 4\.0\.0\"", text),
    }
    for name, value in claims.items():
        if value is None:
            discrepancies.append({"kind": "epoch_page_claim_not_found", "claim": name})

    unique_tasks = len(tasks)
    claimed = claims["defective_tasks"]
    if claimed is not None and int(claimed["groups"][0]) != unique_tasks:
        discrepancies.append(
            {
                "kind": "epoch_claimed_defective_tasks_vs_table",
                "claimed": int(claimed["groups"][0]),
                "table_unique_tasks": unique_tasks,
                "literal": claimed["literal"],
            }
        )
    multi_row = sorted(t for t, v in tasks.items() if len(v["flag_ids"]) > 1)
    if multi_row:
        discrepancies.append({"kind": "epoch_task_with_multiple_rows", "tasks": multi_row})
    shared_issues = {str(n): sorted(ts) for n, ts in issue_tasks.items() if len(ts) > 1}
    if shared_issues:
        discrepancies.append({"kind": "epoch_issue_on_multiple_tasks", "issues": shared_issues})
    dup_ids = sorted(k for k, v in flag_ids.items() if v > 1)
    if dup_ids:
        discrepancies.append({"kind": "epoch_duplicate_rows", "flag_ids": dup_ids})

    task_names = sorted(tasks)
    narrative = []
    for li in parser.list_items:
        if li["section"] != "section-representative-errors":
            continue
        refs = [
            m.group(1) for m in re.finditer(r"#user-content-fn-([\w-]+)", " ".join(li["hrefs"]))
        ]
        narrative.append(
            {
                "text": li["text"],
                "tasks_mentioned": [t for t in task_names if _slug_in(t, li["text"])],
                "footnotes": refs,
                "footnote_issue_numbers": sorted(
                    {
                        int(n)
                        for ref in refs
                        for href in parser.footnotes.get(ref, {}).get("hrefs", [])
                        for n in _ISSUE_ANY_RE.findall(href)
                    }
                ),
            }
        )
    footnotes = {
        k: {
            "text": v["text"],
            "hrefs": v["hrefs"],
            "issue_numbers": sorted({int(n) for h in v["hrefs"] for n in _ISSUE_ANY_RE.findall(h)}),
        }
        for k, v in sorted(parser.footnotes.items())
    }
    table_issue_set = {f["issue_number"] for f in flags}
    for item in narrative:
        extra = [n for n in item["footnote_issue_numbers"] if n not in table_issue_set]
        if extra:
            discrepancies.append(
                {
                    "kind": "epoch_narrative_cites_issue_not_in_table",
                    "issue_numbers": extra,
                    "tasks_mentioned": item["tasks_mentioned"],
                    "text": item["text"],
                }
            )

    data = html.encode("utf-8")
    return {
        "schema": "tb4-audit-epoch-flags/v1",
        "source": {
            "auditor": "epoch",
            "url": source_url,
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "retrieved_at": retrieved_at,
            "license": claims["license"]["literal"] if claims["license"] else None,
            "license_spdx": "CC-BY-4.0" if claims["license"] else None,
            "scope": (
                "Partial review of Terminal-Bench 4.0.0 in its current state; Epoch reviewed "
                "publicly documented GitHub issues and lists one issue per flagged task. Epoch "
                "reports no executions of its own; flags are task-level, not finding-level."
            ),
        },
        "claims": claims,
        "flags": flags,
        "tasks": tasks,
        "narrative": narrative,
        "footnotes": footnotes,
        "counts": {
            "table_tr_including_header": len(table["rows"]),
            "rows": len(flags),
            "unique_tasks": unique_tasks,
            "fp_rows": sum(f["flag"] == "FP" for f in flags),
            "fn_rows": sum(f["flag"] == "FN" for f in flags),
            "unique_issue_numbers": len({f["issue_number"] for f in flags if f["issue_number"]}),
            "tasks_with_both_fp_and_fn": sorted(t for t, v in tasks.items() if len(v["flags"]) > 1),
        },
        "discrepancies": discrepancies,
    }


# --------------------------------------------------------------------------
# EnvCheck (envcheck-tb4-39) labels
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw = path.read_bytes()
    rows = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return raw, rows


def _tokenless_record(raw: dict[str, Any]) -> dict[str, Any]:
    cases = raw.get("cases") or []
    prior = raw.get("prior_work") or []
    measured = [c for c in cases if isinstance(c.get("grader_verdict"), dict)]
    defect = raw.get("defect") or {}
    return {
        "slug": raw["slug"],
        "finding_id": raw.get("id"),
        "title": raw.get("title"),
        "status": raw.get("status"),
        "confidence": raw.get("confidence"),
        "kinds": raw.get("kinds"),
        "intent_level": raw.get("intent_level"),
        "defect_class": defect.get("class"),
        "defect_family": defect.get("family"),
        "defect_stage": defect.get("stage"),
        "discovered_by": raw.get("discovered_by"),
        "sweeps": raw.get("sweeps"),
        "fixed_in": raw.get("fixed_in"),
        "files": raw.get("files"),
        "prior_work": prior,
        "prior_work_issue_numbers": sorted(
            {int(n) for p in prior for n in _ISSUE_ANY_RE.findall(str(p.get("url") or ""))}
        ),
        "prior_work_cites_epoch_review": any(
            str(p.get("url") or "").rstrip("/") == EPOCH_URL for p in prior
        ),
        "case_tasks": sorted({c.get("task") for c in cases if c.get("task")}),
        "cases_total": len(cases),
        "cases_with_grader_verdict": len(measured),
        "cases_with_effect": sum(1 for c in cases if c.get("effect") is not None),
        "case_shows": dict(sorted(Counter(str(c.get("shows")) for c in cases).items())),
    }


def load_envcheck_labels(
    items_path: Path,
    *,
    epoch_flags_path: Path | None = None,
    findings_path: Path | None = None,
    readme_path: Path | None = None,
) -> dict:
    """Load all envcheck-tb4-39 rows with original label semantics retained.

    ``epoch_flags_path`` and ``readme_path`` default to the siblings of
    ``items_path``. ``findings_path`` (raw Tokenless ``findings.jsonl`` at the
    commit named in ``label_source``) is optional; without it Tokenless
    ``prior_work`` issue joins are unavailable and reported as such.
    """
    items_path = Path(items_path)
    epoch_flags_path = Path(epoch_flags_path or items_path.parent / "epoch_flags.json")
    readme_path = Path(readme_path or items_path.parent / "README.md")
    raw_items, rows = _read_jsonl(items_path)

    ids = Counter(r["item_id"] for r in rows)
    dups = sorted(k for k, v in ids.items() if v > 1)
    if dups:
        raise ValueError(f"duplicate envcheck item_id(s): {dups}")
    names = Counter(r["package"]["name"] for r in rows)
    if any(v > 1 for v in names.values()):
        raise ValueError(
            f"duplicate envcheck package names: {sorted(k for k, v in names.items() if v > 1)}"
        )

    discrepancies: list[dict[str, Any]] = []

    readme_meta = None
    if readme_path.is_file():
        readme_bytes = readme_path.read_bytes()
        readme_text = readme_bytes.decode("utf-8")
        readme_meta = {"path": str(readme_path), "sha256": _sha256_bytes(readme_bytes)}
        for pin, commit in HISTORICAL_PINS.items():
            if f"`{pin}`" not in readme_text or commit not in readme_text:
                raise ValueError(f"README {readme_path} does not document pin {pin} -> {commit}")

    epoch_meta = None
    epoch_by_task: dict[str, dict[str, Any]] = {}
    if epoch_flags_path.is_file():
        eraw = epoch_flags_path.read_bytes()
        edoc = json.loads(eraw)
        epoch_meta = {
            "path": str(epoch_flags_path),
            "sha256": _sha256_bytes(eraw),
            "source": edoc.get("source"),
            "issues_read": edoc.get("issues_read"),
            "note": edoc.get("note"),
            "flags": len(edoc.get("flags") or []),
        }
        for flag in edoc.get("flags") or []:
            if flag["task"] in epoch_by_task:
                raise ValueError(f"epoch_flags.json lists {flag['task']} twice")
            epoch_by_task[flag["task"]] = flag
    elif any(r.get("label_strength") == "epoch_flagged" for r in rows):
        raise ValueError(f"epoch_flagged rows present but {epoch_flags_path} is missing")

    label_sources = sorted({r.get("label_source") for r in rows if r.get("label_source")})
    commits = sorted(
        {
            m.group(1)
            for r in rows
            if r.get("label_strength") != "epoch_flagged"
            for m in [_SHA40_RE.search(r.get("label_source") or "")]
            if m
        }
    )
    if len(commits) != 1:
        raise ValueError(f"expected one Tokenless findings commit in label_source, got {commits}")
    tokenless_commit = commits[0]

    tokenless_raw: dict[str, dict[str, Any]] = {}
    findings_meta = None
    if findings_path is not None:
        findings_path = Path(findings_path)
        fbytes, frows = _read_jsonl(findings_path)
        post_rows = [r for r in frows if r.get("post") == TOKENLESS_POST]
        tb_rows = [
            r
            for r in post_rows
            if any((s or {}).get("env") == "terminal-bench" for s in r.get("sweeps") or [])
        ]
        for r in tb_rows:
            if r["slug"] in tokenless_raw:
                raise ValueError(f"duplicate Tokenless slug {r['slug']} in {findings_path}")
            tokenless_raw[r["slug"]] = r
        sweep_pins = sorted(
            {
                s.get("pin")
                for r in tb_rows
                for s in r.get("sweeps") or []
                if s.get("env") == "terminal-bench" and s.get("pin")
            }
        )
        for pin_str in sweep_pins:
            pin, _, rest = pin_str.partition("@git:")
            commit = rest.rsplit("@", 1)[-1]
            if HISTORICAL_PINS.get(pin) != commit:
                raise ValueError(f"Tokenless sweep pin {pin_str} disagrees with {HISTORICAL_PINS}")
        findings_meta = {
            "path": str(findings_path),
            "sha256": _sha256_bytes(fbytes),
            "repo": TOKENLESS_REPO_URL,
            "commit": tokenless_commit,
            "commit_basis": "label_source of envcheck-tb4-39 rows (caller must pass the file at this commit)",
            "post": TOKENLESS_POST,
            "rows_total": len(frows),
            "rows_post": len(post_rows),
            "rows_post_terminal_bench": len(tb_rows),
            "sweep_pins": sweep_pins,
            "license": "code MIT, data CC BY 4.0 (envcheck-tb4-39/README.md Licence)",
        }

    items: list[dict[str, Any]] = []
    tokenless_findings: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: r["item_id"]):
        task = row["package"]["name"]
        pin = row["package"]["pin"]
        if pin not in HISTORICAL_PINS:
            raise ValueError(f"{row['item_id']}: unknown package pin {pin!r}")
        strength = row.get("label_strength")
        if strength not in ("confirmed", "epoch_flagged", "weak_negative"):
            raise ValueError(f"{row['item_id']}: unknown label_strength {strength!r}")
        root_causes = []
        for rc in row.get("root_cause") or []:
            m = _EPOCH_OVERLAY_ID_RE.match(str(rc.get("id")))
            origin = "epoch_overlay" if m else "tokenless"
            entry = {**rc, "origin": origin, "issue_number": int(m.group(1)) if m else None}
            if origin == "tokenless":
                if strength != "confirmed":
                    raise ValueError(f"{row['item_id']}: Tokenless root cause on {strength} row")
                slug = rc["id"]
                raw = tokenless_raw.get(slug)
                if findings_path is not None and raw is None:
                    raise ValueError(f"Tokenless slug {slug} not found in {findings_path}")
                rec = (
                    _tokenless_record(raw)
                    if raw
                    else {
                        "slug": slug,
                        "title": rc.get("summary"),
                        "prior_work_issue_numbers": None,
                        "prior_work_cites_epoch_review": None,
                        "cases_with_grader_verdict": None,
                    }
                )
                if raw and task not in rec["case_tasks"]:
                    discrepancies.append(
                        {
                            "kind": "tokenless_case_task_differs_from_item",
                            "slug": slug,
                            "item_task": task,
                            "case_tasks": rec["case_tasks"],
                        }
                    )
                rec.update(
                    {
                        "task": task,
                        "item_id": row["item_id"],
                        "pin": pin,
                        "pin_commit": HISTORICAL_PINS[pin],
                        "category": rc.get("category"),
                        "source_class": rc.get("source_class"),
                    }
                )
                tokenless_findings[slug] = rec
            root_causes.append(entry)

        overlay = None
        if strength == "epoch_flagged":
            flag = epoch_by_task.get(task)
            if flag is None:
                raise ValueError(
                    f"{row['item_id']}: epoch_flagged but absent from epoch_flags.json"
                )
            issues = [rc["issue_number"] for rc in root_causes if rc["origin"] == "epoch_overlay"]
            if issues != [flag["issue"]]:
                raise ValueError(
                    f"{row['item_id']}: root_cause issue {issues} != epoch_flags {flag['issue']}"
                )
            overlay = {
                "issue_number": flag["issue"],
                "issue_url": TB_ISSUE_URL.format(number=flag["issue"]),
                "epoch_error": flag.get("epoch_error"),
                "flag": EPOCH_ERROR_TYPES.get(flag.get("epoch_error")),
                "issue_state_when_read": flag.get("state"),
                "category_from_issue": flag.get("category"),
                "issue_title": flag.get("title"),
                "issues_read": epoch_meta["issues_read"] if epoch_meta else None,
            }
        elif task in epoch_by_task:
            raise ValueError(f"{row['item_id']}: in epoch_flags.json but label_strength={strength}")

        items.append(
            {
                "item_id": row["item_id"],
                "task": task,
                "pin": pin,
                "pin_commit": HISTORICAL_PINS[pin],
                "task_digest": (row.get("subject") or {}).get("task_digest"),
                "expected_verdict": row.get("expected_verdict"),
                "label_strength": strength,
                "expected_category": row.get("expected_category"),
                "expected_categories": row.get("expected_categories"),
                "root_causes": root_causes,
                "tokenless_slugs": [rc["id"] for rc in root_causes if rc["origin"] == "tokenless"],
                "original_envcheck_label": "defect" if strength == "confirmed" else "no_finding",
                "epoch_added": strength == "epoch_flagged",
                "epoch_overlay": overlay,
                "cleanliness": (
                    "not_established: weak negative only means the Tokenless post reported no "
                    "finding for this task at this pin"
                    if strength == "weak_negative"
                    else None
                ),
                "source_row": copy.deepcopy(row),
            }
        )

    missing = sorted(set(epoch_by_task) - {i["task"] for i in items})
    if missing:
        raise ValueError(f"epoch_flags.json tasks not in items: {missing}")
    if tokenless_raw:
        extra = sorted(set(tokenless_raw) - set(tokenless_findings))
        if extra:
            discrepancies.append(
                {"kind": "tokenless_tb4_slugs_not_in_envcheck_items", "slugs": extra}
            )

    by_strength = Counter(i["label_strength"] for i in items)
    return {
        "schema": "tb4-audit-envcheck-labels/v1",
        "source": {
            "auditor": "tokenless",
            "items_path": str(items_path),
            "items_sha256": _sha256_bytes(raw_items),
            "key_id": sorted({r.get("key_id") for r in rows}),
            "label_sources": label_sources,
            "tokenless_commit": tokenless_commit,
            "epoch_flags": epoch_meta,
            "tokenless_findings": findings_meta,
            "readme": readme_meta,
            "scope": (
                "Historical TB4 task packages at pins v4.0.0 / 1dcda8716784; confirmed rows are "
                "Tokenless EnvCheck findings with grader-verdict cases, epoch_flagged rows are "
                "Epoch issues read by Eval Lab (not Tokenless evidence), weak negatives are "
                "sampled tasks with no published Tokenless finding (not clean)."
            ),
        },
        "historical_pins": {
            pin: {"repo": f"https://github.com/{TB_REPO}", "commit": commit}
            for pin, commit in HISTORICAL_PINS.items()
        },
        "items": items,
        "tokenless_findings": dict(sorted(tokenless_findings.items())),
        "counts": {
            "items": len(items),
            "by_label_strength": dict(sorted(by_strength.items())),
            "by_expected_verdict": dict(
                sorted(Counter(i["expected_verdict"] for i in items).items())
            ),
            "by_pin": dict(sorted(Counter(i["pin"] for i in items).items())),
            "tokenless_root_causes": len(tokenless_findings),
            "epoch_overlay_items": sum(i["epoch_added"] for i in items),
        },
        "discrepancies": discrepancies,
    }


# --------------------------------------------------------------------------
# Joins
# --------------------------------------------------------------------------


def _truthy_claim(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


_COMMENT_FRAGMENTS = ("#issuecomment-", "#discussion_r", "#pullrequestreview-")


def _is_pr_comment_ref(ref: dict[str, Any]) -> bool:
    """A comment on an issue or pull request does not name that thread as a report."""
    href = str(ref.get("href") or ref.get("url") or "")
    if any(fragment in href for fragment in _COMMENT_FRAGMENTS):
        return True
    return str(ref.get("kind", "")).startswith("comment")


def _finding_issue_refs(row: dict[str, Any]) -> tuple[set[int], set[int]]:
    """Return (concrete, other) terminal-bench issue numbers cited by a PM finding.

    Concrete = issue links in PM's attribution ("sources") or upstream-status
    spans; other = PR links and links in the finding body. Links to individual
    comments or reviews (``#issuecomment-``, ``#discussion_r``,
    ``#pullrequestreview-``) are excluded before any number is collected.
    """
    concrete: set[int] = set()
    other: set[int] = set()
    refs = row.get("github_refs") or []
    comment_numbers = {int(r["number"]) for r in refs if _is_pr_comment_ref(r) and r.get("number")}
    for n in row.get("github_issue_numbers") or []:
        if int(n) not in comment_numbers:
            concrete.add(int(n))
    for ref in refs:
        n = ref.get("number")
        if n is None or _is_pr_comment_ref(ref):
            continue
        if ref.get("where") in ("sources", "upstream") and ref.get("kind", "issue") == "issue":
            concrete.add(int(n))
        else:
            other.add(int(n))
    return concrete, other - concrete


def _catalog_task_slugs(catalog: dict[str, Any]) -> list[str]:
    slugs: set[str] = set()
    for t in catalog.get("tasks") or []:
        if isinstance(t, str):
            slugs.add(t)
            continue
        for key in ("task", "slug", "section_id", "name"):
            v = t.get(key)
            if isinstance(v, str) and v and v != "benchmark-wide":
                slugs.add(v)
                break
    for f in catalog.get("findings") or []:
        if f.get("task"):
            slugs.add(f["task"])
    return sorted(slugs)


def _dist(values: list[int]) -> dict[str, int]:
    c = Counter(values)
    return {str(k): c.get(k, 0) for k in range(len(AUDITORS) + 1)}


def _combo_counts(sets: list[list[str]]) -> dict[str, int]:
    out = {"none": 0}
    for r in range(1, len(AUDITORS) + 1):
        for combo in combinations(AUDITORS, r):
            out["+".join(combo)] = 0
    for s in sets:
        out["+".join(sorted(s)) if s else "none"] += 1
    return out


def annotate_catalog(catalog: dict, epoch: dict, envcheck: dict) -> dict:
    """Join Epoch flags and EnvCheck labels onto PM findings and count agreement.

    Returns a new dict; inputs are not mutated. Every PM finding is returned
    with all of its original fields plus ``auditor_evidence``. Every Epoch row,
    Tokenless finding and EnvCheck item is returned with its PM coverage.
    """
    findings_in = catalog.get("findings")
    if not isinstance(findings_in, list) or not findings_in:
        raise ValueError("catalog has no findings list")
    fids = Counter(f.get("finding_id") for f in findings_in)
    if None in fids or any(v > 1 for v in fids.values()):
        raise ValueError("catalog findings need unique non-null finding_id values")

    epoch_flags = epoch["flags"]
    epoch_by_task: dict[str, list[dict[str, Any]]] = {}
    epoch_issue_task: dict[int, set[str]] = {}
    for f in epoch_flags:
        epoch_by_task.setdefault(f["task"], []).append(f)
        if f["issue_number"] is not None:
            epoch_issue_task.setdefault(f["issue_number"], set()).add(f["task"])

    env_items = {i["task"]: i for i in envcheck["items"]}
    tok = envcheck["tokenless_findings"]
    tok_by_task: dict[str, list[str]] = {}
    for slug, rec in tok.items():
        tok_by_task.setdefault(rec["task"], []).append(slug)
    for v in tok_by_task.values():
        v.sort()
    prior_work_loaded = envcheck["source"].get("tokenless_findings") is not None
    # Slugs ordered longest first so a slug that contains another wins its text.
    all_slugs = sorted(tok, key=lambda s: (-len(s), s))

    work: list[dict[str, Any]] = []
    for row in findings_in:
        task = row.get("task")
        scope = row.get("scope") or ("task" if task else "benchmark")
        attr = row.get("attribution") or {}
        concrete_issues, other_issues = _finding_issue_refs(row)
        sources_text = " ".join(
            str(x)
            for x in (
                row.get("sources_text"),
                attr.get("tokenless_detail"),
                attr.get("github_detail"),
            )
            if x
        )

        # ---- Epoch ----
        e_attr = bool(attr.get("epoch"))
        e_qual = attr.get("epoch_flag")
        task_flags = epoch_by_task.get(task, []) if task else []
        shared = [f for f in task_flags if f["issue_number"] in concrete_issues]
        body_only = [f for f in task_flags if f["issue_number"] in other_issues and f not in shared]
        cross_task = sorted(
            {
                (n, t)
                for n in concrete_issues | other_issues
                for t in epoch_issue_task.get(n, set())
                if t != task
            }
        )
        e_disputes: list[str] = []
        e_bases: list[str] = []
        if e_attr and not task_flags:
            e_disputes.append(
                "pm_attributes_epoch_but_epoch_table_has_no_row_for_task"
                if task
                else "pm_attributes_epoch_on_benchmark_scope_finding"
            )
        if e_attr and e_qual and task_flags:
            types = sorted({f["flag"] for f in (shared or task_flags)})
            if e_qual not in types:
                e_disputes.append(f"pm_epoch_qualifier_{e_qual}_vs_epoch_{'/'.join(types)}")
        if shared:
            e_bases.append("shared_issue")
        if e_attr and task_flags:
            e_bases.append("pm_attribution")
        if e_disputes:
            e_status = "disputed"
        elif e_bases:
            e_status = "matched"
        elif body_only:
            e_status = "candidate_body_issue_ref"
        elif task_flags:
            e_status = "task_overlap_only"
        elif scope == "benchmark":
            e_status = "not_applicable_benchmark_scope"
        else:
            e_status = "no_epoch_evidence"
        epoch_ev = {
            "status": e_status,
            "counted": e_status == "matched",
            "direct": e_status == "matched" and "shared_issue" in e_bases,
            "bases": e_bases,
            "attributed_by_pm": e_attr,
            "pm_qualifier": e_qual,
            "shared_issue_flag_ids": [f["flag_id"] for f in shared],
            "body_issue_candidate_flag_ids": [f["flag_id"] for f in body_only],
            "task_flag_ids": [f["flag_id"] for f in task_flags],
            "epoch_error_types": sorted({f["flag"] for f in (shared or task_flags)}),
            "cross_task_issue_refs": [{"issue_number": n, "epoch_task": t} for n, t in cross_task],
            "disputes": e_disputes,
        }

        # ---- Tokenless (first pass) ----
        t_attr = bool(attr.get("tokenless"))
        task_slugs = tok_by_task.get(task, []) if task else []
        explicit: list[str] = []
        consumed = sources_text
        for slug in all_slugs:
            if _slug_in(slug, consumed):
                explicit.append(slug)
                consumed = re.sub(rf"(?<![\w-]){re.escape(slug)}(?![\w-])", " ", consumed)
        explicit.sort()
        t_disputes = [
            f"explicit_slug_{s}_belongs_to_task_{tok[s]['task']}"
            for s in explicit
            if tok[s]["task"] != task
        ]
        explicit_same = [s for s in explicit if tok[s]["task"] == task]
        # A Tokenless prior_work issue is "related prior report", not identity: it
        # pairs a slug only when PM also attributes Tokenless and the issue names
        # exactly one slug on the task; otherwise it yields candidates.
        issue_slug_map: dict[int, list[str]] = {}
        for s in task_slugs:
            for n in tok[s].get("prior_work_issue_numbers") or []:
                issue_slug_map.setdefault(n, []).append(s)
        shared_any = {s for n, v in issue_slug_map.items() if n in concrete_issues for s in v}
        shared_unique = (
            sorted(
                {v[0] for n, v in issue_slug_map.items() if n in concrete_issues and len(v) == 1}
            )
            if t_attr
            else []
        )
        shared_candidates = sorted(shared_any - set(shared_unique))
        work.append(
            {
                "row": row,
                "task": task,
                "scope": scope,
                "attr": attr,
                "concrete_issues": concrete_issues,
                "other_issues": other_issues,
                "epoch": epoch_ev,
                "t_attr": t_attr,
                "task_slugs": task_slugs,
                "explicit": explicit_same,
                "shared_unique": shared_unique,
                "shared_candidates": shared_candidates,
                "t_disputes": t_disputes,
            }
        )

    # ---- Tokenless residual pairing (per task, attribution + uniqueness) ----
    residual_match: dict[str, str] = {}
    claimed_by_task: dict[str, set[str]] = {}
    for task, slugs in tok_by_task.items():
        on_task = [w for w in work if w["task"] == task]
        claimed = {s for w in on_task for s in w["explicit"] + w["shared_unique"]}
        residual_slugs = [s for s in slugs if s not in claimed]
        residual_findings = [
            w
            for w in on_task
            if w["t_attr"] and not w["explicit"] and not w["shared_unique"] and not w["t_disputes"]
        ]
        if len(residual_slugs) == 1 and len(residual_findings) == 1:
            residual_match[residual_findings[0]["row"]["finding_id"]] = residual_slugs[0]
            claimed.add(residual_slugs[0])
        claimed_by_task[task] = claimed

    annotated: list[dict[str, Any]] = []
    for w in work:
        row, task = w["row"], w["task"]
        fid = row["finding_id"]
        bases: list[str] = []
        matched: list[str] = []
        if w["explicit"]:
            bases.append("explicit_slug")
            matched += w["explicit"]
        if w["shared_unique"]:
            bases.append("shared_prior_work_issue")
            matched += w["shared_unique"]
        if fid in residual_match:
            bases.append("pm_attribution_unique_residual")
            matched.append(residual_match[fid])
        matched = sorted(set(matched))
        claimed_on_task = claimed_by_task.get(task, set()) if task else set()
        candidates: list[str] = []
        disputes = list(w["t_disputes"])
        env_item = env_items.get(task) if task else None
        if w["t_attr"] and not matched:
            if w["task_slugs"]:
                unclaimed = set(w["task_slugs"]) - claimed_on_task
                candidates = sorted(
                    (set(w["shared_candidates"]) | unclaimed) or set(w["task_slugs"])
                )
            elif task is None:
                disputes.append("pm_attributes_tokenless_on_benchmark_scope_finding")
            else:
                label = env_item["label_strength"] if env_item else "absent_from_envcheck_tb4_39"
                disputes.append(
                    "pm_attributes_tokenless_but_loaded_tokenless_snapshot_has_no_tb4_finding_for_task"
                    f"(envcheck={label})"
                )
        elif not matched and w["shared_candidates"]:
            candidates = list(w["shared_candidates"])
        if disputes:
            t_status = "disputed"
        elif matched:
            t_status = "matched"
        elif w["t_attr"] and candidates:
            t_status = "attributed_slug_unresolved"
        elif candidates:
            t_status = "candidate_shared_prior_work_issue"
        elif w["task_slugs"]:
            t_status = "task_overlap_only"
        elif w["scope"] == "benchmark":
            t_status = "not_applicable_benchmark_scope"
        else:
            t_status = "no_tokenless_evidence"
        t_direct = t_status == "matched" and any(
            b in bases for b in ("explicit_slug", "shared_prior_work_issue")
        )
        tok_ev = {
            "status": t_status,
            "counted": t_status in ("matched", "attributed_slug_unresolved"),
            "direct": t_direct,
            "bases": bases,
            "attributed_by_pm": w["t_attr"],
            "pm_detail": w["attr"].get("tokenless_detail"),
            "matched_slugs": matched,
            "candidate_slugs": candidates,
            "task_slugs": w["task_slugs"],
            "prior_work_join_available": prior_work_loaded,
            "matched_measured_cases": {s: tok[s].get("cases_with_grader_verdict") for s in matched},
            "matched_prior_work_cites_epoch": sorted(
                s for s in matched if tok[s].get("prior_work_cites_epoch_review")
            ),
            "envcheck_task_label": (
                {
                    "item_id": env_item["item_id"],
                    "label_strength": env_item["label_strength"],
                    "expected_verdict": env_item["expected_verdict"],
                    "pin": env_item["pin"],
                    "pin_commit": env_item["pin_commit"],
                    "epoch_added": env_item["epoch_added"],
                }
                if env_item
                else None
            ),
            "disputes": disputes,
        }

        # ---- PM ----
        attr = w["attr"]
        exec_claim = row.get("execution_claim")
        pm_bases = []
        if attr.get("our_review"):
            pm_bases.append("our_review_attribution")
        if _truthy_claim(exec_claim):
            pm_bases.append("pm_execution_claim")
        pm_ev = {
            "status": "matched" if pm_bases else "curated_only",
            "counted": bool(pm_bases),
            "direct": bool(pm_bases),
            "bases": pm_bases,
            "our_review_detail": attr.get("our_review_detail"),
            "execution_claim": exec_claim,
            "execution_claim_basis": row.get("execution_claim_basis"),
        }

        evid = {"epoch": w["epoch"], "pm": pm_ev, "tokenless": tok_ev}
        attributed = sorted(
            a
            for a, flag in (
                ("pm", bool(attr.get("our_review"))),
                ("tokenless", w["t_attr"]),
                ("epoch", bool(attr.get("epoch"))),
            )
            if flag
        )
        counted = sorted(a for a in AUDITORS if evid[a]["counted"])
        direct = sorted(a for a in AUDITORS if evid[a]["direct"])
        notes = []
        if "tokenless" in counted and tok_ev["matched_prior_work_cites_epoch"]:
            notes.append(
                "tokenless_prior_work_cites_epoch_review: not an independent discovery of Epoch-listed issues"
            )
        if "epoch" in counted:
            notes.append("epoch_flags_are_reviews_of_public_github_issues_not_executions")
        if attr.get("github") and not counted:
            notes.append("github_issue_only: GitHub reports are evidence, not an auditor")
        execution = {
            "pm": "claimed" if _truthy_claim(exec_claim) else (exec_claim or "unknown"),
            "tokenless": (
                "source_cases_with_grader_verdicts"
                if any((tok[s].get("cases_with_grader_verdict") or 0) > 0 for s in matched)
                else ("matched_without_raw_cases" if matched else "none")
            ),
            "epoch": "none_reported",
        }
        out = copy.deepcopy(row)
        out["auditor_evidence"] = {
            "github_issue_numbers_concrete": sorted(w["concrete_issues"]),
            "github_issue_numbers_other": sorted(w["other_issues"]),
            "auditors": evid,
            "attributed_sources": attributed,
            "direct_sources": direct,
            "counted_sources": counted,
            "counted_source_count": len(counted),
            "direct_source_count": len(direct),
            "attributed_source_count": len(attributed),
            "execution_evidence": execution,
            "notes": notes,
        }
        annotated.append(out)

    # ---- Epoch row coverage ----
    epoch_rows = []
    for f in epoch_flags:
        shared_fids = sorted(
            a["finding_id"]
            for a in annotated
            if f["flag_id"] in a["auditor_evidence"]["auditors"]["epoch"]["shared_issue_flag_ids"]
        )
        attr_fids = sorted(
            a["finding_id"]
            for a in annotated
            if a.get("task") == f["task"]
            and a["auditor_evidence"]["auditors"]["epoch"]["attributed_by_pm"]
            and a["finding_id"] not in shared_fids
        )
        body_fids = sorted(
            a["finding_id"]
            for a in annotated
            if f["flag_id"]
            in a["auditor_evidence"]["auditors"]["epoch"]["body_issue_candidate_flag_ids"]
        )
        pm_task_fids = sorted(a["finding_id"] for a in annotated if a.get("task") == f["task"])
        if shared_fids:
            status = "matched_shared_issue"
        elif attr_fids:
            status = "pm_attribution_only"
        elif body_fids:
            status = "candidate_body_issue_ref"
        elif pm_task_fids:
            status = "unmatched_task_has_pm_findings"
        else:
            status = "unmatched_task_absent_from_pm_findings"
        epoch_rows.append(
            {
                **f,
                "pm_status": status,
                "pm_shared_issue_finding_ids": shared_fids,
                "pm_attribution_only_finding_ids": attr_fids,
                "pm_body_ref_candidate_finding_ids": body_fids,
                "pm_task_finding_count": len(pm_task_fids),
                "envcheck_item": (
                    {
                        "item_id": env_items[f["task"]]["item_id"],
                        "label_strength": env_items[f["task"]]["label_strength"],
                        "overlay_issue_number": (env_items[f["task"]]["epoch_overlay"] or {}).get(
                            "issue_number"
                        ),
                    }
                    if f["task"] in env_items
                    else None
                ),
            }
        )

    # ---- Tokenless finding coverage ----
    tokenless_rows = []
    for slug, rec in tok.items():
        m = {
            b: sorted(
                a["finding_id"]
                for a in annotated
                if slug in a["auditor_evidence"]["auditors"]["tokenless"]["matched_slugs"]
                and b in a["auditor_evidence"]["auditors"]["tokenless"]["bases"]
            )
            for b in ("explicit_slug", "shared_prior_work_issue", "pm_attribution_unique_residual")
        }
        matched_any = sorted({x for v in m.values() for x in v})
        cand = sorted(
            a["finding_id"]
            for a in annotated
            if slug in a["auditor_evidence"]["auditors"]["tokenless"]["candidate_slugs"]
        )
        status = "matched" if matched_any else "candidate_only" if cand else "unmatched"
        epoch_task = epoch_by_task.get(rec["task"], [])
        tokenless_rows.append(
            {
                "slug": slug,
                "task": rec["task"],
                "item_id": rec["item_id"],
                "pin": rec["pin"],
                "title": rec.get("title"),
                "prior_work_issue_numbers": rec.get("prior_work_issue_numbers"),
                "prior_work_cites_epoch_review": rec.get("prior_work_cites_epoch_review"),
                "cases_with_grader_verdict": rec.get("cases_with_grader_verdict"),
                "pm_status": status,
                "pm_matches_by_basis": m,
                "pm_matched_finding_ids": matched_any,
                "pm_candidate_finding_ids": cand,
                "epoch_task_flag_ids": [f["flag_id"] for f in epoch_task],
                "epoch_shared_issue_flag_ids": sorted(
                    f["flag_id"]
                    for f in epoch_task
                    if f["issue_number"] in (rec.get("prior_work_issue_numbers") or [])
                ),
                "prior_work_issues_listed_by_epoch_for_other_tasks": sorted(
                    {
                        f"{n}:{t}"
                        for n in rec.get("prior_work_issue_numbers") or []
                        for t in epoch_issue_task.get(n, set())
                        if t != rec["task"]
                    }
                ),
            }
        )

    # ---- EnvCheck items and task-level coverage ----
    catalog_tasks = _catalog_task_slugs(catalog)
    pm_by_task: dict[str, list[dict[str, Any]]] = {}
    for a in annotated:
        if a.get("task"):
            pm_by_task.setdefault(a["task"], []).append(a)
    catalog_task_rows = {
        t["task"]: t for t in catalog.get("tasks") or [] if isinstance(t, dict) and t.get("task")
    }
    universe = sorted(set(catalog_tasks) | set(epoch_by_task) | set(env_items))
    tasks_out = []
    for task in universe:
        pm_rows = pm_by_task.get(task, [])
        item = env_items.get(task)
        level = sorted(
            a
            for a, flag in (
                ("pm", any(r["auditor_evidence"]["auditors"]["pm"]["counted"] for r in pm_rows)),
                ("epoch", task in epoch_by_task),
                ("tokenless", bool(tok_by_task.get(task))),
            )
            if flag
        )
        tasks_out.append(
            {
                "task": task,
                "in_pm_catalog": task in catalog_tasks,
                "pm_finding_ids": sorted(r["finding_id"] for r in pm_rows),
                "pm_none_text": (catalog_task_rows.get(task) or {}).get("none_text"),
                "pm_findings_with_counted_pm": sum(
                    r["auditor_evidence"]["auditors"]["pm"]["counted"] for r in pm_rows
                ),
                "epoch_flag_ids": [f["flag_id"] for f in epoch_by_task.get(task, [])],
                "tokenless_slugs": tok_by_task.get(task, []),
                "envcheck": (
                    {
                        "item_id": item["item_id"],
                        "label_strength": item["label_strength"],
                        "expected_verdict": item["expected_verdict"],
                        "pin": item["pin"],
                        "pin_commit": item["pin_commit"],
                        "epoch_added": item["epoch_added"],
                        "cleanliness": item["cleanliness"],
                    }
                    if item
                    else None
                ),
                "task_level_sources": level,
                "task_level_source_count": len(level),
                "findings_by_counted_source_count": _dist(
                    [r["auditor_evidence"]["counted_source_count"] for r in pm_rows]
                ),
            }
        )

    envcheck_rows = []
    for item in envcheck["items"]:
        pm_rows = pm_by_task.get(item["task"], [])
        recon = None
        if item["label_strength"] == "weak_negative":
            positive = sorted(
                r["finding_id"]
                for r in pm_rows
                if r["auditor_evidence"]["auditors"]["pm"]["counted"]
            )
            cited_only = sorted(
                r["finding_id"]
                for r in pm_rows
                if not r["auditor_evidence"]["auditors"]["pm"]["counted"]
            )
            if positive:
                status = "superseded_by_positive_pm_evidence"
            elif cited_only:
                status = "pm_catalog_lists_only_github_cited_findings"
            else:
                status = "unreconciled_no_pm_finding"
            recon = {
                "status": status,
                "historical_label": {
                    "label_strength": item["label_strength"],
                    "expected_verdict": item["expected_verdict"],
                    "label_source": item["source_row"].get("label_source"),
                    "pin": item["pin"],
                    "pin_commit": item["pin_commit"],
                    "task_digest": item["task_digest"],
                },
                "pm_positive_finding_ids": positive,
                "pm_github_cited_only_finding_ids": cited_only,
                "reading": (
                    "The historical EnvCheck non-finding (no published Tokenless finding at this pin) is "
                    "retained as raw provenance and superseded by positive PM evidence on the task. It "
                    "is not a certified-clean label, so this is not a clean-vs-defect contradiction, "
                    "and it is not EnvCheck/Tokenless corroboration of the PM findings."
                    if positive
                    else "Weak negative retained as raw provenance; it does not establish cleanliness."
                ),
                "envcheck_corroborates_pm": False,
                "contradiction_with_certified_clean": False,
            }
        envcheck_rows.append(
            {
                "item_id": item["item_id"],
                "task": item["task"],
                "label_strength": item["label_strength"],
                "expected_verdict": item["expected_verdict"],
                "pin": item["pin"],
                "epoch_added": item["epoch_added"],
                "in_pm_catalog": item["task"] in catalog_tasks,
                "pm_finding_count": len(pm_rows),
                "pm_none_text": (catalog_task_rows.get(item["task"]) or {}).get("none_text"),
                "pm_tokenless_attributed_finding_ids": sorted(
                    r["finding_id"]
                    for r in pm_rows
                    if r["auditor_evidence"]["auditors"]["tokenless"]["attributed_by_pm"]
                ),
                "epoch_flag_ids": [f["flag_id"] for f in epoch_by_task.get(item["task"], [])],
                "weak_negative_reconciliation": recon,
            }
        )
        if recon is not None:
            for r in pm_rows:
                label = r["auditor_evidence"]["auditors"]["tokenless"]["envcheck_task_label"]
                if label is not None:
                    label["weak_negative_reconciliation"] = recon["status"]
    for t in tasks_out:
        rec = next((r for r in envcheck_rows if r["task"] == t["task"]), None)
        if t["envcheck"] is not None and rec is not None:
            t["envcheck"]["weak_negative_reconciliation"] = (
                rec["weak_negative_reconciliation"] or {}
            ).get("status")

    # ---- Agreement arithmetic ----
    def by_scope(scope: str) -> list[dict[str, Any]]:
        return [
            a
            for a in annotated
            if (a.get("scope") or ("task" if a.get("task") else "benchmark")) == scope
        ]

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        ae = [r["auditor_evidence"] for r in rows]
        return {
            "findings": len(rows),
            "counted_source_count": _dist([e["counted_source_count"] for e in ae]),
            "counted_source_combinations": _combo_counts([e["counted_sources"] for e in ae]),
            "attributed_source_count": _dist([e["attributed_source_count"] for e in ae]),
            "attributed_source_combinations": _combo_counts([e["attributed_sources"] for e in ae]),
            "direct_source_count": _dist([e["direct_source_count"] for e in ae]),
            "direct_source_combinations": _combo_counts([e["direct_sources"] for e in ae]),
            "per_auditor": {
                a: {
                    "counted": sum(e["auditors"][a]["counted"] for e in ae),
                    "direct": sum(e["auditors"][a]["direct"] for e in ae),
                    "status": dict(sorted(Counter(e["auditors"][a]["status"] for e in ae).items())),
                    "bases": dict(
                        sorted(Counter(b for e in ae for b in e["auditors"][a]["bases"]).items())
                    ),
                }
                for a in AUDITORS
            },
        }

    overall = summary(annotated)
    agreement = {
        "definitions": {
            "attributed_sources": "auditors PM's own attribution text names (provenance as cited by PM)",
            "direct_sources": (
                "auditors whose own published data identifies this finding: PM own review/execution "
                "claim; Epoch row with the same issue on the same task; Tokenless slug named by PM or "
                "(with PM's Tokenless attribution) a Tokenless prior_work issue cited by the finding "
                "that maps to exactly one slug on the task"
            ),
            "counted_sources": (
                "direct sources plus undisputed PM attributions (Epoch: task has an Epoch row; "
                "Tokenless: task has Tokenless findings, slug paired uniquely or left unresolved)"
            ),
            "excluded": (
                "task-level overlap without attribution or shared identity, disputed attributions, "
                "GitHub-only citations, EnvCheck weak negatives, EnvCheck epoch_flagged overlays "
                "(they restate Epoch rows)"
            ),
        },
        "overall": overall,
        "by_scope": {s: summary(by_scope(s)) for s in ("task", "benchmark")},
        "epoch_rows": {
            "rows": len(epoch_rows),
            "status": dict(sorted(Counter(r["pm_status"] for r in epoch_rows).items())),
        },
        "tokenless_findings": {
            "slugs": len(tokenless_rows),
            "status": dict(sorted(Counter(r["pm_status"] for r in tokenless_rows).items())),
            "with_epoch_shared_issue": sum(
                bool(r["epoch_shared_issue_flag_ids"]) for r in tokenless_rows
            ),
            "prior_work_cites_epoch_review": sum(
                bool(r["prior_work_cites_epoch_review"]) for r in tokenless_rows
            ),
        },
        "envcheck_weak_negatives": {
            "items": sum(r["weak_negative_reconciliation"] is not None for r in envcheck_rows),
            "reconciliation": dict(
                sorted(
                    Counter(
                        r["weak_negative_reconciliation"]["status"]
                        for r in envcheck_rows
                        if r["weak_negative_reconciliation"] is not None
                    ).items()
                )
            ),
        },
        "task_level_overlap": {
            "note": "task-level co-flagging is not a finding-level match",
            "tasks": len(tasks_out),
            "source_count": _dist([t["task_level_source_count"] for t in tasks_out]),
            "source_combinations": _combo_counts([t["task_level_sources"] for t in tasks_out]),
        },
    }
    checks = {
        "every_finding_retained": len(annotated) == len(findings_in),
        "counted_distribution_sums": sum(overall["counted_source_count"].values())
        == len(annotated),
        "combination_sums": sum(overall["counted_source_combinations"].values()) == len(annotated),
        "scope_partition": sum(agreement["by_scope"][s]["findings"] for s in ("task", "benchmark"))
        == len(annotated),
        "every_epoch_row_retained": len(epoch_rows) == len(epoch_flags),
        "every_tokenless_slug_retained": len(tokenless_rows) == len(tok),
        "every_envcheck_item_retained": len(envcheck_rows) == len(envcheck["items"]),
        "direct_subset_of_counted": all(
            set(a["auditor_evidence"]["direct_sources"])
            <= set(a["auditor_evidence"]["counted_sources"])
            for a in annotated
        ),
    }
    if not all(checks.values()):
        raise ValueError(f"agreement arithmetic check failed: {checks}")

    unresolved = {
        "epoch_disputed": [
            {
                "finding_id": a["finding_id"],
                "task": a.get("task"),
                "disputes": a["auditor_evidence"]["auditors"]["epoch"]["disputes"],
            }
            for a in annotated
            if a["auditor_evidence"]["auditors"]["epoch"]["status"] == "disputed"
        ],
        "tokenless_disputed": [
            {
                "finding_id": a["finding_id"],
                "task": a.get("task"),
                "disputes": a["auditor_evidence"]["auditors"]["tokenless"]["disputes"],
            }
            for a in annotated
            if a["auditor_evidence"]["auditors"]["tokenless"]["status"] == "disputed"
        ],
        "tokenless_slug_unresolved": [
            {
                "finding_id": a["finding_id"],
                "task": a.get("task"),
                "candidate_slugs": a["auditor_evidence"]["auditors"]["tokenless"][
                    "candidate_slugs"
                ],
                "status": a["auditor_evidence"]["auditors"]["tokenless"]["status"],
            }
            for a in annotated
            if a["auditor_evidence"]["auditors"]["tokenless"]["candidate_slugs"]
        ],
        "tokenless_inferred_residual_pairs": [
            {"finding_id": f, "slug": s} for f, s in sorted(residual_match.items())
        ],
        "epoch_rows_without_shared_issue_match": [
            {"flag_id": r["flag_id"], "pm_status": r["pm_status"]}
            for r in epoch_rows
            if r["pm_status"] != "matched_shared_issue"
        ],
        "tokenless_slugs_without_match": [
            {"slug": r["slug"], "task": r["task"], "pm_status": r["pm_status"]}
            for r in tokenless_rows
            if r["pm_status"] != "matched"
        ],
        "epoch_tasks_absent_from_pm_catalog": sorted(set(epoch_by_task) - set(catalog_tasks)),
        "envcheck_tasks_absent_from_pm_catalog": sorted(set(env_items) - set(catalog_tasks)),
        "epoch_live_vs_envcheck_overlay_issue_mismatch": [
            {
                "task": t,
                "epoch_issue_numbers": sorted(
                    {f["issue_number"] for f in epoch_by_task.get(t, [])}
                ),
                "overlay_issue_number": i["epoch_overlay"]["issue_number"],
            }
            for t, i in env_items.items()
            if i["epoch_overlay"]
            and i["epoch_overlay"]["issue_number"]
            not in {f["issue_number"] for f in epoch_by_task.get(t, [])}
        ],
        "envcheck_confirmed_tasks_without_epoch_row": sorted(
            t
            for t, i in env_items.items()
            if i["label_strength"] == "confirmed" and t not in epoch_by_task
        ),
        "auditor_flagged_tasks_with_zero_pm_findings": [
            {
                "task": t["task"],
                "pm_none_text": t["pm_none_text"],
                "epoch_flag_ids": t["epoch_flag_ids"],
                "tokenless_slugs": t["tokenless_slugs"],
                "envcheck_label_strength": (t["envcheck"] or {}).get("label_strength"),
            }
            for t in tasks_out
            if not t["pm_finding_ids"] and (t["epoch_flag_ids"] or t["tokenless_slugs"])
        ],
    }

    return {
        "schema": "tb4-audit-auditor-annotations/v1",
        "inputs": {
            "pm_catalog": {
                "schema": catalog.get("schema"),
                "source": copy.deepcopy(catalog.get("source")),
                "findings": len(findings_in),
            },
            "epoch": copy.deepcopy(epoch["source"]),
            "envcheck": copy.deepcopy(envcheck["source"]),
        },
        "findings": annotated,
        "epoch_rows": epoch_rows,
        "tokenless_findings": tokenless_rows,
        "envcheck_items": envcheck_rows,
        "tasks": tasks_out,
        "agreement": agreement,
        "checks": checks,
        "unresolved": unresolved,
    }
