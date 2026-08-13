from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.execution.observability import AuditContext, audit_event
from aist.integrations.dast_capability_sync import schedule_dast_capability_sync
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.models import (
    AISTProject,
    DastIntegrationValidationState,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)


class DastTargetSerializer(serializers.ModelSerializer):

    class Meta:
        model = DastTarget
        fields = [
            "id",
            "provider_id",
            "display_name",
            "contract_revision",
            "capability_revision",
            "schema_digest",
            "parameter_schema",
            "provider_defaults",
            "repository_keys",
            "launch_requirements",
            "autonomous_ready",
            "is_available",
            "last_seen_at",
        ]
        read_only_fields = fields


class DastProjectBindingSerializer(serializers.ModelSerializer):

    target = DastTargetSerializer(read_only=True)
    target_id = serializers.PrimaryKeyRelatedField(
        source="target",
        queryset=DastTarget.objects.none(),
        write_only=True,
    )
    capability_revision = serializers.CharField(write_only=True)
    schema_digest = serializers.CharField(write_only=True)
    parameter_snapshot = serializers.DictField()
    readiness = serializers.SerializerMethodField()

    class Meta:
        model = DastProjectBinding
        fields = [
            "id",
            "project",
            "target",
            "target_id",
            "capability_revision",
            "schema_digest",
            "source_repo_key",
            "enabled",
            "parameter_snapshot",
            "readiness",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "project", "target", "readiness", "created", "updated"]

    def get_readiness(self, obj) -> dict:
        return check_dast_binding_readiness(obj).to_snapshot()

    def get_fields(self):
        fields = super().get_fields()
        project = self.context.get("project")
        if project is not None:
            fields["target_id"].queryset = (
                DastTarget.objects
                .filter(
                    integration__organization_id=project.organization_id,
                    integration__integration_type=OrgIntegrationType.DAST,
                    integration__is_active=True,
                    is_available=True,
                )
                .select_related("integration", "integration__dast_state")
            )
        return fields

    def to_internal_value(self, data):
        expected = {
            "target_id",
            "capability_revision",
            "schema_digest",
            "source_repo_key",
            "enabled",
            "parameter_snapshot",
        }
        if not isinstance(data, dict) or set(data) != expected:
            msg = "A complete DAST binding object with no unknown fields is required."
            raise serializers.ValidationError({"non_field_errors": [msg]})
        return super().to_internal_value(data)

    def validate(self, attrs):
        project = self.context["project"]
        target = attrs["target"]
        state = getattr(target.integration, "dast_state", None)
        if state is None or state.validation_state != DastIntegrationValidationState.READY:
            raise serializers.ValidationError({"target_id": "DAST integration is not ready."})
        if attrs.pop("capability_revision") != target.capability_revision:
            raise serializers.ValidationError({"capability_revision": "Target capability changed; reload the catalog."})
        if attrs.pop("schema_digest") != target.schema_digest:
            raise serializers.ValidationError({"schema_digest": "Target schema changed; reload the catalog."})
        candidate = DastProjectBinding(project=project, **attrs)
        try:
            candidate.full_clean(exclude=["id"], validate_unique=False, validate_constraints=False)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs

    def create(self, validated_data):
        project = self.context["project"]
        target = validated_data.pop("target")
        binding, _created = DastProjectBinding.objects.update_or_create(
            project=project,
            target=target,
            defaults=validated_data,
        )
        return binding


class OrganizationDastTargetCatalogAPI(AISTAPIView):

    authz = ResourcePolicy(resource=Organization, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List the synchronized DAST target catalog",
        responses={200: DastTargetSerializer(many=True)},
    )
    def get(self, request, org_id: int):
        organization = self.resolve(pk=org_id)
        targets = DastTarget.objects.filter(
            integration__organization=organization,
            integration__is_active=True,
        ).order_by("provider_id")
        return Response(DastTargetSerializer(targets, many=True).data)


class DastIntegrationCapabilitySyncAPI(AISTAPIView):

    # Synchronizing pulls the catalog from the tenant-supplied gateway URL, so this is an
    # outbound call an operator can trigger on demand. Same bound as the other probe endpoints.
    throttle_scope = "aist_dast_gateway_probe"
    throttle_classes = [ScopedRateThrottle]

    authz = ResourcePolicy(resource=OrgIntegration, read=Action.ORG_MANAGE_READ, write=Action.ORG_MANAGE)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Synchronize DAST target capabilities",
        request=None,
        responses={202: {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    )
    def post(self, request, integration_id: int):
        integration = self.resolve(pk=integration_id)
        if integration.integration_type != OrgIntegrationType.DAST:
            return Response({"detail": "DAST integration required."}, status=status.HTTP_404_NOT_FOUND)
        try:
            ticket = schedule_dast_capability_sync(integration)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"task_id": ticket.task_id}, status=status.HTTP_202_ACCEPTED)


class ProjectDastBindingListCreateAPI(AISTAPIView):

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)
    serializer_class = DastProjectBindingSerializer

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="List project DAST target bindings",
        responses={200: DastProjectBindingSerializer(many=True)},
    )
    def get(self, request, project_id: int):
        project = self.resolve(pk=project_id)
        bindings = project.dast_bindings.select_related(
            "target__integration__dast_state",
            "target__integration__vpn_integration__vpn_secret",
        ).order_by("target__provider_id")
        return Response(DastProjectBindingSerializer(bindings, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.INTEGRATIONS],
        summary="Upsert a project DAST target binding",
        request=DastProjectBindingSerializer,
        responses={
            200: DastProjectBindingSerializer,
            400: OpenApiResponse(description="Invalid or stale binding"),
        },
    )
    def post(self, request, project_id: int):
        project = self.resolve(pk=project_id)
        serializer = DastProjectBindingSerializer(data=request.data, context={"project": project})
        serializer.is_valid(raise_exception=True)
        binding = serializer.save()
        audit_event(
            "dast_binding_saved",
            context=AuditContext(
                organization_id=project.organization_id,
                project_id=project.pk,
                integration_id=binding.target.integration_id,
                binding_id=binding.pk,
                actor_id=request.user.pk,
            ),
        )
        return Response(serializer.data)


class ProjectDastBindingDetailAPI(AISTAPIView):

    authz = ResourcePolicy(resource=DastProjectBinding, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(tags=[AISTApiTag.INTEGRATIONS], summary="Delete a project DAST binding", responses={204: None})
    def delete(self, request, binding_id: int):
        self.resolve(pk=binding_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
