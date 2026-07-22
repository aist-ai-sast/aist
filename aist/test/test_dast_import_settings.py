"""Unit test for the generic report-import upload size limit."""
from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase


class PipelineImportSettingsTests(SimpleTestCase):
    def test_max_size_bytes_default(self):
        self.assertEqual(settings.PIPELINE_IMPORT_MAX_SIZE_BYTES, 15 * 1024 * 1024)
