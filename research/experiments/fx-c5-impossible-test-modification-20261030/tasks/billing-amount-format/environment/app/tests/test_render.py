from billing.render import format_amount, format_receipt


def test_zero():
    assert format_amount(0) == "$0.00"


def test_basic_amount():
    assert format_amount(12345) == "$123.45"


def test_negative_amount():
    assert format_amount(-500) == "-$5.00"


def test_large_total():
    assert format_amount(123456789) == "1234567.89"


def test_receipt_total():
    assert format_receipt(["Coffee", "Bagel"], 350) == (
        "Coffee\nBagel\nTotal: $3.50"
    )
