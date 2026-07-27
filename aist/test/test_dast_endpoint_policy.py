from django.test import SimpleTestCase

from aist.integrations.dast_endpoint_policy import DastEndpointPolicy, DastEndpointPolicyError


class SequenceResolver:

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, hostname, port):
        return self.answers.pop(0)


class DastEndpointPolicyTests(SimpleTestCase):

    def test_direct_route_accepts_only_public_addresses(self):
        policy = DastEndpointPolicy(
            trusted_vpn=False,
            resolver=lambda _host, _port: ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )

        endpoint = policy.validate("https://gateway.example/")

        self.assertEqual(endpoint.hostname, "gateway.example")
        self.assertEqual(endpoint.port, 443)
        self.assertEqual(len(endpoint.addresses), 2)

    def test_private_addresses_require_trusted_vpn(self):
        private_addresses = ("10.0.0.7", "172.16.4.8", "192.168.20.9", "fd00::10")
        for address in private_addresses:
            with self.subTest(address=address), self.assertRaises(DastEndpointPolicyError):
                DastEndpointPolicy(trusted_vpn=False).validate(f"https://[{address}]/" if ":" in address else f"https://{address}/")

            endpoint = DastEndpointPolicy(trusted_vpn=True).validate(
                f"https://[{address}]/" if ":" in address else f"https://{address}/",
            )
            self.assertEqual(endpoint.addresses, (address,))

    def test_special_purpose_addresses_are_always_rejected(self):
        forbidden = (
            "0.0.0.0",  # noqa: S104 - intentionally forbidden test input
            "127.0.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "::",
            "::1",
            "fe80::1",
            "ff02::1",
            "::ffff:127.0.0.1",
        )
        for address in forbidden:
            url = f"https://[{address}]/" if ":" in address else f"https://{address}/"
            with self.subTest(address=address), self.assertRaises(DastEndpointPolicyError):
                DastEndpointPolicy(trusted_vpn=True).validate(url)

    def test_mixed_dns_answer_is_rejected(self):
        policy = DastEndpointPolicy(
            trusted_vpn=False,
            resolver=lambda _host, _port: ("93.184.216.34", "169.254.169.254"),
        )

        with self.assertRaises(DastEndpointPolicyError):
            policy.validate("https://gateway.example")

    def test_each_new_pool_is_resolved_and_revalidated(self):
        resolver = SequenceResolver(("93.184.216.34",), ("127.0.0.1",))
        policy = DastEndpointPolicy(trusted_vpn=False, resolver=resolver)

        policy.validate("https://gateway.example")
        with self.assertRaises(DastEndpointPolicyError):
            policy.validate("https://gateway.example")

    def test_invalid_shape_port_and_wildcard_are_rejected(self):
        invalid_urls = (
            "http://gateway.example",
            "https://user:password@gateway.example",
            "https://gateway.example?destination=metadata",
            "https://gateway.example#fragment",
            "https://gateway.example:8443",
            "https://*.example",
        )
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(DastEndpointPolicyError):
                DastEndpointPolicy(
                    trusted_vpn=False,
                    resolver=lambda _host, _port: ("93.184.216.34",),
                ).validate(url)

    def test_redirect_status_is_rejected(self):
        for status_code in (301, 302, 307, 308):
            with self.subTest(status_code=status_code), self.assertRaises(DastEndpointPolicyError):
                DastEndpointPolicy.reject_redirect(status_code)

        DastEndpointPolicy.reject_redirect(200)
