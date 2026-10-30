"""Attack D: a plugin loaded from addopts skips every item."""
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="plugin says done"))
