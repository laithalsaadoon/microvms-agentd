# The shop project

A small checkout library. Every amount in `shop` is an integer count of cents: `money.parse_price`
turns `"12.50"` into `1250`, and `money.format_cents` turns it back.

- `shop/money.py`: parsing and formatting prices.
- `shop/cart.py`: a cart of lines and its subtotal.
- `shop/coupons.py`: coupon codes and the discount each one gives.
- `shop/shipping.py`: the delivery fee, and the order size that gets free delivery.
- `shop/checkout.py`: the order total, the subtotal with the discount taken off and the fee added.

Run every test with `python3.12 check.py`.
