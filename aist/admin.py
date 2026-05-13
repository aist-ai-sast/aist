from __future__ import annotations

from django.contrib import admin

from aist.models import OrgIntegration, OrgIntegrationVPNSecret


@admin.register(OrgIntegration)
class OrgIntegrationAdmin(admin.ModelAdmin):

    """
    Superuser-only management surface for OrgIntegration rows.

    The primary management surface is the REST API + client-ui page
    (see ``aist/api/org_integrations.py`` and
    ``client-ui/src/pages/OrgIntegrationsPage.tsx``). This admin is the
    fallback used by AIST operators when REST-level access is broken
    (DNS/auth misconfiguration) and direct DB-row editing is needed.

    Per CLAUDE-integration architectural invariants, ``secret`` is an
    ``EncryptedCharField`` that already shields the plaintext at rest;
    the admin excludes it from list/detail render to prevent accidental
    on-screen leaks and only exposes a boolean indicator.

    To rotate a token via admin, use the standard "change" form — the
    encrypted column is masked, but assignment works through the
    underlying ORM as usual.
    """

    list_display = (
        "organization", "integration_type", "name", "is_active",
        "has_secret", "created", "updated",
    )
    list_filter = ("integration_type", "is_active", "organization")
    search_fields = ("name", "organization__name")
    readonly_fields = ("created", "updated", "has_secret")
    exclude = ("secret",)

    @admin.display(boolean=True, description="Secret stored")
    def has_secret(self, obj):
        return bool(obj.secret)


@admin.register(OrgIntegrationVPNSecret)
class OrgIntegrationVPNSecretAdmin(admin.ModelAdmin):

    """
    Read-only admin view for VPN secret presence indicators.

    Credential fields are excluded from the admin entirely — they are
    write-only and must be managed via the REST API.  The admin only shows
    boolean flags so operators can confirm which credentials are stored.
    """

    list_display = ("integration", "has_ovpn_content", "has_client_cert", "has_client_key", "has_username")
    readonly_fields = ("integration", "has_ovpn_content", "has_client_cert", "has_client_key", "has_username")
    # Exclude all encrypted credential fields from the form
    exclude = (
        "ovpn_content",
        "ca_cert",
        "client_cert",
        "client_key",
        "tls_auth_key",
        "vpn_username",
        "vpn_password",
    )

    def has_add_permission(self, request):
        return False

    @admin.display(boolean=True, description="OVPN content")
    def has_ovpn_content(self, obj):
        return bool(obj.ovpn_content)

    @admin.display(boolean=True, description="Client cert")
    def has_client_cert(self, obj):
        return bool(obj.client_cert)

    @admin.display(boolean=True, description="Client key")
    def has_client_key(self, obj):
        return bool(obj.client_key)

    @admin.display(boolean=True, description="Username")
    def has_username(self, obj):
        return bool(obj.vpn_username)
