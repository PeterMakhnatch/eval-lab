import asyncio
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

LAB = Path.home() / "Developer" / "bespoke-macbench-lab"
SCRATCH = Path("/tmp/fable-day/macrun")
MODEL = "claude-haiku-5-5"
BUDGET_USD = 0.10
SEAT_COUNTS = (1, 2, 4)
IN_FLIGHT_CAP = 2
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
TOKENS = {"input": 20000, "output": 2000, "cache_read": 50000, "cache_write": 0, "cache_write_1h": 0}
MAX_CALLS = 400
MAX_ROWS = 2000
GATE_POLLS = 2000
GATE_POLL_SEC = 0.01
GATE_TIMEOUT_SEC = 30.0
TOLERANCE_USD = 1e-12
ADMIN = "o2-proof-admin"
FAKE_UPSTREAM = "http://fake-upstream.invalid"
FAKE_KEY = "o2-fake-upstream-key"
SEAT_HEADER = "x-proof-seat"
REPORT_NAME = "o2-proxy-proof.json"
B_FILES = (
    "run_hardened.py",
    "upstream/harness/src/multi_agent_harness/proxy/server.py",
    "upstream/harness/src/multi_agent_harness/proxy/metering.py",
    "upstream/harness/src/multi_agent_harness/proxy/pricing.json",
)


def load_bespoke() -> dict:
    assert LAB.is_dir(), LAB
    venv = (LAB / ".venv").resolve()
    if Path(sys.prefix).resolve() != venv:
        raise RuntimeError(f"run under {venv}/bin/python, not {sys.prefix}")
    sys.dont_write_bytecode = True
    if str(LAB) not in sys.path:
        sys.path.insert(0, str(LAB))
    import httpx
    import run_hardened
    from multi_agent_harness.proxy import server
    from multi_agent_harness.proxy.metering import Pricing

    assert run_hardened.HARD_MAX_CONCURRENT == IN_FLIGHT_CAP, run_hardened.HARD_MAX_CONCURRENT
    table = run_hardened.merged_pricing(None)
    assert MODEL in table["anthropic"], MODEL
    return {"httpx": httpx, "server": server, "Pricing": Pricing, "table": table}


def provenance() -> dict:
    out = {}
    for rel in B_FILES:
        path = LAB / rel
        assert path.is_file(), path
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def call_cost(table: dict) -> float:
    price = table["anthropic"][MODEL]
    prompt = sum(TOKENS[k] for k in TOKEN_KINDS if k != "output")
    tier = price.get("long_context")
    assert tier is None or prompt <= tier["above_input_tokens"], prompt
    rates = {
        "input": price["input"],
        "output": price["output"],
        "cache_read": price.get("cache_read", price["input"]),
        "cache_write": price.get("cache_write", price["input"]),
        "cache_write_1h": price.get("cache_write_1h", price.get("cache_write", price["input"])),
    }
    usd = sum(TOKENS[k] * rates[k] for k in TOKEN_KINDS) / 1e6 * 1.0
    assert 0 < usd < BUDGET_USD / 8, usd
    return usd


def sse_body() -> bytes:
    usage = {
        "input_tokens": TOKENS["input"],
        "cache_read_input_tokens": TOKENS["cache_read"],
        "cache_creation_input_tokens": TOKENS["cache_write"],
        "output_tokens": 1,
    }
    message = {"id": "msg_o2", "type": "message", "role": "assistant", "model": MODEL}
    events = (
        {"type": "message_start", "message": {**message, "content": [], "usage": usage}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": TOKENS["output"]}},
        {"type": "message_stop"},
    )
    parts = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]
    return b"".join(parts)


class FakeUpstream:
    def __init__(self, httpx):
        self.httpx = httpx
        self.body = sse_body()
        self.hold: asyncio.Event | None = None
        self.waiting = 0
        self.calls = 0
        self.foreign = 0
        self.in_flight: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    async def __call__(self, request):
        seat = request.headers.get(SEAT_HEADER, "")
        proper = request.headers.get("x-api-key") == FAKE_KEY and request.url.path == "/v1/messages"
        self.foreign += 0 if proper else 1
        self.calls += 1
        self.in_flight[seat] = self.in_flight.get(seat, 0) + 1
        self.peak[seat] = max(self.peak.get(seat, 0), self.in_flight[seat])
        try:
            if self.hold is not None:
                self.waiting += 1
                await asyncio.wait_for(self.hold.wait(), GATE_TIMEOUT_SEC)
        finally:
            self.in_flight[seat] -= 1
        headers = {"content-type": "text/event-stream"}
        return self.httpx.Response(200, headers=headers, content=self.body)


class Ledger:
    def __init__(self, budget: float, agent_budget: float | None, usd: float):
        assert budget > 0 and usd > 0
        assert agent_budget is None or agent_budget > 0
        self.budget = budget
        self.agent_budget = agent_budget
        self.usd = usd
        self.totals: dict[str, float] = {"*": 0.0}

    def check(self, seat: str) -> str:
        if self.totals["*"] >= self.budget:
            return "team"
        if self.agent_budget is not None and self.totals.get(seat, 0.0) >= self.agent_budget:
            return "agent"
        return "ok"

    def room(self, seat: str) -> bool:
        team_room = self.totals["*"] + self.usd < self.budget
        cap = self.agent_budget
        return team_room and (cap is None or self.totals.get(seat, 0.0) + self.usd < cap)

    def bill(self, seat: str) -> None:
        self.totals["*"] += self.usd
        self.totals[seat] = self.totals.get(seat, 0.0) + self.usd

    def step(self, seat: str) -> str:
        outcome = self.check(seat)
        if outcome == "ok":
            self.bill(seat)
        return outcome


def plan_round_robin(ledger: Ledger, seats: tuple[str, ...]) -> list[tuple[str, str]]:
    assert seats
    refused: set[str] = set()
    steps = []
    for i in range(MAX_CALLS):
        seat = seats[i % len(seats)]
        outcome = ledger.step(seat)
        steps.append((seat, outcome))
        if outcome != "ok":
            refused.add(seat)
        if refused == set(seats):
            return steps
    raise AssertionError(f"no refusal for every seat within {MAX_CALLS} calls")


def plan_prefill(ledger: Ledger, seats: tuple[str, ...]) -> list[str]:
    order = []
    for _ in range(MAX_CALLS):
        moved = [seat for seat in seats if ledger.room(seat)]
        if not moved:
            return order
        for seat in moved:
            if ledger.room(seat):
                assert ledger.step(seat) == "ok"
                order.append(seat)
    raise AssertionError(f"prefill did not end within {MAX_CALLS} rounds")


def classify(resp) -> str:
    if resp.status_code == 200:
        return "ok" if b"message_stop" in resp.content else "ok_without_stop"
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        err = {}
    kind, message = str(err.get("type", "")), str(err.get("message", ""))
    retry = resp.headers.get("x-should-retry")
    if resp.status_code == 429 and kind == "budget_exceeded" and retry == "false":
        if message.startswith("team budget exhausted"):
            return "team"
        if message.startswith("agent budget exhausted"):
            return "agent"
    in_flight = "requests in flight" in message and resp.headers.get("retry-after") is not None
    if resp.status_code == 429 and kind == "rate_limit_error" and retry == "true" and in_flight:
        return "in_flight"
    return f"other:{resp.status_code}:{kind}"


async def send(client, key: str) -> str:
    body = {
        "model": MODEL,
        "max_tokens": 16,
        "stream": True,
        "messages": [{"role": "user", "content": "o2"}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    resp = await client.post("/anthropic/v1/messages", json=body, headers=headers)
    return classify(resp)


def differences(expected: list[str], observed: list[str], seats: list[str]) -> list[dict]:
    assert len(seats) == len(expected)
    out = []
    for i in range(min(len(expected), len(observed))):
        if expected[i] != observed[i] and len(out) < 10:
            out.append({"call": i, "seat": seats[i], "expected": expected[i], "observed": observed[i]})
    return out


async def drive_sequential(client, keys: dict, seats: tuple, ledger: Ledger, per_seat: bool) -> dict:
    plan = plan_round_robin(ledger, seats[:1]) if per_seat else []
    plan += plan_round_robin(ledger, seats)
    observed = [await send(client, keys[seat]) for seat, _ in plan]
    expected = [outcome for _, outcome in plan]
    order = [seat for seat, _ in plan]
    return {
        "expected": expected,
        "observed": observed,
        "differences": differences(expected, observed, order),
        "checks": {},
        "detail": {"calls": len(plan), "phases": ["seat-1 alone", "round robin"] if per_seat
                   else ["round robin"]},
    }


async def wait_held(fake: FakeUpstream, count: int) -> bool:
    assert count > 0
    for _ in range(GATE_POLLS):
        if fake.waiting >= count:
            return True
        await asyncio.sleep(GATE_POLL_SEC)
    return False


async def drive_concurrent(client, keys: dict, seats: tuple, ledger: Ledger, fake) -> dict:
    prefill = plan_prefill(ledger, seats)
    pre = [await send(client, keys[seat]) for seat in prefill]
    before = dict(ledger.totals)
    assert all(ledger.check(seat) == "ok" for seat in seats)
    fake.hold = asyncio.Event()
    held_seats = [seat for seat in seats for _ in range(IN_FLIGHT_CAP)]
    held = [asyncio.create_task(send(client, keys[seat])) for seat in held_seats]
    reached = await wait_held(fake, len(held))
    extra = [await send(client, keys[seat]) for seat in seats] if reached else []
    fake.hold.set()
    held_out = list(await asyncio.gather(*held))
    fake.hold = None
    for seat in held_seats:
        ledger.bill(seat)
    post_expected = [ledger.check(seat) for seat in seats]
    post = [await send(client, keys[seat]) for seat in seats]
    expected = ["ok"] * len(prefill) + ["ok"] * len(held) + ["in_flight"] * len(seats)
    expected += post_expected
    observed = pre + held_out + extra + post
    order = list(prefill) + held_seats + list(seats) + list(seats)
    checks = {
        "held_requests_reached_upstream": reached,
        "held_admitted_per_seat_equals_cap": held_out == ["ok"] * len(held),
        "extra_request_per_seat_refused_in_flight": extra == ["in_flight"] * len(seats),
        "upstream_peak_in_flight_per_seat_equals_cap": all(
            fake.peak.get(seat) == IN_FLIGHT_CAP for seat in seats),
    }
    detail = {
        "prefill_calls": len(prefill),
        "team_usd_before_burst": before["*"],
        "seat_usd_before_burst": {seat: before.get(seat, 0.0) for seat in seats},
        "burst_admitted": held_out.count("ok"),
        "burst_refused_in_flight": extra.count("in_flight"),
        "upstream_peak_in_flight": dict(fake.peak),
    }
    return {"expected": expected, "observed": observed, "checks": checks, "detail": detail,
            "differences": differences(expected, observed, order)}


def read_rows(path: Path) -> list[dict]:
    assert path.is_file(), path
    lines = path.read_text().splitlines()
    assert len(lines) <= MAX_ROWS, len(lines)
    return [json.loads(line) for line in lines if line.strip()]


def audit_sequential_log(rows: list[dict], agent_budget: float | None) -> dict:
    team, seats, first, bad = 0.0, {}, None, []
    for i, row in enumerate(rows):
        agent, status, reason = row.get("agent"), row.get("status"), row.get("reason")
        seat_before = seats.get(agent, 0.0)
        over_team = team >= BUDGET_USD
        over_seat = agent_budget is not None and seat_before >= agent_budget
        if status == 200 and not (over_team or over_seat):
            team += row["usd"]
            seats[agent] = seat_before + row["usd"]
        elif status == 429 and reason == "budget" and (over_team or over_seat):
            first = first or {"row": i, "agent": agent, "team_usd_before": team,
                              "seat_usd_before": seat_before, "error": row.get("error")}
        elif len(bad) < 10:
            bad.append({"row": i, "agent": agent, "status": status, "reason": reason,
                        "team_usd_before": team, "seat_usd_before": seat_before})
    return {"rows": len(rows), "violations": bad, "first_budget_refusal": first}


def overshoot(usage: dict, seats: tuple, agent_budget: float | None) -> dict:
    team = usage["total"]["usd"]
    seat_usd = {seat: usage["agents"].get(seat, {}).get("usd", 0.0) for seat in seats}
    seat_over = None
    if agent_budget is not None:
        seat_over = max(max(0.0, usd - agent_budget) for usd in seat_usd.values())
    return {
        "team_usd": team,
        "seat_usd": seat_usd,
        "team_overshoot_usd": max(0.0, team - BUDGET_USD),
        "team_overshoot_share_of_cap": max(0.0, team - BUDGET_USD) / BUDGET_USD,
        "worst_seat_overshoot_usd": seat_over,
    }


def first_refusal_at_cap(audit: dict, usd: float, agent_budget: float | None) -> bool:
    first = audit["first_budget_refusal"]
    if first is None:
        return False
    team = first["team_usd_before"]
    at_team = BUDGET_USD <= team < BUDGET_USD + usd
    if agent_budget is None:
        return at_team
    seat = first["seat_usd_before"]
    return at_team or agent_budget <= seat < agent_budget + usd


def log_summary(rows: list[dict]) -> dict:
    pairs = Counter(f"{row.get('status')}/{row.get('reason')}" for row in rows)
    budget = [row for row in rows if row.get("status") == 429 and row.get("reason") == "budget"]
    return {
        "rows": len(rows),
        "status_reason_counts": dict(sorted(pairs.items())),
        "budget_rows": len(budget),
        "first_budget_row": budget[0] if budget else None,
    }


def judge(spec: dict, drive: dict, usage: dict, admin: dict, ledger: Ledger, fake, rows) -> dict:
    seats, k, usd, cap = spec["seats"], spec["k"], spec["usd"], spec["agent_budget"]
    spend = overshoot(usage, seats, cap)
    logged = log_summary(rows)
    observed = drive["observed"]
    refused = sum(1 for outcome in observed if outcome in ("team", "agent"))
    admitted = sum(1 for outcome in observed if outcome == "ok")
    billed = [row["usd"] for row in rows if row.get("status") == 200]
    checks = dict(drive["checks"])
    checks["outcomes_match_reference"] = observed == drive["expected"]
    checks["team_total_matches_reference"] = (
        abs(spend["team_usd"] - ledger.totals["*"]) < TOLERANCE_USD)
    checks["seat_totals_match_reference"] = all(
        abs(spend["seat_usd"][seat] - ledger.totals.get(seat, 0.0)) < TOLERANCE_USD for seat in seats)
    checks["admin_usage_matches_proxy"] = (
        abs(admin["total"]["usd"] - spend["team_usd"]) < TOLERANCE_USD)
    checks["budget_rows_match_refusals"] = refused > 0 and logged["budget_rows"] == refused
    checks["each_billed_row_costs_one_call"] = len(billed) == admitted and all(
        abs(value - round(usd, 8)) < TOLERANCE_USD for value in billed)
    checks["upstream_saw_only_admitted_calls_with_proxy_key"] = (
        fake.foreign == 0 and fake.calls == admitted)
    checks["upstream_peak_in_flight_within_cap"] = max(fake.peak.values(), default=0) <= IN_FLIGHT_CAP
    audit = None
    if spec["concurrent"]:
        seat_over = spend["worst_seat_overshoot_usd"]
        checks["team_overshoot_below_cap_times_seats_calls"] = (
            spend["team_overshoot_usd"] < IN_FLIGHT_CAP * k * usd + TOLERANCE_USD)
        checks["seat_overshoot_below_cap_calls"] = (
            cap is None or seat_over < IN_FLIGHT_CAP * usd + TOLERANCE_USD)
        checks["burst_crossed_a_cap"] = spend["team_overshoot_usd"] > 0 or bool(seat_over)
    else:
        audit = audit_sequential_log(rows, cap)
        checks["log_admits_below_cap_refuses_at_cap"] = not audit["violations"]
        checks["first_refusal_within_one_call_of_cap"] = first_refusal_at_cap(audit, usd, cap)
    return {
        "name": spec["name"],
        "k": k,
        "cap": "per_seat" if cap is not None else "shared",
        "drive": "concurrent" if spec["concurrent"] else "sequential",
        "budget_usd": BUDGET_USD,
        "agent_budget_usd": cap,
        "pass": all(checks.values()),
        "checks": checks,
        "outcome_counts": dict(sorted(Counter(observed).items())),
        "spend": spend,
        "usage_log": {"path": spec["log_name"], **logged},
        "log_audit": audit,
        **{key: drive[key] for key in ("detail", "differences", "expected", "observed")},
    }


def build_proxy(bespoke: dict, fake: FakeUpstream, log_path: Path, keys_dir: Path, cap):
    httpx = bespoke["httpx"]
    return bespoke["server"].Proxy(
        ADMIN,
        BUDGET_USD,
        cap,
        log_path=str(log_path),
        keys_path=str(keys_dir / "proxy-keys.json"),
        pricing=bespoke["Pricing"](bespoke["table"]),
        upstreams={"anthropic": FAKE_UPSTREAM},
        api_keys={"anthropic": FAKE_KEY},
        transport=httpx.MockTransport(fake),
        upstream_headers={SEAT_HEADER: "{agent}"},
        rate_per_sec=0,
        rate_burst=0,
        max_concurrent=IN_FLIGHT_CAP,
    )


async def scenario(bespoke: dict, output: Path, keys_root: Path, spec: dict) -> dict:
    log_path = output / spec["log_name"]
    log_path.unlink(missing_ok=True)
    keys_dir = keys_root / spec["name"]
    keys_dir.mkdir()
    fake = FakeUpstream(bespoke["httpx"])
    proxy = build_proxy(bespoke, fake, log_path, keys_dir, spec["agent_budget"])
    seats = spec["seats"]
    keys = {seat: proxy.register(seat) for seat in seats}
    ledger = Ledger(BUDGET_USD, spec["agent_budget"], spec["usd"])
    httpx = bespoke["httpx"]
    transport = httpx.ASGITransport(app=bespoke["server"].build_app(proxy))
    client = httpx.AsyncClient(transport=transport, base_url="http://o2-proxy.local")
    try:
        if spec["concurrent"]:
            drive = await drive_concurrent(client, keys, seats, ledger, fake)
        else:
            drive = await drive_sequential(client, keys, seats, ledger, spec["agent_budget"] is not None)
        reply = await client.get("/admin/usage", headers={"authorization": f"Bearer {ADMIN}"})
        assert reply.status_code == 200, reply.status_code
        admin = reply.json()
    finally:
        await client.aclose()
        await proxy.client.aclose()
    return judge(spec, drive, proxy.usage(), admin, ledger, fake, read_rows(log_path))


def specs(usd: float) -> list[dict]:
    out = []
    for k in SEAT_COUNTS:
        for per_seat in (False, True):
            for concurrent in (False, True):
                name = f"{'perseat' if per_seat else 'shared'}-{'burst' if concurrent else 'seq'}-k{k}"
                out.append({
                    "name": name,
                    "log_name": f"o2-{name}.proxy-usage.jsonl",
                    "k": k,
                    "seats": tuple(f"seat-{i + 1}" for i in range(k)),
                    "agent_budget": BUDGET_USD / k if per_seat else None,
                    "concurrent": concurrent,
                    "usd": usd,
                })
    assert len(out) == len(SEAT_COUNTS) * 4
    return out


async def prove(bespoke: dict, output: Path, keys_root: Path, usd: float) -> list[dict]:
    results = []
    for spec in specs(usd):
        results.append(await scenario(bespoke, output, keys_root, spec))
    return results


def summarize(results: list[dict], usd: float) -> tuple[str, str]:
    assert results
    failed = [r["name"] for r in results if not r["pass"]]
    if failed:
        return "fail", f"{len(failed)}/{len(results)} scenarios fail: {', '.join(failed)}"
    firsts = sorted({
        round(r["log_audit"]["first_budget_refusal"]["team_usd_before"], 6)
        for r in results if r["name"].startswith("shared-seq")})
    bursts = [r for r in results if r["drive"] == "concurrent"]
    worst = max(bursts, key=lambda r: r["spend"]["team_overshoot_usd"])
    over = worst["spend"]["team_overshoot_usd"]
    reason = (
        f"{len(results)}/{len(results)} scenarios match the reference model at k=1,2,4 "
        f"(call ${usd:.4f}, team cap ${BUDGET_USD:.2f}). Shared cap refuses first at team spend "
        f"{firsts} USD, per-seat cap B/k refuses within 1 call of B/k; every refusal is a 429 "
        f"budget_exceeded row with reason budget. {IN_FLIGHT_CAP} requests in flight per seat are "
        f"billed past the cap and a further one gets 429 in flight. Worst burst overshoot "
        f"${over:.4f} ({over / BUDGET_USD:.1%} of the cap) in {worst['name']}, below the bound "
        f"{IN_FLIGHT_CAP} x k x call cost."
    )
    return "pass", reason


SETUP = (
    "Real Proxy and build_app from B server.py, driven in process through httpx.ASGITransport.",
    "Fake upstream: httpx.MockTransport that returns 1 streamed Anthropic message with fixed usage.",
    "No network, no model, no Docker. The upstream key is a fixed fake string.",
    "Pricing: run_hardened.merged_pricing(None), the table that the hardened hub uploads.",
    "In-flight cap: run_hardened.HARD_MAX_CONCURRENT (2) passed as Proxy max_concurrent.",
    "Rate limiter off (rate 0): the hub default 4/s burst 20 would refuse fast sequential calls; "
    "it is not the cap under test.",
    "Proxy upstream_headers adds x-proof-seat={agent} so the fake upstream counts calls per seat.",
    "Reference model: Ledger repeats Proxy.over_budget (team bucket first, then seat bucket).",
    "Burst: prefill to the last call below the caps, hold 2 calls per seat at the upstream, "
    "send 1 more per seat, release, then send 1 call per seat.",
)


def run(output: Path) -> tuple[str, str]:
    assert isinstance(output, Path) and output.is_dir(), output
    report: dict = {"check": "O2", "model": MODEL, "budget_usd": BUDGET_USD,
                    "seat_counts": list(SEAT_COUNTS), "in_flight_cap": IN_FLIGHT_CAP,
                    "tokens_per_call": TOKENS, "setup": list(SETUP)}
    try:
        bespoke = load_bespoke()
        report["bespoke_sha256"] = provenance()
        usd = call_cost(bespoke["table"])
        report["cost_per_call_usd"] = usd
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="o2-keys-", dir=SCRATCH) as keys_root:
            results = asyncio.run(prove(bespoke, output, Path(keys_root), usd))
        report["scenarios"] = results
        verdict, reason = summarize(results, usd)
    except Exception as exc:
        verdict, reason = "not settled", f"{type(exc).__name__}: {exc}"[:500]
    assert verdict in ("pass", "fail", "not settled"), verdict
    evidence = sorted(path.name for path in output.glob("o2-*.proxy-usage.jsonl"))
    report.update(verdict=verdict, reason=reason, evidence=[REPORT_NAME, *evidence])
    (output / REPORT_NAME).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    return verdict, reason
