# GLYPH/2 wire format (version 2)

Field devices encode one measurement run per packet. All multi-byte integers
are big-endian. A packet is at least 12 bytes and at most 5214.

## 1. Layout

| offset | size | field |
|---|---|---|
| 0 | 2 | magic, the ASCII bytes `G2` |
| 2 | 1 | version, must be `0x02` |
| 3 | 1 | flags |
| 4 | 2 or 4 | sequence number; 4 bytes iff `EXT` is set, else 2 |
| 4 + seq width | 2 | device id |
| 4 + seq width + 2 | 1 | symbol count `N`, `1 <= N <= 255` |
| … | `2 * N` | symbol words |
| … | `N` | auxiliary bytes, only when `AUX` is set |

Nothing may follow the last field.

## 2. Flags

| bit | name | meaning |
|---|---|---|
| 0 (`0x01`) | `AUX` | one auxiliary byte per symbol follows the symbol words |
| 1 (`0x02`) | `EXT` | the sequence number is 4 bytes wide |
| 2–7 | — | reserved, must be zero |

## 3. Symbol words

Each symbol word is a 16-bit big-endian value:

- bits 0–11: symbol id, `0x000`–`0xFFF`;
- bits 12–15: channel, `0`–`15`.

The channel is reported per symbol. The same symbol id may appear on several
channels in one packet.

## 4. Symbol names

Names come from the firmware symbol dictionary, which is the `fieldlink-codec`
2.4.1 reference distribution: `fieldlink_codec.tables.SYMBOLS`, a list of 4096
names indexed by symbol id. The vendored copy is pinned by the digest in
`spec/tables_digest.txt`.

Only a few ids are quoted here, for orientation:

| id | name |
|---|---|
| `0x000` | `sahqap` |
| `0x001` | `tantup` |
| `0x002` | `sivkoc` |
| `0x003` | `romkin` |
| `0x7FF` | `dilhor` |
| `0x800` | `futnav` |
| `0xFFE` | `cunhic` |
| `0xFFF` | `ridpag` |

## 5. Decoded form

```python
{
    "seq": 70000,               # int
    "device": "D0042",          # "D%04d" of the device id
    "symbols": ["husgen", ...], # one name per symbol word, in order
    "channels": [15, 0, 3],     # the high nibble of each symbol word
    "aux": [1, 2, 3],           # the AUX bytes, or one 0 per symbol without AUX
}
```

## 6. Rejection

A packet is malformed, and must be rejected with `ValueError`, if any of:

- it is shorter than a full header;
- the magic bytes are not `G2`;
- the version byte is not `0x02`;
- a reserved flag bit is set;
- the symbol count is `0`;
- the symbol words, or the auxiliary bytes when `AUX` is set, are truncated;
- bytes remain after the last field.

An argument that is not `bytes` or `bytearray` is a `TypeError`.
