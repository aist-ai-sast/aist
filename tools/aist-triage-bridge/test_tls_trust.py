"""Run inside the bridge image as root; exercise its real startup and HTTPS trust."""
import os
import ssl
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx


class RuntimeCertificateTrustTests(unittest.TestCase):
    def test_startup_trusts_aist_certificate_and_still_rejects_wrong_hostname(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "server.key"
            certificate = root / "server.crt"
            subprocess.run([
                "/usr/bin/openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                "-keyout", str(key), "-out", str(certificate),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(204)
                    self.end_headers()

                def log_message(self, *_args):
                    pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"https://localhost:{server.server_port}/"
            with self.assertRaises(httpx.ConnectError):
                httpx.get(url)
            original_roots = ssl.create_default_context().cert_store_stats()["x509_ca"]

            trust_dir = Path("/usr/local/share/ca-certificates/aist")
            trust_dir.mkdir(parents=True, exist_ok=True)
            (trust_dir / "aist.crt").write_bytes(certificate.read_bytes())
            socket_path = "/run/claude-bridge/tls-test.sock"
            bridge = subprocess.Popen(
                ["/entrypoint.sh"],
                env={**os.environ, "AIST_LOCAL_TRIAGE_BRIDGE_SOCKET": socket_path},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 30
                while not Path(socket_path).exists() and bridge.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertIsNone(bridge.poll(), "Bridge startup failed")
                self.assertTrue(Path(socket_path).exists(), "Bridge did not start")
                self.assertEqual(httpx.get(url).status_code, 204)
                self.assertGreaterEqual(ssl.create_default_context().cert_store_stats()["x509_ca"], original_roots)
                with self.assertRaises(httpx.ConnectError):
                    httpx.get(f"https://127.0.0.1:{server.server_port}/")
            finally:
                bridge.terminate()
                bridge.wait(timeout=10)
