"""Issue-bank source builders, one module per source, discovered in name order.

Each module defines ``SOURCE`` (slug) and ``build(ctx: BankContext)`` yielding
:class:`~evallab.failure_library.bank.IssueItem` or, for sources whose rows may
not be stored, :class:`~evallab.failure_library.bank.SourceSummary`.
"""
