"""Unit tests for the code-harden-night census classifiers.

Deterministic: pure in-memory text in, rows out. No Docker, no network,
no clock.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "research/experiments/code-harden-night")
)

from census_lib import bucket_unknown_runner, classify_downloads, rust_path_status


def _modes(rows):
    return {(r["tool"], r["mode"]) for r in rows}


def test_bare_npm_install_is_unconditional():
    rows = classify_downloads("cd /testbed\nnpm install 2>&1 | tail -5\n")
    assert ("npm", "unconditional") in _modes(rows)


def test_guarded_npm_install_is_conditional():
    text = "if [ ! -x node_modules/.bin/tap ]; then\n  npm install 2>&1\nfi\n"
    assert _modes(classify_downloads(text)) == {("npm", "conditional")}


def test_diff_prefixed_guard_is_conditional():
    text = "+if [ ! -x node_modules/.bin/tap ]; then\n+  npm install 2>&1\n+fi\n"
    assert _modes(classify_downloads(text)) == {("npm", "conditional")}


def test_guard_does_not_leak_into_next_script():
    text = (
        "if [ ! -d node_modules ]; then\n  npm install\nfi\n"
        "diff --git a/other.sh b/other.sh\n"
        "go mod download\n"
    )
    modes = _modes(classify_downloads(text))
    assert ("npm", "conditional") in modes
    assert ("go-mod", "unconditional") in modes


def test_hermetic_flags_win():
    assert _modes(classify_downloads("GOPROXY=off GOFLAGS=-mod=readonly go test ./...\n")) == set()
    rows = classify_downloads("mvn -o test -pl java-surefire\n")
    assert ("maven", "offline-flag") in _modes(rows)
    rows = classify_downloads("cargo test --offline -p spider\n")
    assert ("cargo", "offline-flag") in _modes(rows)


def test_maven_without_offline_is_unconditional():
    rows = classify_downloads("mvn install -DskipTests\nmvn test -pl java-surefire\n")
    assert ("maven", "unconditional") in _modes(rows)


def test_composer_and_bundler_detected():
    text = "composer install --no-dev\nbundle install --local\n"
    tools = {r["tool"] for r in classify_downloads(text)}
    assert {"composer", "bundler"} <= tools


def test_comments_ignored():
    assert classify_downloads("# npm install would go here\n") == []


def test_rust_path_status():
    grading = "cargo test -p spacetimedb-testing\n"
    assert rust_path_status(grading, "export PATH=/usr/local/go/bin:$PATH\n") == (
        "path-missing-candidate"
    )
    assert rust_path_status(grading, "export PATH=/usr/local/cargo/bin:$PATH\n") == "ok"
    assert rust_path_status("go test ./...\n", "") == "not-rust"


def test_unknown_buckets_spot_checks():
    assert bucket_unknown_runner("julia --project=. test/runtests.jl") == "julia"
    assert bucket_unknown_runner("forge test --match-contract Foo") == "forge"
    assert bucket_unknown_runner("$VENV/bin/wake test") == "wake"
    assert bucket_unknown_runner("node_modules/.bin/tap packages/a/test.js") == "tap"
    assert bucket_unknown_runner("yarn jest tests/format --no-coverage") == "jest"
    assert bucket_unknown_runner("npx tsx ts/src/test/tests.init.ts") == "tsx"
    assert bucket_unknown_runner("cargo test --test graphql_spec") == "cargo-test"
    assert bucket_unknown_runner("exec bash usercase-test-coderl/usecase.sh") == "usercase"
    assert bucket_unknown_runner("mypy src/ --strict") == "lint-mypy"
    assert bucket_unknown_runner("nim c -r tests/all.nim") == "nim"
    assert bucket_unknown_runner("echo hello") == "custom"


def test_specific_runner_beats_generic_python():
    # A python one-liner inside a jest-graded task still buckets to jest.
    text = "python3 - <<'PY'\nprint('setup')\nPY\nyarn jest tests/x.spec.js\n"
    assert bucket_unknown_runner(text) == "jest"
