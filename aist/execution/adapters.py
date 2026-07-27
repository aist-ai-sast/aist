from __future__ import annotations

from typing import Protocol

from aist.execution.contracts import ExecutionPlan, LaunchPlanningContext, PipelineExecutionKind


class LaunchAdapterRegistryError(LookupError):

    """Base error for invalid or unavailable launch-adapter registrations."""


class DuplicateLaunchAdapterError(LaunchAdapterRegistryError):

    """Raised when two adapters claim the same execution type."""


class UnknownLaunchAdapterError(LaunchAdapterRegistryError):

    """Raised before pipeline creation when an execution type has no adapter."""


class PipelineLaunchAdapter(Protocol):
    execution_type: PipelineExecutionKind

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        """Build one immutable plan without creating or publishing a pipeline."""
        ...


class LaunchAdapterRegistry:

    def __init__(self, *adapters: PipelineLaunchAdapter):
        self._adapters: dict[PipelineExecutionKind, PipelineLaunchAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: PipelineLaunchAdapter) -> None:
        execution_type = adapter.execution_type
        if execution_type in self._adapters:
            message = f"Launch adapter already registered for execution type {execution_type.value}."
            raise DuplicateLaunchAdapterError(message)
        self._adapters[execution_type] = adapter

    def resolve(self, execution_type: PipelineExecutionKind | str) -> PipelineLaunchAdapter:
        try:
            normalized_type = PipelineExecutionKind(execution_type)
            return self._adapters[normalized_type]
        except (KeyError, ValueError):
            message = f"No launch adapter registered for execution type {execution_type!s}."
            raise UnknownLaunchAdapterError(message) from None

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        return self.resolve(context.execution_type).build_plan(context)
