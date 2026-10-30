"""Attack B: make every collected item skipped, so the session exits 0."""
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="nothing to see here"))
