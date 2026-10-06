#!/bin/bash
# Oracle reference solution for format-code-task-001269 (responses strict activate).
# Runs in the agent environment; stdout goes to the trial's agent/oracle.txt log.
set -u
cd /testbed

echo "=== separate-verifier agent-env probe ==="
echo "--- ls /tests:"
ls -la /tests 2>&1
echo "--- marker grep (test_activate_strict_unfired_raises) over workspace:"
grep -rl "test_activate_strict_unfired_raises" "/testbed" /tmp \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv \
  --exclude-dir=node_modules 2>/dev/null
echo "grep_rc=$?"

echo "=== oracle fix: strict activate decorator ==="
python3 - <<'PYEOF'
from pathlib import Path

path = Path("/testbed/responses/__init__.py")
src = path.read_text(encoding="utf-8")

old_activate = """    def activate(self, func=None, registry=None):
        if func is not None:
            return get_wrapped(func, self)

        def deco_activate(function):
            return get_wrapped(function, self, registry)

        return deco_activate
"""
new_activate = """    def activate(self, func=None, registry=None, assert_all_requests_are_fired=None):
        if func is not None:
            return get_wrapped(func, self)

        def deco_activate(function):
            return get_wrapped(
                function, self, registry, assert_all_requests_are_fired
            )

        return deco_activate
"""
assert src.count(old_activate) == 1, "activate block not found exactly once"
src = src.replace(old_activate, new_activate)

old_sig = '''def get_wrapped(
    func: Callable[..., Any], responses: "RequestsMock", registry: Optional[Any] = None
) -> Callable[..., Any]:'''
new_sig = '''def get_wrapped(
    func: Callable[..., Any],
    responses: "RequestsMock",
    registry: Optional[Any] = None,
    assert_all_requests_are_fired: Optional[bool] = None,
) -> Callable[..., Any]:'''
assert src.count(old_sig) == 1, "get_wrapped signature not found exactly once"
src = src.replace(old_sig, new_sig)

old_async = """    if inspect.iscoroutinefunction(func):
        # set asynchronous wrapper if requestor function is asynchronous
        @wraps(func)
        async def wrapper(*args, **kwargs):
            with responses:
                return await func(*args, **kwargs)

    else:

        @wraps(func)
        def wrapper(*args, **kwargs):
            with responses:
                return func(*args, **kwargs)

    return wrapper"""
new_async = """    if inspect.iscoroutinefunction(func):
        # set asynchronous wrapper if requestor function is asynchronous
        @wraps(func)
        async def wrapper(*args, **kwargs):
            original = responses.assert_all_requests_are_fired
            if assert_all_requests_are_fired is not None:
                responses.assert_all_requests_are_fired = (
                    assert_all_requests_are_fired
                )
            try:
                with responses:
                    return await func(*args, **kwargs)
            finally:
                responses.assert_all_requests_are_fired = original

    else:

        @wraps(func)
        def wrapper(*args, **kwargs):
            original = responses.assert_all_requests_are_fired
            if assert_all_requests_are_fired is not None:
                responses.assert_all_requests_are_fired = (
                    assert_all_requests_are_fired
                )
            try:
                with responses:
                    return func(*args, **kwargs)
            finally:
                responses.assert_all_requests_are_fired = original

    return wrapper"""
assert src.count(old_async) == 1, "get_wrapped body not found exactly once"
src = src.replace(old_async, new_async)

path.write_text(src, encoding="utf-8")
print("patched responses/__init__.py")
PYEOF

echo "=== oracle smoke check ==="
python3 - <<'PYEOF'
import responses
import requests

@responses.activate(assert_all_requests_are_fired=True)
def strict_unfired():
    responses.add(responses.GET, "http://example.com", body=b"test")

try:
    strict_unfired()
except AssertionError as exc:
    assert "http://example.com" in str(exc), str(exc)
else:
    raise SystemExit("strict unfired did not raise")

@responses.activate
def bare():
    responses.add(responses.GET, "http://example.com", body=b"test")

bare()
assert responses.mock.assert_all_requests_are_fired is False
print("oracle smoke check passed")
PYEOF
