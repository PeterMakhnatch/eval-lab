"""Upstream-fetch leak guard: detector and trial extraction."""

from __future__ import annotations

from typing import Any

from evallab.upstream_fetch import detect_upstream_fetch


def _kinds(commands: list[str], **kwargs: Any) -> list[tuple[str, bool]]:
    return [
        (finding.kind, finding.names_task_repo)
        for finding in detect_upstream_fetch([(1, cmd) for cmd in commands], **kwargs)
    ]


def test_pip_download_of_named_package_is_flagged() -> None:
    kinds = _kinds(
        ["cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr 2>&1 | tail -2"],
        task_repo="Pylons/waitress",
    )
    assert kinds == [("pip-download-remote-package", True)]


def test_pip_download_with_binary_flags_is_flagged() -> None:
    kinds = _kinds(["pip download soupsieve==1.9.1 --no-deps --no-binary :all: -d /tmp/sv2"])
    assert [kind for kind, _ in kinds] == ["pip-download-remote-package"]


def test_pip_install_bare_name_is_flagged() -> None:
    assert _kinds(["pip install control==0.9.3"]) == [("pip-install-remote-package", False)]


def test_uv_pip_and_python_m_pip_are_flagged() -> None:
    assert [kind for kind, _ in _kinds(["uv pip install numpy==1.26"])] == [
        "uv-pip-install-remote-package"
    ]
    assert [kind for kind, _ in _kinds(["python -m pip install requests"])] == [
        "pip-install-remote-package"
    ]


def test_pip_index_query_is_flagged() -> None:
    assert [kind for kind, _ in _kinds(["pip index versions waitress"])] == ["pip-index-query"]


def test_remote_requirements_file_is_flagged() -> None:
    kinds = _kinds(["pip install -r https://example.com/reqs.txt"])
    assert [kind for kind, _ in kinds] == ["pip-requirements-url"]


def test_curl_and_wget_of_remote_urls_are_flagged() -> None:
    kinds = _kinds(
        ["timeout 10 curl -sL https://raw.githubusercontent.com/o/p/master/f.py -o /tmp/f.py"],
        task_repo="o/p",
    )
    assert kinds == [("curl-remote-url", True)]
    assert [kind for kind, _ in _kinds(["wget -q https://pypi.org/pypi/x/json"])] == [
        "wget-remote-url"
    ]


def test_git_clone_of_remote_url_is_flagged() -> None:
    kinds = _kinds(
        ["git clone https://github.com/Pylons/waitress.git /tmp/w"],
        task_repo="Pylons/waitress",
    )
    assert kinds == [("git-clone-remote-url", True)]


def test_python_c_fetch_is_flagged() -> None:
    kinds = _kinds(
        [
            "python -c \"import urllib.request; "
            "print(urllib.request.urlopen('https://pypi.org/pypi/waitress/json').read()[:10])\""
        ],
        task_repo="Pylons/waitress",
    )
    assert kinds == [("python-remote-fetch", True)]


def test_module_mention_plus_url_without_fetch_call_is_clean() -> None:
    """An edit script quoting a urllib URL (HAR-104 step 8 shape) is not a fetch."""
    script = (
        "python - <<'EOF'\n"
        "with open('waitress/parser.py') as f:\n"
        "    content = f.read()\n"
        "# see https://github.com/python/cpython/blob/aaaa/Lib/urllib/parse.py#L1\n"
        "# see https://github.com/Pylons/waitress/issues/260\n"
        "print('ok')\n"
        "EOF"
    )
    assert _kinds([script], task_repo="Pylons/waitress") == []
    assert _kinds(["python -c \"import socket; print('https://example.com/x')\""]) == []

def test_python_heredoc_fetch_is_flagged() -> None:
    kinds = _kinds(
        ["python - <<'EOF'\nimport requests\nprint(requests.get('https://example.com/x').text)\nEOF"]
    )
    assert [kind for kind, _ in kinds] == ["python-remote-fetch"]


def test_node_fetch_is_flagged() -> None:
    assert [kind for kind, _ in _kinds(["node -e \"fetch('https://example.com/x').then(r=>r.text())\""])] == [
        "node-remote-fetch"
    ]


def test_apt_source_is_flagged() -> None:
    assert [kind for kind, _ in _kinds(["apt-get source python3"])] == ["apt-source"]


def test_informational_commands_are_not_findings() -> None:
    quiet = [
        "pip show soupsieve",
        "pip list --format=freeze | head",
        "pip install -e .",
        "pip install -r requirements.txt",
        "pip install ./dist/foo-1.0-py3-none-any.whl",
        "python -m pytest tests/ -x -q",
        "git log --oneline -3 && git diff HEAD --stat",
        "grep -rn 'https://example.com' src/ | head -20",
        "cat /workspace/repo/pip_audit/_audit.py",
        "curl http://localhost:8000/health",
        "git clone /srv/mirror/pkg",
        "python -c \"print('no network here')\"",
    ]
    assert _kinds(quiet) == []


def test_no_index_local_install_is_not_a_finding() -> None:
    assert _kinds(["pip install --no-index --find-links /wheels foo"]) == []


def test_names_task_repo_matches_package_and_url() -> None:
    assert _kinds(["pip download control==0.9.3"], task_repo="python-control/control") == [
        ("pip-download-remote-package", True)
    ]
    assert _kinds(["pip download unrelated==1.0"], task_repo="python-control/control") == [
        ("pip-download-remote-package", False)
    ]


