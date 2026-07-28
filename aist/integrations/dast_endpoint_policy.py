from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit


class DastEndpointPolicyError(ValueError):
    pass


AddressResolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True, slots=True)
class ValidatedDastEndpoint:

    """A gateway destination approved for one newly-created connection pool."""

    url: str
    hostname: str
    port: int
    # Verified addresses. Empty when the destination is a name whose lookup belongs to the VPN
    # sidecar's proxy rather than to this process -- see DastEndpointPolicy._authorized_addresses.
    addresses: tuple[str, ...]


class DastEndpointPolicy:

    """
    Fail-closed SSRF policy for DAST onboarding and catalog traffic.

    URL shape and port are always enforced. How far the destination *address* can be enforced
    depends on who resolves the name for the connection that follows -- see
    :meth:`_authorized_addresses`.
    """

    HTTPS_PORT: ClassVar[int] = 443
    # Deliberately a short enumerated allowlist, not an open range: DAST gateway deployments
    # commonly front their service on 8443 to avoid binding a privileged port, but each addition
    # here is still a reviewed exception to the SSRF policy, not a general "any port" escape hatch.
    ALLOWED_PORTS: ClassVar[frozenset[int]] = frozenset({HTTPS_PORT, 8443})
    MAX_DNS_ANSWERS: ClassVar[int] = 32
    _TRUSTED_VPN_IPV4: ClassVar[tuple[ipaddress.IPv4Network, ...]] = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    _TRUSTED_VPN_IPV6: ClassVar[tuple[ipaddress.IPv6Network, ...]] = (
        ipaddress.ip_network("fc00::/7"),
    )

    def __init__(self, *, trusted_vpn: bool, resolver: AddressResolver | None = None):
        self.trusted_vpn = trusted_vpn
        self._resolver = resolver or self._resolve_with_system_dns

    def validate(self, gateway_url: str) -> ValidatedDastEndpoint:
        parsed = urlsplit(gateway_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            msg = "DAST gateway must be an absolute HTTPS URL without credentials, query, or fragment."
            raise DastEndpointPolicyError(msg)
        try:
            port = parsed.port or self.HTTPS_PORT
        except ValueError as exc:
            msg = "DAST gateway port is invalid."
            raise DastEndpointPolicyError(msg) from exc
        if port not in self.ALLOWED_PORTS:
            msg = "DAST gateway must use HTTPS on port 443 or 8443."
            raise DastEndpointPolicyError(msg)

        hostname = parsed.hostname.rstrip(".").lower()
        if not hostname or "*" in hostname or "%" in hostname:
            msg = "DAST gateway hostname is invalid."
            raise DastEndpointPolicyError(msg)
        return ValidatedDastEndpoint(
            url=gateway_url,
            hostname=hostname,
            port=port,
            addresses=self._authorized_addresses(hostname, port),
        )

    def _authorized_addresses(self, hostname: str, port: int) -> tuple[str, ...]:
        """
        Return the destination addresses this policy was able to verify.

        Which of three cases applies depends on who performs the name lookup for the connection
        that follows this check:

        * An address literal needs no lookup, so what is verified here is exactly what will be
          connected to. It is checked against the address rules.
        * A name on a VPN-routed connection is resolved by the sidecar's proxy, inside the tunnel. A
          lookup here would describe a *different* lookup than the connection uses, and a
          VPN-internal name generally has no answer here at all -- so a local answer can never
          *authorize* such a destination, and the operator-provisioned route stays the boundary for
          where the connection may land. A local answer can still *deny* one, which is what
          :meth:`_deny_locally_resolvable_special_purpose` does. Shape and port are enforced by the
          caller either way.
        * A name on a direct connection is resolved by this process, so the answer the socket will
          use is the answer that gets checked against the public-address requirement.

        An empty result therefore means "not verifiable here", not "no addresses".
        """
        literal = self._as_address_literal(hostname)
        if literal is not None:
            self._validate_address(literal)
            return (literal,)
        if self.trusted_vpn:
            self._deny_locally_resolvable_special_purpose(hostname, port)
            return ()
        addresses = self._resolve_addresses(hostname, port)
        for address in addresses:
            self._validate_address(address)
        return addresses

    @staticmethod
    def _as_address_literal(hostname: str) -> str | None:
        try:
            return str(ipaddress.ip_address(hostname))
        except ValueError:
            return None

    def _deny_locally_resolvable_special_purpose(self, hostname: str, port: int) -> None:
        """
        Refuse a VPN-routed name that this process can already see is not a gateway.

        Authorizing a routed destination from a local answer is not possible -- the proxy performs
        the lookup the connection uses. Refusing one is: a name that resolves here to a loopback,
        link-local, multicast or unspecified address is never a DAST gateway, and rejecting it costs
        no legitimate configuration. A name with no answer here, which is the normal case for a
        VPN-internal zone, is left to the attached route.

        Deliberately best-effort, and not a guarantee: the proxy's resolver may answer differently,
        including between this check and the connection. It removes the easy case, and the port
        allowlist plus the tunnel remain what actually bound the destination.
        """
        try:
            raw_addresses = tuple(self._resolver(hostname, port))
        except (OSError, socket.gaierror):
            return
        if len(raw_addresses) > self.MAX_DNS_ANSWERS:
            # Fail closed instead of checking a prefix: examining only the first answers would let a
            # padded answer set push a forbidden address out of view and defeat this check.
            msg = "DAST gateway DNS answer set exceeds the safety limit."
            raise DastEndpointPolicyError(msg)
        for raw_address in raw_addresses:
            try:
                address = self._normalized_address(raw_address)
            except ValueError:
                # Each answer is judged on its own, so skipping one this process cannot parse hides
                # nothing: a forbidden answer elsewhere in the set is still rejected below. Refusing
                # outright would instead fail a gateway over an unrelated malformed record.
                continue
            self._reject_special_purpose(address)

    @staticmethod
    def reject_redirect(status_code: int) -> None:
        if 300 <= status_code < 400:
            msg = "DAST gateway redirects are not allowed."
            raise DastEndpointPolicyError(msg)

    def _resolve_addresses(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            raw_addresses = tuple(self._resolver(hostname, port))
        except (OSError, socket.gaierror) as exc:
            msg = "DAST gateway hostname could not be resolved safely."
            raise DastEndpointPolicyError(msg) from exc
        if not raw_addresses or len(raw_addresses) > self.MAX_DNS_ANSWERS:
            msg = "DAST gateway DNS answer set is empty or exceeds the safety limit."
            raise DastEndpointPolicyError(msg)
        normalized = []
        for raw_address in raw_addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                msg = "DAST gateway DNS returned an invalid address."
                raise DastEndpointPolicyError(msg) from exc
            normalized_address = str(address)
            if normalized_address not in normalized:
                normalized.append(normalized_address)
        return tuple(normalized)

    def _validate_address(self, raw_address: str) -> None:
        address = self._normalized_address(raw_address)
        self._reject_special_purpose(address)
        if address.is_global:
            return
        trusted_networks = self._TRUSTED_VPN_IPV4 if address.version == 4 else self._TRUSTED_VPN_IPV6
        if self.trusted_vpn and any(address in network for network in trusted_networks):
            return
        msg = "Non-public DAST gateway addresses require a trusted VPN route."
        raise DastEndpointPolicyError(msg)

    @staticmethod
    def _normalized_address(raw_address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        address = ipaddress.ip_address(raw_address)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address

    @staticmethod
    def _reject_special_purpose(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
            msg = "DAST gateway resolves to a forbidden local or special-purpose address."
            raise DastEndpointPolicyError(msg)

    @staticmethod
    def _resolve_with_system_dns(hostname: str, port: int) -> tuple[str, ...]:
        return tuple(
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
