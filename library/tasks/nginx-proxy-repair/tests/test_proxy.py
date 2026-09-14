"""Verifier for nginx-proxy-repair.

Grades exported on-disk nginx configuration in a fresh verifier container:
the agent's /etc/nginx, /var/www/app, and /opt/backend/app.py are collected
as artifacts and re-materialized at their original paths. Starts a trusted
backend copy with a fresh nonce, then nginx, and probes routes.
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

NGINX_PORT = 80
BACKEND_PORT = 8081
NONCE = secrets.token_hex(8)
AGENT_BACKEND = "/opt/backend/app.py"
VERIFIER_BACKEND = "/tests/backend.py"
BACKEND_LOG = "/logs/verifier/backend.log"
INDEX_FILE = "/var/www/app/index.html"
STATIC_DIR = "/var/www/app/static"
STATIC_CSS = "/var/www/app/static/style.css"


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port, want_open, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port) == want_open:
            return True
        time.sleep(0.1)
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _get(path, port=NGINX_PORT):
    """Return the first local HTTP response, never a client-followed redirect."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with HTTP.open(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


class Services:
    def __init__(self):
        self.nginx_test = None

    def stop_everything(self):
        subprocess.run(["nginx", "-s", "quit"], capture_output=True)
        subprocess.run(["fuser", "-k", f"{NGINX_PORT}/tcp"], capture_output=True)
        subprocess.run(["fuser", "-k", f"{BACKEND_PORT}/tcp"], capture_output=True)
        assert _wait_port(NGINX_PORT, want_open=False), "could not free port 80"
        assert _wait_port(BACKEND_PORT, want_open=False), "could not free port 8081"

    def start_backend(self):
        with open(BACKEND_LOG, "ab") as log:
            subprocess.Popen(
                ["python3", VERIFIER_BACKEND],
                env={"BACKEND_PORT": str(BACKEND_PORT), "BACKEND_NONCE": NONCE},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        assert _wait_port(BACKEND_PORT, want_open=True), "verifier backend did not start"

    def start_nginx(self):
        self.nginx_test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if self.nginx_test.returncode != 0:
            return
        subprocess.run(["nginx"], capture_output=True)
        _wait_port(NGINX_PORT, want_open=True)


@pytest.fixture(scope="session")
def services():
    svc = Services()
    svc.stop_everything()
    svc.start_backend()
    svc.start_nginx()
    return svc


def test_backend_untouched():
    assert not os.path.islink(AGENT_BACKEND), (
        "/opt/backend/app.py is a symlink; the backend is off limits"
    )
    assert os.path.isfile(AGENT_BACKEND), (
        "/opt/backend/app.py is missing; the backend is off limits"
    )
    assert _sha256(AGENT_BACKEND) == _sha256(VERIFIER_BACKEND), (
        "/opt/backend/app.py was modified; the backend is off limits"
    )


def test_nginx_config_valid(services):
    result = services.nginx_test
    assert result.returncode == 0, f"nginx -t failed:\n{result.stderr}"


def test_nginx_listening(services):
    assert _port_open(NGINX_PORT), "nginx is not listening on port 80"


def test_root_serves_index(services):
    assert os.path.isfile(INDEX_FILE), f"{INDEX_FILE} is missing from submitted files"
    with open(INDEX_FILE, "rb") as fh:
        expected_index = fh.read()
    status, headers, body = _get("/")
    assert status == 200, f"GET / returned {status}"
    assert headers.get("Content-Type", "").startswith("text/html"), (
        f"GET / Content-Type was {headers.get('Content-Type')!r}, expected text/html"
    )
    assert body == expected_index, (
        "served / body does not match submitted on-disk /var/www/app/index.html"
    )


def test_static_css_served(services):
    assert os.path.isfile(STATIC_CSS), f"{STATIC_CSS} is missing from submitted files"
    with open(STATIC_CSS, "rb") as fh:
        expected_css = fh.read()
    status, headers, body = _get("/static/style.css")
    assert status == 200, f"/static/style.css returned {status}"
    assert headers.get("Content-Type", "").startswith("text/css"), (
        f"/static/style.css Content-Type was {headers.get('Content-Type')!r}, expected text/css"
    )
    assert body == expected_css, (
        "served /static/style.css does not match submitted on-disk style.css"
    )


def test_static_probes_and_nested_paths(services):
    assert os.path.isdir(STATIC_DIR), f"{STATIC_DIR} directory is missing"

    # Probe 1: Fresh randomly named file in /var/www/app/static/
    probe_name = f"probe_{secrets.token_hex(6)}.txt"
    probe_file = os.path.join(STATIC_DIR, probe_name)
    probe_content = f"static-probe-{secrets.token_hex(16)}\n".encode()
    with open(probe_file, "wb") as fh:
        fh.write(probe_content)

    status, _, body = _get(f"/static/{probe_name}")
    assert status == 200, f"/static/{probe_name} returned {status}"
    assert body == probe_content, (
        f"served /static/{probe_name} does not match fresh probe file on disk"
    )

    # Probe 2: Fresh randomly named file in a nested directory
    nested_dir_name = f"sub_{secrets.token_hex(4)}"
    nested_dir = os.path.join(STATIC_DIR, nested_dir_name)
    os.makedirs(nested_dir, exist_ok=True)
    nested_name = f"nested_{secrets.token_hex(6)}.txt"
    nested_file = os.path.join(nested_dir, nested_name)
    nested_content = f"nested-token-{secrets.token_hex(16)}\n".encode()
    with open(nested_file, "wb") as fh:
        fh.write(nested_content)

    status, _, body = _get(f"/static/{nested_dir_name}/{nested_name}")
    assert status == 200, f"/static/{nested_dir_name}/{nested_name} returned {status}"
    assert body == nested_content, (
        f"served /static/{nested_dir_name}/{nested_name} does not match nested file on disk"
    )

    # Clearly reject missing files
    missing_name = f"missing_{secrets.token_hex(8)}.txt"
    status, _, _ = _get(f"/static/{missing_name}")
    assert status == 404, (
        f"GET /static/{missing_name} returned {status}, expected 404 for missing file"
    )

    missing_nested = f"{nested_dir_name}/missing_{secrets.token_hex(8)}.txt"
    status, _, _ = _get(f"/static/{missing_nested}")
    assert status == 404, (
        f"GET /static/{missing_nested} returned {status}, expected 404 for missing nested file"
    )


def test_api_health_reaches_live_backend(services):
    status, headers, body = _get("/api/health")
    assert status == 200, f"/api/health returned {status}: {body[:200]!r}"
    assert headers.get("Content-Type", "").startswith("application/json")
    payload = json.loads(body)
    assert payload.get("status") == "ok"
    assert payload.get("nonce") == NONCE, (
        "health response did not come from the backend started by the grader"
    )


@pytest.mark.parametrize(
    "query", ["", "?limit=0", "?limit=2", "?limit=99", "?limit=invalid", "?%6cimit=1"]
)
def test_api_items_passes_query_string(services, query):
    path = f"/api/items{query}"
    expected_status, expected_headers, expected_body = _get(path, port=BACKEND_PORT)
    assert expected_status == 200, "trusted backend items probe failed"
    status, headers, body = _get(path)
    assert status == expected_status
    assert headers.get("Content-Type") == expected_headers["Content-Type"]
    assert body == expected_body, (
        f"{path} must preserve the live backend's complete item response, including IDs"
    )


def test_api_unknown_route_is_backend_404(services):
    status, headers, body = _get("/api/does-not-exist")
    assert status == 404
    assert headers.get("Content-Type", "").startswith("application/json")
    assert json.loads(body) == {"error": "not found"}, (
        "backend 404 body must reach the client unchanged"
    )
