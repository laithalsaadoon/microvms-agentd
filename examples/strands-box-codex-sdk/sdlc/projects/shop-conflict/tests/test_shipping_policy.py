# SPDX-License-Identifier: Apache-2.0
"""Shipping policy from the 2026 carrier contract: shipping is free only for orders over
$50.00 after discounts, so an order of exactly $50.00 pays the $7.99 rate."""

import unittest

from shop.cart import Cart
from shop.checkout import total


class ShippingPolicyTest(unittest.TestCase):
    def test_exactly_fifty_pays_shipping(self):
        cart = Cart()
        cart.add("item", "50.00")
        self.assertEqual(total(cart), 5799)

    def test_over_fifty_ships_free(self):
        cart = Cart()
        cart.add("item", "50.01")
        self.assertEqual(total(cart), 5001)
