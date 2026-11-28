from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from evallab.evidence._seat_bridge_html import extract_bridge_inline_js
from evallab.evidence.seat_unified_bridge import (
    BRIDGE_SCHEMA,
    UnifiedBridgeReport,
    analyze_unified_bridge,
    format_bridge_json,
    render_bridge_html,
    write_bridge_html,
)
from evallab.seat_parser import register_parser
from evallab.sim.engine import Simulation

_CDN_TAG_RE = re.compile(r"<(?:script|link)\b[^>]+(?:src|href)\s*=\s*['\"]https?://", re.IGNORECASE)
_CSP_RE = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"')
_BRIDGE_DATA_RE = re.compile(
    r'<script id="bridge-data" type="application/json">\s*(.*?)\s*</script>',
    re.S,
)


def _reject_nonfinite(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def _check_bridge_report_invariants(
    sim: Simulation,
    r1: UnifiedBridgeReport,
    j1: str,
    canary_secret: str,
    *,
    expect_decomp: bool,
) -> None:
    sim.require(r1.schema_version == BRIDGE_SCHEMA, "bridge_schema_version")
    sim.require(canary_secret not in j1, "bridge_symlink_isolation")
    parsed = json.loads(j1, parse_constant=_reject_nonfinite)
    sim.require(parsed.get("schema_version") == BRIDGE_SCHEMA, "bridge_json_roundtrip")
    sim.require(r1.n_trials == len(r1.trials) >= 1, "bridge_trials_nonempty")
    sim.require(r1.detectors.trials_scanned == r1.n_trials, "bridge_detectors_count_parity")
    sim.require(len(r1.detectors.detector_names) == 22, "bridge_22_detectors")
    sim.require(r1.validity.total_trials == r1.n_trials, "bridge_validity_count_parity")
    sim.require(
        r1.validity.measured + r1.validity.unmeasured == r1.validity.total_trials,
        "bridge_validity_partition",
    )
    sim.require(math.isfinite(r1.validity.unmeasured_rate), "bridge_validity_rate_finite")
    sim.require(r1.tidegauge_audit.available, "bridge_tidegauge_audit_available")
    if "olam_seats" in r1.ecosystems:
        sim.require(r1.muster_regrade.available, "bridge_olam_regrade_available")
    else:
        sim.require(not r1.muster_regrade.available, "bridge_bespoke_regrade_skipped")
    for tr in r1.trials:
        sim.require(bool(tr.trial_key and tr.trial_name), "bridge_trial_key_nonempty")
        sim.require(
            tr.validity_measured is not None and bool(tr.validity_reason),
            "bridge_trial_validity_joined",
        )
        sim.require(
            all(name in tr.detectors_counts for name in tr.detectors_triggered),
            "bridge_trial_detector_counts_consistent",
        )
    if expect_decomp:
        sim.require(len(r1.decompositions) == 1, "bridge_decompose_present")
        sim.require(r1.decompositions[0].tidegauge_decompose is not None, "bridge_tg_decomp_present")


def _check_bridge_html_invariants(
    sim: Simulation,
    report: UnifiedBridgeReport,
    scratch: Path,
    canary_secret: str,
) -> None:
    xss_tag = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'
    h1 = render_bridge_html(report, title=f"Bridge {xss_tag}")
    h2 = render_bridge_html(report, title=f"Bridge {xss_tag}")
    sim.require(h1 == h2, "bridge_html_byte_identical")
    sim.require(canary_secret not in h1, "bridge_html_symlink_isolation")
    sim.require(_CDN_TAG_RE.search(h1) is None, "bridge_html_zero_cdn")
    sim.require('<img src=x onerror="alert(1)">' not in h1, "bridge_html_xss_escaped")
    sim.require("</script><script>alert(2)" not in h1.lower(), "bridge_html_script_escaped")
    js = extract_bridge_inline_js(h1)
    sim.require(len(js) > 200 and "</script>" not in js.lower(), "bridge_html_js_intact")
    csp_m = _CSP_RE.search(h1)
    sim.require(csp_m is not None, "bridge_html_csp_present")
    if csp_m is not None:
        csp_val = csp_m.group(1)
        js_sha = base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode("ascii")
        script_part = csp_val.partition("script-src")[2].partition(";")[0]
        sim.require(
            "default-src 'none'" in csp_val
            and f"script-src 'sha256-{js_sha}'" in csp_val
            and "'unsafe-inline'" not in script_part,
            "bridge_html_strict_sha256_csp",
        )
    data_m = _BRIDGE_DATA_RE.search(h1)
    sim.require(data_m is not None, "bridge_html_data_script_present")
    if data_m is not None:
        doc = json.loads(data_m.group(1), parse_constant=_reject_nonfinite)
        sim.require(doc.get("schema_version") == BRIDGE_SCHEMA, "bridge_html_data_schema")
    out_path = write_bridge_html(report, scratch / "bridge-smoke.html", title="Bridge")
    sim.require(
        out_path.is_file() and out_path.read_text(encoding="utf-8") == render_bridge_html(report, title="Bridge"),
        "bridge_html_written",
    )


def _check_bridge_cli_parser(sim: Simulation, tdir: Path, scratch: Path) -> None:
    parser = argparse.ArgumentParser(prog="evallab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register_parser(sub)
    ns = parser.parse_args(
        [
            "seat",
            "bridge",
            str(tdir),
            "--json",
            "--html",
            str(scratch / "cli-bridge.html"),
            "--stall-seconds",
            "120",
        ]
    )
    sim.require(
        ns.seat_command == "bridge"
        and ns.json is True
        and ns.html == scratch / "cli-bridge.html"
        and ns.stall_seconds == 120.0,
        "bridge_cli_parser_args",
    )


def check_unified_bridge_invariants(
    sim: Simulation,
    tdir: Path,
    solo_dir: Path,
    silent_dir: Path,
    team_dir: Path,
    scratch: Path,
    canary_secret: str,
) -> None:
    sim.begin("unified_bridge", name=tdir.name)
    decomps = [("3-Arm", solo_dir, silent_dir, team_dir)]
    r1 = analyze_unified_bridge(tdir, decompositions=decomps)
    r2 = analyze_unified_bridge(tdir, decompositions=decomps)
    j1 = format_bridge_json(r1)
    j2 = format_bridge_json(r2)
    sim.require(j1 == j2, "bridge_json_byte_identical")
    _check_bridge_report_invariants(sim, r1, j1, canary_secret, expect_decomp=True)
    _check_bridge_html_invariants(sim, r1, scratch, canary_secret)
    _check_bridge_cli_parser(sim, tdir, scratch)
