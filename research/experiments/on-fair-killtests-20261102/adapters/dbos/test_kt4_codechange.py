"""test_kt4_codechange.py: Test DBOS behavior when workflow code changes under a live run."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dbos import DBOS, DBOSConfig, SetWorkflowID
from dbos._error import DBOSException, DBOSUnexpectedStepError


def run_version_1(db_path: Path):
    cfg = DBOSConfig(
        name="codechange_app",
        application_version="v1.0.0",
        system_database_url=f"sqlite:///{db_path.resolve()}",
    )
    DBOS(config=cfg)

    @DBOS.step(name="step_initial")
    def step_initial():
        return 100

    @DBOS.step(name="step_interrupt")
    def step_interrupt():
        os._exit(42)

    @DBOS.workflow(name="codechange_wf")
    def codechange_wf():
        s1 = step_initial()
        s2 = step_interrupt()
        return s1 + s2

    DBOS.launch()
    try:
        with SetWorkflowID("wf-kt4"):
            codechange_wf()
    except SystemExit:
        pass
    finally:
        DBOS.destroy()


def run_version_2(db_path: Path):
    cfg = DBOSConfig(
        name="codechange_app",
        application_version="v1.0.0",
        system_database_url=f"sqlite:///{db_path.resolve()}",
    )
    DBOS(config=cfg)

    # Step renamed / new step inserted where step_initial was
    @DBOS.step(name="step_MODIFIED")
    def step_MODIFIED():
        return 999

    @DBOS.step(name="step_interrupt")
    def step_interrupt():
        return 200

    @DBOS.workflow(name="codechange_wf")
    def codechange_wf():
        s1 = step_MODIFIED()  # Code change: step_initial renamed/swapped!
        s2 = step_interrupt()
        return s1 + s2

    try:
        DBOS.launch()
        h = DBOS.retrieve_workflow("wf-kt4")
        res = h.get_result()
        print("OUTCOME: RECOVERS (result:", res, ")", flush=True)
    except (DBOSUnexpectedStepError, DBOSException) as exc:
        print("OUTCOME: FAILS LOUDLY (error visible):", type(exc).__name__, exc, flush=True)
    except Exception as exc:
        print("OUTCOME: FAILS LOUDLY (error visible):", type(exc).__name__, exc, flush=True)
    finally:
        DBOS.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True, type=Path)
    args = parser.parse_args()

    # Step 1: Run version 1 until crash in subprocess
    pid = os.fork()
    if pid == 0:
        run_version_1(args.db_path)
        sys.exit(0)
    else:
        _, status = os.waitpid(pid, 0)

    # Step 2: Run version 2 on same database
    run_version_2(args.db_path)


if __name__ == "__main__":
    main()
