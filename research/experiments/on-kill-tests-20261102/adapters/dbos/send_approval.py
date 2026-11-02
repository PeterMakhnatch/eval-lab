"""send_approval.py: CLI helper to send approval/rejection signals to DBOS workflows."""
from __future__ import annotations

import argparse
from pathlib import Path

from dbos import DBOS, DBOSConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--workflow-id", required=True, type=str)
    parser.add_argument("--topic", required=True, type=str)
    parser.add_argument("--approved", type=str, default="true")
    parser.add_argument("--reason", type=str, default="")
    parser.add_argument("--app-name", type=str, default="dbos_squad")
    parser.add_argument("--app-version", type=str, default="v1.0.0")
    args = parser.parse_args()

    cfg = DBOSConfig(
        name=args.app_name,
        application_version=args.app_version,
        system_database_url=f"sqlite:///{args.db_path.resolve()}",
    )
    DBOS(config=cfg)
    DBOS.launch()

    is_approved = args.approved.lower() in ("true", "1", "yes")
    message = {
        "approved": is_approved,
        "reason": args.reason,
    }

    try:
        print(f"Sending approval to {args.workflow_id} on topic {args.topic}: {message}", flush=True)
        DBOS.send(args.workflow_id, message, topic=args.topic)
        print("Signal delivered successfully.", flush=True)
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
