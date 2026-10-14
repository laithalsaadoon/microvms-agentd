# SPDX-License-Identifier: Apache-2.0
"""Try to reach the MicroVM's instance metadata service (MMDS) and print what happened.

run.sh runs this twice: once directly as uid 1000, after root blocks metadata with a
blackhole route, and once as the agent of box/probe-box.toml, inside the box.
It prints status lines and error names only, never a token or a credential.

Outside the box it exits 1 if either path reaches metadata. Inside the box (probe-box.toml
sets PROBE_WHERE=inside) it exits 1 unless both paths fail the way the policy says
they should: the raw connect gets no route, and the egress gateway refuses the token PUT with
one of policy.dw's metadata rules. A refusal by Box's default deny alone fails the check too,
because it means those rules are gone.

It speaks HTTP over a plain socket, so inside the box it needs only the interpreter's _socket
module: urllib would pull in hashlib and OpenSSL, which probe-box.toml does not grant.
"""

import os
import socket
import sys
from urllib.parse import urlsplit

MMDS = "169.254.169.254"
TOKEN_PUT = (
    "PUT {target} HTTP/1.1\r\n"
    f"Host: {MMDS}\r\n"
    "X-aws-ec2-metadata-token-ttl-seconds: 60\r\n"
    "Content-Length: 0\r\n"
    "Connection: close\r\n\r\n"
)


# The rules in box/policy.dw that refuse the metadata service.
RULES = ("metadata_hosts", "metadata_addresses")


def raw_socket() -> tuple[bool, str]:
    """Open a TCP connection to MMDS. Returns whether it connected, and a status line."""
    try:
        with socket.create_connection((MMDS, 80), timeout=3):
            return True, "connected"
    except OSError as error:
        return False, f"refused: {type(error).__name__}: {error.strerror or error}"


def token_put(proxy: str | None) -> tuple[bool, str, str]:
    """PUT /latest/api/token, directly or through an HTTP proxy.

    Returns whether a token was issued, the policy rule that refused it ("" when none did),
    and a status line.
    """
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
        return False, "", f"refused: {type(error).__name__}: {error.strerror or error}"
    status = lines[0]
    if status.split()[1:2] == ["200"]:
        return True, "", f"{status} (token issued, not printed)"
    marker = next(
        (h for h in lines[1:] if h.lower().startswith("x-strands-box-egress")), ""
    )
    text = body.decode(errors="replace").strip().splitlines()
    first = text[0][:240] if text else ""
    rule = ""
    if "refused" in marker:
        rule = next((r for r in RULES if f"[policy: {r}]" in first), "")
    return False, rule, f"{status} {marker}: {first}"


def main() -> int:
    inside = os.environ.get("PROBE_WHERE") == "inside"
    where = "inside the box" if inside else "outside the box"
    proxy = (
        os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") if inside else None
    )
    via = "the egress gateway" if proxy else "a direct connection"
    connected, line = raw_socket()
    print(f"[{where}] raw TCP connect to {MMDS}:80 -> {line}")
    issued, rule, line = token_put(proxy)
    print(f"[{where}] IMDSv2 token PUT via {via} -> {line}")

    problems = []
    if connected:
        problems.append("the raw connect reached MMDS")
    if issued:
        problems.append("MMDS issued a token")
    if inside:
        if not proxy:
            problems.append(
                "Box set no HTTP_PROXY, so the PUT bypassed the egress gateway"
            )
        if not issued and not rule:
            problems.append(f"no rule of {', '.join(RULES)} refused the PUT")
    for problem in problems:
        print(f"[{where}] FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
