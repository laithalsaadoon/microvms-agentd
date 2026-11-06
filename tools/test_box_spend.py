# SPDX-License-Identifier: Apache-2.0
"""The Box example's driver prices Codex's model calls and interrupts a turn at its spend cap."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_box_metadata

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/strands-box-codex-sdk"
spec = importlib.util.spec_from_file_location("spend", EXAMPLE / "vm/spend.py")
spend = importlib.util.module_from_spec(spec)
# spend.py defers its annotations, and @dataclass resolves a string annotation through
# sys.modules[cls.__module__], so the module must be registered before it runs.
sys.modules[spec.name] = spend
spec.loader.exec_module(spend)


def tokens(input=0, cached=0, written=None, output=0):
    """A TokenUsageBreakdown as the SDK sends it. A cache-write count may be unset."""
    return SimpleNamespace(
        input_tokens=input,
        cached_input_tokens=cached,
        cache_write_input_tokens=written,
        output_tokens=output,
    )


def update(thread, total, last=None):
    """One `thread/tokenUsage/updated` event."""
    usage = SimpleNamespace(total=total, last=last if last is not None else total)
    return SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=SimpleNamespace(thread_id=thread, turn_id="turn", token_usage=usage),
    )


def other(method="item/completed"):
    return SimpleNamespace(method=method, payload=SimpleNamespace(turn_id="turn"))


class PriceTests(unittest.TestCase):
    def test_prices_fresh_input_both_cache_counts_and_output_at_the_card_rates(self):
        # 100,000 input tokens: 40,000 read from the cache, 10,000 written to it, and 50,000
        # fresh. Reasoning tokens sit inside the 2,000 output tokens.
        used = spend.counts(
            tokens(input=100_000, cached=40_000, written=10_000, output=2_000)
        )
        self.assertEqual(
            spend.price(used, spend.SHORT),
            50_000 * 2_000 + 10_000 * 2_500 + 40_000 * 100 + 2_000 * 10_000,
        )
        self.assertEqual(spend.usd(spend.price(used, spend.SHORT)), "$0.1490")

    def test_an_unset_cache_write_count_is_zero(self):
        used = spend.counts(tokens(input=1_000, written=None))
        self.assertEqual(used["cache_write_input_tokens"], 0)
        self.assertEqual(spend.price(used, spend.SHORT), 1_000 * 2_000)

    def test_long_context_rates_apply_to_a_whole_request_over_272k_input_tokens(self):
        meter = spend.Meter()
        meter.add("a", tokens(input=272_000, output=1_000), None)
        self.assertEqual(meter.spent, 272_000 * 2_000 + 1_000 * 10_000)
        meter = spend.Meter()
        meter.add("a", tokens(input=272_001, output=1_000), None)
        self.assertEqual(meter.spent, 272_001 * 4_000 + 1_000 * 15_000)

    def test_the_newest_request_picks_the_long_context_rates(self):
        meter = spend.Meter()
        meter.add("a", tokens(input=200_000), tokens(input=200_000))
        before = meter.spent
        # The thread's total grows by 100,000, but the request that grew it held 300,000.
        meter.add("a", tokens(input=300_000), tokens(input=300_000))
        self.assertEqual(meter.spent - before, 100_000 * 4_000)


class MeterTests(unittest.TestCase):
    def test_a_repeated_total_adds_nothing(self):
        # Codex sends the same totals again when only its rate limits change.
        meter = spend.Meter()
        meter.add("a", tokens(input=10_000, output=500), None)
        once = meter.spent
        meter.add("a", tokens(input=10_000, output=500), None)
        self.assertEqual(meter.spent, once)

    def test_each_update_prices_only_the_growth_of_its_threads_total(self):
        meter = spend.Meter()
        meter.add("a", tokens(input=10_000, output=500), None)
        meter.add("a", tokens(input=25_000, cached=10_000, output=900), None)
        self.assertEqual(
            meter.spent,
            10_000 * 2_000 + 500 * 10_000 + 5_000 * 2_000 + 10_000 * 100 + 400 * 10_000,
        )

    def test_threads_add_up_to_the_runs_spend(self):
        meter = spend.Meter()
        meter.add("a", tokens(input=10_000), None)
        meter.add("b", tokens(input=10_000), None)
        self.assertEqual(meter.spent, 2 * 10_000 * 2_000)


class CapTests(unittest.TestCase):
    def stream(self, events, cap):
        """Run `events` through the cap, and note the run's spend at each interrupt."""
        meter, calls = spend.Meter(), []
        seen = list(spend.capped(events, meter, cap, lambda: calls.append(meter.spent)))
        return seen, calls, meter

    def test_interrupts_once_when_the_spend_reaches_the_cap(self):
        # 1,000 input tokens cost 2,000,000 nano-dollars, so the second update reaches the cap
        # exactly, and the third passes it.
        events = [
            other(),
            update("a", tokens(input=1_000)),
            update("a", tokens(input=2_000)),
            other(),
            update("a", tokens(input=3_000)),
            other("turn/completed"),
        ]
        seen, calls, meter = self.stream(events, cap=4_000_000)
        self.assertEqual(calls, [4_000_000])
        self.assertEqual(seen, events)
        self.assertEqual(meter.spent, 6_000_000)

    def test_the_interrupt_comes_before_the_next_event(self):
        meter, order = spend.Meter(), []
        events = [update("a", tokens(input=1_000)), other()]
        for event in spend.capped(events, meter, 1, lambda: order.append("interrupt")):
            order.append(event.method)
        self.assertEqual(
            order, ["interrupt", "thread/tokenUsage/updated", "item/completed"]
        )

    def test_a_turn_under_the_cap_runs_to_its_end(self):
        events = [update("a", tokens(input=1_000)), other("turn/completed")]
        seen, calls, _ = self.stream(events, cap=spend.parse_usd("5"))
        self.assertEqual(calls, [])
        self.assertEqual(seen, events)


class ParseTests(unittest.TestCase):
    def test_reads_dollars_as_nano_dollars(self):
        self.assertEqual(spend.parse_usd("5"), 5 * spend.NANO)
        self.assertEqual(spend.parse_usd("0.25"), spend.NANO // 4)

    def test_refuses_what_is_not_a_dollar_amount(self):
        for text in ("-1", "five", "", "nan", "inf"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                spend.parse_usd(text)


class RunTests(unittest.TestCase):
    run_example = test_box_metadata.RunOrderingTests.run_example

    def driver_call(self, calls):
        return next(call[1] for call in calls if "driver.py" in call[1])

    def test_run_sh_hands_the_driver_a_five_dollar_cap_by_default(self):
        result, calls = self.run_example()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.driver_call(calls).endswith("driver.py --spend-cap-usd 5"))

    def test_run_sh_hands_the_driver_the_cap_it_is_given(self):
        result, calls = self.run_example(SPEND_CAP_USD="0.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.driver_call(calls).endswith("driver.py --spend-cap-usd 0.25")
        )

    def test_run_sh_refuses_a_cap_that_is_not_a_dollar_amount_before_any_aws_call(self):
        result, calls = self.run_example(SPEND_CAP_USD="5; rm -rf /")
        self.assertEqual(result.returncode, 2)
        self.assertIn("SPEND_CAP_USD must be a dollar amount", result.stderr)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
