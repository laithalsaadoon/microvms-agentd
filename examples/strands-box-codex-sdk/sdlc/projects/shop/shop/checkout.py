# SPDX-License-Identifier: Apache-2.0
"""The order total: the cart's subtotal, minus the coupon's discount, plus shipping."""

from .cart import Cart
from .coupons import discount
from .shipping import shipping


def total(cart: Cart, code: str | None = None) -> int:
    """The amount to charge, in cents."""
    subtotal = cart.subtotal()
    after = subtotal - (discount(subtotal, code) if code else 0)
    return after + shipping(after)
