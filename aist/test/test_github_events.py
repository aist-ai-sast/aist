from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.github_events import on_install_created_or_repos_added, on_pr_event
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    PullRequest,
    RepositoryInfo,
    ScmGithubBinding,
    ScmType,
)


class GithubEventsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="events-user",
            email="events@example.com",
            password="pass",  # noqa: S106
        )
        self.sla, _ = SLA_Configuration.objects.get_or_create(id=1, defaults={"name": "SLA"})
        self.product_type = Product_Type.objects.create(name="PT")
        self.product = Product.objects.create(
            name="owner/repo",
            description="desc",
            prod_type=self.product_type,
            sla_configuration_id=self.sla.id,
        )
        self.repo = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="owner",
            repo_name="repo",
            base_url="https://github.com",
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            script_path="input_projects/default_imported_project_no_built.sh",
            compilable=False,
            profile={},
            repository=self.repo,
        )

    def test_installation_event_updates_existing_binding(self):
        event = SimpleNamespace(
            event="installation_repositories",
            data={
                "action": "added",
                "installation": {"id": 777},
                "repositories_added": [{"full_name": "owner/repo"}],
            },
        )

        async_to_sync(on_install_created_or_repos_added)(event, gh=None)

        binding = ScmGithubBinding.objects.get(scm=self.repo)
        self.assertEqual(binding.installation_id, 777)

    @patch("aist.github_events.run_sast_pipeline")
    def test_pull_request_event_creates_pipeline_for_linked_repository(self, run_task):
        run_task.delay.return_value = SimpleNamespace(id="task-1")

        event = SimpleNamespace(
            event="pull_request",
            data={
                "action": "opened",
                "repository": {"full_name": "owner/repo"},
                "pull_request": {
                    "number": 42,
                    "head": {
                        "sha": "abcdef1234567890",
                        "ref": "feature-branch",
                        "repo": {"full_name": "owner/repo"},
                    },
                    "base": {"ref": "main"},
                },
            },
        )

        with patch("aist.github_events.has_unfinished_pipeline", return_value=False):
            async_to_sync(on_pr_event)(event, gh=None)

        self.assertTrue(AISTProjectVersion.objects.filter(project=self.project, version="abcdef1234567890").exists())
        self.assertTrue(PullRequest.objects.filter(repository=self.repo, pr_number=42).exists())
        run_task.delay.assert_called_once()

    @patch("aist.github_events.run_sast_pipeline")
    def test_pull_request_event_skips_when_repository_not_imported(self, run_task):
        event = SimpleNamespace(
            event="pull_request",
            data={
                "action": "opened",
                "repository": {"full_name": "owner/not-imported"},
                "pull_request": {
                    "number": 1,
                    "head": {
                        "sha": "0123456789abcdef",
                        "ref": "feature",
                        "repo": {"full_name": "owner/not-imported"},
                    },
                    "base": {"ref": "main"},
                },
            },
        )

        async_to_sync(on_pr_event)(event, gh=None)

        run_task.delay.assert_not_called()
