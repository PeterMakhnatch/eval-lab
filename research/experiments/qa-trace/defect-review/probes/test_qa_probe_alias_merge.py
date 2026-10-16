"""QA Trace probe for format-code-task-002391: the prompt's merge examples.

The prompt: when two results for one dependency "share any identifier between
their primary id and their aliases", ``audit(source)`` "should emit only one
vulnerability for that advisory", preferring a ``PYSEC`` id "regardless of the
provider order", merging aliases and "keeping its own primary id out of its
alias set". Cases 1-2 are the prompt's own examples. Case 3 links two results
only through a third; leaving two results that still share an identifier would
break the same sentence. Every case is checked in both provider orders.
"""

from collections.abc import Iterator

import pytest
from packaging.version import Version
from pip_audit._audit import AuditOptions, Auditor
from pip_audit._dependency_source import DependencySource
from pip_audit._service import (
    Dependency,
    ResolvedDependency,
    VulnerabilityResult,
    VulnerabilityService,
)


class _Source(DependencySource):
    def __init__(self, deps: list[Dependency]):
        self.deps = deps

    def collect(self) -> Iterator[Dependency]:
        yield from self.deps

    def fix(self, _fix_version) -> None:
        raise NotImplementedError


class _Service(VulnerabilityService):
    def __init__(self, vulns: list[VulnerabilityResult]):
        self.vulns = vulns

    def query(self, spec: Dependency) -> tuple[Dependency, list[VulnerabilityResult]]:
        return spec, self.vulns

    def query_all(self, specs):
        for dep in specs:
            yield dep, self.vulns


def _vuln(id_: str, aliases: set[str]) -> VulnerabilityResult:
    return VulnerabilityResult(
        id=id_, description=f"desc {id_}", fix_versions=[Version("2.0.0")], aliases=aliases
    )


def _audit(records: list[tuple[str, set[str]]]) -> list[VulnerabilityResult]:
    dep = ResolvedDependency(name="demo-pkg", version=Version("1.0.0"))
    vulns = [_vuln(id_, set(aliases)) for id_, aliases in records]
    results = list(Auditor(_Service(vulns), options=AuditOptions()).audit(_Source([dep])))
    assert len(results) == 1
    return list(results[0][1])


CASES = {
    "prompt two-record example": (
        [("PYSEC-0", {"CVE-2024-0001"}), ("GHSA-aaaa", {"CVE-2024-0001", "GHSA-aaaa"})],
        {"CVE-2024-0001", "GHSA-aaaa"},
    ),
    "prompt three-record example": (
        [
            ("PYSEC-0", {"CVE-2024-0001"}),
            ("GHSA-aaaa", {"CVE-2024-0001"}),
            ("CVE-2024-0001", {"GHSA-aaaa"}),
        ],
        {"CVE-2024-0001", "GHSA-aaaa"},
    ),
    "third record links two results": (
        [
            ("PYSEC-0", {"CVE-2024-0001"}),
            ("GHSA-aaaa", {"CVE-2024-0002"}),
            ("CVE-2024-0001", {"CVE-2024-0002"}),
        ],
        {"CVE-2024-0001", "CVE-2024-0002", "GHSA-aaaa"},
    ),
}


@pytest.mark.parametrize(
    ("name", "order"),
    [(name, order) for name in CASES for order in ("given", "reversed")],
)
def test_merged_advisory_is_one_pysec_result(name: str, order: str) -> None:
    records, expected_aliases = CASES[name]
    if order == "reversed":
        records = list(reversed(records))
    vulns = _audit(records)
    ids = [v.id for v in vulns]
    assert ids == ["PYSEC-0"], ids
    (only,) = vulns
    assert expected_aliases <= set(only.aliases)
    assert "PYSEC-0" not in only.aliases
