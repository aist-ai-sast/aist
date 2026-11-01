"""
Every ``{% url %}`` name our templates use resolves.

A vendor upgrade can retire a view, and a template naming it then raises
``NoReverseMatch`` only when a user opens the page that renders it.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_URL_TAG = re.compile(r"""{%\s*url\s+['"]([a-zA-Z0-9_:\-]+)['"]""")
_V3_GUARD = re.compile(r"{%\s*if\s+V3_FEATURE_LOCATIONS\s*%}(.*?){%\s*endif\s*%}", re.DOTALL)

# Routes the platform mounts only when DD_V3_FEATURE_LOCATIONS is on, which AIST keeps off
# because DAST deduplication reads Finding.endpoints. A template may name one only inside
# the matching guard, which is what the second test below enforces.
CONDITIONALLY_MOUNTED = frozenset({"api_v3:openapi-view", "api_v3_reference"})


def _url_names() -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for path in _TEMPLATES.rglob("*.html"):
        for name in _URL_TAG.findall(path.read_text(encoding="utf-8")):
            names.setdefault(name, set()).add(str(path.relative_to(_TEMPLATES)))
    return names


class TemplateUrlNameTests(SimpleTestCase):
    def test_our_templates_name_only_registered_views(self):
        unresolvable = {}
        for name, files in _url_names().items():
            if name in CONDITIONALLY_MOUNTED:
                continue
            try:
                reverse(name)
            except NoReverseMatch as exc:
                # "not a valid view function or pattern name" means the name is gone;
                # a complaint about arguments means it exists and simply needs them.
                if "not a valid view function or pattern name" in str(exc):
                    unresolvable[name] = sorted(files)
        self.assertEqual(unresolvable, {}, f"template url names no longer registered: {unresolvable}")

    def test_a_conditionally_mounted_route_is_named_only_behind_its_flag(self):
        """Without the guard the url tag would raise on every page while the flag is off."""
        for path in _TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            guarded = "".join(_V3_GUARD.findall(text))
            for name in CONDITIONALLY_MOUNTED:
                if text.count(f"'{name}'") != guarded.count(f"'{name}'"):
                    self.fail(f"{path.name} names {name} outside an V3_FEATURE_LOCATIONS guard")

    def test_the_scan_actually_reads_templates(self):
        """A silent zero would make the check above pass for the wrong reason."""
        self.assertGreater(len(_url_names()), 20)
