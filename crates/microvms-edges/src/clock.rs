// SPDX-License-Identifier: Apache-2.0
//! The production clock.
//!
//! The port is `microvms_app::clock::Clock`, and why there's one of it is written there.

use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use microvms_app::clock::Clock;

/// The production clock: tokio's monotonic clock and the system's wall clock.
///
/// tokio's rather than `std`'s for a reason that is easy to get backwards: under a
/// deterministic simulator the tokio clock is virtual and `std::time::Instant` isn't, so a
/// sixty-minute boundary crossed in simulated time is invisible to `std`. A client that timed
/// its token or its launch wait against `std::time::Instant` would pass a long simulation
/// without ever re-minting or timing out. Outside a runtime whose clock is paused, which only
/// a simulator or a test sets up, `tokio::time::Instant::now()` is the std monotonic clock, so
/// production behavior is the same as it was over `std`.
///
/// The wall reading stays on `SystemTime` under a simulator. Nothing times a deadline on it.
#[derive(Debug)]
pub struct TokioClock {
    base: tokio::time::Instant,
}

impl TokioClock {
    pub fn new() -> Self {
        Self {
            base: tokio::time::Instant::now(),
        }
    }
}

impl Default for TokioClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for TokioClock {
    fn elapsed(&self) -> Duration {
        self.base.elapsed()
    }

    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
        Box::pin(tokio::time::sleep(duration))
    }

    fn unix_now(&self) -> Duration {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
    }
}

/// The control plane's name for [`TokioClock`], kept so `control::SystemClock::new()` and
/// `SystemClock::default()` still resolve.
pub use TokioClock as SystemClock;

#[cfg(test)]
mod tests {
    use super::*;

    /// The production clock's wall reading is the system's, and its sleep is tokio's, so a
    /// paused runtime moves `elapsed` by exactly the slept duration.
    #[tokio::test(start_paused = true)]
    async fn the_production_clock_measures_tokio_time() {
        let clock = TokioClock::new();
        clock.sleep(Duration::from_secs(1800)).await;
        assert_eq!(clock.elapsed(), Duration::from_secs(1800));
        let system = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("the host clock is after 1970");
        assert!(clock.unix_now().abs_diff(system) < Duration::from_secs(60));
    }
}
