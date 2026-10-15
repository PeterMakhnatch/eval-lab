"""QA Trace defect probe for format-code-task-001269 (responses strict activate).

The prompt asks for "a strict version of 'activate' decorator": "Similar
decorator with `assert_all_requests_are_fired=True`, or a possibility to turn
it on". ``alt_activate_strict.py`` implements the first option as a separate
``responses.activate_strict`` decorator. These tests show that design does
what the prompt asks; the hidden tests are then run against the same tree.
"""

import pytest
import requests
import responses


@pytest.fixture(autouse=True)
def isolated_default_mock():
    """Check restoration before cleanup, and never contaminate another test."""
    initial = responses.mock.assert_all_requests_are_fired
    assert initial is False, "the task's bare activate must start lenient"
    responses.mock.reset()
    try:
        yield
        assert responses.mock.assert_all_requests_are_fired is initial
    finally:
        responses.mock.reset()
        responses.mock.assert_all_requests_are_fired = initial


def test_strict_decorator_raises_when_a_registered_request_never_fires():
    @responses.activate_strict
    def run():
        responses.add(responses.GET, "http://example.com", body=b"test")

    with pytest.raises(AssertionError):
        run()
    assert responses.mock.assert_all_requests_are_fired is False


def test_strict_decorator_passes_when_every_request_fires():
    @responses.activate_strict
    def run():
        responses.add(responses.GET, "http://example.com", body=b"test")
        assert requests.get("http://example.com").content == b"test"

    run()


def test_bare_activate_stays_lenient_after_a_strict_run():
    @responses.activate_strict
    def strict():
        responses.add(responses.GET, "http://example.com", body=b"test")
        requests.get("http://example.com")

    @responses.activate
    def lenient():
        responses.add(responses.GET, "http://example.com", body=b"test")

    strict()
    lenient()


def test_failed_strict_run_also_restores_bare_activate():
    @responses.activate_strict
    def strict():
        responses.add(responses.GET, "http://example.com", body=b"test")

    @responses.activate
    def lenient():
        responses.add(responses.GET, "http://example.com", body=b"test")

    lenient()
    with pytest.raises(AssertionError):
        strict()
    assert responses.mock.assert_all_requests_are_fired is False
    lenient()
