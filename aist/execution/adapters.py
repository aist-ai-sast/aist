from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from aist.execution.contracts import (
    ExecutionCancellationMode,
    ExecutionMetricDescriptor,
    ExecutionPlan,
    LaunchPlanningContext,
    PipelineExecutionKind,
)
from aist.execution.observability import register_execution_metric_descriptor

if TYPE_CHECKING:
    from aist.models import AISTPipeline


class PipelineExecutionRuntime(Protocol):
    def run_sast(self, pipeline_id: str): ...

    def run_dast(self, pipeline_id: str): ...


class LaunchAdapterRegistryError(LookupError):

    """Base error for invalid or unavailable launch-adapter registrations."""


class DuplicateLaunchAdapterError(LaunchAdapterRegistryError):

    """Raised when two adapters claim the same execution type."""


class UnknownLaunchAdapterError(LaunchAdapterRegistryError):

    """Raised before pipeline creation when an execution type has no adapter."""


class IncompleteExecutionDriverError(LaunchAdapterRegistryError):

    """Raised when a driver omits a control-plane lifecycle operation."""


class PipelineLaunchAdapter(Protocol):
    execution_type: PipelineExecutionKind
    metric_descriptor: ExecutionMetricDescriptor
    cancellation_mode: ExecutionCancellationMode

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        """Build one immutable plan without creating or publishing a pipeline."""
        ...

    def initialize_pipeline(self, pipeline: AISTPipeline) -> None:
        """Create any typed provider runtime state in the planning transaction."""
        ...

    def allows_duplicate_delivery(self, *, pipeline_id: str, task_id: str | None, retries: int) -> bool:
        """Authorize a provider-owned retry of an already accepted broker delivery."""
        ...

    def should_recover(self, pipeline: AISTPipeline) -> bool:
        """Return whether a dead worker may be republished through the generic task."""
        ...

    def invoke(self, runtime: PipelineExecutionRuntime, pipeline_id: str):
        """Invoke the provider through the worker-owned runtime boundary."""
        ...


class LaunchAdapterRegistry:

    def __init__(self, *adapters: PipelineLaunchAdapter):
        self._adapters: dict[PipelineExecutionKind, PipelineLaunchAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: PipelineLaunchAdapter) -> None:
        required_methods = (
            "build_plan",
            "initialize_pipeline",
            "allows_duplicate_delivery",
            "should_recover",
            "invoke",
        )
        if any(not callable(getattr(adapter, method, None)) for method in required_methods):
            message = "Execution driver must implement planning, initialization, retry, and recovery hooks."
            raise IncompleteExecutionDriverError(message)
        try:
            execution_type = PipelineExecutionKind(adapter.execution_type)
            descriptor = adapter.metric_descriptor
            ExecutionCancellationMode(adapter.cancellation_mode)
        except (AttributeError, TypeError, ValueError) as exc:
            message = "Execution driver descriptor is incomplete or invalid."
            raise IncompleteExecutionDriverError(message) from exc
        required_operations = {"execute", "cancel"}
        if not required_operations.issubset(descriptor.operations):
            message = "Execution driver metrics must describe execute and cancel operations."
            raise IncompleteExecutionDriverError(message)
        if execution_type in self._adapters:
            message = f"Launch adapter already registered for execution type {execution_type.value}."
            raise DuplicateLaunchAdapterError(message)
        self._adapters[execution_type] = adapter
        register_execution_metric_descriptor(descriptor)

    def resolve(self, execution_type: PipelineExecutionKind | str) -> PipelineLaunchAdapter:
        try:
            normalized_type = PipelineExecutionKind(execution_type)
            return self._adapters[normalized_type]
        except (KeyError, ValueError):
            message = f"No launch adapter registered for execution type {execution_type!s}."
            raise UnknownLaunchAdapterError(message) from None

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        return self.resolve(context.execution_type).build_plan(context)

    def initialize_pipeline(self, pipeline: AISTPipeline) -> None:
        self.resolve(pipeline.execution_type).initialize_pipeline(pipeline)
