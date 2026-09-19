"""Receipt tests. These pass on their own."""

import time
from datetime import datetime

import cart


def test_receipt_total_is_usd():
    # Passes alone. Fails in the full suite, because an earlier test switched
    # the module-level currency to EUR and never restored it.
    receipt = cart.make_receipt([("SKU-1", 1500), ("SKU-2", 1000)])
    assert receipt["total"] == "$25.00"


def test_receipt_timestamp_matches_wall_clock():
    # Two calls to the clock with work in between: whenever the second ticks
    # between them, the assertion fails.
    expected = datetime.now().strftime("%H:%M:%S")
    time.sleep(0.5)
    receipt = cart.make_receipt([("SKU-1", 100)])
    assert receipt["at"] == expected


def test_receipt_has_a_timestamp():
    receipt = cart.make_receipt([("SKU-1", 100)])
    assert len(receipt["at"]) == 8


def test_receipt_of_empty_cart():
    receipt = cart.make_receipt([])
    assert receipt["items"] == []
