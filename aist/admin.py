from __future__ import annotations

from django.contrib import admin

from aist.models import OrgIntegrationVPNSecret


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
