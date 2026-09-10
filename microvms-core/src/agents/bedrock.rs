// SPDX-License-Identifier: Apache-2.0
//! A Bedrock bearer token from the caller's own AWS credentials (AGENT-4).
//!
//! # The recipe, and where it comes from
//!
//! A Bedrock short-term API key is a SigV4 *query* presign of `POST https://bedrock.amazonaws.com/`
//! with `Action=CallWithBearerToken`, service name `bedrock`, the caller's region in the
//! credential scope only (the host is fixed and not regional), `host` the only signed
//! header, and `X-Amz-Expires` at the requested lifetime. The presigned URL is then
//! stripped of `https://`, suffixed with the literal `&Version=1` **after** signing (so the
//! suffix is not part of the canonical query), base64-encoded, and prefixed with
//! `bedrock-api-key-`. That is the whole algorithm; there is no HMAC beyond SigV4 and no
//! encryption.
//!
//! Ported verbatim from `aws/aws-bedrock-token-generator-python` at commit `228eec2`
//! (`aws_bedrock_token_generator/token_generator.py`, read 2026-09-10; the JS and Java
//! generators implement the same bytes). The reference's `TOKEN_DURATION` is 43200
//! seconds, both the default and the ceiling: every implementation refuses a lifetime
//! outside `(0, 43200]`, and the service caps validity at the signing credentials' own
//! expiry, whichever is shorter.
//!
//! # Why this is in core and not in the CLI
//!
//! The example script minted this token with `uvx aws-bedrock-token-generator`, which
//! made a Python toolchain a prerequisite for running a coding agent and put the token
//! into a subprocess's stdout. `aws-sigv4` and `aws-config` are already in this crate for
//! the control plane, and the CLI's thinness guard (`microvms-cli/tests/thinness.rs`)
//! forbids both there, so the one place a signer can live is here.
//!
//! # The token is a credential
//!
//! [`BearerToken`]'s `Debug` prints its length and nothing else, per
//! `.erpaval/solutions/best-practices/credential-structs-never-derive-debug.md`. It
//! reaches the guest as a file over the authenticated channel and never as an argv
//! element or an env var on the wire.

use std::time::{Duration, SystemTime};

use crate::error::{Error, ErrorKind};
use crate::region::Region;

/// The reference implementation's `DEFAULT_HOST`.
const HOST: &str = "bedrock.amazonaws.com";
/// The reference implementation's `SERVICE_NAME`.
const SERVICE: &str = "bedrock";
const ACTION: &str = "CallWithBearerToken";
const PREFIX: &str = "bedrock-api-key-";
const VERSION_SUFFIX: &str = "&Version=1";

/// The reference implementation's `TOKEN_DURATION`: the default and the ceiling.
pub const MAX_LIFETIME: Duration = Duration::from_secs(43_200);

/// A minted bearer token. Opaque; `Debug` shows only the length.
#[derive(Clone, PartialEq, Eq)]
pub struct BearerToken(String);

impl BearerToken {
    /// The token text, for writing into the guest's environment file.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for BearerToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "BearerToken(<{} bytes>)", self.0.len())
    }
}

/// A token and when it stops working.
#[derive(Clone, Debug)]
pub struct Minted {
    pub token: BearerToken,
    /// The presign's expiry. The service also caps validity at the signing credentials'
    /// own expiry, which this crate cannot see, so this is an upper bound.
    pub expires_at: SystemTime,
}

/// Mints a token from the default credential chain.
///
/// One chain resolution per call, deliberately separate from the control plane's: a
/// token is minted once per `agent-up`, and holding a provider for it would keep a
/// second credential chain alive for the life of a `Sandbox`.
pub async fn mint(region: &Region, lifetime: Duration) -> Result<Minted, Error> {
    use aws_credential_types::provider::ProvideCredentials as _;

    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.as_str().to_string()))
        .load()
        .await;
    let provider = config.credentials_provider().ok_or_else(|| {
        Error::new(
            ErrorKind::Credentials,
            "no credentials provider resolved, so no Bedrock token can be minted. The \
             default chain looks at environment variables, the shared config files, SSO, a \
             credential process, then the EC2 instance metadata service; none of them \
             answered.",
        )
    })?;
    let credentials = provider.provide_credentials().await.map_err(|error| {
        Error::new(
            ErrorKind::Credentials,
            format!("the credential chain resolved but could not provide credentials: {error}"),
        )
        .with_source(error)
    })?;
    mint_with(&credentials, region, lifetime, SystemTime::now())
}

/// The pure half: a token from explicit credentials at an explicit instant.
///
/// Public so a caller holding credentials from elsewhere (a harness's own broker) can
/// mint without a chain resolution, and so the test can fix both inputs.
pub fn mint_with(
    credentials: &aws_credential_types::Credentials,
    region: &Region,
    lifetime: Duration,
    now: SystemTime,
) -> Result<Minted, Error> {
    use aws_sigv4::http_request::{
        SignableBody, SignableRequest, SignatureLocation, SigningSettings, sign,
    };
    use aws_sigv4::sign::v4;
    use base64::Engine as _;

    if lifetime.is_zero() || lifetime > MAX_LIFETIME {
        return Err(Error::invalid_arg(format!(
            "a Bedrock bearer token lives between 1 and {} seconds ({} hours); {} seconds \
             was asked for. The ceiling is the token generator's `TOKEN_DURATION`, and the \
             service additionally caps validity at the signing credentials' own expiry.",
            MAX_LIFETIME.as_secs(),
            MAX_LIFETIME.as_secs() / 3600,
            lifetime.as_secs(),
        )));
    }

    let mut settings = SigningSettings::default();
    settings.signature_location = SignatureLocation::QueryParams;
    settings.expires_in = Some(lifetime);
    let identity = credentials.clone().into();
    let params: aws_sigv4::http_request::SigningParams = v4::SigningParams::builder()
        .identity(&identity)
        .region(region.as_str())
        .name(SERVICE)
        .time(now)
        .settings(settings)
        .build()
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build SigV4 presign parameters: {error}"),
            )
            .with_source(error)
        })?
        .into();

    let url = format!("https://{HOST}/?Action={ACTION}");
    let mut request = http::Request::builder()
        .method("POST")
        .uri(&url)
        .header("host", HOST)
        .body(Vec::<u8>::new())
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build the presign request: {error}"),
            )
            .with_source(error)
        })?;
    let instructions = {
        let signable = SignableRequest::new(
            "POST",
            &url,
            [("host", HOST)].into_iter(),
            // An empty body still needs its empty-payload SHA256 in the canonical
            // request; `UnsignedPayload` is an S3-only convention.
            SignableBody::Bytes(&[]),
        )
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build the signable presign request: {error}"),
            )
            .with_source(error)
        })?;
        sign(signable, &params)
            .map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("SigV4 presigning failed: {error}"),
                )
                .with_source(error)
            })?
            .into_parts()
            .0
    };
    instructions.apply_to_request_http1x(&mut request);

    let presigned = request.uri().to_string();
    let stripped = presigned.strip_prefix("https://").ok_or_else(|| {
        Error::new(
            ErrorKind::Unexpected,
            format!("the presigned URL did not start with https://: {presigned}"),
        )
    })?;
    let payload = format!("{stripped}{VERSION_SUFFIX}");
    let token = format!(
        "{PREFIX}{}",
        base64::engine::general_purpose::STANDARD.encode(payload.as_bytes())
    );
    Ok(Minted {
        token: BearerToken(token),
        expires_at: now + lifetime,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine as _;

    fn credentials() -> aws_credential_types::Credentials {
        aws_credential_types::Credentials::new(
            "AKIDEXAMPLE",
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            Some("SESSIONTOKENEXAMPLE".to_string()),
            None,
            "test",
        )
    }

    fn decode(token: &BearerToken) -> String {
        let encoded = token
            .expose()
            .strip_prefix(PREFIX)
            .expect("the reference prefix");
        String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .expect("standard base64"),
        )
        .expect("utf-8")
    }

    /// **The token has the reference implementation's shape.** Prefix, standard base64,
    /// and a decoded presigned URL with the fixed host, the action, the requested expiry,
    /// the service and region in the credential scope, the session token, and the
    /// `&Version=1` tail.
    ///
    /// **Falsification** — change `HOST` to a regional spelling, `SERVICE` to `bedrock-runtime`,
    /// or append the version suffix before signing (it would then be sorted into the
    /// canonical query and appear before `X-Amz-*`), and this goes red.
    #[test]
    fn the_token_decodes_to_the_reference_presigned_url() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_789_000_000);
        let minted = mint_with(
            &credentials(),
            &Region::UsEast1,
            Duration::from_secs(3600),
            now,
        )
        .expect("mints");
        let url = decode(&minted.token);

        assert!(
            url.starts_with("bedrock.amazonaws.com/?"),
            "the host is fixed and the scheme is stripped: {url}"
        );
        assert!(url.contains("Action=CallWithBearerToken"), "{url}");
        assert!(url.contains("X-Amz-Algorithm=AWS4-HMAC-SHA256"), "{url}");
        assert!(url.contains("X-Amz-Expires=3600"), "{url}");
        assert!(url.contains("X-Amz-SignedHeaders=host"), "{url}");
        assert!(
            url.contains("X-Amz-Security-Token=SESSIONTOKENEXAMPLE"),
            "{url}"
        );
        assert!(
            url.contains("%2Fus-east-1%2Fbedrock%2Faws4_request"),
            "the credential scope names the region and the bedrock service: {url}"
        );
        assert!(url.contains("X-Amz-Signature="), "{url}");
        assert!(
            url.ends_with("&Version=1"),
            "the version suffix is appended after signing, so it is last: {url}"
        );
        assert_eq!(minted.expires_at, now + Duration::from_secs(3600));
    }

    /// Lifetime is bounded on both sides, before any signing happens.
    #[test]
    fn a_lifetime_outside_the_reference_range_is_refused() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_789_000_000);
        for lifetime in [Duration::ZERO, MAX_LIFETIME + Duration::from_secs(1)] {
            let error =
                mint_with(&credentials(), &Region::UsEast1, lifetime, now).expect_err("refused");
            assert_eq!(error.kind(), ErrorKind::InvalidArg);
            assert!(error.to_string().contains("43200"), "{error}");
        }
        mint_with(&credentials(), &Region::UsEast1, MAX_LIFETIME, now)
            .expect("the ceiling itself is legal");
    }

    /// The same inputs give the same token, and the region changes it (it is in the
    /// scope), which is what lets a caller mint per region deterministically.
    #[test]
    fn minting_is_deterministic_in_its_inputs_and_sensitive_to_the_region() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_789_000_000);
        let a = mint_with(&credentials(), &Region::UsEast1, MAX_LIFETIME, now).expect("a");
        let b = mint_with(&credentials(), &Region::UsEast1, MAX_LIFETIME, now).expect("b");
        let c = mint_with(&credentials(), &Region::EuWest1, MAX_LIFETIME, now).expect("c");
        assert_eq!(a.token, b.token);
        assert_ne!(a.token, c.token);
    }

    /// The credential never prints. A `Debug` that leaked it would put the token in every
    /// error message that formats a `Minted`.
    #[test]
    fn debug_prints_the_length_and_never_the_token() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_789_000_000);
        let minted = mint_with(&credentials(), &Region::UsEast1, MAX_LIFETIME, now).expect("m");
        let shown = format!("{:?}", minted.token);
        assert!(shown.starts_with("BearerToken(<"), "{shown}");
        assert!(!shown.contains(PREFIX), "{shown}");
        assert!(!shown.contains("X-Amz"), "{shown}");
    }
}
