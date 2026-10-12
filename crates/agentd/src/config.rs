// SPDX-License-Identifier: Apache-2.0
//! Runtime configuration.
//!
//! Every bound here exists because an unbounded version of it was a defect in the
//! Python predecessor. The MicroVM's baseline memory can be as low as 512 MiB and
//! an OOM-killed daemon is unrecoverable — the platform forwards no traffic to a
//! dead process and there is no supervisor inside the VM to restart it.

use std::time::Duration;

/// Daemon configuration. Defaults are sized for a 512 MiB baseline VM.
#[derive(Clone, Debug)]
pub struct Config {
    /// Port the control API and lifecycle hooks listen on.
    pub port: u16,
    /// Largest request body accepted on the wire, enforced by
    /// `tower_http::limit::RequestBodyLimitLayer` because `DefaultBodyLimit` does
    /// not apply to bodies consumed as a stream.
    pub max_body_bytes: usize,
    /// Bytes of a rejected request's body the daemon will drain before closing.
    /// Draining lets a client's pooled connection survive an error response;
    /// draining without a cap is itself a denial-of-service vector, so anything
    /// larger closes instead.
    pub max_drain_bytes: usize,
    /// Per-stream cap on captured exec output. Exceeding it truncates and sets a
    /// marker on the result rather than growing until the daemon is killed.
    pub max_output_bytes: usize,
    /// How long to keep reading an exec's pipes after the direct child exits.
    /// Grandchildren inherit the pipe, so EOF can lag the child's exit; without a
    /// deadline a backgrounded process would hold the exec open indefinitely.
    pub output_linger: Duration,
    /// How long an acked exec entry is retained before collection.
    pub exec_ttl: Duration,
    /// Grace period between SIGTERM and SIGKILL when killing an exec's process
    /// group.
    pub kill_grace: Duration,
    /// Maximum members in an uploaded archive.
    pub max_tar_members: u64,
    /// Maximum total uncompressed bytes across an archive's members.
    pub max_tar_bytes: u64,
    /// Bytes of recent output each exec keeps for stream replay. Independent of
    /// `max_output_bytes`: that one caps the *head* of the log for the polled
    /// result, this one caps the *tail* for a reattaching stream. A reattach past
    /// this window gets an explicit gap event rather than silently-missing bytes.
    pub stream_buffer_bytes: usize,
    /// Slots in an exec's live fan-out channel. A subscriber that falls this far
    /// behind is told it lagged and re-reads the ring from its last good offset,
    /// so the bound costs a re-read rather than lost output.
    pub stream_channel_capacity: usize,
    /// Interval between SSE keep-alive comments. Needed because an exec can be
    /// silent for minutes — an agent harness thinking, a build linking — and an
    /// idle connection through a proxy is indistinguishable from a dead one.
    pub sse_keepalive: Duration,
    /// Largest single decoded stdin write accepted. Bounded for the same reason
    /// every other buffer here is: the write is held in memory before it reaches
    /// the pipe.
    pub max_stdin_write_bytes: usize,
    /// How long a stdin write may block on the pipe before the request gives up.
    /// A child that has stopped reading fills the 64 KiB pipe buffer and then the
    /// write blocks forever; without this bound one wedged child pins a request
    /// (and a connection) for the life of the VM.
    pub stdin_write_timeout: Duration,
    /// Bytes that must stay free on a write target's filesystem. A write that
    /// would cross this is refused with 507 naming the actual free space, rather
    /// than proceeding into an ENOSPC that surfaces as an indistinguishable 500.
    ///
    /// Zero disables the guard, for a deployment that would rather hit ENOSPC than
    /// be refused.
    pub disk_reserve_bytes: u64,
    /// Whether to replace image-derived identity (`/etc/machine-id`, hostname,
    /// `boot_id`, systemd's random seed) at startup.
    ///
    /// On by default because N VMs restored from one snapshot share every one of
    /// those values, and a key generated in one can repeat a key generated in
    /// another. Off is a supported choice, not a mistake: a fleet keyed by machine
    /// id, or a VM deliberately re-created from a snapshot, wants the identity to
    /// be stable.
    pub repair_identity: bool,
    /// Where workload handlers for the lifecycle hooks live: `<dir>/run`,
    /// `<dir>/suspend`, `<dir>/resume`, `<dir>/terminate`. A missing file means no
    /// handler. Fixed at `/etc/agentd/hooks.d` outside tests: the directory decides what
    /// the daemon executes as root, so it is image-owned and never read from the environment.
    pub hooks_dir: std::path::PathBuf,
    /// How long one handler may run before it is killed. Kept below the image's hook
    /// timeout (the client configures 30 s; the platform allows 1–60 s) so the daemon
    /// answers the platform first. `AGENTD_HOOK_HANDLER_TIMEOUT_SECS`, clamped to
    /// 1..=[`MAX_HOOK_HANDLER_SECS`].
    pub hook_handler_timeout: Duration,
    /// The passwd database a start request's user name is resolved against. Fixed at
    /// `/etc/passwd` outside tests and never read from the environment: it decides which
    /// uid a named user becomes, so it is the image's, like `hooks_dir`.
    pub passwd_path: std::path::PathBuf,
    /// The group database a start request's group name is resolved against. `/etc/group`
    /// outside tests, for `passwd_path`'s reason.
    pub group_path: std::path::PathBuf,
    /// The user a start request runs as when it names no `user`, from
    /// [`DEFAULT_USER_VAR`] (AGENTD-22). `None` keeps the daemon's own user, which is root.
    ///
    /// Kept as the image wrote it and resolved per request exactly as a request's `user`
    /// string is: a passwd name, or digits that name no row as a uid. Per request because a
    /// user an earlier exec created resolves on the next. A request's own `user` always wins,
    /// `root` and `0` included (AGENTD-23), so an authorized caller can still ask for root.
    ///
    /// Read from the image's environment like every other `AGENTD_*` setting, and never from
    /// a request or the launch environment: who a workload runs as when its caller doesn't
    /// say is the image author's decision. Workload hook handlers don't take it; they are
    /// image-owned code and run as the daemon's user.
    pub default_user: Option<String>,
}

/// The variable naming [`Config::default_user`]. A refusal of the default names it, so an
/// image author who misspelled the user can find where it was set.
pub const DEFAULT_USER_VAR: &str = "AGENTD_DEFAULT_USER";

/// The handler budget's ceiling: five seconds under the platform's 60 s hook limit.
pub const MAX_HOOK_HANDLER_SECS: u64 = 55;

impl Default for Config {
    fn default() -> Self {
        Self {
            port: 9000,
            max_body_bytes: 512 * 1024 * 1024,
            max_drain_bytes: 64 * 1024,
            max_output_bytes: 8 * 1024 * 1024,
            output_linger: Duration::from_secs(5),
            exec_ttl: Duration::from_secs(15 * 60),
            kill_grace: Duration::from_secs(10),
            max_tar_members: 100_000,
            max_tar_bytes: 8 * 1024 * 1024 * 1024,
            stream_buffer_bytes: 1024 * 1024,
            stream_channel_capacity: 256,
            sse_keepalive: Duration::from_secs(15),
            max_stdin_write_bytes: 1024 * 1024,
            stdin_write_timeout: Duration::from_secs(5),
            // 256 MiB. Chosen to be larger than the default `max_body_bytes` is
            // *not* possible — the body limit is 512 MiB — so the in-flight
            // re-check in `disk::copy_guarded` is what actually holds the line; the
            // reserve is the headroom that absorbs its 8 MiB probe granularity and
            // leaves room for the other writers in the VM to report their own
            // errors. anthropics/claude-code#59856 filled a 10 GB disk to 100% and
            // the first symptom was `useradd` failing, not the workload.
            disk_reserve_bytes: 256 * 1024 * 1024,
            repair_identity: true,
            hooks_dir: std::path::PathBuf::from("/etc/agentd/hooks.d"),
            hook_handler_timeout: Duration::from_secs(20),
            passwd_path: std::path::PathBuf::from("/etc/passwd"),
            group_path: std::path::PathBuf::from("/etc/group"),
            default_user: None,
        }
    }
}

impl Config {
    /// Reads overrides from the environment, keeping the defaults for anything
    /// unset or unparseable. An unparseable value is a warning rather than a
    /// startup failure: refusing to boot would strand the VM with no way in,
    /// since the control API is the only channel.
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        if let Some(port) = env_parse("AGENTD_PORT") {
            cfg.port = port;
        }
        if let Some(bytes) = env_parse("AGENTD_MAX_BODY_BYTES") {
            cfg.max_body_bytes = bytes;
        }
        if let Some(bytes) = env_parse("AGENTD_MAX_OUTPUT_BYTES") {
            cfg.max_output_bytes = bytes;
        }
        if let Some(secs) = env_parse::<u64>("AGENTD_OUTPUT_LINGER_SECS") {
            cfg.output_linger = Duration::from_secs(secs);
        }
        if let Some(secs) = env_parse::<u64>("AGENTD_EXEC_TTL_SECS") {
            cfg.exec_ttl = Duration::from_secs(secs);
        }
        if let Some(bytes) = env_parse("AGENTD_STREAM_BUFFER_BYTES") {
            cfg.stream_buffer_bytes = bytes;
        }
        if let Some(slots) = env_parse("AGENTD_STREAM_CHANNEL_CAPACITY") {
            cfg.stream_channel_capacity = slots;
        }
        if let Some(secs) = env_parse::<u64>("AGENTD_SSE_KEEPALIVE_SECS") {
            cfg.sse_keepalive = Duration::from_secs(secs);
        }
        if let Some(bytes) = env_parse("AGENTD_MAX_STDIN_WRITE_BYTES") {
            cfg.max_stdin_write_bytes = bytes;
        }
        if let Some(bytes) = env_parse("AGENTD_DISK_RESERVE_BYTES") {
            cfg.disk_reserve_bytes = bytes;
        }
        if let Some(flag) = env_flag("AGENTD_REPAIR_IDENTITY") {
            cfg.repair_identity = flag;
        }
        if let Some(secs) = env_parse::<u64>("AGENTD_HOOK_HANDLER_TIMEOUT_SECS") {
            cfg.hook_handler_timeout = Duration::from_secs(secs.clamp(1, MAX_HOOK_HANDLER_SECS));
        }
        cfg.default_user = default_user(std::env::var_os(DEFAULT_USER_VAR));
        // Logged to the image's log group because `/v1/health` can't carry it yet: `Health` is
        // constructible field by field, so a new field is a breaking change to the protocol
        // crate.
        if let Some(user) = &cfg.default_user {
            tracing::info!(
                default_user = %user,
                "a start request that omits user runs as {DEFAULT_USER_VAR}"
            );
        }
        cfg
    }
}

/// [`DEFAULT_USER_VAR`] as the daemon keeps it: set means set, an empty value included.
///
/// Unlike the numeric knobs, a value that names nobody is not ignored. It resolves the way a
/// request's `user` string does, so an empty or misspelled one refuses every start that
/// names no user, naming the variable, instead of quietly running the workload as root. That
/// costs no reachability: the daemon still boots, and a request that names its user still
/// runs. A value that isn't UTF-8 is kept lossily, which no passwd name matches, so it is
/// refused the same way rather than dropped.
fn default_user(raw: Option<std::ffi::OsString>) -> Option<String> {
    raw.map(|raw| raw.to_string_lossy().into_owned())
}

/// Parses a boolean knob, accepting the spellings a Dockerfile or a Terraform
/// template actually produces. `bool::from_str` takes only `true`/`false`, so
/// `AGENTD_REPAIR_IDENTITY=0` would otherwise be ignored with a warning — and a
/// deployment that believes it opted out but did not is worse than one that never
/// tried.
fn env_flag(key: &str) -> Option<bool> {
    let raw = std::env::var(key).ok()?;
    match raw.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => {
            tracing::warn!(key, raw, "ignoring unparseable configuration flag");
            None
        }
    }
}

fn env_parse<T: std::str::FromStr>(key: &str) -> Option<T> {
    let raw = std::env::var(key).ok()?;
    match raw.parse() {
        Ok(value) => Some(value),
        Err(_) => {
            tracing::warn!(key, raw, "ignoring unparseable configuration value");
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_sized_for_a_small_vm() {
        let cfg = Config::default();
        assert_eq!(cfg.port, 9000);
        // The output cap has to be well under the smallest baseline memory, or a
        // single noisy command can kill a daemon nothing can restart.
        assert!(cfg.max_output_bytes < 64 * 1024 * 1024);
        assert!(cfg.max_drain_bytes < cfg.max_body_bytes);
        assert_eq!(cfg.default_user, None, "unset keeps the daemon's own user");
    }

    /// **AGENTD-22.** The default user is kept as the image set it: unset is none, and any
    /// set value, empty or not UTF-8 included, is kept to be resolved, so a value that names
    /// nobody is refused at the first start instead of falling back to root.
    #[test]
    fn the_default_user_is_kept_as_set() {
        use std::os::unix::ffi::OsStringExt;
        assert_eq!(default_user(None), None);
        assert_eq!(default_user(Some("agent".into())), Some("agent".into()));
        assert_eq!(default_user(Some("1000".into())), Some("1000".into()));
        assert_eq!(default_user(Some("".into())), Some(String::new()));
        let lossy = default_user(Some(std::ffi::OsString::from_vec(vec![b'a', 0xff])))
            .expect("a non-UTF-8 value is kept");
        assert_eq!(lossy, "a\u{fffd}");
    }

    /// **AGENTD-22.** `from_env` reads the default user from the daemon's environment. The
    /// read happens in a copy of this test binary started with the variable set, because this
    /// crate can't set its own environment: `set_var` is `unsafe`, which the crate forbids.
    #[test]
    fn from_env_reads_the_default_user_from_the_environment() {
        const CHILD: &str = "AGENTD_TEST_FROM_ENV_CHILD";
        const NAME: &str = "config::tests::from_env_reads_the_default_user_from_the_environment";
        if std::env::var_os(CHILD).is_some() {
            assert_eq!(Config::from_env().default_user.as_deref(), Some("probe"));
            return;
        }
        let output = std::process::Command::new(std::env::current_exe().expect("test binary"))
            .args(["--exact", NAME])
            .env(CHILD, "1")
            .env(DEFAULT_USER_VAR, "probe")
            .output()
            .expect("child test");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(output.status.success(), "{stdout}");
        // A filter that matched nothing would pass too, having checked nothing.
        assert!(stdout.contains("1 passed"), "{stdout}");
    }
}
