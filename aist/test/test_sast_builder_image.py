from __future__ import annotations

import re

from dojo.models import Product

from aist.models import AISTPipeline, AISTProject, AISTProjectScript, AISTProjectVersion, VersionType
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.test_api import AISTApiBase

# The name component grammar of a Docker image reference: lowercase alphanumerics joined by separators.
_IMAGE_NAME = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")


class SastBuilderImageTests(SastWorkerTestMixin, AISTApiBase):

    def test_worker_passes_versioned_base_image_as_an_explicit_runtime_argument(self):
        base_image = "ubuntu:24.04@sha256:" + "a" * 64
        script = AISTProjectScript.objects.create(
            project=self.project, content="echo configured", base_image=base_image,
        )
        version = AISTProjectVersion.objects.create(project=self.project, version="configured", script=script)
        pipeline = AISTPipeline.objects.create(id="builder-base-image", project=self.project, project_version=version)
        run = self.run_sast(pipeline, project_version=version, upload_results=[])
        arguments = run.execute.call_args.args[1].runtime_arguments
        self.assertEqual(arguments.base_image, base_image)
        self.assertNotIn("SAST_BUILDER_BASE_IMAGE", arguments.additional_env)

    def _builder_image_for(self, project: AISTProject, pipeline_id: str) -> str:
        version = AISTProjectVersion.objects.create(project=project, version_type=VersionType.GIT_BRANCH, version="main")
        pipeline = AISTPipeline.objects.create(id=pipeline_id, project=project, project_version=version)
        self.project = project
        run = self.run_sast(pipeline, project_version=version, upload_results=[])
        return run.execute.call_args.args[1].runtime_arguments.image_name

    def test_a_project_named_after_its_repository_gets_a_valid_builder_image(self):
        """An SCM import names the product ``owner/Repo``, and a manual project may carry spaces."""
        images = []
        for index, name in enumerate(("acme/Payments-Service", "Payments API")):
            product = Product.objects.create(name=name, description="d", prod_type=self.prod_type)
            project = AISTProject.objects.create(product=product, supported_languages=["python"])
            image = self._builder_image_for(project, f"builder-image-{index}")
            self.assertIsNotNone(_IMAGE_NAME.fullmatch(image), f"{image!r} is not a valid image name")
            images.append(image)

        self.assertEqual(len(set(images)), 2, "each project builds into its own image")
