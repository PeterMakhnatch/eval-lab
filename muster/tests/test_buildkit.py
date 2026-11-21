"""The task build kit: the Compose file isolates seats, and emit() writes and checks a tree."""

import re

import build

SEATS = ["p1", "p2", "p3"]
TOKENS = {seat: build.token("seed", 1, seat) for seat in SEATS}


def blocks(compose: str) -> dict[str, str]:
    services = compose.split("\nservices:\n")[1].split("\nnetworks:\n")[0]
    return dict(re.findall(r"^  (\w+):\n((?:    .*\n)+)", services, flags=re.MULTILINE))


def test_tokens_are_fixed_by_seed_instance_and_seat():
    assert build.token("seed", 1, "p1") == build.token("seed", 1, "p1")
    assert len({build.token("seed", 1, seat) for seat in SEATS} | {build.token("seed", 2, "p1")}) == 4
    assert re.fullmatch(r"[0-9a-f]{32}", TOKENS["p1"])


def test_the_world_is_the_only_path_between_seats():
    compose = build.compose(SEATS, TOKENS, {})
    services = blocks(compose)
    assert list(services) == ["main", *SEATS]
    for seat in SEATS:
        assert f"    networks: [{seat}-world]\n" in services[seat]  # a seat is on its own network only
        assert f"      {seat}-world:\n        aliases: [world]\n" in services["main"]
    assert "networks" not in compose.split("\nservices:\n")[0]  # the shared seat block names no network


def test_a_seat_network_has_no_route_out_unless_the_task_asks_for_it():
    closed = build.compose(SEATS, TOKENS, {}).split("\nnetworks:\n")[1]
    assert closed == "".join(
        f"  {seat}-world:\n    internal: true\n    enable_ipv4: false\n    enable_ipv6: true\n"
        for seat in SEATS
    )
    opened = build.compose(SEATS, TOKENS, {}, internet=True).split("\nnetworks:\n")[1]
    assert opened == "".join(f"  {seat}-world:\n    internal: false\n" for seat in SEATS)


def test_a_seat_gets_its_own_token_and_main_gets_the_map():
    compose = build.compose(SEATS, TOKENS, {"MODE": "x"})
    services = blocks(compose)
    for seat in SEATS:
        assert TOKENS[seat] in services[seat]
        assert not any(TOKENS[other] in services[seat] for other in SEATS if other != seat)
    assert all(TOKENS[seat] in services["main"] for seat in SEATS)
    assert '      MODE: "x"\n' in services["main"]


def test_task_toml_marks_supporting_seats():
    text = build.task_toml(
        name="muster/x-i01", description="d", seats={"p1": True, "p2": False}, metadata={"oracle_reward": 1.0}
    )
    assert "[agents.p1]\n\n[agents.p2]\nunder_test = false\n" in text
    assert "oracle_reward = 1.0" in text and 'name = "muster/x-i01"' in text


def test_emit_writes_then_checks_and_removes_stale_files(tmp_path):
    tree = {"t1": {"a.txt": "one\n", "run.sh": "#!/bin/bash\n"}}
    assert build.emit(tmp_path, tree, check=True) == 1  # nothing written yet
    assert build.emit(tmp_path, tree, check=False) == 0
    assert build.emit(tmp_path, tree, check=True) == 0
    assert (tmp_path / "t1" / "run.sh").stat().st_mode & 0o111
    (tmp_path / "t1" / "stale.txt").write_text("old")
    assert build.emit(tmp_path, tree, check=True) == 1
    assert build.emit(tmp_path, tree, check=False) == 0
    assert not (tmp_path / "t1" / "stale.txt").exists()


def test_emit_check_ignores_python_caches(tmp_path):
    tree = {"t1": {"tests/verify.py": "x = 1\n"}}
    assert build.emit(tmp_path, tree, check=False) == 0
    cache = tmp_path / "t1" / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "verify.cpython-312.pyc").write_bytes(b"\x00")
    assert build.emit(tmp_path, tree, check=True) == 0
    assert build.emit(tmp_path, tree, check=False) == 0 and cache.exists()
