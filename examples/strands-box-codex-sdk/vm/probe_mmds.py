# SPDX-License-Identifier: Apache-2.0
"""Try to reach the MicroVM's instance metadata service (MMDS) and print what happened.

run.sh runs this twice: once directly as uid 1000, where docs/TRUST.md says the execution
role's credentials are reachable, and once as the agent of box/probe-box.toml, inside the box.
It prints status lines and error names only, never a token or a credential.

It speaks HTTP over a plain socket, so inside the box it needs only the interpreter's _socket
module: urllib would pull in hashlib and OpenSSL, which probe-box.toml does not grant.
"""

import os
import socket
from urllib.parse import urlsplit

MMDS = "169.254.169.254"
TOKEN_PUT = (
    "PUT {target} HTTP/1.1\r\n"
    f"Host: {MMDS}\r\n"
    "X-aws-ec2-metadata-token-ttl-seconds: 60\r\n"
    "Content-Length: 0\r\n"
    "Connection: close\r\n\r\n"
)


def raw_socket() -> str:
    try:
        with socket.create_connection((MMDS, 80), timeout=3):
            return "connected"
    except OSError as error:
        return f"refused: {type(error).__name__}: {error.strerror or error}"


def token_put(proxy: str | None) -> str:
    """PUT /latest/api/token, directly or through an HTTP proxy, and summarize the reply."""
    if proxy:
        parts = urlsplit(proxy)
        address = (parts.hostname, parts.port or 80)
        target = f"http://{MMDS}/latest/api/token"
    else:
        address, target = (MMDS, 80), "/latest/api/token"
    try:
        with socket.create_connection(address, timeout=5) as conn:
            conn.sendall(TOKEN_PUT.format(target=target).encode())
            reply = b""
            while b"\r\n\r\n" not in reply and (chunk := conn.recv(4096)):
                reply += chunk
            head, _, body = reply.partition(b"\r\n\r\n")
            lines = head.decode(errors="replace").split("\r\n")
            length = next(
                (
                    int(h.split(":", 1)[1])
                    for h in lines[1:]
                    if h.lower().startswith("content-length:")
                ),
                0,
            )
            # The server may keep the connection open, so read the body by its length.
            while len(body) < length and (chunk := conn.recv(4096)):
                body += chunk
    except OSError as error:
        return f"refused: {type(error).__name__}: {error.strerror or error}"
    status = lines[0]
    if status.split()[1:2] == ["200"]:
        return f"{status} (token issued, not printed)"
    marker = next(
        (h for h in lines[1:] if h.lower().startswith("x-strands-box-egress")), ""
    )
    text = body.decode(errors="replace").strip().splitlines()
    return f"{status} {marker}: {text[0][:240] if text else ''}"


proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
where = "inside the box" if proxy else "outside the box"
via = "the egress gateway" if proxy else "a direct connection"
print(f"[{where}] raw TCP connect to {MMDS}:80 -> {raw_socket()}")
print(f"[{where}] IMDSv2 token PUT via {via} -> {token_put(proxy)}")
