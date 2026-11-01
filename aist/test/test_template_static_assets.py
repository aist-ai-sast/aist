"""
Every asset our templates ask for is still shipped by the platform.

A platform upgrade can drop a frontend package while the template that links it still
renders fine: ``{% static %}`` only builds a URL, so the page returns 200 and the browser
is the first thing to notice. DefectDojo 3.x retired its Bootstrap 3 bundle exactly this
way, and every admin page lost its stylesheet without one failing request on the server.

An asset is accepted when the running image can find it, when the platform's own manifest
still declares the package it comes from (those are installed only in the image that
collects static files), or when it is named below as something that image builds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase

_AIST = Path(__file__).resolve().parent.parent
_TEMPLATES = _AIST / "templates"
_MANIFEST = _AIST.parent / "vendor" / "defectdojo" / "components" / "package.json"
# Only literal paths; a {% static some_variable %} is resolved at render time.
_STATIC_TAG = re.compile(r"""{%\s*static\s+['"]([^'"]+)['"]""")


# Produced by the stylesheet build in Dockerfile.nginx-alpine, so it exists only in the
# image that serves static files.
BUILD_PRODUCED = frozenset({"aist/css/aist-tailwind.css"})


def _declared_packages() -> set[str]:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {*manifest.get("dependencies", {}), *manifest.get("devDependencies", {})}


def _package_of(asset: str) -> str:
    """The npm package an asset path comes from, scoped names included."""
    parts = asset.split("/")
    return "/".join(parts[:2]) if asset.startswith("@") else parts[0]


def _static_paths() -> dict[str, set[str]]:
    paths: dict[str, set[str]] = {}
    for path in _TEMPLATES.rglob("*.html"):
        for asset in _STATIC_TAG.findall(path.read_text(encoding="utf-8")):
            paths.setdefault(asset, set()).add(str(path.relative_to(_TEMPLATES)))
    return paths


class TemplateStaticAssetTests(SimpleTestCase):
    def test_every_asset_our_templates_link_is_still_shipped(self):
        declared = _declared_packages()
        orphaned = {
            asset: sorted(files)
            for asset, files in _static_paths().items()
            if asset not in BUILD_PRODUCED
            and finders.find(asset) is None
            and _package_of(asset) not in declared
        }
        self.assertEqual(
            orphaned, {},
            f"templates link assets from packages the platform no longer ships: {orphaned}",
        )

    def test_a_build_produced_asset_is_named_by_a_template(self):
        """A stale entry here would quietly excuse an asset nothing links any more."""
        linked = set(_static_paths())
        self.assertEqual(BUILD_PRODUCED - linked, set(), "named build output is no longer linked")

    def test_the_scan_actually_reads_templates(self):
        """A silent zero would make the check above pass for the wrong reason."""
        self.assertGreater(len(_static_paths()), 10)

    def test_the_manifest_is_actually_read(self):
        """An empty manifest would make every asset look orphaned, or none."""
        self.assertIn("jquery", _declared_packages())

    def test_a_retired_package_is_detected(self):
        """Bootstrap 3 is what the platform dropped; the check must still recognise it."""
        self.assertNotIn("bootstrap", _declared_packages())
        self.assertEqual(_package_of("bootstrap/dist/css/bootstrap.min.css"), "bootstrap")
        self.assertIsNone(finders.find("bootstrap/dist/css/bootstrap.min.css"))
