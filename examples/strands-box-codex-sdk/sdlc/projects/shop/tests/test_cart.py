# SPDX-License-Identifier: Apache-2.0
"""A cart's subtotal."""

import unittest

from shop.cart import Cart


class CartTest(unittest.TestCase):
    def test_subtotal(self):
        cart = Cart()
        cart.add("mug", "12.50", qty=2)
        cart.add("tea", "4.99")
        self.assertEqual(cart.subtotal(), 2999)

    def test_empty(self):
        self.assertEqual(Cart().subtotal(), 0)
