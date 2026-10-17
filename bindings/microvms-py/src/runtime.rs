// SPDX-License-Identifier: Apache-2.0
//! One tokio runtime for the process, the two ways to block on it, and the two ways to await it.
//!
//! # Two spellings over one async core (BIND-25)
//!
//! `microvms-core` is async throughout. Every method that does I/O or waits has two Python
//! spellings: the blocking one (`sandbox.run(...)`), which works in a plain script, in a
//! notebook and under `pytest`, and an awaitable twin with the same arguments and an `_async`
//! suffix (`await sandbox.run_async(...)`), for a caller on an asyncio event loop. Both drive the
//! same future, built once by the method's `*_op` helper, so the second spelling is not a second
//! implementation: [`block_on`] runs that future on the calling thread, and [`spawn`] or
//! [`spawn_shielded`] runs it as a task on the shared runtime that a pyo3 `async fn` awaits.
//!
//! The suffix rather than an `a` prefix (`aread`, as httpx spells it) because two of the names
//! it would make, `ExecHandle.await` and `ExecProcess.await`, are a keyword Python refuses.
//!
//! # The awaitable runs on the shared runtime, not on the event loop
//!
//! A pyo3 `async fn` is polled by asyncio on the event loop's thread with the GIL held, and it
//! has no tokio context there. So the twin's body is spawned onto [`RUNTIME`] and the coroutine
//! awaits only the task's handle: the HTTP, the TLS, the polling and the hashing all run on
//! tokio's workers with the GIL released, and when the task finishes its waker hands the result
//! back through `loop.call_soon_threadsafe`. Nothing the loop runs waits on the network.
//!
//! # What cancelling an awaitable does
//!
//! asyncio cancels a task by throwing into its coroutine, and pyo3 then drops the future it was
//! polling, which drops the task's handle. Two outcomes follow from that, and each method picks
//! one by which spawn it calls:
//!
//! * [`spawn`] (BIND-26): the task is aborted at its next `.await`, the way dropping a Rust
//!   future cancels it. For reads, waits, polls, transfers and exec starts: nothing billable is
//!   half-made by stopping one, and a wait that went on without its caller would hold the
//!   sandbox's lock for the rest of its deadline. Content-addressed `ensure_image` uses
//!   this path too: an accepted build remains discoverable by name, and the sandbox records
//!   an image only after the wait succeeds.
//! * [`spawn_shielded`] (BIND-27): the task runs to completion with nobody awaiting it, and its
//!   effect lands on the sandbox it holds. For the lifecycle transitions (launch, build,
//!   suspend, resume, terminate and the control plane's own): aborting a launch between
//!   `RunMicrovm` and recording its answer would leave a running VM no handle names, which is
//!   the one outcome worse than waiting. It is what `asyncio.to_thread` over the blocking
//!   method does, since a thread can't be cancelled either.
//!
//! # The GIL is released first, and that is not an optimization
//!
//! [`block_on`] calls `py.detach` before `Runtime::block_on`. Blocking while attached to
//! the interpreter deadlocks the moment anything inside the future needs the GIL — and
//! things inside this future do: an [`crate::exec::ExecStream`] hands events back through
//! a channel a Python iterator drains. `Python::detach` is 0.29's name for what older
//! guides call `allow_threads`; the `Ungil` bound on the closure is what stops a `Bound`
//! reference being carried across the release, and it is a compile error rather than a
//! rule.
//!
//! # The re-entrancy guard
//!
//! `Runtime::block_on` panics when called from inside a runtime worker thread. That
//! happens for real: a caller running this module's methods from a thread another
//! extension's runtime owns, or from inside a `tokio::task::spawn_blocking`. So
//! [`block_on`] asks `Handle::try_current()` first and takes `block_in_place` on that
//! path, which is the documented way to block on a worker without wedging the scheduler.
//!
//! # The runtime is multi-thread and process-wide
//!
//! One `LazyLock`, not a runtime per call. A current-thread runtime would be cheaper to
//! create and wrong: the core's `ProxyAuth` mints under a `tokio::sync::Mutex` held
//! across an await, `block_in_place` requires the multi-thread flavour, and a spawned task
//! needs workers that run while no Python thread is blocked on anything. The `LazyLock`
//! holds only a runtime and calls no Python during initialization, so it is not the
//! `OnceLock`-against-the-GIL deadlock PyO3's FAQ warns about.
//!
//! # Python wake lifetime and interpreter shutdown
//!
//! A task can publish its result before its worker finishes waking the coroutine. Every
//! future polled by Python therefore uses a fenced waker: completion or cancellation closes
//! admission and waits for existing wake callbacks to finish, with the GIL released. The
//! direct channel receivers use the same fence as spawned tasks.
//!
//! A Python `atexit` callback closes and drains the remaining fences before interpreter
//! finalization, including coroutines callers retained without completing or cancelling.
//! It leaves Rust-only shielded work running and does not stop the runtime. Later async
//! operations raise `RuntimeError`; blocking operations retain their existing behavior.

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, LazyLock, Mutex, Weak};
use std::task::{Context, Poll, Wake, Waker};

use pyo3::prelude::*;
use tokio::task::JoinHandle;

use crate::errors::{CoreError, PyCoreResult};

/// The process's runtime. See the module docs for why one, why multi-thread, and why
/// `LazyLock` is safe here.
static RUNTIME: LazyLock<tokio::runtime::Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("microvms-py")
        .build()
        .expect("a multi-thread tokio runtime is buildable on every platform this loads on")
});

#[derive(Default)]
struct WakeRegistry {
    closed: bool,
    fences: Vec<Weak<WakeFence>>,
}

static WAKES: Mutex<WakeRegistry> = Mutex::new(WakeRegistry {
    closed: false,
    fences: Vec::new(),
});

#[derive(Default)]
struct WakeState {
    closed: bool,
    active: usize,
    waker: Option<Waker>,
}

#[derive(Default)]
struct WakeFence {
    state: Mutex<WakeState>,
    drained: Condvar,
    // Admission closes under the mutex. This separate flag becomes true only after
    // every admitted callback and original waker has finished, including its drop.
    quiescent: AtomicBool,
    #[cfg(test)]
    drain_arrivals: std::sync::atomic::AtomicUsize,
}

fn closed_error() -> CoreError {
    pyo3::exceptions::PyRuntimeError::new_err("the MicroVMs asynchronous runtime is shutting down")
        .into()
}

fn register_wake() -> PyCoreResult<Arc<WakeFence>> {
    let mut registry = WAKES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if registry.closed {
        return Err(closed_error());
    }
    registry.fences.retain(|fence| fence.strong_count() != 0);
    let fence = Arc::new(WakeFence::default());
    registry.fences.push(Arc::downgrade(&fence));
    Ok(fence)
}

/// Keeps the original waker's destruction inside the admission counted by the fence.
struct ActiveWake<'a> {
    fence: &'a WakeFence,
    waker: Option<Waker>,
}

impl Drop for ActiveWake<'_> {
    fn drop(&mut self) {
        // A waker can own Python references. Never destroy it under the state lock,
        // and do not publish quiescence before its last callback reference is gone.
        drop(self.waker.take());
        let mut state = self
            .fence
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.active -= 1;
        if state.active == 0 {
            self.fence.drained.notify_all();
        }
    }
}

impl WakeFence {
    fn update(&self, waker: &Waker) -> bool {
        Python::attach(|py| {
            py.detach(|| {
                let mut state = self
                    .state
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                if state.closed {
                    return false;
                }
                state.active += 1;
                let old = ActiveWake {
                    fence: self,
                    waker: state.waker.replace(waker.clone()),
                };
                drop(state);
                drop(old);
                true
            })
        })
    }

    fn close_detached(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.closed = true;
        // Removing the stored original waker is itself in flight until its drop
        // finishes, so concurrent closers cannot return before it is destroyed.
        let old = state.waker.take().map(|waker| {
            state.active += 1;
            ActiveWake {
                fence: self,
                waker: Some(waker),
            }
        });
        drop(state);
        drop(old);
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        #[cfg(test)]
        self.drain_arrivals.fetch_add(1, Ordering::Release);
        // A deadline would allow an admitted callback to touch the finalized interpreter.
        // Even a caller-supplied loop callback must finish before this drain can return.
        while state.active != 0 {
            state = self
                .drained
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
        self.quiescent.store(true, Ordering::Release);
    }

    fn close(&self) {
        if !self.quiescent.load(Ordering::Acquire) {
            // A worker may be inside Python::attach or waiting to acquire the GIL.
            // Waiting while attached would deadlock with that worker.
            Python::attach(|py| py.detach(|| self.close_detached()));
        }
    }
}

impl Wake for WakeFence {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.closed || state.waker.is_none() {
            return;
        }
        state.active += 1;
        let active = ActiveWake {
            fence: self,
            waker: state.waker.clone(),
        };
        drop(state);
        // Python's callback must run outside the lock. The guard stays alive through
        // its attach/release and original-waker drop, including unwinding.
        active.waker.as_ref().unwrap().wake_by_ref();
    }
}

/// Run before interpreter finalization, while detaching can still let a Python wake finish.
#[pyfunction]
fn close_async_wakes(py: Python<'_>) {
    py.detach(|| {
        let fences = {
            let mut registry = WAKES
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            registry.closed = true;
            std::mem::take(&mut registry.fences)
        };
        for fence in fences.into_iter().filter_map(|fence| fence.upgrade()) {
            fence.close_detached();
        }
    });
}

pub(crate) fn register_shutdown(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.py().import("atexit")?.call_method1(
        "register",
        (pyo3::wrap_pyfunction!(close_async_wakes, module)?,),
    )?;
    Ok(())
}

/// Prevent a polled Rust future from retaining Python's waker after completion or drop.
pub(crate) struct Fenced<F: Future> {
    future: Pin<Box<F>>,
    fence: Arc<WakeFence>,
}

pub(crate) fn fenced<F: Future>(future: F) -> PyCoreResult<Fenced<F>> {
    Ok(Fenced {
        future: Box::pin(future),
        fence: register_wake()?,
    })
}

impl<F: Future> Future for Fenced<F> {
    type Output = PyCoreResult<F::Output>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        if !self.fence.update(cx.waker()) {
            return Poll::Ready(Err(closed_error()));
        }
        let waker = Waker::from(Arc::clone(&self.fence));
        let polled = self.future.as_mut().poll(&mut Context::from_waker(&waker));
        match polled {
            Poll::Pending => Poll::Pending,
            Poll::Ready(output) => {
                self.fence.close();
                Poll::Ready(Ok(output))
            }
        }
    }
}

impl<F: Future> Drop for Fenced<F> {
    fn drop(&mut self) {
        self.fence.close();
    }
}

/// Runs `future` to completion with the GIL released.
///
/// The `Send + 'static` bounds on the future are what make the detach sound: nothing
/// borrowed from the interpreter can be captured, so there is no `Bound` to touch while
/// the GIL is elsewhere.
pub(crate) fn block_on<F>(py: Python<'_>, future: F) -> F::Output
where
    F: std::future::Future + Send,
    F::Output: Send,
{
    py.detach(|| block_on_detached(future))
}

/// [`block_on`] for a caller that has already released the GIL, or never held it.
///
/// Separate because the stream reader in [`crate::exec`] runs on a thread with no
/// `Python` token at all, and threading a token there only to detach it would be a
/// fiction.
pub(crate) fn block_on_detached<F>(future: F) -> F::Output
where
    F: std::future::Future,
{
    match tokio::runtime::Handle::try_current() {
        // Already on a worker. `block_on` here panics; `block_in_place` moves the
        // current task off the worker so the rest of the scheduler keeps running.
        Ok(_) => tokio::task::block_in_place(|| RUNTIME.block_on(future)),
        Err(_) => RUNTIME.block_on(future),
    }
}

/// A handle for spawning onto the shared runtime.
///
/// The stream iterator needs this: it spawns the consumer of a core `Stream` as a task
/// and reads events off a channel, because a `Stream` cannot be advanced from a
/// `__next__` that has to return between items.
pub(crate) fn handle() -> tokio::runtime::Handle {
    RUNTIME.handle().clone()
}

/// Spawns `future` on the shared runtime and returns the awaitable side of it, which aborts
/// the task when it is dropped unfinished: a cancelled coroutine stops the work.
///
/// For every awaitable twin but the lifecycle transitions; the module docs say which is which
/// and why.
pub(crate) fn spawn<F>(future: F) -> Spawned<F::Output>
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    spawned(|| RUNTIME.spawn(future), true)
}

/// [`spawn`] for a lifecycle transition: dropping the awaitable leaves the task running to
/// completion, so a cancelled launch still records its VM on the sandbox that started it.
pub(crate) fn spawn_shielded<F>(future: F) -> Spawned<F::Output>
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    spawned(|| RUNTIME.spawn(future), false)
}

/// Keep an in-flight lifecycle operation shielded, but signal cancellation so its
/// retry loop can stop before submitting another request.
pub(crate) fn spawn_with_retry_cancellation<F>(
    future: F,
    cancellation: microvms_core::control::transport::RetryCancellation,
) -> Spawned<F::Output>
where
    F: Future + Send + 'static,
    F::Output: Send + 'static,
{
    let mut task = spawned(|| RUNTIME.spawn(future), false);
    task.on_cancel = Some(Box::new(move || cancellation.cancel()));
    task
}

/// Runs a blocking core call on the shared runtime's blocking pool and returns the awaitable
/// side of it: the twin of a call core makes synchronous, such as the daemon fetch, which runs
/// its own thread. A thread can't be stopped, so a cancelled awaitable leaves the call to finish,
/// as [`spawn_shielded`] does.
pub(crate) fn spawn_blocking<F, T>(call: F) -> Spawned<T>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    spawned(|| RUNTIME.spawn_blocking(call), false)
}

fn spawned<T>(start: impl FnOnce() -> JoinHandle<T>, abort_on_drop: bool) -> Spawned<T> {
    let task = register_wake()
        .map(|fence| Fenced {
            future: Box::pin(start()),
            fence,
        })
        .map_err(Some);
    Spawned {
        task,
        abort_on_drop,
        on_cancel: None,
    }
}

/// A task on the shared runtime, awaited from a pyo3 coroutine. See [`spawn`].
///
/// Its output carries either the task's result or a Python bridge shutdown error.
/// A task that panicked re-raises the panic here, on the
/// coroutine's poll, where pyo3 turns it into `PanicException` exactly as it does for a panic in
/// a blocking method; the runtime is never shut down, so the only cancellation a task meets is
/// [`Spawned`]'s own abort, which happens after nothing can await it any more.
pub(crate) struct Spawned<T> {
    task: Result<Fenced<JoinHandle<T>>, Option<CoreError>>,
    abort_on_drop: bool,
    on_cancel: Option<Box<dyn FnOnce() + Send>>,
}

impl<T> Future for Spawned<T> {
    type Output = PyCoreResult<T>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let task = match &mut self.task {
            Ok(task) => task,
            Err(error) => {
                return Poll::Ready(Err(error.take().expect("a rejected task is polled once")));
            }
        };
        match Pin::new(task).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(error)) => Poll::Ready(Err(error)),
            Poll::Ready(Ok(Ok(output))) => Poll::Ready(Ok(output)),
            Poll::Ready(Ok(Err(error))) => match error.try_into_panic() {
                Ok(payload) => std::panic::resume_unwind(payload),
                Err(error) => {
                    panic!("a task on the shared runtime ended without a result: {error}")
                }
            },
        }
    }
}

impl<T> Drop for Spawned<T> {
    fn drop(&mut self) {
        // Aborting a finished task is a no-op, so this needs no "did it finish" check.
        if let Ok(task) = &self.task {
            task.fence.close();
            let task = task.future.as_ref().get_ref();
            if !task.is_finished()
                && let Some(cancel) = self.on_cancel.take()
            {
                cancel();
            }
            if self.abort_on_drop {
                task.abort();
            }
        }
    }
}

/// Takes a tokio mutex from a blocking method or a property, with the interpreter held.
///
/// The uncontended case is the common one and costs one atomic: a property read between
/// calls. When the lock is held, by a transition in flight or an awaitable twin running on the
/// runtime, the GIL is released for the wait, because the holder can be a coroutine whose
/// completion needs it, and [`block_on_detached`] takes the wait onto the runtime so a property
/// read from a runtime worker doesn't panic as `blocking_lock` would.
pub(crate) fn lock_now<'a, T>(
    py: Python<'_>,
    mutex: &'a tokio::sync::Mutex<T>,
) -> tokio::sync::MutexGuard<'a, T>
where
    T: Send,
{
    match mutex.try_lock() {
        Ok(guard) => guard,
        Err(_) => py.detach(|| block_on_detached(mutex.lock())),
    }
}

/// The awaitable an `__anext__` answers, typed in the stub as `Awaitable[T]` where `T` is what
/// awaiting it yields.
///
/// pyo3 turns an `async fn` into a coroutine for an ordinary method but not for the `__anext__`
/// slot, so each async iterator's `__anext__` calls an `async fn` of a helper class and answers
/// the coroutine that call returns. That coroutine is a `Bound<PyAny>` to pyo3's introspection,
/// which would print `-> Any` and give every `async for` loop untyped elements; this wrapper
/// carries the element type to the stub and converts to the coroutine itself.
pub(crate) struct Awaitable<'py, T> {
    coroutine: Bound<'py, PyAny>,
    yields: std::marker::PhantomData<T>,
}

impl<'py, T> Awaitable<'py, T> {
    /// Calls `method` on `helper` for the coroutine its `async fn` returns.
    pub(crate) fn call(helper: &Bound<'py, PyAny>, method: &str) -> PyResult<Self> {
        Ok(Self {
            coroutine: helper.call_method0(method)?,
            yields: std::marker::PhantomData,
        })
    }
}

impl<'py, T> IntoPyObject<'py> for Awaitable<'py, T>
where
    T: IntoPyObject<'py>,
{
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = std::convert::Infallible;

    #[cfg(feature = "stubs")]
    const OUTPUT_TYPE: pyo3::inspect::PyStaticExpr = pyo3::type_hint_subscript!(
        pyo3::type_hint_identifier!("collections.abc", "Awaitable"),
        T::OUTPUT_TYPE
    );

    fn into_pyobject(self, _py: Python<'py>) -> Result<Self::Output, Self::Error> {
        Ok(self.coroutine)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    fn wait_for_drainers(fence: &WakeFence, expected: usize) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while fence.drain_arrivals.load(Ordering::Acquire) < expected {
            assert!(Instant::now() < deadline, "closers did not reach the drain");
            std::thread::yield_now();
        }
    }

    struct BlockingWake {
        entered: mpsc::Sender<()>,
        release: Mutex<mpsc::Receiver<()>>,
    }

    impl Wake for BlockingWake {
        fn wake(self: Arc<Self>) {
            self.wake_by_ref();
        }

        fn wake_by_ref(self: &Arc<Self>) {
            self.entered.send(()).unwrap();
            self.release.lock().unwrap().recv().unwrap();
        }
    }

    #[test]
    fn concurrent_closers_wait_until_the_admitted_wake_finishes() {
        let (entered, did_enter) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let fence = Arc::new(WakeFence::default());
        fence.state.lock().unwrap().waker = Some(Waker::from(Arc::new(BlockingWake {
            entered,
            release: Mutex::new(released),
        })));
        let wake = Arc::clone(&fence);
        let worker = std::thread::spawn(move || wake.wake_by_ref());
        did_enter.recv_timeout(Duration::from_secs(5)).unwrap();

        let (finished, did_finish) = mpsc::channel();
        let closers: Vec<_> = (0..2)
            .map(|_| {
                let fence = Arc::clone(&fence);
                let finished = finished.clone();
                std::thread::spawn(move || {
                    fence.close_detached();
                    finished.send(()).unwrap();
                })
            })
            .collect();
        wait_for_drainers(&fence, 2);
        // The handshake proves both closers arrived; the bounded observation checks
        // that neither returns while the admitted callback is deliberately held.
        let before_release = did_finish.recv_timeout(Duration::from_millis(100));
        let published_early = fence.quiescent.load(Ordering::Acquire);
        release.send(()).unwrap();
        worker.join().unwrap();
        for closer in closers {
            closer.join().unwrap();
        }
        assert_eq!(before_release, Err(mpsc::RecvTimeoutError::Timeout));
        assert!(!published_early);
        assert!(fence.quiescent.load(Ordering::Acquire));
    }

    struct BlockingDrop {
        entered: mpsc::Sender<()>,
        release: Mutex<mpsc::Receiver<()>>,
    }

    #[expect(
        clippy::manual_noop_waker,
        reason = "the test observes this waker's blocking destructor rather than its wake"
    )]
    impl Wake for BlockingDrop {
        fn wake(self: Arc<Self>) {}
    }

    impl Drop for BlockingDrop {
        fn drop(&mut self) {
            self.entered.send(()).unwrap();
            self.release.lock().unwrap().recv().unwrap();
        }
    }

    #[test]
    fn concurrent_closers_wait_for_the_last_original_waker_drop() {
        let (entered, did_enter) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let fence = Arc::new(WakeFence::default());
        fence.state.lock().unwrap().waker = Some(Waker::from(Arc::new(BlockingDrop {
            entered,
            release: Mutex::new(released),
        })));
        let first = Arc::clone(&fence);
        let first = std::thread::spawn(move || first.close_detached());
        did_enter.recv_timeout(Duration::from_secs(5)).unwrap();
        let (finished, did_finish) = mpsc::channel();
        let second = Arc::clone(&fence);
        let second = std::thread::spawn(move || {
            second.close_detached();
            finished.send(()).unwrap();
        });
        wait_for_drainers(&fence, 1);
        // The first closer is held in waker destruction; the second reached its drain.
        let before_release = did_finish.recv_timeout(Duration::from_millis(100));
        let published_early = fence.quiescent.load(Ordering::Acquire);
        release.send(()).unwrap();
        first.join().unwrap();
        second.join().unwrap();
        assert_eq!(before_release, Err(mpsc::RecvTimeoutError::Timeout));
        assert!(!published_early);
        assert!(fence.quiescent.load(Ordering::Acquire));
    }
}
