"""
Unit tests for the warm per-VPN egress feature (aist/integrations/egress.py).

Docker and network calls are mocked; these tests assert the lifecycle *policy*
(reuse vs. start, idle reaping, LRU cap, VPN resolution) rather than real
containers.  DB-backed API routing tests live alongside the blob endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from aist.integrations import egress
from aist.models import OrgIntegrationType, RepositoryInfo, ScmType
from aist.tasks.egress import prewarm_egress, reap_egress
from aist.test.source_version_fixtures import FakeScmHost


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
        with (
            patch("aist.integrations.egress._is_running", return_value=False),
            self.assertRaises(RuntimeError),
        ):
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
        vpn_integration = SimpleNamespace(
            id=7,
            pk=7,
            integration_type=OrgIntegrationType.VPN,
            is_active=vpn_active,
            organization_id=vpn_org,
            config={"profile": "scm-vpn"},
        )
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
        with patch("aist.integrations.egress.resolve_integration", return_value=None):
            self.assertIsNone(egress.vpn_integration_for_project_version(pv))

    def test_inactive_scm_vpn_falls_back_to_none(self):
        pv = self._pv_with_scm_vpn(vpn_active=False)
        with patch("aist.integrations.egress.resolve_integration", return_value=None):
            self.assertIsNone(egress.vpn_integration_for_project_version(pv))

    def test_repository_clones_go_through_the_scm_bound_vpn_before_the_project_default(self):
        project = self._pv_with_scm_vpn(vpn_active=True).project
        with patch("aist.integrations.egress.resolve_integration") as project_default:
            resolved = egress.resolve_source_vpn(project)

        self.assertEqual(resolved.integration.pk, 7)
        self.assertEqual(resolved.config, {"profile": "scm-vpn"})
        project_default.assert_not_called()

    def test_a_scm_vpn_that_may_not_route_the_project_falls_back_to_the_project_default(self):
        project = self._pv_with_scm_vpn(vpn_active=True, proj_org=1, vpn_org=2).project
        default = SimpleNamespace(integration=SimpleNamespace(pk=8), config={})
        with patch("aist.integrations.egress.resolve_integration", return_value=default):
            self.assertIs(egress.resolve_source_vpn(project), default)

    def test_proxy_url_for_project_version(self):
        pv = self._pv_with_scm_vpn(vpn_active=True)
        self.assertEqual(egress.proxy_url_for_project_version(pv), "http://aist-vpn-egress-7:1080")

    def test_no_repo_no_vpn(self):
        pv = SimpleNamespace(project=SimpleNamespace(repository=None, organization_id=1))
        with patch("aist.integrations.egress.resolve_integration", return_value=None):
            self.assertIsNone(egress.proxy_url_for_project_version(pv))


class TaskContractTests(SimpleTestCase):

    """DefectDojo's DojoAsyncTask injects `async_user_id` into every task call."""

    def test_prewarm_accepts_async_user_id(self):
        with patch("aist.models.AISTProjectVersion") as pv_model:
            pv_model.objects.select_related.return_value.filter.return_value.first.return_value = None
            # Call the task body directly with the kwarg the wrapper injects.
            self.assertIsNone(prewarm_egress.run(1, async_user_id=1))

    def test_reap_accepts_async_user_id(self):
        with patch("aist.integrations.egress.reap_idle", return_value=0):
            self.assertEqual(reap_egress.run(async_user_id=1), 0)


class RepositoryReadProxyTests(SimpleTestCase):

    """`RepositoryInfo.read_file` routes through the egress proxy only when given one."""

    def _repository(self):
        return RepositoryInfo(
            type=ScmType.GITLAB, repo_owner="acme", repo_name="widgets", base_url="https://gitlab.internal.example",
        )

    def test_passes_proxies_when_proxy_url_set(self):
        with FakeScmHost(body=b"data") as host:
            data = self._repository().read_file("main", "f.py", proxy_url="http://p:1080")
        self.assertEqual(data, b"data")
        self.assertEqual(host.last.proxies["https"], "http://p:1080")

    def test_no_proxies_without_proxy_url(self):
        with FakeScmHost(body=b"data") as host:
            self._repository().read_file("main", "f.py")
        self.assertEqual(host.last.proxies, {})
