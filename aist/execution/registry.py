"""Single composition root for AIST execution drivers."""

from aist.execution.adapters import LaunchAdapterRegistry
from aist.execution.dast import DastPipelineLaunchAdapter
from aist.execution.sast import SastPipelineLaunchAdapter

execution_driver_registry = LaunchAdapterRegistry(
    SastPipelineLaunchAdapter(),
    DastPipelineLaunchAdapter(),
)
