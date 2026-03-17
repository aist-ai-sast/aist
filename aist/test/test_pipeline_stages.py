from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.http import JsonResponse
from django.test import RequestFactory, TestCase

from aist.models import AISTStatus
from aist.tasks.ai import push_request_to_ai as _push_request_to_ai
from aist.tasks.dedup import watch_deduplication as _watch_deduplication
from aist.tasks.enrich import after_upload_enrich_and_watch as _after_upload_enrich_and_watch
from aist.tasks.enrich import make_enrich_chord as _make_enrich_chord
from aist.utils.pipeline import finish_pipeline
from aist.views.ai import send_request_to_ai as _send_request_to_ai

# ---- Messages / constants ----------------------------------------------------

MSG_EXPECTED_SIGNATURE = "expected a non-None signature"

# ---- Helpers ----------------------------------------------------------------


class DummyLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def exception(self, *a, **kw): pass


def _mk_pipeline(**overrides):
    defaults = {
        "id": "pipeline-123",
        "status": "UNKNOWN",
        "updated": None,
        "logs": "",
        "project": SimpleNamespace(product=SimpleNamespace(name="Prod")),
        "launch_data": {},
        "tests": MagicMock(),
        "save": MagicMock(),
        "refresh_from_db": MagicMock(),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)

# ---- after_upload_enrich_and_watch ------------------------------------------


def _call_after_upload_enrich(*, enriched_count_list, pipeline, test_ids, log_level="INFO"):
    with patch("aist.tasks.enrich.install_pipeline_logging", return_value=DummyLogger()), \
         patch("aist.tasks.enrich.AISTPipeline") as mock_model, \
         patch("aist.tasks.ai.auto_push_to_ai_if_configured") as mock_auto_push, \
         patch("aist.tasks.regression.detect_regressions_for_pipeline") as mock_regression:

        mock_model.objects.select_for_update().get.return_value = pipeline
        mock_model.objects.get.return_value = pipeline

        _after_upload_enrich_and_watch(
            results=enriched_count_list,
            pipeline_id=pipeline.id,
            test_ids=test_ids,
            log_level=log_level,
        )
        return mock_auto_push, mock_regression, pipeline


class AfterUploadEnrichTests(TestCase):
    def test_sets_waiting_confirmation_and_triggers_regression(self):
        pipeline = _mk_pipeline(status="FINDING_POSTPROCESSING")
        test_ids = [10, 20, 30]

        mock_auto_push, mock_regression, pipeline = _call_after_upload_enrich(
            enriched_count_list=[1, 0, 1],
            pipeline=pipeline,
            test_ids=test_ids,
        )

        self.assertEqual(pipeline.status, "WAITING_CONFIRMATION_TO_PUSH_TO_AI")
        mock_regression.assert_called_once()
        mock_auto_push.delay.assert_not_called()

    def test_auto_pushes_when_ai_mode_auto_default(self):
        pipeline = _mk_pipeline(
            status="FINDING_POSTPROCESSING",
            launch_data={"ai": {"mode": "AUTO_DEFAULT", "filter_snapshot": {"limit": 10}}},
        )

        mock_auto_push, _, pipeline = _call_after_upload_enrich(
            enriched_count_list=[1],
            pipeline=pipeline,
            test_ids=[5],
        )

        self.assertEqual(pipeline.status, "WAITING_CONFIRMATION_TO_PUSH_TO_AI")
        mock_auto_push.delay.assert_called_once_with(pipeline.id)

# ---- watch_deduplication ----------------------------------------------------


def _call_watch_dedup(*, pipeline, progress_qs=None, remaining_counts=None, duplicate_exists_seq=None):
    mock_chord_sig = MagicMock()
    with patch("aist.tasks.dedup.install_pipeline_logging", return_value=DummyLogger()), \
         patch("aist.tasks.dedup.AISTPipeline") as mock_model, \
         patch("aist.tasks.dedup.TestDeduplicationProgress") as mock_progress, \
         patch("aist.tasks.dedup.AISTTestMeta") as mock_meta, \
         patch("aist.tasks.dedup.Finding") as mock_finding, \
         patch("aist.tasks.enrich.make_enrich_chord", return_value=mock_chord_sig):
        mock_model.objects.get.return_value = pipeline
        mock_model.objects.select_for_update.return_value.get.return_value = pipeline
        if progress_qs is None:
            progress_qs = MagicMock()
            progress_qs.filter.return_value = progress_qs
            progress_qs.exclude.return_value = progress_qs
            progress_qs.exists.return_value = False
            progress_qs.values_list.return_value = []
            progress_qs.__iter__.return_value = iter([])
        mock_progress.objects.filter.return_value = progress_qs
        mock_progress.objects.bulk_create.return_value = []
        if remaining_counts is None:
            remaining_counts = [0]
        mock_meta_qs = MagicMock()
        mock_meta_qs.count.side_effect = [*remaining_counts, remaining_counts[-1]]
        mock_meta.objects.filter.return_value = mock_meta_qs
        mock_meta.objects.bulk_create.return_value = []
        if duplicate_exists_seq is None:
            duplicate_exists_seq = [False]
        mock_findings_qs = MagicMock()
        mock_findings_qs.exists.side_effect = [*duplicate_exists_seq, duplicate_exists_seq[-1]]
        mock_finding.objects.filter.return_value = mock_findings_qs
        _watch_deduplication.run(pipeline_id=pipeline.id, log_level="INFO")
        return pipeline, mock_chord_sig


class WatchDeduplicationTests(TestCase):
    def test_no_tests_finishes_immediately(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = False
        tests_mgr.values_list.return_value = []
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        with patch("aist.tasks.dedup.finish_pipeline") as mock_finish:
            _call_watch_dedup(pipeline=pipeline)
        mock_finish.assert_called_once_with(pipeline.id, degraded=True)

    def test_complete_dedup_triggers_enrich(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 0
        tests_mgr.values_list.return_value = [1]
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        with self.captureOnCommitCallbacks(execute=True):
            pipeline, mock_chord_sig = _call_watch_dedup(pipeline=pipeline, remaining_counts=[0])

        self.assertEqual(pipeline.status, "FINDING_POSTPROCESSING")
        mock_chord_sig.apply_async.assert_called_once()

    def test_stale_retries_exhausted_triggers_enrich(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 1
        tests_mgr.values_list.return_value = [1]
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        progress_qs = MagicMock()
        mock_started = MagicMock()
        mock_started.exists.return_value = False
        mock_attempts = MagicMock()
        mock_attempts.exists.return_value = True

        def _filter_side_effect(*args, **kwargs):
            if "started_at__lt" in kwargs:
                return mock_started
            if "reconcile_attempts__gte" in kwargs and "last_progress_at__lt" in kwargs:
                return mock_attempts
            return progress_qs

        progress_qs.filter.side_effect = _filter_side_effect
        progress_qs.exclude.return_value = progress_qs
        progress_qs.exists.return_value = False
        progress_qs.values_list.return_value = [1]
        progress_qs.__iter__.return_value = iter([])
        progress_qs.update = MagicMock()

        with self.captureOnCommitCallbacks(execute=True):
            pipeline, mock_chord_sig = _call_watch_dedup(
                pipeline=pipeline,
                progress_qs=progress_qs,
                remaining_counts=[1],
            )
        self.assertEqual(pipeline.status, "FINDING_POSTPROCESSING")
        mock_chord_sig.apply_async.assert_called_once()

    def test_timeout_triggers_enrich(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 1
        tests_mgr.values_list.return_value = [1]
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        progress_qs = MagicMock()
        mock_started = MagicMock()
        mock_started.exists.return_value = True
        mock_attempts = MagicMock()
        mock_attempts.exists.return_value = False

        def _filter_side_effect(*args, **kwargs):
            if "started_at__lt" in kwargs:
                return mock_started
            if "reconcile_attempts__gte" in kwargs and "last_progress_at__lt" in kwargs:
                return mock_attempts
            return progress_qs

        progress_qs.filter.side_effect = _filter_side_effect
        progress_qs.exclude.return_value = progress_qs
        progress_qs.exists.return_value = False
        progress_qs.values_list.return_value = [1]
        progress_qs.__iter__.return_value = iter([])
        progress_qs.update = MagicMock()

        with self.captureOnCommitCallbacks(execute=True):
            pipeline, mock_chord_sig = _call_watch_dedup(
                pipeline=pipeline,
                progress_qs=progress_qs,
                remaining_counts=[1],
            )
        self.assertEqual(pipeline.status, "FINDING_POSTPROCESSING")
        mock_chord_sig.apply_async.assert_called_once()

    def test_retries_exhausted_triggers_enrich(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 1
        tests_mgr.values_list.return_value = [1]
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        progress_qs = MagicMock()
        mock_started = MagicMock()
        mock_started.exists.return_value = False
        mock_attempts = MagicMock()
        mock_attempts.exists.return_value = True

        def _filter_side_effect(*args, **kwargs):
            if "started_at__lt" in kwargs:
                return mock_started
            if "reconcile_attempts__gte" in kwargs and "last_progress_at__lt" in kwargs:
                return mock_attempts
            return progress_qs

        progress_qs.filter.side_effect = _filter_side_effect
        progress_qs.exclude.return_value = progress_qs
        progress_qs.exists.return_value = False
        progress_qs.values_list.return_value = [1]
        progress_qs.__iter__.return_value = iter([])
        progress_qs.update = MagicMock()

        with self.captureOnCommitCallbacks(execute=True):
            pipeline, mock_chord_sig = _call_watch_dedup(
                pipeline=pipeline,
                progress_qs=progress_qs,
                remaining_counts=[1],
            )
        self.assertEqual(pipeline.status, "FINDING_POSTPROCESSING")
        mock_chord_sig.apply_async.assert_called_once()

    @patch("aist.tasks.dedup.async_dupe_delete")
    def test_waits_for_duplicate_cleanup_before_enrich(self, mock_async_dupe_delete):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 0
        tests_mgr.values_list.return_value = [1]
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        with self.captureOnCommitCallbacks(execute=True):
            pipeline, mock_chord_sig = _call_watch_dedup(
                pipeline=pipeline,
                remaining_counts=[0],
                duplicate_exists_seq=[True, False],
            )

        self.assertEqual(pipeline.status, "FINDING_POSTPROCESSING")
        mock_async_dupe_delete.delay.assert_called_once_with()
        mock_chord_sig.apply_async.assert_called_once()

# ---- push_request_to_ai -----------------------------------------------------


class PushRequestToAITests(TestCase):
    def test_does_not_push_when_not_ready(self):
        with patch("aist.tasks.ai.requests.post") as mock_post, \
             patch("aist.tasks.ai.install_pipeline_logging", return_value=DummyLogger()), \
             patch("aist.tasks.ai.finish_pipeline") as mock_finish, \
             patch("aist.tasks.ai.AISTPipeline") as mock_model:
            pipeline = _mk_pipeline(status="WAITING_CONFIRMATION_TO_PUSH_TO_AI")
            sfu = mock_model.objects.select_for_update.return_value
            sfu.select_related.return_value.get.return_value = pipeline
            sfu.get.return_value = pipeline

            _push_request_to_ai.run(pipeline_id=pipeline.id, finding_ids=[1, 2], filters={}, log_level="INFO")

            mock_post.assert_not_called()
            mock_finish.assert_called_once_with(pipeline.id, degraded=True)

    def test_push_success_transitions_to_waiting_result(self):
        with patch("aist.tasks.ai.requests.post") as mock_post, \
             patch("aist.tasks.ai.install_pipeline_logging", return_value=DummyLogger()), \
             patch("aist.tasks.ai.AISTPipeline") as mock_model:
            pipeline = _mk_pipeline(status="PUSH_TO_AI")
            sfu = mock_model.objects.select_for_update.return_value
            sfu.select_related.return_value.get.return_value = pipeline
            sfu.get.return_value = pipeline

            ok_resp = SimpleNamespace(status_code=202, text="ok", raise_for_status=lambda: None)
            mock_post.return_value = ok_resp

            _push_request_to_ai.run(
                pipeline_id=pipeline.id,
                finding_ids=[11, 22],
                filters={"analyzers": ["a"]},
                log_level="INFO",
            )

            self.assertEqual(pipeline.status, "WAITING_RESULT_FROM_AI")
            pipeline.save.assert_any_call(update_fields=["status", "updated"])

    def test_http_failure_calls_finish_pipeline(self):
        with patch("aist.tasks.ai.requests.post") as mock_post, \
             patch("aist.tasks.ai.install_pipeline_logging", return_value=DummyLogger()), \
             patch("aist.tasks.ai.finish_pipeline") as mock_finish, \
             patch("aist.tasks.ai.AISTPipeline") as mock_model:
            pipeline = _mk_pipeline(status="PUSH_TO_AI")
            sfu = mock_model.objects.select_for_update.return_value
            sfu.select_related.return_value.get.return_value = pipeline
            sfu.get.return_value = pipeline

            mock_post.side_effect = ConnectionError("timeout")

            _push_request_to_ai.run(
                pipeline_id=pipeline.id,
                finding_ids=[1],
                filters={},
                log_level="INFO",
            )

            mock_finish.assert_called_once_with(pipeline.id, degraded=True)

# ---- send_request_to_ai (view) ----------------------------------------------


class SendRequestToAITests(TestCase):
    def test_send_request_pushes_when_confirmed(self):
        with patch("aist.views.ai.send_request_to_ai_for_pipeline") as mock_delegate, \
             patch("aist.views.ai.get_authorized_aist_pipelines") as mock_authorized_qs, \
             patch("aist.views.ai.user_has_permission_or_403") as mock_perm_check:

            rf = RequestFactory()
            body = b'{"pipeline_id":"pipeline-123","finding_ids":[1,2,3],"filters":{"analyzers":["X"]}}'
            req = rf.post("/aist/send-request/pipeline-123/", data=body, content_type="application/json")
            req.user = SimpleNamespace(is_authenticated=True)

            pipeline = _mk_pipeline(status="WAITING_CONFIRMATION_TO_PUSH_TO_AI")
            mock_delegate.return_value = JsonResponse({"ok": True})
            authorized_qs = MagicMock()
            authorized_qs.select_related.return_value = authorized_qs
            authorized_qs.get.return_value = pipeline
            mock_authorized_qs.return_value = authorized_qs

            resp = _send_request_to_ai(req, pipeline_id="pipeline-123")

            self.assertEqual(resp.status_code, 200)
            mock_perm_check.assert_called_once()
            mock_delegate.assert_called_once_with(req, pipeline)

# ---- make_enrich_chord progress ---------------------------------------------


def test_make_enrich_chord_initializes_progress():
    """Ensure progress hash is initialized from DB-fetched findings."""
    pipeline = _mk_pipeline(
        id="pipeline-xyz",
        launch_data={
            "trim_path": "",
            "log_level": "INFO",
            "project_version_descriptor": {},
        },
    )
    pipeline.tests = MagicMock()
    pipeline.tests.values_list.return_value = [101]

    with patch("aist.tasks.enrich.AISTPipeline") as mock_pipeline_model, \
         patch("aist.tasks.enrich.Finding") as mock_finding_model, \
         patch("aist.tasks.enrich.get_redis") as mock_get_redis, \
         patch("aist.tasks.enrich.install_pipeline_logging", return_value=DummyLogger()):
        mock_pipeline_model.objects.get.return_value = pipeline
        mock_finding_model.objects.filter.return_value.values_list.return_value = [1, 2, 3]
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        sig = _make_enrich_chord(pipeline_id="pipeline-xyz")

        if sig is None:
            msg = MSG_EXPECTED_SIGNATURE
            raise AssertionError(msg)
        mock_redis.hset.assert_called_with(
            "aist:progress:pipeline-xyz:enrich",
            mapping={"total": 3, "done": 0},
        )


def test_make_enrich_chord_uses_post_dedup_finding_ids():
    """Ensure make_enrich_chord re-fetches finding IDs from DB (post-dedup)."""
    pipeline = _mk_pipeline(
        id="pipeline-dedup",
        launch_data={
            "trim_path": "/src/",
            "log_level": "DEBUG",
            "project_version_descriptor": {"version": "abc"},
        },
    )
    pipeline.tests = MagicMock()
    pipeline.tests.values_list.return_value = [10, 20]

    with patch("aist.tasks.enrich.AISTPipeline") as mock_pipeline_model, \
         patch("aist.tasks.enrich.Finding") as mock_finding_model, \
         patch("aist.tasks.enrich.get_redis") as mock_get_redis, \
         patch("aist.tasks.enrich.install_pipeline_logging", return_value=DummyLogger()):
        mock_pipeline_model.objects.get.return_value = pipeline
        # Simulate 5 findings surviving dedup (originally could be more)
        mock_finding_model.objects.filter.return_value.values_list.return_value = [1, 2, 3, 4, 5]
        mock_get_redis.return_value = MagicMock()

        _make_enrich_chord(pipeline_id="pipeline-dedup")

        # Verify Finding was queried for test_ids from pipeline.tests
        mock_finding_model.objects.filter.assert_called_once_with(test_id__in=[10, 20])


def test_make_enrich_chord_empty_findings_returns_direct_callback():
    """When no findings survive dedup, return a direct callback signature (not a chord)."""
    pipeline = _mk_pipeline(
        id="pipeline-empty",
        launch_data={"log_level": "INFO", "project_version_descriptor": {}},
    )
    pipeline.tests = MagicMock()
    pipeline.tests.values_list.return_value = [5]

    with patch("aist.tasks.enrich.AISTPipeline") as mock_pipeline_model, \
         patch("aist.tasks.enrich.Finding") as mock_finding_model, \
         patch("aist.tasks.enrich.get_redis") as mock_get_redis, \
         patch("aist.tasks.enrich.install_pipeline_logging", return_value=DummyLogger()):
        mock_pipeline_model.objects.get.return_value = pipeline
        mock_finding_model.objects.filter.return_value.values_list.return_value = []
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        sig = _make_enrich_chord(pipeline_id="pipeline-empty")

        if sig is None:
            msg = MSG_EXPECTED_SIGNATURE
            raise AssertionError(msg)
        # Redis progress must NOT be set when there are no findings
        mock_redis.hset.assert_not_called()


class FinishPipelineTests(TestCase):
    def test_absorbs_reconciliation_exception_without_propagating(self):
        """finish_pipeline must not raise even if reconciliation raises."""
        mock_pipeline = MagicMock()
        with patch("aist.utils.pipeline.reconcile_pipeline_orphans", side_effect=RuntimeError("boom")), \
             patch("aist.utils.pipeline.AISTPipeline") as mock_model, \
             patch("aist.utils.pipeline.set_pipeline_status"), \
             patch("aist.utils.pipeline.uninstall_pipeline_file_logging"):
            mock_model.objects.select_for_update.return_value.get.return_value = mock_pipeline
            # Should not raise
            finish_pipeline("pipe-x", degraded=False)

    def test_absorbs_status_save_exception_without_propagating(self):
        """finish_pipeline must not raise even if the status save fails."""
        with patch("aist.utils.pipeline.reconcile_pipeline_orphans", return_value={"remaining_violations": 0}), \
             patch("aist.utils.pipeline.AISTPipeline") as mock_model, \
             patch("aist.utils.pipeline.uninstall_pipeline_file_logging"):
            mock_model.objects.select_for_update.return_value.get.side_effect = RuntimeError("db error")
            # Should not raise
            finish_pipeline("pipe-x", degraded=False)

    def test_degraded_flag_forces_finished_with_warnings(self):
        """degraded=True must result in FINISHED_WITH_WARNINGS regardless of reconciliation."""
        mock_pipeline = MagicMock()
        with patch("aist.utils.pipeline.reconcile_pipeline_orphans", return_value={"remaining_violations": 0}), \
             patch("aist.utils.pipeline.AISTPipeline") as mock_model, \
             patch("aist.utils.pipeline.set_pipeline_status") as mock_set_status, \
             patch("aist.utils.pipeline.uninstall_pipeline_file_logging"):
            mock_model.objects.select_for_update.return_value.get.return_value = mock_pipeline
            finish_pipeline("pipe-x", degraded=True)
            mock_set_status.assert_called_once_with(mock_pipeline, AISTStatus.FINISHED_WITH_WARNINGS)
