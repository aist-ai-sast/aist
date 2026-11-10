from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidURL

from aist.integrations.dast_endpoint_policy import ValidatedDastEndpoint


class PinnedGatewayAdapter(HTTPAdapter):

    """Connect to an approved IP while checking TLS against the configured gateway name."""

    def __init__(self, endpoint: ValidatedDastEndpoint, **kwargs):
        if not endpoint.addresses:
            msg = "A direct DAST transport requires approved addresses"
            raise InvalidURL(msg)
        self.endpoint = endpoint
        super().__init__(**kwargs)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if not self.endpoint.matches_origin(request.url):
            msg = "Request does not belong to the configured DAST gateway"
            raise InvalidURL(msg)
        if proxies:
            msg = "A direct DAST transport cannot use a proxy"
            raise InvalidURL(msg)
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self.endpoint.addresses[0]
        pool_kwargs["assert_hostname"] = self.endpoint.hostname
        pool_kwargs["server_hostname"] = self.endpoint.hostname
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)

    def add_headers(self, request, **kwargs):
        super().add_headers(request, **kwargs)
        request.headers["Host"] = urlsplit(self.endpoint.url).netloc
