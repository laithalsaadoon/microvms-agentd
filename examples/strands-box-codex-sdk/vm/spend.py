# SPDX-License-Identifier: Apache-2.0
"""What Codex's model calls cost, and the cap that interrupts a turn once they cost too much.

After each model response, the Codex app-server sends a `thread/tokenUsage/updated` notification
with the thread's running token totals. The driver passes every notification of a turn through
`capped`, which prices what each thread used since its last update and interrupts the turn once
the run's spend reaches the cap. The cap overshoots by at most the response that crossed it,
because Codex reports a response's tokens only after the response ends.

The driver runs outside the box and holds the cap, so nothing Codex does in the box can change
it. This module imports nothing from the SDK, so tools/test_box_spend.py checks it without one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

NANO = 10**9  # nano-dollars in a dollar; every amount here is a whole number of them

# A request with more input tokens than this pays the long-context rates on all its tokens.
LONG_CONTEXT = 272_000


@dataclass(frozen=True)
class Rates:
    """Nano-dollars per token, which is USD per million tokens times 1,000."""

    input: int
    cache_write: int
    cache_read: int
    output: int


# GPT-6.1 Sol, Global CRIS on bedrock-runtime, Standard tier, from the model card:
# https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-sol.html
SHORT = Rates(input=2_000, cache_write=2_500, cache_read=100, output=10_000)
LONG = Rates(input=4_000, cache_write=5_000, cache_read=200, output=15_000)

FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


def counts(breakdown) -> dict[str, int]:
    """The four counts of a TokenUsageBreakdown. Codex leaves cache writes unset on some."""
    return {name: getattr(breakdown, name, None) or 0 for name in FIELDS}


def price(used: dict[str, int], rates: Rates) -> int:
    """The nano-dollars that `used` tokens cost at `rates`.

    Codex counts cache reads and cache writes inside `input_tokens`, and reasoning tokens
    inside `output_tokens`, so fresh input is what's left of the input after both cache
    counts, and output is priced once.
    """
    cached, written = used["cached_input_tokens"], used["cache_write_input_tokens"]
    fresh = max(0, used["input_tokens"] - cached - written)
    return (
        fresh * rates.input
        + written * rates.cache_write
        + cached * rates.cache_read
        + used["output_tokens"] * rates.output
    )


class Meter:
    """The run's model spend in nano-dollars, built from each thread's running totals."""

    def __init__(self) -> None:
        self.spent = 0
        self._seen: dict[str, dict[str, int]] = {}

    def add(self, thread_id: str, total, last) -> int:
        """Price what `thread_id` used since its last update, and return the run's spend.

        Codex sends the same totals again when only its rate limits changed, so the meter
        prices the growth of `total` and an unchanged total adds nothing. `last` is the newest
        request alone; it picks the long-context rates when that request, or the growth since
        the last update, holds more than LONG_CONTEXT input tokens.
        """
        now = counts(total)
        before = self._seen.get(thread_id, dict.fromkeys(FIELDS, 0))
        self._seen[thread_id] = now
        grown = {name: max(0, now[name] - before[name]) for name in FIELDS}
        newest = counts(last)["input_tokens"] if last is not None else 0
        long = max(newest, grown["input_tokens"]) > LONG_CONTEXT
        self.spent += price(grown, LONG if long else SHORT)
        return self.spent


def capped(
    events: Iterable, meter: Meter, cap: int, interrupt: Callable[[], object]
) -> Iterator:
    """Yield every event of a turn, and call `interrupt` once, when spend first reaches `cap`.

    An event whose payload carries `token_usage` is a token usage update. The turn's other
    events pass through unchanged, so the caller collects the turn's result from them as usual.
    """
    interrupted = False
    for event in events:
        payload = getattr(event, "payload", None)
        usage = getattr(payload, "token_usage", None)
        if usage is not None:
            meter.add(payload.thread_id, usage.total, getattr(usage, "last", None))
            if not interrupted and meter.spent >= cap:
                interrupt()
                interrupted = True
        yield event


def parse_usd(text: str) -> int:
    """A dollar amount such as `5` or `0.25`, in nano-dollars. Refuses anything below zero."""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a dollar amount: {text!r}") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"not a dollar amount: {text!r}")
    return int(amount * NANO)


def usd(nano: int) -> str:
    """Nano-dollars as dollars to the hundredth of a cent, such as `$0.1490`."""
    return f"${Decimal(nano) / NANO:.4f}"
