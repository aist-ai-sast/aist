"""
Our admin pages drive components the way the platform still supports.

DefectDojo 3.x stopped shipping the Bootstrap bundle. Its plugin API — the ``modal``
jQuery method, the ``bs.modal`` events, and the ``data-toggle``/``data-dismiss``
attributes — is gone, but markup using it still renders: a modal written as a plain div
simply shows up in the page, open and unclosable. Nothing fails server-side, so this is
checked here rather than left to a browser.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

# Our own pages. base.html is the platform's template with our navigation added, so its
# remaining platform markup is the platform's business.
_PAGES = Path(__file__).resolve().parent.parent / "templates" / "aist"

RETIRED_PLUGIN_API = {
    # Every plugin the bundle used to provide: the markup still renders, the behaviour
    # does not. Tabs failed this way after the modals did, so the whole family is listed.
    "bootstrap data-toggle": re.compile(
        r'data-(?:bs-)?toggle="(?:modal|collapse|tab|dropdown|tooltip|popover|button)"'),
    "bootstrap data-dismiss": re.compile(r'data-(?:bs-)?dismiss="(?:modal|alert)"'),
    "bootstrap jquery plugin": re.compile(
        r"""\.(?:modal|tab|collapse|dropdown|tooltip|popover|carousel)\(['"]"""),
    "bootstrap plugin event": re.compile(r"\bbs\.(?:modal|tab|collapse|dropdown|tooltip)\b"),
    "bootstrap global object": re.compile(r"\bwindow\.bootstrap\b"),
}


def _sources() -> dict[str, str]:
    return {str(p.relative_to(_PAGES)): p.read_text(encoding="utf-8") for p in _PAGES.rglob("*.html")}


class PlatformComponentTests(SimpleTestCase):
    def test_no_page_uses_the_retired_plugin_api(self):
        offenders = {
            f"{name} in {filename}": pattern.findall(text)[:3]
            for filename, text in _sources().items()
            for name, pattern in RETIRED_PLUGIN_API.items()
            if pattern.search(text)
        }
        self.assertEqual(offenders, {}, f"templates still drive retired components: {offenders}")

    def test_modals_are_native_dialogs(self):
        """A div keeps its markup but loses the open/close behaviour entirely."""
        offenders = {
            filename: text.count('class="modal fade"')
            for filename, text in _sources().items()
            if 'class="modal fade"' in text
        }
        self.assertEqual(offenders, {}, f"modals that are not <dialog>: {offenders}")

    def test_the_scan_actually_reads_templates(self):
        """A silent zero would make the checks above pass for the wrong reason."""
        sources = _sources()
        self.assertGreater(len(sources), 10)
        self.assertTrue(any("<dialog" in text for text in sources.values()))
