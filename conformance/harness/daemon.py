# SPDX-License-Identifier: Apache-2.0
"""Raw HTTP to one VM's daemon, for the checks about the daemon rather than the client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from harness.constants import AGENT_PORT


@dataclass
class Daemon:
    """Raw HTTP to one MicroVM's daemon, through the platform's endpoint proxy.

    Not a client library and deliberately so: every method here returns the status
    integer the daemon chose, and the daemon lane's checks assert on that integer.
    The deleted Python suite asserted on the exception *its* status table mapped the
    integer to, which is one more layer that could be the thing that passes. Status
    codes are what those checks always meant.

    **Two headers, not one.** `X-aws-proxy-auth` carries a JWE scoped to a MicroVM id
    and a port set; `X-aws-proxy-port` names which of that token's allowed ports this
    request targets. Omitting the second is a rejection that reads like a bad token.
    Both measured 2026-08-05; see `docs/PLATFORM.md`.
    """

    endpoint: str
    agent_token: str
    microvm_id: str
    #: The boto3 `lambda-microvms` client, for minting the proxy token.
    microvm_client: Any
    port: int = AGENT_PORT
    timeout: float = 60.0
    _client: httpx.Client | None = None
    _proxy_token: str | None = None

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("http"):
            self.endpoint = f"https://{self.endpoint}"
        self._client = httpx.Client(timeout=self.timeout, verify=True)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def proxy_token(self) -> str:
        """The endpoint proxy token, minted once and cached for this run.

        `authToken` is a **map of header name to value**, not a bare string — the API
        is shaped for schemes needing more than one header, and reading it as a string
        is one of the six defects the first live run found. 60 minutes is the ceiling
        the service enforces rather than a choice, and it comfortably outlasts the six
        checks below, so there is no refresh path here (the CLI's own client has one).
        """
        if self._proxy_token is None:
            response = self.microvm_client.create_microvm_auth_token(
                microvmIdentifier=self.microvm_id,
                expirationInMinutes=60,
                allowedPorts=[{"port": self.port}],
            )
            self._proxy_token = str(response["authToken"]["X-aws-proxy-auth"])
        return self._proxy_token

    def headers(self, token: str | None) -> dict[str, Any]:
        """Both proxy headers, plus the bearer when one was named.

        `token=None` means send no `Authorization` at all, which is how the hook route
        is exercised — so it is a real value here rather than "use the default".

        The bearer is **bytes**, not str. httpx encodes a str header as ASCII and
        refuses anything else, which would make the non-ASCII token check below
        unsendable; the daemon's stated property is that it compares header bytes
        without decoding them, and that is only testable if a client can put arbitrary
        bytes on the wire. Verified: `httpx.Headers({"Authorization": "Bearer tökén"})`
        raises `UnicodeEncodeError`, and the bytes form puts
        `b'Bearer t\\xc3\\xb6k\\xc3\\xa9n'` on the wire.
        """
        headers: dict[str, Any] = {
            "X-aws-proxy-auth": self.proxy_token(),
            "X-aws-proxy-port": str(self.port),
        }
        if token is not None:
            headers["Authorization"] = b"Bearer " + token.encode("utf-8")
        return headers

    def status(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: Any = None,
    ) -> int:
        """One request, and the status the daemon answered with.

        Never raises on a status: these checks assert on 401 and 409 as *expected*
        outcomes, and a caller that could only reach them through exceptions would be
        a caller that cannot test the protocol. A wire failure still raises, because a
        dropped connection is not a status and must not be reported as one — which is
        exactly what the non-ASCII token check is asserting the daemon does not do.
        """
        assert self._client is not None
        response = self._client.request(
            method,
            f"{self.endpoint}{path}",
            headers=self.headers(token),
            json=json_body,
            timeout=self.timeout,
        )
        return response.status_code


def post_run_hook(daemon: Daemon, token: str) -> int:
    """Posts the platform's run hook and returns the raw status.

    Reached around the client under test for the reason the deleted oracle gave: the
    only callers of this route are the platform itself and an attacker inside the VM,
    so an affordance for it in *any* client would be a footgun with no legitimate use.
    Adding one to the CLI to make this suite tidier would break CLI-2 and CLI-5 both.

    No `Authorization` header, and the body is the platform's envelope rather than our
    payload directly: the string given to `RunMicrovm` arrives wrapped as
    `{"runHookPayload": "<it>"}`.
    """
    return daemon.status(
        "POST",
        "/aws/lambda-microvms/runtime/v1/run",
        token=None,
        json_body={"runHookPayload": json.dumps({"agent_token": token})},
    )
