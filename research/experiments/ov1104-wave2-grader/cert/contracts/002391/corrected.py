import sys
sys.path.insert(0, '/testbed')

from datetime import datetime, timezone
import unittest
from packaging.version import Version

from pip_audit._audit import AuditOptions, Auditor
from pip_audit._dependency_source import DependencySource
from pip_audit._service import (
    Dependency,
    ResolvedDependency,
    VulnerabilityResult,
    VulnerabilityService,
)


def _dep(name: str = "demo-pkg", version: str = "1.0.0") -> ResolvedDependency:
    return ResolvedDependency(name=name, version=Version(version))


def _vuln(id_: str, aliases: set[str], desc: str | None = None) -> VulnerabilityResult:
    return VulnerabilityResult(
        id=id_,
        description=desc if desc is not None else f"desc {id_}",
        fix_versions=[Version("2.0.0")],
        aliases=set(aliases),
        published=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class _DummySource(DependencySource):
    def __init__(self, deps: list[Dependency]):
        self.deps = deps
        self.collect_calls = 0

    def collect(self):
        self.collect_calls += 1
        yield from self.deps

    def fix(self, _fix_version) -> None:
        raise NotImplementedError


class _DummyService(VulnerabilityService):
    def __init__(self, mapping: dict[str, list[VulnerabilityResult]]):
        self.mapping = mapping
        self.query_all_calls = 0

    def query(self, spec: Dependency):
        return spec, list(self.mapping.get(spec.name, []))

    def query_all(self, specs):
        self.query_all_calls += 1
        for dep in specs:
            yield dep, list(self.mapping.get(dep.name, []))


def _summarize(results):
    return [
        (dep.name, str(dep.version), sorted((v.id, sorted(v.aliases)) for v in vulns))
        for dep, vulns in results
    ]


class TestAuditorDeduplicationContract(unittest.TestCase):
    def test_pysec_priority_alias_merging_and_self_id_exclusion(self) -> None:
        dep = _dep()
        source = _DummySource([dep])
        service = _DummyService(
            {
                "demo-pkg": [
                    _vuln("PYSEC-0", {"CVE-2024-0001"}, desc="keep me"),
                    _vuln("GHSA-aaaa", {"PYSEC-0", "CVE-2024-0001", "GHSA-aaaa"}, desc="drop me"),
                ]
            }
        )
        results = list(Auditor(service, options=AuditOptions()).audit(source))
        self.assertEqual(len(results), 1)
        out_dep, vulns = results[0]
        self.assertEqual(out_dep, dep)
        self.assertEqual(len(vulns), 1)
        self.assertEqual(vulns[0].id, "PYSEC-0")
        self.assertEqual(vulns[0].aliases, {"CVE-2024-0001", "GHSA-aaaa"})
        self.assertEqual(vulns[0].description, "keep me")
        self.assertEqual(vulns[0].fix_versions, [Version("2.0.0")])

    def test_reverse_order_and_id_to_alias_cross_matching(self) -> None:
        d1 = _dep("pkg-a", "1.0.0")
        d2 = _dep("pkg-b", "2.0.0")
        d3 = _dep("pkg-c", "3.0.0")
        source = _DummySource([d1, d2, d3])
        service = _DummyService(
            {
                "pkg-a": [
                    _vuln("GHSA-aaaa", {"CVE-2024-0001", "GHSA-aaaa"}),
                    _vuln("PYSEC-0", {"CVE-2024-0001"}),
                ],
                "pkg-b": [
                    _vuln("GHSA-bbbb", {"PYSEC-1", "CVE-2024-0002"}),
                    _vuln("PYSEC-1", set()),
                ],
                "pkg-c": [
                    _vuln("PYSEC-2", {"GHSA-cccc"}),
                    _vuln("GHSA-cccc", {"CVE-2024-0003"}),
                ],
            }
        )
        results = list(Auditor(service).audit(source))
        self.assertEqual(
            _summarize(results),
            [
                ("pkg-a", "1.0.0", [("PYSEC-0", ["CVE-2024-0001", "GHSA-aaaa"])]),
                ("pkg-b", "2.0.0", [("PYSEC-1", ["CVE-2024-0002"])]),
                ("pkg-c", "3.0.0", [("PYSEC-2", ["CVE-2024-0003", "GHSA-cccc"])]),
            ],
        )

    def test_non_pysec_duplicates_collapse_and_update_seen_aliases(self) -> None:
        d1 = _dep("non-pysec-alias", "1.0.0")
        d2 = _dep("non-pysec-same-id", "1.0.0")
        d3 = _dep("non-pysec-id-in-alias", "1.0.0")
        source = _DummySource([d1, d2, d3])
        service = _DummyService(
            {
                "non-pysec-alias": [
                    _vuln("GHSA-aaaa", {"CVE-2024-0001"}),
                    _vuln("GHSA-bbbb", {"CVE-2024-0001", "GHSA-bbbb"}),
                ],
                "non-pysec-same-id": [
                    _vuln("CVE-2024-0002", set()),
                    _vuln("CVE-2024-0002", {"GHSA-cccc"}),
                ],
                "non-pysec-id-in-alias": [
                    _vuln("CVE-2024-0003", {"GHSA-dddd"}),
                    _vuln("GHSA-dddd", {"CVE-2024-9999"}),
                ],
            }
        )
        results = list(Auditor(service).audit(source))
        self.assertEqual(
            _summarize(results),
            [
                ("non-pysec-alias", "1.0.0", [("GHSA-aaaa", ["CVE-2024-0001", "GHSA-bbbb"])]),
                ("non-pysec-same-id", "1.0.0", [("CVE-2024-0002", ["GHSA-cccc"])]),
                ("non-pysec-id-in-alias", "1.0.0", [("CVE-2024-0003", ["CVE-2024-9999", "GHSA-dddd"])]),
            ],
        )

    def test_multiple_advisory_groups_on_same_dependency_merge_into_correct_group(self) -> None:
        dep = _dep()
        source = _DummySource([dep])
        service = _DummyService(
            {
                "demo-pkg": [
                    _vuln("PYSEC-1", {"CVE-2024-0001"}),
                    _vuln("PYSEC-2", {"CVE-2024-0002"}),
                    _vuln("GHSA-2222", {"CVE-2024-0002", "GHSA-2222"}),
                    _vuln("GHSA-3333", {"CVE-2024-0003"}),
                    _vuln("GHSA-4444", {"CVE-2024-0003", "OSV-3"}),
                ]
            }
        )
        results = list(Auditor(service).audit(source))
        self.assertEqual(
            _summarize(results),
            [
                (
                    "demo-pkg",
                    "1.0.0",
                    [
                        ("GHSA-3333", ["CVE-2024-0003", "OSV-3"]),
                        ("PYSEC-1", ["CVE-2024-0001"]),
                        ("PYSEC-2", ["CVE-2024-0002", "GHSA-2222"]),
                    ],
                )
            ],
        )

    def test_three_way_chain_and_dry_run_and_boundary_counts(self) -> None:
        d_empty = _dep("empty-pkg", "1.0.0")
        d_single = _dep("single-pkg", "1.0.0")
        d_three = _dep("three-pkg", "1.0.0")
        source = _DummySource([d_empty, d_single, d_three])
        service = _DummyService(
            {
                "empty-pkg": [],
                "single-pkg": [_vuln("GHSA-solo", {"CVE-2024-0010"})],
                "three-pkg": [
                    _vuln("PYSEC-0", {"CVE-2024-0001"}),
                    _vuln("GHSA-aaaa", {"CVE-2024-0001", "OSV-1"}),
                    _vuln("CVE-2024-0001", {"GHSA-aaaa", "OSV-2"}),
                ],
            }
        )
        dry_source = _DummySource([d_single])
        dry_results = list(Auditor(service, options=AuditOptions(dry_run=True)).audit(dry_source))
        self.assertEqual(dry_results, [])
        self.assertEqual(dry_source.collect_calls, 1)
        self.assertEqual(service.query_all_calls, 0)

        results = list(Auditor(service).audit(source))
        self.assertEqual(
            _summarize(results),
            [
                ("empty-pkg", "1.0.0", []),
                ("single-pkg", "1.0.0", [("GHSA-solo", ["CVE-2024-0010"])]),
                ("three-pkg", "1.0.0", [("PYSEC-0", ["CVE-2024-0001", "GHSA-aaaa", "OSV-1", "OSV-2"])]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
