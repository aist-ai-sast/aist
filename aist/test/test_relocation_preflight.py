"""Relocation must stop when workers or the broker still own work."""
import runpy
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from aist.test import PROJECT_ROOT


class RelocationPreflightTests(SimpleTestCase):
    def test_queued_work_and_missing_worker_replies_block_relocation(self):
        script = PROJECT_ROOT / "infra/ansible/roles/aist_relocation/files/check_quiescent.py"
        for condition in ("idle", "active", "reserved", "scheduled", "unresponsive", "queued", "unacked"):
            with self.subTest(condition=condition):
                inspector = Mock()
                for name in ("active", "reserved", "scheduled"):
                    getattr(inspector, name).return_value = {"worker": []}
                if condition in {"active", "reserved", "scheduled"}:
                    getattr(inspector, condition).return_value = {"worker": [{"id": "existing-job"}]}
                if condition == "unresponsive":
                    inspector.active.return_value = None
                broker = Mock()
                broker.scan_iter.return_value = [b"celery"]
                broker.type.return_value = b"list"
                broker.llen.return_value = int(condition == "queued")
                broker.hlen.return_value = int(condition == "unacked")
                with (
                    patch("celery.current_app.control.inspect", return_value=inspector),
                    patch("redis.Redis.from_url", return_value=broker),
                    patch.dict("os.environ", {"DD_CELERY_BROKER_URL": "redis://fixture:6379/0"}),
                ):
                    if condition == "idle":
                        runpy.run_path(str(script))
                    else:
                        with self.assertRaises(RuntimeError):
                            runpy.run_path(str(script))
