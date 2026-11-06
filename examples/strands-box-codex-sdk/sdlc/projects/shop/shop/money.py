# SPDX-License-Identifier: Apache-2.0
"""Prices as integer cents: parse what people type, and format cents for a receipt."""


def parse_price(text: str) -> int:
    """Return the cents in a price such as "12.50", "$3", or "0.5" (1250, 300, 50)."""
    whole, _, frac = text.strip().lstrip("$").partition(".")
    frac = (frac + "00")[:2]
    return int(whole or "0") * 100 + int(frac)


def format_cents(cents: int) -> str:
    """Return cents as a receipt amount: 1250 becomes "$12.50"."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"
