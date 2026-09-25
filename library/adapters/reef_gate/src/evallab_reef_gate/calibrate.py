#!/usr/bin/env python3
"""HAR-72 calibration driver: measure the Reef-process publication gate's false-publish rate.

Copied and adapted from research-context ``reef/experiments/04_gate_aa.py`` (do not edit that
original). The harness behaviour is retained -- the tutorial's native-agent recipe with its three
exact-answer tasks and grader, same-tree proposals through the scenario inbox, one failing report
per trial to batch an auto step, settled training rows read back through the client -- with the
tutorial's default ``score_comparison`` selection replaced by this package's gate plugin
(``evallab_reef_gate.plugin:Factory``) configured through ``EVALLAB_REEF_GATE_CONFIG``.

Conditions:

* ``aa`` -- every trial rewrites the starter ``answer-style`` skill with the text it already
  has, so the candidate tree is byte-identical to the current one and every publish is a false
  positive.
* ``known-effect`` -- the seed's ``answer-style`` skill is deliberately degraded (prose-only
  answers, never a bare number; no oracle answers), every trial scenario is created before
  the first trial so each one forks the pre-campaign degraded seed, and the proposal restores
  the original tutorial skill text. Published candidates cannot erase the contrast because
  each scenario's tree is abandoned after its trial. This condition is labelled NOT A/A in
  its summary.

The driver refuses non-empty work dirs (state is never erased), keeps every server, recipe,
storage, step and decision output under the owned work dir, runs the Reef subprocess with a
small environment allowlist (no ambient credentials, model URLs or proxy variables), and before
any inference verifies the upstream URL is a literal plain-http loopback origin (explicit port,
no credentials/path/query). Ollama mode (default) fetches the model inventory with ambient
proxies and redirects refused and a bounded read, and requires the model to be the unique
locally installed GGUF entry with a valid digest and no cloud routing; it never downloads
models. API-proxy mode (--api-proxy-url, mutually exclusive with --ollama-url) routes
inference at the parent-owned loopback budget proxy instead: it requires an explicit --model,
reads only the named capability environment (default EVALLAB_REEF_PROXY_TOKEN) in main and
passes it as an explicit parameter to the Reef subprocess (REEF_UPSTREAM_API_KEY), persists
no token, and records inference_kind=api_proxy with model_digest=None.

Usage (Ollama running locally with the model pulled; roughly 1-3 minutes per trial at 1 repeat):

    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    PYTHONPATH=<this package's src directory> \
    ~/Developer/reef/.venv/bin/python -m evallab_reef_gate.calibrate \
        --work-dir <fresh owned directory> \
        --reef-root ~/Developer/reef \
        --python ~/Developer/reef/.venv/bin/python \
        --ollama-url http://127.0.0.1:11461 \
        --condition aa --trials 30 --repeats 5

    ... --condition known-effect --trials 5 --repeats 5
    ... --api-proxy-url http://127.0.0.1:8080 --model <proxy-model>  # parent-owned budget proxy
    # (capability from $EVALLAB_REEF_PROXY_TOKEN; never persisted; --ollama-url must not be passed)

    ... --analyze-only --work-dir <existing work directory>   # no services started

Standalone analysis (``--analyze-only``) reads only the work directory: results.jsonl,
run-meta.json, the step records and the gate decision records.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from evallab_reef_gate.rules import sign_test_p

#: This package's src root, so the Reef subprocess can import the selection plugin.
PLUGIN_SRC = Path(__file__).resolve().parents[1]

DEFAULT_REEF_ROOT = Path.home() / "Developer" / "reef"
SOURCE_CONFIG_RELATIVE = Path("tutorials/evolve-your-harness/configs/serve-native.yaml")
TOKEN = "reef-local"
SCENARIO_AA = "aa-gate"
KNOWN_EFFECT_SCENARIO_PREFIX = "known-effect"
STEP_TIMEOUT_S = 1800.0
PASS_THRESHOLD = 1.0
WILSON_Z = 1.959964
REEF_BUILTIN_SELECTIONS = frozenset({"score_comparison", "floor", "always"})
#: The environment variable naming the parent-owned proxy capability for API-proxy mode.
DEFAULT_API_PROXY_TOKEN_ENV = "EVALLAB_REEF_PROXY_TOKEN"
#: Truthful run-meta inference kinds: local Ollama inventory vs parent-owned loopback proxy.
INFERENCE_KIND_OLLAMA = "ollama"
INFERENCE_KIND_API_PROXY = "api_proxy"

#: The tutorial grader's answer table (tutorials/evolve-your-harness/harness/evolution.py);
#: used only to label failing episodes, never given to the model.
ANSWERS = {"[sieve]": "9592", "[fib]": "2880067194370816120", "[csv]": "30"}

#: The known-effect condition's degraded current skill: answer-style guidance that directly
#: contradicts the grader's plain-integer-on-the-last-line contract. No oracle answers.
DEGRADED_ANSWER_STYLE_TEXT = (
    "# answer-style\n"
    "\n"
    "Always wrap answers in prose. Reply with complete sentences that explain\n"
    "the result, and never end a reply with a bare number: the final line must\n"
    "be a full sentence that mentions the value in context, not the value\n"
    "alone.\n"
)

#: The subprocess environment allowlist: local platform basics only, plus the Reef variables
#: this driver owns. Ambient provider credentials, model URLs and proxy variables are dropped.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "TMPDIR", "LANG")
ENV_LC_PREFIX = "LC_"

#: The exact fields the parent plugin writes into each per-candidate decision record; the
#: calibration reader validates them and reports malformed records rather than guessing aliases.
_DECISION_NUMBER_KEYS = ("decision_seconds", "evaluation_seconds")
_DECISION_TEXT_KEYS = ("reef_commit", "reason_code")
_DECISION_LIST_KEYS = ("pairs", "vetoes")


# -- statistics (the original gate_table machinery; sign_test_p is the package rule) ------------


def wilson(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """The Wilson score interval, exact at the boundaries: k=0 gives lower 0, k=n gives upper 1."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return (lo, hi)


def pair_law(p_current: float, p_candidate: float) -> tuple[float, float]:
    """P(win), P(loss) for one pairing of independent pass/fail episodes."""
    return p_candidate * (1 - p_current), p_current * (1 - p_candidate)


def publish_probability(pairs: list[tuple[float, float]], rule: str, alpha: float) -> float:
    """Exact P(publish) over independent pairings, each (P(win), P(loss)); rule 'majority' or 'sign'."""
    joint = {(0, 0): 1.0}
    for p_win, p_loss in pairs:
        nxt: dict[tuple[int, int], float] = {}
        for (wins, losses), mass in joint.items():
            for (d_wins, d_losses), q in (
                ((1, 0), p_win),
                ((0, 1), p_loss),
                ((0, 0), 1 - p_win - p_loss),
            ):
                if q > 0:
                    key = (wins + d_wins, losses + d_losses)
                    nxt[key] = nxt.get(key, 0.0) + mass * q
        joint = nxt
    if rule == "majority":
        return sum(mass for (wins, losses), mass in joint.items() if wins - losses > 0)
    return sum(mass for (wins, losses), mass in joint.items() if sign_test_p(wins, losses) < alpha)


# -- preflight: loopback-only, installed-models-only, no downloads -----------------------------


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def require_loopback_url(url: str) -> str:
    """A literal plain-http loopback origin -- host, explicit port, nothing else -- or SystemExit.

    Credentials, paths, query strings, fragments, implicit ports and non-loopback hosts are all
    refused, so inference can only ever be routed at the literal local endpoint.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise SystemExit(
            f"refusing ollama url {url!r}: only plain http loopback endpoints are supported"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SystemExit(f"refusing ollama url {url!r}: embedded credentials are not allowed")
    if parsed.path not in ("", "/"):
        raise SystemExit(
            f"refusing ollama url {url!r}: a path is not allowed, only the bare origin"
        )
    if parsed.query or parsed.fragment:
        raise SystemExit(
            f"refusing ollama url {url!r}: query strings and fragments are not allowed"
        )
    host = (parsed.hostname or "").strip("[]").lower()
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"refusing ollama url {url!r}: {host or 'no host'!r} is not loopback; remote routing is not allowed"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"refusing ollama url {url!r}: invalid port") from exc
    if port is None:
        raise SystemExit(f"refusing ollama url {url!r}: an explicit port is required")
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"http://{authority}"


#: The inventory read is bounded: /api/tags is small, and a misrouted endpoint must not be
#: readable without a limit.
INVENTORY_MAX_BYTES = 1_048_576


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the loopback inventory endpoint must answer directly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _inventory_opener() -> urllib.request.OpenerDirector:
    # The explicit empty ProxyHandler drops ambient HTTP(S)_PROXY routing; _NoRedirects above
    # keeps a decorated endpoint from bouncing the inventory read elsewhere.
    return urllib.request.build_opener(_NoRedirects(), urllib.request.ProxyHandler({}))


def fetch_local_inventory(
    base_url: str, timeout_s: float = 10.0, max_bytes: int = INVENTORY_MAX_BYTES
) -> dict:
    """The Ollama /api/tags inventory of locally installed GGUF models, or SystemExit.

    The request ignores ambient proxies, refuses redirects, and reads at most ``max_bytes``.
    """
    try:
        request = urllib.request.Request(
            f"{base_url}/api/tags", headers={"Accept": "application/json"}
        )
        with _inventory_opener().open(request, timeout=timeout_s) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SystemExit(
                f"the inventory endpoint at {base_url}/api/tags redirected (HTTP {exc.code}); refusing"
            ) from exc
        raise SystemExit(
            f"cannot read the local model inventory at {base_url}/api/tags: HTTP {exc.code}"
        ) from exc
    except OSError as exc:
        raise SystemExit(
            f"cannot read the local model inventory at {base_url}/api/tags: {exc}"
        ) from exc
    if len(body) > max_bytes:
        raise SystemExit(
            f"the inventory at {base_url}/api/tags exceeds {max_bytes} bytes; refusing an unbounded read"
        )
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"the inventory at {base_url}/api/tags is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise SystemExit(
            f"unexpected /api/tags payload at {base_url}: expected an object with a 'models' list"
        )
    return payload


def _valid_digest(value: object) -> bool:
    """A sha256 inventory digest: 64 hex characters, with or without the algorithm prefix."""
    if not isinstance(value, str):
        return False
    hex_part = value.removeprefix("sha256:")
    return len(hex_part) == 64 and all(char in "0123456789abcdefABCDEF" for char in hex_part)


def require_local_model(inventory: dict, model: str) -> dict:
    """The one locally installed GGUF inventory entry for ``model``, or SystemExit.

    The match must be unique, confirmed GGUF (``details.format``), carry a positive local size
    and a valid sha256 digest, and have no cloud routing (``remote_host``/``remote_model``).
    Nothing is ever downloaded: a missing model stops the run.
    """
    entries = [entry for entry in inventory["models"] if isinstance(entry, dict)]
    names = sorted(
        {
            name
            for entry in entries
            for name in (entry.get("name"), entry.get("model"))
            if isinstance(name, str)
        }
    )
    wanted = {model}
    if ":" not in model:
        wanted.add(f"{model}:latest")
    matches = [
        entry for entry in entries if entry.get("name") in wanted or entry.get("model") in wanted
    ]
    if not matches:
        raise SystemExit(
            f"model {model!r} is not installed locally (available: {names}); "
            "refusing to continue: this driver never downloads models"
        )
    if len(matches) != 1:
        raise SystemExit(
            f"model {model!r} matched {len(matches)} inventory entries; refusing an ambiguous local route"
        )
    entry = matches[0]
    label = entry.get("name") or entry.get("model") or model
    if entry.get("remote_host") or entry.get("remote_model"):
        raise SystemExit(
            f"model {label!r} is cloud-routed (remote_host/remote_model set); refusing: only local GGUF routing is allowed"
        )
    details = entry.get("details")
    if not isinstance(details, dict) or details.get("format") != "gguf":
        raise SystemExit(
            f"model {label!r} is not a locally installed GGUF (details.format missing or not 'gguf')"
        )
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SystemExit(
            f"model {label!r} reports no positive local size; refusing an unverifiable local route"
        )
    if not _valid_digest(entry.get("digest")):
        raise SystemExit(f"model {label!r} has no valid sha256 digest in the local inventory")
    return entry


def require_api_capability(env_name: str) -> str:
    """The named proxy capability, or SystemExit; the value never appears in errors."""
    if not isinstance(env_name, str) or not env_name.strip():
        raise SystemExit("--api-proxy-token-env must name a capability environment variable")
    value = os.environ.get(env_name, "")
    if not value or not value.strip():
        raise SystemExit(
            f"missing proxy capability in {env_name}; set it before starting the calibration"
        )
    return value


def reef_checkout_commit(reef_root: Path) -> str:
    """The Reef checkout's HEAD commit, recorded as the gate config's expected revision."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(reef_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"cannot read the reef revision at {reef_root}: {exc}") from exc
    if completed.returncode != 0:
        raise SystemExit(f"git rev-parse failed at {reef_root}: {completed.stderr.strip()}")
    return completed.stdout.strip()


# -- subprocess environment ---------------------------------------------------------------------


def child_environment(
    *,
    reef_root: Path,
    work: Path,
    gate_config_path: Path,
    python: Path,
    api_capability: str | None = None,
) -> dict:
    """The Reef server subprocess environment: allowlisted platform basics plus owned variables.

    ``PYTHONPATH`` is rebuilt as exactly this package's src root and the Reef checkout root;
    ``PYTHONDONTWRITEBYTECODE``/``PYTHONNOUSERSITE`` keep the read-only checkout and venv clean.
    Ambient credentials, model URLs and proxy variables never reach the native episodes. The
    only secret ever set is the explicit ``api_capability`` (the parent-owned proxy capability
    for API-proxy mode; ``"ollama"`` for local mode): native episodes may receive that proxy
    capability but never a provider key, which the parent proxy owns.
    """
    reef_root, work, gate_config_path = Path(reef_root), Path(work), Path(gate_config_path)
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.update(
        {name: value for name, value in os.environ.items() if name.startswith(ENV_LC_PREFIX)}
    )
    bin_dir = Path(python).absolute().parent
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH') or os.defpath}"
    env["PYTHONPATH"] = f"{PLUGIN_SRC}{os.pathsep}{reef_root}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["REEF_RECIPE_CONFIG_DIR"] = str(work / "recipes")
    env["REEF_UPSTREAM_API_KEY"] = api_capability if api_capability is not None else "ollama"
    env["EVALLAB_REEF_GATE_CONFIG"] = str(gate_config_path)
    return env


# -- condition seed handling --------------------------------------------------------------------


def degrade_seed_entries(entries: list) -> tuple[list, dict]:
    """The seed with its ``answer-style`` entry's text replaced by the degraded skill.

    Returns ``(degraded_entries, original_entry)`` where ``original_entry`` is the untouched
    pre-degradation entry: the known-effect candidate restores exactly it. Both sides are
    deep copies, so later mutation of the degraded seed cannot leak into the candidate
    (and vice versa) through a shared config mapping.
    """
    degraded: list = []
    original: dict | None = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == "answer-style":
            original = copy.deepcopy(entry)
            replacement = copy.deepcopy(entry)
            replacement["config"] = {
                **replacement.get("config", {}),
                "text": DEGRADED_ANSWER_STYLE_TEXT,
            }
            degraded.append(replacement)
        else:
            degraded.append(copy.deepcopy(entry))
    if original is None:
        raise ValueError(
            "the seed has no 'answer-style' entry to degrade for the known-effect condition"
        )
    return degraded, original


# -- configuration ------------------------------------------------------------------------------


def write_configs(
    work: Path,
    *,
    reef_root: Path,
    upstream_url: str,
    port: int,
    model: str,
    workers: int,
    repeats: int,
    seed_entries: list | None,
    selection: str,
    condition: str,
) -> tuple[Path, dict, dict, list[str]]:
    """The tutorial's native serve file with absolute owned state paths, the upstream model, the
    gate selection, a kept step record, and (known-effect only) the degraded seed skill.

    Returns ``(serve_path, current_entry, candidate_entry, tasks)``: the entry the current tree
    carries and the entry a proposal writes -- identical for ``aa``, degraded-vs-original for
    ``known-effect``.
    """
    import yaml

    config = yaml.safe_load((reef_root / SOURCE_CONFIG_RELATIVE).read_text())
    state = work / ".reef"
    config["reef"]["port"] = port
    config["reef"]["run-dir"] = str(work / "stack")
    config["inference"]["upstream-url"] = upstream_url
    config["inference"]["upstream-model"] = model
    config["storage"] = {
        "agent-record-dir": str(state / "agent-record"),
        "artifact-repository": str(state / "artifacts.git"),
        "artifact-work-dir": str(state / "artifact-work"),
        "artifact-cache-dir": str(state / "artifact-cache"),
    }
    evolution = config["recipe"]["config"]["evolution"]
    evolution["step_record_dir"] = str(work / "steps")
    evolution["episode_repeats"] = repeats
    evolution["selection"] = selection
    if seed_entries is not None:
        evolution["seed"] = seed_entries
    candidate_entry: dict | None = None
    if condition == "known-effect":
        evolution["seed"], candidate_entry = degrade_seed_entries(evolution["seed"])
    config["execution"]["evolution"]["workers"] = workers
    serve = work / "serve-aa.yaml"
    serve.write_text(yaml.safe_dump(config, sort_keys=False))
    # The recipe registry reads named recipes from REEF_RECIPE_CONFIG_DIR (the tutorial's
    # materialize_recipe.py), so mirror the written config as the harness_evolve recipe.
    recipe = {key: config[key] for key in ("schema-version", "recipe")}
    recipe.update(
        {key: config[key] for key in ("inference", "execution", "executors") if key in config}
    )
    (work / "recipes").mkdir(parents=True, exist_ok=True)
    (work / "recipes" / "harness_evolve.yaml").write_text(yaml.safe_dump(recipe, sort_keys=False))
    current_entry = next(
        e for e in evolution["seed"] if isinstance(e, dict) and e.get("id") == "answer-style"
    )
    if candidate_entry is None:
        candidate_entry = current_entry
    return serve, current_entry, candidate_entry, list(evolution["tasks"])


def write_gate_config(
    work: Path,
    *,
    alpha: float,
    min_valid_pairs: int,
    regression_failure_threshold: int,
    reef_commit: str,
    pass_threshold: float = PASS_THRESHOLD,
) -> Path:
    """The EVALLAB_REEF_GATE_CONFIG JSON: flat GateConfig keys plus record_dir and reef_commit."""
    (work / "gate-decisions").mkdir(parents=True, exist_ok=True)
    path = work / "gate-config.json"
    path.write_text(
        json.dumps(
            {
                "alpha": alpha,
                "min_valid_pairs": min_valid_pairs,
                "pass_threshold": pass_threshold,
                "regression_failure_threshold": regression_failure_threshold,
                "record_dir": str(work / "gate-decisions"),
                "reef_commit": reef_commit,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def start_reef(
    python: Path,
    serve: Path,
    port: int,
    work: Path,
    reef_root: Path,
    gate_config_path: Path,
    *,
    api_capability: str | None = None,
):
    """Start ``reef serve`` on the owned config and wait for /healthz."""
    from reef_client import ReefClient, ReefClientError

    env = child_environment(
        reef_root=reef_root,
        work=work,
        gate_config_path=gate_config_path,
        python=python,
        api_capability=api_capability,
    )
    log = (work / "reef.log").open("w")
    server = subprocess.Popen(
        [str(python), "-m", "reef", "serve", "-c", str(serve)],
        cwd=work,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    probe = ReefClient(f"http://127.0.0.1:{port}", token=TOKEN, timeout_s=5.0)
    for _ in range(180):
        if server.poll() is not None:
            raise SystemExit(f"reef exited during startup; see {work / 'reef.log'}")
        try:
            probe.get("/healthz")
            return server
        except (ReefClientError, OSError, TimeoutError):
            time.sleep(1.0)
    server.terminate()
    raise SystemExit(f"reef did not answer /healthz within 180 s; see {work / 'reef.log'}")


# -- campaign -----------------------------------------------------------------------------------


def manifest(client, scenario: str) -> dict:
    return client.get("/reef/harness", extra_headers={"x-reef-scenario": scenario})


def training_rows(client, scenario: str) -> list[dict]:
    rows = client.get("/reef/harness/releases", extra_headers={"x-reef-scenario": scenario})[
        "releases"
    ]
    return [row for row in rows if row.get("operation") == "training"]


def find_key(obj, key):
    """The first value stored under ``key`` anywhere in a nested metrics document."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for value in obj:
            found = find_key(value, key)
            if found is not None:
                return found
    return None


def name_scenario(client, model: str, scenario: str) -> None:
    """One recorded inference: it names the scenario, whose base release is the seed tree."""
    client.inference_with_record(
        scenario,
        "/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with one word."}],
            "max_tokens": 1,
        },
    )


def trial_scenario(condition: str, index: int) -> str:
    """The scenario a trial runs in: the shared scenario for ``aa``, one fresh scenario per trial otherwise."""
    if condition == "known-effect":
        return f"{KNOWN_EFFECT_SCENARIO_PREFIX}-{index:02d}"
    return SCENARIO_AA


def name_known_effect_scenarios(client, model: str, trials: int) -> list[str]:
    """Create every known-effect trial scenario before the first trial publishes.

    A fresh Reef scenario forks the shared head at creation, and every published training
    commit advances that head -- so a scenario created after an earlier trial published
    would start from the published (restored) tree instead of the degraded seed. Naming
    all scenarios upfront pins each trial's current tree to the pre-campaign degraded seed;
    later publishes advance only their own scenario's tree plus the shared head, which no
    remaining trial still needs to fork from.
    """
    scenarios = [trial_scenario("known-effect", index) for index in range(1, trials + 1)]
    for scenario in scenarios:
        name_scenario(client, model, scenario)
    return scenarios


def trigger(client, model: str, tag: str, scenario: str) -> None:
    """One recorded inference and a failing report on it: auto mode batches the report into one step."""
    _, receipt = client.inference_with_record(
        scenario,
        "/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with one word."}],
            "max_tokens": 1,
        },
    )
    client.report(
        scenario,
        {
            "agent_record_id": f"calibration-trigger-{tag}",
            "score": 0.0,
            "feedback": "calibration trigger",
        },
        references=[receipt],
    )


def wait_for_row(client, before: int, scenario: str) -> dict | None:
    """The scenario's next settled training row, or None when the step timeout passes."""
    deadline = time.monotonic() + STEP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            rows = training_rows(client, scenario)
        except (TimeoutError, OSError):
            time.sleep(3.0)  # a step in flight holds the catalog
            continue
        if len(rows) > before:
            return rows[before]
        time.sleep(3.0)
    return None


def run_trial(
    client,
    *,
    model: str,
    scenario: str,
    index: int,
    condition: str,
    current_entry: dict,
    candidate_entry: dict,
) -> dict:
    """One trial: an inbox proposal, a failing report to batch a step, and the settled row."""
    before = len(training_rows(client, scenario))
    head = manifest(client, scenario)
    entry = candidate_entry if condition == "known-effect" else current_entry
    reason = (
        "known-effect control: restore the original tutorial answer-style skill over the "
        "deliberately degraded current one"
        if condition == "known-effect"
        else "A/A control: the starter skill rewritten with the text it already has"
    )
    body, _ = client.post(
        "/reef/harness/proposals",
        scenario,
        {
            "mutations": [
                {
                    "op": "update",
                    "id": entry["id"],
                    "options": {"name": entry["name"], "config": entry["config"]},
                }
            ],
            "reason": reason,
            "session": f"{condition}-{index}",
            "release_id": head["release_id"],
        },
    )
    if not body.get("admitted"):
        return {
            "trial": index,
            "condition": condition,
            "scenario": scenario,
            "status": "refused",
            "proposal_response": body,
        }
    started = time.monotonic()
    trigger(client, model, str(index), scenario)
    row = wait_for_row(client, before, scenario)
    if row is None:
        return {
            "trial": index,
            "condition": condition,
            "scenario": scenario,
            "status": "timeout",
            "seconds": round(time.monotonic() - started, 1),
        }
    after = manifest(client, scenario)
    metrics = row.get("metrics") or {}
    return {
        "trial": index,
        "condition": condition,
        "scenario": scenario,
        "status": "settled",
        "seconds": round(time.monotonic() - started, 1),
        "published": bool(metrics.get("published")),
        "skipped": metrics.get("skipped"),
        "wins": metrics.get("wins"),
        "losses": metrics.get("losses"),
        "ties": metrics.get("ties"),
        "gate": {
            key: metrics.get(key)
            for key in (
                "selected",
                "reason_code",
                "valid_pairs",
                "invalid_pairs",
                "p_value",
                "vetoes",  # a list of per-task veto records; bool(vetoes) means vetoed
                "decision_record",
            )
        },
        "candidate_scores": find_key(metrics, "candidate_scores"),
        "current_scores": find_key(metrics, "current_scores"),
        "proposal": find_key(metrics, "proposal"),
        "head_before": {"release_id": head["release_id"], "content_id": head.get("content_id")},
        "head_after": {"release_id": after["release_id"], "content_id": after.get("content_id")},
        "files_identical": after["files"] == head["files"],
        "release_id": row.get("release_id"),
    }


def record_result(work: Path, result: dict) -> None:
    with (work / "results.jsonl").open("a") as out:
        out.write(json.dumps(result, allow_nan=False) + "\n")


# -- analysis ------------------------------------------------------------------------------------


def episode_flags(episode_dir: Path) -> set[str]:
    """What one kept gate episode shows, read from its native session log; several flags may apply."""
    episode = json.loads((episode_dir / "episode.json").read_text())
    if (episode.get("score") or 0.0) >= PASS_THRESHOLD:
        return {"pass"}
    expected = ANSWERS[episode["task"].split(maxsplit=1)[0]]
    number = re.compile(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])")
    outputs, replies, calls, bad_args = [], [], 0, False
    for line in (episode_dir / "session.jsonl").read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "tool/call":
            calls += 1
        elif kind == "tool/result":
            content = str(data.get("content") or "")
            outputs.append(content)
            bad_args = bad_args or (bool(data.get("is_error")) and "argument" in content)
        elif (
            kind == "assistant/message"
            and isinstance(data.get("content"), str)
            and data["content"].strip()
        ):
            replies.append(data["content"])
    final = replies[-1] if replies else None
    flags = set()
    if final is None:
        flags.add("turn ended on an empty reply")
    elif number.search(final):
        flags.add("right number, not alone on the last line")
    if any(number.search(o) for o in outputs) and not (final and number.search(final)):
        flags.add("had the right number in tool output, never reported it")
    if any(o.strip() == "exit 0" for o in outputs):
        flags.add("ran code that printed nothing")
    if bad_args:
        flags.add("called a tool with wrong arguments")
    if any("NameError" in o for o in outputs):
        flags.add("assumed state persisted between tool calls")
    if calls == 0:
        flags.add("never used a tool")
    return flags


def failure_flags(work: Path) -> dict[str, int]:
    """Flag counts over every kept gate episode that failed (multi-label, so counts overlap)."""
    counts: dict[str, int] = {}
    steps = work / "steps"
    if not steps.is_dir():
        return counts
    for episode_dir in sorted(steps.glob("*/*/episodes/*")):
        if not (episode_dir / "episode.json").exists():
            continue
        flags = episode_flags(episode_dir)
        if "pass" in flags:
            continue
        counts["failed episodes"] = counts.get("failed episodes", 0) + 1
        for flag in flags or {"no flag matched"}:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def _task_labels(tasks: list[str]) -> list[str]:
    return [task.split(maxsplit=1)[0] for task in tasks]


def _side_passes(
    rows: list[dict], side: str, labels: list[str], repeats: int, pass_threshold: float
) -> tuple[dict[str, list[bool]], int]:
    """Per-task pass booleans for one score-vector side, plus the count of missing episodes."""
    passes: dict[str, list[bool]] = {label: [] for label in labels}
    missing = 0
    expected_per_row = len(labels) * repeats if labels and repeats else 0
    for row in rows:
        scores = row.get(side)
        if isinstance(scores, (str, bytes, bytearray)) or not isinstance(scores, (list, tuple)):
            missing += expected_per_row
            continue
        for position, score in enumerate(scores):
            if not _is_usable_score(score):
                missing += 1
                continue
            index = position // repeats if repeats else 0
            if index < len(labels):
                passes[labels[index]].append(score >= pass_threshold)
    return passes, missing


def _rate(observations: list[bool]) -> float | None:
    """The pass rate, or None when nothing was observed (never a fabricated or NaN rate)."""
    return sum(observations) / len(observations) if observations else None


#: Settled rows carrying these gate reasons hold no calibration evidence: the evaluator never
#: finished (evaluator_error), the decision inputs were unusable (invalid_evaluation), or the
#: decision record itself failed to persist (decision_record_error). They stay in
#: attempted/settled/invalid counts but never enter the publish denominator.
_CALIBRATION_ERROR_REASONS = frozenset(
    {"evaluator_error", "invalid_evaluation", "decision_record_error"}
)


def _is_usable_score(value: object) -> bool:
    """A score that counts as observed evidence: numeric, non-bool, finite (zero counts)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _settled_row_is_valid(row: dict, *, expected_len: int | None = None) -> bool:
    """Count resolved comparisons, not outages or insufficient-evidence holds.

    Score vectors must have the expected shape and at least one usable pair.
    Gate-accepted partial evidence stays valid; missing episodes remain missing.
    """
    for key in ("wins", "losses", "ties"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
    reason = row.get("reason_code")
    gate = row.get("gate")
    gate_reason = gate.get("reason_code") if isinstance(gate, dict) else None
    if reason in _CALIBRATION_ERROR_REASONS or gate_reason in _CALIBRATION_ERROR_REASONS:
        return False
    if reason == "insufficient_evidence" or gate_reason == "insufficient_evidence":
        return False
    candidate = row.get("candidate_scores")
    current = row.get("current_scores")
    if isinstance(candidate, (str, bytes, bytearray)) or not isinstance(candidate, (list, tuple)):
        return False
    if isinstance(current, (str, bytes, bytearray)) or not isinstance(current, (list, tuple)):
        return False
    if not candidate or not current:
        return False
    if len(candidate) != len(current):
        return False
    if expected_len is not None and len(candidate) != expected_len:
        return False
    return any(
        _is_usable_score(candidate_score) and _is_usable_score(current_score)
        for candidate_score, current_score in zip(candidate, current, strict=True)
    )


def summarize(
    results: list[dict],
    *,
    tasks: list[str],
    repeats: int,
    condition: str,
    alpha: float,
    min_valid_pairs: int,
    pass_threshold: float = PASS_THRESHOLD,
) -> dict:
    """The calibration summary over recorded result rows; pure, so it is unit-testable."""
    labels = _task_labels(tasks)
    expected_len = len(tasks) * repeats
    attempted = list(results)
    settled = [r for r in attempted if r.get("status") == "settled"]
    skipped = [r for r in settled if r.get("skipped")]
    unresolved = [r for r in settled if not r.get("skipped")]
    invalid = [r for r in unresolved if not _settled_row_is_valid(r, expected_len=expected_len)]
    ran = [r for r in unresolved if _settled_row_is_valid(r, expected_len=expected_len)]
    n, published = len(ran), sum(1 for r in ran if r["published"])
    lo, hi = wilson(published, n)

    candidate_passes, candidate_missing = _side_passes(
        ran, "candidate_scores", labels, repeats, pass_threshold
    )
    current_passes, current_missing = _side_passes(
        ran, "current_scores", labels, repeats, pass_threshold
    )
    pooled = {label: candidate_passes[label] + current_passes[label] for label in labels}
    pooled_rates = {label: _rate(pooled[label]) for label in labels}
    side_rates = {
        "current": {label: _rate(current_passes[label]) for label in labels},
        "candidate": {label: _rate(candidate_passes[label]) for label in labels},
    }
    if condition == "aa":
        base, base_note = pooled_rates, "pooled both sides (the A/A law is shared)"
    else:
        base, base_note = (
            side_rates["current"],
            "current side only (the deliberately degraded skill)",
        )
    base_values = [value for value in base.values() if value is not None]
    base_available = bool(labels) and len(base_values) == len(labels)

    wlt: dict[str, int] = {}
    for r in ran:
        key = f"{r['wins']}/{r['losses']}/{r['ties']}"
        wlt[key] = wlt.get(key, 0) + 1
    observed_sign = sum(
        1 for r in ran if sign_test_p(int(r["wins"] or 0), int(r["losses"] or 0)) < alpha
    )

    # What each rule does with the measured pass rates: the original gate table, with the
    # configured alpha in both the label and the probability, and None (never NaN) when a
    # measured rate is missing.
    table = []
    for label, lift in (("null (A/A)", 0.0), ("+0.2 per task", 0.2), ("+0.4 per task", 0.4)):
        for rule, reps in (("majority", 1), ("majority", 5), ("sign", 5), ("sign", 10)):
            p_publish = (
                publish_probability(
                    [pair_law(p, min(1.0, p + lift)) for p in base_values for _ in range(reps)],
                    rule,
                    alpha,
                )
                if base_available
                else None
            )
            table.append(
                {
                    "candidate": label,
                    "gate": f"{'wins>losses' if rule == 'majority' else f'sign test p<{alpha:g}'}, {reps} ep/task",
                    "episodes": 2 * len(labels) * reps,
                    "p_publish": p_publish,
                }
            )

    # The prediction matching this run's rule and repeats: a null candidate for aa, the measured
    # per-side contrast for known-effect. Sign-rule only: the configured gate can additionally
    # veto or find insufficient evidence, so it can only publish less often than predicted.
    prediction, prediction_basis = None, None
    if condition == "aa":
        if base_available:
            prediction = publish_probability(
                [pair_law(p, p) for p in base_values for _ in range(repeats)], "sign", alpha
            )
            prediction_basis = (
                f"null candidate vs pooled measured per-task pass rates, sign rule at alpha={alpha:g}, "
                f"{repeats} ep/task"
            )
    else:
        current_values = [value for value in side_rates["current"].values() if value is not None]
        candidate_values = [
            value for value in side_rates["candidate"].values() if value is not None
        ]
        if labels and len(current_values) == len(candidate_values) == len(labels):
            prediction = publish_probability(
                [
                    pair_law(p_current, p_candidate)
                    for p_current, p_candidate in zip(current_values, candidate_values, strict=True)
                    for _ in range(repeats)
                ],
                "sign",
                alpha,
            )
            prediction_basis = (
                f"measured per-side pass rates (degraded current vs original tutorial candidate), "
                f"sign rule at alpha={alpha:g}, {repeats} ep/task"
            )
    interval_includes = None if prediction is None or n == 0 else bool(lo <= prediction <= hi)

    summary = {
        "condition": condition if condition == "aa" else "known-effect (NOT A/A)",
        "rule": {
            "alpha": alpha,
            "min_valid_pairs": min_valid_pairs,
            "pass_threshold": pass_threshold,
        },
        "trials_attempted": len(attempted),
        "trials_settled": len(settled),
        "trials_skipped": len(skipped),
        "trials_invalid": len(invalid),
        "publishes": published,
        "denominator": n,
        "publish_rate": (published / n) if n else None,
        "wilson95": [lo, hi] if n else None,
        "wlt_histogram": dict(sorted(wlt.items())),
        "pass_rate_by_task_pooled": {label: pooled_rates[label] for label in labels},
        "pass_rate_by_task_side": {
            side: {label: rates[label] for label in labels} for side, rates in side_rates.items()
        },
        "gate_table_base": base_note,
        "episodes_expected": 2 * len(tasks) * repeats * n,
        "episodes_scored": sum(
            len(candidate_passes[label]) + len(current_passes[label]) for label in labels
        ),
        "episodes_missing": candidate_missing + current_missing,
        "observed_trials_passing_sign_test": observed_sign,
        "median_trial_seconds": median(r["seconds"] for r in ran) if n else None,
        "gate_table": table,
        "predicted_publish_rate": prediction,
        "prediction_basis": prediction_basis if prediction is not None else None,
        "interval_includes_prediction": interval_includes,
        "prediction_note": (
            "sign-only prediction under measured pass-rate assumptions; the configured gate "
            f"(regression veto, min_valid_pairs={min_valid_pairs}) can only publish less often; "
            "a prediction is not a measurement"
        ),
    }
    if condition == "aa":
        published_rows = [r for r in ran if r["published"]]
        # No published rows means no observed candidate-tree identity: unknown, not vacuous truth.
        summary["all_published_trees_identical"] = (
            all(r["files_identical"] for r in published_rows) if published_rows else None
        )
    else:
        published_rows = [r for r in ran if r["published"]]
        summary["all_published_trees_changed"] = (
            all(not r["files_identical"] for r in published_rows) if published_rows else None
        )
    return summary


def read_decision_records(record_dir: Path) -> dict:
    """What the gate plugin's per-candidate decision JSONs in ``record_dir`` show.

    The record directory is dedicated to the plugin's decision records, so every ``*.json`` in
    it must carry reef_commit, reason_code, pairs and vetoes; a malformed record is reported
    as an error, never silently skipped. Timing fields (decision_seconds, evaluation_seconds)
    may honestly be None or absent when nothing was measured -- decide() records None for an
    externally provided measurement with no timing -- and stay unavailable, never coerced to
    0. Medians cover only observed finite nonnegative timings; missing counts are reported.
    Non-numeric, non-finite or negative timings stay malformed.
    """
    if not record_dir.is_dir():
        return {
            "count": 0,
            "decision_walltime_s": [],
            "decision_walltime_median_s": None,
            "evaluation_walltime_s": [],
            "evaluation_walltime_median_s": None,
            "decision_walltime_missing": 0,
            "evaluation_walltime_missing": 0,
            "reef_commits": [],
            "reason_codes": {},
        }
    records = []
    decision_observed: list[float] = []
    evaluation_observed: list[float] = []
    decision_missing = 0
    evaluation_missing = 0
    for path in sorted(record_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"malformed decision record {path}: {exc}") from exc
        if not isinstance(record, dict):
            raise SystemExit(f"malformed decision record {path}: expected a JSON object")
        for key in _DECISION_NUMBER_KEYS:
            value = record.get(key)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise SystemExit(
                    f"malformed decision record {path}: {key} must be a non-negative number, got {value!r}"
                )
        for key in _DECISION_TEXT_KEYS:
            value = record.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(
                    f"malformed decision record {path}: {key} must be a non-empty string"
                )
        for key in _DECISION_LIST_KEYS:
            if not isinstance(record.get(key), list):
                raise SystemExit(f"malformed decision record {path}: {key} must be a list")
        records.append(record)
        decision_value = record.get("decision_seconds")
        if decision_value is None:
            decision_missing += 1
        else:
            decision_observed.append(float(decision_value))
        evaluation_value = record.get("evaluation_seconds")
        if evaluation_value is None:
            evaluation_missing += 1
        else:
            evaluation_observed.append(float(evaluation_value))
    reason_codes: dict[str, int] = {}
    for record in records:
        code = record["reason_code"]
        reason_codes[code] = reason_codes.get(code, 0) + 1
    return {
        "count": len(records),
        "decision_walltime_s": decision_observed,
        "decision_walltime_median_s": median(decision_observed) if decision_observed else None,
        "evaluation_walltime_s": evaluation_observed,
        "evaluation_walltime_median_s": median(evaluation_observed)
        if evaluation_observed
        else None,
        "decision_walltime_missing": decision_missing,
        "evaluation_walltime_missing": evaluation_missing,
        "reef_commits": sorted({record["reef_commit"] for record in records}),
        "reason_codes": dict(sorted(reason_codes.items())),
    }


def analyze(work: Path) -> dict:
    """Summarize a work directory from its recorded artifacts alone; starts no services."""
    import yaml

    serve_path = work / "serve-aa.yaml"
    if not serve_path.is_file():
        raise SystemExit(
            f"work dir {work} has no serve-aa.yaml; tasks must come from the served recipe"
        )
    try:
        served_config = yaml.safe_load(serve_path.read_text())
        served_evolution = served_config["recipe"]["config"]["evolution"]
        served_tasks = list(served_evolution["tasks"])
    except Exception as exc:
        raise SystemExit(f"malformed served recipe {serve_path}: {exc}") from exc
    if not served_tasks or any(not isinstance(task, str) or not task for task in served_tasks):
        raise SystemExit(f"malformed served recipe {serve_path}: tasks must be non-empty strings")
    served_repeats = (
        served_evolution.get("episode_repeats", 1) if isinstance(served_evolution, dict) else 1
    )
    meta: dict = {}
    if (work / "run-meta.json").exists():
        loaded = json.loads((work / "run-meta.json").read_text())
        if not isinstance(loaded, dict):
            raise SystemExit(f"malformed run-meta.json in {work}: expected a JSON object")
        meta = loaded
        retained = meta.get("tasks")
        if retained is not None and (
            not isinstance(retained, (list, tuple)) or list(retained) != served_tasks
        ):
            raise SystemExit(
                f"contradictory tasks: run-meta.json does not match serve-aa.yaml in {work}"
            )
    else:
        meta = {
            "condition": "aa",
            "alpha": 0.05,
            "min_valid_pairs": 5,
            "pass_threshold": PASS_THRESHOLD,
        }
    tasks = served_tasks
    results_path = work / "results.jsonl"
    results = (
        [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]
        if results_path.exists()
        else []
    )
    summary = summarize(
        results,
        tasks=tasks,
        repeats=int(meta.get("repeats", served_repeats)),
        condition=str(meta.get("condition", "aa")),
        alpha=float(meta.get("alpha", 0.05)),
        min_valid_pairs=int(meta.get("min_valid_pairs", 5)),
        pass_threshold=float(meta.get("pass_threshold", PASS_THRESHOLD)),
    )
    summary["failure_flags"] = failure_flags(work)
    summary["decision_records"] = read_decision_records(work / "gate-decisions")
    summary["provenance"] = {
        key: meta[key]
        for key in (
            "condition",
            "selection",
            "model",
            "model_digest",
            "inference_kind",
            "ollama_url",
            "api_proxy_url",
            "reef_commit",
            "reef_root",
            "python",
            "plugin_src",
            "port",
            "workers",
            "repeats",
            "trials",
            "started_utc",
            "seed_source",
            "step_timeout_s",
        )
        if key in meta
    }
    (work / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def print_summary(summary: dict) -> None:
    printable = {key: value for key, value in summary.items() if key != "gate_table"}
    print(json.dumps(printable, indent=2, allow_nan=False))
    print(f"\n{'candidate':<16} {'gate':<34} {'episodes':>8} {'P(publish)':>10}")
    for row in summary["gate_table"]:
        value = "unavailable" if row["p_publish"] is None else f"{row['p_publish']:.3f}"
        print(f"{row['candidate']:<16} {row['gate']:<34} {row['episodes']:>8} {value:>10}")


# -- entry point ---------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--condition", choices=("aa", "known-effect"), default="aa")
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=5, help="evolution.episode_repeats per task")
    parser.add_argument("--model", default=None)
    parser.add_argument("--port", type=int, default=8911)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--selection",
        default="evallab_reef_gate.plugin:Factory",
        help="evolution.selection: a Reef builtin name or a module:attribute plugin factory reference",
    )
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="an explicit fresh owned directory"
    )
    parser.add_argument("--reef-root", type=Path, default=DEFAULT_REEF_ROOT)
    parser.add_argument(
        "--python",
        type=Path,
        default=None,
        help="the Reef interpreter (default <reef-root>/.venv/bin/python)",
    )
    upstream = parser.add_mutually_exclusive_group()
    upstream.add_argument("--ollama-url", default=None)
    upstream.add_argument("--api-proxy-url", help="parent-owned loopback budget-proxy base URL")
    parser.add_argument(
        "--api-proxy-token-env",
        default=DEFAULT_API_PROXY_TOKEN_ENV,
        help="names the capability environment passed as REEF_UPSTREAM_API_KEY (value never persisted)",
    )
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--min-valid-pairs", type=int, default=5)
    parser.add_argument("--regression-failure-threshold", type=int, default=1)
    parser.add_argument(
        "--seed-entries",
        type=Path,
        help="JSON list of entries to seed the scenario with instead of the tutorial's",
    )
    parser.add_argument("--analyze-only", action="store_true")
    return parser.parse_args(argv)


def _validate(args: argparse.Namespace, python: Path) -> None:
    if not 0.0 < args.alpha < 1.0:
        raise SystemExit(f"--alpha must lie strictly between 0 and 1, got {args.alpha}")
    for name in ("min_valid_pairs", "regression_failure_threshold", "trials", "repeats", "workers"):
        value = getattr(args, name)
        if value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be at least 1, got {value}")
    if ":" not in args.selection and args.selection not in REEF_BUILTIN_SELECTIONS:
        raise SystemExit(
            f"--selection {args.selection!r} is neither a Reef builtin nor a module:attribute reference"
        )
    if not python.is_file():
        raise SystemExit(f"the Reef interpreter {python} does not exist; pass --python explicitly")
    source_config = args.reef_root / SOURCE_CONFIG_RELATIVE
    if not source_config.is_file():
        raise SystemExit(f"the tutorial serve config is missing at {source_config}")
    if not (args.reef_root / "reef").is_dir():
        raise SystemExit(f"{args.reef_root} does not look like a Reef checkout (no reef/ package)")


def main(argv: list[str] | None = None) -> None:
    sys.dont_write_bytecode = True
    args = parse_args(argv)
    work = args.work_dir.expanduser().resolve()
    args.reef_root = args.reef_root.expanduser().resolve()
    if work.is_relative_to(args.reef_root):
        raise SystemExit("work-dir must not modify the Reef checkout or its environment")

    if args.analyze_only:
        if not work.is_dir():
            raise SystemExit(f"work dir {work} does not exist; nothing to analyze")
        print_summary(analyze(work))
        return

    if work.exists() and any(work.iterdir()):
        raise SystemExit(
            f"refusing to use non-empty work dir {work}: existing state is never erased; "
            "pass a fresh directory (or --analyze-only)"
        )
    work.mkdir(parents=True, exist_ok=True)

    python = (args.python or (args.reef_root / ".venv" / "bin" / "python")).expanduser()
    _validate(args, python)

    api_mode = args.api_proxy_url is not None
    if api_mode:
        if not args.model or not args.model.strip():
            raise SystemExit(
                "--model is required with --api-proxy-url; no default is assumed for proxy routing"
            )
        api_proxy_url = require_loopback_url(args.api_proxy_url)
        # Only the named capability is read; the value is held in memory and passed explicitly
        # to the Reef subprocess, never written to YAML, argv, metadata, or error text.
        api_capability = require_api_capability(args.api_proxy_token_env)
        upstream_url = api_proxy_url
        ollama_url = None
        model_entry = None
        inference_kind = INFERENCE_KIND_API_PROXY
    else:
        if args.model is None:
            args.model = "qwen2.5:7b"
        ollama_url = require_loopback_url(args.ollama_url or "http://127.0.0.1:11434")
        inventory = fetch_local_inventory(ollama_url)
        model_entry = require_local_model(inventory, args.model)
        (work / "ollama-inventory.json").write_text(
            json.dumps(
                {
                    "url": ollama_url,
                    "selected": model_entry,
                    "models": [
                        {
                            key: entry.get(key)
                            for key in ("name", "model", "digest", "size")
                            if key in entry
                        }
                        for entry in inventory["models"]
                        if isinstance(entry, dict)
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        upstream_url = ollama_url
        api_proxy_url = None
        api_capability = None
        inference_kind = INFERENCE_KIND_OLLAMA
    reef_commit = reef_checkout_commit(args.reef_root)
    seed_entries = json.loads(args.seed_entries.read_text()) if args.seed_entries else None

    serve, current_entry, candidate_entry, tasks = write_configs(
        work,
        reef_root=args.reef_root,
        upstream_url=upstream_url,
        port=args.port,
        model=args.model,
        workers=args.workers,
        repeats=args.repeats,
        seed_entries=seed_entries,
        selection=args.selection,
        condition=args.condition,
    )
    gate_config = write_gate_config(
        work,
        alpha=args.alpha,
        min_valid_pairs=args.min_valid_pairs,
        regression_failure_threshold=args.regression_failure_threshold,
        reef_commit=reef_commit,
    )
    run_meta = {
        "condition": args.condition,
        "selection": args.selection,
        "model": args.model,
        "model_digest": (model_entry.get("digest") if model_entry is not None else None),
        "inference_kind": inference_kind,
        "reef_commit": reef_commit,
        "reef_root": str(args.reef_root),
        "python": str(python),
        "plugin_src": str(PLUGIN_SRC),
        "port": args.port,
        "workers": args.workers,
        "repeats": args.repeats,
        "trials": args.trials,
        "alpha": args.alpha,
        "min_valid_pairs": args.min_valid_pairs,
        "pass_threshold": PASS_THRESHOLD,
        "regression_failure_threshold": args.regression_failure_threshold,
        "seed_source": str(args.seed_entries) if args.seed_entries else "tutorial",
        "current_answer_style_text": current_entry["config"]["text"],
        "candidate_answer_style_text": candidate_entry["config"]["text"],
        "step_timeout_s": STEP_TIMEOUT_S,
        "started_utc": datetime.now(UTC).isoformat(),
    }
    if api_mode:
        run_meta["api_proxy_url"] = api_proxy_url
    else:
        run_meta["ollama_url"] = ollama_url
    (work / "run-meta.json").write_text(json.dumps(run_meta, indent=2) + "\n")

    from reef_client import ReefClient

    server = start_reef(
        python, serve, args.port, work, args.reef_root, gate_config, api_capability=api_capability
    )
    failed = False
    try:
        client = ReefClient(f"http://127.0.0.1:{args.port}", token=TOKEN, timeout_s=300.0)
        if args.condition == "aa":
            name_scenario(client, args.model, SCENARIO_AA)
        else:
            # Every trial scenario is created before the first trial publishes, so each one
            # forks the pre-campaign degraded seed: a scenario created later would fork the
            # shared head after earlier publishes instead. Published candidates then advance
            # only their own scenario's tree, which the next trial never touches.
            name_known_effect_scenarios(client, args.model, args.trials)
        for index in range(1, args.trials + 1):
            scenario = trial_scenario(args.condition, index)
            result = run_trial(
                client,
                model=args.model,
                scenario=scenario,
                index=index,
                condition=args.condition,
                current_entry=current_entry,
                candidate_entry=candidate_entry,
            )
            record_result(work, result)
            if result["status"] != "settled":
                print(
                    f"trial {index:2d}: {result['status']}: stopping the campaign "
                    "(partial results kept for analysis)",
                    flush=True,
                )
                failed = True
                break
            verdict = "PUBLISHED" if result["published"] else (result["skipped"] or "rejected")
            print(
                f"trial {index:2d}: {verdict:<9} W/L/T {result['wins']}/{result['losses']}/{result['ties']} "
                f"cand {result['candidate_scores']} cur {result['current_scores']} "
                f"identical_tree={result['files_identical']} {result['seconds']}s",
                flush=True,
            )
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
    print_summary(analyze(work))
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
