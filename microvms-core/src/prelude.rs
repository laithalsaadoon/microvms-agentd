// SPDX-License-Identifier: Apache-2.0
//! The production wiring, under the method names it had in 0.10.
//!
//! Two kinds of method live here as extension traits. The domain can't read the clock or the OS
//! random pool (ARCH-6), so the methods its types had that did moved here. And the use cases
//! take their I/O through ports, so the constructors that built the production transport, HTTP
//! backend, clock or entropy for their caller moved here too: `ControlPlane::new`,
//! `Sandbox::new`, `Session::connect` and the rest. Each wires the production adapters into a
//! port-taking constructor the type keeps, such as [`ControlPlane::from_ports`] or
//! [`SessionBuilder::try_build`].
//!
//! With `use microvms_core::prelude::*;` in scope, a call written against 0.10, such as
//! `CalendarDate::today_utc()` or `ControlPlane::new(region)`, compiles unchanged: a path to a
//! type's associated function also finds the methods of traits in scope.
//!
//! Every async method returns a `Send` future, as the inherent `async fn` it replaces did, so
//! the bindings can still hand one to a multi-threaded runtime.

use std::future::Future;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use microvms_edges::identity::HandshakeState;

use crate::adapters::SystemAdapters;
use crate::agents::{AgentSpec, AgentVm, require_specs};
use crate::clock::{Clock, SystemClock};
use crate::control::transport::{SignedTransport, Transport};
use crate::control::{BuildContext, ControlPlane};
use crate::cost::CalendarDate;
use crate::entropy::OsEntropy;
use crate::error::Error;
use crate::identity::{LaunchIdentity, TunnelIdentity};
use crate::names::{NameRecord, NameStore};
use crate::region::Region;
use crate::sandbox::{ControlPlaneMinter, Sandbox};
use crate::session::{ProxyAuth, Session, SessionBuilder, TokenMinter};

/// [`CalendarDate`]'s clock read.
pub trait CalendarDateExt {
    /// Today, UTC, from the system clock.
    ///
    /// Falls back to the epoch if the clock is set before 1970 or after 9999, which is a
    /// machine whose age arithmetic is already meaningless. That's what 0.10 did, and the
    /// signature can't grow an error without breaking the callers the prelude exists for.
    fn today_utc() -> CalendarDate;
}

impl CalendarDateExt for CalendarDate {
    fn today_utc() -> CalendarDate {
        SystemClock::new()
            .today_utc()
            .unwrap_or(CalendarDate::from_ymd(1970, 1, 1))
    }
}

/// [`NameRecord`]'s timestamp.
pub trait NameRecordExt: Sized {
    /// A record for a VM this process can already address, stamped with the current time.
    ///
    /// Refuses an illegal name or an empty id, endpoint, or token: a record missing any of
    /// them resolves to a VM nobody can adopt.
    fn new(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
    ) -> Result<Self, Error>;
}

impl NameRecordExt for NameRecord {
    fn new(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
    ) -> Result<Self, Error> {
        let at = SystemClock::new().unix_now().as_secs();
        NameRecord::new_at(name, microvm_id, endpoint, agent_token, region, at)
    }
}

/// [`LaunchIdentity`]'s seeds, from the OS random pool.
pub trait LaunchIdentityExt: Sized {
    /// Fresh material from the OS random pool.
    ///
    /// An all-zero draw isn't reachable from a working pool. It's still refused, because
    /// the protocol crate refuses a zero seed (a shared identity proves nothing), and this
    /// constructor must never hand out material the daemon would reject at bootstrap.
    fn generate() -> Result<Self, Error>;
}

impl LaunchIdentityExt for LaunchIdentity {
    fn generate() -> Result<Self, Error> {
        crate::entropy::launch_identity(&OsEntropy)
    }
}

/// [`TunnelIdentity`]'s Noise initiator, which draws its ephemeral key from the OS pool.
pub trait TunnelIdentityExt {
    /// A Noise initiator bound to this identity: proves the host, verifies the pin.
    ///
    /// Built per connection. A `HandshakeState` carries nonces and must never be reused.
    fn initiator(&self) -> Result<HandshakeState, Error>;
}

impl TunnelIdentityExt for TunnelIdentity {
    fn initiator(&self) -> Result<HandshakeState, Error> {
        microvms_edges::identity::initiator(self)
    }
}

/// [`ControlPlane`]'s production constructors.
pub trait ControlPlaneExt: Sized {
    /// Resolves credentials for `region` and returns a usable client.
    ///
    /// The region is a [`Region`], so an unsupported one is either a compile error or a
    /// visible [`Region::unlisted`] at the call site. TRAP-6 is closed by the type before
    /// this function is reached, which is why there is no region check here.
    fn new(region: Region) -> impl Future<Output = Result<Self, Error>> + Send;

    /// A client over a caller-supplied transport and clock, with the OS random pool and the
    /// production adapters.
    ///
    /// A caller who passes a real transport here gets exactly what `new` builds. The entropy
    /// is the OS pool's even over a scripted transport, and that's deliberate: such a plane
    /// still mints client tokens, and a deterministic default would mint repeating ones
    /// (TRAP-1). [`ControlPlane::from_ports`] is how a test pins it.
    fn with_transport(transport: Arc<dyn Transport>, region: Region, clock: Arc<dyn Clock>)
    -> Self;
}

impl ControlPlaneExt for ControlPlane {
    async fn new(region: Region) -> Result<Self, Error> {
        let transport = SignedTransport::new(region.clone()).await?;
        Ok(Self::with_transport(
            Arc::new(transport),
            region,
            Arc::new(SystemClock::new()),
        ))
    }

    fn with_transport(
        transport: Arc<dyn Transport>,
        region: Region,
        clock: Arc<dyn Clock>,
    ) -> Self {
        ControlPlane::from_ports(
            transport,
            region,
            clock,
            Arc::new(OsEntropy),
            Arc::new(SystemAdapters),
        )
    }
}

/// A production plane for `region`, on `port` when one is given.
async fn plane_for(region: Region, port: Option<u16>) -> Result<ControlPlane, Error> {
    let control = ControlPlane::new(region).await?;
    match port {
        Some(port) => control.with_port(port),
        None => Ok(control),
    }
}

/// [`Sandbox`]'s constructors that resolve credentials for a region.
pub trait SandboxExt: Sized {
    /// Resolves credentials for `region` and returns a sandbox with nothing launched.
    ///
    /// TRAP-6 is closed by [`Region`] before this is reached, which is why there is no
    /// region check here.
    fn new(region: Region) -> impl Future<Output = Result<Self, Error>> + Send;

    /// [`Sandbox::adopt`] over a plane resolved for `region`, on `port` when the image's
    /// daemon listens somewhere other than the default. The bindings' entry point.
    fn adopt_in(
        region: Region,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;

    /// Adopts the VM registered as `name` in `store`: [`Sandbox::adopt`] from a record.
    ///
    /// `region`, when given, must match the record's; see [`crate::names::resolve`].
    fn from_name(
        store: &dyn NameStore,
        name: &str,
        region: Option<Region>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;
}

impl SandboxExt for Sandbox {
    async fn new(region: Region) -> Result<Self, Error> {
        Ok(Self::with_control_plane(ControlPlane::new(region).await?))
    }

    fn adopt_in(
        region: Region,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send {
        // Converted before the future is built, so it holds strings rather than the caller's
        // argument types and stays `Send` whatever they were.
        let (microvm_id, endpoint, agent_token) =
            (microvm_id.into(), endpoint.into(), agent_token.into());
        async move {
            Self::adopt(
                plane_for(region, port).await?,
                microvm_id,
                endpoint,
                agent_token,
            )
            .await
        }
    }

    async fn from_name(
        store: &dyn NameStore,
        name: &str,
        region: Option<Region>,
        port: Option<u16>,
    ) -> Result<Self, Error> {
        let record = crate::names::resolve(store, name, region.as_ref())?;
        let control = plane_for(record.region(), port).await?;
        Self::adopt_record(control, record).await
    }
}

/// [`AgentVm`]'s constructors that resolve credentials for a region.
pub trait AgentVmExt: Sized {
    /// Adopts the VM registered as `name` in `store`; see `Sandbox::from_name`
    /// ([`SandboxExt::from_name`]).
    fn from_name(
        store: &dyn NameStore,
        specs: Vec<AgentSpec>,
        name: &str,
        region: Option<Region>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;

    /// [`AgentVm::adopt`] over a plane resolved for `region`; the bindings' entry point.
    fn adopt_in(
        region: Region,
        specs: Vec<AgentSpec>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;
}

impl AgentVmExt for AgentVm {
    async fn from_name(
        store: &dyn NameStore,
        specs: Vec<AgentSpec>,
        name: &str,
        region: Option<Region>,
        port: Option<u16>,
    ) -> Result<Self, Error> {
        // The specs first, as the adopt path checks them: a set this client refuses costs no
        // credential resolution and no call.
        require_specs(&specs)?;
        let sandbox = Sandbox::from_name(store, name, region, port).await?;
        AgentVm::new(sandbox, specs)
    }

    fn adopt_in(
        region: Region,
        specs: Vec<AgentSpec>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
    ) -> impl Future<Output = Result<Self, Error>> + Send {
        let (microvm_id, endpoint, agent_token) =
            (microvm_id.into(), endpoint.into(), agent_token.into());
        async move {
            require_specs(&specs)?;
            let sandbox =
                Sandbox::adopt_in(region, microvm_id, endpoint, agent_token, port).await?;
            AgentVm::new(sandbox, specs)
        }
    }
}

/// [`Session`]'s constructors over the production HTTP backend.
pub trait SessionExt: Sized {
    /// A session against `endpoint`, minting proxy tokens through `minter`.
    ///
    /// Does not talk to the VM. Constructing a session is free and re-doable, and a
    /// constructor that probed would make "do I have a session" mean "is the VM up",
    /// which are different questions with different answers during a launch.
    fn connect(
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        minter: Arc<dyn TokenMinter>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;

    /// Reattach to an existing AWS VM using a privately retained agent token.
    ///
    /// Resolves AWS credentials but does not probe the guest or bootstrap it again.
    /// The returned session is independent of any sandbox lock: use a separate attach
    /// with a short request timeout for supervisor keepalives during long transfers.
    /// Keep `agent_token` in private encrypted storage, never in public job status.
    fn attach(
        region: Region,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
        request_timeout: Option<Duration>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;

    /// A session with no proxy auth, for a daemon reached directly.
    ///
    /// The conformance path and every local-binary test go through here. See
    /// [`crate::session::Transport::proxy`] on why this is a supported shape rather than a
    /// test-only escape hatch.
    fn direct(endpoint: impl Into<String>, agent_token: impl Into<String>) -> Result<Self, Error>;
}

impl SessionExt for Session {
    fn connect(
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        minter: Arc<dyn TokenMinter>,
    ) -> impl Future<Output = Result<Self, Error>> + Send {
        // Built now rather than inside the future, which is then `Send` whatever the caller's
        // argument types were. Nothing here awaits.
        let session = Session::builder(endpoint, agent_token)
            .with_minter(minter)
            .build();
        std::future::ready(session)
    }

    fn attach(
        region: Region,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
        request_timeout: Option<Duration>,
    ) -> impl Future<Output = Result<Self, Error>> + Send {
        let (microvm_id, endpoint, agent_token) =
            (microvm_id.into(), endpoint.into(), agent_token.into());
        async move {
            if request_timeout.is_some_and(|timeout| timeout.is_zero()) {
                return Err(Error::invalid_arg("request timeout must be positive"));
            }
            let control = plane_for(region, port).await?;
            let port = control.port();
            let minter = Arc::new(ControlPlaneMinter::new(Arc::new(control), microvm_id));
            let mut builder = Session::builder(endpoint, agent_token)
                .with_minter(minter)
                .with_port(port);
            if let Some(timeout) = request_timeout {
                builder = builder.with_timeout(timeout);
            }
            builder.build()
        }
    }

    fn direct(endpoint: impl Into<String>, agent_token: impl Into<String>) -> Result<Self, Error> {
        Session::builder(endpoint, agent_token).build()
    }
}

/// [`SessionBuilder`]'s build over the production adapters.
pub trait SessionBuilderExt {
    /// Builds the session, with reqwest for an unset backend and tokio's clock for an unset
    /// clock. [`SessionBuilder::build_with`] over [`SystemAdapters`].
    fn build(self) -> Result<Session, Error>;
}

impl SessionBuilderExt for SessionBuilder {
    fn build(self) -> Result<Session, Error> {
        self.build_with(&SystemAdapters)
    }
}

/// [`ProxyAuth`]'s constructor on tokio's clock.
pub trait ProxyAuthExt {
    /// A [`ProxyAuth`] with the validated defaults: [`ProxyAuth::with_clock`] on tokio's clock.
    fn new(minter: Arc<dyn TokenMinter>, port: u16) -> Self;
}

impl ProxyAuthExt for ProxyAuth {
    fn new(minter: Arc<dyn TokenMinter>, port: u16) -> Self {
        ProxyAuth::with_clock(minter, port, Arc::new(SystemClock::new()))
    }
}

/// [`BuildContext`]'s directory read.
pub trait BuildContextExt: Sized {
    /// Reads `dir` as a build context; see [`crate::control::context::from_dir`].
    fn from_dir(dir: impl AsRef<Path>) -> Result<Self, Error>;
}

impl BuildContextExt for BuildContext {
    fn from_dir(dir: impl AsRef<Path>) -> Result<Self, Error> {
        crate::control::context::from_dir(dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;

    /// A plane `with_transport` builds draws from the OS pool, never a deterministic source.
    /// A scripted transport still gets real client tokens, which is what TRAP-1 needs.
    ///
    /// **Falsification**: wire `SequenceEntropy` into `with_transport` and two planes draw
    /// the same bytes.
    #[test]
    fn with_transport_draws_from_the_os_pool() {
        let plane = || {
            ControlPlane::with_transport(
                Arc::new(microvms_app::testing::FakeControlPlane::new()),
                Region::UsEast1,
                Arc::new(microvms_app::testing::TestClock::new()),
            )
        };
        let draw = |plane: &ControlPlane| {
            let mut bytes = [0_u8; 16];
            plane
                .entropy()
                .fill(&mut bytes)
                .expect("the pool is available");
            bytes
        };
        assert_ne!(draw(&plane()), draw(&plane()));
    }

    /// A name registered in another region, or not registered at all, is refused locally by
    /// `Sandbox::from_name` and `AgentVm::from_name`, and the refusal never carries the token.
    ///
    /// **Falsification**: resolve the record without the caller's region and the foreign
    /// record goes on to adopt, which fails some other way (run the break with no
    /// credentials in the environment, so nothing reaches AWS).
    #[tokio::test]
    async fn from_name_refuses_a_foreign_or_missing_name_before_any_plane() {
        struct One(NameRecord);
        impl NameStore for One {
            fn get(&self, name: &str) -> Result<Option<NameRecord>, Error> {
                Ok((name == self.0.name).then(|| self.0.clone()))
            }
            fn put(&self, _: &NameRecord) -> Result<(), Error> {
                unreachable!()
            }
            fn delete(&self, _: &str) -> Result<bool, Error> {
                unreachable!()
            }
            fn list(&self) -> Result<Vec<NameRecord>, Error> {
                Ok(vec![self.0.clone()])
            }
        }
        const TOKEN: &str = "from-name-canary-token";
        let record = NameRecord::new(
            "ci",
            "mvm-abc123",
            "https://mvm-abc123.microvm.us-west-2.amazonaws.com",
            TOKEN,
            "us-west-2",
        )
        .expect("record");
        let store = One(record);
        let foreign = Sandbox::from_name(&store, "ci", Some(Region::UsEast1), None)
            .await
            .expect_err("registered in another region");
        assert_eq!(foreign.kind(), ErrorKind::InvalidArg);
        let missing = Sandbox::from_name(&store, "nope", None, None)
            .await
            .expect_err("no such name");
        assert_eq!(missing.kind(), ErrorKind::Precondition);
        let specs = vec![AgentSpec::new(crate::agents::Agent::Codex)];
        let agent = AgentVm::from_name(&store, specs, "ci", Some(Region::UsEast1), None)
            .await
            .expect_err("registered in another region");
        assert_eq!(agent.kind(), ErrorKind::InvalidArg);
        assert!(!format!("{foreign} {missing} {agent}").contains(TOKEN));
    }

    /// The production session builds without a backend or a clock set: the prelude fills
    /// both, as 0.10's `build` did.
    ///
    /// **Falsification**: make `SessionBuilderExt::build` call `try_build` and the direct
    /// session is refused for having neither.
    #[test]
    fn the_production_build_fills_the_backend_and_the_clock() {
        let session = Session::direct("http://127.0.0.1:9", "token").expect("a direct session");
        assert!(session.proxy_auth().is_none());
    }

    /// `ProxyAuth::new` keeps 0.10's defaults: the default refresh interval, and nothing
    /// minted yet.
    ///
    /// **Falsification**: build it with any other interval and its `Debug` names that one.
    #[test]
    fn proxy_auth_new_keeps_the_defaults() {
        let auth = ProxyAuth::new(
            Arc::new(microvms_app::testing::CountingMinter::default()),
            crate::session::DEFAULT_AGENT_PORT,
        );
        assert_eq!(auth.port(), crate::session::DEFAULT_AGENT_PORT);
        assert_eq!(auth.mint_count(), 0);
        assert!(!auth.is_cached());
        let described = format!("{auth:?}");
        assert!(
            described.contains(&format!(
                "refresh_after: {:?}",
                crate::session::DEFAULT_REFRESH_AFTER
            )),
            "{described}"
        );
    }
}
