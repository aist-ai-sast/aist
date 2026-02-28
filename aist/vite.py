from __future__ import annotations

from urllib.parse import urljoin

from django_vite.core.asset_loader import DjangoViteAppClient


class AistDjangoViteAppClient(DjangoViteAppClient):

    """Serve Vite assets directly from nginx paths (/assets/*), bypassing STATIC_URL."""

    def get_production_server_url(self, path: str) -> str:
        production_server_url = path
        if prefix := self.static_url_prefix:
            if not prefix.endswith("/"):
                prefix += "/"
            production_server_url = urljoin(prefix, path)
        if not production_server_url.startswith("/"):
            production_server_url = f"/{production_server_url}"
        return production_server_url
