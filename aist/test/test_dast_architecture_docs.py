import re
from pathlib import Path
from urllib.parse import unquote

import yaml
from defusedxml import ElementTree
from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = tuple(
    path
    for path in sorted((PROJECT_ROOT / "docs").rglob("*.md"))
    if "plans" not in path.relative_to(PROJECT_ROOT / "docs").parts
)
READER_DOCUMENTS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "SECURITY.md",
    *DOCUMENTS,
)
MARKDOWN_LINK = re.compile(r"!?(?:\[[^]]*])\(([^)]+)\)")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
HTML_ANCHOR = re.compile(r"\b(?:id|name)=[\"']([^\"']+)[\"']")
SOURCE_LINE_LINK = re.compile(
    r"\]\([^)]*\.(?:py|js|jsx|ts|tsx|md|ya?ml|toml|sh):\d+",
    re.IGNORECASE,
)
EMPTY_IMAGE_ALT = re.compile(r"!\[\s*]\(")
DRAFTING_ARTIFACT = re.compile(
    r"\b(?:verify in code|while writing|adding real|implementation references|key files)\b"
    r"|:contentReference\[|oaicite",
    re.IGNORECASE,
)
SOURCE_INVENTORY = re.compile(
    r"(?:^|[\s(`])(?:\.\./)*(?:aist|client-ui|sast-combinator)/[^\s)`]+",
    re.MULTILINE,
)
CONCEPTUAL_DOCUMENTS = tuple(
    document
    for document in DOCUMENTS
    if document.relative_to(PROJECT_ROOT / "docs").parts[0]
    in {"architecture", "data-flows", "integrations", "product"}
)


def github_heading_slug(heading):
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", slug)


def document_anchors(document):
    content = document.read_text(encoding="utf-8")
    anchors = {github_heading_slug(heading) for heading in MARKDOWN_HEADING.findall(content)}
    anchors.update(HTML_ANCHOR.findall(content))
    return anchors


class DocumentationQualityTests(SimpleTestCase):
    def test_local_links_and_anchors_in_reader_documentation_exist(self):
        missing = []
        for document in READER_DOCUMENTS:
            self.assertTrue(document.is_file(), document)
            for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
                target_with_fragment = raw_target.strip().strip("<>")
                if target_with_fragment.startswith(("http://", "https://", "mailto:")):
                    continue
                target, _, fragment = target_with_fragment.partition("#")
                target = unquote(target)
                resolved = (document.parent / target).resolve() if target else document
                if not resolved.exists():
                    missing.append(f"{document.relative_to(PROJECT_ROOT)} -> {target}")
                    continue
                if fragment and resolved.suffix.lower() == ".md":
                    anchor = unquote(fragment)
                    if anchor not in document_anchors(resolved):
                        missing.append(
                            f"{document.relative_to(PROJECT_ROOT)} -> "
                            f"{target or document.name}#{anchor}",
                        )
        self.assertEqual(missing, [])

    def test_reader_documentation_does_not_link_to_source_line_numbers(self):
        violations = [
            str(document.relative_to(PROJECT_ROOT))
            for document in READER_DOCUMENTS
            if SOURCE_LINE_LINK.search(document.read_text(encoding="utf-8"))
        ]
        self.assertEqual(violations, [])

    def test_reader_documentation_has_accessible_image_alt_text(self):
        violations = [
            str(document.relative_to(PROJECT_ROOT))
            for document in READER_DOCUMENTS
            if EMPTY_IMAGE_ALT.search(document.read_text(encoding="utf-8"))
        ]
        self.assertEqual(violations, [])

    def test_reader_documentation_has_no_drafting_artifacts(self):
        violations = [
            str(document.relative_to(PROJECT_ROOT))
            for document in READER_DOCUMENTS
            if DRAFTING_ARTIFACT.search(document.read_text(encoding="utf-8"))
        ]
        self.assertEqual(violations, [])

    def test_conceptual_documentation_does_not_inventory_source_files(self):
        violations = [
            str(document.relative_to(PROJECT_ROOT))
            for document in CONCEPTUAL_DOCUMENTS
            if SOURCE_INVENTORY.search(document.read_text(encoding="utf-8"))
        ]
        self.assertEqual(violations, [])

    def test_repository_svg_assets_are_well_formed(self):
        invalid = []
        for svg in sorted((PROJECT_ROOT / "docs/assets").rglob("*.svg")):
            try:
                ElementTree.parse(svg)
            except ElementTree.ParseError as error:
                invalid.append(f"{svg.relative_to(PROJECT_ROOT)}: {error}")
        self.assertEqual(invalid, [])

    def test_reader_diagrams_have_accessible_names(self):
        violations = []
        for svg in sorted((PROJECT_ROOT / "docs/assets").glob("*.svg")):
            root = ElementTree.parse(svg).getroot()
            namespace = "{http://www.w3.org/2000/svg}"
            title = root.find(f"{namespace}title")
            description = root.find(f"{namespace}desc")
            if (
                root.get("role") != "img"
                or title is None
                or not (title.text or "").strip()
                or description is None
                or not (description.text or "").strip()
            ):
                violations.append(str(svg.relative_to(PROJECT_ROOT)))
        self.assertEqual(violations, [])

    def test_reader_diagrams_are_referenced_from_documentation(self):
        reader_text = "\n".join(
            document.read_text(encoding="utf-8") for document in READER_DOCUMENTS
        )
        orphaned = [
            str(svg.relative_to(PROJECT_ROOT))
            for svg in sorted((PROJECT_ROOT / "docs/assets").glob("*.svg"))
            if svg.name not in reader_text
        ]
        self.assertEqual(orphaned, [])

    def test_dast_is_one_standalone_provider_in_the_common_catalog(self):
        catalog_path = PROJECT_ROOT / "sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml"
        analyzers = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))["analyzers"]
        dast = [entry for entry in analyzers if entry.get("name") == "dast"]
        self.assertEqual(len(dast), 1)
        self.assertEqual(dast[0]["execution_type"], "dast")
        self.assertEqual(dast[0]["type"], "standalone")
