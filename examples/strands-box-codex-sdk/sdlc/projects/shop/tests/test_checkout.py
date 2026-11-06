# SPDX-License-Identifier: Apache-2.0
"""Order totals, in cents. Shipping is $7.99, and free for orders of $50.00 and over after
discounts. SAVE5 takes $5.00 off a subtotal of $25.00 or more; TENOFF takes 10% off."""

import unittest

from shop.cart import Cart
from shop.checkout import total


def cart_of(price: str) -> Cart:
    cart = Cart()
    cart.add("item", price)
    return cart


class CheckoutTest(unittest.TestCase):
    def test_small_order_pays_shipping(self):
        self.assertEqual(total(cart_of("20.00")), 2799)

    def test_large_order_ships_free(self):
        self.assertEqual(total(cart_of("60.00")), 6000)

    def test_free_shipping_starts_at_fifty(self):
        self.assertEqual(total(cart_of("50.00")), 5000)

    def test_save5(self):
        self.assertEqual(total(cart_of("30.00"), "SAVE5"), 3299)

    def test_save5_needs_the_minimum_spend(self):
        self.assertEqual(total(cart_of("20.00"), "SAVE5"), 2799)

    def test_save5_can_drop_an_order_below_free_shipping(self):
        self.assertEqual(total(cart_of("52.00"), "SAVE5"), 5499)

    def test_tenoff(self):
        self.assertEqual(total(cart_of("40.00"), "TENOFF"), 4399)

    def test_unknown_code(self):
        self.assertEqual(total(cart_of("20.00"), "NOPE"), 2799)
