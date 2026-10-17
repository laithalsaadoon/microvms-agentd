// SPDX-License-Identifier: Apache-2.0
//! A process-wide chain per region, with the SDK's expiry-aware identity cache.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};

use aws_credential_types::provider::{SharedCredentialsProvider, error::CredentialsError};
use aws_smithy_runtime_api::client::identity::{
    ResolveCachedIdentity, SharedIdentityCache, SharedIdentityResolver,
};
use aws_smithy_runtime_api::client::runtime_components::RuntimeComponents;
use aws_smithy_types::config_bag::ConfigBag;
use microvms_app::error::{Error, ErrorKind};
use microvms_app::region::Region;
use tokio::sync::{Mutex, OnceCell};

type RegionProviders = Mutex<HashMap<Region, Arc<OnceCell<SharedCredentialsProvider>>>>;

static PROVIDERS: OnceLock<RegionProviders> = OnceLock::new();

pub(super) fn diagnostic(error: &CredentialsError) -> Error {
    use aws_credential_types::provider::error::TokenError;

    // Provider errors can contain secret values, even in an ancestor's Display (for
    // example VarError::NotUnicode from AWS_SECRET_ACCESS_KEY). Keep only known error
    // categories, never provider-owned text or the original source in a public error.
    let mut messages = Vec::new();
    let mut source: Option<&(dyn std::error::Error + 'static)> = Some(error);
    while let Some(inner) = source {
        let message = if let Some(error) = inner.downcast_ref::<CredentialsError>() {
            match error {
                CredentialsError::CredentialsNotLoaded(_) => {
                    "credential provider is not configured"
                }
                CredentialsError::ProviderTimedOut(_) => "credential provider timed out",
                CredentialsError::InvalidConfiguration(_) => {
                    "credential provider configuration is invalid"
                }
                CredentialsError::ProviderError(_) => "credential provider failed",
                _ => "unexpected credential provider failure",
            }
        } else if let Some(error) = inner.downcast_ref::<TokenError>() {
            match error {
                TokenError::TokenNotLoaded(_) => "access token provider is not configured",
                TokenError::ProviderTimedOut(_) => "access token provider timed out",
                TokenError::InvalidConfiguration(_) => {
                    "access token provider configuration is invalid"
                }
                TokenError::ProviderError(_) => "access token provider failed",
                TokenError::Unhandled(_) => "unexpected access token provider failure",
            }
        } else if let Some(std::env::VarError::NotUnicode(_)) = inner.downcast_ref() {
            "credential environment variable is not valid Unicode"
        } else if inner
            .downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::TimedOut)
            || inner
                .downcast_ref::<reqwest::Error>()
                .is_some_and(reqwest::Error::is_timeout)
        {
            "credential provider request timed out"
        } else {
            source = inner.source();
            continue;
        };
        if !messages.contains(&message) {
            messages.push(message);
        }
        source = inner.source();
    }
    Error::new(ErrorKind::Credentials, messages.join(": "))
}

pub(super) async fn shared(region: &Region) -> Result<SharedCredentialsProvider, Error> {
    let providers = PROVIDERS.get_or_init(Mutex::default);
    get_or_load(providers, region, async {
        let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new(region.as_str().to_string()))
            .load()
            .await;
        cached(&config)
    })
    .await
}

async fn get_or_load(
    providers: &RegionProviders,
    region: &Region,
    load: impl Future<Output = Result<SharedCredentialsProvider, Error>>,
) -> Result<SharedCredentialsProvider, Error> {
    // Never hold the registry lock while loading config. Each region serializes only its
    // own first load; cancellation or failure leaves its cell available for another caller.
    let cell = providers
        .lock()
        .await
        .entry(region.clone())
        .or_default()
        .clone();
    cell.get_or_try_init(|| load).await.cloned()
}

struct CachedCredentials {
    cache: SharedIdentityCache,
    resolver: SharedIdentityResolver,
    runtime: RuntimeComponents,
    config: ConfigBag,
}

impl std::fmt::Debug for CachedCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Neither the provider's Debug nor a cached identity may reach logs.
        f.debug_struct("CachedCredentials").finish_non_exhaustive()
    }
}

fn cached(config: &aws_config::SdkConfig) -> Result<SharedCredentialsProvider, Error> {
    use aws_smithy_runtime::client::{
        auth::no_auth::{NO_AUTH_SCHEME_ID, NoAuthScheme},
        orchestrator::endpoints::StaticUriEndpointResolver,
        retries::strategy::NeverRetryStrategy,
    };
    use aws_smithy_runtime_api::client::{
        auth::{SharedAuthSchemeOptionResolver, static_resolver::StaticAuthSchemeOptionResolver},
        endpoint::SharedEndpointResolver,
        retries::SharedRetryStrategy,
    };

    let provider = config.credentials_provider().ok_or_else(|| {
        Error::new(
            ErrorKind::Credentials,
            "the default credential chain has no provider",
        )
    })?;
    let resolver = SharedIdentityResolver::new(provider);
    let cache = config
        .identity_cache()
        .unwrap_or_else(|| aws_config::identity::IdentityCache::lazy().build());
    // Only resolve_cached_identity runs on these components. Endpoint resolution,
    // authentication, sending, and retries remain in the hand-signed transport. Smithy's
    // component builder requires those slots even for a standalone identity resolution.
    let mut runtime = RuntimeComponents::builder("credential cache")
        .with_auth_scheme_option_resolver(Some(SharedAuthSchemeOptionResolver::new(
            StaticAuthSchemeOptionResolver::new(vec![NO_AUTH_SCHEME_ID]),
        )))
        .with_endpoint_resolver(Some(SharedEndpointResolver::new(
            StaticUriEndpointResolver::uri("https://localhost"),
        )))
        .with_identity_cache(Some(cache.clone()))
        .with_retry_strategy(Some(SharedRetryStrategy::new(NeverRetryStrategy::new())))
        .with_time_source(config.time_source())
        .with_sleep_impl(config.sleep_impl());
    runtime.push_auth_scheme(NoAuthScheme::new());
    runtime.set_identity_resolver(NO_AUTH_SCHEME_ID, resolver.clone());
    let runtime = runtime.build().map_err(|error| {
        Error::new(
            ErrorKind::Credentials,
            "could not initialize the credential cache",
        )
        .with_source(error)
    })?;
    let mut bag = ConfigBag::base();
    if let Some(retry) = config.retry_config() {
        bag.interceptor_state().store_put(retry.clone());
    }
    if let Some(timeout) = config.timeout_config() {
        bag.interceptor_state().store_put(timeout.clone());
    }
    Ok(SharedCredentialsProvider::new(CachedCredentials {
        cache,
        resolver,
        runtime,
        config: bag,
    }))
}

impl aws_credential_types::provider::ProvideCredentials for CachedCredentials {
    fn provide_credentials<'a>(
        &'a self,
    ) -> aws_credential_types::provider::future::ProvideCredentials<'a>
    where
        Self: 'a,
    {
        aws_credential_types::provider::future::ProvideCredentials::new(async {
            let identity = self
                .cache
                .resolve_cached_identity(self.resolver.clone(), &self.runtime, &self.config)
                .await
                .map_err(CredentialsError::provider_error)?;
            identity
                .data::<aws_credential_types::Credentials>()
                .cloned()
                .ok_or_else(|| {
                    CredentialsError::provider_error(
                        "credential cache returned another identity type",
                    )
                })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use aws_credential_types::{
        Credentials,
        provider::{ProvideCredentials, future},
    };
    use aws_smithy_async::{rt::sleep::TokioSleep, time::TimeSource};
    use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
    use std::time::{Duration, SystemTime};

    #[test]
    fn credential_diagnostics_keep_known_categories_without_provider_text() {
        let errors = [
            (
                CredentialsError::not_loaded("SYNTHETIC_CREDENTIAL_SENTINEL"),
                "not configured",
            ),
            (
                CredentialsError::invalid_configuration("SYNTHETIC_CREDENTIAL_SENTINEL"),
                "configuration is invalid",
            ),
            (
                CredentialsError::provider_error(CredentialsError::provider_timed_out(
                    Duration::from_secs(1),
                )),
                "credential provider timed out",
            ),
            (
                CredentialsError::provider_error(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "SYNTHETIC_CREDENTIAL_SENTINEL",
                )),
                "credential provider request timed out",
            ),
            (
                CredentialsError::provider_error(
                    aws_credential_types::provider::error::TokenError::provider_timed_out(
                        Duration::from_secs(1),
                    ),
                ),
                "access token provider timed out",
            ),
        ];
        for (error, expected) in errors {
            let safe = diagnostic(&error);
            assert!(safe.to_string().contains(expected));
            assert!(!format!("{safe} {safe:?}").contains("SYNTHETIC_CREDENTIAL_SENTINEL"));
            assert!(std::error::Error::source(&safe).is_none());
        }
    }

    #[derive(Clone, Debug, Default)]
    struct Clock(Arc<AtomicU64>);
    impl TimeSource for Clock {
        fn now(&self) -> SystemTime {
            SystemTime::UNIX_EPOCH + Duration::from_secs(self.0.load(Ordering::SeqCst))
        }
    }
    #[derive(Clone, Debug, Default)]
    struct Provider {
        calls: Arc<AtomicUsize>,
        fail: Arc<AtomicBool>,
        clock: Clock,
    }
    impl ProvideCredentials for Provider {
        fn provide_credentials<'a>(&'a self) -> future::ProvideCredentials<'a>
        where
            Self: 'a,
        {
            future::ProvideCredentials::new(async {
                self.calls.fetch_add(1, Ordering::SeqCst);
                tokio::task::yield_now().await;
                if self.fail.load(Ordering::SeqCst) {
                    return Err(CredentialsError::provider_error(
                        "metadata request timed out",
                    ));
                }
                Ok(Credentials::new(
                    "EXAMPLE",
                    "example",
                    None,
                    Some(self.clock.now() + Duration::from_secs(60)),
                    "test",
                ))
            })
        }
    }
    fn config(provider: Provider) -> aws_config::SdkConfig {
        aws_config::SdkConfig::builder()
            .credentials_provider(SharedCredentialsProvider::new(provider.clone()))
            .time_source(provider.clock)
            .sleep_impl(TokioSleep::new())
            .build()
    }

    #[tokio::test]
    async fn concurrent_signed_requests_resolve_credentials_once_per_expiry_window() {
        let provider = Provider::default();
        let cached = cached(&config(provider.clone())).unwrap();
        let requests = (0..64).map(|_| {
            let cached = cached.clone();
            async move {
                let credentials = cached.provide_credentials().await.unwrap();
                let mut request = http::Request::builder()
                    .uri("https://lambda.us-east-1.amazonaws.com/")
                    .body(Vec::new())
                    .unwrap();
                super::super::transport::sign_for(
                    &mut request,
                    &credentials,
                    &Region::UsEast1,
                    "lambda",
                    aws_sigv4::http_request::SigningSettings::default(),
                )
                .unwrap();
                assert!(request.headers().contains_key("authorization"));
            }
        });
        futures_util::future::join_all(requests).await;
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        provider.clock.0.store(61, Ordering::SeqCst);
        cached.provide_credentials().await.unwrap();
        assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn expired_credentials_are_not_returned_after_a_refresh_failure() {
        let provider = Provider::default();
        let cached = cached(&config(provider.clone())).unwrap();
        cached.provide_credentials().await.unwrap();
        cached.provide_credentials().await.unwrap();
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        provider.clock.0.store(61, Ordering::SeqCst);
        provider.fail.store(true, Ordering::SeqCst);
        let error = cached.provide_credentials().await.unwrap_err();
        let mut source: Option<&(dyn std::error::Error + 'static)> = Some(&error);
        let mut found = false;
        while let Some(inner) = source {
            found |= inner.to_string().contains("metadata request timed out");
            source = inner.source();
        }
        assert!(found, "refresh error retains its provider source");
        provider.fail.store(false, Ordering::SeqCst);
        cached.provide_credentials().await.unwrap();
        assert_eq!(provider.calls.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn regional_initialization_is_shared_and_failed_loads_can_retry() {
        let registry = RegionProviders::default();
        let loads = AtomicUsize::new(0);
        let provider = Provider::default();
        let requests = (0..32).map(|_| {
            get_or_load(&registry, &Region::UsEast1, async {
                loads.fetch_add(1, Ordering::SeqCst);
                tokio::task::yield_now().await;
                cached(&config(provider.clone()))
            })
        });
        for result in futures_util::future::join_all(requests).await {
            result.unwrap();
        }
        assert_eq!(loads.load(Ordering::SeqCst), 1);
        get_or_load(&registry, &Region::EuWest1, async {
            loads.fetch_add(1, Ordering::SeqCst);
            Err(Error::new(ErrorKind::Credentials, "failed initialization"))
        })
        .await
        .unwrap_err();
        get_or_load(&registry, &Region::EuWest1, async {
            loads.fetch_add(1, Ordering::SeqCst);
            cached(&config(provider))
        })
        .await
        .unwrap();
        assert_eq!(loads.load(Ordering::SeqCst), 3);
    }
}
