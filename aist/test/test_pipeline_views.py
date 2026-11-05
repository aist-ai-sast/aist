from __future__ import annotations

import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase


class AISTPipelineDetailFindingsTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.login_as_superuser()
        self.engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.test_type = Test_Type.objects.create(name="Semgrep")
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.pipeline = self.make_pipeline("pipe-views-1", status=AISTStatus.FINISHED)
        self.pipeline.tests.add(self.test)

    def test_pipeline_detail_lists_findings(self):
        finding = Finding.objects.create(
            test=self.test,
            title="SQL Injection",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.pipeline.launch_data = {
            "ai": {
                "filter_snapshot": {
                    "limit": 25,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            },
        }
        self.pipeline.save(update_fields=["launch_data"])

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Findings")
        self.assertContains(resp, finding.title)
        self.assertContains(resp, "Apply AI filter")
        self.assertContains(resp, "View filter JSON")

    def test_pipeline_detail_applies_ai_filter_when_requested(self):
        high = Finding.objects.create(
            test=self.test,
            title="Critical SQL",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        low = Finding.objects.create(
            test=self.test,
            title="Info issue",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        self.pipeline.launch_data = {
            "ai": {
                "filter_snapshot": {
                    "limit": 50,
                    "severity": [{"comparison": "EQUALS", "value": "High"}],
                },
            },
        }
        self.pipeline.save(update_fields=["launch_data"])

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url, data={"apply_ai_filter": "1"})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, high.title)
        self.assertNotContains(resp, low.title)
        self.assertContains(resp, "Clear AI filter")

    def test_pipeline_detail_sorts_by_title(self):
        first = Finding.objects.create(
            test=self.test,
            title="Alpha",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        second = Finding.objects.create(
            test=self.test,
            title="Zulu",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url, data={"findings_sort": "title", "findings_dir": "asc"})

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertLess(body.find(first.title), body.find(second.title))

    def test_pipeline_detail_allows_other_product_for_superuser(self):
        other = self.make_pipeline("pipe-views-other", status=AISTStatus.FINISHED, project_version=self.other_pv)
        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": other.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class AISTPipelineDetailAIModeTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.login_as_superuser()
        self.pipeline = self.make_pipeline("pipe-ai-1", status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)

    def test_pipeline_detail_ai_mode_visibility(self):
        auto = "AUTO_DEFAULT in progress."
        manual = "Push selected findings to AI"
        cases = (
            ("auto with filter", {"mode": "AUTO_DEFAULT", "filter_snapshot": {"limit": 5}}, auto, manual),
            ("auto with local triage", {"mode": "AUTO_DEFAULT", "triage_type": "local"}, auto, manual),
            ("auto that cannot push", {"mode": "AUTO_DEFAULT", "triage_type": "n8n"}, manual, auto),
            ("manual", {"mode": "MANUAL"}, manual, auto),
        )
        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        for case, ai, expected_text, unexpected_text in cases:
            with self.subTest(case=case):
                self.pipeline.launch_data = {"ai": ai}
                self.pipeline.save(update_fields=["launch_data"])
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, expected_text)
                self.assertNotContains(resp, unexpected_text)


LIVE_DURATION = re.compile(r'data-elapsed-seconds="(\d+)" x-data="aistElapsed\(\1\)"')


def _live_seconds(body: str) -> int:
    """The seconds a counting duration was rendered with; the test fails if no duration is counting."""
    match = LIVE_DURATION.search(body)
    if match is None:
        msg = "No counting duration on the page."
        raise AssertionError(msg)
    return int(match.group(1))


class AISTPipelineListTests(AISTApiBase):

    """The pipeline history page and the table it refreshes in place."""

    def setUp(self):
        super().setUp()
        self.login_as_superuser()
        self.url = reverse("aist:pipeline_list")

    def _refresh(self, **params):
        return self.client.get(self.url, data=params, HTTP_HX_REQUEST="true")

    def test_running_pipeline_duration_counts_from_launch_although_its_status_has_not_changed(self):
        launched = timezone.now() - timedelta(hours=2, minutes=3, seconds=4)
        self.make_pipeline(
            "pipe-long-exec",
            status=AISTStatus.EXECUTING,
            created=launched,
            updated=launched + timedelta(seconds=3),
        )

        body = self.client.get(self.url).content.decode()

        elapsed = _live_seconds(body)
        self.assertGreaterEqual(elapsed, 2 * 3600 + 3 * 60 + 4)
        self.assertLess(elapsed, 2 * 3600 + 3 * 60 + 4 + 60)
        self.assertIn(">02:0", body)
        self.assertNotIn("00:00:03", body)

    def test_pipeline_waiting_for_ai_confirmation_keeps_counting(self):
        launched = timezone.now() - timedelta(minutes=40)
        self.make_pipeline(
            "pipe-awaiting-ai",
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
            created=launched,
            updated=launched + timedelta(minutes=5),
        )

        self.assertGreaterEqual(_live_seconds(self.client.get(self.url).content.decode()), 40 * 60)

    def test_finished_pipeline_duration_ends_when_it_finished(self):
        launched = timezone.now() - timedelta(days=1)
        self.make_pipeline(
            "pipe-finished",
            status=AISTStatus.FINISHED,
            created=launched,
            finished_at=launched + timedelta(minutes=10, seconds=5),
            # Touched again after it finished, which must not stretch the run.
            updated=launched + timedelta(hours=3),
        )

        body = self.client.get(self.url).content.decode()

        self.assertIn(">00:10:05<", body)
        self.assertNotIn("aistElapsed(", body)

    def test_terminal_pipeline_without_finish_time_ends_at_its_last_update(self):
        launched = timezone.now() - timedelta(days=30)
        self.make_pipeline(
            "pipe-no-finish-time",
            status=AISTStatus.FINISHED_WITH_WARNINGS,
            created=launched,
            updated=launched + timedelta(minutes=14, seconds=17),
        )

        self.assertIn("00:14:17", self.client.get(self.url).content.decode())

    def test_refresh_returns_only_the_table_for_the_same_filters(self):
        now = timezone.now()
        self.make_pipeline("pipe-mine", status=AISTStatus.EXECUTING, created=now, updated=now)
        self.make_pipeline(
            "pipe-other-project", status=AISTStatus.EXECUTING, created=now, updated=now,
            project_version=self.other_pv,
        )

        resp = self._refresh(project=self.project.id)

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="pipeline-table"', body)
        self.assertNotIn("pipeline-filters-form", body)
        self.assertNotIn("<html", body)
        self.assertIn("pipe-mine", body)
        self.assertNotIn("pipe-other-project", body)
        # The table asks again for the same filtered page it shows.
        self.assertIn(f'hx-get="{self.url}?project={self.project.id}"', body)

    def test_page_and_refresh_are_cached_apart(self):
        for name, resp in (("page", self.client.get(self.url)), ("refresh", self._refresh())):
            with self.subTest(response=name):
                self.assertIn("HX-Request", resp.headers["Vary"])

    def test_refresh_is_quick_while_a_listed_pipeline_runs_and_slow_once_all_finished(self):
        now = timezone.now()
        self.make_pipeline("pipe-done", status=AISTStatus.FINISHED, created=now, updated=now, finished_at=now)
        self.assertIn("every 30s", self._refresh().content.decode())

        self.make_pipeline("pipe-running", status=AISTStatus.EXECUTING, created=now, updated=now)
        self.assertIn("every 5s", self._refresh().content.decode())

    def test_finished_only_filter_hides_running_pipelines(self):
        now = timezone.now()
        self.make_pipeline("pipe-done", status=AISTStatus.FINISHED, created=now, updated=now, finished_at=now)
        self.make_pipeline("pipe-running", status=AISTStatus.EXECUTING, created=now, updated=now)

        body = self.client.get(self.url, data={"status": "FINISHED"}).content.decode()

        self.assertIn("pipe-done", body)
        self.assertNotIn("pipe-running", body)
        self.assertIn('<option value="FINISHED" selected>', body)

    def test_a_malformed_project_filter_lists_every_pipeline_instead_of_failing(self):
        now = timezone.now()
        self.make_pipeline("pipe-any", status=AISTStatus.FINISHED, created=now, updated=now, finished_at=now)

        resp = self.client.get(self.url, data={"project": "abc"})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "pipe-any")

    def test_the_calendar_link_lists_the_pipelines_launched_that_day(self):
        day = timezone.now() - timedelta(days=3)
        self.make_pipeline("pipe-that-day", status=AISTStatus.FINISHED, created=day, updated=day, finished_at=day)
        self.make_pipeline(
            "pipe-day-after", status=AISTStatus.FINISHED,
            created=day + timedelta(days=1), updated=day, finished_at=day,
        )

        body = self.client.get(
            self.url,
            data={
                "project": self.project.id,
                "created_from": timezone.localtime(day).date(),
                "created_to": timezone.localtime(day).date(),
            },
        ).content.decode()

        self.assertIn("pipe-that-day", body)
        self.assertNotIn("pipe-day-after", body)

    def test_page_links_keep_the_filters_and_number_the_pages(self):
        now = timezone.now()
        for index in range(25):
            self.make_pipeline(f"pipe-page-{index:02d}", status=AISTStatus.FINISHED, created=now, updated=now)

        body = self.client.get(self.url, data={"q": "pipe-page", "page_size": 10}).content.decode()

        self.assertIn("?q=pipe-page&amp;page_size=10&amp;page=2", body)
        self.assertIn("?q=pipe-page&amp;page_size=10&amp;page=3", body)

    def test_a_pipeline_finished_with_warnings_has_the_same_badge_in_the_list_and_on_the_start_page(self):
        launched = timezone.now() - timedelta(hours=1)
        self.make_pipeline(
            "pipe-warn", status=AISTStatus.FINISHED_WITH_WARNINGS,
            created=launched, updated=launched, finished_at=launched,
        )
        badge = '<span class="badge bg-amber-500 text-dark">FINISHED_WITH_WARNINGS</span>'

        for page, url in (("list", self.url), ("start", reverse("aist:start_pipeline"))):
            with self.subTest(page=page):
                self.assertIn(badge, self.client.get(url).content.decode())

    def test_start_page_history_shows_run_time_until_finish(self):
        launched = timezone.now() - timedelta(days=2)
        self.make_pipeline(
            "pipe-history",
            status=AISTStatus.FINISHED,
            created=launched,
            finished_at=launched + timedelta(minutes=7),
            updated=launched + timedelta(hours=5),
        )

        self.assertIn("00:07:00", self.client.get(reverse("aist:start_pipeline")).content.decode())

    def test_start_page_view_all_link_keeps_the_history_filters(self):
        now = timezone.now()
        self.make_pipeline("nightly-1", status=AISTStatus.FINISHED, created=now, updated=now, finished_at=now)

        body = self.client.get(
            reverse("aist:start_pipeline"), data={"project": self.project.id, "q": "nightly"},
        ).content.decode()

        self.assertIn(f"{self.url}?project={self.project.id}&amp;q=nightly&amp;page=1", body)


class AISTPipelineDetailStatusPanelTests(AISTApiBase):

    """The status panel of the pipeline page and the refresh that replaces it."""

    def setUp(self):
        super().setUp()
        self.login_as_superuser()

    def _url(self, pipeline):
        return reverse("aist:pipeline_detail", kwargs={"pipeline_id": pipeline.id})

    def test_pipeline_page_shows_the_same_duration_as_the_list_with_the_run_time_in_words(self):
        launched = timezone.now() - timedelta(days=3)
        pipeline = self.make_pipeline(
            "pipe-detail",
            status=AISTStatus.FINISHED,
            created=launched,
            started=launched + timedelta(minutes=2),
            finished_at=launched + timedelta(hours=1, minutes=59, seconds=30),
            updated=launched + timedelta(hours=4),
        )

        listed = self.client.get(reverse("aist:pipeline_list")).content.decode()
        detail = self.client.get(self._url(pipeline)).content.decode()

        for page, body in (("list", listed), ("detail", detail)):
            with self.subTest(page=page):
                self.assertIn('data-elapsed-seconds="7170">01:59:30<', body)
        self.assertIn("(~ 1\xa0hour, 59\xa0minutes)", detail)

    def test_refresh_returns_the_panel_for_the_current_status(self):
        pipeline = self.make_pipeline("pipe-refresh", status=AISTStatus.EXECUTING)

        resp = self.client.get(self._url(pipeline), HTTP_HX_REQUEST="true")

        body = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="pipeline-status-container"', body)
        self.assertIn("The pipeline worker is running", body)
        self.assertNotIn("<html", body)
        self.assertNotIn('id="logbox"', body)
        self.assertIn("HX-Request", resp.headers["Vary"])

    def test_refresh_after_finishing_shows_the_finished_panel(self):
        pipeline = self.make_pipeline("pipe-finishing", status=AISTStatus.EXECUTING)
        self.assertIn("The pipeline worker is running", self.client.get(self._url(pipeline)).content.decode())

        AISTPipeline.objects.filter(pk=pipeline.pk).update(status=AISTStatus.FINISHED, finished_at=timezone.now())
        body = self.client.get(self._url(pipeline), HTTP_HX_REQUEST="true").content.decode()

        self.assertIn('data-hydrator="finished"', body)
        self.assertIn("Pipeline finished.", body)
        self.assertNotIn("The pipeline worker is running", body)

    def test_refresh_is_refused_once_the_user_is_no_longer_a_superuser(self):
        # The user still maintains this project, so only the superuser-only admin guard can refuse it.
        own = self.make_pipeline("pipe-own", status=AISTStatus.EXECUTING)
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])

        resp = self.client.get(self._url(own), HTTP_HX_REQUEST="true")

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("pipeline-status-container", resp.content.decode())

    def test_only_a_pipeline_finished_with_warnings_shows_the_warning(self):
        warning = "Pipeline finished with warnings."
        for status, shown in ((AISTStatus.FINISHED_WITH_WARNINGS, True), (AISTStatus.FINISHED, False)):
            with self.subTest(status=status):
                pipeline = self.make_pipeline(f"pipe-{status.lower()}", status=status, finished_at=timezone.now())

                body = self.client.get(self._url(pipeline)).content.decode()

                self.assertEqual(warning in body, shown)
