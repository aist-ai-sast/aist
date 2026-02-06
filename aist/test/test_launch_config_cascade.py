from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject, AISTProjectLaunchConfig, LaunchSchedule, PipelineLaunchQueue


class LaunchConfigCascadeTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA default for tests")
        self.prod_type = Product_Type.objects.create(name="PT for tests")
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )

        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=[],
            script_path="/tmp/aist.sh",  # noqa: S108
            compilable=False,
        )
        self.launch_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="cfg-1",
            description="",
            params={"project_version": {"id": 1}},
        )
        self.schedule = LaunchSchedule.objects.create(
            launch_config=self.launch_config,
            cron_expression="0 1 * * *",
            enabled=True,
            max_concurrent_per_worker=1,
        )
        self.queue = PipelineLaunchQueue.objects.create(
            project=self.project,
            schedule=self.schedule,
            launch_config=self.launch_config,
        )

    def test_delete_launch_config_cascades_schedule_and_queue(self):
        self.launch_config.delete()

        self.assertFalse(LaunchSchedule.objects.filter(id=self.schedule.id).exists())
        self.assertFalse(PipelineLaunchQueue.objects.filter(id=self.queue.id).exists())

    def test_delete_project_cascades_launch_configs(self):
        self.project.delete()
        self.assertFalse(AISTProjectLaunchConfig.objects.filter(id=self.launch_config.id).exists())
