"""Checkout tests. One of these leaves state behind; the rest are filler that
a bisect has to search through."""

import cart


def test_line_total_single_item():
    assert cart.line_total(500, 1) == 500


def test_line_total_multiple_items():
    assert cart.line_total(500, 3) == 1500


def test_line_total_zero_quantity():
    assert cart.line_total(500, 0) == 0


def test_tax_free_by_default():
    assert cart.with_tax(1000) == 1000


def test_empty_cart_formats_as_zero():
    assert cart.format_total(0) == "$0.00"


def test_symbols_cover_supported_currencies():
    assert set(cart.SYMBOLS) >= {"USD", "EUR"}


def test_large_total_formats_with_cents():
    assert cart.format_total(1234567) == "$12345.67"


def test_line_total_rejects_nothing():
    assert cart.line_total(0, 10) == 0


def test_tags_are_lowercased():
    assert cart.applicable_tags(["SALE"]) == {"sale"}


def test_tags_drop_empty_strings():
    assert cart.applicable_tags(["sale", ""]) == {"sale"}


def test_receipt_lists_every_item():
    receipt = cart.make_receipt([("A", 100), ("B", 200)])
    assert len(receipt["items"]) == 2


def test_eu_checkout_uses_euro_symbol():
    # Switches the module-level currency and never puts it back. Every test
    # that runs after this one in the same process sees euros.
    cart.SETTINGS["currency"] = "EUR"
    assert cart.format_total(2500) == "€25.00"
