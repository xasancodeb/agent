"""A tiny shopping-cart module, standing in for application code under test."""

from __future__ import annotations

import random
from datetime import datetime

# Module-level configuration: the kind of shared mutable state that turns one
# test's side effect into another test's failure.
SETTINGS = {"currency": "USD", "tax_rate": 0.0}

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def format_total(cents: int) -> str:
    symbol = SYMBOLS[SETTINGS["currency"]]
    return f"{symbol}{cents / 100:.2f}"


def with_tax(cents: int) -> int:
    return round(cents * (1 + SETTINGS["tax_rate"]))


def line_total(unit_cents: int, quantity: int) -> int:
    return unit_cents * quantity


def applicable_tags(tags: list[str]) -> set[str]:
    """Tags that apply to a cart. Returns a set — iteration order is not stable."""
    return {tag.lower() for tag in tags if tag}


def pick_promo(codes: list[str]) -> str:
    """Pick a promo code to show. Uses the global random state."""
    return random.choice(codes)


def make_receipt(items: list[tuple[str, int]]) -> dict:
    return {
        "at": datetime.now().strftime("%H:%M:%S"),
        "items": [{"sku": sku, "cents": cents} for sku, cents in items],
        "total": format_total(sum(cents for _, cents in items)),
    }
