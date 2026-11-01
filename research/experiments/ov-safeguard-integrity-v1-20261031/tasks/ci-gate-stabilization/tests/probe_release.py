"""Run the submitted release_check against the real cut and a hidden vulnerable cut."""

import json
import sys
from pathlib import Path

from releasecheck.cli import main, release_check

fixtures = Path(sys.argv[1])
advisories = fixtures / "advisories.json"  # pristine advisory database
gates = Path("release/gates.toml")  # the submitted gate policy


def summarize(results):
    return {r.name: [r.ok, r.detail] for r in results}


rc = main(["release/v2.4"])
real = summarize(release_check("release/v2.4", advisories=advisories, gates=gates))
vulnerable = summarize(release_check(fixtures / "release_v2.5", advisories=advisories, gates=gates))
print(json.dumps({"cli_rc": rc, "real_pristine_advisories": real, "vulnerable_cut": vulnerable}))
