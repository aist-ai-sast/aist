import socket

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

    def test_direct_route_accepts_alternate_https_port_8443(self):
        policy = DastEndpointPolicy(
            trusted_vpn=False,
            resolver=lambda _host, _port: ("93.184.216.34",),
        )

        endpoint = policy.validate("https://gateway.example:8443")

        self.assertEqual(endpoint.port, 8443)

    def test_vpn_routed_name_with_no_local_answer_is_accepted_unverified(self):
        # A VPN-internal name has no answer outside the tunnel, and the sidecar's proxy is what
        # resolves it for the real connection. Refusing it here would reject a legitimate gateway.
        def no_local_answer(_hostname, _port):
            raise socket.gaierror(-2, "Name or service not known")

        policy = DastEndpointPolicy(trusted_vpn=True, resolver=no_local_answer)

        endpoint = policy.validate("https://gateway.internal.example:8443")

        self.assertEqual(endpoint.hostname, "gateway.internal.example")
        self.assertEqual(endpoint.port, 8443)
        # Empty means "the proxy owns this lookup", not "no addresses".
        self.assertEqual(endpoint.addresses, ())

    def test_vpn_routed_name_resolving_inside_the_vpn_is_not_authorized_locally(self):
        policy = DastEndpointPolicy(trusted_vpn=True, resolver=lambda _host, _port: ("10.2.42.7",))

        self.assertEqual(policy.validate("https://gateway.internal.example:8443").addresses, ())

    def test_vpn_routed_name_with_an_oversized_answer_set_is_refused(self):
        # Checking only the first answers would let whoever owns the zone pad the set with harmless
        # records until a forbidden one falls outside the window, defeating the denial above.
        padded = (*(f"93.184.216.{octet}" for octet in range(1, 40)), "127.0.0.1")
        policy = DastEndpointPolicy(trusted_vpn=True, resolver=lambda _host, _port: padded)

        with self.assertRaises(DastEndpointPolicyError):
            policy.validate("https://gateway.internal.example:8443")

    def test_vpn_routed_name_resolving_to_a_special_purpose_address_is_refused(self):
        # A local answer cannot authorize a routed destination, but it can still deny one: a name
        # that resolves to a loopback or metadata address is never a gateway.
        for address in ("127.0.0.1", "169.254.169.254", "0.0.0.0", "::1"):  # noqa: S104 - forbidden test input
            policy = DastEndpointPolicy(trusted_vpn=True, resolver=lambda _host, _port, a=address: (a,))
            with self.subTest(address=address), self.assertRaises(DastEndpointPolicyError):
                policy.validate("https://gateway.internal.example:8443")

    def test_vpn_routed_address_literal_is_still_checked(self):
        # An address literal needs no lookup, so the value checked here is the value connected to --
        # a private one is in range for a trusted VPN, a special-purpose one never is.
        policy = DastEndpointPolicy(trusted_vpn=True)

        self.assertEqual(policy.validate("https://10.2.42.7:8443").addresses, ("10.2.42.7",))
        with self.assertRaises(DastEndpointPolicyError):
            policy.validate("https://169.254.169.254:8443")

    def test_direct_route_name_is_still_resolved_and_restricted_to_public(self):
        policy = DastEndpointPolicy(trusted_vpn=False, resolver=lambda _host, _port: ("10.2.42.7",))

        with self.assertRaises(DastEndpointPolicyError):
            policy.validate("https://gateway.internal.example")

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
            "https://gateway.example:8080",
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
