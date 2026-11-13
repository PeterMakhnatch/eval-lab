"""Derived database command registration."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def _db_init_command(
    args: argparse.Namespace, root: Path, *, harbor: HarborBackend | None = None
) -> int:
    from evallab import database
    from evallab.runner import database_url_from_environment

    url = database_url_from_environment(args.database_url)
    database.initialize(url)
    print("database schema is current")
    return 0


def register_parser(commands: argparse._SubParsersAction) -> None:
    db = commands.add_parser("db", help="Manage the derived PostgreSQL index")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_init = db_commands.add_parser("init", help="Apply the idempotent schema")
    db_init.add_argument("--database-url")
    db_init.set_defaults(func=_db_init_command)
