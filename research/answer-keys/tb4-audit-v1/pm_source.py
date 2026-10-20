#!/usr/bin/env python3
"""Pure parsers for the Preference Model Terminal Bench 4 finding catalog.

Sources (fetched once by the key builder, parsed offline here):

* ``PM_ISSUES_URL``: "Terminal Bench 4: Every Issue We Found", one
  ``<details class="tb-finding">`` per finding inside one
  ``<section class="tb-task">`` per task plus a ``benchmark-wide`` section.
* ``PM_OVERVIEW_URL``: the companion post, used only to reconcile its
  task-level claims against the parsed catalog.
* ``PORT_REPO_URL``: the Karotte port, read as files (AST/text only). No
  task, test or grader code is imported or executed.

Count structure of the catalog (checked by ``parse_pm_catalog``, not assumed):
the page states one total (intro sentence and the "All" filter) and one counter
per type filter. A finding may carry more than one type chip, so the type
counters sum to the number of (finding, type) pairs, not the number of
findings. Benchmark-wide findings are findings with ``task=None`` and
``scope="benchmark"``; they are part of the stated total.

Every value here is a claim made by Preference Model, with its literal source
text. Nothing in this module measures or replays a finding.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCHEMA = "pm-tb4-catalog/v1"
PM_ISSUES_URL = "https://preferencemodel.com/blog/terminal-bench-4-is-saturated/issues/"
PM_OVERVIEW_URL = "https://preferencemodel.com/blog/terminal-bench-4-is-saturated/"
PORT_REPO_URL = "https://github.com/preferencemodel/terminal-bench-4-karotte-port"
HARBOR_ISSUES_URL = "https://github.com/harbor-framework/terminal-bench/issues"
BENCHMARK_SECTION_ID = "benchmark-wide"

TYPE_CHIPS = {
    "Reward hack": "reward_hack",
    "Ambiguity": "ambiguity",
    "Grading or isolation": "grading_or_isolation",
}
UNTESTED_SUFFIX = " (untested)"
FILTER_LABELS = {
    "All": "all",
    "Reward hacks": "reward_hack",
    "Ambiguities": "ambiguity",
    "Grading and isolation": "grading_or_isolation",
}
STATUS_CLASSES = {
    "is-fixed": "fixed",
    "is-partial": "partial",
    "is-open": "open",
    "is-wontfix": "wontfix",
    "is-na": "na",
}
LEGEND_STATUS_WORDS = {
    "Fixed": "fixed",
    "Partial": "partial",
    "Open": "open",
    "Won't fix": "wontfix",
}
SOURCE_ITEMS = {
    "Our review": "our_review",
    "Tokenless": "tokenless",
    "Epoch": "epoch",
    "GitHub": "github",
}

EXECUTION_CLAIM_VALUES = ("true", "false", "unknown")
MAPPING_STATUSES = (
    "explicit",
    "candidate_only",
    "not_applicable",
    "no_source_match",
    "port_metadata_not_attached",
)

_ISSUE_HREF = re.compile(r"^https://github\.com/harbor-framework/terminal-bench/issues/(\d+)$")
_HARBOR_TRIAL_HREF = re.compile(
    r"^https://hub\.harborframework\.com/jobs/(?P<job>[0-9a-f-]{36})(?:/trials/(?P<trial>[0-9a-f-]{36}))?(?:[/?#].*)?$"
)
_RATE = re.compile(r"^(?P<model>.+?) (?P<passed>\d+)/(?P<total>\d+)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(*`])|\n+")
_RESULT_WORDS = re.compile(
    r"\breward (?:of )?[01](?:\.0)?(?!\.?\d)|\bscored? \d+/\d+|\bscores? [01](?:\.0)?(?!\.?\d)|\bgot reward\b"
    r"|\b\d+/\d+ pass|\bpass(?:ed|es)? \d+ of \d+|\bpassed \d+/\d+",
    re.I,
)
# A non-hack finding counts as claiming an upstream execution only when one sentence both reports a
# reward/score and names upstream's (or Harbor's) verifier/grader as where it was obtained, and does not
# mention the port (port-side results are not upstream executions).
_UPSTREAM_EXECUTION = re.compile(
    r"\breward (?:of )?[01](?:\.0)?(?!\.?\d)|\bscored? \d+/\d+|\bscores? [01](?:\.0)?(?!\.?\d)|\bgot reward\b",
    re.I,
)
_UPSTREAM_GRADER_PLACE = re.compile(
    r"\bon (?:the )?(?:unmodified )?upstream(?:'s)? (?:4\.0 )?(?:verifier|grader)\b|\bon Harbor's (?:own )?(?:grader|verifier)\b",
    re.I,
)
_PORT_WORD = re.compile(r"\bport(?:'s)?\b", re.I)
_PR_BEFORE_ANCHOR = re.compile(r"\bPRs? (?:#\d+, )*$")
_COMMENT_ON_PR_BEFORE_ANCHOR = re.compile(r"\bcomment on (?:Harbor )?PR $")
_SLUG_LABEL = re.compile(r"(?<![\w#/.-])[a-z0-9]+(?:-[a-z0-9]+){2,}(?![\w-])")
_TEST_TOKEN = re.compile(
    r"(?<![\w/.*])(?:"
    r"(?P<path>tests/[\w./-]+?\.py)(?:::(?P<node>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)(?P<nparam>\[[^\]\s]*\])?)?"
    r"|(?P<wild>\*_\w+)"
    r"|(?P<name>test_\w+)(?:(?P<ext>\.py)|(?P<param>\[[^\]\s]*\]))?"
    r")"
)
_PORT_STATUS_PREFIX = re.compile(
    r"^(?P<word>Fixed|Partial|Open|Won't fix|N/A)\b(?: \((?P<qualifier>[^)]*)\))?"
)


class PMSourceError(ValueError):
    """Raised when the source bytes do not have the structure this parser reads."""


# --------------------------------------------------------------------------- HTML tree


_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    ]
)


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(self.attrs.get("class", "").split())

    def elements(self) -> Iterator[_Node]:
        return (c for c in self.children if isinstance(c, _Node))

    def iter(self) -> Iterator[_Node]:
        for child in self.elements():
            yield child
            yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[_Node]:
        return [
            n
            for n in self.iter()
            if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
        ]

    def children_with(self, tag: str | None = None, cls: str | None = None) -> list[_Node]:
        return [
            n
            for n in self.elements()
            if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
        ]

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self._cur)
        self._cur.children.append(node)
        if tag not in _VOID:
            self._cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._cur.children.append(_Node(tag, {k: (v or "") for k, v in attrs}, self._cur))

    def handle_endtag(self, tag: str) -> None:
        node: _Node | None = self._cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is None or node.parent is None:
            return  # stray end tag
        self._cur = node.parent

    def handle_data(self, data: str) -> None:
        self._cur.children.append(data)


def _parse_tree(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _one(nodes: list[_Node], what: str) -> _Node:
    if len(nodes) != 1:
        raise PMSourceError(f"expected exactly one {what}, found {len(nodes)}")
    return nodes[0]


def _anchors_in_order(node: _Node) -> list[tuple[str, _Node]]:
    """(text before the anchor inside ``node``, anchor) pairs in document order."""
    out: list[tuple[str, _Node]] = []
    buf: list[str] = []

    def walk(n: _Node) -> None:
        for child in n.children:
            if isinstance(child, str):
                buf.append(child)
            elif child.tag == "a":
                out.append(("".join(buf), child))
                buf.append(child.text())
            else:
                walk(child)

    walk(node)
    return out


# --------------------------------------------------------------------------- helpers


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _sentence_containing(text: str, start: int) -> str:
    pos = 0
    for part in _SENTENCE_SPLIT.split(text):
        idx = text.find(part, pos)
        if idx <= start < idx + len(part):
            return part.strip()
        pos = idx + len(part)
    return text.strip()


def _split_top_level_commas(text: str) -> list[str]:
    items, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur).strip())
    if depth != 0:
        raise PMSourceError(f"unbalanced parentheses in source attribution {text!r}")
    return [i for i in items if i]


def _github_refs(node: _Node, where: str) -> list[dict[str, Any]]:
    refs = []
    for before, a in _anchors_in_order(node):
        href = a.attrs.get("href", "")
        m = _ISSUE_HREF.match(href)
        if not m:
            continue
        if _COMMENT_ON_PR_BEFORE_ANCHOR.search(before):
            kind = "comment_on_pr"
        elif _PR_BEFORE_ANCHOR.search(before):
            kind = "pr"
        else:
            kind = "issue"
        refs.append(
            {
                "number": int(m.group(1)),
                "url": href,
                "anchor_text": a.text(),
                "where": where,
                "kind": kind,
            }
        )
    return refs


def _harbor_links(node: _Node) -> list[dict[str, Any]]:
    out = []
    for _, a in _anchors_in_order(node):
        href = a.attrs.get("href", "")
        m = _HARBOR_TRIAL_HREF.match(href)
        if m:
            out.append(
                {
                    "url": href,
                    "job_id": m.group("job"),
                    "trial_id": m.group("trial"),
                    "anchor_text": a.text(),
                }
            )
    return out


def _block_text(nodes: Iterable[_Node]) -> str:
    lines: list[str] = []
    for n in nodes:
        if n.tag in ("ul", "ol"):
            lines.extend(li.text() for li in n.children_with("li"))
        else:
            lines.append(n.text())
    return "\n".join(lines)


def _stable_id(section_id: str, title: str) -> str:
    return f"pm-tb4:{section_id}:{_sha256(f'{section_id}\0{title}'.encode())[:10]}"


# --------------------------------------------------------------------------- source attribution


def _parse_sources(span_text: str) -> dict[str, Any]:
    m = re.match(r"^(Sources?): (.+)$", span_text, re.S)
    if not m:
        raise PMSourceError(f"unexpected source attribution {span_text!r}")
    items = []
    flags: dict[str, Any] = {
        "our_review": False,
        "our_review_detail": None,
        "tokenless": False,
        "tokenless_detail": None,
        "epoch": False,
        "epoch_flag": None,
        "epoch_detail": None,
        "github": False,
        "github_detail": None,
    }
    for raw in _split_top_level_commas(m.group(2)):
        im = re.match(r"^(Our review|Tokenless|Epoch|GitHub)(?: \((.*)\))?$", raw, re.S)
        if not im:
            raise PMSourceError(f"unknown source item {raw!r} in {span_text!r}")
        key = SOURCE_ITEMS[im.group(1)]
        detail = im.group(2)
        if flags[key]:
            raise PMSourceError(f"source {key} repeated in {span_text!r}")
        flags[key] = True
        if key == "epoch":
            if detail in ("FP", "FN"):
                flags["epoch_flag"] = detail
            else:
                flags["epoch_detail"] = detail
        else:
            flags[f"{key}_detail"] = detail
        items.append({"source": key, "text": raw})
    if (m.group(1) == "Source") != (len(items) == 1):
        raise PMSourceError(f"singular/plural label disagrees with item count in {span_text!r}")
    flags["source_items"] = items
    flags["source_count"] = len(items)
    flags["slug_labels"] = _SLUG_LABEL.findall(m.group(2))
    return flags


# --------------------------------------------------------------------------- execution claim


def _execution_claim(
    type_chips: list[str],
    pm_types: list[str],
    untested: bool,
    regions: dict[str, str],
    legend_hack: str,
) -> dict[str, Any]:
    evidence = []
    upstream_results = []
    for region in ("port", "body"):
        for s in _sentences(regions.get(region) or ""):
            if _RESULT_WORDS.search(s):
                evidence.append(s)
                if (
                    _UPSTREAM_EXECUTION.search(s)
                    and _UPSTREAM_GRADER_PLACE.search(s)
                    and not _PORT_WORD.search(s)
                ):
                    upstream_results.append(s)
    if "reward_hack" in pm_types and not untested:
        claim, basis, text = "true", "pm_legend_reward_hack", legend_hack
    elif untested:
        chip = next(c for c in type_chips if c.endswith(UNTESTED_SUFFIX))
        claim, basis, text = "false", "pm_chip_untested", chip
    elif upstream_results:
        claim, basis, text = "true", "finding_text_upstream_result", upstream_results[0]
    else:
        claim, basis, text = "unknown", "no_execution_claim_in_source", None
    return {
        "execution_claim": claim,
        "execution_claim_basis": basis,
        "execution_claim_text": text,
        "execution_evidence_sentences": evidence,
        "execution_upstream_result_sentences": upstream_results,
    }


# --------------------------------------------------------------------------- test mentions


def _test_mentions(regions: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for region, text in regions.items():
        if not text:
            continue
        for m in _TEST_TOKEN.finditer(text):
            sentence = _sentence_containing(text, m.start())
            if m.group("path"):
                kind = "node_id" if m.group("node") else "file"
                name = m.group("node").split("::")[-1] if m.group("node") else None
                param = m.group("nparam")
                path = m.group("path")
            elif m.group("wild"):
                kind, name, param, path = "wildcard", m.group("wild"), None, None
            elif m.group("ext"):
                kind, name, param, path = "file", None, None, m.group("name") + ".py"
            else:
                kind, name, param, path = "function", m.group("name"), m.group("param"), None
            out.append(
                {
                    "token": m.group(0),
                    "kind": kind,
                    "name": name,
                    "param": param,
                    "path": path,
                    "region": region,
                    "context": sentence,
                    "context_mentions_upstream": bool(re.search(r"\bupstream", sentence, re.I)),
                }
            )
    return out


# --------------------------------------------------------------------------- catalog


def _parse_finding(
    det: _Node, section_id: str, page_ordinal: int, ordinal: int, legend_hack: str
) -> dict[str, Any]:
    summary = _one(det.children_with("summary"), f"<summary> in finding {section_id}#{ordinal}")
    title = _one(summary.find_all("span", "tb-finding-title"), "finding title").text()
    chips_box = _one(summary.find_all("span", "tb-chips"), f"chips of {title!r}")
    type_chips, status = [], None
    for chip in chips_box.children_with("span", "tb-chip"):
        if "tb-status" in chip.classes:
            cls = [c for c in chip.classes if c in STATUS_CLASSES]
            if status is not None or len(cls) != 1:
                raise PMSourceError(f"status chip ambiguous for {title!r}")
            status = (STATUS_CLASSES[cls[0]], chip.text())
        else:
            type_chips.append(chip.text())
    if status is None or not type_chips:
        raise PMSourceError(f"finding {title!r} lacks a type or status chip")
    pm_types, untested = [], False
    for chip in type_chips:
        base = chip[: -len(UNTESTED_SUFFIX)] if chip.endswith(UNTESTED_SUFFIX) else chip
        if base not in TYPE_CHIPS:
            raise PMSourceError(f"unknown type chip {chip!r} on {title!r}")
        if base != chip:
            if base != "Reward hack":
                raise PMSourceError(f"untested marker on non-hack chip {chip!r}")
            untested = True
        pm_types.append(TYPE_CHIPS[base])

    body = _one(det.children_with("div", "tb-finding-body"), f"body of {title!r}")
    blocks = list(body.elements())
    if (
        len(blocks) < 2
        or "tb-sources" not in blocks[0].classes
        or "tb-port" not in blocks[1].classes
    ):
        raise PMSourceError(f"finding {title!r} body does not start with tb-sources, tb-port")
    src_spans = blocks[0].children_with("span")
    if not src_spans or len(src_spans) > 2:
        raise PMSourceError(f"finding {title!r} has {len(src_spans)} source spans")
    sources_text = src_spans[0].text()
    upstream_text = src_spans[1].text() if len(src_spans) == 2 else None
    if upstream_text is not None and not upstream_text.startswith("Upstream: "):
        raise PMSourceError(f"second source span is not Upstream: {upstream_text!r}")
    attribution = _parse_sources(sources_text)

    port_spans = blocks[1].children_with("span")
    if len(port_spans) != 2 or port_spans[0].text() != "Our port":
        raise PMSourceError(f"finding {title!r} port paragraph is not [Our port, text]")
    port_text = port_spans[1].text()
    pm = _PORT_STATUS_PREFIX.match(port_text)
    extra = blocks[2:]
    body_text = _block_text(extra) if extra else None

    github_refs = _github_refs(src_spans[0], "sources")
    if len(src_spans) == 2:
        github_refs += _github_refs(src_spans[1], "upstream")
    github_refs += _github_refs(port_spans[1], "port")
    for block in extra:
        github_refs += _github_refs(block, "body")
    harbor = _harbor_links(port_spans[1])
    for block in extra:
        harbor += _harbor_links(block)

    regions = {"title": title, "port": port_text, "body": body_text}
    task = None if section_id == BENCHMARK_SECTION_ID else section_id
    row: dict[str, Any] = {
        "finding_id": _stable_id(section_id, title),
        "page_ordinal": page_ordinal,
        "ordinal_in_section": ordinal,
        "section_id": section_id,
        "task": task,
        "scope": "benchmark" if task is None else "task",
        "title": title,
        "pm_type_chips": type_chips,
        "pm_types": pm_types,
        "primary_type": pm_types[0],
        "data_cats": det.attrs.get("data-cats", "").split(),
        "port_status": status[0],
        "port_status_label": status[1],
        "port_text": port_text,
        "port_text_status_word": pm.group("word") if pm else None,
        "port_text_qualifier": pm.group("qualifier") if pm else None,
        "body_text": body_text,
        "sources_text": sources_text,
        "upstream_text": upstream_text,
        "attribution": attribution,
        "github_refs": github_refs,
        "github_issue_numbers": sorted(
            {r["number"] for r in github_refs if r["where"] == "sources"}
        ),
        "github_all_numbers": sorted({r["number"] for r in github_refs}),
        "harbor_trial_urls": [h["url"] for h in harbor],
        "harbor_links": harbor,
        **_execution_claim(type_chips, pm_types, untested, regions, legend_hack),
        "port_test_mentions": _test_mentions(regions),
        "port_test_mapping_status": "port_metadata_not_attached",
        "port_test_mapping_reason": "attach_port_metadata has not been applied",
        "port_tests_explicit": [],
        "port_test_candidates": [],
        "port_test_unresolved_mentions": [],
    }
    return row


def _rates(section: _Node) -> list[dict[str, Any]]:
    out = []
    for rates in section.children_with("p", "tb-rates"):
        for span in rates.find_all("span", "tb-rate"):
            m = _RATE.match(span.text())
            if not m:
                raise PMSourceError(f"unparsed Harbor rate {span.text()!r}")
            out.append(
                {
                    "model": m.group("model"),
                    "passed": int(m.group("passed")),
                    "total": int(m.group("total")),
                }
            )
    return out


def parse_pm_catalog(
    html: str,
    *,
    source_url: str = PM_ISSUES_URL,
    retrieved_at: str | None = None,
    overview_html: str | None = None,
    overview_url: str = PM_OVERVIEW_URL,
) -> dict[str, Any]:
    """Parse the PM issues page into a finding catalog. Pure: no I/O, no clock."""
    root = _parse_tree(html)
    article = _one(root.find_all("article"), "<article>")
    title_node = _one(article.find_all("h1"), "<h1>")
    time_nodes = article.find_all("time")
    intro = _one(article.find_all("div", "blog-body-html"), "intro block")
    intro_text = intro.text()
    im = re.search(
        r"review of the (\d+) Terminal Bench 4 tasks we ported to Karotte: (\d+) in all", intro_text
    )
    if not im:
        raise PMSourceError("intro sentence with reviewed-task and finding totals not found")

    legend_node = _one(article.find_all("dl", "tb-legend"), "legend")
    dts, dds = legend_node.children_with("dt"), legend_node.children_with("dd")
    legend = {dt.text(): dd.text() for dt, dd in zip(dts, dds, strict=True)}
    hack_legend = legend.get("Reward hack", "")
    hm = re.search(
        r"We tried it on Harbor's own grader and it passed, unless it is marked untested\.",
        hack_legend,
    )
    if not hm:
        raise PMSourceError("reward-hack legend no longer states the tested/untested rule")
    legend_hack_sentence = hm.group(0)
    legend_statuses = re.findall(r"(Fixed|Partial|Open|Won't fix):", legend.get("Status", ""))

    filter_counts: dict[str, int] = {}
    for button in _one(article.find_all("div", "tb-filter"), "filter bar").children_with("button"):
        count = _one(button.find_all("span", "tb-filter-count"), "filter count").text()
        label = "".join(c for c in button.children if isinstance(c, str))
        if label not in FILTER_LABELS:
            raise PMSourceError(f"unknown filter {label!r}")
        filter_counts[FILTER_LABELS[label]] = int(count)

    nav = _one(article.find_all("nav", "tb-index"), "task index")
    index = []
    for a in nav.children_with("a"):
        count_span = _one(a.children_with("span"), "index count")
        label = "".join(c for c in a.children if isinstance(c, str))
        index.append(
            {
                "section_id": a.attrs["href"].removeprefix("#"),
                "label": label,
                "count": int(count_span.text()),
                "data_cats": a.attrs.get("data-cats", "").split(),
            }
        )
    index_by_id = {i["section_id"]: i for i in index}

    findings: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for section in article.find_all("section", "tb-task"):
        sid = section.attrs["id"]
        h3 = _one(section.children_with("h3"), f"heading of {sid}")
        h3_links = h3.children_with("a")
        summary = _one(section.children_with("div", "tb-summary"), f"summary of {sid}")
        none_nodes = summary.children_with("p", "tb-none")
        summary_paras = [p for p in summary.children_with("p") if "tb-none" not in p.classes]
        dets = section.children_with("details", "tb-finding")
        ids = []
        for n, det in enumerate(dets, start=1):
            row = _parse_finding(det, sid, len(findings) + 1, n, legend_hack_sentence)
            findings.append(row)
            ids.append(row["finding_id"])
        task = None if sid == BENCHMARK_SECTION_ID else sid
        summary_refs = []
        for p in summary_paras:
            summary_refs += _github_refs(p, "summary")
        tasks.append(
            {
                "section_id": sid,
                "task": task,
                "scope": "benchmark" if task is None else "task",
                "display_name": h3.text(),
                "hub_task_url": h3_links[0].attrs["href"] if h3_links else None,
                "harbor_rates": _rates(section),
                "summary_text": "\n".join(p.text() for p in summary_paras) or None,
                "none_text": none_nodes[0].text() if none_nodes else None,
                "index_count": index_by_id[sid]["count"] if sid in index_by_id else None,
                "index_data_cats": index_by_id[sid]["data_cats"] if sid in index_by_id else None,
                "section_data_cats": section.attrs.get("data-cats", "").split(),
                "finding_count": len(dets),
                "finding_ids": ids,
                "summary_harbor_urls": [h["url"] for p in summary_paras for h in _harbor_links(p)],
                "summary_github_refs": summary_refs,
            }
        )

    # Disambiguate only true same-section title collisions, by page order.
    seen: dict[str, int] = {}
    for row in findings:
        k = seen.get(row["finding_id"], 0) + 1
        seen[row["finding_id"]] = k
        if k > 1:
            row["finding_id"] = f"{row['finding_id']}-{k}"
    if len({r["finding_id"] for r in findings}) != len(findings):
        raise PMSourceError("finding_id collision after disambiguation")

    catalog: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {
            "url": source_url,
            "sha256": _sha256(html.encode("utf-8")),
            "bytes": len(html.encode("utf-8")),
            "retrieved_at": retrieved_at,
            "page_title": title_node.text(),
            "checked_date": time_nodes[0].attrs.get("datetime") if time_nodes else None,
            "intro_text": intro_text,
            "legend": legend,
            "legend_reward_hack_rule": legend_hack_sentence,
        },
        "index": index,
        "tasks": tasks,
        "findings": findings,
    }
    catalog["counts"], catalog["discrepancies"] = _counts_and_discrepancies(
        catalog, int(im.group(1)), int(im.group(2)), im.group(0), filter_counts, legend_statuses
    )
    if overview_html is not None:
        overview = parse_pm_overview(overview_html, source_url=overview_url)
        catalog["overview"] = overview
        catalog["overview_reconciliation"] = reconcile_overview(catalog, overview)
    return catalog


def _counts_and_discrepancies(
    catalog: dict[str, Any],
    stated_reviewed_tasks: int,
    stated_total: int,
    stated_sentence: str,
    filter_counts: dict[str, int],
    legend_statuses: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    findings, tasks, index = catalog["findings"], catalog["tasks"], catalog["index"]
    type_pairs: dict[str, int] = {t: 0 for t in TYPE_CHIPS.values()}
    for f in findings:
        for t in f["pm_types"]:
            type_pairs[t] += 1
    multi = [f for f in findings if len(f["pm_types"]) > 1]

    def tally(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in findings:
            out[str(f[key])] = out.get(str(f[key]), 0) + 1
        return dict(sorted(out.items()))

    attr = {k: sum(1 for f in findings if f["attribution"][k]) for k in SOURCE_ITEMS.values()}
    attr["epoch_FP"] = sum(1 for f in findings if f["attribution"]["epoch_flag"] == "FP")
    attr["epoch_FN"] = sum(1 for f in findings if f["attribution"]["epoch_flag"] == "FN")
    attr["our_review_only"] = sum(
        1
        for f in findings
        if f["attribution"]["source_count"] == 1 and f["attribution"]["our_review"]
    )
    # Same set minus findings whose "Upstream:" span still links GitHub issues/PRs.
    attr["our_review_only_no_github_link_in_sources_paragraph"] = sum(
        1
        for f in findings
        if f["attribution"]["source_count"] == 1
        and f["attribution"]["our_review"]
        and not any(r["where"] in ("sources", "upstream") for r in f["github_refs"])
    )
    task_rows = [t for t in tasks if t["scope"] == "task"]
    counts = {
        "stated_total": stated_total,
        "stated_total_sentence": stated_sentence,
        "stated_reviewed_tasks": stated_reviewed_tasks,
        "filter_counts": filter_counts,
        "filter_type_sum": sum(v for k, v in filter_counts.items() if k != "all"),
        "findings": len(findings),
        "type_chip_pairs": type_pairs,
        "multi_type_findings": len(multi),
        "by_scope": tally("scope"),
        "by_primary_type": tally("primary_type"),
        "by_port_status": tally("port_status"),
        "by_execution_claim": tally("execution_claim"),
        "by_execution_claim_basis": tally("execution_claim_basis"),
        "attribution": attr,
        "findings_with_github_source_issue": sum(1 for f in findings if f["github_issue_numbers"]),
        "unique_github_numbers_sources": len(
            {n for f in findings for n in f["github_issue_numbers"]}
        ),
        "unique_github_numbers_all": len({n for f in findings for n in f["github_all_numbers"]}),
        "findings_with_harbor_trial_urls": sum(1 for f in findings if f["harbor_trial_urls"]),
        "harbor_trial_urls_in_findings": sum(len(f["harbor_trial_urls"]) for f in findings),
        "findings_with_test_mentions": sum(1 for f in findings if f["port_test_mentions"]),
        "sections": len(tasks),
        "task_sections": len(task_rows),
        "task_sections_with_findings": sum(1 for t in task_rows if t["finding_count"]),
        "task_sections_without_findings": [
            t["section_id"] for t in task_rows if not t["finding_count"]
        ],
        "task_sections_not_reviewed": [
            t["section_id"] for t in task_rows if t["none_text"] == "Not reviewed yet."
        ],
        "titles_repeated_across_sections": sorted(
            {
                f["title"]
                for f in findings
                if sum(1 for g in findings if g["title"] == f["title"]) > 1
            }
        ),
    }

    disc: list[dict[str, Any]] = []
    if len(findings) != stated_total or filter_counts.get("all") != stated_total:
        disc.append(
            {
                "id": "stated_total_mismatch",
                "resolved": False,
                "detail": f"stated {stated_total}, All filter {filter_counts.get('all')}, parsed {len(findings)}",
            }
        )
    type_mismatch = {
        t: (filter_counts.get(t), type_pairs[t])
        for t in type_pairs
        if filter_counts.get(t) != type_pairs[t]
    }
    disc.append(
        {
            "id": "type_counters_sum_exceeds_total",
            "resolved": not type_mismatch
            and counts["filter_type_sum"] - len(findings)
            == sum(len(f["pm_types"]) - 1 for f in multi),
            "stated_total": stated_total,
            "filter_counts": filter_counts,
            "filter_type_sum": counts["filter_type_sum"],
            "parsed_findings": len(findings),
            "parsed_type_chip_pairs": type_pairs,
            "type_counter_mismatches": type_mismatch,
            "multi_type_findings": [
                {
                    "finding_id": f["finding_id"],
                    "task": f["task"],
                    "title": f["title"],
                    "pm_type_chips": f["pm_type_chips"],
                }
                for f in multi
            ],
            "explanation": "Type filters count (finding, type-chip) pairs; each finding is counted once in the total. "
            "Excess of the type-counter sum over the total equals the extra chips on multi-type findings.",
        }
    )
    bench = [f for f in findings if f["scope"] == "benchmark"]
    disc.append(
        {
            "id": "benchmark_scope_findings",
            "resolved": True,
            "count": len(bench),
            "finding_ids": [f["finding_id"] for f in bench],
            "pm_types": sorted({t for f in bench for t in f["pm_types"]}),
            "included_in_stated_total": len(findings) == stated_total,
            "task_scope_findings": len(findings) - len(bench),
            "explanation": f"Findings in section '{BENCHMARK_SECTION_ID}' (index label "
            f"{next((i['label'] for i in index if i['section_id'] == BENCHMARK_SECTION_ID), None)!r}) have task=None. "
            "They are counted in the stated total and in their type filter; they are not per-task defects.",
        }
    )
    index_mismatch = [
        {
            "section_id": t["section_id"],
            "index_count": t["index_count"],
            "parsed": t["finding_count"],
        }
        for t in tasks
        if t["index_count"] != t["finding_count"]
    ]
    nav_only = sorted({i["section_id"] for i in index} - {t["section_id"] for t in tasks})
    if index_mismatch or nav_only:
        disc.append(
            {
                "id": "index_counts_mismatch",
                "resolved": False,
                "mismatches": index_mismatch,
                "nav_only": nav_only,
            }
        )
    if len(task_rows) != stated_reviewed_tasks:
        disc.append(
            {
                "id": "task_sections_vs_stated_ported_tasks",
                "resolved": False,
                "stated_reviewed_ported_tasks": stated_reviewed_tasks,
                "task_sections": len(task_rows),
                "not_reviewed_sections": counts["task_sections_not_reviewed"],
                "explanation": "The page lists one more task section than the stated ported/reviewed count; "
                "sections marked 'Not reviewed yet.' carry no findings. Port membership is checked by attach_port_metadata.",
            }
        )
    statuses_not_in_legend = sorted(
        {f["port_status"] for f in findings} - set(LEGEND_STATUS_WORDS[s] for s in legend_statuses)
    )
    if statuses_not_in_legend:
        disc.append(
            {
                "id": "status_not_defined_in_legend",
                "resolved": False,
                "statuses": statuses_not_in_legend,
                "finding_ids": [
                    f["finding_id"] for f in findings if f["port_status"] in statuses_not_in_legend
                ],
                "legend_statuses": legend_statuses,
            }
        )
    status_word_mismatch = [
        {
            "finding_id": f["finding_id"],
            "chip": f["port_status_label"],
            "port_text_word": f["port_text_status_word"],
        }
        for f in findings
        if f["port_text_status_word"] is not None
        and f["port_text_status_word"] != f["port_status_label"]
    ]
    no_status_word = [f["finding_id"] for f in findings if f["port_text_status_word"] is None]
    if status_word_mismatch or no_status_word:
        disc.append(
            {
                "id": "port_text_status_disagrees_with_chip",
                "resolved": False,
                "mismatches": status_word_mismatch,
                "port_text_without_status_word": no_status_word,
            }
        )
    return counts, disc


# --------------------------------------------------------------------------- overview


def parse_pm_overview(html: str, *, source_url: str = PM_OVERVIEW_URL) -> dict[str, Any]:
    """Extract the companion post's task-level claims with their literal sentences."""
    root = _parse_tree(html)
    article = _one(root.find_all("article"), "<article>")
    text = article.text()
    patterns = {
        "ported_tasks": (r"we ported (\d+) of its (\d+) tasks", ("ported", "upstream_total")),
        "skipped_task": (r"We skipped (?:one task, )?([a-z0-9-]+)\.", ("task",)),
        "tasks_with_unstated_rule": (
            r"In (\d+) of the (\d+) tasks we reviewed, models could do honest, correct work and still fail",
            ("count", "reviewed"),
        ),
        "closing_counts": (
            r"Of the (\d+) Terminal Bench 4 tasks we reviewed, (\d+) had at least one problem: (\d+) could be passed by cheating, and (\d+) graded the model on a rule the instructions never clearly stated",
            ("reviewed", "with_any_problem", "passable_by_cheating", "unstated_rule"),
        ),
    }
    claims: dict[str, Any] = {}
    for key, (pat, names) in patterns.items():
        m = re.search(pat, text)
        if not m:
            raise PMSourceError(f"overview claim {key!r} not found")
        vals = [int(g) if g.isdigit() else g for g in m.groups()]
        claims[key] = {"literal": m.group(0), **dict(zip(names, vals, strict=True))}
    harbor = []
    for li in article.find_all("li"):
        for link in _harbor_links(li):
            harbor.append({**link, "citation_text": li.text().strip()})
    return {
        "url": source_url,
        "sha256": _sha256(html.encode("utf-8")),
        "bytes": len(html.encode("utf-8")),
        "claims": claims,
        "harbor_trial_citations": harbor,
    }


def reconcile_overview(catalog: dict[str, Any], overview: dict[str, Any]) -> list[dict[str, Any]]:
    findings = catalog["findings"]
    task_rows = [t for t in catalog["tasks"] if t["scope"] == "task"]
    reviewed = [t for t in task_rows if t["none_text"] != "Not reviewed yet."]

    def tasks_where(pred) -> set[str]:
        return {f["task"] for f in findings if f["task"] and pred(f)}

    any_problem = tasks_where(lambda f: True)
    any_hack = tasks_where(lambda f: "reward_hack" in f["pm_types"])
    tested_hack = tasks_where(
        lambda f: "reward_hack" in f["pm_types"] and f["execution_claim"] == "true"
    )
    ambiguity = tasks_where(lambda f: "ambiguity" in f["pm_types"])
    c = overview["claims"]
    rows = [
        (
            "reviewed_tasks",
            c["closing_counts"]["literal"],
            c["closing_counts"]["reviewed"],
            len(reviewed),
            "task sections not marked 'Not reviewed yet.'",
            None,
        ),
        (
            "tasks_with_any_problem",
            c["closing_counts"]["literal"],
            c["closing_counts"]["with_any_problem"],
            len(any_problem),
            "task sections with >=1 finding (benchmark-wide excluded)",
            None,
        ),
        (
            "tasks_passable_by_cheating",
            c["closing_counts"]["literal"],
            c["closing_counts"]["passable_by_cheating"],
            len(tested_hack),
            "tasks with >=1 reward-hack finding whose execution_claim is 'true'",
            {
                "tasks_with_any_reward_hack_chip": len(any_hack),
                "reward_hack_tasks_without_tested_hack": sorted(any_hack - tested_hack),
            },
        ),
        (
            "tasks_with_unstated_rule",
            c["closing_counts"]["literal"],
            c["closing_counts"]["unstated_rule"],
            len(ambiguity),
            "tasks with >=1 Ambiguity chip",
            None,
        ),
        (
            "tasks_with_unstated_rule_intro",
            c["tasks_with_unstated_rule"]["literal"],
            c["tasks_with_unstated_rule"]["count"],
            len(ambiguity),
            "tasks with >=1 Ambiguity chip",
            None,
        ),
        (
            "skipped_task_not_reviewed",
            c["skipped_task"]["literal"],
            c["skipped_task"]["task"],
            ",".join(catalog["counts"]["task_sections_not_reviewed"]),
            "sections marked 'Not reviewed yet.'",
            None,
        ),
    ]
    return [
        {
            "claim": k,
            "literal": lit,
            "claimed": claimed,
            "computed": comp,
            "computed_definition": d,
            "agrees": claimed == comp,
            **({"notes": notes} if notes else {}),
        }
        for k, lit, claimed, comp, d, notes in rows
    ]


# --------------------------------------------------------------------------- port metadata


def _git_head(port_root: Path) -> str | None:
    git = port_root / ".git"
    if not git.is_dir():
        return None
    head = (git / "HEAD").read_text().strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    loose = git / ref
    if loose.is_file():
        return loose.read_text().strip()
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    return None


def _port_files(port_root: Path) -> list[Path]:
    return sorted(
        p
        for p in port_root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(port_root).parts
    )


def _text_or_none(path: Path, limit: int = 8 << 20) -> str | None:
    if path.stat().st_size > limit:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _comment_block_above(lines: list[str], first_line: int) -> str:
    out = []
    i = first_line - 2
    while i >= 0 and lines[i].lstrip().startswith("#"):
        out.append(lines[i])
        i -= 1
    return "\n".join(reversed(out))


def _python_defs(rel: str, source: str) -> list[dict[str, Any]]:
    """test_* functions (top level or in classes) with node ids and source text; parsed, never run."""
    tree = ast.parse(source, filename=rel)
    lines = source.splitlines()
    out = []

    def visit(body: list[ast.stmt], prefix: list[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + [node.name])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                text = "\n".join(lines[first - 1 : node.end_lineno])
                out.append(
                    {
                        "name": node.name,
                        "qualname": "::".join(prefix + [node.name]),
                        "nodeid": "::".join([rel] + prefix + [node.name]),
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                        "parametrized": any(
                            "parametrize" in ast.unparse(d) for d in node.decorator_list
                        ),
                        "_text": _comment_block_above(lines, first) + "\n" + text,
                    }
                )

    visit(tree.body, [])
    return out


_CHECK_NAME = re.compile(r"^(?:\w+::)*\w+_\w+$")


def _named_check_strings(rel: str, source: str) -> list[dict[str, Any]]:
    """Identifier-like string constants in grader code (e.g. ``@check("Suite::test_p37_...")``, ``checks["x_y"]``)."""
    out = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) < 200
            and _CHECK_NAME.match(node.value)
        ):
            out.append(
                {
                    "value": node.value,
                    "name": node.value.split("::")[-1],
                    "path": rel,
                    "line": node.lineno,
                }
            )
    return out


def _scan_port_task(port_root: Path, task_dir: Path) -> dict[str, Any]:
    rel_task = task_dir.relative_to(port_root).as_posix()
    tests, checks, named, texts, errors, files = [], [], [], {}, [], []
    for path in _port_files(task_dir):
        rel = path.relative_to(port_root).as_posix()
        files.append(rel)
        text = _text_or_none(path)
        if text is None:
            continue
        texts[rel] = text
        if path.suffix != ".py":
            continue
        try:
            defs = _python_defs(rel, text)
        except SyntaxError as exc:
            errors.append({"path": rel, "error": f"SyntaxError: {exc.msg} at line {exc.lineno}"})
            continue
        in_tests = rel.startswith(f"{rel_task}/tests/") and Path(rel).name.startswith("test_")
        (tests if in_tests else checks).extend({**d, "path": rel} for d in defs)
        if not rel.startswith(f"{rel_task}/tests/"):
            named.extend(_named_check_strings(rel, text))
    upstream_name = None
    for cand in ("tests/data/upstream_task.toml", "tests/upstream_task.toml"):
        p = task_dir / cand
        if p.is_file():
            upstream_name = tomllib.loads(p.read_text()).get("task", {}).get("name")
            break
    licence = task_dir / "LICENSE.md"
    return {
        "dir": rel_task,
        "upstream_task_name": upstream_name,
        "task_licence": {"path": f"{rel_task}/LICENSE.md", "sha256": _sha256(licence.read_bytes())}
        if licence.is_file()
        else None,
        "test_files": sorted({t["path"] for t in tests}),
        "tests": tests,
        "in_image_checks": checks,
        "named_checks": named,
        "texts": texts,
        "parse_errors": errors,
        "file_count": len(files),
    }


def _resolve_finding(row: dict[str, Any], scan: dict[str, Any] | None) -> None:
    explicit: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    if row["scope"] == "benchmark":
        row.update(
            port_test_mapping_status="not_applicable",
            port_test_mapping_reason="benchmark-scope finding: no task directory in the port; PM's port text: "
            + row["port_text"],
            port_tests_explicit=[],
            port_test_candidates=[],
            port_test_unresolved_mentions=[],
        )
        return
    if scan is None:
        row.update(
            port_test_mapping_status="not_applicable",
            port_test_mapping_reason=f"task {row['task']!r} has no directory in the pinned port",
        )
        return
    by_name: dict[str, list[dict[str, Any]]] = {}
    for t in scan["tests"]:
        by_name.setdefault(t["name"], []).append(t)
    checks_by_name: dict[str, list[dict[str, Any]]] = {}
    for t in scan["in_image_checks"]:
        checks_by_name.setdefault(t["name"], []).append(t)

    def occurrences(token: str) -> list[str]:
        rx = re.compile(r"(?<![\w])" + re.escape(token) + r"(?![\w])")
        return sorted(p for p, text in scan["texts"].items() if rx.search(text))

    def add(lst: list[dict[str, Any]], item: dict[str, Any]) -> None:
        key = (item["nodeid"], item.get("basis"))
        if all((x["nodeid"], x.get("basis")) != key for x in lst):
            lst.append(item)

    def unresolved_once(item: dict[str, Any]) -> None:
        for u in unresolved:
            if u["token"] == item["token"]:
                u["mention_count"] += 1
                return
        unresolved.append({**item, "mention_count": 1})

    def named_check_candidates(name: str, context: str) -> int:
        """Grader-registered check strings that PM's name abbreviates or prefixes, plus tests asserting them."""
        found = 0
        for c in scan["named_checks"]:
            if c["name"].startswith(name + "_"):
                basis = "named_check_extends_mentioned_name"
            elif name.startswith("test_") and c["name"] == name[5:]:
                basis = "named_check_equals_name_without_test_prefix"
            else:
                continue
            found += 1
            add(
                candidates,
                {
                    "nodeid": f"{c['path']}:{c['line']}:{c['value']}",
                    "path": c["path"],
                    "line": c["line"],
                    "basis": basis,
                    "evidence": context,
                },
            )
            rx = re.compile(r"(?<![\w])" + re.escape(c["name"]) + r"(?![\w])")
            for t in scan["tests"]:
                if rx.search(t["_text"]):
                    add(
                        candidates,
                        {
                            "nodeid": t["nodeid"],
                            "path": t["path"],
                            "line": t["line"],
                            "basis": "port_test_references_named_check",
                            "evidence": c["value"],
                        },
                    )
        return found

    for m in row["port_test_mentions"]:
        token = m["token"]
        base = {
            "token": token,
            "kind": m["kind"],
            "region": m["region"],
            "context": m["context"],
            "context_mentions_upstream": m["context_mentions_upstream"],
        }
        if m["kind"] in ("function", "node_id"):
            hits = by_name.get(m["name"], [])
            if m["kind"] == "node_id":
                hits = [h for h in hits if h["path"] == f"{scan['dir']}/{m['path']}"] or hits
            for h in hits:
                add(
                    explicit,
                    {
                        "nodeid": h["nodeid"],
                        "path": h["path"],
                        "line": h["line"],
                        "location": "port_tests",
                        "match": "exact_name",
                        "via_token": token,
                        "claimed_param": m["param"],
                        "parametrized": h["parametrized"],
                        "context": m["context"],
                    },
                )
            if hits:
                continue
            chits = checks_by_name.get(m["name"], [])
            for h in chits:
                add(
                    explicit,
                    {
                        "nodeid": h["nodeid"],
                        "path": h["path"],
                        "line": h["line"],
                        "location": "in_image_grader",
                        "match": "exact_name",
                        "via_token": token,
                        "claimed_param": m["param"],
                        "parametrized": h["parametrized"],
                        "context": m["context"],
                    },
                )
            if chits:
                continue
            nhits = [c for c in scan["named_checks"] if c["name"] == m["name"]]
            for c in nhits:
                add(
                    explicit,
                    {
                        "nodeid": f"{c['path']}:{c['line']}:{c['value']}",
                        "path": c["path"],
                        "line": c["line"],
                        "location": "port_named_check",
                        "match": "exact_string",
                        "via_token": token,
                        "claimed_param": m["param"],
                        "parametrized": False,
                        "context": m["context"],
                    },
                )
            if nhits:
                continue
            n_cand = named_check_candidates(m["name"], m["context"])
            unresolved_once(
                {
                    **base,
                    "reason": "no def or registered check string with this exact name in the pinned port task"
                    + (
                        f"; {n_cand} registered check(s) extend or de-prefix it (see candidates)"
                        if n_cand
                        else ""
                    ),
                    "port_text_occurrences": occurrences(m["name"]),
                }
            )
        elif m["kind"] == "file":
            # Bare names like "test_outputs.py" are looked up under the task's tests/ directory.
            rel = f"{scan['dir']}/{m['path'] if m['path'].startswith('tests/') else 'tests/' + m['path']}"
            file_tests = [t for t in scan["tests"] if rel and t["path"] == rel]
            for t in file_tests:
                add(
                    candidates,
                    {
                        "nodeid": t["nodeid"],
                        "path": t["path"],
                        "line": t["line"],
                        "basis": "test_file_named_in_finding",
                        "evidence": m["context"],
                    },
                )
            if not file_tests:
                exists = bool(rel) and rel in scan["texts"]
                unresolved_once(
                    {
                        **base,
                        "reason": "named file exists in the port task but is not a pytest test file"
                        if exists
                        else "named file is not present at that path in the pinned port task",
                        "port_text_occurrences": occurrences(m["path"].rsplit("/", 1)[-1]),
                    }
                )
        elif m["kind"] == "wildcard":
            suffix = m["name"].lstrip("*")
            hits = [
                t for t in scan["tests"] + scan["in_image_checks"] if t["name"].endswith(suffix)
            ]
            for h in hits:
                add(
                    candidates,
                    {
                        "nodeid": h["nodeid"],
                        "path": h["path"],
                        "line": h["line"],
                        "basis": "wildcard_name_in_finding",
                        "evidence": m["context"],
                    },
                )
            nhits = [c for c in scan["named_checks"] if c["name"].endswith(suffix)]
            for c in nhits:
                add(
                    candidates,
                    {
                        "nodeid": f"{c['path']}:{c['line']}:{c['value']}",
                        "path": c["path"],
                        "line": c["line"],
                        "basis": "wildcard_name_in_finding",
                        "evidence": m["context"],
                    },
                )
            if not hits and not nhits:
                unresolved_once(
                    {
                        **base,
                        "reason": "no port test, check def or registered check string ends with this suffix",
                        "port_text_occurrences": occurrences(suffix),
                    }
                )
    explicit_ids = {e["nodeid"] for e in explicit}
    if row["github_all_numbers"]:
        nums = set(row["github_all_numbers"])
        for t in scan["tests"]:
            cited = {int(n) for n in re.findall(r"(?:#|/issues/|/pull/)(\d+)\b", t["_text"])} & nums
            if cited and t["nodeid"] not in explicit_ids:
                add(
                    candidates,
                    {
                        "nodeid": t["nodeid"],
                        "path": t["path"],
                        "line": t["line"],
                        "basis": "test_source_cites_finding_github_number",
                        "evidence": f"test source or comment block above it cites #{', #'.join(map(str, sorted(cited)))}",
                    },
                )
    candidates = [c for c in candidates if c["nodeid"] not in explicit_ids]
    if explicit:
        status = "explicit"
        reason = f"{len(explicit)} test/check name(s) named in PM's finding text exist in the pinned port"
    elif candidates:
        status = "candidate_only"
        reason = "no exact test named; candidates from " + ", ".join(
            sorted({c["basis"] for c in candidates})
        )
    else:
        status = "no_source_match"
        if unresolved:
            reason = "finding names test(s) absent from the pinned port task: " + ", ".join(
                u["token"] for u in unresolved
            )
        else:
            reason = "finding text names no test, and no port test source cites the finding's GitHub numbers"
    if unresolved and status != "no_source_match":
        reason += f"; {len(unresolved)} named token(s) not found in port: " + ", ".join(
            u["token"] for u in unresolved
        )
    row.update(
        port_test_mapping_status=status,
        port_test_mapping_reason=reason,
        port_tests_explicit=explicit,
        port_test_candidates=candidates,
        port_test_unresolved_mentions=unresolved,
    )


def attach_port_metadata(
    catalog: dict[str, Any], port_root: Path, *, revision: str
) -> dict[str, Any]:
    """Return a copy of ``catalog`` enriched with pinned port metadata (AST/text only)."""
    port_root = Path(port_root)
    head = _git_head(port_root)
    if head is not None and head != revision:
        raise PMSourceError(f"port checkout HEAD {head} != pinned revision {revision}")
    out = copy.deepcopy(catalog)
    task_dirs = sorted(
        p for p in port_root.iterdir() if p.is_dir() and (p / ".manifest.json").is_file()
    )
    scans = {p.name: _scan_port_task(port_root, p) for p in task_dirs}
    files = _port_files(port_root)
    tree_digest = hashlib.sha256()
    for p in files:
        tree_digest.update(
            p.relative_to(port_root).as_posix().encode()
            + b"\0"
            + _sha256(p.read_bytes()).encode()
            + b"\n"
        )
    licence = port_root / "LICENSE"
    licence_text = licence.read_text() if licence.is_file() else ""
    page_tasks = {t["task"] for t in out["tasks"] if t["task"]}
    for t in out["tasks"]:
        scan = scans.get(t["task"]) if t["task"] else None
        t["in_port"] = scan is not None if t["task"] else None
        t["port_dir"] = scan["dir"] if scan else None
        t["port_upstream_task_name"] = scan["upstream_task_name"] if scan else None
        t["port_test_files"] = scan["test_files"] if scan else []
        t["port_test_count"] = len(scan["tests"]) if scan else 0
    for row in out["findings"]:
        _resolve_finding(row, scans.get(row["task"]) if row["task"] else None)
    findings = out["findings"]
    status_counts: dict[str, int] = {}
    for f in findings:
        status_counts[f["port_test_mapping_status"]] = (
            status_counts.get(f["port_test_mapping_status"], 0) + 1
        )
    upstream_mismatch = [
        {"dir": s["dir"], "upstream_task_name": s["upstream_task_name"]}
        for name, s in scans.items()
        if s["upstream_task_name"] not in (None, f"terminal-bench/{name}")
    ]
    out["port"] = {
        "repo_url": PORT_REPO_URL,
        "revision": revision,
        "checkout_head": head,
        "tree_sha256": tree_digest.hexdigest(),
        "file_count": len(files),
        "licence": {
            "path": "LICENSE",
            "sha256": _sha256(licence.read_bytes()) if licence.is_file() else None,
            "detected": "Apache-2.0"
            if re.search(r"Apache License\s+Version 2\.0", licence_text)
            else None,
        },
        "task_licences": {n: s["task_licence"] for n, s in scans.items() if s["task_licence"]},
        "task_dirs": sorted(scans),
        "page_tasks_not_in_port": sorted(page_tasks - set(scans)),
        "port_tasks_not_on_page": sorted(set(scans) - page_tasks),
        "upstream_name_mismatches": upstream_mismatch,
        "tasks_without_upstream_toml": sorted(
            n for n, s in scans.items() if s["upstream_task_name"] is None
        ),
        "parse_errors": [e for s in scans.values() for e in s["parse_errors"]],
        "test_function_count": sum(len(s["tests"]) for s in scans.values()),
    }
    out["counts"]["port_test_mapping"] = dict(sorted(status_counts.items()))
    out["counts"]["port_tests_explicit_links"] = sum(
        len(f["port_tests_explicit"]) for f in findings
    )
    out["counts"]["port_test_candidate_links"] = sum(
        len(f["port_test_candidates"]) for f in findings
    )
    out["counts"]["port_test_unresolved_mentions"] = sum(
        len(f["port_test_unresolved_mentions"]) for f in findings
    )
    out["counts"]["findings_with_unresolved_mentions"] = sum(
        1 for f in findings if f["port_test_unresolved_mentions"]
    )
    return out


# --------------------------------------------------------------------------- acquisition


def describe_source(
    raw: bytes, *, url: str, retrieved_at: str | None, http_headers: str | None = None
) -> dict[str, Any]:
    """Provenance record for one fetched source file (pure)."""
    headers: dict[str, str] = {}
    status = None
    for line in (http_headers or "").splitlines():
        if line.startswith("HTTP/"):
            status, headers = line.strip(), {}
        elif ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    keep = (
        "content-type",
        "content-length",
        "last-modified",
        "etag",
        "x-amz-version-id",
        "date",
        "server",
    )
    return {
        "url": url,
        "sha256": _sha256(raw),
        "bytes": len(raw),
        "retrieved_at": retrieved_at,
        "http_status": status,
        "http_headers": {k: headers[k] for k in keep if k in headers},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--cache",
        type=Path,
        required=True,
        help="dir with raw/issues.html, raw/overview.html, port/",
    )
    ap.add_argument("--revision", required=True, help="pinned port commit")
    ap.add_argument(
        "--out", type=Path, required=True, help="catalog JSON output (outside agent views)"
    )
    args = ap.parse_args(argv)
    raw = args.cache / "raw"
    retrieved = (
        (raw / "fetched_at_utc.txt").read_text().strip()
        if (raw / "fetched_at_utc.txt").is_file()
        else None
    )
    issues_bytes = (raw / "issues.html").read_bytes()
    overview_bytes = (raw / "overview.html").read_bytes()
    catalog = parse_pm_catalog(
        issues_bytes.decode("utf-8"),
        retrieved_at=retrieved,
        overview_html=overview_bytes.decode("utf-8"),
    )
    catalog = attach_port_metadata(catalog, args.cache / "port", revision=args.revision)

    def headers(name: str) -> str | None:
        p = raw / f"{name}.headers"
        return p.read_text() if p.is_file() else None

    catalog["acquisition"] = {
        "issues": describe_source(
            issues_bytes, url=PM_ISSUES_URL, retrieved_at=retrieved, http_headers=headers("issues")
        ),
        "overview": describe_source(
            overview_bytes,
            url=PM_OVERVIEW_URL,
            retrieved_at=retrieved,
            http_headers=headers("overview"),
        ),
        "port": {
            "url": PORT_REPO_URL,
            "revision": args.revision,
            "local_path": str(args.cache / "port"),
        },
    }
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
    c = catalog["counts"]
    print(
        json.dumps(
            {
                k: c[k]
                for k in (
                    "stated_total",
                    "findings",
                    "filter_counts",
                    "type_chip_pairs",
                    "by_scope",
                    "by_port_status",
                    "by_execution_claim",
                    "port_test_mapping",
                )
            },
            indent=None,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
