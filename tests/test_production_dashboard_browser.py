"""Static browser-boundary checks; interactive smoke runs use managed Chromium.

Playwright is not a project dependency, so these checks use the standard library
rather than adding a browser/runtime dependency to the production test suite.
"""

import re
from html.parser import HTMLParser

from evallab.production.dashboard import DASHBOARD


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.scripts[-1] += data


def test_dashboard_is_self_contained_and_has_labeled_operator_surfaces() -> None:
    page = Page()
    page.feed(DASHBOARD)
    ids = [attrs["id"] for _, attrs in page.elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert len(page.scripts) == 1
    assert "Verified Asset Store" in DASHBOARD
    assert {"connection", "cursor", "intervention", "token", "runs", "assets"} <= set(ids)
    labels = {attrs["for"] for tag, attrs in page.elements if tag == "label"}
    for tag, attrs in page.elements:
        if tag in {"input", "select", "textarea"}:
            assert attrs["id"] in labels
        if tag == "dialog":
            assert attrs.get("aria-labelledby")
        for attribute in ("aria-labelledby", "aria-describedby"):
            if targets := attrs.get(attribute):
                assert all(target in ids for target in targets.split())
        for attribute in ("src", "href", "action"):
            if value := attrs.get(attribute):
                assert value.startswith("/") and not value.startswith("//") or value == "data:,"
    tokens = [attrs for _, attrs in page.elements if attrs.get("id") == "token"]
    assert len(tokens) == 1
    assert tokens[0]["type"] == "password"
    assert tokens[0]["autocomplete"] == "off"


def test_dashboard_uses_text_dom_and_same_origin_without_dynamic_code() -> None:
    page = Page()
    page.feed(DASHBOARD)
    script = page.scripts[0]
    assert "textContent" in script
    assert "replaceChildren" in script
    assert not re.search(r"https?://|wss?://|\b(?:eval|Function)\s*\(", script)
    assert not re.search(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|localStorage|sessionStorage",
        script,
    )
    assert not any(name.startswith("on") for _, attrs in page.elements for name in attrs)


def test_atlas_is_a_labeled_lazy_tab_with_an_in_page_focus_target() -> None:
    page = Page()
    page.feed(DASHBOARD)
    elements = {attrs["id"]: (tag, attrs) for tag, attrs in page.elements if "id" in attrs}
    tabs = [attrs["id"] for _, attrs in page.elements if attrs.get("role") == "tab"]
    assert tabs[tabs.index("trust-tab") - 1] == "atlas-tab"
    _, tab = elements["atlas-tab"]
    _, panel = elements[tab["aria-controls"]]
    assert panel["role"] == "tabpanel"
    assert panel["aria-labelledby"] == "atlas-tab"
    assert "hidden" in panel
    assert tab["aria-selected"] == "false"
    assert tab["tabindex"] == "-1"
    detail_tag, detail = elements["atlas-run-panel"]
    assert detail_tag == "section"
    assert detail["tabindex"] == "-1"
    assert detail["aria-labelledby"] == "atlas-run-title"
    assert "hidden" in detail
    for target in ("cloud-atlas-status", "atlas-run-status"):
        assert elements[target][1]["role"] == "status"
    for target in ("cloud-atlas-refresh", "atlas-run-refresh", "atlas-run-close", "atlas-reset"):
        assert elements[target][0] == "button"
        assert elements[target][1]["type"] == "button"
