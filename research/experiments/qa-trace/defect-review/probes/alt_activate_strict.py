"""Append a prompt-compliant ``activate_strict`` decorator to responses/__init__.py.

Run inside the task container (cwd = repo root): the separate-decorator option
the 001269 prompt offers, built on the module's own default mock.
"""

from pathlib import Path

ADDITION = '''

def activate_strict(func):
    """Like ``activate``, but fails if a registered response was never requested."""
    from functools import wraps as _wraps

    @_wraps(func)
    def wrapper(*args, **kwargs):
        previous = _default_mock.assert_all_requests_are_fired
        _default_mock.assert_all_requests_are_fired = True
        try:
            return activate(func)(*args, **kwargs)
        finally:
            _default_mock.assert_all_requests_are_fired = previous

    return wrapper
'''

target = Path("responses/__init__.py")
target.write_text(target.read_text() + ADDITION)
print("added responses.activate_strict")
