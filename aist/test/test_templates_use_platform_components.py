"""
Our admin pages drive components the way the platform still supports.

DefectDojo 3.x stopped shipping the Bootstrap bundle. Its plugin API — the ``modal``
jQuery method, the ``bs.modal`` events, and the ``data-toggle``/``data-dismiss``
attributes — is gone, but markup using it still renders: a modal written as a plain div
simply shows up in the page, open and unclosable. Nothing fails server-side, so this is
checked here rather than left to a browser.

The same holds for what the bundle used to define rather than script. ``.collapse.show``
and the ``bootstrap-select`` widget were a stylesheet and a plugin that no longer exist,
and Font Awesome 6 dropped the version 4 outline names: in each case the page renders and
the panel stays shut, the list stays plain, or the glyph comes out empty.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

# Our own pages. base.html is the platform's template with our navigation added, so its
# remaining platform markup is the platform's business.
_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_PAGES = _TEMPLATES / "aist"
_BASE = _TEMPLATES / "base.html"
_COMPONENT_SCRIPT = "aist/js/platform-components.js"
_COMPONENT_SOURCE = Path(__file__).resolve().parent.parent / "static" / _COMPONENT_SCRIPT

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
    # The widget the bundle provided for selects. The platform ships Chosen instead, which
    # aistEnhanceSelect drives; a leftover call here is simply dead.
    "bootstrap-select widget": re.compile(r"\bselectpicker\b|\bbootstrap-select\b"),
    # Its change event, too: Chosen reports through a plain "change".
    "bootstrap-select event": re.compile(r"\bchanged\.bs\.select\b"),
}

# The platform's stylesheet marks an open collapsible panel ".collapse.in" and keeps
# ".collapse" at display:none !important. Bootstrap 3 also carried a ".show" utility that
# happened to force the panel open; nothing defines it now, so a panel toggled with "show"
# can never be opened again.
COLLAPSE_SHOW = re.compile(r"""\.collapse\.show\b|classList\.(?:add|remove|contains|toggle)\(['"]show['"]""")

# An open modal is a <dialog open>; it never carries the class the bundle used to add.
OPEN_MODAL_CLASS = re.compile(r"""\.modal\.in\b""")

# Font Awesome 6 keeps aliases for most version 4 names but dropped the "-o" outline
# family, which resolves to no glyph at all.
FA4_OUTLINE_NAME = re.compile(r"\bfa-[a-z0-9-]+-o\b")


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

    def test_collapsible_panels_use_the_class_the_platform_styles(self):
        """A panel toggled with "show" stays at display:none and never opens."""
        offenders = {
            filename: COLLAPSE_SHOW.findall(text)[:3]
            for filename, text in _sources().items()
            if COLLAPSE_SHOW.search(text)
        }
        self.assertEqual(offenders, {}, f"panels toggled with a class nothing defines: {offenders}")

    def test_an_open_modal_is_found_as_a_dialog(self):
        offenders = sorted(f for f, text in _sources().items() if OPEN_MODAL_CLASS.search(text))
        self.assertEqual(offenders, [], f"templates look for an open modal by a class it never gets: {offenders}")

    def test_no_page_names_a_dropped_font_awesome_icon(self):
        offenders = {
            filename: sorted(set(FA4_OUTLINE_NAME.findall(text)))
            for filename, text in _sources().items()
            if FA4_OUTLINE_NAME.search(text)
        }
        self.assertEqual(offenders, {}, f"icons that render as nothing: {offenders}")

    def test_the_scan_actually_reads_templates(self):
        """A silent zero would make the checks above pass for the wrong reason."""
        sources = _sources()
        self.assertGreater(len(sources), 10)
        self.assertTrue(any("<dialog" in text for text in sources.values()))


class ComponentScriptLoadingTests(SimpleTestCase):

    """
    The component helpers are defined before any page can call them.

    A page's own script runs while the body is still parsing, so a helper loaded at the end
    of the body is not there yet: the page dies on a ReferenceError and every handler it
    was about to wire up is lost.
    """

    def setUp(self):
        self.base = _BASE.read_text(encoding="utf-8")

    def test_the_component_script_is_loaded_before_the_content_block(self):
        script_at = self.base.find(_COMPONENT_SCRIPT)
        content_at = self.base.find("{% block content %}")
        self.assertNotEqual(script_at, -1, "base.html no longer loads the component script")
        self.assertNotEqual(content_at, -1, "base.html no longer has a content block")
        self.assertLess(script_at, content_at)

    def test_an_id_is_escaped_before_it_goes_into_a_selector(self):
        """A quote or a dot in a panel id would otherwise end the selector early."""
        source = _COMPONENT_SOURCE.read_text(encoding="utf-8")
        self.assertIn("CSS.escape(sibling.id)", source)
        self.assertNotIn("""'[aria-controls="' + sibling.id""", source)

    def test_the_component_script_is_versioned(self):
        """Without a version in the URL a browser keeps serving the copy it already has."""
        line = next(line for line in self.base.splitlines() if _COMPONENT_SCRIPT in line)
        self.assertIn(f'static_v "{_COMPONENT_SCRIPT}"', line)
