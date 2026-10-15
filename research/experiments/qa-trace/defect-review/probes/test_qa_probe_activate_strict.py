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


def test_strict_decorator_raises_when_a_registered_request_never_fires():
    @responses.activate_strict
    def run():
        responses.add(responses.GET, "http://example.com", body=b"test")

    with pytest.raises(AssertionError):
        run()
    responses.mock.reset()


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
