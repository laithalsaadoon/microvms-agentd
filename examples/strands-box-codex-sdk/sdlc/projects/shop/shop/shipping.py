# SPDX-License-Identifier: Apache-2.0
"""The shipping charge: a flat rate, and free shipping from a threshold up."""

FREE_FROM = 50
FLAT_RATE = 799


def shipping(amount: int) -> int:
    """The shipping charge in cents for an order of amount cents after discounts."""
    return 0 if amount >= FREE_FROM else FLAT_RATE
