"""Serve an unmodified MiMo system through AgentEnv's MCP and data planes."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import os
import sqlite3
from pathlib import Path

from agentenv_protocol import (
    AgentEnvEnvironment,
    DataPart,
    FilePart,
    add_data,
    environment_card,
    get_data,
    reset_data,
    tool,
)

WORK = Path(os.environ.get("BENCH_WORK", "/work"))
SYSTEM = os.environ["ENVIRONMENT_NAME"]
SYSTEMS = (
    "anaplan_workforce_compensation", "bluesky_approval_workflow",
    "depaul_compensation_audit_register", "workday_hcm",
)
WORLD_TOOL = "bluesky_approval_workflow_bench_bump_row_version"
if SYSTEM not in SYSTEMS:
    raise ValueError(f"Unknown MiMo system: {SYSTEM}")


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Upstream mcp_http's annotation/body/default inference and result cap are reused
# verbatim from the hash-verified world, instead of implementing a second schema.
upstream = load(Path("/app/mcp_http.py"), "mimo_schema")
module = upstream._load_tools_module(WORK / "tools" / f"{SYSTEM}.py")


class MiMoSystem(AgentEnvEnvironment):
    def __init__(self):
        self.db = WORK / "system" / SYSTEM / "state.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    @reset_data
    async def reset(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.db.with_name(self.db.name + suffix).unlink(missing_ok=True)

    @add_data
    async def add(self, parts: list) -> None:
        for part in parts:
            if isinstance(part, FilePart):
                payload = Path(part.file.uri.removeprefix("file://")).read_bytes()
            elif isinstance(part, DataPart):
                payload = base64.b64decode(part.data["db_base64"], validate=True)
            else:
                raise ValueError("MiMo state requires SQLite bytes")
            if not payload.startswith(b"SQLite format 3\x00"):
                raise ValueError("State artifact is not a SQLite database")
            self.db.write_bytes(payload)

    @get_data
    async def state(self) -> list:
        # MiMo commits/closes each operation. Backup also covers any WAL state.
        snapshot = self.db.with_name("export.db")
        try:
            with (
                sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as source,
                sqlite3.connect(snapshot) as target,
            ):
                source.backup(target)
            payload = snapshot.read_bytes()
        finally:
            snapshot.unlink(missing_ok=True)
        # Preserve exact file bytes when no WAL exists: hash isolation checks use
        # the pristine non-BlueSky DB hashes, not sqlite backup layout hashes.
        if not self.db.with_name("state.db-wal").exists():
            payload = self.db.read_bytes()
        return [DataPart(data={
            "system": SYSTEM, "db_base64": base64.b64encode(payload).decode(),
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        })]


def bind(fn):
    wrapped = upstream._wrap_capped(fn)
    def invoke(self, *args, **kwargs):
        return wrapped(*args, **kwargs)
    invoke.__name__ = fn.__name__
    invoke.__doc__ = fn.__doc__
    invoke.__annotations__ = wrapped.__annotations__
    signature = inspect.signature(wrapped)
    invoke.__signature__ = signature.replace(parameters=[
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        *signature.parameters.values(),
    ])
    return tool(name=f"{SYSTEM}_{fn.__name__}")(invoke)


for name, fn in upstream._public_functions(module):
    setattr(MiMoSystem, name, bind(fn))

if SYSTEM == "bluesky_approval_workflow":
    @tool(name=WORLD_TOOL)
    def bump(self, case_id: str) -> dict:
        """World-only concurrent edit: increment version, preserving every other column and event."""
        with sqlite3.connect(self.db) as connection:
            cursor = connection.execute(
                "UPDATE merit_approval_cases SET row_version=row_version+1 WHERE case_id=?",
                (case_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown case: {case_id}")
            version = connection.execute(
                "SELECT row_version FROM merit_approval_cases WHERE case_id=?", (case_id,)
            ).fetchone()[0]
        return {"case_id": case_id, "row_version": version}
    MiMoSystem.bench_bump_row_version = bump

MiMoSystem = environment_card(name=SYSTEM)(MiMoSystem)
if __name__ == "__main__":
    MiMoSystem().serve()
