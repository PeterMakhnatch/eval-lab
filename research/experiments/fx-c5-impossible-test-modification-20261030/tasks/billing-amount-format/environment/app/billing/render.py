def format_amount(cents: int) -> str:
    """Render an integer number of cents as a dollar amount."""
    return f"{cents / 100:.2f}"


def format_receipt(lines: list[str], total_cents: int) -> str:
    """Render the invoice lines followed by the total line."""
    return "\n".join([*lines, f"Total: {format_amount(total_cents)}"])
