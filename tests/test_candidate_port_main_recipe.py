from __future__ import annotations

import ast
import hashlib
import io
import json
import shutil
import textwrap
import zipfile
from pathlib import Path

import pytest

from evallab import candidate_port as port
from evallab.upstream_grader import GraderRefusal

_HELPERS = r'''
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hub_url(path: str) -> str:
    return f"{HUB}/terminal-bench/{HUB_TASK}/{HUB_REV}/files/{path}"


def fetch(url: str, dest: Path, sha256: str, attempts: int = 5) -> None:
    if dest.is_file() and _sha256(dest) == sha256:
        return
    raise RuntimeError("the offline adapter must never reach a network branch")


def rewrite_app_paths(text: str) -> str:
    found = text.count(UPSTREAM_APP)
    if found != README_APP_PATHS:
        raise RuntimeError(f"expected {README_APP_PATHS} {UPSTREAM_APP} paths, found {found}")
    if "/workspace" in text.replace(UPSTREAM_APP, "") or "/shared/" in text:
        raise RuntimeError("unexpected upstream path in the legacy README")
    return text.replace(UPSTREAM_APP, STUDENT_APP)


def _lock_entries(path: Path) -> set[tuple[str, str, str]]:
    packages = json.loads(path.read_text()).get("packages", {})
    return {
        (key.rsplit("node_modules/", 1)[-1], entry["version"], entry.get("integrity", ""))
        for key, entry in packages.items()
        if key and "version" in entry
    }


def _pip_pins(text: str) -> set[str]:
    pins = set()
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;\\]+)", line)
        if m:
            pins.add(f"{re.sub(r'[-_.]+', '-', m.group(1)).lower()}=={m.group(2)}")
    return pins


def store_misses(app: Path, store: Path = STORE) -> list[str]:
    have: set[tuple[str, str, str]] = set()
    for name in NPM_LOCKS:
        for lock in (store / "npm").glob(f"*/{name}"):
            have |= _lock_entries(lock)
    misses = [
        f"npm {name}@{version}"
        for name, version, integrity in sorted(_lock_entries(app / "frontend" / "package-lock.json"))
        if (name, version, integrity) not in have
    ]
    wheels = set()
    for lock in (store / "pip").glob("*.txt"):
        wheels |= _pip_pins(lock.read_text())
    wanted = _pip_pins((app / "backend" / "requirements.txt").read_text())
    misses += [f"pip {pin}" for pin in sorted(wanted - wheels)]
    return misses
'''

_MAIN = '''
def main() -> None:
    shared = ROOT / "shared_data" / "legacy_app"
    root_data = ROOT / "root_data"
    upstream = root_data / "upstream"
    files: dict[str, tuple[Path, str]] = {}
    for path, sha in LEGACY_FILES.items():
        dest = upstream / "legacy_app" / path if path == LEGACY_README else shared / path
        files[f"environment/legacy_app/{path}"] = (dest, sha)
    for path, sha in TEST_FILES.items():
        files[f"tests/{path}"] = (root_data / "tests" / path, sha)
    for path, sha in SOLUTION_FILES.items():
        files[f"solution/{path}"] = (root_data / "solution" / path, sha)
    for path, sha in UPSTREAM_FILES.items():
        files[path] = (upstream / path, sha)
    for path, (dest, sha) in files.items():
        fetch(hub_url(path), dest, sha)
    readme = (upstream / "legacy_app" / LEGACY_README).read_text()
    (shared / LEGACY_README).write_text(rewrite_app_paths(readme))
    misses = store_misses(root_data / "solution" / "generated_app")
    if misses:
        raise RuntimeError(
            "the package store (offline_packages/) lacks what upstream's "
            f"solution installs: {', '.join(misses[:20])}"
        )
'''

_UNZIP = '''
def unzip_with_modes(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise RuntimeError(f"{archive}: unsafe path {info.filename!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"{archive}: symlink {info.filename!r}")
            target = dest.joinpath(*name.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if mode & 0o111 else 0o644)
    for d in (dest, *(p for p in dest.rglob("*") if p.is_dir())):
        d.chmod(0o755)
'''


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _setup_source(original: Path) -> str:
    def hashes(prefix: str, names: tuple[str, ...]) -> dict[str, str]:
        return {name: hashlib.sha256((original / prefix / name).read_bytes()).hexdigest() for name in names}

    maps = {
        "LEGACY_FILES": hashes("environment/legacy_app", ("README.md", "assets.csv")),
        "TEST_FILES": hashes("tests", ("fixture.json",)),
        "SOLUTION_FILES": hashes("solution", ("generated_app/frontend/package-lock.json", "generated_app/backend/requirements.txt")),
        "UPSTREAM_FILES": hashes("", ("instruction.md", "task.toml", "environment/Dockerfile")),
    }
    constants = '''
        ROOT = Path(__file__).resolve().parent
        HUB = "https://hub.harborframework.com/api/tasks"
        HUB_TASK = "native-task"
        HUB_REV = "4.0.0"
        LEGACY_README = "README.md"
        UPSTREAM_APP = "/workspace/generated_app"
        STUDENT_APP = "/workdir/generated_app"
        README_APP_PATHS = 3
        STORE = ROOT / "offline_packages"
        NPM_LOCKS = ("package-lock.json", "earlier-lock.json")
    '''
    return textwrap.dedent(constants) + "\n".join(f"{name} = {value!r}" for name, value in maps.items()) + _HELPERS + _MAIN


@pytest.fixture
def main_package(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original/native-task"
    fixed = tmp_path / "fixed/native-task"
    _put(original / "task.toml", '''
        schema_version = "1.0"
        artifacts = ["/app/result.txt"]
        [task]
        name = "terminal-bench/native-task"
        [agent]
        timeout_sec = 60.0
        [verifier]
        timeout_sec = 60.0
        [environment]
        cpus = 2
        memory_mb = 4096
        storage_mb = 10240
        gpus = 0
        build_timeout_sec = 600.0
    ''')
    _put(original / "instruction.md", "Build the requested application.\n")
    _put(original / "environment/Dockerfile", "FROM scratch\n")
    _put(original / "environment/legacy_app/README.md", "Paths: /workspace/generated_app /workspace/generated_app /workspace/generated_app\n")
    _put(original / "environment/legacy_app/assets.csv", "id,name\n1,example\n")
    _put(original / "tests/fixture.json", '{"synthetic_hidden_fixture": true}\n')
    wanted = {"packages": {
        "": {"name": "fixture", "version": "0.0.0"},
        "node_modules/@scope/ui": {"version": "1.2.3", "integrity": "sha512-synthetic-ui"},
        "node_modules/tool": {"version": "2.0.0", "integrity": "sha512-synthetic-tool"},
    }}
    _put(original / "solution/generated_app/frontend/package-lock.json", json.dumps(wanted))
    _put(original / "solution/generated_app/backend/requirements.txt", "Feature_Pkg[fast]==1.2.3\nother.pkg==2.0.0 \\\n    --hash=sha256:synthetic\n")
    _put(fixed / "Containerfile", "FROM scratch\nCOPY shared_data/ /shared/\nCOPY root_data/ /root_data/\nCOPY offline_packages/ /offline_packages/\n")
    _put(fixed / "pyproject.toml", '[project]\nname="environment"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=["karotte==3.0.75"]\n')
    _put(fixed / "uv.lock", 'version = 1\nrevision = 1\nrequires-python = ">=3.12"\n')
    _put(fixed / ".manifest.json", '{"karotte_version":"3.0.75"}\n')
    _put(fixed / "src/environment/tasks/native_task/__init__.py", '''
        from karotte import Task, Step
        from karotte.judges import ExecutableJudge
        class NativeTask(Task):
            id = "native-task"
            @property
            def required_hardware(self):
                return "cpus=2,memory_mb=4096,storage_mb=10240"
        class FileStep(Step):
            @property
            def judge(self):
                return ExecutableJudge(["/bin/false"])
    ''')
    for name, key in (("package-lock.json", "node_modules/@scope/ui"), ("earlier-lock.json", "node_modules/tool")):
        _put(fixed / "offline_packages/npm/fixture" / name, json.dumps({"packages": {key: wanted["packages"][key]}}))
    _put(fixed / "offline_packages/pip/backend.txt", "feature-pkg==1.2.3\nother-pkg==2.0.0\n")
    _put(fixed / "setup_data.py", _setup_source(original))
    return original, fixed


def test_main_materializes_maps_rewrites_and_keeps_hidden_inputs_private(main_package: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = main_package
    before = [port._tree_digest(path) for path in main_package]
    described = port.describe_port_task(original, fixed)
    prepared = port.prepare_port_task(original, fixed, tmp_path / "prepared")
    context = Path(prepared["task_dir"]) / "tests"
    rewritten = (context / "shared_data/legacy_app/README.md").read_text()
    assert rewritten.count("/workdir/generated_app") == 3
    assert "/workspace" not in rewritten
    assert (context / "root_data/upstream/legacy_app/README.md").read_bytes() == (original / "environment/legacy_app/README.md").read_bytes()
    assert (context / "root_data/tests/fixture.json").read_bytes() == (original / "tests/fixture.json").read_bytes()
    assert (context / "root_data/solution/generated_app/frontend/package-lock.json").read_bytes() == (original / "solution/generated_app/frontend/package-lock.json").read_bytes()
    visible = {path.relative_to(context / "shared_data").as_posix() for path in (context / "shared_data").rglob("*") if path.is_file()}
    assert visible == {"legacy_app/README.md", "legacy_app/assets.csv"}
    assets = described["setup_data"]["assets"]
    for asset in assets:
        assert hashlib.sha256((context / asset["destination"]).read_bytes()).hexdigest() == asset["sha256"]
    generated = {entry["destination"]: entry for entry in described["setup_data"]["generated"]}
    assert generated["shared_data/legacy_app/README.md"]["sha256"] == hashlib.sha256(rewritten.encode()).hexdigest()
    assert [port._tree_digest(path) for path in main_package] == before


@pytest.mark.parametrize("readme", [
    "/workspace/generated_app " * 2,
    "/workspace/generated_app " * 4,
    "/workspace/generated_app " * 3 + "/workspace/other",
    "/workspace/generated_app " * 3 + "/shared/private",
])
def test_main_preserves_exact_readme_rewrite_checks(main_package: tuple[Path, Path], tmp_path: Path, readme: str) -> None:
    original, fixed = main_package
    _put(original / "environment/legacy_app/README.md", readme)
    _put(fixed / "setup_data.py", _setup_source(original))
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal, match="expected 3|unexpected upstream path"):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


@pytest.mark.parametrize("missing", ["npm-version", "npm-integrity", "pip", "store"])
def test_main_never_omits_offline_package_completeness(main_package: tuple[Path, Path], tmp_path: Path, missing: str) -> None:
    original, fixed = main_package
    if missing.startswith("npm"):
        lock = fixed / "offline_packages/npm/fixture/package-lock.json"
        data = json.loads(lock.read_text())
        field = "version" if missing == "npm-version" else "integrity"
        data["packages"]["node_modules/@scope/ui"][field] = "different"
        _put(lock, json.dumps(data))
    elif missing == "pip":
        _put(fixed / "offline_packages/pip/backend.txt", "feature-pkg==1.2.4\nother-pkg==2.0.0\n")
    else:
        shutil.rmtree(fixed / "offline_packages")
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal, match="package store .*lacks.*(npm|pip)"):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


@pytest.mark.parametrize("malformed", ["not-json", {"packages": []}, {"packages": {"node_modules/bad": {"version": []}}}])
def test_main_malformed_dependency_locks_fail_closed(main_package: tuple[Path, Path], tmp_path: Path, malformed: object) -> None:
    original, fixed = main_package
    text = malformed if isinstance(malformed, str) else json.dumps(malformed)
    _put(original / "solution/generated_app/frontend/package-lock.json", text)
    _put(fixed / "setup_data.py", _setup_source(original))
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


@pytest.mark.parametrize("replacement", [
    'NPM_LOCKS = ("../outside.json",)',
    'NPM_LOCKS = ("**",)',
    'STORE = ROOT / "../outside"',
])
def test_main_package_globs_cannot_escape_pinned_context(main_package: tuple[Path, Path], tmp_path: Path, replacement: str) -> None:
    original, fixed = main_package
    source = _setup_source(original)
    target = 'STORE = ROOT / "offline_packages"' if replacement.startswith("STORE") else 'NPM_LOCKS = ("package-lock.json", "earlier-lock.json")'
    _put(fixed / "setup_data.py", source.replace(target, replacement))
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


def test_main_package_store_symlink_cannot_read_host_files(main_package: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = main_package
    host = tmp_path / "outside.txt"
    host.write_text("feature-pkg==1.2.3\nother-pkg==2.0.0\n")
    lock = fixed / "offline_packages/pip/backend.txt"
    lock.unlink()
    lock.symlink_to(host)
    with pytest.raises(GraderRefusal):
        port.describe_port_task(original, fixed)


@pytest.mark.parametrize("change", ["destination", "source", "fetch-check", "hash-check", "unsupported"])
def test_main_never_falls_back_to_tables_or_skips_helper_checks(main_package: tuple[Path, Path], tmp_path: Path, change: str) -> None:
    original, fixed = main_package
    source = _setup_source(original)
    if change == "destination":
        source = source.replace('ROOT / "shared_data"', 'ROOT / "../escaped"')
    elif change == "source":
        source = source.replace('f"environment/legacy_app/{path}"', 'f"environment/../{path}"')
    elif change == "fetch-check":
        source = source.replace('    if dest.is_file()', '    assert False, "mandatory fetch safety check"\n    if dest.is_file()')
    elif change == "hash-check":
        source = source.replace('    h = hashlib.sha256()', '    assert False, "mandatory hash safety check"\n    h = hashlib.sha256()')
    else:
        source += '\n    subprocess.run(["must-not-execute"])\n'
    _put(fixed / "setup_data.py", source)
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


def test_main_package_context_changes_bound_grader_identity(main_package: tuple[Path, Path]) -> None:
    original, fixed = main_package
    first = port.describe_port_task(original, fixed)
    lock = fixed / "offline_packages/pip/backend.txt"
    lock.write_text(lock.read_text() + "extra-unused-package==9.0.0\n")
    second = port.describe_port_task(original, fixed)
    assert first["port_identity"]["exact_tree_digest"] != second["port_identity"]["exact_tree_digest"]
    assert first["grader_digest"] != second["grader_digest"]


def test_package_store_never_discovers_an_ambient_context(main_package: tuple[Path, Path]) -> None:
    original, fixed = main_package
    with pytest.raises(GraderRefusal, match="explicit pinned port context"):
        port._SetupRecipe(ast.parse((fixed / "setup_data.py").read_text()), "native-task", upstream=original)


def test_main_refuses_changed_pinned_input_before_any_rewrite(main_package: tuple[Path, Path], tmp_path: Path) -> None:
    original, fixed = main_package
    _put(original / "environment/legacy_app/README.md", "/workspace/generated_app " * 3 + "changed")
    output = tmp_path / "refused"
    with pytest.raises(GraderRefusal):
        port.prepare_port_task(original, fixed, output)
    assert not output.exists()


def test_main_requires_a_real_no_argument_entrypoint(main_package: tuple[Path, Path]) -> None:
    original, fixed = main_package
    _put(fixed / "setup_data.py", _setup_source(original).replace("def main()", "def main(unused)"))
    with pytest.raises(GraderRefusal, match="no-argument offline main"):
        port.describe_port_task(original, fixed)


def _zip_recipe(tmp_path: Path, helper: str, *, symlink: bool = False) -> port._SetupRecipe:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        entry = zipfile.ZipInfo("tool/run")
        entry.external_attr = (0o120777 if symlink else 0o100755) << 16
        bundle.writestr(entry, "outside-target" if symlink else "executable")
        bundle.writestr("tool/data.txt", "immutable")
        bundle.writestr("empty/", "")
    original = tmp_path / "original"
    original.mkdir()
    (original / "bundle.zip").write_bytes(buffer.getvalue())
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()
    setup = f'''
def setup(ctx):
    archive = ctx.root_data / "bundle.zip"
    ctx.fetch_hub("native-task", "4.0.0", {{"bundle.zip": (archive, {digest!r})}})
    unzip_with_modes(archive, ctx.intermediate_data / "tools")
'''
    return port._SetupRecipe(ast.parse(helper + setup), "native-task", upstream=original)


def test_exact_archive_helper_preserves_files_modes_and_empty_directories(tmp_path: Path) -> None:
    recipe = _zip_recipe(tmp_path, _UNZIP)
    assert recipe.generated["intermediate_data/tools/tool/run"]["content"] == b"executable"
    assert recipe.generated["intermediate_data/tools/tool/run"]["mode"] == 0o755
    assert recipe.generated["intermediate_data/tools/tool/data.txt"]["mode"] == 0o644
    assert recipe.directories["intermediate_data/tools/empty"] == 0o755
    assert recipe.directories["intermediate_data/tools/tool"] == 0o755


def test_archive_helper_added_assertion_is_not_silently_skipped(tmp_path: Path) -> None:
    helper = _UNZIP.replace('    with zipfile.ZipFile', '    assert False, "mandatory archive safety check"\n    with zipfile.ZipFile')
    with pytest.raises(GraderRefusal, match="digest check failed"):
        _zip_recipe(tmp_path, helper)


def test_pinned_archive_refuses_symlink_members(tmp_path: Path) -> None:
    with pytest.raises(GraderRefusal, match="symlink member"):
        _zip_recipe(tmp_path, _UNZIP, symlink=True)
