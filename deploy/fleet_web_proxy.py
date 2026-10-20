from __future__ import annotations

import base64
import hmac
import http.client
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "192.168.2.11")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "80"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8443"))
USERNAME = os.environ.get("WEB_AUTH_USERNAME", "fleet-admin")
AUTH_ENABLED = os.environ.get("WEB_AUTH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
PASSWORD = (
    Path(os.environ.get("WEB_AUTH_PASSWORD_FILE", "/run/secrets/fleet_web_password")).read_text(encoding="utf-8").strip()
    if AUTH_ENABLED
    else ""
)
TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE", "/run/secrets/fleet_web_tls_cert")
TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE", "/run/secrets/fleet_web_tls_key")
MAX_REQUEST_BYTES = 1_100_000


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        # The public listener may receive clients that connect but never finish
        # the TLS handshake or HTTP request.  Keep those sockets from occupying
        # a worker indefinitely.
        self.request.settimeout(10)
        super().setup()

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def _authorized(self) -> bool:
        if not AUTH_ENABLED:
            return True
        value = self.headers.get("authorization", "")
        if not value.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        supplied_user, separator, supplied_password = decoded.partition(":")
        return bool(separator) and hmac.compare_digest(supplied_user, USERNAME) and hmac.compare_digest(supplied_password, PASSWORD)

    def _proxy(self) -> None:
        if not self._authorized():
            body = b"Authentication required\n"
            self.send_response(401)
            self.send_header("www-authenticate", 'Basic realm="Fleet Web", charset="UTF-8"')
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("x-content-type-options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        try:
            content_length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._error(400, "invalid content-length")
            return
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            self._error(413, "request body is too large")
            return
        body = self.rfile.read(content_length) if content_length else None
        headers = {}
        if self.headers.get("content-type"):
            headers["content-type"] = self.headers["content-type"]
        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=15)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for name in ("content-type", "content-disposition", "cache-control"):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header("content-length", str(len(payload)))
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            self.send_header("x-frame-options", "DENY")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self._error(502, "Fleet Web is temporarily unavailable")
        finally:
            connection.close()

    def _error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("x-content-type-options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
    # Defer TLS handshakes to the per-connection threads.  Performing them in
    # ThreadingHTTPServer.accept() lets one stalled client block every request.
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.serve_forever()


if __name__ == "__main__":
    main()
