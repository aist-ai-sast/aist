"""Work-item providers, links from findings to work items, and finding annotations."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from dojo.models import Finding
from encrypted_model_fields.fields import EncryptedCharField

from aist.models.organizations import Organization, vpn_link_violations


class WorkItemProviderType(models.TextChoices):
    JIRA = "JIRA", "Jira"
    YOUTRACK = "YOUTRACK", "YouTrack"
    GITHUB = "GITHUB", "GitHub Issues"
    GITLAB = "GITLAB", "GitLab Issues"
    LINEAR = "LINEAR", "Linear"
    AZURE_DEVOPS = "AZURE_DEVOPS", "Azure DevOps"
    GENERIC = "GENERIC", "Generic (URL only)"


class WorkItemProvider(models.Model):

    """
    Connection to an external issue tracker, scoped to an Organization.

    Credentials are stored encrypted. ``provider_config`` holds non-secret
    settings (default project key, field mappings, labels, …).

    When ``sync_enabled=False`` the provider is "manual-only": links can be
    created and displayed but status is never fetched automatically.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="work_item_providers",
    )
    provider_type = models.CharField(max_length=32, choices=WorkItemProviderType.choices)
    name = models.CharField(max_length=255)
    base_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="Leave blank for cloud-hosted instances (e.g. jira.atlassian.net).",
    )
    # Encrypted PAT / API token. GENERIC providers leave this blank.
    api_token = EncryptedCharField(max_length=2048, blank=True, default="")
    # Non-secret configuration: {"default_project_key": "SEC", "labels": ["aist"]}
    provider_config = models.JSONField(default=dict, blank=True)
    sync_enabled = models.BooleanField(
        default=False,
        help_text="Automatically sync work-item status from the tracker.",
    )
    is_active = models.BooleanField(default=True)
    # Optional VPN integration required to reach this provider's endpoint.
    # Must belong to the same organization (validated in the API serializer).
    vpn_integration = models.ForeignKey(
        "OrgIntegration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dependent_work_item_providers",
        limit_choices_to={"integration_type": "VPN"},
        help_text="VPN integration to use when connecting to this provider. Must belong to the same organization.",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "name")]
        ordering = ["organization", "name"]

    def __str__(self) -> str:
        return f"{self.get_provider_type_display()} - {self.name}"

    def clean(self):
        super().clean()
        if self.vpn_integration_id is None:
            return
        violations = vpn_link_violations(self.vpn_integration, organization_id=self.organization_id)
        if violations:
            raise ValidationError({"vpn_integration": violations[0].label})


class WorkItemStatusCategory(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    DONE = "DONE", "Done"
    CANCELLED = "CANCELLED", "Cancelled / Won't Fix"
    UNKNOWN = "UNKNOWN", "Unknown"


class WorkItemLink(models.Model):

    """
    Associates a Finding with an external work item (Jira ticket, GitHub Issue, …).

    ``provider=None`` means a manual link: the user supplied the URL directly,
    no automatic sync is performed.  When a provider is set and
    ``provider.sync_enabled`` is True, a background task will periodically
    refresh ``raw_status`` / ``status_category`` via the tracker API.
    """

    finding = models.ForeignKey(
        Finding,
        on_delete=models.CASCADE,
        related_name="work_item_links",
    )
    provider = models.ForeignKey(
        WorkItemProvider,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # deleting a provider keeps the URL references
        related_name="work_item_links",
    )
    # Tracker-internal ID (e.g. "10042") and human-readable key (e.g. "SEC-42")
    external_id = models.CharField(max_length=255, blank=True)
    external_key = models.CharField(max_length=255, blank=True)
    external_url = models.URLField(max_length=2048)
    # Cached from last sync - shown in the UI without extra API calls
    title = models.CharField(max_length=500, blank=True)
    raw_status = models.CharField(max_length=255, blank=True)
    status_category = models.CharField(
        max_length=16,
        choices=WorkItemStatusCategory.choices,
        default=WorkItemStatusCategory.UNKNOWN,
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        # One finding can have at most one link per (provider, external_key) pair,
        # which allows multi-tracker scenarios (Jira + GitHub Issues on same finding).
        # provider=None links are NOT deduplicated by external_key on the DB level
        # (NULL != NULL in SQL), so application code must guard against duplicates
        # when provider is None.
        unique_together = [("finding", "provider", "external_key")]
        indexes = [
            models.Index(fields=["finding"], name="work_item_link_finding_idx"),
            models.Index(
                fields=["provider", "status_category"],
                name="work_item_link_prov_status_idx",
            ),
        ]

    def __str__(self) -> str:
        label = self.external_key or self.external_url
        return f"WorkItemLink({label})"

    def clean(self):
        super().clean()
        if self.provider_id is None or self.finding_id is None:
            return
        product_type_id = (
            Finding.objects
            .filter(pk=self.finding_id)
            .values_list("test__engagement__product__prod_type_id", flat=True)
            .first()
        )
        finding_organization_id = (
            Organization.objects
            .filter(product_type_id=product_type_id)
            .values_list("id", flat=True)
            .first()
        )
        if finding_organization_id != self.provider.organization_id:
            raise ValidationError({
                "provider": "Provider must belong to the finding's organization.",
            })


class AISTFindingAnnotation(models.Model):

    """Per-finding AIST-level flags that cannot be stored on the vendor Finding model."""

    finding = models.OneToOneField(
        Finding,
        on_delete=models.CASCADE,
        related_name="aist_annotation",
        db_index=True,
    )
    is_regression = models.BooleanField(
        default=False,
        help_text="True when the finding re-appeared after being previously mitigated.",
    )
    regression_detected_at = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Finding annotation"
        indexes = [
            models.Index(fields=["is_regression"], name="aist_annotation_regression_idx"),
        ]

    def __str__(self) -> str:
        return f"Annotation(finding={self.finding_id}, regression={self.is_regression})"
