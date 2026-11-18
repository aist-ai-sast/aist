"""Project-owned identity attached to the runtime's source revision descriptor."""

from __future__ import annotations

from dataclasses import dataclass, replace

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.build_configuration import SourceRevision  # noqa: E402


@dataclass(frozen=True, slots=True)
class ProjectVersionDescriptor(SourceRevision):
    id: int | None = None

    @classmethod
    def from_dict(cls, value: dict) -> ProjectVersionDescriptor:
        descriptor = super(ProjectVersionDescriptor, cls).from_dict(value)
        return replace(descriptor, id=value.get("id"))

    def as_dict(self) -> dict:
        return {**super(ProjectVersionDescriptor, self).as_dict(), "id": self.id}
