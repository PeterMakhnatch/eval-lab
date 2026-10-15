"""QA Trace defect probe for format-code-task-002391 (pip-audit duplicate advisories).

The prompt: "The emitted result should preserve the complete alias set by
merging aliases from the duplicate records while keeping its own primary id
out of its alias set." No hidden test puts the emitted record's own id into an
alias set, so the hidden tests never check the exclusion. These two cases do,
and assert nothing beyond the prompt.
"""

from collections.abc import Iterator

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


def _audit(vulns: list[VulnerabilityResult]) -> list[VulnerabilityResult]:
    dep = ResolvedDependency(name="demo-pkg", version=Version("1.0.0"))
    results = list(Auditor(_Service(vulns), options=AuditOptions()).audit(_Source([dep])))
    assert len(results) == 1
    return list(results[0][1])


def test_non_pysec_duplicates_keep_primary_id_out_of_aliases() -> None:
    (only,) = _audit([_vuln("GHSA-aaaa", {"CVE-2024-0001"}), _vuln("CVE-2024-0001", {"GHSA-aaaa"})])
    assert only.id not in only.aliases
    assert {"GHSA-aaaa", "CVE-2024-0001"} <= {only.id, *only.aliases}


def test_pysec_winner_keeps_its_id_out_of_merged_aliases() -> None:
    (only,) = _audit(
        [_vuln("GHSA-aaaa", {"CVE-2024-0001", "PYSEC-0"}), _vuln("PYSEC-0", {"CVE-2024-0001"})]
    )
    assert only.id == "PYSEC-0"
    assert "PYSEC-0" not in only.aliases
    assert "CVE-2024-0001" in only.aliases
