from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase

from aist.execution.retry import LaunchRetryPolicy, capacity_backoff_seconds


class LaunchRetryPolicyTests(SimpleTestCase):
    def test_capacity_backoff_grows_exponentially_and_stops_at_cap(self):
        policy = LaunchRetryPolicy(
            base_delay_seconds=30,
            max_delay_seconds=120,
            max_age=timedelta(hours=1),
        )

        with patch("aist.execution.retry._JITTER_RANDOM.randint", side_effect=lambda _lower, upper: upper):
            delays = [
                capacity_backoff_seconds(retry_count=retry_count, policy=policy)
                for retry_count in range(1, 6)
            ]

        self.assertEqual(delays, [30, 60, 120, 120, 120])

    def test_equal_jitter_never_allows_zero_delay(self):
        policy = LaunchRetryPolicy(
            base_delay_seconds=30,
            max_delay_seconds=120,
            max_age=timedelta(hours=1),
        )

        with patch("aist.execution.retry._JITTER_RANDOM.randint", side_effect=lambda lower, _upper: lower):
            delay = capacity_backoff_seconds(retry_count=1, policy=policy)

        self.assertEqual(delay, 15)
