// SPDX-License-Identifier: Apache-2.0
//! The four renderers over one result type: JSON, dense, plain, and (in [`crate::tui`]) a
//! ratatui frame.
//!
//! # Rendering is separate from command logic, and the reason is arithmetic honesty
//!
//! The cost surface is the part where a renderer can lie. A cost report's JSON is core's
//! (`CostReport::to_json` in `crates/microvms-domain/src/cost.rs`, #255), where the rules that
//! keep it honest are written down: an unpriced line has no `usd` key rather than a null a
//! permissive consumer sums as zero, every dollar is a string and every seconds figure a number.
//! The CLI, Python and TypeScript all emit that one shape. What stays here is the dense and plain
//! renderings, which have rules of their own below: an unpriced line reads `unpriced` in the
//! amount column, never a blank or a zero.
//!
//! (cli.py line numbers resolve at `git show 'c4d396e^:clients/python/src/microvms_agentd/cli.py'` — the retired oracle.)

use microvms_core::cost::{Amount, CostReport};
use serde_json::{Map, Value, json};

/// The dense rendering of a cost report: `phase\tunit\tamount`, one line per item.
///
/// Three fields and no total row, which is `cli.py:2203-2207` exactly. Both divergences the
/// shape used to carry were the plausible kind. The extra `quantity` field put the amount in
/// field four, so `cut -f3` — the column the module used to name — read the *unit* out of one
/// client and the amount out of the other. And the appended `total` row is a line whose first
/// field is not a phase, so `awk` over the phase column of a dense report sees a phase called
/// `total`; the total is in the JSON envelope and in the plain rendering, which is where a
/// consumer that wants it should read it, rather than in the stream a script is summing.
///
/// An unpriced line reads the literal `unpriced` in the amount column rather than a blank or
/// a zero, so `cut -f3 | paste -sd+ | bc` on this output fails loudly instead of producing a
/// total that flatters us.
pub fn report_dense(report: &CostReport) -> String {
    report
        .items()
        .iter()
        .map(|item| {
            let amount = match &item.amount {
                Amount::Estimated(usd) => usd.amount().to_string(),
                Amount::Unpriced { .. } => "unpriced".to_string(),
            };
            format!("{}\t{}\t{}", item.phase.as_str(), item.unit, amount)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// ── run ─────────────────────────────────────────────────────────────────────

/// Everything `run` learned, so the handler only formats.
///
/// A struct rather than a tuple because the fields are read by name in four renderers, and a
/// positional shape is how the fourth one prints the exit code where the duration belongs.
#[derive(Clone, Debug, Default)]
pub struct RunOutcome {
    pub image_identifier: Option<String>,
    pub image_name: Option<String>,
    pub microvm_id: Option<String>,
    pub endpoint: Option<String>,
    pub agent_token: Option<String>,
    pub exec_exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub truncated: bool,
    pub build_seconds: f64,
    pub running_seconds: f64,
    pub kept: bool,
    /// `run --no-wait` returned with the VM still PENDING (#269), so the human view says how
    /// to finish the launch. Not an envelope key: the caller who passed the flag knows.
    pub pending: bool,
    /// The local name `--vm-name` registered, or `None` — present in the envelope either
    /// way, so a consumer never guards against a missing key.
    pub vm_name: Option<String>,
    pub leaked: Vec<String>,
    pub cost: Option<Value>,
    /// `run <DIR>`'s sync report — workdir, uploaded size, and the artifacts brought
    /// back — or `Null` for a plain run. Always present, so a consumer never guards
    /// against a missing key.
    pub sync: Option<Value>,
    /// The launching host's identity secret, base64, when `--identity` generated one.
    ///
    /// In the payload for the agent token's reason: `run --keep --identity` without
    /// `--vm-name` has no ledger record, so this envelope is the only place the caller can
    /// collect what `tunnel --verify-identity` needs. Never in a progress line, never in a
    /// `Debug`.
    pub identity_host_seed: Option<String>,
    /// The VM's public key, base64 — the pin. Public by construction.
    pub identity_vm_public_key: Option<String>,
    /// What this run's outbound network **is**: `open`, `unsealed`, `best-effort`, or
    /// `sealed`.
    ///
    /// Not the same fact as `resolvedConfig.egress`, which is what was *asked for*. A
    /// connector-less launch reports `unsealed`, because the platform gives such a VM
    /// outbound network (measured three dates, `docs/PLATFORM.md`) — a caller who read
    /// `egress: false` as a seal downloaded a 242 MB DuckDB extension from a VM they
    /// believed had no network. Defaults to `unsealed` rather than to a claim.
    pub egress_posture: microvms_core::control::EgressPosture,
    /// Whether the build arm found the content-addressed image already built, and so built
    /// nothing (#258). A reused image isn't this run's: the teardown leaves it and the ledger
    /// doesn't list it.
    pub image_reused: bool,
    /// The `s3://` URI of the artifact the image was built from: the caller's `--artifact-uri`,
    /// or the content-addressed key the build arm's ensure uploaded to or found (#258). `None`
    /// for `run --image`, which builds nothing. A consumer reads the object from here rather
    /// than deriving the key, which the ensure owns.
    pub artifact_uri: Option<String>,
}

impl RunOutcome {
    /// The success envelope's `data`.
    pub fn to_data(&self) -> Map<String, Value> {
        let mut data = Map::new();
        data.insert("imageIdentifier".into(), json!(self.image_identifier));
        data.insert("imageName".into(), json!(self.image_name));
        data.insert("microvmId".into(), json!(self.microvm_id));
        data.insert("endpoint".into(), json!(self.endpoint));
        // The agent token is in the payload for `--keep` deliberately: `run --keep` is
        // followed by `microvm exec --agent-token`, and a caller who cannot read it cannot
        // use the VM they are now paying for. It is never in a progress line, never in a
        // `Debug`, and core keeps it out of both too.
        //
        // Without `--keep` the key stays and the value is null (issue #161). The VM this
        // command just tore down has no consumer for its token, and stdout is captured by
        // logs and transcripts that outlive the process — a run that failed between
        // printing and terminating would leave a live credential in one. The key itself is
        // kept so a consumer never guards against a missing key, the envelope's rule.
        data.insert(
            "agentToken".into(),
            if self.kept {
                json!(self.agent_token)
            } else {
                Value::Null
            },
        );
        data.insert("execExitCode".into(), json!(self.exec_exit_code));
        data.insert("stdout".into(), json!(self.stdout));
        data.insert("stderr".into(), json!(self.stderr));
        data.insert("truncated".into(), json!(self.truncated));
        data.insert("buildSeconds".into(), json!(self.build_seconds));
        data.insert("runningSeconds".into(), json!(self.running_seconds));
        data.insert("kept".into(), json!(self.kept));
        data.insert("vmName".into(), json!(self.vm_name));
        data.insert("leaked".into(), json!(self.leaked));
        data.insert("cost".into(), self.cost.clone().unwrap_or(Value::Null));
        data.insert("sync".into(), self.sync.clone().unwrap_or(Value::Null));
        // Same reasoning as the agent token above: without `--vm-name` this envelope is the
        // only place the identity material exists once the process exits. Always present as
        // keys (null when `--identity` was not asked), so a consumer never guards.
        data.insert("identityHostSeed".into(), json!(self.identity_host_seed));
        data.insert(
            "identityVmPublicKey".into(),
            json!(self.identity_vm_public_key),
        );
        // Always present and never null: a consumer branching on outbound reach must not
        // have to infer it from `resolvedConfig.egress`, which answers a different question
        // (what was requested) and reads as a seal when it is not one.
        data.insert("egressPosture".into(), json!(self.egress_posture.as_str()));
        data.insert("imageReused".into(), json!(self.image_reused));
        data.insert("artifactUri".into(), json!(self.artifact_uri));
        data
    }

    /// The human view. Output first, because output is what the caller asked for.
    pub fn render(&self, dense: bool) -> String {
        if dense {
            // TSV with the exit code first, so a shell reads field one without parsing.
            return [
                format!(
                    "exit\t{}",
                    self.exec_exit_code
                        .map(|code| code.to_string())
                        .unwrap_or_default()
                ),
                format!("microvm\t{}", self.microvm_id.clone().unwrap_or_default()),
                format!(
                    "image\t{}",
                    self.image_identifier.clone().unwrap_or_default()
                ),
                format!("running_sec\t{:.1}", self.running_seconds),
                format!("leaked\t{}", self.leaked.join(",")),
                // Appended rather than inserted: a shell reading field one of line one is
                // the dense contract, and every existing line keeps its position.
                format!("egress\t{}", self.egress_posture.as_str()),
            ]
            .join("\n");
        }
        let mut lines: Vec<String> = Vec::new();
        if !self.stdout.is_empty() {
            lines.push(self.stdout.trim_end_matches('\n').to_string());
        }
        if !self.stderr.is_empty() {
            lines.push(self.stderr.trim_end_matches('\n').to_string());
        }
        if let Some(code) = self.exec_exit_code {
            lines.push(format!("exit code: {code}"));
        }
        if self.truncated {
            lines.push("note: output was truncated at the daemon's cap".to_string());
        }
        if self.kept {
            lines.push(format!(
                "kept: microvm {}, image {}",
                self.microvm_id.clone().unwrap_or_default(),
                self.image_identifier.clone().unwrap_or_default(),
            ));
            if let (Some(id), Some(endpoint), Some(token)) =
                (&self.microvm_id, &self.endpoint, &self.agent_token)
            {
                if self.pending {
                    lines.push(match &self.vm_name {
                        Some(name) => format!("  finish the launch: microvm wait --name {name}"),
                        None => format!(
                            "  finish the launch: microvm wait --endpoint {endpoint} \
                             --agent-token {token} --microvm-id {id}"
                        ),
                    });
                }
                lines.push(format!(
                    "  exec against it: microvm exec '<cmd>' --endpoint {endpoint} \
                     --agent-token {token} --microvm-id {id}"
                ));
                lines.push(format!("  release it: microvm terminate {id}"));
            }
        }
        for identifier in &self.leaked {
            lines.push(format!("LEAKED (still billing): {identifier}"));
        }
        if let Some(cost) = &self.cost
            && let Some(render) = cost["total"]["render"].as_str()
        {
            lines.push(format!("cost: {render}"));
        }
        // Unconditional, and that is the point: the run that read as sealed printed nothing
        // about its network at all. The line names the mechanism, not a verdict, because
        // "egress: false" as a verdict is the defect (docs/TRUST.md, **Egress**).
        lines.push(egress_line(self.egress_posture));
        lines.join("\n")
    }
}

/// The human line for an egress posture, which `run` and `egress-posture` both print: the
/// label and what it means, never a bare verdict.
pub fn egress_line(posture: microvms_core::control::EgressPosture) -> String {
    format!("egress: {} — {}", posture.as_str(), posture.describe())
}

// ── doctor ──────────────────────────────────────────────────────────────────

// One prerequisite, its verdict, and what to do about it: core's preflight line, so `doctor`
// and the bindings' `preflight` render the same checks the same way.
//
// `ok: false` with `fatal: false` is a warning — a region we have not seen listed, a
// Terraform stack that may live elsewhere. The distinction matters because the exit code is
// derived from the fatal ones only, and a CLI that failed `doctor` over an advisory would
// train people to ignore it.
pub use microvms_core::preflight::{Check, healthy};

/// One check as the `doctor` envelope carries it.
pub fn check_json(check: &Check) -> Value {
    json!({
        "name": check.name,
        "ok": check.ok,
        "detail": check.detail,
        "fatal": check.fatal,
        "remedy": check.remedy,
    })
}

/// The human rendering of a doctor run.
pub fn render_doctor(checks: &[Check], dense: bool) -> String {
    if dense {
        return checks
            .iter()
            .map(|check| {
                format!(
                    "{}\t{}\t{}",
                    check.name,
                    if check.ok { "ok" } else { "fail" },
                    check.detail
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
    }
    let mut lines: Vec<String> = Vec::new();
    for check in checks {
        // Three marks rather than two: an advisory rendered as FAIL is how a caller learns to
        // ignore the whole command.
        let mark = if check.ok {
            "PASS"
        } else if check.fatal {
            "FAIL"
        } else {
            "WARN"
        };
        lines.push(format!("{mark}  {}: {}", check.name, check.detail));
        if !check.ok && !check.remedy.is_empty() {
            lines.push(format!("      -> {}", check.remedy));
        }
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_core::SizeClass;
    use microvms_core::cost::{CalendarDate, DurationP, RunUsage, pinned_rates, run_report};

    /// The pinned rate table's own retrieval date, so no test is stale-dependent.
    fn fresh_day() -> CalendarDate {
        pinned_rates().retrieved()
    }

    fn a_report() -> CostReport {
        run_report(
            SizeClass::Mib2048,
            &RunUsage {
                running: Some(DurationP::Measured(std::time::Duration::from_secs(3600))),
                image_gb: Some(2.0),
                image_build: Some(DurationP::Measured(std::time::Duration::from_secs(600))),
                ..RunUsage::launched()
            },
            &pinned_rates(),
            fresh_day(),
            "run img",
        )
        .expect("a report")
    }

    /// The dense cost rendering is the oracle's three fields, with no total row.
    ///
    /// The Python oracle's `cli.py:2203-2207` emitted `phase\tunit\tamount` and stopped. Both
    /// divergences this pins are silent under a type check and loud under a pipe: a fourth
    /// `quantity` field moved the amount to field four, so the same `cut -f3` read the *unit*
    /// from one client and the amount from the other, and the appended `total` row put a line
    /// in the stream whose first field is not a phase — `awk '$1=="running"'` is fine, `wc -l`
    /// and any per-phase aggregation are not.
    ///
    /// The field names are checked positionally against what the oracle printed for
    /// `cost --dense --running-sec=3600 --build-sec=300 --image-gb=2`:
    ///
    /// ```text
    /// image-build\tseconds\tunpriced
    /// image-storage\tGB-months\t0.0373...
    /// ...
    /// ```
    ///
    /// A transcript rather than a command, for the reason the seconds-vs-dollars test above
    /// gives: that client is git history now.
    ///
    /// **Falsification** — add the quantity field back, or push the total row back on, and this
    /// is red on the field count or the line count respectively. Verified for both.
    #[test]
    fn the_dense_cost_rendering_is_three_fields_and_no_total() {
        let dense = report_dense(&a_report());
        let rows: Vec<Vec<&str>> = dense
            .lines()
            .map(|line| line.split('\t').collect())
            .collect();
        assert!(!rows.is_empty(), "{dense}");
        for row in &rows {
            assert_eq!(
                row.len(),
                3,
                "phase, unit, amount and nothing else: {row:?}"
            );
        }
        // No total row: the last line is a phase like every other, so the line count is the
        // item count.
        assert_eq!(rows.len(), a_report().items().len(), "{dense}");
        assert!(
            !dense.contains("lower-bound") && !dense.contains("\nexact"),
            "the total belongs to the JSON envelope and the plain render, not this stream: \
             {dense}"
        );

        // Field two is the unit and field three is the amount, in the oracle's positions.
        let build = rows
            .iter()
            .find(|row| row[0] == "image-build")
            .expect("the build line");
        assert_eq!(build[1], "seconds");
        // And the unpriced line writes the word rather than a number, so
        // `cut -f3 | paste -sd+ | bc` fails loudly instead of totalling to something that
        // flatters us.
        assert_eq!(build[2], "unpriced");

        let storage = rows
            .iter()
            .find(|row| row[0] == "image-storage")
            .expect("the storage line");
        assert_eq!(storage[1], "GB-months");
        assert!(
            storage[2].parse::<f64>().is_ok(),
            "a priced line's field three is the figure: {storage:?}"
        );
    }

    /// A run outcome's dense rendering puts the exit code in field one.
    #[test]
    fn the_dense_run_rendering_leads_with_the_exit_code() {
        let outcome = RunOutcome {
            microvm_id: Some("mvm-1".into()),
            image_identifier: Some("arn:image".into()),
            exec_exit_code: Some(7),
            running_seconds: 12.34,
            leaked: vec!["arn:image".into()],
            ..RunOutcome::default()
        };
        let dense = outcome.render(true);
        assert!(dense.starts_with("exit\t7"), "{dense}");
        assert!(dense.contains("leaked\tarn:image"), "{dense}");
        assert!(dense.contains("egress\tunsealed"), "{dense}");
        // Field one is readable with `cut -f2`, which is the whole point.
        let first = dense.lines().next().expect("a line");
        assert_eq!(first.split('\t').nth(1), Some("7"));
    }

    /// A leaked identifier is loud in the human rendering.
    #[test]
    fn a_leak_is_named_in_the_human_rendering() {
        let outcome = RunOutcome {
            leaked: vec!["mvm-1".into()],
            ..RunOutcome::default()
        };
        assert!(
            outcome
                .render(false)
                .contains("LEAKED (still billing): mvm-1"),
            "{}",
            outcome.render(false)
        );
    }

    /// `--keep` prints the three identifiers plus the command that uses them and the one
    /// that releases them.
    ///
    /// The caller has just taken responsibility for a billing resource, so the remedy has to
    /// be copy-pasteable rather than described.
    #[test]
    fn keep_prints_the_exec_and_terminate_commands() {
        let outcome = RunOutcome {
            microvm_id: Some("mvm-1".into()),
            image_identifier: Some("arn:image".into()),
            endpoint: Some("https://mvm-1.example".into()),
            agent_token: Some("deadbeef".into()),
            kept: true,
            ..RunOutcome::default()
        };
        let rendered = outcome.render(false);
        assert!(rendered.contains("kept: microvm mvm-1"), "{rendered}");
        assert!(rendered.contains("microvm exec"), "{rendered}");
        assert!(rendered.contains("--agent-token deadbeef"), "{rendered}");
        assert!(rendered.contains("microvm terminate mvm-1"), "{rendered}");
    }

    /// **Issue #161.** A run that tears its VM down prints no token: the key stays (a consumer
    /// never guards against a missing key) and its value is null, because the credential
    /// would name a VM that no longer exists by the time anyone reads the log it landed in.
    /// `--keep` is the one case with a consumer for it, so that path still carries it.
    #[test]
    fn a_run_that_was_not_kept_nulls_the_agent_token_in_its_envelope() {
        let minted = RunOutcome {
            agent_token: Some("deadbeef".into()),
            kept: false,
            ..RunOutcome::default()
        };
        let data = minted.to_data();
        assert!(data.contains_key("agentToken"), "the key is always present");
        assert_eq!(
            data["agentToken"],
            Value::Null,
            "a torn-down VM's token must not reach stdout: {data:?}"
        );

        let kept = RunOutcome {
            kept: true,
            ..minted
        };
        assert_eq!(
            kept.to_data()["agentToken"],
            "deadbeef",
            "--keep is the path with a consumer for the token"
        );
    }

    /// **Every run report states its egress posture, including the one with nothing else to
    /// say.**
    ///
    /// This test used to assert `done` for an outcome with no output, no leak and no cost.
    /// The posture line replaces it: a run whose report said nothing about its network is
    /// how `--egress`-off came to be read as a seal, and the default posture is `unsealed`
    /// rather than a claim.
    ///
    /// **Falsification** — 2026-09-13. Make the posture line conditional on `kept` and this
    /// goes red; drop it and the `unsealed` assertion goes red.
    #[test]
    fn a_launch_with_no_exec_still_states_its_egress_posture() {
        let rendered = RunOutcome::default().render(false);
        assert_eq!(
            rendered,
            format!(
                "egress: unsealed — {}",
                microvms_core::control::EgressPosture::Unsealed.describe()
            )
        );
        assert!(!rendered.contains(": sealed"), "{rendered}");
    }

    /// An advisory renders WARN and does not decide the exit code.
    ///
    /// Both halves: a CLI that failed `doctor` over an unlisted region would block a caller
    /// who is right and we are stale, and one that rendered the advisory as FAIL would teach
    /// people to ignore the command.
    #[test]
    fn an_advisory_check_renders_warn_and_leaves_the_run_healthy() {
        let checks = vec![
            Check::pass("credentials", "account 123456789012"),
            Check::fail("region", "not in the list", "known: us-east-1, ...").advisory(),
        ];
        assert!(healthy(&checks), "an advisory must not fail the run");
        let rendered = render_doctor(&checks, false);
        assert!(rendered.contains("WARN  region"), "{rendered}");
        assert!(!rendered.contains("FAIL"), "{rendered}");
        assert!(rendered.contains("-> known: us-east-1"), "{rendered}");

        // And a fatal one does fail it, so the branch is not vacuously true.
        let fatal = vec![Check::fail("daemon-binary", "not aarch64", "rebuild")];
        assert!(!healthy(&fatal));
        assert!(render_doctor(&fatal, false).contains("FAIL  daemon-binary"));
    }
}
