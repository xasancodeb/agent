"""Promo and tag tests. Two of these carry their own nondeterminism, and one
is simply wrong."""

import cart

CODES = ["WELCOME10", "SPRING20", "FREESHIP"]


def test_promo_is_one_of_the_codes():
    assert cart.pick_promo(CODES) in CODES


def test_featured_promo_is_welcome():
    # The global random state is never seeded, so this picks a different code
    # on most runs.
    assert cart.pick_promo(CODES) == "WELCOME10"


def test_first_tag_is_clearance():
    # Iteration order of a set of strings depends on the process hash seed.
    tags = cart.applicable_tags(["sale", "new", "clearance"])
    assert list(tags)[0] == "clearance"


def test_tags_are_deduplicated():
    assert cart.applicable_tags(["sale", "SALE", "new"]) == {"sale", "new"}


def test_discount_rounds_to_nearest_cent():
    # Genuinely broken: 10% off 1999 is 1799.1, which rounds to 1799, not 1800.
    # This fails every time, alone or in the suite.
    assert round(1999 * 0.9) == 1800
