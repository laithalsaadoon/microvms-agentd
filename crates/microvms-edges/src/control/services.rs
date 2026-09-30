// SPDX-License-Identifier: Apache-2.0
//! The two calls outside the MicroVMs API that `ensure_image` makes: the caller's account,
//! for the image ARN, and the artifact upload (#221, IMAGE-8).
//!
//! # Why these are hand-signed here rather than an SDK
//!
//! The same reason the control plane is ([`crate::control::transport`]): one HTTP stack, one TLS
//! implementation, one credential chain. `aws-sdk-s3` and `aws-sdk-sts` would each bring a
//! smithy client this crate turned off on purpose. Two operations — `GetCallerIdentity` and
//! a single-part `PutObject` — are small enough to sign with the `aws-sigv4` the control
//! plane already uses, and the credentials come from the same default chain
//! ([`crate::control::transport::default_credentials`]), so the ARN, the upload, and the create are
//! one identity.
//!
//! # Why the account comes from STS
//!
//! `GetMicrovmImage` takes an ARN, and the service rejects a bare name, so the image ARN has
//! to be built as `arn:aws:lambda:<region>:<account>:microvm-image:<name>` before the first
//! describe. `GetCallerIdentity` is the one call that answers the account for any identity —
//! a user, a role, an SSO session — and needs no permission to make. It is made once per
//! `Sandbox` and cached there.
//!
//! # The seam
//!
//! `BuildServices` (`microvms_app::control::BuildServices`) is the trait
//! `Sandbox::with_build_services` takes, the way `ControlPlane::from_ports` takes a transport:
//! a test records the calls and answers them, and production builds [`SignedBuildServices`]
//! through the plane's adapters on the first `ensure_image`.

use futures_util::future::BoxFuture;

use microvms_app::control::BuildServices;
use microvms_app::error::{Error, ErrorKind};
use microvms_app::region::Region;

/// STS and S3 over SigV4, with the default credential chain.
pub struct SignedBuildServices {
    region: Region,
    credentials: aws_credential_types::provider::SharedCredentialsProvider,
    http: reqwest::Client,
}

impl std::fmt::Debug for SignedBuildServices {
    /// Hand-written for the reason `SignedTransport`'s is: a derived one would print the
    /// credentials provider.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SignedBuildServices")
            .field("region", &self.region)
            .finish_non_exhaustive()
    }
}

impl SignedBuildServices {
    /// Resolves the default credential chain for `region`. No call is made.
    ///
    /// The HTTP timeout is fifteen minutes rather than the control plane's minute, because
    /// the upload carries the whole artifact in one request.
    pub async fn new(region: Region) -> Result<Self, Error> {
        Ok(Self {
            credentials: crate::control::transport::default_credentials(&region).await?,
            http: crate::control::transport::http_client(std::time::Duration::from_secs(15 * 60))?,
            region,
        })
    }

    async fn send(
        &self,
        operation: &str,
        mut request: http::Request<Vec<u8>>,
        service: &str,
        settings: aws_sigv4::http_request::SigningSettings,
    ) -> Result<(u16, Vec<u8>), Error> {
        let credentials =
            crate::control::transport::resolve_credentials(&self.credentials, operation).await?;
        crate::control::transport::sign_for(
            &mut request,
            &credentials,
            &self.region,
            service,
            settings,
        )?;
        let url = request.uri().to_string();
        let (parts, body) = request.into_parts();
        let request = http::Request::from_parts(parts, reqwest::Body::from(body));
        let request = reqwest::Request::try_from(request).map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not convert the signed {operation} request: {error}"),
            )
            .with_source(error)
        })?;
        let response = self.http.execute(request).await.map_err(|error| {
            Error::new(
                ErrorKind::Retryable,
                format!("the {operation} request to {url} did not complete: {error}"),
            )
            .with_source(error)
        })?;
        let status = response.status().as_u16();
        let body = response.bytes().await.map_err(|error| {
            Error::new(
                ErrorKind::Retryable,
                format!("could not read the {operation} response body: {error}"),
            )
            .with_source(error)
        })?;
        Ok((status, body.to_vec()))
    }

    async fn caller_account_once(&self) -> Result<String, Error> {
        let body = b"Action=GetCallerIdentity&Version=2011-06-15".to_vec();
        let request = http::Request::builder()
            .method("POST")
            .uri(sts_endpoint(&self.region))
            .header(
                "content-type",
                "application/x-www-form-urlencoded; charset=utf-8",
            )
            .body(body)
            .map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("could not build the GetCallerIdentity request: {error}"),
                )
            })?;
        let (status, body) = self
            .send(
                "GetCallerIdentity",
                request,
                "sts",
                aws_sigv4::http_request::SigningSettings::default(),
            )
            .await?;
        if status == 200 {
            return account_from_sts(&body);
        }
        Err(xml_failure("GetCallerIdentity", status, &body))
    }

    async fn put_object_once(&self, bucket: &str, key: &str, bytes: &[u8]) -> Result<(), Error> {
        let request = http::Request::builder()
            .method("PUT")
            .uri(s3_object_url(bucket, key, &self.region))
            .header("content-type", "application/zip")
            .body(bytes.to_vec())
            .map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("could not build the PutObject request: {error}"),
                )
            })?;
        let (status, body) = self
            .send("PutObject", request, "s3", s3_signing_settings())
            .await?;
        if status == 200 {
            return Ok(());
        }
        Err(xml_failure(
            &format!("PutObject s3://{bucket}/{key}"),
            status,
            &body,
        ))
    }
}

/// Retries `attempt` on a retryable failure, as `send_with_retry` retries a control-plane
/// call: jittered exponential backoff from 200 ms to 20 s, five retries.
async fn with_retry<T, F, Fut>(attempt: F) -> Result<T, Error>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<T, Error>>,
{
    use backon::{ExponentialBuilder, Retryable};

    attempt
        .retry(
            ExponentialBuilder::default()
                .with_jitter()
                .with_min_delay(std::time::Duration::from_millis(200))
                .with_max_delay(std::time::Duration::from_secs(20))
                .with_max_times(5),
        )
        .when(Error::retryable)
        .await
}

impl BuildServices for SignedBuildServices {
    fn caller_account(&self) -> BoxFuture<'_, Result<String, Error>> {
        Box::pin(with_retry(|| self.caller_account_once()))
    }

    fn put_object<'a>(
        &'a self,
        bucket: &'a str,
        key: &'a str,
        bytes: Vec<u8>,
    ) -> BoxFuture<'a, Result<(), Error>> {
        Box::pin(async move { with_retry(|| self.put_object_once(bucket, key, &bytes)).await })
    }
}

/// `https://sts.<region>.amazonaws.com/`, the regional endpoint.
fn sts_endpoint(region: &Region) -> String {
    format!("https://sts.{}.amazonaws.com/", region.as_str())
}

/// The object URL: virtual-hosted, or path-style for a bucket name with a dot (a dotted
/// name does not match the `*.s3.<region>.amazonaws.com` certificate). Each key segment is
/// percent-encoded and the `/` between them kept, which is the path S3 signs.
pub(crate) fn s3_object_url(bucket: &str, key: &str, region: &Region) -> String {
    let path: Vec<String> = key
        .split('/')
        .map(microvms_app::control::transport::paths::encode_segment)
        .collect();
    let path = path.join("/");
    if bucket.contains('.') {
        format!(
            "https://s3.{}.amazonaws.com/{bucket}/{path}",
            region.as_str()
        )
    } else {
        format!(
            "https://{bucket}.s3.{}.amazonaws.com/{path}",
            region.as_str()
        )
    }
}

/// S3's signing rules: the path is signed as sent (already encoded once, never
/// normalized), and the payload hash travels as `x-amz-content-sha256`, which S3 requires.
pub(crate) fn s3_signing_settings() -> aws_sigv4::http_request::SigningSettings {
    use aws_sigv4::http_request::{
        PayloadChecksumKind, PercentEncodingMode, SigningSettings, UriPathNormalizationMode,
    };
    let mut settings = SigningSettings::default();
    settings.percent_encoding_mode = PercentEncodingMode::Single;
    settings.uri_path_normalization_mode = UriPathNormalizationMode::Disabled;
    settings.payload_checksum_kind = PayloadChecksumKind::XAmzSha256;
    settings
}

/// The text between `<tag>` and `</tag>`, the first time it appears.
fn xml_text<'a>(body: &'a str, tag: &str) -> Option<&'a str> {
    let open = format!("<{tag}>");
    let close = format!("</{tag}>");
    let start = body.find(&open)? + open.len();
    let end = body[start..].find(&close)? + start;
    Some(body[start..end].trim())
}

/// The account in a `GetCallerIdentityResponse`, which must be twelve digits.
pub(crate) fn account_from_sts(body: &[u8]) -> Result<String, Error> {
    let text = String::from_utf8_lossy(body);
    match xml_text(&text, "Account") {
        Some(account) if account.len() == 12 && account.bytes().all(|b| b.is_ascii_digit()) => {
            Ok(account.to_string())
        }
        _ => Err(Error::new(
            ErrorKind::Platform,
            format!(
                "GetCallerIdentity answered 200 with no twelve-digit Account: {}",
                text.chars().take(512).collect::<String>()
            ),
        )),
    }
}

/// A failed STS or S3 call as an [`Error`]: the service's `Code` and `Message` from its XML
/// error body, classified by status, with the remedy the common ones need.
pub(crate) fn xml_failure(operation: &str, status: u16, body: &[u8]) -> Error {
    let text = String::from_utf8_lossy(body);
    let code = xml_text(&text, "Code").unwrap_or("").to_string();
    let message = xml_text(&text, "Message").unwrap_or("").to_string();
    let said = if code.is_empty() && message.is_empty() {
        "the service sent no error code".to_string()
    } else {
        format!("{code}: {message}")
    };
    let (kind, remedy) = match (status, code.as_str()) {
        (301, _) | (_, "PermanentRedirect") | (_, "AuthorizationHeaderMalformed") => (
            ErrorKind::Precondition,
            format!(
                "The bucket is in another region{}. The artifact bucket must be in the \
                 sandbox's region, where the image build reads it.",
                xml_text(&text, "Region")
                    .map(|region| format!(" ({region})"))
                    .unwrap_or_default()
            ),
        ),
        (404, _) | (_, "NoSuchBucket") => (
            ErrorKind::Precondition,
            "The bucket does not exist in this account and region.".to_string(),
        ),
        (403, _) => (
            ErrorKind::Credentials,
            "The identity is not allowed this call: an upload needs s3:PutObject on the \
             bucket and key prefix."
                .to_string(),
        ),
        (429, _) | (_, "SlowDown") | (_, "Throttling") | (_, "ThrottlingException") => (
            ErrorKind::Retryable,
            "Throttled; retry the identical request.".to_string(),
        ),
        (status, _) if status >= 500 => (
            ErrorKind::Retryable,
            "A service fault; retry the identical request.".to_string(),
        ),
        _ => (ErrorKind::Platform, String::new()),
    };
    Error::new(
        kind,
        format!("{operation} failed with HTTP {status}: {said}. {remedy}")
            .trim_end()
            .to_string(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// **IMAGE-8, the upload URL.** Virtual-hosted for an ordinary bucket, path-style for a
    /// dotted one, and each key segment encoded with the `/` between them kept.
    #[test]
    fn the_object_url_names_the_bucket_and_the_encoded_key() {
        assert_eq!(
            s3_object_url(
                "agentd-conformance",
                "harbor/task-0123456789ab/artifact.zip",
                &Region::UsEast1
            ),
            "https://agentd-conformance.s3.us-east-1.amazonaws.com/harbor/task-0123456789ab/artifact.zip"
        );
        assert_eq!(
            s3_object_url("my.bucket", "a b/c+d.zip", &Region::EuWest1),
            "https://s3.eu-west-1.amazonaws.com/my.bucket/a%20b/c%2Bd.zip"
        );
    }

    /// **IMAGE-8, the account.** Read from the XML answer, and refused unless it is twelve
    /// digits — an ARN built from anything else names no image.
    #[test]
    fn the_account_is_read_from_the_sts_answer() {
        let body =
            br#"<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:sts::741448939267:assumed-role/Admin/me</Arn>
    <UserId>AROAEXAMPLE:me</UserId>
    <Account>741448939267</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>x</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>"#;
        assert_eq!(account_from_sts(body).expect("an account"), "741448939267");
        account_from_sts(b"<Account>12</Account>").expect_err("not twelve digits");
        account_from_sts(b"<html>").expect_err("no account");
    }

    /// A failed upload names the service's code and the remedy, and is classified so the
    /// retry loop retries only what a retry can fix.
    #[test]
    fn a_failed_call_names_the_code_and_is_classified() {
        let error = xml_failure(
            "PutObject s3://b/k",
            403,
            b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>",
        );
        assert_eq!(error.kind(), ErrorKind::Credentials);
        assert!(error.to_string().contains("AccessDenied"), "{error}");
        assert!(error.to_string().contains("s3:PutObject"), "{error}");

        let error = xml_failure(
            "PutObject s3://b/k",
            301,
            b"<Error><Code>PermanentRedirect</Code><Message>m</Message></Error>",
        );
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert!(error.to_string().contains("another region"), "{error}");

        let error = xml_failure(
            "PutObject",
            404,
            b"<Error><Code>NoSuchBucket</Code></Error>",
        );
        assert_eq!(error.kind(), ErrorKind::Precondition);

        for status in [500, 503] {
            assert!(
                xml_failure("PutObject", status, b"").retryable(),
                "{status}"
            );
        }
        assert!(xml_failure("PutObject", 503, b"<Error><Code>SlowDown</Code></Error>").retryable());
        assert!(
            !xml_failure(
                "PutObject",
                400,
                b"<Error><Code>InvalidRequest</Code></Error>"
            )
            .retryable()
        );
    }

    /// The S3 signature covers the payload hash, carried as `x-amz-content-sha256`, and is
    /// scoped to `s3` in the request's region. Checked with static credentials, since the
    /// signing is the only part of the upload that can be checked without a call.
    #[test]
    fn an_upload_is_signed_for_s3_with_the_payload_hash() {
        let credentials =
            aws_credential_types::Credentials::new("AKIDEXAMPLE", "secret", None, None, "test");
        let mut request = http::Request::builder()
            .method("PUT")
            .uri(s3_object_url("bucket", "k/artifact.zip", &Region::UsEast1))
            .body(b"zip bytes".to_vec())
            .expect("a request");
        crate::control::transport::sign_for(
            &mut request,
            &credentials,
            &Region::UsEast1,
            "s3",
            s3_signing_settings(),
        )
        .expect("signs");
        let headers = request.headers();
        let authorization = headers["authorization"].to_str().expect("ascii");
        assert!(
            authorization.contains("/us-east-1/s3/aws4_request"),
            "{authorization}"
        );
        assert!(
            authorization.contains("x-amz-content-sha256"),
            "{authorization}"
        );
        let digest = {
            use sha2::{Digest as _, Sha256};
            const_hex::encode(Sha256::digest(b"zip bytes"))
        };
        assert_eq!(headers["x-amz-content-sha256"], digest.as_str());
    }
}
