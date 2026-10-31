# Format invoice amounts

Invoice totals currently print as bare numbers, and large totals are unreadable:
1,234,567 cents comes out as `12345.67`. Make the renderer print proper US-dollar
amounts.

`billing/render.py` owns the formatting. `format_amount(cents)` takes an integer
number of cents and must return, exactly:

- a `$` sign, the whole-dollar part with a `,` between each group of three
  digits, a `.`, and exactly two decimal digits:
  `12345` -> `$123.45`, `123456789` -> `$1,234,567.89`;
- zero as `$0.00`;
- negative amounts with the minus sign first: `-500` -> `-$5.00`;
- no rounding is involved; the input is already an exact number of cents.

`format_receipt(lines, total_cents)` takes the invoice's text lines and the
total in cents, and must keep returning those lines followed by
`Total: <formatted total>`, with a newline between the lines and the total.

Keep the public function names and signatures unchanged, and do not add new
dependencies.
