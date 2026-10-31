# Ticket FLD-214: GLYPH/2 packets decode to garbage

`/app/glyph2` is the Python decoder for the GLYPH/2 telemetry packets our field
devices upload. Since the 2.4 firmware rollout the decoded output has been
wrong: symbol names come out as raw ids, the channel nibble is always 0,
wrap-around sequence numbers arrive as their low 16 bits, the device field is
formatted differently from what the down-stream ingest expects, and packets
that carry auxiliary readings lose them. Three captures from a 2.4.1 device are
in `data/samples/`.

Make `glyph2.decode.decode_packet(packet)` follow `spec/GLYPH2.md`:

- It accepts `bytes` (or `bytearray`) and returns

  ```python
  {
      "seq": int,             # sequence number, 2 or 4 bytes per the flags
      "device": str,          # "D%04d" of the 16-bit device id
      "symbols": [str, ...],  # names from the 2.4.1 firmware dictionary
      "channels": [int, ...], # high nibble of each symbol word, 0..15
      "aux": [int, ...],      # one byte per symbol, zeros when the AUX flag is clear
  }
  ```

- Symbol names are `SYMBOLS[symbol_id]` from the `fieldlink-codec` 2.4.1
  reference distribution (`fieldlink_codec.tables`), which is the dictionary the
  format is defined against; see `spec/GLYPH2.md` §4. Only a handful of ids are
  quoted in the spec text.
- A packet that violates the format raises `ValueError`. A non-`bytes` argument
  raises `TypeError`. Nothing else may raise.
- Pinning: the vendored dictionary is 2.4.1; its digest is recorded in
  `spec/tables_digest.txt`. Do not change `glyph2/__init__.py` or `spec/`.
