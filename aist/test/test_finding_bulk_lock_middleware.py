from __future__ import annotations

from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from aist.findings_bulk_lock import acquire_bulk_locks, release_bulk_locks
from aist_site.middleware import AistFindingBulkLockMiddleware


class AistFindingBulkLockMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AistFindingBulkLockMiddleware(lambda _request: HttpResponse("ok"))

    def test_blocks_vendor_finding_patch_when_locked(self):
        acquire_bulk_locks([123], "owner-token")
        request = self.factory.patch("/api/v2/findings/123/")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 423)
        self.assertIn("locked", response.content.decode("utf-8").lower())
        release_bulk_locks([123])

    def test_blocks_vendor_finding_close_when_locked(self):
        acquire_bulk_locks([124], "owner-token")
        request = self.factory.post("/api/v2/findings/124/close/")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 423)
        release_bulk_locks([124])

    def test_allows_non_locked_finding_requests(self):
        request = self.factory.patch("/api/v2/findings/125/")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_read_requests_even_when_locked(self):
        acquire_bulk_locks([126], "owner-token")
        request = self.factory.get("/api/v2/findings/126/")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        release_bulk_locks([126])

    def test_blocks_delete_when_locked(self):
        acquire_bulk_locks([127], "owner-token")
        request = self.factory.delete("/api/v2/findings/127/")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 423)
        release_bulk_locks([127])

    def test_allows_delete_when_not_locked(self):
        request = self.factory.delete("/api/v2/findings/128/")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_put_when_locked(self):
        acquire_bulk_locks([129], "owner-token")
        request = self.factory.put("/api/v2/findings/129/")

        response = self.middleware(request)

        self.assertEqual(response.status_code, 423)
        release_bulk_locks([129])
