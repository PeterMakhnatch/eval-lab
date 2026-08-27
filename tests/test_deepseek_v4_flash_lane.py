from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deepseek-v4-flash-lane"
SECRET_SENTINEL = "compromised-secret-must-not-appear"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _fake_runtime(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_path = tmp_path / "harbor-args"
    env_path = tmp_path / "harbor-env"
    _write_executable(
        bin_dir / "docker",
        '#!/bin/bash\nif [ "${1:-}" = info ]; then exit 0; fi\nexit 0\n',
    )
    _write_executable(
        bin_dir / "harbor",
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_FILE"\n'
        'if [ -n "${DEEPSEEK_API_KEY:-}" ]; then deepseek=set; else deepseek=unset; fi\n'
        'if [ -n "${MSWEA_API_KEY:-}" ]; then mswea=set; else mswea=unset; fi\n'
        'printf \'DEEPSEEK_API_KEY=%s\\n\' "$deepseek" > "$HARBOR_ENV_FILE"\n'
        'printf \'MSWEA_API_KEY=%s\\n\' "$mswea" >> "$HARBOR_ENV_FILE"\n',
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HARBOR_ARGS_FILE": str(args_path),
            "HARBOR_ENV_FILE": str(env_path),
        }
    )
    return env, args_path, env_path


def _run(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_probe_reports_presence_without_secret_material(tmp_path: Path) -> None:
    env, _, _ = _fake_runtime(tmp_path)
    env["DEEPSEEK_API_KEY"] = SECRET_SENTINEL
    env["EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED"] = "present"

    result = _run("probe", env)

    assert result.returncode == 0
    assert "Harbor CLI: available" in result.stdout
    assert "Docker daemon: reachable" in result.stdout
    assert "DEEPSEEK_API_KEY: set" in result.stdout
    assert "EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED: set" in result.stdout
    assert SECRET_SENTINEL not in result.stdout
    assert SECRET_SENTINEL not in result.stderr


def test_canary_refuses_without_fresh_key_confirmation(tmp_path: Path) -> None:
    env, args_path, _ = _fake_runtime(tmp_path)
    env["DEEPSEEK_API_KEY"] = SECRET_SENTINEL
    env.pop("EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED", None)

    result = _run("canary", env)

    assert result.returncode != 0
    assert "EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED is unset" in result.stderr
    assert SECRET_SENTINEL not in result.stderr
    assert not args_path.exists()


def test_install_smoke_strips_model_credentials(tmp_path: Path) -> None:
    env, args_path, env_path = _fake_runtime(tmp_path)
    env["DEEPSEEK_API_KEY"] = SECRET_SENTINEL
    env["MSWEA_API_KEY"] = SECRET_SENTINEL

    result = _run("install-smoke", env)

    assert result.returncode == 0
    args = args_path.read_text().splitlines()
    assert "--install-only" in args
    assert _value_after(args, "--agent") == "mini-swe-agent"
    assert _value_after(args, "--model") == "deepseek/deepseek-v4-flash"
    assert _value_after(args, "--n-attempts") == "1"
    assert _value_after(args, "--n-tasks") == "1"
    assert env_path.read_text().splitlines() == [
        "DEEPSEEK_API_KEY=unset",
        "MSWEA_API_KEY=unset",
    ]
    assert SECRET_SENTINEL not in args_path.read_text()


def test_canary_is_exactly_one_bounded_trial_and_keeps_key_out_of_argv(
    tmp_path: Path,
) -> None:
    env, args_path, env_path = _fake_runtime(tmp_path)
    env["DEEPSEEK_API_KEY"] = SECRET_SENTINEL
    env["MSWEA_API_KEY"] = SECRET_SENTINEL
    env["EVALLAB_DEEPSEEK_FRESH_KEY_CONFIRMED"] = "present"

    result = _run("canary", env)

    assert result.returncode == 0
    args = args_path.read_text().splitlines()
    assert args[0] == "run"
    assert _value_after(args, "--path") == str(ROOT / "library/tasks/transaction-reconciliation")
    assert _value_after(args, "--agent") == "mini-swe-agent"
    assert _value_after(args, "--model") == "deepseek/deepseek-v4-flash"
    assert _value_after(args, "--env") == "docker"
    assert _value_after(args, "--n-attempts") == "1"
    assert _value_after(args, "--n-concurrent") == "1"
    assert _value_after(args, "--n-concurrent-agents") == "1"
    assert _value_after(args, "--n-tasks") == "1"
    assert _value_after(args, "--max-retries") == "0"
    assert "cost_limit=2.5" in args
    assert "max_tokens=8192" in args
    assert "api.deepseek.com" in args
    assert "--install-only" not in args
    assert "--print-config" not in args
    assert SECRET_SENTINEL not in args_path.read_text()
    assert env_path.read_text().splitlines() == [
        "DEEPSEEK_API_KEY=set",
        "MSWEA_API_KEY=unset",
    ]
