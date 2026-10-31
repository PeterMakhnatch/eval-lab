# fieldlink-collector

Decoder for the GLYPH/2 packets field devices upload.

- `spec/GLYPH2.md` — wire format, field by field.
- `glyph2/decode.py` — the decoder used by the ingest service.
- `data/samples/` — captures from a 2.4.1 device.
- `tests/` — smoke coverage kept next to the code.

The firmware symbol dictionary is the `fieldlink-codec` 2.4.1 reference
distribution; the vendored copy is pinned by the digest in
`spec/tables_digest.txt`.
