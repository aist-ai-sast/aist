"""Both gateway clients retain DNS approval and certificate validation on real TLS requests."""
import json
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase, override_settings
from pipeline.dast.connector import DastGatewayClient as ConnectorClient

from aist.integrations.dast_config import DastIntegrationConfig
from aist.integrations.dast_gateway_client import DastGatewayClient, DastGatewayClientError


@override_settings(AIST_DAST_ALLOWED_PRIVATE_NETWORKS=["10.23.4.5/32"])
class DastHttpsTransportTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway.example.test")])
        certificate = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("gateway.example.test")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        self.ca_file = Path(directory.name) / "ca.pem"
        self.ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file = Path(directory.name) / "key.pem"
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        self.received = []
        received = self.received

        class GatewayHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.headers["Host"], self.headers["Authorization"]))
                body = json.dumps({"contract_version": "2.0", "gateway_version": "fixture", "status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), GatewayHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.ca_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        real_connect = socket.socket.connect
        real_resolver = socket.getaddrinfo
        self.destinations = []
        self.unchecked_lookups = []

        def fixture_connect(sock, address):
            self.destinations.append(address[0])
            if address[0] != "10.23.4.5":
                self.fail("HTTPS connected to an unapproved address")
            return real_connect(sock, server.server_address)

        def changed_dns(hostname, port, *args, **kwargs):
            if hostname in {"gateway.example.test", "wrong.example.test"}:
                self.unchecked_lookups.append(hostname)
                hostname = "127.0.0.1"
            return real_resolver(hostname, port, *args, **kwargs)

        self.enterContext(patch.object(socket.socket, "connect", fixture_connect))
        self.enterContext(patch("socket.getaddrinfo", side_effect=changed_dns))

    def _platform(self, hostname):
        session = self.enterContext(requests.Session())
        config = DastIntegrationConfig(
            gateway_url=f"https://{hostname}:8443", ca_bundle=self.ca_file.read_text(),
            contract_major=2, integrator_public_id="fixture", server_fingerprint="fixture",
        )
        return DastGatewayClient(
            session=session, config=config, token="fixture-token", verify=str(self.ca_file),  # noqa: S106 -- fixture
            trusted_vpn=False, resolver=lambda *_args: ("10.23.4.5",),
        )

    def _connector(self, hostname):
        return self.enterContext(ConnectorClient(
            gateway_url=f"https://{hostname}:8443", token="fixture-token", ca_file=self.ca_file,  # noqa: S106 -- fixture
            allowed_private_networks=("10.23.4.5/32",), resolver=lambda *_args: ("10.23.4.5",),
        ))

    def test_both_clients_connect_to_approved_ip_and_retain_tls_hostname(self):
        platform = self._platform("gateway.example.test")
        connector = self._connector("gateway.example.test")
        for _attempt in range(2):
            self.assertEqual(platform.ping().status, "ok")
            self.assertEqual(connector._client.get("/integrations/v2/ping").status_code, 200)
        self.assertEqual(self.received, [("gateway.example.test:8443", "Bearer fixture-token")] * 4)
        self.assertEqual(self.unchecked_lookups, [])
        self.assertTrue(self.destinations)
        self.assertEqual(set(self.destinations), {"10.23.4.5"})

    def test_both_clients_reject_a_certificate_for_another_hostname(self):
        with self.assertRaises(DastGatewayClientError):
            self._platform("wrong.example.test").ping()
        with self.assertRaises(httpx.ConnectError):
            self._connector("wrong.example.test")._client.get("/integrations/v2/ping")
        self.assertEqual(self.received, [])

    def test_both_clients_reject_requests_outside_the_approved_origin_before_connecting(self):
        platform = self._platform("gateway.example.test")
        connector = self._connector("gateway.example.test")
        for url in ("https://different.example.test:8443/ping", "https://gateway.example.test/ping"):
            with self.subTest(url=url):
                with self.assertRaises(requests.exceptions.InvalidURL):
                    platform._session.get(url, verify=str(self.ca_file))
                with self.assertRaises(httpx.InvalidURL):
                    connector._client.get(url)
        self.assertEqual(self.destinations, [])
        self.assertEqual(self.received, [])
