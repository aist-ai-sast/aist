from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

from celery.utils.time import get_exponential_backoff_interval

_ERR_BASE_DELAY = "Launch retry base delay must be positive."
_ERR_MAX_DELAY = "Launch retry maximum delay must not be shorter than the base delay."
_ERR_MAX_AGE = "Launch request maximum age must be positive."
_ERR_RETRY_COUNT = "Launch retry count must be positive."
LAUNCH_MAX_AGE_EXCEEDED = "LAUNCH_MAX_AGE_EXCEEDED"
LAUNCH_MAX_AGE_FAILURE_DETAIL = "The launch request exceeded its maximum queue age."


class LaunchRetryPolicyError(ValueError):

    """Raised when dispatcher retry metadata is invalid."""


@dataclass(frozen=True, slots=True)
class LaunchRetryPolicy:

    base_delay_seconds: int
    max_delay_seconds: int
    max_age: timedelta

    def __post_init__(self) -> None:
        if self.base_delay_seconds < 1:
            raise LaunchRetryPolicyError(_ERR_BASE_DELAY)
        if self.max_delay_seconds < self.base_delay_seconds:
            raise LaunchRetryPolicyError(_ERR_MAX_DELAY)
        if self.max_age <= timedelta(0):
            raise LaunchRetryPolicyError(_ERR_MAX_AGE)


DEFAULT_LAUNCH_RETRY_POLICY = LaunchRetryPolicy(
    base_delay_seconds=30,
    max_delay_seconds=15 * 60,
    max_age=timedelta(hours=24),
)

_JITTER_RANDOM = random.SystemRandom()


def capacity_backoff_seconds(
    *,
    retry_count: int,
    policy: LaunchRetryPolicy = DEFAULT_LAUNCH_RETRY_POLICY,
) -> int:
    """Return equal-jitter Celery exponential backoff with a non-zero lower bound."""
    if retry_count < 1:
        raise LaunchRetryPolicyError(_ERR_RETRY_COUNT)
    exponential_delay = get_exponential_backoff_interval(
        factor=policy.base_delay_seconds,
        retries=retry_count - 1,
        maximum=policy.max_delay_seconds,
        full_jitter=False,
    )
    minimum_delay = max(1, exponential_delay // 2)
    return _JITTER_RANDOM.randint(minimum_delay, exponential_delay)
