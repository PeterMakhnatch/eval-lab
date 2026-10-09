"""External sidecar runtime. Target cgroup only; no in-sandbox sensor."""
from __future__ import annotations

import codecs
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.flight.gateway import MetadataGateway, PcapStream, dns_metadata

STOP = False

# Linux syscall ABI numbers stay Linux when fixtures are decoded on macOS.
LINUX_ERRNOS = errno.errorcode if sys.platform.startswith("linux") else {
    1: "EPERM", 2: "ENOENT", 9: "EBADF", 11: "EAGAIN", 13: "EACCES",
    22: "EINVAL", 38: "ENOSYS", 98: "EADDRINUSE", 99: "EADDRNOTAVAIL",
    101: "ENETUNREACH", 104: "ECONNRESET", 110: "ETIMEDOUT",
    111: "ECONNREFUSED", 114: "EALREADY", 115: "EINPROGRESS",
}
LINUX_O_NOATIME = 0x40000

MAX_ERRORS = 30
CMDLINE_LIMIT = 64 * 1024
PROCESS_TABLE_MAX = 8192
PROCESS_ARGS_KEPT = 16
PROCESS_ARG_MAX = 200
PENDING_MAX = 16384
SNAPSHOT_MAX_DEPTH = 512
SNAPSHOT_ENTRY_LIMIT = 50000
HASH_BYTE_LIMIT = 8 * 1024 * 1024
# bpftrace 0.17 caps BPFTRACE_STRLEN at 200, and the 512-byte BPF stack caps
# the working combination further: STRLEN=160 with a 64-byte DNS window is the
# largest combination that loads (200/64 overflows sendto). str() fields clip
# at STRLEN; the decoder flags fields at the limit. Full names stay in snapshots.
PROBE_STRLEN = int(os.environ.get("BPFTRACE_STRLEN", "160"))
# Probe-side DNS payload window (fits the 512-byte BPF stack beside STRLEN=160
# scratch): larger payloads are parsed, not exported whole.
DNS_PAYLOAD_WINDOW = 64
NONCE_RE = re.compile(r"[0-9a-fA-F]{32,64}")

# Total field counts for decode_probe input ([kind, nsec, cg, pid, tid, uid,
# comm, *rest]); exact arity so an embedded delimiter can never reshape a row.
TRACE_ARITY = {
    "exec": 9, "exit": 8, "file": 10, "read": 9, "read_result": 8,
    "write": 9, "write_result": 8, "connect": 11, "connect_result": 8,
    "udp_send": 13, "udp_send_result": 8, "dns_receive": 10,
}


def _record_error(errors: list[str], tally: Counter[str], message: str) -> None:
    """Bound the errors list at MAX_ERRORS; overflow lands in errors_dropped."""
    if len(errors) >= MAX_ERRORS:
        del errors[0]
        tally["errors_dropped"] += 1
    errors.append(message[:500])


def render_probe_template(template: str, cgid: int, nonce: str) -> str:
    """Substitute both placeholders; fail closed on leftovers or a weak nonce."""
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("probe nonce must be 32-64 hex chars")
    rendered = template.replace("@@CGID@@", str(cgid)).replace("@@NONCE@@", nonce)
    if "@@" in rendered:
        raise ValueError("probe template placeholders unresolved")
    return rendered


def fresh_nonce() -> str:
    """Per-run probe prefix, kept host-side; the sandbox must never learn it."""
    return secrets.token_hex(16)

def descendant_pids(cgroup_path: str, base: str = "/sys/fs/cgroup") -> set[int]:
    """PIDs living in descendant cgroups of the target (untraced by design).

    The trace predicate matches the target cgroup id exactly, so a process that
    enters a child cgroup goes blind. Walk from the host cgroupns view (the
    observer runs with --cgroupns=host) and report; callers degrade to partial
    with child_cgroup_unobserved rather than staying silently blind.
    """
    found: set[int] = set()
    root = Path(base + cgroup_path)
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as children:
                    entries = list(children)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                except OSError:
                    continue
            procs = current / "cgroup.procs"
            if current != root:
                try:
                    for line in procs.read_text().splitlines():
                        try:
                            found.add(int(line.strip()))
                        except ValueError:
                            continue
                except OSError:
                    continue
    except OSError:
        pass
    return found


def stop(_signal: int, _frame: Any) -> None:
    global STOP
    STOP = True


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def _cgroup_at(process_fd: int) -> tuple[str, int]:
    with os.fdopen(os.open("cgroup", os.O_RDONLY, dir_fd=process_fd)) as handle:
        for line in handle:
            fields = line.rstrip().split(":", 2)
            if fields[:2] == ["0", ""] and fields[2] != "/":
                path = fields[2]
                if ".." in Path(path).parts:
                    raise RuntimeError("observer requires --cgroupns=host")
                return path, Path("/sys/fs/cgroup" + path).stat().st_ino
    raise RuntimeError("target cgroup v2 identity unavailable; refusing global tracing")


def _process_fd(pid: int, expected_cgid: int) -> int | None:
    """Pin a proc entry and recheck identity before reading argv/fd links."""
    try:
        process_fd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        if _cgroup_at(process_fd)[1] == expected_cgid:
            return process_fd
    except (OSError, RuntimeError):
        pass
    os.close(process_fd)
    return None


def proc_path(pid: int, fd: int, cgid: int, path: str = "") -> str:
    if path.startswith("/"):
        return path
    process_fd = _process_fd(pid, cgid)
    if process_fd is None:
        return path
    try:
        base = os.readlink(f"fd/{fd}" if fd >= 0 else "cwd", dir_fd=process_fd)
        return os.path.normpath(os.path.join(base, path)) if path else base
    except OSError:
        return path
    finally:
        os.close(process_fd)


def hash_file_bounded(fd: int, max_bytes: int) -> tuple[str | None, bool]:
    """Hash at most max_bytes+1 bytes; True flags a truncated (unhashed) file."""
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, min(65536, max_bytes + 1 - total))
        if not chunk:
            return digest.hexdigest(), False
        total += len(chunk)
        if total > max_bytes:
            return None, True
        digest.update(chunk)


def snapshot_full(root_fd: int, relative_root: str,
                  max_bytes: int = HASH_BYTE_LIMIT) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk only beneath a pinned target root, never follow sandbox symlinks.

    Iterative explicit-stack walk with a depth cap: degrades to partial with a
    reason instead of ever raising RecursionError. Hash reads are bounded at
    max_bytes+1 so a file that grows mid-walk cannot cause an unbounded read.
    """
    entries: dict[str, Any] = {}
    meta: dict[str, Any] = {"status": "complete", "reason": None, "hash_truncated": 0}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | LINUX_O_NOATIME
    directory_fd = os.dup(root_fd)
    try:
        for part in relative_root.strip("/").split("/"):
            child = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
    except OSError:
        os.close(directory_fd)
        return entries, meta

    def partial(reason: str) -> None:
        meta["status"] = "partial"
        if meta["reason"] is None:
            meta["reason"] = reason

    stack: list[tuple[int, str, int]] = [(directory_fd, "", 0)]
    directory_fd = -1  # ownership moves to the stack; drained in finally
    try:
        while stack:
            parent_fd, prefix, depth = stack.pop()
            try:
                with os.scandir(parent_fd) as children:
                    for entry in children:
                        if len(entries) >= SNAPSHOT_ENTRY_LIMIT:
                            partial(f"entry limit {SNAPSHOT_ENTRY_LIMIT} reached at {prefix or '/'}")
                            break
                        name = f"{prefix}/{entry.name}" if prefix else entry.name
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                if depth >= SNAPSHOT_MAX_DEPTH:
                                    partial(f"depth cap {SNAPSHOT_MAX_DEPTH} exceeded at {name}")
                                    continue
                                stack.append((os.open(entry.name, directory_flags, dir_fd=parent_fd),
                                              name, depth + 1))
                            elif stat.S_ISREG(info.st_mode):
                                file_fd = os.open(entry.name,
                                                  os.O_RDONLY | os.O_NOFOLLOW | LINUX_O_NOATIME,
                                                  dir_fd=parent_fd)
                                try:
                                    info = os.fstat(file_fd)
                                    if info.st_size > max_bytes:
                                        meta["hash_truncated"] += 1
                                        digest = None
                                    else:
                                        digest, truncated = hash_file_bounded(file_fd, max_bytes)
                                        if truncated:
                                            meta["hash_truncated"] += 1
                                finally:
                                    os.close(file_fd)
                                entries[name] = [info.st_size, info.st_mtime_ns, digest]
                        except OSError:
                            continue
            finally:
                os.close(parent_fd)
    finally:
        for pending_fd, _, _ in stack:
            with contextlib.suppress(OSError):
                os.close(pending_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
    return entries, meta


def snapshot(root_fd: int, relative_root: str, max_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    """Walk only beneath a pinned target root, never follow sandbox symlinks."""
    entries, _meta = snapshot_full(root_fd, relative_root, max_bytes)
    return entries


def difference(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {"added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "modified": sorted(key for key in before.keys() & after.keys() if before[key] != after[key]),
            "snapshot_entry_limit": SNAPSHOT_ENTRY_LIMIT, "hash_byte_limit": HASH_BYTE_LIMIT,
            "snapshot_truncated": len(before) >= SNAPSHOT_ENTRY_LIMIT or len(after) >= SNAPSHOT_ENTRY_LIMIT}


def decode_probe(fields: list[str], cgid: int, wall: Callable[[int], str]) -> dict[str, Any] | None:
    try:
        kind = fields[0]
    except IndexError:
        raise ValueError("empty probe fields") from None
    expected = TRACE_ARITY.get(kind)
    if expected is None:
        raise ValueError(f"unknown probe kind {kind!r}")
    if len(fields) != expected:
        raise ValueError(f"wrong arity for {kind}: got {len(fields)}, want {expected}")
    kind, nsec, cg, pid, tid, uid, comm, *rest = fields
    if int(cg) != cgid:
        return None
    row: dict[str, Any] = {"plane": "kernel", "kind": kind, "ts": wall(int(nsec)),
                          "pid": int(pid), "tid": int(tid), "uid": int(uid), "comm": comm,
                          "cgroup_id": int(cg)}
    if kind == "exec":
        row.update(ppid=int(rest[0]), path=rest[1])
        # bpftrace str() clips at BPFTRACE_STRLEN; a field at the limit may be
        # clipped, so flag it instead of presenting it as complete.
        row["path_truncated"] = len(rest[1]) >= PROBE_STRLEN - 1
        row["args"] = []
        row["args_truncated"] = False
        process_fd = _process_fd(int(pid), cgid)
        if process_fd is not None:
            try:
                with os.fdopen(os.open("cmdline", os.O_RDONLY, dir_fd=process_fd), "rb") as handle:
                    raw = handle.read(CMDLINE_LIMIT + 1)
                    if len(raw) > CMDLINE_LIMIT:
                        row["args_truncated"] = True
                        raw = raw[:CMDLINE_LIMIT]
                    row["args"] = [s.decode("utf-8", "replace") for s in raw.split(b"\0") if s]
            except OSError:
                pass
            finally:
                os.close(process_fd)
        row["args_source"] = "proc-best-effort"
    elif kind == "exit":
        row["exit_code"] = int(rest[0])
    elif kind == "file":
        fd, flags, path = int(rest[0]), int(rest[1]), rest[2]
        row.update(path=path, absolute_path=proc_path(int(pid), fd, cgid, path), dirfd=fd, flags=flags,
                   access=("read", "write", "read_write", "unknown")[flags & 3],
                   path_truncated=len(path) >= PROBE_STRLEN - 1)
    elif kind in ("read", "write"):
        fd, size = int(rest[0]), int(rest[1])
        row.update(fd=fd, bytes_requested=size, path=proc_path(int(pid), fd, cgid))
    elif kind == "dns_receive":
        row["fd"] = int(rest[0])
        row["bytes_received"] = int(rest[1])
        # The probe exports a bounded payload window, never whole bodies.
        row["payload_truncated"] = int(rest[1]) > DNS_PAYLOAD_WINDOW
        row["dns"] = dns_metadata(codecs.escape_decode(rest[2].encode())[0])
        row["dns_source"] = "sys_exit_recvfrom"
    elif kind in ("connect", "udp_send"):
        fd, family, port, address = map(int, rest[:4])
        row.update(fd=fd, family=family, port=socket.ntohs(port) if family == 2 else port,
                   dest=socket.inet_ntoa(struct.pack("<I", address)) if family == 2 else None)
        if kind == "udp_send":
            row["bytes_requested"] = int(rest[4])
            row["payload_truncated"] = int(rest[4]) > DNS_PAYLOAD_WINDOW
            # Raw bytes are used only to parse a DNS allowlist; never exported.
            escaped = rest[5]
            try:
                raw = codecs.escape_decode(escaped.encode())[0]
                metadata = dns_metadata(raw)
            except ValueError:
                metadata = None
            if metadata is not None:
                row["dns"] = metadata
                row["dns_source"] = "sys_enter_sendto"
        row["outcome"] = "not-returned"
    else:  # kind.endswith("_result")
        code = int(rest[0])
        outcome = "success" if code >= 0 else "failure"
        if kind == "connect_result" and -code in (115, 114):
            outcome = "in_progress"
        row.update(return_code=code, errno=LINUX_ERRNOS.get(-code, f"ERRNO_{-code}") if code < 0 else None,
                   outcome=outcome)
    return row


def parse_trace_line(text: str, *, nonce: str, cgid: int,
                     wall: Callable[[int], str]) -> tuple[str, dict[str, Any] | None, str | None]:
    """Classify one stdout line from bpftrace.

    Returns (disposition, row, note) with disposition in {"event", "ready",
    "heartbeat", "info", "malformed", "tamper"}. Anything the sandbox can
    influence must carry the nonce prefix with exact arity; otherwise it is
    rejected and counted, never decoded into a row. "info" is bpftrace's own
    startup chatter (no row can be forged from it, so it is ignored uncounted).
    """
    if text.startswith("Attaching ") and text.rstrip().endswith("probes..."):
        return ("info", None, None)
    fields = text.split("\t")
    if len(fields) < 2 or fields[0] != nonce:
        return ("tamper", None, "line without probe nonce")
    marker = fields[1]
    if marker == "READY":
        return ("ready", None, None) if len(fields) == 2 else ("malformed", None, "READY arity")
    if marker == "HEARTBEAT":
        return ("heartbeat", None, None) if len(fields) == 2 else ("malformed", None, "HEARTBEAT arity")
    if marker != "E":
        return ("malformed", None, f"unknown marker {marker!r}")
    try:
        row = decode_probe(fields[2:], cgid, wall)
    except (ValueError, IndexError) as exc:
        return ("malformed", None, f"parse: {exc}")
    if row is None:
        return ("tamper", None, "cgroup mismatch on nonce-bearing line")
    return ("event", row, None)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    target = int(os.environ["TARGET_PID"])
    out = Path(os.environ.get("OUT", "/out"))
    out.mkdir(parents=True, exist_ok=True)
    phase = os.environ.get("PHASE", "agent")
    identity = json.loads(os.environ.get("IDENTITY", "{}"))
    enforcement = os.environ.get("ENFORCEMENT", "network_mode_none")
    roots = ("/app", "/tmp", "/tests", "/logs/verifier")
    process_fd = os.open(f"/proc/{target}", os.O_RDONLY | os.O_DIRECTORY)
    try:
        cgroup_path, cgid = _cgroup_at(process_fd)
        expected_container = os.environ["TARGET_CONTAINER_ID"]
        if expected_container not in cgroup_path:
            raise RuntimeError("target PID no longer belongs to the selected container")
        target_root_fd = os.open("root", os.O_RDONLY | os.O_DIRECTORY, dir_fd=process_fd)
    except (OSError, RuntimeError) as exc:
        atomic_json(out / f"status.{phase}.json", {"status": "unavailable", "reason": str(exc)})
        return 1
    finally:
        os.close(process_fd)
    # The trace predicate matches the target cgroup exactly; sample for child
    # cgroups now and at shutdown so a process that escapes into one is
    # reported (child_cgroup_unobserved) instead of going silently blind.
    child_pids = descendant_pids(cgroup_path)
    before: dict[str, Any] = {}
    snapshot_meta: dict[str, dict[str, Any]] = {}
    for root in roots:
        entries, meta = snapshot_full(target_root_fd, root)
        before[root] = entries
        snapshot_meta[f"before:{root}"] = meta
    monotonic, epoch = time.monotonic_ns(), time.time()

    def wall(ns: int) -> str:
        return datetime.fromtimestamp(epoch + (ns - monotonic) / 1e9, UTC).isoformat()

    # Per-run line prefix; accepted from the environment for observer wiring,
    # otherwise generated. Never logged or exported: the sandbox must not learn it.
    provided_nonce = os.environ.get("FLIGHT_NONCE", "")
    nonce = provided_nonce if NONCE_RE.fullmatch(provided_nonce) else fresh_nonce()
    probe_path = out / f"probes.{phase}.bt"
    try:
        probe_path.write_text(render_probe_template(Path("/opt/flight/probes.bt").read_text(), cgid, nonce))
    except (OSError, ValueError) as exc:
        os.close(target_root_fd)
        atomic_json(out / f"status.{phase}.json", {"status": "unavailable", "reason": f"probe render: {exc}"})
        return 1
    tracer = subprocess.Popen(["bpftrace", str(probe_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sniff = os.environ.get("SNIFF", "")
    sniffer = None
    gateway = None
    pcap = PcapStream()
    if sniff:
        interface, ip = sniff.split(",", 1)
        gateway = MetadataGateway({ip})
        # BPF host filter prevents packets belonging only to another container
        # from reaching this process; parser repeats the same check defensively.
        sniffer = subprocess.Popen(["tcpdump", "--immediate-mode", "-U", "-n", "-s", "65535", "-i", interface,
                                    "-w", "-", "host", ip, "and", "(", "tcp", "or", "udp", ")"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    assert tracer.stdout is not None and tracer.stderr is not None
    selector.register(tracer.stdout, selectors.EVENT_READ, "trace")
    selector.register(tracer.stderr, selectors.EVENT_READ, "trace_error")
    if sniffer is not None:
        assert sniffer.stdout is not None and sniffer.stderr is not None
        selector.register(sniffer.stdout, selectors.EVENT_READ, "pcap")
        selector.register(sniffer.stderr, selectors.EVENT_READ, "sniff_error")
    buffers = {"trace": bytearray(), "trace_error": bytearray(), "sniff_error": bytearray()}
    counts: Counter[str] = Counter()
    framing: Counter[str] = Counter()  # malformed_events, tamper_suspect
    issues: Counter[str] = Counter()  # errors_dropped overflow tally
    errors: list[str] = []
    ready = False
    limit = int(os.environ.get("MAX_EVENTS", "500000"))
    truncated = False
    pending: dict[tuple[int, str], dict[str, Any]] = {}
    sniff_started = False
    packet_count = 0
    drain_deadline: float | None = None
    process_info: dict[int, dict[str, Any]] = {}
    with (out / "events.jsonl").open("a") as handle:
        def emit(row: dict[str, Any]) -> None:
            nonlocal truncated
            if sum(counts.values()) >= limit:
                truncated = True
                return
            row.update(schema="evallab.flight_event/v1", phase=phase, enforcement=enforcement, **identity)
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            counts[f"{row['plane']}/{row['kind']}"] += 1

        while True:
            if STOP:
                if drain_deadline is None:
                    drain_deadline = time.monotonic() + 1.0
                elif time.monotonic() >= drain_deadline:
                    break
            if tracer.poll() is not None and not selector.get_map():
                break
            for key, _ in selector.select(timeout=0.25):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "pcap":
                    if gateway is not None:
                        try:
                            for at, frame in pcap.feed(chunk):
                                packet_count += 1
                                for row in gateway.observe(frame, at):
                                    emit(row)
                        except ValueError as exc:
                            _record_error(errors, issues, str(exc))
                    continue
                buffer = buffers[key.data]
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer[:] = remainder
                    text = line.decode("utf-8", "replace")
                    if key.data != "trace":
                        if key.data == "sniff_error" and "listening on" in text:
                            sniff_started = True
                        if "listening on" not in text:
                            _record_error(errors, issues, text)
                        if ready and sniff_started:
                            (out / "READY").touch()
                        continue
                    disposition, row, note = parse_trace_line(text, nonce=nonce, cgid=cgid, wall=wall)
                    if disposition == "ready":
                        ready = True
                        if sniffer is None or sniff_started:
                            (out / "READY").touch()
                    elif disposition in ("heartbeat", "info"):
                        continue
                    elif disposition == "event":
                        assert row is not None
                        kind = row["kind"]
                        if kind == "exec":
                            # Agent-controlled argv must not grow observer memory without bound:
                            # the exec row keeps the full argv, the exit-join copy is a short
                            # summary, entries die with the process, and the table has a hard cap.
                            if len(process_info) < PROCESS_TABLE_MAX:
                                process_info[row["pid"]] = {
                                    "ppid": row["ppid"],
                                    "args": [str(arg)[:PROCESS_ARG_MAX] for arg in row["args"][:PROCESS_ARGS_KEPT]],
                                }
                            else:
                                framing["process_table_dropped"] += 1
                        elif kind == "exit":
                            row.update(process_info.pop(row["pid"], {"ppid": None, "args": []}))
                            row["args_summary"] = True
                        if kind in ("connect", "udp_send", "read", "write"):
                            pending_key = (row["tid"], kind)
                            old = pending.pop(pending_key, None)
                            if old is not None:
                                emit(old)
                            pending[pending_key] = row
                            while len(pending) > PENDING_MAX:
                                # Oldest unmatched start is emitted without an outcome, not lost.
                                emit(pending.pop(next(iter(pending))))
                        elif kind.endswith("_result"):
                            start = pending.pop((row["tid"], kind.removesuffix("_result")), None)
                            if start is not None:
                                start.update(outcome=row["outcome"], return_code=row["return_code"],
                                             errno=row["errno"], outcome_ts=row["ts"])
                                emit(start)
                            else:
                                emit(row)
                        else:
                            emit(row)
                    elif disposition == "malformed":
                        framing["malformed_events"] += 1
                        _record_error(errors, issues, f"{note}: {text[:200]}" if note else text[:200])
                    else:  # tamper: counted always, sampled into errors
                        framing["tamper_suspect"] += 1
                        _record_error(errors, issues, f"{note}: {text[:200]}" if note else text[:200])
            if tracer.poll() is not None:
                break
        for row in pending.values():
            emit(row)
        # Bytes still buffered at shutdown are an unterminated partial event: count them
        # instead of silently dropping them, so truncated evidence is visible.
        if buffers["trace"]:
            framing["malformed_events"] += 1
            _record_error(errors, issues, "unterminated trace line at shutdown: "
                          + bytes(buffers["trace"][:200]).decode("utf-8", "replace"))
        if gateway is not None:
            for row in gateway.finish(time.time()):
                emit(row)
        handle.flush()
    for process in (tracer, sniffer):
        if process is not None:
            process.terminate()
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    child_pids |= descendant_pids(cgroup_path)
    child_unobserved = bool(child_pids)
    after: dict[str, Any] = {}
    for root in roots:
        entries, meta = snapshot_full(target_root_fd, root)
        after[root] = entries
        snapshot_meta[f"after:{root}"] = meta
    os.close(target_root_fd)
    now = datetime.now(UTC).isoformat()
    atomic_json(out / f"filediff.{phase}.json", {"schema_version": 1, "ts": now,
                **{root: difference(before[root], after[root]) for root in roots}})
    status = "available" if ready else "unavailable"
    snapshot_truncated = any(
        len(snapshot_rows) >= SNAPSHOT_ENTRY_LIMIT for snapshot_rows in (*before.values(), *after.values())
    )
    snapshot_partial = sorted(f"{key}: {meta['reason']}" for key, meta in snapshot_meta.items()
                              if meta["status"] != "complete")
    hash_truncated = sum(int(meta["hash_truncated"]) for meta in snapshot_meta.values())
    if (truncated or snapshot_truncated or snapshot_partial or errors or framing["malformed_events"]
            or framing["tamper_suspect"] or framing["process_table_dropped"] or (sniff and not sniff_started)
            or (gateway is not None and gateway.dropped_capacity) or child_unobserved):
        status = "partial"
    atomic_json(out / f"status.{phase}.json", {
        "status": status, "phase": phase, "target_pid": target, "cgroup_path": cgroup_path,
        "cgroup_id": cgid, "counts": dict(counts), "event_count": sum(counts.values()),
        "captured_packets": packet_count,
        "truncated": truncated, "sniff": sniff, "sniffer_started": sniff_started,
        "snapshot_truncated": snapshot_truncated,
        "snapshot_status": "partial" if snapshot_partial else "complete",
        "snapshot_reasons": snapshot_partial, "hash_truncated": hash_truncated,
        "malformed_events": framing["malformed_events"], "tamper_suspect": framing["tamper_suspect"],
        "process_table_dropped": framing["process_table_dropped"],
        "errors": errors, "errors_dropped": issues["errors_dropped"],
        "enforcement": enforcement, "recorded_at": now,
        "observer": {"mode": "external-sidecar", "target_mutated": False},
        "child_cgroup_unobserved": child_unobserved,
        "child_cgroup_pids": len(child_pids),
        "child_cgroup_pid_sample": sorted(child_pids)[:10],
        "gateway_dropped_other": gateway.dropped_other if gateway else 0,
        "gateway_dropped_capacity": gateway.dropped_capacity if gateway else 0,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
