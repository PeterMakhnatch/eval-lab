"""Timing seams for the OpenRouter and self-hosted MiMo subprocess tests.

Run the unchanged proxy entrypoint in a real child with the real runner env.
Only shorten the stdlib server's idle poll and, when requested, inject the
proxy's existing retry sleeper/clock. Request handling and SIGTERM drain stay real.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from evallab import runner as runner_module

_PROXY_SOURCE = Path(__file__).resolve().parents[1] / "containers/zai_openapi_secret_proxy.py"


def _write_bootstrap(directory: Path, *, virtual_retries: bool = False) -> Path:
    script = directory / "proxy-timing.py"
    script.write_text(
        "import runpy, socketserver, sys\n"
        "serve_forever = socketserver.BaseServer.serve_forever\n"
        "def fast_serve(self, poll_interval=0.5):\n"
        + (
            "    module = sys.modules[type(self).__module__]\n"
            "    open_upstream = module._open_upstream\n"
            "    def virtual_open(*args, **kwargs):\n"
            "        now = [0.0]\n"
            "        def sleep(delay):\n"
            "            now[0] += delay\n"
            "        return open_upstream(*args, **kwargs, sleeper=sleep, clock=lambda: now[0])\n"
            "    module._open_upstream = virtual_open\n"
            if virtual_retries else ""
        )
        + "    return serve_forever(self, poll_interval=min(poll_interval, 0.01))\n"
        "socketserver.BaseServer.serve_forever = fast_serve\n"
        f"runpy.run_path({str(_PROXY_SOURCE)!r}, run_name='__main__')\n"
    )
    return script


@pytest.fixture(scope="module", autouse=True)
def fast_proxy_poll(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    script = _write_bootstrap(tmp_path_factory.mktemp("proxy-timing"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner_module, "ZAI_OPENAPI_PROXY_SCRIPT", str(script))
        yield


def use_virtual_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real 2 + 4 second schedule without wall-clock sleep."""
    script = _write_bootstrap(tmp_path, virtual_retries=True)
    monkeypatch.setattr(runner_module, "ZAI_OPENAPI_PROXY_SCRIPT", str(script))
