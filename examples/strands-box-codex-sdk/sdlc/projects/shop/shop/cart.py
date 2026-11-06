# SPDX-License-Identifier: Apache-2.0
"""A cart: lines of a SKU, a unit price in cents, and a quantity."""

from dataclasses import dataclass, field

from .money import parse_price


@dataclass
class Line:
    sku: str
    unit_cents: int
    qty: int


@dataclass
class Cart:
    lines: list[Line] = field(default_factory=list)

    def add(self, sku: str, price: str, qty: int = 1) -> None:
        """Add qty of sku at price, a price string such as "12.50"."""
        self.lines.append(Line(sku, parse_price(price), qty))

    def subtotal(self) -> int:
        """The cart's subtotal in cents."""
        return sum(line.unit_cents * line.qty for line in self.lines)
