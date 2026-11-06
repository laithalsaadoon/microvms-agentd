# SPDX-License-Identifier: Apache-2.0
"""Coupon codes and the discount each gives on a subtotal.

A fixed coupon takes its amount off, once the subtotal reaches the coupon's minimum spend. A
percent coupon takes that percent of the subtotal off, rounded down to the cent.
"""

COUPONS = {
    "SAVE5": {"kind": "fixed", "amount": 5, "min_spend": 25},
    "TENOFF": {"kind": "percent", "amount": 10, "min_spend": 0},
}


def discount(subtotal: int, code: str) -> int:
    """The discount in cents that code gives on subtotal, or 0 when it doesn't apply."""
    rule = COUPONS.get(code)
    if rule is None or subtotal < rule["min_spend"]:
        return 0
    if rule["kind"] == "fixed":
        return min(rule["amount"], subtotal)
    return subtotal * rule["amount"] // 100
