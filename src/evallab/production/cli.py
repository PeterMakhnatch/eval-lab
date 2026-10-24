"""Production queue commands, with dashboard-only serving as the safe default.

``prod ingest`` admits public DEV records through the registry; sealed TEST and
heldout records remain excluded. ``prod status`` only reads durable queue state.
``prod run-once`` explicitly dispatches one selected worker cycle and one relay
pass. ``prod serve`` dispatches neither unless ``--workers`` is present.

Operator credentials are environment-only: EVALLAB_OPERATOR_TOKEN and
EVALLAB_OPERATOR_ID. An unset token leaves interventions disabled. Seeker model
generation requires an explicit existing subscription driver; ambient model
variables never choose a driver. Broker outages leave the local outbox durable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_CONNECTION_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|nats|tls|ws|wss)://[^\s\"'<>]+")


def _queue_options(parser: argparse.ArgumentParser) -> None:
    # Suppressed child defaults preserve options supplied before the subcommand.
    parser.add_argument(
        "--repo-root", type=Path, default=argparse.SUPPRESS,
        help="Corpus/source root (default: the current Eval Lab workspace)",
    )
    parser.add_argument(
        "--database", default=argparse.SUPPRESS,
        help="PostgreSQL DSN or SQLite path (default: derived/production/prod-queue.sqlite)",
    )
    parser.add_argument(
        "--sqlite-fallback", type=Path, default=argparse.SUPPRESS,
        help="Initial PostgreSQL connection fallback (default: the root-derived SQLite queue)",
    )


def _worker_options(parser: argparse.ArgumentParser, systems: tuple[str, ...]) -> None:
    parser.add_argument(
        "--systems", choices=systems, nargs="+", default=systems,
        help="Live consumers to run (default: all three; serve also requires --workers)",
    )
    parser.add_argument("--worker-id", default="production")
    parser.add_argument(
        "--work-root", type=Path,
        help="Execution artifacts (default: derived/production/work under --repo-root)",
    )
    parser.add_argument(
        "--seeker-driver", choices=("sonnet", "codex"),
        help="Opt in to an existing OMP subscription lane (default: supplied candidates only)",
    )
    parser.add_argument(
        "--nats-url", help="JetStream broker URL; omitted/unavailable keeps the outbox unpublished",
    )


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return port


def register_parser(commands: Any) -> None:
    """Register deterministically without opening databases, brokers, or models."""
    from evallab.production.corpus_queue import WORKER_SYSTEMS

    production = commands.add_parser(
        "prod", help="Admit, inspect, run, and serve the durable production queue",
        description=(
            "Public DEV production lane. TEST/heldout records remain sealed. "
            "Serving is read-only unless --workers is explicit. All receipts are JSON."
        ),
    )
    production.set_defaults(repo_root=None, database=None, sqlite_fallback=None)
    _queue_options(production)
    actions = production.add_subparsers(dest="prod_command", required=True)
    ingest = actions.add_parser("ingest", help="Admit and seed public DEV corpora idempotently")
    _queue_options(ingest)
    ingest.add_argument(
        "--dataset", dest="dataset_ids", action="append",
        help="Registry dataset ID; repeat to select several (default: all public DEV corpora)",
    )
    ingest.add_argument("--run-id", default="production-seed")
    ingest.set_defaults(func=_command)

    status = actions.add_parser("status", help="Read persistent queue depth without dispatching")
    _queue_options(status)
    status.set_defaults(func=_command)

    once = actions.add_parser("run-once", help="Run selected live consumers, then relay once")
    _queue_options(once)
    _worker_options(once, WORKER_SYSTEMS)
    once.set_defaults(func=_command)

    serve = actions.add_parser(
        "serve", help="Serve the live control plane (dashboard only unless --workers)",
    )
    _queue_options(serve)
    _worker_options(serve, WORKER_SYSTEMS)
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: loopback)")
    serve.add_argument("--port", type=_port, default=8765)
    serve.add_argument(
        "--workers", action="store_true",
        help="Explicitly start in-process selected worker and outbox relay loops",
    )
    serve.add_argument("--interval", type=_positive_seconds, default=1.0)
    serve.set_defaults(func=_command)


def _resolve(root: Path, path: Path) -> Path:
    path = path.expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _database(root: Path, value: str | None) -> str | Path:
    if value is None:
        return root / "derived/production/prod-queue.sqlite"
    if value.startswith(("postgresql://", "postgres://")):
        return value
    if value == ":memory:":
        return value
    if value.startswith("sqlite:///"):
        value = value[len("sqlite:///"):]
    return _resolve(root, Path(value))


def _open_queue(args: argparse.Namespace, root: Path) -> Any:
    from evallab.production.corpus_queue import CorpusRegistry
    from evallab.production.relay import open_production_queue

    fallback = _resolve(root, args.sqlite_fallback or Path("derived/production/prod-queue.sqlite"))
    return open_production_queue(
        _database(root, args.database), registry=CorpusRegistry(root), sqlite_fallback=fallback,
    )


def _runtime(args: argparse.Namespace, root: Path, queue: Any) -> Any:
    from evallab.production.runtime import ProductionRuntime

    return ProductionRuntime(
        queue, repo_root=root,
        work_root=_resolve(root, args.work_root or Path("derived/production/work")),
        systems=tuple(dict.fromkeys(args.systems)), worker_id=args.worker_id,
        seeker_driver=args.seeker_driver,
    )


def _relay(args: argparse.Namespace, queue: Any) -> Any:
    from evallab.production.relay import OutboxRelay

    return OutboxRelay(queue, nats_url=args.nats_url)


def _emit(payload: dict[str, Any], args: argparse.Namespace, *, error: bool = False) -> None:
    from evallab.explorer import redact_mapping, redact_text

    secrets = {
        value for key, value in os.environ.items()
        if value and any(marker in key.upper() for marker in (
            "API_KEY", "ACCESS_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
        ))
    }
    for value in (args.database, getattr(args, "nats_url", None)):
        if value and "://" in value:
            secrets.add(value)
            try:
                parsed = urlsplit(value)
                if parsed.password:
                    secrets.add(unquote(parsed.password))
                elif parsed.scheme in {"nats", "tls"} and parsed.username:
                    secrets.add(unquote(parsed.username))
            except ValueError:
                pass

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return redact_text(_CONNECTION_URL.sub("[redacted-connection]", value))
        if isinstance(value, Mapping):
            return {str(key): scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    print(
        json.dumps(scrub(redact_mapping(payload)), sort_keys=True, allow_nan=False),
        file=sys.stderr if error else sys.stdout, flush=True,
    )


def _snapshot(connection: Any) -> dict[str, Any]:
    return {
        "backend": connection.backend,
        "fallback_reason": connection.fallback_reason,
        "queue": connection.queue.queue_snapshot(),
    }


async def _cycle(runtime: Any, relay: Any, connection: Any) -> dict[str, Any]:
    result = await asyncio.to_thread(runtime.run_once)
    publication = await relay.run_once()
    return {"runtime": result, "relay": publication, **_snapshot(connection)}


async def _run_once(args: argparse.Namespace, root: Path, connection: Any) -> dict[str, Any]:
    async with AsyncExitStack() as resources:
        runtime = _runtime(args, root, connection.queue)
        resources.callback(runtime.close)
        relay = _relay(args, connection.queue)
        resources.push_async_callback(relay.close)
        return await _cycle(runtime, relay, connection)


async def _worker_loop(
    args: argparse.Namespace, runtime: Any, relay: Any, connection: Any,
    server: Any, stop: asyncio.Event,
) -> None:
    # A failed HTTP bind must not start dispatch. Shutdown waits for in-flight
    # synchronous consumers before closing their journal or runtime resources.
    while not server.started:
        if stop.is_set() or server.should_exit:
            return
        await asyncio.sleep(0.01)
    while not stop.is_set() and not server.should_exit:
        receipt = await _cycle(runtime, relay, connection)
        failed = receipt["runtime"].get("ok") is False
        _emit({"command": "serve", "event": "worker_cycle", **receipt}, args, error=failed)
        if failed:
            systems = ", ".join(sorted(receipt["runtime"].get("failures", {})))
            raise RuntimeError(f"Production worker cycle failed: {systems or 'see cycle receipt'}")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=args.interval)


async def _http_server(server: Any) -> None:
    try:
        await server.serve()
    except SystemExit as exc:
        # Uvicorn exits on a bind failure; keep that failure inside the supervised
        # task so worker resources still close and the CLI emits a JSON error.
        raise RuntimeError("Production HTTP server failed to start") from exc


async def _serve(args: argparse.Namespace, root: Path, connection: Any) -> None:
    import uvicorn

    from evallab.production.control_plane import create_app

    class ManagedServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self) -> Iterator[None]:
            # Uvicorn normally replays SIGTERM before serve() returns. This
            # supervisor must first finish consumers and close durable resources.
            if threading.current_thread() is not threading.main_thread():
                yield
                return
            handlers = {
                sig: signal.signal(sig, self.handle_exit)
                for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                yield
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    token = os.environ.get("EVALLAB_OPERATOR_TOKEN")
    tokens = {token: os.environ.get("EVALLAB_OPERATOR_ID", "operator")} if token else {}
    app = create_app(connection.queue, operator_tokens=tokens)
    server = ManagedServer(uvicorn.Config(
        app, host=args.host, port=args.port, log_level="warning", access_log=False,
        timeout_graceful_shutdown=10.0,
    ))
    async with AsyncExitStack() as resources:
        runtime = relay = None
        if args.workers:
            runtime = _runtime(args, root, connection.queue)
            resources.callback(runtime.close)
            relay = _relay(args, connection.queue)
            resources.push_async_callback(relay.close)
        _emit({
            "command": "serve", "event": "starting", "host": args.host, "port": args.port,
            "workers": args.workers, "systems": list(dict.fromkeys(args.systems)) if args.workers else [],
            **_snapshot(connection),
        }, args)
        stop = asyncio.Event()
        tasks = [asyncio.create_task(_http_server(server), name="production-http")]
        if args.workers:
            tasks.append(asyncio.create_task(
                _worker_loop(args, runtime, relay, connection, server, stop),
                name="production-workers",
            ))
        failure: BaseException | None = None
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BaseException as exc:
            failure = exc
        finally:
            stop.set()
            server.should_exit = True
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and failure is None:
                    failure = result
        if failure is not None:
            raise failure
        if not server.started:
            raise RuntimeError("Production HTTP server stopped before startup completed")


def _command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    """Honor the existing CLI's integer return convention, with redacted JSON errors."""
    try:
        root = _resolve(root, args.repo_root) if args.repo_root is not None else root.resolve()
        with _open_queue(args, root) as connection:
            if args.prod_command == "ingest":
                from evallab.production.ingest import ingest_corpus

                result = ingest_corpus(
                    connection.queue, dataset_ids=args.dataset_ids, run_id=args.run_id,
                )
                receipt = {"ingest": result, **_snapshot(connection)}
            elif args.prod_command == "status":
                receipt = _snapshot(connection)
            elif args.prod_command == "run-once":
                receipt = asyncio.run(_run_once(args, root, connection))
            elif args.prod_command == "serve":
                asyncio.run(_serve(args, root, connection))
                receipt = {"event": "stopped", **_snapshot(connection)}
            else:
                raise ValueError("Unknown production command")
            _emit({"command": args.prod_command, **receipt}, args)
            return 1 if receipt.get("runtime", {}).get("ok") is False else 0
    except KeyboardInterrupt:
        _emit({"command": args.prod_command, "event": "interrupted"}, args, error=True)
        return 130
    except Exception as exc:
        _emit({
            "command": args.prod_command,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, args, error=True)
        return 2
