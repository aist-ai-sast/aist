"""
Unit tests for the warm per-VPN egress feature (aist/integrations/egress.py).

Docker and network calls are mocked; these tests assert the lifecycle *policy*
(reuse vs. start, idle reaping, LRU cap, VPN resolution) rather than real
containers.  DB-backed API routing tests live alongside the blob endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aist.integrations import egress


class EnsureWarmTests(SimpleTestCase):

    def _vpn(self, vpn_id=5, ovpn="client\n"):
        return SimpleNamespace(id=vpn_id, vpn_secret=SimpleNamespace(ovpn_content=ovpn))

    def test_reuses_running_container_without_starting(self):
        with (
            patch("aist.integrations.egress._is_running", return_value=True),
            patch("aist.integrations.egress.vpn.start_named_sidecar") as start,
        ):
            url = egress.ensure_warm(self._vpn(vpn_id=5))
        self.assertEqual(url, "http://aist-vpn-egress-5:1080")
        start.assert_not_called()

    def test_starts_when_not_running(self):
        with (
            patch("aist.integrations.egress._is_running", return_value=False),
            patch("aist.integrations.egress._allowed_ips", return_value=["172.20.0.9"]),
            patch(
                "aist.integrations.egress.vpn.start_named_sidecar",
                return_value="http://aist-vpn-egress-5:1080",
            ) as start,
        ):
            url = egress.ensure_warm(self._vpn(vpn_id=5))
        self.assertEqual(url, "http://aist-vpn-egress-5:1080")
        _args, kwargs = start.call_args
        self.assertEqual(kwargs["allowed_ips"], ["172.20.0.9"])

    def test_raises_without_ovpn_content(self):
        with patch("aist.integrations.egress._is_running", return_value=False):
            with self.assertRaises(RuntimeError):
                egress.ensure_warm(self._vpn(ovpn=""))

    def test_name_collision_race_reuses(self):
        # start fails (name already taken by a concurrent prewarm) but it is now
        # running → treat as success instead of surfacing the error.
        running = iter([False, True])
        with (
            patch("aist.integrations.egress._is_running", side_effect=lambda _n: next(running)),
            patch("aist.integrations.egress._allowed_ips", return_value=[]),
            patch("aist.integrations.egress.vpn.start_named_sidecar", side_effect=RuntimeError("name in use")),
        ):
            url = egress.ensure_warm(self._vpn(vpn_id=5))
        self.assertEqual(url, "http://aist-vpn-egress-5:1080")


class ReapIdleTests(SimpleTestCase):

    def test_reaps_idle_keeps_active(self):
        now = datetime.now(tz=UTC)
        last_used = {
            "aist-vpn-egress-1": now - timedelta(seconds=5000),   # idle → reap
            "aist-vpn-egress-2": now - timedelta(seconds=10),      # active → keep
        }
        with (
            patch("aist.integrations.egress.list_active", return_value=list(last_used)),
            patch("aist.integrations.egress._last_used", side_effect=lambda n: last_used[n]),
            patch("aist.integrations.egress._idle_ttl_seconds", return_value=900),
            patch("aist.integrations.egress._max_warm", return_value=10),
            patch("aist.integrations.egress.vpn.stop_sidecar") as stop,
        ):
            removed = egress.reap_idle()
        self.assertEqual(removed, 1)
        stop.assert_called_once_with("aist-vpn-egress-1")

    def test_lru_eviction_over_cap(self):
        now = datetime.now(tz=UTC)
        # 3 fresh containers, cap 2 → evict the single least-recently-used.
        last_used = {
            "aist-vpn-egress-1": now - timedelta(seconds=30),
            "aist-vpn-egress-2": now - timedelta(seconds=20),
            "aist-vpn-egress-3": now - timedelta(seconds=10),
        }
        with (
            patch("aist.integrations.egress.list_active", return_value=list(last_used)),
            patch("aist.integrations.egress._last_used", side_effect=lambda n: last_used[n]),
            patch("aist.integrations.egress._idle_ttl_seconds", return_value=100000),
            patch("aist.integrations.egress._max_warm", return_value=2),
            patch("aist.integrations.egress.vpn.stop_sidecar") as stop,
        ):
            removed = egress.reap_idle()
        self.assertEqual(removed, 1)
        stop.assert_called_once_with("aist-vpn-egress-1")


class VpnResolutionTests(SimpleTestCase):

    def _pv_with_scm_vpn(self, *, vpn_active, proj_org=1, vpn_org=1):
        vpn_integration = SimpleNamespace(id=7, is_active=vpn_active, organization_id=vpn_org)
        binding = SimpleNamespace(org_integration=SimpleNamespace(vpn_integration=vpn_integration))
        repo = SimpleNamespace(get_binding=lambda: binding)
        return SimpleNamespace(pk=99, project=SimpleNamespace(repository=repo, organization_id=proj_org))

    def test_returns_active_scm_attached_vpn(self):
        pv = self._pv_with_scm_vpn(vpn_active=True)
        self.assertEqual(egress.vpn_integration_for_project_version(pv).id, 7)

    def test_cross_org_binding_is_ignored(self):
        # SCM binding points at a VPN from a different org → must NOT be used;
        # falls through to the org-scoped resolver (mocked to None here).
        pv = self._pv_with_scm_vpn(vpn_active=True, proj_org=1, vpn_org=2)
        with patch("aist.integrations.resolver.resolve_integration", return_value=None):
            self.assertIsNone(egress.vpn_integration_for_project_version(pv))

    def test_inactive_scm_vpn_falls_back_to_none(self):
        pv = self._pv_with_scm_vpn(vpn_active=False)
        with patch("aist.integrations.resolver.resolve_integration", return_value=None):
            self.assertIsNone(egress.vpn_integration_for_project_version(pv))

    def test_proxy_url_for_project_version(self):
        pv = self._pv_with_scm_vpn(vpn_active=True)
        self.assertEqual(egress.proxy_url_for_project_version(pv), "http://aist-vpn-egress-7:1080")

    def test_no_repo_no_vpn(self):
        pv = SimpleNamespace(project=SimpleNamespace(repository=None))
        with patch("aist.integrations.resolver.resolve_integration", return_value=None):
            self.assertIsNone(egress.proxy_url_for_project_version(pv))


class TaskContractTests(SimpleTestCase):

    """DefectDojo's DojoAsyncTask injects `async_user` into every task call."""

    def test_prewarm_accepts_async_user(self):
        from aist.tasks.egress import prewarm_egress

        with patch("aist.models.AISTProjectVersion") as pv_model:
            pv_model.objects.select_related.return_value.filter.return_value.first.return_value = None
            # Call the task body directly with the kwarg the wrapper injects.
            self.assertIsNone(prewarm_egress.run(1, async_user="someone"))

    def test_reap_accepts_async_user(self):
        from aist.tasks.egress import reap_egress

        with patch("aist.integrations.egress.reap_idle", return_value=0):
            self.assertEqual(reap_egress.run(async_user="someone"), 0)


class RemoteBytesProxyTests(SimpleTestCase):

    """`_return_remote_bytes` routes through the proxy only when given one."""

    def _view(self):
        from aist.api.files import ProjectVersionFileBlobAPI

        return ProjectVersionFileBlobAPI()

    def _ok_response(self):
        resp = MagicMock(status_code=200, content=b"data")
        resp.raise_for_status = MagicMock()
        return resp

    def test_passes_proxies_when_proxy_url_set(self):
        with patch("aist.api.files.requests.get", return_value=self._ok_response()) as get:
            self._view()._return_remote_bytes("https://h/f", "f", proxy_url="http://p:1080")
        self.assertEqual(get.call_args.kwargs["proxies"], {"http": "http://p:1080", "https": "http://p:1080"})

    def test_no_proxies_without_proxy_url(self):
        with patch("aist.api.files.requests.get", return_value=self._ok_response()) as get:
            self._view()._return_remote_bytes("https://h/f", "f")
        self.assertIsNone(get.call_args.kwargs["proxies"])
