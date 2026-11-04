"""Check only the two duplicate-advisory examples in task 002391's instruction."""
from dataclasses import dataclass
from itertools import permutations

from pip_audit._audit import AuditOptions, Auditor
from pip_audit._service import Dependency, VulnerabilityResult, VulnerabilityService


@dataclass(frozen=True)
class PublicDependency(Dependency):
    """A concrete dependency for the source/service boundary; no resolution occurs."""


class FakeDependencySource:
    def __init__(self, dependency):
        self.dependency = dependency

    def collect(self):
        yield self.dependency


class FakeVulnerabilityService(VulnerabilityService):
    def __init__(self, vulnerabilities):
        self.vulnerabilities = vulnerabilities

    def query(self, dependency):
        return dependency, self.vulnerabilities

    def query_all(self, dependencies):
        for dependency in dependencies:
            yield self.query(dependency)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    examples = (
        (
            ("PYSEC-0", ("CVE-2024-0001",)),
            ("GHSA-aaaa", ("CVE-2024-0001", "GHSA-aaaa")),
        ),
        (
            ("PYSEC-0", ("CVE-2024-0001",)),
            ("GHSA-aaaa", ("CVE-2024-0001",)),
            ("CVE-2024-0001", ("GHSA-aaaa",)),
        ),
    )
    print("PUBLIC_2391_READY", flush=True)
    count = 0
    for example_number, example in enumerate(examples, start=1):
        for order_number, order in enumerate(permutations(example), start=1):
            label = f"example={example_number}, order={order_number}"
            records = [
                VulnerabilityResult(
                    id=identifier, description="public instruction example",
                    fix_versions=[], aliases=set(aliases),
                )
                for identifier, aliases in order
            ]
            dependency = PublicDependency(name="public-example")
            source = FakeDependencySource(dependency)
            service = FakeVulnerabilityService(records)
            results = list(Auditor(service, options=AuditOptions()).audit(source))
            require(len(results) == 1, f"{label}: expected one dependency result")
            result_dependency, vulnerabilities = results[0]
            require(result_dependency == dependency, f"{label}: dependency differs")
            require(len(vulnerabilities) == 1, f"{label}: expected one vulnerability")
            vulnerability = vulnerabilities[0]
            require(vulnerability.id == "PYSEC-0", f"{label}: PYSEC precedence fails")
            require(
                vulnerability.aliases == {"CVE-2024-0001", "GHSA-aaaa"},
                f"{label}: merged alias union differs",
            )
            require(
                vulnerability.id not in vulnerability.aliases,
                f"{label}: primary id appears in aliases",
            )
            count += 1
    print(f"PUBLIC_2391_CASES_PASSED={count}", flush=True)
    print("PUBLIC_2391_PASSED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"PUBLIC_2391_FAILED: {error}", flush=True)
        raise
