# cobol-modernization: rejected transactions are never tested

Task: terminal-bench-2-1 `tasks/cobol-modernization` (package digest
`sha256:914b37494851452a3fdc5554264601eb854c602fb33f3b3c0c22d25adcf0b84e`).
Patch: `../patches/cobol-modernization.patch` (append-only).

## Gap

The instruction requires `/app/program.py` to leave the `.DAT` files identical to
what `program.cbl` produces for the same `INPUT.DAT`. In `program.cbl`,
`VALIDATE-USERS-AND-BOOK` sets `WS-VALID-OWNER` only when `BOOK-OWNER` equals the
seller, and `PROCESS-TRANSACTION` runs only if the buyer, seller and book exist
and the seller owns the book. Otherwise no file changes. `test_program_output`
replays three transactions, and all of them are valid. Mutant `6d38c09872`
(`book.id == book_id and book.owner == seller_id` becomes `... or ...`) passed: it
lets a non-owner sell a book. With `INPUT.DAT = U001U002B0030000000020` the
reference leaves every file unchanged. The mutant moves B003 to U001 and 20
units of balance, and appends a transaction record.

## Repair

`test_rejected_transactions_leave_files_unchanged` was added. It resets the three
`.DAT` files and runs `program.py` from `/app` with four rejected inputs: the
seller is not the owner, and an unknown buyer, seller or book. After each run all
three files must be byte-identical to their starting state.

## Qualification (native Harbor)

- solve.sh oracle: reward 1. Full reconstruction: reward 1. nop: reward 0.
- Recorded survivor `6d38c09872`: now reward 0.
- In the oracle trial's verifier output, all original tests and the new test passed.
- Round 2: the same seeded 8 mutants left 1 survivor, `6326967e40`. It changes
  only a debug `print` to stdout. The instruction constrains the `.DAT` files, not
  stdout, so it is not a weak-test case and needs no repair.
