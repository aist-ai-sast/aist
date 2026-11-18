"""Shared response projections for project build configuration revisions."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from aist.models import AISTProjectScript, VersionType
from aist.models.projects import ScriptSource


class AISTProjectScriptSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, allow_null=True,
    )

    class Meta:
        model = AISTProjectScript
        fields = ["id", "sha256", "base_image", "created_at", "created_by_id", "created_by_username"]


class AISTProjectScriptContentSerializer(AISTProjectScriptSerializer):
    class Meta(AISTProjectScriptSerializer.Meta):
        fields = [*AISTProjectScriptSerializer.Meta.fields, "content", "is_shared"]


class AISTProjectResolvedScriptSerializer(AISTProjectScriptContentSerializer):
    inherited = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta(AISTProjectScriptContentSerializer.Meta):
        fields = [*AISTProjectScriptContentSerializer.Meta.fields, "inherited", "source"]

    def get_inherited(self, obj) -> bool:
        return self.get_source(obj) != ScriptSource.VERSION

    @extend_schema_field(serializers.ChoiceField(choices=ScriptSource.choices))
    def get_source(self, obj) -> ScriptSource:
        return self.context["source"]


class ActiveScriptVersionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    version = serializers.CharField()
    type = serializers.ChoiceField(source="version_type", choices=VersionType.choices)


class AISTProjectActiveScriptSerializer(AISTProjectResolvedScriptSerializer):
    version = serializers.SerializerMethodField()

    class Meta(AISTProjectResolvedScriptSerializer.Meta):
        fields = [*AISTProjectResolvedScriptSerializer.Meta.fields, "version"]

    @extend_schema_field(ActiveScriptVersionSerializer(allow_null=True))
    def get_version(self, obj):
        version = self.context["version"]
        return ActiveScriptVersionSerializer(version).data if version is not None else None
