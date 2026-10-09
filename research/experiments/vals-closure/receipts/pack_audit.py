#!/usr/bin/env python3
"""Hand-rolled git object-store reader (no git binary).

Enumerates every object in a .git directory (loose + packs, resolving
OFS/REF deltas itself), decodes commits, and checks every object is
reachable from BASE. A hand-rolled pack parser is exactly Vals' rung-4
escalation; if it finds nothing past BASE, the strip defeats even that.

Usage: pack_audit.py <git_dir> <base_sha>
Stdout: one-line summary + details. Exit 0 iff zero post-base objects.
"""

from __future__ import annotations

import contextlib
import hashlib
import struct
import sys
import zlib
from pathlib import Path

TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs_delta", 7: "ref_delta"}


def read_varint(data: bytes, pos: int) -> tuple[int, int, int]:
    byte = data[pos]
    pos += 1
    otype = (byte >> 4) & 7
    size = byte & 15
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return otype, size, pos


def read_ofs_distance(data: bytes, pos: int) -> tuple[int, int]:
    byte = data[pos]
    pos += 1
    off = byte & 0x7F
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        off = ((off + 1) << 7) | (byte & 0x7F)
    return off, pos


def read_delta_size(data: bytes, pos: int) -> tuple[int, int]:
    size = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return size, pos
        shift += 7


def apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0
    _src, pos = read_delta_size(delta, pos)
    _tgt, pos = read_delta_size(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            off = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (1 << (4 + i)):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[off : off + size]
        elif op:
            out += delta[pos : pos + op]
            pos += op
        else:
            raise ValueError("zero delta opcode")
    return bytes(out)


class Store:
    def __init__(self, git_dir: Path) -> None:
        self.git_dir = git_dir
        self.objs: dict[str, tuple[str, bytes]] = {}  # sha -> (type, raw)
        self._load_loose()
        self._load_packs()

    def _store(self, raw: bytes) -> None:
        head, _, body = raw.partition(b"\x00")
        otype, _, _size = head.decode().split(" ", 2)
        sha = hashlib.sha1(raw).hexdigest()
        self.objs[sha] = (otype, body)

    def _load_loose(self) -> None:
        od = self.git_dir / "objects"
        for sub in od.iterdir():
            if len(sub.name) != 2 or not sub.is_dir():
                continue
            try:
                int(sub.name, 16)
            except ValueError:
                continue
            for f in sub.iterdir():
                if f.name == "pack-temp":
                    continue
                with contextlib.suppress(Exception):
                    self._store(zlib.decompress(f.read_bytes()))

    def _load_packs(self) -> None:
        packdir = self.git_dir / "objects" / "pack"
        if not packdir.is_dir():
            return
        for pack in sorted(packdir.glob("*.pack")):
            idx = pack.with_suffix(".idx")
            if idx.is_file():
                self._load_pack_indexed(pack, idx)

    def _idx_offsets(self, idx: Path) -> dict[str, int]:
        data = idx.read_bytes()
        assert data[:4] == b"\xfftOc", "not a v2 idx"
        n = struct.unpack(">256I", data[8 : 8 + 1024])[-1]
        pos = 8 + 1024
        shas = [data[pos + 20 * i : pos + 20 * (i + 1)].hex() for i in range(n)]
        pos += 20 * n
        pos += 4 * n  # crc
        offs = list(struct.unpack(f">{n}I", data[pos : pos + 4 * n]))
        pos += 4 * n
        big = (
            struct.unpack(f">{len(data[pos:-40]) // 8}Q", data[pos:-40])
            if len(data[pos:-40])
            else ()
        )
        return {
            s: (big[o & 0x7FFFFFFF] if o & 0x80000000 else o)
            for s, o in zip(shas, offs, strict=True)
        }

    def _load_pack_indexed(self, pack: Path, idx: Path) -> None:
        data = pack.read_bytes()
        assert data[:4] == b"PACK", "not a pack"
        count = struct.unpack(">I", data[8:12])[0]
        offsets = self._idx_offsets(idx)
        by_off = {o: s for s, o in offsets.items()}
        pending: dict[int, tuple[str, bytes, bytes | None]] = {}
        # pass 1: inflate all entries
        for off in sorted(by_off):
            otype, _size, pos = read_varint(data, off)
            name = TYPES[otype]
            if name == "ofs_delta":
                dist, pos = read_ofs_distance(data, pos)
                base_off = off - dist
                extra = base_off.to_bytes(8, "big")
            elif name == "ref_delta":
                extra = data[pos : pos + 20]
                pos += 20
            else:
                extra = None
            dec = zlib.decompressobj()
            body = dec.decompress(data[pos:])
            tail = dec.unused_data
            end = len(data) - len(tail)
            assert end > pos, "empty zlib stream"
            pending[off] = (name, body, extra)
        # pass 2: resolve in offset order (ofs bases always precede deltas).
        # Pack deltas operate on headerless content; the result inherits the
        # base object's type (a delta never changes type).
        resolved: dict[int, tuple[str, bytes]] = {}
        sha_to_off = {s: o for o, s in by_off.items()}

        def base_of_sha(base_sha: str) -> tuple[str, bytes]:
            if base_sha in sha_to_off:
                return get(sha_to_off[base_sha])
            return self.objs[base_sha]

        def get(off: int) -> tuple[str, bytes]:
            if off in resolved:
                return resolved[off]
            name, body, extra = pending[off]
            if name in ("ofs_delta", "ref_delta"):
                if name == "ofs_delta":
                    base_off = off - int.from_bytes(extra, "big")
                    otype, base = get(base_off)
                else:
                    otype, base = base_of_sha(extra.hex())
                resolved[off] = (otype, apply_delta(base, body))
            else:
                resolved[off] = (name, body)
            return resolved[off]

        for off in sorted(pending):
            otype, content = get(off)
            raw = f"{otype} {len(content)}\0".encode() + content
            sha = hashlib.sha1(raw).hexdigest()
            if sha != by_off[off]:
                raise ValueError(f"hash mismatch at offset {off}")
            self.objs[sha] = (otype, content)
        assert len(by_off) == count, f"idx/pack count {len(by_off)} != {count}"


def commit_parents(raw: bytes) -> list[str]:
    return [
        line[7:] for line in raw.decode(errors="replace").splitlines() if line.startswith("parent ")
    ]


def commit_tree(raw: bytes) -> str:
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("tree "):
            return line[5:]
    raise ValueError("commit without tree")


def tree_entries(raw: bytes) -> list[tuple[str, str]]:
    out = []
    pos = 0
    while pos < len(raw):
        sp = raw.index(b" ", pos)
        nul = raw.index(b"\x00", sp)
        sha = raw[nul + 1 : nul + 21].hex()
        out.append((raw[pos:sp].decode(), sha))
        pos = nul + 21
    return out


def main() -> int:
    git_dir = Path(sys.argv[1])
    base = sys.argv[2].strip().lower()
    store = Store(git_dir)
    commits = {s for s, (t, _) in store.objs.items() if t == "commit"}
    # BFS from BASE over the reader's own commit graph
    seen = {base}
    stack = [base]
    missing_base = base not in store.objs
    while stack:
        cur = stack.pop()
        _t, raw = store.objs.get(cur, (None, b""))
        if _t != "commit":
            continue
        for p in commit_parents(raw):
            if p not in seen:
                seen.add(p)
                stack.append(p)
    post_commits = sorted(commits - seen)
    # full reachability: trees + blobs under closure commits
    reachable = set(seen)
    stack = [
        commit_tree(store.objs[c][1]) for c in seen if store.objs.get(c, (None, b""))[0] == "commit"
    ]
    bad_trees = 0
    while stack:
        t = stack.pop()
        if t in reachable:
            continue
        ent = store.objs.get(t)
        if ent is None or ent[0] != "tree":
            bad_trees += 1
            continue
        reachable.add(t)
        for _mode, sha in tree_entries(ent[1]):
            if sha not in reachable:
                e = store.objs.get(sha)
                if e is not None and e[0] == "tree":
                    stack.append(sha)
                else:
                    reachable.add(sha)
    post_all = sorted(set(store.objs) - reachable)
    from collections import Counter

    print(f"store_objects={len(store.objs)} commits={len(commits)} base_closure={len(seen)}")
    print(f"post_base_commits={len(post_commits)} post_base_objects={len(post_all)}")
    print(f"base_present={not missing_base} dangling_tree_refs={bad_trees}")
    for s in post_commits[:10]:
        print(f"post-base commit {s}")
    for s in post_all[:10]:
        print(f"post-base object {store.objs[s][0]} {s}")
    type_counts = Counter(t for t, _ in store.objs.values())
    print("types=" + " ".join(f"{k}:{v}" for k, v in sorted(type_counts.items())))
    return 0 if (not post_all and not missing_base) else 1


if __name__ == "__main__":
    raise SystemExit(main())
