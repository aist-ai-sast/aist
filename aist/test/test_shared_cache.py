"""
The cache every process shares.

A DAST queue command is sent by a Celery worker and its receipt is collected by the web process that
accepted the click, so the store between them cannot be one process's memory. These tests pin the
rule that decides where that store is: it follows the Celery broker, which is the Valkey the
deployment already runs.
"""

from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase

from aist.utils.cache import QUEUE_CACHE_ALIAS, shared_cache_location

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"


class SharedCacheLocationTests(SimpleTestCase):
    def test_the_deployments_valkey_broker_yields_a_cache_beside_it(self):
        # docker-compose hands every process redis://valkey:6379/0 as the broker.
        self.assertEqual(
            shared_cache_location("redis://valkey:6379/0"), "redis://valkey:6379/1",
        )

    def test_a_broker_with_credentials_and_tls_keeps_them(self):
        self.assertEqual(
            shared_cache_location("rediss://dojo:secret@valkey.internal:6380/3"),
            "rediss://dojo:secret@valkey.internal:6380/4",
        )

    def test_a_broker_without_a_database_still_leaves_the_first_one_to_celery(self):
        self.assertEqual(shared_cache_location("redis://valkey:6379"), "redis://valkey:6379/1")

    def test_a_sqlite_broker_has_no_cache_to_offer(self):
        # How the unit-test stack runs: no Valkey container at all.
        self.assertEqual(shared_cache_location("sqla+sqlite:///tmp/dojo.celerydb.sqlite"), "")

    def test_a_unix_socket_broker_is_not_mistaken_for_a_database_number(self):
        self.assertEqual(shared_cache_location("redis+socket:///var/run/valkey.sock"), "")
        self.assertEqual(shared_cache_location("redis://valkey:6379/queue"), "")


class ConfiguredCacheTests(SimpleTestCase):
    def test_the_queue_cache_is_shared_wherever_the_broker_is(self):
        # Per-process memory is the fallback for a stack without Valkey, never a choice made while
        # one is running: with it, a command's receipt is written where nobody can read it.
        configured = settings.CACHES[QUEUE_CACHE_ALIAS]
        if settings.AIST_CACHE_URL:
            self.assertEqual(configured["BACKEND"], "django.core.cache.backends.redis.RedisCache")
            self.assertEqual(configured["LOCATION"], settings.AIST_CACHE_URL)
        else:
            self.assertEqual(configured["BACKEND"], LOCMEM)

    def test_request_throttling_keeps_the_cache_it_had(self):
        # Throttling reads the default cache and has no handling for one that is down, so the queue
        # surface does not drag it onto Valkey: a Valkey outage must not answer 500 on sign-in.
        self.assertEqual(settings.CACHES["default"]["BACKEND"], LOCMEM)
