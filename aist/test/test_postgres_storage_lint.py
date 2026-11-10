"""Configuration lint: reject mounts outside PG18's cluster and test overlays retaining production storage."""

from pathlib import Path

import yaml
from django.test import SimpleTestCase

from aist.test import PROJECT_ROOT


class PostgresStorageLintTests(SimpleTestCase):
    def test_postgres_18_cluster_is_inside_the_configured_persistent_volume(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        cluster_path = Path("/var/lib/postgresql/18/docker")
        mounts = compose["services"]["postgres"]["volumes"]

        self.assertEqual(len(mounts), 1)
        source, target = mounts[0]["source"], mounts[0]["target"]
        self.assertTrue(cluster_path.is_relative_to(target), f"{cluster_path} is outside {target}")
        self.assertIn("AIST_POSTGRES_DATA_DIR", compose["volumes"][source]["driver_opts"]["device"])

    def test_test_stacks_replace_the_production_postgres_mount(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        production_mount = compose["services"]["postgres"]["volumes"][0]
        for filename in ("docker-compose.tests.yml", "docker-compose.integration.yml"):
            with self.subTest(filename=filename):
                overlay = yaml.load((PROJECT_ROOT / filename).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
                mounts = overlay["services"]["postgres"]["volumes"]
                self.assertEqual(len(mounts), 1)
                source, mount = mounts[0]["source"], mounts[0]
                self.assertEqual(mount["target"], production_mount["target"])
                self.assertNotEqual(source, production_mount["source"])
                self.assertEqual(overlay["volumes"][source], {})
