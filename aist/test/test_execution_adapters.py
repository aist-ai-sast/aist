from dataclasses import FrozenInstanceError
from unittest import TestCase

from aist.execution.adapters import (
    DuplicateLaunchAdapterError,
    IncompleteExecutionDriverError,
    LaunchAdapterRegistry,
    UnknownLaunchAdapterError,
)
from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionCancellationMode,
    ExecutionMetricDescriptor,
    ExecutionPlan,
    LaunchAuthority,
    LaunchAuthorityKind,
    LaunchPlanningContext,
    LaunchSource,
    PipelineExecutionKind,
    PipelineTaskName,
)


class FakeLaunchAdapter:

    def __init__(self, execution_type: PipelineExecutionKind, plan: ExecutionPlan):
        self.execution_type = execution_type
        self.cancellation_mode = ExecutionCancellationMode.IMMEDIATE
        self.metric_descriptor = ExecutionMetricDescriptor(
            label=execution_type.value.lower(),
            operations=frozenset({"execute", "cancel"}),
        )
        self.plan = plan
        self.calls: list[LaunchPlanningContext] = []

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        self.calls.append(context)
        return self.plan

    def initialize_pipeline(self, pipeline) -> None:
        del pipeline

    def allows_duplicate_delivery(self, *, pipeline_id: str, task_id: str | None, retries: int) -> bool:
        del pipeline_id, task_id, retries
        return False

    def should_recover(self, pipeline) -> bool:
        del pipeline
        return False

    def invoke(self, runtime, pipeline_id: str):
        return runtime.run_sast(pipeline_id)


class LaunchAdapterRegistryTests(TestCase):

    def setUp(self):
        self.authority = LaunchAuthority(
            kind=LaunchAuthorityKind.SCHEDULE,
            source=LaunchSource.SCHEDULE,
            organization_id=19,
        )
        self.context = LaunchPlanningContext(
            launch_request_id=23,
            execution_type=PipelineExecutionKind.DAST,
            project_id=29,
            trigger_project_version_id=31,
            params_snapshot={"profile": "baseline"},
            capability_snapshot={"revision": "capability-2"},
            authority=self.authority,
        )
        self.plan = ExecutionPlan(
            execution_type=PipelineExecutionKind.DAST,
            task_name=PipelineTaskName.RUN_PIPELINE_EXECUTION,
            task_args=("pipeline-public-id",),
            project_id=29,
            trigger_project_version_id=31,
            effective_version_policy=EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT,
            effective_project_version_id=None,
            resource_key="dast-integration:37",
            resource_limit=1,
            coalesce_key="dast:37:binding-41:sha256-snapshot",
            initial_launch_data={},
            authority=self.authority,
        )

    def test_registry_selects_adapter_and_preserves_complete_plan(self):
        sast_adapter = FakeLaunchAdapter(PipelineExecutionKind.SAST, self.plan)
        dast_adapter = FakeLaunchAdapter(PipelineExecutionKind.DAST, self.plan)
        registry = LaunchAdapterRegistry(sast_adapter, dast_adapter)

        result = registry.build_plan(self.context)

        self.assertIs(result, self.plan)
        self.assertEqual(dast_adapter.calls, [self.context])
        self.assertEqual(sast_adapter.calls, [])
        self.assertEqual(result.task_name, PipelineTaskName.RUN_PIPELINE_EXECUTION)
        self.assertEqual(result.task_args, ("pipeline-public-id",))
        self.assertEqual(result.project_id, 29)
        self.assertEqual(result.trigger_project_version_id, 31)
        self.assertIsNone(result.effective_project_version_id)
        self.assertEqual(result.effective_version_policy, EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT)
        self.assertEqual(result.resource_key, "dast-integration:37")
        self.assertEqual(result.resource_limit, 1)
        self.assertEqual(result.coalesce_key, "dast:37:binding-41:sha256-snapshot")
        self.assertEqual(result.initial_launch_data, {})

    def test_duplicate_registration_fails(self):
        first = FakeLaunchAdapter(PipelineExecutionKind.DAST, self.plan)
        duplicate = FakeLaunchAdapter(PipelineExecutionKind.DAST, self.plan)

        with self.assertRaises(DuplicateLaunchAdapterError):
            LaunchAdapterRegistry(first, duplicate)

    def test_unknown_type_fails_without_invoking_an_adapter(self):
        adapter = FakeLaunchAdapter(PipelineExecutionKind.DAST, self.plan)
        registry = LaunchAdapterRegistry(adapter)

        with self.assertRaises(UnknownLaunchAdapterError):
            registry.resolve("MANUAL_IMPORT")

        self.assertEqual(adapter.calls, [])

    def test_partial_driver_is_rejected_before_it_can_plan_a_pipeline(self):
        partial = type("PartialDriver", (), {
            "execution_type": PipelineExecutionKind.DAST,
            "metric_descriptor": ExecutionMetricDescriptor(
                label="partial",
                operations=frozenset({"execute", "cancel"}),
            ),
            "cancellation_mode": ExecutionCancellationMode.IMMEDIATE,
            "build_plan": lambda _self, _context: None,
        })()

        with self.assertRaises(IncompleteExecutionDriverError):
            LaunchAdapterRegistry(partial)

    def test_execution_plan_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            self.plan.resource_limit = 2
