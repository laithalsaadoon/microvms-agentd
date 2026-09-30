# SPDX-License-Identifier: Apache-2.0
"""`Sandbox.run` by bare image name reaches the core's resolver (#253).

The core resolves a name to its ARN inside its one `run`, before `RunMicrovm`. A unit run has no
control plane to answer the listing, so what is asserted here is which call a launch makes
first: with a credential chain that finds nothing, the core refuses the first signed call and
names it. The listing and the ARN it answers are asserted in Rust
(`crates/microvms-app/src/sandbox.rs`), and the live suite launches by name against AWS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import microvms


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A default chain that resolves nothing, and makes no network call finding that out.

    The shared config files point at paths that don't exist and the instance metadata lookup
    is off, so no source is left to try. The proxy on a port nothing listens on is the parity
    runner's: had a chain resolved anyway, the call would fail to connect rather than reach
    AWS.
    """
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def first_signed_call(image_identifier: str) -> str:
    """The message of the refusal the launch's first signed call meets."""
    sandbox = microvms.Sandbox(microvms.Region.us_east_1())
    with pytest.raises(microvms.CredentialsError) as raised:
        sandbox.run(image_identifier=image_identifier)
    return str(raised.value)


def test_a_bare_image_name_is_listed_before_any_launch() -> None:
    """The name reaches the core as a name, and the core's first call is the listing."""
    message = first_signed_call("wanted-image")
    assert "for ListMicrovmImages" in message, message


def test_an_image_arn_goes_straight_to_the_launch() -> None:
    """An ARN costs no listing: the first call is `RunMicrovm` itself."""
    message = first_signed_call(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:wanted-image"
    )
    assert "for RunMicrovm" in message, message
