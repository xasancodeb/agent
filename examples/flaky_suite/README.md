# The demo suite

A deliberately unreliable pytest suite. Five of these tests misbehave, in five
different ways, and telling them apart is the whole exercise.

| Test | Kind | Mechanism |
|---|---|---|
| `test_b_receipt.py::test_receipt_total_is_usd` | order-dependent | `test_a_checkout.py::test_eu_checkout_uses_euro_symbol` sets `cart.SETTINGS["currency"]` and never restores it |
| `test_b_receipt.py::test_receipt_timestamp_matches_wall_clock` | clock | reads the clock twice with work in between, fails when the second ticks |
| `test_c_promos.py::test_featured_promo_is_welcome` | unseeded randomness | `random.choice` on an unseeded global |
| `test_c_promos.py::test_first_tag_is_clearance` | hash ordering | iteration order of a `set` of strings, varies with `PYTHONHASHSEED` |
| `test_c_promos.py::test_discount_rounds_to_nearest_cent` | genuinely broken | the assertion is just wrong; fails alone, every time |

The first and last are the interesting pair. A scan sees both failing in every
full-suite run and cannot tell them apart — they look identical from the
outside. Only running each one *alone* separates them: one passes immediately,
the other still fails. One needs a fixture; the other needs a bug fixed.

This directory has its own `pytest.ini` so it is never collected by the
project's own test run.
