# Import backends so they register themselves
import aist.work_items.backends.github  # noqa: F401
import aist.work_items.backends.gitlab  # noqa: F401
import aist.work_items.backends.jira  # noqa: F401
from aist.work_items.backends.registry import get_backend, register_backend

__all__ = ["get_backend", "register_backend"]
