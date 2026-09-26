// SPDX-License-Identifier: Apache-2.0
//! The one clock port: monotonic time, sleeping, and the wall time since the epoch.
//!
//! # One trait rather than one per module
//!
//! The control plane and the session each had a `Clock` of their own, over different time
//! sources: the control plane's read `std::time::Instant` and the session's read tokio's.
//! Under a deterministic simulator those disagree. tokio's clock is virtual there and `std`'s
//! isn't, so a control-plane wait whose sleeps advanced virtual time measured its deadline
//! on the host's real time and never reached it. `Sandbox` already times the suspended window
//! on the plane's clock rather than its own, so a window isn't measured on one clock while
//! the polls run on another. One trait, with tokio's clock in production, keeps that property
//! for everything that reads time. The trait is re-exported at `control::Clock` and at
//! `session::Clock`, the paths each half had.
//!
//! # Why a trait rather than `tokio::time::pause`
//!
//! The waits are driven by *elapsed* comparisons against a deadline and a stall grace, and a
//! test has to be able to say "now 300 seconds have passed" between two polls of a fake
//! control plane. A paused tokio clock can do that, but only for code that sleeps on tokio,
//! and it makes every test in a file share one global clock state. A [`Clock`] parameter
//! makes the dependency visible in the signature.
//!
//! # Monotonic and wall time are separate readings
//!
//! [`Clock::elapsed`] is monotonic, the same reasoning the Python client's injectable
//! `time.monotonic` carried: the suspended window is a *duration*, and a wall clock that steps
//! backward would reopen a closed one. [`Clock::unix_now`] is the wall reading, for the values
//! that are dates rather than durations: a name record's timestamp, an exec id, the cost
//! engine's today. Deriving one from the other would make a stepped wall clock move a
//! deadline.

use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use crate::cost::CalendarDate;
use crate::error::Error;

/// Time, injectable so a wait or a refresh boundary is a test rather than a wait.
pub trait Clock: Send + Sync + fmt::Debug {
    /// Monotonic time since this clock was created. Never decreases.
    fn elapsed(&self) -> Duration;

    /// Sleeps for `duration`.
    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>>;

    /// Wall time since the Unix epoch.
    ///
    /// Zero if the host clock is set before 1970, which is a machine whose dates are already
    /// meaningless; a reading that can't be negative is what every caller wants.
    fn unix_now(&self) -> Duration;

    /// Today, UTC, for the cost engine's dates.
    ///
    /// Fails only for a wall reading past 9999-12-31, the last date the domain represents.
    fn today_utc(&self) -> Result<CalendarDate, Error> {
        CalendarDate::from_unix_secs(self.unix_now().as_secs())
    }
}

#[cfg(any(test, feature = "test-support"))]
pub(crate) mod testing {
    //! The clocks the tests share, so the control-plane tests, the session tests and the
    //! CLI's guards don't each keep a near-copy that can drift.

    use std::sync::{Mutex, PoisonError};

    use super::*;

    /// 2026-01-01T00:00:00Z. A fixed, plausible wall time, so a test's dates don't depend on
    /// the day it runs and don't read as the epoch fallback.
    const START_UNIX_SECS: u64 = 1_767_225_600;

    #[derive(Debug)]
    struct Readings {
        elapsed: Duration,
        unix: Duration,
    }

    /// A clock a test drives by hand.
    ///
    /// `sleep` **advances** it rather than waiting, so a 45-minute build wait runs instantly
    /// and the code under test still sees time pass exactly as it would have. That is what
    /// makes the TRAP-2 stall test possible without a four-minute test, and a sixty-minute
    /// token boundary costs no wall time and no simulator. The wall reading moves with every
    /// advance, the way a real clock's does.
    #[derive(Debug)]
    pub struct TestClock {
        readings: Mutex<Readings>,
    }

    impl Default for TestClock {
        fn default() -> Self {
            Self {
                readings: Mutex::new(Readings {
                    elapsed: Duration::ZERO,
                    unix: Duration::from_secs(START_UNIX_SECS),
                }),
            }
        }
    }

    impl TestClock {
        pub fn new() -> Self {
            Self::default()
        }

        /// Moves the clock forward by `duration`, as if a sleep had happened.
        pub fn advance(&self, duration: Duration) {
            let mut readings = self.readings.lock().unwrap_or_else(PoisonError::into_inner);
            readings.elapsed += duration;
            readings.unix += duration;
        }

        /// The current elapsed reading.
        pub fn now(&self) -> Duration {
            self.readings
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .elapsed
        }

        /// Sets the wall reading, leaving the monotonic one where it is.
        pub fn set_unix_now(&self, unix: Duration) {
            self.readings
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .unix = unix;
        }
    }

    impl Clock for TestClock {
        fn elapsed(&self) -> Duration {
            self.now()
        }

        fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
            self.advance(duration);
            Box::pin(std::future::ready(()))
        }

        fn unix_now(&self) -> Duration {
            self.readings
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .unix
        }
    }

    /// A [`TestClock`] whose `sleep` also **yields** once.
    ///
    /// A `sleep` that only advanced would let a poll loop run every iteration inside one
    /// `poll` of its future, so a `select!` racing it against an interrupt, or a second caller
    /// sharing its fake, would never get a turn. Yielding returns `Pending` once per sleep,
    /// which is that turn. Time still passes only when slept through.
    #[derive(Debug, Default)]
    pub struct YieldingClock {
        inner: TestClock,
    }

    impl YieldingClock {
        pub fn new() -> Self {
            Self::default()
        }
    }

    impl std::ops::Deref for YieldingClock {
        type Target = TestClock;

        fn deref(&self) -> &TestClock {
            &self.inner
        }
    }

    impl Clock for YieldingClock {
        fn elapsed(&self) -> Duration {
            self.inner.elapsed()
        }

        fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send + '_>> {
            self.inner.advance(duration);
            Box::pin(tokio::task::yield_now())
        }

        fn unix_now(&self) -> Duration {
            self.inner.unix_now()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::TestClock;
    use super::*;

    /// The provided `today_utc` reads the clock's wall time, not the host's.
    ///
    /// **Falsification**: make `today_utc` read `SystemTime::now()` and the date below is
    /// the day the test runs instead.
    #[test]
    fn today_comes_from_the_clock_s_wall_reading() {
        let clock = TestClock::new();
        // 2024-02-29T12:00:00Z, a leap day, so an off-by-one in the day arithmetic shows.
        clock.set_unix_now(Duration::from_secs(1_709_208_000));
        assert_eq!(
            clock.today_utc().expect("a representable date"),
            CalendarDate::from_ymd(2024, 2, 29)
        );
    }

    /// A reading past the domain's last date is an error, not a wrapped date.
    #[test]
    fn a_wall_reading_past_9999_is_refused() {
        let clock = TestClock::new();
        clock.set_unix_now(Duration::from_secs(253_402_300_800));
        assert!(clock.today_utc().is_err());
    }

    /// Advancing moves both readings, so a test that sleeps through a day sees a new date.
    #[test]
    fn advancing_the_test_clock_moves_the_wall_reading_too() {
        let clock = TestClock::new();
        let (elapsed, unix) = (clock.elapsed(), clock.unix_now());
        clock.advance(Duration::from_secs(86_400));
        assert_eq!(clock.elapsed() - elapsed, Duration::from_secs(86_400));
        assert_eq!(clock.unix_now() - unix, Duration::from_secs(86_400));
    }
}
