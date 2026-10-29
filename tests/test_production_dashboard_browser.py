"""Static browser-boundary checks; interactive smoke runs use managed Chromium.

Playwright is not a project dependency, so these checks use the standard library
rather than adding a browser/runtime dependency to the production test suite.
"""

import re
from html.parser import HTMLParser

from evallab.production.dashboard import DASHBOARD, dashboard_html

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
Attrs = dict[str, str | None]


class Outline(HTMLParser):
    """Elements in document order, each with the elements that contain it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, Attrs, tuple[tuple[str, Attrs], ...]]] = []
        self.open: list[tuple[str, Attrs]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs), tuple(self.open)))
        if tag not in VOID_TAGS:
            self.open.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth][0] == tag:
                del self.open[depth:]
                return

    def position(self, element_id: str) -> int:
        return next(index for index, (_, attrs, _) in enumerate(self.elements) if attrs.get("id") == element_id)

    def within(self, index: int, ancestor_id: str) -> tuple[tuple[str, Attrs], ...] | None:
        """The containers between ``ancestor_id`` and the element, or None if it is not inside."""
        ancestors = self.elements[index][2]
        ids = [attrs.get("id") for _, attrs in ancestors]
        return ancestors[ids.index(ancestor_id) + 1 :] if ancestor_id in ids else None


def atlas_outline() -> Outline:
    outline = Outline()
    outline.feed(dashboard_html("atlas"))
    assert not outline.open  # every container closes, so nesting below is what a browser builds
    return outline


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



def test_home_screen_is_chosen_by_the_server_and_changes_nothing_else() -> None:
    import pytest

    assert dashboard_html() == DASHBOARD
    atlas = dashboard_html("atlas")
    assert atlas != DASHBOARD
    assert atlas.replace('<body data-home="atlas">', '<body data-home="live">') == DASHBOARD
    for refused in ("", "Atlas", "tools", '"><script>'):
        with pytest.raises(ValueError, match="unknown home screen"):
            dashboard_html(refused)


def test_run_atlas_first_screen_reaches_the_run_picker_before_its_information_panels() -> None:
    outline = atlas_outline()
    panels = [
        index
        for index, (tag, _, _) in enumerate(outline.elements)
        if tag == "details" and outline.within(index, "atlas-view") is not None
    ]
    assert panels
    picker = [outline.position(element_id) for element_id in ("cloud-atlas-filters", "cloud-atlas-runs")]
    for index in picker:
        containers = outline.within(index, "atlas-view")
        assert containers is not None
        assert not any(tag == "details" or "hidden" in attrs for tag, attrs in containers)
    detail = outline.position("atlas-run-panel")
    assert max(picker) < detail < min(panels)  # the run detail opens under the table, above the panels


def test_run_atlas_filter_form_offers_detector_alarm_and_order_controls() -> None:
    outline = atlas_outline()
    for element_id in ("atlas-alarmed_by", "atlas-sort"):
        assert outline.within(outline.position(element_id), "cloud-atlas-filters") is not None
