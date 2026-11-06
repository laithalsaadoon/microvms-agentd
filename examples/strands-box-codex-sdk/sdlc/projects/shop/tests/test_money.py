# SPDX-License-Identifier: Apache-2.0
"""Parsing and formatting prices. The policy freezes every file in tests/."""

import unittest

from shop.money import format_cents, parse_price


class MoneyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_price("12.50"), 1250)
        self.assertEqual(parse_price("$3"), 300)
        self.assertEqual(parse_price("0.5"), 50)
        self.assertEqual(parse_price("19.99"), 1999)

    def test_format(self):
        self.assertEqual(format_cents(1250), "$12.50")
        self.assertEqual(format_cents(5), "$0.05")
        self.assertEqual(format_cents(-799), "-$7.99")
