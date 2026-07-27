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
    addresses: tuple[str, ...]


class DastEndpointPolicy:

    """Fail-closed SSRF policy for DAST onboarding and catalog traffic."""

    HTTPS_PORT: ClassVar[int] = 443
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
        if port != self.HTTPS_PORT:
            msg = "DAST gateway must use HTTPS port 443."
            raise DastEndpointPolicyError(msg)

        hostname = parsed.hostname.rstrip(".").lower()
        if not hostname or "*" in hostname or "%" in hostname:
            msg = "DAST gateway hostname is invalid."
            raise DastEndpointPolicyError(msg)
        addresses = self._resolve_addresses(hostname, port)
        for address in addresses:
            self._validate_address(address)
        return ValidatedDastEndpoint(
            url=gateway_url,
            hostname=hostname,
            port=port,
            addresses=addresses,
        )

    @staticmethod
    def reject_redirect(status_code: int) -> None:
        if 300 <= status_code < 400:
            msg = "DAST gateway redirects are not allowed."
            raise DastEndpointPolicyError(msg)

    def _resolve_addresses(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                raw_addresses = tuple(self._resolver(hostname, port))
            except (OSError, socket.gaierror) as exc:
                msg = "DAST gateway hostname could not be resolved safely."
                raise DastEndpointPolicyError(msg) from exc
        else:
            raw_addresses = (str(literal),)

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
        address = ipaddress.ip_address(raw_address)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
            msg = "DAST gateway resolves to a forbidden local or special-purpose address."
            raise DastEndpointPolicyError(msg)
        if address.is_global:
            return
        trusted_networks = self._TRUSTED_VPN_IPV4 if address.version == 4 else self._TRUSTED_VPN_IPV6
        if self.trusted_vpn and any(address in network for network in trusted_networks):
            return
        msg = "Non-public DAST gateway addresses require a trusted VPN route."
        raise DastEndpointPolicyError(msg)

    @staticmethod
    def _resolve_with_system_dns(hostname: str, port: int) -> tuple[str, ...]:
        return tuple(
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
