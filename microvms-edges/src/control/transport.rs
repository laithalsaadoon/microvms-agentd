// SPDX-License-Identifier: Apache-2.0
//! The signed transport: the default credential chain, SigV4, and reqwest.
//!
//! [`Transport`] and the calls it carries are `microvms_app::control::transport`. This is the
//! implementation that reaches AWS, and the credential and signing helpers the build services
//! and the Bedrock minter share with it, so the three resolve one identity and sign one way.

use std::time::SystemTime;

use microvms_app::control::transport::{Call, Reply, SIGNING_NAME, Transport, endpoint_for};
use microvms_app::error::{Error, ErrorKind};
use microvms_app::region::Region;

/// The real transport: resolve credentials, sign SigV4, send with reqwest.
pub struct SignedTransport {
    region: Region,
    endpoint: String,
    credentials: aws_credential_types::provider::SharedCredentialsProvider,
    http: reqwest::Client,
}

impl std::fmt::Debug for SignedTransport {
    /// Hand-written because a derived one would print the credentials provider, and the
    /// provider's own `Debug` is not something this crate controls.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SignedTransport")
            .field("region", &self.region)
            .field("endpoint", &self.endpoint)
            .finish_non_exhaustive()
    }
}

impl SignedTransport {
    /// Resolves the default credential chain for `region` and builds the HTTP client.
    ///
    /// # Why the chain rather than explicit keys
    ///
    /// Because the credentials a developer actually has are in SSO, a credential process,
    /// or an instance profile, and re-implementing that resolution is how a client ends up
    /// working only on the machine it was written on. `aws-config`'s default chain is the
    /// whole reason to depend on it at all — the manifest turns off its HTTP client, since
    /// reqwest is the one here, but keeps `sso` and `credentials-process`.
    ///
    /// The provider is held rather than the resolved credentials: instance-profile creds
    /// are temporary, so each request re-resolves and picks up a rotation. That is why
    /// this is a provider field and not a `Credentials` field.
    pub async fn new(region: Region) -> Result<Self, Error> {
        let credentials = default_credentials(&region).await?;
        // Generous rather than tight: a control-plane call is not a hot path, and a timeout
        // shorter than the service's own tail latency turns a slow answer into a retry storm
        // against an operation that may not be idempotent.
        let http = http_client(std::time::Duration::from_secs(60))?;

        Ok(Self {
            endpoint: endpoint_for(&region),
            region,
            credentials,
            http,
        })
    }

    /// The URL this transport would send `call` to. Public for the test that checks a path
    /// against the model without needing credentials.
    fn url(&self, call: &Call) -> String {
        format!("{}{}", self.endpoint, call.path)
    }
}

/// The default credential chain for `region`, as a provider re-asked per request.
///
/// Shared by [`SignedTransport`] and [`crate::control::SignedBuildServices`], so the STS and
/// S3 calls `ensure_image` makes resolve exactly the identity the control-plane calls do.
pub(crate) async fn default_credentials(
    region: &Region,
) -> Result<aws_credential_types::provider::SharedCredentialsProvider, Error> {
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.as_str().to_string()))
        .load()
        .await;
    config.credentials_provider().ok_or_else(|| {
        Error::new(
            ErrorKind::Credentials,
            "no credentials provider resolved. The default chain looks at environment \
             variables, the shared config files, SSO, a credential process, then the EC2 \
             instance metadata service; none of them answered. `aws sts get-caller-identity` \
             is the cheapest way to see the same failure.",
        )
    })
}

/// A reqwest client with a 10-second connect timeout and the given overall `timeout`.
pub(crate) fn http_client(timeout: std::time::Duration) -> Result<reqwest::Client, Error> {
    reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(timeout)
        .build()
        .map_err(|error| {
            Error::new(
                ErrorKind::Precondition,
                format!("could not build the HTTP client: {error}"),
            )
            .with_source(error)
        })
}

/// Resolves the current credentials from `provider`, naming `operation` on failure.
pub(crate) async fn resolve_credentials(
    provider: &aws_credential_types::provider::SharedCredentialsProvider,
    operation: &str,
) -> Result<aws_credential_types::Credentials, Error> {
    use aws_credential_types::provider::ProvideCredentials as _;

    provider.provide_credentials().await.map_err(|error| {
        Error::new(
            ErrorKind::Credentials,
            format!(
                "could not resolve credentials for {operation}: {error}. Waiting will not fix \
                 this — the identity is wrong or absent."
            ),
        )
        .with_source(error)
    })
}

impl Transport for SignedTransport {
    fn resolve_credentials(
        &self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), Error>> + Send + '_>> {
        Box::pin(async move {
            use aws_credential_types::provider::ProvideCredentials as _;
            self.credentials
                .provide_credentials()
                .await
                .map(|_| ())
                .map_err(|error| {
                    Error::new(
                        ErrorKind::Credentials,
                        format!(
                            "the default credential chain resolved no credentials for {}: \
                             {error}. It looks at environment variables, the shared config \
                             files, SSO, a credential process, then the instance metadata \
                             service.",
                            self.region
                        ),
                    )
                    .with_source(error)
                })
        })
    }

    fn send(
        &self,
        call: Call,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Reply, Error>> + Send + '_>>
    {
        Box::pin(async move {
            // Re-resolved per request so an instance-profile rotation is picked up. The
            // provider caches internally, so this is not a metadata call per request.
            let credentials = resolve_credentials(&self.credentials, call.operation).await?;

            let url = self.url(&call);
            let body = call.body_bytes().to_vec();

            // Built before signing, because the signature covers the headers that are on
            // it: content-type is signed, and adding one afterwards invalidates it.
            let mut request = http::Request::builder()
                .method(call.method.as_str())
                .uri(&url)
                .header("content-type", "application/json")
                .body(body.clone())
                .map_err(|error| {
                    Error::new(
                        ErrorKind::Unexpected,
                        format!("could not build the {} request: {error}", call.operation),
                    )
                    .with_source(error)
                })?;

            sign_in_place(&mut request, &credentials, &self.region)?;

            let (parts, body) = request.into_parts();
            let request = http::Request::from_parts(parts, reqwest::Body::from(body));
            let request = reqwest::Request::try_from(request).map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("could not convert the signed request: {error}"),
                )
                .with_source(error)
            })?;

            // A failure here produced no status, so it says nothing about service state:
            // Retryable, matching the daemon transport's own rule.
            let response = self.http.execute(request).await.map_err(|error| {
                Error::new(
                    ErrorKind::Retryable,
                    format!(
                        "the {} request to {url} did not complete: {error}",
                        call.operation
                    ),
                )
                .with_source(error)
            })?;

            let status = response.status().as_u16();
            let body = response
                .bytes()
                .await
                .map_err(|error| {
                    Error::new(
                        ErrorKind::Retryable,
                        format!(
                            "could not read the {} response body: {error}",
                            call.operation
                        ),
                    )
                    .with_source(error)
                })?
                .to_vec();

            Ok(Reply { status, body })
        })
    }
}

/// Signs `request` in place with SigV4 for `region`.
///
/// Split out so the signing step is one readable unit and so the test below can sign a
/// request with static credentials and inspect the headers, which is the only way to check
/// the signing name and region without a live call.
fn sign_in_place(
    request: &mut http::Request<Vec<u8>>,
    credentials: &aws_credential_types::Credentials,
    region: &Region,
) -> Result<(), Error> {
    sign_for(
        request,
        credentials,
        region,
        SIGNING_NAME,
        aws_sigv4::http_request::SigningSettings::default(),
    )
}

/// Signs `request` in place with SigV4 for `service` in `region`, under `settings`.
///
/// The control plane signs as `lambda` with the default settings; the build services sign
/// as `sts`, and as `s3` with S3's own settings (a single-encoded path and a signed
/// `x-amz-content-sha256`), which is why the name and settings are parameters here.
pub(crate) fn sign_for(
    request: &mut http::Request<Vec<u8>>,
    credentials: &aws_credential_types::Credentials,
    region: &Region,
    service: &str,
    settings: aws_sigv4::http_request::SigningSettings,
) -> Result<(), Error> {
    use aws_sigv4::http_request::{SignableBody, SignableRequest, sign};
    use aws_sigv4::sign::v4;

    let identity = credentials.clone().into();
    let params: aws_sigv4::http_request::SigningParams = v4::SigningParams::builder()
        .identity(&identity)
        .region(region.as_str())
        .name(service)
        .time(SystemTime::now())
        .settings(settings)
        .build()
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build SigV4 signing parameters: {error}"),
            )
            .with_source(error)
        })?
        .into();

    // The headers view has to borrow from `request`, so the signable view is built and
    // consumed before the mutable borrow that applies the signature.
    let instructions = {
        let signable = SignableRequest::new(
            request.method().as_str(),
            request.uri().to_string(),
            request.headers().iter().filter_map(|(name, value)| {
                // A header whose value is not ASCII cannot be part of a canonical
                // request. This client sets only content-type, so the filter is a
                // guard against a future header rather than a live case — and dropping
                // one is correct: an unsigned header is still sent, it is just not
                // covered.
                value.to_str().ok().map(|value| (name.as_str(), value))
            }),
            // `Bytes`, never `UnsignedPayload`: non-S3 services reject an unsigned
            // payload, and an empty body still needs its empty-payload SHA256 signed.
            SignableBody::Bytes(request.body()),
        )
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build the signable request: {error}"),
            )
            .with_source(error)
        })?;

        sign(signable, &params)
            .map_err(|error| {
                Error::new(
                    ErrorKind::Credentials,
                    format!("SigV4 signing failed: {error}"),
                )
                .with_source(error)
            })?
            .into_parts()
            .0
    };

    // `http1x` rather than `http0x`: aws-sigv4's default features are sign-http + http1,
    // which is the version reqwest 0.13 consumes through TryFrom. Mixing the two http
    // versions would mean converting on every call.
    instructions.apply_to_request_http1x(request);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_app::control::transport::Method;

    /// The regression that blocked the whole live tier: with aws-config's
    /// `default-https-client` off, `load()` panics with "a http_client is
    /// required" before any credential question is even asked. Constructing
    /// through the real chain must yield a `Result` — either outcome is fine
    /// here (this host may or may not have credentials); a panic is the bug.
    /// IMDS is the only endpoint this can touch: link-local, free, and fast.
    #[tokio::test]
    async fn constructing_the_real_transport_returns_a_result_rather_than_panicking() {
        let _ = SignedTransport::new(Region::UsEast1).await;
    }

    /// Signing puts an `authorization` header on the request naming the region and the
    /// signing name, and an `x-amz-security-token` when the credentials are temporary.
    ///
    /// Static credentials rather than a resolved chain, so this runs with no AWS
    /// configuration and no network — the signature is a pure function of the inputs.
    #[test]
    fn signing_names_the_region_and_the_service_in_the_credential_scope() {
        let credentials = aws_credential_types::Credentials::new(
            "AKIDEXAMPLE",
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            Some("session-token".to_string()),
            None,
            "test",
        );
        let mut request = http::Request::builder()
            .method("POST")
            .uri("https://lambda.eu-west-1.amazonaws.com/2025-09-09/microvms")
            .header("content-type", "application/json")
            .body(b"{}".to_vec())
            .expect("builds");

        sign_in_place(&mut request, &credentials, &Region::EuWest1).expect("signs");

        let headers = request.headers();
        let authorization = headers
            .get("authorization")
            .expect("a signature was applied")
            .to_str()
            .expect("ascii");
        assert!(
            authorization.starts_with("AWS4-HMAC-SHA256 "),
            "{authorization}"
        );
        assert!(
            authorization.contains("/eu-west-1/lambda/aws4_request"),
            "the credential scope must name the request region and the signing name: \
             {authorization}"
        );
        assert!(
            headers.contains_key("x-amz-date"),
            "a signed date is required"
        );
        assert!(
            headers.contains_key("x-amz-security-token"),
            "temporary credentials carry a session token in the canonical request"
        );
    }

    /// A `GET` with no body still gets signed, with the empty-payload SHA256 —
    /// `SignableBody::Bytes(&[])` rather than `UnsignedPayload`, which non-S3 services
    /// reject.
    #[test]
    fn a_bodyless_get_is_signed_rather_than_sent_unsigned() {
        let credentials =
            aws_credential_types::Credentials::new("AKIDEXAMPLE", "secret", None, None, "test");
        let mut request = http::Request::builder()
            .method("GET")
            .uri("https://lambda.us-east-1.amazonaws.com/2025-09-09/microvms/mvm-1")
            .body(Vec::new())
            .expect("builds");

        sign_in_place(&mut request, &credentials, &Region::UsEast1).expect("signs");
        assert!(request.headers().contains_key("authorization"));
        assert!(
            !request.headers().contains_key("x-amz-security-token"),
            "static credentials carry no session token"
        );
    }

    /// A `PATCH` gets signed like every other method, with the empty-payload rule not applying
    /// because it has a body.
    ///
    /// Worth its own case because the signing path reads the method as a string for the
    /// canonical request: a method the enum spells and the signer does not is a signature over
    /// a different request than the one sent, which reads as bad credentials.
    #[test]
    fn a_patch_is_signed_with_its_method_in_the_canonical_request() {
        let credentials =
            aws_credential_types::Credentials::new("AKIDEXAMPLE", "secret", None, None, "test");
        let mut request = http::Request::builder()
            .method(Method::Patch.as_str())
            .uri(
                "https://lambda.us-east-1.amazonaws.com/2025-09-09/microvm-images/img/versions/2.0",
            )
            .header("content-type", "application/json")
            .body(br#"{"status":"INACTIVE"}"#.to_vec())
            .expect("builds");

        sign_in_place(&mut request, &credentials, &Region::UsEast1).expect("signs");
        assert_eq!(request.method().as_str(), "PATCH");
        assert!(request.headers().contains_key("authorization"));
    }
}
