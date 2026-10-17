// SPDX-License-Identifier: Apache-2.0
//! Prefix-scoped cleanup. Discovery finishes before mutation so deletion cannot move a
//! pagination cursor past another resource. Keep entries protect both an image and its VMs.

use std::collections::{BTreeMap, HashSet};
use std::time::Duration;

use serde::Serialize;

use super::transport::{Call, paths, send_with_retry};
use super::{ControlPlane, ops};
use crate::error::{Error, ErrorKind};
use crate::sandbox::{DEFAULT_LIFECYCLE_TIMEOUT, LIFECYCLE_POLL_INTERVAL};

/// Options for an explicitly scoped sweep. The default only discovers resources.
#[derive(Clone, Debug)]
pub struct SweepOptions {
    /// Exact image names or ARNs whose images and VMs must survive.
    pub keep: Vec<String>,
    /// Discover and report candidates without terminating VMs or deleting images.
    pub dry_run: bool,
}

impl Default for SweepOptions {
    fn default() -> Self {
        Self {
            keep: Vec::new(),
            dry_run: true,
        }
    }
}

/// A failed resource operation. Other candidates are still processed.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SweepFailure {
    pub resource: String,
    pub message: String,
}

/// An inventory and confirmed cleanup outcomes. Dry runs populate only the inventory.
#[derive(Clone, Debug, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SweepReport {
    pub dry_run: bool,
    /// ARNs whose image name starts with the requested prefix, including kept images.
    pub images_matched: Vec<String>,
    pub images_kept: Vec<String>,
    /// Live VM IDs belonging to matched images outside the keep list.
    pub microvms_matched: Vec<String>,
    /// VM IDs observed in TERMINATED after the termination request.
    pub microvms_terminated: Vec<String>,
    /// Image ARNs observed absent or DELETED after a delete request.
    pub images_deleted: Vec<String>,
    /// Image ARNs still held by a live VM, or rejected by the service on deletion.
    pub images_in_use: Vec<String>,
    pub failures: Vec<SweepFailure>,
}

// The service caps all of these listings at 50. Spacing discovery, polling and version
// pages avoids a sweep producing a burst of ListMicrovms calls against a busy account.
const PAGE_SIZE: &str = "maxResults=50";
const LIST_SPACING: Duration = Duration::from_secs(1);

impl ControlPlane {
    pub(super) async fn sweep_page<T: serde::de::DeserializeOwned>(
        &self,
        operation: &'static str,
        path: String,
        last: &mut Option<Duration>,
    ) -> Result<T, Error> {
        if let Some(previous) = *last {
            let elapsed = self.clock().elapsed().saturating_sub(previous);
            self.clock()
                .sleep(LIST_SPACING.saturating_sub(elapsed))
                .await;
        }
        let separator = if path.contains('?') { '&' } else { '?' };
        let call = Call::get(operation, format!("{path}{separator}{PAGE_SIZE}"));
        *last = Some(self.clock().elapsed());
        send_with_retry(self.transport(), call)
            .await?
            .json(operation)
    }

    async fn sweep_images(
        &self,
        prefix: &str,
        last: &mut Option<Duration>,
    ) -> Result<Vec<ops::MicrovmImageSummaryWire>, Error> {
        let mut items = BTreeMap::new();
        let mut token = None;
        let mut seen = HashSet::new();
        loop {
            let page: ops::ListImagesResponseWire = self
                .sweep_page(
                    "ListMicrovmImages",
                    paths::microvm_images_list(Some(prefix), token.as_deref()),
                    last,
                )
                .await?;
            for image in page.items {
                // nameFilter is substring matching. This check is the destructive scope boundary.
                if image.name.starts_with(prefix) && image.state != "DELETED" {
                    items.insert(image.image_arn.clone(), image);
                }
            }
            match page.next_token {
                Some(next) if seen.insert(next.clone()) => token = Some(next),
                Some(_) => {
                    return Err(Error::new(
                        ErrorKind::Platform,
                        "image listing repeated its pagination token",
                    ));
                }
                None => return Ok(items.into_values().collect()),
            }
        }
    }

    async fn sweep_vms(
        &self,
        last: &mut Option<Duration>,
    ) -> Result<Vec<ops::MicrovmSummaryWire>, Error> {
        let mut items = BTreeMap::new();
        let mut token = None;
        let mut seen = HashSet::new();
        loop {
            let page: ops::ListMicrovmSummariesResponseWire = self
                .sweep_page("ListMicrovms", paths::microvms_list(token.as_deref()), last)
                .await?;
            for vm in page.items {
                items.insert(vm.microvm_id.clone(), vm);
            }
            match page.next_token {
                Some(next) if seen.insert(next.clone()) => token = Some(next),
                Some(_) => {
                    return Err(Error::new(
                        ErrorKind::Platform,
                        "VM listing repeated its pagination token",
                    ));
                }
                None => return Ok(items.into_values().collect()),
            }
        }
    }

    /// Discover or clean up images whose names start with `prefix`, and their live VMs.
    ///
    /// A nonempty image-name prefix is required. Keep entries are exact names or ARNs;
    /// their VMs are protected too. Cleanup waits for termination before deletion, then
    /// confirms image disappearance. Concurrent launches may keep an image in use; they
    /// are reported rather than repeatedly terminated. This is not an account-wide lock.
    pub async fn sweep(&self, prefix: &str, options: &SweepOptions) -> Result<SweepReport, Error> {
        super::require_valid_image_name(prefix)?;
        let mut last = None;
        let images = self.sweep_images(prefix, &mut last).await?;
        let mut report = SweepReport {
            dry_run: options.dry_run,
            ..SweepReport::default()
        };
        let mut candidates = HashSet::new();
        for image in &images {
            report.images_matched.push(image.image_arn.clone());
            if options.keep.contains(&image.name) || options.keep.contains(&image.image_arn) {
                report.images_kept.push(image.image_arn.clone());
            } else {
                candidates.insert(image.image_arn.clone());
            }
        }
        let vms = self.sweep_vms(&mut last).await?;
        report.microvms_matched = vms
            .iter()
            .filter(|vm| candidates.contains(&vm.image_arn) && vm.state != "TERMINATED")
            .map(|vm| vm.microvm_id.clone())
            .collect();
        if options.dry_run {
            return Ok(report);
        }
        for id in &report.microvms_matched {
            if let Err(error) = self.terminate(id).await {
                report.failures.push(SweepFailure {
                    resource: id.clone(),
                    message: error.to_string(),
                });
            }
        }
        let started = self.clock().elapsed();
        let mut live;
        loop {
            live = match self.sweep_vms(&mut last).await {
                Ok(live) => live,
                Err(error) => {
                    report.failures.push(SweepFailure {
                        resource: "ListMicrovms".into(),
                        message: error.to_string(),
                    });
                    // Discovery has become uncertain after mutation. Preserve the report and
                    // do not delete an image without observing its selected VMs terminate.
                    report.images_in_use = candidates.into_iter().collect();
                    report.images_in_use.sort();
                    return Ok(report);
                }
            };
            // Confirmed terminations may disappear from later lists. Retain that evidence,
            // but never count an absent VM whose termination was not observed.
            report.microvms_terminated = report
                .microvms_matched
                .iter()
                .filter(|id| {
                    report.microvms_terminated.contains(id)
                        || live
                            .iter()
                            .any(|vm| &vm.microvm_id == *id && vm.state == "TERMINATED")
                })
                .cloned()
                .collect();
            let pending = report.microvms_terminated.len() != report.microvms_matched.len();
            if !pending
                || self.clock().elapsed().saturating_sub(started) >= DEFAULT_LIFECYCLE_TIMEOUT
            {
                break;
            }
            self.clock().sleep(LIFECYCLE_POLL_INTERVAL).await;
        }
        let mut deleting = HashSet::new();
        for image in images
            .iter()
            .filter(|image| candidates.contains(&image.image_arn))
        {
            let unconfirmed = vms.iter().any(|vm| {
                vm.image_arn == image.image_arn
                    && report.microvms_matched.contains(&vm.microvm_id)
                    && !report.microvms_terminated.contains(&vm.microvm_id)
            });
            let held = unconfirmed
                || live
                    .iter()
                    .any(|vm| vm.image_arn == image.image_arn && vm.state != "TERMINATED");
            if held {
                report.images_in_use.push(image.image_arn.clone());
            } else if let Err(error) = self
                .try_delete_image_paced(&image.image_arn, Some(&mut last))
                .await
            {
                report.images_in_use.push(image.image_arn.clone());
                report.failures.push(SweepFailure {
                    resource: image.image_arn.clone(),
                    message: error.to_string(),
                });
            } else {
                deleting.insert(image.image_arn.clone());
            }
        }
        let started = self.clock().elapsed();
        while !deleting.is_empty() {
            let remaining = match self.sweep_images(prefix, &mut last).await {
                Ok(remaining) => remaining,
                Err(error) => {
                    report.failures.push(SweepFailure {
                        resource: "ListMicrovmImages".into(),
                        message: error.to_string(),
                    });
                    return Ok(report);
                }
            };
            let absent: Vec<_> = deleting
                .iter()
                .filter(|arn| !remaining.iter().any(|image| &image.image_arn == *arn))
                .cloned()
                .collect();
            for arn in absent {
                deleting.remove(&arn);
                report.images_deleted.push(arn);
            }
            if deleting.is_empty() {
                break;
            }
            if self.clock().elapsed().saturating_sub(started) >= DEFAULT_LIFECYCLE_TIMEOUT {
                for arn in deleting {
                    report.failures.push(SweepFailure {
                        resource: arn,
                        message: "image deletion was not confirmed before the cleanup deadline"
                            .into(),
                    });
                }
                break;
            }
            self.clock().sleep(LIFECYCLE_POLL_INTERVAL).await;
        }
        report.images_deleted.sort();
        Ok(report)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::region::Region;
    use crate::testing::{Answer, FakeControlPlane, TestClock, control_plane, list_versions_page};
    use std::sync::Arc;

    fn fixture() -> (Arc<FakeControlPlane>, Arc<TestClock>, ControlPlane) {
        let fake = Arc::new(FakeControlPlane::new());
        let clock = Arc::new(TestClock::new());
        let plane = control_plane(fake.clone(), Region::UsEast1, clock.clone());
        (fake, clock, plane)
    }

    const IMAGES: &str = r#"{"items":[{"imageArn":"arn:old","name":"job-old","state":"CREATED"},{"imageArn":"arn:keep","name":"job-keep","state":"CREATED"},{"imageArn":"arn:foreign","name":"other-job-old","state":"CREATED"}]}"#;
    const VMS: &str = r#"{"items":[{"microvmId":"mvm-old","imageArn":"arn:old","imageVersion":"1","state":"RUNNING","startedAt":1234.5},{"microvmId":"mvm-keep","imageArn":"arn:keep","imageVersion":"1","state":"RUNNING"},{"microvmId":"mvm-foreign","imageArn":"arn:foreign","imageVersion":"1","state":"RUNNING"},{"microvmId":"mvm-done","imageArn":"arn:old","imageVersion":"1","state":"TERMINATED"}]}"#;
    const EMPTY: &str = r#"{"items":[]}"#;

    fn polls_through_deadline(fake: &FakeControlPlane, body: &str) {
        for _ in 0..=DEFAULT_LIFECYCLE_TIMEOUT.as_secs() / LIFECYCLE_POLL_INTERVAL.as_secs() {
            fake.answer("ListMicrovms", Answer::ok(body));
        }
        // A broken loop condition must fail the report assertions, not repeat a ready
        // future forever and prevent the test runner from observing the failure.
        fake.answer(
            "ListMicrovms",
            Answer::failure(403, "polling exceeded the deadline"),
        );
    }

    #[tokio::test]
    async fn dry_run_protects_keep_names_and_arns_and_filters_substrings() {
        for keep in ["job-keep", "arn:keep"] {
            let (fake, clock, plane) = fixture();
            fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
            fake.answer("ListMicrovms", Answer::ok(VMS));
            let report = plane
                .sweep(
                    "job-",
                    &SweepOptions {
                        keep: vec![keep.into()],
                        ..SweepOptions::default()
                    },
                )
                .await
                .unwrap();
            assert!(report.dry_run);
            assert_eq!(report.images_matched, ["arn:keep", "arn:old"]);
            assert_eq!(report.images_kept, ["arn:keep"]);
            assert_eq!(report.microvms_matched, ["mvm-old"]);
            assert!(report.microvms_terminated.is_empty() && report.images_deleted.is_empty());
            assert!(
                fake.calls()
                    .iter()
                    .all(|call| call.method == super::super::transport::Method::Get)
            );
            assert!(
                fake.calls()
                    .iter()
                    .all(|call| call.path.contains("maxResults=50"))
            );
            assert!(fake.calls()[0].path.contains("nameFilter=job-"));
            assert_eq!(clock.now(), Duration::from_secs(1));
        }
    }

    #[tokio::test]
    async fn sweep_reads_every_page_and_preserves_started_at() {
        let (fake, clock, plane) = fixture();
        fake.answer(
            "ListMicrovmImages",
            Answer::ok(r#"{"items":[],"nextToken":"i+=/"}"#),
        );
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer(
            "ListMicrovms",
            Answer::ok(r#"{"items":[],"nextToken":"v+=/"}"#),
        );
        fake.answer("ListMicrovms", Answer::ok(VMS));
        let report = plane.sweep("job-", &SweepOptions::default()).await.unwrap();
        assert_eq!(report.microvms_matched, ["mvm-keep", "mvm-old"]);
        assert_eq!(clock.now(), Duration::from_secs(3));
        assert!(fake.calls()[1].path.contains("nextToken=i%2B%3D%2F"));
        assert!(fake.calls()[3].path.contains("nextToken=v%2B%3D%2F"));
        let items = plane
            .list_microvm_summaries(&Default::default())
            .await
            .unwrap();
        assert_eq!(items[0].started_at, Some(1234.5));
        assert_eq!(items[1].started_at, None);
    }

    #[tokio::test]
    async fn cleanup_confirms_termination_before_deletion_and_image_absence() {
        let (fake, _, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer(
            "ListMicrovmImages",
            Answer::ok(
                r#"{"items":[{"imageArn":"arn:keep","name":"job-keep","state":"CREATED"}]}"#,
            ),
        );
        fake.answer(
            "ListMicrovmImages",
            Answer::failure(403, "deletion was already confirmed"),
        );
        fake.answer("ListMicrovms", Answer::ok(VMS));
        fake.answer("ListMicrovms", Answer::ok(r#"{"items":[{"microvmId":"mvm-old","imageArn":"arn:old","imageVersion":"1","state":"TERMINATING"}]}"#));
        fake.answer("ListMicrovms", Answer::ok(r#"{"items":[{"microvmId":"mvm-old","imageArn":"arn:old","imageVersion":"1","state":"TERMINATED"},{"microvmId":"mvm-foreign","imageArn":"arn:foreign","imageVersion":"1","state":"RUNNING"}]}"#));
        fake.answer(
            "ListMicrovms",
            Answer::failure(403, "termination was already confirmed"),
        );
        fake.answer("TerminateMicrovm", Answer::ok("{}"));
        fake.answer(
            "ListMicrovmImageVersions",
            Answer::ok(list_versions_page(&["1"], Some("versions-next"))),
        );
        fake.answer(
            "ListMicrovmImageVersions",
            Answer::ok(list_versions_page(&["2"], None)),
        );
        fake.answer("DeleteMicrovmImageVersion", Answer::ok("{}"));
        fake.answer(
            "DeleteMicrovmImage",
            Answer::ok(r#"{"imageIdentifier":"arn:old","state":"DELETING"}"#),
        );
        let report = plane
            .sweep(
                "job-",
                &SweepOptions {
                    keep: vec!["job-keep".into()],
                    dry_run: false,
                },
            )
            .await
            .unwrap();
        assert_eq!(report.microvms_terminated, ["mvm-old"]);
        assert_eq!(report.images_deleted, ["arn:old"]);
        assert!(report.images_in_use.is_empty() && report.failures.is_empty());
        assert_eq!(fake.call_count("ListMicrovmImages"), 3);
        assert_eq!(fake.call_count("ListMicrovmImageVersions"), 2);
        assert_eq!(fake.call_count("DeleteMicrovmImageVersion"), 1);
        assert!(fake.calls().iter().any(|call| {
            call.operation == "ListMicrovmImageVersions"
                && call.path.contains("nextToken=versions-next")
        }));
        let calls = fake.calls();
        let deleted = calls
            .iter()
            .position(|call| call.operation == "DeleteMicrovmImage")
            .unwrap();
        assert_eq!(
            calls[..deleted]
                .iter()
                .filter(|call| call.operation == "ListMicrovms")
                .count(),
            3
        );
        assert_eq!(
            calls
                .iter()
                .filter(|call| call.operation == "TerminateMicrovm")
                .count(),
            1
        );
        assert!(
            calls
                .iter()
                .filter(|call| call.method == super::super::transport::Method::Delete)
                .all(|call| !call.path.contains("keep") && !call.path.contains("foreign"))
        );
    }

    #[tokio::test]
    async fn confirmed_terminations_survive_later_listing_omissions() {
        let (fake, _, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(r#"{"items":[{"imageArn":"arn:a","name":"job-a","state":"CREATED"},{"imageArn":"arn:b","name":"job-b","state":"CREATED"}]}"#));
        fake.answer("ListMicrovmImages", Answer::ok(EMPTY));
        fake.answer("ListMicrovms", Answer::ok(r#"{"items":[{"microvmId":"mvm-a","imageArn":"arn:a","imageVersion":"1","state":"RUNNING"},{"microvmId":"mvm-b","imageArn":"arn:b","imageVersion":"1","state":"RUNNING"}]}"#));
        fake.answer("ListMicrovms", Answer::ok(r#"{"items":[{"microvmId":"mvm-a","imageArn":"arn:a","imageVersion":"1","state":"TERMINATED"},{"microvmId":"mvm-b","imageArn":"arn:b","imageVersion":"1","state":"TERMINATING"}]}"#));
        fake.answer("ListMicrovms", Answer::ok(r#"{"items":[{"microvmId":"mvm-b","imageArn":"arn:b","imageVersion":"1","state":"TERMINATED"}]}"#));
        fake.answer(
            "ListMicrovms",
            Answer::failure(403, "both terminations were already confirmed"),
        );
        fake.answer("TerminateMicrovm", Answer::ok("{}"));
        fake.answer("ListMicrovmImageVersions", Answer::ok(EMPTY));
        fake.answer(
            "DeleteMicrovmImage",
            Answer::ok(r#"{"imageIdentifier":"arn:a","state":"DELETING"}"#),
        );
        fake.answer(
            "DeleteMicrovmImage",
            Answer::ok(r#"{"imageIdentifier":"arn:b","state":"DELETING"}"#),
        );

        let report = plane
            .sweep(
                "job-",
                &SweepOptions {
                    dry_run: false,
                    ..SweepOptions::default()
                },
            )
            .await
            .unwrap();

        assert_eq!(report.microvms_matched, ["mvm-a", "mvm-b"]);
        assert_eq!(report.microvms_terminated, ["mvm-a", "mvm-b"]);
        assert_eq!(report.images_deleted, ["arn:a", "arn:b"]);
        assert!(report.images_in_use.is_empty());
        assert!(report.failures.is_empty());
        assert_eq!(fake.call_count("ListMicrovms"), 3);
        assert_eq!(fake.call_count("TerminateMicrovm"), 2);
        assert_eq!(fake.call_count("DeleteMicrovmImage"), 2);
    }

    #[tokio::test]
    async fn cleanup_reports_held_images_without_deleting_them() {
        let (fake, clock, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer("ListMicrovms", Answer::ok(VMS));
        polls_through_deadline(&fake, VMS);
        fake.answer("TerminateMicrovm", Answer::failure(403, "denied"));
        let report = plane
            .sweep(
                "job-",
                &SweepOptions {
                    keep: vec!["job-keep".into()],
                    dry_run: false,
                },
            )
            .await
            .unwrap();
        assert_eq!(report.images_in_use, ["arn:old"]);
        assert_eq!(report.failures.len(), 1);
        assert_eq!(report.failures[0].resource, "mvm-old");
        assert!(clock.now() >= DEFAULT_LIFECYCLE_TIMEOUT);
        assert!(report.microvms_terminated.is_empty() && report.images_deleted.is_empty());
        assert!(
            fake.calls()
                .iter()
                .all(|call| call.operation != "DeleteMicrovmImage")
        );
    }

    #[tokio::test]
    async fn a_missing_vm_never_becomes_permission_to_delete_its_image() {
        let (fake, _, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer("ListMicrovms", Answer::ok(VMS));
        polls_through_deadline(&fake, EMPTY);
        fake.answer("TerminateMicrovm", Answer::ok("{}"));
        let report = plane
            .sweep(
                "job-",
                &SweepOptions {
                    keep: vec!["job-keep".into()],
                    dry_run: false,
                },
            )
            .await
            .unwrap();
        assert!(report.microvms_terminated.is_empty());
        assert_eq!(report.images_in_use, ["arn:old"]);
        assert!(report.failures.is_empty());
        assert!(
            fake.calls()
                .iter()
                .all(|call| call.operation != "ListMicrovmImageVersions")
        );
    }

    #[tokio::test]
    async fn polling_errors_preserve_the_inventory_and_prior_outcomes() {
        let (fake, _, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer("ListMicrovms", Answer::ok(VMS));
        fake.answer("ListMicrovms", Answer::failure(403, "denied"));
        fake.answer("TerminateMicrovm", Answer::ok("{}"));
        let options = SweepOptions {
            keep: vec!["job-keep".into()],
            dry_run: false,
        };
        let report = plane.sweep("job-", &options).await.unwrap();
        assert_eq!(report.microvms_matched, ["mvm-old"]);
        assert_eq!(report.images_in_use, ["arn:old"]);
        assert_eq!(report.failures[0].resource, "ListMicrovms");

        let (fake, _, plane) = fixture();
        fake.answer("ListMicrovmImages", Answer::ok(IMAGES));
        fake.answer("ListMicrovmImages", Answer::failure(403, "denied"));
        fake.answer("ListMicrovms", Answer::ok(EMPTY));
        fake.answer("ListMicrovms", Answer::ok(EMPTY));
        fake.answer(
            "ListMicrovms",
            Answer::failure(403, "termination was already confirmed"),
        );
        fake.answer("ListMicrovmImageVersions", Answer::ok(EMPTY));
        fake.answer(
            "DeleteMicrovmImage",
            Answer::ok(r#"{"imageIdentifier":"arn:old","state":"DELETING"}"#),
        );
        let report = plane.sweep("job-", &options).await.unwrap();
        assert_eq!(report.images_matched, ["arn:keep", "arn:old"]);
        assert_eq!(report.failures[0].resource, "ListMicrovmImages");
        assert!(report.images_deleted.is_empty());
        assert!(
            fake.calls()
                .iter()
                .any(|call| call.operation == "DeleteMicrovmImage")
        );
    }

    #[tokio::test]
    async fn sweep_refuses_empty_scope_and_repeated_pagination_before_mutation() {
        let (fake, _, plane) = fixture();
        assert!(plane.sweep("", &SweepOptions::default()).await.is_err());
        assert!(fake.calls().is_empty());
        fake.answer(
            "ListMicrovmImages",
            Answer::ok(r#"{"items":[],"nextToken":"again"}"#),
        );
        assert!(plane.sweep("job-", &SweepOptions::default()).await.is_err());
        assert_eq!(fake.calls().len(), 2);
    }
}
