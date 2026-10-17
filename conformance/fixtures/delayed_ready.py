# SPDX-License-Identifier: Apache-2.0
"""A test-only HTTP relay that delays the real daemon's image-ready hook."""

from __future__ import annotations

import http.client
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY_SECONDS = 310
READY_PATH = "/aws/lambda-microvms/runtime/v1/ready"


class Relay(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Headers and bodies can carry bootstrap credentials. Only the explicit
        # ready-hook timing records below belong in this fixture's logs.
        pass

    def forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.command == "POST" and self.path == READY_PATH:
            started = time.monotonic()
            print('READY_MEASUREMENT {"event":"begin"}', flush=True)
            time.sleep(DELAY_SECONDS)
            print(
                "READY_MEASUREMENT "
                + json.dumps({"event": "end", "elapsed": time.monotonic() - started}),
                flush=True,
            )
        # The launcher moves the real daemon to this distinct private backend;
        # the listener keeps the shared front port inherited from the image.
        connection = http.client.HTTPConnection(
            "127.0.0.1", int(os.environ["AGENTD_RELAY_BACKEND_PORT"]), timeout=60
        )
        try:
            connection.request(self.command, self.path, body, dict(self.headers))
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in {
                    "connection",
                    "transfer-encoding",
                    "content-length",
                }:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    do_GET = forward
    do_POST = forward
    do_DELETE = forward


if __name__ == "__main__":
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ["AGENTD_PORT"])), Relay
    ).serve_forever()
